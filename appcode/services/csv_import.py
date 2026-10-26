"""
CSV import into a table (milestone 2). Server-side, a plain module.

* :func:`check` validates an import's options against the reflected table,
  before anything is uploaded (``JobService.plan_import``).
* :func:`run` is the job (``services.jobs``) the upload route starts: it
  reads the uploaded file, turns each field into a value for its column with
  ``values.coerce`` (the grid's rules: numbers, dates, JSON, ``0x`` hex), and
  inserts in batches **inside one transaction**. Cancel, a refused row or a
  field that cannot be converted (unless bad rows are skipped) rolls
  everything back: the table is as it was.

Names are reflected and quoted by SQLAlchemy; the file's content only ever
travels as bound values.
"""
from __future__ import annotations

import csv
import io
import time
from typing import Any, Optional

# The person's choice -> the codec. "utf-8" drops a byte-order mark too.
ENCODINGS = {"utf-8": "utf-8-sig", "utf-16": "utf-16", "latin-1": "latin-1",
             "cp1252": "cp1252"}
MODES = ("append", "replace")
ON_ERROR = ("stop", "skip")
EMPTY = ("null", "empty")
BATCH_ROWS = 500
MAX_SKIP_LOG = 100
MAX_SOURCE_COLUMNS = 4096
MAX_FIELD_CHARS = 16 * 1024 * 1024

csv.field_size_limit(MAX_FIELD_CHARS)


class CsvError(ValueError):
    """An import that cannot go ahead, said for the person."""


def _flag(value: Any) -> bool:
    return value is True or str(value).lower() in ("1", "true", "yes", "on")


def options_from(raw: dict) -> dict:
    """The file-reading options, checked; the mapping is checked against the table later."""
    delimiter = str(raw.get("delimiter") if raw.get("delimiter") is not None else ",")
    if delimiter == "\\t":
        delimiter = "\t"
    if len(delimiter) != 1 or delimiter in "\"'\r\n":
        raise CsvError("The delimiter must be one character (not a quote or a line break)")
    quote = str(raw.get("quote") if raw.get("quote") is not None else '"')
    if quote not in ('"', "'", ""):
        raise CsvError("The quote character must be \" or ' (or none)")
    if quote and quote == delimiter:
        raise CsvError("The quote and the delimiter must differ")
    encoding = str(raw.get("encoding") or "utf-8")
    if encoding not in ENCODINGS:
        raise CsvError(f"Unknown encoding {encoding!r}")
    null_text = str(raw.get("null_text") or "")
    if len(null_text) > 32:
        raise CsvError("The NULL marker is at most 32 characters")
    mode = str(raw.get("mode") or "append")
    on_error = str(raw.get("on_error") or "stop")
    empty = str(raw.get("empty") or "null")
    if mode not in MODES or on_error not in ON_ERROR or empty not in EMPTY:
        raise CsvError("Unknown import option")
    return {"delimiter": delimiter, "quote": quote, "encoding": encoding,
            "header": _flag(raw.get("header", True)), "null_text": null_text, "mode": mode,
            "on_error": on_error, "empty": empty}


def mapping_from(raw: Any, columns: dict) -> list:
    """``[[column, source index]]`` checked against the table's ``{name: reflected column}``."""
    if not isinstance(raw, list) or not raw:
        raise CsvError("Map at least one column of the file to a column of the table")
    mapping, seen = [], set()
    for pair in raw:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise CsvError("Malformed column mapping")
        name, index = str(pair[0]), pair[1]
        if name not in columns:
            raise CsvError(f"The table has no column {name!r}")
        if name in seen:
            raise CsvError(f"{name} is mapped twice")
        try:
            index = int(index)
        except (TypeError, ValueError):
            raise CsvError("Malformed column mapping") from None
        if not 0 <= index < MAX_SOURCE_COLUMNS:
            raise CsvError("Malformed column mapping")
        if columns[name].get("computed"):
            raise CsvError(f"{name} is computed by the database and cannot be imported")
        seen.add(name)
        mapping.append([name, index])
    return mapping


def _reflect(conn: Any, spec: Any, database: str, schema: str, table: str) -> list:
    from sqlalchemy import inspect
    from sqlalchemy.exc import NoSuchTableError

    insp = inspect(conn)
    target = spec.schema_for(database, schema)
    views = set(insp.get_view_names(schema=target))
    if spec.has_matviews:
        views |= set(insp.get_materialized_view_names(schema=target))
    if table in views:
        raise CsvError(f"{table} is a view; import into a table")
    try:
        return insp.get_columns(table, schema=target)
    except NoSuchTableError:
        raise CsvError(f"No table named {table!r}") from None


def check(user_id: int, conn_id: int, database: str, schema: str, table: str,
          options: dict) -> dict:
    """The plan for one import: everything :func:`run` needs, checked."""
    from services import sqlops

    table = (table or "").strip()
    if not table:
        raise CsvError("Choose a table")
    opts = options_from(options or {})
    with sqlops.connect(user_id, conn_id, database) as (conn, spec, profile, _engine):
        if profile.get("read_only"):
            raise CsvError("This connection is read-only")
        columns = {col["name"]: col for col in _reflect(conn, spec, database, schema, table)}
    mapping = mapping_from((options or {}).get("mapping"), columns)
    note = ("Every row of the table is deleted first, in the same transaction."
            if opts["mode"] == "replace" else "")
    return {"conn_id": int(conn_id), "database": database or "", "schema": schema or "",
            "table": table, "mapping": mapping, "note": note, **opts}


