"""Precise message scheduler.

Timeline for a message due at T (wall-clock, epoch seconds):

  T - PREPARE_LEAD  wake up, make sure the account's client is connected and
                    build the complete request (peer, parsed entities,
                    random_id) so nothing but I/O is left to do at T.
  T - PING_LEAD     ping Telegram a few times: warms the connection and
                    measures the round-trip time (RTT).
  T - lead          fire. With latency compensation lead = RTT/2, so the
                    request reaches Telegram's server at ~T; otherwise 0.

The final approach uses a coarse sleep followed by a short spin, which puts
the timer error well below a millisecond on an idle machine.
"""

import asyncio
import logging
import random
import time
from typing import Any

from telethon import TelegramClient, errors, functions, helpers, types
from telethon.extensions import html, markdown

from .db import Database
from .telegram import GENERAL_TOPIC_ID, REVOKED_ERRORS, NotAuthorized, TelegramAccounts, input_peer_from_row

log = logging.getLogger(__name__)

PREPARE_LEAD = 20.0
PING_LEAD = 3.0
PING_COUNT = 3
PING_GAP = 0.15
PING_SAFETY = 0.05  # pinging must be over this long before we have to fire
MAX_LEAD = 1.5
SPIN_WINDOW = 0.002
MAX_TEXT_LENGTH = 4096


async def sleep_until(target: float) -> None:
    """Sleep until wall-clock time ``target``.

    Long waits are chunked so system clock corrections (NTP) are picked up;
    the last couple of milliseconds are spent yielding in a tight loop because
    event-loop timers only have millisecond resolution.
    """
    while True:
        remaining = target - time.time()
        if remaining <= 0:
            return
        if remaining > SPIN_WINDOW:
            await asyncio.sleep(min(remaining - SPIN_WINDOW, 30.0))
        else:
            await asyncio.sleep(0)


def parse_text(text: str, parse_mode: str) -> tuple[str, list[types.TypeMessageEntity]]:
    if parse_mode == "md":
        message, entities = markdown.parse(text)
    elif parse_mode == "html":
        message, entities = html.parse(text)
    else:
        message, entities = text, []
    if not message.strip():
        raise ValueError("The message is empty.")
    if len(message) > MAX_TEXT_LENGTH:
        raise ValueError(f"The message is too long ({len(message)}/{MAX_TEXT_LENGTH} characters).")
    return message, entities


def build_request(row: dict[str, Any]) -> functions.messages.SendMessageRequest:
    message, entities = parse_text(row["text"], row["parse_mode"])
    topic_id = row.get("topic_id")
    reply_to = None
    if topic_id and topic_id != GENERAL_TOPIC_ID:
        # A forum topic is addressed as a reply to its first (service) message.
        reply_to = types.InputReplyToMessage(reply_to_msg_id=topic_id, top_msg_id=topic_id)
    return functions.messages.SendMessageRequest(
        peer=input_peer_from_row(row),
        message=message,
        entities=entities or None,
        no_webpage=not row["link_preview"],
        silent=bool(row["silent"]),
        reply_to=reply_to,
        random_id=helpers.generate_random_long(),
    )


def extract_sent(result: Any, random_id: int) -> tuple[int | None, float | None]:
    """Find the sent message's id and server date in a SendMessage response."""
    if isinstance(result, types.UpdateShortSentMessage):
        return result.id, result.date.timestamp()
    updates = getattr(result, "updates", None) or []
    msg_id = next((u.id for u in updates
                   if isinstance(u, types.UpdateMessageID) and u.random_id == random_id), None)
    for u in updates:
        if isinstance(u, (types.UpdateNewMessage, types.UpdateNewChannelMessage)):
            if msg_id is None or u.message.id == msg_id:
                return u.message.id, u.message.date.timestamp()
    return msg_id, None


def _ping(client: TelegramClient) -> Any:
    request = functions.PingRequest(ping_id=random.getrandbits(63))
    sender = getattr(client, "_sender", None)
    if sender is not None:
        # Bypass client(...): with receive_updates=False it wraps every request
        # in InvokeWithoutUpdates, which Telegram rejects for ping
        # (INPUT_METHOD_INVALID). The sender resolves Pong replies itself.
        return sender.send(request)
    return client(request)


async def measure_rtt(client: TelegramClient, send_at: float, count: int = PING_COUNT) -> float | None:
    """Lowest round-trip time to Telegram in seconds, or None if no ping came back in time.

    Never runs into the send: another ping is only started if it (plus the
    RTT/2 lead it implies) still fits before ``send_at``.
    """
    best = None
    for _ in range(count):
        if best is not None:
            if time.time() + PING_GAP + 1.5 * best + PING_SAFETY > send_at:
                break
            await asyncio.sleep(PING_GAP)
        budget = send_at - time.time() - PING_SAFETY
        if budget <= 0:
            break
        start = time.perf_counter()
        # Not cancelled on timeout: Telethon logs an error when a reply arrives
        # for a cancelled request, so a slow ping is simply left to finish.
        ping = asyncio.ensure_future(_ping(client))
        done, _ = await asyncio.wait({ping}, timeout=budget)
        if not done:
            ping.add_done_callback(lambda f: f.cancelled() or f.exception())
            break
        ping.result()
        rtt = time.perf_counter() - start
        best = rtt if best is None else min(best, rtt)
    return best


