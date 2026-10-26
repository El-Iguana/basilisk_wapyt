"""
Object actions (milestone 2): the DDL behind Create table, Rename, Drop,
Truncate, Add / rename / drop column and Create / drop index.

Pure: statements are built from the dialect's identifier preparer (names
are always quoted) and the engine adapter's hooks (``engines.base``), so
this is tested without a database. ``sqlops.alter`` previews or runs them.

Types and defaults are SQL the person types, as in the SQL editor they can
already use on the same connection; they are checked only for mistakes that
would turn one statement into several (``;``, comments, unbalanced quotes or
brackets). The statement is always shown before it runs.

A plain module, not a BFF.
"""
from __future__ import annotations

from typing import Any, Optional

ACTIONS = ("create_table", "rename_table", "drop", "truncate", "add_column",
           "rename_column", "drop_column", "create_index", "drop_index")
MAX_NAME = 128
MAX_COLUMNS = 500


class DdlError(ValueError):
    """A request that cannot become a statement, said for the person."""


def name(value: Any, what: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise DdlError(f"{what} is required")
    if len(text) > MAX_NAME:
        raise DdlError(f"{what} is longer than {MAX_NAME} characters")
    if "\x00" in text:
        raise DdlError(f"{what} contains a NUL character")
    return text


def sql_bit(value: Any, what: str, required: bool = False) -> str:
    """A type or default expression: one expression, nothing that could end it early."""
    text = " ".join(str(value or "").split())
    if not text:
        if required:
            raise DdlError(f"{what} is required")
        return ""
    for bad, why in ((";", "a ;"), ("--", "a -- comment"), ("/*", "a /* comment"),
                     ("*/", "a */")):
        if bad in text:
            raise DdlError(f"{what} cannot contain {why}")
    depth, quote = 0, ""
    for ch in text:
        if quote:
            if ch == quote:
                quote = ""
        elif ch in "'\"`[":
            quote = "]" if ch == "[" else ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth < 0:
                break
    # A doubled quote ('it''s') closes and reopens, which the scan treats right.
    if quote or depth:
        raise DdlError(f"{what} has an unclosed quote or bracket")
    return text


def _flag(value: Any) -> bool:
    return value is True or str(value).lower() in ("1", "true", "yes", "on")


def column_sql(spec: Any, prep: Any, column: dict, *, inline_pk: bool = False) -> str:
    """One column definition: name type [identity] [DEFAULT x] [NOT NULL]."""
    col = name(column.get("name"), "Column name")
    type_text = sql_bit(column.get("type"), f"The type of {col}", required=True)
    parts = [prep.quote(col), type_text]
    identity = _flag(column.get("identity"))
    if identity:
        if not spec.identity_clause:
            raise DdlError(f"{spec.label} has no self-numbering columns: give {col} a "
                           "sequence default instead (DEFAULT nextval('a_sequence'))")
        if spec.identity_inline_pk and not inline_pk:
            raise DdlError(f"{spec.label} numbers a column only when it alone is the "
                           "primary key")
        if spec.identity_inline_pk and type_text.upper() != "INTEGER":
            raise DdlError(f"{spec.label} numbers only an INTEGER column ({col} is {type_text})")
        parts.append(spec.identity_clause)
    default = sql_bit(column.get("default"), f"The default of {col}")
    if default and not identity:
        parts.append(f"DEFAULT {default}")
    if _flag(column.get("not_null")) and not (identity and spec.identity_inline_pk):
        parts.append("NOT NULL")
    return " ".join(parts)


def build(spec: Any, prep: Any, action: str, schema: Optional[str], table: str,
          params: dict) -> list:
    """The statements for one action, without trailing semicolons."""
    if action not in ACTIONS:
        raise DdlError(f"Unknown action {action!r}")
    params = params or {}
    if action == "create_table":
        return [_create_table(spec, prep, schema, params)]
    table = name(table, "Table")
    target = spec.qualified(prep, schema, table)
    if action == "rename_table":
        new = name(params.get("new_name"), "New name")
        if new == table:
            raise DdlError("The new name is the same as the old one")
        return [spec.sql_rename_table(prep, schema, table, new)]
    if action == "drop":
        kind = params.get("kind") or "table"
        word = {"table": "TABLE", "view": "VIEW", "matview": "MATERIALIZED VIEW"}.get(kind)
        if word is None:
            raise DdlError(f"Cannot drop a {kind}")
        return [f"DROP {word} {target}"]
    if action == "truncate":
        return [spec.sql_truncate(prep, schema, table)]
    if action == "add_column":
        if _flag(params.get("identity")):
            raise DdlError("An added column cannot number itself here; create it in the table")
        if not spec.add_column_constraints and _flag(params.get("not_null")):
            col = prep.quote(name(params.get("name"), "Column name"))
            return [f"ALTER TABLE {target} {spec.add_column_keyword} "
                    f"{column_sql(spec, prep, {**params, 'not_null': False})}",
                    f"ALTER TABLE {target} ALTER COLUMN {col} SET NOT NULL"]
        return [f"ALTER TABLE {target} {spec.add_column_keyword} "
                f"{column_sql(spec, prep, params)}"]
    if action == "rename_column":
        old = name(params.get("column"), "Column")
        new = name(params.get("new_name"), "New name")
        if new == old:
            raise DdlError("The new name is the same as the old one")
        return [spec.sql_rename_column(prep, schema, table, old, new)]
    if action == "drop_column":
        return [f"ALTER TABLE {target} DROP COLUMN "
                f"{prep.quote(name(params.get('column'), 'Column'))}"]
    if action == "create_index":
        index = name(params.get("name"), "Index name")
        columns = [name(col, "Column") for col in params.get("columns") or []]
        if not columns:
            raise DdlError("Choose at least one column")
        if len(set(columns)) != len(columns):
            raise DdlError("A column is listed twice")
        unique = "UNIQUE " if _flag(params.get("unique")) else ""
        listed = ", ".join(prep.quote(col) for col in columns)
        return [f"CREATE {unique}INDEX {prep.quote(index)} ON {target} ({listed})"]
    # drop_index
    return [spec.sql_drop_index(prep, schema, table, name(params.get("index"), "Index"))]


def _create_table(spec: Any, prep: Any, schema: Optional[str], params: dict) -> str:
    table = name(params.get("name"), "Table name")
    columns = list(params.get("columns") or [])
    if not columns:
        raise DdlError("A table needs at least one column")
    if len(columns) > MAX_COLUMNS:
        raise DdlError(f"At most {MAX_COLUMNS} columns")
    names = [name(col.get("name"), f"Column {i + 1}'s name") for i, col in enumerate(columns)]
    seen: set = set()
    for col in names:
        if col.lower() in seen:
            raise DdlError(f"Two columns are named {col!r}")
        seen.add(col.lower())
    pk = [col for col, spec_col in zip(names, columns) if _flag(spec_col.get("pk"))]
    numbered = [col for col, spec_col in zip(names, columns) if _flag(spec_col.get("identity"))]
    if len(numbered) > 1:
        raise DdlError("Only one column can number itself")
    if numbered and numbered[0] not in pk:
        raise DdlError(f"{numbered[0]} numbers itself, so it must be in the primary key")
    inline = bool(numbered) and spec.identity_inline_pk
    if inline and len(pk) != 1:
        raise DdlError(f"{spec.label} numbers a column only when it alone is the primary key")
    lines = [column_sql(spec, prep, col, inline_pk=inline) for col in columns]
    if pk and not inline:
        lines.append(f"PRIMARY KEY ({', '.join(prep.quote(col) for col in pk)})")
    body = ",\n  ".join(lines)
    return f"CREATE TABLE {spec.qualified(prep, schema, table)} (\n  {body}\n)"
