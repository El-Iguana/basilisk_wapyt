"""Microsoft SQL Server, through pymssql (FreeTDS)."""
from __future__ import annotations

import re
from typing import Any, Optional

from services.engines.base import Engine, Field, Piece, _only_comments

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
