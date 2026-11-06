"""Telegram user-account (MTProto) clients: login flow, client pool, peers."""

import asyncio
import logging
import re
import secrets
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from cryptography.fernet import InvalidToken
from telethon import TelegramClient, errors, functions, types, utils
from telethon.sessions import StringSession

from .config import Settings
from .db import Database
from .security import SessionCipher

log = logging.getLogger(__name__)

DEVICE_INFO = dict(device_model="TelegramQ", system_version="Web scheduler", app_version="0.1.0")

# Errors meaning the stored authorization is gone (terminated from another
# device, account deleted, ...). The user has to log in again.
REVOKED_ERRORS = (
    errors.AuthKeyUnregisteredError,
    errors.SessionRevokedError,
    errors.SessionExpiredError,
    errors.UserDeactivatedError,
    errors.UserDeactivatedBanError,
    errors.AuthKeyDuplicatedError,
)


class NotAuthorized(Exception):
    """The stored Telegram session for this user is no longer valid."""


class UserFacingError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def user_to_dict(user: types.User | dict[str, Any]) -> dict[str, Any]:
    if isinstance(user, dict):
        return {k: user.get(k) for k in ("id", "first_name", "last_name", "username", "phone")}
    return {
        "id": user.id,
        "first_name": user.first_name or "",
        "last_name": user.last_name or "",
        "username": user.username,
        "phone": user.phone,
    }


def flood_message(e: errors.FloodWaitError) -> str:
    return f"Telegram is rate-limiting this action. Try again in {e.seconds} seconds."


class TelegramAccounts:
    """Keeps one connected TelegramClient per logged-in account."""

    def __init__(self, settings: Settings, db: Database, cipher: SessionCipher):
        self.settings = settings
        self.db = db
        self.cipher = cipher
        self._clients: dict[int, TelegramClient] = {}
        self._locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.on_revoked: Callable[[int], Awaitable[None]] | None = None

    def new_client(self, session: str = "") -> TelegramClient:
        return TelegramClient(
            StringSession(session),
            self.settings.api_id,
            self.settings.api_hash,
            receive_updates=False,
            **DEVICE_INFO,
        )

    async def get(self, user_id: int) -> TelegramClient:
        """Return a connected, authorized client for the user."""
        async with self._locks[user_id]:
            client = self._clients.get(user_id)
            if client is None:
                row = await self.db.get_user(user_id)
                if row is None:
                    raise NotAuthorized()
                session = self._decrypt_session(row)
                if session is not None:
                    client = self.new_client(session)
                    self._clients[user_id] = client
            authorized = False
            if client is not None:
                if not client.is_connected():
                    await client.connect()
                try:
                    authorized = await client.is_user_authorized()
                except REVOKED_ERRORS:
                    authorized = False
                if not authorized:
                    self._clients.pop(user_id, None)
                    await client.disconnect()
        if not authorized:
            await self.revoked(user_id)
            raise NotAuthorized()
        return client

    def _decrypt_session(self, row: dict[str, Any]) -> str | None:
        try:
            return self.cipher.decrypt(row["session_enc"])
        except InvalidToken:
            log.warning("Can't decrypt the stored session of user %s (SECRET_KEY changed?)", row["id"])
            return None

    async def revoked(self, user_id: int) -> None:
        log.warning("Telegram session for user %s is no longer valid", user_id)
        client = self._clients.pop(user_id, None)
        if client:
            await client.disconnect()
        if self.on_revoked:
            await self.on_revoked(user_id)

    async def install(self, client: TelegramClient) -> dict[str, Any]:
        """Store a freshly logged-in client, replacing (and logging out) any previous session."""
        me = await client.get_me()
        async with self._locks[me.id]:
            old_client = self._clients.get(me.id)
            old_row = await self.db.get_user(me.id)
            await self.db.upsert_user(
                id=me.id,
                first_name=me.first_name or "",
                last_name=me.last_name or "",
                username=me.username,
                phone=me.phone,
                session_enc=self.cipher.encrypt(client.session.save()),
            )
            self._clients[me.id] = client
        # Keep a single TelegramQ authorization per account: terminate the old one.
        if old_client is not None:
            asyncio.create_task(self._log_out_quietly(old_client))
        elif old_row is not None and (old_session := self._decrypt_session(old_row)) is not None:
            asyncio.create_task(self._log_out_quietly(self.new_client(old_session)))
        return user_to_dict(me)

    async def log_out(self, user_id: int) -> None:
        """Terminate the Telegram authorization for this user."""
        async with self._locks[user_id]:
            client = self._clients.pop(user_id, None)
            if client is None:
                row = await self.db.get_user(user_id)
                session = self._decrypt_session(row) if row else None
                if session is None:
                    return
                client = self.new_client(session)
        await self._log_out_quietly(client)

    @staticmethod
    async def _log_out_quietly(client: TelegramClient) -> None:
        try:
            if not client.is_connected():
                await client.connect()
            await client.log_out()
        except Exception:  # best effort; the session may already be dead
            log.info("Could not log out an old Telegram session", exc_info=True)
            await client.disconnect()

    async def close(self) -> None:
        clients = list(self._clients.values())
        self._clients.clear()
        await asyncio.gather(*(c.disconnect() for c in clients), return_exceptions=True)


