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
import base64
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
    progress_html,
)

from services.about import LICENSE as ABOUT_LICENSE, RELEASES_URL, REPO, REPO_URL, VERSION, WIKI_URL
from services.about_service import AboutService
from services.connection_service import ConnectionService
from services.job_service import JobService
from services.sql_service import SqlService
from services.ui_state_service import UiStateService
from services.user_service import UserService
from services.values import MAX_CELL_CHARS
from ui.compare_tools import (
    default_chosen as cmp_default_chosen,
    grid_rows as cmp_grid_rows,
    object_label as cmp_object_label,
    script as cmp_script,
    statement_count as cmp_statement_count,
    summary as cmp_summary,
)
from ui.er_layout import (
    MODES as ER_MODES,
    bounds as er_bounds,
    edge_path as er_edge_path,
    edges as er_edges,
    layout as er_layout_boxes,
    nodes as er_nodes,
    render as er_render,
    resize as er_resize,
)
from ui.grid_tools import (
    FILTER_OPS,
    MAX_FROZEN,
    NO_VALUE_OPS,
    apply_layout,
    filter_label,
    filter_rows,
    foreign_key_for,
    frozen_count,
    insert_sql,
    kind_for_type,
    markdown_table,
    merge_layout,
    reference_where,
    table_of_query,
    where_with_filters,
)
from ui.import_tools import (
    DELIMITERS,
    ENCODINGS,
    SAMPLE_BYTES,
    auto_map,
    decode_sample,
    format_bytes,
    guess_encoding,
    parse_rows,
    preview_cells,
    sniff_delimiter,
    source_columns,
)
from ui.value_panel import ValuePanel, grid_text

# pytincture's MainWindow detection only knows dhxpyt; without this the app
# starts with HTTP 422.
APP_ENTRYPOINT = "Basilisk"

APP_FAVICON = "static/basilisk_avatar.webp"

EDITOR_SRC = "/vendor/codemirror/basilisk-editor.js?v=3"  # bump with the bundle
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


def _css_attr(value: str) -> str:
    """A value inside a double-quoted CSS attribute selector."""
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


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


