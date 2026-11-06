"""SQLite storage. All timestamps are Unix epoch seconds as floats (µs resolution)."""

import time
from pathlib import Path
from typing import Any

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id           INTEGER PRIMARY KEY,          -- Telegram user id
    first_name   TEXT NOT NULL DEFAULT '',
    last_name    TEXT NOT NULL DEFAULT '',
    username     TEXT,
    phone        TEXT,
    session_enc  TEXT NOT NULL,                -- Fernet-encrypted Telethon StringSession
    group_name   TEXT,                         -- the user's study group, e.g. 3К9393
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS web_sessions (
    token_hash  TEXT PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at  REAL NOT NULL,
    expires_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id             INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    peer_type           TEXT NOT NULL,         -- self | user | chat | channel
    peer_id             INTEGER NOT NULL,
    peer_hash           INTEGER,
    peer_name           TEXT NOT NULL,
    topic_id            INTEGER,               -- forum topic; NULL = not a forum / General
    topic_name          TEXT,
    text                TEXT NOT NULL,
    parse_mode          TEXT NOT NULL DEFAULT 'md',
    silent              INTEGER NOT NULL DEFAULT 0,
    link_preview        INTEGER NOT NULL DEFAULT 1,
    compensate_latency  INTEGER NOT NULL DEFAULT 1,
    send_at             REAL NOT NULL,
    status              TEXT NOT NULL DEFAULT 'pending',  -- pending | sent | failed | cancelled | missed
    created_at          REAL NOT NULL,
    fired_at            REAL,                  -- when the request left our process
    acked_at            REAL,                  -- when Telegram's reply arrived
    lead_ms             REAL,                  -- how early we fired to compensate for latency
    rtt_ms              REAL,                  -- measured round-trip to Telegram before sending
    tg_message_id       INTEGER,
    tg_date             REAL,                  -- server-side message date (1 s resolution)
    error               TEXT,
    -- A place in line for a class. The line is (lesson_key, chat, topic).
    lesson_key          TEXT,
    group_name          TEXT,
    subject             TEXT,
    pair                INTEGER,
    lesson_start        REAL,
    place               INTEGER
);

CREATE INDEX IF NOT EXISTS idx_messages_user ON messages(user_id, send_at);
CREATE INDEX IF NOT EXISTS idx_messages_status ON messages(status);
CREATE INDEX IF NOT EXISTS idx_messages_line ON messages(lesson_key, peer_type, peer_id);

