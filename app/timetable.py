"""The college timetable: daily PDFs from mrk.bsuir.by, parsed into classes.

The college publishes one PDF per day. Each page holds a Word table with one
row per group and one column per pair:

    № группы | 1 пара    | 2 пара     | ...
             | 8.00-9.40 | 9.50-11.30 | ...
    6К9091   | Математика| Физ к и зд | История Беларуси | ...
             | Тарусина  | Авдеенко   | Кондик           |
             | 304       | с/з, а/з   | 214              |

A pair is two 45-minute hours. A cell can fill a whole pair, one of its hours
(the pair column is then split in two), several pairs (practice), or be split
vertically (subgroups, or a time label above the class). Cells are matched to
groups and pairs by geometry, which survives all of that.

``Timetable`` downloads new PDFs from the schedule page and stores their classes.
"""

import asyncio
import datetime as dt
import html
import io
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin

import httpx
import pdfplumber

log = logging.getLogger(__name__)

# Belarus is on UTC+3 all year (no DST since 2011).
MINSK = dt.timezone(dt.timedelta(hours=3), "Europe/Minsk")

# Bell schedule (https://mrk.bsuir.by/en/explore/schedules/call-schedule/),
# used for a pair whose time is missing from a PDF's header.
DEFAULT_PAIR_TIMES = {
    1: ("8:00", "9:40"), 2: ("9:50", "11:30"), 3: ("11:50", "13:30"), 4: ("13:40", "15:20"),
    5: ("15:40", "17:20"), 6: ("17:30", "19:10"), 7: ("19:20", "21:00"),
}
HOUR = dt.timedelta(minutes=45)
CELL_PADDING = 5.4  # Word's default left/right cell margin (0.19 cm), in points

PAIR_HEADER = re.compile(r"(\d)\s*пара", re.I)
TIME_RANGE = re.compile(r"(\d{1,2})[.:](\d{2})\s*[-–—]\s*(\d{1,2})[.:](\d{2})")
SUBGROUP = re.compile(r"\(?\b(\d)\s*-?\s*(?:я\s*)?подгр(?:уппа|\.)?\)?", re.I)
ROOM_TOKEN = re.compile(
    r"\(?(?:ауд\.?|каб\.?|\d+[а-яё]?|\d+к\d+|[са]/з?|чз|бгуир|мрк|корпус|корп\.|завод|общежитие|"
    r"спортзал|бассейн|зал)\)?[,;]?", re.I)
NAME_TOKEN = re.compile(r"[А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?[,;]?|[А-ЯЁ]\.(?:[А-ЯЁ]\.)?[,;]?")
# Notes that are not part of the subject: "3 пара: ауд 216", companies, "девушки".
NOTE = re.compile(r"\d(?:\s*-\s*\d)?\s*пар[аы]\s*:|«|^(?:ООО|ОАО|ЗАО|УП|РУП|ИП)\b|^(?:девушки|юноши)$", re.I)
# Capitalised words that belong to subject names, not to teachers' surnames.
SUBJECT_WORDS = {"Беларуси", "Беларусь", "Беларусі", "Республики", "Отечества"}


@dataclass
class Lesson:
    day: dt.date
    group: str
    pair: int
    start: dt.datetime
    end: dt.datetime
    subject: str
    subgroup: int | None = None
    teachers: str = ""
    rooms: str = ""
    details: str = ""
    send_at: dt.datetime | None = None  # when line messages for it are sent; set by parse_pdf

    @property
    def key(self) -> str:
        """Identifies the class across re-downloads of the same day's PDF."""
        return "|".join([self.day.isoformat(), self.group, self.start.strftime("%H:%M"),
                         self.subject, str(self.subgroup or "")])


@dataclass
class _Line:
    text: str
    width: float
    first_char_width: float
    trailing_space: bool  # Word leaves the space at the end of a line that wrapped at it


@dataclass
class _Cell:
    x0: float
    top: float
    x1: float
    bottom: float
    lines: list[_Line] = field(default_factory=list)

    @property
    def text(self) -> str:
        return " ".join(line.text for line in self.lines).strip()

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return self.bottom - self.top

    def x_overlap(self, other: "_Cell") -> float:
        return max(0.0, min(self.x1, other.x1) - max(self.x0, other.x0))

    def y_overlap(self, other: "_Cell") -> float:
        return max(0.0, min(self.bottom, other.bottom) - max(self.top, other.top))


