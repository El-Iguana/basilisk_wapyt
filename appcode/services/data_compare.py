"""
Data compare (milestone 3): the rows of two tables, matched by key, and the
INSERT / UPDATE / DELETE statements that make the **target** table hold what
the **source** holds. A plain module; it runs as a job (``services.jobs``).

* ``check`` (quick, before the job): both tables exist, their columns pair up
  by name, and there is a key to match rows by: the target's primary key
  (its columns must be in the source), else the source's.
* ``run`` (the job) reads the source and keeps one digest per row (by key),
  then reads the target and sorts each row into *same*, *update* or
  *delete*; source keys never seen are *insert*. A third pass reads the
  source again for the rows it lists. Rows are streamed in batches, so
  memory is a digest per source row (at most ``MAX_ROWS``).
* Values are compared **normalised** (``normaliser``): numbers as decimals,
  JSON parsed, UUIDs as hex, booleans as booleans, CHAR without padding.
  The two columns of a pair may be of different engines; the stronger kind
  wins (a JSON column against a text column compares as JSON).
* Statements are written for the target with literals in its dialect
  (``literal``: ``Engine.sql_text_literal`` …). UPDATE sets only the
  columns that differ; WHERE uses the target's key values; inserts into a
  self-numbering column get the engine's wrappers (``import_wrappers``).
* At most ``MAX_LISTED`` rows of each kind are listed and scripted; the
  counts are always whole.
"""
from __future__ import annotations

import datetime as _dt
import decimal
import hashlib
import json
import time
import uuid
from typing import Any, Callable, Optional

MAX_ROWS = 500_000
MAX_LISTED = 5_000
BATCH = 5_000
SHOWN_CHARS = 200


class DataCompareError(ValueError):
    """A request that cannot be compared, said for the person."""


# ---------------------------------------------------------------------------
# Values
# ---------------------------------------------------------------------------

# Which kind decides when the two columns of a pair differ.
_PRIORITY = ("json", "uuid", "bool", "datetime", "date", "time", "interval", "binary",
             "number", "char", "text")
_NUMBERS = {"int", "bigint", "smallint", "numeric", "float"}


def kind_of(family: tuple) -> str:
    """A ``schema_compare.family`` reduced to how its values compare."""
    name = family[0]
    if name in _NUMBERS:
        return "number"
    if name in ("string", "text", "other"):
        return "text"
    return name


def pair_kind(a: tuple, b: tuple) -> str:
    kinds = {kind_of(a), kind_of(b)}
    return next((kind for kind in _PRIORITY if kind in kinds), "text")


def _decimal(value: Any) -> Any:
    try:
        number = decimal.Decimal(repr(value) if isinstance(value, float) else str(value))
    except (decimal.InvalidOperation, ValueError):
        return str(value)
    if not number.is_finite():
        return str(number)
    return number.normalize() if number else decimal.Decimal(0)


def _uuid(value: Any) -> str:
    if isinstance(value, uuid.UUID):
        return value.hex
    try:
        return uuid.UUID(str(value).strip()).hex
    except ValueError:
        return str(value)


def _json(value: Any) -> str:
    if isinstance(value, (bytes, bytearray)):
        value = bytes(value).decode("utf-8", "replace")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return value
    if value is None:
        return None         # a JSON null is no different from SQL NULL here
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      default=str)


def _clock(value: Any) -> Any:
    """MySQL hands a TIME over as a timedelta; a time of day is what it means."""
    if isinstance(value, _dt.timedelta) and _dt.timedelta(0) <= value < _dt.timedelta(days=1):
        return (_dt.datetime.min + value).time()
    return value


def _moment(kind: str) -> Callable[[Any], Any]:
    """Dates, times and timestamps from objects or ISO text, in one form."""
    parse = {"date": lambda text: _dt.date.fromisoformat(text[:10]),
             "time": _dt.time.fromisoformat}.get(kind, _dt.datetime.fromisoformat)

    def norm(value: Any) -> Any:
        value = _clock(value)
        if isinstance(value, str):
            try:
                value = parse(value.strip())
            except ValueError:
                return value
        if isinstance(value, _dt.datetime):
            if value.tzinfo is not None:
                value = value.astimezone(_dt.timezone.utc).replace(tzinfo=None)
            if kind == "date":
                value = value.date()
        if isinstance(value, _dt.time) and value.tzinfo is not None:
            value = value.replace(tzinfo=None)
        return value.isoformat() if hasattr(value, "isoformat") else str(value)

    return norm


