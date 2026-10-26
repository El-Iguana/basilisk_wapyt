#!/usr/bin/env python3
"""
Drive Basilisk in Chromium against every test database (CLAUDE.md, *Test
databases*). Fails on any browser console error or page error.

    uv run python tests/smoke/seed_sample.py        # first
    BASILISK_PASS=… python3 tests/smoke/ui_smoke.py [postgresql mysql mssql sqlite]

Per engine it creates a connection through the editor (switching engine,
Test, Save), walks the navigator to ``customers``, filters, sorts by a header
click, edits a cell and saves it through the preview, puts it back, checks the
Columns / Keys / DDL tabs, runs SQL (a statement at the cursor, a failing
script, a cancelled slow query), opens History and Generate SQL, exports the
table and a result over HTTP, and deletes the connection again.

Expects an admin whose password is not flagged for change (the reminder
dialog blocks the page): ``manage.py reset-password admin`` on a fresh data
dir. Screenshots land in tests/smoke/ (git-ignored).
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

URL = os.environ.get("BASILISK_URL", "http://127.0.0.1:8768/basilisk/")
USER = os.environ.get("BASILISK_USER", "admin")
PASSWORD = os.environ.get("BASILISK_PASS", "")
DB_PASSWORD = os.environ.get("BASILISK_TEST_PASSWORD", "B@sil:isk/1")
SHOTS = Path(__file__).resolve().parent

ENGINES = {
    "postgresql": {"port": "55432", "database": "shop", "username": "postgres",
                   "path": ["shop", "public", "Tables"], "slow": "SELECT pg_sleep(30)"},
    "mysql": {"port": "53306", "database": "shop", "username": "root",
              "path": ["shop", "Tables"], "slow": "SELECT SLEEP(30)"},
    "mssql": {"port": "51433", "database": "shop", "username": "sa",
              "path": ["shop", "dbo", "Tables"], "slow": "WAITFOR DELAY '00:00:30'"},
    "sqlite": {"database": "shop.db", "path": ["Tables"],
               "slow": "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) "
                       "SELECT COUNT(*) FROM c"},
}

failures: list[str] = []
console: list[str] = []


def check(ok: bool, what: str) -> None:
    print(("  ok   " if ok else "  FAIL ") + what, flush=True)
    if not ok:
        failures.append(what)


def shot(page: Page, name: str) -> None:
    page.screenshot(path=str(SHOTS / f"{name}.png"))


def modal(page: Page) -> str:
    return ".wapyt-modal-overlay[style*='flex'] .wapyt-modal"


def wait_text(page: Page, selector: str, needle: str, timeout: int = 20000) -> bool:
    """Wait until the first element matching a Playwright selector contains ``needle``."""
    deadline = time.monotonic() + timeout / 1000
    locator = page.locator(selector).first
    while time.monotonic() < deadline:
        try:
            if needle in locator.inner_text(timeout=1000):
                return True
        except Exception:  # noqa: BLE001 - not there yet
            pass
        time.sleep(0.2)
    return False


def tree_row(page: Page, label: str):
    return page.locator(".wapyt-tree-row", has=page.locator(
        ".wapyt-tree-label", has_text=label)).filter(has_text=label)


def exact_tree_row(page: Page, label: str):
    return page.locator(f'.wapyt-tree-row:has(.wapyt-tree-label:text-is("{label}"))')


def create_connection(page: Page, engine: str, name: str) -> None:
    spec = ENGINES[engine]
    page.click('.wapyt-toolbar [data-id="new_conn"]')
    page.wait_for_selector('.wapyt-modal input[name="name"]')
    page.click(f'.wapyt-modal [data-field-id="engine"] [data-value="{engine}"]')
    time.sleep(0.2)
    page.fill('.wapyt-modal input[name="name"]', name)
    if engine != "sqlite":
        page.fill('.wapyt-modal input[name="host"]', "127.0.0.1")
        page.fill('.wapyt-modal input[name="port"]', spec["port"])
        page.fill('.wapyt-modal input[name="username"]', spec["username"])
        page.fill('.wapyt-modal input[name="password"]', DB_PASSWORD)
        check(page.is_visible('.wapyt-modal input[name="host"]'), f"{engine}: host field shown")
    else:
        check(not page.is_visible('.wapyt-modal input[name="host"]'), "sqlite: no host field")
    page.fill('.wapyt-modal input[name="database"]', spec["database"])
    page.click('.wapyt-modal [data-button-id="test"]')
    tested = wait_text(page, '.wapyt-modal [data-field-id="test_result"]', "Connected")
    check(tested, f"{engine}: Test connection succeeds")
    shot(page, f"smoke-{engine}-1-editor")
    page.click('.wapyt-modal button[type="submit"]')
    page.wait_for_selector(".wapyt-modal", state="detached", timeout=10000)


def open_customers(page: Page, engine: str, name: str) -> None:
    spec = ENGINES[engine]
    exact_tree_row(page, name).first.click()
    page.keyboard.press("ArrowRight")
    # Connecting expands the default database/schema and its Tables group.
    page.wait_for_selector('.wapyt-tree-row:has(.wapyt-tree-label:text-is("customers"))',
                           timeout=30000)
    check(True, f"{engine}: navigator reaches Tables")
    exact_tree_row(page, "customers").first.dblclick()
    page.wait_for_selector('.bk-view:visible tr[data-row-id="r0"]', timeout=30000)
    stats = page.inner_text(".bk-view:visible .bk-head-stats")
    check("rows" in stats, f"{engine}: customers opens ({stats})")


def table_checks(page: Page, engine: str) -> None:
    view = ".bk-view:visible"
    # Filter, then a header click sorts on the server.
    page.fill(f'{view} [data-role="where"]', "name LIKE 'A%'")
    page.keyboard.press("Enter")
    time.sleep(1.5)
    names = page.locator(f'{view} tbody tr td[data-column-id="c1"]').all_inner_texts()
    check(len(names) == 2 and all(n.startswith("A") for n in names),
          f"{engine}: WHERE filters ({names})")
    page.click(f'{view} [data-bk="clear"]')
    time.sleep(1.5)
    page.click(f'{view} th:has-text("name")')
    time.sleep(1.5)
    order = page.input_value(f'{view} [data-role="order_by"]')
    first = page.inner_text(f'{view} tr[data-row-id="r0"] td[data-column-id="c1"]')
    check("name" in order and first.startswith("Ada"), f"{engine}: header sort ({order}: {first})")

    # Edit a cell, see it pending, save through the preview, put it back.
    cell = page.locator(f'{view} tr[data-row-id="r0"] td[data-column-id="c5"]')
    original = cell.inner_text()
    cell.dblclick()
    page.keyboard.press("Control+a")
    page.keyboard.type("Smoke note")
    page.keyboard.press("Enter")
    time.sleep(0.3)
    check("Save (1)" in page.inner_text(f'{view} [data-bk="save"]'), f"{engine}: edit is pending")
    check("bk-dirty" in (cell.get_attribute("class") or ""), f"{engine}: changed cell is marked")
    page.click(f'{view} [data-bk="save"]')
    page.wait_for_selector(f"{modal(page)} .bk-pre", timeout=10000)
    preview = page.inner_text(f"{modal(page)} .bk-pre")
    check("UPDATE" in preview.upper() and "Smoke note" in preview, f"{engine}: save preview")
    shot(page, f"smoke-{engine}-2-preview")
    page.click(f'{modal(page)} [data-answer="yes"]')
    time.sleep(2)
    check(page.inner_text(f'{view} tr[data-row-id="r0"] td[data-column-id="c5"]') == "Smoke note",
          f"{engine}: saved value reloads")
    cell = page.locator(f'{view} tr[data-row-id="r0"] td[data-column-id="c5"]')
    cell.click(button="right")
    page.click('.wapyt-datatable-menu [data-action-id]:has-text("Set NULL")')
    time.sleep(0.3)
    page.click(f'{view} [data-bk="save"]')
    page.wait_for_selector(f"{modal(page)} .bk-pre", timeout=10000)
    page.click(f'{modal(page)} [data-answer="yes"]')
    time.sleep(2)
    restored = page.inner_text(f'{view} tr[data-row-id="r0"] td[data-column-id="c5"]')
    check(restored == "" and original in ("", "Prefers email.\nSecond line.", "Prefers email. Second line."),
          f"{engine}: Set NULL saved")
    shot(page, f"smoke-{engine}-3-table")

    for sub, needle in (("columns", "email"), ("keys", "PRIMARY KEY"), ("ddl", "customers")):
        page.click(f'{view} .bk-subtab[data-sub="{sub}"]')
        ok = wait_text(page, f'{view} .bk-sub[data-sub="{sub}"]', needle, 15000)
        check(ok, f"{engine}: {sub} tab shows {needle}")
    page.click(f'{view} .bk-subtab[data-sub="data"]')



def panel_open(page: Page, view: str) -> bool:
    return page.locator(f"{view} .bk-results.bk-vp-on").count() > 0


def panel_state(page: Page) -> str:
    return page.inner_text(".bk-vp:visible .bk-vp-status")


def value_panel_checks(page: Page, engine: str) -> None:
    """The value panel: a cut long text loads whole, an edit applies, saves and goes
    back; JSON, image and hex views; the unapplied-change guard; F7."""
    view = ".bk-view:visible"
    vp = ".bk-vp:visible"
    page.click(f'{view} [data-bk="clear"]')
    page.wait_for_selector(f'{view} tr[data-row-id="r10"]', timeout=20000)
    if not panel_open(page, view):
        page.click(f'{view} [data-bk="value_panel"]')
    check(panel_open(page, view), f"{engine}: Value opens the panel")
    notes = f'{view} tr[data-row-id="r10"] td[data-column-id="c5"]'
    try:
        page.wait_for_selector(f"{notes}.bk-cut", timeout=10000)
        check(True, f"{engine}: long note is cut in the grid")
    except Exception:  # noqa: BLE001
        check(False, f"{engine}: long note is cut in the grid")
    page.click(notes)
    ok = wait_text(page, f"{vp} .bk-vp-status", "K characters")
    size = panel_state(page)
    check(ok and "Changed" not in size, f"{engine}: panel loads the whole note ({size.split()[0:3]})")
    shot(page, f"smoke-{engine}-5-value")
    # Append a line, apply, save: the preview is cut, the saved value is whole.
    page.click(f"{vp} .cm-content")
    page.keyboard.press("Control+End")
    page.keyboard.type("\nLine 0541: smoke.")
    check(wait_text(page, f"{vp} .bk-vp-status", "Changed"), f"{engine}: typing marks a change")
    page.keyboard.press("Control+Enter")
    time.sleep(0.3)
    classes = page.get_attribute(notes, "class") or ""
    check("bk-dirty" in classes and "bk-cut" in classes, f"{engine}: Apply makes a pending edit")
    page.click(f'{view} [data-bk="save"]')
    page.wait_for_selector(f"{modal(page)} .bk-pre", timeout=10000)
    preview = page.inner_text(f"{modal(page)} .bk-pre")
    check("characters in all" in preview, f"{engine}: save preview is cut")
    page.click(f'{modal(page)} [data-answer="yes"]')
    time.sleep(2)
    page.click(notes)
    wait_text(page, f"{vp} .bk-vp-status", "K characters")
    page.click(f"{vp} .cm-content")
    page.keyboard.press("Control+End")
    check(wait_text(page, f"{vp} .cm-content", "Line 0541: smoke."), f"{engine}: saved whole")
    page.keyboard.press("Shift+Home")
    page.keyboard.press("Backspace")
    page.keyboard.press("Backspace")
    page.keyboard.press("Control+Enter")
    page.click(f'{view} [data-bk="save"]')
    page.wait_for_selector(f"{modal(page)} .bk-pre", timeout=10000)
    page.click(f'{modal(page)} [data-answer="yes"]')
    time.sleep(2)
    page.click(notes)
    wait_text(page, f"{vp} .bk-vp-status", "K characters")
    check(panel_state(page).split()[0:3] == size.split()[0:3], f"{engine}: note put back")

    # products: JSON, a picture, bytes.
    exact_tree_row(page, "products").first.dblclick()
    page.wait_for_selector(f'{view} tr[data-row-id="r3"]', timeout=30000)
    if not panel_open(page, view):
        page.click(f'{view} [data-bk="value_panel"]')
    page.click(f'{view} tr[data-row-id="r0"] td[data-column-id="c4"]')
    ok = wait_text(page, f"{vp} .bk-vp-status", "Valid JSON")
    pressed = page.get_attribute(f'{vp} [data-view="json"]', "aria-pressed")
    check(ok and pressed == "true", f"{engine}: JSON opens in the JSON view")
    page.click(f"{vp} .cm-content")
    page.keyboard.press("Control+End")
    page.keyboard.type("x")
    check(wait_text(page, f"{vp} .bk-vp-status", "Invalid JSON"), f"{engine}: JSON checked as typed")
    page.keyboard.press("Control+Enter")
    check(wait_text(page, f"{vp} .bk-vp-note", "Invalid JSON"), f"{engine}: bad JSON not applied")
    page.click(f'{view} tr[data-row-id="r1"] td[data-column-id="c4"]')
    check(wait_text(page, f"{vp} .bk-vp-note", "Apply or discard"), f"{engine}: unapplied edit holds")
    page.click(f'{vp} [data-vp="discard"]')
    page.click(f'{view} tr[data-row-id="r0"] td[data-column-id="c5"]')
    time.sleep(0.8)
    natural = page.locator(f"{vp} img").evaluate("img => img.naturalWidth")
    check(natural == 24, f"{engine}: a PNG shows as a picture ({natural} px)")
    page.click(f'{view} tr[data-row-id="r3"] td[data-column-id="c5"]')
    ok = wait_text(page, f"{vp} .cm-content", "00000000  00 01 02 03")
    check(ok and "5,120 bytes" in panel_state(page), f"{engine}: bytes show as a hex dump")
    shot(page, f"smoke-{engine}-6-hex")
    page.click(f'{view} tr[data-row-id="r3"] td[data-column-id="c1"]')
    page.keyboard.press("F7")
    time.sleep(0.3)
    check(not panel_open(page, view), f"{engine}: F7 closes the panel")


def export_checks(page: Page, engine: str, conn_id: int) -> None:
    db = {"postgresql": "shop", "mysql": "shop", "mssql": "shop", "sqlite": ""}[engine]
    schema = {"postgresql": "public", "mssql": "dbo"}.get(engine, "")
    result = page.evaluate(
        """async ([id, db, schema]) => {
            const q = new URLSearchParams({table: 'orders', database: db, schema: schema,
                                           where: "status = 'paid'", format: 'csv'});
            const r = await fetch(`/bk/export/${id}?${q}`);
            const text = await r.text();
            return [r.status, text.split('\\n').length, text.slice(0, 40)];
        }""", [conn_id, db, schema])
    check(result[0] == 200 and result[1] > 3 and "id,customer_id" in result[2],
          f"{engine}: table export CSV ({result[0]}, {result[1]} lines)")


def sql_checks(page: Page, engine: str) -> None:
    page.click('.wapyt-toolbar [data-id="sql"]')
    page.wait_for_selector(".bk-sqltab:visible .cm-content", timeout=30000)
    tab = ".bk-sqltab:visible"
    page.click(f"{tab} .cm-content")
    page.keyboard.press("Control+a")
    page.keyboard.press("Delete")
    script = ("SELECT status, COUNT(*) AS n FROM orders GROUP BY status ORDER BY status;\n\n"
              "SELECT 1/0 AS boom;\n")
    if engine == "mssql":
        script = script.replace(";\n\nSELECT", ";\nGO\nSELECT")
    page.keyboard.insert_text(script)
    page.keyboard.press("Control+Home")
    page.keyboard.press("Control+Enter")
    ok = wait_text(page, f"{tab} .bk-res-tabs", "Result 1")
    check(ok, f"{engine}: Ctrl+Enter runs the statement at the cursor")
    statuses = page.locator(f'{page_grid(tab)} tbody td[data-column-id="c0"]').all_inner_texts()
    check("paid" in statuses, f"{engine}: result rows ({statuses})")
    shot(page, f"smoke-{engine}-4-sql")
    # The value panel on a result: read-only, and Shift+Enter opens it.
    page.click(f'{page_grid(tab)} tr[data-row-id="r0"] td[data-column-id="c0"]')
    page.keyboard.press("Shift+Enter")
    ok = wait_text(page, ".bk-vp:visible .bk-vp-head", "status")
    check(ok and not page.is_visible('.bk-vp:visible [data-vp="apply"]'),
          f"{engine}: Shift+Enter opens a read-only panel on a result")
    page.click(f'{page_grid(tab)} tr[data-row-id="r0"] td[data-column-id="c0"]')
    page.keyboard.press("F7")
    time.sleep(0.3)
    check(not page.is_visible(".bk-vp:visible"), f"{engine}: F7 closes it")
    page.click(f"{tab} .cm-content")
    page.keyboard.press("Control+Shift+Enter")
    if engine in ("sqlite", "mysql"):
        # SQLite and MySQL divide by zero to NULL, so the script succeeds.
        ok = wait_text(page, f"{tab} .bk-res-tabs", "Result 2")
    else:
        ok = wait_text(page, f"{tab} .bk-msgs", "✘")
    check(ok, f"{engine}: script runs to the end or stops at its error")

    # Cancel a slow statement.
    page.click(f"{tab} .cm-content")
    page.keyboard.press("Control+a")
    page.keyboard.insert_text(ENGINES[engine]["slow"])
    page.keyboard.press("Control+Enter")
    page.wait_for_selector(f'{tab} [data-bk="cancel"]:visible', timeout=10000)
    time.sleep(1.5)
    started = time.monotonic()
    page.click(f'{tab} [data-bk="cancel"]')
    page.wait_for_selector(f'{tab} [data-bk="run"]:visible', timeout=20000)
    check(time.monotonic() - started < 10, f"{engine}: Cancel stops a slow statement")

    manual_commit_checks(page, engine, tab)
    explain_checks(page, engine, tab)

    page.click(f'{tab} [data-bk="history"]')
    page.wait_for_selector(f"{modal(page)} tbody tr", timeout=10000)
    rows = page.locator(f"{modal(page)} tbody tr").count()
    check(rows >= 3, f"{engine}: history lists the runs ({rows})")
    page.keyboard.press("Escape")
    page.wait_for_selector(".wapyt-modal", state="detached", timeout=5000)


def run_sql(page: Page, tab: str, sql: str) -> None:
    page.click(f"{tab} .cm-content")
    page.keyboard.press("Control+a")
    page.keyboard.insert_text(sql)
    page.keyboard.press("Control+Enter")
    page.wait_for_selector(f'{tab} [data-bk="run"]:visible', timeout=20000)
    time.sleep(0.4)


def manual_commit_checks(page: Page, engine: str, tab: str) -> None:
    """Manual mode: an UPDATE waits in a transaction; Rollback undoes it;
    leaving manual mode with work pending asks, and Commit keeps it."""
    page.click(f'{tab} [data-bk="mode"][data-mode="manual"]')
    try:
        page.wait_for_selector(f'{tab} [data-bk="commit"]:visible', timeout=5000)
        shown = True
    except Exception:  # noqa: BLE001
        shown = False
    check(shown, f"{engine}: manual mode shows Commit")
    run_sql(page, tab, "UPDATE orders SET status = 'held' WHERE id = 9")
    ok = wait_text(page, f"{tab} .bk-txn", "Transaction · 1 statement", 10000)
    check(ok, f"{engine}: indicator shows the open transaction")
    check(page.locator('.wapyt-tab-badge:text-is("TX")').count() == 1,
          f"{engine}: the tab is badged TX")
    shot(page, f"smoke-{engine}-5-manual")
    page.click(f'{tab} [data-bk="rollback"]')
    ok = wait_text(page, f"{tab} .bk-txn", "No open transaction", 10000)
    check(ok, f"{engine}: Rollback closes the transaction")
    run_sql(page, tab, "SELECT status FROM orders WHERE id = 9")
    after = page.locator(f'{page_grid(tab)} tbody td[data-column-id="c0"]').all_inner_texts()
    # In manual mode the SELECT opened a new transaction; roll it back too.
    check(after and after[0] != "held", f"{engine}: rolled-back change is gone ({after})")
    page.click(f'{tab} [data-bk="rollback"]')
    time.sleep(0.5)

    run_sql(page, tab, "UPDATE orders SET status = status WHERE id = 9")
    page.click(f'{tab} [data-bk="mode"][data-mode="auto"]')
    page.wait_for_selector(f'{modal(page)} [data-choice="commit"]', timeout=10000)
    page.click(f'{modal(page)} [data-choice="commit"]')
    page.wait_for_selector(".wapyt-modal", state="detached", timeout=10000)
    time.sleep(0.5)
    check(not page.is_visible(f'{tab} [data-bk="commit"]'),
          f"{engine}: back in auto-commit after committing")


def explain_checks(page: Page, engine: str, tab: str) -> None:
    """Explain (Ctrl+Shift+E) and Analyze the statement at the cursor."""
    page.click(f"{tab} .cm-content")
    page.keyboard.press("Control+a")
    page.keyboard.insert_text("SELECT 1 AS one;\n\nSELECT c.name, COUNT(*) AS n FROM customers c "
                              "JOIN orders o ON o.customer_id = c.id GROUP BY c.name;")
    page.keyboard.press("Control+End")
    page.keyboard.press("Control+Shift+E")
    ok = wait_text(page, f"{tab} .bk-res-tabs", "Plan", 20000)
    check(ok, f"{engine}: Ctrl+Shift+E shows the plan")
    ops = page.locator(f'{tab} [id$="-plan-grid"] tbody td[data-column-id="op"]').all_inner_texts()
    check(len(ops) >= 2, f"{engine}: plan has nodes ({len(ops)})")
    shot(page, f"smoke-{engine}-6-plan")
    page.click(f'{tab} [data-bk="plan_view"][data-view="raw"]')
    raw = page.inner_text(f'{tab} [id$="-plan-raw"]')
    check(len(raw) > 20, f"{engine}: raw plan")
    page.click(f'{tab} [data-bk="plan_view"][data-view="tree"]')
    if engine != "sqlite":
        page.click(f'{tab} [data-bk="analyze"]')
        ok = wait_text(page, f"{tab} .bk-res-tabs", "Actual plan", 30000)
        headers = page.locator(f'{tab} [id$="-plan-grid"] thead').inner_text() if ok else ""
        check(ok and "Actual rows" in headers, f"{engine}: Analyze shows actual rows")
        shot(page, f"smoke-{engine}-7-analyze")


def page_grid(tab: str) -> str:
    return f"{tab} .bk-res .bk-panel:not([hidden])"


def generate_check(page: Page, engine: str) -> None:
    row = exact_tree_row(page, "orders").first
    row.click(button="right")
    page.click('.wapyt-tree-menu [data-action-id]:has-text("Generate SQL")')
    ok = wait_text(page, f"{modal(page)} .bk-code-host", "SELECT")
    page.click(f'{modal(page)} [data-g="insert"]')
    ok = ok and wait_text(page, f"{modal(page)} .bk-code-host", "INSERT INTO")
    check(ok, f"{engine}: Generate SQL")
    page.keyboard.press("Escape")
    page.wait_for_selector(".wapyt-modal", state="detached", timeout=5000)


def expand(page: Page, label: str) -> None:
    row = exact_tree_row(page, label).first
    row.click()
    page.keyboard.press("ArrowRight")
    time.sleep(0.4)


def navigator_checks(page: Page, engine: str) -> None:
    """Routines, triggers, roles and (PostgreSQL) a materialized view refresh."""
    view = ".bk-view:visible"
    expand(page, "Triggers")
    exact_tree_row(page, "orders_status_check").first.dblclick()
    ok = wait_text(page, f"{view} .bk-obj-props", "orders")
    ok = ok and wait_text(page, f"{view} .bk-obj-def", "orders_status_check")
    check(ok, f"{engine}: a trigger opens with its definition")
    page.click(f'{view} [data-bk="obj_table"]')
    check(wait_text(page, f"{view} .bk-head", "orders"), f"{engine}: trigger opens its table")
    if engine != "sqlite":
        expand(page, "Procedures")
        exact_tree_row(page, "archive_order").first.dblclick()
        ok = wait_text(page, f"{view} .bk-obj-props", "rocedure")  # "SQL stored procedure"
        page.click(f'{view} [data-bk="obj_call"]')
        page.wait_for_selector(".bk-sqltab:visible .cm-content", timeout=20000)
        ok = ok and wait_text(page, ".bk-sqltab:visible .cm-content", "archive_order")
        check(ok, f"{engine}: a procedure opens and Call… writes a call")
        shot(page, f"smoke-{engine}-8-call")
        roles = page.locator('.wapyt-tree-row:has(.wapyt-tree-label:text-matches("roles$"))')
        roles.first.click()
        page.keyboard.press("ArrowRight")
        me = {"postgresql": "postgres", "mysql": "root@%", "mssql": "sa"}[engine]
        page.wait_for_selector(f'.wapyt-tree-row:has(.wapyt-tree-label:text-is("{me}"))',
                               timeout=20000)
        exact_tree_row(page, me).first.dblclick()
        check(wait_text(page, f"{view} .bk-obj-props", "Can log in"),
              f"{engine}: {me} opens under "
              f"{roles.first.locator('.wapyt-tree-label').inner_text().strip()}")
        shot(page, f"smoke-{engine}-9-role")
    if engine == "postgresql":
        expand(page, "Views")
        exact_tree_row(page, "product_sales").first.click(button="right")
        page.click('.wapyt-tree-menu [data-action-id]:has-text("Refresh materialized")')
        page.wait_for_selector(f"{modal(page)} .bk-pre", timeout=10000)
        ok = "CONCURRENTLY" in page.inner_text(f"{modal(page)} .bk-pre")
        page.click(f'{modal(page)} [data-answer="yes"]')
        ok = ok and wait_text(page, "body", "Refreshed product_sales")
        check(ok, f"{engine}: materialized view refreshes CONCURRENTLY")


def ddl_ready(page: Page) -> bool:
    """Wait for the action dialog's live SQL preview."""
    return wait_text(page, f'{modal(page)} [data-ddl="sql"]', ";", 10000)


