"""Pure logic: value conversion, script splitting, SQLite confinement, versions."""
from __future__ import annotations

import datetime as dt
import decimal
import re
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_keys_round_trip_losslessly():
    from services.values import decode_key, encode_key

    for value in (None, True, 0, 2**63 - 1, -5, 1.5, decimal.Decimal("12345678901234567890.123"),
                  dt.datetime(2026, 10, 24, 13, 45, 1, 123456), dt.date(2026, 1, 2),
                  dt.time(9, 30), dt.timedelta(seconds=90.5), uuid.uuid4(), b"\x00\xffbin",
                  "O'Brien; 🦎"):
        assert decode_key(encode_key(value)) == value
    with pytest.raises(ValueError):
        decode_key(["?", "x"])


def test_cells_are_text_and_keep_precision():
    from services.values import MAX_BINARY_BYTES, cell

    assert cell(None) == (None, False)
    assert cell(9007199254740993) == ("9007199254740993", False)
    assert cell(decimal.Decimal("1E+2")) == ("100", False)
    assert cell(True) == ("true", False)
    assert cell({"a": [1, "é"]}) == ('{"a": [1, "é"]}', False)
    shown, cut = cell(b"\x01" * (MAX_BINARY_BYTES + 1))
    assert cut and shown.startswith("0x0101") and shown.endswith("…")


def test_coerce_by_column_type():
    from sqlalchemy import JSON, Boolean, Date, DateTime, Integer, LargeBinary, Numeric, String, Uuid

    from services.values import coerce

    assert coerce("42", Integer()) == 42
    assert coerce("true", Integer()) == 1  # MySQL BOOLEAN is TINYINT(1)
    assert coerce("no", Boolean()) is False
    assert coerce("12.50", Numeric()) == decimal.Decimal("12.50")
    assert coerce("2026-10-24 13:45:00", DateTime()) == dt.datetime(2026, 10, 24, 13, 45)
    assert coerce("0xff00", LargeBinary()) == b"\xff\x00"
    assert coerce('{"a": 1}', JSON()) == {"a": 1}
    assert coerce("  spaced ", String()) == "  spaced "
    assert coerce(None, Integer()) is None
    assert isinstance(coerce(str(uuid.uuid4()), Uuid()), uuid.UUID)
    for bad, sa_type in (("x", Integer()), ("2026-13-01", Date()), ("{", JSON())):
        with pytest.raises(ValueError, match="Must be"):
            coerce(bad, sa_type)


def test_split_keeps_offsets_and_skips_comments():
    from services import engines

    pg = engines.get("postgresql")
    sql = ("-- header\nSELECT 'a;b';\n\n/* note */\nCREATE FUNCTION f() RETURNS int AS $$ "
           "BEGIN RETURN 1; END $$ LANGUAGE plpgsql;\n  -- only a comment\n")
    pieces = pg.split(sql)
    assert [sql[p.start:p.end] for p in pieces] == [p.text for p in pieces]
    assert len(pieces) == 2 and pieces[1].text.startswith("/* note */\nCREATE FUNCTION")


def test_tsql_batches_and_statement_mode():
    from services import engines, sqlops

    ms = engines.get("mssql")
    sql = "DECLARE @x int = 1;\nSELECT @x;\nGO\n  go -- trailing\nSELECT 2;\n"
    batches = ms.split(sql)
    assert [b.text for b in batches] == ["DECLARE @x int = 1;\nSELECT @x;", "SELECT 2;"]
    # Statement mode keeps a DECLARE with the lines that use it.
    picked = sqlops.pick(ms, sql, "statement", sql.index("@x;"))
    assert [p.text for p in picked] == ["DECLARE @x int = 1;\nSELECT @x;"]
    with pytest.raises(ValueError):
        ms.split("SELECT 1\nGO 5\n")
    # Statement mode tolerates GO 5: it only runs the block at the caret.
    assert [p.text for p in ms.split_statements("SELECT 1\nGO 5\nSELECT 2")] == ["SELECT 1", "SELECT 2"]


