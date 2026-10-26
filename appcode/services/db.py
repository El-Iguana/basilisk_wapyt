"""
SQLite storage and Fernet credential encryption. Server-side only.

Saved connection profiles hold database passwords, so they are encrypted at rest with a key that lives outside the database. Losing
``secret.key`` means losing every stored credential — back it up, or pin it
with ``BASILISK_SECRET_KEY``.

The original Basilisk kept connection passwords in plaintext and had a single
shared ``admin``/``admin`` login stored in an ``app_settings`` table. This is
multi-user: every profile belongs to one account, and there is no unscoped read
of the ``connections`` table anywhere.
"""
from __future__ import annotations

import json
import os
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

import bcrypt
from cryptography.fernet import Fernet

DATA_DIR = Path(os.environ.get("BASILISK_DATA_DIR", Path(__file__).resolve().parents[2] / "data"))
DB_PATH = DATA_DIR / "basilisk.db"
KEY_PATH = DATA_DIR / "secret.key"
SESSION_KEY_PATH = DATA_DIR / "session.key"
# Windows opens os.open() files in text mode unless told otherwise; the keys
# are bytes. (The 0o600 mode is ignored there: %LOCALAPPDATA% is per-user.)
_O_BINARY = getattr(os, "O_BINARY", 0)

_CRED_PREFIX = "fernet:"

# bcrypt cost. 12 is ~250ms per verify, which is the point; pytincture
# throttles login attempts on top of it.
_BCRYPT_ROUNDS = 12

_fernet: Optional[Fernet] = None
_fernet_lock = threading.Lock()


def _load_fernet() -> Fernet:
    """Resolve the encryption key once per process, behind a lock."""
    global _fernet
    if _fernet is not None:
        return _fernet
    with _fernet_lock:
        if _fernet is not None:
            return _fernet
        env_key = os.environ.get("BASILISK_SECRET_KEY", "").strip()
        if env_key:
            _fernet = Fernet(env_key.encode())
            return _fernet
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        if KEY_PATH.exists():
            _fernet = Fernet(KEY_PATH.read_bytes().strip())
            return _fernet
        key = Fernet.generate_key()
        # Created with the restrictive mode already in place: chmod after the
        # fact leaves a window where the key is world-readable.
        fd = os.open(KEY_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_BINARY, 0o600)
        try:
            os.write(fd, key)
        finally:
            os.close(fd)
        _fernet = Fernet(key)
        return _fernet


def session_secret() -> str:
    """
    The cookie-signing secret for pytincture's session middleware.

    Persisted rather than generated per boot, or every restart would sign
    everybody out. ``BASILISK_SESSION_SECRET`` overrides it.
    """
    from_env = os.environ.get("BASILISK_SESSION_SECRET", "").strip()
    if from_env:
        return from_env
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if SESSION_KEY_PATH.exists():
        existing = SESSION_KEY_PATH.read_text(encoding="ascii").strip()
        if existing:
            return existing
    secret = secrets.token_urlsafe(48)
    fd = os.open(SESSION_KEY_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | _O_BINARY, 0o600)
    try:
        os.write(fd, secret.encode())
    finally:
        os.close(fd)
    return secret


def encrypt(value: str) -> str:
    if not value:
        return ""
    return _CRED_PREFIX + _load_fernet().encrypt(value.encode()).decode()


def decrypt(value: str) -> str:
    if not value:
        return ""
    if not value.startswith(_CRED_PREFIX):
        return value
    return _load_fernet().decrypt(value[len(_CRED_PREFIX):].encode()).decode()


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(_BCRYPT_ROUNDS)).decode()


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), hashed.encode())
    except (ValueError, TypeError):
        return False


@contextmanager
def get_db() -> Iterator[sqlite3.Connection]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    username   TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    pw_hash    TEXT    NOT NULL,
    is_admin   INTEGER NOT NULL DEFAULT 0,
    -- 1 while the password is one this person did not choose: the install
    -- default, or one an administrator set. The app asks them to change it
    -- on every load until they do.
    must_change_pw INTEGER NOT NULL DEFAULT 0,
    created_at TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- A connection profile. `engine` names services/engines (postgresql, mysql,
-- mssql, sqlite). `database` is the one to open first (for SQLite: the file,
-- relative to BASILISK_SQLITE_ROOT). `options` holds the engine's extra
-- settings (sslmode, ...) as JSON, encrypted like `password`.
-- `env` is the connection type: '' / dev / test / prod (prod confirms writes).
-- `ssh` is the tunnel's settings (host, port, username, password or
-- private_key + passphrase, the pinned host_key) as JSON, encrypted.
CREATE TABLE IF NOT EXISTS connections (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name         TEXT    NOT NULL,
    engine       TEXT    NOT NULL,
    folder       TEXT    NOT NULL DEFAULT '',
    host         TEXT    NOT NULL DEFAULT '',
    port         INTEGER NOT NULL DEFAULT 0,
    database     TEXT    NOT NULL DEFAULT '',
    username     TEXT    NOT NULL DEFAULT '',
    password     TEXT    NOT NULL DEFAULT '',
    options      TEXT    NOT NULL DEFAULT '',
    read_only    INTEGER NOT NULL DEFAULT 0,
    timeout_s    INTEGER NOT NULL DEFAULT 60,
    env          TEXT    NOT NULL DEFAULT '',
    notes        TEXT    NOT NULL DEFAULT '',
    created_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    ssh          TEXT    NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_connections_user ON connections(user_id);

-- Per-user UI state that follows the person, not the browser (column
-- layouts, settings, SQL editor drafts). Opaque JSON owned by the UI.
CREATE TABLE IF NOT EXISTS ui_state (
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    key        TEXT    NOT NULL,
    payload    TEXT    NOT NULL,
    updated_at TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, key)
);

