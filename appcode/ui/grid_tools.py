"""
Pure helpers for the data grids (milestone 2): column layouts, column
filters, copying rows as INSERT statements or a Markdown table, and the
WHERE that opens a foreign key's row.

No browser objects, so ``tests/test_grid_tools.py`` runs them in CPython;
the page (``basilisk.py``) does the drawing.

Grid values are what ``services.values.cell`` makes of a database value:
text, or None for NULL; binary as ``0x`` + hex. ``kind`` is a column's
display kind from ``values.kind_of``: num / bool / date / bin / json / text.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Optional

MAX_FROZEN = 5

# ---------------------------------------------------------------------------
# Identifiers and literals, per engine
# ---------------------------------------------------------------------------


def quote_ident(engine: str, name: str) -> str:
    if engine == "mysql":
        return "`" + name.replace("`", "``") + "`"
    if engine == "mssql":
        return "[" + name.replace("]", "]]") + "]"
    return '"' + name.replace('"', '""') + '"'


def qualified(engine: str, schema: str, name: str) -> str:
    return (quote_ident(engine, schema) + "." if schema else "") + quote_ident(engine, name)


def text_literal(engine: str, value: Any) -> str:
    """A string literal. MySQL reads backslashes as escapes; T-SQL needs N'' for Unicode."""
    text = str(value).replace("'", "''")
    if engine == "mysql":
        text = text.replace("\\", "\\\\")
    return ("N'" if engine == "mssql" else "'") + text + "'"


def _number(text: str) -> Optional[str]:
    """``text`` when it is a plain number SQL can take as it is, else None."""
    stripped = text.strip()
    if not re.fullmatch(r"[-+]?(\d+(\.\d*)?|\.\d+)([eE][-+]?\d+)?", stripped):
        return None
    try:
        Decimal(stripped)
    except InvalidOperation:  # pragma: no cover - the pattern already checks
        return None
    return stripped


def kind_for_type(type_name: str) -> str:
    """A reflected column type's display kind, for filters before any row is seen."""
    upper = (type_name or "").upper()
    if upper.startswith(("BOOL", "BIT")) and "VARYING" not in upper:
        return "bool"
    if any(word in upper for word in ("INT", "NUMERIC", "DECIMAL", "REAL", "FLOAT",
                                      "DOUBLE", "MONEY", "SERIAL")) and "INTERVAL" not in upper \
            and "POINT" not in upper:
        return "num"
    if any(word in upper for word in ("BYTEA", "BLOB", "BINARY", "IMAGE")):
        return "bin"
    return "text"


def literal(engine: str, value: Any, kind: str = "text") -> str:
    """A grid value as a SQL literal for a column of display ``kind``."""
    if value is None:
        return "NULL"
    text = str(value)
    if kind == "num" and _number(text) is not None:
        return _number(text)
    if kind == "bool" and text.lower() in ("true", "false"):
        on = text.lower() == "true"
        if engine in ("mssql", "sqlite"):
            return "1" if on else "0"
        return "TRUE" if on else "FALSE"
    if kind == "bin" and re.fullmatch(r"0x[0-9a-fA-F]*", text):
        digits = text[2:]
        if engine == "mssql":
            return "0x" + digits
        if engine == "postgresql":
            return "'\\x" + digits + "'"
        return "X'" + digits + "'"
    return text_literal(engine, text)


# ---------------------------------------------------------------------------
# Column layouts: {"order": [names], "widths": {name: px}, "hidden": [names],
# "frozen": n}, saved per table and user (ui_state "columns:...").
# ---------------------------------------------------------------------------


def apply_layout(names: list, state: Optional[dict]) -> list:
    """
    ``[(name, width or None, hidden)]`` in the saved order. Columns the
    table no longer has are skipped; new ones are appended in table order.
    Never hides every column.
    """
    state = state or {}
    order = [name for name in state.get("order") or [] if name in names]
    order = list(dict.fromkeys(order))
    order += [name for name in names if name not in order]
    widths = state.get("widths") or {}
    hidden = set(state.get("hidden") or [])
    if all(name in hidden for name in order):
        hidden = set()
    return [(name, _width(widths.get(name)), name in hidden) for name in order]


def _width(value: Any) -> Optional[int]:
    try:
        width = int(value)
    except (TypeError, ValueError):
        return None
    return max(40, min(2000, width)) if width > 0 else None


def merge_layout(state: Optional[dict], columns: list) -> dict:
    """
    Fold the grid's ``[{id, width, hidden}]`` (display order, ``id`` already
    turned into the column's name) into the saved layout. Columns this page
    does not have keep their saved width and place after the shown ones.
    """
    state = dict(state or {})
    shown = [col["id"] for col in columns]
    widths = dict(state.get("widths") or {})
    for col in columns:
        width = _width(col.get("width"))
        if width is not None:
            widths[col["id"]] = width
    hidden = {name for name in state.get("hidden") or [] if name not in shown}
    hidden |= {col["id"] for col in columns if col.get("hidden")}
    order = shown + [name for name in state.get("order") or [] if name not in shown]
    merged = {"order": order, "widths": widths, "hidden": sorted(hidden)}
    if state.get("frozen"):
        merged["frozen"] = frozen_count(state.get("frozen"))
    return merged


