"""
Basilisk — browser UI.

Runs in Pyodide. Everything here is client-side Python; anything touching a
database, a credential or the store of profiles goes through the BFF
services in ``services/``, whose implementations are stripped from what
reaches the browser.

Conventions carried from Monguana and IguanaXterm, each learned the hard way:

* **BFF calls use the generated ``*_async`` name**, with positional
  arguments: ``await SqlService().browse_async(...)``.
* **UI is built in ``load_ui``, never ``__init__``** (wapyt's metaclass calls
  ``load_ui()`` after construction).
* **JS ``null`` arrives as ``JsNull``, which is not ``None``.** Test DOM
  lookups for truthiness; read attributes with ``getAttribute``.
* **Import names from a module** (``from services.about import VERSION``),
  never the module itself: the browser package follows only that form.
* **SQL stays text in the browser.** The server parses, quotes and runs it.
"""
from __future__ import annotations

import asyncio
import html
import json
import traceback
from urllib.parse import quote, urlencode

import js
from pyodide.ffi import create_proxy, to_js

from wapyt import (
    CellConfig,
    ColumnConfig,
    DataTable,
    DataTableConfig,
    FieldConfig,
    Form,
    FormButton,
    FormConfig,
    Layout,
    LayoutConfig,
    MainWindow,
    message,
    ModalConfig,
    ModalWindow,
    Pagination,
    PaginationConfig,
    SelectOption,
    TabConfig,
    TableAction,
    TabWidget,
    TabWidgetConfig,
    ToolbarButton,
    ToolbarConfig,
    ToolbarSeparator,
    ToolbarSpacer,
    ToolbarText,
    Tree,
    TreeAction,
    TreeConfig,
    TreeItem,
)

from services.about import LICENSE as ABOUT_LICENSE, RELEASES_URL, REPO, REPO_URL, VERSION, WIKI_URL
from services.about_service import AboutService
from services.connection_service import ConnectionService
from services.sql_service import SqlService
from services.ui_state_service import UiStateService
from services.user_service import UserService

# pytincture's MainWindow detection only knows dhxpyt; without this the app
# starts with HTTP 422.
APP_ENTRYPOINT = "Basilisk"

APP_FAVICON = "static/basilisk_avatar.webp"

EDITOR_SRC = "/vendor/codemirror/basilisk-editor.js"
EDITOR_LOAD_SECONDS = 10

PAGE_SIZES = [50, 100, 200, 500, 1000]
MAX_ROWS_CHOICES = (100, 500, 1000, 5000, 10000)
DRAFT_SAVE_MS = 1500

# Row keys that cannot collide with a column id (columns are "c0", "c1", …).
_ID = "\u0001id"
_RANK = "\u0001rank"
_ROWCLS = "\u0001row"
_CLS = "\u0001k:"      # + column id: that cell's classes

_ENV_LABELS = {"": "None", "dev": "Development", "test": "Test", "prod": "Production"}

_TOOLBAR_BUTTONS = (
    ("new_conn", "New connection", "mdi-database-plus"),
    ("sql", "SQL editor", "mdi-console-line"),
    ("refresh", "Refresh", "mdi-refresh"),
    ("|", "", ""),
    ("users", "Users", "mdi-account-group"),
    ("password", "Password", "mdi-key"),
    ("shortcuts", "Shortcuts", "mdi-keyboard-outline"),
    ("about", "About", "mdi-information-outline"),
)


def _csrf_token() -> str:
    for part in str(js.document.cookie).split(";"):
        name, _, value = part.strip().partition("=")
        if "csrf" in name.lower() and "basilisk" in name.lower():
            return str(js.decodeURIComponent(value))
    for part in str(js.document.cookie).split(";"):
        name, _, value = part.strip().partition("=")
        if "csrf" in name.lower():
            return str(js.decodeURIComponent(value))
    return ""


def _report(label: str) -> None:
    js.console.error(f"[basilisk] {label} failed:\n{traceback.format_exc()}")


def _spawn(coro, label: str):
    async def _guarded():
        try:
            await coro
        except Exception:  # noqa: BLE001 - last line of defence
            _report(label)

    return asyncio.ensure_future(_guarded())


def _attr(element, name: str) -> str:
    value = element.getAttribute(name) if element and hasattr(element, "getAttribute") else None
    return str(value) if value else ""


def _el(element_id: str):
    return js.document.getElementById(element_id)


def _esc(value) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _node_id(kind: str, *parts) -> str:
    """Deterministic tree ids, so the Tree keeps expansion across ``set_items``."""
    return ":".join([kind, *(quote(str(part), safe="") for part in parts)])


def _jsobj(value):
    return to_js(value, dict_converter=js.Object.fromEntries)


def _quote_ident(engine: str, name: str) -> str:
    if engine == "mysql":
        return "`" + name.replace("`", "``") + "`"
    if engine == "mssql":
        return "[" + name.replace("]", "]]") + "]"
    return '"' + name.replace('"', '""') + '"'


def _sql_literal(value) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _count(value) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "?"


def _column_width(header: str) -> int:
    return max(90, min(320, len(header) * 8 + 44))


def _download(url: str) -> None:
    anchor = js.document.createElement("a")
    anchor.href = url
    anchor.download = ""
    anchor.style.display = "none"
    js.document.body.appendChild(anchor)
    anchor.click()
    anchor.remove()


async def _copy(text: str) -> bool:
    try:
        await js.navigator.clipboard.writeText(text)
        return True
    except Exception:  # noqa: BLE001 - permission or an insecure context
        return False


