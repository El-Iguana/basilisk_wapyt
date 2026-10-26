"""
BFF: browsing and querying databases. A thin front for ``services.sqlops``.

Every method answers ``{"ok": True, ...}`` or ``{"ok": False, "error": ...}``
and never raises to the browser: a driver's message is the useful part, and a
raised exception would reach the page as a bare 500.

pytincture re-executes this module on every call, so nothing lives at module
level but constants (``tests/test_bff_state.py``); the work and the
running-statement registry are in ``sqlops``, imported inside each method.
"""
from __future__ import annotations

from typing import Optional

from pytincture.dataclass import backend_for_frontend, bff_policy

from services.auth import current_user_id


@backend_for_frontend
@bff_policy(application="basilisk")
class SqlService:
    def __init__(self, _user: dict = None) -> None:
        self._user = _user or {}
        self._user_id = current_user_id(self._user)

    def _call(self, name: str, *args, **kwargs) -> dict:
        if not self._user_id:
            return {"ok": False, "error": "Not authenticated"}
        from services import sqlops
        from services.engine_pool import ProfileNotFound, error_text
        from services.engines import EngineError

        try:
            result = getattr(sqlops, name)(self._user_id, *args, **kwargs)
        except ProfileNotFound:
            return {"ok": False, "error": "Connection not found"}
        except (sqlops.OpError, EngineError) as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001 - the driver's message, for the person
            return {"ok": False, "error": error_text(exc)}
        if isinstance(result, dict) and "ok" in result:
            return result
        return {"ok": True, "value": result} if not isinstance(result, dict) else {"ok": True, **result}

    # The tree --------------------------------------------------------------

    def ping(self, conn_id: int) -> dict:
        return self._call("ping", int(conn_id))

    def databases(self, conn_id: int, show_system: bool = False) -> dict:
        return self._call("databases", int(conn_id), bool(show_system))

    def schemas(self, conn_id: int, database: str = "", show_system: bool = False) -> dict:
        return self._call("schemas", int(conn_id), database or "", bool(show_system))

    def objects(self, conn_id: int, database: str = "", schema: str = "") -> dict:
        return self._call("objects", int(conn_id), database or "", schema or "")

    def object_info(self, conn_id: int, database: str, schema: str, kind: str, ident: str,
                    table: str = "") -> dict:
        return self._call("object_info", int(conn_id), database or "", schema or "",
                          str(kind or ""), str(ident or ""), str(table or ""))

    def alter(self, conn_id: int, database: str, schema: str, table: str, action: str,
              params: dict, dry_run: bool = False) -> dict:
        return self._call("alter", int(conn_id), database or "", schema or "", str(table or ""),
                          str(action or ""), params if isinstance(params, dict) else {},
                          bool(dry_run))

    def roles(self, conn_id: int, show_system: bool = False) -> dict:
        return self._call("roles", int(conn_id), bool(show_system))

    def refresh_matview(self, conn_id: int, database: str, schema: str, name: str,
                        dry_run: bool = False) -> dict:
        return self._call("refresh_matview", int(conn_id), database or "", schema or "",
                          str(name or ""), bool(dry_run))

    # One table ---------------------------------------------------------------

    def table_info(self, conn_id: int, database: str, schema: str, name: str) -> dict:
        return self._call("table_info", int(conn_id), database or "", schema or "", name)

    def browse(self, conn_id: int, database: str, schema: str, name: str, where: str = "",
               order_by: str = "", offset: int = 0, limit: int = 100,
               pk: Optional[list] = None, with_count: bool = True) -> dict:
        return self._call("browse", int(conn_id), database or "", schema or "", name,
                          where or "", order_by or "", int(offset or 0), int(limit or 100),
                          list(pk or []), bool(with_count))

    def count(self, conn_id: int, database: str, schema: str, name: str,
              where: str = "") -> dict:
        return self._call("count", int(conn_id), database or "", schema or "", name, where or "")

    def save_changes(self, conn_id: int, database: str, schema: str, name: str,
                     changes: list, dry_run: bool = False) -> dict:
        return self._call("save_changes", int(conn_id), database or "", schema or "", name,
                          list(changes or []), bool(dry_run))

    def cell_value(self, conn_id: int, database: str, schema: str, name: str, key: dict,
                   column: str) -> dict:
        """One cell uncut, for the value panel: found by the row's primary key."""
        return self._call("cell_value", int(conn_id), database or "", schema or "", name,
                          dict(key or {}), str(column))

    def script(self, conn_id: int, database: str, schema: str, name: str, kind: str) -> dict:
        return self._call("script", int(conn_id), database or "", schema or "", name, kind)

    def er_model(self, conn_id: int, database: str = "", schema: str = "") -> dict:
        """Tables, columns, keys and foreign keys of a location, for the ER diagram."""
        return self._call("er_model", int(conn_id), database or "", schema or "")

    # Session manager ----------------------------------------------------------

    def sessions(self, conn_id: int, include_idle: bool = False) -> dict:
        return self._call("sessions", int(conn_id), bool(include_idle))

    def kill_session(self, conn_id: int, session_id: int, mode: str = "cancel") -> dict:
        return self._call("kill_session", int(conn_id), int(session_id), str(mode or ""))

    # SQL editor --------------------------------------------------------------

    def execute(self, conn_id: int, database: str, sql: str, mode: str = "script",
                caret: int = 0, max_rows: int = 500, run_id: str = "",
                confirmed: bool = False, session: str = "") -> dict:
        """``session`` set: manual commit mode, in that editor's open transaction."""
        return self._call("execute", int(conn_id), database or "", sql or "", mode or "script",
                          int(caret or 0), int(max_rows or 500), run_id or "", bool(confirmed),
                          session or "")

    def explain(self, conn_id: int, database: str, sql: str, caret: int = 0,
                analyze: bool = False, confirmed: bool = False) -> dict:
        """The plan of the statement at the caret; ``analyze`` runs it (rolled back)."""
        return self._call("explain", int(conn_id), database or "", sql or "", int(caret or 0),
                          bool(analyze), bool(confirmed))

    def transaction(self, session: str, action: str) -> dict:
        """Manual commit mode: ``commit``, ``rollback``, ``end`` or ``state``."""
        return self._call("transaction", session or "", action or "")

    def cancel(self, run_id: str) -> dict:
        return self._call("cancel", run_id or "")

    def completion(self, conn_id: int, database: str = "") -> dict:
        return self._call("completion", int(conn_id), database or "")

    def format_sql(self, sql: str) -> dict:
        if not self._user_id:
            return {"ok": False, "error": "Not authenticated"}
        from services.sqlops import format_sql

        return {"ok": True, "sql": format_sql(sql or "")}

    def history(self, conn_id: int = 0, limit: int = 200) -> dict:
        return self._call("history", int(conn_id or 0), int(limit or 200))

    def clear_history(self, conn_id: int = 0) -> dict:
        return self._call("clear_history", int(conn_id or 0))
