import time

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.timetable import today

HEADERS = {"X-TelegramQ": "1"}
PEERS = {
    "group": {"peer_type": "chat", "peer_id": 600, "peer_hash": None, "peer_name": "3К9393"},
    "forum": {"peer_type": "channel", "peer_id": 500, "peer_hash": 42, "peer_name": "Dev Team"},
    "me": {"peer_type": "self", "peer_id": 1, "peer_hash": None, "peer_name": "Saved Messages"},
}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("TG_API_ID", "1")
    monkeypatch.setenv("TG_API_HASH", "x")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SECRET_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("SCHEDULE_URL", "")  # never fetch the real timetable

    from app import main
    from app.telegram import TelegramAccounts

    async def fake_get(self, user_id):
        return object()

    async def fake_resolve(client, raw, topic_id=None):
        topic_name = "Releases" if raw == "forum" and topic_id else None
        return {**PEERS[raw], "topic_id": topic_id if raw == "forum" else None, "topic_name": topic_name}

    monkeypatch.setattr(TelegramAccounts, "get", fake_get)
    monkeypatch.setattr(main, "resolve_peer", fake_resolve)

    with TestClient(main.app) as c:
        yield c


def call(client, fn):
    return client.portal.call(fn)


def login(client, user_id=1):
    from app.security import hash_token

    db = client.app.state.db
    call(client, lambda: db.upsert_user(id=user_id, first_name=f"User{user_id}", last_name="",
                                        username=f"user{user_id}", phone=str(user_id), session_enc="x"))
    call(client, lambda: db.create_web_session(hash_token(f"tok{user_id}"), user_id, 3600))
    act_as(client, user_id)


def act_as(client, user_id):
    client.cookies.set("tq_session", f"tok{user_id}")


def lesson(key, send_in=3600, subject="СУБД", group="3К9393", pair=4):
    now = time.time()
    return {"lesson_key": key, "day": today().isoformat(), "group_name": group, "pair": pair,
            "start_at": now + send_in + 7200, "end_at": now + send_in + 13200, "send_at": now + send_in,
            "subject": subject, "subgroup": None, "teachers": "Радкевич", "rooms": "215", "details": ""}


def store_day(client, *lessons):
    """Replace today's timetable with ``lessons``; returns their ids."""
    db = client.app.state.db
    call(client, lambda: db.replace_day(today().isoformat(), "https://example.org/day.pdf", list(lessons)))
    return [call(client, lambda key=x["lesson_key"]: db.get_lesson_by_key(key))["id"] for x in lessons]


def join(client, lesson_id, peer="group", topic_id=None):
    return client.post("/api/lines", headers=HEADERS, json={"lesson_id": lesson_id, "peer": peer, "topic_id": topic_id})


def test_static_and_time(client):
    assert "TelegramQ" in client.get("/").text
    assert abs(client.get("/api/time").json()["now"] - time.time() * 1000) < 1000


def test_requires_login_and_header(client):
    assert client.get("/api/me").status_code == 401
    assert client.post("/api/auth/phone", json={"phone": "+491234567"}).status_code == 403


def test_free_form_messages_are_gone(client):
    login(client)
    r = client.post("/api/messages", headers=HEADERS,
                    json={"peer": "me", "text": "hi", "send_at": (time.time() + 3600) * 1000})
    assert r.status_code in (404, 405)


def test_choose_group_and_see_its_classes(client):
    login(client)
    store_day(client, lesson("a", subject="СУБД"), lesson("b", subject="Физика", group="6К9091"))
    r = client.get("/api/groups").json()
    assert r["groups"] == ["3К9393", "6К9091"] and r["selected"] is None

    assert client.put("/api/me/group", headers=HEADERS, json={"group": "9К0000"}).status_code == 400
    assert client.put("/api/me/group", headers=HEADERS, json={"group": "6К9091"}).json() == {"group": "6К9091"}
    assert client.get("/api/groups").json()["selected"] == "6К9091"

    days = client.get("/api/schedule").json()["days"]
    assert [x["subject"] for d in days for x in d["lessons"]] == ["Физика"]
    days = client.get("/api/schedule", params={"group": "3К9393"}).json()["days"]
    assert [x["subject"] for d in days for x in d["lessons"]] == ["СУБД"]


