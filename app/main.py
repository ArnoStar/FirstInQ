import asyncio
import datetime as dt
import logging
import os
import sys
import time
from contextlib import asynccontextmanager, suppress
from itertools import groupby
from pathlib import Path
from typing import Any

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
from .scheduler import Scheduler
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
async def me(user: dict = Depends(current_user)):
    return user_to_dict(user)


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
    await timetable.refresh(manual=True)
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
    return [entry_out(r, scheduler) for r in rows]


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


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
