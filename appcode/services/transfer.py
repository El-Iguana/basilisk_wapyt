"""
Plain HTTP routes for exports, under ``/bk``.

Not BFF calls: the BFF is JSON in Pyodide's heap, and an export can be
millions of rows. These stream from a server-side cursor
(``stream_results``) straight to the download. They read pytincture's own
session (so they share the app's login); the POST also checks the CSRF
token the way pytincture's BFF handler does.

* ``GET /bk/export/<conn_id>``: a table with the view's WHERE / ORDER BY.
* ``POST /bk/export-query/<conn_id>``: one SELECT from the SQL editor.
* ``GET /bk/cell/<conn_id>``: one cell's value as a file (the value panel's
  Save to file), whatever its size.
* ``POST /bk/import/<plan>``: a CSV file to import, as planned by
  ``JobService.plan_import``; starts the import job.
"""
from __future__ import annotations

import csv
import hmac
import io
import json
import re
from typing import Iterator, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

from services.auth import current_user_id

router = APIRouter(prefix="/bk")

EXPORT_MAX_ROWS = 1_000_000
_BATCH = 1000


def _require_user(request: Request) -> int:
    session = getattr(request, "session", None)
    user = session.get("user") if session else None
    user_id = current_user_id(user if isinstance(user, dict) else None)
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user_id


def _require_csrf(request: Request) -> None:
    session = getattr(request, "session", None)
    expected = (session or {}).get("csrf_token", "")
    supplied = request.headers.get("x-csrf-token", "")
    if not expected or not supplied or not hmac.compare_digest(str(expected), supplied):
        raise HTTPException(status_code=403, detail="CSRF validation failed")


def _safe_filename(*parts: str) -> str:
    name = "_".join(part for part in parts if part)
    return re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-") or "export"


def _guard(value):
    """CSV injection guard: text a spreadsheet would run as a formula."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        try:
            float(value)
            return value
        except ValueError:
            return "'" + value
    return value


def _stream(user_id: int, conn_id: int, database: str, stmt_factory, fmt: str,
            limit: int) -> Iterator[bytes]:
    """
    Rows of one statement as CSV (UTF-8 with a BOM) or JSON lines. The
    statement is opened before the response starts, so an error is a proper
    HTTP status instead of a truncated file.
    """
    from services import sqlops
    from services.values import cell

    ctx = sqlops.connect(user_id, conn_id, database, timeout_s=0)
    conn, spec, _profile, _engine = ctx.__enter__()
    try:
        result = stmt_factory(conn.execution_options(stream_results=True, yield_per=_BATCH),
                              spec)
    except Exception as exc:
        ctx.__exit__(None, None, None)
        from services.engine_pool import error_text

        raise HTTPException(status_code=400, detail=error_text(exc)) from None
    columns = list(result.keys())

    def rows() -> Iterator[bytes]:
        try:
            sent = 0
            if fmt == "csv":
                buffer = io.StringIO()
                writer = csv.writer(buffer)
                writer.writerow(columns)
                yield ("﻿" + buffer.getvalue()).encode("utf-8")
            else:
                yield b"[\n"
            for raw in result:
                if sent >= limit:
                    break
                values = [cell(value)[0] if not isinstance(value, (bytes, memoryview, bytearray))
                          else "0x" + bytes(value).hex() for value in raw]
                if fmt == "csv":
                    buffer = io.StringIO()
                    csv.writer(buffer).writerow([_guard(v) for v in values])
                    yield buffer.getvalue().encode("utf-8")
                else:
                    line = json.dumps(dict(zip(columns, values)), ensure_ascii=False)
                    yield (("," if sent else "") + line + "\n").encode("utf-8")
                sent += 1
            if fmt != "csv":
                yield b"]\n"
        finally:
            result.close()
            ctx.__exit__(None, None, None)

    return rows()


def _response(body: Iterator[bytes], fmt: str, *parts: str) -> StreamingResponse:
    media = "text/csv; charset=utf-8" if fmt == "csv" else "application/json"
    filename = _safe_filename(*parts) + (".csv" if fmt == "csv" else ".json")
    return StreamingResponse(body, media_type=media, headers={
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Cache-Control": "no-store",
    })


def _open_or_404(user_id: int, conn_id: int, database: str):
    from services.engine_pool import ProfileNotFound, pool
    from services.engines import EngineError

    try:
        return pool.open(user_id, conn_id, database)
    except ProfileNotFound:
        raise HTTPException(status_code=404, detail="Connection not found") from None
    except EngineError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None


@router.get("/export/{conn_id}")
def export_table(request: Request, conn_id: int, table: str, database: str = "",
                 schema: str = "", where: str = "", order_by: str = "", format: str = "csv",
                 limit: int = EXPORT_MAX_ROWS):
    """Every row the view's WHERE matches, in its ORDER BY, not just the page."""
    from services import sqlops

    user_id = _require_user(request)
    if format not in ("csv", "json"):
        raise HTTPException(status_code=400, detail="format must be csv or json")
    _open_or_404(user_id, conn_id, database)

    def statement(conn, spec):
        return conn.execute(
            sqlops._browse_stmt(spec, conn, database, schema, table, where, order_by, []))

    cap = max(1, min(int(limit or EXPORT_MAX_ROWS), EXPORT_MAX_ROWS))
    body = _stream(user_id, conn_id, database, statement, format, cap)
    return _response(body, format, database, schema, table)


