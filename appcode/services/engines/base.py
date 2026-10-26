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
    # Which engine's SQL this one speaks, for the page's quoting and
    # literals (ui/grid_tools.py) and dialect-level choices in sqlops:
    # "postgresql", "mysql", "mssql", "sqlite" or "duckdb". CockroachDB
    # speaks PostgreSQL's.
    family = ""
    # Host/port/user/password, or a file (SQLite, DuckDB).
    uses_server = True
    # Materialized views: listed with the views, refreshable.
    has_matviews = False
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

    # Explain plans (sqlops.explain). ``analyze`` runs the statement for real
    # numbers; sqlops runs it in a transaction it rolls back.
    can_analyze = True

    def explain(self, dbapi_conn: Any, sql: str, analyze: bool) -> dict:
        """``{"plan", "summary", "raw", "format"}`` (services.plans shapes)."""
        raise EngineError(f"{self.label} plans are not supported yet")

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
    # More navigator objects (sqlops.objects / object_info / roles)
    # ------------------------------------------------------------------
    # The extra groups a schema shows, in tree order: any of "functions",
    # "procedures", "sequences", "triggers".
    object_groups: tuple = ()
    # A server-level "Users & roles" group (read-only listing).
    has_roles = False
    roles_label = "Users & roles"

    def list_routines(self, conn: Any, schema: Optional[str]) -> list:
        """``[{"id", "name", "kind": "function"|"procedure", "args", "returns"}]``."""
        return []

    def list_sequences(self, conn: Any, schema: Optional[str]) -> list:
        """``[{"name"}]``."""
        return []

    def list_triggers(self, conn: Any, schema: Optional[str]) -> list:
        """``[{"name", "table", "timing", "events"}]``."""
        return []

    def routine_info(self, conn: Any, schema: Optional[str], ident: str) -> Optional[dict]:
        """See :func:`info`; ``params`` (name, type, mode) and ``returns`` too."""
        return None

    def sequence_info(self, conn: Any, schema: Optional[str], name: str) -> Optional[dict]:
        return None

    def trigger_info(self, conn: Any, schema: Optional[str], name: str,
                     table: str) -> Optional[dict]:
        return None

    def list_roles(self, conn: Any, show_system: bool) -> list:
        """``[{"id", "name", "login", "admin"}]``."""
        return []

    def role_info(self, conn: Any, ident: str) -> Optional[dict]:
        return None

    # ------------------------------------------------------------------
    # Object actions (services/ddl.py builds the statements)
    # ------------------------------------------------------------------
    # How a column numbers itself; SQLite's only works inline on a
    # one-column INTEGER primary key ("identity_inline_pk").
    identity_clause = "GENERATED BY DEFAULT AS IDENTITY"
    identity_inline_pk = False
    add_column_keyword = "ADD COLUMN"
    # False where ADD COLUMN takes no NOT NULL (DuckDB): it is set afterwards.
    add_column_constraints = True
    # False where a table's primary or foreign keys cannot change after it is
    # made (DuckDB): compare scripts leave such changes to a person.
    can_alter_keys = True
    # False where a foreign key takes no ON DELETE / ON UPDATE action (DuckDB).
    fk_actions = True
    # True where a column cannot change type, gain NOT NULL or be dropped while
    # an index or a foreign key depends on its table (DuckDB).
    alter_blocked_by_dependents = False
    # False where DDL commits at once and cannot be rolled back (MySQL).
    ddl_transactional = True
    # Suggestions for the type boxes; any type the server knows is accepted.
    common_types: tuple = ()

    @staticmethod
    def qualified(prep: Any, schema: Optional[str], name: str) -> str:
        return (prep.quote_schema(schema) + "." if schema else "") + prep.quote(name)

    def sql_rename_table(self, prep: Any, schema: Optional[str], old: str, new: str) -> str:
        return f"ALTER TABLE {self.qualified(prep, schema, old)} RENAME TO {prep.quote(new)}"

    def sql_rename_column(self, prep: Any, schema: Optional[str], table: str, old: str,
                          new: str) -> str:
        return (f"ALTER TABLE {self.qualified(prep, schema, table)} "
                f"RENAME COLUMN {prep.quote(old)} TO {prep.quote(new)}")

    def sql_truncate(self, prep: Any, schema: Optional[str], table: str) -> str:
        return f"TRUNCATE TABLE {self.qualified(prep, schema, table)}"

    def sql_drop_index(self, prep: Any, schema: Optional[str], table: str, index: str) -> str:
        return f"DROP INDEX {self.qualified(prep, schema, index)}"

    def before_drop_column(self, conn: Any, prep: Any, schema: Optional[str], table: str,
                           column: str) -> list:
        """Statements a column's drop needs first (SQL Server: its default constraint)."""
        return []

    # ------------------------------------------------------------------
    # Schema compare (services/schema_compare.py writes the script)
    # ------------------------------------------------------------------
    # A script runs as GO-separated batches (SQL Server).
    script_batches = False
    # SQLite cannot change a column, a key or a foreign key in place: the
    # compare script rebuilds the table instead.
    rebuilds_tables = False
    # How a view is replaced; None: dropped and created again.
    replace_view: Optional[str] = "CREATE OR REPLACE VIEW"

    def affected_rows(self, result: Any) -> int:
        """Rows a SQLAlchemy UPDATE / DELETE result touched (the grid's one-row check)."""
        return result.rowcount

    def dml_count(self, cursor: Any, sql: str) -> Optional[int]:
        """An engine that answers a write with a count *row* (DuckDB): that count, else None."""
        return None

    def before_compare(self, conn: Any, schema: Optional[str]) -> None:
        """Set the connection up for reading a schema to compare (it is never committed)."""

    def sql_alter_column(self, prep: Any, schema: Optional[str], table: str, column: str,
                         new: dict, old: dict, changed: set) -> list:
        """
        Statements that give ``column`` the ``new`` definition (``type``,
        ``nullable``, ``default``, ``auto``); ``changed`` names what differs
        from ``old``. Standard SQL here.
        """
        target = self.qualified(prep, schema, table)
        col = prep.quote(column)
        out = []
        if "type" in changed:
            out.append(f"ALTER TABLE {target} ALTER COLUMN {col} SET DATA TYPE {new['type']}")
        if "nullable" in changed:
            out.append(f"ALTER TABLE {target} ALTER COLUMN {col} "
                       + ("DROP NOT NULL" if new["nullable"] else "SET NOT NULL"))
        if "default" in changed:
            out.append(f"ALTER TABLE {target} ALTER COLUMN {col} "
                       + (f"SET DEFAULT {new['default']}" if new.get("default") else "DROP DEFAULT"))
        return out

    def sql_change_primary_key(self, prep: Any, schema: Optional[str], table: str,
                               old_name: str, new_columns: list, old_columns: list) -> list:
        """
        ``[("drop" | "add", statement)]`` that move a table's primary key from
        ``old_columns`` (named ``old_name``) to ``new_columns``; either may be
        empty. "drop" runs with the other drops, "add" once every column exists.
        """
        target = self.qualified(prep, schema, table)
        out = []
        if old_columns:
            out.append(("drop", self.sql_drop_constraint(prep, schema, table, old_name, "pk")))
        if new_columns:
            out.append(("add", f"ALTER TABLE {target} ADD PRIMARY KEY "
                               f"({', '.join(prep.quote(c) for c in new_columns)})"))
        return out

    def sql_drop_constraint(self, prep: Any, schema: Optional[str], table: str,
                            name: str, kind: str) -> str:
        """Drop a ``fk``, ``pk`` or ``unique`` constraint by name."""
        return (f"ALTER TABLE {self.qualified(prep, schema, table)} "
                f"DROP CONSTRAINT {prep.quote(name)}")

    def sql_script_before_drop_column(self, prep: Any, schema: Optional[str], table: str,
                                      column: str) -> list:
        """``before_drop_column`` for a script, without asking the server first."""
        return []

    # ------------------------------------------------------------------
    # Data compare (services/data_compare.py writes values as literals)
    # ------------------------------------------------------------------
    bool_literals = ("TRUE", "FALSE")

    def sql_text_literal(self, text: str) -> str:
        return "'" + text.replace("'", "''") + "'"

    def sql_bytes_literal(self, raw: bytes) -> str:
        return "X'" + raw.hex() + "'"

    def sql_datetime_literal(self, value: Any, type_text: str) -> str:
        return self.sql_text_literal(value.isoformat(sep=" "))

    # ------------------------------------------------------------------
    # CSV import (services/csv_import.py)
    # ------------------------------------------------------------------

    def import_wrappers(self, conn: Any, prep: Any, schema: Optional[str], table: str,
                        columns: list) -> tuple:
        """
        ``(before, after)`` statements around an import that fills ``columns``:
        what lets explicit values into a self-numbering column, and what moves
        its counter past them afterwards. MySQL and SQLite need neither.
        """
        return [], []

    # ------------------------------------------------------------------
    # Session manager (sqlops.sessions / kill_session)
    # ------------------------------------------------------------------
    has_sessions = False
    # Stop a session's statement but keep the session (SQL Server can only end it).
    can_cancel_query = True

    def list_sessions(self, conn: Any, include_idle: bool) -> list:
        """
        ``[{"id", "user", "database", "client", "application", "state",
        "started", "seconds", "wait", "blocked_by", "query", "own", "txn"}]``:
        the server's client sessions, the busiest first. ``own`` marks the
        connection asking; ``seconds`` is how long the state has lasted.
        """
        return []

    def kill_session(self, engine: Any, conn: Any, session_id: int, mode: str) -> str:
        """Cancel the statement (``mode="cancel"``) or end the session; returns the SQL used."""
        raise EngineError(f"{self.label} sessions cannot be stopped here")


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


def info(name: str, kind: str, properties: list, definition: str = "", native: bool = True,
         **extra) -> dict:
    """
    What an object tab shows: ``properties`` as ``[label, value]`` pairs
    (``None`` values are left out), the ``definition`` (source or DDL;
    ``native`` when the server wrote it), and optional ``lists``
    (``[{"title", "items"}]``), ``note``, ``params``, ``returns``, ``table``.
    """
    rows = [[label, _shown(value)] for label, value in properties if value not in (None, "")]
    return {"name": name, "kind": kind, "properties": rows, "definition": definition or "",
            "native": native, **extra}


def _shown(value: Any) -> str:
    import datetime as dt

    if value is True:
        return "yes"
    if value is False:
        return "no"
    if isinstance(value, dt.datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    return str(value)


def kind_words(type_desc: str) -> str:
    """``SQL_STORED_PROCEDURE`` -> ``SQL stored procedure``."""
    words = (type_desc or "").replace("_", " ").lower()
    return "SQL" + words[3:] if words.startswith("sql") else words.capitalize()