class Scheduler:
    def __init__(self, db: Database, accounts: TelegramAccounts, missed_grace: float):
        self.db = db
        self.accounts = accounts
        self.missed_grace = missed_grace
        self._tasks: dict[int, asyncio.Task] = {}
        self._firing: set[int] = set()

    async def start(self) -> None:
        now = time.time()
        for row in await self.db.pending_messages():
            if row["send_at"] < now - self.missed_grace:
                await self.db.update_message(
                    row["id"], status="missed", error="The server was not running at the scheduled time.")
            else:
                self.schedule(row)
        log.info("Scheduler started with %d pending message(s)", len(self._tasks))

    async def stop(self) -> None:
        # Let in-flight sends finish so their outcome is recorded; drop the rest.
        waiting = [t for i, t in self._tasks.items() if i not in self._firing]
        firing = [t for i, t in self._tasks.items() if i in self._firing]
        for t in waiting:
            t.cancel()
        await asyncio.gather(*waiting, return_exceptions=True)
        if firing:
            await asyncio.wait(firing, timeout=10)

    def schedule(self, row: dict[str, Any]) -> None:
        msg_id = row["id"]
        task = asyncio.create_task(self._run(row), name=f"message-{msg_id}")
        self._tasks[msg_id] = task
        task.add_done_callback(lambda t: self._tasks.pop(msg_id, None) if self._tasks.get(msg_id) is t else None)

    def is_firing(self, msg_id: int) -> bool:
        return msg_id in self._firing

    def cancel(self, msg_id: int) -> bool:
        """Stop a pending message. Returns False if it is already being sent."""
        if msg_id in self._firing:
            return False
        task = self._tasks.pop(msg_id, None)
        if task:
            task.cancel()
        return True

    async def fail_user(self, user_id: int, reason: str) -> None:
        """Fail every pending message of a user (e.g. their session was revoked)."""
        current = asyncio.current_task()
        for msg_id in await self.db.pending_message_ids(user_id):
            task = self._tasks.get(msg_id)
            if task and task is not current and msg_id not in self._firing:
                self._tasks.pop(msg_id, None)
                task.cancel()
            await self.db.update_message(msg_id, status="failed", error=reason)

    async def _run(self, row: dict[str, Any]) -> None:
        msg_id, user_id, send_at = row["id"], row["user_id"], row["send_at"]
        rtt = None
        lead = 0.0
        try:
            await sleep_until(send_at - PREPARE_LEAD)
            # Re-read: the text (place in line) may have changed since it was scheduled.
            row = await self.db.get_message(msg_id)
            if row is None or row["status"] != "pending":
                return
            client = await self.accounts.get(user_id)
            request = build_request(row)

            await sleep_until(send_at - PING_LEAD)
            try:
                rtt = await measure_rtt(client, send_at)
                if rtt is not None and row["compensate_latency"]:
                    lead = min(rtt / 2, MAX_LEAD)
            except REVOKED_ERRORS:
                raise
            except Exception:
                log.warning("Ping before message %s failed; sending without compensation", msg_id, exc_info=True)

            await sleep_until(send_at - lead)
            self._firing.add(msg_id)
            fired_at = time.time()
            result = await client(request)
            acked_at = time.time()
        except asyncio.CancelledError:
            raise
        except NotAuthorized:
            await self._failed(msg_id, "Your Telegram session was terminated. Log in again.")
        except REVOKED_ERRORS:
            await self.accounts.revoked(user_id)
            await self._failed(msg_id, "Your Telegram session was terminated. Log in again.")
        except errors.FloodWaitError as e:
            await self._failed(msg_id, f"Telegram rate limit: wait {e.seconds} s.")
        except errors.RPCError as e:
            await self._failed(msg_id, f"Telegram refused the message: {e.message}")
        except Exception as e:
            log.exception("Sending message %s failed", msg_id)
            await self._failed(msg_id, str(e) or type(e).__name__)
        else:
            tg_message_id, tg_date = extract_sent(result, request.random_id)
            await self.db.update_message(
                msg_id,
                status="sent",
                fired_at=fired_at,
                acked_at=acked_at,
                lead_ms=lead * 1000,
                rtt_ms=rtt * 1000 if rtt is not None else None,
                tg_message_id=tg_message_id,
                tg_date=tg_date,
                error=None,
            )
            log.info("Message %s sent: timer error %+.3f ms, RTT %s ms",
                     msg_id, (fired_at - (send_at - lead)) * 1000,
                     f"{rtt * 1000:.1f}" if rtt is not None else "n/a")
        finally:
            self._firing.discard(msg_id)

    async def _failed(self, msg_id: int, error: str) -> None:
        await self.db.update_message(msg_id, status="failed", error=error)
