"""
A SQLAlchemy dialect for DuckDB: duckdb_engine's, with reflection read from
DuckDB's own catalog functions (``duckdb_columns()``, ``duckdb_constraints()``,
``duckdb_indexes()`` …). A plain module.

duckdb_engine inherits SQLAlchemy's PostgreSQL reflection, whose queries
(SQLAlchemy 2.1: ``pg_collation``, ``REGCLASS`` casts) DuckDB's pg_catalog
emulation cannot answer. Registered as ``duckdb+basilisk``.

Only one schema per catalogue is read at a time; the "default" schema is
``main``. Keys are ``(schema, table)`` with ``schema`` exactly as asked
(``None`` for the default), as SQLAlchemy's multi-reflection expects.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from duckdb_engine import Dialect as _DuckDBDialect, DuckDBIdentifierPreparer
from sqlalchemy import text, types as T
from sqlalchemy.dialects.postgresql.base import PGTypeCompiler
from sqlalchemy.engine.reflection import ObjectKind

DEFAULT_SCHEMA = "main"


class DuckDBTypeCompiler(PGTypeCompiler):
    """Types under DuckDB's own names (PostgreSQL's would say BYTEA, DOUBLE PRECISION…)."""

    def visit_large_binary(self, type_, **kw):
        return "BLOB"

    visit_BLOB = visit_LargeBinary = visit_BYTEA = visit_large_binary

    def visit_double(self, type_, **kw):
        return "DOUBLE"

    visit_DOUBLE = visit_DOUBLE_PRECISION = visit_double

    def visit_TIMESTAMP(self, type_, **kw):
        return "TIMESTAMP WITH TIME ZONE" if getattr(type_, "timezone", False) else "TIMESTAMP"

    def visit_datetime(self, type_, **kw):
        return self.visit_TIMESTAMP(type_, **kw)

    def visit_TIME(self, type_, **kw):
        return "TIME WITH TIME ZONE" if getattr(type_, "timezone", False) else "TIME"

    def visit_time(self, type_, **kw):
        return self.visit_TIME(type_, **kw)

    def visit_NUMERIC(self, type_, **kw):
        if type_.precision is None:
            return "DECIMAL"
        if type_.scale is None:
            return f"DECIMAL({type_.precision})"
        return f"DECIMAL({type_.precision}, {type_.scale})"

    visit_numeric = visit_DECIMAL = visit_NUMERIC

    def visit_duckdb_named(self, type_, **kw):
        return type_.name


class DuckDBNamed(T.TypeEngine):
    """A DuckDB type SQLAlchemy has no class for (LIST, STRUCT, MAP, UNION…), by name."""
    __visit_name__ = "duckdb_named"

    def __init__(self, name: str) -> None:
        self.name = name

    def __repr__(self) -> str:
        return f"DuckDBNamed({self.name!r})"


_SIMPLE = {
    "BOOLEAN": T.Boolean, "TINYINT": T.SmallInteger, "SMALLINT": T.SmallInteger,
    "INTEGER": T.Integer, "BIGINT": T.BigInteger, "UTINYINT": T.SmallInteger,
    "USMALLINT": T.Integer, "UINTEGER": T.BigInteger, "FLOAT": T.REAL, "REAL": T.REAL,
    "DOUBLE": T.Double, "DATE": T.Date, "INTERVAL": T.Interval, "BLOB": T.LargeBinary,
    "UUID": T.Uuid, "JSON": T.JSON, "VARCHAR": T.String, "BIT": DuckDBNamed,
}


def column_type(data_type: str, precision: Any = None, scale: Any = None) -> T.TypeEngine:
    """A ``duckdb_columns().data_type`` as a SQLAlchemy type."""
    name = (data_type or "").strip()
    upper = name.upper()
    if upper.startswith("DECIMAL"):
        found = re.match(r"DECIMAL\((\d+),\s*(\d+)\)", upper)
        if found:
            return T.Numeric(int(found.group(1)), int(found.group(2)))
        return T.Numeric(precision, scale)
    if upper in ("HUGEINT", "UBIGINT", "UHUGEINT"):
        return T.Numeric(39 if upper != "UBIGINT" else 20, 0)
    if upper.startswith("TIMESTAMP"):
        return T.DateTime(timezone="TIME ZONE" in upper)
    if upper.startswith("TIME"):
        return T.Time(timezone="TIME ZONE" in upper)
    if upper.startswith("VARCHAR"):
        return T.String()
    simple = _SIMPLE.get(upper)
    if simple is DuckDBNamed or simple is None:
        return DuckDBNamed(name)
    return simple()


def _index_columns(expressions: Optional[str]) -> tuple:
    """``(column_names, expressions)`` from duckdb_indexes().expressions (``[a, "b c"]``)."""
    inside = (expressions or "").strip()
    if inside.startswith("[") and inside.endswith("]"):
        inside = inside[1:-1]
    parts, depth, current, quote = [], 0, "", ""
    for ch in inside:
        if quote:
            current += ch
            if ch == quote:
                quote = ""
        elif ch in "'\"":
            quote = ch
            current += ch
        elif ch == "(":
            depth += 1
            current += ch
        elif ch == ")":
            depth -= 1
            current += ch
        elif ch == "," and depth == 0:
            parts.append(current.strip())
            current = ""
        else:
            current += ch
    if current.strip():
        parts.append(current.strip())
    names, exprs = [], []
    for part in parts:
        bare = part[1:-1].replace('""', '"') if part.startswith('"') and part.endswith('"') else part
        if re.fullmatch(r"[A-Za-z_][\w$]*", part) or (part.startswith('"') and part.endswith('"')):
            names.append(bare)
            exprs.append(None)
        else:
            names.append(None)
            exprs.append(part)
    return names, exprs


class DuckDBPreparer(DuckDBIdentifierPreparer):
    """Quotes every word DuckDB's grammar keeps for itself, not only the "reserved" ones
    (``at``, ``columns``, ``values`` … cannot be bare column names either)."""

    def __init__(self, dialect: Any, **kwargs: Any) -> None:
        import duckdb

        super().__init__(dialect, **kwargs)
        self.reserved_words.update(row[0] for row in duckdb.cursor().execute(
            "SELECT keyword_name FROM duckdb_keywords() "
            "WHERE keyword_category IN ('reserved', 'column_name', 'type_function')").fetchall())


class BasiliskDuckDBDialect(_DuckDBDialect):
    supports_statement_cache = False
    type_compiler_cls = DuckDBTypeCompiler
    preparer = DuckDBPreparer

    # -- names ------------------------------------------------------------

    @staticmethod
    def _where(schema: Optional[str]) -> tuple:
        return ("database_name = current_database() AND schema_name = :schema",
                {"schema": schema or DEFAULT_SCHEMA})

    def _names(self, connection: Any, function: str, column: str, schema: Optional[str],
               extra: str = "") -> list:
        where, params = self._where(schema)
        rows = connection.execute(text(
            f"SELECT {column} FROM {function}() WHERE {where} {extra} ORDER BY 1"), params).all()
        return [row[0] for row in rows]

    def get_schema_names(self, connection: Any, **kw: Any) -> list:
        rows = connection.execute(text(
            "SELECT schema_name FROM duckdb_schemas() WHERE database_name = current_database() "
            "AND (NOT internal OR schema_name = 'main') ORDER BY schema_name <> 'main', 1")).all()
        return [row[0] for row in rows]

    def connect(self, *cargs: Any, **cparams: Any) -> Any:
        import duckdb

        try:
            return super().connect(*cargs, **cparams)
        except duckdb.ConnectionException as exc:
            if "different configuration" not in str(exc):
                raise
            raise duckdb.ConnectionException(
                "This DuckDB file is already open in Basilisk the other way (read-only or "
                "read-write), and DuckDB opens a file one way per process: disconnect the "
                "other connection to it first") from exc

    def _get_default_schema_name(self, connection: Any) -> str:
        return DEFAULT_SCHEMA

    def get_table_names(self, connection: Any, schema: Optional[str] = None, **kw: Any) -> list:
        return self._names(connection, "duckdb_tables", "table_name", schema,
                           "AND NOT temporary")

    def get_view_names(self, connection: Any, schema: Optional[str] = None, **kw: Any) -> list:
        return self._names(connection, "duckdb_views", "view_name", schema,
                           "AND NOT internal AND NOT temporary")

    def get_materialized_view_names(self, connection: Any, schema: Optional[str] = None,
                                    **kw: Any) -> list:
        return []

    def get_sequence_names(self, connection: Any, schema: Optional[str] = None,
                           **kw: Any) -> list:
        return self._names(connection, "duckdb_sequences", "sequence_name", schema)

    def has_table(self, connection: Any, table_name: str, schema: Optional[str] = None,
                  **kw: Any) -> bool:
        return table_name in self.get_table_names(connection, schema) or \
            table_name in self.get_view_names(connection, schema)

    def get_view_definition(self, connection: Any, view_name: str,
                            schema: Optional[str] = None, **kw: Any) -> Optional[str]:
        where, params = self._where(schema)
        return connection.execute(text(
            f"SELECT sql FROM duckdb_views() WHERE {where} AND view_name = :name"),
            {**params, "name": view_name}).scalar()

    def _relations(self, connection: Any, schema: Optional[str], filter_names: Any,
                   kind: Any) -> list:
        """The tables (and views, when ``kind`` asks) a multi-reflection call covers."""
        kind = ObjectKind.TABLE if kind is None else kind
        names: list = []
        if ObjectKind.TABLE in kind:
            names += self.get_table_names(connection, schema)
        if ObjectKind.VIEW in kind:
            names += self.get_view_names(connection, schema)
        if filter_names is not None:
            wanted = set(filter_names)
            names = [name for name in names if name in wanted]
        return names

    # -- columns and constraints --------------------------------------------

    def get_multi_columns(self, connection: Any, schema: Optional[str] = None,
                          filter_names: Any = None, scope: Any = None, kind: Any = None,
                          **kw: Any) -> list:
        names = self._relations(connection, schema, filter_names, kind)
        out: dict = {(schema, name): [] for name in names}
        where, params = self._where(schema)
        rows = connection.execute(text(
            "SELECT table_name, column_name, data_type, column_default, is_nullable, "
            "numeric_precision, numeric_scale, comment FROM duckdb_columns() "
            f"WHERE {where} ORDER BY table_name, column_index"), params).all()
        for table, name, data_type, default, nullable, precision, scale, comment in rows:
            if (schema, table) not in out:
                continue
            default = None if default is None else str(default)
            out[(schema, table)].append({
                "name": name, "type": column_type(data_type, precision, scale),
                "nullable": bool(nullable), "default": default, "comment": comment,
                "autoincrement": bool(default and default.lower().startswith("nextval(")),
            })
        return list(out.items())

    def _constraints(self, connection: Any, schema: Optional[str], kind: str) -> list:
        where, params = self._where(schema)
        return connection.execute(text(
            "SELECT table_name, constraint_name, constraint_column_names, referenced_table, "
            "referenced_column_names, expression FROM duckdb_constraints() "
            f"WHERE {where} AND constraint_type = :kind ORDER BY table_name, constraint_index"),
            {**params, "kind": kind}).all()

    def get_multi_pk_constraint(self, connection: Any, schema: Optional[str] = None,
                                filter_names: Any = None, scope: Any = None, kind: Any = None,
                                **kw: Any) -> list:
        names = self._relations(connection, schema, filter_names, kind)
        out = {(schema, name): {"constrained_columns": [], "name": None} for name in names}
        for table, name, columns, *_rest in self._constraints(connection, schema, "PRIMARY KEY"):
            if (schema, table) in out:
                out[(schema, table)] = {"constrained_columns": list(columns or []), "name": name}
        return list(out.items())

    def get_multi_foreign_keys(self, connection: Any, schema: Optional[str] = None,
                               filter_names: Any = None, scope: Any = None, kind: Any = None,
                               **kw: Any) -> list:
        names = self._relations(connection, schema, filter_names, kind)
        out: dict = {(schema, name): [] for name in names}
        for table, name, columns, ref_table, ref_columns, _expr in \
                self._constraints(connection, schema, "FOREIGN KEY"):
            if (schema, table) in out:
                out[(schema, table)].append({
                    "name": name, "constrained_columns": list(columns or []),
                    "referred_schema": None, "referred_table": ref_table,
                    "referred_columns": list(ref_columns or []), "options": {},
                })
        return list(out.items())

    def get_multi_unique_constraints(self, connection: Any, schema: Optional[str] = None,
                                     filter_names: Any = None, scope: Any = None,
                                     kind: Any = None, **kw: Any) -> list:
        names = self._relations(connection, schema, filter_names, kind)
        out: dict = {(schema, name): [] for name in names}
        for table, name, columns, *_rest in self._constraints(connection, schema, "UNIQUE"):
            if (schema, table) in out:
                out[(schema, table)].append({"name": name, "column_names": list(columns or [])})
        return list(out.items())

    def get_multi_check_constraints(self, connection: Any, schema: Optional[str] = None,
                                    filter_names: Any = None, scope: Any = None,
                                    kind: Any = None, **kw: Any) -> list:
        names = self._relations(connection, schema, filter_names, kind)
        out: dict = {(schema, name): [] for name in names}
        for table, name, _cols, _ref, _ref_cols, expression in \
                self._constraints(connection, schema, "CHECK"):
            if (schema, table) in out:
                out[(schema, table)].append({"name": name, "sqltext": expression})
        return list(out.items())

    def get_multi_indexes(self, connection: Any, schema: Optional[str] = None,
                          filter_names: Any = None, scope: Any = None, kind: Any = None,
                          **kw: Any) -> list:
        names = self._relations(connection, schema, filter_names, kind)
        out: dict = {(schema, name): [] for name in names}
        where, params = self._where(schema)
        rows = connection.execute(text(
            "SELECT table_name, index_name, is_unique, expressions FROM duckdb_indexes() "
            f"WHERE {where} AND NOT is_primary ORDER BY table_name, index_name"), params).all()
        for table, name, unique, expressions in rows:
            if (schema, table) not in out:
                continue
            columns, exprs = _index_columns(expressions)
            entry = {"name": name, "column_names": columns, "unique": bool(unique),
                     "dialect_options": {}}
            if any(expr is not None for expr in exprs):
                entry["expressions"] = [expr or col for col, expr in zip(columns, exprs)]
            out[(schema, table)].append(entry)
        return list(out.items())

    def get_indexes(self, connection: Any, table_name: str, schema: Optional[str] = None,
                    **kw: Any) -> list:
        # duckdb_engine answers [] here; ours reads duckdb_indexes().
        found = dict(self.get_multi_indexes(connection, schema, [table_name],
                                            kind=ObjectKind.ANY))
        return found.get((schema, table_name), [])

    def get_multi_table_options(self, connection: Any, **kw: Any) -> list:
        raise NotImplementedError      # no storage options: reflection skips it

    def get_multi_table_comment(self, connection: Any, schema: Optional[str] = None,
                                filter_names: Any = None, scope: Any = None, kind: Any = None,
                                **kw: Any) -> list:
        names = self._relations(connection, schema, filter_names, kind)
        where, params = self._where(schema)
        comments = dict(connection.execute(text(
            f"SELECT table_name, comment FROM duckdb_tables() WHERE {where}"), params).all())
        return [((schema, name), {"text": comments.get(name)}) for name in names]


def register() -> None:
    from sqlalchemy.dialects import registry

    registry.register("duckdb.basilisk", "services.engines.duckdb_dialect",
                      "BasiliskDuckDBDialect")
