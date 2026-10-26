# Basilisk — SQL database browser on pytincture + wapyt

A browser-based SQL database manager in the spirit of DBeaver, for
**PostgreSQL, MySQL / MariaDB, SQL Server and SQLite**. The page is Python in
Pyodide built from **wapyt** widgets (`../wa_pytincture_widgetset`); the server
is **pytincture** (FastAPI) talking to the databases through **SQLAlchemy 2**
and their drivers. Started 2026-10-24, modelled on **Monguana**
(`../monguana_wapyt`, the MongoDB GUI) for its service wiring, auth, login
page, profiles and container. Read Monguana's CLAUDE.md for the framework
traps; the ones that bite here are summarised below, not re-explained.

Private repo: El-Iguana/basilisk_wapyt. Next work: `ROADMAP.md`.

## Related repos

Siblings under `~/Development/Pytinc/`:

- `pytincture/` — the framework, pinned to `pytincture==1.0.0` from PyPI in
  `pyproject.toml` *and* `requirements.txt` (keep them in step).
- `wa_pytincture_widgetset/` — **wapyt**. Basilisk uses Tree, DataTable
  (inline editing, cell/row classes, CSV export), Pagination, Form (toggle
  groups, static fields, buttons), Layout splitters, TabWidget, Toolbar,
  ModalWindow, `message`. The Containerfile pins `WAPYT_REF`.
- `monguana_wapyt/` — the sibling this one copies its plumbing from.

## Layout

```
service.py                 # ASGI entrypoint: config, hooks, routes, static mounts, CLI banner
manage.py                  # admin CLI: health, probe HOST PORT, users, reset-password
appcode/                   # pytincture modules_path
  basilisk.py              #   the page (Pyodide). APP_ENTRYPOINT lives here.
  services/
    auth.py, login_page.py #   from Monguana (authenticator + policy hook; "Username" login form)
    db.py                  #   SQLite: users, connections, ui_state, query_history; Fernet; bcrypt
    engines/               #   one adapter per engine — plain modules, NOT BFFs
      base.py              #     the hooks: URL, levels, read-only, timeout, cancel, split, DDL,
                           #     routines / sequences / triggers / roles (lists and object info)
      postgresql.py mysql.py mssql.py sqlite.py
    engine_pool.py         #   SQLAlchemy engines per (user, connection, database) — plain module
    sessions.py            #   manual commit mode: pinned connection per SQL tab, idle reaper — plain module
    sqlops.py              #   everything done to a database; the running-statement registry
    ddl.py                 #   object actions' statements (create/rename/drop/truncate, columns, indexes) — pure
    values.py              #   driver values <-> grid text; lossless row keys; typed coercion
    plans.py               #   explain plans from every engine in one tree shape (pure parsers)
    connection_service.py  #   BFF: profiles, test, disconnect
    sql_service.py         #   BFF: thin front for sqlops (tree, table, browse, save, execute…)
    user_service.py, ui_state_service.py, about*.py, release_check.py   # from Monguana
    transfer.py            #   plain routes under /bk: table export (GET), query export (POST), one cell (GET)
  ui/value_panel.py        #   the value panel (Pyodide; pure helpers at the top, tested in CPython)
  static/                  #   basilisk_avatar.webp: logo, login page, favicon, About
  vendor/codemirror/       #   the SQL editor bundle (built, committed), served at /vendor
docs/basilisk.webp         # the README's picture (both cut from the original artwork, 1254 px)
tools/codemirror/          # its recipe: pinned package.json, entry.js, build.sh
tests/                     # test_units.py, test_plans.py, test_value_panel.py, test_bff_state.py, test_live.py (needs the databases)
tests/smoke/               # seed_sample.py + ui_smoke.py (Playwright, all four engines)
Containerfile, scripts/podman-run.sh   # image + dev container "basilisk"
```

## Running

```bash
cp .env.example .env                                     # set BASILISK_ADMIN_PASS
uv sync
../wa_pytincture_widgetset/scripts/dev_wheel.sh appcode  # after ANY wapyt asset edit
uv run python service.py                                 # http://127.0.0.1:8768/basilisk/
uv run --group dev pytest -q                             # live tests skip absent servers
scripts/podman-run.sh                                    # dev container: host network, local wapyt
python manage.py reset-password admin                    # clears the change-password reminder
```

