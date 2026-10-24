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
}
# Where the sample lives in each engine's tree: (database, schema).
LOCATION = {
    "postgresql": ("shop", "public"),
    "mysql": ("shop", ""),
    "mssql": ("shop", "dbo"),
    "sqlite": ("", ""),
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
    password = PASSWORD if engine != "sqlite" else ""
    result = ConnectionService({"user_id": user_id}).save(
        name=f"{engine} test", engine=engine, password=password, **values)
    assert result["ok"], result
    return result["id"]


@pytest.fixture(scope="session", params=list(PROFILES))
def profile(request, user_id):
    engine = request.param
    if engine == "sqlite":
        source = ROOT / "data" / "sqlite" / "shop.db"
        if not source.exists():
            pytest.skip("run tests/smoke/seed_sample.py first")
        target = Path(os.environ["BASILISK_SQLITE_ROOT"]) / "shop.db"
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
        if engine in ("postgresql", "mssql"):
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

    engine, _ = profile
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
    tables = found["schema"] if engine == "sqlite" else found["schema"][schema or database]
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