class _Counting(io.RawIOBase):
    """A binary file that remembers how many bytes have been read (the progress bar)."""

    def __init__(self, raw: Any) -> None:
        super().__init__()
        self._raw = raw
        self.count = 0

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        n = self._raw.readinto(buffer) or 0
        self.count += n
        return n


def field_value(text: str, opts: dict) -> Optional[str]:
    """A field's text, or None for NULL (the NULL marker; an empty field when asked)."""
    if opts["null_text"] and text == opts["null_text"]:
        return None
    if text == "" and opts["empty"] == "null":
        return None
    return text


def reader_for(stream: Any, opts: dict) -> Any:
    text = io.TextIOWrapper(io.BufferedReader(stream), encoding=ENCODINGS[opts["encoding"]],
                            errors="strict", newline="")
    if opts["quote"]:
        return csv.reader(text, delimiter=opts["delimiter"], quotechar=opts["quote"],
                          quoting=csv.QUOTE_MINIMAL)
    return csv.reader(text, delimiter=opts["delimiter"], quoting=csv.QUOTE_NONE)


def run(user_id: int, plan: dict, path: Any, progress: Any) -> dict:
    """The import job; returns ``{"inserted", "skipped", "deleted", "ms", "table"}``."""
    from sqlalchemy import delete, insert

    from services import sqlops
    from services.engine_pool import error_text
    from services.values import coerce

    started = time.monotonic()
    database, schema, name = plan["database"], plan["schema"], plan["table"]
    size = path.stat().st_size
    progress.item("rows", name, total=size, unit="bytes")
    with sqlops.connect(user_id, plan["conn_id"], database, timeout_s=0) as (
            conn, spec, profile, _engine):
        if profile.get("read_only"):
            raise CsvError("This connection is read-only")
        columns = {col["name"]: col for col in _reflect(conn, spec, database, schema, name)}
        missing = [col for col, _index in plan["mapping"] if col not in columns]
        if missing:
            raise CsvError(f"The table changed: it has no column {missing[0]!r} any more")
        types = {col: columns[col]["type"] for col, _index in plan["mapping"]}
        table = sqlops._sa_table(spec, database, schema, name, tuple(types.items()))
        target = spec.schema_for(database, schema)
        prep = conn.dialect.identifier_preparer
        before, after = spec.import_wrappers(conn, prep, target, name, list(types))
        conn.rollback()     # the reflection's implicit transaction; ours starts now

        inserted = skipped = deleted = 0
        batch: list = []
        batch_from = 0

        def flush() -> None:
            nonlocal inserted, batch
            if not batch:
                return
            try:
                conn.execute(insert(table), batch)
            except Exception as exc:
                rows = f"row {batch_from}" if len(batch) == 1 else \
                    f"rows {batch_from:,}-{batch_from + len(batch) - 1:,}"
                raise CsvError(f"The database refused {rows} of the file: {error_text(exc)} "
                               "Nothing was imported.") from None
            inserted += len(batch)
            batch = []
            progress.advance("rows", counter.count, note=f"{inserted:,} rows")
            progress.check()

        with conn.begin():
            if plan["mode"] == "replace":
                deleted = spec.affected_rows(conn.execute(delete(table))) or 0
                progress.log(f"Deleted the {deleted:,} rows that were there.")
            for sql in before:
                progress.log(sql)
                conn.exec_driver_sql(sql)
            opts = plan
            with open(path, "rb") as raw:
                counter = _Counting(raw)
                reader = reader_for(counter, opts)
                record = 0
                try:
                    for fields in reader:
                        if not fields:
                            continue        # a blank line
                        record += 1
                        if opts["header"] and record == 1:
                            continue
                        row: dict = {}
                        column = ""
                        try:
                            for column, index in plan["mapping"]:
                                text = fields[index] if index < len(fields) else ""
                                value = field_value(text, opts)
                                try:
                                    row[column] = coerce(value, types[column])
                                except ValueError:
                                    if value != "":
                                        raise
                                    row[column] = None   # an empty field in a number column
                        except ValueError as exc:
                            where = f"line {reader.line_num}, column {column}"
                            if opts["on_error"] == "stop":
                                raise CsvError(f"Row {record:,} ({where}): {exc}. "
                                               "Nothing was imported.") from None
                            skipped += 1
                            if skipped <= MAX_SKIP_LOG:
                                progress.log(f"Skipped row {record:,} ({where}): {exc}")
                            elif skipped == MAX_SKIP_LOG + 1:
                                progress.log("… more rows skipped; only the first "
                                             f"{MAX_SKIP_LOG} are listed.")
                            continue
                        if not batch:
                            batch_from = record
                        batch.append(row)
                        if len(batch) >= BATCH_ROWS:
                            flush()
                        elif record % BATCH_ROWS == 0:
                            progress.check()
                except UnicodeDecodeError:
                    raise CsvError(f"The file is not {opts['encoding']} text (near line "
                                   f"{reader.line_num}); choose its encoding.") from None
                except csv.Error as exc:
                    raise CsvError(f"Line {reader.line_num}: {exc}") from None
                flush()
            for sql in after:
                progress.log(sql)
                conn.exec_driver_sql(sql)
            progress.log("Committing…")
    note = f"{inserted:,} rows" + (f", {skipped:,} skipped" if skipped else "")
    progress.finish("rows", note=note)
    progress.log(f"Imported {inserted:,} rows into {name}"
                 + (f"; skipped {skipped:,}" if skipped else "") + ".")
    return {"inserted": inserted, "skipped": skipped, "deleted": deleted, "table": name,
            "ms": int((time.monotonic() - started) * 1000)}