class Basilisk(MainWindow):
    # No __init__: the LoadUICaller metaclass calls load_ui() after
    # construction, and defining both builds the UI twice.

    def load_ui(self) -> None:
        self.set_theme("dark")
        js.document.title = "Basilisk"

        self._conns: list = []
        self._engines: list = []
        # Loaded tree levels. Keys are locations: (conn, db, schema).
        self._dbs: dict = {}                 # conn -> [database rows]
        self._schemas: dict = {}             # (conn, db) -> [schema rows]
        self._objects: dict = {}             # (conn, db, schema) -> {"tables", "views"}
        self._errors: dict = {}              # tree node id -> load error
        self._loading: set = set()
        self._selected: dict = {}
        self._tabs: dict = {}                # tab id -> table or SQL tab state
        self._tab_counter = 0
        self._run_counter = 0
        self._me: dict = {}
        self._settings: dict = {"show_system": False}
        self._proxies: list = []
        self._editor_ready = None
        self._release: dict = {}

        self._build_chrome()
        _spawn(self._load_identity(), "identity load")
        _spawn(self._reload_connections(), "connection list")
        _spawn(self._load_settings(), "settings")
        _spawn(self._check_release(), "release check")
        _spawn(self._ensure_editor(), "editor bundle")

    # ------------------------------------------------------------------
    # Chrome
    # ------------------------------------------------------------------

    def _build_chrome(self) -> None:
        self.toolbar = self.add_toolbar("mainwindow_header", self._toolbar_config())
        self.toolbar.on_click(lambda payload: self._on_toolbar(
            "about" if payload["id"] == "update" else str(payload["id"])))

        body = self.add_layout("mainwindow", LayoutConfig(type="line", cols=[
            CellConfig(id="sidebar", width="300px", resizable=True, min_size="200px",
                       max_size="640px"),
            CellConfig(id="workspace", width="100%"),
        ]))
        self.body = body

        sidebar = body.get_cell("sidebar")
        sidebar_el = sidebar.getContainer() if hasattr(sidebar, "getContainer") else sidebar
        sidebar_el.innerHTML = (
            '<div class="bk-sidebar">'
            '  <div class="bk-brand">'
            '    <img class="bk-brand-logo" src="/static/basilisk_avatar.webp"'
            '         alt="" width="40" height="40" decoding="async">'
            '    <div class="bk-brand-text">'
            '      <span class="bk-brand-name">Basilisk</span>'
            '      <span class="bk-brand-sub">SQL &middot; pytincture &middot; wapyt</span>'
            "    </div>"
            "  </div>"
            '  <div class="bk-sidebar-tree" id="bk-tree-host"></div>'
            "</div>"
        )

        server = ["server"]
        location = ["server", "database", "schema"]
        objects = ["table", "view"]
        self.tree = Tree(
            TreeConfig(
                filterable=True,
                filter_placeholder="Filter",
                label="Database navigator",
                empty_text="No connections yet.\nUse New connection to add one.",
                context_actions=[
                    TreeAction("connect", "Connect / refresh", "mdi-connection", kinds=server),
                    TreeAction("sql", "New SQL editor", "mdi-console-line", kinds=location),
                    TreeAction("refresh", "Refresh", "mdi-refresh",
                               kinds=["database", "schema", "group"]),
                    TreeAction("disconnect", "Disconnect", "mdi-lan-disconnect", kinds=server),
                    TreeAction(separator=True),
                    TreeAction("edit_conn", "Edit connection…", "mdi-pencil", kinds=server),
                    TreeAction("dup_conn", "Duplicate", "mdi-content-copy", kinds=server),
                    TreeAction("delete_conn", "Delete connection", "mdi-delete",
                               kinds=server, danger=True),
                    TreeAction("toggle_system", "Show / hide system objects",
                               "mdi-eye-settings-outline", kinds=server),
                    TreeAction("open", "Open data", "mdi-table-eye", kinds=objects),
                    TreeAction("properties", "Columns and keys", "mdi-table-cog", kinds=objects),
                    TreeAction("ddl", "DDL", "mdi-code-braces", kinds=objects),
                    TreeAction("select_sql", "SELECT in a SQL editor", "mdi-console-line",
                               kinds=objects),
                    TreeAction("generate", "Generate SQL…", "mdi-script-text-outline",
                               kinds=["table"]),
                    TreeAction("export_csv", "Export CSV", "mdi-file-delimited-outline",
                               kinds=objects),
                    TreeAction("copy_name", "Copy qualified name", "mdi-content-copy",
                               kinds=objects),
                ],
            ),
            root="#bk-tree-host",
        )
        self.tree.on_select(self._on_tree_select)
        self.tree.on_activate(self._on_tree_activate)
        self.tree.on_toggle(self._on_tree_toggle)
        self.tree.on_action(self._on_tree_action)

        workspace = body.get_cell("workspace")
        workspace_el = workspace.getContainer() if hasattr(workspace, "getContainer") else workspace
        workspace_el.innerHTML = '<div class="bk-workspace" id="bk-tabs-host"></div>'
        self.tabs = TabWidget(TabWidgetConfig(tabs=[]), container=_el("bk-tabs-host"))
        self.tabs.on_close(self._on_tab_close)

        style = js.document.createElement("style")
        style.textContent = _CSS
        js.document.head.appendChild(style)

        self._listen("click", self._on_click)
        self._listen("change", self._on_change)
        self._listen("keydown", self._on_keydown)
        before = create_proxy(self._on_before_unload)
        self._proxies.append(before)
        js.window.addEventListener("beforeunload", before)

    def _listen(self, event: str, handler) -> None:
        proxy = create_proxy(handler)
        self._proxies.append(proxy)
        js.document.addEventListener(event, proxy)

    def _toolbar_config(self) -> ToolbarConfig:
        items: list = []
        for key, label, icon in _TOOLBAR_BUTTONS:
            items.append(ToolbarSeparator() if key == "|" else ToolbarButton(key, label, icon))
        items += [
            ToolbarSpacer(),
            ToolbarButton("update", icon="mdi-arrow-up-circle", variant="accent",
                          hidden=True, keep_label=True),
            ToolbarText("user"),
            ToolbarButton("logout", "Logout", "mdi-logout", tooltip="Sign out"),
        ]
        return ToolbarConfig(items=items, label="Basilisk")

    def _on_before_unload(self, event) -> None:
        if any(tab.get("pending") or (tab.get("txn") or {}).get("open")
               for tab in self._tabs.values()):
            event.preventDefault()
            event.returnValue = ""

    # ------------------------------------------------------------------
    # Delegated DOM events: one listener each, routed by data-bk and the
    # enclosing tab root's data-tab.
    # ------------------------------------------------------------------

    def _tab_of(self, element) -> str | None:
        root = element.closest("[data-tab]") if hasattr(element, "closest") else None
        tid = _attr(root, "data-tab") if root else ""
        return tid if tid in self._tabs else None

    def _on_click(self, event) -> None:
        target = event.target
        if not target or not hasattr(target, "closest"):
            return
        button = target.closest("[data-bk]")
        if not button or button.disabled:
            return
        tid = self._tab_of(button)
        if tid is None:
            return
        action = _attr(button, "data-bk")
        tab = self._tabs[tid]
        if tab["kind"] == "table":
            self._table_action(tid, action, button)
        else:
            self._sql_action(tid, action, button)

    def _on_change(self, event) -> None:
        target = event.target
        if not target or not hasattr(target, "closest"):
            return
        tid = self._tab_of(target)
        if tid is None:
            return
        role = _attr(target, "data-role")
        tab = self._tabs[tid]
        if tab["kind"] == "sql":
            if role == "database":
                _spawn(self._sql_switch_db(tid, str(target.value), target), "database")
            elif role == "max_rows":
                tab["max_rows"] = int(target.value)

    def _on_keydown(self, event) -> None:
        target = event.target
        if not target or not hasattr(target, "closest"):
            return
        key = str(event.key)
        tid = self._tab_of(target)
        if tid is not None and not event.defaultPrevented:
            tab = self._tabs[tid]
            if tab["kind"] == "table" and key == "Enter" and _attr(target, "data-role") in (
                "where", "order_by"
            ):
                event.preventDefault()
                self._apply_filter(tid)
                return
            if tab["kind"] == "sql" and _attr(target, "data-role") == "fallback":
                if key == "Enter" and (event.ctrlKey or event.metaKey):
                    event.preventDefault()
                    _spawn(self._sql_run(tid, "script" if event.shiftKey else "statement"), "run")
                    return
        if (event.ctrlKey or event.metaKey) and key == "]":
            event.preventDefault()
            self._on_toolbar("sql")
            return
        tag = str(target.tagName or "").lower()
        if tag in ("input", "textarea", "select") or target.isContentEditable:
            return
        if event.ctrlKey or event.metaKey or event.altKey:
            return
        if any(str(o.style.display) == "flex" for o in js.document.querySelectorAll(".wapyt-modal-overlay")):
            return
        if key == "?":
            self._shortcuts_dialog()

    # ------------------------------------------------------------------
    # Identity, settings, connections
    # ------------------------------------------------------------------

    async def _load_identity(self) -> None:
        self._me = await UserService().me_async()
        suffix = " · admin" if self._me.get("is_admin") else ""
        self.toolbar.set_text("user", f"{self._me.get('username', '')}{suffix}")
        if not self._me.get("is_admin"):
            self.toolbar.set_hidden("users", True)
        if self._me.get("must_change_password"):
            self._password_nag()

    async def _load_settings(self) -> None:
        result = await UiStateService().get_async("settings")
        if result.get("ok") and result.get("value"):
            self._settings.update(result["value"])

    async def _toggle_system(self, conn_id: int) -> None:
        self._settings["show_system"] = not self._settings.get("show_system")
        shown = self._settings["show_system"]
        self._toast(f"System databases and schemas are {'shown' if shown else 'hidden'}.")
        await UiStateService().set_async("settings", self._settings)
        await self._refresh_conn(conn_id)

    async def _reload_connections(self) -> None:
        self._conns = await ConnectionService().list_async()
        known = {conn["id"] for conn in self._conns}
        for conn_id in list(self._dbs) + [key[0] for key in self._objects]:
            if conn_id not in known:
                self._forget(conn_id)
        self._rebuild_tree()

    def _conn(self, conn_id) -> dict | None:
        try:
            wanted = int(conn_id)
        except (TypeError, ValueError):
            return None
        return next((conn for conn in self._conns if conn["id"] == wanted), None)

    def _forget(self, conn_id: int) -> None:
        self._dbs.pop(conn_id, None)
        for store in (self._schemas, self._objects):
            for key in [key for key in store if key[0] == conn_id]:
                store.pop(key, None)

    # ------------------------------------------------------------------
    # The tree: connection > database > schema > Tables / Views > object,
    # with the levels each engine has (conn["levels"]).
    # ------------------------------------------------------------------

    def _rebuild_tree(self) -> None:
        folders: dict = {}
        for conn in self._conns:
            folders.setdefault(conn.get("folder") or "", []).append(conn)
        items: list = []
        for folder in sorted(folders, key=str.lower):
            nodes = [self._server_item(conn) for conn in folders[folder]]
            if folder == "":
                items.extend(nodes)
            else:
                items.insert(0, TreeItem(id=_node_id("f", folder), label=folder, badge=len(nodes),
                                         icon="mdi-folder", open_icon="mdi-folder-open",
                                         items=nodes, data={"kind": "folder"}))
        self.tree.set_items(items)

    def _server_item(self, conn: dict) -> TreeItem:
        conn_id = conn["id"]
        node = _node_id("c", conn_id)
        levels = conn.get("levels") or []
        if conn.get("engine") == "sqlite":
            where = f"SQLite · {conn.get('database')}"
        else:
            where = f"{conn.get('engine_label')} · {conn.get('host')}:{conn.get('port')}"
            if conn.get("username"):
                where = f"{conn.get('username')}@{where}"
        flags = []
        if conn.get("read_only"):
            flags.append("read-only")
        if conn.get("env"):
            flags.append(_ENV_LABELS.get(conn["env"], conn["env"]).lower())
        tooltip = "\n".join(part for part in (
            conn["name"], where, ", ".join(flags), conn.get("notes") or "") if part)
        if "database" in levels:
            loaded = self._dbs.get(conn_id)
            children = None if loaded is None else (
                [self._db_item(conn, row) for row in loaded]
                or [self._note_item(node, "No databases visible")])
        else:
            children = self._object_children(conn, (conn_id, "", ""), node)
        if children is None:
            children = [self._note_item(node, self._errors.get(node) or (
                "Connecting…" if node in self._loading else "Expand to connect"))]
        badge = {"prod": "PROD", "test": "TEST", "dev": "DEV"}.get(conn.get("env") or "")
        return TreeItem(
            id=node, label=conn["name"], icon=conn.get("icon") or "mdi-database",
            tooltip=tooltip, items=children, badge=badge,
            data={"kind": "server", "conn": conn_id, "db": "", "schema": ""},
        )

    def _db_item(self, conn: dict, row: dict) -> TreeItem:
        conn_id = conn["id"]
        name = row["name"]
        node = _node_id("d", conn_id, name)
        levels = conn.get("levels") or []
        if "schema" in levels:
            loaded = self._schemas.get((conn_id, name))
            children = None if loaded is None else (
                [self._schema_item(conn, name, srow) for srow in loaded]
                or [self._note_item(node, "No schemas visible")])
        else:
            children = self._object_children(conn, (conn_id, name, ""), node)
        if children is None:
            children = [self._note_item(node, self._errors.get(node) or (
                "Loading…" if node in self._loading else "Expand to list"))]
        return TreeItem(
            id=node, label=name, icon="mdi-database-outline" if row.get("system") else "mdi-database",
            tooltip=name + (" (default)" if row.get("current") else ""),
            items=children, badge="•" if row.get("current") else None,
            data={"kind": "database", "conn": conn_id, "db": name, "schema": ""},
        )

    def _schema_item(self, conn: dict, db: str, row: dict) -> TreeItem:
        conn_id = conn["id"]
        name = row["name"]
        node = _node_id("s", conn_id, db, name)
        children = self._object_children(conn, (conn_id, db, name), node)
        if children is None:
            children = [self._note_item(node, self._errors.get(node) or (
                "Loading…" if node in self._loading else "Expand to list"))]
        return TreeItem(
            id=node, label=name, icon="mdi-folder-table-outline" if row.get("system") else "mdi-folder-table",
            tooltip=f"{db}.{name}" + (" (default)" if row.get("default") else ""),
            items=children,
            data={"kind": "schema", "conn": conn_id, "db": db, "schema": name},
        )

    def _object_children(self, conn: dict, loc: tuple, parent: str) -> list | None:
        """The Tables / Views groups of a location, or None while unloaded."""
        found = self._objects.get(loc)
        if found is None:
            return None
        conn_id, db, schema = loc
        groups = []
        for group, label, icon, rows in (
            ("tables", "Tables", "mdi-table-multiple", found.get("tables") or []),
            ("views", "Views", "mdi-eye-outline", found.get("views") or []),
        ):
            gid = _node_id("g", conn_id, db, schema, group)
            kind = "table" if group == "tables" else "view"
            items = [self._object_item(loc, row, kind) for row in rows] or [
                self._note_item(gid, f"No {label.lower()}")]
            groups.append(TreeItem(
                id=gid, label=label, icon=icon, open_icon=icon, badge=len(rows), items=items,
                data={"kind": "group", "conn": conn_id, "db": db, "schema": schema},
            ))
        return groups

    def _object_item(self, loc: tuple, row: dict, kind: str) -> TreeItem:
        conn_id, db, schema = loc
        name = row["name"]
        rows = row.get("rows")
        tip = ".".join(part for part in (db, schema, name) if part)
        if rows is not None:
            tip += f"\n≈ {_count(rows)} rows"
        icon = "mdi-table" if kind == "table" else (
            "mdi-table-eye" if row.get("materialized") else "mdi-eye-outline")
        return TreeItem(
            id=_node_id("t", conn_id, db, schema, name), label=name, icon=icon, tooltip=tip,
            data={"kind": kind, "conn": conn_id, "db": db, "schema": schema, "name": name},
        )

    @staticmethod
    def _note_item(parent: str, text: str) -> TreeItem:
        return TreeItem(id=f"{parent}:note", label=text, icon="mdi-information-outline",
                        data={"kind": "note"})

    @staticmethod
    def _payload_data(payload: dict) -> dict:
        node = payload.get("node") or {}
        return dict(node.get("data") or {})

    def _on_tree_select(self, payload: dict) -> None:
        self._selected = self._payload_data(payload)

    def _on_tree_toggle(self, payload: dict) -> None:
        if not payload.get("expanded"):
            return
        node_id = str(payload.get("id") or "")
        data = dict((self.tree.get_node(node_id) or {}).get("data") or {})
        conn = self._conn(data.get("conn"))
        if conn is None:
            return
        levels = conn.get("levels") or []
        kind = data.get("kind")
        conn_id, db, schema = conn["id"], data.get("db") or "", data.get("schema") or ""
        if kind == "server":
            if "database" in levels:
                if conn_id not in self._dbs:
                    _spawn(self._load_databases(conn_id), "databases")
            elif (conn_id, "", "") not in self._objects:
                _spawn(self._load_objects((conn_id, "", ""), node_id), "objects")
        elif kind == "database":
            if "schema" in levels:
                if (conn_id, db) not in self._schemas:
                    _spawn(self._load_schemas(conn_id, db), "schemas")
            elif (conn_id, db, "") not in self._objects:
                _spawn(self._load_objects((conn_id, db, ""), node_id), "objects")
        elif kind == "schema" and (conn_id, db, schema) not in self._objects:
            _spawn(self._load_objects((conn_id, db, schema), node_id), "objects")

    def _on_tree_activate(self, payload: dict) -> None:
        data = self._payload_data(payload)
        if data.get("kind") in ("table", "view"):
            self._open_table(self._loc(data), data["name"], data["kind"])

    @staticmethod
    def _loc(data: dict) -> tuple:
        return (int(data["conn"]), data.get("db") or "", data.get("schema") or "")

    async def _load(self, node: str, call):
        """Run a tree load with its Loading… note and error note."""
        self._loading.add(node)
        self._errors.pop(node, None)
        self._rebuild_tree()
        try:
            result = await call
        finally:
            self._loading.discard(node)
        if not result.get("ok"):
            self._errors[node] = f"⚠ {result.get('error', 'Could not load')}"
            self._toast(result.get("error", "Could not load"), kind="error")
        return result

    async def _load_databases(self, conn_id: int) -> None:
        node = _node_id("c", conn_id)
        result = await self._load(node, SqlService().databases_async(
            conn_id, bool(self._settings.get("show_system"))))
        if result.get("ok"):
            self._dbs[conn_id] = result["value"]
            conn = self._conn(conn_id)
            if conn:
                conn["open"] = True
            current = next((row["name"] for row in result["value"] if row.get("current")), None)
            self._rebuild_tree()
            if current:
                self.tree.expand(_node_id("d", conn_id, current))
                levels = (conn or {}).get("levels") or []
                if "schema" in levels and (conn_id, current) not in self._schemas:
                    await self._load_schemas(conn_id, current)
                elif "schema" not in levels and (conn_id, current, "") not in self._objects:
                    await self._load_objects((conn_id, current, ""), _node_id("d", conn_id, current))
            return
        self._dbs.pop(conn_id, None)
        self._rebuild_tree()

    async def _load_schemas(self, conn_id: int, db: str) -> None:
        node = _node_id("d", conn_id, db)
        result = await self._load(node, SqlService().schemas_async(
            conn_id, db, bool(self._settings.get("show_system"))))
        if result.get("ok"):
            self._schemas[(conn_id, db)] = result["schemas"]
            self._rebuild_tree()
            default = result.get("default")
            if default and any(row["name"] == default for row in result["schemas"]):
                self.tree.expand(_node_id("s", conn_id, db, default))
                if (conn_id, db, default) not in self._objects:
                    await self._load_objects((conn_id, db, default),
                                             _node_id("s", conn_id, db, default))
            return
        self._schemas.pop((conn_id, db), None)
        self._rebuild_tree()

    async def _load_objects(self, loc: tuple, node: str) -> None:
        conn_id, db, schema = loc
        result = await self._load(node, SqlService().objects_async(conn_id, db, schema))
        if result.get("ok"):
            self._objects[loc] = {"tables": result["tables"], "views": result["views"]}
            self._rebuild_tree()
            self.tree.expand(_node_id("g", conn_id, db, schema, "tables"))
        else:
            self._objects.pop(loc, None)
            self._rebuild_tree()

    async def _refresh_conn(self, conn_id: int) -> None:
        expanded = set(self.tree.get_expanded())
        conn = self._conn(conn_id)
        if conn is None:
            return
        self._forget(conn_id)
        self.tree.expand(_node_id("c", conn_id))
        levels = conn.get("levels") or []
        if "database" not in levels:
            await self._load_objects((conn_id, "", ""), _node_id("c", conn_id))
            return
        await self._load_databases(conn_id)
        for row in self._dbs.get(conn_id, []):
            dnode = _node_id("d", conn_id, row["name"])
            if dnode not in expanded:
                continue
            if "schema" in levels:
                if (conn_id, row["name"]) not in self._schemas:
                    await self._load_schemas(conn_id, row["name"])
                for srow in self._schemas.get((conn_id, row["name"]), []):
                    loc = (conn_id, row["name"], srow["name"])
                    snode = _node_id("s", *loc)
                    if snode in expanded and loc not in self._objects:
                        await self._load_objects(loc, snode)
            elif (conn_id, row["name"], "") not in self._objects:
                await self._load_objects((conn_id, row["name"], ""), dnode)

    async def _refresh_location(self, data: dict) -> None:
        conn_id = int(data["conn"])
        conn = self._conn(conn_id) or {}
        levels = conn.get("levels") or []
        kind = data.get("kind")
        db, schema = data.get("db") or "", data.get("schema") or ""
        if kind == "database" and "schema" in levels:
            self._schemas.pop((conn_id, db), None)
            for key in [key for key in self._objects if key[:2] == (conn_id, db)]:
                self._objects.pop(key)
            await self._load_schemas(conn_id, db)
            return
        node = _node_id("s", conn_id, db, schema) if schema else (
            _node_id("d", conn_id, db) if db else _node_id("c", conn_id))
        await self._load_objects((conn_id, db, schema), node)

    def _on_tree_action(self, payload: dict) -> None:
        data = self._payload_data(payload)
        self._selected = data
        action = str(payload.get("action") or "")
        conn_id = data.get("conn")
        if conn_id is None:
            return
        name = data.get("name")
        handlers = {
            "connect": lambda: self._refresh_conn(conn_id),
            "disconnect": lambda: self._disconnect(conn_id),
            "refresh": lambda: self._refresh_location(data),
            "edit_conn": lambda: self._connection_editor(conn_id),
            "dup_conn": lambda: self._duplicate_connection(conn_id),
            "delete_conn": lambda: self._delete_connection(conn_id),
            "toggle_system": lambda: self._toggle_system(conn_id),
            "generate": lambda: self._generate_dialog(self._loc(data), name),
            "select_sql": lambda: self._select_in_editor(self._loc(data), name),
            "copy_name": lambda: self._copy_name(self._loc(data), name),
        }
        if action == "sql":
            self._open_sql(conn_id, self._editor_db(data))
        elif action in ("open", "properties", "ddl"):
            sub = {"open": "data", "properties": "columns", "ddl": "ddl"}[action]
            self._open_table(self._loc(data), name, data.get("kind", "table"), sub)
        elif action == "export_csv":
            self._export_table(self._loc(data), name, "", "", "csv")
        elif action in handlers:
            _spawn(handlers[action](), action)

    def _editor_db(self, data: dict) -> str:
        """The database a SQL editor opened from a tree node starts on."""
        return data.get("db") or ""

    def _on_toolbar(self, action: str) -> None:
        conn_id = self._selected.get("conn")
        if action == "logout":
            _spawn(self._logout(), "logout")
        elif action == "new_conn":
            _spawn(self._connection_editor(None), "connection editor")
        elif action == "users":
            _spawn(self._admin_panel(), "admin panel")
        elif action == "password":
            self._password_dialog()
        elif action == "shortcuts":
            self._shortcuts_dialog()
        elif action == "about":
            self._about_dialog()
        elif action == "refresh":
            _spawn(self._refresh_all(), "refresh")
        elif action == "sql":
            active = self._tabs.get(self.tabs.get_active() or "")
            if active:
                self._open_sql(active["conn"], active.get("db") or "")
            elif conn_id:
                self._open_sql(conn_id, self._editor_db(self._selected))
            elif len(self._conns) == 1:
                self._open_sql(self._conns[0]["id"], "")
            else:
                self._toast("Select a connection in the navigator first.", kind="warning")

    async def _refresh_all(self) -> None:
        await self._reload_connections()
        for conn_id in sorted({*self._dbs, *[key[0] for key in self._objects]}):
            await self._refresh_conn(conn_id)

    async def _disconnect(self, conn_id: int) -> None:
        await ConnectionService().disconnect_async(conn_id)
        self._forget(conn_id)
        self.tree.collapse(_node_id("c", conn_id))
        self._rebuild_tree()

    async def _logout(self) -> None:
        """pytincture's logout is a POST that needs the CSRF header, hence fetch."""
        application = str(js.window.location.pathname).strip("/").split("/")[0]
        options = js.Object.new()
        options.method = "POST"
        options.credentials = "same-origin"
        headers = js.Object.new()
        setattr(headers, "X-CSRF-Token", _csrf_token())
        options.headers = headers
        try:
            response = await js.fetch(f"/{application}/auth/logout", options)
        except Exception:  # noqa: BLE001
            self._toast("Could not sign out.", kind="error")
            return
        if not response.ok:
            self._toast("Could not sign out.", kind="error")
            return
        js.window.location.assign(f"/{application}/login")

    async def _copy_name(self, loc: tuple, name: str) -> None:
        conn = self._conn(loc[0]) or {}
        engine = conn.get("engine") or ""
        parts = [part for part in (loc[2] or (loc[1] if engine == "mysql" else ""), name) if part]
        text = ".".join(_quote_ident(engine, part) for part in parts)
        if await _copy(text):
            self._toast(f"Copied {text}", kind="success")
        else:
            await message.alert(text, title="Qualified name")

    # ------------------------------------------------------------------
    # Connection profiles
    # ------------------------------------------------------------------

    async def _connection_editor(self, conn_id: int | None) -> None:
        existing: dict = {}
        if conn_id:
            existing = await ConnectionService().get_async(conn_id)
            if not existing:
                self._toast("Connection not found.", kind="error")
                return
        if not self._engines:
            self._engines = await ConnectionService().engines_async()
        folders = await ConnectionService().folders_async()
        specs = {spec["name"]: spec for spec in self._engines}
        engine = existing.get("engine") or "postgresql"
        options = existing.get("options") or {}

        extra_fields: list = []
        extra_ids: dict = {}
        for spec in self._engines:
            for field in spec["fields"]:
                fid = f"opt_{spec['name']}_{field['id']}"
                extra_ids.setdefault(spec["name"], []).append((fid, field["id"]))
                value = options.get(field["id"], field["default"]) if spec["name"] == engine \
                    else field["default"]
                if field["kind"] == "select":
                    extra_fields.append(FieldConfig(
                        id=fid, label=field["label"], type="select", value=value,
                        options=[SelectOption(value=v, label=l) for v, l in field["options"]],
                        help=field.get("help") or None))
                elif field["kind"] == "checkbox":
                    extra_fields.append(FieldConfig(id=fid, label=field["label"], type="toggle",
                                                    value=bool(value), span=2))
                else:
                    extra_fields.append(FieldConfig(id=fid, label=field["label"], value=value))

        title = "Edit connection" if conn_id else "New connection"
        modal = ModalWindow(ModalConfig(dispose_on_close=True, title=title, width=640, height=720))
        has_password = bool(existing.get("has_password"))
        form = Form(
            FormConfig(
                columns=2, submit_text="Save", cancel_text="Cancel",
                buttons=[FormButton("test", "Test connection", icon="mdi-lan-check")],
                fields=[
                    FieldConfig(id="engine", label="Database", type="toggle_group", span=2,
                                value=engine, options=[
                                    SelectOption(value=spec["name"], label=spec["label"],
                                                 icon=spec["icon"]) for spec in self._engines]),
                    FieldConfig(id="name", label="Name", required=True,
                                value=existing.get("name", "")),
                    FieldConfig(id="folder", label="Folder", type="combo", allow_custom=True,
                                value=existing.get("folder") or None,
                                options=[SelectOption(value=f) for f in folders],
                                placeholder="(none)"),
                    FieldConfig(id="host", label="Host", value=existing.get("host") or "127.0.0.1"),
                    FieldConfig(id="port", label="Port", type="number", min=1, max=65535,
                                value=existing.get("port") or specs[engine]["default_port"] or None),
                    FieldConfig(id="database", label="Database", span=2,
                                value=existing.get("database", "")),
                    FieldConfig(id="username", label="User", value=existing.get("username", ""),
                                autocomplete="off"),
                    FieldConfig(id="password", label="Password", type="password",
                                autocomplete="new-password",
                                placeholder="(stored — leave empty to keep)" if has_password
                                else ""),
                    *extra_fields,
                    FieldConfig(id="env", label="Connection type", type="toggle_group",
                                value=existing.get("env") or "", span=2, options=[
                                    SelectOption(value="", label="None"),
                                    SelectOption(value="dev", label="Development"),
                                    SelectOption(value="test", label="Test"),
                                    SelectOption(value="prod", label="Production",
                                                 icon="mdi-alert-octagon-outline")],
                                help="Production asks before running anything that writes."),
                    FieldConfig(id="read_only", label="Read-only", type="toggle",
                                value=bool(existing.get("read_only"))),
                    FieldConfig(id="timeout_s", label="Statement timeout (s, 0 = none)",
                                type="number", min=0, max=86400,
                                value=existing.get("timeout_s", 60)),
                    FieldConfig(id="notes", label="Notes", type="textarea", rows=2, span=2,
                                value=existing.get("notes", "")),
                    FieldConfig(id="test_result", label="Test", type="static", span=2,
                                placeholder="Not tested yet"),
                ],
            ),
            container=modal.body,
        )
        if has_password:
            form.set_properties("password", help="Type a single space to clear the stored password.")
        state = {"engine": engine}

        def _apply_engine(name: str, initial: bool = False) -> None:
            spec = specs.get(name) or specs["postgresql"]
            previous = specs.get(state["engine"])
            state["engine"] = spec["name"]
            for field_id in ("host", "port", "username", "password"):
                (form.show_field if spec["uses_server"] else form.hide_field)(field_id)
            for other, ids in extra_ids.items():
                for fid, _key in ids:
                    (form.show_field if other == spec["name"] else form.hide_field)(fid)
            if spec["name"] == "sqlite":
                form.set_properties(
                    "database", label="File", required=True,
                    placeholder="shop.db",
                    help=f"A path inside the SQLite folder on the server: {spec['root']}")
            else:
                form.set_properties(
                    "database", label="Database", required=False,
                    placeholder=spec["default_database"] or "(all databases)",
                    help="Opened first; the others are listed too." if spec["levels"]
                    else None)
            if spec["read_only_by_rollback"]:
                form.set_properties("read_only", help="SQL Server has no read-only session: every "
                                    "statement runs in a transaction that is rolled back.")
            else:
                form.set_properties("read_only", help=None)
            if not initial and previous and spec["uses_server"]:
                values = form.get_values()
                if not values.get("port") or values.get("port") == previous.get("default_port"):
                    form.set_values({"port": spec["default_port"]})

        _apply_engine(engine, initial=True)

        def _collect() -> dict:
            values = form.get_values()
            spec = specs[state["engine"]]
            opts = {key: values.get(fid) for fid, key in extra_ids.get(spec["name"], [])}
            profile = {
                "engine": spec["name"],
                "host": (values.get("host") or "").strip(),
                "port": int(values.get("port") or spec["default_port"] or 0),
                "database": (values.get("database") or "").strip(),
                "username": (values.get("username") or "").strip(),
                "options": opts,
                "read_only": bool(values.get("read_only")),
            }
            password = values.get("password") or ""
            if password == " ":
                profile["password"] = ""
            elif password:
                profile["password"] = password
            return {**profile, "name": (values.get("name") or "").strip(),
                    "folder": values.get("folder") or "", "env": values.get("env") or "",
                    "timeout_s": int(values.get("timeout_s") or 0),
                    "notes": values.get("notes") or ""}

        def _on_change(payload: dict) -> None:
            if payload.get("id") == "engine" and payload.get("value"):
                _apply_engine(str(payload["value"]))

        async def _test() -> None:
            form.set_button_loading("test", True)
            form.set_values({"test_result": "Connecting…"})
            try:
                p = _collect()
                args = [conn_id or None, p["engine"], p["host"], p["port"], p["database"],
                        p["username"]]
                if "password" in p:
                    result = await ConnectionService().test_async(
                        *args, p["password"], p["options"], p["read_only"])
                else:
                    result = await ConnectionService().test_async(
                        *args, "\x00unchanged\x00", p["options"], p["read_only"])
                if result.get("ok"):
                    form.set_values({"test_result": f"✔ Connected — server {result.get('version')}"
                                     f" in {result.get('ms')} ms"})
                else:
                    form.set_values({"test_result": f"✘ {result.get('error')}"})
            finally:
                form.set_button_loading("test", False)

        async def _save(_values: dict) -> None:
            form.set_busy(True)
            try:
                p = _collect()
                kwargs_order = [conn_id or None, p["name"], p["engine"], p["folder"], p["host"],
                                p["port"], p["database"], p["username"]]
                password = p.get("password", "\x00unchanged\x00")
                result = await ConnectionService().save_async(
                    *kwargs_order, password, p["options"], p["read_only"], p["timeout_s"],
                    p["env"], p["notes"])
                if not result.get("ok"):
                    if result.get("errors"):
                        form.set_errors(result["errors"])
                    else:
                        form.set_error(None, result.get("error", "Could not save"))
                    return
                modal.close()
                self._forget(result["id"])
                await self._reload_connections()
                self.tree.select(_node_id("c", result["id"]))
                self._selected = {"kind": "server", "conn": result["id"]}
            finally:
                form.set_busy(False)

        form.on_change(_on_change)
        form.on_click(lambda payload: _spawn(_test(), "test") if payload.get("id") == "test" else None)
        form.on_cancel(lambda _payload: modal.close())
        form.on_submit(lambda values: _spawn(_save(values), "connection save"))
        modal.show()
        form.set_focus("name")

    async def _duplicate_connection(self, conn_id: int) -> None:
        result = await ConnectionService().duplicate_async(conn_id)
        if not result.get("ok"):
            self._toast(result.get("error", "Could not duplicate"), kind="error")
            return
        await self._reload_connections()

    async def _delete_connection(self, conn_id: int) -> None:
        conn = self._conn(conn_id)
        if conn is None:
            return
        if not await message.confirm(
            f"Delete the connection “{conn['name']}”?\n\nThe database is not touched; "
            "its query history here is removed.",
            title="Delete connection", ok_text="Delete", danger=True,
        ):
            return
        result = await ConnectionService().delete_async(conn_id)
        if not result.get("ok"):
            self._toast(result.get("error", "Could not delete"), kind="error")
            return
        for tid in [tid for tid, tab in self._tabs.items() if tab["conn"] == conn_id]:
            self._close_tab(tid)
        self._selected = {}
        self._forget(conn_id)
        await self._reload_connections()

    # ------------------------------------------------------------------
    # The editor bundle
    # ------------------------------------------------------------------

    async def _ensure_editor(self) -> bool:
        """Load the CodeMirror bundle once per page; False keeps plain text boxes."""
        if self._editor_ready is not None:
            return await self._editor_ready
        loop = asyncio.get_event_loop()
        future = loop.create_future()
        self._editor_ready = future
        if getattr(js.window, "BkEditor", None):
            future.set_result(True)
            return True
        script = js.document.createElement("script")
        script.src = EDITOR_SRC

        def _done(ok: bool) -> None:
            if not future.done():
                future.set_result(ok)

        on_load = create_proxy(lambda _event: _done(bool(getattr(js.window, "BkEditor", None))))
        on_error = create_proxy(lambda _event: _done(False))
        self._proxies.extend([on_load, on_error])
        script.addEventListener("load", on_load)
        script.addEventListener("error", on_error)
        js.document.head.appendChild(script)
        timer = create_proxy(lambda: _done(False))
        self._proxies.append(timer)
        js.setTimeout(timer, EDITOR_LOAD_SECONDS * 1000)
        ok = await future
        if not ok:
            js.console.warn("[basilisk] the SQL editor bundle did not load; using plain text boxes")
        return ok

    def _mount_editor(self, host, *, value: str = "", dialect: str = "StandardSQL",
                      read_only: bool = False, callbacks: dict | None = None,
                      placeholder: str = "", label: str = "SQL editor"):
        proxies = {name: create_proxy(fn) for name, fn in (callbacks or {}).items()}
        self._proxies.extend(proxies.values())
        options = {"value": value, "dialect": dialect, "readOnly": read_only,
                   "placeholder": placeholder, "label": label, **proxies}
        return js.window.BkEditor.create(host, _jsobj(options))

    # ------------------------------------------------------------------
    # Tabs
    # ------------------------------------------------------------------

    def _new_tab(self, title: str, icon: str):
        self._tab_counter += 1
        tid = f"t{self._tab_counter}"
        self.tabs.add_tab(TabConfig(id=tid, title=title, closable=True, icon=f"mdi {icon}"))
        cell = self.tabs.get_cell(tid)
        return tid, cell.getContainer() if hasattr(cell, "getContainer") else cell

    def _on_tab_close(self, payload) -> None:
        tid = payload.get("id") if isinstance(payload, dict) else payload
        tab = self._tabs.get(str(tid))
        if tab and tab.get("pending"):
            # The tab is already gone from the widget; say what was lost.
            self._toast(f"Closed {tab['name']} with {len(tab['pending'])} unsaved row changes.",
                        kind="warning")
        self._discard_tab(str(tid))

    def _close_tab(self, tid: str) -> None:
        self._discard_tab(tid)
        self.tabs.remove_tab(tid)

    def _discard_tab(self, tid: str) -> None:
        tab = self._tabs.pop(tid, None)
        if tab is None:
            return
        if tab["kind"] == "sql" and tab.get("manual"):
            _spawn(self._end_session(tab), "end session")
        for key in ("grid", "cols_table", "idx_table", "fk_table", "con_table", "pager"):
            widget = tab.get(key)
            if widget is not None:
                try:
                    widget.destroy()
                except Exception:  # noqa: BLE001 - already torn down with its tab
                    pass
        for key in ("editor", "ddl_editor"):
            editor = tab.get(key)
            if editor is not None:
                try:
                    editor.destroy()
                except Exception:  # noqa: BLE001
                    pass
        if tab.get("draft_timer"):
            js.clearTimeout(tab["draft_timer"])

    def _status(self, tid: str, text: str = "", kind: str = "error") -> None:
        status = _el(f"{tid}-status")
        if status:
            status.textContent = text
            status.setAttribute("data-kind", kind)
            status.hidden = not text

    def _crumbs(self, conn: dict, db: str, schema: str, name: str = "", icon: str = "") -> str:
        parts = [f'<span class="mdi {_esc(conn.get("icon") or "mdi-database")}"></span>'
                 f'<span>{_esc(conn.get("name", "?"))}</span>']
        for part, part_icon in ((db, "mdi-database"), (schema, "mdi-folder-table")):
            if part:
                parts.append(f'<span class="bk-sep">›</span><span class="mdi {part_icon}"></span>'
                             f"<span>{_esc(part)}</span>")
        if name:
            parts.append(f'<span class="bk-sep">›</span><span class="mdi {icon}"></span>'
                         f"<strong>{_esc(name)}</strong>")
        badges = ""
        if conn.get("env"):
            badges += (f'<span class="bk-badge" data-env="{_esc(conn["env"])}">'
                       f'{_esc(_ENV_LABELS.get(conn["env"], conn["env"]))}</span>')
        if conn.get("read_only"):
            badges += '<span class="bk-badge" data-env="ro"><span class="mdi mdi-lock"></span> read-only</span>'
        return "".join(parts) + badges

    # ------------------------------------------------------------------
    # Table tabs: Data | Columns | Keys & indexes | DDL
    # ------------------------------------------------------------------

    def _open_table(self, loc: tuple, name: str, kind: str = "table", sub: str = "data") -> str:
        for tid, tab in self._tabs.items():
            if tab["kind"] == "table" and tab["loc"] == loc and tab["name"] == name:
                self.tabs.set_active(tid)
                self._show_sub(tid, sub)
                return tid
        conn = self._conn(loc[0]) or {}
        icon = "mdi-table" if kind == "table" else "mdi-eye-outline"
        tid, container = self._new_tab(name, icon)
        container.innerHTML = self._table_html(tid, conn, loc, name, icon)
        tab = {
            "tid": tid, "kind": "table", "conn": loc[0], "db": loc[1], "loc": loc, "name": name,
            "object_kind": kind, "engine": conn.get("engine") or "", "info": None,
            "page": 1, "page_size": 100, "total": None, "exact": False,
            "where": "", "order_by": "", "columns": [], "rows": [], "keys": [],
            "truncated": set(), "pending": {}, "new_counter": 0, "tdir": "asc",
            "sub": "data", "grid": None, "pager": None, "busy": False, "menu_col": None,
        }
        self._tabs[tid] = tab
        tab["pager"] = Pagination(PaginationConfig(page_size=100, page_sizes=PAGE_SIZES,
                                                   label="Rows"),
                                  container=_el(f"{tid}-pager"))
        tab["pager"].on_change(lambda payload: _spawn(self._on_page(tid, payload), "page"))
        self.tabs.set_active(tid)
        self._show_sub(tid, sub)
        _spawn(self._load_table(tid), "table open")
        return tid

    def _table_html(self, tid: str, conn: dict, loc: tuple, name: str, icon: str) -> str:
        engine = conn.get("engine") or ""
        quote_hint = _esc(_quote_ident(engine, "column"))
        return f"""
<div class="bk-view" data-tab="{tid}">
  <div class="bk-head">{self._crumbs(conn, loc[1], loc[2], name, icon)}
    <span class="bk-head-stats" id="{tid}-stats"></span></div>
  <div class="bk-subtabs" role="tablist">
    <button type="button" class="bk-subtab" data-bk="sub" data-sub="data" role="tab">
      <span class="mdi mdi-table"></span>Data</button>
    <button type="button" class="bk-subtab" data-bk="sub" data-sub="columns" role="tab">
      <span class="mdi mdi-table-column"></span>Columns</button>
    <button type="button" class="bk-subtab" data-bk="sub" data-sub="keys" role="tab">
      <span class="mdi mdi-key-chain"></span>Keys &amp; indexes</button>
    <button type="button" class="bk-subtab" data-bk="sub" data-sub="ddl" role="tab">
      <span class="mdi mdi-code-braces"></span>DDL</button>
  </div>
  <div class="bk-sub" data-sub="data">
    <div class="bk-filter">
      <label class="bk-flabel" for="{tid}-where">WHERE</label>
      <input class="bk-input bk-code bk-grow" data-role="where" id="{tid}-where" spellcheck="false"
        autocomplete="off" placeholder="status = 'paid' AND total &gt; 100  ·  Enter applies">
      <label class="bk-flabel" for="{tid}-order">ORDER BY</label>
      <input class="bk-input bk-code bk-order" data-role="order_by" id="{tid}-order"
        spellcheck="false" autocomplete="off" placeholder="{quote_hint} DESC">
      <button type="button" class="bk-btn bk-primary" data-bk="apply" title="Apply (Enter)">
        <span class="mdi mdi-filter"></span><span>Apply</span></button>
      <button type="button" class="bk-btn" data-bk="clear" title="Clear the filter and order">
        <span class="mdi mdi-filter-remove-outline"></span></button>
    </div>
    <div class="bk-bar">
      <button type="button" class="bk-btn" data-bk="add" data-write="1" title="Add a row">
        <span class="mdi mdi-table-row-plus-after"></span><span>Add row</span></button>
      <button type="button" class="bk-btn" data-bk="dup" data-write="1" title="Duplicate the selected rows">
        <span class="mdi mdi-content-duplicate"></span><span>Duplicate</span></button>
      <button type="button" class="bk-btn" data-bk="delete" data-write="1" title="Mark the selected rows for deletion">
        <span class="mdi mdi-table-row-remove"></span><span>Delete</span></button>
      <span class="bk-bar-sep"></span>
      <button type="button" class="bk-btn bk-save" data-bk="save" id="{tid}-save" disabled
        title="Save the changes in one transaction">
        <span class="mdi mdi-content-save"></span><span id="{tid}-save-label">Save</span></button>
      <button type="button" class="bk-btn" data-bk="revert" id="{tid}-revert" disabled
        title="Discard the unsaved changes">
        <span class="mdi mdi-undo"></span><span>Revert</span></button>
      <span class="bk-bar-sep"></span>
      <button type="button" class="bk-btn" data-bk="refresh" title="Reload this page">
        <span class="mdi mdi-refresh"></span></button>
      <button type="button" class="bk-btn" data-bk="count" title="Count every matching row">
        <span class="mdi mdi-counter"></span><span>Count</span></button>
      <button type="button" class="bk-btn" data-bk="export_csv" title="Export every matching row as CSV">
        <span class="mdi mdi-file-delimited-outline"></span><span>CSV</span></button>
      <button type="button" class="bk-btn" data-bk="export_json" title="Export every matching row as JSON">
        <span class="mdi mdi-code-json"></span><span>JSON</span></button>
      <button type="button" class="bk-btn" data-bk="to_sql" title="Open this query in a SQL editor">
        <span class="mdi mdi-console-line"></span><span>SQL</span></button>
      <span class="bk-spacer"></span>
      <span class="bk-note" id="{tid}-note"></span>
      <div class="bk-pager" id="{tid}-pager"></div>
    </div>
    <div class="bk-status" id="{tid}-status" hidden></div>
    <div class="bk-results"><div class="bk-panel" id="{tid}-grid"></div></div>
    <div class="bk-foot"><code id="{tid}-sql"></code></div>
  </div>
  <div class="bk-sub" data-sub="columns" hidden>
    <div class="bk-results"><div class="bk-panel" id="{tid}-cols"></div></div>
  </div>
  <div class="bk-sub bk-scroll" data-sub="keys" hidden>
    <h3 class="bk-h3"><span class="mdi mdi-sort-ascending"></span>Indexes</h3>
    <div class="bk-box" id="{tid}-idx"></div>
    <h3 class="bk-h3"><span class="mdi mdi-link-variant"></span>Foreign keys
      <small>double-click to open the referenced table</small></h3>
    <div class="bk-box" id="{tid}-fks"></div>
    <h3 class="bk-h3"><span class="mdi mdi-check-decagram-outline"></span>Unique and check constraints</h3>
    <div class="bk-box" id="{tid}-cons"></div>
  </div>
  <div class="bk-sub" data-sub="ddl" hidden>
    <div class="bk-bar">
      <button type="button" class="bk-btn" data-bk="copy_ddl"><span class="mdi mdi-content-copy"></span><span>Copy</span></button>
      <button type="button" class="bk-btn" data-bk="ddl_sql"><span class="mdi mdi-console-line"></span><span>Open in SQL editor</span></button>
      <span class="bk-note" id="{tid}-ddl-note"></span>
    </div>
    <div class="bk-results"><div class="bk-panel bk-code-host" id="{tid}-ddl"></div></div>
  </div>
</div>"""

    def _show_sub(self, tid: str, sub: str) -> None:
        tab = self._tabs[tid]
        tab["sub"] = sub
        root = js.document.querySelector(f'.bk-view[data-tab="{tid}"]')
        if not root:
            return
        for panel in root.querySelectorAll(".bk-sub"):
            panel.hidden = _attr(panel, "data-sub") != sub
        for button in root.querySelectorAll(".bk-subtab"):
            button.setAttribute("aria-selected", "true" if _attr(button, "data-sub") == sub else "false")
        if sub == "ddl":
            _spawn(self._render_ddl(tid), "ddl")

    async def _load_table(self, tid: str) -> None:
        tab = self._tabs[tid]
        conn_id, db, schema = tab["loc"]
        info = await SqlService().table_info_async(conn_id, db, schema, tab["name"])
        if tid not in self._tabs:
            return
        if not info.get("ok"):
            self._status(tid, info.get("error", "Could not read the table"))
            return
        tab["info"] = info
        editable = bool(info.get("editable"))
        root = js.document.querySelector(f'.bk-view[data-tab="{tid}"]')
        for button in root.querySelectorAll("[data-write]"):
            button.hidden = not editable
        for button in (_el(f"{tid}-save"), _el(f"{tid}-revert")):
            button.hidden = not editable
        note = ""
        if info.get("read_only"):
            note = "Read-only connection"
        elif info["kind"] == "view":
            note = "A view: read-only"
        elif not info.get("pk"):
            note = "No primary key: read-only"
        _el(f"{tid}-note").textContent = note
        self._render_properties(tid)
        if tab["sub"] == "ddl":
            await self._render_ddl(tid)
        await self._browse(tid, with_count=True)

    # -- browsing ---------------------------------------------------------

    async def _browse(self, tid: str, with_count: bool = False) -> None:
        tab = self._tabs.get(tid)
        if tab is None or tab["info"] is None:
            return
        tab["busy"] = True
        tab["pager"].set_busy(True)
        if tab["grid"] is not None:
            tab["grid"].set_busy(True)
        self._status(tid, "")
        conn_id, db, schema = tab["loc"]
        offset = (tab["page"] - 1) * tab["page_size"]
        try:
            result = await SqlService().browse_async(
                conn_id, db, schema, tab["name"], tab["where"], tab["order_by"], offset,
                tab["page_size"], tab["info"].get("pk") or [], with_count)
        finally:
            if tid in self._tabs:
                tab["busy"] = False
                tab["pager"].set_busy(False)
                if tab["grid"] is not None:
                    tab["grid"].set_busy(False)
        if tid not in self._tabs:
            return
        if not result.get("ok"):
            self._status(tid, result.get("error", "Query failed"))
            return
        if with_count:
            tab["total"], tab["exact"] = result.get("total"), bool(result.get("exact"))
        tab["columns"] = result["columns"]
        tab["kinds"] = result["kinds"]
        tab["rows"] = result["rows"]
        tab["keys"] = result["keys"]
        tab["truncated"] = {(r, c) for r, c in result.get("truncated") or []}
        tab["pending"] = {}
        self._render_grid(tid)
        shown = len(result["rows"])
        if tab["total"] is None:
            tab["pager"].set_total(None, has_more=shown >= tab["page_size"], shown=shown)
        else:
            tab["pager"].set_total(tab["total"], exact=tab["exact"], shown=shown)
        _el(f"{tid}-sql").textContent = f"{result['sql']}  ·  {result['ms']} ms"
        stats = _el(f"{tid}-stats")
        if stats:
            stats.textContent = (f"{_count(tab['total'])} rows" if tab["total"] is not None
                                 else "rows not counted")
        self._sync_save(tid)

    def _row_id(self, index: int) -> str:
        return f"r{index}"

    def _render_grid(self, tid: str) -> None:
        tab = self._tabs[tid]
        info = tab["info"] or {}
        meta = {col["name"]: col for col in info.get("columns") or []}
        editable = bool(info.get("editable"))
        columns = []
        for i, name in enumerate(tab["columns"]):
            col = meta.get(name) or {}
            css = []
            if col.get("pk"):
                css.append("bk-pkcol")
            if tab["kinds"][i] == "num":
                css.append("bk-num")
            columns.append(ColumnConfig(
                id=f"c{i}", header=name, width=_column_width(name),
                align="right" if tab["kinds"][i] == "num" else None,
                sort_by=_RANK, editable=editable and not col.get("computed"),
                cell_class_by=_CLS + f"c{i}", css=" ".join(css) or None,
            ))
        rows = [self._grid_row(tid, r, values) for r, values in enumerate(tab["rows"])]
        if tab["grid"] is None:
            tab["grid"] = self._make_grid(tid, columns)
        else:
            tab["grid"].set_columns(columns)
        tab["grid"].set_rows(rows)

    def _grid_row(self, tid: str, index: int, values: list, row_id: str | None = None,
                  row_cls: str = "") -> dict:
        tab = self._tabs[tid]
        rank = index if tab["tdir"] != "desc" else -index
        row = {_ID: row_id or self._row_id(index), _RANK: rank, _ROWCLS: row_cls}
        for c, value in enumerate(values):
            row[f"c{c}"] = value
            classes = []
            if value is None:
                classes.append("bk-null")
            if (index, c) in tab["truncated"] and row_id is None:
                classes.append("bk-cut")
            row[_CLS + f"c{c}"] = " ".join(classes)
        return row

    def _make_grid(self, tid: str, columns: list) -> DataTable:
        tab = self._tabs[tid]
        editable = bool((tab["info"] or {}).get("editable"))
        actions = [
            TableAction("copy", "Copy value", "mdi-content-copy"),
            TableAction("copy_row", "Copy row as JSON", "mdi-code-json"),
            TableAction("filter_eq", "Filter: column = value", "mdi-filter-outline"),
            TableAction("filter_ne", "Filter: column <> value", "mdi-filter-minus-outline"),
        ]
        if editable:
            actions += [
                TableAction(separator=True),
                TableAction("set_null", "Set NULL", "mdi-null"),
                TableAction("set_empty", "Set empty text", "mdi-format-text-variant-outline"),
                TableAction("dup", "Duplicate row", "mdi-content-duplicate"),
                TableAction("delete", "Delete row", "mdi-delete", danger=True),
            ]
        grid = DataTable(
            DataTableConfig(
                columns=columns, id_field=_ID, selection="multi",
                resizable_columns=True, reorderable_columns=True, frozen_columns=0,
                empty_text="No rows", loading_text="Loading…",
                row_class_by=_ROWCLS, context_actions=actions,
            ),
            container=_el(f"{tid}-grid"),
        )
        grid.on_edit(lambda payload: self._on_cell_edit(tid, payload))
        grid.on_action(lambda payload: _spawn(self._on_grid_action(tid, payload), "grid action"))
        grid.on_sort(lambda payload: self._on_grid_sort(tid, payload))
        # The context menu's payload has the row, not the column: remember
        # which cell was right-clicked.
        host = _el(f"{tid}-grid")

        def _remember_cell(event) -> None:
            cell = event.target.closest("td") if hasattr(event.target, "closest") else None
            tab["menu_col"] = _attr(cell, "data-column-id") or None

        proxy = create_proxy(_remember_cell)
        self._proxies.append(proxy)
        host.addEventListener("contextmenu", proxy, True)
        return grid

    def _on_grid_sort(self, tid: str, payload: dict) -> None:
        tab = self._tabs.get(tid)
        if tab is None:
            return
        column = str(payload.get("column") or payload.get("id") or payload.get("sortBy") or "")
        direction = str(payload.get("direction") or payload.get("dir") or "asc")
        tab["tdir"] = direction
        if not column.startswith("c"):
            return
        try:
            name = tab["columns"][int(column[1:])]
        except (ValueError, IndexError):
            return
        order = f"{_quote_ident(tab['engine'], name)} {'DESC' if direction == 'desc' else 'ASC'}"
        _el(f"{tid}-order").value = order
        _spawn(self._apply_filter_async(tid), "sort")

    def _apply_filter(self, tid: str) -> None:
        _spawn(self._apply_filter_async(tid), "filter")

    async def _apply_filter_async(self, tid: str) -> None:
        tab = self._tabs[tid]
        if not await self._discard_ok(tid):
            return
        tab["where"] = str(_el(f"{tid}-where").value or "").strip()
        tab["order_by"] = str(_el(f"{tid}-order").value or "").strip()
        tab["page"] = 1
        tab["pager"].set_page(1)
        await self._browse(tid, with_count=True)

    async def _on_page(self, tid: str, payload: dict) -> None:
        tab = self._tabs.get(tid)
        if tab is None:
            return
        if not await self._discard_ok(tid):
            tab["pager"].set_page(tab["page"])
            tab["pager"].set_page_size(tab["page_size"])
            return
        size_changed = int(payload.get("page_size") or tab["page_size"]) != tab["page_size"]
        tab["page"] = int(payload.get("page") or 1)
        tab["page_size"] = int(payload.get("page_size") or tab["page_size"])
        await self._browse(tid, with_count=size_changed and tab["total"] is None)

    async def _discard_ok(self, tid: str) -> bool:
        pending = self._tabs[tid].get("pending") or {}
        if not pending:
            return True
        return await message.confirm(
            f"Discard {len(pending)} unsaved row change{'s' if len(pending) != 1 else ''}?",
            title="Unsaved changes", ok_text="Discard", danger=True)

    def _table_action(self, tid: str, action: str, button) -> None:
        tab = self._tabs[tid]
        if action == "sub":
            self._show_sub(tid, _attr(button, "data-sub"))
        elif action == "apply":
            self._apply_filter(tid)
        elif action == "clear":
            _el(f"{tid}-where").value = ""
            _el(f"{tid}-order").value = ""
            self._apply_filter(tid)
        elif action == "refresh":
            _spawn(self._refresh_table(tid), "refresh")
        elif action == "count":
            _spawn(self._count_rows(tid), "count")
        elif action == "add":
            self._add_row(tid)
        elif action == "dup":
            self._duplicate_rows(tid, tab["grid"].get_selected_ids() if tab["grid"] else [])
        elif action == "delete":
            self._delete_rows(tid, tab["grid"].get_selected_ids() if tab["grid"] else [])
        elif action == "save":
            _spawn(self._save_table(tid), "save")
        elif action == "revert":
            self._revert(tid)
        elif action in ("export_csv", "export_json"):
            self._export_table(tab["loc"], tab["name"], tab["where"], tab["order_by"],
                               "csv" if action == "export_csv" else "json")
        elif action == "to_sql":
            self._table_to_sql(tid)
        elif action == "copy_ddl":
            _spawn(self._copy_ddl(tid), "copy ddl")
        elif action == "ddl_sql":
            info = tab.get("info") or {}
            self._open_sql(tab["conn"], tab["db"], info.get("ddl") or "")

    async def _refresh_table(self, tid: str) -> None:
        if await self._discard_ok(tid):
            await self._browse(tid, with_count=True)

    async def _count_rows(self, tid: str) -> None:
        tab = self._tabs[tid]
        conn_id, db, schema = tab["loc"]
        result = await SqlService().count_async(conn_id, db, schema, tab["name"], tab["where"])
        if not result.get("ok"):
            self._toast(result.get("error", "Could not count"), kind="error")
            return
        tab["total"], tab["exact"] = result["total"], True
        tab["pager"].set_total(tab["total"], exact=True, shown=len(tab["rows"]))
        _el(f"{tid}-stats").textContent = f"{_count(tab['total'])} rows"

    def _table_to_sql(self, tid: str) -> None:
        tab = self._tabs[tid]
        info = tab.get("info") or {}
        text = f"SELECT *\nFROM {info.get('qualified') or tab['name']}"
        if tab["where"]:
            text += f"\nWHERE {tab['where']}"
        if tab["order_by"]:
            text += f"\nORDER BY {tab['order_by']}"
        self._open_sql(tab["conn"], tab["db"], text + ";\n")

    def _export_table(self, loc: tuple, name: str, where: str, order_by: str, fmt: str) -> None:
        conn_id, db, schema = loc
        query = urlencode({"table": name, "database": db, "schema": schema, "where": where,
                           "order_by": order_by, "format": fmt})
        _download(f"/bk/export/{conn_id}?{query}")
        self._toast(f"Exporting {name} as {fmt.upper()}…")

    # -- editing: changes stay pending until Save --------------------------

    def _index_of(self, row_id: str) -> int | None:
        return int(row_id[1:]) if row_id.startswith("r") and row_id[1:].isdigit() else None

    def _cell_class(self, tid: str, row_id: str, column: str, classes: str) -> None:
        tab = self._tabs[tid]
        js_row = tab["grid"].datatable.getRow(row_id)
        if js_row:
            # The class key is not a column, so set it on the live row and
            # redraw the cell through set_cell (which re-reads cell_class_by).
            setattr(js_row, _CLS + column, classes)
            value = (tab["grid"].get_row(row_id) or {}).get(column)
            tab["grid"].set_cell(row_id, column, value)

    def _row_class(self, tid: str, row_id: str, classes: str) -> None:
        tab = self._tabs[tid]
        js_row = tab["grid"].datatable.getRow(row_id)
        if js_row:
            setattr(js_row, _ROWCLS, classes)
            tr = js.document.querySelector(f'#{tid}-grid tr[data-row-id="{row_id}"]')
            if tr:
                for token in ("bk-new", "bk-deleted", "bk-changed"):
                    tr.classList.remove(token)
                for token in classes.split():
                    tr.classList.add(token)

    def _pending_for(self, tid: str, row_id: str) -> dict:
        tab = self._tabs[tid]
        pending = tab["pending"].get(row_id)
        if pending is None:
            index = self._index_of(row_id)
            pk = (tab["info"] or {}).get("pk") or []
            key = dict(zip(pk, tab["keys"][index])) if index is not None and tab["keys"] else {}
            pending = {"op": "update", "key": key, "values": {}, "orig": {}}
            tab["pending"][row_id] = pending
        return pending

    def _on_cell_edit(self, tid: str, payload: dict) -> None:
        tab = self._tabs.get(tid)
        if tab is None:
            return
        row_id = str(payload.get("id"))
        column = str(payload.get("column"))
        value = payload.get("value")
        old = payload.get("old_value")
        index = self._index_of(row_id)
        c = int(column[1:])
        if index is not None and (index, c) in tab["truncated"]:
            tab["grid"].set_cell(row_id, column, old)
            tab["grid"].set_cell_error(row_id, column, "Too large to edit in the grid; use a SQL editor")
            return
        pending = self._pending_for(tid, row_id)
        if pending["op"] == "delete":
            tab["grid"].set_cell(row_id, column, old)
            return
        name = tab["columns"][c]
        if name not in pending["orig"]:
            pending["orig"][name] = old
        original = pending["orig"][name]
        if original is None and value == "" and pending["op"] == "update":
            value = None  # opening a NULL cell and leaving it empty keeps it NULL
            tab["grid"].set_cell(row_id, column, None)
        self._set_pending_value(tid, row_id, column, value)

    def _set_pending_value(self, tid: str, row_id: str, column: str, value) -> None:
        tab = self._tabs[tid]
        pending = self._pending_for(tid, row_id)
        name = tab["columns"][int(column[1:])]
        original = pending["orig"].get(name)
        classes = []
        if pending["op"] == "insert" or value != original:
            pending["values"][name] = value
            classes.append("bk-dirty")
        else:
            pending["values"].pop(name, None)
        if value is None:
            classes.append("bk-null")
        self._cell_class(tid, row_id, column, " ".join(classes))
        if pending["op"] == "update" and not pending["values"]:
            tab["pending"].pop(row_id, None)
            self._row_class(tid, row_id, "")
        elif pending["op"] == "update":
            self._row_class(tid, row_id, "bk-changed")
        self._sync_save(tid)

    def _sync_save(self, tid: str) -> None:
        tab = self._tabs[tid]
        count = len(tab["pending"])
        save, revert, label = _el(f"{tid}-save"), _el(f"{tid}-revert"), _el(f"{tid}-save-label")
        if save:
            save.disabled = count == 0
            revert.disabled = count == 0
            label.textContent = f"Save ({count})" if count else "Save"
            save.classList.toggle("bk-primary", count > 0)
        self.tabs.set_badge(tid, count or None)

    def _add_row(self, tid: str, values: list | None = None, focus: bool = True) -> str:
        tab = self._tabs[tid]
        tab["new_counter"] += 1
        row_id = f"n{tab['new_counter']}"
        width = len(tab["columns"])
        values = values or [None] * width
        row = self._grid_row(tid, -tab["new_counter"], values, row_id=row_id, row_cls="bk-new")
        row[_RANK] = -1_000_000 - tab["new_counter"]
        pending = {"op": "insert", "key": {}, "values": {}, "orig": {}}
        for c, value in enumerate(values):
            if value is not None:
                pending["values"][tab["columns"][c]] = value
                row[_CLS + f"c{c}"] = "bk-dirty"
        tab["pending"][row_id] = pending
        rows = [row] + tab["grid"].get_rows()
        tab["grid"].set_rows(rows)
        self._sync_save(tid)
        if focus:
            meta = {col["name"]: col for col in (tab["info"] or {}).get("columns") or []}
            first = next((c for c, name in enumerate(tab["columns"])
                          if not (meta.get(name) or {}).get("autoincrement")), 0)
            tab["grid"].edit_cell(row_id, f"c{first}")
        return row_id

    def _current_values(self, tid: str, row_id: str) -> list:
        tab = self._tabs[tid]
        row = tab["grid"].get_row(row_id) or {}
        return [row.get(f"c{c}") for c in range(len(tab["columns"]))]

    def _duplicate_rows(self, tid: str, ids: list) -> None:
        tab = self._tabs[tid]
        if not ids:
            self._toast("Select the rows to duplicate.", kind="warning")
            return
        meta = {col["name"]: col for col in (tab["info"] or {}).get("columns") or []}
        for row_id in ids:
            index = self._index_of(row_id)
            if index is not None and any((index, c) in tab["truncated"]
                                         for c in range(len(tab["columns"]))):
                self._toast("A row with a value too large for the grid cannot be duplicated here.",
                            kind="warning")
                continue
            values = self._current_values(tid, row_id)
            for c, name in enumerate(tab["columns"]):
                if (meta.get(name) or {}).get("autoincrement") or (meta.get(name) or {}).get("computed"):
                    values[c] = None
            self._add_row(tid, values, focus=False)

    def _delete_rows(self, tid: str, ids: list) -> None:
        tab = self._tabs[tid]
        if not ids:
            self._toast("Select the rows to delete.", kind="warning")
            return
        drop_new = []
        for row_id in ids:
            pending = tab["pending"].get(row_id)
            if row_id.startswith("n"):
                drop_new.append(row_id)
                tab["pending"].pop(row_id, None)
                continue
            if pending and pending["op"] == "delete":
                # A second Delete undoes the mark.
                tab["pending"].pop(row_id, None)
                self._row_class(tid, row_id, "")
                continue
            pending = self._pending_for(tid, row_id)
            pending.update(op="delete", values={})
            self._row_class(tid, row_id, "bk-deleted")
        if drop_new:
            tab["grid"].set_rows([row for row in tab["grid"].get_rows() if row[_ID] not in drop_new])
        self._sync_save(tid)

    def _revert(self, tid: str) -> None:
        tab = self._tabs[tid]
        tab["pending"] = {}
        self._render_grid(tid)
        self._sync_save(tid)

    async def _on_grid_action(self, tid: str, payload: dict) -> None:
        tab = self._tabs.get(tid)
        if tab is None:
            return
        action = payload.get("action")
        row_id = str(payload.get("id") or "")
        row = payload.get("row") or {}
        column = tab.get("menu_col") or "c0"
        try:
            c = int(column[1:])
            name = tab["columns"][c]
        except (ValueError, IndexError):
            return
        value = row.get(column)
        if action == "copy":
            text = "NULL" if value is None else str(value)
            if await _copy(text):
                self._toast("Copied.", kind="success")
        elif action == "copy_row":
            data = {tab["columns"][i]: row.get(f"c{i}") for i in range(len(tab["columns"]))}
            if await _copy(json.dumps(data, ensure_ascii=False, indent=2)):
                self._toast("Copied the row as JSON.", kind="success")
        elif action in ("filter_eq", "filter_ne"):
            ident = _quote_ident(tab["engine"], name)
            if value is None:
                clause = f"{ident} IS {'NOT ' if action == 'filter_ne' else ''}NULL"
            else:
                clause = f"{ident} {'=' if action == 'filter_eq' else '<>'} {_sql_literal(value)}"
            where = _el(f"{tid}-where")
            current = str(where.value or "").strip()
            where.value = f"({current}) AND {clause}" if current else clause
            await self._apply_filter_async(tid)
        elif action == "set_null":
            self._set_cell(tid, row_id, column, None)
        elif action == "set_empty":
            self._set_cell(tid, row_id, column, "")
        elif action == "dup":
            self._duplicate_rows(tid, [row_id])
        elif action == "delete":
            selected = [str(item) for item in (payload.get("selected") or [])]
            self._delete_rows(tid, selected if row_id in selected else [row_id])

    def _set_cell(self, tid: str, row_id: str, column: str, value) -> None:
        tab = self._tabs[tid]
        index = self._index_of(row_id)
        if index is not None and (index, int(column[1:])) in tab["truncated"]:
            self._toast("Too large to edit in the grid; use a SQL editor.", kind="warning")
            return
        pending = self._pending_for(tid, row_id)
        if pending["op"] == "delete":
            return
        name = tab["columns"][int(column[1:])]
        row = tab["grid"].get_row(row_id) or {}
        if name not in pending["orig"]:
            pending["orig"][name] = row.get(column)
        tab["grid"].set_cell(row_id, column, value)
        self._set_pending_value(tid, row_id, column, value)

    def _changes(self, tid: str) -> list:
        tab = self._tabs[tid]
        changes = []
        for row_id, pending in tab["pending"].items():
            change = {"row": row_id, "op": pending["op"]}
            if pending["op"] != "insert":
                change["key"] = pending["key"]
            if pending["op"] != "delete":
                change["values"] = dict(pending["values"])
            changes.append(change)
        return changes

    async def _save_table(self, tid: str) -> None:
        tab = self._tabs[tid]
        changes = self._changes(tid)
        if not changes:
            return
        conn_id, db, schema = tab["loc"]
        preview = await SqlService().save_changes_async(conn_id, db, schema, tab["name"],
                                                        changes, True)
        if not preview.get("ok"):
            self._show_cell_errors(tid, preview)
            return
        conn = self._conn(conn_id) or {}
        if not await self._confirm_sql(
            "Save changes", preview["sql"],
            f"{preview['count']} statement{'s' if preview['count'] != 1 else ''}, run in one "
            "transaction: if any row cannot be saved, none are.",
            prod=conn.get("env") == "prod", ok_text="Save",
        ):
            return
        result = await SqlService().save_changes_async(conn_id, db, schema, tab["name"],
                                                       changes, False)
        if not result.get("ok"):
            self._show_cell_errors(tid, result)
            return
        self._toast(f"Saved {result['count']} change{'s' if result['count'] != 1 else ''}.",
                    kind="success")
        tab["pending"] = {}
        await self._browse(tid, with_count=True)

    def _show_cell_errors(self, tid: str, result: dict) -> None:
        tab = self._tabs[tid]
        errors = result.get("errors") or []
        if not errors:
            self._status(tid, result.get("error", "Could not save"))
            return
        for error in errors:
            try:
                c = tab["columns"].index(error["column"])
            except ValueError:
                continue
            tab["grid"].set_cell_error(str(error["row"]), f"c{c}", error["error"])
        self._status(tid, f"{len(errors)} value{'s' if len(errors) != 1 else ''} could not be "
                     "used; see the marked cells.")

    async def _confirm_sql(self, title: str, statements: list, note: str, *, prod: bool = False,
                           ok_text: str = "Run") -> bool:
        """Show SQL before it runs; resolves to the person's answer."""
        loop = asyncio.get_event_loop()
        answer = loop.create_future()
        modal = ModalWindow(ModalConfig(dispose_on_close=True, title=title, width=760, height=520))
        shown = statements[:200]
        more = f"\n-- … and {len(statements) - 200} more" if len(statements) > 200 else ""
        warn = ('<div class="bk-warn"><span class="mdi mdi-alert-octagon-outline"></span> '
                "This is a <b>production</b> connection.</div>") if prod else ""
        modal.body.innerHTML = (
            '<div class="bk-dialog">' + warn +
            f'<div class="bk-hint">{_esc(note)}</div>'
            f'<pre class="bk-pre">{_esc(chr(10).join(shown) + more)}</pre>'
            '<div class="bk-actions">'
            '<button type="button" class="bk-btn" data-answer="no">Cancel</button>'
            f'<button type="button" class="bk-btn {"bk-danger" if prod else "bk-primary"}" '
            f'data-answer="yes">{_esc(ok_text)}</button></div></div>')

        def _on_click(event) -> None:
            which = event.target.closest("[data-answer]")
            if which and not answer.done():
                answer.set_result(_attr(which, "data-answer") == "yes")
                modal.close()

        def _on_close(_payload) -> None:
            if not answer.done():
                answer.set_result(False)

        proxy = create_proxy(_on_click)
        self._proxies.append(proxy)
        modal.body.addEventListener("click", proxy)
        modal.on_close(_on_close)
        modal.show()
        focus = modal.body.querySelector('[data-answer="no"]' if prod else '[data-answer="yes"]')
        if focus:
            focus.focus()
        return await answer

    # -- properties and DDL ----------------------------------------------

    def _render_properties(self, tid: str) -> None:
        tab = self._tabs[tid]
        info = tab["info"]
        cols = DataTable(DataTableConfig(
            columns=[
                ColumnConfig(id="pos", header="#", width=48, align="right"),
                ColumnConfig(id="name", header="Name", width=200, icon_by="icon"),
                ColumnConfig(id="type", header="Type", width=170),
                ColumnConfig(id="null", header="Null", width=70),
                ColumnConfig(id="default", header="Default", width=200),
                ColumnConfig(id="extra", header="Extra", width=140),
                ColumnConfig(id="comment", header="Comment", width=260),
            ],
            rows=[{
                "id": col["name"], "pos": i + 1, "name": col["name"], "type": col["type"],
                "null": "yes" if col["nullable"] else "NOT NULL",
                "default": col["default"] or "",
                "extra": ", ".join(part for part, on in (
                    ("auto increment", col["autoincrement"]), ("computed", col["computed"])) if on),
                "comment": col["comment"] or "",
                "icon": "mdi-key" if col["pk"] else ("mdi-link-variant" if any(
                    col["name"] in fk["columns"] for fk in info["foreign_keys"]) else ""),
                "icon_title": "Primary key" if col["pk"] else "Foreign key",
            } for i, col in enumerate(info["columns"])],
            selection="single", resizable_columns=True, empty_text="No columns",
        ), container=_el(f"{tid}-cols"))
        tab["cols_table"] = cols

        tab["idx_table"] = DataTable(DataTableConfig(
            columns=[ColumnConfig(id="name", header="Name", width=240),
                     ColumnConfig(id="columns", header="Columns", width=320),
                     ColumnConfig(id="kind", header="Kind", width=120)],
            rows=([{"id": "pk", "name": "(primary key)", "columns": ", ".join(info["pk"]),
                    "kind": "PRIMARY KEY"}] if info["pk"] else []) + [
                {"id": f"i{i}", "name": ix["name"] or "", "columns": ", ".join(ix["columns"]),
                 "kind": "UNIQUE" if ix["unique"] else "INDEX"}
                for i, ix in enumerate(info["indexes"])],
            selection="none", empty_text="No indexes"), container=_el(f"{tid}-idx"))

        tab["fk_table"] = DataTable(DataTableConfig(
            columns=[ColumnConfig(id="name", header="Name", width=220),
                     ColumnConfig(id="columns", header="Columns", width=200),
                     ColumnConfig(id="ref", header="References", width=320),
                     ColumnConfig(id="rules", header="On delete / update", width=180)],
            rows=[{"id": f"f{i}", "name": fk["name"] or "", "columns": ", ".join(fk["columns"]),
                   "ref": f"{(fk['ref_schema'] + '.') if fk['ref_schema'] else ''}"
                          f"{fk['ref_table']} ({', '.join(fk['ref_columns'])})",
                   "rules": " / ".join(rule or "-" for rule in (fk["on_delete"], fk["on_update"])),
                   "table": fk["ref_table"], "schema": fk["ref_schema"] or ""}
                  for i, fk in enumerate(info["foreign_keys"])],
            selection="single", empty_text="No foreign keys"), container=_el(f"{tid}-fks"))
        tab["fk_table"].on_activate(lambda payload: self._open_reference(tid, payload))

        tab["con_table"] = DataTable(DataTableConfig(
            columns=[ColumnConfig(id="name", header="Name", width=240),
                     ColumnConfig(id="kind", header="Kind", width=100),
                     ColumnConfig(id="definition", header="Definition", width=420)],
            rows=[{"id": f"u{i}", "name": u["name"] or "", "kind": "UNIQUE",
                   "definition": ", ".join(u["columns"])} for i, u in enumerate(info["uniques"])]
            + [{"id": f"k{i}", "name": c["name"] or "", "kind": "CHECK",
                "definition": c["sql"] or ""} for i, c in enumerate(info["checks"])],
            selection="none", empty_text="None"), container=_el(f"{tid}-cons"))

    def _open_reference(self, tid: str, payload: dict) -> None:
        tab = self._tabs[tid]
        row = payload.get("row") or {}
        conn = self._conn(tab["conn"]) or {}
        levels = conn.get("levels") or []
        conn_id, db, schema = tab["loc"]
        ref_schema = row.get("schema") or ""
        if conn.get("engine") == "mysql":
            loc = (conn_id, ref_schema or db, "")
        elif "schema" in levels:
            loc = (conn_id, db, ref_schema or schema)
        else:
            loc = (conn_id, db, schema)
        self._open_table(loc, row.get("table"), "table")

    async def _render_ddl(self, tid: str) -> None:
        tab = self._tabs.get(tid)
        if tab is None or tab["info"] is None or tab.get("ddl_shown"):
            return
        tab["ddl_shown"] = True
        info = tab["info"]
        _el(f"{tid}-ddl-note").textContent = (
            "From the server" if info.get("ddl_native")
            else "Generated from the catalogue by SQLAlchemy; the server's own may differ in detail")
        host = _el(f"{tid}-ddl")
        conn = self._conn(tab["conn"]) or {}
        if await self._ensure_editor():
            tab["ddl_editor"] = self._mount_editor(host, value=info["ddl"], read_only=True,
                                                   dialect=conn.get("dialect") or "StandardSQL",
                                                   label="DDL")
        else:
            host.innerHTML = f'<pre class="bk-pre bk-fill">{_esc(info["ddl"])}</pre>'

    async def _copy_ddl(self, tid: str) -> None:
        info = self._tabs[tid].get("info") or {}
        if await _copy(info.get("ddl") or ""):
            self._toast("Copied the DDL.", kind="success")

    # ------------------------------------------------------------------
    # SQL editor tabs
    # ------------------------------------------------------------------

    def _open_sql(self, conn_id: int, db: str = "", text: str = "") -> str:
        conn = self._conn(conn_id)
        if conn is None:
            self._toast("Connection not found.", kind="error")
            return ""
        number = 1 + sum(1 for tab in self._tabs.values()
                         if tab["kind"] == "sql" and tab["conn"] == conn_id)
        title = f"{conn['name']}" + (f" ({number})" if number > 1 else "")
        tid, container = self._new_tab(title, "mdi-console-line")
        default_db = db or conn.get("database") or conn.get("default_database") or ""
        tab = {
            "tid": tid, "kind": "sql", "conn": conn_id, "db": default_db,
            "name": title, "engine": conn.get("engine") or "", "editor": None,
            "results": [], "shown": None, "run_id": None, "max_rows": 500,
            "grid": None, "fallback": None, "draft_timer": None, "restore_draft": not text,
            # Manual commit mode: the tab's server session (sessions.py) and
            # the last transaction state it reported.
            "manual": bool(self._settings.get("manual_commit")) and not conn.get("read_only"),
            "sid": f"{tid}-{int(js.Date.now())}", "txn": None,
        }
        self._tabs[tid] = tab
        container.innerHTML = self._sql_html(tid, conn)
        layout = Layout(LayoutConfig(css="bk-split-layout", rows=[
            CellConfig(id=f"{tid}-top", height="45%", resizable=True, min_size="80px"),
            CellConfig(id=f"{tid}-bottom", height="100%", min_size="80px"),
        ]))
        _el(f"{tid}-split").appendChild(layout.layout.getRootElement())
        tab["layout"] = layout
        top = layout.get_cell(f"{tid}-top").getContainer()
        bottom = layout.get_cell(f"{tid}-bottom").getContainer()
        top.innerHTML = f'<div class="bk-editor-host" id="{tid}-editor"></div>'
        bottom.innerHTML = self._results_html(tid)
        self.tabs.set_active(tid)
        self._sql_header(tid)
        self._sql_mode_ui(tid)
        _spawn(self._sql_setup(tid, text), "sql editor")
        return tid

    def _sql_html(self, tid: str, conn: dict) -> str:
        levels = conn.get("levels") or []
        rows = "".join(f'<option value="{n}"{" selected" if n == 500 else ""}>{n:,} rows</option>'
                       for n in MAX_ROWS_CHOICES)
        db_select = (
            '<label class="bk-flabel">Database</label>'
            f'<select class="bk-select" data-role="database" id="{tid}-db">'
            f'<option value="{_esc(self._tabs[tid]["db"])}">{_esc(self._tabs[tid]["db"] or "(default)")}</option>'
            "</select>") if "database" in levels else ""
        return f"""
<div class="bk-view bk-sqltab" data-tab="{tid}">
  <div class="bk-head" id="{tid}-head"></div>
  <div class="bk-bar">
    <button type="button" class="bk-btn bk-primary" data-bk="run" id="{tid}-run"
      title="Run the statement at the cursor, or the selection (Ctrl+Enter)">
      <span class="mdi mdi-play"></span><span>Run</span></button>
    <button type="button" class="bk-btn" data-bk="run_script" id="{tid}-script"
      title="Run the whole script (Ctrl+Shift+Enter or Alt+X)">
      <span class="mdi mdi-play-box-multiple-outline"></span><span>Script</span></button>
    <button type="button" class="bk-btn bk-danger" data-bk="cancel" id="{tid}-cancel" hidden
      title="Cancel the running statement">
      <span class="mdi mdi-stop"></span><span>Cancel</span></button>
    <span class="bk-bar-sep"></span>
    {db_select}
    <select class="bk-select" data-role="max_rows" title="Rows fetched per result">{rows}</select>
    <span class="bk-bar-sep"></span>
    {self._txn_controls(tid, conn)}
    <span class="bk-bar-sep"></span>
    <button type="button" class="bk-btn" data-bk="format" title="Format (Ctrl+Shift+F)">
      <span class="mdi mdi-format-align-left"></span><span>Format</span></button>
    <button type="button" class="bk-btn" data-bk="history" title="Statements run before">
      <span class="mdi mdi-history"></span><span>History</span></button>
    <span class="bk-spacer"></span>
    <span class="bk-note" id="{tid}-cursor"></span>
  </div>
  <div class="bk-split" id="{tid}-split"></div>
</div>"""

    def _txn_controls(self, tid: str, conn: dict) -> str:
        if conn.get("read_only"):
            return ('<span class="bk-note" title="Every transaction on a read-only connection '
                    'is rolled back"><span class="mdi mdi-lock"></span> read-only</span>')
        return f"""<span class="bk-seg" role="group" aria-label="Commit mode">
      <button type="button" class="bk-seg-btn" data-bk="mode" data-mode="auto"
        title="Each statement is committed as it runs">Auto-commit</button>
      <button type="button" class="bk-seg-btn" data-bk="mode" data-mode="manual"
        title="Statements run in a transaction you commit or roll back">Manual</button></span>
    <button type="button" class="bk-btn bk-commit" data-bk="commit" id="{tid}-commit" hidden
      title="Commit the open transaction"><span class="mdi mdi-check-all"></span><span>Commit</span></button>
    <button type="button" class="bk-btn" data-bk="rollback" id="{tid}-rollback" hidden
      title="Roll back the open transaction"><span class="mdi mdi-undo-variant"></span><span>Rollback</span></button>
    <span class="bk-txn" id="{tid}-txn" hidden></span>"""

    def _results_html(self, tid: str) -> str:
        return f"""
<div class="bk-res" data-tab="{tid}">
  <div class="bk-res-bar">
    <div class="bk-res-tabs" id="{tid}-restabs" role="tablist"></div>
    <span class="bk-spacer"></span>
    <span class="bk-note" id="{tid}-summary"></span>
    <button type="button" class="bk-btn" data-bk="res_csv" id="{tid}-res-csv" hidden
      title="Save the rows shown as CSV"><span class="mdi mdi-file-delimited-outline"></span><span>CSV</span></button>
    <button type="button" class="bk-btn" data-bk="res_export" id="{tid}-res-export" hidden
      title="Run this statement again and export every row"><span class="mdi mdi-database-export-outline"></span><span>Export all</span></button>
  </div>
  <div class="bk-status" id="{tid}-status" hidden></div>
  <div class="bk-results">
    <div class="bk-panel" id="{tid}-grid"></div>
    <pre class="bk-panel bk-msgs" id="{tid}-msgs" hidden></pre>
    <div class="bk-panel bk-empty" id="{tid}-empty">Run a statement (Ctrl+Enter) or the whole
      script (Ctrl+Shift+Enter). Ctrl+Space completes table and column names.</div>
  </div>
</div>"""

    def _sql_header(self, tid: str) -> None:
        tab = self._tabs[tid]
        conn = self._conn(tab["conn"]) or {}
        head = _el(f"{tid}-head")
        if head:
            head.innerHTML = self._crumbs(conn, tab["db"], "", "", "")

    async def _sql_setup(self, tid: str, text: str) -> None:
        tab = self._tabs[tid]
        conn = self._conn(tab["conn"]) or {}
        if tab["restore_draft"]:
            draft = await UiStateService().get_async(f"draft:{tab['conn']}")
            value = (draft.get("value") or {}) if draft.get("ok") else {}
            others = [t for t in self._tabs.values() if t["kind"] == "sql"
                      and t["conn"] == tab["conn"] and t["tid"] != tid]
            if value.get("sql") and not others:
                text = value["sql"]
                if value.get("db") and "database" in (conn.get("levels") or []):
                    tab["db"] = value["db"]
                    self._sql_header(tid)
        host = _el(f"{tid}-editor")
        if host is None or tid not in self._tabs:
            return
        if await self._ensure_editor():
            def _cursor(line, col) -> None:
                label = _el(f"{tid}-cursor")
                if label:
                    label.textContent = f"Ln {line}, Col {col}"

            tab["editor"] = self._mount_editor(
                host, value=text, dialect=conn.get("dialect") or "StandardSQL",
                placeholder="SELECT * FROM …  ·  Ctrl+Enter runs the statement at the cursor",
                callbacks={
                    "onRun": lambda *_: _spawn(self._sql_run(tid, "statement"), "run"),
                    "onRunScript": lambda *_: _spawn(self._sql_run(tid, "script"), "run script"),
                    "onFormat": lambda *_: _spawn(self._sql_format(tid), "format"),
                    "onChange": lambda *_: self._save_draft(tid),
                    "onCursor": _cursor,
                })
            tab["editor"].focus()
        else:
            host.innerHTML = (f'<textarea class="bk-fallback" data-role="fallback" id="{tid}-fallback"'
                              ' spellcheck="false"></textarea>')
            area = _el(f"{tid}-fallback")
            area.value = text
            tab["fallback"] = area
        await self._sql_load_dbs(tid)
        await self._sql_completion(tid)

    async def _sql_load_dbs(self, tid: str) -> None:
        tab = self._tabs.get(tid)
        conn = self._conn(tab["conn"]) if tab else None
        if not tab or "database" not in ((conn or {}).get("levels") or []):
            return
        result = await SqlService().databases_async(tab["conn"], bool(self._settings.get("show_system")))
        select = _el(f"{tid}-db")
        if not result.get("ok") or not select:
            return
        names = [row["name"] for row in result["value"]]
        if tab["db"] and tab["db"] not in names:
            names.insert(0, tab["db"])
        if not tab["db"] and names:
            tab["db"] = next((row["name"] for row in result["value"] if row.get("current")), names[0])
        select.innerHTML = "".join(
            f'<option value="{_esc(name)}"{" selected" if name == tab["db"] else ""}>{_esc(name)}</option>'
            for name in names)
        self._sql_header(tid)

    async def _sql_completion(self, tid: str) -> None:
        tab = self._tabs.get(tid)
        if not tab or tab["editor"] is None:
            return
        result = await SqlService().completion_async(tab["conn"], tab["db"])
        if result.get("ok") and tid in self._tabs and tab["editor"] is not None:
            tab["editor"].setSchema(_jsobj(result["schema"]), result.get("default_schema") or js.undefined)

    def _save_draft(self, tid: str) -> None:
        tab = self._tabs.get(tid)
        if tab is None:
            return
        if tab.get("draft_timer"):
            js.clearTimeout(tab["draft_timer"])

        def _store() -> None:
            if tid in self._tabs:
                _spawn(UiStateService().set_async(f"draft:{tab['conn']}",
                                                  {"sql": self._sql_text(tid)[:30000],
                                                   "db": tab["db"]}), "draft")

        proxy = create_proxy(_store)
        self._proxies.append(proxy)
        tab["draft_timer"] = js.setTimeout(proxy, DRAFT_SAVE_MS)

    def _sql_text(self, tid: str) -> str:
        tab = self._tabs[tid]
        if tab["editor"] is not None:
            return str(tab["editor"].getValue())
        return str(tab["fallback"].value) if tab["fallback"] else ""

    def _sql_selection(self, tid: str) -> dict:
        tab = self._tabs[tid]
        if tab["editor"] is not None:
            sel = tab["editor"].getSelection().to_py()
            return {"from": int(sel["from"]), "to": int(sel["to"]), "text": str(sel["text"]),
                    "head": int(sel["head"])}
        area = tab["fallback"]
        start, end = int(area.selectionStart), int(area.selectionEnd)
        return {"from": start, "to": end, "text": str(area.value)[start:end], "head": end}

    def _sql_action(self, tid: str, action: str, button) -> None:
        if action == "mode":
            _spawn(self._sql_set_mode(tid, _attr(button, "data-mode") == "manual"), "commit mode")
        elif action in ("commit", "rollback"):
            _spawn(self._sql_finish(tid, action), action)
        elif action == "run":
            _spawn(self._sql_run(tid, "statement"), "run")
        elif action == "run_script":
            _spawn(self._sql_run(tid, "script"), "run script")
        elif action == "cancel":
            _spawn(self._sql_cancel(tid), "cancel")
        elif action == "format":
            _spawn(self._sql_format(tid), "format")
        elif action == "history":
            _spawn(self._history_dialog(tid), "history")
        elif action == "res":
            self._show_result(tid, int(_attr(button, "data-res") or 0))
        elif action == "res_csv":
            self._result_csv(tid)
        elif action == "res_export":
            _spawn(self._result_export(tid), "export")

    def _sql_busy(self, tid: str, busy: bool) -> None:
        for suffix, hide in (("run", busy), ("script", busy), ("cancel", not busy)):
            button = _el(f"{tid}-{suffix}")
            if button:
                button.hidden = hide

    async def _sql_run(self, tid: str, mode: str, confirmed: bool = False) -> None:
        tab = self._tabs.get(tid)
        if tab is None or tab["run_id"]:
            return
        text = self._sql_text(tid)
        sel = self._sql_selection(tid)
        offset = 0
        if sel["text"].strip():
            sql, mode, caret, offset = sel["text"], "script", 0, sel["from"]
        else:
            sql, caret = text, sel["head"]
        if not sql.strip():
            return
        self._run_counter += 1
        run_id = f"{tid}-{self._run_counter}"
        tab["run_id"] = run_id
        self._sql_busy(tid, True)
        self._status(tid, "Running…", "info")
        if tab["editor"] is not None:
            tab["editor"].mark(js.undefined, js.undefined)
        try:
            result = await SqlService().execute_async(
                tab["conn"], tab["db"], sql, mode, caret, tab["max_rows"], run_id, confirmed,
                tab["sid"] if tab["manual"] else "")
        finally:
            if tid in self._tabs:
                tab["run_id"] = None
                self._sql_busy(tid, False)
        if tid not in self._tabs:
            return
        if result.get("needs_confirm"):
            if await self._confirm_sql(
                "Production connection", result.get("statements") or [],
                f"{result.get('count')} statement{'s' if result.get('count') != 1 else ''} may "
                "change data on a production connection.", prod=True, ok_text="Run anyway",
            ):
                await self._sql_run(tid, mode, confirmed=True)
            else:
                self._status(tid, "Not run.", "info")
            return
        if result.get("session_lost"):
            # The transaction ended behind the tab's back (idle, disconnected).
            self._sql_txn(tid, None)
            self._status(tid, result["error"], "info")
            self._toast(result["error"], kind="warning")
            return
        if not result.get("results") and result.get("error"):
            self._status(tid, result["error"])
            return
        if tab["manual"]:
            self._sql_txn(tid, result.get("txn"))
        self._status(tid, "")
        for entry in result["results"]:
            entry["start"] += offset
            entry["end"] += offset
            if entry.get("error_at") is not None:
                entry["error_at"] += offset
        self._render_results(tid, result)

    # -- manual commit mode ------------------------------------------------

    def _sql_mode_ui(self, tid: str) -> None:
        tab = self._tabs[tid]
        root = js.document.querySelector(f'.bk-sqltab[data-tab="{tid}"]')
        if not root:
            return
        for button in root.querySelectorAll('[data-bk="mode"]'):
            on = (_attr(button, "data-mode") == "manual") == tab["manual"]
            button.setAttribute("aria-pressed", "true" if on else "false")
        for suffix in ("commit", "rollback", "txn"):
            element = _el(f"{tid}-{suffix}")
            if element:
                element.hidden = not tab["manual"]
        self._sql_txn(tid, tab["txn"])

    def _sql_txn(self, tid: str, state: dict | None) -> None:
        """Draw the transaction indicator, the buttons and the tab badge."""
        tab = self._tabs.get(tid)
        if tab is None:
            return
        tab["txn"] = state
        chip = _el(f"{tid}-txn")
        open_ = bool(state and state.get("open"))
        failed = bool(state and state.get("failed"))
        commit, rollback = _el(f"{tid}-commit"), _el(f"{tid}-rollback")
        if commit:
            commit.disabled = not open_ or failed
            rollback.disabled = not open_
            commit.classList.toggle("bk-primary", open_ and not failed)
        if chip:
            minutes = int((state or {}).get("idle_limit", 600)) // 60
            if failed:
                chip.textContent = "Transaction failed: roll back"
                chip.setAttribute("data-state", "failed")
            elif open_:
                count = state.get("statements", 0)
                chip.textContent = f"Transaction · {count} statement{'s' if count != 1 else ''}"
                chip.setAttribute("data-state", "open")
            else:
                chip.textContent = "No open transaction"
                chip.setAttribute("data-state", "idle")
            chip.title = (f"Manual commit mode. An open transaction holds locks on the server; "
                          f"it is rolled back after {minutes} minutes without activity.")
        if tab["manual"]:
            self.tabs.set_badge(tid, "TX" if open_ else None)
        else:
            self.tabs.set_badge(tid, None)

    async def _choose(self, title: str, text: str, choices: list) -> str | None:
        """A small dialog of buttons: ``[(id, label, style)]``; None if dismissed."""
        loop = asyncio.get_event_loop()
        answer = loop.create_future()
        modal = ModalWindow(ModalConfig(dispose_on_close=True, title=title, width=500, height=230))
        modal.body.innerHTML = (
            '<div class="bk-dialog"><div class="bk-hint bk-pre-line">' + _esc(text) + "</div>"
            '<div class="bk-actions">' + "".join(
                f'<button type="button" class="bk-btn {style}" data-choice="{key}">{_esc(label)}</button>'
                for key, label, style in choices) + "</div></div>")

        def _on_click(event) -> None:
            which = event.target.closest("[data-choice]")
            if which and not answer.done():
                answer.set_result(_attr(which, "data-choice"))
                modal.close()

        def _on_close(_payload) -> None:
            if not answer.done():
                answer.set_result(None)

        proxy = create_proxy(_on_click)
        self._proxies.append(proxy)
        modal.body.addEventListener("click", proxy)
        modal.on_close(_on_close)
        modal.show()
        return await answer

    async def _settle_txn(self, tid: str, why: str) -> bool:
        """With a transaction open, ask to commit or roll back first. False = cancelled."""
        tab = self._tabs[tid]
        txn = tab.get("txn") or {}
        if not tab["manual"] or not txn.get("open"):
            return True
        count = txn.get("statements", 0)
        choices = [("rollback", "Roll back", "bk-danger"), ("cancel", "Cancel", "")]
        if not txn.get("failed"):
            choices.insert(0, ("commit", "Commit", "bk-primary"))
        choice = await self._choose(
            "Open transaction",
            f"{count} statement{'s' if count != 1 else ''} in this editor "
            f"{'are' if count != 1 else 'is'} not committed yet.\n\n{why}", choices)
        if choice in ("commit", "rollback"):
            return await self._sql_finish(tid, choice)
        return False

    async def _sql_set_mode(self, tid: str, manual: bool) -> None:
        tab = self._tabs.get(tid)
        if tab is None or tab["manual"] == manual or tab["run_id"]:
            return
        if not manual:
            if not await self._settle_txn(tid, "Commit or roll back before switching to auto-commit."):
                return
            await self._end_session(tab)
        tab["manual"] = manual
        tab["txn"] = None
        self._sql_mode_ui(tid)
        self._settings["manual_commit"] = manual
        _spawn(UiStateService().set_async("settings", self._settings), "settings")
        self._toast("Manual commit: statements wait for Commit." if manual
                    else "Auto-commit: each statement is committed as it runs.")

    async def _sql_finish(self, tid: str, action: str) -> bool:
        tab = self._tabs.get(tid)
        if tab is None:
            return False
        conn = self._conn(tab["conn"]) or {}
        if action == "commit" and conn.get("env") == "prod":
            count = (tab.get("txn") or {}).get("statements", 0)
            if not await message.confirm(
                f"Commit {count} statement{'s' if count != 1 else ''} on the production "
                f"connection “{conn.get('name')}”?", title="Commit on production",
                ok_text="Commit", danger=True):
                return False
        result = await SqlService().transaction_async(tab["sid"], action)
        if not result.get("ok"):
            self._toast(result.get("error", f"Could not {action}"), kind="error")
            if "already" in str(result.get("error", "")) or "rolled back" in str(result.get("error", "")):
                self._sql_txn(tid, None)
            return False
        state = result.get("txn") or {}
        self._sql_txn(tid, state)
        done = state.get("done") or 0
        if state.get("nothing") or not state.get("was_open", True):
            self._toast("There was no open transaction.")
        elif action == "commit":
            self._toast(f"Committed {done} statement{'s' if done != 1 else ''}.", kind="success")
        else:
            self._toast(f"Rolled back {done} statement{'s' if done != 1 else ''}.", kind="warning")
        return True

    async def _end_session(self, tab: dict) -> None:
        result = await SqlService().transaction_async(tab["sid"], "end")
        if result.get("ok") and result.get("rolled_back"):
            self._toast(f"{tab['name']}: rolled back {result['rolled_back']} uncommitted "
                        f"statement{'s' if result['rolled_back'] != 1 else ''}.", kind="warning")

    async def _sql_switch_db(self, tid: str, db: str, select) -> None:
        tab = self._tabs[tid]
        if db == tab["db"]:
            return
        if not await self._settle_txn(tid, "Commit or roll back before switching database."):
            select.value = tab["db"]
            return
        tab["db"] = db
        self._sql_header(tid)
        _spawn(self._sql_completion(tid), "completion")
        self._save_draft(tid)

    async def _sql_cancel(self, tid: str) -> None:
        tab = self._tabs.get(tid)
        if not tab or not tab["run_id"]:
            return
        result = await SqlService().cancel_async(tab["run_id"])
        if not result.get("ok"):
            self._toast(result.get("error", "Could not cancel"), kind="warning")

    async def _sql_format(self, tid: str) -> None:
        tab = self._tabs[tid]
        sel = self._sql_selection(tid)
        whole = not sel["text"].strip()
        result = await SqlService().format_sql_async(self._sql_text(tid) if whole else sel["text"])
        if not result.get("ok"):
            return
        if tab["editor"] is not None:
            if whole:
                tab["editor"].setValue(result["sql"], 0)
            else:
                tab["editor"].insert(result["sql"].rstrip("\n"))
        elif tab["fallback"] is not None and whole:
            tab["fallback"].value = result["sql"]

    def _render_results(self, tid: str, result: dict) -> None:
        tab = self._tabs[tid]
        entries = result.get("results") or []
        tab["results"] = [entry for entry in entries if entry.get("columns")]
        lines = []
        for entry in entries:
            first = entry["sql"].strip().splitlines()[0][:120] if entry["sql"].strip() else ""
            if entry.get("error"):
                lines.append(f"✘ [{entry['ms']} ms] {first}\n   {entry['error']}")
            elif entry.get("columns"):
                more = " (more not fetched)" if entry.get("more") else ""
                lines.append(f"✔ [{entry['ms']} ms] {first}\n   {_count(entry['rowcount'])} rows{more}")
            else:
                affected = entry.get("rowcount")
                lines.append(f"✔ [{entry['ms']} ms] {first}\n   "
                             + (f"{_count(affected)} rows affected" if affected is not None else "done"))
        if result.get("rolled_back"):
            lines.append("\nRead-only connection: the transaction was rolled back.")
        txn = result.get("txn") or {}
        if txn.get("lost"):
            lines.append("\nThe cancelled statement ended the database session; "
                         "the transaction was rolled back.")
        elif txn.get("open"):
            lines.append(f"\nIn a transaction: {txn['statements']} statement"
                         f"{'s' if txn['statements'] != 1 else ''} not committed yet.")
        lines.append(f"\nTotal {result.get('ms', 0)} ms")
        _el(f"{tid}-msgs").textContent = "\n".join(lines)
        error = next((entry for entry in entries if entry.get("error")), None)
        if tab["editor"] is not None:
            if error is not None:
                at = error.get("error_at")
                if at is not None:
                    tab["editor"].mark(at, max(at + 1, min(error["end"], at + 40)), "error")
                    tab["editor"].select(at, at)
                else:
                    tab["editor"].mark(error["start"], error["end"], "error")
            elif entries:
                tab["editor"].mark(entries[0]["start"], entries[-1]["end"], "run")
        buttons = "".join(
            f'<button type="button" class="bk-restab" data-bk="res" data-res="{i}" role="tab">'
            f'<span class="mdi mdi-table"></span>Result {i + 1}'
            f'<small>{_count(entry["rowcount"])}{"+" if entry.get("more") else ""}</small></button>'
            for i, entry in enumerate(tab["results"]))
        buttons += ('<button type="button" class="bk-restab" data-bk="res" data-res="-1" role="tab"'
                    f'{" data-error" if error else ""}>'
                    '<span class="mdi mdi-message-text-outline"></span>Messages</button>')
        _el(f"{tid}-restabs").innerHTML = buttons
        _el(f"{tid}-empty").hidden = True
        if error is not None or not tab["results"]:
            self._show_result(tid, -1)
        else:
            self._show_result(tid, len(tab["results"]) - 1 if len(tab["results"]) == 1 else 0)

    def _show_result(self, tid: str, index: int) -> None:
        tab = self._tabs[tid]
        tab["shown"] = index
        for button in js.document.querySelectorAll(f'#{tid}-restabs .bk-restab'):
            button.setAttribute("aria-selected",
                                "true" if _attr(button, "data-res") == str(index) else "false")
        grid_host, msgs = _el(f"{tid}-grid"), _el(f"{tid}-msgs")
        showing_rows = 0 <= index < len(tab["results"])
        grid_host.hidden = not showing_rows
        msgs.hidden = showing_rows
        _el(f"{tid}-res-csv").hidden = not showing_rows
        _el(f"{tid}-res-export").hidden = not showing_rows
        summary = _el(f"{tid}-summary")
        if not showing_rows:
            summary.textContent = ""
            return
        entry = tab["results"][index]
        summary.textContent = (f"{_count(entry['rowcount'])} rows"
                               + (" · more not fetched" if entry.get("more") else "")
                               + f" · {entry['ms']} ms")
        columns = [ColumnConfig(id=f"c{i}", header=name, width=_column_width(name),
                                align="right" if entry["kinds"][i] == "num" else None,
                                cell_class_by=_CLS + f"c{i}")
                   for i, name in enumerate(entry["columns"])]
        cut = {(r, c) for r, c in entry.get("truncated") or []}
        rows = []
        for r, values in enumerate(entry["rows"]):
            row = {_ID: f"r{r}"}
            for c, value in enumerate(values):
                row[f"c{c}"] = value
                row[_CLS + f"c{c}"] = " ".join(
                    cls for cls, on in (("bk-null", value is None), ("bk-cut", (r, c) in cut)) if on)
            rows.append(row)
        if tab["grid"] is None:
            tab["grid"] = DataTable(DataTableConfig(
                columns=columns, rows=rows, id_field=_ID, selection="multi",
                resizable_columns=True, reorderable_columns=True, empty_text="No rows",
                context_actions=[TableAction("copy_row", "Copy row as JSON", "mdi-code-json"),
                                 TableAction("copy_rows", "Copy selected as TSV", "mdi-content-copy")],
            ), container=grid_host)
            tab["grid"].on_action(lambda payload: _spawn(self._result_action(tid, payload),
                                                         "result action"))
        else:
            tab["grid"].set_columns(columns)
            tab["grid"].set_rows(rows)

    async def _result_action(self, tid: str, payload: dict) -> None:
        tab = self._tabs[tid]
        entry = tab["results"][tab["shown"]]
        names = entry["columns"]
        if payload.get("action") == "copy_row":
            row = payload.get("row") or {}
            data = {names[i]: row.get(f"c{i}") for i in range(len(names))}
            if await _copy(json.dumps(data, ensure_ascii=False, indent=2)):
                self._toast("Copied the row as JSON.", kind="success")
        elif payload.get("action") == "copy_rows":
            rows = tab["grid"].get_selected_rows() or [payload.get("row") or {}]
            lines = ["\t".join(names)] + [
                "\t".join("" if row.get(f"c{i}") is None else str(row.get(f"c{i}"))
                          for i in range(len(names))) for row in rows]
            if await _copy("\n".join(lines)):
                self._toast(f"Copied {len(rows)} rows.", kind="success")

    def _result_csv(self, tid: str) -> None:
        tab = self._tabs[tid]
        if tab["grid"] is None:
            return
        count = tab["grid"].export_csv(f"result-{tab['shown'] + 1}.csv")
        self._toast(f"Saved {count} rows as CSV.", kind="success")

    async def _result_export(self, tid: str) -> None:
        """All rows of the shown result's statement, streamed by /bk/export-query."""
        tab = self._tabs[tid]
        entry = tab["results"][tab["shown"]]
        options = js.Object.new()
        options.method = "POST"
        options.credentials = "same-origin"
        headers = js.Object.new()
        setattr(headers, "X-CSRF-Token", _csrf_token())
        setattr(headers, "Content-Type", "application/json")
        options.headers = headers
        options.body = json.dumps({"sql": entry["sql"], "database": tab["db"], "format": "csv"})
        self._toast("Exporting every row…")
        try:
            response = await js.fetch(f"/bk/export-query/{tab['conn']}", options)
        except Exception:  # noqa: BLE001
            self._toast("The export failed.", kind="error")
            return
        if not response.ok:
            try:
                detail = (await response.json()).to_py().get("detail")
            except Exception:  # noqa: BLE001
                detail = None
            self._toast(detail or f"The export failed ({response.status}).", kind="error")
            return
        blob = await response.blob()
        url = js.URL.createObjectURL(blob)
        anchor = js.document.createElement("a")
        anchor.href = url
        anchor.download = f"result-{tab['shown'] + 1}.csv"
        js.document.body.appendChild(anchor)
        anchor.click()
        anchor.remove()
        revoke = create_proxy(lambda: js.URL.revokeObjectURL(url))
        self._proxies.append(revoke)
        js.setTimeout(revoke, 10_000)

    async def _history_dialog(self, tid: str) -> None:
        tab = self._tabs[tid]
        result = await SqlService().history_async(tab["conn"], 300)
        if not result.get("ok"):
            self._toast(result.get("error", "Could not load the history"), kind="error")
            return
        modal = ModalWindow(ModalConfig(dispose_on_close=True, title="Query history", width=900,
                                        height=560))
        modal.body.innerHTML = (
            '<div class="bk-dialog bk-fill">'
            '<div class="bk-hint">Double-click inserts the statement at the cursor.</div>'
            '<div class="bk-box bk-grow-box" data-slot="table"></div>'
            '<div class="bk-actions"><button type="button" class="bk-btn" data-h="clear">'
            '<span class="mdi mdi-delete-sweep-outline"></span>Clear this connection\'s history</button>'
            '<span class="bk-spacer"></span>'
            '<button type="button" class="bk-btn" data-h="close">Close</button></div></div>')
        table = DataTable(DataTableConfig(
            columns=[
                ColumnConfig(id="ran_at", header="When (UTC)", width=150),
                ColumnConfig(id="database", header="Database", width=110),
                ColumnConfig(id="sql", header="Statement", width=460),
                ColumnConfig(id="result", header="Result", width=110, icon_by="icon"),
                ColumnConfig(id="ms", header="ms", width=70, align="right"),
            ],
            rows=[{"id": str(row["id"]), "ran_at": row["ran_at"], "database": row["database"],
                   "sql": " ".join(row["sql"].split())[:400], "full": row["sql"],
                   "result": "ok" if row["ok"] else "error",
                   "icon": "mdi-check" if row["ok"] else "mdi-alert",
                   "ms": row["ms"] if row["ms"] is not None else ""}
                  for row in result["value"]],
            selection="single", filterable=True, filter_placeholder="Filter statements",
            empty_text="Nothing run yet", resizable_columns=True,
        ), container=modal.body.querySelector("[data-slot=table]"))

        def _insert(payload: dict) -> None:
            row = payload.get("row") or {}
            modal.close()
            text = (row.get("full") or "").rstrip()
            if not text.endswith(";"):
                text += ";"
            if tab["editor"] is not None:
                tab["editor"].insert(text + "\n")
            elif tab["fallback"] is not None:
                tab["fallback"].value = str(tab["fallback"].value) + "\n" + text

        async def _clear() -> None:
            if await message.confirm("Clear the query history of this connection?",
                                     title="Clear history", ok_text="Clear", danger=True):
                await SqlService().clear_history_async(tab["conn"])
                table.set_rows([])

        def _on_click(event) -> None:
            which = event.target.closest("[data-h]")
            if not which:
                return
            if _attr(which, "data-h") == "close":
                modal.close()
            else:
                _spawn(_clear(), "clear history")

        table.on_activate(_insert)
        proxy = create_proxy(_on_click)
        self._proxies.append(proxy)
        modal.body.addEventListener("click", proxy)
        modal.show()

    def _select_in_editor(self, loc: tuple, name: str):
        async def _go() -> None:
            conn_id, db, schema = loc
            result = await SqlService().script_async(conn_id, db, schema, name, "select")
            if not result.get("ok"):
                self._toast(result.get("error", "Could not build the query"), kind="error")
                return
            self._open_sql(conn_id, db, result["value"])
        return _go()

    async def _generate_dialog(self, loc: tuple, name: str) -> None:
        conn_id, db, schema = loc
        conn = self._conn(conn_id) or {}
        modal = ModalWindow(ModalConfig(dispose_on_close=True, title=f"Generate SQL — {name}",
                                        width=820, height=560))
        kinds = (("select", "SELECT"), ("insert", "INSERT"), ("update", "UPDATE"),
                 ("delete", "DELETE"), ("ddl", "DDL"))
        modal.body.innerHTML = (
            '<div class="bk-dialog bk-fill">'
            '<div class="bk-seg" role="group">' + "".join(
                f'<button type="button" class="bk-seg-btn" data-g="{k}">{label}</button>'
                for k, label in kinds) + "</div>"
            '<div class="bk-box bk-grow-box bk-code-host" data-slot="code"></div>'
            '<div class="bk-actions"><button type="button" class="bk-btn" data-g="copy">'
            '<span class="mdi mdi-content-copy"></span>Copy</button>'
            '<button type="button" class="bk-btn bk-primary" data-g="open">'
            '<span class="mdi mdi-console-line"></span>Open in SQL editor</button></div></div>')
        host = modal.body.querySelector("[data-slot=code]")
        state = {"text": "", "editor": None}
        use_editor = await self._ensure_editor()

        async def _show(kind: str) -> None:
            for button in modal.body.querySelectorAll("[data-g]"):
                if _attr(button, "data-g") in dict(kinds):
                    button.setAttribute("aria-pressed", "true" if _attr(button, "data-g") == kind else "false")
            result = await SqlService().script_async(conn_id, db, schema, name, kind)
            state["text"] = result.get("value", "") if result.get("ok") else f"-- {result.get('error')}"
            if use_editor:
                if state["editor"] is None:
                    state["editor"] = self._mount_editor(host, value=state["text"], read_only=True,
                                                         dialect=conn.get("dialect") or "StandardSQL")
                else:
                    state["editor"].setValue(state["text"], 0)
            else:
                host.innerHTML = f'<pre class="bk-pre bk-fill">{_esc(state["text"])}</pre>'

        def _on_click(event) -> None:
            which = event.target.closest("[data-g]")
            if not which:
                return
            what = _attr(which, "data-g")
            if what == "copy":
                _spawn(_copy(state["text"]), "copy")
                self._toast("Copied.", kind="success")
            elif what == "open":
                modal.close()
                self._open_sql(conn_id, db, state["text"])
            else:
                _spawn(_show(what), "generate")

        proxy = create_proxy(_on_click)
        self._proxies.append(proxy)
        modal.body.addEventListener("click", proxy)
        modal.show()
        await _show("select")

    # ------------------------------------------------------------------
    # Accounts and about (from Monguana)
    # ------------------------------------------------------------------

    def _password_nag(self) -> None:
        modal = ModalWindow(ModalConfig(dispose_on_close=True, title="Change your password",
                                        width=480, height=250))
        modal.body.innerHTML = (
            '<div class="bk-nag">'
            '<p><span class="mdi mdi-shield-alert-outline"></span> '
            "You are still using a password you did not choose: the default "
            "<code>change_me</code>, or one an administrator set for you. Anyone "
            "who knows it can sign in as you and use your saved connections.</p>"
            "<p>Basilisk will ask again each time it loads until you change it.</p>"
            '<div class="bk-actions">'
            '<button type="button" class="bk-btn" data-nag="later">Later</button>'
            '<button type="button" class="bk-btn bk-primary" data-nag="change">'
            '<span class="mdi mdi-key"></span><span>Change password</span></button>'
            "</div></div>"
        )

        def _on_click(event) -> None:
            which = event.target.closest("[data-nag]")
            if not which:
                return
            modal.close()
            if _attr(which, "data-nag") == "change":
                self._password_dialog()

        proxy = create_proxy(_on_click)
        self._proxies.append(proxy)
        modal.body.addEventListener("click", proxy)
        modal.show()
        focus = modal.body.querySelector('[data-nag="change"]')
        if focus:
            focus.focus()

    def _password_dialog(self) -> None:
        modal = ModalWindow(ModalConfig(dispose_on_close=True, title="Change password",
                                        width=440, height=360))
        form = Form(FormConfig(
            submit_text="Change password", cancel_text="Cancel",
            fields=[
                FieldConfig(id="current_password", label="Current password", type="password",
                            required=True, autocomplete="current-password"),
                FieldConfig(id="new_password", label="New password", type="password",
                            required=True, min_length=8, autocomplete="new-password"),
                FieldConfig(id="confirm", label="Confirm new password", type="password",
                            required=True, matches="new_password",
                            matches_message="Passwords do not match", autocomplete="new-password"),
            ]), container=modal.body)
        form.on_cancel(lambda _payload: modal.close())

        async def _submit(values: dict) -> None:
            form.set_busy(True)
            try:
                result = await UserService().change_own_password_async(
                    values["current_password"], values["new_password"])
                if not result.get("ok"):
                    if result.get("errors"):
                        form.set_errors(result["errors"])
                    else:
                        form.set_error(None, result.get("error", "Could not change"))
                    return
                modal.close()
                self._me["must_change_password"] = False
                self._toast("Password changed.", kind="success")
            finally:
                form.set_busy(False)

        form.on_submit(lambda values: _spawn(_submit(values), "password change"))
        modal.show()
        form.focus_first()

    async def _admin_panel(self) -> None:
        if not self._me.get("is_admin"):
            self._toast("Administrator access required.", kind="error")
            return
        modal = ModalWindow(ModalConfig(dispose_on_close=True, title="Users", width=760, height=560))
        modal.body.innerHTML = (
            '<div class="bk-dialog bk-fill">'
            '  <div class="bk-box bk-grow-box" data-slot="table"></div>'
            '  <div data-slot="form"></div>'
            "</div>"
        )
        table = DataTable(DataTableConfig(
            columns=[
                ColumnConfig(id="username", header="Username"),
                ColumnConfig(id="role", header="Role", width=90),
                ColumnConfig(id="connection_count", header="Connections", width=110, align="right"),
                ColumnConfig(id="created_at", header="Created", width=110),
            ],
            selection="single", empty_text="No users",
            context_actions=[
                TableAction("toggle_admin", "Toggle admin", "mdi-shield-account"),
                TableAction("reset", "Reset password", "mdi-lock-reset"),
                TableAction(separator=True),
                TableAction("delete", "Delete user", "mdi-delete", danger=True),
            ],
        ), container=modal.body.querySelector("[data-slot=table]"))

        async def _refresh() -> None:
            users = await UserService().list_async()
            table.set_rows([{**user, "role": "admin" if user["is_admin"] else "user"}
                            for user in users])

        def _on_action(payload: dict) -> None:
            user_id = payload.get("id")
            row = payload.get("row") or {}
            action = payload.get("action")

            async def _run() -> None:
                service = UserService()
                if action == "delete":
                    if not await message.confirm(
                        f"Delete “{row.get('username')}” and their saved connections?",
                        title="Delete user", ok_text="Delete", danger=True,
                    ):
                        return
                    result = await service.delete_async(int(user_id))
                elif action == "toggle_admin":
                    result = await service.set_admin_async(int(user_id), not row.get("is_admin"))
                elif action == "reset":
                    new_password = await message.prompt(
                        f"New password for {row.get('username')} (min 8 chars):",
                        title="Reset password", ok_text="Reset", password=True,
                    )
                    if not new_password:
                        return
                    result = await service.reset_password_async(int(user_id), str(new_password))
                else:
                    return
                if not result.get("ok"):
                    self._toast(result.get("error")
                                or "; ".join((result.get("errors") or {}).values())
                                or "Action failed", kind="error")
                await _refresh()

            _spawn(_run(), f"admin {action}")

        table.on_action(_on_action)
        create_form = Form(FormConfig(
            columns=3, submit_text="Add user",
            fields=[
                FieldConfig(id="username", label="New user", required=True),
                FieldConfig(id="password", label="Password", type="password", required=True,
                            min_length=8),
                FieldConfig(id="is_admin", label="Administrator", type="checkbox"),
            ]), container=modal.body.querySelector("[data-slot=form]"))

        async def _create(values: dict) -> None:
            create_form.set_busy(True)
            try:
                result = await UserService().create_async(
                    values["username"], values["password"], bool(values.get("is_admin")))
                if not result.get("ok"):
                    if result.get("errors"):
                        create_form.set_errors(result["errors"])
                    else:
                        create_form.set_error(None, result.get("error", "Failed"))
                    return
                create_form.set_values({"username": "", "password": "", "is_admin": False})
                await _refresh()
            finally:
                create_form.set_busy(False)

        create_form.on_submit(lambda values: _spawn(_create(values), "create user"))
        modal.show()
        await _refresh()

    async def _check_release(self) -> None:
        result = await AboutService().latest_async()
        self._release = dict(result) if result.get("ok") else {
            "checked": False, "reason": "unreachable", "error": result.get("error", "")}
        if self._release.get("newer"):
            self.toolbar.set_text("update", f"Update {self._release['latest']}")
            self.toolbar.set_hidden("update", False)
        status = _el("bk-about-update")
        if status:
            status.innerHTML = self._release_html()

    def _release_html(self) -> str:
        release = self._release
        if not release:
            return '<span class="mdi mdi-loading mdi-spin"></span> Checking for a newer release…'
        if release.get("newer"):
            return (f'<span class="mdi mdi-arrow-up-circle"></span> <b>Basilisk '
                    f'{_esc(release["latest"])} is out</b>. <a href="{_esc(release.get("url") or RELEASES_URL)}" '
                    'target="_blank" rel="noopener">What\'s new</a>')
        if release.get("checked"):
            return '<span class="mdi mdi-check-circle-outline"></span> This is the latest release.'
        if release.get("reason") == "off":
            return ('<span class="mdi mdi-minus-circle-outline"></span> Release checks are off '
                    f'(BASILISK_UPDATE_CHECK). <a href="{RELEASES_URL}" target="_blank" rel="noopener">See releases</a>')
        return ('<span class="mdi mdi-cloud-off-outline"></span> Could not check for a newer release. '
                f'<a href="{RELEASES_URL}" target="_blank" rel="noopener">See releases</a>')

    def _about_dialog(self) -> None:
        links = (
            ("mdi-github", "Source", REPO, REPO_URL),
            ("mdi-tag-outline", "Releases", "Downloads and what changed", RELEASES_URL),
            ("mdi-bug-outline", "Issues", "Report a problem or ask for a feature", f"{REPO_URL}/issues"),
            ("mdi-book-open-variant", "Wiki", "How to use it", WIKI_URL),
        )
        modal = ModalWindow(ModalConfig(dispose_on_close=True, title="About Basilisk", width=520, height=400))
        modal.body.innerHTML = (
            '<div class="bk-about">'
            '<div class="bk-about-head">'
            '<img class="bk-about-logo" src="/static/basilisk_avatar.webp" alt="">'
            '<div><div class="bk-about-name">Basilisk '
            f'<span class="bk-about-version">{_esc(VERSION)}</span></div>'
            '<div class="bk-about-sub">SQL databases in the browser · PostgreSQL · MySQL · '
            f'SQL Server · SQLite · {_esc(ABOUT_LICENSE)} license</div></div></div>'
            f'<div class="bk-about-update" id="bk-about-update">{self._release_html()}</div>'
            '<div class="bk-about-links">' + "".join(
                f'<a class="bk-about-link" href="{_esc(url)}" target="_blank" rel="noopener">'
                f'<span class="mdi {icon}"></span><span><b>{_esc(label)}</b>'
                f'<small>{_esc(hint)}</small></span></a>'
                for icon, label, hint, url in links
            ) + "</div></div>"
        )
        modal.show()

    def _shortcuts_dialog(self) -> None:
        rows = (
            ("Ctrl+Enter", "Run the statement at the cursor, or the selection"),
            ("Ctrl+Shift+Enter, Alt+X", "Run the whole script"),
            ("Ctrl+Space", "Complete keywords, tables and columns"),
            ("Ctrl+Shift+F", "Format the script (or the selection)"),
            ("Ctrl+F", "Find and replace in the editor"),
            ("Ctrl+]", "New SQL editor"),
            ("Enter", "Apply the WHERE / ORDER BY of a table"),
            ("Double-click, F2", "Edit a cell (saved with Save)"),
            ("?", "This list"),
        )
        modal = ModalWindow(ModalConfig(dispose_on_close=True, title="Keyboard shortcuts",
                                        width=560, height=440))
        modal.body.innerHTML = '<table class="bk-kv">' + "".join(
            f"<tr><th><kbd>{_esc(key)}</kbd></th><td>{_esc(text)}</td></tr>" for key, text in rows
        ) + "</table>"
        modal.show()

    def _toast(self, text: str, kind: str = "info") -> None:
        message.toast(text, kind=kind)