| Variable | Default | What |
|---|---|---|
| `BASILISK_ADMIN_USER` / `_PASS` | `admin` / `change_me` | first account, only when there are no users |
| `BASILISK_DATA_DIR` | `./data` (container `/data`) | SQLite store, `secret.key`, `session.key` |
| `BASILISK_SQLITE_ROOT` | `<data>/sqlite` | the only folder SQLite profiles may open files in |
| `BASILISK_SECRET_KEY` / `_SESSION_SECRET` | generated into files | credential key / cookie key |
| `PORT` | `8768` | |
| `BASILISK_HOST` | `127.0.0.1` | loopback address the app is reached on |
| `BASILISK_BIND` | `0.0.0.0` | listen address (`.env.example` sets 127.0.0.1) |
| `BASILISK_CANONICAL_ORIGIN`, `_ALLOWED_HOSTS` | loopback | behind a TLS proxy |
| `BASILISK_UPDATE_CHECK` | on | GitHub latest-release check in About |

### Test databases

Throwaway containers, password **`B@sil:isk/1`** (the `@ : /` are deliberate:
they would break a password pasted into a URL):

```bash
PW='B@sil:isk/1'
podman run -d --name basilisk-pg -p 127.0.0.1:55432:5432 -e POSTGRES_PASSWORD="$PW" docker.io/library/postgres:17
podman run -d --name basilisk-mysql -p 127.0.0.1:53306:3306 -e MYSQL_ROOT_PASSWORD="$PW" docker.io/library/mysql:8.4
podman run -d --name basilisk-mssql -p 127.0.0.1:51433:1433 -e ACCEPT_EULA=Y \
  -e MSSQL_SA_PASSWORD="$PW" -e MSSQL_PID=Developer mcr.microsoft.com/mssql/server:2022-latest
uv run python tests/smoke/seed_sample.py          # the shop sample in all four (SQLite: data/sqlite/shop.db)
uv run --group dev pytest -q tests/test_live.py   # 100 live tests
BASILISK_PASS=… python3 tests/smoke/ui_smoke.py   # ~31 UI checks per engine
```

Users: `postgres` / `root` / `sa`. The sample (`seed_sample.py`): customers,
products (JSON, binary, a bigint past 2^53), orders (137 rows, uuid, time),
order_items (composite key), `audit_log` (no primary key), a real PNG in
`products.photo` and a 32,000-character note on customer 11 (both for the
value panel), view
`order_totals`; PostgreSQL and SQL Server also have a `sales` schema. For the
navigator (`EXTRAS`): `order_total` (overloaded on PostgreSQL), a table
function `customer_orders` (PostgreSQL, SQL Server), procedure
`archive_order` with an output argument, sequence `invoice_no` (PostgreSQL,
SQL Server), trigger `orders_status_check` on `orders` in all four (it only
refuses an empty status), and on PostgreSQL the materialized view
`product_sales` with a unique index.
**Reseed before the smoke test**: `test_live.py` edits rows. The smoke test
needs an admin whose password is not flagged (`manage.py reset-password`) and,
against the container, `shop.db` copied into `/data/sqlite` (`podman cp`).

## Design

### Engines (`services/engines/`)

Everything dialect-specific is an `Engine` method; `sqlops` never branches on
the engine name except through them. **A new engine** = a subclass + an entry
in `engines/__init__.py` + a profile in `test_live.py`.

| | levels (tree) | engine per | read-only | timeout | cancel |
|---|---|---|---|---|---|
| PostgreSQL | database › schema | database | session `READ ONLY` + rollback | `statement_timeout` | `cancel_safe()` |
| MySQL | database (= schema) | profile; `select_db` | session `READ ONLY` + rollback | `max_execution_time` (SELECT only; MariaDB `max_statement_time`) | `KILL QUERY` on a 2nd connection |
| SQL Server | database › schema | database | **rollback only** (no read-only session) | `_conn.query_timeout` | `KILL <spid>`, then the connection is invalidated |
| SQLite | none | profile | `mode=ro` URI + rollback | progress handler | `interrupt()` |

