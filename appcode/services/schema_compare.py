"""
Schema compare (milestone 3): what differs between two schemas, and the
statements that make the **target** like the **source**. A plain module.

* ``snapshot`` reads one schema with SQLAlchemy's bulk reflection (one query
  per kind, not per table): tables with columns, primary keys, foreign keys,
  indexes and unique constraints, and views with their definitions.
* ``compare`` pairs tables, views and columns by name (exactly, then
  ignoring case), and keys and indexes **by their columns**: engines make up
  their names, so names are not compared. Each difference comes with the
  statements that fix it, tagged with a phase (``PHASES``) so that any subset
  the person picks runs in a workable order: foreign keys are dropped before
  anything else and added after every table and column exists.
* The statements are written for the target: its dialect quotes names and
  compiles types, and its engine adapter has the DDL that differs between
  engines (``Engine.sql_alter_column``, ``sql_drop_constraint`` …). SQLite
  cannot change a column, a key or a foreign key in place, so a table with
  such changes is **rebuilt** (new table, copy, drop, rename).
* Between different engines, types are compared by family (an INTEGER is an
  INT, a TIMESTAMP a DATETIME), defaults only when both are plain values,
  and view definitions not at all.
* Nothing here runs anything: the page opens the script in a SQL editor on
  the target, where the usual confirmations apply.
"""
from __future__ import annotations

import copy
import re
from typing import Any, Callable, Optional

MAX_TABLES = 2000
MAX_VIEWS = 500

# The order statements run in, whatever subset is chosen.
PHASES = ("Drop views", "Drop foreign keys", "Drop indexes", "Drop primary keys",
          "Create tables", "Add columns", "Change columns", "Drop columns",
          "Add primary keys", "Create indexes", "Add foreign keys", "Drop tables",
          "Create or replace views")
P_DROP_VIEW, P_DROP_FK, P_DROP_INDEX, P_DROP_PK, P_CREATE_TABLE, P_ADD_COLUMN, \
    P_ALTER_COLUMN, P_DROP_COLUMN, P_ADD_PK, P_CREATE_INDEX, P_ADD_FK, P_DROP_TABLE, \
    P_VIEW = range(len(PHASES))

_KIND_ORDER = {"table": 0, "view": 0, "column": 1, "pk": 2, "index": 3, "fk": 4}


# ---------------------------------------------------------------------------
# Reading a schema
# ---------------------------------------------------------------------------

def snapshot(conn: Any, spec: Any, database: str, schema: str,
             only: Optional[list] = None) -> dict:
    """
    One schema's tables and views, in plain dicts (types stay SQLAlchemy
    types). ``only`` reads just those tables, and no views.
    """
    from sqlalchemy import inspect

    target = spec.schema_for(database, schema)
    spec.before_compare(conn, target)
    insp = inspect(conn)
    dialect = conn.dialect
    names = sorted(insp.get_table_names(schema=target))
    if only is not None:
        names = [name for name in names if name in only]
    model = {"engine": spec.name, "label": spec.label, "schema": target, "dialect": dialect,
             "tables": {}, "views": {}, "truncated": len(names) > MAX_TABLES}
    names = names[:MAX_TABLES]
    if names:
        columns = insp.get_multi_columns(schema=target, filter_names=names)
        pks = insp.get_multi_pk_constraint(schema=target, filter_names=names)
        fks = _multi(insp.get_multi_foreign_keys, target, names)
        indexes = _multi(insp.get_multi_indexes, target, names)
        uniques = _multi(insp.get_multi_unique_constraints, target, names)
    for name in names:
        key = (target, name)
        pk = pks.get(key) or {}
        pk_cols = list(pk.get("constrained_columns") or [])
        cols = []
        for col in columns.get(key) or []:
            text = type_text(col["type"], dialect)
            computed = col.get("computed") or None
            cols.append({
                "name": col["name"], "type": col["type"], "type_text": text,
                # A key column is never NULL, whatever the engine reports (SQLite's rowid).
                "nullable": bool(col.get("nullable", True)) and col["name"] not in pk_cols,
                "default": None if col.get("default") is None else str(col["default"]).strip(),
                "auto": _auto(col, pk_cols, text, spec.name),
                # SQLite numbers every one-column INTEGER key (the rowid) whether asked or not.
                "rowid": spec.name == "sqlite" and pk_cols == [col["name"]]
                and text.upper() == "INTEGER",
                "computed": str(computed.get("sqltext")) if computed else None,
                "comment": col.get("comment") or None,
            })
        found_fks = []
        for fk in fks.get(key) or []:
            ref_schema = fk.get("referred_schema")
            if ref_schema == target or (spec.name == "mysql" and ref_schema == database):
                ref_schema = None
            options = fk.get("options") or {}
            found_fks.append({
                "name": fk.get("name"), "columns": list(fk.get("constrained_columns") or []),
                "ref_schema": ref_schema, "ref_table": fk.get("referred_table") or "",
                "ref_columns": list(fk.get("referred_columns") or []),
                "on_delete": _fk_action(options.get("ondelete")),
                "on_update": _fk_action(options.get("onupdate")),
            })
        found_ix: list = []
        for ix in indexes.get(key) or []:
            if ix.get("duplicates_constraint"):
                continue        # listed again as the unique constraint below
            column_names = list(ix.get("column_names") or [])
            expressions = [str(e) for e in ix.get("expressions") or []]
            expression = any(c is None for c in column_names) or (not column_names and expressions)
            found_ix.append({
                "name": ix.get("name"), "unique": bool(ix.get("unique")), "constraint": False,
                "columns": expressions if expression else column_names,
                "expression": bool(expression),
            })
        for uq in uniques.get(key) or []:
            found_ix.append({"name": uq.get("name"), "unique": True, "constraint": True,
                             "columns": list(uq.get("column_names") or []), "expression": False})
        seen: set = set()
        kept = []
        for ix in sorted(found_ix, key=lambda ix: not ix["constraint"]):
            if _ix_key(ix) not in seen:
                seen.add(_ix_key(ix))
                kept.append(ix)
        model["tables"][name] = {"name": name, "columns": cols, "fks": found_fks,
                                 "indexes": sorted(kept, key=lambda ix: str(ix["name"] or "")),
                                 "pk": {"name": pk.get("name"), "columns": pk_cols}}
    view_names = [] if only is not None else sorted(insp.get_view_names(schema=target))
    model["truncated"] = model["truncated"] or len(view_names) > MAX_VIEWS
    for name in view_names[:MAX_VIEWS]:
        try:
            model["views"][name] = (insp.get_view_definition(name, schema=target) or "").strip()
        except Exception:  # noqa: BLE001 - no permission to read it: compared by name only
            model["views"][name] = ""
    return model


