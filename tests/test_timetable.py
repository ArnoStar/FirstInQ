import datetime as dt
from pathlib import Path

import httpx
import pytest

from app.db import Database
from app.timetable import (
    MINSK,
    Timetable,
    _join_wrapped,
    _Line,
    find_pdf_links,
    line_send_time,
    parse_cell_text,
    parse_pdf,
    today,
)

FIXTURES = Path(__file__).parent / "fixtures"
MONDAY = dt.date(2026, 11, 9)
FRIDAY = dt.date(2026, 11, 6)  # pairs 5-7 are single hours on this day


@pytest.fixture(scope="module")
def monday():
    return parse_pdf((FIXTURES / "timetable-09.11.2026.pdf").read_bytes(), MONDAY)


@pytest.fixture(scope="module")
def friday():
    return parse_pdf((FIXTURES / "timetable-06.11.2026.pdf").read_bytes(), FRIDAY)


def classes(lessons, group):
    return [(x.start.strftime("%H:%M"), x.end.strftime("%H:%M"), x.subject, x.subgroup, x.send_at.strftime("%H:%M"))
            for x in lessons if x.group == group]


def test_every_group_is_found(monday):
    groups = {x.group for x in monday}
    assert len(groups) == 50
    assert {"3К9393", "6К9091", "4К9342"} <= groups


def test_hours_of_a_split_pair(monday):
    assert classes(monday, "6К9091")[:3] == [
        ("08:00", "09:40", "Математика", None, "08:00"),
        ("09:50", "10:35", "Физ к и зд", None, "08:00"),
        ("10:45", "11:30", "Русская литература", None, "08:00"),
    ]


def test_two_subgroups_in_one_cell(monday):
    found = [x for x in monday if x.group == "3К9392" and x.pair == 2]
    assert [(x.subject, x.subgroup, x.teachers, x.rooms) for x in found] == [
        ("Трехмерное моделирование", 1, "Морозова", "413"),
        ("СУБД", 2, "Радкевич", "2к4"),
    ]


def test_line_opens_at_the_break_before_the_previous_pair(monday):
    assert classes(monday, "3К9391") == [
        ("13:40", "15:20", "КС", None, "11:30"),       # pair 4: break before pair 3
        ("15:40", "17:20", "ПССИП", None, "13:30"),
        ("17:30", "19:10", "СУБД", None, "15:20"),
    ]
    # A time written in the cell (above the class) wins over the pair's time.
    assert ("13:10", "13:30", "Информационный час", None, "09:40") in classes(monday, "3К9393")
    # Nothing before the first pair: the line opens at the class's own break.
    assert classes(monday, "5К9491")[0] == ("08:55", "09:40", "Физ к и зд", None, "08:45")


def test_short_pairs_and_wrapped_words(friday):
    assert ("16:35", "17:20", "СУБД", None, "15:20") in classes(friday, "3К9391")
    assert ("15:40", "16:00", "Информационный час", None, "13:30") in classes(friday, "5К9491")
    subjects = {x.subject for x in friday}
    assert {"Иностранный язык", "Веб-программирование на стороне сервера", "История Беларуси"} <= subjects
    assert not any(" " not in s and len(s) > 25 for s in subjects)  # no glued words


def test_teachers_and_rooms_are_not_part_of_the_subject(friday):
    pe = next(x for x in friday if x.group == "6К9092" and x.subject.startswith("Физ"))
    assert (pe.subject, pe.teachers, pe.rooms) == ("Физ к и зд", "Авдеенко Макейчик", "с/з, а/з")
    history = next(x for x in friday if x.group == "6К9091" and x.subject == "История Беларуси")
    assert (history.teachers, history.rooms) == ("Кондик", "214")


def at(hm):
    return dt.datetime.combine(MONDAY, dt.time.fromisoformat(hm), MINSK)


BELLS = [(at(a), at(b)) for a, b in [("08:00", "09:40"), ("09:50", "11:30"), ("11:50", "13:30"), ("13:40", "15:20")]]