@dataclass
class _Slot:
    pair: int
    start: dt.datetime
    end: dt.datetime
    cell: _Cell


# --- cell text -----------------------------------------------------------------

def _join_wrapped(lines: list[_Line], cell_width: float) -> list[str]:
    """Undo Word's line wrapping inside a word.

    A line that wrapped at a space keeps that space at its end. Without one,
    the line was broken inside a word: after a hyphen or slash, or because a
    single word was wider than the cell ("Иностранн" + "ый язык"). The latter
    leaves a full-width line followed by lowercase letters.
    """
    available = cell_width - 2 * CELL_PADDING
    out: list[str] = []
    prev: _Line | None = None
    for line in lines:
        text = line.text
        if prev is not None and not prev.trailing_space:
            after_break_char = prev.text.endswith(("-", "/")) and len(prev.text) > 1
            word_split = (" " not in prev.text
                          and re.match(r"[а-яё]+(?:[\s,;)]|$)", text) is not None
                          and not _is_room_line(text)
                          and prev.width + line.first_char_width > available - 1.5)
            if after_break_char or word_split:
                out[-1] += text
                prev = line
                continue
        out.append(text)
        prev = line
    return out


def _is_room_line(line: str) -> bool:
    tokens = line.replace(",", ", ").split()
    return bool(tokens) and all(ROOM_TOKEN.fullmatch(t) for t in tokens)


def _is_name_line(line: str) -> bool:
    tokens = re.sub(r"([А-ЯЁ]\.)\s+(?=[А-ЯЁ]\.)", r"\1", line).split()
    return (bool(tokens) and all(NAME_TOKEN.fullmatch(t) for t in tokens)
            and not any(t.strip(",;") in SUBJECT_WORDS for t in tokens))


def parse_time_range(text: str) -> tuple[dt.time, dt.time] | None:
    m = TIME_RANGE.search(text)
    if not m:
        return None
    h1, m1, h2, m2 = map(int, m.groups())
    if not (h1 < 24 and h2 < 24 and m1 < 60 and m2 < 60):
        return None
    return dt.time(h1, m1), dt.time(h2, m2)


def _is_time_label(text: str) -> bool:
    return bool(text) and TIME_RANGE.sub("", text).strip(" ,;\n") == ""


@dataclass
class CellText:
    subject: str
    subgroup: int | None = None
    teachers: str = ""
    rooms: str = ""
    details: str = ""
    time: tuple[dt.time, dt.time] | None = None


def _split_blocks(lines: list[str]) -> list[list[str]]:
    """A cell can hold two classes (one per subgroup); each ends with its room."""
    blocks: list[list[str]] = [[]]
    for line in lines:
        block = blocks[-1]
        if block and re.match(r"(?:-?\s*я\s*)?подгр", line, re.I) and re.search(r"\d$", block[-1]):
            block[-1] += " " + line  # "2" / "подгруппа" wrapped apart
            continue
        if (block and _is_room_line(block[-1]) and not _is_room_line(line) and not NOTE.search(line)
                and not SUBGROUP.fullmatch(line) and not _is_time_label(line)):
            blocks.append([])
        blocks[-1].append(line)
    return blocks


def _parse_block(lines: list[str]) -> CellText:
    time = None
    subgroup = None
    notes: list[str] = []
    rest: list[str] = []
    for line in lines:
        if rest and NOTE.search(line):
            notes.append(line)
            continue
        if time is None and (found := parse_time_range(line)):
            time = found
            line = TIME_RANGE.sub("", line, count=1)
        if subgroup is None and (m := SUBGROUP.search(line)):
            subgroup = int(m[1])
            line = line[:m.start()] + line[m.end():]
        line = re.sub(r"\s+", " ", line).strip(" ,;")
        if line:
            rest.append(line)

    rooms: list[str] = []
    while len(rest) > 1 and _is_room_line(rest[-1]):
        rooms.insert(0, rest.pop())
    teachers: list[str] = []
    while len(rest) > 1 and _is_name_line(rest[-1]):
        teachers.insert(0, rest.pop())
    return CellText(subject=" ".join(rest), subgroup=subgroup, teachers=" ".join(teachers),
                    rooms=" ".join(rooms), details=" ".join(notes), time=time)


