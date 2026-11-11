# Basilisk — SQL database browser on pytincture + wapyt

A browser-based SQL database manager in the spirit of DBeaver, for
**PostgreSQL, MySQL / MariaDB, SQL Server, SQLite, DuckDB and CockroachDB**. The page is Python in
Pyodide built from **wapyt** widgets (`../wa_pytincture_widgetset`); the server
is **pytincture** (FastAPI) talking to the databases through **SQLAlchemy 2**
and their drivers. Started 2026-10-24, modelled on **Monguana**
(`../monguana_wapyt`, the MongoDB GUI) for its service wiring, auth, login
page, profiles and container. Read Monguana's CLAUDE.md for the framework
traps; the ones that bite here are summarised below, not re-explained.

Private repo: El-Iguana/basilisk_wapyt. Next work: `ROADMAP.md`. Releases
(`v*` tags, the Windows installer attached) since **0.1.0** (2026-10-26).
**The wiki** (`basilisk_wapyt.wiki.git`, pushed directly, no PRs) is the user
guide, one page per feature with screenshots in its `images/`: update the page
when a feature changes, as README and INSTALL.md.

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
      duckdb.py duckdb_dialect.py   # DuckDB + its SQLAlchemy dialect (reflection from duckdb_*())
      cockroachdb.py       #     CockroachDB: PostgreSQL's adapter + a dialect subclass
    engine_pool.py         #   SQLAlchemy engines per (user, connection, database) — plain module
    tunnels.py             #   SSH tunnels: the local forwarder, profile SSH settings — plain module
    ssh.py                 #   paramiko helpers from IguanaXterm: key loading, host-key pinning
    sessions.py            #   manual commit mode: pinned connection per SQL tab, idle reaper — plain module
    sqlops.py              #   everything done to a database; the running-statement registry
    ddl.py                 #   object actions' statements (create/rename/drop/truncate, columns, indexes) — pure
    schema_compare.py      #   schema compare: read two schemas, the differences and their DDL — plain module
    data_compare.py        #   data compare: rows of two tables by key, the INSERT/UPDATE/DELETE script (a job) — plain module
    csv_import.py          #   CSV import: option checks, the import job — plain module
    jobs.py                #   background jobs with progress (from Monguana) + import plans — plain module
    job_service.py         #   BFF: plan an import, start a data compare, follow / cancel a job
    values.py              #   driver values <-> grid text; lossless row keys; typed coercion
    plans.py               #   explain plans from every engine in one tree shape (pure parsers)
    connection_service.py  #   BFF: profiles, test, disconnect
    sql_service.py         #   BFF: thin front for sqlops (tree, table, browse, save, execute…)
    user_service.py, ui_state_service.py, about*.py, release_check.py   # from Monguana
    transfer.py            #   plain routes under /bk: table export (GET), query export (POST), one cell (GET),
                           #   CSV import upload (POST)
  ui/value_panel.py        #   the value panel (Pyodide; pure helpers at the top, tested in CPython)
  ui/grid_tools.py         #   grids: column layouts, column filters, copy as INSERT / Markdown, FK rows — pure
  ui/import_tools.py       #   the Import CSV dialog: sniffing, preview, column matching — pure
  ui/er_layout.py          #   the ER diagram: model, layout, SVG — pure
  ui/compare_tools.py      #   the compare tabs (schema, data): rows, the ticked differences' script — pure
  static/                  #   basilisk_avatar.webp: logo, login page, favicon, About
  vendor/codemirror/       #   the SQL editor bundle (built, committed), served at /vendor
docs/basilisk.webp         # the README's picture (both cut from the original artwork, 1254 px)
tools/codemirror/          # its recipe: pinned package.json, entry.js, build.sh
tests/                     # test_units.py, test_plans.py, test_value_panel.py, test_grid_tools.py,
                           # test_import_tools.py, test_er_layout.py, test_tunnels.py, test_schema_compare.py, test_data_compare.py,
                           # test_bff_state.py, test_live.py (needs the databases)