-- One parsed timetable PDF per day.
CREATE TABLE IF NOT EXISTS schedule_days (
    day         TEXT PRIMARY KEY,              -- ISO date
    url         TEXT NOT NULL,
    fetched_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS lessons (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    lesson_key  TEXT NOT NULL UNIQUE,          -- stable across re-downloads (see timetable.Lesson.key)
    day         TEXT NOT NULL,
    group_name  TEXT NOT NULL,
    pair        INTEGER NOT NULL,
    start_at    REAL NOT NULL,
    end_at      REAL NOT NULL,
    send_at     REAL NOT NULL,                 -- when line messages for it are sent
    subject     TEXT NOT NULL,
    subgroup    INTEGER,
    teachers    TEXT NOT NULL DEFAULT '',
    rooms       TEXT NOT NULL DEFAULT '',
    details     TEXT NOT NULL DEFAULT ''
);

-- Admin settings (see Lines.timing).
CREATE TABLE IF NOT EXISTS settings (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_lessons_group ON lessons(group_name, start_at);
CREATE INDEX IF NOT EXISTS idx_lessons_day ON lessons(day);
"""

# Columns added after the first release: (table, column, definition).
ADDED_COLUMNS = [
    ("messages", "topic_id", "INTEGER"),
    ("messages", "topic_name", "TEXT"),
    ("users", "group_name", "TEXT"),
    ("messages", "lesson_key", "TEXT"),
    ("messages", "group_name", "TEXT"),
    ("messages", "subject", "TEXT"),
    ("messages", "pair", "INTEGER"),
    ("messages", "lesson_start", "REAL"),
    ("messages", "place", "INTEGER"),
]

# Entries that hold a place in a line.
ACTIVE = ('pending', 'sent')


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA journal_mode=WAL")
        await self.conn.execute("PRAGMA foreign_keys=ON")
        await self._add_missing_columns()  # before SCHEMA, whose indexes may use them
        await self.conn.executescript(SCHEMA)
        await self.conn.commit()

    async def _add_missing_columns(self) -> None:
        for table, column, definition in ADDED_COLUMNS:
            async with self.conn.execute(f"PRAGMA table_info({table})") as cur:
                existing = {row["name"] for row in await cur.fetchall()}
            if existing and column not in existing:  # a missing table is created by SCHEMA
                await self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()
            self.conn = None

    async def _one(self, sql: str, params: tuple = ()) -> dict[str, Any] | None:
        async with self.conn.execute(sql, params) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def _all(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        async with self.conn.execute(sql, params) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def _exec(self, sql: str, params: tuple = ()) -> int:
        cur = await self.conn.execute(sql, params)
        await self.conn.commit()
        return cur.lastrowid

    # --- users -------------------------------------------------------------

    async def upsert_user(self, *, id: int, first_name: str, last_name: str,
                          username: str | None, phone: str | None, session_enc: str) -> None:
        now = time.time()
        await self._exec(
            """INSERT INTO users (id, first_name, last_name, username, phone, session_enc, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                 first_name=excluded.first_name, last_name=excluded.last_name,
                 username=excluded.username, phone=excluded.phone,
                 session_enc=excluded.session_enc, updated_at=excluded.updated_at""",
            (id, first_name, last_name, username, phone, session_enc, now, now),
        )

    async def get_user(self, user_id: int) -> dict[str, Any] | None:
        return await self._one("SELECT * FROM users WHERE id = ?", (user_id,))

    async def delete_user(self, user_id: int) -> None:
        await self._exec("DELETE FROM users WHERE id = ?", (user_id,))

    async def set_user_group(self, user_id: int, group_name: str | None) -> None:
        await self._exec("UPDATE users SET group_name = ?, updated_at = ? WHERE id = ?",
                         (group_name, time.time(), user_id))

    # --- web sessions ------------------------------------------------------

    async def create_web_session(self, token_hash: str, user_id: int, ttl_seconds: float) -> None:
        now = time.time()
        await self._exec(
            "INSERT INTO web_sessions (token_hash, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (token_hash, user_id, now, now + ttl_seconds),
        )

    async def get_web_session_user(self, token_hash: str) -> dict[str, Any] | None:
        return await self._one(
            """SELECT u.* FROM web_sessions s JOIN users u ON u.id = s.user_id
               WHERE s.token_hash = ? AND s.expires_at > ?""",
            (token_hash, time.time()),
        )

    async def delete_web_session(self, token_hash: str) -> None:
        await self._exec("DELETE FROM web_sessions WHERE token_hash = ?", (token_hash,))

    async def delete_web_sessions_for_user(self, user_id: int) -> None:
        await self._exec("DELETE FROM web_sessions WHERE user_id = ?", (user_id,))

    async def purge_expired_web_sessions(self) -> None:
        await self._exec("DELETE FROM web_sessions WHERE expires_at <= ?", (time.time(),))

    # --- messages ----------------------------------------------------------

    async def create_message(self, **fields: Any) -> dict[str, Any]:
        fields.setdefault("created_at", time.time())
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        msg_id = await self._exec(f"INSERT INTO messages ({cols}) VALUES ({marks})", tuple(fields.values()))
        return await self.get_message(msg_id)

    async def get_message(self, msg_id: int, user_id: int | None = None) -> dict[str, Any] | None:
        if user_id is None:
            return await self._one("SELECT * FROM messages WHERE id = ?", (msg_id,))
        return await self._one("SELECT * FROM messages WHERE id = ? AND user_id = ?", (msg_id, user_id))

    async def list_messages(self, user_id: int) -> list[dict[str, Any]]:
        return await self._all("SELECT * FROM messages WHERE user_id = ? ORDER BY send_at", (user_id,))

    async def pending_messages(self) -> list[dict[str, Any]]:
        return await self._all("SELECT * FROM messages WHERE status = 'pending' ORDER BY send_at")

    async def pending_message_ids(self, user_id: int) -> list[int]:
        rows = await self._all("SELECT id FROM messages WHERE user_id = ? AND status = 'pending'", (user_id,))
        return [r["id"] for r in rows]

    async def all_messages(self, upcoming: bool, limit: int = 500) -> list[dict[str, Any]]:
        """Every user's messages, with the sender's name: pending ones soonest first, others latest first."""
        where, order = ("m.status = 'pending'", "m.send_at") if upcoming else ("m.status != 'pending'", "m.send_at DESC")
        return await self._all(
            f"""SELECT m.*, u.first_name AS user_first_name, u.last_name AS user_last_name,
                      u.username AS user_username
               FROM messages m JOIN users u ON u.id = m.user_id
               WHERE {where} ORDER BY {order} LIMIT ?""", (limit,))

    async def update_message(self, msg_id: int, **fields: Any) -> None:
        sets = ", ".join(f"{k} = ?" for k in fields)
        await self._exec(f"UPDATE messages SET {sets} WHERE id = ?", (*fields.values(), msg_id))

    async def delete_message(self, msg_id: int) -> None:
        await self._exec("DELETE FROM messages WHERE id = ?", (msg_id,))

    async def list_users(self) -> list[dict[str, Any]]:
        return await self._all(
            """SELECT u.id, u.first_name, u.last_name, u.username, u.phone, u.group_name, u.created_at, u.updated_at,
                      (SELECT COUNT(*) FROM messages m WHERE m.user_id = u.id AND m.status = 'pending') AS pending,
                      (SELECT COUNT(*) FROM messages m WHERE m.user_id = u.id AND m.status = 'sent') AS sent
               FROM users u ORDER BY u.created_at""")

    # --- settings --------------------------------------------------------------

    async def get_setting(self, key: str, default: str) -> str:
        row = await self._one("SELECT value FROM settings WHERE key = ?", (key,))
        return row["value"] if row else default

    async def set_setting(self, key: str, value: str) -> None:
        await self._exec("INSERT INTO settings (key, value) VALUES (?, ?) "
                         "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))

    # --- lines ---------------------------------------------------------------

    async def line_entries(self, row: dict[str, Any]) -> list[dict[str, Any]]:
        """Entries holding a place in the same line as ``row`` (same class, chat and topic), in join order."""
        return await self._all(
            f"""SELECT * FROM messages
               WHERE lesson_key = ? AND peer_type = ? AND peer_id = ? AND IFNULL(topic_id, 1) = IFNULL(?, 1)
                 AND status IN {ACTIVE} ORDER BY id""",
            (row["lesson_key"], row["peer_type"], row["peer_id"], row["topic_id"]))

    async def pending_line_entries(self, user_id: int | None = None) -> list[dict[str, Any]]:
        if user_id is None:
            return await self._all("SELECT * FROM messages WHERE status = 'pending' AND lesson_key IS NOT NULL")
        return await self._all("SELECT * FROM messages WHERE status = 'pending' AND lesson_key IS NOT NULL "
                               "AND user_id = ?", (user_id,))

    async def set_pending_line_compensation(self, compensate: bool) -> None:
        await self._exec("UPDATE messages SET compensate_latency = ? WHERE status = 'pending' AND lesson_key IS NOT NULL",
                         (int(compensate),))

    async def user_line_entries(self, user_id: int, lesson_keys: list[str]) -> list[dict[str, Any]]:
        if not lesson_keys:
            return []
        marks = ", ".join("?" for _ in lesson_keys)
        return await self._all(
            f"SELECT * FROM messages WHERE user_id = ? AND lesson_key IN ({marks}) AND status IN {ACTIVE} ORDER BY id",
            (user_id, *lesson_keys))

    # --- timetable -------------------------------------------------------------

    async def schedule_days(self) -> dict[str, dict[str, Any]]:
        rows = await self._all("SELECT * FROM schedule_days")
        return {r["day"]: r for r in rows}

    async def replace_day(self, day: str, url: str, lessons: list[dict[str, Any]]) -> None:
        """Store a day's freshly parsed lessons. Unchanged lessons keep their id."""
        keys = [lesson["lesson_key"] for lesson in lessons]
        try:
            marks = ", ".join("?" for _ in keys)
            keep = f" AND lesson_key NOT IN ({marks})" if keys else ""
            await self.conn.execute(f"DELETE FROM lessons WHERE day = ?{keep}", (day, *keys))
            for lesson in lessons:
                cols = ", ".join(lesson)
                marks = ", ".join("?" for _ in lesson)
                updates = ", ".join(f"{c} = excluded.{c}" for c in lesson if c != "lesson_key")
                await self.conn.execute(
                    f"INSERT INTO lessons ({cols}) VALUES ({marks}) ON CONFLICT(lesson_key) DO UPDATE SET {updates}",
                    tuple(lesson.values()))
            await self.conn.execute(
                """INSERT INTO schedule_days (day, url, fetched_at) VALUES (?, ?, ?)
                   ON CONFLICT(day) DO UPDATE SET url = excluded.url, fetched_at = excluded.fetched_at""",
                (day, url, time.time()))
            await self.conn.commit()
        except BaseException:
            await self.conn.rollback()
            raise

    async def schedule_day_stats(self) -> list[dict[str, Any]]:
        return await self._all(
            """SELECT d.day, d.url, d.fetched_at, COUNT(l.id) AS classes, COUNT(DISTINCT l.group_name) AS groups
               FROM schedule_days d LEFT JOIN lessons l ON l.day = d.day
               GROUP BY d.day ORDER BY d.day DESC""")

    async def prune_days(self, before_day: str) -> None:
        await self.conn.execute("DELETE FROM lessons WHERE day < ?", (before_day,))
        await self._exec("DELETE FROM schedule_days WHERE day < ?", (before_day,))

    async def groups(self) -> list[str]:
        rows = await self._all("SELECT DISTINCT group_name FROM lessons ORDER BY group_name")
        return [r["group_name"] for r in rows]

    async def group_lessons(self, group_name: str, from_day: str) -> list[dict[str, Any]]:
        return await self._all(
            "SELECT * FROM lessons WHERE group_name = ? AND day >= ? ORDER BY start_at, subgroup",
            (group_name, from_day))

    async def get_lesson(self, lesson_id: int) -> dict[str, Any] | None:
        return await self._one("SELECT * FROM lessons WHERE id = ?", (lesson_id,))

    async def get_lesson_by_key(self, lesson_key: str) -> dict[str, Any] | None:
        return await self._one("SELECT * FROM lessons WHERE lesson_key = ?", (lesson_key,))
