"""
Database values to and from what the browser shows. Server-side only.

* :func:`cell` turns a driver value into display text (``None`` stays None,
  shown as NULL). Text, not numbers: a bigint or a decimal must not lose
  digits in a JavaScript number.
* :func:`encode_key` / :func:`decode_key` carry a primary-key value to the
  browser and back **losslessly** (type-tagged), so an edit or delete
  targets exactly the row it came from even when the display was cut short.
* :func:`coerce` turns text typed in a cell into a value for a column of a
  reflected SQLAlchemy type, or raises ValueError with a message for the cell.
"""
from __future__ import annotations

import base64
import datetime as _dt
import decimal
import json
import math
import uuid
from typing import Any

# A cell longer than this is cut for display and cannot be edited in the
# grid (the edit would save the cut text).
MAX_CELL_CHARS = 20_000
MAX_BINARY_BYTES = 2_048
# The value panel shows and edits values up to this size (characters for
# text, bytes for binary); a larger one can only be saved to a file. Binary
# travels as hex when edited, and pytincture refuses requests over 16 MB.
VIEW_MAX_BYTES = 4 * 1024 * 1024

_TRUE = {"1", "t", "true", "y", "yes", "on"}
_FALSE = {"0", "f", "false", "n", "no", "off"}


def cell(value: Any) -> tuple[Any, bool]:
    """``(text or None, truncated)`` for one value."""
    if value is None:
        return None, False
    if isinstance(value, bool):
        return ("true" if value else "false"), False
    if isinstance(value, int):
        return str(value), False
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return str(value), False
        return repr(value), False
    if isinstance(value, decimal.Decimal):
        return format(value, "f") if value.is_finite() else str(value), False
    if isinstance(value, _dt.datetime):
        return value.isoformat(sep=" "), False
    if isinstance(value, (_dt.date, _dt.time)):
        return value.isoformat(), False
    if isinstance(value, _dt.timedelta):
        return str(value), False
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        shown = "0x" + raw[:MAX_BINARY_BYTES].hex()
        return (shown + "…", True) if len(raw) > MAX_BINARY_BYTES else (shown, False)
    if isinstance(value, uuid.UUID):
        return str(value), False
    if isinstance(value, (dict, list, tuple)):
        text = json.dumps(value, ensure_ascii=False, default=str)
    else:
        text = str(value)
    if len(text) > MAX_CELL_CHARS:
        return text[:MAX_CELL_CHARS] + "…", True
    return text, False


def full_value(value: Any) -> dict:
    """
    One value for the value panel, uncut up to :data:`VIEW_MAX_BYTES`:
    ``{"null", "binary", "text", "b64", "size", "too_large"}``. Binary comes
    as base64; everything else as the same text the grid shows.
    """
    if value is None:
        return {"null": True, "binary": False, "text": None, "b64": None, "size": 0,
                "too_large": False}
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        big = len(raw) > VIEW_MAX_BYTES
        return {"null": False, "binary": True, "text": None,
                "b64": None if big else base64.b64encode(raw).decode("ascii"),
                "size": len(raw), "too_large": big}
    text = full_text(value)
    big = len(text) > VIEW_MAX_BYTES
    return {"null": False, "binary": False, "text": None if big else text, "b64": None,
            "size": len(text), "too_large": big}


def full_text(value: Any) -> str:
    """:func:`cell`'s text without the cut (not for binary)."""
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, default=str)
    if isinstance(value, (str, int, float, decimal.Decimal, bool, _dt.date, _dt.time,
                          _dt.timedelta, uuid.UUID)):
        return cell(value)[0] if not isinstance(value, str) else value
    return str(value)


def kind_of(value: Any) -> str:
    """A column's display kind from one of its values: num / bool / date / bin / json / text."""
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float, decimal.Decimal)):
        return "num"
    if isinstance(value, (_dt.date, _dt.time, _dt.datetime, _dt.timedelta)):
        return "date"
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "bin"
    if isinstance(value, (dict, list)):
        return "json"
    return "text"


# ---------------------------------------------------------------------------
# Keys
# ---------------------------------------------------------------------------