def object_action_checks(page: Page, engine: str, name: str) -> None:
    """Create a table from the navigator, add a column from its tab, drop it."""
    where = {"postgresql": "public", "mysql": "shop", "mssql": "dbo", "sqlite": name}[engine]
    int_type = {"postgresql": "integer", "mysql": "int", "mssql": "int", "sqlite": "INTEGER"}[engine]
    exact_tree_row(page, where).first.click(button="right")
    page.click('.wapyt-tree-menu [data-action-id]:has-text("Create table")')
    page.wait_for_selector(f'{modal(page)} [data-p="name"]', timeout=10000)
    page.fill(f'{modal(page)} [data-p="name"]', "smoke_gadgets")
    ok = ddl_ready(page) and "CREATE TABLE" in page.inner_text(f'{modal(page)} [data-ddl="sql"]')
    check(ok, f"{engine}: Create table previews its SQL")
    shot(page, f"smoke-{engine}-10-create")
    page.click(f'{modal(page)} [data-answer="yes"]')
    page.wait_for_selector(".wapyt-modal", state="detached", timeout=15000)
    view = ".bk-view:visible"
    check(wait_text(page, f"{view} .bk-head", "smoke_gadgets"), f"{engine}: the new table opens")
    page.click(f'{view} [data-bk="col_add"]')
    page.wait_for_selector(f'{modal(page)} [data-p="name"]', timeout=10000)
    page.fill(f'{modal(page)} [data-p="name"]', "qty")
    page.fill(f'{modal(page)} [data-p="type"]', int_type)
    ddl_ready(page)
    page.click(f'{modal(page)} [data-answer="yes"]')
    page.wait_for_selector(".wapyt-modal", state="detached", timeout=15000)
    ok = wait_text(page, f'{view} [id$="-cols"]', "qty")
    check(ok, f"{engine}: Add column shows the new column")
    exact_tree_row(page, "smoke_gadgets").first.click(button="right")
    page.click('.wapyt-tree-menu [data-action-id]:has-text("Drop")')
    page.wait_for_selector(f"{modal(page)} .bk-danger", timeout=10000)
    page.click(f'{modal(page)} [data-answer="yes"]')
    page.wait_for_selector(".wapyt-modal", state="detached", timeout=15000)
    time.sleep(1.5)
    check(exact_tree_row(page, "smoke_gadgets").count() == 0
          and page.locator('.wapyt-tab:has-text("smoke_gadgets")').count() == 0,
          f"{engine}: Drop removes the table and its tab")


