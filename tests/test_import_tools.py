"""The Import CSV dialog's helpers (ui/import_tools.py), the server's option checks and plans."""
from __future__ import annotations

import pytest

from ui import import_tools as it


def test_sniff_delimiter():
    assert it.sniff_delimiter("a,b,c\n1,2,3\n") == ","
    assert it.sniff_delimiter("a;b;c\n1;2,5;3\n4;5;6\n") == ";"
    assert it.sniff_delimiter("a\tb\n1\t2\n") == "\t"
    assert it.sniff_delimiter('name|note\nx|"a, b, c"\n') == "|"
    assert it.sniff_delimiter("") == ","


def test_decode_sample_drops_the_cut_line_and_bom():
    raw = "﻿a,b\n1,2\n3,4".encode("utf-8")
    assert it.decode_sample(raw, "utf-8", cut=True) == "a,b\n1,2\n"
    assert it.decode_sample(raw, "utf-8", cut=False) == "a,b\n1,2\n3,4"
    assert it.guess_encoding("café".encode("cp1252") + b",x\n" * 3) == "cp1252"
    assert it.guess_encoding("café".encode("utf-8")) == "utf-8"
    assert it.guess_encoding("x".encode("utf-16")) == "utf-16"
    # A character cut at the very end of the sample is still UTF-8.
    assert it.guess_encoding(("a" * 10 + "é").encode("utf-8")[:-1]) == "utf-8"


def test_parse_rows_and_mapping():
    rows = it.parse_rows('Id,Full Name,e_mail\n1,"Doe, J",j@x\n\n2,"Two\nLines",\n', ",", '"')
    assert rows == [["Id", "Full Name", "e_mail"], ["1", "Doe, J", "j@x"], ["2", "Two\nLines", ""]]
    assert it.source_columns(rows, True)[1] == (1, "2: Full Name")
    assert it.source_columns(rows, False)[1] == (1, "Column 2")
    table = [{"name": "id"}, {"name": "full_name"}, {"name": "EMAIL"}, {"name": "made",
                                                                         "computed": True}]
    assert it.auto_map(table, rows, True) == {"id": 0, "full_name": 1, "EMAIL": 2}
    assert it.auto_map(table, rows, False) == {"id": 0, "full_name": 1, "EMAIL": 2}
    cells = it.preview_cells(rows, True, {"id": 0, "EMAIL": 2}, ["id", "EMAIL", "x"], "", True)
    assert cells[1] == [("value", "2"), ("null", None), ("default", None)]
    assert it.format_bytes(512) == "512 bytes" and it.format_bytes(1536) == "1.5 KB"


def test_import_options_are_checked():
    from services import csv_import

    opts = csv_import.options_from({"delimiter": "\\t", "header": "false"})
    assert opts["delimiter"] == "\t" and opts["header"] is False and opts["mode"] == "append"
    for bad in ({"delimiter": ",,"}, {"delimiter": '"'}, {"quote": "x"},
                {"quote": ";", "delimiter": ";"}, {"encoding": "ebcdic"}, {"mode": "merge"},
                {"null_text": "x" * 40}):
        with pytest.raises(csv_import.CsvError):
            csv_import.options_from(bad)
    columns = {"id": {}, "made": {"computed": True}}
    assert csv_import.mapping_from([["id", "2"]], columns) == [["id", 2]]
    for bad in ([], [["nope", 0]], [["id", 0], ["id", 1]], [["id", -1]], [["made", 0]],
                [["id"]], "id"):
        with pytest.raises(csv_import.CsvError):
            csv_import.mapping_from(bad, columns)
    assert csv_import.field_value("NULL", {"null_text": "NULL", "empty": "empty"}) is None
    assert csv_import.field_value("", {"null_text": "", "empty": "empty"}) == ""
    assert csv_import.field_value("", {"null_text": "", "empty": "null"}) is None


def test_plans_belong_to_their_user_and_are_used_once():
    from services import jobs

    plan_id = jobs.put_plan(1, {"table": "t"})
    assert jobs.take_plan(plan_id, 2) is None
    assert jobs.take_plan(plan_id, 1) == {"table": "t"}
    assert jobs.take_plan(plan_id, 1) is None