def test_tsql_statements_split_on_blank_lines():
    from services import engines, sqlops

    ms = engines.get("mssql")
    sql = ("DECLARE @n int = 3;\nSELECT @n;\n\n"
           "-- a note\nSELECT 'one;\n\ntwo' AS s, [a\n\nb] AS q;\n\n"
           "/* outer /* inner */\n\n still comment */\nSELECT 3;\n"
           "\n\n  \n"
           "IF @n > 1\nBEGIN\n  SELECT 4;\n\n  SELECT CASE WHEN 1 = 1\n\n THEN 5 END;\nEND\n\n"
           "BEGIN TRAN;\n\nCOMMIT;\n\n"
           "SELECT (1\n\n + 1);\nGO\n"
           "CREATE OR ALTER PROCEDURE p AS\nSELECT 1;\n\nSELECT 2;\nGO\n"
           "-- comment only\n\nSELECT 6")
    pieces = ms.split_statements(sql)
    assert [sql[p.start:p.end] for p in pieces] == [p.text for p in pieces]
    assert [p.text.splitlines()[0] for p in pieces] == [
        "DECLARE @n int = 3;", "-- a note", "/* outer /* inner */", "IF @n > 1",
        "BEGIN TRAN;", "COMMIT;", "SELECT (1", "CREATE OR ALTER PROCEDURE p AS", "SELECT 6"]
    assert pieces[3].text.endswith("THEN 5 END;\nEND")
    assert pieces[7].text.endswith("SELECT 2;")
    # The caret on a blank line picks the block above, as for other engines.
    gap = sql.index("SELECT @n;\n\n") + len("SELECT @n;\n")
    assert sqlops.pick(ms, sql, "statement", gap)[0].text.startswith("DECLARE @n")
    # Script mode still sends whole GO batches.
    assert len(ms.split(sql)) == 3
    # Other engines keep splitting on ';'.
    pg = engines.get("postgresql")
    assert [p.text for p in sqlops.pick(pg, "SELECT 1;\nSELECT 2;", "statement", 12)] == ["SELECT 2;"]


def test_statement_at_caret():
    from services import engines, sqlops

    pg = engines.get("postgresql")
    sql = "SELECT 1;\n\nSELECT 2;\n"
    assert sqlops.pick(pg, sql, "statement", 0)[0].text == "SELECT 1;"
    assert sqlops.pick(pg, sql, "statement", sql.index("2"))[0].text == "SELECT 2;"
    assert sqlops.pick(pg, sql, "statement", len(sql))[0].text == "SELECT 2;"  # caret after the end
    assert sqlops.pick(pg, sql, "statement", 10)[0].text == "SELECT 1;"   # blank line: the one above


def test_first_word_ignores_comments():
    from services.sqlops import first_word

    assert first_word("/* x */ -- y\n  select 1") == "SELECT"
    assert first_word("\n\nDELETE FROM t") == "DELETE"


def test_sqlite_files_stay_in_the_root(tmp_path, monkeypatch):
    from services.engines import EngineError
    from services.engines.sqlite import resolve_file

    monkeypatch.setenv("BASILISK_SQLITE_ROOT", str(tmp_path))
    (tmp_path / "a.db").write_bytes(b"")
    assert resolve_file("a.db") == tmp_path / "a.db"
    for bad in ("../basilisk.db", "/etc/passwd.db", "C:/x.db", "sub/../../x.db", "a.txt", ""):
        with pytest.raises(EngineError):
            resolve_file(bad, must_exist=False)
    with pytest.raises(EngineError, match="No such file"):
        resolve_file("missing.db")


def test_typed_sql_escapes_colons():
    from sqlalchemy.dialects import postgresql

    from services.sqlops import typed_sql

    clause = typed_sql("x::text = 'a :b'")
    assert not clause.compile(dialect=postgresql.dialect()).params
    assert str(clause) == "x::text = 'a :b'"


def test_version_matches_pyproject():
    from services.about import VERSION

    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert re.search(r'^version = "([^"]+)"', pyproject, re.M).group(1) == VERSION


def test_vendored_editor_matches_its_version_file():
    import hashlib

    vendor = ROOT / "appcode" / "vendor" / "codemirror"
    digest = hashlib.sha256((vendor / "basilisk-editor.js").read_bytes()).hexdigest()
    assert f"sha256 {digest}" in (vendor / "VERSION").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Navigator objects: pure helpers
# ---------------------------------------------------------------------------

def test_sqlite_trigger_head():
    from services.engines.sqlite import _trigger_head

    assert _trigger_head("CREATE TRIGGER t BEFORE UPDATE OF status ON orders BEGIN END") == {
        "timing": "BEFORE", "events": "UPDATE OF status"}
    assert _trigger_head("create temp trigger if not exists t after insert on x begin end") == {
        "timing": "AFTER", "events": "INSERT"}
    assert _trigger_head('CREATE TRIGGER "a b" INSTEAD  OF delete ON v BEGIN END') == {
        "timing": "INSTEAD OF", "events": "DELETE"}
    # No timing means BEFORE in SQLite.
    assert _trigger_head("CREATE TRIGGER t DELETE ON x BEGIN END")["timing"] == "BEFORE"
    assert _trigger_head(None) == {"timing": "", "events": ""}


