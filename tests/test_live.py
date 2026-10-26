"""
sqlops against real databases: the four test containers and a SQLite file
(CLAUDE.md, *Test databases*). Run ``tests/smoke/seed_sample.py`` first; an
engine whose server is not reachable is skipped.

    uv run --group dev pytest tests/test_live.py
"""
from __future__ import annotations

import os
import shutil
import threading
import time
from pathlib import Path

import pytest

PASSWORD = os.environ.get("BASILISK_TEST_PASSWORD", "B@sil:isk/1")
ROOT = Path(__file__).resolve().parents[1]

PROFILES = {
    "postgresql": dict(host="127.0.0.1", port=55432, username="postgres", database="shop"),
    "mysql": dict(host="127.0.0.1", port=53306, username="root", database="shop"),
    "mssql": dict(host="127.0.0.1", port=51433, username="sa", database="shop"),
    "sqlite": dict(database="shop.db"),
    "duckdb": dict(database="shop.duckdb"),
    "cockroachdb": dict(host="127.0.0.1", port=56257, username="root", database="shop",
                        options={"sslmode": "disable"}),
}
FILE_ENGINES = ("sqlite", "duckdb")
# Where the sample lives in each engine's tree: (database, schema).
LOCATION = {
    "postgresql": ("shop", "public"),
    "mysql": ("shop", ""),
    "mssql": ("shop", "dbo"),
    "sqlite": ("", ""),
    "duckdb": ("", ""),
    "cockroachdb": ("shop", "public"),
}


@pytest.fixture(scope="session")
def user_id():
    from services.db import get_db, hash_password, init_db

    init_db()
    with get_db() as conn:
        conn.execute("INSERT INTO users (username, pw_hash, is_admin) VALUES ('tester', ?, 0)",
                     (hash_password("x" * 8),))
        return conn.execute("SELECT id FROM users WHERE username = 'tester'").fetchone()[0]


def _make_profile(user_id: int, engine: str, **extra) -> int:
    from services.connection_service import ConnectionService

    values = {**PROFILES[engine], **extra}
    password = "" if engine in FILE_ENGINES else PASSWORD
    result = ConnectionService({"user_id": user_id}).save(
        name=f"{engine} test", engine=engine, password=password, **values)
    assert result["ok"], result
    return result["id"]


@pytest.fixture(scope="session", params=list(PROFILES))
def profile(request, user_id):
    engine = request.param
    if engine in FILE_ENGINES:
        source = ROOT / "data" / "sqlite" / PROFILES[engine]["database"]
        if not source.exists():
            pytest.skip("run tests/smoke/seed_sample.py first")
        target = Path(os.environ["BASILISK_SQLITE_ROOT"]) / PROFILES[engine]["database"]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(source, target)
    conn_id = _make_profile(user_id, engine)
    from services import sqlops

    try:
        sqlops.ping(user_id, conn_id)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"{engine} not reachable: {exc}")
    return engine, conn_id


def test_tree(user_id, profile):
    from services import sqlops

    engine, conn_id = profile
    database, schema = LOCATION[engine]
    if database:
        names = [row["name"] for row in sqlops.databases(user_id, conn_id)]
        assert "shop" in names
    if schema:
        names = [row["name"] for row in sqlops.schemas(user_id, conn_id, database)["schemas"]]
        assert schema in names
        if engine in ("postgresql", "mssql", "cockroachdb"):
            assert "sales" in names
            assert not any(n.startswith("pg_") or n == "sys" for n in names)
    found = sqlops.objects(user_id, conn_id, database, schema)
    tables = {row["name"] for row in found["tables"]}
    assert {"customers", "products", "orders", "order_items", "audit_log"} <= tables
    assert "order_totals" in {row["name"] for row in found["views"]}


def test_table_info(user_id, profile):
    from services import sqlops

    engine, conn_id = profile
    database, schema = LOCATION[engine]
    info = sqlops.table_info(user_id, conn_id, database, schema, "order_items")
    assert info["pk"] == ["order_id", "sku"]
    assert info["editable"]
    refs = {fk["ref_table"] for fk in info["foreign_keys"]}
    assert refs == {"orders", "products"}
    assert "CREATE TABLE" in info["ddl"].upper()
    orders = sqlops.table_info(user_id, conn_id, database, schema, "orders")
    assert any(ix["columns"] == ["customer_id"] for ix in orders["indexes"])
    audit = sqlops.table_info(user_id, conn_id, database, schema, "audit_log")
    assert audit["pk"] == [] and not audit["editable"]
    view = sqlops.table_info(user_id, conn_id, database, schema, "order_totals")
    assert view["kind"] == "view" and not view["editable"]
    assert "order_totals" in view["ddl"].lower()


def test_browse_pages_filters_and_counts(user_id, profile):
    from services import sqlops

    engine, conn_id = profile
    database, schema = LOCATION[engine]
    page = sqlops.browse(user_id, conn_id, database, schema, "orders", limit=50, pk=["id"])
    assert page["total"] == 137 and page["exact"]
    assert len(page["rows"]) == 50 and page["rows"][0][0] == "1"
    assert len(page["keys"]) == 50
    last = sqlops.browse(user_id, conn_id, database, schema, "orders", offset=100, limit=50, pk=["id"])
    assert len(last["rows"]) == 37
    filtered = sqlops.browse(user_id, conn_id, database, schema, "orders",
                             where="status = 'paid' AND id < 50", order_by="id DESC", pk=["id"])
    assert filtered["total"] == len(filtered["rows"]) > 0
    ids = [int(row[0]) for row in filtered["rows"]]
    assert ids == sorted(ids, reverse=True)
    # "%" and ":" in typed SQL are literal, whatever the driver's paramstyle.
    odd = sqlops.browse(user_id, conn_id, database, schema, "customers",
                        where="name LIKE 'A%' OR email LIKE '%:%'", pk=["id"])
    assert len(odd["rows"]) == 2 and all(row[1].startswith("A") for row in odd["rows"])
    nokey = sqlops.browse(user_id, conn_id, database, schema, "audit_log", limit=10)
    assert len(nokey["rows"]) == 5 and nokey["keys"] == []
    with pytest.raises(sqlops.OpError):
        sqlops.browse(user_id, conn_id, database, schema, "orders", where="no_such_column = 1")


def test_values_round_trip_display(user_id, profile):
    from services import sqlops

    engine, conn_id = profile
    database, schema = LOCATION[engine]
    page = sqlops.browse(user_id, conn_id, database, schema, "products", pk=["sku"])
    cols = page["columns"]
    by_sku = {row[0]: dict(zip(cols, row)) for row in page["rows"]}
    assert by_sku["SKU-002"]["stock"] == "9007199254740993"  # no float rounding
    assert by_sku["SKU-000"]["price"] == "19.99"
    assert by_sku["SKU-001"]["photo"] is None
    assert by_sku["SKU-000"]["photo"].startswith("0x89504e47")
    assert [r for r, c in page["truncated"]]  # the 5 KB photo is cut for display
    people = sqlops.browse(user_id, conn_id, database, schema, "customers", pk=["id"])
    names = [row[1] for row in people["rows"]]
    assert "Zoë Ünicode 🦎" in names and "O'Brien; Robert" in names


def test_save_changes_update_insert_delete(user_id, profile):
    from services import sqlops
    from services.values import encode_key

    engine, conn_id = profile
    database, schema = LOCATION[engine]
    key = {"id": encode_key(3)}
    preview = sqlops.save_changes(user_id, conn_id, database, schema, "customers", [
        {"row": "r3", "op": "update", "key": key,
         "values": {"name": "Alan M. Turing", "vip": "true", "joined": "2026-02-03", "notes": None}},
    ], dry_run=True)
    assert preview["ok"] and "UPDATE" in preview["sql"][0].upper()
    done = sqlops.save_changes(user_id, conn_id, database, schema, "customers", [
        {"row": "r3", "op": "update", "key": key,
         "values": {"name": "Alan M. Turing", "vip": "true", "joined": "2026-02-03", "notes": None}},
        {"row": "n1", "op": "insert", "values": {"name": "New Person", "vip": "false",
                                                  "email": "new@example.com"}},
    ])
    assert done["ok"] and done["count"] == 2
    page = sqlops.browse(user_id, conn_id, database, schema, "customers",
                         where="id = 3 OR email = 'new@example.com'", pk=["id"])
    rows = {row[2] or "": row for row in page["rows"]}
    assert rows["user3@example.com"][1] == "Alan M. Turing"
    assert rows["user3@example.com"][4] == "2026-02-03"
    new_id = int(rows["new@example.com"][0])
    gone = sqlops.save_changes(user_id, conn_id, database, schema, "customers", [
        {"row": "x", "op": "delete", "key": {"id": encode_key(new_id)}}])
    assert gone["ok"]
    bad = sqlops.save_changes(user_id, conn_id, database, schema, "customers", [
        {"row": "r3", "op": "update", "key": key, "values": {"joined": "not a date"}}])
    assert not bad["ok"] and bad["errors"][0]["column"] == "joined"
    # A key that matches nothing: nothing saved, a clear error.
    with pytest.raises(sqlops.OpError, match="matched 0 rows"):
        sqlops.save_changes(user_id, conn_id, database, schema, "customers", [
            {"row": "r3", "op": "update", "key": key, "values": {"name": "Kept"}},
            {"row": "ghost", "op": "update", "key": {"id": encode_key(99999)},
             "values": {"name": "x"}}])
    page = sqlops.browse(user_id, conn_id, database, schema, "customers", where="id = 3", pk=["id"])
    assert page["rows"][0][1] == "Alan M. Turing"  # the batch rolled back


