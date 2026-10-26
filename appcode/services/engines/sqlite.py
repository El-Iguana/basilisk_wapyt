"""
SQLite files, confined to ``BASILISK_SQLITE_ROOT``.

The server opens the file, so a profile must never name an arbitrary path:
it could open (or create) any file the server can reach, Basilisk's own
database of credentials included. A profile names a path *relative to the
root*, and :func:`resolve_file` checks it on save **and** on every open.
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any, Optional

from services.engines.base import Engine, EngineError, Field, info

SUFFIXES = (".db", ".sqlite", ".sqlite3", ".db3")


def sqlite_root() -> Path:
    configured = os.environ.get("BASILISK_SQLITE_ROOT", "").strip()
    if configured:
        return Path(configured).resolve()
    from services.db import DATA_DIR

    return (Path(DATA_DIR) / "sqlite").resolve()


def resolve_file(relative: str, *, must_exist: bool = True) -> Path:
    """The file a profile's path names, inside the root, or EngineError."""
    text = (relative or "").strip().replace("\\", "/")
    if not text:
        raise EngineError("File is required")
    if text.startswith("/") or (len(text) > 1 and text[1] == ":"):
        raise EngineError("Give the file's path inside the SQLite folder, not an absolute path")
    root = sqlite_root()
    target = (root / text).resolve()
    if target != root and root not in target.parents:
        raise EngineError("The file must be inside the SQLite folder")
    if target.suffix.lower() not in SUFFIXES:
        raise EngineError(f"The file must end in {', '.join(SUFFIXES)}")
    if must_exist and not target.is_file():
        raise EngineError(f"No such file in the SQLite folder: {text}")
    return target