tests/smoke/               # seed_sample.py + ui_smoke.py (Playwright, all four engines)
Containerfile, scripts/podman-run.sh   # image + dev container "basilisk"
compose.yaml, compose.host-network.yaml   # users' installs (INSTALL.md); volume basilisk-data
INSTALL.md, docs/UPGRADE_TEST.md       # the install guide; the hand test for an in-place update
tools/windows/             # native Windows install: build.py, launcher.py, basilisk.iss, basilisk.ico
.github/workflows/windows.yml          # Windows: unit tests, build setup.exe, install/start/stop/uninstall
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
| `BASILISK_BIND` | `BASILISK_HOST` (the image: `0.0.0.0`) | listen address |
| `BASILISK_PORT` | `8768` | compose only: the published (host) port |
| `BASILISK_CANONICAL_ORIGIN`, `_ALLOWED_HOSTS` | loopback | behind a TLS proxy |
| `BASILISK_UPDATE_CHECK` | on | GitHub latest-release check in About |
| `BASILISK_IMPORT_MAX_BYTES` | 1 GiB | largest CSV file an import accepts |

### Test databases

Throwaway containers, password **`B@sil:isk/1`** (the `@ : /` are deliberate:
they would break a password pasted into a URL):

```bash
PW='B@sil:isk/1'
podman run -d --name basilisk-pg -p 127.0.0.1:55432:5432 -e POSTGRES_PASSWORD="$PW" docker.io/library/postgres:17
podman run -d --name basilisk-mysql -p 127.0.0.1:53306:3306 -e MYSQL_ROOT_PASSWORD="$PW" docker.io/library/mysql:8.4
podman run -d --name basilisk-mssql -p 127.0.0.1:51433:1433 -e ACCEPT_EULA=Y \
  -e MSSQL_SA_PASSWORD="$PW" -e MSSQL_PID=Developer mcr.microsoft.com/mssql/server:2022-latest
tests/smoke/cockroach.sh                          # CockroachDB basilisk-crdb on 127.0.0.1:56257 (root, no password)
uv run python tests/smoke/seed_sample.py          # the shop sample in all six (files: data/sqlite/shop.db, shop.duckdb)
tests/smoke/ssh_server.sh                         # SSH server basilisk-ssh on 127.0.0.1:52222 (tunnel tests)
uv run --group dev pytest -q tests/test_live.py   # 216 live tests
BASILISK_PASS=… python3 tests/smoke/ui_smoke.py   # ~95 UI checks per engine (six engines: ~1 hour)
```

Users: `postgres` / `root` / `sa` / `root` (CockroachDB, insecure, any password). The sample (`seed_sample.py`): customers,
products (JSON, binary, a bigint past 2^53), orders (137 rows, uuid, time),
order_items (composite key), `audit_log` (no primary key), a real PNG in
`products.photo` and a 32,000-character note on customer 11 (both for the
value panel), view
`order_totals`; PostgreSQL, SQL Server and CockroachDB also have a `sales` schema. For the
navigator (`EXTRAS`): `order_total` (overloaded on PostgreSQL), a table
function `customer_orders` (PostgreSQL, SQL Server), procedure
`archive_order` with an output argument, sequence `invoice_no` (PostgreSQL,
SQL Server), trigger `orders_status_check` on `orders` in all four (it only
refuses an empty status), and on PostgreSQL the materialized view
`product_sales` with a unique index. CockroachDB gets PostgreSQL's extras
(its trigger without `UPDATE OF status`: no column lists); DuckDB only the
sequence, and sequence defaults number its ids (no SERIAL, no ON DELETE).
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
| DuckDB | none (`main`) | profile | `read_only=True` open | a timer that interrupts | `interrupt()` |
| CockroachDB | database › schema | database | as PostgreSQL | `statement_timeout` | `cancel_safe()` |