def _multi(method: Callable, schema: Optional[str], names: list) -> dict:
    try:
        return method(schema=schema, filter_names=names) or {}
    except NotImplementedError:
        return {}


def type_text(sa_type: Any, dialect: Any) -> str:
    try:
        return str(sa_type.compile(dialect=dialect))
    except Exception:  # noqa: BLE001 - some reflected types cannot compile
        return repr(sa_type)


def _auto(col: dict, pk: list, text: str, engine: str) -> bool:
    """Whether the column numbers itself (identity, serial, AUTO_INCREMENT, rowid)."""
    if col.get("identity") or col.get("autoincrement") is True:
        return True
    if str(col.get("default") or "").lower().startswith("nextval("):
        return True
    # SQLite: a one-column INTEGER PRIMARY KEY is the rowid, numbered by itself.
    return engine == "sqlite" and pk == [col["name"]] and text.upper() == "INTEGER"


def _fk_action(value: Any) -> Optional[str]:
    text = " ".join(str(value or "").upper().split())
    return None if text in ("", "NO ACTION", "RESTRICT") else text


def _fold(names) -> tuple:
    return tuple(str(name).casefold() for name in names)


def _ix_key(ix: dict) -> tuple:
    return (_fold(ix["columns"]), ix["unique"])


# ---------------------------------------------------------------------------
# Types and defaults
# ---------------------------------------------------------------------------

def _generic(sa_type: Any) -> Any:
    """The engine-neutral type, without collation; unbounded strings as TEXT."""
    from sqlalchemy import types as T

    if _mysql_bool(sa_type):
        return T.Boolean()
    try:
        generic = sa_type.as_generic()
    except NotImplementedError:
        generic = sa_type
    if isinstance(sa_type, T.CHAR) and not isinstance(generic, T.CHAR):
        generic = (T.NCHAR if isinstance(sa_type, T.NCHAR) else T.CHAR)(sa_type.length)
    if isinstance(generic, T.String) and not isinstance(generic, (T.Text, T.Enum)) \
            and generic.length is None:
        generic = T.UnicodeText() if isinstance(generic, T.Unicode) else T.Text()
    if getattr(generic, "collation", None):
        generic = copy.copy(generic)
        generic.collation = None
    return generic


def _mysql_bool(sa_type: Any) -> bool:
    """MySQL's BOOLEAN is TINYINT(1)."""
    return type(sa_type).__name__ == "TINYINT" and getattr(sa_type, "display_width", None) == 1


def family(sa_type: Any) -> tuple:
    """What a type is, across engines: ``("string", 20)``, ``("int",)`` …"""
    from sqlalchemy import types as T

    t = _generic(sa_type)
    for cls, name in ((T.Boolean, "bool"), (T.BigInteger, "bigint"),
                      (T.SmallInteger, "smallint"), (T.Integer, "int"), (T.Float, "float")):
        if isinstance(t, cls):
            return (name,)
    if isinstance(t, T.Numeric):
        return ("numeric", t.precision, t.scale)
    if isinstance(t, T.DateTime):
        return ("datetime", bool(getattr(t, "timezone", False)))
    for cls, name in ((T.Date, "date"), (T.Time, "time"), (T.Interval, "interval"),
                      (T.JSON, "json"), (T.Uuid, "uuid"), (T.LargeBinary, "binary")):
        if isinstance(t, cls):
            return (name,)
    if isinstance(t, T._Binary):
        return ("binary",) if t.length is None else ("binary", t.length)
    if isinstance(t, T.Text) or (isinstance(t, T.String) and t.length is None):
        return ("text",)
    if isinstance(t, T.String):
        return ("char" if isinstance(t, (T.CHAR, T.NCHAR)) else "string", t.length)
    return ("other", type(t).__name__.lower())


def type_for(sa_type: Any, source_text: str, dialect: Any, same: bool) -> Optional[str]:
    """The source column's type as the target writes it; None when it has none."""
    if same:
        return source_text
    for candidate in (_generic(sa_type), sa_type):
        try:
            return str(candidate.compile(dialect=dialect))
        except Exception:  # noqa: BLE001 - try the next form
            continue
    return None


def _norm(text: str) -> str:
    return re.sub(r"\s*,\s*", ",", " ".join(str(text or "").split())).upper()


_COLLATE = re.compile(r"\s+COLLATE\s+\S+", re.I)