def _binary_type(type_name: str) -> bool:
    upper = type_name.upper()
    return any(word in upper for word in ("BYTEA", "BLOB", "BINARY", "IMAGE", "RAW"))


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
        self._objects: dict = {}             # (conn, db, schema) -> sqlops.objects result
        self._roles: dict = {}               # conn -> [role rows]
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
        objects = ["table", "view", "matview"]
        code = ["function", "procedure", "sequence", "trigger"]
        routines = ["function", "procedure"]
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
                               kinds=["database", "schema", "group", "roles"]),
                    TreeAction("sessions", "Sessions…", "mdi-pulse", kinds=server),
                    TreeAction("er", "ER diagram", "mdi-graph-outline",
                               kinds=["server", "database", "schema", "group"]),
                    TreeAction("er_table", "ER diagram around this table", "mdi-graph-outline",
                               kinds=["table"]),
                    TreeAction("compare", "Compare schema with…", "mdi-compare-horizontal",
                               kinds=["server", "database", "schema"]),
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
                    TreeAction("refresh_matview", "Refresh materialized view…",
                               "mdi-table-refresh", kinds=["matview"]),
                    TreeAction("open_obj", "Open", "mdi-information-outline",
                               kinds=code + ["role"]),
                    TreeAction("call_sql", "Call in a SQL editor", "mdi-play-outline",
                               kinds=routines),
                    TreeAction("def_sql", "Definition in a SQL editor", "mdi-console-line",
                               kinds=code + ["role"]),
                    TreeAction("trigger_table", "Open the table", "mdi-table", kinds=["trigger"]),
                    TreeAction("copy_name", "Copy qualified name", "mdi-content-copy",
                               kinds=objects + ["function", "procedure", "sequence"]),
                    TreeAction(separator=True),
                    TreeAction("create_table", "Create table…", "mdi-table-plus",
                               kinds=["server", "database", "schema", "group"]),
                    TreeAction("add_column", "Add column…", "mdi-table-column-plus-after",
                               kinds=["table"]),
                    TreeAction("create_index", "Create index…", "mdi-sort-ascending",
                               kinds=["table"]),
                    TreeAction("rename_table", "Rename…", "mdi-rename-box", kinds=["table"]),
                    TreeAction("import_csv", "Import CSV…", "mdi-file-import-outline",
                               kinds=["table"]),
                    TreeAction("truncate", "Truncate…", "mdi-table-remove", kinds=["table"],
                               danger=True),
                    TreeAction("drop", "Drop…", "mdi-delete", kinds=objects, danger=True),
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
        self._listen("input", self._on_input)
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
        elif tab["kind"] == "object":
            self._object_action(tid, action, button)
        elif tab["kind"] == "sessions":
            self._sessions_action(tid, action, button)
        elif tab["kind"] == "er":
            self._er_action(tid, action, button)
        elif tab["kind"] == "compare":
            self._cmp_action(tid, action, button)
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
        if tab["kind"] == "compare":
            if role in ("cmp_conn", "cmp_db", "cmp_schema"):
                _spawn(self._cmp_pick(tid, _attr(target, "data-side"), role, str(target.value)),
                       "compare choice")
            return
        if tab["kind"] == "sessions":
            if role == "s_auto":
                tab["auto"] = bool(target.checked)
                if tab["auto"]:
                    _spawn(self._load_sessions(tid), "sessions")
            elif role == "s_idle":
                tab["idle"] = bool(target.checked)
                _spawn(self._load_sessions(tid), "sessions")
            return
        if tab["kind"] == "sql":
            if role == "database":
                _spawn(self._sql_switch_db(tid, str(target.value), target), "database")
            elif role == "max_rows":
                tab["max_rows"] = int(target.value)

    def _on_input(self, event) -> None:
        target = event.target
        if not target or not hasattr(target, "closest"):
            return
        tid = self._tab_of(target)
        if tid is not None and _attr(target, "data-role") == "er_find":
            self._er_find(tid, str(target.value or ""))

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
        if key == "F7" and not (event.ctrlKey or event.metaKey or event.altKey):
            active = tid or self.tabs.get_active()
            tab = self._tabs.get(str(active)) if active else None
            if tab is not None and (tab["kind"] == "sql" or tab.get("sub") == "data"):
                event.preventDefault()
                self._vp_toggle(str(active))
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
        if key == "Enter" and event.shiftKey:
            # A click on a cell does not give it keyboard focus, so Shift+Enter
            # after a click lands here: open the panel on the clicked cell.
            active = self.tabs.get_active()
            tab = self._tabs.get(str(active)) if active else None
            if tab is not None and tab.get("focus_cell"):
                event.preventDefault()
                self._vp_open(str(active), *tab["focus_cell"], focus=True)
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
        self._roles.pop(conn_id, None)
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
        tunnel = conn.get("ssh") or {}
        if tunnel.get("enabled"):
            where += f"\nthrough SSH {tunnel.get('username')}@{tunnel.get('host')}:{tunnel.get('port')}"
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
        elif conn.get("has_roles"):
            children = children + [self._roles_item(conn)]
        badge = {"prod": "PROD", "test": "TEST", "dev": "DEV"}.get(conn.get("env") or "")
        return TreeItem(
            id=node, label=conn["name"], icon=conn.get("icon") or "mdi-database",
            tooltip=tooltip, items=children, badge=badge,
            data={"kind": "server", "conn": conn_id, "db": "", "schema": ""},
        )

    def _roles_item(self, conn: dict) -> TreeItem:
        conn_id = conn["id"]
        node = _node_id("r", conn_id)
        loaded = self._roles.get(conn_id)
        if loaded is None:
            items = [self._note_item(node, self._errors.get(node) or (
                "Loading…" if node in self._loading else "Expand to list"))]
        else:
            items = [self._role_item(conn_id, row) for row in loaded] or [
                self._note_item(node, "None visible")]
        return TreeItem(
            id=node, label=conn.get("roles_label") or "Users & roles", icon="mdi-account-group",
            open_icon="mdi-account-group", badge=len(loaded) if loaded is not None else None,
            items=items, data={"kind": "roles", "conn": conn_id, "db": "", "schema": ""})

    @staticmethod
    def _role_item(conn_id: int, row: dict) -> TreeItem:
        flags = [word for word, on in (("can log in", row.get("login")),
                                       ("administrator", row.get("admin")),
                                       ("role", row.get("role"))) if on]
        icon = "mdi-account-star" if row.get("admin") else (
            "mdi-account" if row.get("login") else "mdi-account-outline")
        return TreeItem(
            id=_node_id("u", conn_id, row["id"]), label=row["name"], icon=icon,
            tooltip=row["name"] + (f"\n{', '.join(flags)}" if flags else ""),
            data={"kind": "role", "conn": conn_id, "db": "", "schema": "", "name": row["name"],
                  "ident": row["id"]})

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

    # group key, label, icon, which list of the objects result, row kind
    _GROUPS = (
        ("tables", "Tables", "mdi-table-multiple", "tables", "table"),
        ("views", "Views", "mdi-eye-outline", "views", "view"),
        ("functions", "Functions", "mdi-function-variant", "routines", "function"),
        ("procedures", "Procedures", "mdi-cog-play-outline", "routines", "procedure"),
        ("sequences", "Sequences", "mdi-counter", "sequences", "sequence"),
        ("triggers", "Triggers", "mdi-lightning-bolt-outline", "triggers", "trigger"),
    )

    def _object_children(self, conn: dict, loc: tuple, parent: str) -> list | None:
        """The object groups of a location (Tables, Views, ...), or None while unloaded."""
        found = self._objects.get(loc)
        if found is None:
            return None
        conn_id, db, schema = loc
        shown = {"tables", "views", *(found.get("groups") or [])}
        errors = found.get("errors") or {}
        groups = []
        for group, label, icon, source, kind in self._GROUPS:
            if group not in shown:
                continue
            gid = _node_id("g", conn_id, db, schema, group)
            rows = [row for row in found.get(source) or []
                    if source != "routines" or row.get("kind") == kind]
            if source in errors:
                items = [self._note_item(gid, f"⚠ {errors[source]}")]
            else:
                items = self._group_items(loc, rows, kind) or [
                    self._note_item(gid, f"No {label.lower()}")]
            groups.append(TreeItem(
                id=gid, label=label, icon=icon, open_icon=icon,
                badge=None if source in errors else len(rows), items=items,
                data={"kind": "group", "conn": conn_id, "db": db, "schema": schema},
            ))
        return groups

    def _group_items(self, loc: tuple, rows: list, kind: str) -> list:
        if kind in ("table", "view"):
            return [self._object_item(loc, row, kind) for row in rows]
        conn_id, db, schema = loc
        names = [row["name"] for row in rows]
        items = []
        for row in rows:
            name = row["name"]
            ident = row.get("id") or name
            table = row.get("table") or ""
            label, tip = name, ".".join(part for part in (db, schema, name) if part)
            if kind in ("function", "procedure"):
                signature = f"{name}({row.get('args') or ''})"
                if names.count(name) > 1:   # overloads: tell them apart
                    label = signature
                tip = f"{tip}({row.get('args') or ''})"
                if row.get("returns"):
                    tip += f"\nreturns {row['returns']}"
            elif kind == "trigger":
                if names.count(name) > 1:
                    label = f"{name} ({table})"
                tip += f"\n{row.get('timing', '')} {row.get('events', '')} ON {table}".rstrip()
            items.append(TreeItem(
                id=_node_id("o", conn_id, db, schema, kind, ident, table), label=label,
                icon=dict((g[4], g[2]) for g in self._GROUPS)[kind], tooltip=tip,
                data={"kind": kind, "conn": conn_id, "db": db, "schema": schema, "name": name,
                      "ident": ident, "table": table, "label": label},
            ))
        return items

    def _object_item(self, loc: tuple, row: dict, kind: str) -> TreeItem:
        conn_id, db, schema = loc
        name = row["name"]
        rows = row.get("rows")
        tip = ".".join(part for part in (db, schema, name) if part)
        if rows is not None:
            tip += f"\n≈ {_count(rows)} rows"
        if row.get("materialized"):
            kind = "matview"    # its own menu (Refresh); opens like a view
            tip += "\nmaterialized view"
        icon = "mdi-table" if kind == "table" else (
            "mdi-table-eye" if kind == "matview" else "mdi-eye-outline")
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
        elif kind == "roles" and conn_id not in self._roles and node_id not in self._loading:
            _spawn(self._load_roles(conn_id), "roles")

    def _on_tree_activate(self, payload: dict) -> None:
        data = self._payload_data(payload)
        kind = data.get("kind")
        if kind in ("table", "view", "matview"):
            self._open_table(self._loc(data), data["name"], "table" if kind == "table" else "view")
        elif kind in self._OBJECT_ICONS:
            self._open_object(data)

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
            self._objects[loc] = {key: value for key, value in result.items() if key != "ok"}
            self._rebuild_tree()
            self.tree.expand(_node_id("g", conn_id, db, schema, "tables"))
        else:
            self._objects.pop(loc, None)
            self._rebuild_tree()

    async def _load_roles(self, conn_id: int) -> None:
        node = _node_id("r", conn_id)
        result = await self._load(node, SqlService().roles_async(
            conn_id, bool(self._settings.get("show_system"))))
        if result.get("ok"):
            self._roles[conn_id] = result["value"]
        else:
            self._roles.pop(conn_id, None)
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
        if _node_id("r", conn_id) in expanded and conn.get("has_roles"):
            await self._load_roles(conn_id)
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
        if kind == "roles":
            self._roles.pop(conn_id, None)
            await self._load_roles(conn_id)
            return
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
            "refresh_matview": lambda: self._refresh_matview(self._loc(data), name),
            "call_sql": lambda: self._object_to_sql(data, "call_sql"),
            "def_sql": lambda: self._object_to_sql(data, "definition"),
            "create_table": lambda: self._create_table(data),
            "add_column": lambda: self._add_column(self._loc(data), name),
            "create_index": lambda: self._create_index(self._loc(data), name),
            "rename_table": lambda: self._rename_table(self._loc(data), name),
            "truncate": lambda: self._truncate(self._loc(data), name),
            "import_csv": lambda: self._import_dialog(self._loc(data), name),
            "drop": lambda: self._drop(self._loc(data), name, data.get("kind") or "table"),
        }
        if action == "sql":
            self._open_sql(conn_id, self._editor_db(data))
        elif action in ("open", "properties", "ddl"):
            sub = {"open": "data", "properties": "columns", "ddl": "ddl"}[action]
            kind = data.get("kind", "table")
            self._open_table(self._loc(data), name, "table" if kind == "table" else "view", sub)
        elif action == "open_obj":
            self._open_object(data)
        elif action == "sessions":
            self._open_sessions(conn_id)
        elif action == "er":
            self._open_er(data)
        elif action == "er_table":
            self._open_er(data, focus=name)
        elif action == "compare":
            self._open_compare(data)
        elif action == "trigger_table":
            self._open_table(self._loc(data), data.get("table") or "", "table")
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
        modal = ModalWindow(ModalConfig(dispose_on_close=True, title=title, width=640, height=760))
        has_password = bool(existing.get("has_password"))
        ssh = existing.get("ssh") or {}
        ssh_fields = [
            FieldConfig(id="ssh_enabled", label="Connect through SSH", type="toggle", span=2,
                        value=bool(ssh.get("enabled")),
                        help="A tunnel through an SSH server that can reach the database. "
                             "Host and Port above are then as that server sees them."),
            FieldConfig(id="ssh_host", label="SSH host", value=ssh.get("host") or ""),
            FieldConfig(id="ssh_port", label="SSH port", type="number", min=1, max=65535,
                        value=ssh.get("port") or 22),
            FieldConfig(id="ssh_username", label="SSH user", value=ssh.get("username") or "",
                        autocomplete="off"),
            FieldConfig(id="ssh_auth", label="Sign in with", type="toggle_group",
                        value=ssh.get("auth") or "password", options=[
                            SelectOption(value="password", label="Password"),
                            SelectOption(value="key", label="Private key")]),
            FieldConfig(id="ssh_password", label="SSH password", type="password", span=2,
                        autocomplete="new-password",
                        placeholder="(stored — leave empty to keep)" if ssh.get("has_password")
                        else ""),
            FieldConfig(id="ssh_private_key", label="Private key", type="textarea", rows=4,
                        span=2, autocomplete="off",
                        placeholder="(stored — leave empty to keep)" if ssh.get("has_key")
                        else "-----BEGIN OPENSSH PRIVATE KEY-----"),
            FieldConfig(id="ssh_passphrase", label="Key passphrase", type="password", span=2,
                        autocomplete="new-password",
                        placeholder="(stored — leave empty to keep)" if ssh.get("has_passphrase")
                        else "(none)"),
            FieldConfig(id="ssh_host_key", label="SSH host key", type="static",
                        value=ssh.get("host_key") or "",
                        placeholder="Not pinned yet: the first connection pins it"),
            FieldConfig(id="ssh_forget", label="Forget the pinned key", type="toggle",
                        help="Only if the server was rebuilt: a changed key can also mean "
                             "someone is in the middle."),
        ]
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
                    *ssh_fields,
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
        state = {"engine": engine, "learned": None}

        def _apply_ssh() -> None:
            values = form.get_values()
            spec = specs.get(state["engine"]) or {}
            server = bool(spec.get("uses_server"))
            on = server and bool(values.get("ssh_enabled"))
            key = values.get("ssh_auth") == "key"
            (form.show_field if server else form.hide_field)("ssh_enabled")
            for field_id, shown in (("ssh_host", on), ("ssh_port", on), ("ssh_username", on),
                                    ("ssh_auth", on), ("ssh_password", on and not key),
                                    ("ssh_private_key", on and key),
                                    ("ssh_passphrase", on and key), ("ssh_host_key", on),
                                    ("ssh_forget", on and bool(ssh.get("host_key")))):
                (form.show_field if shown else form.hide_field)(field_id)

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
            _apply_ssh()

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
            tunnel = {
                "enabled": bool(spec["uses_server"] and values.get("ssh_enabled")),
                "host": (values.get("ssh_host") or "").strip(),
                "port": int(values.get("ssh_port") or 22),
                "username": (values.get("ssh_username") or "").strip(),
                "auth": values.get("ssh_auth") or "password",
            }
            # Secrets only when typed (a single space clears one), as for the password.
            for secret in ("password", "private_key", "passphrase"):
                typed = values.get(f"ssh_{secret}") or ""
                if typed == " ":
                    tunnel[secret] = ""
                elif typed.strip():
                    tunnel[secret] = typed
            if values.get("ssh_forget"):
                tunnel["forget_host_key"] = True
            if state["learned"]:
                tunnel["host_key"] = state["learned"]
            profile["ssh"] = tunnel
            return {**profile, "name": (values.get("name") or "").strip(),
                    "folder": values.get("folder") or "", "env": values.get("env") or "",
                    "timeout_s": int(values.get("timeout_s") or 0),
                    "notes": values.get("notes") or ""}

        def _on_change(payload: dict) -> None:
            if payload.get("id") == "engine" and payload.get("value"):
                _apply_engine(str(payload["value"]))
            elif payload.get("id") in ("ssh_enabled", "ssh_auth"):
                _apply_ssh()
            elif payload.get("id") in ("ssh_host", "ssh_port"):
                state["learned"] = None      # a key seen on another host is not this one's

        async def _test() -> None:
            form.set_button_loading("test", True)
            form.set_values({"test_result": "Connecting…"})
            try:
                p = _collect()
                args = [conn_id or None, p["engine"], p["host"], p["port"], p["database"],
                        p["username"]]
                result = await ConnectionService().test_async(
                    *args, p.get("password", "\x00unchanged\x00"), p["options"], p["read_only"],
                    p["ssh"])
                if result.get("ok"):
                    text = (f"✔ Connected — server {result.get('version')}"
                            f" in {result.get('ms')} ms")
                    if result.get("ssh"):
                        text += " · through SSH"
                        if result.get("ssh_new_key"):
                            state["learned"] = result.get("ssh_host_key") or None
                            form.set_values({"ssh_host_key": f"{result.get('ssh_fingerprint')} "
                                                             "(new: pinned when you save)"})
                    form.set_values({"test_result": text})
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
                    p["env"], p["notes"], p["ssh"])
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
                      placeholder: str = "", label: str = "SQL editor", **extra):
        proxies = {name: create_proxy(fn) for name, fn in (callbacks or {}).items()}
        self._proxies.extend(proxies.values())
        options = {"value": value, "dialect": dialect, "readOnly": read_only,
                   "placeholder": placeholder, "label": label, **extra, **proxies}
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
        for key in ("vp", "grid", "cols_table", "idx_table", "fk_table", "con_table", "pager",
                    "plan_grid"):
            widget = tab.get(key)
            if widget is not None:
                try:
                    widget.destroy()
                except Exception:  # noqa: BLE001 - already torn down with its tab
                    pass
        for key in ("editor", "ddl_editor", "def_editor", "cmp_editor"):
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

    def _open_table(self, loc: tuple, name: str, kind: str = "table", sub: str = "data",
                    where: str | None = None) -> str:
        """Open (or bring forward) a table's tab; ``where`` replaces its filter."""
        for tid, tab in self._tabs.items():
            if tab["kind"] == "table" and tab["loc"] == loc and tab["name"] == name:
                self.tabs.set_active(tid)
                self._show_sub(tid, sub)
                if where is not None:
                    _el(f"{tid}-where").value = where
                    self._apply_filter(tid)
                return tid
        conn = self._conn(loc[0]) or {}
        icon = "mdi-table" if kind == "table" else "mdi-eye-outline"
        tid, container = self._new_tab(name, icon)
        container.innerHTML = self._table_html(tid, conn, loc, name, icon)
        tab = {
            "tid": tid, "kind": "table", "conn": loc[0], "db": loc[1], "loc": loc, "name": name,
            "object_kind": kind, "engine": conn.get("engine") or "", "info": None,
            "page": 1, "page_size": 100, "total": None, "exact": False,
            "where": where or "", "order_by": "", "columns": [], "rows": [], "keys": [],
            "truncated": set(), "pending": {}, "new_counter": 0, "tdir": "asc",
            "sub": "data", "grid": None, "pager": None, "busy": False, "menu_col": None,
            "vp": None, "focus_cell": None, "filters": [], "layout": None, "layout_gen": 0,
        }
        self._tabs[tid] = tab
        if where:
            _el(f"{tid}-where").value = where
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
      <button type="button" class="bk-btn" data-bk="clear" title="Clear the filter, column filters and order">
        <span class="mdi mdi-filter-remove-outline"></span></button>
    </div>
    <div class="bk-chips" id="{tid}-chips" hidden></div>
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
      <button type="button" class="bk-btn" data-bk="count" data-compact="1" title="Count every matching row">
        <span class="mdi mdi-counter"></span><span>Count</span></button>
      <button type="button" class="bk-btn" data-bk="export_csv" data-compact="1" title="Export every matching row as CSV">
        <span class="mdi mdi-file-delimited-outline"></span><span>CSV</span></button>
      <button type="button" class="bk-btn" data-bk="export_json" data-compact="1" title="Export every matching row as JSON">
        <span class="mdi mdi-code-json"></span><span>JSON</span></button>
      <button type="button" class="bk-btn" data-bk="to_sql" data-compact="1" title="Open this query in a SQL editor">
        <span class="mdi mdi-console-line"></span><span>SQL</span></button>
      <button type="button" class="bk-btn" data-bk="value_panel" data-compact="1"
        title="Show the focused cell's whole value (F7; Shift+Enter on a cell)">
        <span class="mdi mdi-dock-right"></span><span>Value</span></button>
      <span class="bk-bar-sep"></span>
      <button type="button" class="bk-btn" data-bk="col_filter" data-compact="1" title="Filter by a column">
        <span class="mdi mdi-filter-plus-outline"></span><span>Filter</span></button>
      <button type="button" class="bk-btn" data-bk="columns" data-compact="1" title="Show, hide and freeze columns">
        <span class="mdi mdi-view-column-outline"></span><span>Columns</span></button>
      <button type="button" class="bk-btn" data-bk="freeze" id="{tid}-freeze" aria-pressed="false"
        title="Freeze the first column while scrolling sideways">
        <span class="mdi mdi-pin-outline"></span></button>
      <button type="button" class="bk-btn" data-bk="import_csv" data-compact="1" data-ddl-bar="1" hidden
        title="Import rows from a CSV file">
        <span class="mdi mdi-file-import-outline"></span><span>Import</span></button>
      <span class="bk-spacer"></span>
      <span class="bk-note" id="{tid}-note"></span>
      <div class="bk-pager" id="{tid}-pager"></div>
    </div>
    <div class="bk-status" id="{tid}-status" hidden></div>
    <div class="bk-results"><div class="bk-panel bk-vp-grid" id="{tid}-grid"></div>
      <div class="bk-vp" id="{tid}-vp" hidden></div></div>
    <div class="bk-foot"><code id="{tid}-sql"></code></div>
  </div>
  <div class="bk-sub" data-sub="columns" hidden>
    <div class="bk-bar" data-ddl-bar="1" hidden>
      <button type="button" class="bk-btn" data-bk="col_add" title="Add a column to the table">
        <span class="mdi mdi-table-column-plus-after"></span><span>Add column</span></button>
      <button type="button" class="bk-btn" data-bk="col_rename" title="Rename the selected column">
        <span class="mdi mdi-rename-box"></span><span>Rename</span></button>
      <button type="button" class="bk-btn" data-bk="col_drop" title="Drop the selected column">
        <span class="mdi mdi-table-column-remove"></span><span>Drop</span></button>
    </div>
    <div class="bk-results"><div class="bk-panel" id="{tid}-cols"></div></div>
  </div>
  <div class="bk-sub bk-scroll" data-sub="keys" hidden>
    <h3 class="bk-h3"><span class="mdi mdi-sort-ascending"></span>Indexes
      <span class="bk-h3-tools" data-ddl-bar="1" hidden>
        <button type="button" class="bk-btn bk-mini" data-bk="ix_create">
          <span class="mdi mdi-plus"></span><span>Create index</span></button>
        <button type="button" class="bk-btn bk-mini" data-bk="ix_drop" title="Drop the selected index">
          <span class="mdi mdi-delete-outline"></span><span>Drop</span></button>
      </span></h3>
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
        # Column and index actions: any table on a writable connection.
        for bar in root.querySelectorAll("[data-ddl-bar]"):
            bar.hidden = info["kind"] != "table" or bool(info.get("read_only"))
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
        saved = await UiStateService().get_async(self._layout_key(tab))
        if tid not in self._tabs:
            return
        if saved.get("ok") and isinstance(saved.get("value"), dict):
            tab["layout"] = saved["value"]
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
                conn_id, db, schema, tab["name"], self._where(tid), tab["order_by"], offset,
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
        columns = self._grid_columns(tid)
        rows = [self._grid_row(tid, r, values) for r, values in enumerate(tab["rows"])]
        first = tab["grid"] is None
        if first:
            tab["grid"] = self._make_grid(tid, columns)
        else:
            tab["grid"].set_columns(columns)
        self._set_frozen(tid, frozen_count((tab.get("layout") or {}).get("frozen")))
        tab["grid"].set_rows(rows)
        tab["focus_cell"] = None
        if tab.get("vp") is not None:
            tab["vp"].reset()
        elif first and (self._settings.get("value_panel") or {}).get("open"):
            self._vp(tid).open()

    def _grid_columns(self, tid: str) -> list:
        tab = self._tabs[tid]
        info = tab["info"] or {}
        meta = {col["name"]: col for col in info.get("columns") or []}
        fk_columns = {name for fk in info.get("foreign_keys") or [] for name in fk["columns"]}
        editable = bool(info.get("editable"))
        index = {name: i for i, name in enumerate(tab["columns"])}
        columns = []
        # The saved layout (order, widths, hidden), per table and user.
        for name, width, hidden in apply_layout(tab["columns"], tab.get("layout")):
            i = index[name]
            col = meta.get(name) or {}
            css = []
            if col.get("pk"):
                css.append("bk-pkcol")
            if name in fk_columns:
                css.append("bk-fkcol")
            if tab["kinds"][i] == "num":
                css.append("bk-num")
            columns.append(ColumnConfig(
                id=f"c{i}", header=name, width=width or _column_width(name),
                align="right" if tab["kinds"][i] == "num" else None,
                sort_by=_RANK, editable=editable and not col.get("computed"),
                cell_class_by=_CLS + f"c{i}", css=" ".join(css) or None, hidden=hidden,
            ))
        return columns

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
            TableAction("value", "View / edit value  (Shift+Enter)", "mdi-text-box-search-outline"),
            TableAction("copy", "Copy value", "mdi-content-copy"),
            TableAction("copy_row", "Copy row as JSON", "mdi-code-json"),
            TableAction("filter_eq", "Filter: column = value", "mdi-filter-outline"),
            TableAction("filter_ne", "Filter: column <> value", "mdi-filter-minus-outline"),
        ]
        actions += [
            TableAction("filter_col", "Filter column…", "mdi-filter-plus-outline"),
            TableAction("fk_open", "Open the referenced row", "mdi-link-variant"),
            TableAction(separator=True),
            TableAction("copy_insert", "Copy as INSERT", "mdi-database-import-outline"),
            TableAction("copy_md", "Copy as Markdown", "mdi-language-markdown-outline"),
            TableAction("hide_col", "Hide this column", "mdi-eye-off-outline"),
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
        grid.on_columns(lambda payload: self._on_columns(tid, payload))
        self._watch_cells(tid, _el(f"{tid}-grid"))
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
        panel = self._tabs[tid].get("vp")
        if not pending and panel is not None and panel.dirty:
            return await message.confirm(
                "Discard the change in the value panel that has not been applied?",
                title="Unapplied change", ok_text="Discard", danger=True)
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
            tab["filters"] = []
            self._draw_chips(tid)
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
            self._export_table(tab["loc"], tab["name"], self._where(tid), tab["order_by"],
                               "csv" if action == "export_csv" else "json")
        elif action == "to_sql":
            self._table_to_sql(tid)
        elif action == "value_panel":
            self._vp_toggle(tid)
        elif action == "copy_ddl":
            _spawn(self._copy_ddl(tid), "copy ddl")
        elif action == "ddl_sql":
            info = tab.get("info") or {}
            self._open_sql(tab["conn"], tab["db"], info.get("ddl") or "")
        elif action in ("col_add", "col_rename", "col_drop", "ix_create", "ix_drop"):
            _spawn(self._tab_ddl(tid, action), action)
        elif action in ("col_filter", "columns", "freeze", "unfilter", "clear_filters"):
            self._grid_tool(tid, action, button)
        elif action == "import_csv":
            _spawn(self._import_dialog(tab["loc"], tab["name"]), "import")

    async def _refresh_table(self, tid: str) -> None:
        if await self._discard_ok(tid):
            await self._browse(tid, with_count=True)

    async def _count_rows(self, tid: str) -> None:
        tab = self._tabs[tid]
        conn_id, db, schema = tab["loc"]
        result = await SqlService().count_async(conn_id, db, schema, tab["name"], self._where(tid))
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
        where = self._where(tid)
        if where:
            text += f"\nWHERE {where}"
        if tab["order_by"]:
            text += f"\nORDER BY {tab['order_by']}"
        self._open_sql(tab["conn"], tab["db"], text + ";\n")

    def _export_table(self, loc: tuple, name: str, where: str, order_by: str, fmt: str) -> None:
        conn_id, db, schema = loc
        query = urlencode({"table": name, "database": db, "schema": schema, "where": where,
                           "order_by": order_by, "format": fmt})
        _download(f"/bk/export/{conn_id}?{query}")
        self._toast(f"Exporting {name} as {fmt.upper()}…")

    # -- column layout, column filters, copy as, foreign keys (milestone 2) --
    # A table's layout (order, widths, hidden, frozen) is saved per table and
    # user; a query result's columns are not (its shape changes with the SQL),
    # but its frozen count is a setting. A table's column filters are WHERE
    # conditions run by the server; a result's filter the fetched rows.

    @staticmethod
    def _layout_key(tab: dict) -> str:
        conn_id, db, schema = tab["loc"]
        return f"columns:{conn_id}:{db}:{schema}:{tab['name']}"

    def _shown_entry(self, tid: str) -> dict | None:
        tab = self._tabs[tid]
        shown = tab.get("shown")
        results = tab.get("results") or []
        return results[shown] if isinstance(shown, int) and 0 <= shown < len(results) else None

    def _column_names(self, tid: str) -> list:
        """The shown grid's column names, by column index (ids are "c<i>")."""
        tab = self._tabs[tid]
        if tab["kind"] == "table":
            return tab["columns"]
        entry = self._shown_entry(tid)
        return entry["columns"] if entry else []

    def _column_kinds(self, tid: str) -> list:
        tab = self._tabs[tid]
        if tab["kind"] == "table":
            return tab.get("kinds") or []
        entry = self._shown_entry(tid)
        return entry["kinds"] if entry else []

    def _engine_of(self, tid: str) -> str:
        return (self._conn(self._tabs[tid]["conn"]) or {}).get("engine") or ""

    def _on_columns(self, tid: str, payload: dict) -> None:
        """A column was resized, moved, hidden or shown: remember it (saved after a pause)."""
        tab = self._tabs.get(tid)
        if tab is None:
            return
        names = tab["columns"]
        state = []
        for col in payload.get("columns") or []:
            cid = str(col.get("id") or "")
            try:
                name = names[int(cid[1:])]
            except (ValueError, IndexError):
                continue
            state.append({"id": name, "width": col.get("width"), "hidden": bool(col.get("hidden"))})
        tab["layout"] = merge_layout(tab.get("layout"), state)
        self._save_layout(tid)

    def _save_layout(self, tid: str) -> None:
        tab = self._tabs[tid]
        tab["layout_gen"] += 1
        _spawn(self._save_layout_later(tid, tab["layout_gen"]), "save column layout")

    async def _save_layout_later(self, tid: str, generation: int) -> None:
        # One save per burst of changes, not one per dragged pixel.
        await asyncio.sleep(0.4)
        tab = self._tabs.get(tid)
        if tab is None or tab["layout_gen"] != generation:
            return
        result = await UiStateService().set_async(self._layout_key(tab), tab["layout"] or {})
        if not result.get("ok"):
            js.console.warn(f"[basilisk] column layout not saved: {result.get('error')}")

    async def _reset_layout(self, tid: str) -> None:
        tab = self._tabs[tid]
        tab["layout"] = None
        tab["layout_gen"] += 1
        result = await UiStateService().clear_async(self._layout_key(tab))
        if not result.get("ok"):
            self._toast(result.get("error", "Could not reset the columns"), kind="error")
            return
        if tab["grid"] is not None:
            tab["grid"].set_columns(self._grid_columns(tid))
            self._set_frozen(tid, 0)
        self._toast("Columns are back to the table's own order and widths.")

    def _set_frozen(self, tid: str, count: int) -> None:
        tab = self._tabs[tid]
        if tab["grid"] is None:
            return
        tab["grid"].set_frozen_columns(count)
        tab["frozen"] = count
        button = _el(f"{tid}-freeze")
        if button:
            button.setAttribute("aria-pressed", "true" if count else "false")

    def _change_frozen(self, tid: str, count: int) -> None:
        tab = self._tabs[tid]
        count = frozen_count(count)
        self._set_frozen(tid, count)
        if tab["kind"] == "table":
            layout = dict(tab.get("layout") or {})
            if count:
                layout["frozen"] = count
            else:
                layout.pop("frozen", None)
            tab["layout"] = layout
            self._save_layout(tid)
        else:
            self._settings["freeze_results"] = count
            _spawn(UiStateService().set_async("settings", self._settings), "settings")

    def _visible_columns(self, tid: str) -> list:
        grid = self._tabs[tid]["grid"]
        return [col for col in grid.get_column_state() if not col.get("hidden")]

    def _hide_column(self, tid: str, column: str) -> None:
        tab = self._tabs[tid]
        if tab["grid"] is None:
            return
        if len(self._visible_columns(tid)) <= 1:
            self._toast("At least one column stays shown.", kind="warning")
            return
        tab["grid"].hide_column(column)
        self._toast("Column hidden; Columns shows it again.")

    def _grid_tool(self, tid: str, action: str, button) -> None:
        tab = self._tabs[tid]
        if tab["grid"] is None:
            self._toast("Nothing is shown yet.", kind="warning")
            return
        if action == "col_filter":
            self._filter_dialog(tid)
        elif action == "columns":
            self._columns_dialog(tid)
        elif action == "freeze":
            self._change_frozen(tid, 0 if tab.get("frozen") else 1)
        elif action == "unfilter":
            filters = list(self._filters_of(tab))
            try:
                filters.pop(int(_attr(button, "data-i")))
            except (ValueError, IndexError):
                return
            _spawn(self._change_filters(tid, filters), "filter")
        elif action == "clear_filters":
            _spawn(self._change_filters(tid, []), "filter")

    @staticmethod
    def _filters_of(tab: dict) -> list:
        return tab.get("filters" if tab["kind"] == "table" else "rfilters") or []

    def _where(self, tid: str) -> str:
        """A table's typed WHERE with its column filters, as the server runs it."""
        tab = self._tabs[tid]
        kinds = {col["name"]: kind_for_type(col["type"])
                 for col in (tab.get("info") or {}).get("columns") or []}
        return where_with_filters(tab["engine"], tab["where"], tab.get("filters") or [], kinds)

    async def _change_filters(self, tid: str, filters: list) -> None:
        tab = self._tabs.get(tid)
        if tab is None:
            return
        if tab["kind"] == "table":
            if not await self._discard_ok(tid):
                return
            tab["filters"] = filters
            self._draw_chips(tid)
            tab["page"] = 1
            tab["pager"].set_page(1)
            await self._browse(tid, with_count=True)
            return
        entry = self._shown_entry(tid)
        tab["rfilters"] = filters
        self._draw_chips(tid)
        if entry is None or tab["grid"] is None:
            return
        tab["grid"].set_rows(self._result_rows(tid, entry))
        self._result_summary(tid)
        tab["focus_cell"] = None
        if tab.get("vp") is not None:
            tab["vp"].reset()

    def _draw_chips(self, tid: str) -> None:
        tab = self._tabs[tid]
        host = _el(f"{tid}-chips")
        if not host:
            return
        filters = self._filters_of(tab)
        if tab["kind"] != "table" and self._shown_entry(tid) is None:
            host.hidden = True
            return
        host.hidden = not filters
        chips = "".join(
            f'<span class="bk-chip"><span class="mdi mdi-filter-outline"></span>'
            f'<span class="bk-chip-text">{_esc(filter_label(flt))}</span>'
            f'<button type="button" class="bk-chip-x" data-bk="unfilter" data-i="{i}" '
            f'title="Remove this filter" aria-label="Remove this filter">'
            f'<span class="mdi mdi-close"></span></button></span>'
            for i, flt in enumerate(filters))
        if len(filters) > 1:
            chips += ('<button type="button" class="bk-btn bk-mini" data-bk="clear_filters">'
                      'Clear filters</button>')
        host.innerHTML = chips

    def _small_modal(self, title: str, inner: str, width: int, height: int, handlers: dict):
        """A dialog whose body events go to ``handlers`` ({event name: callable(event)})."""
        modal = ModalWindow(ModalConfig(dispose_on_close=True, title=title, width=width,
                                        height=height))
        modal.body.innerHTML = inner
        for event_name, handler in handlers.items():
            proxy = create_proxy(handler)
            self._proxies.append(proxy)
            modal.body.addEventListener(event_name, proxy)
        modal.show()
        return modal

    def _filter_dialog(self, tid: str, column: str | None = None, value=None) -> None:
        """Add a column filter: column, operator, value."""
        tab = self._tabs[tid]
        names = self._column_names(tid)
        if not names:
            return
        from_cell = column is not None    # the cell menu: start from that cell's value
        column = column if column in names else names[0]
        op = ("null" if value is None else "eq") if from_cell else "contains"
        options = "".join(f'<option value="{_esc(name)}"{" selected" if name == column else ""}>'
                          f'{_esc(name)}</option>' for name in names)
        ops = "".join(f'<option value="{key}"{" selected" if key == op else ""}>{_esc(label)}</option>'
                      for key, label in FILTER_OPS)
        where = ("Filters every row of the table (added to WHERE)."
                 if tab["kind"] == "table" else "Filters the rows this result fetched.")
        inner = (
            '<div class="bk-dialog bk-ddl">'
            f'<div class="bk-hint">{_esc(where)}</div>'
            '<div class="bk-ddl-fields bk-filter-fields">'
            + self._field("Column", f'<select class="bk-input" data-f="column">{options}</select>')
            + self._field("Is", f'<select class="bk-input" data-f="op">{ops}</select>')
            + self._field("Value", '<input class="bk-input" data-f="value" spellcheck="false" '
                          f'autocomplete="off" value="{_esc("" if value is None else value)}">')
            + '</div><div class="bk-hint" data-f="error"></div>'
            '<div class="bk-actions">'
            '<button type="button" class="bk-btn" data-answer="no">Cancel</button>'
            '<button type="button" class="bk-btn bk-primary" data-answer="yes">'
            '<span class="mdi mdi-filter-plus-outline"></span>Add filter</button></div></div>')
        holder: dict = {}

        def _field(key: str):
            return holder["modal"].body.querySelector(f'[data-f="{key}"]')

        def _sync(_event=None) -> None:
            no_value = str(_field("op").value) in NO_VALUE_OPS
            _field("value").disabled = no_value

        def _add() -> None:
            flt = {"column": str(_field("column").value), "op": str(_field("op").value)}
            if flt["op"] not in NO_VALUE_OPS:
                flt["value"] = str(_field("value").value)
                if flt["value"] == "" and flt["op"] not in ("eq", "ne"):
                    error = _field("error")
                    error.textContent = "Type a value, or choose is NULL."
                    error.setAttribute("data-kind", "error")
                    return
            holder["modal"].close()
            _spawn(self._change_filters(tid, self._filters_of(tab) + [flt]), "filter")

        def _on_click(event) -> None:
            which = event.target.closest("[data-answer]")
            if not which:
                return
            if _attr(which, "data-answer") == "yes":
                _add()
            else:
                holder["modal"].close()

        def _on_key(event) -> None:
            if str(event.key) == "Enter" and _attr(event.target, "data-f") == "value":
                event.preventDefault()
                _add()

        holder["modal"] = self._small_modal("Filter by a column", inner, 560, 330, {
            "click": _on_click, "change": _sync, "keydown": _on_key})
        _sync()
        target = _field("value") if not _field("value").disabled else _field("op")
        target.focus()
        if hasattr(target, "select"):
            target.select()

    def _columns_dialog(self, tid: str) -> None:
        """Show / hide columns, how many to freeze, and (tables) reset the saved layout."""
        tab = self._tabs[tid]
        grid = tab["grid"]
        names = self._column_names(tid)
        rows = []
        for col in grid.get_column_state():
            cid = str(col.get("id") or "")
            try:
                name = names[int(cid[1:])]
            except (ValueError, IndexError):
                continue
            rows.append(
                f'<label class="bk-check bk-col-check"><input type="checkbox" data-col="{_esc(cid)}"'
                f'{"" if col.get("hidden") else " checked"}><span>{_esc(name)}</span></label>')
        freeze = "".join(f'<option value="{n}"{" selected" if n == tab.get("frozen", 0) else ""}>'
                         f'{"None" if n == 0 else n}</option>' for n in range(MAX_FROZEN + 1))
        table = tab["kind"] == "table"
        hint = ("Saved for this table: order and widths too (drag a header to move it, its "
                "edge to resize)." if table else
                "For this result only; the frozen count is remembered.")
        inner = (
            '<div class="bk-dialog bk-ddl">'
            f'<div class="bk-hint">{_esc(hint)}</div>'
            f'<div class="bk-col-list">{"".join(rows)}</div>'
            '<div class="bk-actions">'
            f'<label class="bk-inline-label">Freeze the first <select class="bk-input '
            f'bk-mini-select" data-c="freeze">{freeze}</select> columns</label>'
            '<span class="bk-spacer"></span>'
            '<button type="button" class="bk-btn" data-c="all">Show all</button>'
            + ('<button type="button" class="bk-btn" data-c="reset">Reset layout</button>'
               if table else '')
            + '<button type="button" class="bk-btn bk-primary" data-c="close">Close</button>'
            '</div></div>')
        holder: dict = {}

        def _on_change(event) -> None:
            target = event.target
            if _attr(target, "data-c") == "freeze":
                self._change_frozen(tid, int(target.value or 0))
                return
            cid = _attr(target, "data-col")
            if not cid or tid not in self._tabs:
                return
            if not target.checked and len(self._visible_columns(tid)) <= 1:
                target.checked = True
                self._toast("At least one column stays shown.", kind="warning")
                return
            grid.set_column_hidden(cid, not target.checked)

        def _on_click(event) -> None:
            which = event.target.closest("[data-c]")
            if not which or str(which.tagName).lower() != "button":
                return
            what = _attr(which, "data-c")
            if what == "close":
                holder["modal"].close()
            elif what == "all":
                for box in holder["modal"].body.querySelectorAll("[data-col]"):
                    if not box.checked:
                        box.checked = True
                        grid.set_column_hidden(_attr(box, "data-col"), False)
            elif what == "reset":
                holder["modal"].close()
                _spawn(self._reset_layout(tid), "reset columns")

        height = min(620, 220 + 30 * ((len(rows) + 2) // 3))
        holder["modal"] = self._small_modal("Columns", inner, 640, height, {
            "change": _on_change, "click": _on_click})

    async def _copy_rows(self, tid: str, payload: dict, action: str) -> None:
        """The clicked row, or the selection it is part of, as INSERTs or a Markdown table."""
        tab = self._tabs[tid]
        grid = tab["grid"]
        row_id = str(payload.get("id") or "")
        selected = [str(item) for item in (payload.get("selected") or [])]
        ids = selected if row_id in selected else [row_id]
        visible = [str(col["id"]) for col in self._visible_columns(tid)]
        names_all, kinds_all = self._column_names(tid), self._column_kinds(tid)
        index = [int(cid[1:]) for cid in visible]
        names = [names_all[i] for i in index]
        kinds = [kinds_all[i] if i < len(kinds_all) else "text" for i in index]
        entry = self._shown_entry(tid) if tab["kind"] != "table" else None
        cut_cells = {(int(r), int(c)) for r, c in (entry or {}).get("truncated") or []}
        rows, cut = [], 0
        for rid in ids:
            live = grid.get_row(rid) or {}
            values = []
            for cid, i in zip(visible, index):
                value = live.get(cid)
                if tab["kind"] == "table":
                    value = await self._full_text(tid, rid, cid, value)
                elif (self._index_of(rid), i) in cut_cells:
                    cut += 1
                values.append(value)
            rows.append(values)
        engine = self._engine_of(tid)
        note = ""
        if action == "copy_md":
            text, what = markdown_table(names, rows, kinds), "a Markdown table"
        else:
            if tab["kind"] == "table":
                target = (tab.get("info") or {}).get("qualified") or tab["name"]
            else:
                target = table_of_query(entry["sql"] if entry else "") or "table_name"
                if target == "table_name":
                    note = " The query reads no single table: replace table_name."
            text, what = insert_sql(engine, target, names, rows, kinds), "INSERT statements"
        if cut:
            note += f" {cut} value{'s were' if cut != 1 else ' was'} cut short in the result."
        if await _copy(text):
            plural = "s" if len(rows) != 1 else ""
            self._toast(f"Copied {len(rows)} row{plural} as {what}.{note}",
                        kind="warning" if cut else "success")
        else:
            self._toast("The browser refused the clipboard.", kind="error")

    def _reference_loc(self, tid: str, ref_schema: str) -> tuple:
        """Where a foreign key's referenced table lives, from this table's location."""
        tab = self._tabs[tid]
        conn = self._conn(tab["conn"]) or {}
        levels = conn.get("levels") or []
        conn_id, db, schema = tab["loc"]
        if conn.get("engine") == "mysql":
            return (conn_id, ref_schema or db, "")
        if "schema" in levels:
            return (conn_id, db, ref_schema or schema)
        return (conn_id, db, schema)

    def _open_fk_row(self, tid: str, column: str, row: dict) -> None:
        """Open the row a foreign-key cell points at: its table, filtered to that key."""
        tab = self._tabs[tid]
        fk = foreign_key_for((tab.get("info") or {}).get("foreign_keys") or [], column)
        if fk is None:
            self._toast(f"{column} is not part of a foreign key.", kind="warning")
            return
        values = {name: row.get(f"c{i}") for i, name in enumerate(tab["columns"])}
        kinds = {name: tab["kinds"][i] for i, name in enumerate(tab["columns"])}
        where = reference_where(tab["engine"], fk, values, kinds)
        if where is None:
            self._toast("The key is NULL: this row references nothing.", kind="warning")
            return
        self._open_table(self._reference_loc(tid, fk.get("ref_schema") or ""), fk["ref_table"],
                         "table", where=where)

    # -- the value panel (ui/value_panel.py) ---------------------------------

    def _watch_cells(self, tid: str, host) -> None:
        """
        Which cell has focus (the value panel follows it), Shift+Enter on a
        cell, and which cell was right-clicked: the context menu's payload has
        the row but not the column.
        """
        tab = self._tabs[tid]

        def _cell_of(event):
            target = event.target
            cell = target.closest("td[data-column-id]") if hasattr(target, "closest") else None
            row = cell.closest("tr[data-row-id]") if cell else None
            if not row:
                return None, None
            return _attr(row, "data-row-id"), _attr(cell, "data-column-id")

        def _focus(event) -> None:
            row_id, column = _cell_of(event)
            if not row_id or tab.get("focus_cell") == (row_id, column):
                return
            tab["focus_cell"] = (row_id, column)
            panel = tab.get("vp")
            if panel is not None and panel.is_open:
                _spawn(self._vp_follow(tid, row_id, column), "value panel")

        def _menu(event) -> None:
            _row, column = _cell_of(event)
            tab["menu_col"] = column

        def _key(event) -> None:
            if str(event.key) != "Enter" or not event.shiftKey or event.ctrlKey or event.metaKey:
                return
            if str(event.target.tagName or "").lower() != "td":
                return  # typing in a cell editor
            row_id, column = _cell_of(event)
            if row_id:
                event.preventDefault()
                event.stopPropagation()
                self._vp_open(tid, row_id, column, focus=True)

        for name, handler in (("focusin", _focus), ("click", _focus), ("contextmenu", _menu),
                              ("keydown", _key)):
            proxy = create_proxy(handler)
            self._proxies.append(proxy)
            host.addEventListener(name, proxy, True)

    def _vp(self, tid: str):
        """The tab's value panel, made on first use."""
        tab = self._tabs.get(tid)
        if tab is None:
            return None
        if tab.get("vp") is None:
            host = _el(f"{tid}-vp")
            if host is None:
                return None
            prefs = self._settings.get("value_panel") or {}
            tab["vp"] = ValuePanel(self, host.parentElement, host,
                                   width=int(prefs.get("width") or 420),
                                   on_width=lambda width: self._vp_remember(width=width),
                                   on_close=lambda: self._vp_remember(open=False),
                                   spawn=_spawn)
            if tab["kind"] == "sql":
                shown = tab.get("shown")
                tab["vp"].suppress(not (shown is not None and 0 <= shown < len(tab["results"])))
        return tab["vp"]

    def _vp_remember(self, **changes) -> None:
        """Open or closed and the width are remembered per user, for the next tab."""
        prefs = dict(self._settings.get("value_panel") or {})
        if all(prefs.get(key) == value for key, value in changes.items()):
            return
        prefs.update(changes)
        self._settings["value_panel"] = prefs
        _spawn(UiStateService().set_async("settings", self._settings), "settings")

    def _vp_toggle(self, tid: str) -> None:
        panel = self._vp(tid)
        if panel is None:
            return
        if panel.is_open:
            panel.close()
        else:
            self._vp_open(tid)

    def _vp_open(self, tid: str, row_id: str | None = None, column: str | None = None,
                 focus: bool = False) -> None:
        panel = self._vp(tid)
        if panel is None:
            return
        if not panel.is_open:
            panel.open()
            self._vp_remember(open=True)
        tab = self._tabs[tid]
        if row_id is None:
            row_id, column = tab.get("focus_cell") or (None, None)
        else:
            tab["focus_cell"] = (row_id, column)
        if row_id and column:
            _spawn(self._vp_follow(tid, row_id, column, focus=focus), "value panel")

    async def _vp_follow(self, tid: str, row_id: str, column: str, focus: bool = False) -> None:
        tab = self._tabs.get(tid)
        panel = tab.get("vp") if tab else None
        if panel is None or not panel.is_open or tab["grid"] is None:
            return
        ctx = (self._vp_table_ctx(tid, row_id, column) if tab["kind"] == "table"
               else self._vp_result_ctx(tid, row_id, column))
        if ctx is None:
            return
        if await panel.show(ctx) and focus:
            panel.focus()

    def _vp_refresh(self, tid: str, row_id: str, column: str) -> None:
        """A cell changed outside the panel (grid edit, Set NULL): show its new value."""
        panel = self._tabs[tid].get("vp")
        if (panel is not None and panel.is_open and panel.current_key() == (row_id, column)
                and not panel.dirty):
            _spawn(self._vp_follow(tid, row_id, column), "value panel")

    def _row_key(self, tid: str, row_id: str) -> dict | None:
        tab = self._tabs[tid]
        pending = tab["pending"].get(row_id)
        if pending and pending.get("key"):
            return pending["key"]
        index = self._index_of(row_id)
        pk = (tab["info"] or {}).get("pk") or []
        if index is None or not pk or not tab["keys"]:
            return None
        return dict(zip(pk, tab["keys"][index]))

    def _vp_table_ctx(self, tid: str, row_id: str, column: str) -> dict | None:
        tab = self._tabs[tid]
        try:
            c = int(column[1:])
            name = tab["columns"][c]
        except (ValueError, IndexError):
            return None
        row = tab["grid"].get_row(row_id)
        if not row:
            return None
        info = tab["info"] or {}
        meta = next((col for col in info.get("columns") or [] if col["name"] == name), {})
        type_name = str(meta.get("type") or "")
        pending = tab["pending"].get(row_id)
        changed = pending is not None and name in pending["values"]
        index = self._index_of(row_id)
        if changed:
            text, cut = pending["values"][name], False
        else:
            text = row.get(column)
            cut = index is not None and (index, c) in tab["truncated"]
        key = self._row_key(tid, row_id) if index is not None else None
        conn_id, db, schema = tab["loc"]
        binary = tab["kinds"][c] == "bin" or _binary_type(type_name)
        deleted = pending is not None and pending["op"] == "delete"
        editable = bool(info.get("editable")) and not meta.get("computed") and not deleted
        if info.get("read_only"):
            reason = "Read-only connection."
        elif info.get("kind") == "view":
            reason = "Views are read-only here."
        elif not info.get("pk"):
            reason = "Read-only: the table has no primary key to find the row by."
        elif meta.get("computed"):
            reason = "A computed column cannot be edited."
        elif deleted:
            reason = "The row is marked for deletion."
        else:
            reason = ""
        download = None
        if key and not changed:
            download = f"/bk/cell/{conn_id}?" + urlencode({
                "table": tab["name"], "column": name, "key": json.dumps(key),
                "database": db, "schema": schema})
        load = None
        if cut and key:
            def load():
                return SqlService().cell_value_async(conn_id, db, schema, tab["name"], key, name)
        return {
            "key": (row_id, column), "column": name, "type": type_name,
            "pk": bool(meta.get("pk")), "nullable": meta.get("nullable"),
            "binary": binary, "json": tab["kinds"][c] == "json" or "JSON" in type_name.upper(),
            "text": text, "cut": cut, "load": load,
            "cut_note": "Only the start of the value is shown: without a primary key the row "
                        "cannot be read again.",
            "editable": editable, "reason": reason,
            "apply": lambda new, old: self._vp_apply(tid, row_id, column, new, old),
            "download": download, "filename": f"{tab['name']}-{name}",
        }

    def _vp_result_ctx(self, tid: str, row_id: str, column: str) -> dict | None:
        tab = self._tabs[tid]
        shown = tab.get("shown")
        if shown is None or not 0 <= shown < len(tab["results"]):
            return None
        entry = tab["results"][shown]
        try:
            c = int(column[1:])
            name = entry["columns"][c]
            r = int(row_id[1:])
        except (ValueError, IndexError, TypeError):
            return None
        row = tab["grid"].get_row(row_id)
        if not row:
            return None
        cut = any(int(cr) == r and int(cc) == c for cr, cc in entry.get("truncated") or [])
        return {
            "key": (row_id, column), "column": name, "type": "", "pk": False, "nullable": None,
            "binary": entry["kinds"][c] == "bin", "json": entry["kinds"][c] == "json",
            "text": row.get(column), "cut": cut, "load": None,
            "cut_note": "A query result keeps only the start of long values; open the table "
                        "to see the whole value.",
            "editable": False, "reason": "", "apply": None, "download": None,
            "filename": f"result-{shown + 1}-{name}",
        }

    def _vp_apply(self, tid: str, row_id: str, column: str, new, old) -> bool:
        """The panel's Apply: a pending edit like any other, shown cut in the grid if long."""
        tab = self._tabs.get(tid)
        if tab is None or tab["grid"] is None:
            return False
        pending = self._pending_for(tid, row_id)
        if pending["op"] == "delete":
            return False
        name = tab["columns"][int(column[1:])]
        if name not in pending["orig"]:
            pending["orig"][name] = old
        tab["grid"].set_cell(row_id, column, grid_text(new))
        self._set_pending_value(tid, row_id, column, new, from_panel=True)
        return True

    def _too_long(self, tid: str, row_id: str, c: int) -> bool:
        """The grid has only the start of this cell's value, so it cannot be edited there."""
        tab = self._tabs[tid]
        name = tab["columns"][c]
        pending = tab["pending"].get(row_id)
        if pending is not None and name in pending["values"]:
            value = pending["values"][name]
            return isinstance(value, str) and len(value) > MAX_CELL_CHARS
        index = self._index_of(row_id)
        return index is not None and (index, c) in tab["truncated"]

    async def _full_text(self, tid: str, row_id: str, column: str, shown):
        """A cell's whole value as grid text, fetched when the grid has it cut."""
        tab = self._tabs[tid]
        try:
            c = int(column[1:])
        except ValueError:
            return shown
        name = tab["columns"][c]
        pending = tab["pending"].get(row_id)
        if pending is not None and name in pending["values"]:
            return pending["values"][name]
        index = self._index_of(row_id)
        key = self._row_key(tid, row_id)
        if index is None or (index, c) not in tab["truncated"] or not key:
            return shown
        conn_id, db, schema = tab["loc"]
        result = await SqlService().cell_value_async(conn_id, db, schema, tab["name"], key, name)
        if not result.get("ok") or result.get("too_large"):
            self._toast(result.get("error") or "The value is too large to copy; save it to a "
                        "file from the value panel.", kind="warning")
            return shown
        if result.get("null"):
            return None
        if result.get("binary"):
            return "0x" + base64.b64decode(result.get("b64") or "").hex()
        return result.get("text")

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
        if self._too_long(tid, row_id, c):
            tab["grid"].set_cell(row_id, column, old)
            tab["grid"].set_cell_error(row_id, column,
                                       "Too long to edit in the grid; use the value panel (Shift+Enter)")
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

    def _set_pending_value(self, tid: str, row_id: str, column: str, value,
                           from_panel: bool = False) -> None:
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
        elif isinstance(value, str) and len(value) > MAX_CELL_CHARS:
            classes.append("bk-cut")
        self._cell_class(tid, row_id, column, " ".join(classes))
        if pending["op"] == "update" and not pending["values"]:
            tab["pending"].pop(row_id, None)
            self._row_class(tid, row_id, "")
        elif pending["op"] == "update":
            self._row_class(tid, row_id, "bk-changed")
        self._sync_save(tid)
        if not from_panel:
            self._vp_refresh(tid, row_id, column)

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
                if isinstance(value, str) and len(value) > MAX_CELL_CHARS:
                    row[f"c{c}"] = grid_text(value)
                    row[_CLS + f"c{c}"] = "bk-dirty bk-cut"
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
            changed = (tab["pending"].get(row_id) or {}).get("values") or {}
            if index is not None and any((index, c) in tab["truncated"] and name not in changed
                                         for c, name in enumerate(tab["columns"])):
                self._toast("A row with a value too long for the grid cannot be duplicated here.",
                            kind="warning")
                continue
            values = self._current_values(tid, row_id)
            for c, name in enumerate(tab["columns"]):
                if name in changed:
                    values[c] = changed[name]
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
        if action == "value":
            self._vp_open(tid, row_id, column, focus=True)
        elif action == "copy":
            value = await self._full_text(tid, row_id, column, value)
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
        elif action == "filter_col":
            self._filter_dialog(tid, name, value)
        elif action == "fk_open":
            self._open_fk_row(tid, name, row)
        elif action in ("copy_insert", "copy_md"):
            await self._copy_rows(tid, payload, action)
        elif action == "hide_col":
            self._hide_column(tid, column)
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
                           ok_text: str = "Run", danger: bool = False) -> bool:
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
            f'<button type="button" class="bk-btn {"bk-danger" if prod or danger else "bk-primary"}" '
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
            selection="single", empty_text="No indexes"), container=_el(f"{tid}-idx"))

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
        row = payload.get("row") or {}
        self._open_table(self._reference_loc(tid, row.get("schema") or ""), row.get("table"),
                         "table")

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
    # Object actions: create / rename / drop / truncate a table, its
    # columns and indexes. Each shows its SQL (sqlops.alter, dry run)
    # before it runs; drops and truncates get the red confirm.
    # ------------------------------------------------------------------

    def _writable(self, conn_id: int) -> bool:
        conn = self._conn(conn_id) or {}
        if conn.get("read_only"):
            self._toast("This connection is read-only.", kind="warning")
            return False
        return True

    def _tabs_of(self, loc: tuple, name: str) -> list:
        return [tid for tid, tab in self._tabs.items()
                if tab["kind"] == "table" and tab["loc"] == loc and tab["name"] == name]

    def _edits_pending(self, loc: tuple, name: str) -> bool:
        if any(self._tabs[tid].get("pending") for tid in self._tabs_of(loc, name)):
            self._toast(f"{name} has unsaved row changes: save or revert them first.",
                        kind="warning")
            return True
        return False

    def _type_list(self, conn: dict) -> str:
        options = "".join(f'<option value="{_esc(t)}">' for t in conn.get("types") or [])
        return f'<datalist id="bk-types-{conn.get("id")}">{options}</datalist>'

    async def _ddl_dialog(self, loc: tuple, table: str, action: str, title: str, fields: str,
                          *, collect=None, on_click=None, ok_text: str = "Run",
                          height: int = 460) -> dict | None:
        """
        A form for one object action with its SQL previewed as you type
        (a dry run of sqlops.alter). Resolves to the run's result, or None
        when cancelled.
        """
        conn_id, db, schema = loc
        conn = self._conn(conn_id) or {}
        prod = conn.get("env") == "prod"
        loop = asyncio.get_event_loop()
        answer = loop.create_future()
        modal = ModalWindow(ModalConfig(dispose_on_close=True, title=title, width=720,
                                        height=height))
        warn = ('<div class="bk-warn"><span class="mdi mdi-alert-octagon-outline"></span> '
                "This is a <b>production</b> connection.</div>") if prod else ""
        modal.body.innerHTML = (
            '<div class="bk-dialog bk-ddl">' + warn + self._type_list(conn) +
            f'<div class="bk-ddl-fields">{fields}</div>'
            '<div class="bk-hint" data-ddl="note">…</div>'
            '<pre class="bk-pre bk-ddl-sql" data-ddl="sql"></pre>'
            '<div class="bk-actions">'
            '<button type="button" class="bk-btn" data-answer="no">Cancel</button>'
            f'<button type="button" class="bk-btn {"bk-danger" if prod else "bk-primary"}" '
            f'data-answer="yes" disabled>{_esc(ok_text)}</button></div></div>')
        body = modal.body
        state = {"params": None, "timer": None, "seq": 0, "busy": False, "order": {}}
        note, sql_box = body.querySelector('[data-ddl="note"]'), body.querySelector('[data-ddl="sql"]')
        run = body.querySelector('[data-answer="yes"]')

        def _collect() -> dict:
            values: dict = {}
            for el in body.querySelectorAll("[data-p]"):
                key = _attr(el, "data-p")
                if _attr(el, "data-multi"):
                    continue
                values[key] = bool(el.checked) if str(el.type) == "checkbox" else str(el.value)
            for key, picked in state["order"].items():
                values[key] = list(picked)
            if collect is not None:
                values.update(collect(body))
            return values

        async def _preview() -> None:
            state["seq"] += 1
            seq = state["seq"]
            params = _collect()
            result = await SqlService().alter_async(conn_id, db, schema, table, action, params,
                                                    True)
            if seq != state["seq"] or answer.done():
                return
            if result.get("ok"):
                state["params"] = params
                sql_box.textContent = "\n\n".join(result["sql"])
                note.textContent = result.get("note") or ""
                note.removeAttribute("data-kind")
                run.disabled = bool(result.get("read_only"))
            else:
                state["params"] = None
                sql_box.textContent = ""
                note.textContent = result.get("error", "Not ready")
                note.setAttribute("data-kind", "error")
                run.disabled = True

        def _fire() -> None:
            _spawn(_preview(), "ddl preview")

        fire = create_proxy(_fire)
        self._proxies.append(fire)

        def _schedule(_event=None) -> None:
            if state["timer"]:
                js.clearTimeout(state["timer"])
            state["timer"] = js.setTimeout(fire, 250)

        def _on_change(event) -> None:
            target = event.target
            if _attr(target, "data-multi"):
                picked = state["order"].setdefault(_attr(target, "data-p"), [])
                value = _attr(target, "data-v")
                if target.checked and value not in picked:
                    picked.append(value)
                elif not target.checked and value in picked:
                    picked.remove(value)
                for el in body.querySelectorAll("[data-order-of]"):
                    at = picked.index(_attr(el, "data-order-of")) + 1 \
                        if _attr(el, "data-order-of") in picked else 0
                    el.textContent = str(at) if at else ""
            # Text and checkboxes already scheduled a preview on "input".

        async def _run() -> None:
            if state["busy"]:
                return
            state["busy"] = True
            # Preview what the fields say now, so what runs is what is shown.
            if state["timer"]:
                js.clearTimeout(state["timer"])
                state["timer"] = None
            await _preview()
            if state["params"] is None:
                state["busy"] = False
                return
            run.disabled = True
            note.textContent = "Running…"
            result = await SqlService().alter_async(conn_id, db, schema, table, action,
                                                    state["params"], False)
            state["busy"] = False
            if result.get("ok"):
                if not answer.done():
                    answer.set_result(result)
                modal.close()
                return
            note.textContent = result.get("error", "Failed")
            note.setAttribute("data-kind", "error")
            run.disabled = False

        def _on_click(event) -> None:
            which = event.target.closest("[data-answer]")
            if which:
                if _attr(which, "data-answer") == "yes":
                    _spawn(_run(), "ddl run")
                elif not answer.done():
                    answer.set_result(None)
                    modal.close()
                return
            if on_click is not None and on_click(event, body):
                _schedule()

        def _on_close(_payload) -> None:
            if state["timer"]:
                js.clearTimeout(state["timer"])
            if not answer.done():
                answer.set_result(None)

        for event_name, handler in (("click", _on_click), ("input", _schedule),
                                    ("change", _on_change)):
            proxy = create_proxy(handler)
            self._proxies.append(proxy)
            body.addEventListener(event_name, proxy)
        modal.on_close(_on_close)
        modal.show()
        first = body.querySelector(".bk-ddl-fields input:not([type=checkbox])")
        if first:
            first.focus()
            first.select()
        _fire()
        return await answer

    @staticmethod
    def _field(label: str, inner: str) -> str:
        return f'<label class="bk-ddl-field"><span>{_esc(label)}</span>{inner}</label>'

    def _name_input(self, key: str, value: str = "", placeholder: str = "") -> str:
        return (f'<input class="bk-input" data-p="{key}" value="{_esc(value)}" '
                f'placeholder="{_esc(placeholder)}" spellcheck="false" autocomplete="off">')

    def _column_fields(self, conn: dict) -> str:
        types = f'bk-types-{conn.get("id")}'
        return (self._field("Name", self._name_input("name", "", "new_column"))
                + self._field("Type", f'<input class="bk-input bk-code" data-p="type" '
                              f'list="{types}" spellcheck="false" autocomplete="off" '
                              f'value="{_esc((conn.get("types") or [""])[0])}">')
                + self._field("Default", '<input class="bk-input bk-code" data-p="default" '
                              'placeholder="none (an SQL expression: 0, &#39;text&#39;, now())" '
                              'spellcheck="false" autocomplete="off">')
                + '<label class="bk-check"><input type="checkbox" data-p="not_null"> NOT NULL'
                  "</label>")

    async def _after_table_change(self, loc: tuple, name: str, sub: str = "columns") -> None:
        """Reopen a table's tabs so they show its new columns and indexes."""
        for tid in self._tabs_of(loc, name):
            was = self._tabs[tid]["sub"]
            kind = self._tabs[tid]["object_kind"]
            self._close_tab(tid)
            self._open_table(loc, name, kind, was or sub)

    # ------------------------------------------------------------------
    # ER diagram (milestone 3): ui/er_layout.py lays out and draws the
    # SVG; this tab pans, zooms, drags tables (their places are remembered
    # per schema), highlights relations and exports a light SVG.
    # ------------------------------------------------------------------

    ER_ZOOM = (0.15, 3.0)

    def _open_er(self, data: dict, focus: str | None = None) -> str | None:
        conn = self._conn(data.get("conn")) or {}
        levels = conn.get("levels") or []
        loc = self._loc(data)
        if ("schema" in levels and not loc[2]) or ("database" in levels and not loc[1]):
            self._toast("Choose a " + ("schema" if "schema" in levels else "database")
                        + " in the navigator for its diagram.", kind="warning")
            return None
        for tid, tab in self._tabs.items():
            if tab["kind"] == "er" and tab["loc"] == loc and tab["focus"] == focus:
                self.tabs.set_active(tid)
                return tid
        where = ".".join(part for part in loc[1:] if part) or conn.get("name", "")
        title = f"ER · {focus}" if focus else f"ER · {where}"
        tid, container = self._new_tab(title, "mdi-graph-outline")
        modes = "".join(
            f'<button type="button" class="bk-seg-btn" data-bk="er_mode" data-mode="{key}" '
            f'aria-pressed="{"true" if key == "all" else "false"}">{label}</button>'
            for key, label in (("all", "All columns"), ("keys", "Keys"), ("none", "Names")))
        container.innerHTML = f"""
<div class="bk-view" data-tab="{tid}">
  <div class="bk-head">{self._crumbs(conn, loc[1], loc[2], focus or "ER diagram",
                                     "mdi-table" if focus else "mdi-graph-outline")}
    <span class="bk-head-stats" id="{tid}-stats"></span></div>
  <div class="bk-bar">
    <span class="bk-seg" role="group" aria-label="Columns shown">{modes}</span>
    <span class="bk-bar-sep"></span>
    <input class="bk-input bk-er-find" data-role="er_find" id="{tid}-find" spellcheck="false"
      autocomplete="off" placeholder="Find a table" aria-label="Find a table">
    <span class="bk-bar-sep"></span>
    <button type="button" class="bk-btn" data-bk="er_zoom" data-z="-1" title="Zoom out">
      <span class="mdi mdi-magnify-minus-outline"></span></button>
    <span class="bk-note bk-er-zoom" id="{tid}-zoom">100%</span>
    <button type="button" class="bk-btn" data-bk="er_zoom" data-z="1" title="Zoom in">
      <span class="mdi mdi-magnify-plus-outline"></span></button>
    <button type="button" class="bk-btn" data-bk="er_fit" title="Fit the diagram to the window">
      <span class="mdi mdi-fit-to-screen-outline"></span><span>Fit</span></button>
    <button type="button" class="bk-btn" data-bk="er_layout" title="Lay the tables out again (forgets moved ones)">
      <span class="mdi mdi-auto-fix"></span><span>Re-layout</span></button>
    <button type="button" class="bk-btn" data-bk="er_all" {"" if focus else "hidden"}
      title="The diagram of every table here">
      <span class="mdi mdi-graph-outline"></span><span>Whole schema</span></button>
    <span class="bk-spacer"></span>
    <button type="button" class="bk-btn" data-bk="er_refresh" title="Read the tables again">
      <span class="mdi mdi-refresh"></span></button>
    <button type="button" class="bk-btn" data-bk="er_svg" title="Save the diagram as an SVG picture">
      <span class="mdi mdi-download-outline"></span><span>SVG</span></button>
  </div>
  <div class="bk-status" id="{tid}-status" hidden></div>
  <div class="bk-er-host" id="{tid}-er" tabindex="0"><div class="bk-vp-empty">Reading the tables…</div></div>
  <div class="bk-foot"><code>Drag a table to move it · drag the background to pan · the wheel zooms ·
    click a table to pick out its relations · double-click to open it</code></div>
</div>"""
        self._tabs[tid] = {
            "tid": tid, "kind": "er", "conn": loc[0], "db": loc[1], "loc": loc, "focus": focus,
            "name": focus or "ER diagram", "model": None, "tables": {}, "edges": [],
            "mode": "all", "boxes": {}, "zoom": 1.0, "px": 40.0, "py": 40.0, "drag": None,
            "picked": None, "saved_gen": 0,
        }
        self._er_events(tid)
        self.tabs.set_active(tid)
        _spawn(self._load_er(tid), "er diagram")
        return tid

    def _er_key(self, tab: dict) -> str:
        conn_id, db, schema = tab["loc"]
        return f"er:{conn_id}:{db}:{schema}"

    async def _load_er(self, tid: str, relayout: bool = False) -> None:
        tab = self._tabs.get(tid)
        if tab is None:
            return
        conn_id, db, schema = tab["loc"]
        result = await SqlService().er_model_async(conn_id, db, schema)
        if tid not in self._tabs:
            return
        if not result.get("ok"):
            self._status(tid, result.get("error", "Could not read the tables"))
            return
        self._status(tid, "Only the first 400 tables are drawn." if result.get("truncated")
                     else "", kind="warning")
        saved = {}
        if not tab["focus"]:
            state = await UiStateService().get_async(self._er_key(tab))
            if state.get("ok") and isinstance(state.get("value"), dict):
                saved = state["value"]
                if saved.get("mode") in ER_MODES:
                    tab["mode"] = saved["mode"]
        tab["model"] = result
        tab["tables"] = er_nodes(result, tab["focus"])
        tab["edges"] = er_edges(tab["tables"])
        boxes = er_layout_boxes(tab["tables"], tab["mode"])
        if not relayout:
            for key, place in (saved.get("positions") or {}).items():
                if key in boxes and isinstance(place, list) and len(place) == 2:
                    boxes[key] = {**boxes[key], "x": float(place[0]), "y": float(place[1])}
        tab["boxes"] = boxes
        count = len([t for t in tab["tables"].values() if not t.get("ghost")])
        _el(f"{tid}-stats").textContent = (
            f"{count} table{'s' if count != 1 else ''} · {len(tab['edges'])} foreign key"
            f"{'s' if len(tab['edges']) != 1 else ''}")
        self._er_mode_buttons(tid)
        self._er_draw(tid)
        self._er_fit(tid)

    def _er_mode_buttons(self, tid: str) -> None:
        root = js.document.querySelector(f'.bk-view[data-tab="{tid}"]')
        for button in root.querySelectorAll('[data-bk="er_mode"]'):
            button.setAttribute("aria-pressed", "true" if _attr(button, "data-mode")
                                == self._tabs[tid]["mode"] else "false")

    def _er_draw(self, tid: str) -> None:
        tab = self._tabs[tid]
        host = _el(f"{tid}-er")
        if not tab["tables"]:
            host.innerHTML = '<div class="bk-vp-empty">No tables here.</div>'
            return
        host.innerHTML = er_render(tab["tables"], tab["mode"], tab["boxes"])
        self._er_transform(tid)
        if tab["picked"] in tab["tables"]:
            self._er_pick(tid, tab["picked"])

    def _er_transform(self, tid: str) -> None:
        tab = self._tabs[tid]
        root = js.document.querySelector(f"#{tid}-er .bk-er-root")
        if root:
            root.setAttribute("transform", f"translate({tab['px']:.1f},{tab['py']:.1f}) "
                                           f"scale({tab['zoom']:.4f})")
        _el(f"{tid}-zoom").textContent = f"{round(tab['zoom'] * 100)}%"

    def _er_fit(self, tid: str) -> None:
        tab = self._tabs[tid]
        host = _el(f"{tid}-er")
        x0, y0, x1, y1 = er_bounds(tab["boxes"])
        width, height = float(host.clientWidth or 800), float(host.clientHeight or 500)
        if x1 <= x0 or y1 <= y0:
            return
        zoom = min(1.25, (width - 60) / (x1 - x0), (height - 60) / (y1 - y0))
        tab["zoom"] = max(self.ER_ZOOM[0], zoom)
        tab["px"] = (width - (x1 - x0) * tab["zoom"]) / 2 - x0 * tab["zoom"]
        tab["py"] = max(20.0, (height - (y1 - y0) * tab["zoom"]) / 2) - y0 * tab["zoom"]
        self._er_transform(tid)

    def _er_zoom(self, tid: str, factor: float, cx: float | None = None,
                 cy: float | None = None) -> None:
        tab = self._tabs[tid]
        host = _el(f"{tid}-er")
        if cx is None:
            cx, cy = float(host.clientWidth) / 2, float(host.clientHeight) / 2
        zoom = min(self.ER_ZOOM[1], max(self.ER_ZOOM[0], tab["zoom"] * factor))
        # Keep the point under the cursor where it is.
        tab["px"] = cx - (cx - tab["px"]) * zoom / tab["zoom"]
        tab["py"] = cy - (cy - tab["py"]) * zoom / tab["zoom"]
        tab["zoom"] = zoom
        self._er_transform(tid)

    def _er_events(self, tid: str) -> None:
        host = _el(f"{tid}-er")

        def local(event) -> tuple:
            rect = host.getBoundingClientRect()
            return float(event.clientX) - float(rect.left), float(event.clientY) - float(rect.top)

        def table_of(event):
            node = event.target.closest(".bk-er-table") if hasattr(event.target, "closest") else None
            return _attr(node, "data-t") if node else ""

        def on_down(event) -> None:
            tab = self._tabs.get(tid)
            if tab is None or int(event.button) != 0:
                return
            key = table_of(event)
            x, y = local(event)
            box = tab["boxes"].get(key) if key else None
            tab["drag"] = {"key": key, "x": x, "y": y, "moved": False,
                           "bx": box["x"] if box else 0, "by": box["y"] if box else 0,
                           "px": tab["px"], "py": tab["py"]}
            host.setPointerCapture(event.pointerId)
            host.focus()
            event.preventDefault()

        def on_move(event) -> None:
            tab = self._tabs.get(tid)
            drag = tab and tab["drag"]
            if not drag:
                return
            x, y = local(event)
            dx, dy = x - drag["x"], y - drag["y"]
            if not drag["moved"] and abs(dx) + abs(dy) < 4:
                return
            drag["moved"] = True
            if drag["key"]:
                self._er_move(tid, drag["key"], drag["bx"] + dx / tab["zoom"],
                              drag["by"] + dy / tab["zoom"])
            else:
                tab["px"], tab["py"] = drag["px"] + dx, drag["py"] + dy
                self._er_transform(tid)
                host.classList.add("bk-er-panning")

        def on_up(event) -> None:
            tab = self._tabs.get(tid)
            drag = tab and tab["drag"]
            if not drag:
                return
            tab["drag"] = None
            host.classList.remove("bk-er-panning")
            if drag["moved"]:
                if drag["key"]:
                    self._er_remember(tid)
                return
            # Pointer capture sends dblclick to the host, not the table:
            # two clicks on one table within 400 ms open it.
            now = float(js.Date.now())
            last = tab.get("last_click") or ("", 0.0)
            tab["last_click"] = (drag["key"], now)
            if drag["key"] and last[0] == drag["key"] and now - last[1] < 400:
                tab["last_click"] = ("", 0.0)
                open_table(drag["key"])
                return
            self._er_pick(tid, drag["key"] or None)

        def on_wheel(event) -> None:
            if tid not in self._tabs:
                return
            event.preventDefault()
            x, y = local(event)
            step = 1.0015 ** (-float(event.deltaY) * (0.05 if int(event.deltaMode) == 0 else 1.6))
            self._er_zoom(tid, max(0.5, min(2.0, step)), x, y)

        def open_table(key: str) -> None:
            tab = self._tabs.get(tid)
            if tab is None:
                return
            table = tab["tables"].get(key) or {}
            if table.get("ghost"):
                self._toast(f"{key} is in another schema.", kind="warning")
                return
            self._open_table(tab["loc"], key, "table")

        def on_over(event) -> None:
            self._er_hover(tid, table_of(event), True)

        def on_out(event) -> None:
            self._er_hover(tid, table_of(event), False)

        for name, handler in (("pointerdown", on_down), ("pointermove", on_move),
                              ("pointerup", on_up), ("pointercancel", on_up),
                              ("pointerover", on_over),
                              ("pointerout", on_out)):
            proxy = create_proxy(handler)
            self._proxies.append(proxy)
            host.addEventListener(name, proxy)
        wheel = create_proxy(on_wheel)
        self._proxies.append(wheel)
        host.addEventListener("wheel", wheel, _jsobj({"passive": False}))

    def _er_move(self, tid: str, key: str, x: float, y: float) -> None:
        """Move one box and redraw the lines that touch it."""
        tab = self._tabs[tid]
        tab["boxes"][key] = {**tab["boxes"][key], "x": round(x), "y": round(y)}
        host = _el(f"{tid}-er")
        group = host.querySelector(f'.bk-er-table[data-t="{_css_attr(key)}"]')
        if group:
            group.setAttribute("transform", f"translate({round(x)},{round(y)})")
        for number, edge in enumerate(tab["edges"]):
            if key in (edge["child"], edge["parent"]):
                path = host.querySelector(f'.bk-er-edge[data-e="{number}"]')
                if path:
                    path.setAttribute("d", er_edge_path(tab["tables"], tab["mode"],
                                                        tab["boxes"], edge))

    def _er_related(self, tab: dict, key: str) -> tuple:
        """The tables and edge numbers that touch ``key``."""
        tables, numbers = {key}, set()
        for number, edge in enumerate(tab["edges"]):
            if key in (edge["child"], edge["parent"]):
                numbers.add(number)
                tables |= {edge["child"], edge["parent"]}
        return tables, numbers

    def _er_hover(self, tid: str, key: str, on: bool) -> None:
        tab = self._tabs.get(tid)
        if tab is None or not key or tab["drag"]:
            return
        host = _el(f"{tid}-er")
        _tables, numbers = self._er_related(tab, key)
        group = host.querySelector(f'.bk-er-table[data-t="{_css_attr(key)}"]')
        if group:
            group.classList.toggle("bk-er-hot", on or tab["picked"] == key)
        for number in numbers:
            path = host.querySelector(f'.bk-er-edge[data-e="{number}"]')
            if path:
                path.classList.toggle("bk-er-hot", on or number in self._er_picked_edges(tab))
                hot = on or number in self._er_picked_edges(tab)
                path.setAttribute("marker-start", path.getAttribute("marker-start")
                                  .replace("-hot)", ")").replace(")", "-hot)" if hot else ")"))
                path.setAttribute("marker-end", "url(#bk-er-one-hot)" if hot
                                  else "url(#bk-er-one)")

    def _er_picked_edges(self, tab: dict) -> set:
        return self._er_related(tab, tab["picked"])[1] if tab["picked"] else set()

    def _er_pick(self, tid: str, key: str | None) -> None:
        """Pick out one table's relations (the rest dims); None clears."""
        tab = self._tabs[tid]
        tab["picked"] = key if key in tab["tables"] else None
        host = _el(f"{tid}-er")
        tables, numbers = self._er_related(tab, tab["picked"]) if tab["picked"] else (set(), set())
        for group in host.querySelectorAll(".bk-er-table"):
            name = _attr(group, "data-t")
            group.classList.toggle("bk-er-dim", bool(tab["picked"]) and name not in tables)
            group.classList.toggle("bk-er-hot", name == tab["picked"])
        for path in host.querySelectorAll(".bk-er-edge"):
            number = int(_attr(path, "data-e") or -1)
            hot = number in numbers
            path.classList.toggle("bk-er-dim", bool(tab["picked"]) and not hot)
            path.classList.toggle("bk-er-hot", hot)
            start = path.getAttribute("marker-start").replace("-hot)", ")")
            path.setAttribute("marker-start", start.replace(")", "-hot)") if hot else start)
            path.setAttribute("marker-end", "url(#bk-er-one-hot)" if hot else "url(#bk-er-one)")

    def _er_find(self, tid: str, text: str) -> None:
        tab = self._tabs[tid]
        needle = text.strip().lower()
        if not needle:
            self._er_pick(tid, None)
            return
        names = sorted(tab["boxes"], key=lambda k: (not k.lower().startswith(needle), len(k), k))
        found = next((k for k in names if needle in k.lower()), None)
        if found is None:
            return
        self._er_pick(tid, found)
        box = tab["boxes"][found]
        host = _el(f"{tid}-er")
        tab["px"] = float(host.clientWidth) / 2 - (box["x"] + box["w"] / 2) * tab["zoom"]
        tab["py"] = float(host.clientHeight) / 2 - (box["y"] + box["h"] / 2) * tab["zoom"]
        self._er_transform(tid)

    def _er_remember(self, tid: str) -> None:
        """Moved tables and the column mode, per schema and user (not for a focused diagram)."""
        tab = self._tabs[tid]
        if tab["focus"]:
            return
        tab["saved_gen"] += 1
        _spawn(self._er_save_later(tid, tab["saved_gen"]), "save diagram")

    async def _er_save_later(self, tid: str, generation: int) -> None:
        await asyncio.sleep(0.5)
        tab = self._tabs.get(tid)
        if tab is None or tab["saved_gen"] != generation:
            return
        value = {"mode": tab["mode"], "positions": {
            key: [box["x"], box["y"]] for key, box in tab["boxes"].items()}}
        result = await UiStateService().set_async(self._er_key(tab), value)
        if not result.get("ok"):
            js.console.warn(f"[basilisk] diagram not saved: {result.get('error')}")

    def _er_action(self, tid: str, action: str, button) -> None:
        tab = self._tabs[tid]
        if action == "er_zoom":
            self._er_zoom(tid, 1.25 if _attr(button, "data-z") == "1" else 0.8)
        elif action == "er_fit":
            self._er_fit(tid)
        elif action == "er_refresh":
            _spawn(self._load_er(tid), "er diagram")
        elif action == "er_all":
            data = {"conn": tab["loc"][0], "db": tab["loc"][1], "schema": tab["loc"][2]}
            self._open_er(data)
        elif not tab["tables"]:
            return
        elif action == "er_mode":
            mode = _attr(button, "data-mode")
            if mode in ER_MODES and mode != tab["mode"]:
                tab["mode"] = mode
                tab["boxes"] = er_resize(tab["tables"], mode, tab["boxes"])
                self._er_mode_buttons(tid)
                self._er_draw(tid)
                self._er_remember(tid)
        elif action == "er_layout":
            tab["boxes"] = er_layout_boxes(tab["tables"], tab["mode"])
            self._er_draw(tid)
            self._er_fit(tid)
            self._er_remember(tid)
        elif action == "er_svg":
            conn = self._conn(tab["conn"]) or {}
            where = ".".join(part for part in tab["loc"][1:] if part) or conn.get("name", "")
            title = f"{where} · {tab['focus']}" if tab["focus"] else where
            svg = er_render(tab["tables"], tab["mode"], tab["boxes"], theme="light",
                            standalone=True, title=title)
            blob = js.Blob.new(to_js([svg]), _jsobj({"type": "image/svg+xml"}))
            url = js.URL.createObjectURL(blob)
            anchor = js.document.createElement("a")
            anchor.href = url
            anchor.download = "-".join(part for part in (where.replace(".", "-"), tab["focus"], "er")
                                       if part) + ".svg"
            js.document.body.appendChild(anchor)
            anchor.click()
            anchor.remove()
            revoke = create_proxy(lambda: js.URL.revokeObjectURL(url))
            self._proxies.append(revoke)
            js.setTimeout(revoke, 10_000)
            self._toast("Saved the diagram as SVG.", kind="success")

    # ------------------------------------------------------------------
    # Schema compare (milestone 3): two schemas side by side, the
    # differences with a tick each, and the script that makes the target
    # like the source (ui/compare_tools.py, services/schema_compare.py).
    # The script opens in a SQL editor on the target; nothing runs here.
    # ------------------------------------------------------------------

    def _open_compare(self, data: dict) -> str | None:
        conn_id = data.get("conn")
        if self._conn(conn_id) is None:
            return None
        loc = self._loc(data)
        source = {"conn": loc[0], "db": loc[1], "schema": loc[2]}
        target = dict(source)
        conn = self._conn(loc[0]) or {}
        where = ".".join(part for part in loc[1:] if part) or conn.get("name", "")
        tid, container = self._new_tab(f"Compare · {where}", "mdi-compare-horizontal")
        sides = "".join(
            f'<div class="bk-cmp-side" data-side="{side}">'
            f'<span class="bk-cmp-label" title="{_esc(hint)}">'
            f'<span class="mdi {icon}"></span>{label}</span>'
            f'<select class="bk-select bk-cmp-conn" data-role="cmp_conn" data-side="{side}" '
            f'aria-label="{label} connection"></select>'
            f'<select class="bk-select" data-role="cmp_db" data-side="{side}" '
            f'aria-label="{label} database" hidden></select>'
            f'<select class="bk-select" data-role="cmp_schema" data-side="{side}" '
            f'aria-label="{label} schema" hidden></select></div>'
            + ('<button type="button" class="bk-btn" data-bk="cmp_swap" '
               'title="Swap the source and the target">'
               '<span class="mdi mdi-swap-horizontal"></span></button>' if side == "src" else "")
            for side, label, icon, hint in (
                ("src", "Source", "mdi-database-export-outline", "The schema to copy from"),
                ("tgt", "Target", "mdi-database-import-outline",
                 "The schema the script changes to look like the source")))
        container.innerHTML = f"""
<div class="bk-view" data-tab="{tid}">
  <div class="bk-head">{self._crumbs(conn, loc[1], loc[2], "Schema compare",
                                     "mdi-compare-horizontal")}
    <span class="bk-head-stats" id="{tid}-stats"></span></div>
  <div class="bk-bar bk-cmp-pick">{sides}
    <button type="button" class="bk-btn bk-primary" data-bk="cmp_run" id="{tid}-run"
      title="Read both schemas and list what differs">
      <span class="mdi mdi-compare-horizontal"></span><span>Compare</span></button>
  </div>
  <div class="bk-status" id="{tid}-status" hidden></div>
  <div class="bk-split" id="{tid}-split"></div>
  <div class="bk-foot"><code id="{tid}-foot">Choose two schemas and Compare. Tick what the
    script should change; drops start unticked. Double-click a difference for its details.</code></div>
</div>"""
        tab = {
            "tid": tid, "kind": "compare", "conn": loc[0], "db": loc[1], "loc": loc,
            "name": "Schema compare", "sides": {"src": source, "tgt": target},
            "lists": {"src": {"dbs": [], "schemas": []}, "tgt": {"dbs": [], "schemas": []}},
            "result": None, "chosen": set(), "grid": None, "cmp_editor": None, "busy": False,
            "labels": ("", ""), "stale": False,
        }
        self._tabs[tid] = tab
        layout = Layout(LayoutConfig(css="bk-split-layout", rows=[
            CellConfig(id=f"{tid}-top", height="52%", resizable=True, min_size="90px"),
            CellConfig(id=f"{tid}-bottom", height="100%", min_size="90px"),
        ]))
        _el(f"{tid}-split").appendChild(layout.layout.getRootElement())
        tab["layout"] = layout
        layout.get_cell(f"{tid}-top").getContainer().innerHTML = f"""
<div class="bk-res">
  <div class="bk-res-bar">
    <span class="bk-cmp-title"><span class="mdi mdi-format-list-checks"></span>Differences</span>
    <button type="button" class="bk-btn" data-bk="cmp_tick" data-which="all" title="Apply every difference">
      <span class="mdi mdi-checkbox-multiple-marked-outline"></span><span>All</span></button>
    <button type="button" class="bk-btn" data-bk="cmp_tick" data-which="safe"
      title="Everything but drops (the first choice)">
      <span class="mdi mdi-shield-check-outline"></span><span>No drops</span></button>
    <button type="button" class="bk-btn" data-bk="cmp_tick" data-which="none" title="Apply nothing">
      <span class="mdi mdi-checkbox-multiple-blank-outline"></span><span>None</span></button>
    <span class="bk-spacer"></span>
    <span class="bk-note" id="{tid}-note"></span>
  </div>
  <div class="bk-results"><div class="bk-panel" id="{tid}-grid"></div></div>
</div>"""
        layout.get_cell(f"{tid}-bottom").getContainer().innerHTML = f"""
<div class="bk-res">
  <div class="bk-res-bar">
    <span class="bk-cmp-title"><span class="mdi mdi-script-text-outline"></span>Script</span>
    <span class="bk-note" id="{tid}-count"></span>
    <span class="bk-spacer"></span>
    <button type="button" class="bk-btn bk-primary" data-bk="cmp_sql"
      title="Open the script in a SQL editor on the target, to read and run it">
      <span class="mdi mdi-console-line"></span><span>Open in SQL editor</span></button>
    <button type="button" class="bk-btn" data-bk="cmp_copy" title="Copy the script">
      <span class="mdi mdi-content-copy"></span><span>Copy</span></button>
    <button type="button" class="bk-btn" data-bk="cmp_save" title="Save the script as a .sql file">
      <span class="mdi mdi-download-outline"></span><span>.sql</span></button>
  </div>
  <div class="bk-results"><div class="bk-panel bk-code-host" id="{tid}-script"></div></div>
</div>"""
        grid = DataTable(DataTableConfig(
            columns=[
                ColumnConfig(id="use", header="Apply", width=66, align="center", editable=True,
                             editor="checkbox", sortable=False),
                ColumnConfig(id="change", header="Change", width=100, icon_by="change_icon"),
                ColumnConfig(id="kind", header="Kind", width=110),
                ColumnConfig(id="object", header="Object", width=230),
                ColumnConfig(id="detail", header="Difference", width=330),
                ColumnConfig(id="source", header="In the source", width=260),
                ColumnConfig(id="target", header="In the target", width=260),
            ],
            id_field="rid", selection="single", resizable_columns=True, row_class_by="cls",
            filterable=True, filter_placeholder="Filter the differences",
            empty_text="Choose the two schemas, then Compare.", loading_text="Comparing…",
            context_actions=[
                TableAction("details", "Details and statements", "mdi-text-box-search-outline"),
                TableAction(separator=True),
                TableAction("tick_table", "Apply everything on this table",
                            "mdi-checkbox-multiple-marked-outline"),
                TableAction("untick_table", "Apply nothing on this table",
                            "mdi-checkbox-multiple-blank-outline"),
            ],
        ), container=_el(f"{tid}-grid"))
        grid.on_edit(lambda payload: self._cmp_on_edit(tid, payload))
        grid.on_select(lambda payload: self._cmp_on_select(tid, payload))
        grid.on_activate(lambda payload: self._cmp_detail(tid, str(payload.get("id") or "")))
        grid.on_action(lambda payload: self._cmp_menu(tid, payload))
        tab["grid"] = grid
        self.tabs.set_active(tid)
        _spawn(self._cmp_start(tid), "schema compare")
        return tid

    async def _cmp_start(self, tid: str) -> None:
        """Fill the source's lists, then pick a target next to it (another schema, if any)."""
        tab = self._tabs.get(tid)
        if tab is None:
            return
        self._cmp_fill(tid, "src")
        self._cmp_fill(tid, "tgt")
        await self._cmp_lists(tid, "src")
        tab = self._tabs.get(tid)
        if tab is None:
            return
        source, target = tab["sides"]["src"], tab["sides"]["tgt"]
        target.update(conn=source["conn"], db=source["db"], schema=source["schema"])
        levels = (self._conn(source["conn"]) or {}).get("levels") or []
        lists = tab["lists"]["src"]
        if "schema" in levels:
            target["schema"] = next((name for name in lists["schemas"]
                                     if name != source["schema"]), source["schema"])
        elif "database" in levels:
            target["db"] = next((name for name in lists["dbs"] if name != source["db"]),
                                source["db"])
        else:
            engine = (self._conn(source["conn"]) or {}).get("engine")
            others = [conn for conn in self._conns if conn["id"] != source["conn"]]
            pick = next((conn for conn in others if conn.get("engine") == engine),
                        others[0] if others else None)
            if pick is not None:
                target.update(conn=pick["id"], db="", schema="")
        await self._cmp_lists(tid, "tgt")

    def _cmp_where(self, pick: dict) -> str:
        conn = self._conn(pick["conn"]) or {}
        return " › ".join(part for part in (conn.get("name", "?"), pick["db"], pick["schema"])
                          if part)

    async def _cmp_lists(self, tid: str, side: str) -> None:
        """The databases and schemas a side can choose from; keeps a valid choice."""
        tab = self._tabs.get(tid)
        if tab is None:
            return
        pick = tab["sides"][side]
        levels = (self._conn(pick["conn"]) or {}).get("levels") or []
        lists = {"dbs": [], "schemas": []}
        if "database" in levels:
            result = await SqlService().databases_async(pick["conn"], False)
            if result.get("ok"):
                rows = result.get("value") or []
                lists["dbs"] = [row["name"] for row in rows]
                if pick["db"] not in lists["dbs"]:
                    pick["db"] = next((row["name"] for row in rows if row.get("current")),
                                      lists["dbs"][0] if lists["dbs"] else "")
            else:
                self._status(tid, result.get("error", "Could not list the databases"))
        if "schema" in levels:
            result = await SqlService().schemas_async(pick["conn"], pick["db"], False)
            if result.get("ok"):
                lists["schemas"] = [row["name"] for row in result.get("schemas") or []]
                if pick["schema"] not in lists["schemas"]:
                    default = result.get("default")
                    pick["schema"] = default if default in lists["schemas"] else (
                        lists["schemas"][0] if lists["schemas"] else "")
            else:
                self._status(tid, result.get("error", "Could not list the schemas"))
        if tid in self._tabs:
            tab["lists"][side] = lists
            self._cmp_fill(tid, side)

    def _cmp_fill(self, tid: str, side: str) -> None:
        tab = self._tabs[tid]
        pick, lists = tab["sides"][side], tab["lists"][side]
        root = _el(f"{tid}-split").closest(".bk-view")
        box = root.querySelector(f'.bk-cmp-side[data-side="{side}"]')
        conn = self._conn(pick["conn"]) or {}
        levels = conn.get("levels") or []

        def options(values: list, chosen, labels=None) -> str:
            return "".join(
                f'<option value="{_esc(value)}"{" selected" if str(value) == str(chosen) else ""}>'
                f'{_esc(labels[i] if labels else value)}</option>' for i, value in enumerate(values))

        conns = sorted(self._conns, key=lambda c: str(c.get("name", "")).lower())
        box.querySelector('[data-role="cmp_conn"]').innerHTML = options(
            [c["id"] for c in conns], pick["conn"],
            [f'{c.get("name", "?")} ({c.get("engine_label", "")})' for c in conns])
        db = box.querySelector('[data-role="cmp_db"]')
        db.hidden = "database" not in levels
        db.innerHTML = options(lists["dbs"] or ([pick["db"]] if pick["db"] else []), pick["db"])
        schema = box.querySelector('[data-role="cmp_schema"]')
        schema.hidden = "schema" not in levels
        schema.innerHTML = options(lists["schemas"] or ([pick["schema"]] if pick["schema"] else []),
                                   pick["schema"])

    async def _cmp_pick(self, tid: str, side: str, role: str, value: str) -> None:
        tab = self._tabs.get(tid)
        if tab is None:
            return
        pick = tab["sides"][side]
        if role == "cmp_conn":
            pick.update(conn=int(value), db="", schema="")
            await self._cmp_lists(tid, side)
        elif role == "cmp_db":
            pick.update(db=value, schema="")
            await self._cmp_lists(tid, side)
        elif role == "cmp_schema":
            pick["schema"] = value
        if tid in self._tabs and tab["result"] is not None:
            tab["stale"] = True
            self._status(tid, "The schemas changed since the last comparison: Compare again.",
                         "info")

    def _cmp_action(self, tid: str, action: str, button) -> None:
        tab = self._tabs[tid]
        if action == "cmp_run":
            _spawn(self._cmp_run(tid), "schema compare")
        elif action == "cmp_swap":
            sides, lists = tab["sides"], tab["lists"]
            sides["src"], sides["tgt"] = sides["tgt"], sides["src"]
            lists["src"], lists["tgt"] = lists["tgt"], lists["src"]
            self._cmp_fill(tid, "src")
            self._cmp_fill(tid, "tgt")
            if tab["result"] is not None:
                _spawn(self._cmp_run(tid), "schema compare")
        elif tab["result"] is None:
            return
        elif action == "cmp_tick":
            diffs = tab["result"]["diffs"]
            which = _attr(button, "data-which")
            chosen = ([d["id"] for d in diffs] if which == "all" else
                      cmp_default_chosen(diffs) if which == "safe" else [])
            self._cmp_set_chosen(tid, set(chosen))
        elif action == "cmp_sql":
            target = tab["sides"]["tgt"]
            conn = self._conn(target["conn"]) or {}
            if conn.get("read_only"):
                self._toast("The target connection is read-only: the script can be read there, "
                            "but its changes are rolled back.", kind="warning")
            self._open_sql(target["conn"], target["db"], text=self._cmp_text(tid))
        elif action == "cmp_copy":
            _spawn(self._copy_text(self._cmp_text(tid), "Copied the script."), "copy")
        elif action == "cmp_save":
            blob = js.Blob.new(to_js([self._cmp_text(tid)]), _jsobj({"type": "application/sql"}))
            url = js.URL.createObjectURL(blob)
            anchor = js.document.createElement("a")
            anchor.href = url
            names = [tab["sides"][side]["schema"] or tab["sides"][side]["db"] or
                     (self._conn(tab["sides"][side]["conn"]) or {}).get("name", side)
                     for side in ("src", "tgt")]
            anchor.download = f"compare-{names[0]}-to-{names[1]}.sql".replace(" ", "_")
            js.document.body.appendChild(anchor)
            anchor.click()
            anchor.remove()
            revoke = create_proxy(lambda: js.URL.revokeObjectURL(url))
            self._proxies.append(revoke)
            js.setTimeout(revoke, 10_000)

    async def _cmp_run(self, tid: str) -> None:
        tab = self._tabs.get(tid)
        if tab is None or tab["busy"]:
            return
        source, target = tab["sides"]["src"], tab["sides"]["tgt"]
        for side, pick in (("source", source), ("target", target)):
            levels = (self._conn(pick["conn"]) or {}).get("levels") or []
            if ("schema" in levels and not pick["schema"]) or ("database" in levels and not pick["db"]):
                self._toast(f"Choose the {side}'s " + ("schema" if "schema" in levels else "database")
                            + ".", kind="warning")
                return
        if (source["conn"], source["db"], source["schema"]) == (target["conn"], target["db"],
                                                                target["schema"]):
            self._toast("The source and the target are the same: choose two different schemas.",
                        kind="warning")
            return
        tab["busy"] = True
        run = _el(f"{tid}-run")
        run.disabled = True
        tab["grid"].set_busy(True)
        self._status(tid, "Reading both schemas…", "info")
        try:
            result = await SqlService().compare_schemas_async(
                source["conn"], source["db"], source["schema"],
                target["conn"], target["db"], target["schema"])
        finally:
            tab["busy"] = False
            if tid in self._tabs:
                run.disabled = False
                tab["grid"].set_busy(False)
        if tid not in self._tabs:
            return
        if not result.get("ok"):
            self._status(tid, result.get("error", "Could not compare"))
            return
        tab["result"] = result
        tab["stale"] = False
        tab["labels"] = (self._cmp_where(source), self._cmp_where(target))
        self._status(tid, "\n".join(result.get("notes") or []), "info")
        _el(f"{tid}-stats").textContent = (
            f"{tab['labels'][0]} → {tab['labels'][1]} · {result.get('ms', 0)} ms")
        _el(f"{tid}-note").textContent = cmp_summary(result)
        tab["grid"].set_empty_text("The two schemas are the same.")
        self._cmp_set_chosen(tid, set(cmp_default_chosen(result["diffs"])), rows=True)

    def _cmp_set_chosen(self, tid: str, chosen: set, rows: bool = False) -> None:
        tab = self._tabs[tid]
        before = tab["chosen"]
        tab["chosen"] = chosen
        diffs = tab["result"]["diffs"]
        if rows:
            tab["grid"].set_rows(cmp_grid_rows(diffs, chosen))
        else:
            for diff in diffs:
                if (diff["id"] in chosen) != (diff["id"] in before):
                    tab["grid"].set_cell(diff["id"], "use", diff["id"] in chosen)
        _spawn(self._cmp_show_script(tid), "compare script")

    def _cmp_text(self, tid: str) -> str:
        tab = self._tabs[tid]
        result = tab["result"]
        if result is None:
            return ""
        source, target = tab["labels"]
        header = [f"Make {target} like {source}",
                  f"Written by Basilisk {VERSION}: read it before running it."]
        header += result.get("notes") or []
        return cmp_script(result, tab["chosen"], header)

    async def _cmp_show_script(self, tid: str) -> None:
        tab = self._tabs.get(tid)
        if tab is None:
            return
        text = self._cmp_text(tid)
        count = cmp_statement_count(tab["result"], tab["chosen"]) if tab["result"] else 0
        total = len(tab["result"]["diffs"]) if tab["result"] else 0
        _el(f"{tid}-count").textContent = (
            f"{count} statement{'s' if count != 1 else ''} · {len(tab['chosen'])} of {total} "
            "differences applied")
        target = self._conn(tab["sides"]["tgt"]["conn"]) or {}
        dialect = target.get("dialect") or "StandardSQL"
        if tab.get("cmp_editor") is not None:
            if tab.get("cmp_dialect") == dialect:
                tab["cmp_editor"].setValue(text)
                return
            # Another engine: highlight in its dialect.
            tab["cmp_editor"].destroy()
            tab["cmp_editor"] = None
        host = _el(f"{tid}-script")
        host.innerHTML = ""
        tab["cmp_dialect"] = dialect
        if await self._ensure_editor():
            if tid not in self._tabs:
                return
            if tab.get("cmp_editor") is not None:
                tab["cmp_editor"].setValue(self._cmp_text(tid))
                return
            tab["cmp_editor"] = self._mount_editor(
                host, value=self._cmp_text(tid), read_only=True, label="Compare script",
                dialect=dialect)
        else:
            host.innerHTML = f'<pre class="bk-pre bk-fill">{_esc(text)}</pre>'

    def _cmp_on_edit(self, tid: str, payload: dict) -> None:
        tab = self._tabs.get(tid)
        if tab is None or payload.get("column") != "use" or tab["result"] is None:
            return
        rid = str(payload.get("id") or "")
        chosen = set(tab["chosen"])
        if payload.get("value"):
            chosen.add(rid)
        else:
            chosen.discard(rid)
        tab["chosen"] = chosen
        _spawn(self._cmp_show_script(tid), "compare script")

    def _cmp_diff(self, tid: str, rid: str) -> dict | None:
        tab = self._tabs.get(tid)
        result = (tab or {}).get("result") or {}
        return next((d for d in result.get("diffs") or [] if d["id"] == rid), None)

    def _cmp_on_select(self, tid: str, payload: dict) -> None:
        diff = self._cmp_diff(tid, str(payload.get("id") or ""))
        foot = _el(f"{tid}-foot")
        if diff is None or not foot:
            return
        parts = [cmp_object_label(diff) + ": " + (diff.get("detail") or "")]
        if diff.get("manual"):
            parts.append("⚠ " + diff["manual"])
        parts += diff.get("items") or []
        foot.textContent = " · ".join(parts)
        foot.title = "\n".join(parts)

    def _cmp_menu(self, tid: str, payload: dict) -> None:
        tab = self._tabs.get(tid)
        diff = self._cmp_diff(tid, str(payload.get("id") or ""))
        if tab is None or diff is None:
            return
        action = payload.get("action")
        if action == "details":
            self._cmp_detail(tid, diff["id"])
        elif action in ("tick_table", "untick_table"):
            same = {d["id"] for d in tab["result"]["diffs"] if d["group"] == diff["group"]}
            chosen = (tab["chosen"] | same) if action == "tick_table" else (tab["chosen"] - same)
            self._cmp_set_chosen(tid, chosen)

    def _cmp_detail(self, tid: str, rid: str) -> None:
        tab = self._tabs.get(tid)
        diff = self._cmp_diff(tid, rid)
        if tab is None or diff is None:
            return
        source, target = tab["labels"]
        facts = [("Difference", diff.get("detail")), ("Source · " + source, diff.get("source")),
                 ("Target · " + target, diff.get("target")), ("By hand", diff.get("manual"))]
        props = "".join(f"<dt>{_esc(label)}</dt><dd>{_esc(value)}</dd>"
                        for label, value in facts if value)
        items = "".join(f"<li>{_esc(item)}</li>" for item in diff.get("items") or [])
        statements = cmp_script({"diffs": [diff], "phases": tab["result"].get("phases"),
                                 "batches": tab["result"].get("batches")}, [diff["id"]])
        modal = ModalWindow(ModalConfig(dispose_on_close=True, width=860, height=600,
                                        title=f"{cmp_object_label(diff)} · "
                                              f"{diff.get('kind', '')}"))
        modal.body.innerHTML = (
            '<div class="bk-dialog bk-fill">'
            f'<dl class="bk-session-facts">{props}</dl>'
            + (f'<ul class="bk-cmp-items">{items}</ul>' if items else "")
            + f'<pre class="bk-pre bk-grow-box">{_esc(statements)}</pre>'
            '<div class="bk-actions"><span class="bk-spacer"></span>'
            '<button type="button" class="bk-btn bk-primary" data-s="close">Close</button>'
            '</div></div>')

        def _on_click(event) -> None:
            if event.target.closest("[data-s]"):
                modal.close()

        proxy = create_proxy(_on_click)
        self._proxies.append(proxy)
        modal.body.addEventListener("click", proxy)
        modal.show()

    # ------------------------------------------------------------------
    # Session manager (milestone 2): what runs on a server now, refreshed
    # every few seconds, with Cancel query and End session. One tab per
    # connection; stopping is refused on read-only profiles (sqlops).
    # ------------------------------------------------------------------

    SESSIONS_REFRESH_S = 5

    def _open_sessions(self, conn_id: int) -> str | None:
        conn = self._conn(conn_id) or {}
        if not conn.get("has_sessions"):
            self._toast(f"{conn.get('engine_label') or 'This database'} has no server sessions "
                        "to list.", kind="warning")
            return None
        for tid, tab in self._tabs.items():
            if tab["kind"] == "sessions" and tab["conn"] == conn_id:
                self.tabs.set_active(tid)
                return tid
        tid, container = self._new_tab(f"Sessions · {conn.get('name', '?')}", "mdi-pulse")
        container.innerHTML = f"""
<div class="bk-view" data-tab="{tid}">
  <div class="bk-head">{self._crumbs(conn, "", "", "Sessions", "mdi-pulse")}
    <span class="bk-head-stats" id="{tid}-stats"></span></div>
  <div class="bk-bar">
    <button type="button" class="bk-btn" data-bk="s_refresh" title="Refresh now">
      <span class="mdi mdi-refresh"></span><span>Refresh</span></button>
    <label class="bk-check"><input type="checkbox" data-role="s_auto" checked>
      <span>Every {self.SESSIONS_REFRESH_S} s</span></label>
    <label class="bk-check"><input type="checkbox" data-role="s_idle">
      <span>Show idle sessions</span></label>
    <span class="bk-bar-sep"></span>
    <button type="button" class="bk-btn" data-bk="s_cancel" id="{tid}-s-cancel"
      title="Stop the selected sessions' statements; the sessions stay">
      <span class="mdi mdi-stop-circle-outline"></span><span>Cancel query</span></button>
    <button type="button" class="bk-btn bk-danger-text" data-bk="s_kill"
      title="End the selected sessions (their open transactions roll back)">
      <span class="mdi mdi-close-octagon-outline"></span><span>End session</span></button>
    <button type="button" class="bk-btn" data-bk="s_sql" title="Open the selected session's statement in a SQL editor">
      <span class="mdi mdi-console-line"></span><span>SQL</span></button>
    <span class="bk-spacer"></span>
    <span class="bk-note" id="{tid}-note"></span>
  </div>
  <div class="bk-status" id="{tid}-status" hidden></div>
  <div class="bk-results"><div class="bk-panel" id="{tid}-grid"></div></div>
  <div class="bk-foot"><code id="{tid}-sql">Double-click a session for its whole statement.</code></div>
</div>"""
        self._tabs[tid] = {
            "tid": tid, "kind": "sessions", "conn": conn_id, "db": "", "loc": (conn_id, "", ""),
            "name": "Sessions", "grid": None, "auto": True, "idle": False, "rows": {},
            "busy": False, "can_cancel": True, "again": False,
        }
        grid = DataTable(DataTableConfig(
            columns=[
                ColumnConfig(id="id", header="Session", width=86, align="right", sort_by="id_n"),
                ColumnConfig(id="user", header="User", width=120),
                ColumnConfig(id="database", header="Database", width=120),
                ColumnConfig(id="client", header="Client", width=130),
                ColumnConfig(id="application", header="Application", width=130),
                ColumnConfig(id="state", header="State", width=150),
                ColumnConfig(id="duration", header="For", width=90, align="right",
                             sort_by="secs_n"),
                ColumnConfig(id="wait", header="Waiting on", width=130),
                ColumnConfig(id="blocked", header="Blocked by", width=96),
                ColumnConfig(id="query", header="Statement", width=520),
            ],
            id_field="rid", selection="multi", resizable_columns=True, reorderable_columns=True,
            frozen_columns=1, row_class_by="cls", empty_text="Nothing is running.",
            loading_text="Loading…",
            context_actions=[
                TableAction("show", "Show the statement", "mdi-text-box-search-outline"),
                TableAction("sql", "Open in a SQL editor", "mdi-console-line"),
                TableAction("copy", "Copy the statement", "mdi-content-copy"),
                TableAction(separator=True),
                TableAction("cancel", "Cancel query", "mdi-stop-circle-outline"),
                TableAction("kill", "End session", "mdi-close-octagon-outline", danger=True),
            ],
        ), container=_el(f"{tid}-grid"))
        grid.on_activate(lambda payload: self._session_dialog(tid, str(payload.get("id") or "")))
        grid.on_action(lambda payload: self._session_menu(tid, payload))
        self._tabs[tid]["grid"] = grid
        self.tabs.set_active(tid)
        _spawn(self._poll_sessions(tid), "sessions")
        return tid

    @staticmethod
    def _duration(seconds) -> str:
        if seconds is None:
            return ""
        seconds = float(seconds)
        if seconds < 60:
            return f"{seconds:.1f} s"
        minutes, secs = divmod(int(seconds), 60)
        if minutes < 60:
            return f"{minutes} min {secs} s"
        hours, minutes = divmod(minutes, 60)
        if hours < 48:
            return f"{hours} h {minutes} min"
        return f"{hours // 24} d {hours % 24} h"

    async def _poll_sessions(self, tid: str) -> None:
        """Refresh now, then every few seconds while the tab is open and auto is on."""
        await self._load_sessions(tid)
        while tid in self._tabs:
            await asyncio.sleep(self.SESSIONS_REFRESH_S)
            tab = self._tabs.get(tid)
            if tab is None:
                return
            visible = str(self.tabs.get_active() or "") == tid
            if tab["auto"] and visible and not tab["busy"]:
                await self._load_sessions(tid)

    async def _load_sessions(self, tid: str) -> None:
        tab = self._tabs.get(tid)
        if tab is None:
            return
        if tab["busy"]:
            tab["again"] = True     # a change while listing: list once more after
            return
        tab["busy"] = True
        tab["again"] = False
        try:
            result = await SqlService().sessions_async(tab["conn"], tab["idle"])
        finally:
            tab["busy"] = False
        if tid not in self._tabs:
            return
        if tab.get("again"):
            _spawn(self._load_sessions(tid), "sessions")
        if not result.get("ok"):
            self._status(tid, result.get("error", "Could not list the sessions"))
            return
        self._status(tid, "")
        tab["can_cancel"] = bool(result.get("can_cancel"))
        cancel = _el(f"{tid}-s-cancel")
        if cancel:
            cancel.hidden = not tab["can_cancel"]
        selected = set(tab["grid"].get_selected_ids())
        rows, tab["rows"] = [], {}
        for row in result["sessions"]:
            rid = f"s{row['id']}"
            tab["rows"][rid] = row
            query = " ".join((row.get("query") or "").split())
            classes = " ".join(cls for cls, on in (
                ("bk-own", row.get("own")), ("bk-blocked", row.get("blocked_by")),
                ("bk-active", str(row.get("state") or "").lower().startswith(
                    ("active", "running", "query", "suspended", "runnable")))) if on)
            rows.append({
                "rid": rid, "id": str(row["id"]) + (" ★" if row.get("own") else ""),
                "id_n": row["id"], "user": row["user"], "database": row["database"],
                "client": row["client"], "application": row["application"],
                "state": row["state"] + (" · in transaction" if row.get("txn")
                                         and "transaction" not in row["state"] else ""),
                "duration": self._duration(row.get("seconds")), "secs_n": row.get("seconds") or 0,
                "wait": row["wait"], "blocked": ", ".join(str(pid) for pid in row["blocked_by"]),
                "query": query[:400], "cls": classes,
            })
        tab["grid"].set_rows(rows)
        keep = [rid for rid in selected if rid in tab["rows"]]
        if keep:
            tab["grid"].select(keep)
        active = sum(1 for row in rows if "bk-active" in row["cls"])
        _el(f"{tid}-stats").textContent = (f"{len(rows)} session{'s' if len(rows) != 1 else ''}"
                                           f" · {active} running")
        _el(f"{tid}-note").textContent = (
            "★ is this browser's own connection · " if any(r.get("own") for r in result["sessions"])
            else "") + f"listed in {result.get('ms', 0)} ms"

    def _sessions_action(self, tid: str, action: str, button) -> None:
        tab = self._tabs[tid]
        ids = tab["grid"].get_selected_ids()
        if action == "s_refresh":
            _spawn(self._load_sessions(tid), "sessions")
        elif action in ("s_cancel", "s_kill"):
            _spawn(self._stop_sessions(tid, ids, "cancel" if action == "s_cancel" else "terminate"),
                   "stop sessions")
        elif action == "s_sql":
            if not ids:
                self._toast("Select a session first.", kind="warning")
                return
            self._session_to_sql(tid, ids[0])

    def _session_menu(self, tid: str, payload: dict) -> None:
        rid = str(payload.get("id") or "")
        selected = [str(item) for item in payload.get("selected") or []]
        ids = selected if rid in selected else [rid]
        action = payload.get("action")
        if action == "show":
            self._session_dialog(tid, rid)
        elif action == "sql":
            self._session_to_sql(tid, rid)
        elif action == "copy":
            row = self._tabs[tid]["rows"].get(rid) or {}
            _spawn(self._copy_text(row.get("query") or "", "Copied the statement."), "copy")
        elif action in ("cancel", "kill"):
            _spawn(self._stop_sessions(tid, ids, "cancel" if action == "cancel" else "terminate"),
                   "stop sessions")

    async def _copy_text(self, text: str, done: str) -> None:
        if await _copy(text):
            self._toast(done, kind="success")

    def _session_to_sql(self, tid: str, rid: str) -> None:
        tab = self._tabs[tid]
        row = tab["rows"].get(rid)
        if not row or not row.get("query"):
            self._toast("That session has no statement to show.", kind="warning")
            return
        conn = self._conn(tab["conn"]) or {}
        db = row.get("database") if "database" in (conn.get("levels") or []) else ""
        self._open_sql(tab["conn"], db or "", row["query"].rstrip() + "\n")

    def _session_dialog(self, tid: str, rid: str) -> None:
        tab = self._tabs.get(tid)
        row = (tab or {}).get("rows", {}).get(rid)
        if not row:
            return
        facts = [("Session", row["id"]), ("User", row["user"]), ("Database", row["database"]),
                 ("Client", row["client"]), ("Application", row["application"]),
                 ("State", row["state"]), ("Since", row.get("started")),
                 ("For", self._duration(row.get("seconds"))), ("Waiting on", row["wait"]),
                 ("Blocked by", ", ".join(str(pid) for pid in row["blocked_by"])),
                 ("In a transaction", "yes" if row.get("txn") else "")]
        props = "".join(f"<dt>{_esc(label)}</dt><dd>{_esc(value)}</dd>"
                        for label, value in facts if value not in (None, ""))
        modal = ModalWindow(ModalConfig(dispose_on_close=True, title=f"Session {row['id']}",
                                        width=820, height=560))
        modal.body.innerHTML = (
            '<div class="bk-dialog bk-fill">'
            f'<dl class="bk-session-facts">{props}</dl>'
            f'<pre class="bk-pre bk-grow-box">{_esc(row.get("query") or "(no statement)")}</pre>'
            '<div class="bk-actions"><button type="button" class="bk-btn" data-s="sql">'
            '<span class="mdi mdi-console-line"></span>Open in a SQL editor</button>'
            '<span class="bk-spacer"></span>'
            '<button type="button" class="bk-btn bk-primary" data-s="close">Close</button></div></div>')

        def _on_click(event) -> None:
            which = event.target.closest("[data-s]")
            if not which:
                return
            modal.close()
            if _attr(which, "data-s") == "sql":
                self._session_to_sql(tid, rid)

        proxy = create_proxy(_on_click)
        self._proxies.append(proxy)
        modal.body.addEventListener("click", proxy)
        modal.show()

    async def _stop_sessions(self, tid: str, ids: list, mode: str) -> None:
        tab = self._tabs.get(tid)
        if tab is None:
            return
        conn = self._conn(tab["conn"]) or {}
        if conn.get("read_only"):
            self._toast("This connection is read-only: it does not stop sessions.", kind="warning")
            return
        if mode == "cancel" and not tab["can_cancel"]:
            self._toast(f"{conn.get('engine_label')} can only end a session.", kind="warning")
            return
        rows = [tab["rows"][rid] for rid in ids if rid in tab["rows"]]
        own = [row for row in rows if row.get("own")]
        rows = [row for row in rows if not row.get("own")]
        if not rows:
            self._toast("Select a session first." if not own else
                        "That is this browser's own connection.", kind="warning")
            return
        verb = "Cancel the statement of" if mode == "cancel" else "End"
        lines = [f"-- session {row['id']} · {row['user'] or '?'} · {row['state']}\n"
                 + (" ".join((row.get("query") or "").split())[:300] or "(no statement)")
                 for row in rows]
        note = (f"{verb} {len(rows)} session{'s' if len(rows) != 1 else ''}?"
                + (" Their open transactions are rolled back." if mode == "terminate" else
                   " The sessions stay; a transaction may be left to roll back."))
        if not await self._confirm_sql(
                "Cancel query" if mode == "cancel" else "End session", lines, note,
                prod=conn.get("env") == "prod", danger=True,
                ok_text="Cancel query" if mode == "cancel" else "End session"):
            return
        failed = []
        for row in rows:
            result = await SqlService().kill_session_async(tab["conn"], row["id"], mode)
            if not result.get("ok"):
                failed.append(f"{row['id']}: {result.get('error', 'failed')}")
        if failed:
            self._toast("Not stopped: " + "; ".join(failed), kind="error")
        else:
            self._toast(f"{'Cancelled' if mode == 'cancel' else 'Ended'} {len(rows)} "
                        f"session{'s' if len(rows) != 1 else ''}.", kind="success")
        await asyncio.sleep(0.3)
        await self._load_sessions(tid)

    # ------------------------------------------------------------------
    # Import CSV (milestone 2): pick a file, map its columns, then upload it
    # to /bk/import/<plan>, which runs services/csv_import.py as a job in
    # one transaction. The console follows the job; closing it does not stop it.
    # ------------------------------------------------------------------

    async def _import_dialog(self, loc: tuple, name: str) -> None:
        conn_id, db, schema = loc
        if not self._writable(conn_id) or self._edits_pending(loc, name):
            return
        info = await SqlService().table_info_async(conn_id, db, schema, name)
        if not info.get("ok"):
            self._toast(info.get("error", "Could not read the table"), kind="error")
            return
        if info["kind"] != "table":
            self._toast(f"{name} is a view; import into a table.", kind="warning")
            return
        conn = self._conn(conn_id) or {}
        prod = conn.get("env") == "prod"
        columns = info["columns"]
        state: dict = {"file": None, "raw": b"", "cut": False, "rows": [], "mapping": {},
                       "busy": False}

        def select(key: str, options, value: str) -> str:
            items = "".join(f'<option value="{_esc(v)}"{" selected" if v == value else ""}>'
                            f'{_esc(label)}</option>' for v, label in options)
            return f'<select class="bk-input" data-i="{key}">{items}</select>'

        warn = ('<div class="bk-warn"><span class="mdi mdi-alert-octagon-outline"></span> '
                "This is a <b>production</b> connection.</div>") if prod else ""
        opts = (
            self._field("Delimiter", select("delimiter", DELIMITERS, ","))
            + self._field("Quote", select("quote", (('"', 'Double "'), ("'", "Single '"),
                                                    ("", "None")), '"'))
            + self._field("Encoding", select("encoding", ENCODINGS, "utf-8"))
            + self._field("NULL marker", '<input class="bk-input bk-code" data-i="null_text" '
                          'placeholder="e.g. NULL or \\N" spellcheck="false" autocomplete="off">')
            + self._field("Existing rows", select("mode", (
                ("append", "Keep, add to them"), ("replace", "Delete them first")), "append"))
            + self._field("A bad value", select("on_error", (
                ("stop", "Stops: import nothing"), ("skip", "Skips its row")), "stop")))
        checks = ('<label class="bk-check"><input type="checkbox" data-i="header" checked>'
                  "<span>First row is the header</span></label>"
                  '<label class="bk-check"><input type="checkbox" data-i="empty" checked>'
                  "<span>Empty fields are NULL</span></label>")
        modal = ModalWindow(ModalConfig(dispose_on_close=True, title=f"Import CSV into {name}",
                                        width=980, height=700))
        body = modal.body
        body.innerHTML = (
            '<div class="bk-dialog bk-import">' + warn +
            '<div class="bk-import-file">'
            '<label class="bk-btn bk-primary"><span class="mdi mdi-file-upload-outline"></span>'
            '<span>Choose a CSV file…</span><input type="file" data-i="file" hidden '
            'accept=".csv,.tsv,.tab,.txt,text/csv,text/plain,text/tab-separated-values"></label>'
            '<span class="bk-note" data-i="fname">No file chosen yet.</span></div>'
            f'<div class="bk-import-opts">{opts}</div>'
            f'<div class="bk-import-checks">{checks}</div>'
            '<div class="bk-import-main">'
            '<div class="bk-import-map" data-i="map"></div>'
            '<div class="bk-import-preview" data-i="preview"></div></div>'
            '<div class="bk-hint" data-i="note">The whole file is imported in one transaction: '
            "if anything fails, the table is left as it was.</div>"
            '<div class="bk-import-progress" data-i="progress" hidden></div>'
            '<div class="bk-actions">'
            '<button type="button" class="bk-btn" data-answer="no">Cancel</button>'
            f'<button type="button" class="bk-btn {"bk-danger" if prod else "bk-primary"}" '
            'data-answer="yes" disabled><span class="mdi mdi-database-import-outline"></span>'
            '<span data-i="go">Import</span></button></div></div>')

        def el(key: str):
            return body.querySelector(f'[data-i="{key}"]')

        def value(key: str) -> str:
            return str(el(key).value)

        def render() -> None:
            rows, header = state["rows"], bool(el("header").checked)
            sources = source_columns(rows, header)
            lines = []
            for col in columns:
                if col.get("computed"):
                    continue
                picked = state["mapping"].get(col["name"])
                options = ['<option value="">— not imported (its default) —</option>'] + [
                    f'<option value="{i}"{" selected" if picked == i else ""}>{_esc(label)}</option>'
                    for i, label in sources]
                flags = []
                if col.get("pk"):
                    flags.append('<span class="mdi mdi-key" title="Primary key"></span>')
                if col.get("autoincrement"):
                    flags.append('<small title="Numbered by the database when not imported">'
                                 "auto</small>")
                needs = (picked is None and not col.get("nullable") and col.get("default") is None
                         and not col.get("autoincrement"))
                if needs:
                    flags.append('<span class="mdi mdi-alert" title="NOT NULL with no default: '
                                 'map it, or every row is refused"></span>')
                lines.append(
                    f'<tr><td class="bk-import-col">{"".join(flags)}<span>{_esc(col["name"])}</span></td>'
                    f'<td class="bk-code bk-import-type" title="{_esc(col["type"])}">{_esc(col["type"])}</td>'
                    f'<td><select class="bk-input" data-map="{_esc(col["name"])}">'
                    f'{"".join(options)}</select></td></tr>')
            el("map").innerHTML = (
                '<table class="bk-import-table"><thead><tr><th>Column</th><th>Type</th>'
                f'<th>From the file</th></tr></thead><tbody>{"".join(lines)}</tbody></table>')
            mapped = [col["name"] for col in columns if col["name"] in state["mapping"]]
            if not rows:
                el("preview").innerHTML = ('<div class="bk-vp-empty">Choose a file to see its first '
                                           "rows here.</div>")
            elif not mapped:
                el("preview").innerHTML = ('<div class="bk-vp-empty">No column of the file is '
                                           "mapped yet.</div>")
            else:
                cells = preview_cells(rows, header, state["mapping"], mapped,
                                      value("null_text"), bool(el("empty").checked))
                head = "".join(f"<th>{_esc(n)}</th>" for n in mapped)
                body_rows = "".join(
                    "<tr>" + "".join(
                        '<td class="bk-null"></td>' if kind == "null" else
                        f'<td title="{_esc(text)}">{_esc(text)}</td>' for kind, text in row) + "</tr>"
                    for row in cells)
                el("preview").innerHTML = (
                    f'<table class="bk-import-table bk-import-rows"><thead><tr>{head}</tr></thead>'
                    f"<tbody>{body_rows}</tbody></table>")
            go = body.querySelector('[data-answer="yes"]')
            go.disabled = state["file"] is None or not state["mapping"] or state["busy"]
            replace = value("mode") == "replace"
            el("go").textContent = "Delete the rows and import" if replace else "Import"
            go.classList.toggle("bk-danger", replace or prod)
            go.classList.toggle("bk-primary", not (replace or prod))

        def reparse(remap: bool) -> None:
            text = decode_sample(state["raw"], value("encoding"), state["cut"])
            state["rows"] = parse_rows(text, value("delimiter"), value("quote"))
            if remap:
                state["mapping"] = auto_map(columns, state["rows"], bool(el("header").checked))
            render()

        async def read_file(chosen) -> None:
            state["file"] = chosen
            size = int(chosen.size)
            buffer = await chosen.slice(0, SAMPLE_BYTES).arrayBuffer()
            state["raw"] = js.Uint8Array.new(buffer).to_bytes()
            state["cut"] = size > SAMPLE_BYTES
            el("fname").textContent = f"{chosen.name} · {format_bytes(size)}"
            el("encoding").value = guess_encoding(state["raw"])
            text = decode_sample(state["raw"], value("encoding"), state["cut"])
            el("delimiter").value = ("\t" if str(chosen.name).lower().endswith((".tsv", ".tab"))
                                     else sniff_delimiter(text))
            reparse(True)

        def on_change(event) -> None:
            target = event.target
            key = _attr(target, "data-i")
            if key == "file":
                files = target.files
                if files and files.length:
                    _spawn(read_file(files.item(0)), "read csv")
            elif _attr(target, "data-map"):
                column = _attr(target, "data-map")
                if str(target.value) == "":
                    state["mapping"].pop(column, None)
                else:
                    state["mapping"][column] = int(target.value)
                render()
            elif key in ("delimiter", "quote", "encoding", "header"):
                reparse(key == "header" or not state["mapping"])
            elif key in ("empty", "null_text", "mode", "on_error"):
                render()

        def on_input(event) -> None:
            if _attr(event.target, "data-i") == "null_text":
                render()

        async def start() -> None:
            note = el("note")
            options = {
                "delimiter": value("delimiter"), "quote": value("quote"),
                "encoding": value("encoding"), "header": bool(el("header").checked),
                "empty": "null" if el("empty").checked else "empty",
                "null_text": value("null_text"), "mode": value("mode"),
                "on_error": value("on_error"),
                "mapping": [[col, index] for col, index in state["mapping"].items()],
            }
            state["busy"] = True
            render()
            try:
                planned = await JobService().plan_import_async(conn_id, db, schema, name, options)
                if not planned.get("ok"):
                    note.textContent = planned.get("error", "Cannot import")
                    note.setAttribute("data-kind", "error")
                    return
                bar = el("progress")
                bar.hidden = False
                size = int(state["file"].size)

                def on_progress(done: int, total: int) -> None:
                    bar.innerHTML = progress_html(done, total or size or 1, label="Uploading",
                                                  text=f"{format_bytes(done)} / "
                                                       f"{format_bytes(total or size)}")

                on_progress(0, size)
                status, payload = await self._upload(f"/bk/import/{planned['plan']}",
                                                     state["file"], on_progress)
                if status != 200 or not payload.get("job"):
                    bar.hidden = True
                    note.textContent = payload.get("detail") or (
                        "The upload failed." if status == 0 else f"The upload failed ({status}).")
                    note.setAttribute("data-kind", "error")
                    return
            finally:
                state["busy"] = False
            modal.close()
            await self._job_console(payload["job"], f"Import into {name}", loc, name)

        def on_click(event) -> None:
            which = event.target.closest("[data-answer]")
            if not which or which.disabled:
                return
            if _attr(which, "data-answer") == "yes":
                if not state["busy"]:
                    _spawn(start(), "import")
            else:
                modal.close()

        for event_name, handler in (("change", on_change), ("input", on_input),
                                    ("click", on_click)):
            proxy = create_proxy(handler)
            self._proxies.append(proxy)
            body.addEventListener(event_name, proxy)
        modal.show()
        render()

    async def _upload(self, url: str, body, on_progress) -> tuple:
        """
        POST a file with upload progress. XMLHttpRequest, not fetch: fetch
        reports nothing while a request body is being sent. Returns
        ``(status, parsed JSON or {})``; status 0 means the network failed.
        """
        future = asyncio.get_event_loop().create_future()
        xhr = js.XMLHttpRequest.new()
        xhr.open("POST", url)
        xhr.withCredentials = True
        xhr.setRequestHeader("X-CSRF-Token", _csrf_token())
        xhr.setRequestHeader("Content-Type", "text/csv")

        def _progress(event) -> None:
            if event.lengthComputable:
                on_progress(int(event.loaded), int(event.total))

        def _settle(*_args) -> None:
            if not future.done():
                future.set_result(None)

        proxies = [create_proxy(_progress), create_proxy(_settle)]
        self._proxies.extend(proxies)
        xhr.upload.addEventListener("progress", proxies[0])
        for event_name in ("load", "error", "abort", "timeout"):
            xhr.addEventListener(event_name, proxies[1])
        xhr.send(body)
        await future
        try:
            payload = json.loads(str(xhr.responseText or "{}"))
        except ValueError:
            payload = {}
        return int(xhr.status), payload if isinstance(payload, dict) else {}

    async def _job_console(self, job_id: str, title: str, loc: tuple, name: str) -> None:
        """Follow an import: its bar, the log, Cancel; then the summary and a refreshed tab."""
        modal = ModalWindow(ModalConfig(dispose_on_close=True, title=title, width=700,
                                        height=480))
        modal.body.innerHTML = (
            '<div class="bk-dialog bk-console">'
            '<div class="bk-console-head"><span class="bk-console-state" data-state="running">'
            '<span class="mdi mdi-progress-clock"></span><span>Running…</span></span>'
            '<span class="bk-note" data-j="elapsed"></span></div>'
            '<div data-j="items"></div>'
            '<pre class="bk-pre bk-console-log" data-j="log"></pre>'
            '<div class="bk-actions">'
            '<button type="button" class="bk-btn bk-danger" data-job="cancel">'
            '<span class="mdi mdi-stop"></span><span>Cancel</span></button>'
            '<button type="button" class="bk-btn" data-job="close">Close</button></div></div>')
        body = modal.body
        items_el, log_el = body.querySelector('[data-j="items"]'), body.querySelector('[data-j="log"]')
        state_el = body.querySelector(".bk-console-state")
        cancel_el = body.querySelector('[data-job="cancel"]')

        def _on_click(event) -> None:
            which = event.target.closest("[data-job]")
            if not which:
                return
            if _attr(which, "data-job") == "close":
                modal.close()
            elif not which.disabled:
                which.disabled = True
                _spawn(JobService().cancel_async(job_id), "cancel job")

        proxy = create_proxy(_on_click)
        self._proxies.append(proxy)
        body.addEventListener("click", proxy)
        modal.show()
        bar_state = {"running": "active", "done": "done", "failed": "error", "cancelled": "paused"}
        seen = 0
        while True:
            status = await JobService().status_async(job_id, seen)
            if not status.get("ok"):
                state_el.setAttribute("data-state", "failed")
                state_el.textContent = status.get("error", "The job is gone")
                return
            seen = status["log_total"]
            bars = []
            for item in status["items"]:
                total, done = item.get("total") or 0, item.get("done") or 0
                bars.append(progress_html(done if total else (0 if item["state"] == "running" else 1),
                                          total or 1, label=item["label"],
                                          text=item.get("note") or format_bytes(done),
                                          state=bar_state.get(item["state"], "active")))
            items_el.innerHTML = "".join(bars) or '<div class="bk-hint">Starting…</div>'
            if status["log"]:
                log_el.textContent = str(log_el.textContent) + "\n".join(status["log"]) + "\n"
                log_el.scrollTop = log_el.scrollHeight
            body.querySelector('[data-j="elapsed"]').textContent = f"{status['elapsed']:.0f} s"
            state = status["state"]
            label = {
                "running": "Cancelling…" if status.get("cancelling") else "Running…",
                "done": "Done", "failed": f"Failed: {status.get('error', '')}",
                "cancelled": "Cancelled: nothing was imported (rolled back)",
            }.get(state, state)
            icon = {"running": "mdi-progress-clock", "done": "mdi-check-circle",
                    "failed": "mdi-alert-circle", "cancelled": "mdi-stop-circle"}.get(state, "mdi-circle")
            state_el.setAttribute("data-state", state)
            state_el.innerHTML = f'<span class="mdi {icon}"></span><span>{_esc(label)}</span>'
            cancel_el.hidden = state != "running"
            if state != "running":
                break
            await asyncio.sleep(0.5)
        if state == "done":
            result = status.get("result") or {}
            skipped = result.get("skipped") or 0
            self._toast(f"Imported {_count(result.get('inserted'))} rows into {name}"
                        + (f"; skipped {_count(skipped)}" if skipped else "") + ".",
                        kind="warning" if skipped else "success")
            for tid in self._tabs_of(loc, name):
                if not self._tabs[tid].get("pending"):
                    _spawn(self._browse(tid, with_count=True), "refresh after import")

    async def _reload_location(self, loc: tuple) -> None:
        conn_id, db, schema = loc
        await self._refresh_location({"kind": "schema", "conn": conn_id, "db": db,
                                      "schema": schema})

    async def _create_table(self, data: dict) -> None:
        conn_id = int(data["conn"])
        conn = self._conn(conn_id) or {}
        levels = conn.get("levels") or []
        loc = self._loc(data)
        if ("schema" in levels and not loc[2]) or ("database" in levels and not loc[1]):
            self._toast("Choose the " + ("schema" if "schema" in levels else "database")
                        + " for the new table in the navigator.", kind="warning")
            return
        if not self._writable(conn_id):
            return
        types = f'bk-types-{conn_id}'
        int_type = (conn.get("types") or ["integer"])[0]

        def row(col: dict) -> str:
            def check(key):
                return (f'<td class="bk-ddl-c"><input type="checkbox" data-c="{key}"'
                        f'{" checked" if col.get(key) else ""}></td>')
            return (f'<tr data-col="1"><td><input class="bk-input" data-c="name" '
                    f'value="{_esc(col.get("name", ""))}" spellcheck="false" autocomplete="off"></td>'
                    f'<td><input class="bk-input bk-code" data-c="type" list="{types}" '
                    f'value="{_esc(col.get("type", ""))}" spellcheck="false" autocomplete="off"></td>'
                    + check("pk") + check("identity") + check("not_null") +
                    '<td><input class="bk-input bk-code" data-c="default" spellcheck="false" '
                    'autocomplete="off"></td>'
                    '<td><button type="button" class="bk-btn bk-mini" data-ddl="remove" '
                    'title="Remove this column"><span class="mdi mdi-close"></span></button></td></tr>')

        fields = (self._field("Table name", self._name_input("name", "", "new_table"))
                  + '<table class="bk-ddl-cols"><thead><tr><th>Column</th><th>Type</th>'
                  '<th title="Primary key">PK</th><th title="Numbers itself (identity)">Auto</th>'
                  '<th>NOT NULL</th><th>Default</th><th></th></tr></thead><tbody>'
                  + row({"name": "id", "type": int_type, "pk": True, "identity": True})
                  + '</tbody></table><button type="button" class="bk-btn bk-mini" data-ddl="add">'
                  '<span class="mdi mdi-plus"></span><span>Add column</span></button>')

        def collect(body) -> dict:
            columns = []
            for tr in body.querySelectorAll("tr[data-col]"):
                col = {}
                for el in tr.querySelectorAll("[data-c]"):
                    key = _attr(el, "data-c")
                    col[key] = bool(el.checked) if str(el.type) == "checkbox" else str(el.value)
                columns.append(col)
            return {"columns": columns}

        def on_click(event, body) -> bool:
            button = event.target.closest("[data-ddl]")
            if not button:
                return False
            if _attr(button, "data-ddl") == "add":
                body.querySelector(".bk-ddl-cols tbody").insertAdjacentHTML(
                    "beforeend", row({"type": (conn.get("types") or [""])[0]}))
                rows = body.querySelectorAll("tr[data-col]")
                rows[rows.length - 1].querySelector("input").focus()
            elif _attr(button, "data-ddl") == "remove":
                button.closest("tr").remove()
            return True

        done = await self._ddl_dialog(loc, "", "create_table", "Create table", fields,
                                      collect=collect, on_click=on_click, ok_text="Create",
                                      height=560)
        if done:
            created = done.get("table") or ""
            self._toast(f"Created {created}.", kind="success")
            await self._reload_location(loc)
            if created:
                self._open_table(loc, created, "table", "columns")

    async def _add_column(self, loc: tuple, name: str) -> None:
        if not self._writable(loc[0]) or self._edits_pending(loc, name):
            return
        conn = self._conn(loc[0]) or {}
        done = await self._ddl_dialog(loc, name, "add_column", f"Add a column to {name}",
                                      self._column_fields(conn), ok_text="Add column")
        if done:
            self._toast(f"Added the column to {name}.", kind="success")
            await self._after_table_change(loc, name)

    async def _create_index(self, loc: tuple, name: str) -> None:
        if not self._writable(loc[0]):
            return
        info = await SqlService().table_info_async(loc[0], loc[1], loc[2], name)
        if not info.get("ok"):
            self._toast(info.get("error", "Could not read the table"), kind="error")
            return
        boxes = "".join(
            f'<label class="bk-check"><input type="checkbox" data-p="columns" data-multi="1" '
            f'data-v="{_esc(col["name"])}"> <span class="bk-order" '
            f'data-order-of="{_esc(col["name"])}"></span>{_esc(col["name"])} '
            f'<small title="{_esc(col["type"])}">{_esc(col["type"])}</small></label>' for col in info["columns"])
        fields = (self._field("Index name", self._name_input("name", f"ix_{name}_"))
                  + '<div class="bk-ddl-field"><span>Columns <small>in the order you tick them'
                  f'</small></span><div class="bk-ddl-boxes">{boxes}</div></div>'
                  '<label class="bk-check"><input type="checkbox" data-p="unique"> UNIQUE</label>')
        done = await self._ddl_dialog(loc, name, "create_index", f"Create an index on {name}",
                                      fields, ok_text="Create index", height=560)
        if done:
            self._toast(f"Created the index on {name}.", kind="success")
            await self._after_table_change(loc, name, "keys")

    async def _rename_table(self, loc: tuple, name: str) -> None:
        if not self._writable(loc[0]) or self._edits_pending(loc, name):
            return
        fields = self._field("New name", self._name_input("new_name", name))
        done = await self._ddl_dialog(loc, name, "rename_table", f"Rename {name}", fields,
                                      ok_text="Rename", height=330)
        if not done:
            return
        new = done.get("table") or ""
        self._toast(f"Renamed {name}" + (f" to {new}." if new else "."), kind="success")
        reopen = [self._tabs[tid]["sub"] for tid in self._tabs_of(loc, name)]
        for tid in self._tabs_of(loc, name):
            self._close_tab(tid)
        await self._reload_location(loc)
        if reopen and new:
            self._open_table(loc, new, "table", reopen[0])

    async def _simple_ddl(self, loc: tuple, name: str, action: str, params: dict, title: str,
                          warning: str, ok_text: str) -> bool:
        conn_id, db, schema = loc
        conn = self._conn(conn_id) or {}
        plan = await SqlService().alter_async(conn_id, db, schema, name, action, params, True)
        if not plan.get("ok"):
            self._toast(plan.get("error", "Not possible"), kind="error")
            return False
        if not await self._confirm_sql(title, plan["sql"], f"{warning} {plan.get('note', '')}",
                                       prod=conn.get("env") == "prod", ok_text=ok_text,
                                       danger=True):
            return False
        done = await SqlService().alter_async(conn_id, db, schema, name, action, params, False)
        if not done.get("ok"):
            await message.alert(done.get("error", "Failed"), title=title)
            return False
        return True

    async def _truncate(self, loc: tuple, name: str) -> None:
        if not self._writable(loc[0]) or self._edits_pending(loc, name):
            return
        if await self._simple_ddl(loc, name, "truncate", {}, f"Truncate {name}",
                                  "Every row of the table is deleted.", "Truncate"):
            self._toast(f"Truncated {name}.", kind="success")
            for tid in self._tabs_of(loc, name):
                await self._browse(tid, with_count=True)
            await self._reload_location(loc)

    async def _drop(self, loc: tuple, name: str, kind: str) -> None:
        if not self._writable(loc[0]) or self._edits_pending(loc, name):
            return
        word = {"table": "table", "view": "view", "matview": "materialized view"}.get(kind, kind)
        warning = ("The table and every row in it are deleted." if kind == "table"
                   else f"The {word} is deleted; its tables are not touched.")
        if await self._simple_ddl(loc, name, "drop", {"kind": kind}, f"Drop {word} {name}",
                                  warning, "Drop"):
            self._toast(f"Dropped {name}.", kind="success")
            for tid in self._tabs_of(loc, name):
                self._close_tab(tid)
            await self._reload_location(loc)

    async def _tab_ddl(self, tid: str, action: str) -> None:
        """The Columns / Keys & indexes buttons of a table tab."""
        tab = self._tabs[tid]
        loc, name, info = tab["loc"], tab["name"], tab.get("info") or {}
        if action == "col_add":
            await self._add_column(loc, name)
            return
        if action == "ix_create":
            await self._create_index(loc, name)
            return
        if action in ("col_rename", "col_drop"):
            picked = tab["cols_table"].get_selected_ids() if tab.get("cols_table") else []
            if not picked:
                self._toast("Select a column first.", kind="warning")
                return
            column = str(picked[0])
            if not self._writable(loc[0]) or self._edits_pending(loc, name):
                return
            if action == "col_rename":
                fields = self._field("New name", self._name_input("new_name", column)) + (
                    f'<input type="hidden" data-p="column" value="{_esc(column)}">')
                if await self._ddl_dialog(loc, name, "rename_column", f"Rename {column}",
                                          fields, ok_text="Rename", height=330):
                    self._toast(f"Renamed {column}.", kind="success")
                    await self._after_table_change(loc, name)
            elif await self._simple_ddl(loc, name, "drop_column", {"column": column},
                                        f"Drop column {column}",
                                        "The column and its values are deleted.", "Drop"):
                self._toast(f"Dropped {column}.", kind="success")
                await self._after_table_change(loc, name)
            return
        # ix_drop
        rows = tab["idx_table"].get_selected_rows() if tab.get("idx_table") else []
        if not rows:
            self._toast("Select an index first.", kind="warning")
            return
        row = rows[0]
        if row.get("id") == "pk":
            self._toast("That is the primary key, not an index.", kind="warning")
            return
        index = row.get("name") or ""
        if not self._writable(loc[0]):
            return
        if await self._simple_ddl(loc, name, "drop_index", {"index": index},
                                  f"Drop index {index}", "The index is deleted.", "Drop"):
            self._toast(f"Dropped {index}.", kind="success")
            await self._after_table_change(loc, name, "keys")

    # ------------------------------------------------------------------
    # Object tabs: a routine, sequence, trigger or role (read-only):
    # its properties on the left, its definition on the right.
    # ------------------------------------------------------------------

    _OBJECT_ICONS = {"function": "mdi-function-variant", "procedure": "mdi-cog-play-outline",
                     "sequence": "mdi-counter", "trigger": "mdi-lightning-bolt-outline",
                     "role": "mdi-account"}

    def _open_object(self, data: dict) -> str:
        kind = data["kind"]
        loc = self._loc(data)
        ident = str(data.get("ident") or data["name"])
        table = data.get("table") or ""
        for tid, tab in self._tabs.items():
            if (tab["kind"] == "object" and tab["loc"] == loc and tab["object_kind"] == kind
                    and tab["ident"] == ident and tab["table"] == table):
                self.tabs.set_active(tid)
                return tid
        conn = self._conn(loc[0]) or {}
        icon = self._OBJECT_ICONS[kind]
        label = data.get("label") or data["name"]
        tid, container = self._new_tab(label, icon)
        self._tabs[tid] = {
            "tid": tid, "kind": "object", "conn": loc[0], "db": loc[1], "loc": loc,
            "name": data["name"], "label": label, "object_kind": kind, "ident": ident,
            "table": table, "engine": conn.get("engine") or "", "info": None, "def_editor": None,
        }
        container.innerHTML = self._object_html(tid, conn, loc, kind, label, icon)
        self.tabs.set_active(tid)
        _spawn(self._load_object(tid), "object open")
        return tid

    def _object_html(self, tid: str, conn: dict, loc: tuple, kind: str, label: str,
                     icon: str) -> str:
        extra = ""
        if kind in ("function", "procedure"):
            extra += ('<button type="button" class="bk-btn" data-bk="obj_call" '
                      'title="A statement that calls it, in a new SQL editor">'
                      '<span class="mdi mdi-play-outline"></span><span>Call…</span></button>')
        if kind == "trigger":
            extra += ('<button type="button" class="bk-btn" data-bk="obj_table" '
                      'title="Open the table the trigger is on">'
                      '<span class="mdi mdi-table"></span><span>Open table</span></button>')
        word = kind.capitalize()
        if kind == "role":
            word = {"mysql": "Account", "mssql": "Login or role"}.get(conn.get("engine"), "Role")
        return f"""
<div class="bk-view" data-tab="{tid}">
  <div class="bk-head">{self._crumbs(conn, loc[1], loc[2], label, icon)}
    <span class="bk-head-stats">{_esc(word)}</span></div>
  <div class="bk-bar">
    <button type="button" class="bk-btn" data-bk="obj_copy" title="Copy the definition">
      <span class="mdi mdi-content-copy"></span><span>Copy</span></button>
    <button type="button" class="bk-btn" data-bk="obj_sql" title="The definition, in a new SQL editor">
      <span class="mdi mdi-console-line"></span><span>Open in SQL editor</span></button>
    {extra}
    <button type="button" class="bk-btn" data-bk="obj_refresh" title="Read it again">
      <span class="mdi mdi-refresh"></span></button>
    <span class="bk-spacer"></span>
    <span class="bk-note" id="{tid}-note"></span>
  </div>
  <div class="bk-status" id="{tid}-status" hidden></div>
  <div class="bk-obj">
    <div class="bk-obj-props" id="{tid}-props"><p class="bk-obj-empty">Loading…</p></div>
    <div class="bk-results bk-obj-def"><div class="bk-panel bk-code-host" id="{tid}-def"></div></div>
  </div>
</div>"""

    async def _load_object(self, tid: str) -> None:
        tab = self._tabs.get(tid)
        if tab is None:
            return
        conn_id, db, schema = tab["loc"]
        result = await SqlService().object_info_async(conn_id, db, schema, tab["object_kind"],
                                                      tab["ident"], tab["table"])
        if tid not in self._tabs:
            return
        if not result.get("ok"):
            self._status(tid, result.get("error", "Could not read it"))
            _el(f"{tid}-props").innerHTML = ""
            return
        tab["info"] = result
        self._status(tid, result.get("note") or "", "info")
        _el(f"{tid}-note").textContent = (
            "Definition from the server" if result.get("native")
            else "Definition generated from the catalogue")
        rows = "".join(f"<tr><th>{_esc(label)}</th><td>{_esc(value)}</td></tr>"
                       for label, value in result.get("properties") or [])
        lists = ""
        for block in result.get("lists") or []:
            items = block.get("items") or []
            entries = "".join(
                f'<li><button type="button" class="bk-link" data-bk="obj_role" '
                f'data-name="{_esc(item)}">{_esc(item)}</button></li>'
                if tab["object_kind"] == "role" else f"<li>{_esc(item)}</li>" for item in items)
            lists += (f'<h3 class="bk-h3">{_esc(block["title"])} <small>{len(items)}</small></h3>'
                      + (f'<ul class="bk-obj-list">{entries}</ul>' if items
                         else '<p class="bk-obj-empty">None</p>'))
        _el(f"{tid}-props").innerHTML = (
            f'<table class="bk-kv bk-obj-kv"><tbody>{rows}</tbody></table>{lists}')
        await self._show_definition(tid)

    async def _show_definition(self, tid: str) -> None:
        tab = self._tabs[tid]
        text = (tab["info"] or {}).get("definition") or "-- No definition to show"
        if tab.get("def_editor") is not None:
            tab["def_editor"].setValue(text)
            return
        host = _el(f"{tid}-def")
        conn = self._conn(tab["conn"]) or {}
        if await self._ensure_editor():
            if tid not in self._tabs:
                return
            tab["def_editor"] = self._mount_editor(host, value=text, read_only=True,
                                                   dialect=conn.get("dialect") or "StandardSQL",
                                                   label="Definition", wrap=True)
        else:
            host.innerHTML = f'<pre class="bk-pre bk-fill bk-wrap">{_esc(text)}</pre>'

    def _object_action(self, tid: str, action: str, button) -> None:
        tab = self._tabs[tid]
        info = tab.get("info") or {}
        if action == "obj_refresh":
            _spawn(self._load_object(tid), "object refresh")
        elif action == "obj_copy":
            _spawn(self._copy_text(info.get("definition") or "", "Copied the definition."),
                   "copy")
        elif action in ("obj_sql", "obj_call"):
            text = info.get("call_sql" if action == "obj_call" else "definition")
            if text:
                self._open_sql(tab["conn"], tab["db"], text=text)
        elif action == "obj_table" and tab["table"]:
            self._open_table(tab["loc"], tab["table"], "table")
        elif action == "obj_role":
            name = _attr(button, "data-name")
            self._open_object({"kind": "role", "conn": tab["conn"], "db": "", "schema": "",
                               "name": name, "ident": name})

    async def _copy_text(self, text: str, done: str) -> None:
        if await _copy(text):
            self._toast(done, kind="success")
        else:
            self._toast("Could not reach the clipboard.", kind="error")

    async def _object_to_sql(self, data: dict, which: str) -> None:
        """A tree action: the definition (or a call) of an object, in a new SQL editor."""
        loc = self._loc(data)
        result = await SqlService().object_info_async(
            loc[0], loc[1], loc[2], data["kind"], str(data.get("ident") or data["name"]),
            data.get("table") or "")
        if not result.get("ok"):
            self._toast(result.get("error", "Could not read it"), kind="error")
            return
        self._open_sql(loc[0], loc[1], text=result.get(which) or "")

    async def _refresh_matview(self, loc: tuple, name: str) -> None:
        conn_id, db, schema = loc
        conn = self._conn(conn_id) or {}
        if conn.get("read_only"):
            self._toast("This connection is read-only.", kind="warning")
            return
        plan = await SqlService().refresh_matview_async(conn_id, db, schema, name, True)
        if not plan.get("ok"):
            self._toast(plan.get("error", "Could not refresh"), kind="error")
            return
        if not await self._confirm_sql("Refresh materialized view", [plan["sql"]], plan["note"],
                                       prod=conn.get("env") == "prod", ok_text="Refresh"):
            return
        self._toast(f"Refreshing {name}…")
        done = await SqlService().refresh_matview_async(conn_id, db, schema, name, False)
        if not done.get("ok"):
            self._toast(done.get("error", "Could not refresh"), kind="error")
            return
        self._toast(f"Refreshed {name} in {done['ms']:,} ms.", kind="success")
        for tid, tab in list(self._tabs.items()):
            if tab["kind"] == "table" and tab["loc"] == loc and tab["name"] == name:
                await self._refresh_table(tid)

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
            "menu_col": None, "vp": None, "focus_cell": None,
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
        # SQLite can only estimate (EXPLAIN QUERY PLAN).
        analyze = "" if conn.get("engine") == "sqlite" else (
            f'<button type="button" class="bk-btn" data-bk="analyze" id="{tid}-analyze" '
            'title="Run the statement for its actual plan: real rows and times '
            '(a write is rolled back)"><span class="mdi mdi-timer-outline"></span>'
            "<span>Analyze</span></button>")
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
    <button type="button" class="bk-btn" data-bk="explain" id="{tid}-explain"
      title="The estimated plan of the statement at the cursor (Ctrl+Shift+E)">
      <span class="mdi mdi-file-tree-outline"></span><span>Explain</span></button>
    {analyze}
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
    <button type="button" class="bk-btn" data-bk="value_panel" id="{tid}-res-value" hidden
      title="Show the focused cell's whole value (F7; Shift+Enter on a cell)">
      <span class="mdi mdi-dock-right"></span><span>Value</span></button>
    <span class="bk-res-tools" id="{tid}-res-tools" hidden>
      <button type="button" class="bk-btn" data-bk="col_filter" title="Filter the rows by a column">
        <span class="mdi mdi-filter-plus-outline"></span></button>
      <button type="button" class="bk-btn" data-bk="columns" title="Show, hide and freeze columns">
        <span class="mdi mdi-view-column-outline"></span></button>
      <button type="button" class="bk-btn" data-bk="freeze" id="{tid}-freeze" aria-pressed="false"
        title="Freeze the first column while scrolling sideways">
        <span class="mdi mdi-pin-outline"></span></button>
    </span>
  </div>
  <div class="bk-chips" id="{tid}-chips" hidden></div>
  <div class="bk-status" id="{tid}-status" hidden></div>
  <div class="bk-results">
    <div class="bk-panel bk-vp-grid" id="{tid}-grid"></div>
    <div class="bk-vp" id="{tid}-vp" hidden></div>
    <pre class="bk-panel bk-msgs" id="{tid}-msgs" hidden></pre>
    <div class="bk-panel bk-plan" id="{tid}-plan" hidden>
      <div class="bk-plan-bar">
        <span class="bk-seg" role="group" aria-label="Plan view">
          <button type="button" class="bk-seg-btn" data-bk="plan_view" data-view="tree" aria-pressed="true">Plan</button>
          <button type="button" class="bk-seg-btn" data-bk="plan_view" data-view="raw" aria-pressed="false">Raw</button></span>
        <span class="bk-note bk-plan-summary" id="{tid}-plan-summary"></span>
      </div>
      <div class="bk-results">
        <div class="bk-panel" id="{tid}-plan-grid"></div>
        <pre class="bk-panel bk-msgs" id="{tid}-plan-raw" hidden></pre>
      </div>
    </div>
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
                    "onExplain": lambda *_: _spawn(self._sql_explain(tid, False), "explain"),
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
        elif action in ("explain", "analyze"):
            _spawn(self._sql_explain(tid, action == "analyze"), action)
        elif action == "plan_view":
            self._plan_view(tid, _attr(button, "data-view"))
        elif action == "res_csv":
            self._result_csv(tid)
        elif action == "res_export":
            _spawn(self._result_export(tid), "export")
        elif action == "value_panel":
            self._vp_toggle(tid)
        elif action in ("col_filter", "columns", "freeze", "unfilter", "clear_filters"):
            self._grid_tool(tid, action, button)

    def _sql_busy(self, tid: str, busy: bool) -> None:
        for suffix in ("explain", "analyze"):
            button = _el(f"{tid}-{suffix}")
            if button:
                button.disabled = busy
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

    # -- explain plans ------------------------------------------------------

    async def _sql_explain(self, tid: str, analyze: bool, confirmed: bool = False) -> None:
        tab = self._tabs.get(tid)
        if tab is None or tab["run_id"]:
            return
        sel = self._sql_selection(tid)
        if sel["text"].strip():
            sql, caret, offset = sel["text"], 0, sel["from"]
        else:
            sql, caret, offset = self._sql_text(tid), sel["head"], 0
        if not sql.strip():
            return
        tab["run_id"] = "explain"
        self._sql_busy(tid, True)
        self._status(tid, "Running the statement for its actual plan…" if analyze
                     else "Explaining…", "info")
        try:
            result = await SqlService().explain_async(tab["conn"], tab["db"], sql, caret,
                                                      analyze, confirmed)
        finally:
            if tid in self._tabs:
                tab["run_id"] = None
                self._sql_busy(tid, False)
        if tid not in self._tabs:
            return
        if result.get("needs_confirm"):
            conn = self._conn(tab["conn"]) or {}
            if await self._confirm_sql(
                "Analyze a statement that changes data", [result.get("statement") or ""],
                "Analyze runs the statement for real to measure it, then rolls the transaction "
                "back. Effects outside the transaction (sequence values, triggers that call out) "
                "are not undone.", prod=conn.get("env") == "prod", ok_text="Run and roll back"):
                await self._sql_explain(tid, analyze, confirmed=True)
            else:
                self._status(tid, "Not run.", "info")
            return
        if not result.get("ok"):
            self._status(tid, result.get("error", "Could not explain"))
            if tab["editor"] is not None:
                tab["editor"].mark(js.undefined, js.undefined)
            return
        self._status(tid, "")
        result["start"] += offset
        result["end"] += offset
        tab["plan"] = result
        if tab["editor"] is not None:
            tab["editor"].mark(result["start"], result["end"], "run")
        self._draw_restabs(tid)
        self._render_plan(tid)
        self._show_result(tid, -2)

    def _render_plan(self, tid: str) -> None:
        tab = self._tabs[tid]
        plan = tab["plan"]
        analyzed = plan["analyzed"]

        def number(value, digits: int = 2) -> str:
            if value is None:
                return ""
            if float(value).is_integer() and abs(value) >= 1:
                return f"{int(value):,}"
            return f"{value:,.{digits}f}".rstrip("0").rstrip(".") if value else "0"

        rows = []
        for i, item in enumerate(plan["rows"]):
            depth = item["depth"]
            indent = "\u00a0\u00a0\u00a0" * max(0, depth - 1) + ("└\u00a0" if depth else "")
            share = item.get("share")
            hot = "bk-hot" if share is not None and share >= 0.3 else (
                "bk-warm" if share is not None and share >= 0.1 else "")
            rows.append({
                "id": f"p{i}", "op": indent + item["op"], "object": item["object"],
                "est": number(item["est_rows"]), "act": number(item["act_rows"]),
                "time": number(item["time_ms"], 3), "loops": number(item["loops"]),
                "cost": number(item["cost"]), "share": "" if share is None else f"{share:.0%}",
                "detail": item["detail"], "share_cls": hot,
                "miss": "mdi-alert" if item.get("misestimate") else "",
                "miss_title": "The estimate is off by 10x or more" if item.get("misestimate") else "",
                "icon": "mdi-table" if item["object"] else "",
            })
        columns = [
            ColumnConfig(id="op", header="Operation", width=300, sortable=False, icon_by="icon"),
            ColumnConfig(id="object", header="Object", width=200, sortable=False),
            ColumnConfig(id="est", header="Est. rows", width=95, align="right", sortable=False),
        ]
        if analyzed:
            columns += [
                ColumnConfig(id="act", header="Actual rows", width=110, align="right",
                             sortable=False, icon_by="miss"),
                ColumnConfig(id="loops", header="Loops", width=70, align="right", sortable=False),
                ColumnConfig(id="time", header="Time ms", width=90, align="right", sortable=False),
            ]
        columns += [
            ColumnConfig(id="cost", header="Cost", width=90, align="right", sortable=False),
            ColumnConfig(id="share", header="Own cost %", width=95, align="right",
                         sortable=False, cell_class_by="share_cls"),
            ColumnConfig(id="detail", header="Details", width=420, sortable=False),
        ]
        if tab.get("plan_grid") is None:
            tab["plan_grid"] = DataTable(DataTableConfig(
                columns=columns, rows=rows, selection="single", resizable_columns=True,
                sortable=False, empty_text="No plan",
            ), container=_el(f"{tid}-plan-grid"))
            tab["plan_grid"].on_activate(lambda payload: self._plan_node_dialog(tid, payload))
        else:
            tab["plan_grid"].set_columns(columns)
            tab["plan_grid"].set_rows(rows)
        _el(f"{tid}-plan-raw").textContent = plan["raw"]
        summary = plan.get("summary") or {}
        parts = ["Actual plan" if analyzed else "Estimated plan"]
        if summary.get("cost") is not None:
            parts.append(f"total cost {number(summary['cost'])}")
        if summary.get("planning_ms") is not None:
            parts.append(f"planning {number(summary['planning_ms'], 3)} ms")
        if summary.get("execution_ms") is not None:
            parts.append(f"execution {number(summary['execution_ms'], 3)} ms")
        if plan.get("rolled_back"):
            parts.append("the statement's changes were rolled back")
        if summary.get("warnings"):
            parts.append("warnings: " + ", ".join(summary["warnings"]))
        _el(f"{tid}-plan-summary").textContent = " · ".join(parts)
        self._plan_view(tid, tab.get("plan_view") or "tree")

    def _plan_view(self, tid: str, view: str) -> None:
        tab = self._tabs[tid]
        tab["plan_view"] = view
        _el(f"{tid}-plan-grid").hidden = view != "tree"
        _el(f"{tid}-plan-raw").hidden = view != "raw"
        for button in js.document.querySelectorAll(f'#{tid}-plan [data-bk="plan_view"]'):
            button.setAttribute("aria-pressed", "true" if _attr(button, "data-view") == view else "false")

    def _plan_node_dialog(self, tid: str, payload: dict) -> None:
        tab = self._tabs[tid]
        row_id = str(payload.get("id") or "p0")
        item = tab["plan"]["rows"][int(row_id[1:])]
        facts = [("Operation", item["op"]), ("Object", item["object"]), ("Details", item["detail"]),
                 ("Estimated rows", item["est_rows"]), ("Actual rows", item["act_rows"]),
                 ("Loops", item["loops"]), ("Time (ms)", item["time_ms"]),
                 ("Cost", item["cost"]),
                 ("Own share of the statement's cost",
                  None if item.get("share") is None else f"{item['share']:.1%}"),
                 *[(key, value) for key, value in item.get("extra") or []]]
        modal = ModalWindow(ModalConfig(dispose_on_close=True, title=item["op"], width=620,
                                        height=480))
        modal.body.innerHTML = '<table class="bk-kv">' + "".join(
            f"<tr><th>{_esc(key)}</th><td>{_esc(value)}</td></tr>"
            for key, value in facts if value not in (None, "")) + "</table>"
        modal.show()

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
        tab["rfilters"] = []
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
        tab["msg_error"] = error is not None
        tab["ran"] = True
        self._draw_restabs(tid)
        if error is not None or not tab["results"]:
            self._show_result(tid, -1)
        else:
            self._show_result(tid, len(tab["results"]) - 1 if len(tab["results"]) == 1 else 0)

    def _draw_restabs(self, tid: str) -> None:
        """Result N, Messages (when something ran) and Plan (when explained)."""
        tab = self._tabs[tid]
        buttons = "".join(
            f'<button type="button" class="bk-restab" data-bk="res" data-res="{i}" role="tab">'
            f'<span class="mdi mdi-table"></span>Result {i + 1}'
            f'<small>{_count(entry["rowcount"])}{"+" if entry.get("more") else ""}</small></button>'
            for i, entry in enumerate(tab["results"]))
        if tab.get("ran"):
            buttons += ('<button type="button" class="bk-restab" data-bk="res" data-res="-1" role="tab"'
                        f'{" data-error" if tab.get("msg_error") else ""}>'
                        '<span class="mdi mdi-message-text-outline"></span>Messages</button>')
        plan = tab.get("plan")
        if plan:
            buttons += ('<button type="button" class="bk-restab" data-bk="res" data-res="-2" role="tab">'
                        '<span class="mdi mdi-file-tree-outline"></span>'
                        f'{"Actual plan" if plan["analyzed"] else "Plan"}</button>')
        _el(f"{tid}-restabs").innerHTML = buttons
        _el(f"{tid}-empty").hidden = True

    def _show_result(self, tid: str, index: int) -> None:
        tab = self._tabs[tid]
        if tab.get("shown") != index and 0 <= index < len(tab.get("results") or []):
            tab["rfilters"] = []        # another result: its own columns
        tab["shown"] = index
        for button in js.document.querySelectorAll(f'#{tid}-restabs .bk-restab'):
            button.setAttribute("aria-selected",
                                "true" if _attr(button, "data-res") == str(index) else "false")
        grid_host, msgs = _el(f"{tid}-grid"), _el(f"{tid}-msgs")
        showing_rows = 0 <= index < len(tab["results"])
        grid_host.hidden = not showing_rows
        _el(f"{tid}-res-value").hidden = not showing_rows
        if tab.get("vp") is not None:
            tab["vp"].suppress(not showing_rows)
        msgs.hidden = index != -1
        _el(f"{tid}-plan").hidden = index != -2
        _el(f"{tid}-res-csv").hidden = not showing_rows
        _el(f"{tid}-res-export").hidden = not showing_rows
        _el(f"{tid}-res-tools").hidden = not showing_rows
        self._draw_chips(tid)
        summary = _el(f"{tid}-summary")
        if not showing_rows:
            summary.textContent = ""
            return
        entry = tab["results"][index]
        columns = [ColumnConfig(id=f"c{i}", header=name, width=_column_width(name),
                                align="right" if entry["kinds"][i] == "num" else None,
                                cell_class_by=_CLS + f"c{i}")
                   for i, name in enumerate(entry["columns"])]
        rows = self._result_rows(tid, entry)
        if tab["grid"] is None:
            tab["grid"] = DataTable(DataTableConfig(
                columns=columns, rows=rows, id_field=_ID, selection="multi",
                resizable_columns=True, reorderable_columns=True, empty_text="No rows",
                context_actions=[
                    TableAction("value", "View value  (Shift+Enter)", "mdi-text-box-search-outline"),
                    TableAction("copy_row", "Copy row as JSON", "mdi-code-json"),
                    TableAction("copy_rows", "Copy selected as TSV", "mdi-content-copy"),
                    TableAction("copy_insert", "Copy as INSERT", "mdi-database-import-outline"),
                    TableAction("copy_md", "Copy as Markdown", "mdi-language-markdown-outline"),
                    TableAction(separator=True),
                    TableAction("filter_col", "Filter column…", "mdi-filter-plus-outline"),
                    TableAction("hide_col", "Hide this column", "mdi-eye-off-outline")],
            ), container=grid_host)
            tab["grid"].on_action(lambda payload: _spawn(self._result_action(tid, payload),
                                                         "result action"))
            self._watch_cells(tid, grid_host)
            if (self._settings.get("value_panel") or {}).get("open"):
                self._vp(tid).open()
        else:
            tab["grid"].set_columns(columns)
            tab["grid"].set_rows(rows)
        self._set_frozen(tid, frozen_count(self._settings.get("freeze_results")))
        self._result_summary(tid)
        tab["focus_cell"] = None
        if tab.get("vp") is not None:
            tab["vp"].reset()

    def _result_rows(self, tid: str, entry: dict) -> list:
        """The shown result's grid rows that pass the column filters; ids keep the row number."""
        tab = self._tabs[tid]
        cut = {(r, c) for r, c in entry.get("truncated") or []}
        rows = []
        for r in filter_rows(entry["rows"], entry["columns"], tab.get("rfilters") or []):
            row = {_ID: f"r{r}"}
            for c, value in enumerate(entry["rows"][r]):
                row[f"c{c}"] = value
                row[_CLS + f"c{c}"] = " ".join(
                    cls for cls, on in (("bk-null", value is None), ("bk-cut", (r, c) in cut)) if on)
            rows.append(row)
        tab["rshown"] = len(rows)
        return rows

    def _result_summary(self, tid: str) -> None:
        tab = self._tabs[tid]
        entry = tab["results"][tab["shown"]]
        shown = (f"{_count(tab.get('rshown'))} of {_count(entry['rowcount'])} rows"
                 if tab.get("rfilters") else f"{_count(entry['rowcount'])} rows")
        _el(f"{tid}-summary").textContent = (
            shown + (" · more not fetched" if entry.get("more") else "") + f" · {entry['ms']} ms")

    async def _result_action(self, tid: str, payload: dict) -> None:
        tab = self._tabs[tid]
        entry = tab["results"][tab["shown"]]
        names = entry["columns"]
        if payload.get("action") == "value":
            self._vp_open(tid, str(payload.get("id") or ""), tab.get("menu_col") or "c0",
                          focus=True)
        elif payload.get("action") == "copy_row":
            row = payload.get("row") or {}
            data = {names[i]: row.get(f"c{i}") for i in range(len(names))}
            if await _copy(json.dumps(data, ensure_ascii=False, indent=2)):
                self._toast("Copied the row as JSON.", kind="success")
        elif payload.get("action") in ("copy_insert", "copy_md"):
            await self._copy_rows(tid, payload, str(payload.get("action")))
        elif payload.get("action") == "filter_col":
            column = tab.get("menu_col") or "c0"
            try:
                name = names[int(column[1:])]
            except (ValueError, IndexError):
                return
            self._filter_dialog(tid, name, (payload.get("row") or {}).get(column))
        elif payload.get("action") == "hide_col":
            self._hide_column(tid, tab.get("menu_col") or "c0")
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
            ("Ctrl+Shift+E", "Explain: the plan of the statement at the cursor"),
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
.bk-plan{display:flex;flex-direction:column;}
.bk-plan-bar{display:flex;align-items:center;gap:8px;padding:5px 8px;flex:0 0 auto;
  border-bottom:1px solid var(--bk-line);background:var(--bk-bg);}
