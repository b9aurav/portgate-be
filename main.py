"""
main.py — Root application entrypoint for PortGate.

Forwards to app.main for clean modular execution.
Supports both:
  uvicorn main:app --reload
  uvicorn app.main:app --reload
"""

import sys
from pathlib import Path

# Ensure portgate-be root is on sys.path
_root = Path(__file__).resolve().parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from app.main import app
from app.services.session_store import session_store
from app import config

MAGIC_LINK_SESSIONS = session_store.raw_sessions

__all__ = ["app", "session_store", "MAGIC_LINK_SESSIONS"]

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level=config.LOG_LEVEL,
    )