@pytest.mark.parametrize("start, expected", [
    ("08:00", "08:00"),  # first pair: no break before it
    ("08:55", "08:45"),  # second hour of the first pair
    ("09:50", "08:00"),  # pair 2 -> before pair 1
    ("10:45", "08:00"),  # second hour of pair 2 -> still before pair 1
    ("11:50", "09:40"),  # pair 3 -> before pair 2
    ("13:40", "11:30"),  # pair 4 -> before pair 3
])
def test_line_send_time(start, expected):
    assert line_send_time(at(start), BELLS) == at(expected)


def line(text, width=10.0, trailing_space=True, first=4.0):
    return _Line(text, width, first, trailing_space)


def test_join_wrapped_lines():
    cell = 53.2  # 42.4 pt of room for text
    assert _join_wrapped([line("Иностранн", 37.5, False), line("ый язык")], cell) == ["Иностранный язык"]
    # A full line that wrapped at a space keeps its space.
    assert _join_wrapped([line("Белорусский", 41.0, True), line("язык")], cell) == ["Белорусский", "язык"]
    assert _join_wrapped([line("Веб-", 15.8, False), line("программирование")], cell) == ["Веб-программирование"]
    assert _join_wrapped([line("Лагойкина", 40.0, False), line("с/з, а/з")], cell) == ["Лагойкина", "с/з, а/з"]


def test_parse_cell_text():
    (info,) = parse_cell_text(["Практика 1 подгруппа", "08:00-13:00", "Мазаник", "ауд 221"])
    assert (info.subject, info.subgroup, info.teachers, info.rooms) == ("Практика", 1, "Мазаник", "ауд 221")
    assert info.time == (dt.time(8), dt.time(13))
    (info,) = parse_cell_text(["История", "Беларуси", "Кондик", "214"])
    assert (info.subject, info.teachers) == ("История Беларуси", "Кондик")
    (info,) = parse_cell_text(["Практика", "Ручаевская", "ауд 212", "4 пара: ауд 216"])
    assert (info.subject, info.rooms, info.details) == ("Практика", "ауд 212", "4 пара: ауд 216")


def test_find_pdf_links():
    page = ('<a href="/mediafiles/schedule/09.11.2026-70ae.pdf" class="day">'
            "<a href='/mediafiles/schedule/31.02.2026-bad.pdf'><a href=\"/other.pdf\">")
    assert find_pdf_links(page, "https://mrk.bsuir.by/en/schedule/") == {
        MONDAY: "https://mrk.bsuir.by/mediafiles/schedule/09.11.2026-70ae.pdf"}


async def test_refresh_downloads_new_days_once(tmp_path):
    tomorrow = today() + dt.timedelta(days=1)
    pdf_name = f"{tomorrow:%d.%m.%Y}-abc.pdf"
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if request.url.path == "/en/schedule/":
            return httpx.Response(200, text=f'<a href="/mediafiles/schedule/{pdf_name}">'
                                             '<a href="/mediafiles/schedule/01.01.2020-old.pdf">')
        if request.url.path.endswith(pdf_name):
            return httpx.Response(200, content=(FIXTURES / "timetable-09.11.2026.pdf").read_bytes())
        return httpx.Response(404)

    db = Database(tmp_path / "t.sqlite3")
    await db.connect()
    changes = []

    async def on_change():
        changes.append(True)

    timetable = Timetable(db, "https://mrk.bsuir.by/en/schedule/", 900, on_change=on_change,
                          transport=httpx.MockTransport(handler))
    await timetable.refresh()
    # The latest past day (fetched to learn the groups) failed; the new day still loaded.
    assert timetable.status()["error"].startswith("01.01: Client error '404 Not Found'")
    assert len(await db.groups()) == 50
    lessons = await db.group_lessons("3К9391", tomorrow.isoformat())
    assert [x["subject"] for x in lessons] == ["КС", "ПССИП", "СУБД"]
    assert changes == [True]

    requests.clear()
    await timetable.refresh()
    assert requests == ["/en/schedule/", "/mediafiles/schedule/01.01.2020-old.pdf"]  # the known day isn't fetched again
    assert changes == [True]
    await db.close()
