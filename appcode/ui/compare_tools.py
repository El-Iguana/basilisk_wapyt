"""
The schema compare tab's helpers (milestone 3). Pure: no ``js``, no widgets,
so they run in Pyodide and are tested in CPython.

``SqlService.compare_schemas`` answers the differences, each with its
statements tagged by phase (``services/schema_compare.py``). The page lets
the person tick which differences to apply; ``script`` puts the ticked ones'
statements in phase order (foreign keys dropped first, added last…), so any
subset reads top to bottom as something that can run.
"""
from __future__ import annotations

KIND_LABELS = {"table": "Table", "view": "View", "column": "Column", "pk": "Primary key",
               "index": "Index", "fk": "Foreign key"}
CHANGE_LABELS = {"add": "Create", "alter": "Change", "drop": "Drop"}
CHANGE_ICONS = {"add": "mdi-plus-circle-outline", "alter": "mdi-swap-horizontal-circle-outline",
                "drop": "mdi-minus-circle-outline"}


def default_chosen(diffs: list) -> list:
    """Ticked at first: everything but drops, which lose data."""
    return [diff["id"] for diff in diffs if diff.get("change") != "drop"]


def object_label(diff: dict) -> str:
    if diff.get("kind") in ("table", "view"):
        return diff.get("group") or diff.get("name") or ""
    return f"{diff.get('group', '')}.{diff.get('name', '')}"


def grid_rows(diffs: list, chosen) -> list:
    """The differences grid's rows."""
    chosen = set(chosen)
    rows = []
    for diff in diffs:
        change = diff.get("change") or ""
        manual = diff.get("manual") or ""
        rows.append({
            "rid": diff["id"], "use": diff["id"] in chosen,
            "object": object_label(diff),
            "kind": KIND_LABELS.get(diff.get("kind"), diff.get("kind") or ""),
            "change": CHANGE_LABELS.get(change, change), "change_icon": CHANGE_ICONS.get(change),
            "detail": ("⚠ " if manual else "") + (diff.get("detail") or ""),
            "source": diff.get("source") or "", "target": diff.get("target") or "",
            "cls": f"bk-cmp-{change}" + (" bk-cmp-manual" if manual else ""),
        })
    return rows


def counts(diffs: list) -> dict:
    found = {"add": 0, "alter": 0, "drop": 0}
    for diff in diffs:
        if diff.get("change") in found:
            found[diff["change"]] += 1
    return found


def summary(result: dict) -> str:
    """``2 to create · 3 to change · 1 only in the target · 4 tables the same``."""
    found = counts(result.get("diffs") or [])
    parts = []
    if found["add"]:
        parts.append(f"{found['add']} to create")
    if found["alter"]:
        parts.append(f"{found['alter']} to change")
    if found["drop"]:
        parts.append(f"{found['drop']} only in the target")
    same = int(result.get("same_tables") or 0)
    parts.append(f"{same} table{'s' if same != 1 else ''} the same")
    views = int(result.get("same_views") or 0)
    if views:
        parts.append(f"{views} view{'s' if views != 1 else ''} the same")
    return " · ".join(parts)


def _is_comment(statement: str) -> bool:
    lines = [line for line in statement.strip().splitlines() if line.strip()]
    return bool(lines) and all(line.lstrip().startswith("--") for line in lines)


def _ended(statement: str, batches: bool) -> str:
    text = statement.rstrip()
    if _is_comment(text):
        return text
    if batches:
        return text + "\nGO"
    return text if text.endswith(";") else text + ";"


def script(result: dict, chosen, header: list = ()) -> str:
    """The ticked differences' statements, phase by phase, ready for a SQL editor."""
    chosen = set(chosen)
    batches = bool(result.get("batches"))
    phases = list(result.get("phases") or [])
    pieces = []
    for order, diff in enumerate(result.get("diffs") or []):
        if diff["id"] not in chosen:
            continue
        for index, (phase, statement) in enumerate(diff.get("sql") or []):
            pieces.append((int(phase), order, index, statement))
    pieces.sort(key=lambda piece: piece[:3])
    out = [f"-- {line}" for line in header]
    if not pieces:
        out += ["", "-- Nothing to run: tick the differences to apply."]
    current = None
    for phase, _order, _index, statement in pieces:
        if phase != current:
            current = phase
            name = phases[phase] if 0 <= phase < len(phases) else f"Step {phase + 1}"
            if out and out[-1] != "":
                out.append("")
            out.append(f"-- ── {name} " + "─" * max(4, 60 - len(name)))
        out += [_ended(statement, batches), ""]
    return "\n".join(out).strip() + "\n"


def statement_count(result: dict, chosen) -> int:
    """How many statements (not comments) the ticked differences hold."""
    chosen = set(chosen)
    return sum(1 for diff in result.get("diffs") or [] if diff["id"] in chosen
               for _phase, statement in diff.get("sql") or [] if not _is_comment(statement))