def parse_cell_text(lines: list[str]) -> list[CellText]:
    """Split a cell's (unwrapped) lines into classes: subject, subgroup, teachers, rooms."""
    return [info for block in _split_blocks(lines) if (info := _parse_block(block)).subject]


# --- PDF geometry ----------------------------------------------------------------

def _make_line(chars: list[dict]) -> _Line | None:
    chars = sorted(chars, key=lambda c: c["x0"])
    visible = [c for c in chars if c["text"].strip()]
    if not visible:
        return None
    text = ""
    prev = None
    for c in chars:
        # A gap without a space glyph still separates words.
        if (prev is not None and c["text"].strip() and prev["text"].strip()
                and c["x0"] - prev["x1"] > 0.25 * c["size"]):
            text += " "
        text += c["text"]
        prev = c
    return _Line(text=" ".join(text.split()),
                 width=visible[-1]["x1"] - visible[0]["x0"],
                 first_char_width=visible[0]["x1"] - visible[0]["x0"],
                 trailing_space=chars[-1] is not visible[-1])


def _cell(page: pdfplumber.page.Page, bbox: tuple[float, float, float, float]) -> _Cell:
    cell = _Cell(*bbox)
    current: list[dict] = []
    for c in sorted(page.within_bbox(bbox).chars, key=lambda c: (c["top"], c["x0"])):
        if current and c["top"] - current[0]["top"] > 0.5 * current[0]["size"]:
            if line := _make_line(current):
                cell.lines.append(line)
            current = []
        current.append(c)
    if current and (line := _make_line(current)):
        cell.lines.append(line)
    return cell


def _at(day: dt.date, t: dt.time) -> dt.datetime:
    return dt.datetime.combine(day, t, MINSK)


def _cell_times(c: _Cell, covered: list[_Slot], explicit: tuple[dt.time, dt.time] | None,
                day: dt.date) -> tuple[dt.datetime, dt.datetime]:
    if explicit is not None:
        return _at(day, explicit[0]), _at(day, explicit[1])
    if len(covered) > 1:
        return covered[0].start, covered[-1].end
    slot = covered[0]
    # One hour of a two-hour pair: the pair's column is split in two.
    if slot.end - slot.start >= 2 * HOUR and c.width < 0.7 * slot.cell.width:
        if (c.x0 + c.x1) / 2 < (slot.cell.x0 + slot.cell.x1) / 2:
            return slot.start, slot.start + HOUR
        return slot.end - HOUR, slot.end
    return slot.start, slot.end


