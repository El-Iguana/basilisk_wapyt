"""MySQL and MariaDB, through PyMySQL. A database is a schema here."""
from __future__ import annotations

from typing import Any, Optional

from services.engines.base import Engine, Field

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

    def session_id(self, dbapi_conn: Any) -> Any:
        return dbapi_conn.thread_id()

    def cancel(self, engine: Any, dbapi_conn: Any, session: Any) -> None:
        raw = engine.raw_connection()
        try:
            with raw.cursor() as cur:
                cur.execute(f"KILL QUERY {int(session)}")
        finally:
            raw.close()

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
