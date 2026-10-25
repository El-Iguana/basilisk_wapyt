"""
Everything Basilisk does to a database. Server-side only, a plain module.

``sql_service.SqlService`` (the BFF) is a thin, authenticated front for these
functions; keeping them here means they can hold state (the running-statement
registry) and be tested without pytincture.

Every function takes the caller's ``user_id`` first and reaches a database
only through ``engine_pool.pool.open(user_id, ...)``, which refuses a profile
the caller does not own.

Safety model, in short (CLAUDE.md has the reasons):

* Table names, schemas and columns from the browser are always **quoted by
  SQLAlchemy**, never pasted into SQL.
* The WHERE and ORDER BY boxes of a table view are SQL typed by the person,
  who can run any SQL in an editor anyway. They run on a connection that is
  never committed, with bound LIMIT/OFFSET (a single statement on every
  driver but pymssql, whose batch is rolled back).
* A read-only profile is read-only in the database session where the engine
  allows (PostgreSQL, MySQL, SQLite ``mode=ro``) and, everywhere, every
  transaction it opens is rolled back.
"""
from __future__ import annotations

import re
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterator, Optional

from services.engine_pool import error_text, pool
from services.values import cell, coerce, decode_key, encode_key, full_value, kind_of

PAGE_SIZES = (50, 100, 200, 500, 1000)
MAX_PAGE_SIZE = 1000
MAX_RESULT_ROWS = 10_000
BROWSE_TIMEOUT_S = 30
COUNT_TIMEOUT_S = 5
CATALOG_TIMEOUT_S = 30
MAX_COMPLETION_TABLES = 3000
MAX_CHANGES = 1000
HISTORY_KEEP = 500
HISTORY_SQL_CHARS = 20_000

# The first keyword of statements that do not change anything, for the
# "production connection" confirmation. A safety net, not a security check.
_READ_FIRST_WORDS = frozenset({"SELECT", "SHOW", "EXPLAIN", "DESCRIBE", "DESC", "WITH",
                               "VALUES", "TABLE", "USE"})
# Statements that are transaction control themselves: never wrapped in a
# savepoint in manual mode.
_TXN_WORDS = frozenset({"BEGIN", "START", "COMMIT", "END", "ROLLBACK", "SAVEPOINT", "RELEASE",
                        "ABORT"})
_LEADING_COMMENTS = re.compile(r"^(?:\s+|--[^\n]*\n?|/\*.*?\*/)*", re.S)


class OpError(Exception):
    """A refusal or failure to show the person as it is."""


# ---------------------------------------------------------------------------
# Connections
# ---------------------------------------------------------------------------

@contextmanager
def connect(user_id: int, conn_id: int, database: Optional[str] = None,
            timeout_s: int = CATALOG_TIMEOUT_S) -> Iterator[tuple]:
    """
    ``(conn, spec, profile, engine)``: a SQLAlchemy connection that is
    **never committed** (closing it rolls back), with a statement timeout.
    """
    engine, spec, profile = pool.open(user_id, conn_id, database)
    with engine.connect() as conn:
        dbapi = conn.connection.dbapi_connection
        if timeout_s:
            spec.set_timeout(dbapi, min(timeout_s, profile.get("timeout_s") or timeout_s))
        if "database" in spec.levels and database and spec.database_for(profile, database) is None:
            spec.use_database(dbapi, database)
        try:
            yield conn, spec, profile, engine
        finally:
            try:
                spec.set_timeout(dbapi, 0)
            except Exception:  # noqa: BLE001 - the connection may be gone; the pool notices
                pass


def _inspector(conn: Any) -> Any:
    from sqlalchemy import inspect

    return inspect(conn)


def _type_name(sa_type: Any, dialect: Any) -> str:
    try:
        return str(sa_type.compile(dialect=dialect))
    except Exception:  # noqa: BLE001 - some reflected types cannot compile
        return repr(sa_type)


def _sa_table(spec: Any, database: Optional[str], schema: Optional[str], name: str,
              columns: tuple = ()) -> Any:
    from sqlalchemy import column, table

    return table(name, *[column(col_name, col_type) for col_name, col_type in columns],
                 schema=spec.schema_for(database, schema))


# ---------------------------------------------------------------------------
# The tree
# ---------------------------------------------------------------------------

def ping(user_id: int, conn_id: int) -> dict:
    from sqlalchemy import text

    with connect(user_id, conn_id, timeout_s=10) as (conn, spec, profile, _engine):
        conn.execute(text("SELECT 1"))
        version = conn.dialect.server_version_info
    return {"version": ".".join(str(part) for part in version or ())}


def databases(user_id: int, conn_id: int, show_system: bool = False) -> list:
    with connect(user_id, conn_id) as (conn, spec, profile, _engine):
        if "database" not in spec.levels:
            return []
        names = spec.list_databases(conn)
        current = profile.get("database") or spec.default_database
    rows = []
    for name in names:
        system = spec.is_system_database(name) and name != current
        if system and not show_system:
            continue
        rows.append({"name": name, "system": spec.is_system_database(name),
                     "current": name == current})
    return rows