def test_execute_script_statement_and_errors(user_id, profile):
    from services import sqlops

    engine, conn_id = profile
    database, _schema = LOCATION[engine]
    script = ("SELECT COUNT(*) AS n FROM orders;\n\n"
              "SELECT '100%' AS pct, 'a;b' AS semi;\n"
              "UPDATE orders SET status = status WHERE id = 1;\n")
    if engine == "mssql":
        script = script.replace(";\n\n", ";\nGO\n").replace(";\nUPDATE", ";\nGO\nUPDATE")
    out = sqlops.execute(user_id, conn_id, database, script)
    assert out["ok"], out
    results = out["results"]
    assert results[0]["rows"] == [["137"]]
    assert results[1]["rows"] == [["100%", "a;b"]]
    assert results[-1]["columns"] is None and results[-1]["rowcount"] == 1
    caret = script.index("'100%'")
    one = sqlops.execute(user_id, conn_id, database, script, mode="statement", caret=caret)
    assert len(one["results"]) == 1 and one["results"][0]["rows"] == [["100%", "a;b"]]
    bad = sqlops.execute(user_id, conn_id, database,
                         "SELECT 1 AS a;\nSELEC oops;\nSELECT 2 AS b;" if engine != "mssql"
                         else "SELECT 1 AS a\nGO\nSELEC oops\nGO\nSELECT 2 AS b")
    assert not bad["ok"]
    assert len(bad["results"]) == 2 and bad["results"][1]["error"]
    capped = sqlops.execute(user_id, conn_id, database, "SELECT * FROM orders", max_rows=10)
    assert len(capped["results"][0]["rows"]) == 10 and capped["results"][0]["more"]
    assert sqlops.history(user_id, conn_id)[0]["sql"].startswith("SELECT * FROM orders")


def test_read_only_profile_refuses_writes(user_id, profile):
    from services import sqlops

    engine, conn_id = profile
    if engine == "duckdb":
        # DuckDB opens a file one way per process: close the read-write one first.
        from services.engine_pool import pool

        pool.close(user_id, conn_id)
    ro = _make_profile(user_id, engine, read_only=True)
    database, schema = LOCATION[engine]
    out = sqlops.execute(user_id, ro, database, "UPDATE orders SET status = 'x' WHERE id = 2")
    page = sqlops.browse(user_id, ro, database, schema, "orders", where="id = 2", pk=["id"])
    assert page["rows"][0][-1] != "x"
    if engine != "mssql":  # SQL Server: it ran, then was rolled back
        assert not out["ok"]
    with pytest.raises(sqlops.OpError, match="read-only"):
        sqlops.save_changes(user_id, ro, database, schema, "orders", [
            {"row": "r", "op": "delete", "key": {"id": ["i", "2"]}}])
    if engine == "duckdb":
        from services.engine_pool import pool

        pool.close(user_id, ro)


def test_prod_profile_asks_before_writing(user_id, profile):
    from services import sqlops

    engine, _ = profile
    prod = _make_profile(user_id, engine, env="prod")
    database, _schema = LOCATION[engine]
    asked = sqlops.execute(user_id, prod, database, "SELECT 1 AS a; DELETE FROM audit_log WHERE 1 = 0"
                           if engine != "mssql" else "DELETE FROM audit_log WHERE 1 = 0")
    assert asked.get("needs_confirm") and asked["count"] == 1
    assert sqlops.execute(user_id, prod, database, "SELECT 1 AS a")["ok"]
    assert sqlops.execute(user_id, prod, database, "DELETE FROM audit_log WHERE 1 = 0",
                          confirmed=True)["ok"]


def test_cancel_and_timeout(user_id, profile):
    from services import sqlops

    engine, conn_id = profile
    database, _schema = LOCATION[engine]
    slow = {
        "postgresql": "SELECT pg_sleep(30)",
        "mysql": "SELECT SLEEP(30)",
        "mssql": "WAITFOR DELAY '00:00:30'",
        "sqlite": "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) "
                  "SELECT COUNT(*) FROM c",
        "duckdb": "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) "
                  "SELECT COUNT(*) FROM c",
        "cockroachdb": "SELECT pg_sleep(30)",
    }[engine]
    box: dict = {}
    worker = threading.Thread(target=lambda: box.update(
        out=sqlops.execute(user_id, conn_id, database, slow, run_id="slow-1")))
    started = time.monotonic()
    worker.start()
    time.sleep(1.5)
    assert sqlops.cancel(user_id, "slow-1")["ok"]
    worker.join(20)
    assert not worker.is_alive()
    assert time.monotonic() - started < 15
    result = box["out"]["results"][0]
    if engine == "mysql":  # KILL QUERY makes SLEEP() return 1 early, not fail
        assert result.get("error") or result["rows"] == [["1"]]
    else:
        assert result.get("error", "").startswith("Cancelled")
    # The pool still works afterwards.
    assert sqlops.execute(user_id, conn_id, database, "SELECT 1 AS one")["ok"]

    quick = _make_profile(user_id, engine, timeout_s=1)
    started = time.monotonic()
    out = sqlops.execute(user_id, quick, database, slow)
    assert time.monotonic() - started < 10
    if engine != "mysql":
        assert not out["ok"]


def test_completion_and_scripts(user_id, profile):
    from services import sqlops

    engine, conn_id = profile
    database, schema = LOCATION[engine]
    found = sqlops.completion(user_id, conn_id, database)
    tables = found["schema"] if engine in FILE_ENGINES else found["schema"][schema or database]
    assert "email" in tables["customers"]
    for kind in ("select", "insert", "update", "delete", "ddl"):
        text = sqlops.script(user_id, conn_id, database, schema, "customers", kind)
        assert "customers" in text
    assert sqlops.format_sql("select a,b from t where x=1").startswith("SELECT a,")


def test_json_cell_edit_saves_an_object(user_id, profile):
    from services import sqlops
    from services.values import encode_key

    engine, conn_id = profile
    database, schema = LOCATION[engine]
    done = sqlops.save_changes(user_id, conn_id, database, schema, "products", [
        {"row": "r", "op": "update", "key": {"sku": encode_key("SKU-004")},
         "values": {"attributes": '{"mist": true, "ml": 500}'}}])
    assert done["ok"], done
    page = sqlops.browse(user_id, conn_id, database, schema, "products",
                         where="sku = 'SKU-004'", pk=["sku"])
    shown = dict(zip(page["columns"], page["rows"][0]))["attributes"]
    import json

    assert json.loads(shown) == {"mist": True, "ml": 500}, shown  # an object, not a string


# ---------------------------------------------------------------------------
# Manual commit mode (sessions.py)
# ---------------------------------------------------------------------------

def _status_of(user_id, conn_id, engine, order_id):
    from services import sqlops

    database, schema = LOCATION[engine]
    page = sqlops.browse(user_id, conn_id, database, schema, "orders", where=f"id = {order_id}",
                         pk=["id"])
    return dict(zip(page["columns"], page["rows"][0]))["status"]


def test_manual_commit_rollback_and_commit(user_id, profile):
    from services import sqlops

    engine, conn_id = profile
    database, _schema = LOCATION[engine]
    sid = f"t-{engine}-1"
    before = _status_of(user_id, conn_id, engine, 5)
    out = sqlops.execute(user_id, conn_id, database,
                         "UPDATE orders SET status = 'manual' WHERE id = 5", session=sid)
    assert out["ok"] and out["txn"]["open"] and out["txn"]["statements"] == 1, out
    # The same session sees its own change.
    seen = sqlops.execute(user_id, conn_id, database, "SELECT status FROM orders WHERE id = 5",
                          session=sid)
    assert seen["results"][0]["rows"] == [["manual"]]
    if engine in ("postgresql", "mysql"):  # others would block on the row lock
        assert _status_of(user_id, conn_id, engine, 5) == before  # not visible outside
    rolled = sqlops.transaction(user_id, sid, "rollback")
    assert rolled["ok"] and not rolled["txn"]["open"] and rolled["txn"]["done"] == 2
    assert _status_of(user_id, conn_id, engine, 5) == before

    sqlops.execute(user_id, conn_id, database,
                   "UPDATE orders SET status = 'kept' WHERE id = 5", session=sid)
    assert sqlops.transaction(user_id, sid, "commit")["ok"]
    assert _status_of(user_id, conn_id, engine, 5) == "kept"
    sqlops.execute(user_id, conn_id, database,
                   f"UPDATE orders SET status = '{before}' WHERE id = 5", session=sid)
    sqlops.transaction(user_id, sid, "commit")
    ended = sqlops.transaction(user_id, sid, "end")
    assert ended["ok"] and ended["ended"]
    assert sqlops.history(user_id, conn_id)[0]["sql"] == "COMMIT"