-- Every statement run from a SQL editor, newest kept (history.py trims it).
CREATE TABLE IF NOT EXISTS query_history (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    conn_id    INTEGER NOT NULL,
    database   TEXT    NOT NULL DEFAULT '',
    sql        TEXT    NOT NULL,
    ok         INTEGER NOT NULL,
    rows       INTEGER,
    ms         INTEGER,
    ran_at     TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_history_user ON query_history(user_id, conn_id, id);
"""


# The first account's password when BASILISK_ADMIN_PASS is not set. Easy on
# purpose: the account is flagged, and the app asks for a new password on
# every load until it is changed. "changeme" was the default before.
DEFAULT_ADMIN_PASSWORD = "change_me"
_KNOWN_DEFAULTS = (DEFAULT_ADMIN_PASSWORD, "changeme")


def _migrate(conn: sqlite3.Connection) -> None:
    """Columns added after a database was first created."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(connections)")}
    if "ssh" not in columns:        # SSH tunnels (milestone 3)
        conn.execute("ALTER TABLE connections ADD COLUMN ssh TEXT NOT NULL DEFAULT ''")


def init_db() -> None:
    """Create the schema, migrate it, and seed the first admin."""
    with get_db() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)

        if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
            admin_user = os.environ.get("BASILISK_ADMIN_USER", "admin")
            from_env = os.environ.get("BASILISK_ADMIN_PASS", "")
            admin_pass = from_env or DEFAULT_ADMIN_PASSWORD
            # Flagged either way: nobody chose this password in the app.
            conn.execute(
                "INSERT INTO users (username, pw_hash, is_admin, must_change_pw) VALUES (?, ?, 1, 1)",
                (admin_user, hash_password(admin_pass)),
            )
            source = (
                "the password from BASILISK_ADMIN_PASS" if from_env
                else f"the default password {DEFAULT_ADMIN_PASSWORD!r}"
            )
            banner = "=" * 58
            # A password from the environment is deliberately not echoed.
            print(
                f"\n{banner}\n"
                f"  Created the initial admin account: {admin_user!r}\n"
                f"  with {source}.\n"
                f"  Basilisk asks for a new one on every load until it is changed.\n"
                f"{banner}\n",
                flush=True,
            )


def fetch_connection(conn_id: int, user_id: int) -> Optional[dict]:
    """
    One profile with its secrets decrypted, or ``None``.

    Every caller scopes by ``user_id``: one user's saved servers are never
    reachable from another's session.
    """
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM connections WHERE id = ? AND user_id = ?",
            (int(conn_id), int(user_id)),
        ).fetchone()
    if not row:
        return None
    profile = dict(row)
    profile["password"] = decrypt(profile.get("password") or "")
    profile["options"] = decode_options(profile.get("options") or "")
    profile["ssh"] = decode_options(profile.get("ssh") or "")
    profile["read_only"] = bool(profile.get("read_only"))
    return profile


def pin_ssh_host_key(conn_id: int, user_id: int, serialized: str) -> None:
    """Pin a tunnel's host key on first use; a key already pinned is never replaced here."""
    with get_db() as conn:
        row = conn.execute("SELECT ssh FROM connections WHERE id = ? AND user_id = ?",
                           (int(conn_id), int(user_id))).fetchone()
        if not row:
            return
        settings = decode_options(row["ssh"] or "")
        if not settings or settings.get("host_key"):
            return
        settings["host_key"] = serialized
        conn.execute("UPDATE connections SET ssh = ? WHERE id = ? AND user_id = ?",
                     (encode_options(settings), int(conn_id), int(user_id)))


def encode_options(options: Optional[dict]) -> str:
    """A backend's settings as stored: JSON, encrypted (they may hold secrets)."""
    if not options:
        return ""
    return encrypt(json.dumps(options, sort_keys=True))


def decode_options(stored: str) -> dict:
    if not stored:
        return {}
    value = json.loads(decrypt(stored))
    return value if isinstance(value, dict) else {}