### The pool (`engine_pool.py`)

A SQLAlchemy Engine per `(user, connection, database)` — PostgreSQL and SQL
Server connections are bound to one database. The profile is re-read per
call and the engine rebuilt when its fingerprint (URL, options, read-only)
changes; idle engines are disposed after 15 minutes. The `connect` event
hook keeps only `read_only`, never the password.

### Safety

- Table/schema/column names from the browser are **always quoted by
  SQLAlchemy** (`table()`/`column()` constructs), never pasted into SQL.
- The WHERE / ORDER BY boxes are SQL the person types (they can run any SQL
  in an editor anyway). They run on a connection that is **never committed**,
  with bound LIMIT/OFFSET, so one statement on every driver but pymssql, whose
  batch is rolled back.
- **`typed_sql()` escapes colons**: SQLAlchemy's `text()` reads `:name` (and
  PostgreSQL's `::` casts) as bind parameters.
- Query export uses `exec_driver_sql` with **`no_parameters=True`**: with
  parameters, pyformat drivers treat `%` in `LIKE 'a%'` specially.
- SQL editor statements go to the DB-API cursor **without parameters** for the
  same reason (`'100%'` is tested).
- SQLite paths are relative to `BASILISK_SQLITE_ROOT` and checked on save
  **and** every open (`resolve_file`): a profile must never open the app's own
  database or any other server file.
- Profiles are per user; there is no unscoped read of `connections`.

### Grid editing (DBeaver's model: pending, then Save)

Edits, added rows and deletions stay **pending** (amber cells, green new rows,
struck-through deletions; the tab shows a count badge) until **Save**, which
shows the generated SQL (`save_changes(dry_run=True)`) and then runs it in
**one transaction**: an update or delete that does not match exactly one row
rolls everything back. Rows are found again by their **primary key**, carried
to the browser **losslessly** (`values.encode_key`: type-tagged, bytes in
base64), so a cut-short display never targets the wrong row. Tables without a
primary key, views and read-only profiles are read-only in the grid.

- Values travel as **text** (bigints and decimals keep every digit) and are
  coerced back by the reflected column type (`values.coerce`). **JSON columns
  are parsed** — SQLAlchemy's JSON type has no `python_type` and would have
  stored the text as a JSON *string* (caught by a unit test). MySQL's BOOLEAN
  is TINYINT, so integer columns accept true/false.
- Cells over 20,000 characters (binary over 2 KB) are cut for display, flagged
  `bk-cut`, and edited in the **value panel** instead of the grid
  (`_too_long`).
- Opening a NULL cell and leaving it empty keeps NULL; **Set NULL** and **Set
  empty text** are in the cell menu.
- The DataTable's context menu payload has the row but not the column: a
  capture-phase `contextmenu` listener remembers the cell (`tab["menu_col"]`).
- Pending-cell classes: the class key is not a column, so `_cell_class` sets
  it on the live JS row (`datatable.getRow`) and redraws via `set_cell`.
- Server-side sort: a header click writes `"col" ASC|DESC` (quoted per
  dialect) into ORDER BY and reloads; every column's `sort_by` is a rank key
  signed by the table's direction, so the widget's local sort keeps the
  server's order (Monguana's trick).

### Value panel (`ui/value_panel.py`, milestone 2)

A panel on the right of a table's Data grid and of SQL results (Value button,
**F7**, **Shift+Enter** on a cell, or "View / edit value" in the cell menu),
following the cell last clicked or focused. Open/closed and its width are
remembered per user (`settings.value_panel`).

- **Views**: Text, JSON (pretty, checked as you type), Hex (an `offset hex
  ascii` dump, first 1 MB) and Image (PNG/JPEG/GIF/WebP/BMP/ICO sniffed from
  the bytes; never SVG; a picture that fails to decode falls back to Hex).
  Binary is edited in the Text view as hex.
- **A cut cell is fetched whole** by primary key (`SqlService.cell_value` →
  `sqlops.fetch_cell` → `values.full_value`, base64 for bytes) up to
  `VIEW_MAX_BYTES` (4 MB: binary travels as hex when saved, and pytincture
  refuses requests over 16 MB). Bigger values can only be saved to a file,
  through `GET /bk/cell/<conn>`. Query results keep no key, so a cut result
  value stays cut.