def clipboard(page: Page) -> str:
    return str(page.evaluate("() => navigator.clipboard.readText()"))


def menu(page: Page, cell, label: str) -> None:
    cell.click(button="right")
    page.click(f'.wapyt-datatable-menu [data-action-id]:has-text("{label}")')
    time.sleep(0.4)


def headers(page: Page, view: str) -> list:
    return [t.strip() for t in page.locator(f"{view} .bk-vp-grid thead th").all_inner_texts()]


def grid_tool_checks(page: Page, engine: str, name: str) -> None:
    """Column layout memory, freeze, column filters, copy as, open a foreign key's row."""
    view = ".bk-view:visible"
    email = page.locator(f'{view} tr[data-row-id="r0"] td[data-column-id="c2"]')
    menu(page, email, "Hide this column")
    check("email" not in headers(page, view), f"{engine}: a column hides from the cell menu")
    page.click(f'{view} [data-bk="freeze"]')
    frozen = page.get_attribute(f'{view} [data-bk="freeze"]', "aria-pressed") == "true"
    check(frozen and page.locator(f"{view} [data-frozen]").count() > 0,
          f"{engine}: the first column freezes")
    time.sleep(1)               # the layout is saved after a pause
    close_tabs(page)
    exact_tree_row(page, "customers").first.dblclick()
    page.wait_for_selector(f'{view} tr[data-row-id="r0"]', timeout=30000)
    time.sleep(0.5)
    kept = "email" not in headers(page, view) and page.get_attribute(
        f'{view} [data-bk="freeze"]', "aria-pressed") == "true"
    check(kept, f"{engine}: hidden and frozen columns are remembered for the table")
    page.click(f'{view} [data-bk="columns"]')
    page.wait_for_selector(f"{modal(page)} [data-col]", timeout=10000)
    shot(page, f"smoke-{engine}-11-columns")
    page.click(f'{modal(page)} [data-c="reset"]')
    time.sleep(1.5)
    check("email" in headers(page, view) and page.get_attribute(
        f'{view} [data-bk="freeze"]', "aria-pressed") == "false",
          f"{engine}: Reset layout brings the columns back")

    # A column filter is a WHERE condition the server runs.
    page.click(f'{view} [data-bk="col_filter"]')
    page.wait_for_selector(f'{modal(page)} [data-f="value"]', timeout=10000)
    page.select_option(f'{modal(page)} [data-f="column"]', "name")
    page.select_option(f'{modal(page)} [data-f="op"]', "starts")
    page.fill(f'{modal(page)} [data-f="value"]', "a")
    page.keyboard.press("Enter")
    time.sleep(1.5)
    names = page.locator(f'{view} tbody tr td[data-column-id="c1"]').all_inner_texts()
    chip = page.inner_text(f"{view} .bk-chips")
    check(len(names) == 2 and all(n.startswith("A") for n in names) and "starts with" in chip,
          f"{engine}: a column filter narrows the rows ({names}, {chip!r})")
    check("LIKE" in page.inner_text(f"{view} .bk-foot").upper(), f"{engine}: the filter is SQL")
    shot(page, f"smoke-{engine}-12-filter")
    page.click(f'{view} [data-bk="unfilter"]')
    time.sleep(1.5)
    check(page.locator(f"{view} .bk-vp-grid tbody tr").count() > 2
          and not page.is_visible(f"{view} .bk-chips"),
          f"{engine}: removing the chip shows every row")

    # Copy as Markdown / INSERT.
    first = page.locator(f'{view} tr[data-row-id="r0"] td[data-column-id="c1"]')
    menu(page, first, "Copy as Markdown")
    text = clipboard(page)
    check(text.startswith("| id | name") and "|---:|" in text, f"{engine}: Copy as Markdown")
    menu(page, first, "Copy as INSERT")
    text = clipboard(page)
    check(text.startswith("INSERT INTO") and "customers" in text and "VALUES (1," in text,
          f"{engine}: Copy as INSERT ({text[:60]!r})")

    # A foreign-key cell opens the row it points at.
    exact_tree_row(page, "orders").first.dblclick()
    page.wait_for_selector(f'{view} .bk-head:has-text("orders") ~ * tr[data-row-id="r0"], '
                           f'{view} tr[data-row-id="r0"]', timeout=30000)
    wait_text(page, f"{view} .bk-head", "orders")
    time.sleep(1)
    column = page.locator(f'{view} .bk-vp-grid th:has-text("customer_id")').first \
        .get_attribute("data-column-id")
    target = page.locator(f'{view} tr[data-row-id="r0"] td[data-column-id="{column}"]')
    wanted = target.inner_text().strip()
    menu(page, target, "Open the referenced row")
    ok = wait_text(page, f"{view} .bk-head", "customers")
    time.sleep(1.5)
    where = page.input_value(f'{view} [data-role="where"]')
    rows = page.locator(f"{view} .bk-vp-grid tbody tr").count()
    check(ok and rows == 1 and wanted in where,
          f"{engine}: a foreign key opens its row ({where}, {rows} rows)")
    shot(page, f"smoke-{engine}-13-fk")