@router.post("/export-query/{conn_id}")
async def export_query(request: Request, conn_id: int):
    """One statement from the SQL editor, all of its rows. Body: JSON {sql, database, format}."""
    user_id = _require_user(request)
    _require_csrf(request)
    try:
        payload = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="Expected JSON") from None
    sql = str(payload.get("sql") or "").strip()
    database = str(payload.get("database") or "")
    fmt = str(payload.get("format") or "csv")
    if not sql:
        raise HTTPException(status_code=400, detail="Nothing to export")
    if fmt not in ("csv", "json"):
        raise HTTPException(status_code=400, detail="format must be csv or json")
    _engine, spec, profile = _open_or_404(user_id, conn_id, database)
    from services.engines.base import strip_trailing_semicolon
    from services.sqlops import _READ_FIRST_WORDS, first_word

    if first_word(sql) not in _READ_FIRST_WORDS:
        raise HTTPException(status_code=400, detail="Only a query that returns rows can be exported")
    # Sent as typed: no bind parameters, so neither ":" nor "%" is special.
    body = _stream(user_id, conn_id, database,
                   lambda conn, _spec: conn.execution_options(no_parameters=True)
                   .exec_driver_sql(strip_trailing_semicolon(sql)), fmt, EXPORT_MAX_ROWS)
    return _response(body, fmt, profile.get("name") or "query", "result")


@router.get("/cell/{conn_id}")
def download_cell(request: Request, conn_id: int, table: str, column: str, key: str,
                  database: str = "", schema: str = ""):
    """
    One cell as a file: binary as is, JSON as ``.json``, anything else as
    UTF-8 text. ``key`` is the row's primary key as JSON ``{column: tagged}``.
    """
    from services import sqlops
    from services.values import full_text

    user_id = _require_user(request)
    try:
        parsed = json.loads(key)
    except ValueError:
        raise HTTPException(status_code=400, detail="Malformed row key") from None
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=400, detail="Malformed row key")
    _open_or_404(user_id, conn_id, database)
    try:
        value = sqlops.fetch_cell(user_id, conn_id, database, schema, table, parsed, column)
    except sqlops.OpError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    if value is None:
        raise HTTPException(status_code=404, detail="The value is NULL")
    if isinstance(value, (bytes, bytearray, memoryview)):
        body, media, ext = bytes(value), "application/octet-stream", ".bin"
    elif isinstance(value, (dict, list)):
        body, media, ext = full_text(value).encode("utf-8"), "application/json", ".json"
    else:
        body, media, ext = full_text(value).encode("utf-8"), "text/plain; charset=utf-8", ".txt"
    filename = _safe_filename(table, column) + ext
    return Response(body, media_type=media, headers={
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
    })


def max_import_bytes() -> int:
    import os

    try:
        return max(1, int(os.environ.get("BASILISK_IMPORT_MAX_BYTES", 1024 * 1024 * 1024)))
    except ValueError:
        return 1024 * 1024 * 1024


@router.post("/import/{plan_id}")
async def import_csv(request: Request, plan_id: str):
    """
    The CSV file of a planned import (``JobService.plan_import``), as the raw
    request body, not a form: nothing needs parsing, and nothing is read
    until the caller is authenticated, the CSRF token checks out and the
    plan is theirs. It goes to a file, since the job outlives this request.
    """
    import anyio
    from fastapi.responses import JSONResponse

    from services import csv_import, jobs

    user_id = _require_user(request)
    _require_csrf(request)
    plan = jobs.take_plan(plan_id, user_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="That import has expired; start it again")
    limit = max_import_bytes()
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise HTTPException(status_code=413, detail=f"Larger than the {limit:,}-byte limit")
    upload = jobs.new_file(".csv")
    try:
        received = 0
        with open(upload, "wb") as out:
            async for block in request.stream():
                received += len(block)
                if received > limit:
                    raise HTTPException(status_code=413,
                                        detail=f"Larger than the {limit:,}-byte limit")
                await anyio.to_thread.run_sync(out.write, block)
    except BaseException:
        upload.unlink(missing_ok=True)
        raise

    def work(progress) -> dict:
        try:
            return csv_import.run(user_id, plan, upload, progress)
        finally:
            upload.unlink(missing_ok=True)

    try:
        job = jobs.start(user_id, "import", f"Import into {plan['table']}", work)
    except jobs.JobLimit as exc:
        upload.unlink(missing_ok=True)
        raise HTTPException(status_code=429, detail=str(exc)) from None
    return JSONResponse({"ok": True, "job": job.id, "bytes": received})