def _bool(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray)):
        return int.from_bytes(bytes(value), "big") != 0     # MySQL BIT(1)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("1", "t", "true", "y", "yes", "on"):
            return True
        if text in ("0", "f", "false", "n", "no", "off"):
            return False
        return value
    return bool(value)


def normaliser(kind: str) -> Callable[[Any], Any]:
    """A function giving a value the form it is compared in."""
    plain = {
        "json": _json, "uuid": _uuid, "bool": _bool, "datetime": _moment("datetime"),
        "date": _moment("date"), "time": _moment("time"), "number": _decimal, "char": lambda v: str(v).rstrip(),
        "binary": lambda v: bytes(v) if isinstance(v, (bytes, bytearray, memoryview)) else
        str(v).encode(), "interval": str,
    }.get(kind, lambda v: v if isinstance(v, str) else str(v))
    return lambda value: None if value is None else plain(value)


def shown(value: Any) -> Optional[str]:
    """A value as the grid shows it, cut short."""
    from services.values import cell

    text, _cut = cell(value)
    if text is None:
        return None
    return text if len(text) <= SHOWN_CHARS else text[:SHOWN_CHARS] + "…"


def literal(spec: Any, value: Any, family: tuple, type_text: str) -> str:
    """``value`` as a SQL literal for a target column of ``family``."""
    if value is None:
        return "NULL"
    kind = kind_of(family)
    if kind == "bool" or isinstance(value, bool):
        truth = _bool(value)
        if isinstance(truth, bool):
            if kind == "bool":
                return spec.bool_literals[0 if truth else 1]
            if kind == "number":
                return "1" if truth else "0"
    if isinstance(value, (bytes, bytearray, memoryview)):
        return spec.sql_bytes_literal(bytes(value))
    if kind == "number" and isinstance(value, (int, float, decimal.Decimal)) \
            and not isinstance(value, bool):
        if isinstance(value, float):
            return repr(value) if value == value and value not in (float("inf"), float("-inf")) \
                else spec.sql_text_literal(str(value))
        if isinstance(value, decimal.Decimal):
            return format(value, "f") if value.is_finite() else spec.sql_text_literal(str(value))
        return str(value)
    if isinstance(value, uuid.UUID) or kind == "uuid":
        try:
            parsed = value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
        except ValueError:
            return spec.sql_text_literal(str(value))
        # A UUID column takes the usual form; MySQL and SQLite keep it as 32 hex digits.
        return spec.sql_text_literal(str(parsed) if kind == "uuid" else parsed.hex)
    value = _clock(value) if kind == "time" else value
    if kind == "datetime" and isinstance(value, str):
        try:
            value = _dt.datetime.fromisoformat(value.strip())
        except ValueError:
            pass
    if isinstance(value, _dt.datetime):
        return spec.sql_datetime_literal(value, type_text)
    if isinstance(value, (_dt.date, _dt.time)):
        return spec.sql_text_literal(value.isoformat())
    if isinstance(value, (dict, list)):
        return spec.sql_text_literal(json.dumps(value, ensure_ascii=False, default=str))
    if isinstance(value, decimal.Decimal):
        return spec.sql_text_literal(format(value, "f"))
    return spec.sql_text_literal(str(value))


# ---------------------------------------------------------------------------
# Checking a request
# ---------------------------------------------------------------------------

