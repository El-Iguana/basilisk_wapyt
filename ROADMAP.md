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

## Milestone 2: DBeaver parity, the everyday parts

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
- **Object actions**: create/rename/drop table (with the generated DDL shown
  first), truncate, create/drop index, add column — DBeaver's table designer
  in a modest form.
- **Import CSV** into a table (column mapping, type coercion, one transaction,
  progress through a job like Monguana's dumps).
- **Column layout memory** per table (widths, order, hidden), as Monguana's
  phase 35.
- **Result grid niceties**: frozen first column, per-column filter, copy as
  INSERT / Markdown, open a foreign key's row from a cell.
- **Session manager**: running queries per server with kill (pg_stat_activity,
  SHOW PROCESSLIST, sys.dm_exec_requests).

## Milestone 3: bigger pieces

- **ER diagram** of a schema (tables, columns, FK lines), auto-laid-out, export
  as SVG.
- **Data compare / schema compare** between two connections.
- **SSH tunnels** for connections (paramiko; IguanaXterm has the know-how).
- **More engines** through the adapter: MariaDB-specific bits, CockroachDB
  (PostgreSQL dialect), DuckDB, Oracle (oracledb thin mode), BriskDB.
- **Install story** like Monguana's: compose files, INSTALL.md, Windows
  installer.

## Small items

- wapyt: TabWidget ignores `TabConfig.icon`; its badge needs dark colours.
- The pager wraps onto a second toolbar row on narrow windows; give it its own
  row below a width.
- Statement mode for T-SQL splits on `;`, which breaks `DECLARE` scope; a
  smarter splitter (blank lines as delimiters, as DBeaver offers) would help.
