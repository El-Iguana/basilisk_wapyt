"""
CockroachDB, through psycopg 3 and sqlalchemy-cockroachdb. It speaks
PostgreSQL's SQL and wire protocol, so the adapter is PostgreSQL's with a
few differences:

* Its own dialect for reflection (``cockroachdb+basilisk``): the table list
  leaves out views (``information_schema.tables`` lists them too), and
  foreign keys are read table by table (the bundled multi-table query does
  not match SQLAlchemy 2.1's columns).
* EXPLAIN has no JSON form: the text tree (``• scan`` nodes with indented
  ``key: value`` lines) is parsed (``plans.parse_cockroach``).
* No server sessions listing yet (``SHOW CLUSTER SESSIONS`` ids are not
  numbers); ``SERIAL`` is ``unique_rowid()``, so imports need no sequence
  fix-up.
* ``ALTER COLUMN … TYPE`` beyond a safe widening needs the
  ``enable_experimental_alter_column_type_general`` setting; the script
  says so where it matters (compare notes).
"""
from __future__ import annotations

from typing import Any, Optional

from sqlalchemy_cockroachdb.psycopg import CockroachDBDialect_psycopg

from services.engines.postgresql import PostgreSQL


class BasiliskCockroachDialect(CockroachDBDialect_psycopg):
    supports_statement_cache = True

    def get_table_names(self, conn: Any, schema: Optional[str] = None, **kw: Any) -> list:
        from sqlalchemy import text

        rows = conn.execute(text(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = :schema "
            "AND table_type = 'BASE TABLE' ORDER BY table_name"),
            {"schema": schema or self.default_schema_name}).all()
        return [row[0] for row in rows]

    def get_columns(self, conn: Any, table_name: str, schema: Optional[str] = None,
                    **kw: Any) -> list:
        """The bundled reflection leaves identity columns out: they number themselves too."""
        from sqlalchemy import text

        columns = super().get_columns(conn, table_name, schema, **kw)
        identity = {row[0]: row[1] for row in conn.execute(text(
            "SELECT column_name, identity_generation FROM information_schema.columns "
            "WHERE table_schema = :schema AND table_name = :table AND is_identity = 'YES'"),
            {"schema": schema or self.default_schema_name, "table": table_name}).all()}
        for column in columns:
            if column["name"] in identity:
                column["identity"] = {"always": identity[column["name"]] == "ALWAYS"}
                column["autoincrement"] = True
        return columns

    def get_view_definition(self, connection: Any, view_name: str,
                            schema: Optional[str] = None, **kw: Any) -> Optional[str]:
        """
        SHOW CREATE VIEW: pg_get_viewdef() answers another schema's view when two
        share a name (CockroachDB 26), and the bundled lookup ignores the schema.
        """
        prep = self.identifier_preparer
        name = prep.quote_schema(schema or self.default_schema_name) + "." + prep.quote(view_name)
        row = connection.exec_driver_sql(f"SHOW CREATE VIEW {name}").first()
        return None if row is None else str(row[1])

    def _hidden(self, conn: Any, schema: Optional[str]) -> set:
        from sqlalchemy import text

        rows = conn.execute(text(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema = :schema AND is_hidden = 'YES'"),
            {"schema": schema or self.default_schema_name}).all()
        return {(table, column) for table, column in rows}

    def _no_hidden_key(self, conn: Any, schema: Optional[str], table: str, pk: dict,
                       hidden: Optional[set] = None) -> dict:
        """A table made without a key gets a hidden ``rowid`` key: that is no key here."""
        columns = (pk or {}).get("constrained_columns") or []
        hidden = self._hidden(conn, schema) if hidden is None else hidden
        if columns and all((table, column) in hidden for column in columns):
            return {"constrained_columns": [], "name": None}
        return pk

    # Neither calls the other: sqlalchemy-cockroachdb 2.0.4 (under SQLAlchemy
    # 2.0) answers the multi form through the single one, SQLAlchemy 2.1's
    # PostgreSQL dialect the other way round.
    def get_pk_constraint(self, connection: Any, table_name: str, schema: Optional[str] = None,
                          **kw: Any) -> dict:
        pk = super().get_pk_constraint(connection, table_name, schema, **kw)
        return self._no_hidden_key(connection, schema, table_name, pk)

    def get_multi_pk_constraint(self, connection: Any, schema: Optional[str] = None,
                                filter_names: Any = None, scope: Any = None, kind: Any = None,
                                **kw: Any) -> list:
        from sqlalchemy.engine.reflection import ObjectKind, ObjectScope

        found = list(super().get_multi_pk_constraint(
            connection, schema=schema, filter_names=filter_names,
            scope=scope or ObjectScope.DEFAULT, kind=kind or ObjectKind.TABLE, **kw))
        hidden = self._hidden(connection, schema)
        return [(key, self._no_hidden_key(connection, schema, key[1], pk, hidden))
                for key, pk in found]

    _FK_ACTIONS = {"c": "CASCADE", "n": "SET NULL", "d": "SET DEFAULT", "r": "RESTRICT"}

    def _foreign_keys(self, conn: Any, schema: Optional[str], names: Any) -> dict:
        """
        ``{table: [fk, …]}`` from pg_constraint, read here: the bundled readers
        call each other in a loop under one SQLAlchemy or the other.
        """
        from sqlalchemy import bindparam, text

        target = schema or self.default_schema_name
        rows = conn.execute(text(
            "SELECT cl.relname, con.conname, con.conkey, con.confkey, rn.nspname, rcl.relname, "
            "con.confdeltype, con.confupdtype, con.conrelid, con.confrelid "
            "FROM pg_catalog.pg_constraint con "
            "JOIN pg_catalog.pg_class cl ON cl.oid = con.conrelid "
            "JOIN pg_catalog.pg_namespace n ON n.oid = cl.relnamespace "
            "JOIN pg_catalog.pg_class rcl ON rcl.oid = con.confrelid "
            "JOIN pg_catalog.pg_namespace rn ON rn.oid = rcl.relnamespace "
            "WHERE con.contype = 'f' AND n.nspname = :schema ORDER BY 1, 2"),
            {"schema": target}).all()
        if names is not None:
            wanted = set(names)
            rows = [row for row in rows if row[0] in wanted]
        relids = sorted({row[8] for row in rows} | {row[9] for row in rows})
        attnames: dict = {}
        if relids:
            for relid, num, attname in conn.execute(text(
                    "SELECT attrelid, attnum, attname FROM pg_catalog.pg_attribute "
                    "WHERE attrelid IN :ids").bindparams(bindparam("ids", expanding=True)),
                    {"ids": relids}).all():
                attnames[(relid, num)] = attname
        out: dict = {}
        for table, name, conkey, confkey, ref_schema, ref_table, on_delete, on_update, \
                relid, ref_relid in rows:
            out.setdefault(table, []).append({
                "name": name,
                "constrained_columns": [attnames.get((relid, n), str(n)) for n in conkey or []],
                "referred_schema": None if ref_schema == target and schema is None else ref_schema,
                "referred_table": ref_table,
                "referred_columns": [attnames.get((ref_relid, n), str(n)) for n in confkey or []],
                "options": {key: self._FK_ACTIONS[code] for key, code in
                            (("ondelete", on_delete), ("onupdate", on_update))
                            if code in self._FK_ACTIONS},
            })
        return out

    def get_foreign_keys(self, connection: Any, table_name: str, schema: Optional[str] = None,
                         **kw: Any) -> list:
        return self._foreign_keys(connection, schema, [table_name]).get(table_name, [])

    def get_multi_foreign_keys(self, connection: Any, schema: Optional[str] = None,
                               filter_names: Any = None, scope: Any = None, kind: Any = None,
                               **kw: Any) -> list:
        names = filter_names if filter_names is not None else \
            self.get_table_names(connection, schema)
        found = self._foreign_keys(connection, schema, names)
        return [((schema, name), found.get(name, [])) for name in names]

def register() -> None:
    from sqlalchemy.dialects import registry

    registry.register("cockroachdb.basilisk", "services.engines.cockroachdb",
                      "BasiliskCockroachDialect")


class CockroachDB(PostgreSQL):
    name = "cockroachdb"
    label = "CockroachDB"
    icon = "mdi-database-sync-outline"
    default_port = 26257
    default_database = "defaultdb"
    system_databases = frozenset({"system", "postgres"})
    system_schemas = frozenset({"information_schema", "pg_catalog", "pg_extension",
                                "crdb_internal"})
    has_sessions = False

    def url(self, profile: dict, database: Optional[str]):
        register()
        return super().url(profile, database).set(drivername="cockroachdb+basilisk")

    def explain(self, dbapi_conn: Any, sql: str, analyze: bool) -> dict:
        from services.plans import parse_cockroach

        with dbapi_conn.cursor() as cur:
            cur.execute(("EXPLAIN ANALYZE " if analyze else "EXPLAIN ") + sql)
            lines = [str(row[0]) for row in cur.fetchall()]
        return {**parse_cockroach(lines), "raw": "\n".join(lines), "format": "text"}

    def sql_change_primary_key(self, prep: Any, schema: Optional[str], table: str,
                               old_name: str, new_columns: list, old_columns: list) -> list:
        # Tables are schema-locked by default (v25+): a lone DROP CONSTRAINT on the
        # key is refused, and ALTER PRIMARY KEY keeps the old key as a unique
        # index. One statement that drops and adds does neither.
        target = self.qualified(prep, schema, table)
        columns = ", ".join(prep.quote(c) for c in new_columns)
        if not new_columns:
            return []           # every CockroachDB table keeps a primary key
        if not old_columns:
            return [("add", f"ALTER TABLE {target} ALTER PRIMARY KEY USING COLUMNS ({columns})")]
        name = prep.quote(old_name)
        return [("add", f"ALTER TABLE {target} DROP CONSTRAINT {name}, "
                        f"ADD CONSTRAINT {name} PRIMARY KEY ({columns})")]

    def sql_alter_column(self, prep: Any, schema: Optional[str], table: str, column: str,
                         new: dict, old: dict, changed: set) -> list:
        out = super().sql_alter_column(prep, schema, table, column, new, old, changed)
        if "type" in changed:
            # Changing a type in place (beyond a safe widening) is behind a setting.
            out.insert(0, "SET enable_experimental_alter_column_type_general = true")
        return out