def test_call_sql_templates():
    from services.sqlops import call_sql

    scalar = {"params": [{"name": "a", "type": "integer", "mode": "IN"},
                         {"name": "b", "type": "text", "mode": "OUT"}]}
    assert call_sql("postgresql", "function", "s.f", scalar) == (
        "SELECT s.f(\n  NULL /* a integer */\n);\n")       # OUT arguments are not passed
    assert call_sql("postgresql", "function", "s.f", {"params": [], "sets": True}) == (
        "SELECT * FROM s.f();\n")
    assert call_sql("postgresql", "procedure", "s.p", scalar) == (
        "CALL s.p(\n  NULL /* a integer */,\n  NULL /* OUT b text */\n);\n")
    assert call_sql("mysql", "procedure", "d.p", scalar) == (
        "CALL d.p(\n  NULL /* a integer */,\n  @b /* OUT b text */\n);\nSELECT @b;\n")
    tsql = {"params": [{"name": "@a", "type": "int", "mode": "IN"},
                       {"name": "@b", "type": "int", "mode": "OUTPUT"}]}
    assert call_sql("mssql", "procedure", "dbo.p", tsql) == (
        "DECLARE @out_b int;\nEXEC dbo.p\n  @a = NULL /* @a int */,\n"
        "  @b = @out_b OUTPUT /* @b int */;\nSELECT @out_b;\n")
    assert call_sql("mssql", "procedure", "dbo.p", {"params": []}) == "EXEC dbo.p;\n"


def test_mysql_create_user_hides_the_hash():
    from services.engines.mysql import _HASH

    shown = ("CREATE USER `u`@`%` IDENTIFIED WITH 'caching_sha2_password' AS "
             "'$A$005$a\\'b\\\\c' REQUIRE NONE")
    assert _HASH.sub(r"\1'…'", shown) == (
        "CREATE USER `u`@`%` IDENTIFIED WITH 'caching_sha2_password' AS '…' REQUIRE NONE")
    hexed = "CREATE USER `u`@`%` IDENTIFIED WITH 'x' AS 0x24410A REQUIRE NONE"
    assert _HASH.sub(r"\1'…'", hexed).endswith("AS '…' REQUIRE NONE")


# ---------------------------------------------------------------------------
# Object actions: services/ddl.py
# ---------------------------------------------------------------------------

def _ddl(engine: str):
    from sqlalchemy.dialects import mssql, mysql, postgresql, sqlite

    from services import engines

    # CockroachDB and DuckDB quote names as PostgreSQL does.
    dialect = {"postgresql": postgresql, "mysql": mysql, "mssql": mssql, "sqlite": sqlite,
               "cockroachdb": postgresql, "duckdb": postgresql}[engine].dialect()
    return engines.get(engine), dialect.identifier_preparer


def test_ddl_create_table_per_engine():
    from services import ddl

    columns = [{"name": "id", "type": "integer", "pk": True, "identity": True},
               {"name": "Label", "type": "varchar(50)", "not_null": True, "default": "'x'"}]
    spec, prep = _ddl("postgresql")
    assert ddl.build(spec, prep, "create_table", "sales", "", {
        "name": "New T", "columns": columns}) == [
        'CREATE TABLE sales."New T" (\n  id integer GENERATED BY DEFAULT AS IDENTITY,\n'
        '  "Label" varchar(50) DEFAULT \'x\' NOT NULL,\n  PRIMARY KEY (id)\n)']
    spec, prep = _ddl("mssql")
    assert "id integer IDENTITY(1,1)" in ddl.build(spec, prep, "create_table", "dbo", "", {
        "name": "t", "columns": columns})[0]
    spec, prep = _ddl("sqlite")
    assert ddl.build(spec, prep, "create_table", None, "", {"name": "t", "columns": [
        dict(columns[0], type="INTEGER"), columns[1]]}) == [
        "CREATE TABLE t (\n  id INTEGER PRIMARY KEY AUTOINCREMENT,\n"
        "  \"Label\" varchar(50) DEFAULT 'x' NOT NULL\n)"]
    with pytest.raises(ddl.DdlError, match="INTEGER"):   # SQLite numbers only INTEGER
        ddl.build(spec, prep, "create_table", None, "", {"name": "t", "columns": [
            dict(columns[0], type="int"), columns[1]]})
    with pytest.raises(ddl.DdlError, match="alone"):
        ddl.build(spec, prep, "create_table", None, "", {"name": "t", "columns": [
            dict(columns[0], type="INTEGER"), dict(columns[1], pk=True)]})
    with pytest.raises(ddl.DdlError, match="primary key"):
        ddl.build(spec, prep, "create_table", None, "", {"name": "t", "columns": [
            dict(columns[0], pk=False)]})
    with pytest.raises(ddl.DdlError, match="Two columns"):
        ddl.build(spec, prep, "create_table", None, "", {"name": "t", "columns": [
            columns[1], dict(columns[1], name="label")]})