def import_checks(page: Page, engine: str) -> None:
    """Import a CSV file into a new table from its tree menu."""
    page.click('.wapyt-toolbar [data-id="sql"]')
    page.wait_for_selector(".bk-sqltab:visible .cm-content", timeout=30000)
    tab = ".bk-sqltab:visible"
    text_type = "nvarchar(40)" if engine == "mssql" else "varchar(40)"
    run_sql(page, tab, f"CREATE TABLE smoke_import (id int PRIMARY KEY, label {text_type}, "
                       "price numeric(8,2))")
    page.click('.wapyt-toolbar [data-id="refresh"]')
    page.wait_for_selector('.wapyt-tree-row:has(.wapyt-tree-label:text-is("smoke_import"))',
                           timeout=30000)
    exact_tree_row(page, "smoke_import").first.click(button="right")
    page.click('.wapyt-tree-menu [data-action-id]:has-text("Import CSV")')
    page.wait_for_selector(f'{modal(page)} [data-i="file"]', state="attached", timeout=15000)
    body = "ID;Label;Price\n1;one;1.5\n2;\"two; quoted\";2\n3;three;\n"
    page.set_input_files(f'{modal(page)} [data-i="file"]', files=[{
        "name": "smoke.csv", "mimeType": "text/csv", "buffer": body.encode()}])
    ok = wait_text(page, f'{modal(page)} [data-i="preview"]', "two; quoted", 10000)
    picked = page.input_value(f'{modal(page)} [data-i="delimiter"]')
    mapped = page.input_value(f'{modal(page)} [data-map="price"]')
    check(ok and picked == ";" and mapped == "2",
          f"{engine}: Import reads the file, guesses ; and maps by header")
    shot(page, f"smoke-{engine}-13-import")
    page.click(f'{modal(page)} [data-answer="yes"]')
    done = wait_text(page, f"{modal(page)} .bk-console-state", "Done", 30000)
    log = page.inner_text(f"{modal(page)} .bk-console-log") if done else ""
    check(done and "Imported 3 rows" in log, f"{engine}: the import job finishes ({log[-60:]!r})")
    page.click(f'{modal(page)} [data-job="close"]')
    run_sql(page, tab, "SELECT COUNT(*) AS n, SUM(price) AS total FROM smoke_import")
    cells = page.locator(f'{page_grid(tab)} tr[data-row-id="r0"] td[data-column-id]') \
        .all_inner_texts()
    check(cells[:1] == ["3"] and cells[1].startswith("3.5"), f"{engine}: imported rows ({cells})")
    run_sql(page, tab, "DROP TABLE smoke_import")
    result_grid_checks(page, engine, tab)