# --- login flow -------------------------------------------------------------

SENT_CODE_VIA = {
    types.auth.SentCodeTypeApp: "in the Telegram app on your other devices",
    types.auth.SentCodeTypeSms: "by SMS",
    types.auth.SentCodeTypeSmsWord: "by SMS",
    types.auth.SentCodeTypeSmsPhrase: "by SMS",
    types.auth.SentCodeTypeFirebaseSms: "by SMS",
    types.auth.SentCodeTypeFragmentSms: "via Fragment",
    types.auth.SentCodeTypeCall: "by a phone call",
    types.auth.SentCodeTypeEmailCode: "to your login email",
}


@dataclass
class PendingLogin:
    client: TelegramClient
    phone: str
    phone_code_hash: str
    created: float
    needs_password: bool = False


class LoginFlows:
    """Multi-step phone-number login (code, then optional 2FA password)."""

    TTL_SECONDS = 600

    def __init__(self, accounts: TelegramAccounts):
        self.accounts = accounts
        self._pending: dict[str, PendingLogin] = {}

    async def start(self, phone: str) -> dict[str, Any]:
        phone = re.sub(r"[\s\-()]", "", phone)
        if not re.fullmatch(r"\+?\d{6,16}", phone):
            raise UserFacingError("Enter the phone number in international format, e.g. +491701234567.")
        client = self.accounts.new_client()
        await client.connect()
        try:
            sent = await client.send_code_request(phone)
        except errors.FloodWaitError as e:
            await client.disconnect()
            raise UserFacingError(flood_message(e), 429)
        except (errors.PhoneNumberInvalidError, errors.PhoneNumberUnoccupiedError):
            await client.disconnect()
            raise UserFacingError("That phone number is not registered on Telegram.")
        except (errors.ApiIdInvalidError, errors.ApiIdPublishedFloodError):
            await client.disconnect()
            raise UserFacingError("Telegram rejected TG_API_ID / TG_API_HASH. Check them at https://my.telegram.org.", 500)
        except errors.PhoneNumberBannedError:
            await client.disconnect()
            raise UserFacingError("This phone number is banned from Telegram.", 403)
        except errors.PhoneNumberFloodError:
            await client.disconnect()
            raise UserFacingError("Too many login attempts for this number. Try again later.", 429)
        except Exception:
            await client.disconnect()
            raise

        if isinstance(sent.type, types.auth.SentCodeTypeSetUpEmailRequired):
            await client.disconnect()
            raise UserFacingError("Telegram requires you to set up a login email first. Log in once with an official app.")

        login_id = secrets.token_urlsafe(24)
        self._pending[login_id] = PendingLogin(client, phone, sent.phone_code_hash, time.time())
        via = SENT_CODE_VIA.get(type(sent.type), "")
        return {"login_id": login_id, "step": "code", "sent_via": via,
                "code_length": getattr(sent.type, "length", None)}

    def _get(self, login_id: str) -> PendingLogin:
        pending = self._pending.get(login_id)
        if pending is None or time.time() - pending.created > self.TTL_SECONDS:
            raise UserFacingError("This login attempt expired. Start again.", 410)
        return pending

    async def submit_code(self, login_id: str, code: str) -> dict[str, Any]:
        pending = self._get(login_id)
        code = re.sub(r"\D", "", code)
        try:
            await pending.client.sign_in(pending.phone, code, phone_code_hash=pending.phone_code_hash)
        except errors.SessionPasswordNeededError:
            pending.needs_password = True
            hint = None
            try:
                pw = await pending.client(functions.account.GetPasswordRequest())
                hint = pw.hint
            except Exception:
                pass
            return {"step": "password", "hint": hint}
        except (errors.PhoneCodeInvalidError, errors.PhoneCodeEmptyError):
            raise UserFacingError("That code is not correct.")
        except errors.PhoneCodeExpiredError:
            await self._discard(login_id)
            raise UserFacingError("The code expired. Start again to get a new one.", 410)
        except errors.PhoneNumberUnoccupiedError:
            await self._discard(login_id)
            raise UserFacingError("There is no Telegram account for this number. Sign up in an official app first.")
        except errors.FloodWaitError as e:
            raise UserFacingError(flood_message(e), 429)
        return await self._finish(login_id)

    async def submit_password(self, login_id: str, password: str) -> dict[str, Any]:
        pending = self._get(login_id)
        if not pending.needs_password:
            raise UserFacingError("Enter the login code first.")
        try:
            await pending.client.sign_in(password=password)
        except errors.PasswordHashInvalidError:
            raise UserFacingError("Wrong password.")
        except errors.FloodWaitError as e:
            raise UserFacingError(flood_message(e), 429)
        except errors.PhonePasswordFloodError:
            raise UserFacingError("Too many password attempts. Try again later.", 429)
        return await self._finish(login_id)

    async def _finish(self, login_id: str) -> dict[str, Any]:
        pending = self._pending.pop(login_id)
        user = await self.accounts.install(pending.client)
        return {"step": "done", "user": user}

    async def _discard(self, login_id: str) -> None:
        pending = self._pending.pop(login_id, None)
        if pending:
            await pending.client.disconnect()

    async def purge_expired(self) -> None:
        now = time.time()
        for login_id, p in list(self._pending.items()):
            if now - p.created > self.TTL_SECONDS:
                await self._discard(login_id)

    async def close(self) -> None:
        for login_id in list(self._pending):
            await self._discard(login_id)