`Engine.family` says whose SQL an engine speaks, for the page's quoting and
literals (`ui/grid_tools.py`, the tab's `engine`) and dialect choices in
sqlops (CockroachDB is `postgresql`, DuckDB `duckdb`). Feature flags rather
than names elsewhere: `uses_server` (a file engine), `has_matviews`,
`savepoints`, `can_analyze`, `identity_clause` (None: no self-numbering
column), `can_alter_keys`, `fk_actions`, `alter_blocked_by_dependents`,
`add_column_constraints`. `connection_service._public` sends `family`,
`uses_server`, `can_analyze` and `has_identity` to the page.

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

### Grids: layouts, column filters, copy as, foreign keys (milestone 2)

Pure helpers in `ui/grid_tools.py`; the page draws.

- **Column layout** per table and user in `ui_state` (`columns:<conn>:<db>:<schema>:<table>`):
  `{order, widths, hidden, frozen}` by column **name** (grid ids are `c<i>`
  by result position). wapyt's `columns` event (resize, reorder, hide, show)
  feeds `merge_layout`, saved 0.4 s after the last change; `apply_layout`
  orders the next page's columns, skipping gone ones and appending new ones,
  and never hides them all. Columns… shows / hides, sets how many to freeze
  and resets. Query results are not saved (their shape follows the SQL); their
  frozen count is a setting (`settings.freeze_results`).
- **Column filters** are chips. On a table they become WHERE conditions
  (`filter_sql`, ANDed with the typed WHERE by `where_with_filters`), so they
  filter every row, count, export and "SQL" too; values are always literals
  (`literal`: a "number" that is not one is quoted). Contains / starts with
  use `LIKE … ESCAPE '!'` (PostgreSQL `ILIKE` on `CAST(… AS TEXT)`, SQL
  Server `CAST(… AS NVARCHAR(MAX))`, whose `[` is also escaped). **MySQL
  string literals double backslashes** (`text_literal`). On a result,
  `filter_rows` filters the fetched rows; row ids keep their row number, so
  the value panel still finds its cell.
- **Copy as INSERT / Markdown**: the clicked row or the selection it is in,
  visible columns in display order. Tables fetch cut values whole
  (`_full_text`); a result says how many were cut. A result's INSERT target
  is `table_of_query` (one plain `FROM t`, quoted names and strings masked
  before looking for keywords, so `"Order Items"` is no ORDER BY), else
  `table_name`.
- **Open the referenced row**: `foreign_key_for` (single-column keys first)
  and `reference_where` build `ref_col = value` for the referenced table,
  opened (or refiltered) with that WHERE. FK columns get a link icon in the
  header (`bk-fkcol`).

### Import CSV (`csv_import.py`, milestone 2)

- The dialog reads the first 256 KB in the browser (`File.slice`), guesses
  encoding (UTF-16 BOM, UTF-8, else Windows-1252) and delimiter, maps columns
  by header name (case, spaces, underscores ignored) or by position, and
  previews what will be stored. `JobService.plan_import` checks everything
  against the reflected table **before the upload** and keeps a plan
  (`jobs.put_plan`, per user, 15 minutes, used once); `POST /bk/import/<plan>`
  takes the raw file (XHR, for upload progress), writes it to `data/jobs`
  and starts the job; the console polls `JobService.status`.
- `csv_import.run` coerces each field with `values.coerce` (the grid's rules)
  and inserts 500 rows per `executemany`, all in **one transaction**: a
  field that does not convert (unless "skip its row"), a row the database
  refuses (named by its rows in the file) or Cancel rolls everything back.
  "Delete them first" is a `DELETE` in the same transaction (not TRUNCATE,
  which MySQL commits). An empty field in a non-text column is NULL even when
  empty fields are kept as empty text.
- Self-numbering columns: `import_wrappers` gives SQL Server
  `SET IDENTITY_INSERT … ON/OFF` when one is mapped, and PostgreSQL a
  `setval(pg_get_serial_sequence(…), MAX + 1, false)` afterwards so the next
  insert does not collide. MySQL and SQLite move on by themselves.
- `csv.reader` gives `[]` for a blank line but `['']` for a line that is
  just `""`, a real empty value: only `[]` is skipped.

### Session manager (milestone 2)

A **Sessions** tab per server (navigator menu): `Engine.list_sessions`
(`pg_stat_activity` client backends; `SHOW FULL PROCESSLIST`;
`sys.dm_exec_sessions` + requests + `dm_exec_sql_text`) normalised by
`sqlops.sessions`; idle sessions hidden unless asked (SQL Server shows
sleeping ones holding a transaction, which every pymssql connection does).
Refreshed every 5 s while the tab is in front. The asking connection is
marked ★ and cannot be stopped. **Cancel query**: `pg_cancel_backend`,
`KILL QUERY`; **End session**: `pg_terminate_backend`, `KILL`. SQL Server
has only `KILL`, which refuses to run inside a transaction: it runs on a raw
auto-commit connection that is then thrown away. Refused on read-only
profiles; production asks in red. The pool pre-pings, so ending one of
Basilisk's own pooled connections costs nothing but that statement.

### ER diagram (`ui/er_layout.py`, milestone 3)

- `sqlops.er_model` reads a location with SQLAlchemy's bulk reflection
  (`get_multi_columns` / `_pk_constraint` / `_foreign_keys`: one query per
  kind, not per table), at most 400 tables. Types lose `COLLATE` and
  character sets (`_short_type`); `ref_schema` is None for the same schema
  (MySQL reports the database).
- `er_layout` is pure and drawn by character count (monospace text): layers
  by longest path from the referenced tables (cycles broken by a DFS), a
  table only referenced from far away pulled next to its nearest referrer,
  barycentre sweeps per layer, connected groups packed in rows, unconnected
  tables last. A table in another schema is a dashed **ghost** box. Lines are
  cubic curves between facing sides at the columns' rows: a crow's foot at
  the referencing end (a circle when its columns are nullable), two bars at
  the referenced end. One `<style>` inside the SVG serves the page (dark)
  and the export (light, `standalone=True`, own size and background).
- The tab pans (drag the background), zooms (wheel, around the cursor) and
  drags tables, redrawing only their lines. Positions and the column mode are
  saved per schema and user (`er:<conn>:<db>:<schema>` in `ui_state`); a
  table's own diagram (**ER diagram around this table**) is not saved.
  **Pointer capture sends `dblclick` to the host, not the table**: two clicks
  on one table within 400 ms open it instead.

### Schema compare (`schema_compare.py`, milestone 3)

- **Compare schema with…** on a server, database or schema opens a tab with a
  source and a target (connection, database, schema each; any two
  locations, different engines too). `sqlops.compare_schemas` reads both with
  bulk reflection (`snapshot`), and `compare` answers one difference per
  object, each with its statements **for the target**, tagged with a phase
  (`PHASES`: drop views, drop FKs, drop indexes, drop PKs, create tables,
  add / change / drop columns, add PKs, create indexes, add FKs, drop
  tables, views). The page (`ui/compare_tools.script`) puts the ticked
  differences' statements in phase order, so any subset runs top to bottom;
  drops start unticked. The script opens in a SQL editor on the target:
  nothing runs from the compare tab itself.
- Tables, views and columns pair by name (exactly, then ignoring case); keys,
  indexes (unique constraints and unique indexes are one thing) and foreign
  keys pair **by their columns**: engines invent their names. A changed FK
  is a drop and an add.
- Dialect differences are `Engine` hooks: `sql_alter_column` (PostgreSQL
  `TYPE … USING`, MySQL `MODIFY` restating the column, SQL Server drops the
  default constraint (looked up in the script itself, `_drop_default_sql`)
  before changing the type, standard SQL otherwise), `sql_drop_constraint`
  (MySQL `DROP FOREIGN KEY` / `PRIMARY KEY` / `INDEX`),
  `sql_script_before_drop_column`, `replace_view` (`CREATE OR ALTER` on SQL
  Server; SQLite drops and creates), `script_batches` (SQL Server scripts
  end each statement with `GO`), `before_compare`.
- **SQLite cannot alter a column, key or FK**: a table with such changes is
  one "Rebuilt" difference (`_Writer.rebuild`): `foreign_keys` off, new table
  `_bk_new_<t>`, rows copied, old table dropped, renamed (with
  `legacy_alter_table` on: a view on the table fails the rename otherwise),
  indexes made again, `foreign_key_check`. Triggers on it are lost (noted).
- **Between engines** types compare by family (`family`: INT, TEXT,
  VARCHAR(n), NUMERIC(p,s)…) and are written through `as_generic()` without
  collation; MySQL TINYINT(1) is a boolean, SQL Server keeps JSON as
  NVARCHAR(max), MySQL and SQLite keep UUIDs as CHAR(32), MySQL's own
  index per foreign key is no difference, and SQLite's rowid key numbers
  itself whether asked or not. Defaults compare only when both are plain
  values (`portable_default`); view definitions are not compared and are
  written into the script commented out.
- **A key column is never NULL** whatever the engine reports (SQLite says a
  rowid key is nullable).
- **PostgreSQL view definitions leave out the schema** of tables on the
  search path, so `before_compare` sets `search_path` to `pg_catalog` for the
  read (never committed); `retarget` then points the source schema's names at
  the target's. SQL Server definitions are the text as written: unqualified
  names keep meaning the default schema (noted when the schemas differ).
- `tests/test_schema_compare.py` round-trips two SQLite files;
  `test_live.py` round-trips each engine (compare, run the script, compare
  again: nothing left) and copies each server's schema into SQLite and back.

### Data compare (`data_compare.py`, milestone 3)

- **Compare data with…** on a table opens a "datacmp" tab: the compare tab
  (same pickers, grid, script panel and `_cmp_*` code, branching on the
  kind) with a table per side and a WHERE box used on both sides.
- `JobService.compare_data` checks first (`check`: tables, columns paired by
  name, the key: the **target's primary key**, whose columns the source must
  have, else the source's with a note), then runs `run` as a job; the page
  polls `JobService.status` and shows the steps, with Cancel. The result
  rides on the finished job.
- `run` reads the source into `{key: digest}` (blake2b of the normalised
  values; at most `MAX_ROWS`), streams the target sorting each row into
  same / update / delete, takes source keys never seen as inserts, and reads
  the source again for the listed rows (`MAX_LISTED` per kind). A key seen
  twice on one side is refused (the target's key may not be unique in the
  source).
- **Values are read untyped** (the driver's own values, no SQLAlchemy result
  processing): SQLite keeps any value in any column, so a `CREATE TABLE AS`
  copy whose DATE became NUM made typed reading fail. The normalisers read
  text too.
- **Normalised comparison** (`normaliser`, kind by `pair_kind`, the stronger
  kind of the two columns): numbers as normalised decimals; JSON parsed and
  sorted, a JSON `null` equal to NULL; UUIDs as hex (CHAR(32) on MySQL and
  SQLite); booleans from 0/1, text and MySQL BIT bytes; dates, times and
  timestamps parsed from objects or ISO text, time zones to UTC, MySQL TIME
  (a timedelta) as a time of day; CHAR without its padding.
- **Literals for the target** (`literal`): `Engine.sql_text_literal`
  (MySQL doubles backslashes, SQL Server `N''`), `sql_bytes_literal`
  (PostgreSQL `'\x..'::bytea`, SQL Server `0x..`, `X'..'`), `bool_literals`
  (SQL Server 1/0), `sql_datetime_literal` (SQL Server `DATETIME` takes
  milliseconds only, `DATETIME2` seven digits). UPDATE sets only the
  differing, writable columns; WHERE uses the target's key values; inserts
  are wrapped by `import_wrappers` (SQL Server `IDENTITY_INSERT`,
  PostgreSQL `setval`). The script is deletes, updates, inserts, and says
  that Manual commit mode applies it as one transaction.
- `tests/test_data_compare.py` (pure parts); `test_live.py` round-trips a
  table with awkward values (quotes, backslashes, Unicode, bytes, booleans,
  decimals, microseconds) on each engine and across engines both ways.

### DuckDB and CockroachDB (milestone 3)

- **DuckDB** runs in-process (`duckdb` < 2 + duckdb-engine 0.17). Its
  dialect inherits SQLAlchemy's PostgreSQL reflection, whose 2.1 queries
  (`pg_collation`, `REGCLASS`, `pg_inherits`) DuckDB cannot answer, so
  `duckdb_dialect.BasiliskDuckDBDialect` (`duckdb+basilisk`) reads
  `duckdb_tables()`, `duckdb_columns()`, `duckdb_constraints()`,
  `duckdb_indexes()` instead, names types as DuckDB does (BLOB, DOUBLE,
  TIMESTAMP), and quotes every keyword DuckDB's grammar keeps (`at`,
  `columns`…: not only the "reserved" category). duckdb-engine's own
  `get_indexes` answers [] and is overridden too.
- DuckDB quirks the adapter covers: a write answers a **`Count` row**, not a
  rowcount (`Engine.affected_rows`, `dml_count`); statements commit unless
  `BEGIN` ran (explain's Analyze begins one and rolls back); no statement
  timeout (a `threading.Timer` interrupts); no setval (`import_wrappers`
  draws the sequence past imported ids with `nextval` over a `range`);
  binary literals are `'\xHH…'::BLOB` per byte; `ADD COLUMN` takes no NOT NULL
  (set after); no ON DELETE / UPDATE actions; keys cannot change on an
  existing table; a column of a table an index or a foreign key depends on
  cannot change type, gain NOT NULL or be dropped (compare marks those "by
  hand"); VARCHAR keeps no length (compare treats VARCHAR(n) as its VARCHAR).
- **DuckDB opens a file one way per process**: a read-only and a read-write
  profile on the same file cannot both be open (the dialect's `connect`
  says so plainly); ATTACH cannot work around it ("unique file handle").
- **CockroachDB** is PostgreSQL's adapter on sqlalchemy-cockroachdb, with
  `BasiliskCockroachDialect` (`cockroachdb+basilisk`) fixing: the table list
  (views and materialized views were in it), foreign keys read table by
  table (the multi query does not fit 2.1), identity columns (not reported),
  the **hidden `rowid` key** of a table made without one (reported as its
  key; Basilisk never reads hidden columns, so it is no key here), and view
  definitions (`pg_get_viewdef` answers another schema's view of the same
  name; `SHOW CREATE VIEW` is right).
- CockroachDB 25+ tables are **schema-locked**: a lone `DROP CONSTRAINT` on
  the key is refused and `ALTER PRIMARY KEY` keeps the old key as a unique
  index, so `sql_change_primary_key` drops and adds in one statement.
  `ALTER COLUMN … TYPE` beyond a safe widening needs
  `enable_experimental_alter_column_type_general`, set first in the script.
  EXPLAIN is a text tree (`plans.parse_cockroach`).
- View definitions name the database (`shop.public.orders`): `retarget`
  treats the middle of a three-part name as the schema.

### SSH tunnels (`tunnels.py`, milestone 3)

- A profile's `ssh` column is encrypted JSON: enabled, host, port, username,
  auth (`password` | `key`), password or private_key + passphrase, and the
  pinned `host_key`. The browser gets `tunnels.public_settings` only: no
  secrets, the key as its `SHA256:` fingerprint. Secrets are three-state as
  the database password (absent keeps, `""` clears; the editor sends a single
  space to clear); switching auth drops the other secret.
  `tunnels.merge_settings` forgets the pin when the SSH host or port changes.
- `engine_pool.build_engine` gives each engine with SSH on its own
  `Tunnel`: the SSH connection is made **then** (so its error is the one
  shown), a loopback listener on an ephemeral port forwards each driver
  connection over `direct-tcpip`, and the URL points at it. Disposing the
  engine closes the tunnel (`_tunnels`, keyed by the engine). The pool builds
  engines **outside its lock** now (an SSH dial can take seconds) and rebuilds
  one whose tunnel is no longer healthy; a tunnel also redials by itself for
  its next connection.
- **Host keys**: pinned on first use like IguanaXterm (`ssh.PinnedHostKeyPolicy`);
  the pool saves it (`db.pin_ssh_host_key`, never over an existing pin), and
  Test returns the key it saw so the editor pins it on Save. A changed key is
  refused with `HostKeyChanged`; the editor's "Forget the pinned key" clears
  it. Never the server's own agent or `~/.ssh` keys (`allow_agent=False`).
- The test SSH server (`tests/smoke/ssh_server.sh`) is Alpine + OpenSSH on
  the host network, so "127.0.0.1:55432" is the database as it sees it.
- TLS with hostname verification (PostgreSQL `verify-full`) checks the
  certificate against `127.0.0.1`, the tunnel's end, and fails.

### SQL editor

CodeMirror 6 + `@codemirror/lang-sql` (dialect per engine, schema completion
from `completion()` via `get_multi_columns`), built by `tools/codemirror/` into
`window.BkEditor`. **Ctrl+Enter** runs the selection or the statement at the
caret (split server-side by sqlparse, with offsets), **Ctrl+Shift+Enter / Alt+X**
the script, stopping at the first error, which is underlined (PostgreSQL gives
a position; MySQL a line). SQL Server scripts run as **`GO` batches**; its
statement mode (`split_statements`) splits a batch on **blank lines** outside
strings, comments, brackets and `BEGIN/CASE … END`, never on `;` (that broke
`DECLARE` scope), and a `CREATE`/`ALTER` of a routine, trigger or view takes the
rest of its batch. Statements run in **auto-commit**; a read-only
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

## Installing (users): compose, Windows

**INSTALL.md** is the users' guide; keep it true when the image, the compose
files, the launcher or the ports change, and its section 10 says what was
tested. Traps, all measured (2026-10-26), most shared with Monguana (read its
CLAUDE.md, *How the image is built* and *Native Windows install*):

- **`BASILISK_BIND` is not in `.env.example` any more**, and the server
  defaults to `BASILISK_HOST`. `docker run --env-file .env` used to override
  the image's `0.0.0.0` with `127.0.0.1` and the published port answered
  nothing. The compose files still set it (0.0.0.0 bridged, the host address
  with host networking).
- **From a bridge network, `host.*.internal` does not reach the host's
  127.0.0.1** (timed out here): the test databases are published on
  127.0.0.1, so they need `compose.host-network.yaml` (or a bundled server,
  `--profile postgres`). `manage.py probe` tells the cases apart.
- Container, image and volume names are fixed (`basilisk`, `basilisk-data`), so
  a ZIP update in a new folder takes over the same data. The dev container
  (`scripts/podman-run.sh`) uses the volume **`basilisk_data`** (underscore):
  separate data.
- **The Windows bundle** (`tools/windows/build.py`, Monguana's builder) is the
  official embeddable CPython plus Windows wheels from `uv.lock` (`uv pip
  --python-platform`, `--only-binary`): every driver has one (psycopg-binary,
  pymssql, DuckDB ~60 MB of the ~150 MB). It builds on Linux; only Inno Setup
  needs Windows. `uv run python build/windows/bundle/app/basilisk_launcher.py
  --check` (with `BASILISK_DATA_DIR` set) tests the launcher here.
- The launcher runs on **127.0.0.1**, ports 8768–8799 (Monguana keeps
  127.0.0.2: Basilisk's cookies are namespaced, so it needs no own address);
  `BASILISK_SQLITE_ROOT` is `%LOCALAPPDATA%\Basilisk\databases` (tray:
  *Database files folder*). The uninstaller's "delete your data" says it
  deletes those files too; the default keeps everything.
- **The installer's AppId** (`basilisk.iss`) must never change: Windows
  matches upgrades and the uninstaller by it.
- A `v*` tag runs the workflow and attaches `Basilisk-<version>-setup.exe` to
  the release (created if missing). The image installs `requirements.txt` with
  pip, which resolves older packages than `uv.lock` (SQLAlchemy 2.0 vs 2.1):
  the Windows bundle follows `uv.lock`, the image does not.

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
- **A multi-select DataTable's first cell is its checkbox**: in tests, find
  cells by `data-column-id`, never by position. A tab's hidden sub-pages hold
  grids too, so scope row counts to `.bk-vp-grid`.
- **`io.TextIOWrapper` needs a buffered stream**: a counting wrapper is an
  `io.RawIOBase` inside `io.BufferedReader`.

## Conventions

- `BASILISK_*` environment prefix, port **8768**, `/data` volume, SQLite +
  Fernet at rest, MIT.
- Version in `pyproject.toml` **and** `appcode/services/about.py` (`test_units`
  checks); the editor bundle and its `VERSION` are committed together (also
  checked).
- Default branch `main`; every change ships on a feature branch with a PR.
  Commit style: imperative sentence case, no prefix. Commit means commit and push.
- This file is tracked.