def schemas(user_id: int, conn_id: int, database: str = "", show_system: bool = False) -> dict:
    with connect(user_id, conn_id, database) as (conn, spec, _profile, _engine):
        if "schema" not in spec.levels:
            return {"schemas": [], "default": None}
        insp = _inspector(conn)
        names = insp.get_schema_names()
        default = insp.default_schema_name
    rows = [{"name": name, "system": spec.is_system_schema(name), "default": name == default}
            for name in names if show_system or not spec.is_system_schema(name) or name == default]
    return {"schemas": rows, "default": default}


def objects(user_id: int, conn_id: int, database: str = "", schema: str = "") -> dict:
    """Tables and views in one schema (or MySQL database, or SQLite file)."""
    with connect(user_id, conn_id, database) as (conn, spec, _profile, _engine):
        insp = _inspector(conn)
        target = spec.schema_for(database, schema)
        tables = sorted(insp.get_table_names(schema=target))
        views = sorted(insp.get_view_names(schema=target))
        materialized: list = []
        if spec.name == "postgresql":
            materialized = sorted(insp.get_materialized_view_names(schema=target))
        try:
            estimates = spec.row_estimates(conn, target)
        except Exception:  # noqa: BLE001 - estimates are a nicety
            estimates = {}
    return {
        "tables": [{"name": name, "rows": estimates.get(name)} for name in tables],
        "views": [{"name": name, "materialized": False} for name in views]
        + [{"name": name, "materialized": True} for name in materialized],
    }


# ---------------------------------------------------------------------------
# One table
# ---------------------------------------------------------------------------

def _reflect_columns(insp: Any, name: str, target: Optional[str]) -> list:
    return insp.get_columns(name, schema=target)


def table_info(user_id: int, conn_id: int, database: str, schema: str, name: str) -> dict:
    from sqlalchemy.exc import NoSuchTableError

    with connect(user_id, conn_id, database) as (conn, spec, profile, _engine):
        insp = _inspector(conn)
        target = spec.schema_for(database, schema)
        views = set(insp.get_view_names(schema=target))
        if spec.name == "postgresql":
            views |= set(insp.get_materialized_view_names(schema=target))
        kind = "view" if name in views else "table"
        try:
            columns = _reflect_columns(insp, name, target)
        except NoSuchTableError:
            raise OpError(f"No table or view named {name!r}") from None
        pk = list((_quiet(insp.get_pk_constraint, name, schema=target) or {})
                  .get("constrained_columns") or [])
        fks = _quiet(insp.get_foreign_keys, name, schema=target) or []
        indexes = _quiet(insp.get_indexes, name, schema=target) or []
        uniques = _quiet(insp.get_unique_constraints, name, schema=target) or []
        checks = _quiet(insp.get_check_constraints, name, schema=target) or []
        comment = (_quiet(insp.get_table_comment, name, schema=target) or {}).get("text")
        ddl, ddl_native = _ddl(conn, spec, insp, target, name, kind)
        prep = conn.dialect.identifier_preparer
        qualified = (prep.quote_schema(target) + "." if target else "") + prep.quote(name)
        dialect = conn.dialect

    return {
        "name": name, "schema": target, "kind": kind, "comment": comment,
        "qualified": qualified,
        "columns": [{
            "name": col["name"],
            "type": _type_name(col["type"], dialect),
            "nullable": bool(col.get("nullable", True)),
            "default": None if col.get("default") is None else str(col["default"]),
            "autoincrement": bool(col.get("autoincrement") is True or (
                col.get("autoincrement") == "auto" and col["name"] in pk and len(pk) == 1
                and _type_name(col["type"], dialect).upper().startswith(("INT", "BIGINT",
                                                                       "SMALLINT", "SERIAL")))),
            "comment": col.get("comment"),
            "pk": col["name"] in pk,
            "computed": bool(col.get("computed")),
        } for col in columns],
        "pk": pk,
        "foreign_keys": [{
            "name": fk.get("name"),
            "columns": fk.get("constrained_columns") or [],
            "ref_schema": fk.get("referred_schema"),
            "ref_table": fk.get("referred_table"),
            "ref_columns": fk.get("referred_columns") or [],
            "on_delete": (fk.get("options") or {}).get("ondelete"),
            "on_update": (fk.get("options") or {}).get("onupdate"),
        } for fk in fks],
        "indexes": [{
            "name": ix.get("name"), "columns": [c for c in (ix.get("column_names") or []) if c]
            or [str(e) for e in (ix.get("expressions") or [])],
            "unique": bool(ix.get("unique")),
        } for ix in indexes],
        "uniques": [{"name": u.get("name"), "columns": u.get("column_names") or []}
                    for u in uniques],
        "checks": [{"name": c.get("name"), "sql": c.get("sqltext")} for c in checks],
        "ddl": ddl, "ddl_native": ddl_native,
        # Editing needs a key to find the row again, and a writable profile.
        "editable": kind == "table" and bool(pk) and not profile.get("read_only"),
        "read_only": bool(profile.get("read_only")),
    }


def _quiet(method, *args, **kwargs):
    try:
        return method(*args, **kwargs)
    except NotImplementedError:
        return None