def _wrapped(text: str) -> bool:
    """Whether the first ``(`` closes at the very end: ``(a)`` yes, ``(a)+(b)`` no."""
    depth, quote = 0, ""
    for i, ch in enumerate(text):
        if quote:
            if ch == quote:
                quote = ""
        elif ch == "'":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0 and i != len(text) - 1:
                return False
    return depth == 0


def norm_default(value: Optional[str]) -> Optional[str]:
    """A default without SQL Server's wrapping parentheses and extra spaces."""
    if value is None:
        return None
    text = " ".join(str(value).split())
    while len(text) >= 2 and text[0] == "(" and text[-1] == ")" and _wrapped(text):
        text = text[1:-1].strip()
    return text


_PG_CAST = re.compile(r"::[\w ]+(?:\(\d+(?:,\s*\d+)?\))?(?:\[\])?$")
_LITERAL = re.compile(r"^(?:-?\d+(?:\.\d+)?|N?'(?:[^']|'')*'|NULL|TRUE|FALSE|"
                      r"CURRENT_TIMESTAMP|CURRENT_DATE|CURRENT_TIME)$", re.I)


def portable_default(value: Optional[str], engine: str) -> Optional[str]:
    """A default every engine reads the same way (a number, a string…), for ``engine``; else None."""
    text = norm_default(value)
    if text is None:
        return None
    text = _PG_CAST.sub("", text).strip()
    text = norm_default(text) or ""
    if not _LITERAL.match(text):
        return None
    upper = text.upper()
    if text[:2] in ("N'", "n'") and engine != "mssql":
        text = text[1:]
    if engine == "mssql" and upper in ("TRUE", "FALSE"):
        return "1" if upper == "TRUE" else "0"
    if upper in ("NULL", "TRUE", "FALSE", "CURRENT_TIMESTAMP", "CURRENT_DATE", "CURRENT_TIME"):
        return upper
    return text


def describe(col: dict) -> str:
    """One column as people read it: ``VARCHAR(20) NOT NULL DEFAULT 'x'``."""
    parts = [col["type_text"]]
    if col.get("computed"):
        parts.append(f"AS ({col['computed']})")
    if not col["nullable"]:
        parts.append("NOT NULL")
    if col.get("default") is not None and not col.get("auto"):
        parts.append(f"DEFAULT {norm_default(col['default'])}")
    if col.get("auto"):
        parts.append("auto-numbered")
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------

_IDENT = r'(?:"[^"]*"|`[^`]*`|\[[^\]]*\]|[\w$#@]+)'
_VIEW_HEAD = re.compile(
    r"^\s*CREATE\s+(?:OR\s+(?:REPLACE|ALTER)\s+)?(?:ALGORITHM\s*=\s*\w+\s+)?"
    r"(?:DEFINER\s*=\s*\S+\s+)?(?:SQL\s+SECURITY\s+\w+\s+)?VIEW\s+"
    rf"(?:{_IDENT}\s*\.\s*)?{_IDENT}", re.I | re.S)
_LEADING_COMMENTS = re.compile(r"^(?:\s+|--[^\n]*\n?|/\*.*?\*/)*", re.S)


def view_rest(definition: str) -> str:
    """
    What follows a view's name (``AS SELECT …``, with any column list or
    options): MySQL, SQL Server and SQLite give the whole CREATE statement,
    PostgreSQL only the query.
    """
    text = (definition or "").strip()
    body = text[_LEADING_COMMENTS.match(text).end():]
    body = body.rstrip().rstrip(";").rstrip()
    found = _VIEW_HEAD.match(body)
    if found:
        return body[found.end():].strip()
    return "AS\n" + body


def retarget(text: str, old: Optional[str], new: Optional[str], quote_new: Callable) -> str:
    """References to the source schema (``"sales".orders``) pointed at the target's."""
    if not old or not new or old == new:
        return text
    target = quote_new(new) + "."
    # Preceded by a "." too: in db.schema.table the middle part is the schema.
    pattern = (r'(?<![\w"`\]])(?:"' + re.escape(old) + r'"|`' + re.escape(old) + r'`|\['
               + re.escape(old) + r'\]' + (r"|" + re.escape(old) if re.fullmatch(r"\w+", old)
                                            else "") + r')\s*\.')
    return re.sub(pattern, lambda _m: target, text)


def view_key(definition: str, schema: Optional[str], target: Optional[str]) -> str:
    """A definition reduced for comparing: no quotes, case or spaces; schema retargeted."""
    rest = retarget(view_rest(definition), schema, target, lambda name: name)
    return re.sub(r"\s+", "", re.sub(r'["`\[\]]', "", rest)).casefold()


# ---------------------------------------------------------------------------
# Comparing
# ---------------------------------------------------------------------------

def match(left: list, right: list) -> tuple:
    """``(pairs, only_left, only_right)`` by name: exactly first, then ignoring case."""
    rest = list(right)
    taken = set()
    pairs, unmatched = [], []
    for name in left:
        if name in rest and name not in taken:
            pairs.append((name, name))
            taken.add(name)
        else:
            unmatched.append(name)
    folded: dict = {}
    for name in rest:
        if name not in taken:
            folded.setdefault(name.casefold(), []).append(name)
    only_left = []
    for name in unmatched:
        found = folded.get(name.casefold()) or []
        if len(found) == 1:
            pairs.append((name, found[0]))
            taken.add(found[0])
            folded.pop(name.casefold())
        else:
            only_left.append(name)
    return pairs, only_left, sorted(name for name in rest if name not in taken)


