<p align="center">
  <img src="docs/basilisk.webp" alt="Basilisk: a blue iguana in a red fez and white suit, cigar in hand" width="320">
</p>

<h1 align="center">Basilisk</h1>

A browser-based SQL database manager — think DBeaver, in a browser tab.
PostgreSQL, MySQL / MariaDB, SQL Server and SQLite, built on
[pytincture](https://github.com/pytincture/pytincture) (the page is Python
running in Pyodide) and the wapyt widget set.

- **Database navigator**: connections in folders, then databases, schemas,
  tables and views, loaded as you open them. Development / test / production
  connection types; read-only connections.
- **Table editor**: paged data with a WHERE / ORDER BY bar, sorting by header,
  inline editing that stays pending until **Save** (shown as SQL first, run in
  one transaction), add / duplicate / delete rows, Set NULL, filter by a
  cell's value; columns, keys, indexes, foreign keys (double-click to follow)
  and DDL.
- **SQL editor**: syntax highlighting and table/column completion for each
  dialect, run the statement at the cursor or the whole script, cancel a
  running statement, per-statement timeouts, results in a grid, messages,
  query history, formatting, drafts kept per connection.
- **Export** a table (with its filter) or a query result to CSV or JSON,
  streamed from the server.
- Multi-user, passwords encrypted at rest, sign-in required.

## Run it

```bash
cp .env.example .env          # set BASILISK_ADMIN_PASS
uv sync
../wa_pytincture_widgetset/scripts/dev_wheel.sh appcode   # the widget set for the browser
uv run python service.py      # http://127.0.0.1:8768/basilisk/
```

Or in a container: `scripts/podman-run.sh` (Linux + Podman, host networking so
connections can reach databases on the same machine).

SQLite connections open files inside one folder on the server
(`BASILISK_SQLITE_ROOT`, by default `data/sqlite`).

## Develop

See `CLAUDE.md` for the design, the test databases and the traps, and
`ROADMAP.md` for what comes next.

```bash
uv run --group dev pytest -q
```

MIT licensed.
