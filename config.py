"""
config.py — Central configuration for JobHunter.

Loads all secrets from environment variables (via .env file).
Never hardcode API keys here — use the .env file.
"""

import os
import sys
from pathlib import Path
from dotenv import load_dotenv

# ── Load .env from project root ──────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _require_env(key: str) -> str:
    """Return env var value or exit with a clear error."""
    value = os.getenv(key)
    if not value:
        print(f"[FATAL] Missing required environment variable: {key}")
        sys.exit(1)
    return value


# ── API Keys & Tokens ────────────────────────────────────────────────────────
GEMINI_API_KEY: str = _require_env("GEMINI_API_KEY")
TELEGRAM_TOKEN: str = _require_env("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID: str = _require_env("TELEGRAM_CHAT_ID")

# ── Google Sheets ─────────────────────────────────────────────────────────────
SHEETS_CREDENTIALS_PATH: str = os.getenv(
    "SHEETS_CREDENTIALS_PATH",
    str(BASE_DIR / "sheets_credentials.json"),
)
SHEET_ID: str = _require_env("SHEET_ID")

# ── Gmail ─────────────────────────────────────────────────────────────────────
GMAIL_CREDENTIALS_PATH: str = os.getenv(
    "GMAIL_CREDENTIALS_PATH",
    str(BASE_DIR / "gmail_credentials.json"),
)
GMAIL_TOKEN_PATH: str = os.getenv(
    "GMAIL_TOKEN_PATH",
    str(BASE_DIR / "credentials" / "token.json"),
)

# ── Wellfound Auth ────────────────────────────────────────────────────────────
WELLFOUND_EMAIL: str = os.getenv("WELLFOUND_EMAIL", "")
WELLFOUND_PASSWORD: str = os.getenv("WELLFOUND_PASSWORD", "")

# ── Paths ─────────────────────────────────────────────────────────────────────
DATA_DIR: Path = BASE_DIR / "data"
BASE_RESUME_PATH: Path = DATA_DIR / "base_resume.docx"
RESUME_VERSIONS_DIR: Path = DATA_DIR / "resume_versions"
CREDENTIALS_DIR: Path = BASE_DIR / "credentials"
DB_PATH: Path = BASE_DIR / "jobhunter.db"

# Ensure key directories exist
RESUME_VERSIONS_DIR.mkdir(parents=True, exist_ok=True)
CREDENTIALS_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)

# ── Constants ─────────────────────────────────────────────────────────────────
# Companies to never apply to
BLACKLIST: list[str] = [
    # Add company names here, e.g.:
    # "Scam Corp",
    # "Toxic Inc",
]

# Scraper settings
MAX_JOB_AGE_HOURS: int = 48  # Only process jobs posted within this window
MAX_DAILY_APPLICATIONS: int = 15  # Hard cap on daily applications
APPLICATION_DELAY_MIN: int = 180  # Min seconds between applications (3 min)
APPLICATION_DELAY_MAX: int = 300  # Max seconds between applications (5 min)

# Scoring thresholds
SCORE_APPLY_THRESHOLD: int = 7   # Minimum score to auto-queue for apply
SCORE_MANUAL_THRESHOLD: int = 9  # Score at or above requires Telegram approval

# Gemini model
GEMINI_MODEL: str = "gemini-1.5-flash"

# Google Sheets worksheet name
SHEET_WORKSHEET_NAME: str = "Applications"