def test_manual_mode_survives_a_failed_statement(user_id, profile):
    from services import sqlops

    engine, conn_id = profile
    database, _schema = LOCATION[engine]
    sid = f"t-{engine}-2"
    sqlops.execute(user_id, conn_id, database,
                   "UPDATE orders SET status = 'step1' WHERE id = 6", session=sid)
    bad = sqlops.execute(user_id, conn_id, database, "SELECT no_such_column FROM orders",
                         session=sid)
    assert not bad["ok"]
    if engine == "postgresql":
        assert "still open" in bad["results"][0]["error"]  # the savepoint undid only it
    assert not bad["txn"]["failed"] and bad["txn"]["open"]
    done = sqlops.transaction(user_id, sid, "commit")
    assert done["ok"], done
    assert _status_of(user_id, conn_id, engine, 6) == "step1"
    sqlops.transaction(user_id, sid, "end")


def test_manual_mode_guards(user_id, profile, monkeypatch):
    from services import sessions, sqlops

    engine, conn_id = profile
    database, _schema = LOCATION[engine]
    sid = f"t-{engine}-3"
    before = _status_of(user_id, conn_id, engine, 7)
    sqlops.execute(user_id, conn_id, database,
                   "UPDATE orders SET status = 'idle' WHERE id = 7", session=sid)
    if engine in ("postgresql", "mssql"):
        moved = sqlops.execute(user_id, conn_id, "postgres" if engine == "postgresql" else "master",
                               "SELECT 1 AS one", session=sid)
        assert not moved["ok"] and "switching database" in moved["error"]
    # Idle past the limit: rolled back by the reaper, and the page is told once.
    assert sessions.reap(now=sessions.peek(user_id, sid).last_used + sessions.IDLE_SECONDS + 1) >= 1
    assert _status_of(user_id, conn_id, engine, 7) == before
    told = sqlops.execute(user_id, conn_id, database, "SELECT 1 AS one", session=sid)
    assert not told["ok"] and "idle" in told["error"]
    again = sqlops.execute(user_id, conn_id, database, "SELECT 1 AS one", session=sid)
    assert again["ok"]
    sqlops.transaction(user_id, sid, "end")

    ro = _make_profile(user_id, engine, read_only=True)
    refused = sqlops.execute(user_id, ro, database, "SELECT 1 AS one", session=f"t-{engine}-ro")
    assert not refused["ok"] and "read-only" in refused["error"]


def test_editing_a_profile_ends_its_transaction(user_id, profile):
    from services import sqlops
    from services.connection_service import ConnectionService

    engine, _ = profile
    conn_id = _make_profile(user_id, engine)
    database, _schema = LOCATION[engine]
    sid = f"t-{engine}-4"
    sqlops.execute(user_id, conn_id, database,
                   "UPDATE orders SET status = 'gone' WHERE id = 8", session=sid)
    ConnectionService({"user_id": user_id}).disconnect(conn_id)
    assert _status_of(user_id, conn_id, engine, 8) != "gone"
    told = sqlops.transaction(user_id, sid, "commit")
    assert not told["ok"] and "disconnected" in told["error"]


# ---------------------------------------------------------------------------
# Explain plans
# ---------------------------------------------------------------------------

JOIN = ("SELECT c.name, COUNT(*) AS n FROM customers c JOIN orders o ON o.customer_id = c.id "
        "WHERE o.status = 'paid' GROUP BY c.name ORDER BY n DESC;")


def test_explain_estimated_and_actual(user_id, profile):
    from services import sqlops

    engine, conn_id = profile
    database, _schema = LOCATION[engine]
    script = "SELECT 1 AS warmup;\n\n" + JOIN
    plan = sqlops.explain(user_id, conn_id, database, script, caret=len(script) - 5)
    assert plan["sql"].startswith("SELECT c.name") and plan["start"] > 0
    ops = " | ".join(row["op"] + " " + row["object"] for row in plan["rows"])
    assert "orders" in ops or " o" in ops, ops
    assert all(row["act_rows"] is None for row in plan["rows"])
    if engine == "sqlite":
        with pytest.raises(sqlops.OpError, match="cannot run"):
            sqlops.explain(user_id, conn_id, database, JOIN, analyze=True)
        return
    assert max(row["depth"] for row in plan["rows"]) >= 2
    actual = sqlops.explain(user_id, conn_id, database, JOIN, analyze=True)
    assert actual["analyzed"] and any(row["act_rows"] for row in actual["rows"])


def test_explain_analyze_of_a_write_is_rolled_back(user_id, profile):
    from services import sqlops

    engine, conn_id = profile
    if engine == "sqlite":
        return
    database, _schema = LOCATION[engine]
    write = "UPDATE orders SET status = 'explained' WHERE id = 10"
    asked = sqlops.explain(user_id, conn_id, database, write, analyze=True)
    assert asked.get("needs_confirm")
    if engine == "mysql":  # EXPLAIN ANALYZE: SELECT and multi-table UPDATE/DELETE only
        try:
            sqlops.explain(user_id, conn_id, database, write, analyze=True, confirmed=True)
        except sqlops.OpError:
            pass
    else:
        done = sqlops.explain(user_id, conn_id, database, write, analyze=True, confirmed=True)
        assert done["rolled_back"]
    assert _status_of(user_id, conn_id, engine, 10) != "explained"


# ---------------------------------------------------------------------------
# The value panel: one cell uncut, by primary key
# ---------------------------------------------------------------------------

def test_cell_value_reads_a_cut_value_whole(user_id, profile):
    import base64

    from services import sqlops
    from services.values import MAX_CELL_CHARS, encode_key

    engine, conn_id = profile
    database, schema = LOCATION[engine]
    photo = sqlops.cell_value(user_id, conn_id, database, schema, "products",
                              {"sku": encode_key("SKU-003")}, "photo")
    assert photo["binary"] and base64.b64decode(photo["b64"]) == bytes(range(256)) * 20
    png = sqlops.cell_value(user_id, conn_id, database, schema, "products",
                            {"sku": encode_key("SKU-000")}, "photo")
    assert base64.b64decode(png["b64"]).startswith(b"\x89PNG\r\n\x1a\n")
    page = sqlops.browse(user_id, conn_id, database, schema, "customers", where="id = 11",
                         pk=["id"])
    notes_col = page["columns"].index("notes")
    assert [0, notes_col] in page["truncated"]
    note = sqlops.cell_value(user_id, conn_id, database, schema, "customers",
                             {"id": encode_key(11)}, "notes")
    assert len(note["text"]) > MAX_CELL_CHARS and note["text"].endswith("was happy.")
    assert sqlops.cell_value(user_id, conn_id, database, schema, "products",
                             {"sku": encode_key("SKU-001")}, "photo")["null"]
    with pytest.raises(sqlops.OpError, match="no longer there"):
        sqlops.cell_value(user_id, conn_id, database, schema, "products",
                          {"sku": encode_key("SKU-999")}, "photo")
    with pytest.raises(sqlops.OpError, match="No column"):
        sqlops.cell_value(user_id, conn_id, database, schema, "products",
                          {"sku": encode_key("SKU-000")}, "nope")
    with pytest.raises(sqlops.OpError, match="primary key"):
        sqlops.cell_value(user_id, conn_id, database, schema, "products", {}, "photo")


def test_long_value_edit_saves_whole_and_previews_cut(user_id, profile):
    from services import sqlops
    from services.values import encode_key

    engine, conn_id = profile
    database, schema = LOCATION[engine]
    key = {"id": encode_key(11)}
    original = sqlops.cell_value(user_id, conn_id, database, schema, "customers", key,
                                 "notes")["text"]
    edited = original + "\nLine 0541: added from the value panel."
    change = [{"row": "r10", "op": "update", "key": key, "values": {"notes": edited}}]
    preview = sqlops.save_changes(user_id, conn_id, database, schema, "customers", change,
                                  dry_run=True)
    assert preview["ok"] and "characters in all" in preview["sql"][0]
    assert sqlops.save_changes(user_id, conn_id, database, schema, "customers", change)["ok"]
    try:
        back = sqlops.cell_value(user_id, conn_id, database, schema, "customers", key, "notes")
        assert back["text"] == edited
    finally:
        sqlops.save_changes(user_id, conn_id, database, schema, "customers", [
            {"row": "r10", "op": "update", "key": key, "values": {"notes": original}}])
    binary = [{"row": "r", "op": "update", "key": {"sku": encode_key("SKU-004")},
               "values": {"photo": "0x" + bytes(range(256)).hex() * 10}}]
    assert sqlops.save_changes(user_id, conn_id, database, schema, "products", binary)["ok"]
    import base64

    photo = sqlops.cell_value(user_id, conn_id, database, schema, "products",
                              {"sku": encode_key("SKU-004")}, "photo")
    assert base64.b64decode(photo["b64"]) == bytes(range(256)) * 10
    sqlops.save_changes(user_id, conn_id, database, schema, "products", [
        {"row": "r", "op": "update", "key": {"sku": encode_key("SKU-004")},
         "values": {"photo": None}}])


