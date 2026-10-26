"""The data grids' pure helpers (ui/grid_tools.py): layouts, filters, copy as, foreign keys."""
from __future__ import annotations

import pytest

from ui import grid_tools as g


def test_apply_layout_keeps_saved_order_and_appends_new_columns():
    state = {"order": ["b", "gone", "a"], "widths": {"a": 120, "b": "bad", "c": 99999},
             "hidden": ["a"]}
    assert g.apply_layout(["a", "b", "c"], state) == [
        ("b", None, False), ("a", 120, True), ("c", 2000, False)]
    assert g.apply_layout(["a", "b"], None) == [("a", None, False), ("b", None, False)]
    # Never every column hidden.
    assert [h for _n, _w, h in g.apply_layout(["a"], {"hidden": ["a"]})] == [False]


def test_merge_layout_remembers_columns_this_page_lacks():
    saved = {"order": ["a", "x", "b"], "widths": {"x": 80}, "hidden": ["x"], "frozen": 2}
    merged = g.merge_layout(saved, [{"id": "b", "width": 150, "hidden": False},
                                    {"id": "a", "width": None, "hidden": True}])
    assert merged == {"order": ["b", "a", "x"], "widths": {"x": 80, "b": 150},
                      "hidden": ["a", "x"], "frozen": 2}
    assert g.frozen_count("9") == g.MAX_FROZEN and g.frozen_count(None) == 0


@pytest.mark.parametrize("engine, expected", [
    ("postgresql", "CAST(\"name\" AS TEXT) ILIKE '%50!%!_off%' ESCAPE '!'"),
    ("mysql", "`name` LIKE '%50!%!_off%' ESCAPE '!'"),
    ("mssql", "CAST([name] AS NVARCHAR(MAX)) LIKE N'%50!%!_off%' ESCAPE '!'"),
    ("sqlite", "\"name\" LIKE '%50!%!_off%' ESCAPE '!'"),
])
def test_contains_escapes_wildcards_per_engine(engine, expected):
    assert g.filter_sql(engine, {"column": "name", "op": "contains", "value": "50%_off"}) == expected


def test_filter_sql_comparisons_and_nulls():
    assert g.filter_sql("postgresql", {"column": "id", "op": "gt", "value": "10"}, "num") == '"id" > 10'
    # Not a number: quoted, so it can never become SQL.
    assert g.filter_sql("postgresql", {"column": "id", "op": "eq", "value": "1 OR 1=1"},
                        "num") == "\"id\" = '1 OR 1=1'"
    assert g.filter_sql("mysql", {"column": "s", "op": "eq", "value": "a\\'b"}) == "`s` = 'a\\\\''b'"
    assert g.filter_sql("mssql", {"column": "s", "op": "starts", "value": "[x"}) == \
        "CAST([s] AS NVARCHAR(MAX)) LIKE N'![x%' ESCAPE '!'"
    assert g.filter_sql("sqlite", {"column": "s", "op": "null"}) == '"s" IS NULL'
    assert g.filter_sql("sqlite", {"column": "s", "op": "not_contains", "value": "a"}) == \
        "(\"s\" NOT LIKE '%a%' ESCAPE '!' OR \"s\" IS NULL)"
    with pytest.raises(ValueError):
        g.filter_sql("sqlite", {"column": "s", "op": "drop"})


def test_where_with_filters():
    filters = [{"column": "qty", "op": "ge", "value": "2"}]
    assert g.where_with_filters("sqlite", "", [], {}) == ""
    assert g.where_with_filters("sqlite", " a = 1 ", [], {}) == "a = 1"
    assert g.where_with_filters("sqlite", "a = 1 OR b = 2", filters, {"qty": "num"}) == \
        '(a = 1 OR b = 2) AND "qty" >= 2'
    assert g.where_with_filters("sqlite", "", filters, {}) == "\"qty\" >= '2'"


