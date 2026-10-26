"""
DuckDB files, in the same folder as SQLite files (``BASILISK_SQLITE_ROOT``,
see ``engines/sqlite.py``: a profile names a path inside it, checked on save
and on every open).

DuckDB runs in this process (``duckdb`` + duckdb_engine, through Basilisk's
own dialect for reflection: ``duckdb_dialect.py``). One file has one
database; Basilisk shows its ``main`` schema (``levels = ()``).

* **Read-only** opens the file read-only (DuckDB refuses writes itself).
  DuckDB keeps one instance per file in a process, so a file cannot be open
  read-only and read-write at once: the second profile says so.
* **No statement timeout** in DuckDB: a timer interrupts the connection
  instead (``set_timeout``); Cancel interrupts it too.
* Transactions: DuckDB commits each statement unless BEGIN ran;
  ``rollback`` without a transaction is an error, which is ignored.
* No self-numbering column type: a sequence default does it
  (``identity_clause`` is None, and the editor says so). Keys and foreign
  keys cannot be added to or dropped from an existing table.
"""
from __future__ import annotations

import threading
from typing import Any, Optional

from services.engines.base import Engine, EngineError, Field, info
from services.engines.sqlite import resolve_file

SUFFIXES = (".duckdb", ".ddb")


class DuckDB(Engine):
    name = "duckdb"
    label = "DuckDB"
    family = "duckdb"
    icon = "mdi-duck"
    levels = ()
    editor_dialect = "PostgreSQL"
    uses_server = False
    suffixes = SUFFIXES
    extra_fields = (
        Field("create", "Create the file if it does not exist", kind="checkbox", default=False),
    )

    def __init__(self) -> None:
        self._timers: dict = {}
        self._timers_lock = threading.Lock()

    def validate(self, profile: dict) -> dict:
        options = profile.get("options") or {}
        try:
            resolve_file(profile.get("database") or "", suffixes=SUFFIXES,
                         must_exist=not options.get("create"))
        except EngineError as exc:
            return {"database": str(exc)}
        return {}

    def url(self, profile: dict, database: Optional[str]):
        from sqlalchemy import URL

        from services.engines import duckdb_dialect

        duckdb_dialect.register()
        options = profile.get("options") or {}
        path = resolve_file(profile.get("database") or "", suffixes=SUFFIXES,
                            must_exist=not options.get("create") or bool(profile.get("read_only")))
        path.parent.mkdir(parents=True, exist_ok=True)
        return URL.create("duckdb+basilisk", database=str(path))

    def engine_kwargs(self, profile: dict) -> dict:
        return {"connect_args": {"read_only": bool(profile.get("read_only"))}}

    def set_timeout(self, dbapi_conn: Any, seconds: int) -> None:
        key = id(dbapi_conn)
        with self._timers_lock:
            old = self._timers.pop(key, None)
            if old is not None:
                old.cancel()
            if seconds:
                timer = threading.Timer(int(seconds), self._interrupt, (dbapi_conn,))
                timer.daemon = True
                self._timers[key] = timer
                timer.start()

    @staticmethod
    def _interrupt(dbapi_conn: Any) -> None:
        try:
            dbapi_conn.interrupt()
        except Exception:  # noqa: BLE001 - closed meanwhile
            pass

    @staticmethod
    def _rollback(dbapi_conn: Any) -> None:
        try:
            dbapi_conn.rollback()
        except Exception as exc:  # noqa: BLE001 - "no transaction is active"
            if "no transaction" not in str(exc):
                raise

    def set_autocommit(self, dbapi_conn: Any, on: bool) -> None:
        # Statements commit by themselves unless a transaction was begun.
        self._rollback(dbapi_conn)
        if not on:
            dbapi_conn.execute("BEGIN TRANSACTION")

    def end_manual(self, dbapi_conn: Any) -> None:
        self._rollback(dbapi_conn)

    def cancel(self, engine: Any, dbapi_conn: Any, session: Any) -> None:
        dbapi_conn.interrupt()

    # A write answers with one row, column "Count", not a DB-API rowcount.
    def affected_rows(self, result: Any) -> int:
        row = result.fetchone() if result.returns_rows else None
        return int(row[0]) if row is not None else result.rowcount

    def dml_count(self, cursor: Any, sql: str) -> Optional[int]:
        from services.sqlops import first_word

        description = cursor.description or []
        if len(description) == 1 and str(description[0][0]) == "Count" and \
                first_word(sql) in ("INSERT", "UPDATE", "DELETE", "MERGE"):
            row = cursor.fetchone()
            return int(row[0]) if row else 0
        return None

    def explain(self, dbapi_conn: Any, sql: str, analyze: bool) -> dict:
        import json

        from services.plans import parse_duckdb

        options = "ANALYZE, FORMAT JSON" if analyze else "FORMAT JSON"
        cur = dbapi_conn.cursor()
        # DuckDB commits each statement unless a transaction was begun: begin
        # one, so an analysed write is rolled back with the connection.
        self._rollback(dbapi_conn)
        dbapi_conn.execute("BEGIN TRANSACTION")
        try:
            cur.execute(f"EXPLAIN ({options}) {sql}")
            document = json.loads(cur.fetchall()[0][1])
        finally:
            self._rollback(dbapi_conn)
        return {**parse_duckdb(document), "raw": json.dumps(document, indent=2), "format": "json"}

    def schema_for(self, database: Optional[str], schema: Optional[str]) -> Optional[str]:
        return None

    def row_estimates(self, conn: Any, schema: Optional[str]) -> dict:
        from sqlalchemy import text

        rows = conn.execute(text(
            "SELECT table_name, estimated_size FROM duckdb_tables() "
            "WHERE database_name = current_database() AND schema_name = 'main'")).all()
        return {name: count for name, count in rows}

    def native_ddl(self, conn: Any, schema: Optional[str], name: str, kind: str) -> Optional[str]:
        from sqlalchemy import text

        here = "database_name = current_database() AND schema_name = 'main'"
        if kind == "view":
            sql = conn.execute(text(f"SELECT sql FROM duckdb_views() WHERE {here} "
                                    "AND view_name = :name"), {"name": name}).scalar()
            return sql
        rows = [conn.execute(text(f"SELECT sql FROM duckdb_tables() WHERE {here} "
                                  "AND table_name = :name"), {"name": name}).scalar()]
        rows += [row[0] for row in conn.execute(text(
            f"SELECT sql FROM duckdb_indexes() WHERE {here} AND table_name = :name "
            "AND sql IS NOT NULL ORDER BY index_name"), {"name": name}).all()]
        rows = [row.rstrip().rstrip(";") + ";" for row in rows if row]
        return "\n\n".join(rows) or None

    # ------------------------------------------------------------------
    # Navigator objects: sequences
    # ------------------------------------------------------------------
    object_groups = ("sequences",)

    def list_sequences(self, conn: Any, schema: Optional[str]) -> list:
        from sqlalchemy import text

        rows = conn.execute(text(
            "SELECT sequence_name FROM duckdb_sequences() WHERE database_name = current_database() "
            "AND schema_name = 'main' ORDER BY 1")).all()
        return [{"name": row[0]} for row in rows]

    def sequence_info(self, conn: Any, schema: Optional[str], name: str) -> Optional[dict]:
        from sqlalchemy import text

        row = conn.execute(text(
            "SELECT start_value, increment_by, min_value, max_value, cycle, last_value, sql "
            "FROM duckdb_sequences() WHERE database_name = current_database() "
            "AND schema_name = 'main' AND sequence_name = :name"), {"name": name}).first()
        if row is None:
            return None
        start, step, low, high, cycle, last, sql = row
        return info(name, "sequence", [
            ("Start", start), ("Increment", step), ("Minimum", low), ("Maximum", high),
            ("Cycles", "yes" if cycle else "no"), ("Last value", last),
        ], (sql or "").rstrip().rstrip(";") + ";")

    # ------------------------------------------------------------------
    # Object actions, compare scripts, data compare
    # ------------------------------------------------------------------
    identity_clause = None
    can_alter_keys = False
    fk_actions = False
    alter_blocked_by_dependents = True
    common_types = ("INTEGER", "BIGINT", "DECIMAL(12,2)", "DOUBLE", "BOOLEAN", "VARCHAR",
                    "DATE", "TIME", "TIMESTAMP", "UUID", "JSON", "BLOB")

    # ALTER TABLE … ADD COLUMN takes no constraint: NOT NULL is set after.
    add_column_constraints = False

    def import_wrappers(self, conn: Any, prep: Any, schema: Optional[str], table: str,
                        columns: list) -> tuple:
        """
        After explicit values in a column numbered by a sequence default, draw
        that sequence past them: DuckDB has no setval, nor ALTER SEQUENCE … RESTART.
        """
        import re

        from sqlalchemy import text

        rows = conn.execute(text(
            "SELECT column_name, column_default FROM duckdb_columns() "
            "WHERE database_name = current_database() AND schema_name = 'main' "
            "AND table_name = :table"), {"table": table}).all()
        after = []
        for column, default in rows:
            found = re.match(r"nextval\('([^']+)'\)", str(default or ""))
            if column not in columns or not found:
                continue
            seq = found.group(1).replace("'", "''")
            after.append(
                f"SELECT COUNT(nextval('{seq}')) FROM range(GREATEST(0, "
                f"(SELECT COALESCE(MAX({prep.quote(column)}), 0) FROM {self.qualified(prep, schema, table)}) - "
                f"COALESCE((SELECT last_value FROM duckdb_sequences() WHERE sequence_name = '{seq}' "
                f"AND schema_name = 'main'), (SELECT start_value - 1 FROM duckdb_sequences() "
                f"WHERE sequence_name = '{seq}' AND schema_name = 'main'))))")
        return [], after

    def sql_bytes_literal(self, raw: bytes) -> str:
        return "'" + "".join(f"\\x{byte:02x}" for byte in raw) + "'::BLOB"
