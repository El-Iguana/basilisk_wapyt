"""
The ER diagram's model, layout and drawing (milestone 3). Pure: no browser
objects, so ``tests/test_er_layout.py`` runs it in CPython; the page
(``basilisk.py``) puts the SVG in a tab and handles pan, zoom and dragging.

The model is ``sqlops.er_model``'s: tables with columns, primary key and
foreign keys (``ref_schema`` None for the same schema). A table in another
schema that something references becomes a **ghost** box (header only).

Layout, in short: tables that reference nothing sit left, the ones that
reference them to their right (layers by longest path, cycles broken), each
layer ordered by its neighbours' average position (a few sweeps), connected
groups packed into rows, unconnected tables last in a grid. Text is
monospace so box widths can be measured by character count.

Edges run from the referencing column (the "many" end, a crow's foot; a
circle when the column may be NULL) to the referenced column (the "one" end,
a bar), as cubic curves between the boxes' facing sides.
"""
from __future__ import annotations

import html
import math
from typing import Optional

CHAR_W = 7.25        # 12px monospace
ROW_H = 20
HEAD_H = 28
PAD = 10
TAG_W = 24           # the "PK" / "FK" column
MIN_W = 150
MAX_W = 360
GAP_X = 110
GAP_Y = 34
GROUP_GAP = 90
SWEEPS = 6
MODES = ("all", "keys", "none")


def _esc(value) -> str:
    return html.escape("" if value is None else str(value), quote=True)


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


def node_key(schema: Optional[str], table: str) -> str:
    """A box's key: the table's name, or ``schema.table`` for another schema's."""
    return f"{schema}.{table}" if schema else table


def fk_columns(table: dict) -> set:
    return {col for fk in table.get("fks") or [] for col in fk.get("columns") or []}


def shown_columns(table: dict, mode: str) -> list:
    """The columns a box lists: all of them, only keys, or none."""
    if table.get("ghost") or mode == "none":
        return []
    columns = table.get("columns") or []
    if mode == "keys":
        keys = set(table.get("pk") or []) | fk_columns(table)
        return [col for col in columns if col["name"] in keys]
    return list(columns)


def nodes(model: dict, focus: Optional[str] = None) -> dict:
    """
    ``{key: table}`` with ghost tables for references to other schemas. With
    ``focus``, only that table and the tables it references or that
    reference it.
    """
    tables = {t["name"]: t for t in model.get("tables") or []}
    if focus is not None and focus in tables:
        keep = {focus}
        for name, table in tables.items():
            for fk in table.get("fks") or []:
                target = node_key(fk.get("ref_schema"), fk["ref_table"])
                if name == focus:
                    keep.add(target)
                if target == focus:
                    keep.add(name)
        tables = {name: table for name, table in tables.items() if name in keep}
        ghosts_wanted = keep - set(tables)
    else:
        ghosts_wanted = None
    found = dict(tables)
    for table in tables.values():
        for fk in table.get("fks") or []:
            key = node_key(fk.get("ref_schema"), fk["ref_table"])
            if key not in found and (ghosts_wanted is None or key in ghosts_wanted):
                found[key] = {"name": key, "ghost": True, "columns": [], "pk": [], "fks": []}
    return found


def edges(tables: dict) -> list:
    """``[{child, parent, fk, index}]`` for every foreign key whose both ends are shown."""
    out = []
    for name, table in tables.items():
        for index, fk in enumerate(table.get("fks") or []):
            parent = node_key(fk.get("ref_schema"), fk["ref_table"])
            if parent in tables:
                out.append({"child": name, "parent": parent, "fk": fk, "index": index})
    return out


# ---------------------------------------------------------------------------
# Sizes and layout
# ---------------------------------------------------------------------------


def box_size(table: dict, mode: str) -> tuple:
    rows = shown_columns(table, mode)
    longest = max([len(table["name"]) * CHAR_W + 2 * PAD + 8] + [
        PAD + TAG_W + (len(col["name"]) + 2 + len(col.get("type") or "")) * CHAR_W + PAD
        for col in rows])
    width = int(min(MAX_W, max(MIN_W, math.ceil(longest))))
    hidden = len(table.get("columns") or []) - len(rows) if mode == "keys" else 0
    count = len(rows) + (1 if hidden > 0 else 0)
    height = HEAD_H + (count * ROW_H + 8 if count else 0)
    return width, height