def _ddl(conn: Any, spec: Any, insp: Any, target: Optional[str], name: str,
         kind: str) -> tuple[str, bool]:
    """The CREATE statement: the server's own when it has one, else generated."""
    try:
        native = spec.native_ddl(conn, target, name, kind)
    except Exception:  # noqa: BLE001 - fall back to generating it
        native = None
    if native:
        return native, True
    prep = conn.dialect.identifier_preparer
    qualified = (prep.quote_schema(target) + "." if target else "") + prep.quote(name)
    if kind == "view":
        try:
            body = (insp.get_view_definition(name, schema=target) or "").strip()
        except Exception:  # noqa: BLE001
            body = ""
        if body.upper().startswith("CREATE"):
            return body if body.endswith(";") else body + ";", True
        return f"CREATE VIEW {qualified} AS\n{body.rstrip(';')};", True
    from sqlalchemy import MetaData, Table
    from sqlalchemy.schema import CreateIndex, CreateTable

    try:
        table = Table(name, MetaData(), schema=target, autoload_with=conn)
        parts = [str(CreateTable(table).compile(dialect=conn.dialect)).strip() + ";"]
        for index in sorted(table.indexes, key=lambda ix: ix.name or ""):
            parts.append(str(CreateIndex(index).compile(dialect=conn.dialect)).strip() + ";")
        return "\n\n".join(parts), False
    except Exception as exc:  # noqa: BLE001 - show why rather than fail the tab
        return f"-- Could not generate the DDL: {error_text(exc)}", False


# ---------------------------------------------------------------------------
# Browsing a table
# ---------------------------------------------------------------------------

def typed_sql(fragment: str) -> Any:
    """
    SQL the person typed, as a ``text()`` clause. Colons are escaped, or
    SQLAlchemy would read ``:name`` (and PostgreSQL's ``::`` casts) as bind
    parameters.
    """
    from sqlalchemy import text

    return text(fragment.replace(":", "\\:"))


def _browse_stmt(spec: Any, conn: Any, database: str, schema: str, name: str,
                 where: str, order_by: str, pk: list) -> Any:
    from sqlalchemy import literal_column, select, text

    table = _sa_table(spec, database, schema, name)
    stmt = select(literal_column("*")).select_from(table)
    if where.strip():
        stmt = stmt.where(typed_sql(f"({where.strip()})"))
    if order_by.strip():
        stmt = stmt.order_by(typed_sql(order_by.strip()))
    elif pk:
        prep = conn.dialect.identifier_preparer
        stmt = stmt.order_by(*[text(prep.quote(col)) for col in pk])
    elif spec.name == "mssql":
        # OFFSET needs an ORDER BY in T-SQL.
        stmt = stmt.order_by(text("(SELECT NULL)"))
    return stmt


def browse(user_id: int, conn_id: int, database: str, schema: str, name: str,
           where: str = "", order_by: str = "", offset: int = 0, limit: int = 100,
           pk: Optional[list] = None, with_count: bool = True) -> dict:
    from sqlalchemy import func, select, text

    limit = max(1, min(int(limit or 100), MAX_PAGE_SIZE))
    offset = max(0, int(offset or 0))
    pk = list(pk or [])
    started = time.monotonic()
    with connect(user_id, conn_id, database, BROWSE_TIMEOUT_S) as (conn, spec, _p, _engine):
        stmt = _browse_stmt(spec, conn, database, schema, name, where, order_by, pk)
        shown_sql = _render(stmt.limit(limit).offset(offset), conn.dialect)
        try:
            result = conn.execute(stmt.limit(limit).offset(offset))
            keys = list(result.keys())
            raw_rows = result.fetchall()
        except Exception as exc:
            raise OpError(error_text(exc)) from None
        ms = int((time.monotonic() - started) * 1000)

        total, exact = None, False
        if with_count:
            total, exact = _count(conn, spec, database, schema, name, where)

    rows, kinds, truncated = _encode_rows(raw_rows, len(keys))
    key_index = [keys.index(col) for col in pk if col in keys]
    row_keys = [[encode_key(row[i]) for i in key_index] for row in raw_rows] \
        if len(key_index) == len(pk) and pk else []
    return {
        "columns": keys, "rows": rows, "kinds": kinds, "truncated": truncated,
        "keys": row_keys, "total": total, "exact": exact,
        "offset": offset, "limit": limit, "ms": ms, "sql": shown_sql,
    }


def _count(conn: Any, spec: Any, database: str, schema: str, name: str,
           where: str) -> tuple[Optional[int], bool]:
    from sqlalchemy import func, select, text

    table = _sa_table(spec, database, schema, name)
    stmt = select(func.count()).select_from(table)
    if where.strip():
        stmt = stmt.where(typed_sql(f"({where.strip()})"))
    dbapi = conn.connection.dbapi_connection
    try:
        spec.set_timeout(dbapi, COUNT_TIMEOUT_S)
        if spec.name == "postgresql":
            # A timed-out statement aborts the transaction; a savepoint keeps it.
            with conn.begin_nested():
                return int(conn.execute(stmt).scalar() or 0), True
        return int(conn.execute(stmt).scalar() or 0), True
    except Exception:  # noqa: BLE001 - slow or refused: the page still shows
        return None, False


def count(user_id: int, conn_id: int, database: str, schema: str, name: str,
          where: str = "") -> dict:
    from sqlalchemy import func, select, text

    table = None
    with connect(user_id, conn_id, database, BROWSE_TIMEOUT_S) as (conn, spec, _p, _engine):
        table = _sa_table(spec, database, schema, name)
        stmt = select(func.count()).select_from(table)
        if where.strip():
            stmt = stmt.where(typed_sql(f"({where.strip()})"))
        try:
            return {"total": int(conn.execute(stmt).scalar() or 0)}
        except Exception as exc:
            raise OpError(error_text(exc)) from None


