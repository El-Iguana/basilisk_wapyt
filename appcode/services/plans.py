"""
Query plans from each engine, in one shape. Server-side only, pure functions
(``tests/test_plans.py`` feeds them captured output).

A plan node::

    {"op": "Hash Join", "object": "orders o", "detail": "Hash Cond: …",
     "cost": 17.5, "est_rows": 30.0, "act_rows": 12.0, "time_ms": 0.09,
     "loops": 1, "extra": [["Sort Method", "quicksort"], …], "children": [...]}

Missing numbers are ``None``. ``time_ms`` is the node's total time (a
per-loop time multiplied out where the engine reports it per loop). Each
engine's raw plan travels alongside for the Raw view.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any, Optional


def node(op: str, *, object: str = "", detail: str = "", cost: Optional[float] = None,
         est_rows: Optional[float] = None, act_rows: Optional[float] = None,
         time_ms: Optional[float] = None, loops: Optional[int] = None,
         extra: Optional[list] = None, children: Optional[list] = None) -> dict:
    return {"op": op, "object": object, "detail": detail, "cost": cost, "est_rows": est_rows,
            "act_rows": act_rows, "time_ms": time_ms, "loops": loops, "extra": extra or [],
            "children": children or []}


def _num(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# PostgreSQL: EXPLAIN (FORMAT JSON [, ANALYZE, BUFFERS])
# ---------------------------------------------------------------------------

_PG_DETAIL_KEYS = ("Index Cond", "Recheck Cond", "Hash Cond", "Merge Cond", "Join Filter",
                   "Filter", "Sort Key", "Group Key", "One-Time Filter", "TID Cond",
                   "Cache Key", "Presorted Key")
_PG_USED_KEYS = {"Node Type", "Plans", "Relation Name", "Alias", "Index Name", "Schema",
                 "Startup Cost", "Total Cost", "Plan Rows", "Actual Rows", "Actual Total Time",
                 "Actual Loops", "Actual Startup Time", "Join Type", "Strategy", "CTE Name",
                 "Function Name", "Parent Relationship", "Parallel Aware", "Async Capable",
                 "Plan Width", *_PG_DETAIL_KEYS}


def parse_postgresql(document: Any) -> dict:
    """``document``: the JSON EXPLAIN returns (a list with one object)."""
    import json

    if isinstance(document, str):
        document = json.loads(document)
    top = document[0] if isinstance(document, list) else document
    root = _pg_node(top["Plan"])
    summary = {"cost": root["cost"]}
    for key, name in (("Planning Time", "planning_ms"), ("Execution Time", "execution_ms")):
        if key in top:
            summary[name] = _num(top[key])
    return {"plan": root, "summary": summary}


def _pg_node(plan: dict) -> dict:
    op = plan.get("Node Type", "?")
    qualifier = plan.get("Join Type") or (plan.get("Strategy")
                                          if plan.get("Strategy") not in (None, "Plain") else None)
    if qualifier and op != "Hash":
        op = f"{op} ({qualifier})"
    target = plan.get("Relation Name") or plan.get("CTE Name") or plan.get("Function Name") or ""
    if target and plan.get("Alias") and plan.get("Alias") != target:
        target = f"{target} {plan['Alias']}"
    if plan.get("Index Name"):
        target = f"{target} using {plan['Index Name']}".strip()
    details = []
    for key in _PG_DETAIL_KEYS:
        if key in plan:
            value = plan[key]
            value = ", ".join(value) if isinstance(value, list) else value
            details.append(f"{key}: {value}")
    loops = plan.get("Actual Loops")
    per_loop = _num(plan.get("Actual Total Time"))
    extra = [[key, ", ".join(map(str, value)) if isinstance(value, list) else str(value)]
             for key, value in plan.items()
             if key not in _PG_USED_KEYS and not isinstance(value, dict)
             and value not in (0, False, None)]
    return node(
        op, object=target, detail="; ".join(details), cost=_num(plan.get("Total Cost")),
        est_rows=_num(plan.get("Plan Rows")),
        # PostgreSQL reports rows and time per loop.
        act_rows=None if plan.get("Actual Rows") is None
        else _num(plan["Actual Rows"]) * (loops or 1),
        time_ms=None if per_loop is None else per_loop * (loops or 1),
        loops=loops, extra=extra,
        children=[_pg_node(child) for child in plan.get("Plans") or []],
    )


# ---------------------------------------------------------------------------
# MySQL 8: EXPLAIN FORMAT=TREE and EXPLAIN ANALYZE (text trees)
# ---------------------------------------------------------------------------

_NUMBER = r"[-+\d.e]+"
_MY_COST = re.compile(rf"\(cost=({_NUMBER})(?: rows=({_NUMBER}))?\)")
_MY_ROWS_ONLY = re.compile(rf"\(rows=({_NUMBER})\)")
_MY_ACTUAL = re.compile(
    rf"\(actual time=({_NUMBER})\.\.({_NUMBER}) rows=({_NUMBER}) loops=(\d+)\)")
_MY_OBJECT = re.compile(r"\bon (<[^>]+>|`[^`]+`|[\w$.]+)(?: using ([\w$`]+))?")


def parse_mysql_tree(text: str) -> dict:
    roots: list = []
    stack: list = []          # (indent, node)
    for line in text.splitlines():
        if "->" not in line:
            if stack and line.strip():   # a wrapped continuation of the last node
                stack[-1][1]["detail"] += " " + line.strip()
            continue
        indent = len(line) - len(line.lstrip())
        body = line.strip()[2:].strip()
        cost = _MY_COST.search(body)
        actual = _MY_ACTUAL.search(body)
        rows_only = _MY_ROWS_ONLY.search(body)
        never = "(never executed)" in body
        label = body
        for match in (cost, actual, rows_only):
            if match:
                label = label.replace(match.group(0), "")
        label = label.replace("(never executed)", "").strip()
        op, _, detail = label.partition(": ")
        found = _MY_OBJECT.search(op)
        target = ""
        if found:
            # "Index lookup on o using ix (customer_id=c.id)": operation,
            # object, and what follows as the detail.
            target = found.group(1).strip("`")
            if found.group(2):
                target += f" using {found.group(2).strip('`')}"
            detail = " ".join(part for part in (op[found.end():].strip(), detail) if part)
            op = op[:found.start()].strip()
        loops = int(actual.group(4)) if actual else (0 if never else None)
        item = node(
            op.strip(), object=target, detail=detail.strip(),
            cost=_num(cost.group(1)) if cost else None,
            est_rows=_num(cost.group(2)) if cost and cost.group(2)
            else (_num(rows_only.group(1)) if rows_only else None),
            # MySQL reports rows and time per loop, like PostgreSQL.
            act_rows=_num(actual.group(3)) * loops if actual else (0.0 if never else None),
            time_ms=_num(actual.group(2)) * loops if actual else (0.0 if never else None),
            loops=loops,
        )
        while stack and stack[-1][0] >= indent:
            stack.pop()
        (stack[-1][1]["children"] if stack else roots).append(item)
        stack.append((indent, item))
    root = roots[0] if len(roots) == 1 else node("Query", children=roots)
    costs = [n["cost"] for n in _walk(root) if n["cost"] is not None]
    return {"plan": root, "summary": {"cost": max(costs) if costs else None}}


def parse_mysql_table(columns: list, rows: list) -> dict:
    """Classic tabular EXPLAIN (MariaDB, old MySQL): one row per table access."""
    children = []
    for row in rows:
        data = {str(col).lower(): value for col, value in zip(columns, row)}
        op = " ".join(str(part) for part in (data.get("select_type"), data.get("type")) if part)
        details = [f"{key}: {data[key]}" for key in ("possible_keys", "key", "ref", "extra")
                   if data.get(key) not in (None, "")]
        children.append(node(op or "access", object=str(data.get("table") or ""),
                             detail="; ".join(details), est_rows=_num(data.get("rows")),
                             act_rows=_num(data.get("r_rows"))))
    return {"plan": node("Query", children=children), "summary": {"cost": None}}


# ---------------------------------------------------------------------------
# SQL Server: SHOWPLAN_XML / STATISTICS XML
# ---------------------------------------------------------------------------

_NS = "{http://schemas.microsoft.com/sqlserver/2004/07/showplan}"


def parse_mssql(xml_text: str) -> dict:
    tree = ET.fromstring(xml_text)
    statement = next((stmt for stmt in tree.iter(f"{_NS}StmtSimple")
                      if stmt.find(f"{_NS}QueryPlan") is not None), None)
    if statement is None:
        return {"plan": node("No plan (the statement has no query plan)"), "summary": {}}
    plan = statement.find(f"{_NS}QueryPlan")
    top = _own_relops(plan)
    children = [_ms_node(relop) for relop in top]
    root = children[0] if len(children) == 1 else node(statement.get("StatementType") or "Query",
                                                        children=children)
    summary = {"cost": _num(statement.get("StatementSubTreeCost"))}
    times = plan.find(f"{_NS}QueryTimeStats")
    if times is not None:
        summary["execution_ms"] = _num(times.get("ElapsedTime"))
    if plan.get("CompileTime"):
        summary["planning_ms"] = _num(plan.get("CompileTime"))
    warnings = [child.tag.replace(_NS, "") for w in plan.iter(f"{_NS}Warnings") for child in w]
    if warnings:
        summary["warnings"] = sorted(set(warnings))
    return {"plan": root, "summary": summary}


def _own_relops(element: Any) -> list:
    """The RelOps directly below ``element``, not those nested in them."""
    found: list = []
    for child in element:
        if child.tag == f"{_NS}RelOp":
            found.append(child)
        else:
            found.extend(_own_relops(child))
    return found


def _own(element: Any, tag: str) -> Optional[Any]:
    """The first ``tag`` in this operator's own subtree, stopping at child RelOps."""
    for child in element:
        if child.tag == f"{_NS}RelOp":
            continue
        if child.tag == f"{_NS}{tag}":
            return child
        hit = _own(child, tag)
        if hit is not None:
            return hit
    return None