def test_places_follow_join_order_and_close_gaps(client):
    (lesson_id,) = store_day(client, lesson("a"))
    for user in (1, 2, 3):
        login(client, user)
        r = join(client, lesson_id)
        assert r.status_code == 201, r.text
        assert (r.json()["place"], r.json()["text"]) == (user, f"{user} СУБД")

    act_as(client, 1)
    assert join(client, lesson_id).status_code == 409  # one place per person per line
    first = client.get("/api/lines").json()[0]
    assert client.post(f"/api/lines/{first['id']}/cancel", headers=HEADERS).json()["status"] == "cancelled"

    for user, place in ((2, 1), (3, 2)):
        act_as(client, user)
        (entry,) = client.get("/api/lines").json()
        assert (entry["place"], entry["text"], entry["status"]) == (place, f"{place} СУБД", "pending")
        places = client.get("/api/schedule", params={"group": "3К9393"}).json()["days"][0]["lessons"][0]["places"]
        assert [p["place"] for p in places] == [place]

    act_as(client, 1)  # back in line: at the end
    assert join(client, lesson_id).json()["place"] == 3


def test_each_chat_has_its_own_line(client):
    (lesson_id,) = store_day(client, lesson("a"))
    login(client, 1)
    assert join(client, lesson_id, "group").json()["place"] == 1
    login(client, 2)
    assert join(client, lesson_id, "forum", topic_id=5).json()["place"] == 1
    assert join(client, lesson_id, "group").json()["place"] == 2
    entry = join(client, lesson_id, "forum").json()  # General topic is another line
    assert (entry["place"], entry["topic_id"]) == (1, None)


def test_cannot_join_once_the_line_opened(client):
    (lesson_id,) = store_day(client, lesson("a", send_in=-5))
    login(client)
    r = join(client, lesson_id)
    assert r.status_code == 409 and "too late" in r.json()["detail"]
    assert join(client, 9999).status_code == 404


def test_places_are_fixed_shortly_before_the_line_opens(client):
    (lesson_id,) = store_day(client, lesson("a", send_in=15))
    login(client, 1)
    first = join(client, lesson_id).json()
    login(client, 2)
    assert join(client, lesson_id).json()["place"] == 2
    act_as(client, 1)
    client.post(f"/api/lines/{first['id']}/cancel", headers=HEADERS)
    act_as(client, 2)
    assert client.get("/api/lines").json()[0]["place"] == 2  # messages are being prepared


def test_timetable_changes_move_or_drop_places(client):
    a, b = store_day(client, lesson("a"), lesson("b", subject="Физика"))
    login(client)
    join(client, a)
    join(client, b)

    moved = lesson("a", send_in=7200)
    store_day(client, moved)  # "b" disappeared, "a" now opens an hour later
    call(client, client.app.state.lines.sync)

    entries = {e["subject"]: e for e in client.get("/api/lines").json()}
    assert entries["СУБД"]["status"] == "pending"
    assert entries["СУБД"]["send_at"] == pytest.approx(moved["send_at"] * 1000)
    assert entries["Физика"]["status"] == "failed"
    assert "removed from the timetable" in entries["Физика"]["error"]


def test_leave_and_remove(client):
    (lesson_id,) = store_day(client, lesson("a"))
    login(client)
    entry = join(client, lesson_id).json()
    assert client.delete(f"/api/lines/{entry['id']}", headers=HEADERS).status_code == 409
    assert client.post(f"/api/lines/{entry['id']}/cancel", headers=HEADERS).status_code == 200
    assert client.post(f"/api/lines/{entry['id']}/cancel", headers=HEADERS).status_code == 409
    assert client.delete(f"/api/lines/{entry['id']}", headers=HEADERS).status_code == 200
    assert client.get("/api/lines").json() == []


def test_join_into_forum_topic(client):
    (lesson_id,) = store_day(client, lesson("a"))
    login(client)
    r = join(client, lesson_id, "forum", topic_id=5)
    assert r.status_code == 201, r.text
    entry = r.json()
    assert (entry["peer_name"], entry["topic_id"], entry["topic_name"]) == ("Dev Team", 5, "Releases")
    assert entry["peer_ref"] == "-1000000000500"


def test_sign_out_keeps_account(client):
    login(client)
    r = client.post("/api/auth/logout", headers=HEADERS, json={"disconnect_telegram": False})
    assert r.status_code == 200
    client.cookies.clear()
    assert client.get("/api/me").status_code == 401
    assert client.portal.call(lambda: client.app.state.db.get_user(1)) is not None


def test_disconnecting_frees_places(client):
    (lesson_id,) = store_day(client, lesson("a"))
    login(client, 1)
    join(client, lesson_id)
    login(client, 2)
    join(client, lesson_id)
    act_as(client, 1)
    client.app.state.accounts.log_out = lambda user_id: _noop()
    client.post("/api/auth/logout", headers=HEADERS, json={"disconnect_telegram": True})
    act_as(client, 2)
    assert client.get("/api/lines").json()[0]["place"] == 1


async def _noop():
    pass