def result_grid_checks(page: Page, engine: str, tab: str) -> None:
    """A query result: a column filter on the fetched rows, freeze, Copy as INSERT."""
    run_sql(page, tab, "SELECT id, name FROM customers ORDER BY id")
    grid = page_grid(tab)
    page.click(f'{tab} [data-bk="col_filter"]')
    page.wait_for_selector(f'{modal(page)} [data-f="value"]', timeout=10000)
    page.select_option(f'{modal(page)} [data-f="column"]', "name")
    page.fill(f'{modal(page)} [data-f="value"]', "an")
    page.click(f'{modal(page)} [data-answer="yes"]')
    time.sleep(0.8)
    names = page.locator(f'{grid} tbody td[data-column-id="c1"]').all_inner_texts()
    summary = page.inner_text(f"{tab} [id$='-summary']")
    check(names and all("an" in n.lower() for n in names) and "of 14 rows" in summary,
          f"{engine}: a result filters by a column ({len(names)}; {summary})")
    page.click(f'{tab} [data-bk="freeze"]')
    check(page.locator(f"{grid} [data-frozen]").count() > 0, f"{engine}: a result freezes")
    page.click(f'{tab} [data-bk="freeze"]')
    shot(page, f"smoke-{engine}-15-result")
    menu(page, page.locator(f'{grid} tr[data-row-id] td[data-column-id="c1"]').first,
         "Copy as INSERT")
    text = clipboard(page)
    check(text.startswith("INSERT INTO customers (") and "VALUES (" in text,
          f"{engine}: a result copies as INSERT into its table ({text[:50]!r})")