def _components(keys: list, links: list) -> list:
    neighbours = {key: set() for key in keys}
    for child, parent in links:
        if child != parent:
            neighbours[child].add(parent)
            neighbours[parent].add(child)
    seen, groups = set(), []
    for key in keys:
        if key in seen:
            continue
        stack, group = [key], []
        seen.add(key)
        while stack:
            node = stack.pop()
            group.append(node)
            for other in sorted(neighbours[node]):
                if other not in seen:
                    seen.add(other)
                    stack.append(other)
        groups.append(sorted(group))
    return groups


def _layers(group: list, links: list) -> dict:
    """Longest-path layers, parents first; back edges of cycles are ignored."""
    parents = {key: set() for key in group}
    members = set(group)
    for child, parent in links:
        if child != parent and child in members and parent in members:
            parents[child].add(parent)
    # Drop the edges that close a cycle (found by a depth-first walk).
    state: dict = {}
    acyclic = {key: set() for key in group}

    def walk(start: str) -> None:
        stack = [(start, iter(sorted(parents[start])))]
        state[start] = 1
        while stack:
            node, it = stack[-1]
            nxt = next(it, None)
            if nxt is None:
                state[node] = 2
                stack.pop()
                continue
            if state.get(nxt) == 1:
                continue            # a back edge: leave it out
            acyclic[node].add(nxt)
            if nxt not in state:
                state[nxt] = 1
                stack.append((nxt, iter(sorted(parents[nxt]))))

    for key in group:
        if key not in state:
            walk(key)
    layer = {key: 0 for key in group}
    # Longest path: relax until nothing moves (the graph has no cycle now).
    changed = True
    while changed:
        changed = False
        for key in group:
            want = 1 + max((layer[p] for p in acyclic[key]), default=-1)
            if want != layer[key]:
                layer[key], changed = want, True
    # A table only referenced from far right moves next to its nearest
    # referencing table, so its lines do not cross the layers between.
    children: dict = {key: set() for key in group}
    for key, ups in acyclic.items():
        for parent in ups:
            children[parent].add(key)
    for key in sorted(group, key=lambda k: (-layer[k], k)):
        if children[key]:
            want = min(layer[child] for child in children[key]) - 1
            if want > layer[key]:
                layer[key] = want
    low = min(layer.values())
    return {key: value - low for key, value in layer.items()}


def _order(layers: dict, links: list) -> list:
    """Each layer's keys, ordered to shorten edges (barycentre sweeps)."""
    count = max(layers.values()) + 1
    rows = [sorted(k for k, n in layers.items() if n == i) for i in range(count)]
    neighbours: dict = {key: set() for key in layers}
    for child, parent in links:
        if child in layers and parent in layers and child != parent:
            neighbours[child].add(parent)
            neighbours[parent].add(child)
    for sweep in range(SWEEPS):
        indexes = range(1, count) if sweep % 2 == 0 else range(count - 2, -1, -1)
        for i in indexes:
            ref = i - 1 if sweep % 2 == 0 else i + 1
            place = {key: p for p, key in enumerate(rows[ref])}

            def centre(key: str, i=i, place=place) -> float:
                near = [place[n] for n in neighbours[key] if n in place]
                return sum(near) / len(near) if near else rows[i].index(key)

            rows[i] = sorted(rows[i], key=lambda key: (centre(key), key))
    return rows


def layout(tables: dict, mode: str) -> dict:
    """``{key: {"x", "y", "w", "h"}}`` for every box, plus nothing else to know."""
    sizes = {key: box_size(table, mode) for key, table in tables.items()}
    links = [(e["child"], e["parent"]) for e in edges(tables)]
    groups = _components(sorted(tables), links)
    connected = [g for g in groups if len(g) > 1]
    single = [g[0] for g in groups if len(g) == 1]
    connected.sort(key=lambda g: (-len(g), g[0]))
    placed: list = []      # (group boxes relative, width, height)
    for group in connected:
        rows = _order(_layers(group, links), links)
        boxes, x = {}, 0
        heights = [sum(sizes[k][1] for k in row) + GAP_Y * (len(row) - 1) for row in rows]
        tallest = max(heights)
        for row, height in zip(rows, heights):
            width = max(sizes[k][0] for k in row)
            y = (tallest - height) / 2
            for key in row:
                w, h = sizes[key]
                boxes[key] = {"x": x, "y": round(y), "w": w, "h": h}
                y += h + GAP_Y
            x += width + GAP_X
        placed.append((boxes, x - GAP_X, tallest))
    area = sum(w * h for w, h in sizes.values()) or 1
    wrap = max(1400, int(math.sqrt(area) * 1.9),
               max([width for _b, width, _h in placed] or [0]))
    result: dict = {}
    x = y = row_h = 0
    for boxes, width, height in placed:
        if x and x + width > wrap:
            x, y, row_h = 0, y + row_h + GROUP_GAP, 0
        for key, box in boxes.items():
            result[key] = {**box, "x": box["x"] + x, "y": box["y"] + y}
        x += width + GROUP_GAP
        row_h = max(row_h, height)
    if single:
        x, y = 0, (y + row_h + GROUP_GAP) if placed else 0
        row_h = 0
        for key in single:
            w, h = sizes[key]
            if x and x + w > wrap:
                x, y, row_h = 0, y + row_h + GAP_Y, 0
            result[key] = {"x": x, "y": y, "w": w, "h": h}
            x += w + GAP_X // 2
            row_h = max(row_h, h)
    return result


