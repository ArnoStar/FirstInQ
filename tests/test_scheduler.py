import asyncio
import datetime as dt
import time

import pytest
from telethon import errors, functions, types

from app import scheduler as sched
from app.db import Database
from app.scheduler import Scheduler, extract_sent, sleep_until
from app.telegram import NotAuthorized, input_peer_from_row, peer_ref

ONE_WAY = 0.020  # simulated network latency each way


class FakeClient:
    def __init__(self, fail_with: Exception | None = None, one_way: float = ONE_WAY):
        self.sent: list[tuple[float, functions.messages.SendMessageRequest]] = []
        self.fail_with = fail_with
        self.one_way = one_way

    async def __call__(self, request):
        if isinstance(request, functions.PingRequest):
            await asyncio.sleep(2 * self.one_way)
            return types.Pong(msg_id=1, ping_id=request.ping_id)
        if isinstance(request, functions.messages.SendMessageRequest):
            if self.fail_with:
                raise self.fail_with
            self.sent.append((time.time(), request))
            await asyncio.sleep(2 * self.one_way)
            return types.UpdateShortSentMessage(
                id=4242, pts=1, pts_count=1, date=dt.datetime.now(dt.timezone.utc))
        raise AssertionError(f"unexpected request {request!r}")


class FakeAccounts:
    def __init__(self, client=None, authorized=True):
        self.client = client or FakeClient()
        self.authorized = authorized
        self.revoked_users: list[int] = []

    async def get(self, user_id):
        if not self.authorized:
            raise NotAuthorized()
        return self.client

    async def revoked(self, user_id):
        self.revoked_users.append(user_id)


@pytest.fixture(autouse=True)
def short_leads(monkeypatch):
    monkeypatch.setattr(sched, "PREPARE_LEAD", 0.3)
    monkeypatch.setattr(sched, "PING_LEAD", 0.25)


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "test.sqlite3")
    await database.connect()
    await database.upsert_user(id=1, first_name="Ada", last_name="", username="ada", phone="1", session_enc="x")
    yield database
    await database.close()


async def add_message(db, send_at, **extra):
    fields = dict(user_id=1, peer_type="self", peer_id=1, peer_hash=None, peer_name="Saved Messages",
                  text="hello **world**", parse_mode="md", silent=0, link_preview=1,
                  compensate_latency=1, send_at=send_at, status="pending")
    fields.update(extra)
    return await db.create_message(**fields)


