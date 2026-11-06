import time

from tests.test_api import HEADERS, act_as, call, client, join, lesson, login, store_day  # noqa: F401 (fixture)


def schedule(client, peer="group", text="Hello", send_in=3600, **extra):
    return client.post("/api/admin/messages", headers=HEADERS,
                       json={"peer": peer, "text": text, "send_at": (time.time() + send_in) * 1000, **extra})


def test_only_admins_get_in(client):
    login(client, 1)
    assert client.get("/api/me").json()["is_admin"] is False
    for path in ("/api/admin/overview", "/api/admin/messages", "/api/admin/users", "/api/admin/timetable"):
        assert client.get(path).status_code == 403, path
    assert schedule(client).status_code == 403

    login(client, 9)  # by Telegram user id
    assert client.get("/api/me").json()["is_admin"] is True
    login(client, 5, phone="375291234567")  # by phone number
    assert client.get("/api/me").json()["is_admin"] is True
    assert client.get("/api/admin/overview").status_code == 200


def test_admin_schedules_messages(client):
    login(client, 9)
    assert schedule(client, send_in=-60).status_code == 400
    r = schedule(client, text="**Hi** all", silent=True)
    assert r.status_code == 201, r.text
    msg = r.json()
    assert (msg["kind"], msg["user_name"], msg["status"], msg["silent"]) == ("message", "User9", "pending", True)

    (listed,) = client.get("/api/admin/messages").json()
    assert listed["id"] == msg["id"]
    assert client.get("/api/lines").json() == []  # not a place in line
    overview = client.get("/api/admin/overview").json()
    assert (overview["pending"], overview["pending_lines"]) == (1, 0)

    assert client.post(f"/api/admin/messages/{msg['id']}/cancel", headers=HEADERS).status_code == 200
    (done,) = client.get("/api/admin/messages", params={"upcoming": False}).json()
    assert (done["status"], done["error"]) == ("cancelled", None)
    assert client.delete(f"/api/admin/messages/{msg['id']}", headers=HEADERS).status_code == 200


def test_admin_sees_and_cancels_everyones_places(client):
    (lesson_id,) = store_day(client, lesson("a"))
    for user in (1, 2):
        login(client, user)
        join(client, lesson_id)
    login(client, 9)
    places = client.get("/api/admin/messages").json()
    assert [(m["user_name"], m["text"], m["kind"]) for m in places] == [
        ("User1", "1 СУБД", "line"), ("User2", "2 СУБД", "line")]

    client.post(f"/api/admin/messages/{places[0]['id']}/cancel", headers=HEADERS)
    act_as(client, 1)
    (mine,) = client.get("/api/lines").json()
    assert (mine["status"], mine["error"]) == ("cancelled", "Cancelled by an admin.")
    act_as(client, 2)
    assert client.get("/api/lines").json()[0]["text"] == "1 СУБД"  # moved up


def test_admin_removes_a_user(client):
    (lesson_id,) = store_day(client, lesson("a"))
    for user in (1, 2):
        login(client, user)
        join(client, lesson_id)
    login(client, 9)
    logged_out = []

    async def log_out(user_id):
        logged_out.append(user_id)

    client.app.state.accounts.log_out = log_out
    users = {u["id"]: u for u in client.get("/api/admin/users").json()}
    assert (users[1]["pending"], users[9]["is_admin"], users[9]["is_you"]) == (1, True, True)

    assert client.delete("/api/admin/users/9", headers=HEADERS).status_code == 409  # not yourself
    assert client.delete("/api/admin/users/1", headers=HEADERS).status_code == 200
    assert logged_out == [1]
    assert 1 not in {u["id"] for u in client.get("/api/admin/users").json()}
    act_as(client, 2)
    assert client.get("/api/lines").json()[0]["place"] == 1


def test_line_timing_setting(client):
    (lesson_id,) = store_day(client, lesson("a"))
    login(client, 1)
    join(client, lesson_id)
    login(client, 9)
    assert client.get("/api/admin/overview").json()["line_timing"] == "arrive"
    assert client.put("/api/admin/settings", headers=HEADERS, json={"line_timing": "soon"}).status_code == 422
    assert client.put("/api/admin/settings", headers=HEADERS, json={"line_timing": "exact"}).status_code == 200
    assert client.get("/api/admin/overview").json()["line_timing"] == "exact"

    (taken,) = client.get("/api/admin/messages").json()
    assert taken["compensate_latency"] is False  # applies to places already taken
    login(client, 2)
    join(client, lesson_id)
    act_as(client, 9)
    assert [m["compensate_latency"] for m in client.get("/api/admin/messages").json()] == [False, False]


def test_admin_timetable(client):
    store_day(client, lesson("a"), lesson("b", group="6К9091"))
    login(client, 9)
    t = client.get("/api/admin/timetable").json()
    assert t["enabled"] is False
    (day,) = t["days"]
    assert (day["classes"], day["groups"]) == (2, 2)
    assert client.post("/api/admin/timetable/refresh", headers=HEADERS).status_code == 409  # fetching is off
    assert client.post("/api/admin/timetable/days/2001-01-01/reload", headers=HEADERS).status_code == 404
