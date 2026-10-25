#!/usr/bin/env python3
"""
Create the ``shop`` sample in every test database (CLAUDE.md, *Test
databases*): customers, products, orders, order_items with foreign keys, a
view, a table without a primary key, and one column of most types.

    uv run python tests/smoke/seed_sample.py            # all four
    uv run python tests/smoke/seed_sample.py postgresql # one

Idempotent: it drops and recreates the sample each time. The SQLite file goes
to ``$BASILISK_SQLITE_ROOT/shop.db`` (default ``data/sqlite/shop.db``).
"""
from __future__ import annotations

import datetime as dt
import decimal
import os
import random
import sys
import uuid
from pathlib import Path

from sqlalchemy import (JSON, BigInteger, Boolean, Column, Date, DateTime, ForeignKey, Integer,
                        LargeBinary, MetaData, Numeric, String, Table, Text, Time, Unicode, UnicodeText, Uuid,
                        create_engine, text, URL)

PASSWORD = os.environ.get("BASILISK_TEST_PASSWORD", "B@sil:isk/1")
ROOT = Path(__file__).resolve().parents[2]

SERVERS = {
    "postgresql": URL.create("postgresql+psycopg", "postgres", PASSWORD, "127.0.0.1", 55432, "postgres"),
    "mysql": URL.create("mysql+pymysql", "root", PASSWORD, "127.0.0.1", 53306, None),
    "mssql": URL.create("mssql+pymssql", "sa", PASSWORD, "127.0.0.1", 51433, "master"),
}


def sqlite_path() -> Path:
    root = Path(os.environ.get("BASILISK_SQLITE_ROOT", ROOT / "data" / "sqlite"))
    root.mkdir(parents=True, exist_ok=True)
    return root / "shop.db"


def metadata(schema: str | None) -> MetaData:
    md = MetaData(schema=schema)
    Table("customers", md,
          Column("id", Integer, primary_key=True, autoincrement=True),
          Column("name", Unicode(100), nullable=False),
          Column("email", String(200), unique=True),
          Column("vip", Boolean, nullable=False, default=False),
          Column("joined", Date),
          Column("notes", UnicodeText),
          comment="People who buy things")
    Table("products", md,
          Column("sku", String(20), primary_key=True),
          Column("title", Unicode(200), nullable=False),
          Column("price", Numeric(10, 2), nullable=False),
          Column("stock", BigInteger),
          Column("attributes", JSON),
          Column("photo", LargeBinary))
    Table("orders", md,
          Column("id", Integer, primary_key=True, autoincrement=True),
          Column("customer_id", Integer, ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, index=True),
          Column("placed_at", DateTime, nullable=False),
          Column("ship_by", Time),
          Column("token", Uuid),
          Column("status", String(20), nullable=False, default="new"))
    Table("order_items", md,
          Column("order_id", Integer, ForeignKey("orders.id", ondelete="CASCADE"), primary_key=True),
          Column("sku", String(20), ForeignKey("products.sku"), primary_key=True),
          Column("qty", Integer, nullable=False))
    Table("audit_log", md,  # no primary key: read-only in the grid
          Column("at", DateTime),
          Column("message", UnicodeText))
    return md


