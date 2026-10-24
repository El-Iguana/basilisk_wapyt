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
    picked = sqlops.pick(ms, sql, "statement", sql.index("@x;"))
    assert [p.text for p in picked] == ["SELECT @x;"]
    with pytest.raises(ValueError):
        ms.split("SELECT 1\nGO 5\n")


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