def test_cell_download_route(user_id, profile):
    import json as _json

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from starlette.middleware.sessions import SessionMiddleware

    from services import transfer
    from services.values import encode_key

    engine, conn_id = profile
    database, schema = LOCATION[engine]
    from starlette.responses import JSONResponse

    app = FastAPI()

    def _login(request):  # a plain Starlette route: no annotations to resolve
        request.session["user"] = {"user_id": user_id}
        return JSONResponse({})

    app.add_route("/login-for-test", _login)

    app.include_router(transfer.router)
    app.add_middleware(SessionMiddleware, secret_key="test")
    client = TestClient(app)
    params = {"table": "products", "column": "photo", "database": database, "schema": schema,
              "key": _json.dumps({"sku": encode_key("SKU-003")})}
    assert client.get(f"/bk/cell/{conn_id}", params=params).status_code == 401
    client.get("/login-for-test")
    got = client.get(f"/bk/cell/{conn_id}", params=params)
    assert got.status_code == 200, got.text
    assert got.content == bytes(range(256)) * 20
    assert got.headers["content-type"] == "application/octet-stream"
    assert 'filename="products_photo.bin"' in got.headers["content-disposition"]
    params.update(table="customers", column="notes", key=_json.dumps({"id": encode_key(11)}))
    note = client.get(f"/bk/cell/{conn_id}", params=params)
    assert note.status_code == 200 and note.text.startswith("Line 0001:")
    params.update(key=_json.dumps({"id": encode_key(2)}))   # NULL notes
    assert client.get(f"/bk/cell/{conn_id}", params=params).status_code == 404
    params.update(key="not json")
    assert client.get(f"/bk/cell/{conn_id}", params=params).status_code == 400


# ---------------------------------------------------------------------------
# More navigator objects: routines, sequences, triggers, roles, matviews
# ---------------------------------------------------------------------------

GROUPS = {
    "postgresql": ["functions", "procedures", "sequences", "triggers"],
    "mysql": ["functions", "procedures", "triggers"],
    "mssql": ["functions", "procedures", "sequences", "triggers"],
    "sqlite": ["triggers"],
    "duckdb": ["sequences"],
    "cockroachdb": ["functions", "procedures", "sequences", "triggers"],
}
PG_LIKE = ("postgresql", "cockroachdb")


def test_navigator_lists_routines_sequences_triggers(user_id, profile):
    from services import sqlops

    engine, conn_id = profile
    database, schema = LOCATION[engine]
    found = sqlops.objects(user_id, conn_id, database, schema)
    assert found["groups"] == GROUPS[engine] and found["errors"] == {}
    if engine == "duckdb":
        assert {s["name"] for s in found["sequences"]} >= {"invoice_no", "customers_id_seq"}
        assert not found.get("routines") and not found.get("triggers")
        return
    triggers = {(t["name"], t["table"]) for t in found["triggers"]}
    assert ("orders_status_check", "orders") in triggers
    trigger = next(t for t in found["triggers"] if t["name"] == "orders_status_check")
    assert trigger["timing"] in ("BEFORE", "AFTER") and "UPDATE" in trigger["events"].upper()
    routines = {(r["name"], r["kind"]) for r in found["routines"]}
    sequences = {s["name"] for s in found["sequences"]}
    if engine == "sqlite":
        assert routines == set() and sequences == set()
        return
    assert {("order_total", "function"), ("archive_order", "procedure")} <= routines
    if engine in PG_LIKE:
        overloads = [r for r in found["routines"] if r["name"] == "order_total"]
        integer = "integer" if engine == "postgresql" else "int8"
        assert sorted(r["args"] for r in overloads) == [
            f"p_order {integer}", f"p_order {integer}, p_tax numeric"]
        assert len({r["id"] for r in overloads}) == 2
        assert "invoice_no" in sequences
        if engine == "postgresql":      # CockroachDB's SERIAL is unique_rowid(), no sequence
            assert "customers_id_seq" in sequences
        assert any(v["name"] == "product_sales" and v["materialized"] for v in found["views"])
    if engine == "mssql":
        assert "invoice_no" in sequences
        assert ("customer_orders", "function") in routines


def test_object_info_definitions_and_call_templates(user_id, profile):
    from services import sqlops

    engine, conn_id = profile
    database, schema = LOCATION[engine]
    found = sqlops.objects(user_id, conn_id, database, schema)
    if engine == "duckdb":
        seq = sqlops.object_info(user_id, conn_id, database, schema, "sequence", "invoice_no")
        assert dict(seq["properties"])["Start"] == "1000"
        assert seq["definition"].upper().startswith("CREATE SEQUENCE")
        return
    trigger = sqlops.object_info(user_id, conn_id, database, schema, "trigger",
                                 "orders_status_check", "orders")
    assert "orders_status_check" in trigger["definition"]
    assert trigger["table"] == "orders"
    assert dict(trigger["properties"])["Table"] == "orders"
    if engine == "sqlite":
        return
    by_name = {}
    for r in found["routines"]:
        by_name.setdefault(r["name"], r)
    total = sqlops.object_info(user_id, conn_id, database, schema, "function",
                               by_name["order_total"]["id"])
    assert "order_total" in total["definition"] and total["definition"].upper().count("CREATE") >= 1
    assert total["call_sql"].lstrip().upper().startswith("SELECT")
    assert "order_total" in total["call_sql"] and "NULL" in total["call_sql"]
    proc = sqlops.object_info(user_id, conn_id, database, schema, "procedure",
                              by_name["archive_order"]["id"])
    first = {"postgresql": "CALL", "cockroachdb": "CALL", "mysql": "CALL",
             "mssql": "DECLARE"}[engine]
    assert proc["call_sql"].startswith(first), proc["call_sql"]
    if engine == "mysql":
        assert "@p_moved" in proc["call_sql"] and proc["call_sql"].rstrip().endswith("SELECT @p_moved;")
    if engine == "mssql":
        assert "@moved = @out_moved OUTPUT" in proc["call_sql"]
        table_fn = sqlops.object_info(user_id, conn_id, database, schema, "function",
                                      by_name["customer_orders"]["id"])
        assert table_fn["call_sql"].startswith("SELECT * FROM")
    if engine in PG_LIKE:
        table_fn = sqlops.object_info(user_id, conn_id, database, schema, "function",
                                      by_name["customer_orders"]["id"])
        assert table_fn["call_sql"].startswith("SELECT * FROM")
        assert dict(total["properties"])["Comment"] == "Sum of an order's lines"
    if engine in ("postgresql", "mssql", "cockroachdb"):
        seq = sqlops.object_info(user_id, conn_id, database, schema, "sequence", "invoice_no")
        assert dict(seq["properties"])["Start"] == "1000"
        assert seq["definition"].startswith("CREATE SEQUENCE")
    # The calls run: the templates are valid SQL once the NULLs are filled.
    ran = sqlops.execute(user_id, conn_id, database,
                         total["call_sql"].replace("NULL", "1", 1))
    assert ran["ok"] and ran["results"][0].get("error") is None, ran
    with pytest.raises(sqlops.OpError, match="any more"):
        sqlops.object_info(user_id, conn_id, database, schema, "sequence", "no_such_thing")


def test_roles_listing(user_id, profile):
    from services import sqlops

    engine, conn_id = profile
    listed = sqlops.roles(user_id, conn_id)
    if engine in FILE_ENGINES:
        assert listed == []
        return
    me = {"postgresql": "postgres", "cockroachdb": "root", "mysql": "root@%", "mssql": "sa"}[engine]
    row = next(r for r in listed if r["name"] == me)
    assert row["login"] and row["admin"]
    shown = sqlops.object_info(user_id, conn_id, "", "", "role", me)
    assert shown["definition"]
    if engine == "postgresql":
        assert "SUPERUSER" in shown["definition"] and not any(
            r["name"].startswith("pg_") for r in listed)
        assert any(r["name"].startswith("pg_") for r in sqlops.roles(user_id, conn_id, True))
    if engine == "mysql":
        assert "GRANT" in shown["definition"]
        assert "password hash hidden" in shown["definition"] and "$A$" not in shown["definition"]
    if engine == "mssql":
        assert "sysadmin" in [i for lst in shown["lists"] for i in lst["items"]]


def test_refresh_materialized_view(user_id, profile):
    from services import sqlops

    engine, conn_id = profile
    database, schema = LOCATION[engine]
    if engine not in PG_LIKE:
        with pytest.raises(sqlops.OpError, match="no materialized views"):
            sqlops.refresh_matview(user_id, conn_id, database, schema, "order_totals", True)
        return
    plan = sqlops.refresh_matview(user_id, conn_id, database, schema, "product_sales", True)
    assert plan["sql"] == 'REFRESH MATERIALIZED VIEW CONCURRENTLY public.product_sales;'
    done = sqlops.refresh_matview(user_id, conn_id, database, schema, "product_sales")
    assert done["ok"] and "ms" in done
    ro = _make_profile(user_id, engine, read_only=True)
    with pytest.raises(sqlops.OpError, match="read-only"):
        sqlops.refresh_matview(user_id, ro, database, schema, "product_sales")
    with pytest.raises(sqlops.OpError, match="No materialized view"):
        sqlops.refresh_matview(user_id, conn_id, database, schema, "order_totals", True)


# ---------------------------------------------------------------------------
# Object actions: create / alter / drop through sqlops.alter
# ---------------------------------------------------------------------------