async def wait_for_status(db, msg_id, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        row = await db.get_message(msg_id)
        if row["status"] != "pending":
            return row
        await asyncio.sleep(0.02)
    raise AssertionError("message stayed pending")


async def test_sleep_until_is_sub_millisecond():
    errors_ms = []
    for _ in range(5):
        target = time.time() + 0.1
        await sleep_until(target)
        errors_ms.append((time.time() - target) * 1000)
    assert all(0 <= e < 1.0 for e in errors_ms), errors_ms


async def test_sends_early_by_half_rtt(db):
    accounts = FakeAccounts()
    scheduler = Scheduler(db, accounts, missed_grace=60)
    send_at = time.time() + 0.6
    row = await add_message(db, send_at)
    scheduler.schedule(row)

    sent = await wait_for_status(db, row["id"])
    assert sent["status"] == "sent"
    assert sent["tg_message_id"] == 4242
    assert sent["rtt_ms"] == pytest.approx(2 * ONE_WAY * 1000, abs=8)
    assert sent["lead_ms"] == pytest.approx(sent["rtt_ms"] / 2)
    planned = send_at - sent["lead_ms"] / 1000
    assert abs(sent["fired_at"] - planned) < 0.002

    (fired, request), = accounts.client.sent
    assert request.message == "hello world"  # markdown parsed ahead of time
    assert isinstance(request.entities[0], types.MessageEntityBold)
    assert isinstance(request.peer, types.InputPeerSelf)
    await scheduler.stop()


async def test_without_compensation_fires_at_target(db):
    scheduler = Scheduler(db, FakeAccounts(), missed_grace=60)
    send_at = time.time() + 0.5
    row = await add_message(db, send_at, compensate_latency=0, silent=1)
    scheduler.schedule(row)
    sent = await wait_for_status(db, row["id"])
    assert sent["lead_ms"] == 0
    assert abs(sent["fired_at"] - send_at) < 0.002
    await scheduler.stop()


async def test_ping_bypasses_invoke_without_updates(db):
    # Like a real TelegramClient with receive_updates=False: client(PingRequest)
    # is rejected by Telegram, so pings must go through the sender directly.
    class Sender:
        def send(self, request):
            assert isinstance(request, functions.PingRequest)
            future = asyncio.get_running_loop().create_future()
            asyncio.get_running_loop().call_later(
                2 * ONE_WAY, future.set_result, types.Pong(msg_id=1, ping_id=request.ping_id))
            return future

    class RealisticClient(FakeClient):
        _sender = Sender()

        async def __call__(self, request):
            if isinstance(request, functions.PingRequest):
                raise errors.BadRequestError(request, "INPUT_METHOD_INVALID")
            return await super().__call__(request)

    scheduler = Scheduler(db, FakeAccounts(client=RealisticClient()), missed_grace=60)
    row = await add_message(db, time.time() + 0.5)
    scheduler.schedule(row)
    sent = await wait_for_status(db, row["id"])
    assert sent["rtt_ms"] == pytest.approx(2 * ONE_WAY * 1000, abs=8)
    assert sent["lead_ms"] == pytest.approx(sent["rtt_ms"] / 2)
    await scheduler.stop()


async def test_slow_network_never_delays_the_send(db):
    # 3 pings of 160 ms RTT don't fit in the 250 ms ping window; measuring must stop early.
    scheduler = Scheduler(db, FakeAccounts(client=FakeClient(one_way=0.08)), missed_grace=60)
    send_at = time.time() + 0.5
    row = await add_message(db, send_at)
    scheduler.schedule(row)
    sent = await wait_for_status(db, row["id"])
    assert sent["rtt_ms"] == pytest.approx(160, abs=10)
    assert abs(sent["fired_at"] - (send_at - sent["lead_ms"] / 1000)) < 0.002
    await scheduler.stop()


async def test_cancel_prevents_sending(db):
    accounts = FakeAccounts()
    scheduler = Scheduler(db, accounts, missed_grace=60)
    row = await add_message(db, time.time() + 0.5)
    scheduler.schedule(row)
    await asyncio.sleep(0.05)
    assert scheduler.cancel(row["id"])
    await asyncio.sleep(0.7)
    assert accounts.client.sent == []
    await scheduler.stop()


async def test_start_marks_long_overdue_as_missed_and_sends_recent(db):
    accounts = FakeAccounts()
    old = await add_message(db, time.time() - 3600)
    recent = await add_message(db, time.time() - 5)
    scheduler = Scheduler(db, accounts, missed_grace=60)
    await scheduler.start()
    assert (await db.get_message(old["id"]))["status"] == "missed"
    assert (await wait_for_status(db, recent["id"]))["status"] == "sent"
    await scheduler.stop()


async def test_revoked_session_fails_message(db):
    accounts = FakeAccounts(client=FakeClient(fail_with=errors.AuthKeyUnregisteredError(None)))
    scheduler = Scheduler(db, accounts, missed_grace=60)
    row = await add_message(db, time.time() + 0.4)
    scheduler.schedule(row)
    failed = await wait_for_status(db, row["id"])
    assert failed["status"] == "failed"
    assert "terminated" in failed["error"]
    assert accounts.revoked_users == [1]
    await scheduler.stop()


async def test_not_authorized_fails_message(db):
    scheduler = Scheduler(db, FakeAccounts(authorized=False), missed_grace=60)
    row = await add_message(db, time.time() + 0.4)
    scheduler.schedule(row)
    assert (await wait_for_status(db, row["id"]))["status"] == "failed"
    await scheduler.stop()


def test_extract_sent_from_full_updates():
    date = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    msg = types.Message(id=77, peer_id=types.PeerUser(5), date=date, message="hi")
    result = types.Updates(
        updates=[types.UpdateMessageID(id=77, random_id=999),
                 types.UpdateNewMessage(message=msg, pts=1, pts_count=1)],
        users=[], chats=[], date=date, seq=0)
    assert extract_sent(result, 999) == (77, date.timestamp())


@pytest.mark.parametrize("row, ref", [
    ({"peer_type": "self", "peer_id": 1, "peer_hash": None}, "me"),
    ({"peer_type": "user", "peer_id": 123, "peer_hash": 456}, "123"),
    ({"peer_type": "chat", "peer_id": 123, "peer_hash": None}, "-123"),
    ({"peer_type": "channel", "peer_id": 123, "peer_hash": 456}, "-1000000000123"),
])
def test_peer_roundtrip(row, ref):
    assert peer_ref(row) == ref
    assert input_peer_from_row(row) is not None


def test_parse_text_limits():
    with pytest.raises(ValueError):
        sched.parse_text("**  **", "md")
    with pytest.raises(ValueError):
        sched.parse_text("x" * 5000, "none")
    assert sched.parse_text("<b>hi</b>", "html")[0] == "hi"


async def test_sends_the_latest_place_in_line(db):
    accounts = FakeAccounts()
    scheduler = Scheduler(db, accounts, missed_grace=60)
    row = await add_message(db, time.time() + 0.6, text="2 СУБД", parse_mode="none")
    scheduler.schedule(row)
    await db.update_message(row["id"], text="1 СУБД")  # someone ahead left the line
    await wait_for_status(db, row["id"])
    (_, request), = accounts.client.sent
    assert request.message == "1 СУБД"
    await scheduler.stop()


async def test_messages_due_together_do_not_wait_for_each_other(db):
    accounts = FakeAccounts()
    scheduler = Scheduler(db, accounts, missed_grace=60)
    send_at = time.time() + 0.6
    rows = [await add_message(db, send_at, text=f"{n} СУБД", parse_mode="none") for n in (1, 2, 3)]
    for row in rows:
        scheduler.schedule(row)
    for row in rows:
        assert (await wait_for_status(db, row["id"]))["status"] == "sent"
    fired = [t for t, _ in accounts.client.sent]
    assert max(fired) - min(fired) < 0.005  # a send takes 40 ms; none waited for another
    await scheduler.stop()
