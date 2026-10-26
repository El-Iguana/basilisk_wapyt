"""Microsoft SQL Server, through pymssql (FreeTDS)."""
from __future__ import annotations

import re
from typing import Any, Optional

from services.engines.base import Engine, EngineError, Field, Piece, _only_comments, info, kind_words

ENCRYPTION = (
    ("request", "request (TLS when offered)"),
    ("off", "off"),
    ("require", "require"),
)

# A batch separator: GO alone on its line, optionally with a repeat count
# (which is not supported here and refused).
_GO = re.compile(r"^[ \t]*GO(?:[ \t]+(\d+))?[ \t]*(?:--[^\n]*)?$", re.I | re.M)


class SQLServer(Engine):
    name = "mssql"
    label = "SQL Server"
    icon = "mdi-microsoft"
    default_port = 1433
    levels = ("database", "schema")
    default_database = "master"
    editor_dialect = "MSSQL"
    system_databases = frozenset({"master", "model", "msdb", "tempdb"})
    system_schemas = frozenset({"sys", "INFORMATION_SCHEMA", "guest"})
    system_schema_prefixes = ("db_",)
    # SQL Server has no read-only session: a read-only profile runs every
    # statement in a transaction that is rolled back.
    read_only_by_rollback = True
    extra_fields = (
        Field("encryption", "TLS", kind="select", default="request", options=ENCRYPTION),
    )

    def url(self, profile: dict, database: Optional[str]):
        from sqlalchemy import URL

        return URL.create(
            "mssql+pymssql",
            username=profile.get("username") or None,
            password=profile.get("password") or None,
            host=profile.get("host") or None,
            port=int(profile.get("port") or self.default_port),
            database=self.database_for(profile, database),
        )

    def engine_kwargs(self, profile: dict) -> dict:
        options = profile.get("options") or {}
        encryption = options.get("encryption") or "request"
        if encryption not in {value for value, _ in ENCRYPTION}:
            encryption = "request"
        return {"connect_args": {
            "login_timeout": 10, "appname": "Basilisk", "encryption": encryption,
            # datetime2 parameters, not datetime (which rounds to 3 ms).
            "use_datetime2": True,
        }}

    def set_timeout(self, dbapi_conn: Any, seconds: int) -> None:
        dbapi_conn._conn.query_timeout = max(0, int(seconds))

    def set_autocommit(self, dbapi_conn: Any, on: bool) -> None:
        if not on:
            dbapi_conn.rollback()
        dbapi_conn.autocommit(on)

    def transaction_state(self, dbapi_conn: Any, statements: int) -> dict:
        # pymssql opens a transaction itself after every commit and rollback,
        # so @@TRANCOUNT alone is always 1: "open" also needs something run.
        # The count still catches a COMMIT typed into the script.
        if not statements:
            return {"open": False, "failed": False}
        cur = dbapi_conn.cursor()
        try:
            cur.execute("SELECT @@TRANCOUNT")
            return {"open": int(cur.fetchone()[0]) > 0, "failed": False}
        finally:
            cur.close()

    def explain(self, dbapi_conn: Any, sql: str, analyze: bool) -> dict:
        from services.plans import parse_mssql

        option = "STATISTICS XML" if analyze else "SHOWPLAN_XML"
        cur = dbapi_conn.cursor()
        try:
            # Each SET must be a batch of its own; pymssql sends one per execute.
            cur.execute(f"SET {option} ON")
            try:
                cur.execute(sql)
                xml_text = None
                while True:
                    if cur.description and str(cur.description[0][0]).endswith("Showplan"):
                        xml_text = cur.fetchone()[0]
                    elif cur.description:
                        cur.fetchall()          # the statement's own rows, under ANALYZE
                    if not cur.nextset():
                        break
            finally:
                cur.execute(f"SET {option} OFF")
        finally:
            cur.close()
        if not xml_text:
            raise EngineError("SQL Server returned no plan for this statement")
        return {**parse_mssql(xml_text), "raw": xml_text, "format": "xml"}

    def session_id(self, dbapi_conn: Any) -> Any:
        cur = dbapi_conn.cursor()
        try:
            cur.execute("SELECT @@SPID")
            return int(cur.fetchone()[0])
        finally:
            cur.close()

    def cancel(self, engine: Any, dbapi_conn: Any, session: Any) -> None:
        # Not dbapi_conn._conn.cancel(): FreeTDS connections are not safe to
        # touch from another thread mid-query. KILL ends the whole session;
        # sqlops invalidates the connection afterwards.
        raw = engine.raw_connection()
        try:
            raw.dbapi_connection.autocommit(True)
            cur = raw.cursor()
            cur.execute(f"KILL {int(session)}")
            cur.close()
        finally:
            raw.invalidate()

    has_sessions = True
    can_cancel_query = False    # KILL ends the session; there is no "cancel the query" of another

    def list_sessions(self, conn: Any, include_idle: bool) -> list:
        from sqlalchemy import text

        rows = conn.execute(text(
            "SELECT s.session_id, s.login_name, DB_NAME(COALESCE(r.database_id, s.database_id)), "
            "  COALESCE(NULLIF(s.host_name, ''), c.client_net_address), s.program_name, "
            "  COALESCE(r.status, s.status), COALESCE(r.start_time, s.last_request_end_time), "
            "  DATEDIFF(SECOND, COALESCE(r.start_time, s.last_request_end_time), GETDATE()), "
            "  r.wait_type, NULLIF(r.blocking_session_id, 0), "
            "  CAST(t.text AS NVARCHAR(MAX)), CASE WHEN s.session_id = @@SPID THEN 1 ELSE 0 END, "
            "  s.open_transaction_count "
            "FROM sys.dm_exec_sessions s "
            "LEFT JOIN sys.dm_exec_requests r ON r.session_id = s.session_id "
            "OUTER APPLY (SELECT TOP 1 x.client_net_address, x.most_recent_sql_handle "
            "  FROM sys.dm_exec_connections x WHERE x.session_id = s.session_id) c "
            "OUTER APPLY sys.dm_exec_sql_text(COALESCE(r.sql_handle, c.most_recent_sql_handle)) t "
            "WHERE s.is_user_process = 1 "
            + ("" if include_idle else
               "AND (r.session_id IS NOT NULL OR s.open_transaction_count > 0) ")
            + "ORDER BY CASE WHEN r.session_id IS NULL THEN 1 ELSE 0 END, r.start_time")).all()
        return [{
            "id": row[0], "user": row[1], "database": row[2], "client": row[3],
            "application": row[4], "state": row[5], "started": row[6], "seconds": row[7],
            "wait": row[8], "blocked_by": [row[9]] if row[9] else [], "query": row[10],
            "own": bool(row[11]), "txn": bool(row[12]),
        } for row in rows]

    def kill_session(self, engine: Any, conn: Any, session_id: int, mode: str) -> str:
        # KILL refuses to run inside a transaction, and pymssql always has one
        # open: a raw connection in auto-commit, thrown away after.
        sql = f"KILL {int(session_id)}"
        raw = engine.raw_connection()
        try:
            raw.dbapi_connection.autocommit(True)
            cur = raw.cursor()
            cur.execute(sql)
            cur.close()
        finally:
            raw.invalidate()
        return sql

    def import_wrappers(self, conn: Any, prep: Any, schema: Optional[str], table: str,
                        columns: list) -> tuple:
        from sqlalchemy import text

        name = self.qualified(prep, schema or "dbo", table)
        numbered = conn.execute(text(
            "SELECT name FROM sys.identity_columns WHERE object_id = OBJECT_ID(:table)"),
            {"table": name}).scalar()
        if numbered is None or numbered not in columns:
            return [], []
        return [f"SET IDENTITY_INSERT {name} ON"], [f"SET IDENTITY_INSERT {name} OFF"]

    def list_databases(self, conn: Any) -> list:
        from sqlalchemy import text

        rows = conn.execute(text(
            "SELECT name FROM sys.databases WHERE HAS_DBACCESS(name) = 1 AND state = 0 "
            "ORDER BY name")).all()
        return [row[0] for row in rows]

    def row_estimates(self, conn: Any, schema: Optional[str]) -> dict:
        from sqlalchemy import text

        rows = conn.execute(text(
            "SELECT t.name, SUM(p.rows) FROM sys.tables t "
            "JOIN sys.schemas s ON s.schema_id = t.schema_id "
            "JOIN sys.partitions p ON p.object_id = t.object_id AND p.index_id IN (0, 1) "
            "WHERE s.name = :schema GROUP BY t.name"), {"schema": schema or "dbo"}).all()
        return {name: int(count) for name, count in rows if count is not None}

    def split(self, sql: str) -> list:
        """T-SQL runs in batches separated by ``GO``; each batch is sent whole."""
        pieces: list = []
        cursor = 0
        bounds = [(m.start(), m.end(), m.group(1)) for m in _GO.finditer(sql)]
        for start, end, repeat in bounds + [(len(sql), len(sql), None)]:
            if repeat:
                raise ValueError("GO with a repeat count is not supported")
            chunk = sql[cursor:start]
            if not _only_comments(chunk):
                lead = len(chunk) - len(chunk.lstrip())
                body = chunk.strip()
                pieces.append(Piece(cursor + lead, cursor + lead + len(body), body))
            cursor = end
        return pieces

    # ------------------------------------------------------------------
    # More navigator objects
    # ------------------------------------------------------------------
    object_groups = ("functions", "procedures", "sequences", "triggers")
    has_roles = True
    roles_label = "Logins & roles"
    identity_clause = "IDENTITY(1,1)"
    add_column_keyword = "ADD"
    common_types = ("int", "bigint", "smallint", "decimal(12,2)", "float", "bit",
                    "nvarchar(255)", "nvarchar(max)", "date", "time", "datetime2",
                    "uniqueidentifier", "varbinary(max)")

    # sp_rename takes the old name as a (bracketed) string and the new one bare.
    def sql_rename_table(self, prep: Any, schema: Optional[str], old: str, new: str) -> str:
        return (f"EXEC sp_rename {_nstring(self.qualified(prep, schema, old))}, "
                f"{_nstring(new)}")

    def sql_rename_column(self, prep: Any, schema: Optional[str], table: str, old: str,
                          new: str) -> str:
        path = f"{self.qualified(prep, schema, table)}.{prep.quote(old)}"
        return f"EXEC sp_rename {_nstring(path)}, {_nstring(new)}, N'COLUMN'"

    def sql_drop_index(self, prep: Any, schema: Optional[str], table: str, index: str) -> str:
        return f"DROP INDEX {prep.quote(index)} ON {self.qualified(prep, schema, table)}"

    script_batches = True
    replace_view = "CREATE OR ALTER VIEW"

    bool_literals = ("1", "0")

    def sql_text_literal(self, text: str) -> str:
        return _nstring(text)

    def sql_bytes_literal(self, raw: bytes) -> str:
        return "0x" + raw.hex()

    def sql_datetime_literal(self, value: Any, type_text: str) -> str:
        # DATETIME keeps milliseconds and refuses more digits; DATETIME2 keeps seven.
        precise = any(word in (type_text or "").upper() for word in ("DATETIME2", "DATETIMEOFFSET"))
        text = value.isoformat(sep="T") if precise else value.isoformat(sep="T", timespec="milliseconds")
        return "'" + text + "'"

    def _drop_default_sql(self, prep: Any, schema: Optional[str], table: str, column: str) -> str:
        """A batch that drops the column's default constraint, whatever it is called."""
        target = self.qualified(prep, schema, table)
        return ("DECLARE @df nvarchar(300) = (SELECT QUOTENAME(d.name) FROM sys.default_constraints d "
                "JOIN sys.columns c ON c.object_id = d.parent_object_id "
                "AND c.column_id = d.parent_column_id "
                f"WHERE d.parent_object_id = OBJECT_ID({_nstring(target)}) "
                f"AND c.name = {_nstring(column)});\n"
                f"IF @df IS NOT NULL EXEC (N'ALTER TABLE ' + {_nstring(target)} "
                "+ N' DROP CONSTRAINT ' + @df)")

    def sql_alter_column(self, prep: Any, schema: Optional[str], table: str, column: str,
                         new: dict, old: dict, changed: set) -> list:
        target = self.qualified(prep, schema, table)
        col = prep.quote(column)
        out = []
        # A default constraint blocks ALTER COLUMN, so a type change drops and re-adds it.
        drop_default = bool(old.get("default")) and bool(changed & {"default", "type"})
        if drop_default:
            out.append(self._drop_default_sql(prep, schema, table, column))
        if changed & {"type", "nullable"}:
            out.append(f"ALTER TABLE {target} ALTER COLUMN {col} {new['type']} "
                       + ("NULL" if new["nullable"] else "NOT NULL"))
        if new.get("default") and ("default" in changed or drop_default):
            out.append(f"ALTER TABLE {target} ADD DEFAULT {new['default']} FOR {col}")
        return out

    def sql_script_before_drop_column(self, prep: Any, schema: Optional[str], table: str,
                                      column: str) -> list:
        return [self._drop_default_sql(prep, schema, table, column)]

    def before_drop_column(self, conn: Any, prep: Any, schema: Optional[str], table: str,
                           column: str) -> list:
        from sqlalchemy import text

        rows = conn.execute(text(
            "SELECT d.name FROM sys.default_constraints d "
            "JOIN sys.columns c ON c.object_id = d.parent_object_id "
            "AND c.column_id = d.parent_column_id "
            "WHERE d.parent_object_id = OBJECT_ID(:table) AND c.name = :column"),
            {"table": self.qualified(prep, schema, table), "column": column}).all()
        return [f"ALTER TABLE {self.qualified(prep, schema, table)} "
                f"DROP CONSTRAINT {prep.quote(row[0])}" for row in rows]
    _ROUTINE_TYPES = {"P": "procedure", "PC": "procedure", "FN": "function", "IF": "function",
                      "TF": "function", "FS": "function", "FT": "function"}

    def _params(self, conn: Any, schema: Optional[str], object_id: Optional[int] = None) -> dict:
        from sqlalchemy import text

        sql = ("SELECT p.object_id, p.parameter_id, p.name, TYPE_NAME(p.user_type_id), "
               "p.max_length, p.precision, p.scale, p.is_output FROM sys.parameters p "
               "JOIN sys.objects o ON o.object_id = p.object_id "
               "JOIN sys.schemas s ON s.schema_id = o.schema_id WHERE s.name = :schema")
        binds: dict = {"schema": schema or "dbo"}
        if object_id is not None:
            sql += " AND p.object_id = :oid"
            binds["oid"] = object_id
        found: dict = {}
        for oid, pos, name, type_name, length, precision, scale, output in conn.execute(
                text(sql + " ORDER BY p.object_id, p.parameter_id"), binds).all():
            entry = found.setdefault(oid, {"params": [], "returns": ""})
            shown = _type_text(type_name, length, precision, scale)
            if pos == 0:
                entry["returns"] = shown
            else:
                entry["params"].append({"name": name or "", "type": shown,
                                        "mode": "OUTPUT" if output else "IN"})
        return found

    def list_routines(self, conn: Any, schema: Optional[str]) -> list:
        from sqlalchemy import text

        rows = conn.execute(text(
            "SELECT o.object_id, o.name, RTRIM(o.type) FROM sys.objects o "
            "JOIN sys.schemas s ON s.schema_id = o.schema_id WHERE s.name = :schema "
            "AND o.type IN ('P', 'PC', 'FN', 'IF', 'TF', 'FS', 'FT') AND o.is_ms_shipped = 0 "
            "ORDER BY o.name"), {"schema": schema or "dbo"}).all()
        params = self._params(conn, schema)
        out = []
        for oid, name, kind in rows:
            entry = params.get(oid, {"params": [], "returns": ""})
            out.append({"id": str(oid), "name": name, "kind": self._ROUTINE_TYPES[kind],
                        "args": ", ".join(_param_text(p) for p in entry["params"]),
                        "returns": "TABLE" if kind in ("IF", "TF", "FT") else entry["returns"]})
        return out

    def routine_info(self, conn: Any, schema: Optional[str], ident: str) -> Optional[dict]:
        from sqlalchemy import text

        try:
            oid = int(ident)
        except (TypeError, ValueError):
            return None
        row = conn.execute(text(
            "SELECT o.name, RTRIM(o.type), o.type_desc, o.create_date, o.modify_date, "
            "OBJECT_DEFINITION(o.object_id), m.is_schema_bound, "
            "USER_NAME(m.execute_as_principal_id), m.execute_as_principal_id "
            "FROM sys.objects o JOIN sys.schemas s ON s.schema_id = o.schema_id "
            "LEFT JOIN sys.sql_modules m ON m.object_id = o.object_id "
            "WHERE o.object_id = :oid AND s.name = :schema"),
            {"oid": oid, "schema": schema or "dbo"}).first()
        if row is None or row[1] not in self._ROUTINE_TYPES:
            return None
        name, code, type_desc, created, modified, source, bound, run_as, run_as_id = row
        entry = self._params(conn, schema, oid).get(oid, {"params": [], "returns": ""})
        kind = self._ROUTINE_TYPES[code]
        sets = code in ("IF", "TF", "FT")
        returns = "TABLE" if sets else entry["returns"]
        note = "" if source else (
            "The source is hidden: the routine is encrypted, CLR, or you lack VIEW DEFINITION.")
        return info(name, kind, [
            ("Kind", kind_words(type_desc)),
            ("Arguments", ", ".join(_param_text(p) for p in entry["params"]) or "(none)"),
            ("Returns", returns if kind == "function" else None),
            ("Schema bound", bool(bound) if bound is not None else None),
            ("Runs as", "OWNER" if run_as_id == -2 else (run_as or "CALLER")),
            ("Created", created), ("Modified", modified), ("Object id", oid),
        ], (source or "").strip(), note=note, returns=returns, sets=sets, params=entry["params"])

    def list_sequences(self, conn: Any, schema: Optional[str]) -> list:
        from sqlalchemy import text

        rows = conn.execute(text(
            "SELECT q.name FROM sys.sequences q JOIN sys.schemas s ON s.schema_id = q.schema_id "
            "WHERE s.name = :schema ORDER BY q.name"), {"schema": schema or "dbo"}).all()
        return [{"name": row[0]} for row in rows]

    def sequence_info(self, conn: Any, schema: Optional[str], name: str) -> Optional[dict]:
        from sqlalchemy import text

        target = schema or "dbo"
        # The values are sql_variant, which the driver cannot read: cast them.
        row = conn.execute(text(
            "SELECT TYPE_NAME(q.user_type_id), q.precision, q.scale, "
            "CAST(q.start_value AS NVARCHAR(50)), CAST(q.increment AS NVARCHAR(50)), "
            "CAST(q.minimum_value AS NVARCHAR(50)), CAST(q.maximum_value AS NVARCHAR(50)), "
            "q.is_cycling, q.is_cached, q.cache_size, CAST(q.current_value AS NVARCHAR(50)), "
            "q.is_exhausted, q.create_date FROM sys.sequences q "
            "JOIN sys.schemas s ON s.schema_id = q.schema_id "
            "WHERE s.name = :schema AND q.name = :name"), {"schema": target, "name": name}).first()
        if row is None:
            return None
        (type_name, precision, scale, start, step, low, high, cycle, cached, cache, current,
         exhausted, created) = row
        shown_type = _type_text(type_name, 0, precision, scale)
        prep = conn.dialect.identifier_preparer
        cache_text = (f"CACHE {cache}" if cache else "CACHE") if cached else "NO CACHE"
        ddl = (f"CREATE SEQUENCE {prep.quote_schema(target)}.{prep.quote(name)}\n"
               f"    AS {shown_type}\n    START WITH {start}\n    INCREMENT BY {step}\n"
               f"    MINVALUE {low}\n    MAXVALUE {high}\n"
               f"    {'CYCLE' if cycle else 'NO CYCLE'}\n    {cache_text};")
        return info(name, "sequence", [
            ("Type", shown_type), ("Current value", current), ("Start", start),
            ("Increment", step), ("Minimum", low), ("Maximum", high), ("Cycles", bool(cycle)),
            ("Cache", (cache or "default") if cached else "no cache"),
            ("Exhausted", bool(exhausted)), ("Created", created),
        ], ddl, native=False)

    def _trigger_events(self, conn: Any, ids: list) -> dict:
        from sqlalchemy import bindparam, text

        if not ids:
            return {}
        events: dict = {}
        for oid, event in conn.execute(text(
                "SELECT object_id, type_desc FROM sys.trigger_events WHERE object_id IN :ids "
                "ORDER BY object_id, type").bindparams(bindparam("ids", expanding=True)),
                {"ids": ids}).all():
            events.setdefault(oid, []).append(event)
        return {oid: " OR ".join(found) for oid, found in events.items()}

    def list_triggers(self, conn: Any, schema: Optional[str]) -> list:
        from sqlalchemy import text

        rows = conn.execute(text(
            "SELECT t.object_id, t.name, tbl.name, t.is_instead_of_trigger FROM sys.triggers t "
            "JOIN sys.objects tbl ON tbl.object_id = t.parent_id "
            "JOIN sys.schemas s ON s.schema_id = tbl.schema_id "
            "WHERE t.parent_class = 1 AND s.name = :schema ORDER BY tbl.name, t.name"),
            {"schema": schema or "dbo"}).all()
        events = self._trigger_events(conn, [row[0] for row in rows])
        return [{"name": name, "table": table, "timing": "INSTEAD OF" if instead else "AFTER",
                 "events": events.get(oid, "")} for oid, name, table, instead in rows]

    def trigger_info(self, conn: Any, schema: Optional[str], name: str,
                     table: str) -> Optional[dict]:
        from sqlalchemy import text

        row = conn.execute(text(
            "SELECT t.object_id, tbl.name, t.is_instead_of_trigger, t.is_disabled, "
            "OBJECT_DEFINITION(t.object_id), t.create_date, t.modify_date FROM sys.triggers t "
            "JOIN sys.objects tbl ON tbl.object_id = t.parent_id "
            "JOIN sys.schemas s ON s.schema_id = tbl.schema_id "
            "WHERE t.parent_class = 1 AND s.name = :schema AND t.name = :name"),
            {"schema": schema or "dbo", "name": name}).first()
        if row is None:
            return None
        oid, table, instead, disabled, source, created, modified = row
        return info(name, "trigger", [
            ("Table", table), ("Timing", "INSTEAD OF" if instead else "AFTER"),
            ("Events", self._trigger_events(conn, [oid]).get(oid, "")),
            ("For each", "STATEMENT"), ("Enabled", not disabled),
            ("Created", created), ("Modified", modified),
        ], (source or "").strip(), table=table,
            note="" if source else "The source is hidden (encrypted, or no VIEW DEFINITION).")

    def list_roles(self, conn: Any, show_system: bool) -> list:
        from sqlalchemy import text

        rows = conn.execute(text(
            "SELECT name, type, is_disabled, is_fixed_role, "
            "IS_SRVROLEMEMBER('sysadmin', name) FROM sys.server_principals "
            "WHERE type IN ('S', 'U', 'G', 'R', 'E', 'X') AND name NOT LIKE '##%' "
            "ORDER BY name")).all()
        return [{"id": name, "name": name, "login": kind != "R" and not disabled,
                 "admin": admin == 1, "role": kind == "R"}
                for name, kind, disabled, fixed, admin in rows
                if show_system or not (fixed or name.upper().startswith("NT "))]

    def role_info(self, conn: Any, ident: str) -> Optional[dict]:
        from sqlalchemy import text

        row = conn.execute(text(
            "SELECT p.principal_id, p.type, p.type_desc, p.is_disabled, p.create_date, "
            "p.modify_date, p.default_database_name, p.default_language_name, "
            "l.is_policy_checked, l.is_expiration_checked, p.is_fixed_role "
            "FROM sys.server_principals p LEFT JOIN sys.sql_logins l "
            "ON l.principal_id = p.principal_id WHERE p.name = :name"), {"name": ident}).first()
        if row is None:
            return None
        (pid, kind, type_desc, disabled, created, modified, database, language, policy,
         expiry, fixed) = row
        member_of = [r[0] for r in conn.execute(text(
            "SELECT r.name FROM sys.server_role_members m JOIN sys.server_principals r "
            "ON r.principal_id = m.role_principal_id WHERE m.member_principal_id = :id "
            "ORDER BY 1"), {"id": pid}).all()]
        members = [r[0] for r in conn.execute(text(
            "SELECT r.name FROM sys.server_role_members m JOIN sys.server_principals r "
            "ON r.principal_id = m.member_principal_id WHERE m.role_principal_id = :id "
            "ORDER BY 1"), {"id": pid}).all()]
        prep = conn.dialect.identifier_preparer
        quoted = prep.quote_identifier(ident)
        if kind == "R":
            ddl = "-- A fixed server role" if fixed else f"CREATE SERVER ROLE {quoted};"
        elif kind == "S":
            ddl = (f"CREATE LOGIN {quoted} WITH PASSWORD = N'…' /* not shown */\n"
                   f"    , DEFAULT_DATABASE = {prep.quote_identifier(database or 'master')}\n"
                   f"    , CHECK_POLICY = {'ON' if policy else 'OFF'}\n"
                   f"    , CHECK_EXPIRATION = {'ON' if expiry else 'OFF'};")
        else:
            ddl = f"CREATE LOGIN {quoted} FROM {'EXTERNAL PROVIDER' if kind in 'EX' else 'WINDOWS'};"
        if disabled and kind != "R":
            ddl += f"\n\nALTER LOGIN {quoted} DISABLE;"
        for role in member_of:
            ddl += f"\n\nALTER SERVER ROLE {prep.quote_identifier(role)} ADD MEMBER {quoted};"
        return info(ident, "role", [
            ("Kind", kind_words(type_desc)),
            ("Can log in", None if kind == "R" else not disabled),
            ("Default database", database), ("Default language", language),
            ("Password policy", None if policy is None else bool(policy)),
            ("Password expiry", None if expiry is None else bool(expiry)),
            ("Created", created), ("Modified", modified),
        ], ddl, native=False, lists=[{"title": "Member of", "items": member_of},
                                     {"title": "Members", "items": members}])


def _nstring(text: str) -> str:
    return "N'" + text.replace("'", "''") + "'"


def _type_text(type_name: str, length: int, precision: int, scale: int) -> str:
    name = (type_name or "").lower()
    if name in ("varchar", "char", "varbinary", "binary", "nvarchar", "nchar"):
        if length == -1:
            return f"{name}(max)"
        if length:
            return f"{name}({length // 2 if name.startswith('n') else length})"
    if name in ("decimal", "numeric"):
        return f"{name}({precision}, {scale})"
    return name


def _param_text(param: dict) -> str:
    out = " OUTPUT" if param["mode"] == "OUTPUT" else ""
    return f"{param['name']} {param['type']}{out}".strip()