def _encode_rows(raw_rows: list, width: int) -> tuple[list, list, list]:
    """Rows as display text, each column's kind, and ``[row, col]`` cells cut short."""
    kinds: list = [None] * width
    rows: list = []
    truncated: list = []
    for r, raw in enumerate(raw_rows):
        out = []
        for c in range(width):
            value = raw[c]
            if kinds[c] is None and value is not None:
                kinds[c] = kind_of(value)
            text, cut = cell(value)
            if cut:
                truncated.append([r, c])
            out.append(text)
        rows.append(out)
    return rows, [kind or "text" for kind in kinds], truncated


def fetch_cell(user_id: int, conn_id: int, database: str, schema: str, name: str,
               key: dict, column: str) -> Any:
    """One column of one row, found by its primary key, as the driver returns it."""
    from sqlalchemy import and_, select

    with connect(user_id, conn_id, database, BROWSE_TIMEOUT_S) as (conn, spec, _p, _engine):
        insp = _inspector(conn)
        target = spec.schema_for(database, schema)
        columns = _reflect_columns(insp, name, target)
        pk = list((insp.get_pk_constraint(name, schema=target) or {})
                  .get("constrained_columns") or [])
        types = {col["name"]: col["type"] for col in columns}
        if column not in types:
            raise OpError(f"No column named {column!r}")
        if not pk or set(key or {}) != set(pk):
            raise OpError("This row has no primary key to find it by")
        table = _sa_table(spec, database, schema, name, tuple(types.items()))
        try:
            where = and_(*[table.c[col] == decode_key(key[col]) for col in pk])
        except ValueError as exc:
            raise OpError(str(exc)) from None
        try:
            rows = conn.execute(select(table.c[column]).where(where).limit(2)).fetchall()
        except Exception as exc:
            raise OpError(error_text(exc)) from None
    if len(rows) != 1:
        raise OpError("The row is no longer there; refresh to see the current data.")
    return rows[0][0]


def cell_value(user_id: int, conn_id: int, database: str, schema: str, name: str,
               key: dict, column: str) -> dict:
    """The value panel's uncut value (see ``values.full_value``)."""
    return full_value(fetch_cell(user_id, conn_id, database, schema, name, key, column))


def _render(stmt: Any, dialect: Any) -> str:
    """SQL to show the person, with values inlined where the dialect can."""
    try:
        return str(stmt.compile(dialect=dialect, compile_kwargs={"literal_binds": True}))
    except Exception:  # noqa: BLE001 - some types have no literal form
        compiled = stmt.compile(dialect=dialect)
        params = ", ".join(f"{key}={value!r}" for key, value in compiled.params.items())
        return f"{compiled}\n-- with {params}" if params else str(compiled)


PREVIEW_MAX_CHARS = 4000


def _preview(sql: str) -> str:
    """A statement for the Save preview: a megabyte value is cut, not shown whole."""
    if len(sql) <= PREVIEW_MAX_CHARS:
        return sql
    return sql[:PREVIEW_MAX_CHARS] + f"… -- cut for display; {len(sql):,} characters in all"


# ---------------------------------------------------------------------------
# Saving grid edits
# ---------------------------------------------------------------------------

def save_changes(user_id: int, conn_id: int, database: str, schema: str, name: str,
                 changes: list, dry_run: bool = False) -> dict:
    """
    Apply a batch of row changes in **one transaction**: every update and
    delete must touch exactly one row, or nothing is kept.

    ``changes``: ``[{"row": id, "op": "update"|"insert"|"delete",
    "key": {pk column: tagged key}, "values": {column: text or None}}]``.
    Cell errors come back as ``errors: [{row, column, error}]`` before
    anything runs.
    """
    from sqlalchemy import and_, delete, insert, update

    if not isinstance(changes, list) or not changes:
        raise OpError("Nothing to save")
    if len(changes) > MAX_CHANGES:
        raise OpError(f"At most {MAX_CHANGES} changed rows can be saved at once")

    engine, spec, profile = pool.open(user_id, conn_id, database)
    if profile.get("read_only"):
        raise OpError("This connection is read-only")

    with connect(user_id, conn_id, database) as (conn, spec, _p, _engine):
        insp = _inspector(conn)
        target = spec.schema_for(database, schema)
        columns = _reflect_columns(insp, name, target)
        pk = list((insp.get_pk_constraint(name, schema=target) or {})
                  .get("constrained_columns") or [])
        dialect = conn.dialect
    if not pk:
        raise OpError("This table has no primary key, so its rows cannot be edited here")
    types = {col["name"]: col["type"] for col in columns}
    table = _sa_table(spec, database, schema, name, tuple(types.items()))

    statements: list = []
    errors: list = []
    for change in changes:
        row_id = change.get("row")
        op = change.get("op")
        values = change.get("values") or {}
        if op not in ("update", "insert", "delete"):
            raise OpError(f"Unknown change {op!r}")
        typed: dict = {}
        for column, text in values.items():
            if column not in types:
                errors.append({"row": row_id, "column": column, "error": "No such column"})
                continue
            try:
                typed[table.c[column]] = coerce(text, types[column])
            except ValueError as exc:
                errors.append({"row": row_id, "column": column, "error": str(exc)})
        where = None
        if op in ("update", "delete"):
            key = change.get("key") or {}
            if set(key) != set(pk):
                raise OpError("A changed row lost its primary key; refresh and try again")
            try:
                where = and_(*[table.c[col] == decode_key(key[col]) for col in pk])
            except ValueError as exc:
                raise OpError(str(exc)) from None
        if op == "update":
            if typed:
                statements.append((row_id, op, update(table).where(where).values(typed)))
        elif op == "insert":
            statements.append((row_id, op, insert(table).values(typed) if typed
                               else insert(table)))
        else:
            statements.append((row_id, op, delete(table).where(where)))

    if errors:
        return {"ok": False, "errors": errors}
    preview = [_preview(_render(stmt, dialect) + ";") for _row, _op, stmt in statements]
    if dry_run:
        return {"ok": True, "sql": preview, "count": len(statements)}

    started = time.monotonic()
    try:
        with engine.connect() as conn:
            dbapi = conn.connection.dbapi_connection
            spec.set_timeout(dbapi, profile.get("timeout_s") or 60)
            with conn.begin():
                for row_id, op, stmt in statements:
                    result = conn.execute(stmt)
                    if op != "insert" and result.rowcount != 1:
                        raise OpError(
                            f"Row {row_id}: {op} matched {result.rowcount} rows instead of 1. "
                            "Nothing was saved; refresh to see the current data.")
            spec.set_timeout(dbapi, 0)
    except OpError:
        raise
    except Exception as exc:
        raise OpError(error_text(exc)) from None
    return {"ok": True, "count": len(statements), "sql": preview,
            "ms": int((time.monotonic() - started) * 1000)}


