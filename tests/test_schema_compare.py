"""
Schema compare (services/schema_compare.py, ui/compare_tools.py) without a
server: the pure helpers, and a round trip between two SQLite files (compare,
run the script on the target, compare again: nothing left).
"""
from __future__ import annotations

import sqlite3

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects import mssql, mysql, postgresql, sqlite

from services import schema_compare as sc
from services.engines import get
from ui import compare_tools as ct

# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_match_exact_then_ignoring_case():
    pairs, left, right = sc.match(["Orders", "items", "a", "B"], ["orders", "items", "b", "B", "z"])
    assert ("items", "items") in pairs and ("B", "B") in pairs
    assert ("Orders", "orders") in pairs
    assert left == ["a"] and right == ["b", "z"]
    # Two names that differ only in case on the right: no guessing.
    pairs, left, right = sc.match(["x"], ["X", "x "])
    assert pairs == [("x", "X")]


def test_defaults_normalised_and_portable():
    assert sc.norm_default("((0))") == "0"
    assert sc.norm_default("('new')") == "'new'"
    assert sc.norm_default("(a) + (b)") == "(a) + (b)"
    assert sc.portable_default("'new'::character varying", "mysql") == "'new'"
    assert sc.portable_default("(N'it''s')", "postgresql") == "'it''s'"
    assert sc.portable_default("(N'x')", "mssql") == "N'x'"
    assert sc.portable_default("true", "mssql") == "1"
    assert sc.portable_default("now()", "mysql") is None
    assert sc.portable_default("current_timestamp", "sqlite") == "CURRENT_TIMESTAMP"
    assert sc.portable_default("-1.5", "sqlite") == "-1.5"


def test_type_families_across_engines():
    assert sc.family(postgresql.INTEGER()) == sc.family(mysql.INTEGER()) == ("int",)
    assert sc.family(mysql.TINYINT(display_width=1)) == sc.family(postgresql.BOOLEAN()) == ("bool",)
    assert sc.family(mssql.VARCHAR(20, collation="X")) == sc.family(mysql.VARCHAR(20)) \
        == ("string", 20)
    assert sc.family(mssql.NVARCHAR()) == sc.family(postgresql.TEXT()) == ("text",)
    assert sc.family(postgresql.TIMESTAMP()) == sc.family(mssql.DATETIME())
    assert sc.family(postgresql.TIMESTAMP(timezone=True)) != sc.family(postgresql.TIMESTAMP())
    assert sc.family(postgresql.NUMERIC(10, 2)) == ("numeric", 10, 2)


def test_types_written_for_another_engine():
    pg, my, ms = postgresql.dialect(), mysql.dialect(), mssql.dialect()
    assert sc.type_for(mssql.VARCHAR(20, collation="X"), "", pg, False) == "VARCHAR(20)"
    assert sc.type_for(mysql.CHAR(32), "", ms, False) == "CHAR(32)"
    assert sc.type_for(mysql.TINYINT(display_width=1), "", pg, False) == "BOOLEAN"
    assert sc.type_for(mssql.UNIQUEIDENTIFIER(), "", pg, False) == "UUID"
    assert sc.type_for(postgresql.JSONB(), "", my, False) == "JSON"
    assert sc.type_for(postgresql.ARRAY(postgresql.INTEGER()), "", my, False) is None
    # The same engine keeps the type as the source wrote it.
    assert sc.type_for(mssql.VARCHAR(20), "VARCHAR(20) COLLATE X", ms, True) == \
        "VARCHAR(20) COLLATE X"


@pytest.mark.parametrize("definition, rest", [
    (" SELECT id\n   FROM orders;", "AS\nSELECT id\n   FROM orders"),
    ("CREATE ALGORITHM=UNDEFINED DEFINER=`root`@`%` SQL SECURITY DEFINER VIEW `v` AS select 1",
     "AS select 1"),
    ("-- why\nCREATE VIEW [sales].[v] (a) WITH SCHEMABINDING AS SELECT 1 AS a",
     "(a) WITH SCHEMABINDING AS SELECT 1 AS a"),
    ('CREATE VIEW "my view" AS SELECT 2', "AS SELECT 2"),
])
def test_view_rest(definition, rest):
    assert sc.view_rest(definition) == rest