def encode_key(value: Any) -> list:
    if value is None:
        return ["n", ""]
    if isinstance(value, bool):
        return ["bo", "1" if value else "0"]
    if isinstance(value, int):
        return ["i", str(value)]
    if isinstance(value, float):
        return ["f", repr(value)]
    if isinstance(value, decimal.Decimal):
        return ["d", str(value)]
    if isinstance(value, _dt.datetime):
        return ["dt", value.isoformat()]
    if isinstance(value, _dt.date):
        return ["da", value.isoformat()]
    if isinstance(value, _dt.time):
        return ["t", value.isoformat()]
    if isinstance(value, _dt.timedelta):
        return ["td", repr(value.total_seconds())]
    if isinstance(value, uuid.UUID):
        return ["u", str(value)]
    if isinstance(value, (bytes, bytearray, memoryview)):
        return ["b", base64.b64encode(bytes(value)).decode()]
    return ["s", str(value)]


def decode_key(tagged: Any) -> Any:
    if not isinstance(tagged, (list, tuple)) or len(tagged) != 2:
        raise ValueError("Malformed row key")
    tag, text = tagged[0], str(tagged[1])
    if tag == "n":
        return None
    if tag == "bo":
        return text == "1"
    if tag == "i":
        return int(text)
    if tag == "f":
        return float(text)
    if tag == "d":
        return decimal.Decimal(text)
    if tag == "dt":
        return _dt.datetime.fromisoformat(text)
    if tag == "da":
        return _dt.date.fromisoformat(text)
    if tag == "t":
        return _dt.time.fromisoformat(text)
    if tag == "td":
        return _dt.timedelta(seconds=float(text))
    if tag == "u":
        return uuid.UUID(text)
    if tag == "b":
        return base64.b64decode(text)
    if tag == "s":
        return text
    raise ValueError(f"Unknown key type {tag!r}")


# ---------------------------------------------------------------------------
# Typed text back to a column value
# ---------------------------------------------------------------------------

def python_type(sa_type: Any) -> Any:
    try:
        return sa_type.python_type
    except (NotImplementedError, AttributeError):
        return None


def coerce(text: Any, sa_type: Any) -> Any:
    """A cell's new text as a value for a column of ``sa_type``."""
    if text is None:
        return None
    if not isinstance(text, str):
        text = str(text)
    from sqlalchemy.types import JSON

    if isinstance(sa_type, JSON):
        # No python_type, and its bind processor json.dumps() whatever it is
        # given: the text must be parsed, or an object is saved as a string.
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            raise ValueError("Must be JSON") from None
    wanted = python_type(sa_type)
    if wanted is None or wanted is str:
        return text
    stripped = text.strip()
    try:
        if wanted is bool:
            lowered = stripped.lower()
            if lowered in _TRUE:
                return True
            if lowered in _FALSE:
                return False
            raise ValueError("Must be true or false")
        if wanted is int:
            # MySQL's BOOLEAN is TINYINT(1), and it reflects as an integer.
            lowered = stripped.lower()
            if lowered in ("true", "false"):
                return int(lowered == "true")
            return int(stripped)
        if wanted is float:
            return float(stripped)
        if wanted is decimal.Decimal:
            return decimal.Decimal(stripped)
        if wanted is _dt.datetime:
            return _dt.datetime.fromisoformat(stripped)
        if wanted is _dt.date:
            return _dt.date.fromisoformat(stripped)
        if wanted is _dt.time:
            return _dt.time.fromisoformat(stripped)
        if wanted is uuid.UUID:
            return uuid.UUID(stripped)
        if wanted is bytes:
            if stripped.lower().startswith("0x"):
                return bytes.fromhex(stripped[2:])
            return text.encode("utf-8")
        if wanted in (dict, list):
            return json.loads(text)
        if wanted is _dt.timedelta:
            return text  # the server parses interval text better than we can
    except (ValueError, decimal.InvalidOperation, json.JSONDecodeError) as exc:
        raise ValueError(_message(wanted, exc)) from None
    return text


def _message(wanted: Any, exc: Exception) -> str:
    names = {
        int: "a whole number", float: "a number", decimal.Decimal: "a number",
        _dt.datetime: "a date and time (2026-10-24 13:45:00)", _dt.date: "a date (2026-10-24)",
        _dt.time: "a time (13:45:00)", uuid.UUID: "a UUID", bytes: "hex bytes (0x…)",
        dict: "JSON", list: "JSON", bool: "true or false",
    }
    if wanted in names:
        return f"Must be {names[wanted]}"
    return str(exc)