def _side(user_id: int, side: dict, label: str) -> dict:
    """One table's columns and key, read now."""
    from sqlalchemy import inspect
    from sqlalchemy.exc import NoSuchTableError

    from services import schema_compare, sqlops
    from services.engine_pool import error_text

    with sqlops.connect(user_id, side["conn"], side["db"]) as (conn, spec, profile, _engine):
        schema = spec.schema_for(side["db"], side["schema"])
        insp = inspect(conn)
        try:
            columns = insp.get_columns(side["table"], schema=schema)
        except NoSuchTableError:
            raise DataCompareError(f"The {label} has no table named {side['table']!r}") from None
        except Exception as exc:
            raise DataCompareError(f"Reading the {label}: {error_text(exc)}") from None
        if not columns:     # CockroachDB answers [] for a table that is not there
            raise DataCompareError(f"The {label} has no table named {side['table']!r}")
        try:
            pk = list((insp.get_pk_constraint(side["table"], schema=schema) or {})
                      .get("constrained_columns") or [])
        except NotImplementedError:
            pk = []
        dialect = conn.dialect
    return {
        **side, "engine": spec.name, "label": spec.label, "target_schema": schema,
        "read_only": bool(profile.get("read_only")), "pk": pk,
        "columns": [{"name": col["name"], "type": col["type"],
                     "type_text": schema_compare.type_text(col["type"], dialect),
                     "family": schema_compare.family(col["type"]),
                     "computed": bool(col.get("computed"))} for col in columns],
    }


def check(user_id: int, source: dict, target: dict, where: str = "") -> dict:
    """The plan for ``run``: tables read, columns paired, the key chosen."""
    from services.schema_compare import match

    for side in (source, target):
        side["table"] = str(side.get("table") or "").strip()
        if not side["table"]:
            raise DataCompareError("Choose a table on both sides")
    if all(source[k] == target[k] for k in ("conn", "db", "schema", "table")):
        raise DataCompareError("The source and the target are the same table")
    src, tgt = _side(user_id, source, "source"), _side(user_id, target, "target")
    s_cols = {col["name"]: col for col in src["columns"]}
    t_cols = {col["name"]: col for col in tgt["columns"]}
    pairs, only_s, only_t = match(list(s_cols), list(t_cols))
    to_source = {b.casefold(): a for a, b in pairs}
    to_target = {a.casefold(): b for a, b in pairs}
    notes = []
    if tgt["pk"]:
        missing = [col for col in tgt["pk"] if col.casefold() not in to_source]
        if missing:
            raise DataCompareError(f"The source has no column {missing[0]!r} of the target's "
                                   "primary key: rows cannot be matched")
        key = [(to_source[col.casefold()], col) for col in tgt["pk"]]
    elif src["pk"] and all(col.casefold() in to_target for col in src["pk"]):
        key = [(col, to_target[col.casefold()]) for col in src["pk"]]
        notes.append("The target has no primary key: rows are matched by the source's "
                     f"({', '.join(src['pk'])}), which the target may repeat.")
    else:
        raise DataCompareError("Neither table has a primary key to match rows by")
    if only_s:
        notes.append(f"Only in the source, not compared: {', '.join(only_s)}.")
    if only_t:
        notes.append(f"Only in the target, not compared (inserted rows get their default): "
                     f"{', '.join(only_t)}.")
    keyed = {b for _a, b in key}
    columns = []
    for a, b in pairs:
        sa, tb = s_cols[a], t_cols[b]
        columns.append({"source": a, "target": b, "key": b in keyed,
                        "kind": pair_kind(sa["family"], tb["family"]),
                        "family": list(tb["family"]), "type_text": tb["type_text"],
                        "writable": not tb["computed"]})
    # Key columns first: rows are keyed by the first len(key) values.
    order = {b: i for i, (_a, b) in enumerate(key)}
    columns.sort(key=lambda col: (not col["key"], order.get(col["target"], 0)))
    return {"source": src, "target": tgt, "columns": columns, "keys": len(key),
            "where": str(where or "").strip(), "notes": notes}


def public_plan(plan: dict) -> dict:
    """The plan without SQLAlchemy objects, for a job's result."""
    def side(info: dict) -> dict:
        return {key: info[key] for key in ("conn", "db", "schema", "table", "engine", "label",
                                           "read_only")}
    return {"source": side(plan["source"]), "target": side(plan["target"]),
            "columns": plan["columns"], "keys": plan["keys"], "where": plan["where"],
            "notes": plan["notes"]}