def _parse_table(page: pdfplumber.page.Page, table: pdfplumber.table.Table, day: dt.date) -> tuple[list[Lesson], list[_Slot]]:
    cells = [_cell(page, bbox) for bbox in table.cells]
    group_header = next((c for c in cells if "групп" in c.text.lower()), None)
    pair_cells = [(int(m[1]), c) for c in cells if (m := PAIR_HEADER.fullmatch(c.text))]
    if group_header is None or not pair_cells:
        return [], []  # e.g. the electives page

    slots: list[_Slot] = []
    header_bottom = max(c.bottom for _, c in pair_cells)
    for pair, pc in pair_cells:
        below = [c for c in cells if abs(c.top - pc.bottom) < 2 and c.x_overlap(pc) > 0.5 * pc.width]
        times = next((t for c in below if (t := parse_time_range(c.text))), None)
        if below:
            header_bottom = max(header_bottom, *(c.bottom for c in below))
        if times is None:
            start, end = DEFAULT_PAIR_TIMES.get(pair, (None, None))
            if start is None:
                continue
            times = tuple(dt.time.fromisoformat(f"{t:0>5}") for t in (start, end))
        slots.append(_Slot(pair, _at(day, times[0]), _at(day, times[1]), pc))
    slots.sort(key=lambda s: s.cell.x0)

    body = [c for c in cells if c.top >= header_bottom - 1 and c.text]
    groups: list[tuple[str, _Cell]] = []
    lesson_cells: list[_Cell] = []
    for c in body:
        if group_header.x0 - 1 <= (c.x0 + c.x1) / 2 <= group_header.x1 + 1:
            name = re.sub(r"\s+", "", c.text).replace("K", "К")
            if re.search(r"\d", name):
                groups.append((name, c))
        else:
            lesson_cells.append(c)

    labels = [c for c in lesson_cells if _is_time_label(c.text)]
    lessons: list[Lesson] = []
    for c in lesson_cells:
        if c in labels:
            continue
        owners = [name for name, g in groups if c.y_overlap(g) > 0.5 * min(c.height, g.height)]
        covered = [s for s in slots if c.x_overlap(s.cell) > 0.5 * min(c.width, s.cell.width)]
        if not owners or not covered:
            continue
        label = next((lb for lb in labels if abs(lb.bottom - c.top) < 2 and c.x_overlap(lb) > 0.5 * c.width), None)
        for info in parse_cell_text(_join_wrapped(c.lines, c.width)):
            if info.time is None and label is not None:
                info.time = parse_time_range(label.text)
            start, end = _cell_times(c, covered, info.time, day)
            pair = next((s.pair for s in slots if s.start <= start < s.end), covered[0].pair)
            for group in owners:
                lessons.append(Lesson(day=day, group=group, pair=pair, start=start, end=end,
                                      subject=info.subject, subgroup=info.subgroup, teachers=info.teachers,
                                      rooms=info.rooms, details=info.details))
    return lessons, slots


Period = tuple[dt.datetime, dt.datetime]


def _periods(pairs: list[Period]) -> list[Period]:
    """Every lesson period of the day: the hours of two-hour pairs, or whole short pairs."""
    periods = set()
    for start, end in pairs:
        if end - start >= 2 * HOUR:
            periods.add((start, start + HOUR))
            periods.add((end - HOUR, end))
        else:
            periods.add((start, end))
    return sorted(periods)


def break_before(moment: dt.datetime, pairs: list[Period]) -> dt.datetime:
    """Start of the break before ``moment``: the end of the last period ending by then.

    With no period before it (the first pair) there is no break, so it's
    ``moment`` itself.
    """
    ends = [end for _, end in _periods(pairs) if end <= moment]
    return max(ends) if ends else moment


def line_send_time(start: dt.datetime, pairs: list[Period]) -> dt.datetime:
    """When the line message for a class starting at ``start`` is sent.

    At the start of the break before the previous pair on the bell schedule
    (whether or not the group has a class then). A class in the first pair has
    no previous pair; its message goes at the start of its own break, which
    for 8:00 is 8:00 itself.
    """
    previous = [p for p in pairs if p[1] <= start]
    anchor = max(previous, key=lambda p: p[1])[0] if previous else start
    return break_before(anchor, pairs)


def parse_pdf(data: bytes, day: dt.date) -> list[Lesson]:
    lessons: list[Lesson] = []
    pairs: dict[int, Period] = {}
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in pdf.pages:
            for table in page.find_tables():
                found, slots = _parse_table(page, table, day)
                lessons += found
                for s in slots:
                    pairs.setdefault(s.pair, (s.start, s.end))
            page.close()
    bells = sorted(pairs.values())
    unique: dict[str, Lesson] = {}
    for lesson in lessons:
        lesson.send_at = line_send_time(lesson.start, bells)
        unique.setdefault(lesson.key, lesson)
    return sorted(unique.values(), key=lambda x: (x.group, x.start, x.subgroup or 0))


# --- fetching -------------------------------------------------------------------

PDF_LINK = re.compile(r"""href=["']([^"']*?(\d{2})\.(\d{2})\.(\d{4})[^"'/]*\.pdf)["']""", re.I)
USER_AGENT = "TelegramQ timetable reader"
KEEP_DAYS = 14  # stored days older than this are deleted


