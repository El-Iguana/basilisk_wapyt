"""Data compare's pure parts: value normalising, literals, the page's rows and script."""
from __future__ import annotations

import datetime as dt
import decimal
import uuid

from services import data_compare as dc
from services.engines import get
from ui import compare_tools as ct


def test_pair_kind_prefers_the_stronger_kind():
    assert dc.pair_kind(("json",), ("text",)) == "json"
    assert dc.pair_kind(("uuid",), ("char", 32)) == "uuid"
    assert dc.pair_kind(("bool",), ("smallint",)) == "bool"
    assert dc.pair_kind(("int",), ("numeric", 10, 2)) == "number"
    assert dc.pair_kind(("string", 20), ("text",)) == "text"


def test_normalisers_make_engines_agree():
    number = dc.normaliser("number")
    assert number(decimal.Decimal("1.50")) == number(1.5) == number("1.5")
    assert number(decimal.Decimal("100")) == number(100)
    assert number(0) == number(decimal.Decimal("0.00"))
    js = dc.normaliser("json")
    assert js({"b": 1, "a": [1, 2]}) == js('{"a": [1, 2], "b": 1}')
    assert js("null") is None and js(None) is None
    ids = dc.normaliser("uuid")
    u = uuid.uuid4()
    assert ids(u) == ids(str(u)) == ids(u.hex)
    flag = dc.normaliser("bool")
    assert flag(1) is True and flag("false") is False and flag(True) is True
    when = dc.normaliser("datetime")
    utc = dt.datetime(2026, 1, 2, 3, 4, 5, tzinfo=dt.timezone.utc)
    assert when(utc) == when(dt.datetime(2026, 1, 2, 3, 4, 5)) == when("2026-01-02 03:04:05")
    clock = dc.normaliser("time")
    assert clock(dt.time(9, 30)) == clock("09:30:00.000000") == clock(dt.timedelta(hours=9, minutes=30))
    day = dc.normaliser("date")
    assert day(dt.date(2024, 3, 15)) == day("2024-03-15") == day(dt.datetime(2024, 3, 15))
    assert flag(b"\x01") is True and flag(b"\x00") is False
    assert dc.normaliser("char")("ab   ") == "ab"
    assert dc.normaliser("binary")(memoryview(b"\x00\x01")) == b"\x00\x01"


def test_literals_per_engine():
    pg, my, ms, lite = (get(name) for name in ("postgresql", "mysql", "mssql", "sqlite"))
    assert dc.literal(pg, None, ("text",), "") == "NULL"
    assert dc.literal(pg, True, ("bool",), "") == "TRUE"
    assert dc.literal(ms, True, ("bool",), "BIT") == "1"
    assert dc.literal(my, 1, ("bool",), "") == "TRUE"
    assert dc.literal(pg, decimal.Decimal("1.50"), ("numeric", 10, 2), "") == "1.50"
    assert dc.literal(pg, "it's", ("text",), "") == "'it''s'"
    assert dc.literal(my, "a\\b", ("text",), "") == "'a\\\\b'"
    assert dc.literal(ms, "x", ("string", 5), "") == "N'x'"
    assert dc.literal(pg, b"\x00\xff", ("binary",), "") == "'\\x00ff'::bytea"
    assert dc.literal(ms, b"\x00\xff", ("binary",), "") == "0x00ff"
    assert dc.literal(lite, b"\x00\xff", ("binary",), "") == "X'00ff'"
    u = uuid.UUID("12345678-1234-5678-1234-567812345678")
    assert dc.literal(pg, u, ("uuid",), "") == f"'{u}'"
    assert dc.literal(my, u, ("char", 32), "") == f"'{u.hex}'"
    assert dc.literal(pg, u.hex, ("uuid",), "") == f"'{u}'"
    when = dt.datetime(2026, 1, 2, 3, 4, 5, 123456)
    assert dc.literal(ms, when, ("datetime", False), "DATETIME") == "'2026-01-02T03:04:05.123'"
    assert dc.literal(ms, when, ("datetime", False), "DATETIME2") == "'2026-01-02T03:04:05.123456'"
    assert dc.literal(pg, when, ("datetime", False), "") == "'2026-01-02 03:04:05.123456'"
    assert dc.literal(ms, "2026-01-02 03:04:05.123456", ("datetime", False), "DATETIME") == \
        "'2026-01-02T03:04:05.123'"
    assert dc.literal(pg, dt.timedelta(hours=9, minutes=5), ("time",), "") == "'09:05:00'"
    assert dc.literal(pg, {"a": 1}, ("json",), "") == "'{\"a\": 1}'"
    assert dc.literal(pg, 5, ("text",), "") == "'5'"


RESULT = {
    "keys": 1,
    "columns": [{"target": "id"}, {"target": "name"}, {"target": "price"}],
    "counts": {"insert": 1, "update": 1, "delete": 1, "same": 7},
    "insert_before": ["SET IDENTITY_INSERT t ON"], "insert_after": ["SET IDENTITY_INSERT t OFF"],
    "rows": [
        {"id": "r1", "change": "delete", "key": "id = 9", "changed": [], "source": [],
         "target": ["9", "gone", None], "sql": "DELETE FROM t WHERE id = 9"},
        {"id": "r2", "change": "update", "key": "id = 2", "changed": ["name"],
         "source": ["2", "new", "1"], "target": ["2", "old", "1"],
         "sql": "UPDATE t SET name = 'new' WHERE id = 2"},
        {"id": "r3", "change": "insert", "key": "id = 3", "changed": [],
         "source": ["3", "x", None], "target": [], "sql": "INSERT INTO t (id) VALUES (3)"},
    ],
}


def test_data_rows_summary_and_script():
    assert ct.data_default_chosen(RESULT["rows"]) == ["r2", "r3"]
    rows = ct.data_rows(RESULT, ["r2"])
    assert [row["use"] for row in rows] == [False, True, False]
    assert rows[1]["detail"] == "name: old → new" and rows[1]["cls"] == "bk-cmp-alter"
    assert rows[2]["detail"] == "name=x · price=NULL"
    assert ct.data_summary(RESULT) == "1 to insert · 1 to update · 1 only in the target · 7 the same"
    text = ct.data_script(RESULT, ["r1", "r2", "r3"], ["head"])
    body = [line for line in text.splitlines() if line and not line.startswith("--")]
    assert body == ["DELETE FROM t WHERE id = 9;", "UPDATE t SET name = 'new' WHERE id = 2;",
                    "SET IDENTITY_INSERT t ON;", "INSERT INTO t (id) VALUES (3);",
                    "SET IDENTITY_INSERT t OFF;"]
    assert "IDENTITY_INSERT" not in ct.data_script(RESULT, ["r2"])
    assert "Nothing to run" in ct.data_script(RESULT, [])
    assert ct.data_statement_count(RESULT, ["r1", "r3"]) == 2