class SQLite(Engine):
    name = "sqlite"
    label = "SQLite"
    icon = "mdi-feather"
    levels = ()
    editor_dialect = "SQLite"
    uses_server = False
    extra_fields = (
        Field("create", "Create the file if it does not exist", kind="checkbox", default=False),
    )

    def validate(self, profile: dict) -> dict:
        options = profile.get("options") or {}
        try:
            resolve_file(profile.get("database") or "", must_exist=not options.get("create"))
        except EngineError as exc:
            return {"database": str(exc)}
        return {}

    def url(self, profile: dict, database: Optional[str]):
        from sqlalchemy import URL

        options = profile.get("options") or {}
        path = resolve_file(profile.get("database") or "",
                            must_exist=not options.get("create") or bool(profile.get("read_only")))
        if profile.get("read_only"):
            return URL.create("sqlite", database=f"file:{path}?mode=ro", query={"uri": "true"})
        path.parent.mkdir(parents=True, exist_ok=True)
        return URL.create("sqlite", database=str(path))

    def engine_kwargs(self, profile: dict) -> dict:
        return {"connect_args": {"check_same_thread": False, "timeout": 10}}

    def on_connect(self, dbapi_conn: Any, profile: dict) -> None:
        dbapi_conn.execute("PRAGMA foreign_keys = ON")

    def set_timeout(self, dbapi_conn: Any, seconds: int) -> None:
        if not seconds:
            dbapi_conn.set_progress_handler(None, 0)
            return
        deadline = time.monotonic() + int(seconds)
        # A non-zero return interrupts the statement ("interrupted").
        dbapi_conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 20_000)

    def set_autocommit(self, dbapi_conn: Any, on: bool) -> None:
        if not on and dbapi_conn.in_transaction:
            dbapi_conn.rollback()
        if on and dbapi_conn.in_transaction:
            dbapi_conn.rollback()
        dbapi_conn.isolation_level = None if on else ""

    def begin_manual(self, dbapi_conn: Any) -> None:
        # PEP 249 mode (Python 3.12+): always inside a transaction, DDL
        # included. The legacy mode only opened one before INSERT/UPDATE/DELETE.
        if dbapi_conn.in_transaction:
            dbapi_conn.rollback()
        dbapi_conn.autocommit = False

    def end_manual(self, dbapi_conn: Any) -> None:
        import sqlite3

        dbapi_conn.rollback()
        dbapi_conn.autocommit = sqlite3.LEGACY_TRANSACTION_CONTROL
        dbapi_conn.isolation_level = ""

    def transaction_state(self, dbapi_conn: Any, statements: int) -> dict:
        # PEP 249 mode keeps a transaction open at all times; "open" means
        # something ran in it.
        return {"open": statements > 0 and dbapi_conn.in_transaction, "failed": False}

    can_analyze = False

    def explain(self, dbapi_conn: Any, sql: str, analyze: bool) -> dict:
        from services.plans import parse_sqlite

        rows = dbapi_conn.execute("EXPLAIN QUERY PLAN " + sql).fetchall()
        raw = "\n".join(f"{row[0]}\t{row[1]}\t{row[3]}" for row in rows)
        return {**parse_sqlite(rows), "raw": "id\tparent\tdetail\n" + raw, "format": "text"}

    def cancel(self, engine: Any, dbapi_conn: Any, session: Any) -> None:
        dbapi_conn.interrupt()

    def schema_for(self, database: Optional[str], schema: Optional[str]) -> Optional[str]:
        return None

    def native_ddl(self, conn: Any, schema: Optional[str], name: str, kind: str) -> Optional[str]:
        from sqlalchemy import text

        rows = conn.execute(text(
            "SELECT sql FROM sqlite_master WHERE tbl_name = :name AND sql IS NOT NULL "
            "ORDER BY CASE type WHEN 'table' THEN 0 WHEN 'view' THEN 0 ELSE 1 END, name"),
            {"name": name}).all()
        return ";\n\n".join(row[0] for row in rows) + ";" if rows else None


    # ------------------------------------------------------------------
    # More navigator objects: SQLite has triggers, and nothing else here.
    # ------------------------------------------------------------------
    object_groups = ("triggers",)
    identity_clause = "PRIMARY KEY AUTOINCREMENT"
    identity_inline_pk = True
    common_types = ("INTEGER", "REAL", "TEXT", "BLOB", "NUMERIC")

    def sql_truncate(self, prep: Any, schema: Optional[str], table: str) -> str:
        return f"DELETE FROM {self.qualified(prep, schema, table)}"

    def list_triggers(self, conn: Any, schema: Optional[str]) -> list:
        from sqlalchemy import text

        rows = conn.execute(text(
            "SELECT name, tbl_name, sql FROM sqlite_master WHERE type = 'trigger' "
            "ORDER BY tbl_name, name")).all()
        return [{"name": name, "table": table, **_trigger_head(sql)} for name, table, sql in rows]

    def trigger_info(self, conn: Any, schema: Optional[str], name: str,
                     table: str) -> Optional[dict]:
        from sqlalchemy import text

        row = conn.execute(text(
            "SELECT tbl_name, sql FROM sqlite_master WHERE type = 'trigger' AND name = :name"),
            {"name": name}).first()
        if row is None:
            return None
        table, sql = row
        head = _trigger_head(sql)
        return info(name, "trigger", [
            ("Table", table), ("Timing", head["timing"]), ("Events", head["events"]),
            ("For each", "ROW"),
        ], (sql or "").rstrip().rstrip(";") + ";", table=table)


_TRIGGER_HEAD = re.compile(
    r"CREATE\s+(?:TEMP(?:ORARY)?\s+)?TRIGGER\s+(?:IF\s+NOT\s+EXISTS\s+)?.+?\s+"
    r"(BEFORE|AFTER|INSTEAD\s+OF)?\s*(DELETE|INSERT|UPDATE(?:\s+OF\s+.+?)?)\s+ON\s",
    re.I | re.S)


def _trigger_head(sql: Optional[str]) -> dict:
    """Timing and event from a trigger's CREATE text (SQLite keeps no columns for them)."""
    match = _TRIGGER_HEAD.match((sql or "").strip())
    if not match:
        return {"timing": "", "events": ""}
    timing = re.sub(r"\s+", " ", (match.group(1) or "BEFORE").upper())
    event = re.sub(r"\s+", " ", match.group(2))
    return {"timing": timing, "events": event if " OF " in event.upper() else event.upper()}