# ---------------------------------------------------------------------------
# Running SQL
# ---------------------------------------------------------------------------

class _Run:
    __slots__ = ("user_id", "spec", "engine", "dbapi", "session", "cancelled")

    def __init__(self, user_id: int, spec: Any, engine: Any, dbapi: Any, session: Any) -> None:
        self.user_id = user_id
        self.spec = spec
        self.engine = engine
        self.dbapi = dbapi
        self.session = session
        self.cancelled = False


_runs: dict = {}
_runs_lock = threading.Lock()


def cancel(user_id: int, run_id: str) -> dict:
    with _runs_lock:
        run = _runs.get((int(user_id), str(run_id)))
    if run is None:
        return {"ok": False, "error": "Nothing is running"}
    run.cancelled = True
    try:
        run.spec.cancel(run.engine, run.dbapi, run.session)
    except Exception as exc:  # noqa: BLE001 - shown to the person
        return {"ok": False, "error": error_text(exc)}
    return {"ok": True}


def first_word(sql: str) -> str:
    match = re.match(r"[A-Za-z]+", _LEADING_COMMENTS.sub("", sql, count=1))
    return match.group(0).upper() if match else ""


def pick(spec: Any, sql: str, mode: str, caret: int) -> list:
    """The pieces to run: the statement at the caret, or the whole script."""
    from services.engines.base import Engine

    if mode == "statement":
        # Statement-by-statement even for T-SQL, whose script mode runs
        # whole GO batches.
        pieces = Engine.split(spec, sql)
        if not pieces:
            return []
        caret = max(0, min(int(caret or 0), len(sql)))
        inside = [p for p in pieces if p.start <= caret <= p.end]
        if inside:
            return [inside[0]]
        before = [p for p in pieces if p.end <= caret]
        return [before[-1] if before else pieces[0]]
    return spec.split(sql)


def execute(user_id: int, conn_id: int, database: str, sql: str, mode: str = "script",
            caret: int = 0, max_rows: int = 500, run_id: str = "",
            confirmed: bool = False, session: str = "") -> dict:
    """
    Run a script, the statement at the caret, or a selection (sent as the
    script); the run stops at the first error.

    Without ``session`` (auto-commit) each statement is committed on a pooled
    connection (a read-only profile: one transaction, rolled back). With one
    (manual commit mode, ``sessions.py``) they run in the tab's open
    transaction on its pinned connection, and nothing is committed here.
    """
    max_rows = max(1, min(int(max_rows or 500), MAX_RESULT_ROWS))
    engine, spec, profile = pool.open(user_id, conn_id, database)
    try:
        pieces = pick(spec, sql or "", mode, caret)
    except ValueError as exc:
        raise OpError(str(exc)) from None
    if not pieces:
        raise OpError("Nothing to run")

    if profile.get("env") == "prod" and not confirmed and not profile.get("read_only"):
        writes = [p.text for p in pieces if first_word(p.text) not in _READ_FIRST_WORDS]
        if writes:
            return {"ok": False, "needs_confirm": True,
                    "statements": [w[:300] for w in writes[:5]], "count": len(writes)}
    if session:
        return _execute_manual(user_id, conn_id, database, session, pieces, max_rows, run_id,
                               profile)

    read_only = bool(profile.get("read_only"))
    results: list = []
    started = time.monotonic()
    key = (int(user_id), str(run_id or ""))
    raw = engine.raw_connection()
    dbapi = raw.dbapi_connection
    broken = False
    try:
        spec.use_database(dbapi, database if spec.database_for(profile, database) is None
                          and "database" in spec.levels else None)
        _register_run(key, spec, engine, dbapi, run_id)
        if not read_only:
            spec.set_autocommit(dbapi, True)
        spec.set_timeout(dbapi, profile.get("timeout_s") or 0)
        broken = _run_pieces(spec, dbapi, pieces, max_rows, key, run_id, results, manual=False)
        _remember(user_id, conn_id, database, results)
    finally:
        with _runs_lock:
            _runs.pop(key, None)
        try:
            if read_only:
                dbapi.rollback()
            else:
                spec.set_autocommit(dbapi, False)
            spec.set_timeout(dbapi, 0)
        except Exception:  # noqa: BLE001 - a killed session cannot be reset
            broken = True
        if broken:
            raw.invalidate()
        else:
            raw.close()

    failed = any(result.get("error") for result in results)
    return {
        "ok": not failed, "results": results, "ms": int((time.monotonic() - started) * 1000),
        "read_only": read_only, "rolled_back": read_only,
    }