def frozen_count(value: Any) -> int:
    try:
        return max(0, min(MAX_FROZEN, int(value)))
    except (TypeError, ValueError):
        return 0


def is_default(state: Optional[dict]) -> bool:
    """Nothing worth keeping: no widths, nothing hidden, no freeze, natural order."""
    state = state or {}
    return not (state.get("widths") or state.get("hidden") or state.get("frozen")
                or state.get("moved"))


# ---------------------------------------------------------------------------
# Column filters. A table's become WHERE conditions (the server filters every
# row, not just the page); a query result's filter the rows in the page.
# ---------------------------------------------------------------------------

FILTER_OPS = (
    ("contains", "contains"),
    ("not_contains", "does not contain"),
    ("starts", "starts with"),
    ("eq", "="),
    ("ne", "≠"),
    ("gt", ">"),
    ("ge", "≥"),
    ("lt", "<"),
    ("le", "≤"),
    ("null", "is NULL"),
    ("not_null", "is not NULL"),
)
NO_VALUE_OPS = frozenset({"null", "not_null"})
_COMPARE = {"eq": "=", "ne": "<>", "gt": ">", "ge": ">=", "lt": "<", "le": "<="}


def op_label(op: str) -> str:
    return dict(FILTER_OPS).get(op, op)


def filter_label(flt: dict) -> str:
    """``price > 10``, for the filter chip."""
    op = flt.get("op", "")
    if op in NO_VALUE_OPS:
        return f"{flt.get('column', '')} {op_label(op)}"
    return f"{flt.get('column', '')} {op_label(op)} {flt.get('value', '')}"


def _like_pattern(engine: str, value: str, prefix: bool) -> str:
    escaped = value.replace("!", "!!").replace("%", "!%").replace("_", "!_")
    if engine == "mssql":
        escaped = escaped.replace("[", "![")
    return ("" if prefix else "%") + escaped + "%"


def filter_sql(engine: str, flt: dict, kind: str = "text") -> str:
    """One filter as a SQL condition on its column."""
    op = flt.get("op")
    column = quote_ident(engine, str(flt.get("column") or ""))
    value = str(flt.get("value") or "")
    if op == "null":
        return f"{column} IS NULL"
    if op == "not_null":
        return f"{column} IS NOT NULL"
    if op in ("contains", "not_contains", "starts"):
        pattern = text_literal(engine, _like_pattern(engine, value, op == "starts"))
        if engine == "postgresql":
            target, like = f"CAST({column} AS TEXT)", "ILIKE"
        elif engine == "mssql":
            target, like = f"CAST({column} AS NVARCHAR(MAX))", "LIKE"
        else:
            target, like = column, "LIKE"
        negate = "NOT " if op == "not_contains" else ""
        condition = f"{target} {negate}{like} {pattern} ESCAPE '!'"
        # NOT LIKE skips NULLs; "does not contain" should keep them.
        return f"({condition} OR {column} IS NULL)" if negate else condition
    if op in _COMPARE:
        return f"{column} {_COMPARE[op]} {literal(engine, value, kind)}"
    raise ValueError(f"Unknown filter {op!r}")


def where_with_filters(engine: str, where: str, filters: list, kinds: dict) -> str:
    """The typed WHERE and the column filters, ANDed."""
    parts = [f"({where.strip()})"] if where and where.strip() else []
    parts += [filter_sql(engine, flt, kinds.get(flt.get("column"), "text")) for flt in filters]
    if len(parts) == 1 and not filters:
        return where.strip()
    return " AND ".join(parts)


def _as_number(value: Any) -> Optional[Decimal]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None


def filter_match(flt: dict, value: Any) -> bool:
    """Does one result cell pass one filter (case-insensitive text, numbers as numbers)."""
    op = flt.get("op")
    if op == "null":
        return value is None
    if op == "not_null":
        return value is not None
    needle = str(flt.get("value") or "")
    if op == "not_contains":
        return value is None or needle.casefold() not in str(value).casefold()
    if value is None:
        return False
    text = str(value)
    if op == "contains":
        return needle.casefold() in text.casefold()
    if op == "starts":
        return text.casefold().startswith(needle.casefold())
    left, right = _as_number(text), _as_number(needle)
    if left is not None and right is not None:
        a, b = left, right
    else:
        a, b = text.casefold(), needle.casefold()
    return {"eq": a == b, "ne": a != b, "gt": a > b, "ge": a >= b, "lt": a < b,
            "le": a <= b}.get(op, False)


def filter_rows(rows: list, columns: list, filters: list) -> list:
    """The indexes of ``rows`` (lists of values) passing every filter."""
    where = [(columns.index(flt["column"]), flt) for flt in filters
             if flt.get("column") in columns]
    return [i for i, row in enumerate(rows)
            if all(filter_match(flt, row[c] if c < len(row) else None) for c, flt in where)]