def resize(tables: dict, mode: str, boxes: dict) -> dict:
    """The same places with sizes for another column mode (dragged boxes stay put)."""
    out = {}
    for key, box in boxes.items():
        if key in tables:
            w, h = box_size(tables[key], mode)
            out[key] = {**box, "w": w, "h": h}
    return out


def bounds(boxes: dict) -> tuple:
    """``(min x, min y, max x, max y)`` around every box."""
    if not boxes:
        return 0, 0, 0, 0
    return (min(b["x"] for b in boxes.values()), min(b["y"] for b in boxes.values()),
            max(b["x"] + b["w"] for b in boxes.values()),
            max(b["y"] + b["h"] for b in boxes.values()))


# ---------------------------------------------------------------------------
# Edges
# ---------------------------------------------------------------------------


def _row_y(table: dict, mode: str, box: dict, column: Optional[str]) -> float:
    """Where a column's row is, or the header's middle when the box does not list it."""
    rows = [col["name"] for col in shown_columns(table, mode)]
    if column in rows:
        return box["y"] + HEAD_H + 4 + rows.index(column) * ROW_H + ROW_H / 2
    return box["y"] + HEAD_H / 2


def edge_path(tables: dict, mode: str, boxes: dict, edge: dict) -> str:
    child, parent = tables[edge["child"]], tables[edge["parent"]]
    a, b = boxes[edge["child"]], boxes[edge["parent"]]
    fk = edge["fk"]
    sy = _row_y(child, mode, a, (fk.get("columns") or [None])[0])
    ey = _row_y(parent, mode, b, (fk.get("ref_columns") or [None])[0])
    if edge["child"] == edge["parent"]:          # a table that references itself
        sx = a["x"] + a["w"]
        reach = 36
        return (f"M{sx:.1f},{sy:.1f} C{sx + reach:.1f},{sy:.1f} {sx + reach:.1f},{ey:.1f} "
                f"{sx:.1f},{ey:.1f}")
    a_mid, b_mid = a["x"] + a["w"] / 2, b["x"] + b["w"] / 2
    if a["x"] >= b["x"] + b["w"]:                # parent to the left
        sx, ex, sd, ed = a["x"], b["x"] + b["w"], -1, 1
    elif b["x"] >= a["x"] + a["w"]:              # parent to the right
        sx, ex, sd, ed = a["x"] + a["w"], b["x"], 1, -1
    else:                                        # overlapping columns: go round the right
        sx, ex, sd, ed = a["x"] + a["w"], b["x"] + b["w"], 1, 1
        if a_mid > b_mid:
            sx, ex, sd, ed = a["x"], b["x"], -1, -1
    reach = max(40.0, abs(ex - sx) / 2) if sd != ed else 60.0
    return (f"M{sx:.1f},{sy:.1f} C{sx + sd * reach:.1f},{sy:.1f} "
            f"{ex + ed * reach:.1f},{ey:.1f} {ex:.1f},{ey:.1f}")


def optional_fk(table: dict, fk: dict) -> bool:
    """A key whose columns may all be NULL: the row may reference nothing."""
    nullable = {col["name"]: col.get("nullable", True) for col in table.get("columns") or []}
    columns = fk.get("columns") or []
    return bool(columns) and all(nullable.get(col, True) for col in columns)


def edge_title(edge: dict) -> str:
    fk = edge["fk"]
    name = f"{fk['name']}: " if fk.get("name") else ""
    return (f"{name}{edge['child']} ({', '.join(fk.get('columns') or [])}) → "
            f"{edge['parent']} ({', '.join(fk.get('ref_columns') or [])})")


# ---------------------------------------------------------------------------
# SVG
# ---------------------------------------------------------------------------

