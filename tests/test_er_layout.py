"""The ER diagram's model, layout and SVG (ui/er_layout.py)."""
from __future__ import annotations

import xml.etree.ElementTree as ET

from ui import er_layout as er

I, V = "INTEGER", "VARCHAR(20)"


def col(name, type_=I, nullable=True):
    return {"name": name, "type": type_, "nullable": nullable}


def fk(column, table, ref="id", schema=None, name=None):
    return {"name": name, "columns": [column], "ref_schema": schema, "ref_table": table,
            "ref_columns": [ref]}


MODEL = {"tables": [
    {"name": "customers", "pk": ["id"], "fks": [], "columns": [col("id", I, False), col("name", V)]},
    {"name": "orders", "pk": ["id"], "fks": [fk("customer_id", "customers", name="fk_c")],
     "columns": [col("id", I, False), col("customer_id", I, False), col("total", "NUMERIC")]},
    {"name": "products", "pk": ["sku"], "fks": [], "columns": [col("sku", V, False)]},
    {"name": "order_items", "pk": ["order_id", "sku"],
     "fks": [fk("order_id", "orders"), fk("sku", "products", "sku"),
             fk("region", "regions", schema="sales")],
     "columns": [col("order_id", I, False), col("sku", V, False), col("region")]},
    {"name": "audit_log", "pk": [], "fks": [], "columns": [col("at", "TIMESTAMP")]},
    {"name": "emp", "pk": ["id"], "fks": [fk("boss_id", "emp")],
     "columns": [col("id", I, False), col("boss_id")]},
    # A cycle: a -> b -> a.
    {"name": "a", "pk": ["id"], "fks": [fk("b_id", "b")], "columns": [col("id"), col("b_id")]},
    {"name": "b", "pk": ["id"], "fks": [fk("a_id", "a")], "columns": [col("id"), col("a_id")]},
]}


def test_nodes_add_ghosts_and_focus_keeps_neighbours():
    tables = er.nodes(MODEL)
    assert tables["sales.regions"]["ghost"] and len(tables) == 9
    focused = er.nodes(MODEL, focus="orders")
    assert sorted(focused) == ["customers", "order_items", "orders"]
    items = er.nodes(MODEL, focus="order_items")
    assert sorted(items) == ["order_items", "orders", "products", "sales.regions"]
    assert len(er.edges(focused)) == 2          # order_items' other keys lead out of view


def test_shown_columns_per_mode():
    items = er.nodes(MODEL)["order_items"]
    assert [c["name"] for c in er.shown_columns(items, "all")] == ["order_id", "sku", "region"]
    orders = er.nodes(MODEL)["orders"]
    assert [c["name"] for c in er.shown_columns(orders, "keys")] == ["id", "customer_id"]
    assert er.shown_columns(orders, "none") == []
    # Keys mode says how many are left out, on one line.
    assert er.box_size(orders, "keys")[1] <= er.box_size(orders, "all")[1]
    assert er.box_size(orders, "none")[1] == er.HEAD_H


def test_layout_parents_left_no_overlaps_and_ghosts():
    tables = er.nodes(MODEL)
    boxes = er.layout(tables, "all")
    assert set(boxes) == set(tables)
    assert boxes["customers"]["x"] < boxes["orders"]["x"] < boxes["order_items"]["x"]
    # products is only referenced by order_items: it sits next to it, not at the far left.
    assert boxes["products"]["x"] == boxes["orders"]["x"]
    for one in boxes:
        for other in boxes:
            if one < other:
                a, b = boxes[one], boxes[other]
                apart = (a["x"] + a["w"] <= b["x"] or b["x"] + b["w"] <= a["x"]
                         or a["y"] + a["h"] <= b["y"] or b["y"] + b["h"] <= a["y"])
                assert apart, (one, other)
    # The cycle is laid out side by side, not lost.
    assert boxes["a"]["x"] != boxes["b"]["x"]
    resized = er.resize(tables, "none", boxes)
    assert resized["orders"]["x"] == boxes["orders"]["x"]
    assert resized["orders"]["h"] == er.HEAD_H


def test_edges_paths_and_markers():
    tables = er.nodes(MODEL)
    boxes = er.layout(tables, "all")
    links = er.edges(tables)
    assert len(links) == 7
    to_customers = next(e for e in links if e["parent"] == "customers")
    path = er.edge_path(tables, "all", boxes, to_customers)
    start = [float(v) for v in path[1:].split(" ")[0].split(",")]
    # Leaves orders on its left side, at customer_id's row.
    assert start[0] == boxes["orders"]["x"]
    assert start[1] == boxes["orders"]["y"] + er.HEAD_H + 4 + er.ROW_H * 1.5
    self_loop = next(e for e in links if e["child"] == "emp")
    assert er.edge_path(tables, "all", boxes, self_loop).startswith(
        f"M{boxes['emp']['x'] + boxes['emp']['w']:.1f}")
    assert not er.optional_fk(tables["orders"], to_customers["fk"])
    assert er.optional_fk(tables["emp"], self_loop["fk"])
    assert er.edge_title(to_customers) == "fk_c: orders (customer_id) → customers (id)"


def test_render_is_valid_svg_and_escapes_names():
    model = {"tables": [{"name": 'x<y&"z', "pk": ["id"], "fks": [], "columns": [col("id")]}]}
    tables = er.nodes(model)
    boxes = er.layout(tables, "all")
    for standalone in (False, True):
        svg = er.render(tables, "all", boxes, theme="light", standalone=standalone, title="a&b")
        root = ET.fromstring(svg.split("?>", 1)[-1])
        assert root.tag.endswith("svg")
    assert "x&lt;y&amp;" in svg and "a&amp;b" in svg
    full = er.render(er.nodes(MODEL), "keys", er.layout(er.nodes(MODEL), "keys"))
    ET.fromstring(full)
    assert full.count('class="bk-er-edge"') == 7 and "bk-er-many-opt" in full
    assert "1 more column" in full                    # orders' total, in Keys mode


def test_long_names_and_types_fit_their_box():
    model = {"tables": [{"name": "t", "pk": [], "fks": [], "columns": [
        col("a_really_long_column_name_that_goes_on_and_on", "VARCHAR(4000) ARRAY")]}]}
    tables = er.nodes(model)
    box = er.layout(tables, "all")["t"]
    assert box["w"] == er.MAX_W
    svg = er.table_svg("t", tables["t"], "all", box)
    room = int((box["w"] - er.PAD - er.TAG_W - er.PAD) / er.CHAR_W)
    texts = [t.text or "" for t in ET.fromstring(f"<g>{svg}</g>").iter() if t.tag == "text"]
    assert len(texts[1]) + len(texts[2]) + 2 <= room and texts[1].endswith("…")