def session_checks(page: Page, engine: str, name: str) -> None:
    """The server's sessions in their own tab."""
    exact_tree_row(page, name).first.click(button="right")
    page.click('.wapyt-tree-menu [data-action-id]:has-text("Sessions")')
    if engine == "sqlite":
        check(wait_text(page, "body", "no server sessions"), "sqlite: no sessions to list")
        return
    view = ".bk-view:visible"
    ok = wait_text(page, f"{view} .bk-head-stats", "session", 20000)
    rows = page.locator(f"{view} tbody tr").count()
    check(ok and rows >= 1 and wait_text(page, f"{view} tbody", "★"),
          f"{engine}: Sessions lists the server's sessions ({rows})")
    page.check(f'{view} [data-role="s_idle"]')
    time.sleep(1.5)
    check(page.locator(f"{view} tbody tr").count() >= rows, f"{engine}: idle sessions show too")
    page.locator(f"{view} tbody tr").first.dblclick()
    check(wait_text(page, f"{modal(page)} .bk-session-facts", "User"),
          f"{engine}: a session opens its details")
    shot(page, f"smoke-{engine}-14-sessions")
    page.click(f'{modal(page)} [data-s="close"]')


def er_box(page: Page, table: str):
    return page.locator(f'.bk-view:visible .bk-er-table[data-t="{table}"]')


def er_checks(page: Page, engine: str, name: str) -> None:
    """The ER diagram: draw, drag (remembered), column modes, find, SVG, focus on a table."""
    where = {"postgresql": "public", "mysql": "shop", "mssql": "dbo", "sqlite": name}[engine]
    view = ".bk-view:visible"
    exact_tree_row(page, where).first.click(button="right")
    page.click('.wapyt-tree-menu [data-action-id="er"]')
    page.wait_for_selector(f"{view} .bk-er-table", timeout=30000)
    stats = page.inner_text(f"{view} .bk-head-stats")
    edges = page.locator(f"{view} .bk-er-edge").count()
    check(er_box(page, "order_items").count() == 1 and edges == 3 and "3 foreign keys" in stats,
          f"{engine}: the ER diagram draws the tables and keys ({stats})")
    shot(page, f"smoke-{engine}-16-er")
    box = er_box(page, "customers")
    before = box.get_attribute("transform")
    path = page.locator(f'{view} .bk-er-edge[data-to="customers"]').first.get_attribute("d")
    area = box.bounding_box()
    page.mouse.move(area["x"] + 30, area["y"] + 12)
    page.mouse.down()
    page.mouse.move(area["x"] + 90, area["y"] + 112, steps=8)
    page.mouse.up()
    moved = box.get_attribute("transform")
    redrawn = page.locator(f'{view} .bk-er-edge[data-to="customers"]').first.get_attribute("d")
    check(moved != before and redrawn != path, f"{engine}: dragging a table moves it and its lines")
    page.click(f'{view} [data-bk="er_mode"][data-mode="keys"]')
    check(page.locator(f"{view} .bk-er-more").count() > 0, f"{engine}: Keys mode hides other columns")
    time.sleep(1)                       # saved after a pause
    close_tabs(page)
    exact_tree_row(page, where).first.click(button="right")
    page.click('.wapyt-tree-menu [data-action-id="er"]')
    page.wait_for_selector(f"{view} .bk-er-table", timeout=30000)
    check(er_box(page, "customers").get_attribute("transform") == moved
          and page.locator(f"{view} .bk-er-more").count() > 0,
          f"{engine}: the moved table and the mode are remembered")
    page.fill(f'{view} [data-role="er_find"]', "order_i")
    hot = "bk-er-hot" in (er_box(page, "order_items").get_attribute("class") or "")
    dim = "bk-er-dim" in (er_box(page, "audit_log").get_attribute("class") or "")
    check(hot and dim, f"{engine}: Find picks a table out and dims the unrelated ones")
    with page.expect_download() as download:
        page.click(f'{view} [data-bk="er_svg"]')
    text = Path(download.value.path()).read_text()
    check(text.startswith("<?xml") and "order_items" in text and "<svg" in text,
          f"{engine}: the diagram saves as SVG ({download.value.suggested_filename})")
    page.click(f'{view} [data-bk="er_layout"]')
    time.sleep(1)
    er_box(page, "orders").dblclick()
    check(wait_text(page, f"{view} .bk-head", "orders"), f"{engine}: double-click opens the table")
    close_tabs(page)
    exact_tree_row(page, "orders").first.click(button="right")
    page.click('.wapyt-tree-menu [data-action-id="er_table"]')
    page.wait_for_selector(f"{view} .bk-er-table", timeout=30000)
    shown = sorted(_attr for _attr in page.eval_on_selector_all(
        f"{view} .bk-er-table", "els => els.map(e => e.dataset.t)"))
    check(shown == ["customers", "order_items", "orders"],
          f"{engine}: a table's diagram shows its neighbours ({shown})")


CMP_RESET = {
    "postgresql": "DROP SCHEMA IF EXISTS bk_cmp CASCADE;\nCREATE SCHEMA bk_cmp;",
    "mysql": "DROP DATABASE IF EXISTS bk_cmp;\nCREATE DATABASE bk_cmp;",
    "mssql": ("IF SCHEMA_ID('bk_cmp') IS NOT NULL BEGIN\n"
              "DECLARE @s nvarchar(max) = N'';\n"
              "SELECT @s += N'ALTER TABLE bk_cmp.' + QUOTENAME(OBJECT_NAME(parent_object_id)) "
              "+ N' DROP CONSTRAINT ' + QUOTENAME(name) + N';' FROM sys.foreign_keys "
              "WHERE schema_id = SCHEMA_ID('bk_cmp');\n"
              "SELECT @s += N'DROP VIEW bk_cmp.' + QUOTENAME(name) + N';' FROM sys.views "
              "WHERE schema_id = SCHEMA_ID('bk_cmp');\n"
              "SELECT @s += N'DROP TABLE bk_cmp.' + QUOTENAME(name) + N';' FROM sys.tables "
              "WHERE schema_id = SCHEMA_ID('bk_cmp');\n"
              "EXEC (@s); DROP SCHEMA bk_cmp; END\nGO\nCREATE SCHEMA bk_cmp\nGO\n"),
}


def run_script(page: Page, tab: str, sql: str) -> str:
    """Run a whole script in the SQL tab; its messages."""
    page.click(f"{tab} .cm-content")
    page.keyboard.press("Control+a")
    page.keyboard.insert_text(sql)
    page.keyboard.press("Control+Shift+Enter")
    page.wait_for_selector(f'{tab} [data-bk="run"]:visible', timeout=60000)
    time.sleep(0.5)
    return page.inner_text(f"{tab} .bk-msgs") if page.locator(f"{tab} .bk-msgs").count() else ""


def compare_rows(page: Page, view: str) -> list:
    return page.eval_on_selector_all(
        f'{view} tbody tr[data-row-id] td[data-column-id="object"]', "els => els.map(e => e.textContent)")