THEMES = {
    "dark": {"bg": "#0b1220", "box": "#111827", "line": "#334155", "head": "#1f2937",
             "title": "#f8fafc", "text": "#e2e8f0", "muted": "#94a3b8", "pk": "#fbbf24",
             "fk": "#7dd3fc", "edge": "#64748b", "hot": "#f59e0b", "ghost": "#0f172a"},
    "light": {"bg": "#ffffff", "box": "#ffffff", "line": "#cbd5e1", "head": "#f1f5f9",
              "title": "#0f172a", "text": "#1e293b", "muted": "#64748b", "pk": "#b45309",
              "fk": "#0369a1", "edge": "#94a3b8", "hot": "#d97706", "ghost": "#f8fafc"},
}


def svg_style(theme: str) -> str:
    c = THEMES[theme]
    return (
        f".bk-er-box{{fill:{c['box']};stroke:{c['line']};stroke-width:1}}"
        f".bk-er-ghost .bk-er-box{{fill:{c['ghost']};stroke-dasharray:5 4}}"
        f".bk-er-head{{fill:{c['head']}}}"
        f".bk-er-title{{fill:{c['title']};font:600 12.5px ui-monospace,SFMono-Regular,Menlo,"
        f"Consolas,monospace}}"
        f".bk-er-col{{fill:{c['text']};font:12px ui-monospace,SFMono-Regular,Menlo,Consolas,"
        f"monospace}}"
        f".bk-er-type,.bk-er-more{{fill:{c['muted']};font:11.5px ui-monospace,SFMono-Regular,"
        f"Menlo,Consolas,monospace}}"
        f".bk-er-pk{{fill:{c['pk']};font:700 9.5px system-ui,sans-serif}}"
        f".bk-er-fk{{fill:{c['fk']};font:700 9.5px system-ui,sans-serif}}"
        f".bk-er-notnull{{font-weight:600}}"
        f".bk-er-edge{{fill:none;stroke:{c['edge']};stroke-width:1.4}}"
        f".bk-er-mark{{fill:none;stroke:{c['edge']};stroke-width:1.4}}"
        f".bk-er-hot .bk-er-box{{stroke:{c['hot']};stroke-width:2}}"
        f".bk-er-edge.bk-er-hot{{stroke:{c['hot']};stroke-width:2.2}}"
        f".bk-er-dim{{opacity:.28}}"
    )


def _markers(theme: str) -> str:
    edge, hot = THEMES[theme]["edge"], THEMES[theme]["hot"]
    out = []
    for suffix, colour in (("", edge), ("-hot", hot)):
        stroke = f'fill="none" stroke="{colour}" stroke-width="1.4"'
        out.append(
            # The "many" end (the path's start, pointing out of the box): a
            # crow's foot and a bar; the optional form has a circle instead.
            f'<marker id="bk-er-many{suffix}" viewBox="0 0 20 16" refX="0" refY="8" '
            f'markerWidth="20" markerHeight="16" markerUnits="userSpaceOnUse" '
            f'orient="auto"><path d="M0,1 L12,8 L0,15 M0,8 L12,8 M14,2 L14,14" '
            f'{stroke}/></marker>'
            f'<marker id="bk-er-many-opt{suffix}" viewBox="0 0 26 16" refX="0" refY="8" '
            f'markerWidth="26" markerHeight="16" markerUnits="userSpaceOnUse" '
            f'orient="auto"><path d="M0,1 L12,8 L0,15 M0,8 L12,8" {stroke}/>'
            f'<circle cx="18" cy="8" r="4" fill="{THEMES[theme]["bg"]}" stroke="{colour}" '
            f'stroke-width="1.4"/></marker>'
            # The "one" end: two bars.
            f'<marker id="bk-er-one{suffix}" viewBox="0 0 14 16" refX="14" refY="8" '
            f'markerWidth="14" markerHeight="16" markerUnits="userSpaceOnUse" orient="auto">'
            f'<path d="M3,2 L3,14 M8,2 L8,14" {stroke}/></marker>')
    return "".join(out)


def edge_svg(tables: dict, mode: str, boxes: dict, edge: dict, number: int) -> str:
    optional = optional_fk(tables[edge["child"]], edge["fk"])
    many = "bk-er-many-opt" if optional else "bk-er-many"
    return (f'<path class="bk-er-edge" data-e="{number}" data-from="{_esc(edge["child"])}" '
            f'data-to="{_esc(edge["parent"])}" d="{edge_path(tables, mode, boxes, edge)}" '
            f'marker-start="url(#{many})" marker-end="url(#bk-er-one)">'
            f'<title>{_esc(edge_title(edge))}</title></path>')


def _fit(text: str, chars: int) -> str:
    return text if len(text) <= chars else text[:max(1, chars - 1)] + "…"


