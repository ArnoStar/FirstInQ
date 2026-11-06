# TelegramQ

Take a place in line for a class at the Minsk Radioengineering College
(МРК БГУИР). TelegramQ reads the college's published timetable, you pick your
group and a class, and when that class's line opens your **own Telegram
account** (not a bot) posts

```
{your place} {class}
```

e.g. `2 СУБД`, to the chat you chose, to the millisecond.

There is no free-form scheduling: you can't write your own text or pick your
own time. You can only take (or leave) a place in a line.

## How lines work

- **The timetable.** The college publishes one PDF per day on
  <https://mrk.bsuir.by/en/schedule/>. TelegramQ checks that page every 15
  minutes (`SCHEDULE_REFRESH_MINUTES`), downloads new or changed PDFs for today
  and later days, and reads every group's classes from them. Pair times come
  from each PDF's header, so shortened days (e.g. Friday afternoons) are right.
  A class can be a whole pair, one hour of a pair, a subgroup's class, or carry
  its own time (e.g. *Информационный час 13:10–13:30*).
- **When a line opens.** At the start of the break before the **previous
  pair** on the bell schedule, whether or not your group has a class then. For
  a class in pair 4 (13:40) that is 11:30, the end of pair 2. A class in pair 1
  has no previous pair; its line opens at the start of its own break, which
  for 8:00 is 8:00 itself.
- **Your place.** Each class has one line per chat (and forum topic). Places
  go in join order: the first person to take a place in that chat is 1, the
  next is 2, and so on. One place per person per line. If someone leaves,
  everyone behind them moves up a place, until 30 seconds before the line
  opens. From then on places are fixed, because the messages are being
  prepared.
- **Sending.** Everyone's message goes out at the same moment, in parallel. No
  one waits for anyone else's message, so `2 СУБД` may arrive before `1 СУБД`.
  The timing is the precise sender described below.
- **Timetable changes.** If a class you're in line for moves, your message
  moves with it. If it disappears from the timetable, your place is dropped
  and marked failed.

All times are Minsk time (UTC+3), whatever the server's or browser's time zone.

### Timing

TelegramQ fires the request itself rather than using Telegram's own
scheduling:

| When | What happens |
| --- | --- |
| T − 20 s | Make sure the account's connection is up and build the complete request (recipient, text with the current place, random id), so only I/O is left. |
| T − 3 s  | Ping Telegram up to 3 times. This warms the connection and measures the round-trip time (RTT). Pinging stops early if it would run into the send. |
| T − RTT/2 | Fire, half an RTT early, so the request **arrives** at Telegram at ~T. |

The last stretch uses a coarse sleep followed by a ~2 ms spin, so the timer
error is a few microseconds on an idle machine. After sending, the UI shows
the timer error, the RTT, how early it fired and the estimated arrival time.

The reference clock is the server's, so keep it NTP-synced (e.g.
`systemd-timesyncd` or `chrony`). The web UI measures the offset between the
browser and server clocks; countdowns use server time.

## Setup

1. Create an API app at <https://my.telegram.org> → *API development tools*
   and note the `api_id` and `api_hash`. These identify the app, not a
   user: you register once for the whole server, and every user still logs
   in with their own account.
2. Run `make run`. The first time, it creates `.env` from `.env.example`
   and stops. Fill in `TG_API_ID` and `TG_API_HASH`.
3. Run `make run` again. Dependencies are installed automatically.

   Other targets: `make dev` (auto-reload), `make test`, `make key` (prints
   a `SECRET_KEY`), `make clean`, and `make` to list them all. Change the
   address with `make run HOST=0.0.0.0 PORT=9000`.

4. Open <http://127.0.0.1:8080>, enter your phone number, then the code
   Telegram sends you (and your cloud password if 2FA is on).

**Run a single process.** The scheduler lives inside the web process, so
don't start uvicorn with `--workers > 1`.

To expose it beyond localhost, put it behind an HTTPS reverse proxy and set
`COOKIE_SECURE=true`.

### With Docker

With `.env` filled in (step 2 above), one command builds and starts it in
the background. It restarts automatically, including after a reboot:

```sh
make up        # then open http://127.0.0.1:8080
make logs      # follow the logs
make down      # stop
```

Without Make, use `docker compose up -d --build`.

- **Shared data.** The container uses the same `./data` folder as `make run`,
  so you stay logged in whichever way you start it. The folder is locked, so
  a second instance (e.g. `make run` while the container is up) refuses to
  start rather than sending every message twice.
- **Local only by default.** The port is published on `127.0.0.1`. Change it
  with `BIND`, `PORT` and `DATA_PATH` in `.env` (see `.env.example`).
- **VPNs.** Both commands work with a VPN on. Behind a tunnel with a
  smaller MTU (WireGuard uses 1420), Docker's default 1500-byte packets are
  silently dropped and connections stall. To avoid that:
  - The container network uses MTU 1420, so the connection to Telegram
    works. If your VPN's MTU is lower, set `DOCKER_MTU` in `.env`.
  - The build downloads packages with pip plus a small build-only hook
    (`docker/sitecustomize.py`) that asks servers for smaller TCP segments.
    Versions and hashes still come from `uv.lock`.
- **Clock.** Containers use the host's clock, so keep the host NTP-synced.

## Using it

- **Group:** pick your group at the top of the timetable. You see its classes
  from today on, with when each line opens and a countdown.
- **Take a place:** click *Take a place* on a class and choose the chat: search
  your chats (users, groups, channels you can post in, Saved Messages) or type
  `@username`, a `t.me/…` link, or a phone number from your contacts. For
  supergroups with topics, a *Topic* picker appears (default *General*); a
  topic link like `https://t.me/groupname/5` works too. The last chat you used
  is filled in next time.
- **In line:** your places, the exact message, when it goes out, and a
  countdown. *Leave* gives your place up. *History* shows sent messages with
  their timing.
- *Check for new days* re-reads the college's schedule page right away (at most
  once a minute).

If the server was down when a line opened, the message is still sent on
startup if it's at most `MISSED_GRACE_SECONDS` (default 60) late. Otherwise it
is marked *missed*.

## Security

- The Telethon session string is a full login to your Telegram account. It is
  stored **encrypted** (Fernet) in SQLite. The key comes from `SECRET_KEY`, or
  is generated once into `DATA_DIR/secret.key` (mode 600). Keep `data/` and
  the key private.
- Browser sessions use a random token in an `HttpOnly`, `SameSite=Lax` cookie.
  The database stores only its SHA-256 hash. State-changing API calls also
  require an `X-TelegramQ` header as CSRF protection.
- Each Telegram account has at most one TelegramQ authorization. Logging in
  again terminates the previous one. It appears as **TelegramQ** under
  Telegram → *Settings → Devices*, and ending it there stops all pending
  messages.
- *Sign out of this browser* keeps your places in line.
  *Disconnect Telegram account* logs the session out at Telegram, gives up
  your places (everyone behind you moves up) and deletes your data from the
  server.

Use your own account responsibly. Telegram limits or bans accounts that use
the API for spam or bulk messaging.

## Development

```sh
make test
```

The tests use a fake Telegram client to check timer precision, latency
compensation, slow-network behaviour, cancellation, missed messages, revoked
sessions, places in line and the HTTP API. The timetable parser is tested on
two real daily PDFs in `tests/fixtures/`.

```
app/
  main.py        FastAPI app: routes, auth cookies, error mapping
  timetable.py   reads the college's daily PDFs; when each class's line opens
  lines.py       places in line: join, leave, move up, follow timetable changes
  scheduler.py   precise per-message timers, RTT measurement, sending
  telegram.py    Telethon client pool, phone/code/2FA login, chat resolution
  db.py          SQLite (aiosqlite) storage
  security.py    session encryption, token hashing
  config.py      settings from environment / .env
static/          single-page UI (plain HTML/CSS/JS, no build step)
Dockerfile, compose.yaml   container image and one-command deployment
docker/          build-time hook that keeps downloads working behind VPNs
tests/
```

Not supported yet: QR-code login, and choosing a different moment for a line
to open.