# --- peers ------------------------------------------------------------------

SELF_ALIASES = {"me", "self", "saved", "saved messages"}
GENERAL_TOPIC_ID = 1  # a forum's General topic; its messages carry no topic reference


def _peer_kind(entity: Any) -> str:
    if isinstance(entity, types.User):
        return "bot" if entity.bot else "user"
    if isinstance(entity, types.Channel) and entity.broadcast:
        return "channel"
    return "group"


def _can_post(entity: Any) -> bool:
    if isinstance(entity, types.Channel) and entity.broadcast:
        return bool(entity.creator or (entity.admin_rights and entity.admin_rights.post_messages))
    return True


def _is_forum(entity: Any) -> bool:
    return isinstance(entity, types.Channel) and bool(entity.forum)


async def list_dialogs(client: TelegramClient, limit: int = 300) -> list[dict[str, Any]]:
    out = [{"peer": "me", "name": "Saved Messages", "kind": "self", "username": None, "forum": False}]
    async for d in client.iter_dialogs(limit=limit):
        e = d.entity
        if isinstance(e, types.User) and (e.is_self or e.deleted):
            continue
        if not _can_post(e):
            continue
        out.append({
            "peer": str(d.id),
            "name": d.name or "(no name)",
            "kind": _peer_kind(e),
            "username": getattr(e, "username", None),
            "forum": _is_forum(e),
        })
    return out


# t.me/<username>[/<topic>[/<message>]]  and  t.me/c/<internal id>[/<topic>[/<message>]]
_LINK = re.compile(
    r"(?:https?://)?(?:t\.me|telegram\.me)/"
    r"(?:c/(?P<internal>\d+)|(?P<username>[A-Za-z0-9_]{4,}))"
    r"(?:/(?P<topic>\d+))?(?:/\d+)?/?(?:\?.*)?"
)


def parse_target(raw: str) -> tuple[str | int, int | None]:
    """Split user input into a chat reference and the topic id of a topic link, if any."""
    p = raw.strip()
    if p.lower() in SELF_ALIASES:
        return "me", None
    if re.fullmatch(r"-?\d+", p):
        return int(p), None
    m = _LINK.fullmatch(p)
    if m:
        chat = int(f"-100{m['internal']}") if m["internal"] else m["username"]
        return chat, int(m["topic"]) if m["topic"] else None
    return p, None


async def _get_entity(client: TelegramClient, target: str | int, raw: str) -> Any:
    if target == "":
        raise UserFacingError("Choose a recipient.")
    try:
        try:
            return await client.get_entity(target)
        except ValueError:
            if not isinstance(target, int):
                raise
            # Numeric ids only resolve when the entity is in the client's cache.
            await client.get_dialogs(limit=None)
            return await client.get_entity(target)
    except (ValueError, errors.UsernameInvalidError, errors.UsernameNotOccupiedError):
        raise UserFacingError(f"Could not find a chat for “{raw.strip()}”.")
    except errors.FloodWaitError as e:
        raise UserFacingError(flood_message(e), 429)