def test_ddl_alter_statements_per_engine():
    from services import ddl

    def built(engine, action, schema="s", table="t", **params):
        spec, prep = _ddl(engine)
        return ddl.build(spec, prep, action, schema, table, params)[0]

    assert built("postgresql", "rename_table", new_name="u") == "ALTER TABLE s.t RENAME TO u"
    assert built("mysql", "rename_table", new_name="u") == "RENAME TABLE s.t TO s.u"
    assert built("mssql", "rename_table", new_name="it's") == (
        "EXEC sp_rename N's.t', N'it''s'")
    assert built("mssql", "rename_column", column="a b", new_name="c") == (
        "EXEC sp_rename N's.t.[a b]', N'c', N'COLUMN'")
    assert built("postgresql", "rename_column", column="a", new_name="b") == (
        "ALTER TABLE s.t RENAME COLUMN a TO b")
    assert built("sqlite", "truncate", schema=None) == "DELETE FROM t"
    assert built("postgresql", "truncate") == "TRUNCATE TABLE s.t"
    assert built("mssql", "add_column", name="n", type="int", not_null=True,
                 default="0") == "ALTER TABLE s.t ADD n int DEFAULT 0 NOT NULL"
    assert built("postgresql", "drop", kind="matview") == "DROP MATERIALIZED VIEW s.t"
    assert built("postgresql", "create_index", name="ix", columns=["a", "b"], unique=True) == (
        "CREATE UNIQUE INDEX ix ON s.t (a, b)")
    assert built("postgresql", "drop_index", index="ix") == "DROP INDEX s.ix"
    assert built("mysql", "drop_index", index="ix") == "DROP INDEX ix ON s.t"
    with pytest.raises(ddl.DdlError, match="same"):
        built("postgresql", "rename_table", new_name="t")
    with pytest.raises(ddl.DdlError, match="at least one"):
        built("postgresql", "create_index", name="ix", columns=[])


def _current(engine, columns, pk=("id",), indexes=(), fks=(), schema="s"):
    """A ``schema_compare.snapshot`` model of table t, written by hand."""
    spec, prep = _ddl(engine)
    cols = [{"name": c[0], "type": None, "type_text": c[1], "nullable": c[2], "default": c[3],
             "auto": c[4] if len(c) > 4 else False, "rowid": False, "computed": None,
             "comment": c[5] if len(c) > 5 else None} for c in columns]
    table = {"name": "t", "columns": cols, "fks": list(fks), "indexes": list(indexes),
             "pk": {"name": None, "columns": list(pk)}}
    return spec, {"engine": engine, "label": spec.label, "schema": schema,
                  "dialect": prep.dialect, "tables": {"t": table}}


def test_ddl_alter_column_per_engine():
    from services import ddl

    columns = [("id", "INTEGER", False, None, True), ("label", "VARCHAR(50)", True, "'a'"),
               ("n", "INTEGER", False, "0", False, "it's counted")]

    def altered(engine, **params):
        spec, current = _current(engine, columns)
        return ddl.alter_column(spec, current, {"table": "t", **params})[0]

    assert altered("postgresql", column="label", type="VARCHAR(200)", default="'a'") == [
        "ALTER TABLE s.t ALTER COLUMN label TYPE VARCHAR(200) USING label::VARCHAR(200)"]
    assert altered("postgresql", column="n", type="INTEGER", default="") == [
        "ALTER TABLE s.t ALTER COLUMN n DROP NOT NULL", "ALTER TABLE s.t ALTER COLUMN n DROP DEFAULT"]
    # CockroachDB: a size change needs no USING (which would rewrite the column).
    assert altered("cockroachdb", column="label", type="VARCHAR(200)", default="'a'") == [
        "SET enable_experimental_alter_column_type_general = true",
        "ALTER TABLE s.t ALTER COLUMN label SET DATA TYPE VARCHAR(200)"]
    assert "USING label::INT8" in altered("cockroachdb", column="label", type="INT8",
                                         default="'a'")[1]
    # MySQL restates the whole column, its comment included.
    assert altered("mysql", column="n", type="BIGINT", not_null=True, default="0") == [
        "ALTER TABLE s.t MODIFY COLUMN n BIGINT NOT NULL DEFAULT 0 COMMENT 'it''s counted'"]
    assert altered("mysql", column="id", type="BIGINT", not_null=True) == [
        "ALTER TABLE s.t MODIFY COLUMN id BIGINT NOT NULL AUTO_INCREMENT"]
    ms = altered("mssql", column="label", type="NVARCHAR(80)", not_null=True, default="'b'")
    assert ms[0].startswith("DECLARE @df") and ms[1:] == [
        "ALTER TABLE s.t ALTER COLUMN label NVARCHAR(80) NOT NULL",
        "ALTER TABLE s.t ADD DEFAULT 'b' FOR label"]
    sqlite = altered("sqlite", column="label", type="TEXT", default="'a'")
    assert sqlite[:2] == [ddl.FK_OFF, "BEGIN"] and sqlite[-1] == ddl.FK_ON
    assert sqlite[-2].startswith(ddl.FK_CHECK)
    assert any("label TEXT DEFAULT 'a'" in step for step in sqlite)