.bk-plan-bar .bk-seg{margin:0;}
.bk-plan-summary{white-space:normal;}
td.bk-hot{background:rgba(239,68,68,.22) !important;color:#fecaca;font-weight:600;}
td.bk-warm{background:rgba(245,158,11,.16) !important;color:#fde68a;}
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
  .bk-bar.bk-cmp-pick .bk-primary span:not(.mdi){display:inline;}
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
/* Object tabs: properties beside the definition, stacked when narrow. */
.bk-obj{display:flex;flex:1 1 auto;min-height:0;}
.bk-obj-props{flex:0 0 360px;overflow:auto;padding:6px 14px 16px;border-right:1px solid var(--bk-line);
  box-sizing:border-box;}
.bk-obj-def{flex:1 1 auto;min-width:0;}
.bk-obj-kv th{width:auto;}
.bk-obj-kv td{word-break:break-word;}
.bk-obj-list{margin:0;padding:0 0 0 2px;list-style:none;}
.bk-obj-list li{padding:2px 0;}
.bk-obj-empty{margin:4px 0;color:var(--bk-dim);}
.bk-wrap{white-space:pre-wrap;word-break:break-word;}
/* Object action dialogs: fields, a live SQL preview, Run. */
.bk-ddl{height:100%;box-sizing:border-box;}
.bk-ddl-fields{display:flex;flex-direction:column;gap:9px;flex:0 1 auto;min-height:0;overflow:auto;}
.bk-ddl-field{display:flex;flex-direction:column;gap:4px;}
.bk-ddl-field > span{font:600 11px system-ui,sans-serif;text-transform:uppercase;letter-spacing:.05em;
  color:var(--bk-muted);}
.bk-ddl-field small{text-transform:none;letter-spacing:0;font-weight:400;color:var(--bk-dim);}
.bk-check{display:inline-flex;align-items:center;justify-content:flex-start;gap:6px;
  color:var(--bk-text);cursor:pointer;width:auto;}
.bk-check input[type=checkbox]{flex:0 0 auto;width:auto;margin:0;}
.bk-check small{color:var(--bk-dim);}
.bk-ddl-boxes{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:4px 12px;
  justify-items:start;
  max-height:170px;overflow:auto;padding:6px 8px;border:1px solid var(--bk-line);border-radius:6px;}
.bk-ddl-boxes .bk-check{max-width:100%;min-width:0;white-space:nowrap;}
.bk-ddl-boxes small{overflow:hidden;text-overflow:ellipsis;min-width:0;}
.bk-order{display:inline-block;min-width:14px;text-align:center;color:var(--bk-accent);font-weight:600;}
.bk-ddl-sql{flex:1 1 auto;min-height:70px;white-space:pre-wrap;word-break:break-word;}
.bk-hint[data-kind="error"]{color:#fca5a5;}
.bk-ddl-cols{border-collapse:collapse;width:100%;}
.bk-ddl-cols th{text-align:left;font:600 11px system-ui,sans-serif;color:var(--bk-muted);
  padding:2px 4px;white-space:nowrap;}
.bk-ddl-cols td{padding:2px 4px;}
.bk-ddl-cols td .bk-input{width:100%;}
.bk-ddl-cols .bk-ddl-c{text-align:center;width:44px;}
.bk-ddl-fields > .bk-btn{align-self:flex-start;}
.bk-btn.bk-mini{padding:3px 8px;font-size:12px;}
.bk-h3-tools{display:inline-flex;gap:4px;margin-left:auto;text-transform:none;letter-spacing:0;}
.bk-link{background:none;border:0;padding:0;color:var(--bk-accent);cursor:pointer;
  font:inherit;text-align:left;}
.bk-link:hover{text-decoration:underline;}
@container (max-width: 720px){
  .bk-obj{flex-direction:column;}
  .bk-obj-props{flex:0 0 auto;max-height:45%;border-right:0;border-bottom:1px solid var(--bk-line);}
}
.bk-kv th{text-align:left;font-weight:500;color:var(--bk-muted);padding:6px 12px 6px 0;
  white-space:nowrap;vertical-align:top;width:40%;}
.bk-kv td{padding:6px 0;}
.bk-kv kbd{padding:2px 6px;border:1px solid var(--bk-line-2);border-radius:4px;background:var(--bk-bg);
  font:12px ui-monospace,Menlo,Consolas,monospace;}
/* The value panel (ui/value_panel.py): the right side of a grid's box. */
.bk-results.bk-vp-on > .bk-vp-grid{right:var(--bk-vp-w,420px);width:auto;}
.bk-vp{position:absolute;top:0;bottom:0;right:0;width:var(--bk-vp-w,420px);display:flex;
  min-width:0;background:var(--bk-panel);border-left:1px solid var(--bk-line-2);z-index:2;}
.bk-vp [hidden]{display:none !important;}
.bk-vp-grip{position:absolute;left:-4px;top:0;bottom:0;width:7px;cursor:col-resize;z-index:3;}
.bk-vp-grip:hover,.bk-vp-dragging .bk-vp-grip{background:rgba(245,158,11,.35);}
.bk-vp-dragging,.bk-vp-dragging *{user-select:none;}
.bk-vp-inner{display:flex;flex-direction:column;flex:1 1 auto;min-width:0;min-height:0;}
.bk-vp-head{display:flex;align-items:center;gap:6px;padding:4px 6px 4px 10px;font-size:12.5px;
  border-bottom:1px solid var(--bk-line);}
.bk-vp-head > .mdi{color:var(--bk-accent);font-size:15px;}
.bk-vp-title{font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0;}
.bk-vp-type{color:var(--bk-muted);font-size:11.5px;white-space:nowrap;overflow:hidden;
  text-overflow:ellipsis;min-width:0;}
.bk-vp-bar{display:flex;align-items:center;gap:6px;padding:4px 8px;flex-wrap:wrap;
  border-bottom:1px solid var(--bk-line);}
.bk-vp-bar .bk-seg{align-self:center;}
.bk-vp-bar .bk-seg-btn{padding:3px 9px;font-size:12px;}
.bk-vp-bar .bk-seg-btn:disabled{opacity:.35;cursor:not-allowed;background:transparent;}
.bk-vp-small{padding:3px 8px;font-size:12px;}
.bk-vp-icon{padding:5px 7px;}
.bk-vp-wrap{display:flex;align-items:center;gap:4px;font-size:12px;color:var(--bk-muted);}
.bk-vp-note{padding:5px 10px;font-size:12px;line-height:1.4;border-bottom:1px solid var(--bk-line);}
.bk-vp-info{color:var(--bk-muted);}
.bk-vp-ok{color:#86efac;}
.bk-vp-warn,.bk-vp-warning{color:#fbbf24;}
.bk-vp-error{color:#f87171;}
.bk-vp-body{position:relative;flex:1 1 auto;min-height:0;background:var(--bk-deep);}
.bk-vp-editor{position:absolute;inset:0;}
.bk-vp-editor .cm-editor{height:100%;}
.bk-vp-plain{width:100%;height:100%;box-sizing:border-box;padding:8px 10px;border:0;resize:none;
  background:var(--bk-deep);color:var(--bk-text);font:12.5px ui-monospace,Menlo,Consolas,monospace;}
.bk-vp-image{position:absolute;inset:0;overflow:auto;display:flex;align-items:center;
  justify-content:center;padding:12px;box-sizing:border-box;background-color:#1e293b;
  background-image:conic-gradient(#273449 25%,transparent 0 50%,#273449 0 75%,transparent 0);
  background-size:16px 16px;}
.bk-vp-image img{max-width:100%;max-height:100%;}
.bk-vp-empty{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;
  gap:6px;padding:16px;text-align:center;color:var(--bk-muted);font-size:12.5px;}
.bk-vp-status{display:flex;gap:10px;padding:3px 10px;font-size:11.5px;color:var(--bk-muted);
  border-top:1px solid var(--bk-line);white-space:nowrap;overflow:hidden;}
.bk-vp-actions{display:flex;align-items:center;gap:6px;padding:6px 8px;
  border-top:1px solid var(--bk-line);}
.bk-vp-actions .bk-btn{padding:5px 9px;}

/* A narrow table tab: the less-used buttons keep their icon and tooltip only. */
@container (max-width: 1300px){.bk-bar .bk-btn[data-compact] > span:not(.mdi){display:none;}}
/* Grid tools: column filter chips, the Columns dialog, frozen toggle, FK headers. */
.bk-chips{display:flex;flex-wrap:wrap;align-items:center;gap:6px;flex:0 0 auto;padding:6px 10px;
  background:var(--bk-panel);border-bottom:1px solid var(--bk-line);}
.bk-chip{display:inline-flex;align-items:center;gap:5px;max-width:420px;padding:2px 4px 2px 9px;
  border:1px solid var(--bk-accent-strong);border-radius:999px;background:rgba(245,158,11,.1);
  color:var(--bk-text);font:12px ui-monospace,Menlo,Consolas,monospace;}
.bk-chip .mdi{font-size:13px;color:var(--bk-accent);}
.bk-chip-text{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.bk-chip-x{display:inline-flex;align-items:center;justify-content:center;width:20px;height:20px;
  padding:0;border:0;border-radius:50%;background:transparent;color:var(--bk-muted);cursor:pointer;}
.bk-chip-x:hover,.bk-chip-x:focus-visible{background:#1e293b;color:var(--bk-text);outline:none;}
.bk-btn[aria-pressed="true"]{color:var(--bk-accent);border-color:var(--bk-accent-strong);
  background:#1e293b;}
.bk-res-tools{display:inline-flex;gap:6px;}
.bk-col-list{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:4px 12px;
  flex:1 1 auto;min-height:0;overflow:auto;padding:4px 2px;align-content:start;}
.bk-col-check span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.bk-mini-select{padding:3px 6px;margin:0 4px;font-family:system-ui,sans-serif;}
.bk-inline-label{display:inline-flex;align-items:center;color:var(--bk-muted);font-size:12.5px;
  white-space:nowrap;}
.bk-filter-fields{display:grid;grid-template-columns:1fr 1fr 1.3fr;align-items:end;}
th.bk-fkcol .wapyt-datatable-header-label::before,th.bk-fkcol > span:first-child::before{
  content:"\\F0339";font-family:"Material Design Icons";color:#7dd3fc;margin-right:4px;}
@container (max-width: 640px){.bk-col-list{grid-template-columns:repeat(2,minmax(0,1fr));}}

/* Import CSV: options, the column mapping beside the preview, the job console. */
.bk-import{height:100%;box-sizing:border-box;}
.bk-import-file{display:flex;align-items:center;gap:10px;flex:0 0 auto;}
.bk-import-file .bk-btn{cursor:pointer;}
.bk-import-opts{display:grid;grid-template-columns:.9fr .8fr 1.1fr 1fr 1.15fr 1.15fr;gap:8px;
  flex:0 0 auto;}
.bk-import-opts .bk-input{width:100%;min-width:0;}
.bk-import-checks{display:flex;gap:18px;flex:0 0 auto;}
.bk-import-main{display:flex;gap:10px;flex:1 1 auto;min-height:0;}
.bk-import-map{flex:0 0 46%;min-width:0;overflow:auto;border:1px solid var(--bk-line);border-radius:6px;}
.bk-import-preview{position:relative;flex:1 1 auto;min-width:0;overflow:auto;
  border:1px solid var(--bk-line);border-radius:6px;background:var(--bk-deep);}
.bk-import-table{border-collapse:collapse;width:100%;font-size:12.5px;}
.bk-import-table th{position:sticky;top:0;z-index:1;text-align:left;padding:5px 8px;
  background:var(--bk-panel);color:var(--bk-muted);font:600 11px system-ui,sans-serif;
  border-bottom:1px solid var(--bk-line);white-space:nowrap;}
.bk-import-table td{padding:3px 8px;border-bottom:1px solid var(--bk-line);}
.bk-import-table td .bk-input{width:100%;padding:3px 6px;}
.bk-import-col{white-space:nowrap;}
.bk-import-col .mdi{margin-right:4px;}
.bk-import-col .mdi-key{color:#fbbf24;}
.bk-import-col .mdi-alert{color:#f87171;}
.bk-import-col small{margin-right:5px;padding:0 5px;border-radius:4px;background:#1e293b;
  color:var(--bk-muted);font-size:10.5px;}
.bk-import-type{max-width:140px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
  color:var(--bk-muted);}
.bk-import-rows td{max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
  font-family:ui-monospace,Menlo,Consolas,monospace;}
.bk-import-progress{flex:0 0 auto;}
.bk-console{height:100%;box-sizing:border-box;}
.bk-console-head{display:flex;align-items:center;justify-content:space-between;flex:0 0 auto;}
.bk-console-state{display:inline-flex;align-items:center;gap:6px;font-weight:600;}
.bk-console-state[data-state="done"]{color:#86efac;}
.bk-console-state[data-state="failed"]{color:#fca5a5;}
.bk-console-state[data-state="cancelled"]{color:#fbbf24;}
.bk-console-log{min-height:120px;white-space:pre-wrap;word-break:break-word;font-size:12px;}
@container (max-width: 760px){.bk-import-opts{grid-template-columns:repeat(3,minmax(0,1fr));}
  .bk-import-main{flex-direction:column;}.bk-import-map{flex-basis:auto;max-height:45%;}}

/* ER diagram. */
.bk-er-host{position:relative;flex:1 1 auto;min-height:0;overflow:hidden;background:var(--bk-deep);
  cursor:grab;user-select:none;touch-action:none;outline:none;}
.bk-er-host.bk-er-panning{cursor:grabbing;}
.bk-er-svg{position:absolute;inset:0;width:100%;height:100%;display:block;}
.bk-er-table{cursor:move;}
.bk-er-find{width:180px;}
.bk-er-zoom{min-width:42px;text-align:center;font-variant-numeric:tabular-nums;}

/* Session manager. */
tr.bk-active td:first-child{box-shadow:inset 3px 0 0 #22c55e;}
tr.bk-blocked td{background:rgba(245,158,11,.1);}
tr.bk-blocked td:first-child{box-shadow:inset 3px 0 0 #f59e0b;}
tr.bk-own td{color:var(--bk-muted);}
.bk-danger-text{color:#fca5a5;}
.bk-session-facts{display:grid;grid-template-columns:max-content 1fr;gap:3px 14px;margin:0;
  flex:0 0 auto;font-size:12.5px;}
.bk-session-facts dt{color:var(--bk-muted);}
.bk-session-facts dd{margin:0;word-break:break-word;}

/* Schema compare */
.bk-cmp-pick{gap:8px;row-gap:6px;}
.bk-cmp-side{display:inline-flex;align-items:center;gap:6px;flex-wrap:wrap;min-width:0;}
.bk-cmp-side .bk-select{max-width:220px;}
.bk-cmp-label{display:inline-flex;align-items:center;gap:5px;font:600 11px system-ui,sans-serif;
  text-transform:uppercase;letter-spacing:.06em;color:var(--bk-muted);margin-right:2px;}
.bk-cmp-label .mdi{font-size:15px;color:var(--bk-accent);}
.bk-cmp-title{display:inline-flex;align-items:center;gap:6px;margin-right:8px;
  font:600 11px system-ui,sans-serif;text-transform:uppercase;letter-spacing:.06em;color:var(--bk-muted);}
.bk-cmp-title .mdi{font-size:15px;color:var(--bk-accent);}
tr.bk-cmp-add td[data-column-id="change"]{color:#86efac;}
tr.bk-cmp-alter td[data-column-id="change"]{color:#fcd34d;}
tr.bk-cmp-drop td[data-column-id="change"]{color:#fca5a5;}
tr.bk-cmp-drop td[data-column-id="object"]{text-decoration:line-through;text-decoration-color:#f87171;}
tr.bk-cmp-manual td[data-column-id="detail"]{color:#fcd34d;}
.bk-cmp-items{margin:0;padding:6px 12px 6px 30px;max-height:140px;overflow:auto;flex:0 0 auto;
  font:12.5px ui-monospace,Menlo,Consolas,monospace;color:var(--bk-muted);
  border:1px solid var(--bk-line);border-radius:6px;}
"""