TYPES = {
    "postgresql": ("integer", "varchar(50)"),
    "mysql": ("int", "varchar(50)"),
    "mssql": ("int", "nvarchar(50)"),
    "sqlite": ("INTEGER", "TEXT"),
    "duckdb": ("INTEGER", "VARCHAR"),
    "cockroachdb": ("integer", "varchar(50)"),
}


def _id_column(user_id, conn_id, engine, table):
    """A self-numbering key column; DuckDB has none, so a sequence default stands in."""
    from services import sqlops

    if engine != "duckdb":
        return {"name": "id", "type": TYPES[engine][0], "pk": True, "identity": True}
    database, _schema = LOCATION[engine]
    sqlops.execute(user_id, conn_id, database, f"CREATE SEQUENCE IF NOT EXISTS {table}_seq")
    return {"name": "id", "type": "INTEGER", "pk": True, "default": f"nextval('{table}_seq')"}


def test_object_actions_round_trip(user_id, profile):
    from services import sqlops

    engine, conn_id = profile
    database, schema = LOCATION[engine]
    int_type, text_type = TYPES[engine]
    for name in ("bk_scratch", "bk_scratch2"):
        sqlops.execute(user_id, conn_id, database, f"DROP TABLE IF EXISTS {name}")

    def alter(table, action, **params):
        done = sqlops.alter(user_id, conn_id, database, schema, table, action, params)
        assert done["ok"], done
        return done

    plan = sqlops.alter(user_id, conn_id, database, schema, "", "create_table", {
        "name": "bk_scratch", "columns": [
            _id_column(user_id, conn_id, engine, "bk_scratch"),
            {"name": "label", "type": text_type, "not_null": True, "default": "'none: yet'"},
        ]}, True)
    assert plan["sql"][0].startswith("CREATE TABLE") and plan["note"]
    assert ("cannot be rolled back" in plan["note"]) == (engine == "mysql")
    alter("", "create_table", **{"name": "bk_scratch", "columns": [
        _id_column(user_id, conn_id, engine, "bk_scratch"),
        {"name": "label", "type": text_type, "not_null": True, "default": "'none: yet'"}]})
    info = sqlops.table_info(user_id, conn_id, database, schema, "bk_scratch")
    assert info["pk"] == ["id"] and [c["name"] for c in info["columns"]] == ["id", "label"]
    ran = sqlops.execute(user_id, conn_id, database,
                         "INSERT INTO bk_scratch (label) VALUES ('a')")
    assert ran["ok"] and not ran["results"][0].get("error"), ran
    page = sqlops.browse(user_id, conn_id, database, schema, "bk_scratch", pk=["id"])
    assert page["rows"] == [["1", "a"]]

    alter("bk_scratch", "add_column", name="qty", type=int_type, not_null=True, default="0")
    alter("bk_scratch", "rename_column", column="qty", new_name="amount")
    alter("bk_scratch", "create_index", name="ix_bk_scratch_label", columns=["label"],
          unique=True)
    info = sqlops.table_info(user_id, conn_id, database, schema, "bk_scratch")
    assert [c["name"] for c in info["columns"]] == ["id", "label", "amount"]
    assert any(ix["name"] == "ix_bk_scratch_label" and ix["unique"] for ix in info["indexes"])
    alter("bk_scratch", "drop_index", index="ix_bk_scratch_label")
    alter("bk_scratch", "drop_column", column="amount")
    alter("bk_scratch", "truncate")
    assert sqlops.browse(user_id, conn_id, database, schema, "bk_scratch", pk=["id"])["rows"] == []
    alter("bk_scratch", "rename_table", new_name="bk_scratch2")
    tables = {t["name"] for t in sqlops.objects(user_id, conn_id, database, schema)["tables"]}
    assert "bk_scratch2" in tables and "bk_scratch" not in tables
    alter("bk_scratch2", "drop", kind="table")
    tables = {t["name"] for t in sqlops.objects(user_id, conn_id, database, schema)["tables"]}
    assert "bk_scratch2" not in tables

    with pytest.raises(sqlops.OpError, match="cannot contain"):
        sqlops.alter(user_id, conn_id, database, schema, "orders", "add_column",
                     {"name": "x", "type": "int; DROP TABLE orders"}, True)
    if engine == "duckdb":      # one way per process: test_read_only_profile_refuses_writes
        return
    ro = _make_profile(user_id, engine, read_only=True)
    preview = sqlops.alter(user_id, ro, database, schema, "orders", "truncate", {}, True)
    assert preview["read_only"]
    with pytest.raises(sqlops.OpError, match="read-only"):
        sqlops.alter(user_id, ro, database, schema, "orders", "truncate", {})
    assert sqlops.browse(user_id, conn_id, database, schema, "orders", pk=["id"])["total"] == 137


# ---------------------------------------------------------------------------
# Import CSV (services/csv_import.py), run the way the job runs it
# ---------------------------------------------------------------------------

def _run_import(user_id, conn_id, engine, table, text, progress=None, **options):
    from services import csv_import, jobs

    database, schema = LOCATION[engine]
    plan = csv_import.check(user_id, conn_id, database, schema, table, options)
    path = jobs.new_file(".csv")
    path.write_bytes(text.encode(options.get("_codec", "utf-8")))
    try:
        return csv_import.run(user_id, plan, path, progress or jobs.Progress())
    finally:
        path.unlink(missing_ok=True)


def test_csv_import(user_id, profile):
    from services import csv_import, jobs, sqlops

    engine, conn_id = profile
    database, schema = LOCATION[engine]
    int_type, text_type = TYPES[engine]
    sqlops.execute(user_id, conn_id, database, "DROP TABLE IF EXISTS bk_import")
    made = sqlops.alter(user_id, conn_id, database, schema, "", "create_table", {
        "name": "bk_import", "columns": [
            _id_column(user_id, conn_id, engine, "bk_import"),
            {"name": "label", "type": text_type, "not_null": True},
            {"name": "amount", "type": "NUMERIC(10,2)"},
            {"name": "seen", "type": "DATE"}]})
    assert made["ok"], made

    def rows():
        page = sqlops.browse(user_id, conn_id, database, schema, "bk_import", pk=["id"])
        return page["rows"]

    plain = [["label", 0], ["amount", 1], ["seen", 2]]
    try:
        done = _run_import(user_id, conn_id, engine, "bk_import",
                           'Label,Amount,Seen\n"a, with comma",1.50,2026-10-24\n'
                           '"two\nlines",,\nñandú 🐍,3,2026-01-02\n\n',
                           mapping=plain)
        assert done["inserted"] == 3 and done["skipped"] == 0
        got = rows()
        assert [row[1] for row in got] == ["a, with comma", "two\nlines", "ñandú 🐍"]
        assert got[1][2] is None and got[1][3] is None          # empty fields are NULL
        assert got[0][2] in ("1.50", "1.5") and got[0][3].startswith("2026-10-24")

        # Explicit numbers in the self-numbering column; it carries on after them.
        done = _run_import(user_id, conn_id, engine, "bk_import", "10;x\n11;y\n",
                           delimiter=";", header=False, mapping=[["id", 0], ["label", 1]])
        assert done["inserted"] == 2
        ran = sqlops.execute(user_id, conn_id, database,
                             "INSERT INTO bk_import (label) VALUES ('next')")
        assert not ran["results"][0].get("error"), ran
        assert [row[0] for row in rows()][-3:] == ["10", "11", "12"]

        # A value that does not fit: stop (nothing kept) or skip that row.
        bad = "label,amount\nok,1\nbroken,abc\nfine,2\n"
        with pytest.raises(csv_import.CsvError, match="Row 3 .*amount.*Nothing was imported"):
            _run_import(user_id, conn_id, engine, "bk_import", bad, mapping=plain[:2])
        assert len(rows()) == 6
        done = _run_import(user_id, conn_id, engine, "bk_import", bad, mapping=plain[:2],
                           on_error="skip")
        assert done == {**done, "inserted": 2, "skipped": 1}
        assert len(rows()) == 8

        # The database refusing a row (NOT NULL) rolls the whole file back.
        with pytest.raises(csv_import.CsvError, match="refused"):
            _run_import(user_id, conn_id, engine, "bk_import", "label\nfirst\n\"\"\n",
                        mapping=[["label", 0]])
        assert len(rows()) == 8
        # ... unless empty fields are empty text.
        done = _run_import(user_id, conn_id, engine, "bk_import", "label\n\"\"\n",
                           mapping=[["label", 0]], empty="empty")
        assert done["inserted"] == 1 and rows()[-1][1] == ""

        # Replace: the rows that were there go, in the same transaction.
        done = _run_import(user_id, conn_id, engine, "bk_import", "label\nonly\n",
                           mapping=[["label", 0]], mode="replace", null_text="NULL")
        assert done["deleted"] == 9 and [row[1] for row in rows()] == ["only"]

        # Cancel between batches: nothing stays.
        job = jobs.Job(user_id, "import", "test")
        job.cancel_event.set()
        many = "label\n" + "".join(f"r{i}\n" for i in range(csv_import.BATCH_ROWS + 10))
        with pytest.raises(jobs.Cancelled):
            _run_import(user_id, conn_id, engine, "bk_import", many, jobs.Progress(job),
                        mapping=[["label", 0]])
        assert [row[1] for row in rows()] == ["only"]

        # Windows-1252 text.
        done = _run_import(user_id, conn_id, engine, "bk_import", "label\ncafé\n",
                           mapping=[["label", 0]], encoding="cp1252", _codec="cp1252")
        assert rows()[-1][1] == "café"
        with pytest.raises(csv_import.CsvError, match="not utf-8"):
            _run_import(user_id, conn_id, engine, "bk_import", "label\ncafé\n",
                        mapping=[["label", 0]], _codec="cp1252")

        # Refusals before anything is uploaded.
        with pytest.raises(csv_import.CsvError, match="no column"):
            csv_import.check(user_id, conn_id, database, schema, "bk_import",
                             {"mapping": [["nope", 0]]})
        with pytest.raises(csv_import.CsvError, match="view"):
            csv_import.check(user_id, conn_id, database, schema, "order_totals",
                             {"mapping": [["id", 0]]})
        if engine == "duckdb":  # one way per process: test_read_only_profile_refuses_writes
            return
        ro = _make_profile(user_id, engine, read_only=True)
        with pytest.raises(csv_import.CsvError, match="read-only"):
            csv_import.check(user_id, ro, database, schema, "bk_import",
                             {"mapping": [["label", 0]]})
    finally:
        sqlops.execute(user_id, conn_id, database, "DROP TABLE IF EXISTS bk_import")