def find_pdf_links(page: str, base_url: str) -> dict[dt.date, str]:
    """The daily timetable PDFs linked from the schedule page, by day (a later link wins)."""
    links: dict[dt.date, str] = {}
    for m in PDF_LINK.finditer(page):
        href, day, month, year = m.groups()
        try:
            date = dt.date(int(year), int(month), int(day))
        except ValueError:
            continue
        links[date] = urljoin(base_url, html.unescape(href))
    return links


def today() -> dt.date:
    return dt.datetime.now(MINSK).date()


def lesson_row(lesson: Lesson) -> dict[str, Any]:
    return {
        "lesson_key": lesson.key,
        "day": lesson.day.isoformat(),
        "group_name": lesson.group,
        "pair": lesson.pair,
        "start_at": lesson.start.timestamp(),
        "end_at": lesson.end.timestamp(),
        "send_at": lesson.send_at.timestamp(),
        "subject": lesson.subject,
        "subgroup": lesson.subgroup,
        "teachers": lesson.teachers,
        "rooms": lesson.rooms,
        "details": lesson.details,
    }


class Timetable:
    """Keeps the stored timetable in sync with the PDFs published on the schedule page."""

    MANUAL_REFRESH_GAP = 60  # seconds

    def __init__(self, db, url: str, interval_seconds: float,
                 on_change: Callable[[], Awaitable[None]] | None = None,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.db = db
        self.url = url
        self.interval = interval_seconds
        self.on_change = on_change
        self.transport = transport  # for tests
        self.last_attempt: float | None = None
        self.last_success: float | None = None
        self.last_error: str | None = None
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if self.url:
            self._task = asyncio.create_task(self._loop(), name="timetable")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _loop(self) -> None:
        while True:
            await self.refresh()
            await asyncio.sleep(self.interval)

    def status(self) -> dict[str, Any]:
        return {
            "enabled": bool(self.url),
            "source": self.url,
            "updated_at": self.last_success * 1000 if self.last_success else None,
            "error": self.last_error,
            "refreshing": self._lock.locked(),
        }

    async def refresh(self, manual: bool = False) -> None:
        if not self.url or self._lock.locked():
            return
        if manual and self.last_attempt and time.time() - self.last_attempt < self.MANUAL_REFRESH_GAP:
            return
        async with self._lock:
            self.last_attempt = time.time()
            try:
                changed, errors = await self._refresh()
            except Exception as e:
                log.warning("Could not read the timetable from %s: %s", self.url, e)
                self.last_error = f"Could not read the timetable page: {e}"
                return
            self.last_success = time.time()
            self.last_error = "; ".join(errors) or None
        if changed and self.on_change:
            await self.on_change()

    async def _refresh(self) -> tuple[bool, list[str]]:
        changed = False
        errors: list[str] = []
        first = today()
        async with httpx.AsyncClient(timeout=30, follow_redirects=True, transport=self.transport,
                                     headers={"User-Agent": USER_AGENT}) as client:
            page = await client.get(self.url)
            page.raise_for_status()
            links = find_pdf_links(page.text, str(page.url))
            wanted = {d: u for d, u in links.items() if d >= first}
            past = [d for d in links if d < first]
            if past:  # the latest past day, so the groups are known before the next day is published
                wanted.setdefault(max(past), links[max(past)])
            known = await self.db.schedule_days()
            for day, url in sorted(wanted.items()):
                if known.get(day.isoformat(), {}).get("url") == url:
                    continue
                try:
                    response = await client.get(url)
                    response.raise_for_status()
                    lessons = await asyncio.to_thread(parse_pdf, response.content, day)
                except Exception as e:
                    log.warning("Could not read the timetable for %s (%s): %s", day, url, e)
                    errors.append(f"{day:%d.%m}: {e}")
                    continue
                if not lessons:  # keep what we have rather than wipe the day
                    log.warning("No classes found in %s", url)
                    errors.append(f"{day:%d.%m}: no classes found in the PDF")
                    continue
                await self.db.replace_day(day.isoformat(), url, [lesson_row(x) for x in lessons])
                log.info("Timetable for %s: %d classes from %s", day, len(lessons), url)
                changed = True
        await self.db.prune_days((first - dt.timedelta(days=KEEP_DAYS)).isoformat())
        return changed, errors