def test_ddl_alter_column_refusals():
    from services import ddl

    columns = [("id", "INTEGER", False, None, True), ("label", "VARCHAR(50)", True, "'a'")]
    spec, current = _current("postgresql", columns)

    def refused(match, **params):
        with pytest.raises(ddl.DdlError, match=match):
            ddl.alter_column(spec, current, {"table": "t", **params})

    refused("Nothing to change", column="label", type="varchar( 50 )", default="'a'")
    refused("primary key", column="id", type="INTEGER")
    refused("numbers itself", column="id", type="INTEGER", not_null=True, default="5")
    refused("no column", column="nope", type="INTEGER")
    refused("cannot contain", column="label", type="text; DROP TABLE t")
    refused("Type is required", column="label", type=" ")
    current["tables"]["t"]["columns"][1]["computed"] = "upper(x)"
    refused("computed", column="label", type="text")
    # DuckDB cannot change a column while an index depends on the table.
    spec, current = _current("duckdb", columns, indexes=[{"name": "ix", "columns": ["label"]}])
    with pytest.raises(ddl.DdlError, match="index or a foreign key"):
        ddl.alter_column(spec, current, {"table": "t", "column": "label", "type": "TEXT",
                                         "default": "'a'"})
    assert ddl.alter_column(spec, current, {"table": "t", "column": "label",
                                            "type": "VARCHAR(50)", "default": "'b'"})[0]


def test_ddl_sql_bits_refuse_statement_breakers():
    from services.ddl import DdlError, sql_bit

    assert sql_bit("numeric( 12, 2 )", "Type") == "numeric( 12, 2 )"
    assert sql_bit("  int[]  ", "Type") == "int[]"
    assert sql_bit("'it''s'", "Default") == "'it''s'"
    assert sql_bit("", "Default") == ""
    for bad in ("int; DROP TABLE t", "0 -- x", "0 /* x", "'open", "(1", "1)"):
        with pytest.raises(DdlError):
            sql_bit(bad, "Default")
    with pytest.raises(DdlError, match="required"):
        sql_bit(" ", "Type", required=True)


def test_duckdb_reflection_helpers():
    from sqlalchemy import types as T

    from services.engines.duckdb_dialect import DuckDBNamed, _index_columns, column_type

    assert isinstance(column_type("INTEGER"), T.Integer)
    numeric = column_type("DECIMAL(10,2)")
    assert (numeric.precision, numeric.scale) == (10, 2)
    assert column_type("TIMESTAMP WITH TIME ZONE").timezone
    assert isinstance(column_type("BLOB"), T.LargeBinary)
    assert isinstance(column_type("VARCHAR"), T.String)
    assert isinstance(column_type("INTEGER[]"), DuckDBNamed)
    assert _index_columns("[placed]") == (["placed"], [None])
    assert _index_columns('[a, "b c"]') == (["a", "b c"], [None, None])
    assert _index_columns("[lower(name), id]") == ([None, "id"], ["lower(name)", None])


def test_new_engine_literals_and_filters():
    from services.engines import get
    from ui.grid_tools import filter_sql, literal

    duck = get("duckdb")
    assert duck.sql_bytes_literal(b"\x00\xff") == "'\\x00\\xff'::BLOB"
    assert literal("duckdb", "0x00ff", "bin") == "'\\x00\\xff'::BLOB"
    assert "ILIKE" in filter_sql("duckdb", {"column": "name", "op": "contains", "value": "a"})
    crdb = get("cockroachdb")
    assert crdb.family == "postgresql" and crdb.has_matviews and crdb.savepoints
    assert duck.family == "duckdb" and not duck.uses_server and duck.identity_clause is None
