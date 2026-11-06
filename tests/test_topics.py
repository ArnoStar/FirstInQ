import datetime as dt

import aiosqlite
import pytest
from telethon import functions, types

from app.db import Database
from app.scheduler import build_request
from app.telegram import UserFacingError, list_topics, parse_target, resolve_peer

NOW = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)


def topic(id, title, closed=False):
    return types.ForumTopic(
        id=id, date=NOW, peer=types.PeerChannel(500), title=title, icon_color=0, top_message=id,
        read_inbox_max_id=0, read_outbox_max_id=0, unread_count=0, unread_mentions_count=0,
        unread_reactions_count=0, unread_poll_votes_count=0, from_id=types.PeerUser(1),
        notify_settings=types.PeerNotifySettings(), closed=closed)


TOPICS = [topic(1, "General"), topic(5, "Releases"), types.ForumTopicDeleted(id=9), topic(12, "Old", closed=True)]


class ForumClient:
    def __init__(self, forum=True):
        self.channel = types.Channel(id=500, title="Dev Team", photo=types.ChatPhotoEmpty(), date=NOW,
                                     megagroup=True, forum=forum, access_hash=42)
        self.requests = []

    async def get_entity(self, target):
        if target in (-1000000000500, "devteam"):
            return self.channel
        raise ValueError("not found")

    async def __call__(self, request):
        self.requests.append(request)
        if isinstance(request, functions.messages.GetForumTopicsRequest):
            return types.messages.ForumTopics(count=len(TOPICS), topics=TOPICS, messages=[], chats=[], users=[], pts=0)
        if isinstance(request, functions.messages.GetForumTopicsByIDRequest):
            found = [t for t in TOPICS if t.id in request.topics]
            return types.messages.ForumTopics(count=len(found), topics=found, messages=[], chats=[], users=[], pts=0)
        raise AssertionError(request)


@pytest.mark.parametrize("raw, expected", [
    ("me", ("me", None)),
    ("-1000000000500", (-1000000000500, None)),
    ("@devteam", ("@devteam", None)),
    ("t.me/devteam", ("devteam", None)),
    ("https://t.me/devteam/5", ("devteam", 5)),
    ("https://t.me/devteam/5/1234", ("devteam", 5)),
    ("https://t.me/c/500/5", (-100500, 5)),
    ("https://t.me/c/500/5/77?single", (-100500, 5)),
])
def test_parse_target(raw, expected):
    assert parse_target(raw) == expected


def row(**extra):
    base = dict(peer_type="channel", peer_id=500, peer_hash=42, text="hi", parse_mode="none",
                link_preview=1, silent=0, topic_id=None)
    return {**base, **extra}


def test_request_targets_topic():
    request = build_request(row(topic_id=5))
    assert request.reply_to.reply_to_msg_id == 5
    assert request.reply_to.top_msg_id == 5


@pytest.mark.parametrize("topic_id", [None, 1])
def test_general_topic_needs_no_reply_to(topic_id):
    assert build_request(row(topic_id=topic_id)).reply_to is None


async def test_list_topics_skips_deleted():
    topics = await list_topics(ForumClient(), "-1000000000500")
    assert [(t["id"], t["title"], t["closed"]) for t in topics] == [
        (1, "General", False), (5, "Releases", False), (12, "Old", True)]


async def test_list_topics_of_normal_group_is_empty():
    assert await list_topics(ForumClient(forum=False), "-1000000000500") == []


async def test_resolve_with_picked_topic():
    fields = await resolve_peer(ForumClient(), "-1000000000500", topic_id=5)
    assert fields["topic_id"] == 5 and fields["topic_name"] == "Releases"
    assert fields["peer_type"] == "channel" and fields["peer_name"] == "Dev Team"


async def test_resolve_topic_from_link():
    fields = await resolve_peer(ForumClient(), "https://t.me/devteam/5")
    assert (fields["topic_id"], fields["topic_name"]) == (5, "Releases")


async def test_resolve_forum_without_topic_goes_to_general():
    fields = await resolve_peer(ForumClient(), "t.me/devteam")
    assert fields["topic_id"] is None and fields["topic_name"] is None


async def test_resolve_unknown_topic_is_rejected():
    with pytest.raises(UserFacingError, match="doesn't exist"):
        await resolve_peer(ForumClient(), "-1000000000500", topic_id=9)


async def test_message_link_in_normal_group_is_rejected():
    with pytest.raises(UserFacingError, match="single message"):
        await resolve_peer(ForumClient(forum=False), "https://t.me/devteam/5")


async def test_old_database_gets_topic_columns(tmp_path):
    path = tmp_path / "old.sqlite3"
    async with aiosqlite.connect(path) as conn:  # schema of the first release
        await conn.execute("""CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            peer_type TEXT NOT NULL, peer_id INTEGER NOT NULL, peer_hash INTEGER, peer_name TEXT NOT NULL,
            text TEXT NOT NULL, parse_mode TEXT NOT NULL DEFAULT 'md', silent INTEGER NOT NULL DEFAULT 0,
            link_preview INTEGER NOT NULL DEFAULT 1, compensate_latency INTEGER NOT NULL DEFAULT 1,
            send_at REAL NOT NULL, status TEXT NOT NULL DEFAULT 'pending', created_at REAL NOT NULL,
            fired_at REAL, acked_at REAL, lead_ms REAL, rtt_ms REAL, tg_message_id INTEGER, tg_date REAL, error TEXT)""")
        await conn.commit()
    db = Database(path)
    await db.connect()
    async with db.conn.execute("PRAGMA table_info(messages)") as cur:
        columns = {r["name"] for r in await cur.fetchall()}
    assert {"topic_id", "topic_name"} <= columns
    await db.close()
