# Basilisk roadmap

## Milestone 1: core browser — done (2026-10-24)

Connection profiles for PostgreSQL, MySQL/MariaDB, SQL Server and SQLite
(test, folders, connection types, read-only, timeouts); the navigator
(databases › schemas › tables/views, row estimates, system objects on
request); the table editor (paged data, WHERE / ORDER BY, header sort,
pending edits saved as one previewed transaction, add/duplicate/delete rows,
Set NULL, filter by value, Columns / Keys & indexes / DDL); the SQL editor
(CodeMirror + lang-sql with schema completion, statement / script runs,
cancel, error position, results grid, messages, history, format, drafts);
CSV/JSON export of tables and query results; users, password reminder,
About; container image. 58 unit and live tests, a 4-engine Playwright smoke
test.

## Milestone 2: DBeaver parity, the everyday parts — done (2026-10-26)

- ~~**Manual commit mode**~~ — done (2026-10-24): Auto-commit | Manual switch,
  pinned connection per tab, Commit / Rollback, transaction indicator and tab
  badge, PostgreSQL savepoints per statement, idle rollback after 10 minutes,
  prompts before leaving the mode or switching database with work pending.
  Follow-ups: a before-close hook in wapyt's TabWidget so closing a tab can ask
  instead of rolling back; a "smart commit" option (auto-commit reads, manual
  for writes).
- ~~**Explain plan**~~ — done (2026-10-24): Explain (Ctrl+Shift+E) and Analyze
  for all four engines, one plan grid with own-cost shading, misestimate
  warnings, node details and the raw plan. Follow-ups: a graphical plan
  (boxes and arrows), and comparing two plans.
