from __future__ import annotations

import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env file from project root
_base_dir = Path(__file__).resolve().parent.parent
load_dotenv(_base_dir / ".env")

# ---------------------------------------------------------------------------
# Core Configuration
# ---------------------------------------------------------------------------
GOOGLE_API_KEY: str | None = os.getenv("GOOGLE_API_KEY")
GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
GEMINI_FALLBACK_MODEL: str = os.getenv("GEMINI_FALLBACK_MODEL", "gemini-3.8-flash")

_raw_origins: str = os.getenv("ALLOWED_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000")
ALLOWED_ORIGINS: list[str] = (
    ["*"]
    if _raw_origins.strip() == "*"
    else [o.strip() for o in _raw_origins.split(",") if o.strip()]
)

MAX_FILE_BYTES: int = 20 * 1024 * 1024  # 20 MB limit per document
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "info").lower()
SESSIONS_PERSIST_DIR: Path = _base_dir / "data" / "sessions"

# ---------------------------------------------------------------------------
# Supabase Persistence Configuration
# ---------------------------------------------------------------------------
SUPABASE_URL: str = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_KEY: str = (
    os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    or os.getenv("SUPABASE_SECRET_KEY")
    or os.getenv("SUPABASE_KEY")
    or ""
).strip()

SUPABASE_ENABLED: bool = bool(
    SUPABASE_URL
    and SUPABASE_KEY
    and not SUPABASE_URL.startswith("https://your-project")
    and not SUPABASE_KEY.startswith("your_supabase")
)