# ---------------------------------------------------------------------------
# The job
# ---------------------------------------------------------------------------

def _stream(user_id: int, info: dict, plan: dict, which: str, handle: Callable,
            progress: Any, item: str) -> int:
    """Every row of one side (key columns first), in batches, to ``handle``."""
    from sqlalchemy import select

    from services import sqlops
    from services.engine_pool import error_text
    from services.jobs import Cancelled

    names = [col[which] for col in plan["columns"]]
    done = 0
    with sqlops.connect(user_id, info["conn"], info["db"], timeout_s=0) as (
            conn, spec, _profile, _engine):
        # Untyped: the driver's own values. A declared type may not hold (SQLite
        # keeps any value in any column), and the normalisers read text too.
        table = sqlops._sa_table(spec, info["db"], info["schema"], info["table"],
                                 tuple((name, None) for name in names))
        stmt = select(*[table.c[name] for name in names])
        if plan["where"]:
            stmt = stmt.where(sqlops.typed_sql(plan["where"]))
        try:
            result = conn.execution_options(stream_results=True, yield_per=BATCH).execute(stmt)
            for part in result.partitions(BATCH):
                handle(part)
                done += len(part)
                progress.advance(item, done, f"{done:,} rows")
                progress.check()
        except (DataCompareError, Cancelled):
            raise
        except Exception as exc:
            raise DataCompareError(f"Reading the {which}: {error_text(exc)}") from None
    return done