def table_svg(key: str, table: dict, mode: str, box: dict) -> str:
    w, h = box["w"], box["h"]
    ghost = bool(table.get("ghost"))
    parts = [f'<g class="bk-er-table{" bk-er-ghost" if ghost else ""}" data-t="{_esc(key)}" '
             f'transform="translate({box["x"]:.0f},{box["y"]:.0f})">',
             f'<title>{_esc(key)}{" (another schema)" if ghost else ""}</title>',
             f'<rect class="bk-er-box" width="{w}" height="{h}" rx="6"/>',
             f'<path class="bk-er-head" d="M0.5,6.5 a6,6 0 0 1 6,-6 h{w - 13} a6,6 0 0 1 6,6 '
             f'v{HEAD_H - 7} h{-(w - 1)} z"/>' if h > HEAD_H else
             f'<rect class="bk-er-head" x="0.5" y="0.5" width="{w - 1}" height="{h - 1}" rx="6"/>',
             f'<text class="bk-er-title" x="{PAD}" y="18">'
             f'{_esc(_fit(key, int((w - 2 * PAD) / CHAR_W)))}</text>']
    rows = shown_columns(table, mode)
    pk, fks = set(table.get("pk") or []), fk_columns(table)
    room = int((w - PAD - TAG_W - PAD) / CHAR_W)
    for i, col in enumerate(rows):
        y = HEAD_H + 4 + i * ROW_H + 14
        tag = ""
        if col["name"] in pk:
            tag = f'<text class="bk-er-pk" x="{PAD}" y="{y - 1}">PK</text>'
        elif col["name"] in fks:
            tag = f'<text class="bk-er-fk" x="{PAD}" y="{y - 1}">FK</text>'
        # The name first; the type gets what is left of the row.
        name = _fit(col["name"], max(4, room - 6))
        type_room = room - len(name) - 2
        type_text = _fit(col.get("type") or "", type_room) if type_room >= 4 else ""
        cls = "bk-er-col" + ("" if col.get("nullable", True) else " bk-er-notnull")
        parts.append(
            f'{tag}<text class="{cls}" x="{PAD + TAG_W}" y="{y}">'
            f'{_esc(name)}</text>'
            f'<text class="bk-er-type" x="{w - PAD}" y="{y}" text-anchor="end">'
            f'{_esc(type_text)}</text>')
    hidden = len(table.get("columns") or []) - len(rows) if mode == "keys" and not ghost else 0
    if hidden > 0:
        y = HEAD_H + 4 + len(rows) * ROW_H + 14
        parts.append(f'<text class="bk-er-more" x="{PAD + TAG_W}" y="{y}">… {hidden} more '
                     f'column{"s" if hidden != 1 else ""}</text>')
    parts.append("</g>")
    return "".join(parts)


def render(tables: dict, mode: str, boxes: dict, *, theme: str = "dark",
           standalone: bool = False, title: str = "") -> str:
    """
    The diagram as SVG. In the page the root group is panned and zoomed by
    the page (``standalone=False``, no size); an export (``standalone``)
    carries its own size, background and title.
    """
    links = edges(tables)
    x0, y0, x1, y1 = bounds(boxes)
    margin = 40
    body = (f'<g class="bk-er-edges">'
            + "".join(edge_svg(tables, mode, boxes, e, i) for i, e in enumerate(links))
            + '</g><g class="bk-er-tables">'
            + "".join(table_svg(key, tables[key], mode, boxes[key]) for key in sorted(boxes))
            + "</g>")
    style = f"<style>{svg_style(theme)}</style>"
    defs = f"<defs>{style}{_markers(theme)}</defs>"
    if not standalone:
        return (f'<svg xmlns="http://www.w3.org/2000/svg" class="bk-er-svg">{defs}'
                f'<g class="bk-er-root">{body}</g></svg>')
    width, height = (x1 - x0) + 2 * margin, (y1 - y0) + 2 * margin + (30 if title else 0)
    head = (f'<text x="{x0}" y="{y0 - margin + 22}" style="font:600 15px system-ui,sans-serif;'
            f'fill:{THEMES[theme]["title"]}">{_esc(title)}</text>') if title else ""
    top = y0 - margin - (30 if title else 0)
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width:.0f}" height="{height:.0f}" '
            f'viewBox="{x0 - margin:.0f} {top:.0f} {width:.0f} {height:.0f}">{defs}'
            f'<rect x="{x0 - margin:.0f}" y="{top:.0f}" width="{width:.0f}" height="{height:.0f}" '
            f'fill="{THEMES[theme]["bg"]}"/>{head}<g>{body}</g></svg>\n')