def compare(source: dict, target: dict, spec: Any) -> dict:
    """The differences, each with the statements that make the target like the source."""
    return _Writer(source, target, spec).run()


class _Writer:
    def __init__(self, source: dict, target: dict, spec: Any) -> None:
        self.src, self.tgt, self.spec = source, target, spec
        self.dialect = target["dialect"]
        self.prep = self.dialect.identifier_preparer
        self.schema = target["schema"]
        self.same = source["engine"] == target["engine"]
        self.engine = target["engine"]
        self.diffs: list = []

    # -- names and pieces of SQL -----------------------------------------

    def q(self, name: str) -> str:
        return self.prep.quote(name)

    def table(self, name: str) -> str:
        return self.spec.qualified(self.prep, self.schema, name)

    def cols(self, names) -> str:
        return ", ".join(self.q(name) for name in names)

    def type_of(self, col: dict) -> Optional[str]:
        return type_for(col["type"], col["type_text"], self.dialect, self.same)

    def default_of(self, col: dict) -> Optional[str]:
        if col.get("auto") or col.get("default") is None:
            return None
        return col["default"] if self.same else portable_default(col["default"], self.engine)

    def same_type(self, a: dict, b: dict) -> bool:
        if self.same:
            return _norm(a["type_text"]) == _norm(b["type_text"])
        kinds = {family(a["type"]), family(b["type"])}
        if len(kinds) == 1:
            return True
        # SQL Server has no JSON type: JSON is kept as text there.
        engines = {self.src["engine"], self.engine}
        if "mssql" in engines and kinds == {("json",), ("text",)}:
            return True
        # DuckDB keeps no VARCHAR length: VARCHAR(20) elsewhere is its VARCHAR.
        if "duckdb" in engines and {kind[0] for kind in kinds} == {"string", "text"}:
            return True
        # MySQL and SQLite have no UUID type: a UUID is CHAR(32) there, as SQLAlchemy writes it.
        if engines & {"mysql", "sqlite"} and kinds == {("char", 32), ("uuid",)}:
            return True
        mine = self.type_of(a)
        return mine is not None and \
            _norm(_COLLATE.sub("", mine)) == _norm(_COLLATE.sub("", b["type_text"]))

    def mysql_fk_index(self, ix: dict, tbl: dict, engine: str) -> bool:
        """
        MySQL adds an index for every foreign key that has none; other engines
        do not, so between engines it is no difference.
        """
        return (engine == "mysql" and not self.same and not ix["unique"]
                and any(_fold(fk["columns"]) == _fold(ix["columns"]) for fk in tbl["fks"]))

    def defaults_differ(self, a: dict, b: dict) -> bool:
        if a["auto"] or b["auto"]:
            return False
        if self.same:
            return norm_default(a["default"]) != norm_default(b["default"])
        mine = portable_default(a["default"], self.engine)
        theirs = portable_default(b["default"], self.engine)
        if (a["default"] is not None and mine is None) or (b["default"] is not None and theirs is None):
            return False        # an expression: not comparable across engines
        return mine != theirs

    def column_def(self, col: dict, default: Optional[str] = None, inline_pk: bool = False) -> str:
        if inline_pk:
            return f"{self.q(col['name'])} INTEGER PRIMARY KEY"
        parts = [self.q(col["name"]), self.type_of(col) or f"/* {col['type_text']}? */"]
        if col["auto"] and not self.spec.identity_inline_pk and self.spec.identity_clause:
            parts.append(self.spec.identity_clause)
        if default is not None and not col["auto"]:
            parts.append(f"DEFAULT {default}")
        if not col["nullable"]:
            parts.append("NOT NULL")
        return " ".join(parts)

    def fk_clause(self, fk: dict, columns: list) -> str:
        ref = self.spec.qualified(self.prep, fk["ref_schema"] or self.schema, fk["ref_table"])
        text = (("CONSTRAINT " + self.q(fk["name"]) + " ") if fk["name"] else "") + \
            f"FOREIGN KEY ({self.cols(columns)}) REFERENCES {ref} ({self.cols(fk['ref_columns'])})"
        if fk["on_delete"] and self.spec.fk_actions:
            text += f" ON DELETE {fk['on_delete']}"
        if fk["on_update"] and self.spec.fk_actions:
            text += f" ON UPDATE {fk['on_update']}"
        return text

    def create_table(self, tbl: dict, name: str, defaults: Callable[[dict], Optional[str]],
                     fks_inline: bool) -> tuple:
        """``(statement, notes)``: the table without its indexes (and FKs unless inline)."""
        notes = []
        pk = tbl["pk"]["columns"]
        inline = None
        if self.spec.identity_inline_pk and len(pk) == 1:
            auto = [col for col in tbl["columns"] if col["auto"] and col["name"] == pk[0]]
            inline = auto[0]["name"] if auto else None
        lines = []
        for col in tbl["columns"]:
            if col["computed"]:
                notes.append(f"computed column {col['name']} left out: add it by hand")
                continue
            if self.type_of(col) is None:
                notes.append(f"{col['name']}: {self.tgt['label']} has no type for {col['type_text']}")
            lines.append(self.column_def(col, defaults(col), inline_pk=col["name"] == inline))
        if pk and not inline:
            lines.append(f"PRIMARY KEY ({self.cols(pk)})")
        if fks_inline:
            lines += [self.fk_clause(fk, fk["columns"]) for fk in tbl["fks"]]
        if not self.spec.fk_actions and any(fk["on_delete"] or fk["on_update"]
                                            for fk in tbl["fks"]):
            notes.append(f"ON DELETE / ON UPDATE left out: {self.tgt['label']} has no "
                         "foreign key actions")
        body = ",\n  ".join(lines)
        return f"CREATE TABLE {self.table(name)} (\n  {body}\n)", notes

    def create_index(self, table: str, ix: dict, columns: list) -> str:
        name = ix["name"] or ("ux_" if ix["unique"] else "ix_") + "_".join([table] + columns)[:60]
        unique = "UNIQUE " if ix["unique"] else ""
        return f"CREATE {unique}INDEX {self.q(name)} ON {self.table(table)} ({self.cols(columns)})"

    def drop_index(self, table: str, ix: dict) -> str:
        if ix["constraint"]:
            return self.spec.sql_drop_constraint(self.prep, self.schema, table, ix["name"], "unique")
        return self.spec.sql_drop_index(self.prep, self.schema, table, ix["name"])

    def add(self, group: str, kind: str, name: str, change: str, detail: str, *,
            source: str = "", target: str = "", sql: list = (), notes: list = (),
            items: list = ()) -> dict:
        diff = {"group": group, "kind": kind, "name": name, "change": change,
                "detail": detail, "source": source, "target": target,
                "sql": [[phase, statement] for phase, statement in sql],
                "manual": "; ".join(notes), "items": list(items)}
        if notes:
            first = min((phase for phase, _s in sql), default=P_ALTER_COLUMN)
            where = group if kind in ("table", "view") else f"{group}.{name}"
            diff["sql"].insert(0, [first, "-- " + where + ": " + "; ".join(notes)])
        self.diffs.append(diff)
        return diff

    # -- the walk ----------------------------------------------------------

    def run(self) -> dict:
        src, tgt = self.src["tables"], self.tgt["tables"]
        pairs, only_src, only_tgt = match(list(src), list(tgt))
        same_tables = 0
        for rank, name in enumerate(_dependency_order([src[name] for name in only_src])):
            self.new_table(src[name], rank)
        for name_s, name_t in pairs:
            before = len(self.diffs)
            self.table_pair(src[name_s], tgt[name_t])
            same_tables += len(self.diffs) == before
        for name in only_tgt:
            self.dropped_table(tgt[name])
        same_views = self.views()
        order = {"add": 0, "alter": 1, "drop": 2}
        self.diffs.sort(key=lambda d: (d["group"].casefold(), _KIND_ORDER.get(d["kind"], 9),
                                       d["name"].casefold(), order.get(d["change"], 3)))
        for number, diff in enumerate(self.diffs, 1):
            diff["id"] = f"d{number}"
        notes = []
        if not self.same:
            notes.append(f"{self.src['label']} and {self.tgt['label']} are different engines: "
                         "types are compared by kind, defaults only when they are plain values, "
                         "and view definitions not at all.")
        if self.spec.rebuilds_tables:
            notes.append("SQLite cannot change a column, key or foreign key in place: such a "
                         "table is rebuilt (new table, rows copied, old one dropped).")
        if not self.spec.ddl_transactional:
            notes.append(f"{self.tgt['label']} commits each DDL statement as it runs: a "
                         "failure halfway leaves the earlier ones done.")
        if self.src["truncated"] or self.tgt["truncated"]:
            notes.append(f"Only the first {MAX_TABLES} tables and {MAX_VIEWS} views of a "
                         "schema are compared.")
        return {"diffs": self.diffs, "same_tables": same_tables, "same_views": same_views,
                "phases": list(PHASES), "notes": notes, "batches": bool(self.spec.script_batches),
                "tables": [len(src), len(tgt)], "views": [len(self.src["views"]),
                                                          len(self.tgt["views"])]}

    def new_table(self, tbl: dict, rank: int = 0) -> None:
        name = tbl["name"]
        inline = self.spec.rebuilds_tables or not self.spec.can_alter_keys
        create, notes = self.create_table(tbl, name, self.default_of, fks_inline=inline)
        # Referenced tables first: a foreign key written inline needs its table.
        sql = [(P_CREATE_TABLE + min(rank, 999) / 1000, create)]
        for ix in tbl["indexes"]:
            if ix["expression"]:
                notes.append(f"expression index {ix['name']} left out: add it by hand")
            else:
                sql.append((P_CREATE_INDEX, self.create_index(name, ix, ix["columns"])))
        if not inline:
            sql += [(P_ADD_FK, f"ALTER TABLE {self.table(name)} ADD "
                               + self.fk_clause(fk, fk["columns"])) for fk in tbl["fks"]]
        self.add(name, "table", name, "add", "Only in the source", source=_summary(tbl),
                 sql=sql, notes=notes)

    def dropped_table(self, tbl: dict) -> None:
        name = tbl["name"]
        sql = []
        if not self.spec.rebuilds_tables:
            sql += [(P_DROP_FK, self.spec.sql_drop_constraint(self.prep, self.schema, name,
                                                                fk["name"], "fk"))
                    for fk in tbl["fks"] if fk["name"]]
        sql.append((P_DROP_TABLE, f"DROP TABLE {self.table(name)}"))
        self.add(name, "table", name, "drop", "Only in the target", target=_summary(tbl), sql=sql)

    def table_pair(self, s: dict, t: dict) -> None:
        name = t["name"]
        target = self.table(name)
        rebuild: list = []          # SQLite: why the table must be rebuilt
        start = len(self.diffs)
        sc = {col["name"]: col for col in s["columns"]}
        tc = {col["name"]: col for col in t["columns"]}
        pairs, only_s, only_t = match(list(sc), list(tc))
        rename = {a.casefold(): b for a, b in pairs}

        def to_target(columns: list) -> list:
            return [rename.get(col.casefold(), col) for col in columns]

        # DuckDB: a table an index or a foreign key depends on keeps its columns as they are.
        pinned = self.spec.alter_blocked_by_dependents and (bool(t["indexes"]) or any(
            fk["ref_table"].casefold() == name.casefold() and not fk["ref_schema"]
            for other in self.tgt["tables"].values() for fk in other["fks"]))
        pinned_note = (f"{self.tgt['label']} cannot change this column while an index or a "
                       "foreign key depends on the table: rebuild it by hand")
        used_t = {c.casefold() for c in t["pk"]["columns"]}
        for ix in t["indexes"]:
            used_t |= set(_fold(ix["columns"]))
        for fk in t["fks"]:
            used_t |= set(_fold(fk["columns"]))

        for col_name in only_s:
            col = sc[col_name]
            notes = []
            default = self.default_of(col)
            if col["computed"]:
                notes.append("a computed column: add it by hand")
            if not col["nullable"] and default is None and not col["auto"]:
                notes.append("NOT NULL without a default fails if the table has rows")
            if self.spec.rebuilds_tables and (col["auto"] or col["computed"]
                                              or (not col["nullable"] and default is None)):
                rebuild.append(f"add column {col_name}")
            sql = [] if col["computed"] else [
                (P_ADD_COLUMN, f"ALTER TABLE {target} {self.spec.add_column_keyword} "
                               f"{self.column_def(col, default)}")]
            self.add(name, "column", col_name, "add", "Only in the source", source=describe(col),
                     sql=sql, notes=notes)
        for col_name in only_t:
            col = tc[col_name]
            if self.spec.rebuilds_tables and col_name.casefold() in used_t:
                rebuild.append(f"drop column {col_name}")
            sql = [(P_DROP_COLUMN, statement) for statement in
                   self.spec.sql_script_before_drop_column(self.prep, self.schema, name, col_name)]
            sql.append((P_DROP_COLUMN, f"ALTER TABLE {target} DROP COLUMN {self.q(col_name)}"))
            notes = [pinned_note] if pinned else []
            self.add(name, "column", col_name, "drop", "Only in the target", target=describe(col),
                     sql=[] if pinned else sql, notes=notes)
        for name_s, name_t in pairs:
            a, b = sc[name_s], tc[name_t]
            changed, parts, notes = set(), [], []
            new_type = self.type_of(a)
            if not self.same_type(a, b):
                changed.add("type")
                parts.append(f"type {b['type_text']} → {new_type or a['type_text']}")
                if new_type is None:
                    notes.append(f"{self.tgt['label']} has no type for {a['type_text']}")
            if a["nullable"] != b["nullable"]:
                changed.add("nullable")
                parts.append("NOT NULL → NULL" if a["nullable"] else "NULL → NOT NULL")
            if self.defaults_differ(a, b):
                changed.add("default")
                parts.append(f"default {norm_default(b['default']) or 'none'} → "
                             f"{norm_default(a['default']) or 'none'}")
            if a["auto"] != b["auto"] and (self.same or not (a.get("rowid") or b.get("rowid"))):
                parts.append("auto-numbering " + ("added" if a["auto"] else "removed"))
                notes.append("change the auto-numbering by hand")
            if bool(a["computed"]) != bool(b["computed"]) or (
                    self.same and a["computed"] and _norm(a["computed"]) != _norm(b["computed"])):
                parts.append("computed expression differs")
                notes.append("change the computed column by hand")
            if not parts:
                continue
            sql = []
            if pinned and changed & {"type", "nullable"}:
                notes.append(pinned_note)
                changed = changed - {"type", "nullable"}
            if changed and new_type is not None:
                if self.spec.rebuilds_tables:
                    rebuild.append(f"change column {name_t}")
                else:
                    new = {"type": new_type, "nullable": a["nullable"],
                           "default": self.default_of(a), "auto": a["auto"]}
                    old = {"type": b["type_text"], "nullable": b["nullable"],
                           "default": b["default"], "auto": b["auto"]}
                    sql = [(P_ALTER_COLUMN, statement) for statement in self.spec.sql_alter_column(
                        self.prep, self.schema, name, name_t, new, old, changed)]
            self.add(name, "column", name_t, "alter", " · ".join(parts), source=describe(a),
                     target=describe(b), sql=sql, notes=notes)

        # Primary key
        sp, tp = s["pk"]["columns"], t["pk"]["columns"]
        if _fold(sp) != _fold(tp):
            sql, notes = [], []
            if self.spec.rebuilds_tables:
                rebuild.append("primary key")
            elif not self.spec.can_alter_keys:
                notes.append(f"{self.tgt['label']} cannot change an existing table's primary "
                             "key: rebuild the table by hand")
            elif tp and not (t["pk"]["name"] or self.engine == "mysql"):
                notes.append("the primary key has no name: change it by hand")
            else:
                pieces = self.spec.sql_change_primary_key(
                    self.prep, self.schema, name, t["pk"]["name"] or "", to_target(sp), tp)
                sql = [(P_DROP_PK if when == "drop" else P_ADD_PK, statement)
                       for when, statement in pieces]
                if not pieces:
                    notes.append(f"{self.tgt['label']} cannot leave a table without a primary "
                                 "key: change it by hand")
            change = "add" if not tp else "drop" if not sp else "alter"
            self.add(name, "pk", t["pk"]["name"] or s["pk"]["name"] or "PRIMARY KEY", change,
                     "Primary key " + {"add": "only in the source", "drop": "only in the target",
                                       "alter": "on different columns"}[change],
                     source=", ".join(sp), target=", ".join(tp), sql=sql, notes=notes)

        # Indexes and unique constraints, by their columns
        s_ix = {(tuple(c.casefold() for c in to_target(ix["columns"])), ix["unique"]): ix
                for ix in s["indexes"]}
        t_ix = {_ix_key(ix): ix for ix in t["indexes"]}
        for key, ix in s_ix.items():
            if key in t_ix or self.mysql_fk_index(ix, s, self.src["engine"]):
                continue
            label = ix["name"] or ", ".join(ix["columns"])
            notes, sql = [], []
            if ix["expression"]:
                notes.append("an expression index: create it by hand")
            else:
                sql = [(P_CREATE_INDEX, self.create_index(name, ix, to_target(ix["columns"])))]
            self.add(name, "index", label, "add", "Only in the source",
                     source=_ix_text(ix), sql=sql, notes=notes)
        for key, ix in t_ix.items():
            if key in s_ix or self.mysql_fk_index(ix, t, self.engine):
                continue
            label = ix["name"] or ", ".join(ix["columns"])
            sql = []
            if ix["constraint"] and self.spec.rebuilds_tables:
                rebuild.append(f"drop unique constraint on {', '.join(ix['columns'])}")
            elif ix["name"]:
                sql = [(P_DROP_INDEX, self.drop_index(name, ix))]
            self.add(name, "index", label, "drop", "Only in the target", target=_ix_text(ix),
                     sql=sql)

        # Foreign keys, by their columns and what they reference
        def fk_key(fk: dict, columns: list) -> tuple:
            return (_fold(columns), (fk["ref_schema"] or "").casefold(),
                    fk["ref_table"].casefold(), _fold(fk["ref_columns"]))

        s_fk = {fk_key(fk, to_target(fk["columns"])): fk for fk in s["fks"]}
        t_fk = {fk_key(fk, fk["columns"]): fk for fk in t["fks"]}
        for key, fk in s_fk.items():
            other = t_fk.get(key)
            if other and (not self.spec.fk_actions or (other["on_delete"], other["on_update"])
                          == (fk["on_delete"], fk["on_update"])):
                continue
            sql = []
            if other and other["name"]:
                sql.append((P_DROP_FK, self.spec.sql_drop_constraint(
                    self.prep, self.schema, name, other["name"], "fk")))
            sql.append((P_ADD_FK, f"ALTER TABLE {target} ADD "
                                  + self.fk_clause(fk, to_target(fk["columns"]))))
            notes = []
            if self.spec.rebuilds_tables:
                rebuild.append("foreign key on " + ", ".join(fk["columns"]))
                sql = []
            elif not self.spec.can_alter_keys:
                notes.append(f"{self.tgt['label']} cannot add a foreign key to an existing "
                             "table: rebuild the table by hand")
                sql = []
            self.add(name, "fk", fk["name"] or other and other["name"] or ", ".join(fk["columns"]),
                     "alter" if other else "add",
                     "ON DELETE / ON UPDATE differ" if other else "Only in the source",
                     source=_fk_text(fk), target=_fk_text(other) if other else "", sql=sql,
                     notes=notes)
        for key, fk in t_fk.items():
            if key in s_fk:
                continue
            sql, notes = [], []
            if self.spec.rebuilds_tables or not fk["name"]:
                rebuild.append("drop foreign key on " + ", ".join(fk["columns"]))
            elif not self.spec.can_alter_keys:
                notes.append(f"{self.tgt['label']} cannot drop a foreign key from an existing "
                             "table: rebuild the table by hand")
            else:
                sql = [(P_DROP_FK, self.spec.sql_drop_constraint(self.prep, self.schema, name,
                                                                 fk["name"], "fk"))]
            self.add(name, "fk", fk["name"] or ", ".join(fk["columns"]), "drop",
                     "Only in the target", target=_fk_text(fk), sql=sql, notes=notes)

        if rebuild and self.spec.rebuilds_tables:
            self.rebuild(s, t, start, pairs, rebuild)

    def rebuild(self, s: dict, t: dict, start: int, pairs: list, reasons: list) -> None:
        """SQLite: this table's differences become one rebuild to the source's shape."""
        folded = self.diffs[start:]
        del self.diffs[start:]
        name = t["name"]
        temp = f"_bk_new_{name}"
        t_cols = {col["name"]: col for col in t["columns"]}
        s_cols = {col["name"] for col in s["columns"] if not col["computed"]}
        copied = [(a, b) for a, b in pairs if a in s_cols]

        def defaults(col: dict) -> Optional[str]:
            if self.same:
                return self.default_of(col)
            mine = self.default_of(col)
            if mine is not None:
                return mine
            other = next((b for a, b in pairs if a == col["name"]), None)
            return None if other is None or t_cols[other]["auto"] else t_cols[other]["default"]

        create, notes = self.create_table(s, temp, defaults, fks_inline=True)
        rename = {a.casefold(): b for a, b in pairs}
        sql = [(P_ALTER_COLUMN, "PRAGMA foreign_keys = OFF"), (P_ALTER_COLUMN, create)]
        if copied:
            sql.append((P_ALTER_COLUMN,
                        f"INSERT INTO {self.table(temp)} ({self.cols(a for a, _b in copied)}) "
                        f"SELECT {self.cols(b for _a, b in copied)} FROM {self.table(name)}"))
        # Legacy mode: a view on the table would otherwise fail the rename
        # (its table is gone for that moment).
        sql += [(P_ALTER_COLUMN, f"DROP TABLE {self.table(name)}"),
                (P_ALTER_COLUMN, "PRAGMA legacy_alter_table = ON"),
                (P_ALTER_COLUMN, f"ALTER TABLE {self.table(temp)} RENAME TO {self.q(name)}"),
                (P_ALTER_COLUMN, "PRAGMA legacy_alter_table = OFF")]
        for ix in s["indexes"]:
            if ix["expression"]:
                notes.append(f"expression index {ix['name']} left out: add it by hand")
            else:
                cols = [rename.get(c.casefold(), c) for c in ix["columns"]]
                sql.append((P_ALTER_COLUMN, self.create_index(name, ix, cols)))
        sql += [(P_ALTER_COLUMN, "PRAGMA foreign_keys = ON"),
                (P_ALTER_COLUMN, f"PRAGMA foreign_key_check({self.q(name)})")]
        notes.append("rebuilt: triggers on the table are dropped with it")
        items = [f"{d['kind']} {d['name']}: {d['detail']}" for d in folded]
        self.add(name, "table", name, "alter",
                 f"Rebuilt ({len(folded)} change{'s' if len(folded) != 1 else ''}: "
                 + ", ".join(dict.fromkeys(reasons)) + ")",
                 source=_summary(s), target=_summary(t), sql=sql, notes=notes, items=items)

    def views(self) -> int:
        src, tgt = self.src["views"], self.tgt["views"]
        pairs, only_s, only_t = match(list(src), list(tgt))
        same = 0
        for name in only_s:
            self.add(name, "view", name, "add", "Only in the source",
                     source=_first_line(src[name]), **self.view_sql(name, src[name], replace=False))
        for name in only_t:
            self.add(name, "view", name, "drop", "Only in the target",
                     target=_first_line(tgt[name]),
                     sql=[(P_DROP_VIEW, f"DROP VIEW {self.table(name)}")])
        for name_s, name_t in pairs:
            if not self.same or view_key(src[name_s], self.src["schema"], self.schema) == \
                    view_key(tgt[name_t], self.schema, self.schema):
                same += 1
                continue
            self.add(name_t, "view", name_t, "alter", "Definitions differ",
                     source=_first_line(src[name_s]), target=_first_line(tgt[name_t]),
                     **self.view_sql(name_t, src[name_s], replace=True))
        return same

    def view_sql(self, name: str, definition: str, replace: bool) -> dict:
        rest = retarget(view_rest(definition), self.src["schema"], self.schema,
                        self.prep.quote_schema)
        target = self.table(name)
        if not self.same:
            lines = f"CREATE VIEW {target} {rest}".splitlines()
            return {"sql": [(P_VIEW, "\n".join("-- " + line for line in lines))],
                    "notes": [f"written for {self.src['label']}: check it, then remove the "
                              "comment marks"]}
        notes = []
        if self.engine == "mssql" and (self.src["schema"] or "") != (self.schema or ""):
            notes.append("names the definition leaves unqualified still mean the default schema")
        if replace and self.spec.replace_view:
            return {"sql": [(P_VIEW, f"{self.spec.replace_view} {target} {rest}")], "notes": notes}
        sql = [(P_VIEW, f"DROP VIEW IF EXISTS {target}")] if replace else []
        return {"sql": sql + [(P_VIEW, f"CREATE VIEW {target} {rest}")], "notes": notes}