# ---------------------------------------------------------------------------
# Session manager
# ---------------------------------------------------------------------------

SLEEP_SQL = {
    "postgresql": "SELECT pg_sleep(20) /* bk-sleeper */",
    "mysql": "SELECT SLEEP(20) /* bk-sleeper */",
    "mssql": "WAITFOR DELAY '00:00:20' /* bk-sleeper */",
}


def test_sessions_list_and_stop(user_id, profile):
    from services import sqlops

    engine, conn_id = profile
    database, _schema = LOCATION[engine]
    if engine in ("sqlite", "duckdb", "cockroachdb"):
        with pytest.raises(sqlops.OpError, match="no server sessions"):
            sqlops.sessions(user_id, conn_id)
        return
    everything = sqlops.sessions(user_id, conn_id, True)["sessions"]
    assert any(row["own"] for row in everything)
    assert all(isinstance(row["id"], int) for row in everything)

    finished = {}

    def sleeper():
        started = time.monotonic()
        finished["result"] = sqlops.execute(user_id, conn_id, database, SLEEP_SQL[engine])
        finished["seconds"] = time.monotonic() - started

    thread = threading.Thread(target=sleeper, daemon=True)
    thread.start()
    target = None
    for _ in range(40):
        time.sleep(0.25)
        busy = sqlops.sessions(user_id, conn_id)["sessions"]
        target = next((row for row in busy if "bk-sleeper" in row["query"] and not row["own"]),
                      None)
        if target:
            break
    assert target, "the sleeping statement never showed up"
    assert target["seconds"] is not None and target["user"]
    if engine == "postgresql":
        assert target["application"] == "Basilisk"
    mode = "terminate" if engine == "mssql" else "cancel"
    with pytest.raises(sqlops.OpError, match="cancel or terminate"):
        sqlops.kill_session(user_id, conn_id, target["id"], "nuke")
    if engine == "mssql":
        with pytest.raises(sqlops.OpError, match="only end a session"):
            sqlops.kill_session(user_id, conn_id, target["id"], "cancel")
    ro = _make_profile(user_id, engine, read_only=True)
    with pytest.raises(sqlops.OpError, match="read-only"):
        sqlops.kill_session(user_id, ro, target["id"], mode)
    done = sqlops.kill_session(user_id, conn_id, target["id"], mode)
    assert str(target["id"]) in done["sql"]
    thread.join(15)
    assert not thread.is_alive() and finished["seconds"] < 15
    # The pool notices the ended connection and gives a working one next.
    ran = sqlops.execute(user_id, conn_id, database, "SELECT 1")
    assert not ran["results"][0].get("error"), ran


def test_er_model(user_id, profile):
    from services import sqlops

    engine, conn_id = profile
    database, schema = LOCATION[engine]
    model = sqlops.er_model(user_id, conn_id, database, schema)
    tables = {t["name"]: t for t in model["tables"]}
    assert {"customers", "products", "orders", "order_items", "audit_log"} <= set(tables)
    items = tables["order_items"]
    assert items["pk"] == ["order_id", "sku"]
    refs = {(fk["ref_schema"], fk["ref_table"], tuple(fk["ref_columns"])) for fk in items["fks"]}
    assert refs == {(None, "orders", ("id",)), (None, "products", ("sku",))}
    assert tables["orders"]["fks"][0]["columns"] == ["customer_id"]
    types = " ".join(c["type"] for t in model["tables"] for c in t["columns"]).upper()
    assert "COLLATE" not in types and "CHARACTER SET" not in types
    assert not model["truncated"]


# ---------------------------------------------------------------------------
# Schema compare (services/schema_compare.py): compare two copies of a small
# schema, run the script on the target, compare again: nothing is left.
# ---------------------------------------------------------------------------

CMP_AUTO = {"duckdb": "INTEGER DEFAULT nextval('ids') PRIMARY KEY",
            "cockroachdb": "INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY",
            "postgresql": "INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY",
            "mysql": "INTEGER AUTO_INCREMENT PRIMARY KEY",
            "mssql": "INTEGER IDENTITY(1,1) PRIMARY KEY", "sqlite": "INTEGER PRIMARY KEY"}
CMP_SOURCE = [
    "CREATE TABLE {s}customers (id {auto}, name VARCHAR(100) NOT NULL, email VARCHAR(200), "
    "CONSTRAINT uq_customers_email UNIQUE (email))",
    "CREATE TABLE {s}orders (id {auto}, customer_id INTEGER NOT NULL, "
    "status VARCHAR(20) DEFAULT 'new', total NUMERIC(10,2), CONSTRAINT fk_orders_customer "
    "FOREIGN KEY (customer_id) REFERENCES {s}customers (id) ON DELETE CASCADE)",
    "CREATE INDEX ix_orders_status ON {s}orders (status)",
    "CREATE TABLE {s}order_tags (order_id INTEGER NOT NULL, tag VARCHAR(30) NOT NULL, "
    "PRIMARY KEY (order_id, tag), CONSTRAINT fk_tags_order FOREIGN KEY (order_id) "
    "REFERENCES {s}orders (id))",
    "CREATE TABLE {s}only_a (id INTEGER PRIMARY KEY, note VARCHAR(50))",
    "CREATE VIEW {s}big_orders AS SELECT id, total FROM {s}orders WHERE total > 100",
]
CMP_TARGET = [
    "CREATE TABLE {s}customers (id {auto}, name VARCHAR(50), phone VARCHAR(30))",
    "CREATE TABLE {s}orders (id {auto}, customer_id INTEGER NOT NULL, "
    "status VARCHAR(20) DEFAULT 'open', total NUMERIC(10,2), CONSTRAINT fk_orders_customer "
    "FOREIGN KEY (customer_id) REFERENCES {s}customers (id))",
    "CREATE INDEX ix_orders_cust ON {s}orders (customer_id)",
    "CREATE TABLE {s}order_tags (order_id INTEGER NOT NULL, tag VARCHAR(30) NOT NULL, "
    "PRIMARY KEY (order_id))",
    "CREATE TABLE {s}only_b (id INTEGER PRIMARY KEY)",
    "CREATE VIEW {s}big_orders AS SELECT id, total FROM {s}orders WHERE total > 500",
    "INSERT INTO {s}customers (name, phone) VALUES ('Ann', '555')",
    "INSERT INTO {s}orders (customer_id, status, total) VALUES (1, 'open', 150)",
    "INSERT INTO {s}order_tags VALUES (1, 'rush')",
]


def _run_ddl(user_id, conn_id, database, statements):
    from services.engine_pool import pool

    engine, spec, profile = pool.open(user_id, conn_id, database)
    with engine.connect() as conn:
        if database and "database" in spec.levels and spec.database_for(profile, database) is None:
            spec.use_database(conn.connection.dbapi_connection, database)
        for statement in statements:
            conn.exec_driver_sql(statement)
        conn.commit()