def explain(user_id: int, conn_id: int, database: str, sql: str, caret: int = 0,
            analyze: bool = False, confirmed: bool = False) -> dict:
    """
    The plan of the statement at the caret (or the selection, sent as ``sql``).

    ``analyze`` runs the statement to get real row counts and times. It runs on
    a connection that is never committed, so a write is rolled back; anything a
    statement does outside the transaction (sequences, notifications, calls
    out) is not. A non-read statement therefore needs ``confirmed``.
    """
    from services.engines.base import strip_trailing_semicolon
    from services.plans import annotate, flatten

    engine, spec, profile = pool.open(user_id, conn_id, database)
    try:
        pieces = pick(spec, sql or "", "statement", caret)
    except ValueError as exc:
        raise OpError(str(exc)) from None
    if not pieces:
        raise OpError("Nothing to explain")
    piece = pieces[0]
    if analyze and not spec.can_analyze:
        raise OpError(f"{spec.label} cannot run a statement for its actual plan; "
                      "use Explain for the estimated one")
    writes = first_word(piece.text) not in _READ_FIRST_WORDS
    if analyze and writes and not confirmed:
        return {"ok": False, "needs_confirm": True, "statement": piece.text[:300]}

    started = time.monotonic()
    with connect(user_id, conn_id, database, timeout_s=86_400) as (conn, spec, _p, _engine):
        dbapi = conn.connection.dbapi_connection
        try:
            result = spec.explain(dbapi, strip_trailing_semicolon(piece.text), bool(analyze))
        except Exception as exc:
            raise OpError(error_text(exc)) from None
        # The connection is rolled back when it closes: ANALYZE kept nothing.
    annotate(result)
    return {
        "rows": flatten(result["plan"]), "summary": result.get("summary") or {},
        "raw": result["raw"], "format": result["format"], "analyzed": bool(analyze),
        "rolled_back": bool(analyze and writes), "sql": piece.text[:2000],
        "start": piece.start, "end": piece.end, "can_analyze": spec.can_analyze,
        "ms": int((time.monotonic() - started) * 1000),
    }


def _register_run(key: tuple, spec: Any, engine: Any, dbapi: Any, run_id: str) -> None:
    if run_id:
        session_id = spec.session_id(dbapi)
        with _runs_lock:
            _runs[key] = _Run(key[0], spec, engine, dbapi, session_id)


def _run_pieces(spec: Any, dbapi: Any, pieces: list, max_rows: int, key: tuple, run_id: str,
                results: list, *, manual: bool) -> bool:
    """Run until the first error. True when the connection is no longer usable."""
    for index, piece in enumerate(pieces):
        outcome = _run_piece(spec, dbapi, piece, index, max_rows, manual=manual)
        results.extend(outcome)
        if outcome and outcome[-1].get("error"):
            run = _runs.get(key) if run_id else None
            if run is not None and run.cancelled:
                outcome[-1]["error"] = "Cancelled. " + outcome[-1]["error"]
                return spec.name == "mssql"  # KILL ended the whole session
            break
    return False


def _execute_manual(user_id: int, conn_id: int, database: str, sid: str, pieces: list,
                    max_rows: int, run_id: str, profile: dict) -> dict:
    from services import sessions

    try:
        session = sessions.acquire(user_id, sid, conn_id, database)
    except sessions.SessionError as exc:
        return {"ok": False, "error": str(exc), "session_lost": True}
    if not session.lock.acquire(blocking=False):
        return {"ok": False, "error": "A statement is still running in this editor."}
    spec, dbapi = session.spec, session.dbapi
    results: list = []
    started = time.monotonic()
    key = (int(user_id), str(run_id or ""))
    broken = False
    try:
        spec.use_database(dbapi, database if "database" in spec.levels
                          and spec.database_for(profile, database) is None else None)
        _register_run(key, spec, session.engine, dbapi, run_id)
        try:
            spec.set_timeout(dbapi, profile.get("timeout_s") or 0)
        except Exception:  # noqa: BLE001 - a failed PostgreSQL transaction refuses SET too
            pass
        broken = _run_pieces(spec, dbapi, pieces, max_rows, key, run_id, results, manual=True)
        session.statements += len({r["index"] for r in results if not r.get("error")})
        _remember(user_id, conn_id, database, results)
    finally:
        with _runs_lock:
            _runs.pop(key, None)
        session.last_used = time.monotonic()
        state = None if broken else session.state()
        if state is not None and not state["open"]:
            session.statements = 0  # a COMMIT or ROLLBACK typed in the script
            state["statements"] = 0
        session.lock.release()
    if broken:
        sessions.drop(user_id, sid, "lost: cancelling ended the database session")
        state = {"session": sid, "open": False, "failed": False, "statements": 0, "lost": True}
    failed = any(result.get("error") for result in results)
    return {"ok": not failed, "results": results,
            "ms": int((time.monotonic() - started) * 1000), "read_only": False,
            "rolled_back": False, "txn": state}


