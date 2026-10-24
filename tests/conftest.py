"""
Shared setup: a throwaway data directory (set before any service module is
imported, since ``services.db`` reads it at import) and ``appcode`` on the
path, as service.py arranges it.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_DATA = tempfile.mkdtemp(prefix="basilisk-test-")
os.environ["BASILISK_DATA_DIR"] = _DATA
os.environ.setdefault("BASILISK_SQLITE_ROOT", str(Path(_DATA) / "sqlite"))
os.environ["BASILISK_UPDATE_CHECK"] = "off"
sys.path.insert(0, str(ROOT / "appcode"))
