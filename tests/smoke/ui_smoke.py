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

    page.click(f'{tab} [data-bk="history"]')
    page.wait_for_selector(f"{modal(page)} tbody tr", timeout=10000)
    rows = page.locator(f"{modal(page)} tbody tr").count()
    check(rows >= 3, f"{engine}: history lists the runs ({rows})")
    page.keyboard.press("Escape")
    page.wait_for_selector(".wapyt-modal", state="detached", timeout=5000)


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
        page = browser.new_page(viewport={"width": 1440, "height": 900})
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
                export_checks(page, engine, conn_id_of(page, name))
                sql_checks(page, engine)
                generate_check(page, engine)
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
