<p align="center">
  <img src="docs/basilisk.webp" alt="Basilisk: a blue iguana in a red fez and white suit, cigar in hand" width="320">
</p>

<h1 align="center">Basilisk</h1>

A browser-based SQL database manager — think DBeaver, in a browser tab.
PostgreSQL, MySQL / MariaDB, SQL Server and SQLite, built on
[pytincture](https://github.com/pytincture/pytincture) (the page is Python
running in Pyodide) and the wapyt widget set.

- **Database navigator**: connections in folders, then databases, schemas,
  tables, views, functions, procedures, sequences and triggers, and each
  server's users and roles, loaded as you open them. Open a routine, sequence,
  trigger or role to see its properties and definition; a routine's **Call…**
  writes a ready-to-fill call in a SQL editor. PostgreSQL materialized views
  can be refreshed (concurrently when they allow it). Development / test /
  production connection types; read-only connections.
- **Object actions**: create a table (columns, types, primary key, an
  auto-numbered column), rename, truncate or drop it, add / rename / drop
  columns, create / drop indexes, drop views. Each shows its SQL as you
  fill it in and runs it in one transaction where the engine allows.
- **Table editor**: paged data with a WHERE / ORDER BY bar, sorting by header,
  inline editing that stays pending until **Save** (shown as SQL first, run in
  one transaction), add / duplicate / delete rows, Set NULL, filter by a
  cell's value; columns, keys, indexes, foreign keys (double-click to follow)
  and DDL.
- **Grids**: column filters (shown as chips; on a table they filter every row
  on the server), hide, move and resize columns (remembered per table),
  freeze the first columns, copy rows as INSERT statements or a Markdown
  table, and open the row a foreign-key cell points at.
- **Import CSV** into a table: the delimiter and encoding are guessed,
  columns are matched by name, a preview shows what will be stored. The whole
  file goes in one transaction, with a progress bar and Cancel.
- **ER diagram** of a schema, or of one table and its neighbours: tables
  laid out by their foreign keys (crow's-foot lines), drag tables around
  (remembered), all columns / keys only / names, find, export as SVG.
- **Schema compare**: two schemas side by side (on one server, two servers,
  or two different engines): tables, columns, keys, indexes, foreign keys
  and views that differ, and a script that makes the target like the source.
  Tick what to apply (drops start unticked), then open the script in a SQL
  editor on the target, copy it or save it.
- **Sessions**: what is running on a PostgreSQL, MySQL / MariaDB or SQL
  Server server, refreshed every few seconds, with Cancel query and End
  session.
- **Value panel** (F7, or Shift+Enter on a cell): the whole value of the
  focused cell beside the grid, as text, pretty JSON, a hex dump or a
  picture. Edit long text, JSON and binary there, load a value from a file or
  save it to one.
- **SQL editor**: syntax highlighting and table/column completion for each
  dialect, run the statement at the cursor or the whole script, cancel a
  running statement, per-statement timeouts, results in a grid, messages,
  query history, formatting, drafts kept per connection. Auto-commit or
  manual commit with Commit / Rollback; explain plans (estimated or with
  actual rows and times) for all four engines.
- **Export** a table (with its filter) or a query result to CSV or JSON,
  streamed from the server.
- **SSH tunnels**: reach a database through an SSH server, with a password
  or a private key (any type `ssh-keygen` makes, with a passphrase or not);
  the server's host key is pinned on first use.
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