def _cmp_location(user_id, engine, conn_id, name, statements):
    """A fresh schema (database, SQLite file) ``name`` holding ``statements``: (conn, db, schema)."""
    if engine in FILE_ENGINES:
        suffix = ".db" if engine == "sqlite" else ".duckdb"
        if engine == "duckdb":
            # DuckDB keeps a file's database open in the process: a new name each time.
            import uuid

            name = f"{name}_{uuid.uuid4().hex[:8]}"
        path = Path(os.environ["BASILISK_SQLITE_ROOT"]) / f"{name}{suffix}"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.unlink(missing_ok=True)
        if engine == "sqlite":
            import sqlite3

            sqlite3.connect(path).close()
            prepare = []
        else:
            # No ON DELETE actions, and a sequence default for the numbered keys.
            statements = [st.replace(" ON DELETE CASCADE", "") for st in statements]
            prepare = ["CREATE SEQUENCE ids"]
        conn_id = _make_profile(user_id, engine, database=f"{name}{suffix}",
                                options={"create": True})
        _run_ddl(user_id, conn_id, "", prepare + [st.format(s="", auto=CMP_AUTO[engine])
                                                  for st in statements])
        return conn_id, "", ""
    database = LOCATION[engine][0]
    if engine == "mysql":
        _run_ddl(user_id, conn_id, "", [f"DROP DATABASE IF EXISTS {name}",
                                        f"CREATE DATABASE {name}"])
        database = name
    elif engine in ("postgresql", "cockroachdb"):
        _run_ddl(user_id, conn_id, database, [f"DROP SCHEMA IF EXISTS {name} CASCADE",
                                              f"CREATE SCHEMA {name}"])
    else:
        drops = [f"IF OBJECT_ID('{name}.big_orders') IS NOT NULL DROP VIEW {name}.big_orders"]
        drops += [f"IF OBJECT_ID('{name}.{t}') IS NOT NULL DROP TABLE {name}.{t}"
                  for t in ("order_tags", "orders", "customers", "only_a", "only_b", "items")]
        _run_ddl(user_id, conn_id, database, drops + [
            f"IF SCHEMA_ID('{name}') IS NULL EXEC('CREATE SCHEMA {name}')"])
    _run_ddl(user_id, conn_id, database, [st.format(s=f"{name}.", auto=CMP_AUTO[engine])
                                          for st in statements])
    return conn_id, database, "" if engine == "mysql" else name


def _apply(user_id, src, tgt, only=None):
    """Compare, run every difference's statements on the target, compare again."""
    from services import sqlops
    from ui import compare_tools

    result = sqlops.compare_schemas(user_id, *src, *tgt)
    chosen = [d["id"] for d in result["diffs"] if only is None or only(d)]
    script = compare_tools.script(result, chosen, ["test"])
    ran = sqlops.execute(user_id, tgt[0], tgt[1], script, "script")
    assert ran["ok"], [r.get("error") for r in ran["results"] if r.get("error")] or ran
    return result, sqlops.compare_schemas(user_id, *src, *tgt)


def test_schema_compare_round_trip(user_id, profile):
    from services import sqlops

    engine, conn_id = profile
    src = _cmp_location(user_id, engine, conn_id, "cmp_a", CMP_SOURCE)
    tgt = _cmp_location(user_id, engine, conn_id, "cmp_b", CMP_TARGET)
    with pytest.raises(sqlops.OpError, match="the same"):
        sqlops.compare_schemas(user_id, *src, *src)
    first, again = _apply(user_id, src, tgt)
    found = {(d["change"], d["kind"], d["group"]) for d in first["diffs"]}
    assert {("add", "table", "only_a"), ("drop", "table", "only_b"),
            ("alter", "view", "big_orders")} <= found
    if engine != "sqlite":
        named = {(d["change"], d["kind"], d["group"], d["name"]) for d in first["diffs"]}
        wanted = {("add", "column", "customers", "email"), ("alter", "column", "customers", "name"),
                  ("drop", "column", "customers", "phone"), ("alter", "column", "orders", "status"),
                  ("add", "index", "orders", "ix_orders_status"),
                  ("alter", "fk", "orders", "fk_orders_customer"),
                  ("add", "fk", "order_tags", "fk_tags_order")}
        if engine == "duckdb":     # no ON DELETE actions; foreign keys get its own names
            wanted -= {("alter", "fk", "orders", "fk_orders_customer"),
                       ("add", "fk", "order_tags", "fk_tags_order")}
            assert any(d["kind"] == "fk" and d["group"] == "order_tags" for d in first["diffs"])
        assert wanted <= named, named
    if engine == "duckdb":
        # What DuckDB cannot change in place (keys, columns of a table an index or a
        # foreign key depends on) stays, marked "by hand"; everything else went.
        assert again["diffs"] and all(d["manual"] for d in again["diffs"]), again["diffs"]
        assert {d["group"] for d in again["diffs"]} <= {"customers", "order_tags"}
        assert again["same_views"] == 1
    else:
        assert again["diffs"] == [], again["diffs"]
        assert again["same_tables"] == 4 and again["same_views"] == 1
    page = sqlops.browse(user_id, tgt[0], tgt[1], tgt[2], "orders", pk=["id"])
    assert page["total"] == 1


def test_schema_compare_into_sqlite(user_id, profile):
    """Another engine's schema written for SQLite: everything but the view comes across."""
    engine, conn_id = profile
    if engine == "sqlite":
        pytest.skip("the same engine: test_schema_compare_round_trip")
    src = _cmp_location(user_id, engine, conn_id, "cmp_a", CMP_SOURCE)
    tgt = _cmp_location(user_id, "sqlite", 0, f"cmp_from_{engine}", [])
    first, again = _apply(user_id, src, tgt)
    assert any("different engines" in note for note in first["notes"])
    assert [(d["change"], d["kind"], d["group"]) for d in again["diffs"]] == \
        [("add", "view", "big_orders")], [(d["group"], d["name"], d["detail"], d.get("items"))
                                          for d in again["diffs"]]
    assert again["same_tables"] == 4


def test_schema_compare_from_sqlite(user_id, profile):
    """A SQLite schema written for a server engine: the other direction."""
    engine, conn_id = profile
    if engine == "sqlite":
        pytest.skip("the same engine: test_schema_compare_round_trip")
    src = _cmp_location(user_id, "sqlite", 0, "cmp_sqlite_src", CMP_SOURCE)
    tgt = _cmp_location(user_id, engine, conn_id, "cmp_c", [])
    _first, again = _apply(user_id, src, tgt)
    assert [(d["change"], d["kind"], d["group"]) for d in again["diffs"]] == \
        [("add", "view", "big_orders")], [(d["group"], d["name"], d["detail"])
                                          for d in again["diffs"]]
    assert again["same_tables"] == 4


# ---------------------------------------------------------------------------
# Data compare (services/data_compare.py): two copies of a table that differ,
# the script run on the target, compared again: the same.
# ---------------------------------------------------------------------------

DCMP_TYPES = {
    "postgresql": {"bool": "BOOLEAN", "when": "TIMESTAMP", "bin": "BYTEA", "id": "INTEGER"},
    "mysql": {"bool": "BOOLEAN", "when": "DATETIME(6)", "bin": "VARBINARY(32)", "id": "INTEGER"},
    "mssql": {"bool": "BIT", "when": "DATETIME2", "bin": "VARBINARY(32)", "id": "INTEGER"},
    "sqlite": {"bool": "BOOLEAN", "when": "DATETIME", "bin": "BLOB", "id": "INTEGER"},
    "duckdb": {"bool": "BOOLEAN", "when": "TIMESTAMP", "bin": "BLOB", "id": "INTEGER"},
    "cockroachdb": {"bool": "BOOL", "when": "TIMESTAMP", "bin": "BYTES", "id": "INTEGER"},
}
DCMP_DDL = ("CREATE TABLE {s}items (id {id} NOT NULL PRIMARY KEY, name VARCHAR(60) NOT NULL, "
            "price NUMERIC(10,2), flag {bool}, seen {when}, raw {bin}, note VARCHAR(200))")


def _dcmp_rows(variant):
    import datetime
    import decimal

    when = datetime.datetime(2026, 10, 26, 9, 30, 15, 250000)
    rows = {
        1: dict(name="same", price=decimal.Decimal("1.50"), flag=True, seen=when,
                raw=b"\x00\xff", note=None),
        2: dict(name="it's a \\path", price=decimal.Decimal("20.00"), flag=False, seen=when,
                raw=b"ab", note="N'one"),
        3: dict(name="only source", price=None, flag=None, seen=None, raw=None, note="x"),
        5: dict(name="flag and note", price=decimal.Decimal("5"), flag=True, seen=None,
                raw=None, note="ünïcode ✓"),
    }
    if variant == "target":
        rows.pop(3)
        rows[2] = dict(rows[2], name="old name", price=decimal.Decimal("19.99"))
        rows[5] = dict(rows[5], flag=False, note=None)
        rows[9] = dict(name="only target", price=None, flag=None, seen=None, raw=None, note=None)
    return [dict(id=key, **value) for key, value in sorted(rows.items())]


def _dcmp_location(user_id, engine, conn_id, name, variant):
    """A schema ``name`` (as _cmp_location) holding ``items`` with the ``variant`` rows."""
    from sqlalchemy import MetaData, Table, insert

    from services.engine_pool import pool

    statements = [DCMP_DDL.format(s="{s}", **DCMP_TYPES[engine])]
    conn_id, database, schema = _cmp_location(user_id, engine, conn_id, name, statements)
    engine_, spec, profile = pool.open(user_id, conn_id, database)
    with engine_.connect() as conn:
        if database and "database" in spec.levels and spec.database_for(profile, database) is None:
            spec.use_database(conn.connection.dbapi_connection, database)
        table = Table("items", MetaData(), schema=spec.schema_for(database, schema),
                      autoload_with=conn)
        conn.execute(insert(table), _dcmp_rows(variant))
        conn.commit()
    return conn_id, database, schema


def _dcmp(user_id, src, tgt):
    from services import data_compare
    from services.jobs import Progress

    plan = data_compare.check(user_id, dict(zip(("conn", "db", "schema"), src), table="items"),
                              dict(zip(("conn", "db", "schema"), tgt), table="items"))
    return data_compare.run(user_id, plan, Progress())