_CSS = """
:root{--bk-accent:#f59e0b;--bk-accent-strong:#b45309;--bk-bg:#0f172a;--bk-panel:#111827;
  --bk-deep:#0b1220;--bk-line:#1f2937;--bk-line-2:#334155;--bk-text:#e2e8f0;--bk-muted:#94a3b8;
  --bk-dim:#64748b;}
[data-wapyt-theme="dark"] .wapyt-toolbar{background:var(--bk-panel);color:#cbd5f5;
  border-bottom-color:var(--bk-line);}
[data-wapyt-theme="dark"] .wapyt-toolbar-sep{background:var(--bk-line-2);}

.bk-sidebar{display:flex;flex-direction:column;height:100%;min-height:0;}
.bk-brand{display:flex;align-items:center;gap:10px;padding:10px 12px;flex:0 0 auto;
  background:var(--bk-panel);border-bottom:1px solid var(--bk-line);}
.bk-brand-logo{width:40px;height:40px;border-radius:50%;object-fit:cover;
  border:1px solid var(--bk-accent-strong);background:var(--bk-bg);}
.bk-brand-text{display:flex;flex-direction:column;min-width:0;line-height:1.25;}
.bk-brand-name{font:600 14px system-ui,sans-serif;color:var(--bk-text);}
.bk-brand-sub{font:10.5px system-ui,sans-serif;color:var(--bk-dim);letter-spacing:.03em;}
.bk-sidebar-tree{flex:1 1 auto;min-height:0;}
.bk-workspace{height:100%;min-height:0;}
.wapyt-tabwidget-panels:empty::after{
  content:"Double-click a table in the navigator to open it, or start a SQL editor (Ctrl+]).";
  display:flex;align-items:center;justify-content:center;height:100%;
  color:var(--bk-dim);font:14px system-ui,sans-serif;text-align:center;padding:0 24px;}
.wapyt-tabwidget-tabs:empty{display:none;}
/* Unsaved row changes in a table tab; wapyt's badge has no dark colours. */
.wapyt-tab-badge{background:var(--bk-accent-strong);color:#fffbeb;font-weight:600;}

.bk-view{display:flex;flex-direction:column;height:100%;min-height:0;
  font:13px system-ui,sans-serif;color:var(--bk-text);container-type:inline-size;}
.bk-view [hidden],.bk-res [hidden],.bk-btn[hidden]{display:none !important;}
.bk-head{display:flex;align-items:center;gap:6px;flex:0 0 auto;padding:7px 12px;
  background:var(--bk-bg);border-bottom:1px solid var(--bk-line);color:var(--bk-muted);
  white-space:nowrap;overflow:hidden;}
.bk-head strong{color:var(--bk-text);}
.bk-head .mdi{font-size:15px;color:var(--bk-dim);}
.bk-sep{color:var(--bk-line-2);}
.bk-head-stats{margin-left:auto;color:var(--bk-dim);font-size:12px;}
.bk-badge{margin-left:8px;padding:1px 8px;border-radius:999px;font:600 11px system-ui,sans-serif;
  letter-spacing:.03em;border:1px solid var(--bk-line-2);color:var(--bk-muted);}
.bk-badge[data-env="prod"]{background:#450a0a;border-color:#b91c1c;color:#fecaca;}
.bk-badge[data-env="test"]{background:#3b2506;border-color:#b45309;color:#fde68a;}
.bk-badge[data-env="dev"]{background:#052e16;border-color:#15803d;color:#bbf7d0;}
.bk-badge[data-env="ro"] .mdi{font-size:12px;color:inherit;}
.bk-subtabs{display:flex;gap:2px;padding:4px 10px 0;background:var(--bk-bg);
  border-bottom:1px solid var(--bk-line);flex:0 0 auto;}
.bk-subtab{display:inline-flex;align-items:center;gap:6px;padding:6px 12px;border:1px solid transparent;
  border-bottom:0;border-radius:6px 6px 0 0;background:transparent;color:var(--bk-muted);cursor:pointer;
  font:12.5px system-ui,sans-serif;margin-bottom:-1px;}
.bk-subtab:hover{color:var(--bk-text);}
.bk-subtab[aria-selected="true"]{background:var(--bk-panel);border-color:var(--bk-line);
  color:var(--bk-accent);}
.bk-sub{display:flex;flex-direction:column;flex:1 1 auto;min-height:0;}
.bk-scroll{overflow:auto;padding:4px 12px 16px;}
.bk-h3{display:flex;align-items:baseline;gap:8px;margin:14px 0 6px;
  font:600 11px system-ui,sans-serif;text-transform:uppercase;letter-spacing:.06em;color:var(--bk-muted);}
.bk-h3 small{text-transform:none;letter-spacing:0;font-weight:400;color:var(--bk-dim);}
.bk-h3 .mdi{font-size:14px;color:var(--bk-accent);}
.bk-box{position:relative;height:190px;border:1px solid var(--bk-line);border-radius:6px;
  overflow:hidden;flex:0 0 auto;}
.bk-grow-box{flex:1 1 auto;height:auto;min-height:120px;}
.bk-filter{display:flex;align-items:center;gap:6px;flex:0 0 auto;padding:7px 10px;
  background:var(--bk-panel);border-bottom:1px solid var(--bk-line);}
.bk-flabel{font:600 11px ui-monospace,Menlo,Consolas,monospace;color:var(--bk-accent);
  letter-spacing:.04em;white-space:nowrap;}
.bk-grow{flex:1 1 auto;min-width:0;}
.bk-order{flex:0 1 260px;min-width:100px;}
.bk-input,.bk-select{background:var(--bk-bg);color:var(--bk-text);border:1px solid var(--bk-line-2);
  border-radius:6px;padding:6px 8px;font:12.5px system-ui,sans-serif;box-sizing:border-box;}
.bk-input:focus,.bk-select:focus{outline:none;border-color:var(--bk-accent);
  box-shadow:0 0 0 3px rgba(245,158,11,.18);}
.bk-code{font:12.5px ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;}
.bk-select{padding:5px 6px;}
.bk-btn{display:inline-flex;align-items:center;gap:6px;padding:6px 11px;color:#cbd5f5;
  background:#1e293b;border:1px solid var(--bk-line-2);border-radius:6px;cursor:pointer;
  font:12.5px system-ui,sans-serif;white-space:nowrap;}
.bk-btn:hover:not(:disabled){background:var(--bk-line-2);}
.bk-btn:disabled{opacity:.45;cursor:not-allowed;}
.bk-btn .mdi{font-size:15px;}
.bk-primary{background:var(--bk-accent-strong);border-color:var(--bk-accent);color:#fffbeb;}
.bk-primary:hover:not(:disabled){background:#92400e;}
.bk-danger{background:#7f1d1d;border-color:#b91c1c;color:#fee2e2;}
.bk-danger:hover:not(:disabled){background:#991b1b;}
.bk-bar{display:flex;align-items:center;gap:4px;flex:0 0 auto;padding:5px 10px;
  background:var(--bk-bg);border-bottom:1px solid var(--bk-line);flex-wrap:wrap;}
.bk-bar .bk-btn:not(.bk-primary):not(.bk-danger){background:transparent;border-color:transparent;padding:5px 9px;}
.bk-bar .bk-btn:not(.bk-primary):not(.bk-danger):hover:not(:disabled){background:var(--bk-line);border-color:var(--bk-line-2);}
.bk-bar-sep{width:1px;height:18px;margin:0 4px;background:var(--bk-line-2);}
.bk-spacer{flex:1 1 auto;}
.bk-note{color:var(--bk-dim);font-size:12px;white-space:nowrap;margin-right:6px;}
.bk-pager{display:inline-flex;}
.bk-status{flex:0 0 auto;padding:6px 12px;font:12.5px ui-monospace,Menlo,Consolas,monospace;
  white-space:pre-wrap;border-bottom:1px solid var(--bk-line);}
.bk-status[data-kind="error"]{background:#2a1215;color:#fca5a5;}
.bk-status[data-kind="info"]{background:#1f1a0b;color:#fcd34d;}
.bk-results{position:relative;flex:1 1 auto;min-height:0;}
.bk-panel{position:absolute;inset:0;min-width:0;min-height:0;}
.bk-panel .wapyt-datatable-table{min-width:100%;}
.bk-foot{flex:0 0 auto;padding:4px 12px;border-top:1px solid var(--bk-line);background:var(--bk-deep);
  overflow:hidden;white-space:nowrap;text-overflow:ellipsis;}
.bk-foot code{font:11.5px ui-monospace,Menlo,Consolas,monospace;color:var(--bk-dim);}
.bk-code-host .cm-editor{height:100%;}
.bk-editor-host{height:100%;min-height:0;}
.bk-editor-host .cm-editor{height:100%;}
.bk-fallback{width:100%;height:100%;box-sizing:border-box;resize:none;border:0;padding:10px 12px;
  background:var(--bk-deep);color:var(--bk-text);font:13px/1.5 ui-monospace,Menlo,Consolas,monospace;}
.bk-fallback:focus{outline:none;}
.bk-split{flex:1 1 auto;min-height:0;position:relative;}
.bk-split > .wapyt-layout,.bk-split-layout{height:100%;}
.bk-res{display:flex;flex-direction:column;height:100%;min-height:0;font:13px system-ui,sans-serif;
  color:var(--bk-text);}
.bk-res-bar{display:flex;align-items:center;gap:6px;padding:4px 8px;background:var(--bk-bg);
  border-bottom:1px solid var(--bk-line);flex:0 0 auto;}
.bk-res-tabs{display:flex;gap:2px;flex-wrap:wrap;min-width:0;}
.bk-restab{display:inline-flex;align-items:center;gap:6px;padding:4px 10px;border-radius:6px;
  border:1px solid transparent;background:transparent;color:var(--bk-muted);cursor:pointer;
  font:12px system-ui,sans-serif;}
.bk-restab small{color:var(--bk-dim);}
.bk-restab:hover{background:var(--bk-line);}
.bk-restab[aria-selected="true"]{background:var(--bk-panel);border-color:var(--bk-line-2);color:var(--bk-accent);}
.bk-restab[data-error]{color:#fca5a5;}
.bk-commit.bk-primary{background:#15803d;border-color:#22c55e;color:#f0fdf4;}
.bk-commit.bk-primary:hover:not(:disabled){background:#166534;}
.bk-txn{padding:2px 9px;border-radius:999px;font:600 11.5px system-ui,sans-serif;white-space:nowrap;
  border:1px solid var(--bk-line-2);color:var(--bk-dim);}
.bk-txn[data-state="open"]{background:#3b2506;border-color:#b45309;color:#fde68a;}
.bk-txn[data-state="open"]::before{content:"● ";color:#f59e0b;}
.bk-txn[data-state="failed"]{background:#450a0a;border-color:#b91c1c;color:#fecaca;}
.bk-pre-line{white-space:pre-line;}
.bk-msgs{margin:0;padding:10px 14px;overflow:auto;background:var(--bk-deep);color:#e2e8f0;
  font:12.5px/1.55 ui-monospace,Menlo,Consolas,monospace;white-space:pre-wrap;}
.bk-empty{display:flex;align-items:center;justify-content:center;text-align:center;padding:24px;
  color:var(--bk-dim);}
/* Cells: NULL, cut short, pending changes. */
td.bk-null:not([data-editing])::before{content:"NULL";color:#64748b;font-style:italic;font-size:11.5px;}
td.bk-null:has(input)::before{content:none;}
td.bk-cut{color:#cbd5e1;}
td.bk-cut::after{content:"";}
td.bk-dirty{background:rgba(245,158,11,.16) !important;box-shadow:inset 2px 0 0 #f59e0b;}
tr.bk-new td{background:rgba(34,197,94,.08);}
tr.bk-new td:first-child{box-shadow:inset 3px 0 0 #22c55e;}
tr.bk-deleted td{background:rgba(239,68,68,.12);text-decoration:line-through;color:#fca5a5;}
tr.bk-changed td:first-child{box-shadow:inset 3px 0 0 #f59e0b;}
th.bk-pkcol .wapyt-datatable-header-label::before,th.bk-pkcol > span:first-child::before{
  content:"\\F0306";font-family:"Material Design Icons";color:#fbbf24;margin-right:4px;}
td.bk-num{font-variant-numeric:tabular-nums;}
.wapyt-datatable-cell-icon.mdi-key{color:#fbbf24;}
.wapyt-datatable-cell-icon.mdi-link-variant{color:#7dd3fc;}
.wapyt-datatable-cell-icon.mdi-alert{color:#f87171;}
.wapyt-datatable-cell-icon.mdi-check{color:#4ade80;}
@container (max-width: 900px){
  .bk-bar .bk-btn span:not(.mdi){display:none;}
  .bk-note{display:none;}
}

.bk-dialog{display:flex;flex-direction:column;gap:10px;color:var(--bk-text);font:13px system-ui,sans-serif;}
.bk-fill{height:100%;min-height:0;}
.bk-hint{color:var(--bk-muted);font-size:12.5px;}
.bk-pre{margin:0;padding:10px 12px;flex:1 1 auto;min-height:0;overflow:auto;background:var(--bk-deep);
  border:1px solid var(--bk-line);border-radius:6px;color:#e2e8f0;
  font:12.5px/1.5 ui-monospace,Menlo,Consolas,monospace;white-space:pre-wrap;}
.bk-panel .bk-pre{height:100%;box-sizing:border-box;border:0;border-radius:0;}
.bk-warn{padding:8px 12px;border-radius:6px;background:#450a0a;border:1px solid #b91c1c;color:#fecaca;}
.bk-actions{display:flex;align-items:center;justify-content:flex-end;gap:8px;flex:0 0 auto;}
.bk-seg{display:inline-flex;gap:2px;padding:2px;align-self:flex-start;background:var(--bk-panel);
  border:1px solid var(--bk-line);border-radius:7px;}
.bk-seg-btn{padding:4px 12px;border:0;border-radius:5px;background:transparent;color:var(--bk-muted);
  cursor:pointer;font:600 11.5px system-ui,sans-serif;letter-spacing:.04em;}
.bk-seg-btn:hover{color:var(--bk-text);background:var(--bk-line);}
.bk-seg-btn[aria-pressed="true"]{color:var(--bk-accent);background:#1e293b;}
.bk-nag{display:flex;flex-direction:column;gap:10px;padding:4px 2px;font:13px/1.55 system-ui,sans-serif;color:#cbd5f5;}
.bk-nag p{margin:0;}
.bk-nag .mdi-shield-alert-outline{color:#fbbf24;font-size:17px;vertical-align:-2px;}
.bk-nag code{padding:1px 5px;border-radius:4px;background:var(--bk-panel);color:#fde68a;}
.bk-about{display:flex;flex-direction:column;gap:14px;padding:4px 2px;font:13px/1.5 system-ui,sans-serif;color:#cbd5f5;}
.bk-about-head{display:flex;align-items:center;gap:14px;}
.bk-about-logo{width:56px;height:56px;border-radius:50%;}
.bk-about-name{font-size:20px;font-weight:600;color:var(--bk-text);}
.bk-about-version{font:600 13px ui-monospace,Menlo,Consolas,monospace;color:#fcd34d;margin-left:4px;}
.bk-about-sub{color:var(--bk-muted);font-size:12px;}
.bk-about-update{padding:8px 10px;border-radius:6px;background:var(--bk-panel);border:1px solid var(--bk-line-2);}
.bk-about-update a,.bk-about-link{color:#fcd34d;}
.bk-about-links{display:grid;grid-template-columns:1fr 1fr;gap:8px;}
.bk-about-link{display:flex;align-items:flex-start;gap:9px;padding:9px 10px;border-radius:6px;
  border:1px solid var(--bk-line-2);text-decoration:none;}
.bk-about-link:hover{background:var(--bk-line);}
.bk-about-link .mdi{font-size:20px;line-height:1.1;}
.bk-about-link b{display:block;color:var(--bk-text);font-weight:600;}
.bk-about-link small{color:var(--bk-muted);font-size:11.5px;}
.bk-kv{border-collapse:collapse;width:100%;font:13px system-ui,sans-serif;color:var(--bk-text);}
.bk-kv th{text-align:left;font-weight:500;color:var(--bk-muted);padding:6px 12px 6px 0;
  white-space:nowrap;vertical-align:top;width:40%;}
.bk-kv td{padding:6px 0;}
.bk-kv kbd{padding:2px 6px;border:1px solid var(--bk-line-2);border-radius:4px;background:var(--bk-bg);
  font:12px ui-monospace,Menlo,Consolas,monospace;}
"""