- **Apply** (Ctrl+Enter) hands the value to the page as an ordinary pending
  edit (`_vp_apply`); Save previews and writes it like any other. The grid
  shows the start (`grid_text`). The panel never writes to the database.
- **An unapplied change pins the panel** to its cell ("Apply or discard"),
  and paging or refreshing asks before throwing it away.
- The JSON view re-indents for reading; an edit that leaves the same JSON
  keeps the stored text exactly, and a one-line value stays on one line
  (`json_result`).
- The Save preview cuts any statement over 4,000 characters
  (`sqlops._preview`).
- **A click on a wapyt grid cell does not focus it**, so Shift+Enter after a
  click reaches the document: `_on_keydown` opens the panel on
  `tab["focus_cell"]`, which a capture-phase click listener keeps
  (`_watch_cells`).

### Navigator objects (milestone 2)

- `sqlops.objects` lists tables and views, then the engine's
  `object_groups` (functions, procedures, sequences, triggers; SQLite has
  only triggers, MySQL no sequences). Each extra list fails on its own: its
  group shows the error and the tables still load. On PostgreSQL each runs
  in a **savepoint**, since one failed query aborts the transaction.
- Opening one (`object_info`) answers `engines.base.info`: `[label, value]`
  properties, the definition (the server's own where it has one:
  `pg_get_functiondef`, `SHOW CREATE`, `OBJECT_DEFINITION`, `sqlite_master`;
  generated for sequences and roles), optional lists (role memberships) and
  a note. Object tabs are read-only: properties left, definition right.
- Routine identity: PostgreSQL by **oid** (overloads share a name and are
  labelled `name(args)`), SQL Server by object_id, MySQL by `KIND:name`.
  PostgreSQL trigger names belong to their table, so a trigger is
  `(name, table)`.
- `call_sql` writes a call with `NULL /* name type */` per argument (block
  comments, so a closing `;` never lands in one): `SELECT f(...)`,
  `SELECT * FROM f(...)` for set-returning, `CALL p(...)`; MySQL OUT
  arguments become `@vars` selected after; T-SQL declares `@out_` variables
  for OUTPUT arguments and `EXEC`s with named arguments.
- **Users & roles** (`list_roles` / `role_info`) are server-level, a group
  under the connection after its databases: `pg_roles` (hiding `pg_*`
  unless system objects are shown), `mysql.user` (needs SELECT on `mysql`;
  `SHOW CREATE USER` with the **password hash masked**), SQL Server
  `sys.server_principals` ("Logins & roles", fixed roles and `NT ` accounts
  hidden). Database-level users are not listed yet.
- **Refresh materialized view** (PostgreSQL) shows the statement first;
  `CONCURRENTLY` when the view is populated and has a unique index on plain
  columns. Refused on read-only profiles; prod connections get the red
  confirm.

### Object actions (`ddl.py`, milestone 2)

- `ddl.build(spec, prep, action, schema, table, params)` returns the
  statements; names are always quoted by the dialect's preparer. Dialect
  differences are `Engine` hooks: `identity_clause` (PostgreSQL
  `GENERATED BY DEFAULT AS IDENTITY`, MySQL `AUTO_INCREMENT`, SQL Server
  `IDENTITY(1,1)`, SQLite only inline on a one-column `INTEGER PRIMARY KEY`),
  `add_column_keyword`, `sql_rename_table` / `sql_rename_column` (SQL
  Server: `sp_rename` with the old name as a bracketed string, the new one
  bare), `sql_truncate` (SQLite: `DELETE FROM`), `sql_drop_index`
  (`… ON table` for MySQL and SQL Server).
- Types and defaults are typed SQL, like the editor's; `ddl.sql_bit` only
  refuses what would end the statement early (`;`, comments, unbalanced
  quotes or brackets). An auto-numbered column must be in the primary key.
- `sqlops.alter` previews (dry run) or runs in one transaction with
  `exec_driver_sql` (not `text()`: a default like `'10:30'` is no bind
  parameter). **MySQL commits DDL as it runs**: its note says it cannot be
  rolled back. SQL Server cannot drop a column that has a default:
  `before_drop_column` looks up the constraint and drops it first, shown in
  the preview.