def test_retarget_points_schema_references_at_the_target():
    quote = postgresql.dialect().identifier_preparer.quote_schema
    text = 'SELECT * FROM sales.orders o JOIN "sales".items i ON i.sales = o.id JOIN [sales].x'
    out = sc.retarget(text, "sales", "Shop B", quote)
    assert out.count('"Shop B".') == 3 and "i.sales" in out
    assert sc.retarget(text, "sales", "sales", quote) == text
    assert sc.view_key("CREATE VIEW `v` AS select `a`.`x` from `a`.`t`", "a", "b") == \
        sc.view_key("CREATE VIEW `v` AS select `b`.`x` from `b`.`t`", "b", "b")


# ---------------------------------------------------------------------------
# The page's script assembly
# ---------------------------------------------------------------------------

RESULT = {
    "phases": list(sc.PHASES), "batches": False, "same_tables": 2, "same_views": 0,
    "diffs": [
        {"id": "d1", "group": "orders", "kind": "fk", "name": "fk_c", "change": "alter",
         "detail": "ON DELETE", "manual": "",
         "sql": [[sc.P_DROP_FK, "ALTER TABLE orders DROP CONSTRAINT fk_c"],
                 [sc.P_ADD_FK, "ALTER TABLE orders ADD CONSTRAINT fk_c FOREIGN KEY (c) REFERENCES c (id)"]]},
        {"id": "d2", "group": "t", "kind": "table", "name": "t", "change": "add", "detail": "",
         "manual": "", "sql": [[sc.P_CREATE_TABLE, "CREATE TABLE t (id INTEGER)"]]},
        {"id": "d3", "group": "u", "kind": "table", "name": "u", "change": "drop", "detail": "",
         "manual": "x", "sql": [[sc.P_DROP_TABLE, "-- u: x"], [sc.P_DROP_TABLE, "DROP TABLE u"]]},
    ],
}


def test_script_orders_by_phase_and_ends_statements():
    assert ct.default_chosen(RESULT["diffs"]) == ["d1", "d2"]
    text = ct.script(RESULT, ["d1", "d2", "d3"], ["Make b like a"])
    lines = [line for line in text.splitlines() if line and not line.startswith("-- ──")]
    assert lines[0] == "-- Make b like a"
    assert lines[1:] == ["ALTER TABLE orders DROP CONSTRAINT fk_c;", "CREATE TABLE t (id INTEGER);",
                         "ALTER TABLE orders ADD CONSTRAINT fk_c FOREIGN KEY (c) REFERENCES c (id);",
                         "-- u: x", "DROP TABLE u;"]
    assert "-- ── Drop foreign keys" in text and "-- ── Add foreign keys" in text
    assert ct.statement_count(RESULT, ["d1", "d3"]) == 3
    assert "Nothing to run" in ct.script(RESULT, [])


def test_script_in_batches_for_sql_server():
    text = ct.script({**RESULT, "batches": True}, ["d2"])
    assert "CREATE TABLE t (id INTEGER)\nGO" in text


def test_grid_rows_and_summary():
    rows = ct.grid_rows(RESULT["diffs"], ["d2"])
    assert [row["use"] for row in rows] == [False, True, False]
    assert rows[0]["object"] == "orders.fk_c" and rows[1]["object"] == "t"
    assert rows[2]["detail"].startswith("⚠") and "bk-cmp-manual" in rows[2]["cls"]
    assert ct.summary(RESULT) == "1 to create · 1 to change · 1 only in the target · 2 tables the same"


# ---------------------------------------------------------------------------
# Two SQLite files: compare, apply, compare again
# ---------------------------------------------------------------------------