async def list_topics(client: TelegramClient, raw: str, query: str | None = None) -> list[dict[str, Any]]:
    """Topics of a forum supergroup (up to 100, most recently active first)."""
    target, _ = parse_target(raw)
    entity = await _get_entity(client, target, raw)
    if not _is_forum(entity):
        return []
    result = await client(functions.messages.GetForumTopicsRequest(
        peer=utils.get_input_peer(entity), offset_date=None, offset_id=0, offset_topic=0,
        limit=100, q=query or None))
    return [{"id": t.id, "title": t.title, "closed": bool(t.closed), "pinned": bool(t.pinned)}
            for t in result.topics if isinstance(t, types.ForumTopic)]


async def _topic_title(client: TelegramClient, input_peer: types.TypeInputPeer, topic_id: int) -> str:
    try:
        result = await client(functions.messages.GetForumTopicsByIDRequest(peer=input_peer, topics=[topic_id]))
    except errors.BadRequestError:
        result = None
    for t in getattr(result, "topics", []):
        if isinstance(t, types.ForumTopic) and t.id == topic_id:
            return t.title or ("General" if topic_id == GENERAL_TOPIC_ID else f"Topic {topic_id}")
    if topic_id == GENERAL_TOPIC_ID:
        return "General"
    raise UserFacingError("That topic doesn't exist (anymore).")


def _peer_fields(entity: Any) -> dict[str, Any]:
    input_peer = utils.get_input_peer(entity)
    if isinstance(input_peer, types.InputPeerSelf):
        return {"peer_type": "self", "peer_id": entity.id, "peer_hash": None, "peer_name": "Saved Messages"}
    name = utils.get_display_name(entity) or "(no name)"
    if isinstance(input_peer, types.InputPeerUser):
        return {"peer_type": "user", "peer_id": input_peer.user_id, "peer_hash": input_peer.access_hash, "peer_name": name}
    if isinstance(input_peer, types.InputPeerChat):
        return {"peer_type": "chat", "peer_id": input_peer.chat_id, "peer_hash": None, "peer_name": name}
    if isinstance(input_peer, types.InputPeerChannel):
        return {"peer_type": "channel", "peer_id": input_peer.channel_id, "peer_hash": input_peer.access_hash, "peer_name": name}
    raise UserFacingError("Unsupported recipient type.")


async def resolve_peer(client: TelegramClient, raw: str, topic_id: int | None = None) -> dict[str, Any]:
    """Resolve user input to storable peer fields (type, id, access hash, names, topic).

    ``topic_id`` picks a forum topic; without it, a topic link (t.me/group/5)
    selects its topic, and anything else goes to the General topic.
    """
    target, link_topic = parse_target(raw)
    entity = await _get_entity(client, target, raw)
    if not _can_post(entity):
        raise UserFacingError("You can't post in that channel.")

    fields = _peer_fields(entity)
    from_link = topic_id is None and link_topic is not None
    if topic_id is None:
        topic_id = link_topic
    if topic_id is not None and not _is_forum(entity):
        if from_link:
            raise UserFacingError("That link points to a single message. Use a link to the chat or to a forum topic.")
        raise UserFacingError("That chat has no topics.")
    fields["topic_id"] = topic_id
    fields["topic_name"] = (await _topic_title(client, utils.get_input_peer(entity), topic_id)
                            if topic_id is not None else None)
    return fields


def peer_ref(row: dict[str, Any]) -> str:
    """A value that ``resolve_peer`` accepts again (for "reuse" in the UI)."""
    if row["peer_type"] == "self":
        return "me"
    return str(utils.get_peer_id(input_peer_from_row(row)))


def input_peer_from_row(row: dict[str, Any]) -> types.TypeInputPeer:
    kind = row["peer_type"]
    if kind == "self":
        return types.InputPeerSelf()
    if kind == "user":
        return types.InputPeerUser(row["peer_id"], row["peer_hash"])
    if kind == "chat":
        return types.InputPeerChat(row["peer_id"])
    if kind == "channel":
        return types.InputPeerChannel(row["peer_id"], row["peer_hash"])
    raise ValueError(f"unknown peer type {kind!r}")
