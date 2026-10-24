"""
What every database engine adapter provides. Server-side only.

An engine adapter is the only place that knows a dialect's quirks: how its
URL is built, what its tree looks like (databases, schemas), how to make a
session read-only, how to bound a statement's run time, how to cancel one,
and how to split a script. Everything else (``sqlops``) talks SQLAlchemy and
DB-API through these hooks.

**A plain module, not a BFF**: imported normally, so the registry in
``engines/__init__.py`` is built once per process.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional

# One field in the connection editor beyond the common ones. ``kind`` is
# text / password / number / select / checkbox; ``options`` for select.
@dataclass(frozen=True)
class Field:
    id: str
    label: str
    kind: str = "text"
    default: Any = ""
    help: str = ""
    options: tuple = ()

    def to_dict(self) -> dict:
        return {
            "id": self.id, "label": self.label, "kind": self.kind, "default": self.default,
            "help": self.help, "options": [list(option) for option in self.options],
        }


@dataclass(frozen=True)
class Piece:
    """One statement (or T-SQL batch) of a script, with its place in the text."""
    start: int
    end: int
    text: str


class EngineError(Exception):
    """A profile that cannot be opened as it stands, said for the person."""


class Engine:
    name = ""
    label = ""
    icon = "mdi-database"
    default_port = 0
    # What sits between a connection and its tables, outermost first.
    # ("database", "schema") for PostgreSQL / SQL Server, ("database",) for
    # MySQL (a database *is* a schema), () for SQLite.
    levels: tuple = ("database", "schema")
    # The database a profile opens when it names none.
    default_database = ""
    # CodeMirror's lang-sql dialect name (tools/codemirror/entry.js).
    editor_dialect = "StandardSQL"
    # Host/port/user/password, or a file (SQLite).
    uses_server = True
    system_databases: frozenset = frozenset()
    system_schema_prefixes: tuple = ()
    system_schemas: frozenset = frozenset()
    # Extra editor fields, stored in the profile's encrypted ``options``.
    extra_fields: tuple = ()
    # True when the engine cannot make a session read-only itself: then a
    # read-only profile relies on rolling back every transaction.
    read_only_by_rollback = False

    # ------------------------------------------------------------------
    # Profiles
    # ------------------------------------------------------------------

    def validate(self, profile: dict) -> dict:
        errors: dict = {}
        if self.uses_server:
            if not (profile.get("host") or "").strip():
                errors["host"] = "Host is required"
            try:
                port = int(profile.get("port") or 0)
            except (TypeError, ValueError):
                port = -1
            if not 0 < port < 65536:
                errors["port"] = "Port must be 1-65535"
        return errors

    def url(self, profile: dict, database: Optional[str]):  # pragma: no cover - abstract
        raise NotImplementedError

    def engine_kwargs(self, profile: dict) -> dict:
        return {}

    def fingerprint_options(self, profile: dict, database: Optional[str]) -> dict:
        """Everything that changes how a connection is made."""
        return {
            "url": self.url(profile, database).render_as_string(hide_password=False),
            "read_only": bool(profile.get("read_only")),
            "options": profile.get("options") or {},
        }

    def database_for(self, profile: dict, database: Optional[str]) -> Optional[str]:
        """Which database an engine is opened on, "" meaning the profile's."""
        if "database" not in self.levels:
            return None
        return (database or profile.get("database") or self.default_database or None)

    # ------------------------------------------------------------------
    # Sessions
    # ------------------------------------------------------------------

    def on_connect(self, dbapi_conn: Any, profile: dict) -> None:
        """Run once per new DB-API connection (read-only mode, session settings)."""

    def set_timeout(self, dbapi_conn: Any, seconds: int) -> None:
        """Bound each following statement on this connection (0 = no limit)."""

    def set_autocommit(self, dbapi_conn: Any, on: bool) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    # Manual commit mode (sessions.py): a pinned connection with a transaction
    # the person commits or rolls back. ``savepoints`` wraps each statement so
    # an error undoes only that statement (PostgreSQL aborts the whole
    # transaction otherwise).
    savepoints = False

    def begin_manual(self, dbapi_conn: Any) -> None:
        self.set_autocommit(dbapi_conn, False)

    def end_manual(self, dbapi_conn: Any) -> None:
        """Back to the state the pool hands out: rolled back, not auto-commit."""
        dbapi_conn.rollback()

    def transaction_state(self, dbapi_conn: Any, statements: int) -> dict:
        """``{"open", "failed"}``; by default open once anything ran."""
        return {"open": statements > 0, "failed": False}

    def use_database(self, dbapi_conn: Any, database: Optional[str]) -> None:
        """Make ``database`` current on a connection shared by several (MySQL)."""

    def session_id(self, dbapi_conn: Any) -> Any:
        """What :meth:`cancel` needs to stop this connection's statement."""
        return None

    def cancel(self, engine: Any, dbapi_conn: Any, session: Any) -> None:
        """Stop the statement running on ``dbapi_conn`` (called from another thread)."""
        raise EngineError(f"{self.label} statements cannot be cancelled")

    # ------------------------------------------------------------------
    # Catalogue
    # ------------------------------------------------------------------

    def list_databases(self, conn: Any) -> list:
        return []

    def is_system_database(self, name: str) -> bool:
        return name in self.system_databases

    def is_system_schema(self, name: str) -> bool:
        return name in self.system_schemas or any(
            name.startswith(prefix) for prefix in self.system_schema_prefixes)

    def schema_for(self, database: Optional[str], schema: Optional[str]) -> Optional[str]:
        """The schema SQLAlchemy is asked about for a tree location."""
        return schema or None

    def row_estimates(self, conn: Any, schema: Optional[str]) -> dict:
        """Approximate row counts by table name, where the catalogue has them."""
        return {}

    def native_ddl(self, conn: Any, schema: Optional[str], name: str, kind: str) -> Optional[str]:
        """The server's own CREATE statement, when it can give one."""
        return None

    # ------------------------------------------------------------------
    # Scripts
    # ------------------------------------------------------------------

    def split(self, sql: str) -> list:
        """Statements of a script, with offsets, comments-only pieces dropped."""
        import sqlparse

        pieces: list = []
        cursor = 0
        for statement in sqlparse.parse(sql):
            text = str(statement)
            at = sql.find(text, cursor)
            if at < 0:  # pragma: no cover - sqlparse keeps the text verbatim
                at = cursor
            cursor = at + len(text)
            if _only_comments(text):
                continue
            lead = len(text) - len(text.lstrip())
            body = text.strip()
            pieces.append(Piece(at + lead, at + lead + len(body), body))
        return pieces


_COMMENT = re.compile(r"--[^\n]*|/\*.*?\*/", re.S)


def _only_comments(text: str) -> bool:
    return not _COMMENT.sub("", text).strip().strip(";").strip()


def strip_trailing_semicolon(text: str) -> str:
    """Drivers differ on a final ``;``; SQLite and PostgreSQL accept it, others may not."""
    return text.rstrip().rstrip(";").rstrip() if text.rstrip().endswith(";") else text
