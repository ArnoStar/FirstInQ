import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

EXAMPLE_API_ID = "123456"
DEFAULT_SCHEDULE_URL = "https://mrk.bsuir.by/en/schedule/"
EXAMPLE_API_HASH = "0123456789abcdef0123456789abcdef"


class ConfigError(RuntimeError):
    pass


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    api_id: int
    api_hash: str
    secret_key: str | None
    data_dir: Path
    cookie_secure: bool
    web_session_days: int
    missed_grace_seconds: float
    schedule_url: str = DEFAULT_SCHEDULE_URL  # page linking the daily timetable PDFs; empty = don't fetch
    schedule_refresh_minutes: float = 15
    admins: frozenset[str] = field(default_factory=frozenset)  # Telegram user ids and phone numbers (digits)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "telegramq.sqlite3"


def parse_admins(value: str) -> frozenset[str]:
    """ADMINS: Telegram user ids or phone numbers (spaces and dashes allowed), separated by commas."""
    return frozenset(digits for token in re.split(r"[,;\n]+", value)
                     if (digits := re.sub(r"\D", "", token)))


def load_settings() -> Settings:
    api_id = os.environ.get("TG_API_ID", "").strip()
    api_hash = os.environ.get("TG_API_HASH", "").strip()
    if not api_id or not api_hash:
        raise ConfigError(
            "TG_API_ID and TG_API_HASH must be set in .env. "
            "Get them at https://my.telegram.org -> API development tools."
        )
    if api_id == EXAMPLE_API_ID or api_hash == EXAMPLE_API_HASH:
        raise ConfigError(
            "TG_API_ID and TG_API_HASH in .env are still the example values. "
            "Create an app at https://my.telegram.org -> API development tools "
            "and paste its api_id and api_hash."
        )
    if not api_id.isdigit():
        raise ConfigError("TG_API_ID must be the numeric api_id from https://my.telegram.org.")
    return Settings(
        api_id=int(api_id),
        api_hash=api_hash,
        secret_key=os.environ.get("SECRET_KEY", "").strip() or None,
        data_dir=Path(os.environ.get("DATA_DIR", "./data")).resolve(),
        cookie_secure=_bool(os.environ.get("COOKIE_SECURE")),
        web_session_days=int(os.environ.get("WEB_SESSION_DAYS", "30")),
        missed_grace_seconds=float(os.environ.get("MISSED_GRACE_SECONDS", "60")),
        schedule_url=os.environ.get("SCHEDULE_URL", DEFAULT_SCHEDULE_URL).strip(),
        schedule_refresh_minutes=max(1.0, float(os.environ.get("SCHEDULE_REFRESH_MINUTES", "15"))),
        admins=parse_admins(os.environ.get("ADMINS", "")),
    )
