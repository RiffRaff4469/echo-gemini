"""Pytest bootstrap for the server tests.

The server modules import each other flat (``from protocol import ...``) because
they run as ``python server/main.py``, which puts ``server/`` on ``sys.path``.
pytest is invoked from the repo root, so it needs the same thing.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SERVER_DIR))

# Tests must never pick up a developer's real .env -- no live key, no real
# secret, no surprise wake-model downloads.
os.environ["ECHO_SHARED_SECRET"] = "test-secret-not-a-real-one"
os.environ.pop("GEMINI_API_KEY", None)
os.environ["WAKE_ENABLED"] = "false"
os.environ["WAKE_DOWNLOAD_MODELS"] = "false"
