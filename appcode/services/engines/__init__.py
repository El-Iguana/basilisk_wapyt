"""
The database engines a profile can point at. Server-side only, a plain module
(not a BFF), so the registry is built once per process.
"""
from __future__ import annotations

from services.engines.base import Engine, EngineError, Field, Piece
from services.engines.cockroachdb import CockroachDB
from services.engines.duckdb import DuckDB
from services.engines.mssql import SQLServer
from services.engines.mysql import MySQL
from services.engines.postgresql import PostgreSQL
from services.engines.sqlite import SQLite

__all__ = ["Engine", "EngineError", "Field", "Piece", "get", "listing"]

_REGISTRY: dict = {engine.name: engine for engine in (PostgreSQL(), MySQL(), SQLServer(), SQLite(), DuckDB(), CockroachDB())}


def get(name: str) -> Engine:
    try:
        return _REGISTRY[name]
    except KeyError:
        raise EngineError(f"Unknown database engine {name!r}") from None


def listing() -> list:
    return list(_REGISTRY.values())