def _dcmp_apply(user_id, src, tgt):
    from services import sqlops
    from ui import compare_tools

    first = _dcmp(user_id, src, tgt)
    script = compare_tools.data_script(first, [row["id"] for row in first["rows"]], ["test"])
    ran = sqlops.execute(user_id, tgt[0], tgt[1], script, "script")
    assert ran["ok"], [r.get("error") for r in ran["results"] if r.get("error")] or ran
    return first, _dcmp(user_id, src, tgt)


def test_data_compare_round_trip(user_id, profile):
    engine, conn_id = profile
    src = _dcmp_location(user_id, engine, conn_id, "dcmp_a", "source")
    tgt = _dcmp_location(user_id, engine, conn_id, "dcmp_b", "target")
    first, again = _dcmp_apply(user_id, src, tgt)
    assert first["counts"] == {"insert": 1, "update": 2, "delete": 1, "same": 1}, first["counts"]
    changed = {row["key"]: row["changed"] for row in first["rows"] if row["change"] == "update"}
    assert changed == {"id = 2": ["name", "price"], "id = 5": ["flag", "note"]}, changed
    assert again["counts"] == {"insert": 0, "update": 0, "delete": 0, "same": 4}, again["rows"]


def test_data_compare_across_engines(user_id, profile):
    """A server's table into SQLite and SQLite's into the server: values survive the trip."""
    engine, conn_id = profile
    if engine == "sqlite":
        pytest.skip("the same engine: test_data_compare_round_trip")
    server = _dcmp_location(user_id, engine, conn_id, "dcmp_a", "source")
    lite = _dcmp_location(user_id, "sqlite", 0, f"dcmp_{engine}", "target")
    first, again = _dcmp_apply(user_id, server, lite)
    assert first["counts"]["same"] == 1 and again["counts"]["same"] == 4, again["rows"]
    back = _dcmp_location(user_id, engine, conn_id, "dcmp_b", "target")
    _first, again = _dcmp_apply(user_id, lite, back)
    assert again["counts"] == {"insert": 0, "update": 0, "delete": 0, "same": 4}, again["rows"]


def test_data_compare_where(user_id, profile):
    from services import data_compare
    from services.jobs import Progress

    engine, conn_id = profile
    src = _dcmp_location(user_id, engine, conn_id, "dcmp_a", "source")
    tgt = _dcmp_location(user_id, engine, conn_id, "dcmp_b", "target")
    plan = data_compare.check(user_id, dict(zip(("conn", "db", "schema"), src), table="items"),
                              dict(zip(("conn", "db", "schema"), tgt), table="items"),
                              "id <= 2")
    result = data_compare.run(user_id, plan, Progress())
    assert result["counts"] == {"insert": 0, "update": 1, "delete": 0, "same": 1}
    with pytest.raises(data_compare.DataCompareError, match="Reading the source"):
        bad = data_compare.check(user_id, dict(zip(("conn", "db", "schema"), src), table="items"),
                                 dict(zip(("conn", "db", "schema"), tgt), table="items"),
                                 "no_such_column = 1")
        data_compare.run(user_id, bad, Progress())


def test_data_compare_refusals(user_id, profile):
    from services import data_compare

    engine, conn_id = profile
    database, schema = LOCATION[engine]
    here = {"conn": conn_id, "db": database, "schema": schema}
    with pytest.raises(data_compare.DataCompareError, match="primary key"):
        data_compare.check(user_id, dict(here, table="audit_log"), dict(here, table="orders"))
    with pytest.raises(data_compare.DataCompareError, match="same table"):
        data_compare.check(user_id, dict(here, table="orders"), dict(here, table="orders"))
    with pytest.raises(data_compare.DataCompareError, match="no table"):
        data_compare.check(user_id, dict(here, table="orders"), dict(here, table="nope"))


# ---------------------------------------------------------------------------
# SSH tunnels: tests/smoke/ssh_server.sh runs the SSH server
# ---------------------------------------------------------------------------

SSH = {"enabled": True, "host": "127.0.0.1", "port": 52222, "username": "tunnel",
       "auth": "password", "password": "Tunn3l-pass"}
SSH_KEYS = ROOT / "data" / "ssh-test"


def _ssh_up() -> bool:
    import socket

    try:
        socket.create_connection(("127.0.0.1", 52222), timeout=2).close()
        return True
    except OSError:
        return False


def test_ssh_tunnel(user_id, profile):
    from services import sqlops
    from services.connection_service import ConnectionService
    from services.db import fetch_connection, get_db
    from services.engine_pool import pool, tunnel_of

    engine, _plain = profile
    if engine in FILE_ENGINES:
        pytest.skip("database files are local")
    if not _ssh_up():
        pytest.skip("run tests/smoke/ssh_server.sh first")
    database, schema = LOCATION[engine]
    service = ConnectionService({"user_id": user_id})
    values = dict(PROFILES[engine])

    tested = service.test(None, engine, password=PASSWORD, ssh=SSH, **values)
    assert tested["ok"] and tested["ssh"] and tested["ssh_new_key"], tested
    assert tested["ssh_fingerprint"].startswith("SHA256:")
    saved = service.save(name=f"{engine} via ssh", engine=engine, password=PASSWORD,
                         ssh={**SSH, "host_key": tested["ssh_host_key"]}, **values)
    assert saved["ok"], saved
    conn_id = saved["id"]
    stored = fetch_connection(conn_id, user_id)["ssh"]
    assert stored["host_key"] == tested["ssh_host_key"] and stored["password"] == "Tunn3l-pass"
    public = service.get(conn_id)["ssh"]
    assert public["has_password"] and "password" not in public
    assert public["host_key"] == tested["ssh_fingerprint"]

    page = sqlops.browse(user_id, conn_id, database, schema, "orders", limit=5, pk=["id"])
    assert page["total"] == 137
    engine_obj = pool.open(user_id, conn_id, database)[0]
    tunnel = tunnel_of(engine_obj)
    assert tunnel is not None and tunnel.healthy() and str(tunnel.port) in str(engine_obj.url)

    # A dropped SSH connection: the pool builds a new engine and tunnel.
    tunnel._client.close()
    assert not tunnel.healthy()
    assert sqlops.ping(user_id, conn_id)["version"]
    assert tunnel_of(pool.open(user_id, conn_id, database)[0]) is not tunnel

    # Saving without new secrets keeps them; a changed host forgets the pin.
    again = service.save(conn_id, f"{engine} via ssh", engine, password="\x00unchanged\x00",
                         ssh={k: v for k, v in SSH.items() if k != "password"}, **values)
    assert again["ok"] and fetch_connection(conn_id, user_id)["ssh"]["password"] == "Tunn3l-pass"
    assert fetch_connection(conn_id, user_id)["ssh"]["host_key"] == tested["ssh_host_key"]

    # A pinned key that does not match is refused.
    from services.db import encode_options

    wrong = {**fetch_connection(conn_id, user_id)["ssh"],
             "host_key": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl"}
    with get_db() as conn:
        conn.execute("UPDATE connections SET ssh = ? WHERE id = ?", (encode_options(wrong), conn_id))
    pool.close(user_id, conn_id)
    with pytest.raises(Exception, match="has changed"):
        sqlops.ping(user_id, conn_id)
    forgot = service.save(conn_id, f"{engine} via ssh", engine, password="\x00unchanged\x00",
                          ssh={**SSH, "forget_host_key": True}, **values)
    assert forgot["ok"] and not fetch_connection(conn_id, user_id)["ssh"]["host_key"]
    assert sqlops.ping(user_id, conn_id)["version"]          # pinned again on first use
    assert fetch_connection(conn_id, user_id)["ssh"]["host_key"] == tested["ssh_host_key"]

    # Bad SSH credentials are the error, not a driver's.
    bad = service.test(None, engine, password=PASSWORD, ssh={**SSH, "password": "nope"}, **values)
    assert not bad["ok"] and "uthentication" in bad["error"]

    # Keys: plain and with a passphrase; a wrong passphrase is a field error.
    if (SSH_KEYS / "id_ed25519").exists():
        key = (SSH_KEYS / "id_ed25519").read_text()
        locked = (SSH_KEYS / "id_ed25519_locked").read_text()
        by_key = {**SSH, "auth": "key", "private_key": key}
        del by_key["password"]
        assert service.test(None, engine, password=PASSWORD, ssh=by_key, **values)["ok"]
        assert service.test(None, engine, password=PASSWORD, **values,
                            ssh={**by_key, "private_key": locked, "passphrase": "sesame"})["ok"]
        refused = service.save(name="x", engine=engine, password=PASSWORD, **values,
                               ssh={**by_key, "private_key": locked, "passphrase": "wrong"})
        assert "ssh_private_key" in refused["errors"]
        switched = service.save(conn_id, f"{engine} via ssh", engine, password="\x00unchanged\x00",
                                ssh=by_key, **values)
        assert switched["ok"]
        stored = fetch_connection(conn_id, user_id)["ssh"]
        assert stored["auth"] == "key" and stored["password"] == ""   # the old secret is dropped
        assert sqlops.ping(user_id, conn_id)["version"]
    service.delete(conn_id)