def _ms_node(relop: Any) -> dict:
    physical, logical = relop.get("PhysicalOp") or "?", relop.get("LogicalOp") or ""
    op = physical if not logical or logical == physical else f"{physical} ({logical})"
    target = ""
    obj = _own(relop, "Object")
    if obj is not None:
        parts = [obj.get(key, "").strip("[]") for key in ("Schema", "Table")]
        target = ".".join(part for part in parts if part)
        if obj.get("Alias") and obj.get("Alias").strip("[]") != obj.get("Table", "").strip("[]"):
            target += f" {obj.get('Alias').strip('[]')}"
        if obj.get("Index"):
            target += f" using {obj.get('Index').strip('[]')}"
    details = []
    for tag, label in (("SeekPredicates", "Seek"), ("Predicate", "Where")):
        found = _own(relop, tag)
        if found is not None:
            scalar = next((e.get("ScalarString") for e in found.iter(f"{_NS}ScalarOperator")
                           if e.get("ScalarString")), None)
            if scalar:
                details.append(f"{label}: {scalar}")
    act_rows = time_ms = loops = None
    runtime = relop.find(f"{_NS}RunTimeInformation")
    if runtime is not None:
        threads = list(runtime.iter(f"{_NS}RunTimeCountersPerThread"))
        act_rows = sum(_num(t.get("ActualRows")) or 0 for t in threads)
        loops = int(sum(_num(t.get("ActualExecutions")) or 0 for t in threads))
        elapsed = [_num(t.get("ActualElapsedms")) for t in threads if t.get("ActualElapsedms")]
        time_ms = max(elapsed) if elapsed else None
    extra = [[key, relop.get(key)] for key in ("EstimateIO", "EstimateCPU", "EstimateRebinds",
                                               "EstimateRewinds", "Parallel", "EstimatedExecutionMode")
             if relop.get(key) not in (None, "0", "false")]
    return node(op, object=target, detail="; ".join(details),
                cost=_num(relop.get("EstimatedTotalSubtreeCost")),
                est_rows=_num(relop.get("EstimateRows")), act_rows=act_rows, time_ms=time_ms,
                loops=loops, extra=extra,
                children=[_ms_node(child) for child in _own_relops(relop)])