def _dependency_order(tables: list) -> list:
    """Table names, each after the tables its foreign keys reference (cycles: as they come)."""
    names = {tbl["name"] for tbl in tables}
    refs = {tbl["name"]: [fk["ref_table"] for fk in tbl["fks"]
                          if fk["ref_table"] in names and not fk["ref_schema"]
                          and fk["ref_table"] != tbl["name"]] for tbl in tables}
    done: list = []
    state: dict = {}

    def visit(name: str) -> None:
        if state.get(name):
            return              # done, or on the path (a cycle)
        state[name] = 1
        for ref in refs[name]:
            visit(ref)
        done.append(name)
        state[name] = 2

    for tbl in tables:
        visit(tbl["name"])
    return done


def _summary(tbl: dict) -> str:
    parts = [f"{len(tbl['columns'])} column{'s' if len(tbl['columns']) != 1 else ''}"]
    if tbl["pk"]["columns"]:
        parts.append("key " + ", ".join(tbl["pk"]["columns"]))
    if tbl["indexes"]:
        parts.append(f"{len(tbl['indexes'])} index{'es' if len(tbl['indexes']) != 1 else ''}")
    if tbl["fks"]:
        parts.append(f"{len(tbl['fks'])} foreign key{'s' if len(tbl['fks']) != 1 else ''}")
    return " · ".join(parts)


def _ix_text(ix: dict) -> str:
    kind = "unique constraint" if ix["constraint"] else "unique index" if ix["unique"] else "index"
    return f"{kind} ({', '.join(ix['columns'])})"


def _fk_text(fk: dict) -> str:
    ref = (fk["ref_schema"] + "." if fk["ref_schema"] else "") + fk["ref_table"]
    text = f"({', '.join(fk['columns'])}) → {ref} ({', '.join(fk['ref_columns'])})"
    if fk["on_delete"]:
        text += f" ON DELETE {fk['on_delete']}"
    if fk["on_update"]:
        text += f" ON UPDATE {fk['on_update']}"
    return text


def _first_line(definition: str) -> str:
    text = " ".join(view_rest(definition).split())
    return text[:200] + ("…" if len(text) > 200 else "")
