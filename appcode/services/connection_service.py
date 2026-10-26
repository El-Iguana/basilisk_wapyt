"""
BFF: saved connection profiles.

Every query is scoped by the caller's own ``user_id``. The password never
travels back to the browser: ``list``/``get`` return ``has_password``, and
:meth:`save` overwrites it only when a replacement is supplied — so "leave
unchanged" and "clear it" are different requests.
"""
from __future__ import annotations

from typing import Any, Optional

from pytincture.dataclass import backend_for_frontend, bff_policy

from services.auth import current_user_id
from services.db import encode_options, encrypt, fetch_connection, get_db

_PUBLIC_COLUMNS = (
    "id, name, engine, folder, host, port, database, username, options, read_only, "
    "timeout_s, env, notes, created_at"
)

_SENTINEL_UNCHANGED = "\x00unchanged\x00"
ENVIRONMENTS = ("", "dev", "test", "prod")


@backend_for_frontend
@bff_policy(application="basilisk")
class ConnectionService:
    def __init__(self, _user: dict = None) -> None:
        self._user = _user or {}
        self._user_id = current_user_id(self._user)

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def list(self) -> list:
        """Every profile the caller owns, ordered for the sidebar tree."""
        if not self._user_id:
            return []
        from services.engine_pool import pool

        open_ids = set(pool.open_ids(self._user_id))
        with get_db() as conn:
            rows = conn.execute(
                f"SELECT {_PUBLIC_COLUMNS}, (password <> '') AS has_password "
                "FROM connections WHERE user_id = ? "
                "ORDER BY folder COLLATE NOCASE, name COLLATE NOCASE",
                (self._user_id,),
            ).fetchall()
        return [self._public(row, open_ids) for row in rows]

    def get(self, conn_id: int) -> dict:
        if not self._user_id:
            return {}
        with get_db() as conn:
            row = conn.execute(
                f"SELECT {_PUBLIC_COLUMNS}, (password <> '') AS has_password "
                "FROM connections WHERE id = ? AND user_id = ?",
                (int(conn_id), self._user_id),
            ).fetchone()
        return self._public(row, set()) if row else {}

    def engines(self) -> list:
        """What a profile can point at, with each engine's editor fields."""
        if not self._user_id:
            return []
        from services import engines
        from services.engines.sqlite import sqlite_root

        return [{
            "name": spec.name, "label": spec.label, "icon": spec.icon,
            "default_port": spec.default_port, "default_database": spec.default_database,
            "uses_server": spec.uses_server, "levels": list(spec.levels),
            "fields": [field.to_dict() for field in spec.extra_fields],
            "read_only_by_rollback": spec.read_only_by_rollback,
            "root": str(sqlite_root()) if spec.name == "sqlite" else "",
        } for spec in engines.listing()]

    def folders(self) -> list:
        if not self._user_id:
            return []
        with get_db() as conn:
            rows = conn.execute(
                "SELECT DISTINCT folder FROM connections "
                "WHERE user_id = ? AND folder <> '' ORDER BY folder COLLATE NOCASE",
                (self._user_id,),
            ).fetchall()
        return [row["folder"] for row in rows]

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def save(
        self,
        conn_id: Optional[int] = None,
        name: str = "",
        engine: str = "postgresql",
        folder: str = "",
        host: str = "",
        port: int = 0,
        database: str = "",
        username: str = "",
        password: str = _SENTINEL_UNCHANGED,
        options: Optional[dict] = None,
        read_only: bool = False,
        timeout_s: int = 60,
        env: str = "",
        notes: str = "",
    ) -> dict:
        """
        Create or update a profile. ``password`` is three-state: omitted
        leaves the stored one, ``""`` clears it, anything else replaces it.
        """
        if not self._user_id:
            return {"ok": False, "error": "Not authenticated"}
        profile = {
            "engine": engine, "host": host, "port": port, "database": database,
            "options": options or {}, "read_only": read_only,
        }
        errors = self._validate(name, profile, timeout_s, env)
        if errors:
            return {"ok": False, "errors": errors}

        from services import engines

        spec = engines.get(engine)
        values: dict[str, Any] = {
            "name": name.strip(),
            "engine": spec.name,
            "folder": (folder or "").strip(),
            "host": (host or "").strip() if spec.uses_server else "",
            "port": int(port or spec.default_port) if spec.uses_server else 0,
            "database": (database or "").strip(),
            "username": (username or "").strip() if spec.uses_server else "",
            "options": encode_options(options or {}),
            "read_only": int(bool(read_only)),
            "timeout_s": int(timeout_s),
            "env": env or "",
            "notes": notes or "",
        }
        if password != _SENTINEL_UNCHANGED:
            values["password"] = encrypt(password or "") if spec.uses_server else ""

        with get_db() as conn:
            if conn_id:
                owned = conn.execute(
                    "SELECT id FROM connections WHERE id = ? AND user_id = ?",
                    (int(conn_id), self._user_id),
                ).fetchone()
                if owned is None:
                    return {"ok": False, "error": "Connection not found"}
                assignments = ", ".join(f"{column} = ?" for column in values)
                conn.execute(
                    f"UPDATE connections SET {assignments} WHERE id = ? AND user_id = ?",
                    [*values.values(), int(conn_id), self._user_id],
                )
                new_id = int(conn_id)
            else:
                values.setdefault("password", "")
                columns = ", ".join(["user_id", *values])
                marks = ", ".join("?" * (len(values) + 1))
                cursor = conn.execute(
                    f"INSERT INTO connections ({columns}) VALUES ({marks})",
                    [self._user_id, *values.values()],
                )
                new_id = cursor.lastrowid

        from services import sessions
        from services.engine_pool import pool

        sessions.end_for_conn(self._user_id, new_id, "rolled back: the connection was edited")
        pool.close(self._user_id, new_id)
        return {"ok": True, "id": new_id}

    def delete(self, conn_id: int) -> dict:
        if not self._user_id:
            return {"ok": False, "error": "Not authenticated"}
        with get_db() as conn:
            cursor = conn.execute(
                "DELETE FROM connections WHERE id = ? AND user_id = ?",
                (int(conn_id), self._user_id),
            )
            conn.execute("DELETE FROM query_history WHERE user_id = ? AND conn_id = ?",
                         (self._user_id, int(conn_id)))
        if cursor.rowcount == 0:
            return {"ok": False, "error": "Connection not found"}
        from services import sessions
        from services.engine_pool import pool

        sessions.end_for_conn(self._user_id, int(conn_id), "rolled back: the connection was deleted")
        pool.close(self._user_id, int(conn_id))
        return {"ok": True}

    def duplicate(self, conn_id: int) -> dict:
        """A copy of a profile, password included, named "… (copy)"."""
        if not self._user_id:
            return {"ok": False, "error": "Not authenticated"}
        with get_db() as conn:
            cursor = conn.execute(
                "INSERT INTO connections (user_id, name, engine, folder, host, port, database, "
                "  username, password, options, read_only, timeout_s, env, notes) "
                "SELECT user_id, name || ' (copy)', engine, folder, host, port, database, "
                "  username, password, options, read_only, timeout_s, env, notes "
                "FROM connections WHERE id = ? AND user_id = ?",
                (int(conn_id), self._user_id),
            )
        if cursor.rowcount == 0:
            return {"ok": False, "error": "Connection not found"}
        return {"ok": True, "id": cursor.lastrowid}

    # ------------------------------------------------------------------
    # Connectivity
    # ------------------------------------------------------------------

    def test(
        self,
        conn_id: Optional[int] = None,
        engine: str = "postgresql",
        host: str = "",
        port: int = 0,
        database: str = "",
        username: str = "",
        password: str = _SENTINEL_UNCHANGED,
        options: Optional[dict] = None,
        read_only: bool = False,
    ) -> dict:
        """
        Connect with the editor's current values, saved or not. For a stored
        profile an omitted password falls back to the stored one.
        """
        if not self._user_id:
            return {"ok": False, "error": "Not authenticated"}
        stored: dict = {}
        if conn_id:
            stored = fetch_connection(int(conn_id), self._user_id) or {}
            if not stored:
                return {"ok": False, "error": "Connection not found"}
        profile = {
            "engine": engine, "host": host, "port": port, "database": database,
            "username": username, "options": options or {}, "read_only": read_only,
            "password": stored.get("password", "") if password == _SENTINEL_UNCHANGED else password,
        }
        errors = self._validate("x", profile, 60, "")
        if errors:
            return {"ok": False, "error": next(iter(errors.values()))}

        import time

        from services import engines
        from services.engine_pool import build_engine, dispose_quietly, error_text

        spec = engines.get(engine)
        started = time.monotonic()
        built = None
        try:
            built = build_engine(spec, profile, spec.database_for(profile, None))
            from sqlalchemy import text

            with built.connect() as conn:
                conn.execute(text("SELECT 1"))
                version = ".".join(str(part) for part in conn.dialect.server_version_info or ())
        except Exception as exc:  # noqa: BLE001 - the driver's message is the answer
            return {"ok": False, "error": error_text(exc)}
        finally:
            if built is not None:
                dispose_quietly(built)
        return {"ok": True, "version": version, "ms": int((time.monotonic() - started) * 1000)}

    def disconnect(self, conn_id: int) -> dict:
        from services import sessions
        from services.engine_pool import pool

        sessions.end_for_conn(self._user_id, int(conn_id), "rolled back: you disconnected")
        pool.close(self._user_id, int(conn_id))
        return {"ok": True}

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _public(row: Any, open_ids: set) -> dict:
        from services import engines
        from services.db import decode_options

        data = dict(row)
        options = decode_options(data.pop("options", "") or "")
        try:
            spec = engines.get(data.get("engine") or "")
            data["engine_label"] = spec.label
            data["icon"] = spec.icon
            data["levels"] = list(spec.levels)
            data["dialect"] = spec.editor_dialect
            data["read_only_by_rollback"] = spec.read_only_by_rollback
            data["default_database"] = spec.default_database
            data["has_roles"] = spec.has_roles
            data["roles_label"] = spec.roles_label
        except engines.EngineError:
            data.update(engine_label=data.get("engine") or "?", icon="mdi-database-alert",
                        levels=[], dialect="StandardSQL", read_only_by_rollback=False,
                        default_database="", has_roles=False, roles_label="")
        # Engine options hold no secrets today; send them back for the editor.
        data["options"] = options
        data["read_only"] = bool(data.get("read_only"))
        data["has_password"] = bool(data.pop("has_password", 0))
        data["open"] = data["id"] in open_ids
        return data

    @staticmethod
    def _validate(name: str, profile: dict, timeout_s: Any, env: str) -> dict:
        from services import engines

        errors: dict = {}
        if not (name or "").strip():
            errors["name"] = "Name is required"
        try:
            timeout = int(timeout_s)
        except (TypeError, ValueError):
            timeout = -1
        if not 0 <= timeout <= 86_400:
            errors["timeout_s"] = "Timeout must be 0 (none) to 86400 seconds"
        if env not in ENVIRONMENTS:
            errors["env"] = "Unknown connection type"
        try:
            spec = engines.get(profile.get("engine") or "")
        except engines.EngineError as exc:
            errors["engine"] = str(exc)
            return errors
        errors.update(spec.validate(profile))
        return errors