- The page: `_ddl_dialog` is a form whose SQL is re-previewed 250 ms after
  each keystroke; **Run** previews the current fields once more and runs
  exactly that. Drops and truncates use the red `_confirm_sql(danger=True)`.
  Tables with unsaved row edits refuse; changed tables' tabs are reopened.
  The Columns and Keys & indexes pages have the buttons for any table on a
  writable connection (no primary key needed).

### SQL editor

CodeMirror 6 + `@codemirror/lang-sql` (dialect per engine, schema completion
from `completion()` via `get_multi_columns`), built by `tools/codemirror/` into
`window.BkEditor`. **Ctrl+Enter** runs the selection or the statement at the
caret (split server-side by sqlparse, with offsets), **Ctrl+Shift+Enter / Alt+X**
the script, stopping at the first error, which is underlined (PostgreSQL gives
a position; MySQL a line). SQL Server scripts run as **`GO` batches**; statement
mode still splits on `;`. Statements run in **auto-commit**; a read-only
profile runs them in one transaction that is rolled back. A **production**
profile (`env=prod`) asks before running anything whose first word is not a
read (`_READ_FIRST_WORDS`) — a safety net, not a security check. Results:
up to 10,000 rows per result set (500 by default), CSV of what is shown
(DataTable `export_csv`), or **Export all** through `POST /bk/export-query`.
Every statement goes to `query_history` (500 kept per user). The editor text
is saved per connection (`draft:<conn>` in ui_state) and restored when that
connection's first editor opens. The editor/results split is a nested wapyt
`Layout` with a resizable cell.

### Manual commit mode (`sessions.py`, milestone 2)

The SQL editor's **Auto-commit | Manual** switch (remembered per user as
`settings.manual_commit`). In manual mode the tab owns a **pinned
connection** (`Session`, keyed by `(user_id, session id)`; the page makes the
id per tab) and statements run in one transaction until **Commit** or
**Rollback**; the bar shows "● Transaction · N statements" and the tab a `TX`
badge. `execute(..., session=…)` runs through `_execute_manual`;
`SqlService.transaction(session, action)` does commit / rollback / end / state.