def transaction(user_id: int, session: str, action: str) -> dict:
    """Commit, roll back or end a tab's manual-commit session."""
    from services import sessions

    try:
        if action == "commit":
            state = sessions.commit(user_id, session)
        elif action == "rollback":
            state = sessions.rollback(user_id, session)
        elif action == "end":
            return {"ok": True, **sessions.end(user_id, session)}
        elif action == "state":
            found = sessions.peek(user_id, session)
            if found is None or not found.lock.acquire(blocking=False):
                return {"ok": True, "txn": None}
            try:
                return {"ok": True, "txn": found.state()}
            finally:
                found.lock.release()
        else:
            raise OpError(f"Unknown transaction action {action!r}")
    except sessions.SessionError as exc:
        return {"ok": False, "error": str(exc)}
    if state.get("done") and not state.get("nothing"):
        session_ = sessions.peek(user_id, session)
        if session_ is not None:
            _remember(user_id, session_.conn_id, session_.database, [{
                "index": 0, "sql": "COMMIT" if action == "commit" else "ROLLBACK",
                "rowcount": state["done"], "ms": 0}])
    return {"ok": True, "txn": state}


def _run_piece(spec: Any, dbapi: Any, piece: Any, index: int, max_rows: int,
               manual: bool = False) -> list:
    """
    Every result set one piece produced; the last carries an error, if any.

    In auto-commit an error rolls the connection back. In manual mode it must
    not (that would discard the person's earlier work): an engine with
    ``savepoints`` undoes just the failed statement, the others leave the
    transaction as the server left it.
    """
    savepoint = manual and spec.savepoints and first_word(piece.text) not in _TXN_WORDS
    from services.engines.base import strip_trailing_semicolon

    text = piece.text if spec.name in ("postgresql", "sqlite") else strip_trailing_semicolon(piece.text)
    base = {"index": index, "start": piece.start, "end": piece.end, "sql": piece.text[:2000]}
    started = time.monotonic()
    cur = dbapi.cursor()
    out: list = []
    try:
        if savepoint:
            cur.execute("SAVEPOINT bk_stmt")
        cur.execute(text)
        while True:
            entry = dict(base)
            if cur.description:
                columns = [str(desc[0]) if desc[0] else f"column{i + 1}"
                           for i, desc in enumerate(cur.description)]
                fetched = cur.fetchmany(max_rows + 1)
                more = len(fetched) > max_rows
                rows, kinds, truncated = _encode_rows(fetched[:max_rows], len(columns))
                entry.update(columns=columns, rows=rows, kinds=kinds, truncated=truncated,
                             more=more, rowcount=len(rows))
            else:
                entry.update(columns=None, rowcount=cur.rowcount if cur.rowcount >= 0 else None)
            entry["ms"] = int((time.monotonic() - started) * 1000)
            out.append(entry)
            if not _next_result(cur):
                break
            started = time.monotonic()
        if savepoint:
            cur.execute("RELEASE SAVEPOINT bk_stmt")
    except Exception as exc:  # noqa: BLE001 - the statement's own error, shown as such
        entry = dict(base)
        entry.update(error=error_text(exc), ms=int((time.monotonic() - started) * 1000),
                     error_at=_error_offset(exc, piece))
        out.append(entry)
        try:
            if savepoint:
                cur2 = dbapi.cursor()
                cur2.execute("ROLLBACK TO SAVEPOINT bk_stmt")
                cur2.close()
                entry["error"] += "\n(Only this statement was undone; the transaction is still open.)"
            elif not manual:
                dbapi.rollback()
        except Exception:  # noqa: BLE001 - e.g. the transaction had already failed
            pass
    finally:
        try:
            cur.close()
        except Exception:  # noqa: BLE001
            pass
    return out


def _next_result(cur: Any) -> bool:
    nextset = getattr(cur, "nextset", None)
    if nextset is None:
        return False
    try:
        return bool(nextset())
    except Exception:  # noqa: BLE001 - sqlite3 has none; others may refuse
        return False


def _error_offset(exc: Exception, piece: Any) -> Optional[int]:
    """Where in the script the server says the error is, when it says."""
    diag = getattr(exc, "diag", None)
    position = getattr(diag, "statement_position", None) if diag else None
    if position:
        try:
            return piece.start + int(position) - 1
        except (TypeError, ValueError):
            return None
    match = re.search(r"\bat line (\d+)", str(exc))
    if match:
        line = int(match.group(1))
        lines = piece.text.split("\n")
        if 0 < line <= len(lines):
            return piece.start + sum(len(part) + 1 for part in lines[: line - 1])
    return None


