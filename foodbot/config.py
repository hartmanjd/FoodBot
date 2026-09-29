from dataclasses import dataclass
import os
import re
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


@dataclass(frozen=True)
class Settings:
    token: str
    owner: int
    secret: str
    database: str = "data/foodbot.sqlite3"
    timezone: str = "America/Los_Angeles"
    day: int = 0
    checkin_time: str = "10:00"
    quiet_start: int = 21
    quiet_end: int = 9
    max_followups: int = 2
    instacart_key: str = ""
    instacart_env: str = "development"
    openai_key: str = ""
    openai_model: str = "gpt-4o-mini"

    @classmethod
    def from_env(cls):
        load_dotenv()
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        owner = os.getenv("TELEGRAM_OWNER_ID", "")
        secret = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")
        if not token or not owner.isdigit() or int(owner) <= 0:
            raise ValueError("Set TELEGRAM_BOT_TOKEN and a positive TELEGRAM_OWNER_ID. See docs/SETUP.md.")
        if not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", secret):
            raise ValueError("TELEGRAM_WEBHOOK_SECRET must be 32–256 letters, numbers, underscores or hyphens.")
        timezone = os.getenv("TIMEZONE", "America/Los_Angeles")
        ZoneInfo(timezone)
        day = DAYS.index(os.getenv("CHECKIN_DAY", "monday").lower())
        time = os.getenv("CHECKIN_TIME", "10:00")
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", time):
            raise ValueError("CHECKIN_TIME must be HH:MM in 24-hour time.")
        start, end = int(os.getenv("QUIET_START", "21")), int(os.getenv("QUIET_END", "9"))
        if not (0 <= start <= 23 and 0 <= end <= 23 and start != end):
            raise ValueError("Quiet hours must be distinct hours from 0 to 23.")
        hour = int(time[:2])
        quiet = start <= hour < end if start < end else hour >= start or hour < end
        if quiet:
            raise ValueError("Choose CHECKIN_TIME outside quiet hours.")
        followups = int(os.getenv("MAX_FOLLOWUPS", "2"))
        if not 0 <= followups <= 2:
            raise ValueError("MAX_FOLLOWUPS must be 0, 1, or 2.")
        env = os.getenv("INSTACART_ENV", "development")
        if env not in ("development", "production"):
            raise ValueError("INSTACART_ENV must be development or production.")
        return cls(token, int(owner), secret, os.getenv("DATABASE_PATH", "data/foodbot.sqlite3"),
                   timezone, day, time, start, end, followups,
                   os.getenv("INSTACART_API_KEY", ""), env,
                   os.getenv("OPENAI_API_KEY", ""), os.getenv("OPENAI_MODEL", "gpt-4o-mini"))