- **PostgreSQL wraps each statement in `SAVEPOINT bk_stmt`**: an error rolls
  back to it, so only that statement is undone and the transaction stays
  usable (psql's `ON_ERROR_ROLLBACK`). Statements that are transaction control
  themselves (`_TXN_WORDS`) are not wrapped. Other engines leave a failed
  statement's transaction as is; auto-commit still rolls back on error, manual
  mode never does.
- **Idle transactions are rolled back** after 10 minutes (`IDLE_SECONDS`) by a
  daemon reaper thread: an open transaction holds locks. The id is remembered
  for an hour (`_expired`), so the next run or commit says what happened
  (`session_lost`) instead of quietly starting a new transaction.
- Leaving manual mode, or switching database, with work pending asks
  **Commit / Roll back / Cancel** (`_settle_txn`). Closing the tab cannot be
  vetoed (wapyt TabWidget has no before-close), so it **rolls back** and says
  so in a toast. Leaving the page warns (`beforeunload`).
- Editing, deleting or disconnecting a profile ends its sessions
  (`end_for_conn`); deleting a user, theirs; shutdown, all.
- Read-only profiles cannot use it (they roll back everything anyway); at most
  5 sessions per user (each pins a pool connection); one run at a time per
  session (its lock).
- Commit on a **production** profile asks first.
- `transaction_state` per engine: PostgreSQL reads `transaction_status`
  (and knows a **failed** transaction, which can only be rolled back); SQLite
  runs in PEP 249 mode (`autocommit = False`, so DDL is transactional too) and
  is put back to legacy control on release; MySQL counts statements.

### Explain plans (`plans.py`, milestone 2)

**Explain** (Ctrl+Shift+E) shows the estimated plan of the statement at the
cursor (or the selection); **Analyze** runs it for actual rows, loops and
times. Each engine's `explain()` produces its native plan and `plans.py`
turns it into one node shape (`op`, `object`, `detail`, `cost`, `est_rows`,
`act_rows`, `time_ms`, `loops`, `extra`, `children`); `flatten` gives the
grid's rows and `annotate` adds `share` and `misestimate`. `test_plans.py`
pins each parser on captured output.

| | estimated | actual (Analyze) |
|---|---|---|
| PostgreSQL | `EXPLAIN (FORMAT JSON)` | `+ ANALYZE, BUFFERS` |
| MySQL 8 | `EXPLAIN FORMAT=TREE` (text, parsed by indent) | `EXPLAIN ANALYZE` (SELECT and multi-table UPDATE/DELETE only) |
| MariaDB / old MySQL | tabular `EXPLAIN` | `ANALYZE` (tabular, `r_rows`) |
| SQL Server | `SET SHOWPLAN_XML ON` | `SET STATISTICS XML ON` (the plan is an extra result set after the rows) |
| SQLite | `EXPLAIN QUERY PLAN` (no costs) | not possible |

- **Analyze executes the statement** on a connection that is never committed,
  so a write is rolled back; a non-read statement needs confirming first
  (`needs_confirm`), because effects outside the transaction (sequences,
  triggers that call out) stay. It uses a pooled connection, not a manual-
  commit session: the plan sees committed data only.
- **"Own cost %"** is a node's cost minus its children's, over the statement's
  total: engines report costs cumulatively, which painted every parent 100%.
  ≥30% is red, ≥10% amber. PostgreSQL and MySQL report rows and times **per
  loop**; they are multiplied out, and the estimate is compared with
  `est_rows × loops`. An estimate off by 10x (with 100+ rows) gets a warning
  icon. Double-click a node for every field the engine gave.
- **Raw** shows the engine's own JSON, text or XML.

## Things that will bite

- Everything in Monguana's list: `APP_ENTRYPOINT`, BFF modules re-executed per
  call (state lives in `engine_pool` / `sqlops`; `test_bff_state.py`), UI in
  `load_ui`, `*_async` BFF calls, hooks by dotted path +
  `AUTH_SESSION_CLAIM_KEYS`, absolute imports, import names not modules in
  the page, literal loopback IP (never `localhost`), `JsNull` ≠ `None`, keep app
  URLs absolute (`/static`, `/bk`, `/vendor`), no CDN.
- **A JS object from the editor is a JsProxy**: `getSelection()` has a `from`
  key, a Python keyword — call `.to_py()` and index it.
- **pytincture validates BFF arguments by annotation**; BFF methods here take
  plain positional arguments, called positionally from the page.
- **`pkill -f "python service.py"` kills your own shell** when the command line
  contains that text; stop the dev server by PID.
- **wapyt TabWidget ignores `TabConfig.icon`** and its badge has no dark colours
  (styled in `_CSS`). Worth fixing in wapyt.
- MySQL and SQLite return NULL for `1/0`; PostgreSQL and SQL Server raise.
- SQL Server `VARCHAR` cannot hold an emoji (the seed uses `NVARCHAR`).
- MySQL reorders JSON object keys.
- **Plain modules (`plans.py`, `sqlops.py`, `sessions.py`, engines) load once**:
  restart the dev server after editing them. Only BFF modules and the page
  are re-read per request.
- **pymssql opens its own `BEGIN TRANSACTION`** after every commit and
  rollback when auto-commit is off, so `@@TRANCOUNT` is always 1: SQL Server's
  "transaction open" needs statements counted too. A `COMMIT` typed into the
  script leaves pymssql's state behind; use the Commit button.
- **MySQL commits implicitly on DDL** (CREATE, ALTER, DROP…), even in manual
  mode; the indicator, which counts statements, cannot tell.

## Conventions

- `BASILISK_*` environment prefix, port **8768**, `/data` volume, SQLite +
  Fernet at rest, MIT.
- Version in `pyproject.toml` **and** `appcode/services/about.py` (`test_units`
  checks); the editor bundle and its `VERSION` are committed together (also
  checked).
- Default branch `main`; every change ships on a feature branch with a PR.
  Commit style: imperative sentence case, no prefix. Commit means commit and push.
- This file is tracked.