# ---------------------------------------------------------------------------
# SQLite: EXPLAIN QUERY PLAN rows (id, parent, notused, detail)
# ---------------------------------------------------------------------------

def parse_sqlite(rows: list) -> dict:
    nodes: dict = {0: node("Query plan")}
    for row in rows:
        ident, parent, detail = int(row[0]), int(row[1]), str(row[3])
        op, _, rest = detail.partition(" ")
        if op in ("SCAN", "SEARCH"):
            target, _, using = rest.partition(" USING ")
            nodes[ident] = node(op.title(), object=target, detail=f"using {using}" if using else "")
        else:
            # "USE TEMP B-TREE FOR GROUP BY", "CO-ROUTINE x", "COMPOUND QUERY"…
            nodes[ident] = node(detail[:1] + detail[1:].lower())
        nodes.get(parent, nodes[0])["children"].append(nodes[ident])
    root = nodes[0]
    return {"plan": root["children"][0] if len(root["children"]) == 1 else root,
            "summary": {"cost": None}}


# ---------------------------------------------------------------------------
# Shared
# ---------------------------------------------------------------------------

def _walk(item: dict):
    yield item
    for child in item["children"]:
        yield from _walk(child)


def flatten(root: dict) -> list:
    """Depth-first rows for a grid: each node with its ``depth``, children dropped."""
    out: list = []

    def visit(item: dict, depth: int) -> None:
        row = {key: value for key, value in item.items() if key != "children"}
        row["depth"] = depth
        row["leaf"] = not item["children"]
        out.append(row)
        for child in item["children"]:
            visit(child, depth + 1)

    visit(root, 0)
    return out


def annotate(result: dict) -> dict:
    """
    Add what the page highlights: each node's share of the statement's cost,
    and estimates off by 10x or more from what actually happened.
    """
    root = result["plan"]
    total = result.get("summary", {}).get("cost") or root.get("cost")
    for item in _walk(root):
        # Engines report a node's cost including its children's; the share is
        # the node's own part (as SSMS shows it), which points at the
        # expensive step instead of painting every parent 100%.
        if total and item["cost"] is not None:
            below = sum(child["cost"] or 0 for child in item["children"])
            item["share"] = max(0.0, item["cost"] - below) / total
        else:
            item["share"] = None
        est, act = item["est_rows"], item["act_rows"]
        loops = item["loops"] or 1
        if est is not None and act is not None and max(est * loops, act) >= 100:
            ratio = (act + 1) / (est * loops + 1)
            item["misestimate"] = ratio >= 10 or ratio <= 0.1
        else:
            item["misestimate"] = False
    return result