SOURCE = """
CREATE TABLE customers (id INTEGER PRIMARY KEY, name VARCHAR(100) NOT NULL, email VARCHAR(200),
  CONSTRAINT uq_customers_email UNIQUE (email));
CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL,
  status VARCHAR(20) DEFAULT 'new', total NUMERIC(10,2),
  CONSTRAINT fk_orders_customer FOREIGN KEY (customer_id) REFERENCES customers (id) ON DELETE CASCADE);
CREATE INDEX ix_orders_status ON orders (status);
CREATE TABLE order_tags (order_id INTEGER NOT NULL, tag VARCHAR(30) NOT NULL,
  PRIMARY KEY (order_id, tag), CONSTRAINT fk_tags_order FOREIGN KEY (order_id) REFERENCES orders (id));
CREATE TABLE only_a (id INTEGER PRIMARY KEY, note VARCHAR(50));
CREATE VIEW big_orders AS SELECT id, total FROM orders WHERE total > 100;
"""
TARGET = """
CREATE TABLE customers (id INTEGER PRIMARY KEY, name VARCHAR(50), phone VARCHAR(30));
CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL,
  status VARCHAR(20) DEFAULT 'open', total NUMERIC(10,2),
  CONSTRAINT fk_orders_customer FOREIGN KEY (customer_id) REFERENCES customers (id));
CREATE INDEX ix_orders_cust ON orders (customer_id);
CREATE TABLE order_tags (order_id INTEGER NOT NULL, tag VARCHAR(30) NOT NULL, PRIMARY KEY (order_id));
CREATE TABLE only_b (id INTEGER PRIMARY KEY);
CREATE VIEW big_orders AS SELECT id, total FROM orders WHERE total > 500;
INSERT INTO customers VALUES (1, 'Ann', '555');
INSERT INTO orders VALUES (7, 1, 'open', 150);
INSERT INTO order_tags VALUES (7, 'rush');
"""


def _snap(path):
    with create_engine(f"sqlite:///{path}").connect() as conn:
        return sc.snapshot(conn, get("sqlite"), "", "")


def test_sqlite_round_trip(tmp_path):
    a, b = tmp_path / "a.db", tmp_path / "b.db"
    for path, ddl in ((a, SOURCE), (b, TARGET)):
        with sqlite3.connect(path) as conn:
            conn.executescript(ddl)
    result = sc.compare(_snap(a), _snap(b), get("sqlite"))
    found = {(d["change"], d["kind"], d["group"]) for d in result["diffs"]}
    assert found == {("alter", "table", "customers"), ("alter", "table", "orders"),
                     ("alter", "table", "order_tags"), ("add", "table", "only_a"),
                     ("drop", "table", "only_b"), ("alter", "view", "big_orders")}
    orders = next(d for d in result["diffs"] if d["group"] == "orders")
    assert orders["detail"].startswith("Rebuilt") and any("ON DELETE" in i for i in orders["items"])

    everything = [d["id"] for d in result["diffs"]]
    script = ct.script(result, everything, ["test"])
    conn = sqlite3.connect(b)
    conn.executescript(script)
    assert conn.execute("SELECT name, email FROM customers").fetchall() == [("Ann", None)]
    assert conn.execute("SELECT status, total FROM orders").fetchall() == [("open", 150)]
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    conn.close()
    again = sc.compare(_snap(a), _snap(b), get("sqlite"))
    assert again["diffs"] == [], again["diffs"]
    assert again["same_tables"] == 4 and again["same_views"] == 1


def test_sqlite_into_a_postgresql_script(tmp_path):
    """Written for another engine: types translated, the view left for a person."""
    a, b = tmp_path / "a.db", tmp_path / "empty.db"
    with sqlite3.connect(a) as conn:
        conn.executescript(SOURCE)
    sqlite3.connect(b).close()
    source = _snap(a)
    target = {**_snap(b), "engine": "postgresql", "label": "PostgreSQL",
              "dialect": postgresql.dialect(), "schema": "shop"}
    result = sc.compare(source, target, get("postgresql"))
    text = ct.script(result, [d["id"] for d in result["diffs"]])
    assert "CREATE TABLE shop.customers (\n  id INTEGER GENERATED BY DEFAULT AS IDENTITY NOT NULL,\n" in text
    assert "CREATE UNIQUE INDEX uq_customers_email ON shop.customers (email);" in text
    assert text.index("CREATE TABLE shop.orders") < text.index(
        "ALTER TABLE shop.orders ADD CONSTRAINT fk_orders_customer")
    assert "-- CREATE VIEW shop.big_orders AS SELECT" in text
    assert text.count("written for SQLite") == 1
    assert any("different engines" in note for note in result["notes"])
