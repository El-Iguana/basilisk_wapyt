"""
BFF: plan a CSV import, and follow or cancel any job (milestone 2).

The registry lives in ``services.jobs`` (a plain module); this file is
re-executed per call and keeps nothing. An import is checked here
(``plan_import``) and starts from its upload route in ``transfer.py``, since
the file arrives there.
"""
from __future__ import annotations

from pytincture.dataclass import backend_for_frontend, bff_policy

from services.auth import current_user_id


@backend_for_frontend
@bff_policy(application="basilisk")
class JobService:
    def __init__(self, _user: dict = None) -> None:
        self._user = _user or {}
        self._user_id = current_user_id(self._user)

    def plan_import(self, conn_id: int, database: str, schema: str, table: str,
                    options: dict) -> dict:
        """
        Check an import's options against the table; returns a plan id for
        ``POST /bk/import/<plan>``, whose body is the CSV file.
        """
        if not self._user_id:
            return {"ok": False, "error": "Not authenticated"}
        from services import csv_import, jobs, sqlops
        from services.engine_pool import ProfileNotFound, error_text
        from services.engines import EngineError

        try:
            plan = csv_import.check(self._user_id, int(conn_id), database or "", schema or "",
                                    str(table or ""), options if isinstance(options, dict) else {})
        except ProfileNotFound:
            return {"ok": False, "error": "Connection not found"}
        except (csv_import.CsvError, sqlops.OpError, EngineError) as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001 - the driver's message, for the person
            return {"ok": False, "error": error_text(exc)}
        return {"ok": True, "plan": jobs.put_plan(self._user_id, plan),
                "note": plan.get("note", "")}

    def status(self, job_id: str, log_from: int = 0) -> dict:
        from services import jobs

        job = jobs.get(job_id, self._user_id)
        if job is None:
            return {"ok": False, "error": "No such job (it may have expired)"}
        return {"ok": True, **job.snapshot(log_from)}

    def cancel(self, job_id: str) -> dict:
        from services import jobs

        if not jobs.cancel(job_id, self._user_id):
            return {"ok": False, "error": "That job is not running"}
        return {"ok": True}
