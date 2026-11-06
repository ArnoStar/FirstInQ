"""Places in line for a class.

A line is one class in one chat (and forum topic). Joining it takes the next
place, and the user's account posts "{place} {subject}" to that chat when the
line opens: at the start of the break before the previous pair (see
``timetable.line_send_time``). Everyone's message goes out at that moment, in
no particular order.

Leaving moves everyone behind you up one place, until LOCK_SECONDS before the
line opens. From then on places are fixed: the messages are being prepared.

How the moment is hit is an admin setting (``timing``):
  "arrive"  send half a round trip early, so the message reaches Telegram at
            the line's time. Network jitter can make it a few ms early.
  "exact"   send at the line's time. Never early; it reaches Telegram one
            network trip (typically 15-50 ms) later.
"""

import asyncio
import time
from typing import Any

from .db import Database
from .scheduler import PREPARE_LEAD, Scheduler
from .telegram import UserFacingError

LOCK_SECONDS = PREPARE_LEAD + 10
TIMINGS = ("arrive", "exact")


def line_text(place: int, subject: str) -> str:
    return f"{place} {subject}"


class Lines:
    def __init__(self, db: Database, scheduler: Scheduler):
        self.db = db
        self.scheduler = scheduler
        self._lock = asyncio.Lock()  # places are read and assigned atomically
        self.timing = "arrive"

    async def start(self) -> None:
        self.timing = await self.db.get_setting("line_timing", "arrive")

    async def set_timing(self, timing: str) -> None:
        """Change how line messages hit their moment, for pending ones too."""
        if timing not in TIMINGS:
            raise ValueError(timing)
        async with self._lock:
            await self.db.set_setting("line_timing", timing)
            self.timing = timing
            # The scheduler re-reads each message shortly before sending it.
            await self.db.set_pending_line_compensation(timing == "arrive")

    async def join(self, user_id: int, lesson: dict[str, Any], peer: dict[str, Any]) -> dict[str, Any]:
        """Take the next place in the line for ``lesson`` in the chat ``peer`` (from ``resolve_peer``)."""
        async with self._lock:
            if lesson["send_at"] <= time.time():
                raise UserFacingError("This line has already opened, so it's too late to take a place.", 409)
            entries = await self.db.line_entries({"lesson_key": lesson["lesson_key"], **peer})
            mine = next((e for e in entries if e["user_id"] == user_id), None)
            if mine is not None:
                raise UserFacingError(f"You're already #{mine['place']} in this line.", 409)
            place = max((e["place"] or 0 for e in entries), default=0) + 1
            row = await self.db.create_message(
                user_id=user_id,
                **peer,
                text=line_text(place, lesson["subject"]),
                parse_mode="none",
                silent=0,
                link_preview=0,
                compensate_latency=int(self.timing == "arrive"),
                send_at=lesson["send_at"],
                status="pending",
                lesson_key=lesson["lesson_key"],
                group_name=lesson["group_name"],
                subject=lesson["subject"],
                pair=lesson["pair"],
                lesson_start=lesson["start_at"],
                place=place,
            )
            self.scheduler.schedule(row)
            return row

    async def leave(self, user_id: int, msg_id: int) -> dict[str, Any]:
        return await self.cancel(msg_id, user_id=user_id)

    async def cancel(self, msg_id: int, user_id: int | None = None, error: str | None = None) -> dict[str, Any]:
        """Cancel a pending message (a place in line, or an admin's message).

        With ``user_id``, only that user's own message.
        """
        async with self._lock:
            row = await self.db.get_message(msg_id, user_id)
            if row is None:
                raise UserFacingError("Not found.", 404)
            if row["status"] != "pending":
                raise UserFacingError("Only messages that haven't been sent yet can be cancelled.", 409)
            if not self.scheduler.cancel(msg_id):
                raise UserFacingError("Too late: the message is being sent right now.", 409)
            await self.db.update_message(msg_id, status="cancelled", error=error)
            if row["lesson_key"]:
                await self._renumber(row)
            return await self.db.get_message(msg_id)

    async def drop_user(self, user_id: int, reason: str) -> None:
        """Fail a user's pending places (session revoked, account disconnected) and close the gaps."""
        async with self._lock:
            entries = await self.db.pending_line_entries(user_id)
            await self.scheduler.fail_user(user_id, reason)
            for row in entries:
                await self._renumber(row)

    async def sync(self) -> None:
        """After a timetable update, follow classes whose time changed and drop removed ones."""
        async with self._lock:
            for row in await self.db.pending_line_entries():
                if self.scheduler.is_firing(row["id"]):
                    continue
                lesson = await self.db.get_lesson_by_key(row["lesson_key"])
                if lesson is None:
                    if self.scheduler.cancel(row["id"]):
                        await self.db.update_message(row["id"], status="failed",
                                                     error="This class was removed from the timetable.")
                        await self._renumber(row)
                elif abs(lesson["send_at"] - row["send_at"]) > 0.001 and self.scheduler.cancel(row["id"]):
                    await self.db.update_message(row["id"], send_at=lesson["send_at"])
                    self.scheduler.schedule(await self.db.get_message(row["id"]))

    async def _renumber(self, row: dict[str, Any]) -> None:
        """Close the gaps in ``row``'s line, unless its places are already fixed."""
        if time.time() > row["send_at"] - LOCK_SECONDS:
            return
        for place, entry in enumerate(await self.db.line_entries(row), start=1):
            if entry["place"] != place:
                await self.db.update_message(entry["id"], place=place, text=line_text(place, entry["subject"]))
