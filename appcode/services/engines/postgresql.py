"""PostgreSQL, through psycopg 3."""
from __future__ import annotations

from typing import Any, Optional

from services.engines.base import Engine, Field

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