def compare_checks(page: Page, engine: str, name: str) -> None:
    """Schema compare: the sample against an empty schema, apply the script, compare again."""
    where = {"postgresql": "public", "mysql": "shop", "mssql": "dbo", "sqlite": name}[engine]
    view = ".bk-view:visible"
    if engine == "sqlite":
        exact_tree_row(page, where).first.click(button="right")
        page.click('.wapyt-tree-menu [data-action-id="compare"]')
        page.wait_for_selector(f'{view} [data-role="cmp_conn"][data-side="tgt"] option', state="attached")
        page.select_option(f'{view} [data-role="cmp_conn"][data-side="tgt"]',
                           page.get_attribute(f'{view} [data-role="cmp_conn"][data-side="src"]'
                                              " option[selected]", "value"))
        time.sleep(0.5)
        page.click(f'{view} [data-bk="cmp_run"]')
        check(wait_text(page, ".wapyt-toast, .wapyt-message", "the same"),
              f"{engine}: comparing a schema with itself is refused")
        return
    exact_tree_row(page, name).first.click(button="right")
    page.click('.wapyt-tree-menu [data-action-id="sql"]')
    page.wait_for_selector(".bk-sqltab:visible .cm-content", timeout=30000)
    msgs = run_script(page, ".bk-sqltab:visible", CMP_RESET[engine])
    check("✘" not in msgs, f"{engine}: an empty bk_cmp to compare with" + (f" ({msgs})" if "✘" in msgs else ""))
    close_tabs(page)
    exact_tree_row(page, where).first.click(button="right")
    page.click('.wapyt-tree-menu [data-action-id="compare"]')
    role = "cmp_db" if engine == "mysql" else "cmp_schema"
    picker = f'{view} [data-role="{role}"][data-side="tgt"]'
    page.wait_for_selector(f'{picker} option[value="bk_cmp"]', state="attached", timeout=30000)
    page.select_option(picker, "bk_cmp")
    time.sleep(0.5)
    page.click(f'{view} [data-bk="cmp_run"]')
    page.wait_for_selector(f"{view} tbody tr[data-row-id]", timeout=60000)
    objects = compare_rows(page, view)
    wanted = {"customers", "products", "orders", "order_items", "audit_log", "order_totals"}
    check(wanted <= set(objects), f"{engine}: the compare lists the sample's tables and view ({len(objects)})")
    count = page.inner_text(f'{view} [id$="-count"]')
    text = page.inner_text(f"{view} .bk-code-host .cm-content")
    check("CREATE TABLE" in text and "Create tables" in text and "statements" in count,
          f"{engine}: the script shows ({count})")
    shot(page, f"smoke-{engine}-18-compare")
    row = page.locator(f'{view} tbody tr:has(td[data-column-id="object"]:text-is("customers"))')
    row.locator('td[data-column-id="use"] input').click()
    time.sleep(0.4)
    fewer = page.inner_text(f'{view} [id$="-count"]')
    check(fewer != count, f"{engine}: unticking a difference changes the script ({fewer})")
    page.click(f'{view} [data-bk="cmp_tick"][data-which="all"]')
    time.sleep(0.4)
    row.dblclick()
    page.wait_for_selector(f"{modal(page)} .bk-pre", timeout=5000)
    check("CREATE TABLE" in page.inner_text(f"{modal(page)} .bk-pre"),
          f"{engine}: double-click shows a difference's statements")
    page.keyboard.press("Escape")
    page.wait_for_selector(".wapyt-modal", state="detached", timeout=5000)
    page.click(f'{view} [data-bk="cmp_sql"]')
    page.wait_for_selector(".bk-sqltab:visible .cm-content", timeout=30000)
    tab = ".bk-sqltab:visible"
    page.click(f"{tab} .cm-content")
    page.keyboard.press("Control+Shift+Enter")
    page.wait_for_selector(f'{tab} [data-bk="run"]:visible', timeout=60000)
    time.sleep(0.5)
    msgs = page.inner_text(f"{tab} .bk-msgs")
    check("✘" not in msgs and "✔" in msgs, f"{engine}: the script runs on the target"
          + ("" if "✘" not in msgs else f" ({[l for l in msgs.splitlines() if '✘' in l][:1]})"))
    page.click('.wapyt-tab:has-text("Compare")')
    page.click(f'{view} [data-bk="cmp_run"]')
    check(wait_text(page, f"{view} .bk-results", "The two schemas are the same", timeout=60000),
          f"{engine}: after the script the schemas compare the same ({compare_rows(page, view)[:5]})")
    close_tabs(page)
    exact_tree_row(page, name).first.click(button="right")
    page.click('.wapyt-tree-menu [data-action-id="sql"]')
    page.wait_for_selector(".bk-sqltab:visible .cm-content", timeout=30000)
    run_script(page, ".bk-sqltab:visible", {
        "postgresql": "DROP SCHEMA bk_cmp CASCADE;", "mysql": "DROP DATABASE bk_cmp;",
        "mssql": CMP_RESET["mssql"].split("GO\nCREATE")[0]}[engine])


def data_compare_checks(page: Page, engine: str, name: str) -> None:
    """Data compare: customers against a changed copy, apply the script, compare again."""
    view = ".bk-view:visible"
    copy = ("SELECT * INTO bk_dcmp FROM customers" if engine == "mssql"
            else "CREATE TABLE bk_dcmp AS SELECT * FROM customers")
    exact_tree_row(page, name).first.click(button="right")
    page.click('.wapyt-tree-menu [data-action-id="sql"]')
    page.wait_for_selector(".bk-sqltab:visible .cm-content", timeout=30000)
    msgs = run_script(page, ".bk-sqltab:visible",
                      f"DROP TABLE IF EXISTS bk_dcmp;\n{copy};\n"
                      "DELETE FROM bk_dcmp WHERE id = 3;\n"
                      "UPDATE bk_dcmp SET name = 'Changed' WHERE id = 2;\n")
    check("✘" not in msgs, f"{engine}: a changed copy of customers")
    close_tabs(page)
    exact_tree_row(page, "customers").first.click(button="right")
    page.click('.wapyt-tree-menu [data-action-id="data_compare"]')
    page.wait_for_selector(f'{view} [data-role="cmp_table"][data-side="src"] option', state="attached",
                           timeout=30000)
    side = f'{view} .bk-cmp-side[data-side="tgt"]'
    src_conn = page.get_attribute(f'{view} [data-role="cmp_conn"][data-side="src"] option[selected]',
                                  "value")
    page.select_option(f'{side} [data-role="cmp_conn"]', src_conn)
    time.sleep(1)
    if engine in ("postgresql", "mssql"):
        page.select_option(f'{side} [data-role="cmp_schema"]',
                           "public" if engine == "postgresql" else "dbo")
    elif engine == "mysql":
        page.select_option(f'{side} [data-role="cmp_db"]', "shop")
    page.wait_for_selector(f'{side} [data-role="cmp_table"] option[value="bk_dcmp"]',
                           state="attached", timeout=30000)
    page.select_option(f'{side} [data-role="cmp_table"]', "bk_dcmp")
    page.click(f'{view} [data-bk="cmp_run"]')
    page.wait_for_selector(f"{view} tbody tr[data-row-id]", timeout=60000)
    changes = sorted(page.eval_on_selector_all(
        f'{view} tbody tr[data-row-id] td[data-column-id="change"]', "els => els.map(e => e.textContent)"))
    columns = page.eval_on_selector_all(
        f'{view} tbody tr[data-row-id] td[data-column-id="columns"]', "els => els.map(e => e.textContent)")
    check(changes == ["Insert", "Update"] and "name" in columns,
          f"{engine}: the data compare finds the missing and the changed row ({changes}, {columns})")
    text = page.inner_text(f"{view} .bk-code-host .cm-content")
    check("UPDATE" in text and "INSERT INTO" in text, f"{engine}: the row script shows")
    shot(page, f"smoke-{engine}-19-data-compare")
    page.locator(f"{view} tbody tr[data-row-id]").first.dblclick()
    page.wait_for_selector(f"{modal(page)} .bk-cmp-kv", timeout=5000)
    check(page.locator(f"{modal(page)} .bk-cmp-kv tr").count() > 3,
          f"{engine}: double-click shows the row's values")
    page.keyboard.press("Escape")
    page.wait_for_selector(".wapyt-modal", state="detached", timeout=5000)
    page.click(f'{view} [data-bk="cmp_sql"]')
    page.wait_for_selector(".bk-sqltab:visible .cm-content", timeout=30000)
    tab = ".bk-sqltab:visible"
    page.click(f"{tab} .cm-content")
    page.keyboard.press("Control+Shift+Enter")
    page.wait_for_selector(f'{tab} [data-bk="run"]:visible', timeout=60000)
    time.sleep(0.5)
    msgs = page.inner_text(f"{tab} .bk-msgs")
    check("✘" not in msgs and "✔" in msgs, f"{engine}: the row script runs on the target"
          + ("" if "✘" not in msgs else f" ({[l for l in msgs.splitlines() if '✘' in l][:1]})"))
    page.click('.wapyt-tab:has-text("Compare data")')
    page.click(f'{view} [data-bk="cmp_run"]')
    check(wait_text(page, f"{view} .bk-results", "The two tables hold the same rows", timeout=60000),
          f"{engine}: after the script the tables hold the same rows")
    close_tabs(page)
    exact_tree_row(page, name).first.click(button="right")
    page.click('.wapyt-tree-menu [data-action-id="sql"]')
    page.wait_for_selector(".bk-sqltab:visible .cm-content", timeout=30000)
    run_script(page, ".bk-sqltab:visible", "DROP TABLE bk_dcmp;")