def run(user_id: int, plan: dict, progress: Any) -> dict:
    """The comparison; returns the counts, the listed rows and their statements."""
    from services import sqlops

    started = time.monotonic()
    cols = plan["columns"]
    nkeys = plan["keys"]
    norms = [normaliser(col["kind"]) for col in cols]

    def keyed(row) -> tuple:
        values = [norm(value) for norm, value in zip(norms, row)]
        return tuple(values[:nkeys]), values[nkeys:]

    def digest(values: list) -> bytes:
        return hashlib.blake2b(repr(values).encode("utf-8", "replace"), digest_size=16).digest()

    def show_key(row) -> str:
        return " · ".join(f"{col['target']} = {shown(value)}"
                          for col, value in zip(cols[:nkeys], row[:nkeys]))

    # 1. The source: a digest per key.
    src: dict = {}
    progress.item("source", f"Reading the source ({plan['source']['table']})", unit="rows")

    def take_source(part) -> None:
        for row in part:
            key, rest = keyed(row)
            if key in src:
                raise DataCompareError(f"The source has two rows with {show_key(row)}: "
                                       "the key does not identify a row there")
            src[key] = digest(rest)
        if len(src) > MAX_ROWS:
            raise DataCompareError(f"The source has more than {MAX_ROWS:,} rows: compare a "
                                   "part of it with a WHERE condition")

    _stream(user_id, plan["source"], plan, "source", take_source, progress, "source")
    progress.finish("source", total=len(src))
    progress.log(f"Source: {len(src):,} rows.")

    # 2. The target: same, update or delete.
    seen: set = set()
    counts = {"insert": 0, "update": 0, "delete": 0, "same": 0}
    target_rows: dict = {}          # listed updates and deletes: key -> the target's row
    progress.item("target", f"Reading the target ({plan['target']['table']})", unit="rows")

    def take_target(part) -> None:
        for row in part:
            key, rest = keyed(row)
            if key in seen:
                raise DataCompareError(f"The target has two rows with {show_key(row)}: "
                                       "the key does not identify a row there")
            seen.add(key)
            mine = src.get(key)
            change = "delete" if mine is None else "same" if mine == digest(rest) else "update"
            counts[change] += 1
            if change != "same" and counts[change] <= MAX_LISTED:
                target_rows[key] = (change, row)

    _stream(user_id, plan["target"], plan, "target", take_target, progress, "target")
    progress.finish("target", total=len(seen))
    progress.log(f"Target: {len(seen):,} rows.")
    inserts = [key for key in src if key not in seen]
    counts["insert"] = len(inserts)
    del src

    # 3. The source again, for the rows listed.
    wanted = {key for key, (change, _row) in target_rows.items() if change == "update"}
    wanted |= set(inserts[:MAX_LISTED])
    source_rows: dict = {}
    if wanted:
        progress.item("again", "Reading the differing source rows", unit="rows")

        def take_again(part) -> None:
            for row in part:
                key, _rest = keyed(row)
                if key in wanted:
                    source_rows[key] = row

        _stream(user_id, plan["source"], plan, "source", take_again, progress, "again")
        progress.finish("again", total=len(source_rows))

    # 4. The listed rows and their statements, written for the target.
    from services.engines import get

    tgt = plan["target"]
    spec = get(tgt["engine"])
    with sqlops.connect(user_id, tgt["conn"], tgt["db"]) as (conn, _spec, _profile, _engine):
        prep = conn.dialect.identifier_preparer
        schema = spec.schema_for(tgt["db"], tgt["schema"])
        writable = [col["target"] for col in cols if col["writable"]]
        before, after = spec.import_wrappers(conn, prep, schema, tgt["table"], writable) \
            if inserts else ([], [])
    table = spec.qualified(prep, schema, tgt["table"])

    def lit(col: dict, value: Any) -> str:
        return literal(spec, value, tuple(col["family"]), col["type_text"])

    def where(row) -> str:
        return " AND ".join(f"{prep.quote(col['target'])} = {lit(col, value)}"
                            for col, value in zip(cols[:nkeys], row[:nkeys]))

    rows = []
    for key, (change, trow) in target_rows.items():
        if change == "delete":
            rows.append({"change": "delete", "key": show_key(trow), "changed": [],
                         "source": [], "target": [shown(v) for v in trow],
                         "sql": f"DELETE FROM {table} WHERE {where(trow)}"})
            continue
        srow = source_rows.get(key)
        if srow is None:
            continue        # changed while being read
        changed = [i for i in range(nkeys, len(cols))
                   if norms[i](srow[i]) != norms[i](trow[i])]
        sets = [f"{prep.quote(cols[i]['target'])} = {lit(cols[i], srow[i])}" for i in changed
                if cols[i]["writable"]]
        rows.append({"change": "update", "key": show_key(trow),
                     "changed": [cols[i]["target"] for i in changed],
                     "source": [shown(v) for v in srow], "target": [shown(v) for v in trow],
                     "sql": (f"UPDATE {table} SET {', '.join(sets)} WHERE {where(trow)}"
                             if sets else "")})
    names = ", ".join(prep.quote(col["target"]) for col in cols if col["writable"])
    for key in inserts[:MAX_LISTED]:
        srow = source_rows.get(key)
        if srow is None:
            continue
        values = ", ".join(lit(col, value) for col, value in zip(cols, srow) if col["writable"])
        rows.append({"change": "insert", "key": show_key(srow), "changed": [],
                     "source": [shown(v) for v in srow], "target": [],
                     "sql": f"INSERT INTO {table} ({names}) VALUES ({values})"})
    order = {"delete": 0, "update": 1, "insert": 2}
    rows.sort(key=lambda row: order[row["change"]])
    for number, row in enumerate(rows, 1):
        row["id"] = f"r{number}"
    listed = {change: sum(1 for row in rows if row["change"] == change) for change in order}
    notes = list(plan["notes"])
    cut = [change for change in order if counts[change] > listed[change]]
    if cut:
        notes.append(f"Only the first {MAX_LISTED:,} rows of each kind are listed and scripted "
                     f"({', '.join(cut)}): run again after applying them.")
    progress.log(f"{counts['insert']:,} to insert, {counts['update']:,} to update, "
                 f"{counts['delete']:,} only in the target, {counts['same']:,} the same.")
    return {**public_plan(plan), "counts": counts, "listed": listed, "rows": rows,
            "notes": notes, "insert_before": before, "insert_after": after,
            "ms": int((time.monotonic() - started) * 1000)}
