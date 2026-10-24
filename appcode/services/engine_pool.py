"""
SQLAlchemy engines, one per ``(user, connection, database)``.

**Not a BFF module, on purpose.** pytincture re-executes a BFF module's source
on every call, so a cache defined in ``sql_service.py`` would be a new, empty
dict per request (see Monguana's and IguanaXterm's CLAUDE.md). This module is
imported normally and cached in ``sys.modules``, so ``pool`` is a singleton.

A SQLAlchemy ``Engine`` is itself a thread-safe connection pool. PostgreSQL
and SQL Server need one per database (a connection is bound to its
database); MySQL and SQLite use one per profile (``Engine.database_for``
returns None).

The profile is re-read on every call (one indexed SQLite lookup) and the
engine rebuilt when its fingerprint changes, so an edited profile applies at
once. Idle engines are disposed after 15 minutes.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from typing import Any, Optional

from services import engines
from services.db import fetch_connection

_IDLE_TIMEOUT_SECONDS = 15 * 60


class ProfileNotFound(LookupError):
    pass


def fingerprint(spec: Any, profile: dict, database: Optional[str]) -> str:
    material = json.dumps([spec.name, spec.fingerprint_options(profile, database)],
                          sort_keys=True, default=str).encode()
    return hashlib.sha256(material).hexdigest()


class _Entry:
    __slots__ = ("engine", "fingerprint", "last_used")

    def __init__(self, engine: Any, digest: str) -> None:
        self.engine = engine
        self.fingerprint = digest
        self.last_used = time.monotonic()


def build_engine(spec: Any, profile: dict, database: Optional[str]) -> Any:
    from sqlalchemy import create_engine, event

    kwargs = {"pool_pre_ping": True, "pool_recycle": 1800}
    if spec.name != "sqlite":
        kwargs.update(pool_size=3, max_overflow=5, pool_timeout=15)
    kwargs.update(spec.engine_kwargs(profile))
    engine = create_engine(spec.url(profile, database), **kwargs)
    # Only what on_connect needs: never keep the password on the hook.
    session_profile = {"read_only": bool(profile.get("read_only"))}

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_conn, _record):  # noqa: ANN001 - SQLAlchemy hook
        spec.on_connect(dbapi_conn, session_profile)

    return engine


class EnginePool:
    def __init__(self) -> None:
        self._entries: dict[tuple, _Entry] = {}
        self._lock = threading.Lock()

    def open(self, user_id: int, conn_id: int, database: Optional[str] = None) -> tuple:
        """``(engine, spec, profile)`` for a profile the caller owns."""
        profile = fetch_connection(int(conn_id), int(user_id))
        if profile is None:
            raise ProfileNotFound("Connection not found")
        spec = engines.get(profile.get("engine") or "")
        target = spec.database_for(profile, database)
        digest = fingerprint(spec, profile, target)
        key = (int(user_id), int(conn_id), target or "")

        stale: list = []
        with self._lock:
            stale.extend(self._evict_idle(keep=key))
            entry = self._entries.get(key)
            if entry is not None and entry.fingerprint != digest:
                stale.append(entry.engine)
                entry = None
            if entry is None:
                entry = _Entry(build_engine(spec, profile, target), digest)
                self._entries[key] = entry
            entry.last_used = time.monotonic()
            engine = entry.engine
        for old in stale:
            dispose_quietly(old)
        return engine, spec, profile

    def close(self, user_id: int, conn_id: int) -> None:
        """Every engine of one profile (all its databases)."""
        with self._lock:
            keys = [key for key in self._entries if key[:2] == (int(user_id), int(conn_id))]
            entries = [self._entries.pop(key) for key in keys]
        for entry in entries:
            dispose_quietly(entry.engine)

    def close_user(self, user_id: int) -> None:
        with self._lock:
            keys = [key for key in self._entries if key[0] == int(user_id)]
            entries = [self._entries.pop(key) for key in keys]
        for entry in entries:
            dispose_quietly(entry.engine)

    def open_ids(self, user_id: int) -> list[int]:
        with self._lock:
            return sorted({key[1] for key in self._entries if key[0] == int(user_id)})

    def close_all(self) -> None:
        with self._lock:
            entries = list(self._entries.values())
            self._entries.clear()
        for entry in entries:
            dispose_quietly(entry.engine)

    def _evict_idle(self, keep: tuple) -> list:
        cutoff = time.monotonic() - _IDLE_TIMEOUT_SECONDS
        idle = [key for key, entry in self._entries.items()
                if key != keep and entry.last_used < cutoff]
        return [self._entries.pop(key).engine for key in idle]


def dispose_quietly(engine: Any) -> None:
    try:
        engine.dispose()
    except Exception:  # noqa: BLE001 - a dead server is not an error here
        pass


pool = EnginePool()


def error_text(exc: BaseException) -> str:
    """
    The useful part of a driver error: SQLAlchemy wraps it with the statement
    and a link to its docs; the person needs the server's message.
    """
    original = getattr(exc, "orig", None) or exc
    text = str(original).strip() or original.__class__.__name__
    # pymssql: (code, b'message DB-Lib error message ...')
    args = getattr(original, "args", ())
    if len(args) == 2 and isinstance(args[1], bytes):
        text = args[1].decode("utf-8", "replace")
        text = text.split("DB-Lib error message")[0].strip().rstrip(".") + "."
    text = text.split("\n(Background on this error")[0]
    text = text.split("\n[SQL:")[0]
    return text.strip()[:2000]