def ssh_up() -> bool:
    import socket

    try:
        socket.create_connection(("127.0.0.1", 52222), timeout=2).close()
        return True
    except OSError:
        return False


def ssh_checks(page: Page, engine: str) -> None:
    """A connection through the test SSH server (tests/smoke/ssh_server.sh)."""
    if engine == "sqlite":
        return
    if not ssh_up():
        print(f"  skip {engine}: no SSH server on 127.0.0.1:52222", flush=True)
        return
    spec = ENGINES[engine]
    name = f"Smoke {engine} SSH"
    form = ".wapyt-modal"
    page.click('.wapyt-toolbar [data-id="new_conn"]')
    page.wait_for_selector(f'{form} input[name="name"]')
    page.click(f'{form} [data-field-id="engine"] [data-value="{engine}"]')
    time.sleep(0.2)
    check(not page.is_visible(f'{form} input[name="ssh_host"]'), f"{engine}: SSH fields start hidden")
    page.fill(f'{form} input[name="name"]', name)
    page.fill(f'{form} input[name="host"]', "127.0.0.1")
    page.fill(f'{form} input[name="port"]', spec["port"])
    page.fill(f'{form} input[name="username"]', spec["username"])
    page.fill(f'{form} input[name="password"]', DB_PASSWORD)
    page.fill(f'{form} input[name="database"]', spec["database"])
    page.click(f'{form} [data-field-id="ssh_enabled"] input, {form} [data-field-id="ssh_enabled"] [role="switch"]')
    page.wait_for_selector(f'{form} input[name="ssh_host"]', state="visible", timeout=5000)
    page.fill(f'{form} input[name="ssh_host"]', "127.0.0.1")
    page.fill(f'{form} input[name="ssh_port"]', "52222")
    page.fill(f'{form} input[name="ssh_username"]', "tunnel")
    page.fill(f'{form} input[name="ssh_password"]', "Tunn3l-pass")
    shot(page, f"smoke-{engine}-17-ssh-editor")
    page.click(f'{form} [data-button-id="test"]')
    ok = wait_text(page, f'{form} [data-field-id="test_result"]', "through SSH", 30000)
    pinned = wait_text(page, f'{form} [data-field-id="ssh_host_key"]', "pinned when you save", 5000)
    check(ok and pinned, f"{engine}: Test connects through SSH and shows the new host key")
    page.click(f'{form} button[type="submit"]')
    page.wait_for_selector(".wapyt-modal", state="detached", timeout=15000)
    exact_tree_row(page, name).first.click()
    page.keyboard.press("ArrowRight")
    page.wait_for_selector('.wapyt-tree-row:has(.wapyt-tree-label:text-is("customers"))',
                           timeout=30000)
    exact_tree_row(page, "customers").first.dblclick()
    page.wait_for_selector('.bk-view:visible tr[data-row-id="r0"]', timeout=30000)
    check("14 rows" in page.inner_text(".bk-view:visible .bk-head-stats"),
          f"{engine}: a table opens through the tunnel")
    close_tabs(page)
    exact_tree_row(page, name).first.click(button="right")
    page.click('.wapyt-tree-menu [data-action-id]:has-text("Edit connection")')
    page.wait_for_selector(f'{form} [data-field-id="ssh_host_key"]', timeout=10000)
    shown = page.inner_text(f'{form} [data-field-id="ssh_host_key"]')
    stored = page.get_attribute(f'{form} input[name="ssh_password"]', "placeholder") or ""
    check("SHA256:" in shown and "stored" in stored,
          f"{engine}: the editor shows the pinned key, never the SSH password")
    page.click(f'{form} [data-button-id="cancel"], {form} button:has-text("Cancel")')
    page.wait_for_selector(".wapyt-modal", state="detached", timeout=10000)
    delete_connection(page, engine, name)


def close_tabs(page: Page) -> None:
    for _ in range(10):
        close = page.locator(".wapyt-tab-close").first
        if not close.count():
            return
        close.click()
        time.sleep(0.2)


def delete_connection(page: Page, engine: str, name: str) -> None:
    exact_tree_row(page, name).first.click(button="right")
    page.click('.wapyt-tree-menu [data-action-id]:has-text("Delete connection")')
    page.click('.wapyt-msg-dialog .wapyt-msg-btn-primary')
    time.sleep(1)
    check(exact_tree_row(page, name).count() == 0, f"{engine}: connection deleted")


def conn_id_of(page: Page, name: str) -> int:
    node = exact_tree_row(page, name).first.get_attribute("data-node-id") or ""
    return int(node.split(":")[1])


def main() -> int:
    if not PASSWORD:
        print("set BASILISK_PASS", file=sys.stderr)
        return 2
    engines = sys.argv[1:] or list(ENGINES)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        context.grant_permissions(["clipboard-read", "clipboard-write"])
        page = context.new_page()
        page.on("console", lambda m: console.append(f"{m.type}: {m.text}")
                if m.type == "error" else None)
        page.on("pageerror", lambda e: console.append(f"pageerror: {e}"))
        page.goto(URL + "login")
        page.fill('input[name="email"]', USER)
        page.fill('input[name="password"]', PASSWORD)
        page.click('input[type="submit"], button[type="submit"]')
        page.wait_for_selector(".bk-sidebar", timeout=90000)
        page.wait_for_function("() => window.BkEditor", timeout=30000)
        for engine in engines:
            print(f"\n== {engine}", flush=True)
            name = f"Smoke {engine}"
            try:
                create_connection(page, engine, name)
                open_customers(page, engine, name)
                table_checks(page, engine)
                value_panel_checks(page, engine)
                close_tabs(page)
                open_customers(page, engine, name)
                export_checks(page, engine, conn_id_of(page, name))
                sql_checks(page, engine)
                generate_check(page, engine)
                navigator_checks(page, engine)
                close_tabs(page)
                object_action_checks(page, engine, name)
                close_tabs(page)
                open_customers(page, engine, name)
                grid_tool_checks(page, engine, name)
                close_tabs(page)
                import_checks(page, engine)
                close_tabs(page)
                session_checks(page, engine, name)
                close_tabs(page)
                er_checks(page, engine, name)
                close_tabs(page)
                compare_checks(page, engine, name)
                close_tabs(page)
                data_compare_checks(page, engine, name)
                close_tabs(page)
                ssh_checks(page, engine)
            except Exception as exc:  # noqa: BLE001 - report and carry on
                check(False, f"{engine}: {type(exc).__name__}: {str(exc).splitlines()[0]}")
                shot(page, f"smoke-{engine}-error")
            finally:
                close_tabs(page)
                try:
                    delete_connection(page, engine, name)
                except Exception as exc:  # noqa: BLE001
                    check(False, f"{engine}: delete: {exc}")
        browser.close()
    real = [line for line in console if "favicon" not in line]
    check(not real, "no browser console errors" + ("" if not real else ":\n    " + "\n    ".join(real[:10])))
    print(f"\n{len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
