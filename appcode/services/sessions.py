"""
Manual commit mode: one pinned database connection per SQL editor tab.

In auto-commit (the default) every run borrows a pooled connection and
commits each statement. In manual mode the tab owns a connection for as long
as it stays in that mode, statements run inside one transaction, and nothing
is kept until the person presses Commit. Server-side only, a **plain module**
(not a BFF), so the registry survives pytincture re-executing BFF modules.

* A session is keyed by ``(user_id, session_id)``: the page makes the id per
  tab, and the user id scopes it, so one person cannot reach another's.
* It is bound to one connection profile and one database (PostgreSQL and SQL
  Server connections cannot switch). Switching database with work pending is
  refused; with nothing pending the connection is swapped.
* **Idle transactions are rolled back** after ``IDLE_SECONDS``: an open
  transaction holds locks on the server. A reaper thread does it, and the id
  is remembered for an hour so the page can say what happened instead of
  silently starting afresh.
* At most ``MAX_PER_USER`` sessions per person: each pins a pool connection.
* A per-session lock refuses a second run while one is in progress.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Optional

from services.engine_pool import error_text, pool

IDLE_SECONDS = 10 * 60
REAP_EVERY_SECONDS = 30
EXPIRED_KEEP_SECONDS = 3600
MAX_PER_USER = 5


class SessionError(Exception):
    """Shown to the person as it is."""


class Session:
    __slots__ = ("user_id", "sid", "conn_id", "database", "spec", "engine", "raw", "dbapi",
                 "lock", "statements", "last_used", "started")

    def __init__(self, user_id: int, sid: str, conn_id: int, database: str, spec: Any,
                 engine: Any) -> None:
        self.user_id = user_id
        self.sid = sid
        self.conn_id = conn_id
        self.database = database
        self.spec = spec
        self.engine = engine
        self.raw = engine.raw_connection()
        self.dbapi = self.raw.dbapi_connection
        self.lock = threading.Lock()
        self.statements = 0
        self.last_used = time.monotonic()
        self.started: Optional[float] = None
        spec.begin_manual(self.dbapi)

    def state(self) -> dict:
        try:
            found = self.spec.transaction_state(self.dbapi, self.statements)
        except Exception:  # noqa: BLE001 - a dead connection has no transaction
            found = {"open": False, "failed": True}
        if not found["open"]:
            self.started = None
        elif self.started is None:
            self.started = time.time()
        return {
            "session": self.sid, "open": bool(found["open"]), "failed": bool(found["failed"]),
            "statements": self.statements, "database": self.database,
            "since": self.started, "idle_limit": IDLE_SECONDS,
        }

    def release(self, *, invalidate: bool = False) -> None:
        """Roll back and hand the connection back to the pool."""
        try:
            if not invalidate:
                self.spec.set_timeout(self.dbapi, 0)
                self.spec.end_manual(self.dbapi)
        except Exception:  # noqa: BLE001 - a broken connection cannot be reset
            invalidate = True
        try:
            if invalidate:
                self.raw.invalidate()
            else:
                self.raw.close()
        except Exception:  # noqa: BLE001
            pass


_sessions: dict = {}
_expired: dict = {}           # (user, sid) -> (monotonic time, reason)
_lock = threading.Lock()
_reaper: Optional[threading.Thread] = None


def _key(user_id: int, sid: str) -> tuple:
    return (int(user_id), str(sid))


def _start_reaper() -> None:
    global _reaper
    if _reaper is not None and _reaper.is_alive():
        return
    _reaper = threading.Thread(target=_reap_forever, name="basilisk-session-reaper", daemon=True)
    _reaper.start()


def _reap_forever() -> None:
    while True:
        time.sleep(REAP_EVERY_SECONDS)
        try:
            reap()
        except Exception:  # noqa: BLE001 - the reaper must outlive any one failure
            pass


def reap(now: Optional[float] = None) -> int:
    """Roll back sessions idle past the limit. Busy sessions are left alone."""
    now = time.monotonic() if now is None else now
    victims = []
    with _lock:
        for key, session in list(_sessions.items()):
            if now - session.last_used > IDLE_SECONDS and session.lock.acquire(blocking=False):
                victims.append((key, _sessions.pop(key)))
                _expired[key] = (now, f"rolled back after {IDLE_SECONDS // 60} minutes idle")
        for key, (when, _reason) in list(_expired.items()):
            if now - when > EXPIRED_KEEP_SECONDS:
                _expired.pop(key, None)
    for _key_, session in victims:
        session.release()
        session.lock.release()
    return len(victims)


def acquire(user_id: int, sid: str, conn_id: int, database: str) -> Session:
    """
    The tab's session, opened if needed. Raises SessionError when the tab's
    previous transaction was ended behind its back (idle, disconnected), so
    the page can say so once; the next call opens a fresh one.
    """
    key = _key(user_id, sid)
    stale: Optional[Session] = None
    with _lock:
        expired = _expired.pop(key, None)
        if expired is not None:
            raise SessionError(f"The open transaction was {expired[1]}. Nothing was run; "
                               "run again to start a new transaction.")
        session = _sessions.get(key)
        if session is not None and (session.conn_id != int(conn_id)
                                    or session.database != (database or "")):
            if not session.lock.acquire(blocking=False):
                raise SessionError("A statement is still running; cancel it first.")
            try:
                pending = session.statements and session.state()["open"]
            finally:
                session.lock.release()
            if pending:
                raise SessionError("Commit or roll back before switching database.")
            stale = _sessions.pop(key)
            session = None
        if session is None:
            if sum(1 for k in _sessions if k[0] == int(user_id)) >= MAX_PER_USER:
                raise SessionError(
                    f"At most {MAX_PER_USER} editors can be in manual commit mode at once.")
            engine, spec, profile = pool.open(user_id, conn_id, database)
            if profile.get("read_only"):
                raise SessionError("A read-only connection has nothing to commit; "
                                   "it runs in auto-commit and rolls back every transaction.")
            session = Session(int(user_id), str(sid), int(conn_id), database or "", spec, engine)
            _sessions[key] = session
            _start_reaper()
        session.last_used = time.monotonic()
    if stale is not None:
        stale.release()
    return session


def peek(user_id: int, sid: str) -> Optional[Session]:
    with _lock:
        return _sessions.get(_key(user_id, sid))


def drop(user_id: int, sid: str, reason: str) -> None:
    """Forget a session whose connection died; the page is told ``reason`` once."""
    with _lock:
        session = _sessions.pop(_key(user_id, sid), None)
        if session is not None:
            _expired[_key(user_id, sid)] = (time.monotonic(), reason)
    if session is not None:
        session.release(invalidate=True)


def _finish(user_id: int, sid: str, commit: bool) -> dict:
    session = peek(user_id, sid)
    if session is None:
        with _lock:
            expired = _expired.pop(_key(user_id, sid), None)
        if expired is not None:
            raise SessionError(f"The transaction was already {expired[1]}.")
        return {"session": sid, "open": False, "failed": False, "statements": 0,
                "nothing": True}
    if not session.lock.acquire(blocking=False):
        raise SessionError("A statement is still running; cancel it first.")
    try:
        before = session.state()
        if commit and before["failed"]:
            raise SessionError("The transaction failed and can only be rolled back.")
        try:
            (session.dbapi.commit if commit else session.dbapi.rollback)()
        except Exception as exc:
            if commit:
                raise SessionError(f"Commit failed: {error_text(exc)}") from None
            raise
        count = session.statements
        session.statements = 0
        session.last_used = time.monotonic()
        state = session.state()
        state.update(done=count, was_open=before["open"])
        return state
    finally:
        session.lock.release()


def commit(user_id: int, sid: str) -> dict:
    return _finish(user_id, sid, True)


def rollback(user_id: int, sid: str) -> dict:
    return _finish(user_id, sid, False)


def end(user_id: int, sid: str) -> dict:
    """Leave manual mode (or close the tab): roll back and release."""
    with _lock:
        _expired.pop(_key(user_id, sid), None)
        session = _sessions.pop(_key(user_id, sid), None)
    if session is None:
        return {"ended": False, "statements": 0}
    acquired = session.lock.acquire(timeout=5)
    try:
        open_ = session.state()["open"]
        count = session.statements
        session.release(invalidate=not acquired)
    finally:
        if acquired:
            session.lock.release()
    return {"ended": True, "rolled_back": count if open_ else 0}


def end_for_conn(user_id: int, conn_id: int, reason: str) -> None:
    """A profile was edited, deleted or disconnected: end its sessions."""
    with _lock:
        keys = [k for k, s in _sessions.items() if k[0] == int(user_id) and s.conn_id == int(conn_id)]
        victims = [_sessions.pop(k) for k in keys]
        for k in keys:
            _expired[k] = (time.monotonic(), reason)
    for session in victims:
        acquired = session.lock.acquire(timeout=5)
        session.release(invalidate=not acquired)
        if acquired:
            session.lock.release()


def end_user(user_id: int) -> None:
    with _lock:
        keys = [k for k in _sessions if k[0] == int(user_id)]
        victims = [_sessions.pop(k) for k in keys]
    for session in victims:
        session.release(invalidate=True)


def close_all() -> None:
    with _lock:
        victims = list(_sessions.values())
        _sessions.clear()
    for session in victims:
        session.release()
