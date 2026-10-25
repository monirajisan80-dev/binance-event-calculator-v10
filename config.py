import os
import math
from dotenv import load_dotenv

load_dotenv()


def _req(name: str) -> str:
    v = os.getenv(name)
    if not v:
        raise RuntimeError(f"Missing required env var: {name}")
    return v


BOT_TOKEN = _req("BOT_TOKEN")
OWNER_ID = int(_req("OWNER_ID"))
if OWNER_ID <= 0:
    raise RuntimeError("OWNER_ID must be a positive Telegram user ID")
# Regular admins are appointed only by the owner through Telegram's /admin panel.

# Telethon user client (needed to read a public channel you don't admin)
API_ID = int(os.getenv("TG_API_ID", "0"))
API_HASH = os.getenv("TG_API_HASH", "")
SESSION_STRING = os.getenv("TG_SESSION_STRING", "")

DEFAULT_CHANNEL = os.getenv("DEFAULT_CHANNEL", "binance_announcements")
DEFAULT_CHANNELS = [c.strip() for c in os.getenv("DEFAULT_CHANNELS", DEFAULT_CHANNEL).split(",") if c.strip()]
DEFAULT_KEYWORDS = [k.strip() for k in os.getenv(
    "DEFAULT_KEYWORDS", "Spot Trading Tournament,Trade to Share").split(",") if k.strip()]

# Supabase PostgreSQL connection string (Project Settings -> Database -> Connection string).
# Prefer the Session pooler URI (port 5432) on Render; the Transaction pooler (6543) also works.
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
DB_POOL_MIN = int(os.getenv("DB_POOL_MIN", "1"))
DB_POOL_MAX = int(os.getenv("DB_POOL_MAX", "5"))
SCRAPE_INTERVAL_SEC = int(os.getenv("SCRAPE_INTERVAL_SEC", "300"))
REQUEST_DELAY_SEC = float(os.getenv("REQUEST_DELAY_SEC", "2.0"))
CLEANUP_DAYS = int(os.getenv("CLEANUP_DAYS", "30"))
FALLBACK_REWARD_CAP = float(os.getenv("FALLBACK_REWARD_CAP", os.getenv("USER_CAP_BNB", "0.05")))
FALLBACK_REWARD_TOKEN = os.getenv("FALLBACK_REWARD_TOKEN", "BNB").strip().upper()
if not math.isfinite(FALLBACK_REWARD_CAP) or FALLBACK_REWARD_CAP <= 0:
    raise RuntimeError("FALLBACK_REWARD_CAP must be a finite positive number")
SKIP_TOP = int(os.getenv("SKIP_TOP", "1000"))
# Health server: started automatically when PORT is set (Render Web Service sets it).
# Background Workers have no PORT, so nothing binds. Force with HEALTH_SERVER=1/0.
_port = os.getenv("PORT", "").strip()
PORT = int(_port or "10000")
HEALTH_SERVER = os.getenv("HEALTH_SERVER", "1" if _port else "0").strip().lower() in {"1", "true", "yes"}
