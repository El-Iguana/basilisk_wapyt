"""MySQL and MariaDB, through PyMySQL. A database is a schema here."""
from __future__ import annotations

import re
from typing import Any, Optional

from services.engines.base import Engine, Field, info

TLS_MODES = (
    ("off", "off"),
    ("require", "require (no certificate check)"),
)


class MySQL(Engine):
    name = "mysql"
    label = "MySQL / MariaDB"
    icon = "mdi-dolphin"
    default_port = 3306
    levels = ("database",)
    editor_dialect = "MySQL"
    system_databases = frozenset({"information_schema", "mysql", "performance_schema", "sys"})
    extra_fields = (
        Field("tls", "TLS", kind="select", default="off", options=TLS_MODES),
    )

    def url(self, profile: dict, database: Optional[str]):
        from sqlalchemy import URL

        # The URL names no database: every database is reachable from one
        # connection, and the tree passes the database as the schema.
        return URL.create(
            "mysql+pymysql",
            username=profile.get("username") or None,
            password=profile.get("password") or None,
            host=profile.get("host") or None,
            port=int(profile.get("port") or self.default_port),
            database=profile.get("database") or None,
            query={"charset": "utf8mb4"},
        )

    def database_for(self, profile: dict, database: Optional[str]) -> Optional[str]:
        # One engine per profile: USE is cheap and the URL is the same.
        return None

    def engine_kwargs(self, profile: dict) -> dict:
        options = profile.get("options") or {}
        args: dict = {"connect_timeout": 10}
        if options.get("tls") == "require":
            # PyMySQL: no CA given = TLS without verification.
            args["ssl"] = {"check_hostname": False}
        return {"connect_args": args}

    def on_connect(self, dbapi_conn: Any, profile: dict) -> None:
        if profile.get("read_only"):
            with dbapi_conn.cursor() as cur:
                cur.execute("SET SESSION TRANSACTION READ ONLY")

    def set_timeout(self, dbapi_conn: Any, seconds: int) -> None:
        ms = max(0, int(seconds)) * 1000
        with dbapi_conn.cursor() as cur:
            try:
                cur.execute(f"SET SESSION max_execution_time = {ms}")  # MySQL: SELECTs
            except Exception:  # noqa: BLE001 - MariaDB names it differently
                cur.execute(f"SET SESSION max_statement_time = {ms / 1000:.3f}")

    def set_autocommit(self, dbapi_conn: Any, on: bool) -> None:
        if not on:
            dbapi_conn.rollback()
        dbapi_conn.autocommit(on)

    def use_database(self, dbapi_conn: Any, database: Optional[str]) -> None:
        if database:
            dbapi_conn.select_db(database)

    def explain(self, dbapi_conn: Any, sql: str, analyze: bool) -> dict:
        from services.plans import parse_mysql_table, parse_mysql_tree

        with dbapi_conn.cursor() as cur:
            try:
                # MySQL 8.0.18+: a tree, with real numbers under ANALYZE.
                cur.execute(("EXPLAIN ANALYZE " if analyze else "EXPLAIN FORMAT=TREE ") + sql)
                text = cur.fetchone()[0]
                return {**parse_mysql_tree(text), "raw": text, "format": "text"}
            except Exception as exc:  # noqa: BLE001 - MariaDB and old MySQL: the table form
                if "syntax" not in str(exc).lower() and "1064" not in str(exc):
                    raise
            cur.execute(("ANALYZE " if analyze else "EXPLAIN ") + sql)
            columns = [desc[0] for desc in cur.description]
            rows = cur.fetchall()
        raw = "\t".join(columns) + "\n" + "\n".join("\t".join("" if v is None else str(v)
                                                              for v in row) for row in rows)
        return {**parse_mysql_table(columns, rows), "raw": raw, "format": "text"}

    def session_id(self, dbapi_conn: Any) -> Any:
        return dbapi_conn.thread_id()

    def cancel(self, engine: Any, dbapi_conn: Any, session: Any) -> None:
        raw = engine.raw_connection()
        try:
            with raw.cursor() as cur:
                cur.execute(f"KILL QUERY {int(session)}")
        finally:
            raw.close()

    has_sessions = True

    def list_sessions(self, conn: Any, include_idle: bool) -> list:
        from sqlalchemy import text

        own = conn.execute(text("SELECT CONNECTION_ID()")).scalar()
        found = []
        for row in conn.execute(text("SHOW FULL PROCESSLIST")).all():
            command = str(row[4] or "")
            if not include_idle and command in ("Sleep", "Daemon", "Binlog Dump"):
                continue
            found.append({
                "id": row[0], "user": row[1], "database": row[3], "client": row[2],
                "application": "", "state": command + (f": {row[6]}" if row[6] else ""),
                "started": None, "seconds": row[5], "wait": "", "blocked_by": [],
                "query": row[7], "own": row[0] == own, "txn": False,
            })
        found.sort(key=lambda item: (not str(item["state"]).startswith("Query"),
                                     -(item["seconds"] or 0)))
        return found

    def kill_session(self, engine: Any, conn: Any, session_id: int, mode: str) -> str:
        from sqlalchemy import text

        sql = f"KILL {'QUERY ' if mode == 'cancel' else ''}{int(session_id)}"
        conn.execute(text(sql))
        return sql

    def list_databases(self, conn: Any) -> list:
        from sqlalchemy import text

        return sorted(row[0] for row in conn.execute(text("SHOW DATABASES")).all())

    def schema_for(self, database: Optional[str], schema: Optional[str]) -> Optional[str]:
        return database or schema or None

    def row_estimates(self, conn: Any, schema: Optional[str]) -> dict:
        from sqlalchemy import text

        rows = conn.execute(text(
            "SELECT table_name, table_rows FROM information_schema.tables "
            "WHERE table_schema = :schema AND table_type = 'BASE TABLE'"),
            {"schema": schema}).all()
        return {name: int(count) for name, count in rows if count is not None}

    def native_ddl(self, conn: Any, schema: Optional[str], name: str, kind: str) -> Optional[str]:
        from sqlalchemy import text

        prep = conn.dialect.identifier_preparer
        target = (prep.quote_identifier(schema) + "." if schema else "") + prep.quote_identifier(name)
        what = "VIEW" if kind == "view" else "TABLE"
        row = conn.execute(text(f"SHOW CREATE {what} {target}")).first()
        return (row[1] + ";") if row else None

    # ------------------------------------------------------------------
    # More navigator objects (MariaDB sequences are not listed yet)
    # ------------------------------------------------------------------
    object_groups = ("functions", "procedures", "triggers")
    has_roles = True
    identity_clause = "AUTO_INCREMENT"
    ddl_transactional = False
    common_types = ("int", "bigint", "smallint", "decimal(12,2)", "double", "tinyint(1)",
                    "varchar(255)", "text", "date", "time", "datetime", "timestamp", "json",
                    "blob")

    def sql_rename_table(self, prep: Any, schema: Optional[str], old: str, new: str) -> str:
        return (f"RENAME TABLE {self.qualified(prep, schema, old)} "
                f"TO {self.qualified(prep, schema, new)}")

    def sql_drop_index(self, prep: Any, schema: Optional[str], table: str, index: str) -> str:
        return f"DROP INDEX {prep.quote(index)} ON {self.qualified(prep, schema, table)}"

    def sql_alter_column(self, prep: Any, schema: Optional[str], table: str, column: str,
                         new: dict, old: dict, changed: set) -> list:
        # MODIFY restates the whole column, so every part comes from ``new``.
        parts = [prep.quote(column), new["type"], "NULL" if new["nullable"] else "NOT NULL"]
        if new.get("default") and not new.get("auto"):
            parts.append(f"DEFAULT {new['default']}")
        if new.get("auto"):
            parts.append("AUTO_INCREMENT")
        return [f"ALTER TABLE {self.qualified(prep, schema, table)} MODIFY COLUMN {' '.join(parts)}"]

    def sql_drop_constraint(self, prep: Any, schema: Optional[str], table: str,
                            name: str, kind: str) -> str:
        target = self.qualified(prep, schema, table)
        if kind == "fk":
            return f"ALTER TABLE {target} DROP FOREIGN KEY {prep.quote(name)}"
        if kind == "pk":
            return f"ALTER TABLE {target} DROP PRIMARY KEY"
        return f"ALTER TABLE {target} DROP INDEX {prep.quote(name)}"
    _SYSTEM_ACCOUNTS = frozenset({"mysql.sys", "mysql.session", "mysql.infoschema",
                                  "mariadb.sys"})

    def list_routines(self, conn: Any, schema: Optional[str]) -> list:
        from sqlalchemy import text

        rows = conn.execute(text(
            "SELECT ROUTINE_NAME, ROUTINE_TYPE, DTD_IDENTIFIER FROM information_schema.ROUTINES "
            "WHERE ROUTINE_SCHEMA = :schema ORDER BY ROUTINE_NAME"), {"schema": schema}).all()
        params = self._params(conn, schema)
        return [{"id": f"{kind}:{name}", "name": name, "kind": kind.lower(),
                 "args": ", ".join(_param_text(p) for p in params.get((kind, name), [])),
                 "returns": returns or ""} for name, kind, returns in rows]

    def _params(self, conn: Any, schema: Optional[str], name: Optional[str] = None) -> dict:
        from sqlalchemy import text

        sql = ("SELECT ROUTINE_TYPE, SPECIFIC_NAME, PARAMETER_MODE, PARAMETER_NAME, "
               "DTD_IDENTIFIER FROM information_schema.PARAMETERS "
               "WHERE SPECIFIC_SCHEMA = :schema AND ORDINAL_POSITION > 0")
        if name is not None:
            sql += " AND SPECIFIC_NAME = :name"
        sql += " ORDER BY SPECIFIC_NAME, ORDINAL_POSITION"
        binds = {"schema": schema} if name is None else {"schema": schema, "name": name}
        found: dict = {}
        for kind, routine, mode, pname, ptype in conn.execute(text(sql), binds).all():
            found.setdefault((kind, routine), []).append(
                {"name": pname or "", "type": ptype or "", "mode": mode or "IN"})
        return found

    def list_triggers(self, conn: Any, schema: Optional[str]) -> list:
        from sqlalchemy import text

        rows = conn.execute(text(
            "SELECT TRIGGER_NAME, EVENT_OBJECT_TABLE, ACTION_TIMING, EVENT_MANIPULATION "
            "FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA = :schema "
            "ORDER BY EVENT_OBJECT_TABLE, TRIGGER_NAME"), {"schema": schema}).all()
        return [{"name": name, "table": table, "timing": timing, "events": event}
                for name, table, timing, event in rows]

    def routine_info(self, conn: Any, schema: Optional[str], ident: str) -> Optional[dict]:
        from sqlalchemy import text

        kind, _, name = (ident or "").partition(":")
        if kind not in ("FUNCTION", "PROCEDURE") or not name:
            return None
        row = conn.execute(text(
            "SELECT DTD_IDENTIFIER, ROUTINE_BODY, SQL_DATA_ACCESS, IS_DETERMINISTIC, "
            "SECURITY_TYPE, DEFINER, CREATED, LAST_ALTERED, ROUTINE_COMMENT, ROUTINE_DEFINITION "
            "FROM information_schema.ROUTINES WHERE ROUTINE_SCHEMA = :schema "
            "AND ROUTINE_NAME = :name AND ROUTINE_TYPE = :kind"),
            {"schema": schema, "name": name, "kind": kind}).first()
        if row is None:
            return None
        returns, body, access, deterministic, security, definer, created, altered, comment, \
            source = row
        params = self._params(conn, schema, name).get((kind, name), [])
        prep = conn.dialect.identifier_preparer
        qualified = f"{prep.quote_identifier(schema)}.{prep.quote_identifier(name)}"
        ddl, native = None, True
        try:
            shown = conn.execute(text(f"SHOW CREATE {kind} {qualified}")).first()
            ddl = shown[2] if shown else None
        except Exception:  # noqa: BLE001 - needs SHOW_ROUTINE or being the definer
            ddl = None
        if not ddl:
            native = False
            args = ", ".join(_param_text(p) for p in params)
            head = f"CREATE {kind} {qualified}({args})"
            if kind == "FUNCTION":
                head += f" RETURNS {returns}"
            ddl = f"{head}\n{source or '-- The body is hidden: it needs the SHOW_ROUTINE privilege'}"
        return info(name, kind.lower(), [
            ("Kind", kind.capitalize()),
            ("Arguments", ", ".join(_param_text(p) for p in params) or "(none)"),
            ("Returns", returns if kind == "FUNCTION" else None),
            ("Language", body), ("Data access", access),
            ("Deterministic", deterministic == "YES"), ("Security", security),
            ("Definer", definer), ("Created", created), ("Last altered", altered),
            ("Comment", comment),
        ], ddl.rstrip().rstrip(";") + ";", native=native, returns=returns or "", sets=False,
            params=params)

    def trigger_info(self, conn: Any, schema: Optional[str], name: str,
                     table: str) -> Optional[dict]:
        from sqlalchemy import text

        row = conn.execute(text(
            "SELECT EVENT_OBJECT_TABLE, ACTION_TIMING, EVENT_MANIPULATION, ACTION_ORDER, "
            "ACTION_STATEMENT, DEFINER, CREATED FROM information_schema.TRIGGERS "
            "WHERE TRIGGER_SCHEMA = :schema AND TRIGGER_NAME = :name"),
            {"schema": schema, "name": name}).first()
        if row is None:
            return None
        table, timing, event, order, statement, definer, created = row
        prep = conn.dialect.identifier_preparer
        native = True
        try:
            shown = conn.execute(text(
                f"SHOW CREATE TRIGGER {prep.quote_identifier(schema)}."
                f"{prep.quote_identifier(name)}")).first()
            ddl = shown[2] if shown else None
        except Exception:  # noqa: BLE001
            ddl = None
        if not ddl:
            native = False
            ddl = (f"CREATE TRIGGER {prep.quote_identifier(name)} {timing} {event} "
                   f"ON {prep.quote_identifier(table)}\nFOR EACH ROW {statement}")
        return info(name, "trigger", [
            ("Table", table), ("Timing", timing), ("Events", event), ("For each", "ROW"),
            ("Order", order), ("Definer", definer), ("Created", created),
        ], ddl.rstrip().rstrip(";") + ";", native=native, table=table)

    def list_roles(self, conn: Any, show_system: bool) -> list:
        from sqlalchemy import text

        rows = conn.execute(text(
            "SELECT User, Host, account_locked, Super_priv FROM mysql.user "
            "ORDER BY User, Host")).all()
        return [{"id": f"{user}@{host}", "name": f"{user}@{host}", "login": locked != "Y",
                 "admin": superuser == "Y"} for user, host, locked, superuser in rows
                if show_system or user not in self._SYSTEM_ACCOUNTS]

    def role_info(self, conn: Any, ident: str) -> Optional[dict]:
        from sqlalchemy import text

        user, _, host = (ident or "").rpartition("@")
        row = conn.execute(text(
            "SELECT account_locked, password_expired, password_last_changed, plugin, "
            "Super_priv, max_connections, max_user_connections FROM mysql.user "
            "WHERE User = :user AND Host = :host"), {"user": user, "host": host}).first()
        if row is None:
            return None
        locked, expired, changed, plugin, superuser, max_conn, max_user_conn = row
        account = {"user": user, "host": host}
        parts = []
        try:
            created = conn.execute(text("SHOW CREATE USER :user@:host"), account).scalar()
            parts.append(_HASH.sub(r"\1'…' /* password hash hidden */", created or ""))
        except Exception:  # noqa: BLE001 - needs SELECT on the mysql schema
            pass
        try:
            parts += [r[0] for r in conn.execute(text("SHOW GRANTS FOR :user@:host"), account)]
        except Exception:  # noqa: BLE001
            pass
        try:
            granted = [f"{r[0]}@{r[1]}" for r in conn.execute(text(
                "SELECT FROM_USER, FROM_HOST FROM mysql.role_edges "
                "WHERE TO_USER = :user AND TO_HOST = :host ORDER BY 1, 2"), account)]
            holders = [f"{r[0]}@{r[1]}" for r in conn.execute(text(
                "SELECT TO_USER, TO_HOST FROM mysql.role_edges "
                "WHERE FROM_USER = :user AND FROM_HOST = :host ORDER BY 1, 2"), account)]
        except Exception:  # noqa: BLE001 - MariaDB keeps roles elsewhere
            granted, holders = [], []
        return info(ident, "role", [
            ("User", user), ("Host", host), ("Can log in", locked != "Y"),
            ("Password expired", expired == "Y"), ("Password changed", changed),
            ("Authentication", plugin), ("Super privilege", superuser == "Y"),
            ("Max connections per hour", max_conn or "no limit"),
            ("Max simultaneous connections", max_user_conn or "no limit"),
        ], ";\n\n".join(part.rstrip(";") for part in parts if part) + (";" if parts else ""),
            lists=[{"title": "Granted roles", "items": granted},
                   {"title": "Granted to", "items": holders}])


# The password hash in SHOW CREATE USER: quoted (with backslash escapes) or hex.
_HASH = re.compile(r"(IDENTIFIED\s+WITH\s+'[^']*'\s+AS\s+)(?:'(?:[^'\\]|\\.|'')*'|0x[0-9A-Fa-f]+)",
                   re.I)


def _param_text(param: dict) -> str:
    mode = "" if param["mode"] == "IN" else param["mode"] + " "
    return f"{mode}{param['name']} {param['type']}".strip()
