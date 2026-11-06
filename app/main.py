import asyncio
import datetime as dt
import logging
import os
import re
import sys
import time
from contextlib import asynccontextmanager, suppress
from itertools import groupby
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from telethon import errors

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None

from .config import ConfigError, load_settings
from .db import Database
from .lines import Lines
from .scheduler import Scheduler, parse_text
from .security import SessionCipher, hash_token, load_or_create_key, new_token
from .telegram import (
    REVOKED_ERRORS,
    LoginFlows,
    NotAuthorized,
    TelegramAccounts,
    UserFacingError,
    flood_message,
    list_dialogs,
    list_topics,
    peer_ref,
    resolve_peer,
    user_to_dict,
)
from .timetable import MINSK, Timetable, today

# Fail fast with one readable line instead of a startup traceback.
try:
    load_settings()
except ConfigError as e:
    sys.exit(f"Configuration error: {e}")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("telethon").setLevel(logging.WARNING)
log = logging.getLogger("telegramq")

COOKIE_NAME = "tq_session"
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
MAX_SCHEDULE_AHEAD = 366 * 24 * 3600
REVOKED_MESSAGE = "Your Telegram session was terminated. Log in again."


def _lock_data_dir(data_dir: Path) -> int | None:
    """Hold an exclusive lock on the data directory for the process lifetime.

    Two instances on the same database (e.g. Docker and ``make run``) would
    each send every scheduled message, so the second one refuses to start.
    """
    if fcntl is None:
        return None
    fd = os.open(data_dir / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise RuntimeError(
            f"Another TelegramQ instance is already using {data_dir}. "
            "Stop it first — two instances would send every message twice."
        ) from None
    return fd


async def _janitor(db: Database, logins: LoginFlows) -> None:
    while True:
        await asyncio.sleep(60)
        try:
            await logins.purge_expired()
            await db.purge_expired_web_sessions()
        except Exception:
            log.exception("Cleanup failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    lock_fd = _lock_data_dir(settings.data_dir)
    db = Database(settings.db_path)
    await db.connect()
    cipher = SessionCipher(load_or_create_key(settings.secret_key, settings.data_dir))
    accounts = TelegramAccounts(settings, db, cipher)
    logins = LoginFlows(accounts)
    scheduler = Scheduler(db, accounts, settings.missed_grace_seconds)
    lines = Lines(db, scheduler)
    timetable = Timetable(db, settings.schedule_url, settings.schedule_refresh_minutes * 60, on_change=lines.sync)

    async def on_revoked(user_id: int) -> None:
        await lines.drop_user(user_id, REVOKED_MESSAGE)
        await db.delete_web_sessions_for_user(user_id)

    accounts.on_revoked = on_revoked
    await lines.start()
    await scheduler.start()
    await timetable.start()
    janitor = asyncio.create_task(_janitor(db, logins))

    app.state.settings = settings
    app.state.db = db
    app.state.accounts = accounts
    app.state.logins = logins
    app.state.scheduler = scheduler
    app.state.lines = lines
    app.state.timetable = timetable
    try:
        yield
    finally:
        janitor.cancel()
        with suppress(asyncio.CancelledError):
            await janitor
        await timetable.stop()
        await scheduler.stop()
        await logins.close()
        await accounts.close()
        await db.close()
        if lock_fd is not None:
            os.close(lock_fd)


app = FastAPI(title="TelegramQ", lifespan=lifespan, docs_url=None, redoc_url=None)


@app.middleware("http")
async def require_api_header(request: Request, call_next):
    # Cross-site pages can't set custom headers without a CORS preflight we never
    # allow, so this blocks CSRF on state-changing endpoints (on top of SameSite).
    if (request.url.path.startswith("/api/")
            and request.method not in ("GET", "HEAD", "OPTIONS")
            and request.headers.get("x-telegramq") != "1"):
        return JSONResponse({"detail": "Missing X-TelegramQ header."}, status_code=403)
    return await call_next(request)


@app.exception_handler(UserFacingError)
async def _user_error(request: Request, exc: UserFacingError):
    return JSONResponse({"detail": exc.message}, status_code=exc.status)


@app.exception_handler(NotAuthorized)
async def _not_authorized(request: Request, exc: NotAuthorized):
    return _logged_out_response(request)


async def _revoked(request: Request, exc: Exception):
    user_id = getattr(request.state, "user_id", None)
    if user_id is not None:
        await request.app.state.accounts.revoked(user_id)
    return _logged_out_response(request)


for _error in REVOKED_ERRORS:
    app.add_exception_handler(_error, _revoked)


@app.exception_handler(errors.FloodWaitError)
async def _flood(request: Request, exc: errors.FloodWaitError):
    return JSONResponse({"detail": flood_message(exc)}, status_code=429)


@app.exception_handler(errors.RPCError)
async def _rpc_error(request: Request, exc: errors.RPCError):
    log.warning("Telegram error on %s: %s", request.url.path, exc)
    return JSONResponse({"detail": f"Telegram error: {exc.message}"}, status_code=502)


def _logged_out_response(request: Request) -> JSONResponse:
    response = JSONResponse({"detail": REVOKED_MESSAGE}, status_code=401)
    response.delete_cookie(COOKIE_NAME, path="/")
    return response


# --- auth -------------------------------------------------------------------

async def current_user(request: Request) -> dict[str, Any]:
    token = request.cookies.get(COOKIE_NAME)
    user = await request.app.state.db.get_web_session_user(hash_token(token)) if token else None
    if user is None:
        raise HTTPException(401, "Not logged in.")
    request.state.user_id = user["id"]
    return user


async def _start_web_session(request: Request, response: Response, user_id: int) -> None:
    settings = request.app.state.settings
    ttl = settings.web_session_days * 86400
    token = new_token()
    await request.app.state.db.create_web_session(hash_token(token), user_id, ttl)
    response.set_cookie(COOKIE_NAME, token, max_age=ttl, httponly=True, samesite="lax",
                        secure=settings.cookie_secure, path="/")


class PhoneIn(BaseModel):
    phone: str = Field(min_length=5, max_length=32)


class CodeIn(BaseModel):
    login_id: str = Field(max_length=64)
    code: str = Field(min_length=1, max_length=16)


class PasswordIn(BaseModel):
    login_id: str = Field(max_length=64)
    password: str = Field(min_length=1, max_length=256)


class LogoutIn(BaseModel):
    disconnect_telegram: bool = False


@app.get("/api/time")
async def server_time():
    return {"now": time.time() * 1000}


@app.get("/api/me")
async def me(request: Request, user: dict = Depends(current_user)):
    return {**user_to_dict(user), "is_admin": is_admin(request, user)}


@app.post("/api/auth/phone")
async def auth_phone(body: PhoneIn, request: Request):
    return await request.app.state.logins.start(body.phone)


@app.post("/api/auth/code")
async def auth_code(body: CodeIn, request: Request, response: Response):
    result = await request.app.state.logins.submit_code(body.login_id, body.code)
    if result["step"] == "done":
        await _start_web_session(request, response, result["user"]["id"])
    return result


@app.post("/api/auth/password")
async def auth_password(body: PasswordIn, request: Request, response: Response):
    result = await request.app.state.logins.submit_password(body.login_id, body.password)
    await _start_web_session(request, response, result["user"]["id"])
    return result


@app.post("/api/auth/logout")
async def logout(body: LogoutIn, request: Request, response: Response, user: dict = Depends(current_user)):
    state = request.app.state
    if body.disconnect_telegram:
        await state.lines.drop_user(user["id"], "Cancelled: Telegram account was disconnected.")
        await state.accounts.log_out(user["id"])
        await state.db.delete_user(user["id"])  # cascades to web sessions and messages
    else:
        await state.db.delete_web_session(hash_token(request.cookies[COOKIE_NAME]))
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"ok": True}


# --- timetable ----------------------------------------------------------------

def ms(v: float | None) -> float | None:
    return v * 1000 if v is not None else None


class GroupIn(BaseModel):
    group: str | None = Field(default=None, max_length=32)


def lesson_out(row: dict[str, Any], entries: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "day": row["day"],
        "pair": row["pair"],
        "start": ms(row["start_at"]),
        "end": ms(row["end_at"]),
        "send_at": ms(row["send_at"]),
        "subject": row["subject"],
        "subgroup": row["subgroup"],
        "teachers": row["teachers"],
        "rooms": row["rooms"],
        "details": row["details"],
        "places": [{"id": e["id"], "place": e["place"], "status": e["status"], "peer_name": e["peer_name"],
                    "topic_name": e["topic_name"]} for e in entries],
    }


@app.get("/api/groups")
async def groups(request: Request, user: dict = Depends(current_user)):
    state = request.app.state
    return {"groups": await state.db.groups(), "selected": user["group_name"],
            "timetable": state.timetable.status()}


@app.put("/api/me/group")
async def set_group(body: GroupIn, request: Request, user: dict = Depends(current_user)):
    db = request.app.state.db
    group = body.group.strip() if body.group else None
    if group and group not in await db.groups():
        raise UserFacingError("There is no such group in the timetable.")
    await db.set_user_group(user["id"], group or None)
    return {"group": group or None}


@app.get("/api/schedule")
async def schedule(request: Request, group: str | None = None, user: dict = Depends(current_user)):
    """The group's classes from today on, with the user's places in their lines."""
    state = request.app.state
    group = group or user["group_name"]
    rows = await state.db.group_lessons(group, today().isoformat()) if group else []
    entries = await state.db.user_line_entries(user["id"], [r["lesson_key"] for r in rows])
    by_key: dict[str, list[dict[str, Any]]] = {}
    for e in entries:
        by_key.setdefault(e["lesson_key"], []).append(e)
    days = [{"day": day, "lessons": [lesson_out(r, by_key.get(r["lesson_key"], [])) for r in day_rows]}
            for day, day_rows in groupby(rows, key=lambda r: r["day"])]
    return {"group": group, "days": days, "utc_offset_minutes": MINSK.utcoffset(None) // dt.timedelta(minutes=1),
            "timetable": state.timetable.status()}


@app.post("/api/schedule/refresh")
async def refresh_schedule(request: Request, user: dict = Depends(current_user)):
    timetable = request.app.state.timetable
    await timetable.refresh(min_gap=Timetable.MANUAL_REFRESH_GAP)
    return timetable.status()


# --- chats & lines ----------------------------------------------------------------

class JoinLine(BaseModel):
    lesson_id: int
    peer: str = Field(min_length=1, max_length=256)
    topic_id: int | None = Field(default=None, ge=1, description="Forum topic; omit for General / non-forums")


def entry_out(row: dict[str, Any], scheduler: Scheduler) -> dict[str, Any]:
    out = {
        "id": row["id"],
        "peer_name": row["peer_name"],
        "peer_type": row["peer_type"],
        "peer_ref": peer_ref(row),
        "topic_id": row["topic_id"],
        "topic_name": row["topic_name"],
        "text": row["text"],
        "place": row["place"],
        "subject": row["subject"],
        "group_name": row["group_name"],
        "pair": row["pair"],
        "lesson_start": ms(row["lesson_start"]),
        "status": "sending" if row["status"] == "pending" and scheduler.is_firing(row["id"]) else row["status"],
        "send_at": ms(row["send_at"]),
        "created_at": ms(row["created_at"]),
        "fired_at": ms(row["fired_at"]),
        "acked_at": ms(row["acked_at"]),
        "lead_ms": row["lead_ms"],
        "rtt_ms": row["rtt_ms"],
        "tg_message_id": row["tg_message_id"],
        "tg_date": ms(row["tg_date"]),  # Telegram's own timestamp (whole seconds)
        "error": row["error"],
        "timer_error_ms": None,
        "arrival_error_ms": None,
    }
    if row["fired_at"] is not None:
        planned_fire = row["send_at"] - (row["lead_ms"] or 0) / 1000
        out["timer_error_ms"] = (row["fired_at"] - planned_fire) * 1000
        if row["rtt_ms"] is not None:
            est_arrival = row["fired_at"] + row["rtt_ms"] / 2000
            out["arrival_error_ms"] = (est_arrival - row["send_at"]) * 1000
    return out


@app.get("/api/dialogs")
async def dialogs(request: Request, user: dict = Depends(current_user)):
    client = await request.app.state.accounts.get(user["id"])
    return await list_dialogs(client)


@app.get("/api/topics")
async def topics(peer: str, request: Request, q: str | None = None, user: dict = Depends(current_user)):
    client = await request.app.state.accounts.get(user["id"])
    return await list_topics(client, peer, q)


@app.get("/api/lines")
async def my_lines(request: Request, user: dict = Depends(current_user)):
    scheduler = request.app.state.scheduler
    rows = await request.app.state.db.list_messages(user["id"])
    return [entry_out(r, scheduler) for r in rows if r["lesson_key"]]  # admins' own messages are in the panel


@app.post("/api/lines", status_code=201)
async def join_line(body: JoinLine, request: Request, user: dict = Depends(current_user)):
    state = request.app.state
    lesson = await state.db.get_lesson(body.lesson_id)
    if lesson is None:
        raise UserFacingError("That class is no longer in the timetable. Reload the page.", 404)
    if lesson["send_at"] <= time.time():
        raise UserFacingError("This line has already opened, so it's too late to take a place.", 409)
    client = await state.accounts.get(user["id"])
    peer = await resolve_peer(client, body.peer, body.topic_id)
    row = await state.lines.join(user["id"], lesson, peer)
    return entry_out(row, state.scheduler)


@app.post("/api/lines/{entry_id}/cancel")
async def leave_line(entry_id: int, request: Request, user: dict = Depends(current_user)):
    state = request.app.state
    row = await state.lines.leave(user["id"], entry_id)
    return entry_out(row, state.scheduler)


@app.delete("/api/lines/{entry_id}")
async def remove_entry(entry_id: int, request: Request, user: dict = Depends(current_user)):
    state = request.app.state
    row = await state.db.get_message(entry_id, user["id"])
    if row is None:
        raise HTTPException(404, "Not found.")
    if row["status"] == "pending":
        raise UserFacingError("Leave the line before removing it.", 409)
    await state.db.delete_message(entry_id)
    return {"ok": True}


# --- admin ------------------------------------------------------------------------

def is_admin(request: Request, user: dict[str, Any]) -> bool:
    """Admins are listed in ADMINS (.env) by Telegram user id or phone number."""
    admins = request.app.state.settings.admins
    phone = re.sub(r"\D", "", user.get("phone") or "")
    return str(user["id"]) in admins or (bool(phone) and phone in admins)


async def current_admin(request: Request, user: dict = Depends(current_user)) -> dict[str, Any]:
    if not is_admin(request, user):
        raise HTTPException(403, "Only admins can do this.")
    return user


def display_name(first: str | None, last: str | None, username: str | None) -> str:
    return " ".join(x for x in (first, last) if x) or (f"@{username}" if username else "(no name)")


def admin_entry_out(row: dict[str, Any], scheduler: Scheduler) -> dict[str, Any]:
    return {
        **entry_out(row, scheduler),
        "kind": "line" if row["lesson_key"] else "message",
        "user_id": row["user_id"],
        "user_name": display_name(row.get("user_first_name"), row.get("user_last_name"), row.get("user_username")),
        "parse_mode": row["parse_mode"],
        "silent": bool(row["silent"]),
        "link_preview": bool(row["link_preview"]),
        "compensate_latency": bool(row["compensate_latency"]),
    }


class NewMessage(BaseModel):
    peer: str = Field(min_length=1, max_length=256)
    text: str = Field(min_length=1, max_length=20000)
    send_at: float = Field(description="Unix epoch in milliseconds")
    topic_id: int | None = Field(default=None, ge=1, description="Forum topic; omit for General / non-forums")
    parse_mode: Literal["md", "html", "none"] = "md"
    silent: bool = False
    link_preview: bool = True
    compensate_latency: bool = True


class SettingsIn(BaseModel):
    line_timing: Literal["arrive", "exact"]


@app.get("/api/admin/overview")
async def admin_overview(request: Request, admin: dict = Depends(current_admin)):
    state = request.app.state
    pending = await state.db.pending_messages()
    return {
        "users": len(await state.db.list_users()),
        "pending": len(pending),
        "pending_lines": sum(1 for r in pending if r["lesson_key"]),
        "next_send_at": ms(pending[0]["send_at"]) if pending else None,
        "timetable": state.timetable.status(),
        "line_timing": state.lines.timing,
    }


@app.get("/api/admin/messages")
async def admin_messages(request: Request, upcoming: bool = True, admin: dict = Depends(current_admin)):
    state = request.app.state
    return [admin_entry_out(r, state.scheduler) for r in await state.db.all_messages(upcoming)]


@app.post("/api/admin/messages", status_code=201)
async def admin_schedule_message(body: NewMessage, request: Request, admin: dict = Depends(current_admin)):
    """Schedule a free-form message, sent from the admin's own account."""
    state = request.app.state
    send_at = body.send_at / 1000
    now = time.time()
    if send_at < now - 1:
        raise UserFacingError("That time is in the past.")
    if send_at > now + MAX_SCHEDULE_AHEAD:
        raise UserFacingError("You can schedule at most one year ahead.")
    try:
        parse_text(body.text, body.parse_mode)
    except ValueError as e:
        raise UserFacingError(str(e))
    client = await state.accounts.get(admin["id"])
    peer = await resolve_peer(client, body.peer, body.topic_id)
    row = await state.db.create_message(
        user_id=admin["id"],
        **peer,
        text=body.text,
        parse_mode=body.parse_mode,
        silent=int(body.silent),
        link_preview=int(body.link_preview),
        compensate_latency=int(body.compensate_latency),
        send_at=send_at,
        status="pending",
    )
    state.scheduler.schedule(row)
    names = {"user_first_name": admin["first_name"], "user_last_name": admin["last_name"],
             "user_username": admin["username"]}
    return admin_entry_out({**row, **names}, state.scheduler)


@app.post("/api/admin/messages/{msg_id}/cancel")
async def admin_cancel_message(msg_id: int, request: Request, admin: dict = Depends(current_admin)):
    state = request.app.state
    row = await state.db.get_message(msg_id)
    own = row is not None and row["user_id"] == admin["id"]
    await state.lines.cancel(msg_id, error=None if own else "Cancelled by an admin.")
    return {"ok": True}


@app.delete("/api/admin/messages/{msg_id}")
async def admin_remove_message(msg_id: int, request: Request, admin: dict = Depends(current_admin)):
    db = request.app.state.db
    row = await db.get_message(msg_id)
    if row is None:
        raise HTTPException(404, "Not found.")
    if row["status"] == "pending":
        raise UserFacingError("Cancel the message before removing it.", 409)
    await db.delete_message(msg_id)
    return {"ok": True}


@app.get("/api/admin/users")
async def admin_users(request: Request, admin: dict = Depends(current_admin)):
    return [{
        **user_to_dict(u),
        "name": display_name(u["first_name"], u["last_name"], u["username"]),
        "group_name": u["group_name"],
        "pending": u["pending"],
        "sent": u["sent"],
        "created_at": ms(u["created_at"]),
        "is_admin": is_admin(request, u),
        "is_you": u["id"] == admin["id"],
    } for u in await request.app.state.db.list_users()]


@app.delete("/api/admin/users/{user_id}")
async def admin_remove_user(user_id: int, request: Request, admin: dict = Depends(current_admin)):
    """Disconnect a user: give up their places, end TelegramQ's Telegram session, delete their data."""
    state = request.app.state
    if user_id == admin["id"]:
        raise UserFacingError("To disconnect your own account, use the account menu.", 409)
    if await state.db.get_user(user_id) is None:
        raise HTTPException(404, "No such user.")
    await state.lines.drop_user(user_id, "Cancelled: an admin removed this account.")
    await state.accounts.log_out(user_id)
    await state.db.delete_user(user_id)  # cascades to web sessions and messages
    return {"ok": True}


async def _timetable_out(state) -> dict[str, Any]:
    days = [{**d, "fetched_at": ms(d["fetched_at"])} for d in await state.db.schedule_day_stats()]
    return {**state.timetable.status(), "interval_minutes": state.timetable.interval / 60, "days": days}


@app.get("/api/admin/timetable")
async def admin_timetable(request: Request, admin: dict = Depends(current_admin)):
    return await _timetable_out(request.app.state)


@app.post("/api/admin/timetable/refresh")
async def admin_refresh_timetable(request: Request, admin: dict = Depends(current_admin)):
    state = request.app.state
    if not state.timetable.url:
        raise UserFacingError("Reading the timetable is turned off (SCHEDULE_URL is empty).", 409)
    await state.timetable.refresh()
    return await _timetable_out(state)


@app.post("/api/admin/timetable/days/{day}/reload")
async def admin_reload_day(day: dt.date, request: Request, admin: dict = Depends(current_admin)):
    state = request.app.state
    try:
        classes = await state.timetable.reload_day(day)
    except LookupError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        raise UserFacingError(f"Could not re-read {day:%d.%m.%Y}: {e}", 502)
    return {**await _timetable_out(state), "classes": classes}


@app.put("/api/admin/settings")
async def admin_settings(body: SettingsIn, request: Request, admin: dict = Depends(current_admin)):
    await request.app.state.lines.set_timing(body.line_timing)
    return {"line_timing": body.line_timing}


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