# ---------------------------------------------------------------------------
# Copy as INSERT / Markdown
# ---------------------------------------------------------------------------

_IDENT = r'(?:"(?:[^"]|"")+"|`(?:[^`]|``)+`|\[(?:[^\]]|\]\])+\]|[A-Za-z_][\w$#@]*)'
_NAME = re.compile(rf"\s*({_IDENT}(?:\s*\.\s*{_IDENT}){{0,2}})", re.S)
_SELECT_FROM = re.compile(r"^\s*SELECT\b.*?\bFROM\b(.*)$", re.I | re.S)
_END = re.compile(r"\b(?:WHERE|ORDER|GROUP|HAVING|LIMIT|OFFSET|FETCH|FOR|WINDOW)\b|;", re.I)
_QUOTED = re.compile(r'"(?:[^"]|"")*"|`(?:[^`]|``)*`|\[(?:[^\]]|\]\])*\]|\'(?:[^\']|\'\')*\'')
_SEVERAL = re.compile(r"\b(?:JOIN|UNION|INTERSECT|EXCEPT|LATERAL|APPLY)\b|[,(]", re.I)


def table_of_query(sql: str) -> Optional[str]:
    """
    The one table a plain ``SELECT … FROM t`` reads, as written, or None
    (joins, subqueries, several tables): the INSERTs' target.
    """
    text = re.sub(r"--[^\n]*|/\*.*?\*/", " ", sql or "", flags=re.S)
    # Keywords are looked for with quoted names and strings blanked out, so
    # "Order Items" is not an ORDER BY; the name is cut from the real text.
    masked = _QUOTED.sub(lambda m: m.group(0)[0] + "x" * (len(m.group(0)) - 2)
                         + m.group(0)[-1], text)
    match = _SELECT_FROM.match(masked)
    if not match:
        return None
    end = _END.search(masked, match.start(1))
    source_masked = masked[match.start(1):end.start() if end else len(masked)]
    if _SEVERAL.search(source_masked):
        return None
    source = text[match.start(1):match.start(1) + len(source_masked)]
    name = _NAME.match(source)
    if not name:
        return None
    rest = source[name.end():].split()
    # An alias may follow ("t", "AS t"); anything more is not a plain table.
    if len(rest) > 2 or (len(rest) == 2 and rest[0].upper() != "AS"):
        return None
    return re.sub(r"\s*\.\s*", ".", name.group(1))


def insert_sql(engine: str, table: str, columns: list, rows: Iterable[list],
               kinds: Optional[list] = None) -> str:
    """One INSERT per row; ``table`` is already quoted (or as the query wrote it)."""
    kinds = kinds or ["text"] * len(columns)
    names = ", ".join(quote_ident(engine, name) for name in columns)
    lines = []
    for row in rows:
        values = ", ".join(literal(engine, row[i] if i < len(row) else None,
                                   kinds[i] if i < len(kinds) else "text")
                           for i in range(len(columns)))
        lines.append(f"INSERT INTO {table} ({names}) VALUES ({values});")
    return "\n".join(lines)


def _md_cell(value: Any) -> str:
    if value is None:
        return "NULL"
    text = str(value).replace("\\", "\\\\").replace("|", "\\|")
    return re.sub(r"\r\n|\r|\n", "<br>", text)


def markdown_table(columns: list, rows: Iterable[list], kinds: Optional[list] = None) -> str:
    """A GitHub-flavoured Markdown table; numeric columns right-aligned."""
    kinds = kinds or []
    head = "| " + " | ".join(_md_cell(name) for name in columns) + " |"
    rule = "|" + "|".join(
        "---:" if i < len(kinds) and kinds[i] == "num" else "---" for i in range(len(columns))) + "|"
    body = ["| " + " | ".join(_md_cell(row[i] if i < len(row) else None)
                              for i in range(len(columns))) + " |" for row in rows]
    return "\n".join([head, rule, *body])


# ---------------------------------------------------------------------------
# Foreign keys: open the referenced row from a cell
# ---------------------------------------------------------------------------


def foreign_key_for(foreign_keys: list, column: str) -> Optional[dict]:
    """The first foreign key ``column`` is part of (single-column ones first)."""
    found = [fk for fk in foreign_keys or [] if column in (fk.get("columns") or [])]
    found.sort(key=lambda fk: len(fk.get("columns") or []))
    return found[0] if found else None


def reference_where(engine: str, fk: dict, row: dict, kinds: dict) -> Optional[str]:
    """
    ``WHERE`` for the referenced table: each referenced column equals this
    row's value. None when a part of the key is NULL (it references nothing).
    """
    parts = []
    for mine, theirs in zip(fk.get("columns") or [], fk.get("ref_columns") or []):
        value = row.get(mine)
        if value is None:
            return None
        parts.append(f"{quote_ident(engine, theirs)} = "
                     f"{literal(engine, value, kinds.get(mine, 'text'))}")
    return " AND ".join(parts) or None
