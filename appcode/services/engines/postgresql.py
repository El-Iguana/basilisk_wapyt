"""PostgreSQL, through psycopg 3."""
from __future__ import annotations

from typing import Any, Optional

from services.engines.base import Engine, Field, info

SSL_MODES = (
    ("prefer", "prefer (TLS when offered)"),
    ("disable", "disable"),
    ("require", "require (no certificate check)"),
    ("verify-full", "verify-full"),
)


class PostgreSQL(Engine):
    name = "postgresql"
    label = "PostgreSQL"
    icon = "mdi-elephant"
    default_port = 5432
    levels = ("database", "schema")
    default_database = "postgres"
    editor_dialect = "PostgreSQL"
    family = "postgresql"
    has_matviews = True
    system_schemas = frozenset({"information_schema", "pg_catalog", "pg_toast"})
    system_schema_prefixes = ("pg_temp_", "pg_toast_temp_")
    system_databases = frozenset({"postgres"})
    extra_fields = (
        Field("sslmode", "TLS", kind="select", default="prefer", options=SSL_MODES),
    )

    def url(self, profile: dict, database: Optional[str]):
        from sqlalchemy import URL

        return URL.create(
            "postgresql+psycopg",
            username=profile.get("username") or None,
            password=profile.get("password") or None,
            host=profile.get("host") or None,
            port=int(profile.get("port") or self.default_port),
            database=self.database_for(profile, database),
        )

    def engine_kwargs(self, profile: dict) -> dict:
        options = profile.get("options") or {}
        sslmode = options.get("sslmode") or "prefer"
        if sslmode not in {value for value, _ in SSL_MODES}:
            sslmode = "prefer"
        return {"connect_args": {
            "sslmode": sslmode, "connect_timeout": 10, "application_name": "Basilisk",
        }}

    def on_connect(self, dbapi_conn: Any, profile: dict) -> None:
        if profile.get("read_only"):
            with dbapi_conn.cursor() as cur:
                cur.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
            dbapi_conn.commit()

    def set_timeout(self, dbapi_conn: Any, seconds: int) -> None:
        with dbapi_conn.cursor() as cur:
            cur.execute(f"SET statement_timeout = {max(0, int(seconds)) * 1000}")

    def set_autocommit(self, dbapi_conn: Any, on: bool) -> None:
        if dbapi_conn.autocommit != on:
            dbapi_conn.rollback()
            dbapi_conn.autocommit = on

    savepoints = True

    def transaction_state(self, dbapi_conn: Any, statements: int) -> dict:
        from psycopg.pq import TransactionStatus

        status = dbapi_conn.info.transaction_status
        return {"open": status in (TransactionStatus.INTRANS, TransactionStatus.INERROR),
                "failed": status == TransactionStatus.INERROR}

    def explain(self, dbapi_conn: Any, sql: str, analyze: bool) -> dict:
        import json

        from services.plans import parse_postgresql

        options = "FORMAT JSON, ANALYZE, BUFFERS" if analyze else "FORMAT JSON"
        with dbapi_conn.cursor() as cur:
            cur.execute(f"EXPLAIN ({options}) {sql}")
            document = cur.fetchone()[0]
        if isinstance(document, str):
            document = json.loads(document)
        return {**parse_postgresql(document), "raw": json.dumps(document, indent=2),
                "format": "json"}

    def session_id(self, dbapi_conn: Any) -> Any:
        return None  # psycopg cancels on the connection object itself

    def cancel(self, engine: Any, dbapi_conn: Any, session: Any) -> None:
        # Thread-safe: sends a cancel request on a side channel.
        dbapi_conn.cancel_safe(timeout=5)

    def sql_bytes_literal(self, raw: bytes) -> str:
        return "'\\x" + raw.hex() + "'::bytea"

    def before_compare(self, conn: Any, schema: Optional[str]) -> None:
        # pg_get_viewdef leaves out the schema of tables on the search path, so
        # a view copied to another schema would still read this one's tables.
        # With only pg_catalog searched, every name comes back qualified.
        if schema:
            conn.exec_driver_sql("SET LOCAL search_path TO pg_catalog")

    def sql_alter_column(self, prep: Any, schema: Optional[str], table: str, column: str,
                         new: dict, old: dict, changed: set) -> list:
        out = super().sql_alter_column(prep, schema, table, column, new, old, changed - {"type"})
        if "type" in changed:
            col = prep.quote(column)
            # USING converts the stored values; without it text -> integer is refused.
            out.insert(0, f"ALTER TABLE {self.qualified(prep, schema, table)} ALTER COLUMN {col} "
                          f"TYPE {new['type']} USING {col}::{new['type']}")
        return out

    def import_wrappers(self, conn: Any, prep: Any, schema: Optional[str], table: str,
                        columns: list) -> tuple:
        """After explicit values in a serial or identity column, move its sequence past them."""
        from sqlalchemy import text

        name = self.qualified(prep, schema, table)
        after = []
        for column in columns:
            sequence = conn.execute(text("SELECT pg_get_serial_sequence(:table, :column)"),
                                    {"table": name, "column": column}).scalar()
            if sequence:
                quoted = "'" + sequence.replace("'", "''") + "'"
                after.append(f"SELECT setval({quoted}, COALESCE(MAX({prep.quote(column)}), 0) + 1, "
                             f"false) FROM {name}")
        return [], after

    has_sessions = True

    def list_sessions(self, conn: Any, include_idle: bool) -> list:
        from sqlalchemy import text

        rows = conn.execute(text(
            "SELECT pid, usename, datname, "
            "  COALESCE(host(client_addr), CASE WHEN client_port = -1 THEN 'local socket' END), "
            "  application_name, state, "
            "  CASE WHEN state = 'active' THEN query_start ELSE state_change END, "
            "  EXTRACT(EPOCH FROM clock_timestamp() - "
            "    CASE WHEN state = 'active' THEN query_start ELSE state_change END), "
            "  CASE WHEN wait_event IS NOT NULL THEN wait_event_type || ': ' || wait_event END, "
            "  pg_blocking_pids(pid), query, pid = pg_backend_pid(), xact_start IS NOT NULL "
            "FROM pg_stat_activity WHERE backend_type = 'client backend' "
            + ("" if include_idle else "AND state IS DISTINCT FROM 'idle' ")
            + "ORDER BY state = 'active' DESC, query_start NULLS LAST")).all()
        return [{
            "id": row[0], "user": row[1], "database": row[2], "client": row[3],
            "application": row[4], "state": row[5], "started": row[6], "seconds": row[7],
            "wait": row[8], "blocked_by": list(row[9] or []), "query": row[10], "own": row[11],
            "txn": bool(row[12]),
        } for row in rows]

    def kill_session(self, engine: Any, conn: Any, session_id: int, mode: str) -> str:
        from sqlalchemy import text

        function = "pg_cancel_backend" if mode == "cancel" else "pg_terminate_backend"
        done = conn.execute(text(f"SELECT {function}(:pid)"), {"pid": int(session_id)}).scalar()
        if not done:
            raise EngineError(f"Session {session_id} is gone, or this login may not stop it")
        return f"SELECT {function}({int(session_id)})"

    def list_databases(self, conn: Any) -> list:
        from sqlalchemy import text

        rows = conn.execute(text(
            "SELECT datname FROM pg_database WHERE datallowconn AND NOT datistemplate "
            "ORDER BY datname")).all()
        return [row[0] for row in rows]

    def row_estimates(self, conn: Any, schema: Optional[str]) -> dict:
        from sqlalchemy import text

        rows = conn.execute(text(
            "SELECT c.relname, c.reltuples::bigint FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = :schema AND c.relkind IN ('r', 'p', 'm')"),
            {"schema": schema or "public"}).all()
        # -1 means "never analysed" (PostgreSQL 14+).
        return {name: int(count) for name, count in rows if count is not None and count >= 0}

    # ------------------------------------------------------------------
    # More navigator objects
    # ------------------------------------------------------------------
    object_groups = ("functions", "procedures", "sequences", "triggers")
    has_roles = True
    common_types = ("integer", "bigint", "smallint", "numeric(12,2)", "real",
                    "double precision", "boolean", "text", "varchar(255)", "date", "time",
                    "timestamp", "timestamptz", "uuid", "jsonb", "bytea")

    def list_routines(self, conn: Any, schema: Optional[str]) -> list:
        from sqlalchemy import text

        rows = conn.execute(text(
            "SELECT p.oid, p.proname, p.prokind, pg_get_function_identity_arguments(p.oid), "
            "pg_get_function_result(p.oid) FROM pg_proc p "
            "JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname = :schema AND p.prokind IN ('f', 'p') "
            "ORDER BY p.proname, 4"), {"schema": schema or "public"}).all()
        return [{"id": str(oid), "name": name, "kind": "procedure" if kind == "p" else "function",
                 "args": args or "", "returns": result or ""}
                for oid, name, kind, args, result in rows]

    def list_sequences(self, conn: Any, schema: Optional[str]) -> list:
        from sqlalchemy import text

        rows = conn.execute(text(
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = :schema AND c.relkind = 'S' ORDER BY 1"),
            {"schema": schema or "public"}).all()
        return [{"name": row[0]} for row in rows]

    def list_triggers(self, conn: Any, schema: Optional[str]) -> list:
        from sqlalchemy import text

        rows = conn.execute(text(
            "SELECT t.tgname, c.relname, t.tgtype FROM pg_trigger t "
            "JOIN pg_class c ON c.oid = t.tgrelid JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = :schema AND NOT t.tgisinternal ORDER BY c.relname, t.tgname"),
            {"schema": schema or "public"}).all()
        return [{"name": name, "table": table, **_trigger_type(tgtype)}
                for name, table, tgtype in rows]

    def routine_info(self, conn: Any, schema: Optional[str], ident: str) -> Optional[dict]:
        from sqlalchemy import text

        try:
            oid = int(ident)
        except (TypeError, ValueError):
            return None
        row = conn.execute(text(
            "SELECT p.proname, p.prokind, pg_get_function_identity_arguments(p.oid), "
            "pg_get_function_result(p.oid), l.lanname, p.provolatile, p.prosecdef, "
            "pg_get_userbyid(p.proowner), obj_description(p.oid, 'pg_proc'), "
            "pg_get_functiondef(p.oid), p.proretset FROM pg_proc p "
            "JOIN pg_namespace n ON n.oid = p.pronamespace "
            "JOIN pg_language l ON l.oid = p.prolang "
            "WHERE p.oid = :oid AND n.nspname = :schema AND p.prokind IN ('f', 'p')"),
            {"oid": oid, "schema": schema or "public"}).first()
        if row is None:
            return None
        name, kind, args, result, language, volatile, definer, owner, comment, source, retset = row
        params = conn.execute(text(
            # proargnames / proargmodes are 1-based and line up with
            # proallargtypes when it is set, else with proargtypes.
            "SELECT COALESCE(p.proargnames[a.n], ''), format_type(a.t, NULL), "
            "COALESCE(p.proargmodes[a.n]::text, 'i') FROM pg_proc p, "
            "unnest(COALESCE(p.proallargtypes, p.proargtypes::oid[])) WITH ORDINALITY a(t, n) "
            "WHERE p.oid = :oid ORDER BY a.n"), {"oid": oid}).all()
        modes = {"i": "IN", "o": "OUT", "b": "INOUT", "v": "VARIADIC", "t": "TABLE"}
        kind = "procedure" if kind == "p" else "function"
        return info(name, kind, [
            ("Kind", kind.capitalize()),
            ("Arguments", args or "(none)"),
            ("Returns", None if kind == "procedure" else result),
            ("Language", language),
            ("Volatility", None if kind == "procedure" else
             {"i": "IMMUTABLE", "s": "STABLE", "v": "VOLATILE"}.get(volatile)),
            ("Security", "DEFINER" if definer else "INVOKER"),
            ("Owner", owner),
            ("Comment", comment),
            ("Object id", oid),
        ], (source or "").rstrip() + ";", returns=result or "", sets=bool(retset)
            or (result or "").upper().startswith("TABLE"),
            params=[{"name": pname, "type": ptype, "mode": modes.get(pmode, "IN")}
                    for pname, ptype, pmode in params])

    def sequence_info(self, conn: Any, schema: Optional[str], name: str) -> Optional[dict]:
        from sqlalchemy import text

        target = schema or "public"
        row = conn.execute(text(
            "SELECT c.oid, format_type(s.seqtypid, NULL), s.seqstart, s.seqincrement, s.seqmin, "
            "s.seqmax, s.seqcache, s.seqcycle, pg_get_userbyid(c.relowner), "
            "obj_description(c.oid, 'pg_class') FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "JOIN pg_sequence s ON s.seqrelid = c.oid "
            "WHERE n.nspname = :schema AND c.relname = :name AND c.relkind = 'S'"),
            {"schema": target, "name": name}).first()
        if row is None:
            return None
        oid, type_name, start, step, low, high, cache, cycle, owner, comment = row
        try:
            # Null until first used, or without the privilege to read it.
            last = conn.execute(text(
                "SELECT last_value FROM pg_sequences WHERE schemaname = :schema "
                "AND sequencename = :name"), {"schema": target, "name": name}).scalar()
        except Exception:  # noqa: BLE001
            last = None
        owned = conn.execute(text(
            "SELECT d.refobjid::regclass::text, a.attname, d.deptype FROM pg_depend d "
            "JOIN pg_attribute a ON a.attrelid = d.refobjid AND a.attnum = d.refobjsubid "
            "WHERE d.objid = :oid AND d.classid = 'pg_class'::regclass "
            "AND d.refclassid = 'pg_class'::regclass AND d.deptype IN ('a', 'i')"),
            {"oid": oid}).first()
        prep = conn.dialect.identifier_preparer
        qualified = f"{prep.quote_schema(target)}.{prep.quote(name)}"
        ddl = (f"CREATE SEQUENCE {qualified}\n    AS {type_name}\n    INCREMENT BY {step}\n"
               f"    MINVALUE {low}\n    MAXVALUE {high}\n    START WITH {start}\n"
               f"    CACHE {cache}\n    {'CYCLE' if cycle else 'NO CYCLE'};")
        note = ""
        if owned and owned[2] == "i":
            note = (f"The sequence behind the identity column {owned[0]}.{owned[1]}; "
                    "it is created with the table.")
        elif owned:
            ddl += f"\n\nALTER SEQUENCE {qualified} OWNED BY {owned[0]}.{prep.quote(owned[1])};"
        return info(name, "sequence", [
            ("Type", type_name), ("Current value", last if last is not None else "(not used yet)"),
            ("Start", start), ("Increment", step), ("Minimum", low), ("Maximum", high),
            ("Cache", cache), ("Cycles", bool(cycle)),
            ("Owned by", f"{owned[0]}.{owned[1]}" if owned else None),
            ("Owner", owner), ("Comment", comment),
        ], ddl, native=False, note=note)

    def trigger_info(self, conn: Any, schema: Optional[str], name: str,
                     table: str) -> Optional[dict]:
        from sqlalchemy import text

        row = conn.execute(text(
            "SELECT t.tgtype, t.tgenabled, t.tgfoid::regprocedure::text, "
            "pg_get_triggerdef(t.oid, true), obj_description(t.oid, 'pg_trigger') "
            "FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = :schema AND c.relname = :table AND t.tgname = :name "
            "AND NOT t.tgisinternal"),
            {"schema": schema or "public", "table": table, "name": name}).first()
        if row is None:
            return None
        tgtype, enabled, function, ddl, comment = row
        kind = _trigger_type(tgtype)
        return info(name, "trigger", [
            ("Table", table), ("Timing", kind["timing"]), ("Events", kind["events"]),
            ("For each", kind["level"]),
            ("Enabled", {"O": "yes", "D": "no", "R": "replica only", "A": "always"}.get(enabled)),
            ("Function", function), ("Comment", comment),
        ], ddl + ";", table=table)

    def list_roles(self, conn: Any, show_system: bool) -> list:
        from sqlalchemy import text

        rows = conn.execute(text(
            "SELECT rolname, rolcanlogin, rolsuper FROM pg_roles ORDER BY rolname")).all()
        return [{"id": name, "name": name, "login": bool(login), "admin": bool(superuser)}
                for name, login, superuser in rows if show_system or not name.startswith("pg_")]

    def role_info(self, conn: Any, ident: str) -> Optional[dict]:
        from sqlalchemy import text

        row = conn.execute(text(
            "SELECT oid, rolcanlogin, rolsuper, rolinherit, rolcreaterole, rolcreatedb, "
            "rolreplication, rolbypassrls, rolconnlimit, rolvaliduntil, "
            "shobj_description(oid, 'pg_authid') FROM pg_roles WHERE rolname = :name"),
            {"name": ident}).first()
        if row is None:
            return None
        (oid, login, superuser, inherit, createrole, createdb, replication, bypass, limit,
         until, comment) = row
        member_of = [r[0] for r in conn.execute(text(
            "SELECT r.rolname FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.roleid "
            "WHERE m.member = :oid ORDER BY 1"), {"oid": oid}).all()]
        members = [r[0] for r in conn.execute(text(
            "SELECT r.rolname FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.member "
            "WHERE m.roleid = :oid ORDER BY 1"), {"oid": oid}).all()]
        prep = conn.dialect.identifier_preparer
        flags = [word if on else "NO" + word for word, on in (
            ("LOGIN", login), ("SUPERUSER", superuser), ("INHERIT", inherit),
            ("CREATEROLE", createrole), ("CREATEDB", createdb), ("REPLICATION", replication),
            ("BYPASSRLS", bypass))]
        ddl = f"CREATE ROLE {prep.quote(ident)} WITH\n    " + "\n    ".join(flags)
        if limit is not None and limit >= 0:
            ddl += f"\n    CONNECTION LIMIT {limit}"
        if until is not None:
            ddl += f"\n    VALID UNTIL '{until}'"
        ddl += ";"
        for role in member_of:
            ddl += f"\n\nGRANT {prep.quote(role)} TO {prep.quote(ident)};"
        return info(ident, "role", [
            ("Can log in", bool(login)), ("Superuser", bool(superuser)),
            ("Inherits privileges", bool(inherit)), ("Can create roles", bool(createrole)),
            ("Can create databases", bool(createdb)), ("Replication", bool(replication)),
            ("Bypasses row security", bool(bypass)),
            ("Connection limit", "none" if limit is None or limit < 0 else limit),
            ("Valid until", until), ("Comment", comment),
        ], ddl, native=False, lists=[{"title": "Member of", "items": member_of},
                                     {"title": "Members", "items": members}])


def _trigger_type(tgtype: int) -> dict:
    """pg_trigger.tgtype's bits, in words."""
    timing = "INSTEAD OF" if tgtype & 64 else ("BEFORE" if tgtype & 2 else "AFTER")
    events = [word for bit, word in ((4, "INSERT"), (16, "UPDATE"), (8, "DELETE"),
                                     (32, "TRUNCATE")) if tgtype & bit]
    return {"timing": timing, "events": " OR ".join(events),
            "level": "ROW" if tgtype & 1 else "STATEMENT"}