- ~~**Value viewer / editor panel**~~ — done (2026-10-25): a side panel on
  table grids and SQL results (F7, Shift+Enter) with Text / JSON / Hex /
  Image views; cut values fetched whole by primary key; edits are pending
  grid edits; load from / save to a file. Follow-ups: editing a cut value in
  a result set (needs the row's key), a wapyt DataTable "focus on click".
- ~~**More objects in the navigator**~~ — done (2026-10-26): functions and
  procedures (overloads apart, Call… writes a call), sequences, triggers, a
  read-only Users & roles group per server, PostgreSQL materialized view
  refresh (concurrently when allowed). Follow-ups: triggers listed in a
  table's own tab; SQL Server database users and MySQL/MariaDB events;
  MariaDB sequences; role privileges (grants on objects); hiding
  extension-owned PostgreSQL functions.
- ~~**Object actions**~~ — done (2026-10-26): create table (columns, types,
  primary key, an auto-numbered column), rename, truncate, drop tables and
  views; add / rename / drop columns; create / drop indexes. Each previews
  its SQL live and runs in one transaction where the engine allows.
  ~~Change a column's type / NULL / default~~ — done (2026-11-11): a
  Change… button on the Columns tab, every engine; SQLite rebuilds the table
  in one transaction (indexes and triggers made again). Follow-ups: foreign keys and check
  constraints; PostgreSQL DROP … CASCADE; create view; drop routines,
  sequences and triggers.
- ~~**Import CSV**~~ — done (2026-10-26): from a table's menu or its Data
  bar; delimiter and encoding guessed, columns matched by header, a preview,
  then a job (upload progress, a bar, the log, Cancel) that inserts the whole
  file in one transaction; bad values stop it or skip their row; append or
  replace; SQL Server identity and PostgreSQL sequences handled. Follow-ups:
  date formats other than ISO (a format box per column); skipping rows the
  database refuses (savepoints per batch); Excel files; import into a new
  table (create it from the file's columns).
- ~~**Column layout memory**~~ — done (2026-10-26): widths, order, hidden
  columns and frozen count per table and user, on the server; a Columns
  dialog (show / hide, freeze, reset). Follow-ups: a header menu in wapyt's
  DataTable (hide / freeze from the header itself).
- ~~**Result grid niceties**~~ — done (2026-10-26): freeze the first columns;
  column filters as chips (server-side WHERE on tables, the fetched rows on
  results); copy rows as INSERT (into the result's table when the query reads
  one) or a Markdown table; open a foreign key's row from its cell.
  Follow-ups: filter inputs under each header (wapyt); open the rows that
  reference this one (the reverse direction); copy as CSV with options.
- ~~**Session manager**~~ — done (2026-10-26): a Sessions tab per server
  (PostgreSQL, MySQL / MariaDB, SQL Server): user, database, client,
  application, state, how long, waits, blocking sessions, the statement;
  refreshed every 5 s; Cancel query and End session. Follow-ups: locks and
  the blocking tree; per-database filter; MySQL's transaction state from
  `information_schema.INNODB_TRX`.

## Milestone 3: bigger pieces

- ~~**ER diagram**~~ — done (2026-10-26): a schema's tables or one table and
  its neighbours, laid out by foreign keys, crow's-foot lines, drag
  (remembered per schema), pan and zoom, all columns / keys / names, find,
  SVG export; tables in other schemas as ghost boxes. Follow-ups: PNG export;
  views in the diagram; edge routing around boxes (orthogonal lines); create
  a foreign key by dragging between columns; print / PDF.
- ~~**Schema compare**~~ — done (2026-10-26): two schemas on any connections
  (different engines too); tables, columns, primary keys, indexes and unique
  constraints, foreign keys and views; a script for the target in a safe
  order, per ticked difference, opened in a SQL editor (SQLite tables are
  rebuilt). Follow-ups: check constraints, routines, sequences, triggers and
  materialized views; renames (now a drop and an add); column order; SQL
  Server unique constraints (now unique indexes); comparing whole databases.
- ~~**Data compare**~~ — done (2026-10-26): two tables on any connections
  (different engines too), rows matched by the target's primary key (or the
  source's), an optional WHERE for both; inserts, updates (only the changed
  columns) and deletes as a ticked list and a script in the target's
  literals; a background job with progress and Cancel; up to 500,000 source
  rows, 5,000 listed per kind. Follow-ups: choosing key columns by hand (for
  tables without a key); comparing every table of two schemas at once;
  ignoring columns; a faster path when both tables are on one server (a SQL
  join instead of reading both); case-insensitive keys as the collation says.
- ~~**SSH tunnels**~~ — done (2026-10-26): password or private-key sign-in,
  host key pinned on first use (forgettable), a tunnel per pooled engine
  dialled before the driver connects, rebuilt when it drops. Follow-ups: jump
  hosts (a tunnel through a tunnel); TLS hostname verification through the
  tunnel (send the real host name as SNI); keyboard-interactive / 2FA
  sign-in.
- **More engines** — DuckDB and CockroachDB done (2026-10-26): every feature
  where the engine has it (DuckDB: no server sessions, routines or triggers;
  CockroachDB: no sessions listing yet), Basilisk's own SQLAlchemy dialects
  where the bundled ones do not fit SQLAlchemy 2.1, plan parsers for both,
  six-engine tests. Follow-ups: CockroachDB sessions (`SHOW CLUSTER
  SESSIONS`, string ids); DuckDB schemas other than `main`, macros as
  functions, attached databases; DuckDB 2.0 (duckdb-engine's reflection does
  not work with it yet: pinned below 2). Still to come: MariaDB-specific
  bits, Oracle (oracledb thin mode).
- **BriskDB** — deferred (2026-10-26): 0.1.0b3's PostgreSQL listener has no
  general catalog (`pg_catalog` / `information_schema`) and every statement
  needs a routing key, so SQLAlchemy cannot list or read its tables. Revisit
  when BriskDB offers catalog queries (or Basilisk grows a non-SQLAlchemy
  adapter for its embedded API).
- ~~**Install story**~~ — done (2026-10-26): `compose.yaml` (bridge, an
  optional bundled PostgreSQL) and `compose.host-network.yaml` (Linux),
  INSTALL.md (engines, connecting from a container, files, SSH, backups,
  updating, troubleshooting, what was tested), and a native per-user Windows
  installer (embeddable CPython, Inno Setup, a tray launcher) built and
  install-tested by the `windows` workflow, attached to releases on a `v*`
  tag. Follow-ups: code-signing the installer; a hand test of the installer on
  a Windows desktop and of an in-place upgrade (docs/UPGRADE_TEST.md);
  macOS (native app or Homebrew); published images (GHCR) so compose need not
  build.

## Small items

- wapyt: TabWidget ignores `TabConfig.icon`; its badge needs dark colours.
- The pager wraps onto a second toolbar row on narrow windows (the less-used
  buttons now drop their labels below 1,300 px); give it its own row below a
  width on purpose.
- Statement mode for T-SQL splits on `;`, which breaks `DECLARE` scope; a
  smarter splitter (blank lines as delimiters, as DBeaver offers) would help.