def _remember(user_id: int, conn_id: int, database: str, results: list) -> None:
    from services.db import get_db

    seen: set = set()
    rows = []
    for result in results:
        if result["index"] in seen:
            continue
        seen.add(result["index"])
        rows.append((int(user_id), int(conn_id), database or "",
                     result["sql"][:HISTORY_SQL_CHARS], 0 if result.get("error") else 1,
                     result.get("rowcount"), result.get("ms")))
    if not rows:
        return
    with get_db() as db:
        db.executemany(
            "INSERT INTO query_history (user_id, conn_id, database, sql, ok, rows, ms) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
        db.execute(
            "DELETE FROM query_history WHERE user_id = ? AND id NOT IN ("
            "  SELECT id FROM query_history WHERE user_id = ? ORDER BY id DESC LIMIT ?)",
            (int(user_id), int(user_id), HISTORY_KEEP))


def history(user_id: int, conn_id: int = 0, limit: int = 200) -> list:
    from services.db import get_db

    limit = max(1, min(int(limit or 200), HISTORY_KEEP))
    with get_db() as db:
        if conn_id:
            rows = db.execute(
                "SELECT id, conn_id, database, sql, ok, rows, ms, ran_at FROM query_history "
                "WHERE user_id = ? AND conn_id = ? ORDER BY id DESC LIMIT ?",
                (int(user_id), int(conn_id), limit)).fetchall()
        else:
            rows = db.execute(
                "SELECT id, conn_id, database, sql, ok, rows, ms, ran_at FROM query_history "
                "WHERE user_id = ? ORDER BY id DESC LIMIT ?", (int(user_id), limit)).fetchall()
    return [dict(row) for row in rows]


def clear_history(user_id: int, conn_id: int = 0) -> None:
    from services.db import get_db

    with get_db() as db:
        if conn_id:
            db.execute("DELETE FROM query_history WHERE user_id = ? AND conn_id = ?",
                       (int(user_id), int(conn_id)))
        else:
            db.execute("DELETE FROM query_history WHERE user_id = ?", (int(user_id),))


# ---------------------------------------------------------------------------
# Editor helpers
# ---------------------------------------------------------------------------

def completion(user_id: int, conn_id: int, database: str = "") -> dict:
    """Tables and their columns, in CodeMirror lang-sql's ``schema`` shape."""
    from sqlalchemy.engine.reflection import ObjectKind

    with connect(user_id, conn_id, database) as (conn, spec, profile, _engine):
        insp = _inspector(conn)
        if "schema" in spec.levels:
            names = [n for n in insp.get_schema_names() if not spec.is_system_schema(n)]
            default = insp.default_schema_name
        elif "database" in spec.levels:
            default = database or profile.get("database") or ""
            names = [default] if default else []
        else:
            names, default = [None], None
        nested: dict = {}
        total = 0
        for schema_name in names:
            if total >= MAX_COMPLETION_TABLES:
                break
            try:
                found = insp.get_multi_columns(schema=schema_name, kind=ObjectKind.ANY)
            except Exception:  # noqa: BLE001 - one unreadable schema is not fatal
                continue
            tables: dict = {}
            for (_schema, table_name), cols in found.items():
                tables[table_name] = [col["name"] for col in cols]
                total += 1
                if total >= MAX_COMPLETION_TABLES:
                    break
            nested[schema_name or ""] = tables
    if names == [None]:
        return {"schema": nested.get("", {}), "default_schema": None, "tables": total}
    return {"schema": nested, "default_schema": default, "tables": total}


def format_sql(sql: str) -> str:
    import sqlparse

    return sqlparse.format(sql or "", reindent=True, keyword_case="upper",
                           indent_width=2, wrap_after=100).strip() + "\n"


def script(user_id: int, conn_id: int, database: str, schema: str, name: str,
           kind: str) -> str:
    """A starting SELECT / INSERT / UPDATE / DELETE for a table, or its DDL."""
    info = table_info(user_id, conn_id, database, schema, name)
    if kind == "ddl":
        return info["ddl"]
    with connect(user_id, conn_id, database) as (conn, _spec, _profile, _engine):
        prep = conn.dialect.identifier_preparer
    target = info["qualified"]
    cols = info["columns"]
    quoted = [prep.quote(col["name"]) for col in cols]
    pk = [prep.quote(col) for col in info["pk"]] or quoted[:1]
    if kind == "select":
        listing = ",\n  ".join(quoted)
        return f"SELECT\n  {listing}\nFROM {target};\n"
    if kind == "insert":
        writable = [col for col in cols if not col["autoincrement"] and not col["computed"]]
        names = ", ".join(prep.quote(col["name"]) for col in writable)
        values = ",\n  ".join(f"NULL  -- {col['name']}: {col['type']}"
                              + ("" if col["nullable"] else " NOT NULL") for col in writable)
        return f"INSERT INTO {target} ({names})\nVALUES (\n  {values}\n);\n"
    if kind == "update":
        sets = ",\n  ".join(f"{prep.quote(col['name'])} = NULL" for col in cols
                            if not col["pk"] and not col["computed"])
        where = " AND ".join(f"{col} = NULL" for col in pk)
        return f"UPDATE {target}\nSET\n  {sets}\nWHERE {where};\n"
    if kind == "delete":
        where = " AND ".join(f"{col} = NULL" for col in pk)
        return f"DELETE FROM {target}\nWHERE {where};\n"
    raise OpError(f"Unknown script kind {kind!r}")