def _png(width: int = 24, height: int = 24) -> bytes:
    """A real little PNG (an amber gradient), for the value panel's Image view."""
    import struct
    import zlib

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))

    raw = b"".join(b"\x00" + b"".join(bytes((245, 80 + 3 * x, 11 + 4 * y)) for x in range(width))
                   for y in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


# Longer than the grid shows (values.MAX_CELL_CHARS), for the value panel.
LONG_NOTE = "\n".join(f"Line {n:04}: the customer called about order {n} and was happy."
                       for n in range(1, 541))


def rows() -> dict:
    rnd = random.Random(7)
    names = ["Ada Lovelace", "Grace Hopper", "Alan Turing", "Edsger Dijkstra", "Barbara Liskov",
             "Donald Knuth", "Margaret Hamilton", "Ken Thompson", "Frances Allen", "Linus Torvalds",
             "Radia Perlman", "Tim Berners-Lee", "O'Brien; Robert", "Zoë Ünicode 🦎"]
    customers = [{"id": i + 1, "name": n, "email": f"user{i + 1}@example.com" if i != 3 else None,
                  "vip": i % 4 == 0, "joined": dt.date(2024, 1, 1) + dt.timedelta(days=37 * i),
                  "notes": LONG_NOTE if i == 10 else (None if i % 3 else "Prefers email.\nSecond line.")}
                 for i, n in enumerate(names)]
    products = [{"sku": f"SKU-{i:03}", "title": t, "price": decimal.Decimal(p), "stock": s,
                 "attributes": a, "photo": b}
                for i, (t, p, s, a, b) in enumerate([
                    ("Lizard lamp", "19.99", 12, {"color": "green", "watts": 40}, _png()),
                    ("Basking rock", "45.00", 3, {"material": "slate"}, None),
                    ("Cricket box", "4.50", 9_007_199_254_740_993, None, None),
                    ("Terrarium XL", "249.95", 0, {"dims": [120, 60, 60]}, bytes(range(256)) * 20),
                    ("Mister", "12.00", None, {}, None),
                ])]
    orders, items = [], []
    statuses = ["new", "paid", "shipped", "cancelled"]
    for oid in range(1, 138):
        orders.append({"id": oid, "customer_id": rnd.randint(1, len(names)),
                       "placed_at": dt.datetime(2026, 1, 1, 9) + dt.timedelta(hours=7 * oid, seconds=oid),
                       "ship_by": dt.time(rnd.randint(8, 17), 30),
                       "token": uuid.UUID(int=rnd.getrandbits(128)), "status": rnd.choice(statuses)})
        for sku in rnd.sample([p["sku"] for p in products], rnd.randint(1, 3)):
            items.append({"order_id": oid, "sku": sku, "qty": rnd.randint(1, 5)})
    audit = [{"at": dt.datetime(2026, 10, 1, 12, i), "message": f"event {i}"} for i in range(5)]
    return {"customers": customers, "products": products, "orders": orders,
            "order_items": items, "audit_log": audit}


VIEW = ("CREATE VIEW {q}order_totals AS SELECT o.id AS order_id, c.name AS customer, "
        "SUM(i.qty * p.price) AS total FROM {q}orders o JOIN {q}customers c ON c.id = o.customer_id "
        "JOIN {q}order_items i ON i.order_id = o.id JOIN {q}products p ON p.sku = i.sku "
        "GROUP BY o.id, c.name")


def load(engine, schema: str | None) -> None:
    md = metadata(schema)
    q = f"{schema}." if schema else ""
    with engine.begin() as conn:
        conn.execute(text(f"DROP VIEW IF EXISTS {q}order_totals"))
    md.drop_all(engine)
    md.create_all(engine)
    data = rows()
    with engine.begin() as conn:
        # SQL Server: SQLAlchemy switches IDENTITY_INSERT on for explicit ids.
        conn.execute(md.tables[f"{q}customers"].insert(), data["customers"])
        conn.execute(md.tables[f"{q}orders"].insert(), data["orders"])
        for name in ("products", "order_items", "audit_log"):
            conn.execute(md.tables[f"{q}{name}"].insert(), data[name])
        if engine.dialect.name == "postgresql":
            for name in ("customers", "orders"):
                conn.execute(text(f"SELECT setval(pg_get_serial_sequence('{q}{name}', 'id'), "
                                  f"(SELECT MAX(id) FROM {q}{name}))"))
        conn.execute(text(VIEW.format(q=q)))


def seed(which: str) -> None:
    if which == "sqlite":
        path = sqlite_path()
        path.unlink(missing_ok=True)
        load(create_engine(URL.create("sqlite", database=str(path))), None)
        print(f"sqlite: {path}")
        return
    admin = create_engine(SERVERS[which], isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        if which == "postgresql":
            conn.execute(text("DROP DATABASE IF EXISTS shop WITH (FORCE)"))
            conn.execute(text("CREATE DATABASE shop"))
        elif which == "mysql":
            conn.execute(text("DROP DATABASE IF EXISTS shop"))
            conn.execute(text("CREATE DATABASE shop CHARACTER SET utf8mb4"))
        else:
            conn.execute(text("IF DB_ID('shop') IS NOT NULL BEGIN "
                              "ALTER DATABASE shop SET SINGLE_USER WITH ROLLBACK IMMEDIATE; "
                              "DROP DATABASE shop; END"))
            conn.execute(text("CREATE DATABASE shop"))
    admin.dispose()
    url = SERVERS[which].set(database="shop")
    engine = create_engine(url)
    load(engine, None)
    if which in ("postgresql", "mssql"):
        with engine.begin() as conn:
            conn.execute(text("CREATE SCHEMA sales"))
        load(engine, "sales")
    engine.dispose()
    print(f"{which}: shop")


if __name__ == "__main__":
    for name in sys.argv[1:] or ["postgresql", "mysql", "mssql", "sqlite"]:
        seed(name)