def test_filter_match_and_rows():
    assert g.filter_match({"op": "contains", "value": "ÄB"}, "xäbx")
    assert not g.filter_match({"op": "contains", "value": "a"}, None)
    assert g.filter_match({"op": "not_contains", "value": "a"}, None)
    assert g.filter_match({"op": "gt", "value": "9"}, "10")          # numbers as numbers
    assert not g.filter_match({"op": "gt", "value": "b"}, "a")
    assert g.filter_match({"op": "eq", "value": "1.0"}, "1")
    rows = [["1", "apple"], ["2", None], ["10", "banana"]]
    assert g.filter_rows(rows, ["id", "name"], [{"column": "id", "op": "ge", "value": "2"},
                                                {"column": "name", "op": "not_null"}]) == [2]
    assert g.filter_rows(rows, ["id"], [{"column": "missing", "op": "null"}]) == [0, 1, 2]


def test_literals_per_kind_and_engine():
    assert g.literal("postgresql", None) == "NULL"
    assert g.literal("postgresql", "42", "num") == "42"
    assert g.literal("postgresql", "4e2; x", "num") == "'4e2; x'"
    assert g.literal("mssql", "true", "bool") == "1" and g.literal("mysql", "false", "bool") == "FALSE"
    assert g.literal("postgresql", "0xdead", "bin") == "'\\xdead'"
    assert g.literal("mssql", "0xdead", "bin") == "0xdead"
    assert g.literal("sqlite", "0xBEEF", "bin") == "X'BEEF'"
    assert g.literal("mssql", "ñ", "text") == "N'ñ'"
    assert g.kind_for_type("BIGINT") == "num" and g.kind_for_type("NUMERIC(10, 2)") == "num"
    assert g.kind_for_type("BOOLEAN") == "bool" and g.kind_for_type("INTERVAL") == "text"
    assert g.kind_for_type("BYTEA") == "bin" and g.kind_for_type("VARCHAR(20)") == "text"


def test_insert_sql_and_markdown():
    sql = g.insert_sql("postgresql", '"public"."t"', ["id", "note"], [["1", "it's"], ["2", None]],
                       ["num", "text"])
    assert sql == ('INSERT INTO "public"."t" ("id", "note") VALUES (1, \'it\'\'s\');\n'
                   'INSERT INTO "public"."t" ("id", "note") VALUES (2, NULL);')
    md = g.markdown_table(["id", "a|b"], [["1", "x|y\nz"], ["2", None]], ["num", "text"])
    assert md.splitlines() == ["| id | a\\|b |", "|---:|---|", "| 1 | x\\|y<br>z |",
                               "| 2 | NULL |"]


@pytest.mark.parametrize("sql, table", [
    ("SELECT * FROM orders", "orders"),
    ('select a from "sales"."Order Items" o where x = 1', '"sales"."Order Items"'),
    ("SELECT * FROM [dbo].[x];", "[dbo].[x]"),
    ("-- note\nSELECT id FROM `shop`.`orders` ORDER BY id", "`shop`.`orders`"),
    ("SELECT 'from x' FROM y WHERE a = 'JOIN'", "y"),
    ("SELECT * FROM customers AS c", "customers"),
    ("SELECT * FROM a JOIN b ON 1 = 1", None),
    ("SELECT * FROM t1, t2", None),
    ("SELECT * FROM (SELECT 1) s", None),
    ("SELECT 1", None),
    ("UPDATE t SET a = 1", None),
])
def test_table_of_query(sql, table):
    assert g.table_of_query(sql) == table


def test_reference_where():
    fks = [{"columns": ["order_id", "sku"], "ref_columns": ["oid", "sku"]},
           {"columns": ["order_id"], "ref_columns": ["id"]}]
    fk = g.foreign_key_for(fks, "order_id")
    assert fk["ref_columns"] == ["id"]               # the single-column key first
    assert g.foreign_key_for(fks, "qty") is None
    assert g.reference_where("mysql", fk, {"order_id": "7"}, {"order_id": "num"}) == "`id` = 7"
    both = g.foreign_key_for(fks, "sku")
    assert g.reference_where("postgresql", both, {"order_id": "7", "sku": "A-1"},
                             {"order_id": "num"}) == "\"oid\" = 7 AND \"sku\" = 'A-1'"
    assert g.reference_where("postgresql", fk, {"order_id": None}, {}) is None
