"""
The value panel: one cell's whole value beside a grid. Runs in Pyodide only.

The page opens it on a table's Data grid and on SQL results. It follows the
focused cell and shows the value uncut (fetched by primary key when the grid
had to cut it), as text, pretty JSON, a hex dump or an image. On a table it
also edits: **Apply** hands the new value to the page, which treats it like
any other grid edit (pending, previewed, saved in one transaction), so the
panel never writes to the database itself.

The page describes a cell with a ``ctx`` dict:

* ``key``: ``(row_id, column_id)``, what "the same cell" means
* ``column``, ``type``, ``pk``, ``nullable``
* ``binary`` / ``json``: what the column holds, from its type and values
* ``text``: the value as the grid holds it ("0x…" for binary, None for NULL)
* ``cut``: ``text`` was cut for the grid; ``load`` (async, or None) fetches
  the whole value as ``services.values.full_value`` describes it, and
  ``cut_note`` says why not when there is no ``load``
* ``editable`` and ``reason`` (why not, shown when it is read-only)
* ``apply(new_text, original_text)``: the page records the edit
* ``download``: a URL that serves the saved value as a file, or None
* ``filename``: a stem for Save to file

Values cross as text the way the grid has them: binary as ``0x`` + hex,
which ``services.values.coerce`` turns back into bytes.

Pure helpers (hex dumps, image sniffing, JSON handling) are at the top and
take no browser objects, so ``tests/test_value_panel.py`` runs them in
CPython.
"""
from __future__ import annotations

import base64
import json
from typing import Any, Callable, Optional

try:  # the page; CPython imports the helpers without these
    import js
    from pyodide.ffi import create_proxy, to_js
except ImportError:  # pragma: no cover - tests
    js = None

from services.values import MAX_CELL_CHARS, VIEW_MAX_BYTES

HEX_DUMP_MAX = 1024 * 1024      # a longer value's dump shows its first megabyte
JSON_LIVE_MAX = 2 * 1024 * 1024  # validate while typing below this size
MIN_WIDTH = 260

_IMAGES = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"\x00\x00\x01\x00", "image/x-icon"),
)

_VIEWS = (("text", "Text"), ("json", "JSON"), ("hex", "Hex"), ("image", "Image"))


# ---------------------------------------------------------------------------
# Helpers (no browser objects)
# ---------------------------------------------------------------------------

def image_type(data: Optional[bytes]) -> Optional[str]:
    """The image type of ``data`` from its first bytes, or None. Never SVG: it can carry script."""
    if not data or not isinstance(data, (bytes, bytearray)):
        return None
    for magic, mime in _IMAGES:
        if data.startswith(magic):
            return mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:2] == b"BM" and len(data) > 26:
        return "image/bmp"
    return None


def hex_dump(data: bytes, limit: int = HEX_DUMP_MAX) -> str:
    """``offset  hex bytes  ascii`` lines, 16 bytes each, up to ``limit`` bytes."""
    lines = []
    shown = data[:limit]
    for offset in range(0, len(shown), 16):
        chunk = shown[offset:offset + 16]
        left, right = chunk[:8].hex(" "), chunk[8:].hex(" ")
        hexes = f"{left:<23}  {right:<23}"
        text = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"{offset:08x}  {hexes}  {text}")
    if len(data) > limit:
        lines.append(f"… the first {limit:,} of {len(data):,} bytes")
    return "\n".join(lines)


def to_bytes(text: Optional[str]) -> Optional[bytes]:
    """A grid's binary text ("0x…", maybe cut with "…") as bytes; other text as UTF-8."""
    if text is None:
        return None
    body = text[:-1] if text.endswith("…") else text
    if body[:2].lower() == "0x":
        try:
            return bytes.fromhex(body[2:])
        except ValueError:
            pass
    return text.encode("utf-8")


def parse_hex(text: str) -> bytes:
    """Typed hex ("0x" optional, spaces and line breaks allowed) as bytes, or ValueError."""
    body = "".join(text.split())
    if body[:2].lower() == "0x":
        body = body[2:]
    if len(body) % 2:
        raise ValueError("Hex needs an even number of digits")
    try:
        return bytes.fromhex(body)
    except ValueError:
        raise ValueError("Not hex: use the digits 0-9 and a-f") from None


def bytes_text(data: Optional[bytes]) -> Optional[str]:
    return None if data is None else "0x" + data.hex()


def looks_like_json(text: Optional[str]) -> bool:
    """Text that is a JSON object or array (worth opening in the JSON view)."""
    if not text or len(text) > VIEW_MAX_BYTES:
        return False
    stripped = text.lstrip()
    if stripped[:1] not in ("{", "["):
        return False
    try:
        json.loads(text)
        return True
    except ValueError:
        return False


def pretty_json(text: str) -> str:
    """Indented JSON, or ValueError with where it went wrong."""
    return json.dumps(load_json(text), indent=2, ensure_ascii=False)


def load_json(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON: {exc.msg} (line {exc.lineno}, column {exc.colno})") from None


def json_result(edited: str, original: Optional[str]) -> str:
    """
    The text to store after an edit in the JSON view. The same JSON as before
    keeps the original text exactly; a value that was on one line stays on
    one line (the view pretty-prints only for reading).
    """
    parsed = load_json(edited)
    if original is not None:
        try:
            if json.loads(original) == parsed:
                return original
        except ValueError:
            pass
        if "\n" not in original:
            return json.dumps(parsed, ensure_ascii=False)
    return edited


def size_text(count: int, binary: bool) -> str:
    unit = "byte" if binary else "character"
    if count < 10_000:
        return f"{count:,} {unit}{'' if count == 1 else 's'}"
    for limit, suffix in ((1024 ** 2, "K"), (1024 ** 3, "M")):
        if count < limit:
            return f"{count / (limit / 1024):,.1f} {suffix}{'B' if binary else ' characters'}"
    return f"{count / 1024 ** 3:,.1f} G{'B' if binary else ' characters'}"


def grid_text(text: Optional[str]) -> Optional[str]:
    """What the grid cell shows for a value: cut past ``MAX_CELL_CHARS``."""
    if text is None or len(text) <= MAX_CELL_CHARS:
        return text
    return text[:MAX_CELL_CHARS] + "…"


def _esc(value) -> str:
    import html

    return html.escape("" if value is None else str(value), quote=True)


# ---------------------------------------------------------------------------
# A plain text box, when the CodeMirror bundle did not load
# ---------------------------------------------------------------------------

class _PlainEditor:
    """The few editor calls the panel makes, on a <textarea>."""

    def __init__(self, host, on_change: Callable, proxies: list) -> None:
        self.area = js.document.createElement("textarea")
        self.area.className = "bk-vp-plain"
        self.area.spellcheck = False
        host.appendChild(self.area)
        proxy = create_proxy(lambda _event: on_change(None))
        proxies.append(proxy)
        self.area.addEventListener("input", proxy)

    def getValue(self) -> str:  # noqa: N802 - the CodeMirror wrapper's names
        return str(self.area.value)

    def setValue(self, text, caret=None) -> None:  # noqa: N802
        self.area.value = text or ""

    def setReadOnly(self, value) -> None:  # noqa: N802
        self.area.readOnly = bool(value)

    def setLanguage(self, _name) -> None:  # noqa: N802
        pass

    def setWrap(self, value) -> None:  # noqa: N802
        self.area.wrap = "soft" if value else "off"

    def length(self) -> int:
        return len(str(self.area.value))

    def focus(self) -> None:
        self.area.focus()

    def destroy(self) -> None:
        self.area.remove()


# ---------------------------------------------------------------------------
# The panel
# ---------------------------------------------------------------------------

class ValuePanel:
    """
    One panel per grid. ``results`` is the grid's ``.bk-results`` box (the
    panel takes its right side), ``host`` the empty element to draw in.
    ``app`` gives ``_ensure_editor``, ``_mount_editor``, ``_toast`` and
    ``_spawn``-style error reporting through ``spawn``.
    """

    def __init__(self, app, results, host, *, width: int = 420,
                 on_width: Optional[Callable[[int], None]] = None,
                 on_close: Optional[Callable[[], None]] = None,
                 spawn: Optional[Callable] = None) -> None:
        self.app = app
        self.results = results
        self.host = host
        self.width = max(MIN_WIDTH, int(width or 420))
        self.on_width = on_width
        self.on_close = on_close
        self.spawn = spawn
        self.is_open = False
        self.suppressed = False   # open, but the grid is not shown (messages, plan)
        self.ctx: Optional[dict] = None
        self.orig: Any = None     # the value as loaded: str, bytes or None
        self.work: Any = None     # the value being edited
        self.binary = False
        self.too_large = False
        self.size = 0
        self.cut_left = False     # showing a cut value that cannot be fetched whole
        self.edited = False       # typed in the editor since the last read
        self.view = "text"
        self.wrap = True
        self.error = ""
        self.bad_image = False    # the bytes looked like a picture but did not decode
        self._filling = False     # the panel is setting the editor's text itself
        self.token = 0
        self.editor = None
        self._editor_task = None
        self._proxies: list = []
        self._timer = None
        self._validate_proxy = None
        self._build()

    # -- DOM ------------------------------------------------------------

    def _build(self) -> None:
        views = "".join(
            f'<button type="button" class="bk-seg-btn" data-vp="view" data-view="{key}" '
            f'aria-pressed="false">{label}</button>' for key, label in _VIEWS)
        self.host.innerHTML = f"""
<div class="bk-vp-grip" data-vp="grip" title="Drag to resize"></div>
<div class="bk-vp-inner">
  <div class="bk-vp-head">
    <span class="mdi mdi-text-box-search-outline"></span>
    <span class="bk-vp-title" data-slot="title">Value</span>
    <span class="bk-vp-type" data-slot="type"></span>
    <span class="bk-spacer"></span>
    <button type="button" class="bk-btn bk-vp-icon" data-vp="close" title="Close the value panel (F7)"
      aria-label="Close the value panel"><span class="mdi mdi-close"></span></button>
  </div>
  <div class="bk-vp-bar">
    <span class="bk-seg" role="group" aria-label="Show as">{views}</span>
    <button type="button" class="bk-btn bk-vp-small" data-vp="format" hidden
      title="Indent the JSON (Ctrl+Shift+F)"><span class="mdi mdi-code-json"></span>Format</button>
    <span class="bk-spacer"></span>
    <label class="bk-vp-wrap"><input type="checkbox" data-vp="wrap" checked> Wrap</label>
  </div>
  <div class="bk-vp-note" data-slot="note" hidden></div>
  <div class="bk-vp-body">
    <div class="bk-vp-editor" data-slot="editor"></div>
    <div class="bk-vp-image" data-slot="image" hidden><img alt="" data-slot="img"></div>
    <div class="bk-vp-empty" data-slot="empty">Select a cell to see its value.</div>
  </div>
  <div class="bk-vp-status"><span data-slot="size"></span><span class="bk-spacer"></span>
    <span data-slot="state"></span><span data-slot="cursor"></span></div>
  <div class="bk-vp-actions">
    <button type="button" class="bk-btn bk-primary" data-vp="apply" disabled
      title="Apply to the cell (Ctrl+Enter); Save writes it to the database">
      <span class="mdi mdi-check"></span>Apply</button>
    <button type="button" class="bk-btn" data-vp="discard" disabled
      title="Put back the value the cell has"><span class="mdi mdi-undo"></span>Discard</button>
    <button type="button" class="bk-btn" data-vp="null" title="Set the cell to NULL">
      <span class="mdi mdi-null"></span>NULL</button>
    <span class="bk-spacer"></span>
    <button type="button" class="bk-btn bk-vp-icon" data-vp="load" title="Load the value from a file"
      aria-label="Load from file"><span class="mdi mdi-file-upload-outline"></span></button>
    <button type="button" class="bk-btn bk-vp-icon" data-vp="save" title="Save the value to a file"
      aria-label="Save to file"><span class="mdi mdi-file-download-outline"></span></button>
    <button type="button" class="bk-btn bk-vp-icon" data-vp="copy" title="Copy the value"
      aria-label="Copy"><span class="mdi mdi-content-copy"></span></button>
  </div>
  <input type="file" data-slot="file" hidden>
</div>"""
        self._on(self.host, "click", self._on_click)
        self._on(self.host, "change", self._on_change)
        self._on(self.host, "pointerdown", self._on_grip)
        self._on(self._slot("file"), "change", self._on_file)
        self._on(self._slot("img"), "error", self._on_bad_image)
        self.results.style.setProperty("--bk-vp-w", f"{self.width}px")

    def _on(self, element, event: str, handler) -> None:
        proxy = create_proxy(handler)
        self._proxies.append(proxy)
        element.addEventListener(event, proxy)

    def _slot(self, name: str):
        return self.host.querySelector(f'[data-slot="{name}"]')

    def _button(self, name: str):
        return self.host.querySelector(f'[data-vp="{name}"]')

    def _run(self, coro, label: str) -> None:
        if self.spawn is not None:
            self.spawn(coro, label)

    # -- open / close -----------------------------------------------------

    def open(self) -> None:
        self.is_open = True
        self._place()
        if self.editor is None and self._editor_task is None:
            self._run(self._ensure_editor(), "value panel editor")

    def close(self) -> None:
        self.is_open = False
        self._place()
        if self.on_close is not None:
            self.on_close()

    def suppress(self, hidden: bool) -> None:
        """The grid is hidden (SQL messages or plan): hide the panel with it."""
        self.suppressed = hidden
        self._place()

    def _place(self) -> None:
        shown = self.is_open and not self.suppressed
        self.host.hidden = not shown
        self.results.classList.toggle("bk-vp-on", shown)

    def focus(self) -> None:
        if self.editor is not None:
            self.editor.focus()

    def destroy(self) -> None:
        if self._timer is not None:
            js.clearTimeout(self._timer)
        if self.editor is not None:
            try:
                self.editor.destroy()
            except Exception:  # noqa: BLE001 - already gone with the tab
                pass
        for proxy in self._proxies:
            try:
                proxy.destroy()
            except Exception:  # noqa: BLE001
                pass
        self._proxies = []

    async def _ensure_editor(self) -> None:
        ok = await self.app._ensure_editor()
        if self.editor is not None:
            return
        host = self._slot("editor")
        callbacks = {
            "onChange": lambda _text: self._changed(),
            "onCursor": lambda line, col: self._cursor(line, col),
            "onRun": lambda: self.apply(),
            "onSave": lambda: self.apply(),
            "onFormat": lambda: self._format(),
        }
        if ok:
            self.editor = self.app._mount_editor(
                host, value="", read_only=True, callbacks=callbacks, label="Cell value",
                language="text", changeText=False, wrap=self.wrap, lineNumbers=True,
                placeholder="")
        else:
            self.editor = _PlainEditor(host, lambda _text: self._changed(), self._proxies)
        self._render()

    # -- the cell -----------------------------------------------------------

    @property
    def dirty(self) -> bool:
        if self.ctx is None:
            return False
        if self.edited:
            return True
        return self.work != self.orig

    def current_key(self):
        return None if self.ctx is None else self.ctx.get("key")

    def reset(self, note: str = "") -> None:
        """The grid was redrawn: forget the cell (an unapplied change is lost, and said so)."""
        lost = self.dirty
        self.token += 1
        self.ctx = None
        self.orig = self.work = None
        self.edited = False
        self.error = ""
        self._render()
        if lost:
            self.app._toast("The value panel's unapplied change was discarded.", kind="warning")
        if note:
            self._note(note, "info")

    async def show(self, ctx: dict, *, force: bool = False) -> bool:
        """Show a cell. Refused (False) while the shown cell has an unapplied change."""
        if (not force and self.ctx is not None and self.dirty
                and ctx.get("key") != self.ctx.get("key")):
            self._note("Apply or discard this change before moving to another cell.", "warning")
            return False
        self.token += 1
        token = self.token
        self.ctx = ctx
        self.edited = False
        self.error = ""
        self.binary = bool(ctx.get("binary"))
        self.too_large = False
        self.cut_left = False
        self.bad_image = False
        text = ctx.get("text")
        if ctx.get("cut") and ctx.get("load") is not None:
            self._loading()
            loaded = await ctx["load"]()
            if token != self.token:
                return True
            if not loaded.get("ok"):
                self.orig = self.work = None
                self.error = loaded.get("error") or "Could not read the value"
                self._render()
                return True
            self._take(loaded)
        else:
            self.cut_left = bool(ctx.get("cut"))
            if self.binary:
                value = to_bytes(text)
                self.size = len(value or b"")
            else:
                value = text
                self.size = len(text or "")
            self.orig = self.work = value
        self.view = self._default_view()
        self._render()
        return True

    def _take(self, loaded: dict) -> None:
        self.binary = bool(loaded.get("binary")) or self.binary
        self.size = int(loaded.get("size") or 0)
        self.too_large = bool(loaded.get("too_large"))
        if loaded.get("null"):
            value = None
        elif self.too_large:
            value = None
        elif loaded.get("binary"):
            value = base64.b64decode(loaded.get("b64") or "")
        elif self.binary:
            value = to_bytes(loaded.get("text"))
        else:
            value = loaded.get("text")
        self.orig = self.work = value

    def _default_view(self) -> str:
        if self.binary:
            return "image" if image_type(self.work) else "hex"
        if (self.ctx or {}).get("json") or looks_like_json(self.work):
            return "json"
        return "text"

    # -- reading the editor ------------------------------------------------

    def _read(self) -> None:
        """Bring typed changes into ``work`` (ValueError when they do not parse)."""
        if not self.edited or self.editor is None:
            return
        typed = str(self.editor.getValue())
        if self.view == "text":
            if self.binary:
                self.work = parse_hex(typed)
            else:
                self.work = typed
        elif self.view == "json":
            original = self.orig if isinstance(self.orig, str) else None
            self.work = json_result(typed, original)
        self.edited = False

    # -- actions ------------------------------------------------------------

    def apply(self) -> None:
        ctx = self.ctx
        if ctx is None or not ctx.get("editable"):
            return
        try:
            self._read()
        except ValueError as exc:
            self._note(str(exc), "error")
            return
        if ctx.get("json") and isinstance(self.work, str):
            try:
                load_json(self.work)
            except ValueError as exc:
                self._note(f"{exc}. The column holds JSON.", "error")
                return
        if self.work == self.orig:
            self._render()
            return
        new = bytes_text(self.work) if self.binary and self.work is not None else self.work
        old = bytes_text(self.orig) if self.binary and self.orig is not None else self.orig
        if ctx["apply"](new, old) is False:
            return
        self.orig = self.work
        self.too_large = False
        self.cut_left = False
        self._render()
        self._note("Applied to the cell. Save writes it to the database.", "ok")

    def discard(self) -> None:
        self.edited = False
        self.work = self.orig
        self.error = ""
        self._render()

    def set_null(self) -> None:
        ctx = self.ctx
        if ctx is None or not ctx.get("editable"):
            return
        self.edited = False
        self.work = None
        self.too_large = False
        self.apply()

    def _fill(self, text: str) -> None:
        """Put text in the editor without it counting as an edit."""
        self._filling = True
        try:
            self.editor.setValue(text, 0)
        finally:
            self._filling = False

    def _changed(self) -> None:
        if self._filling or self.ctx is None or not self.ctx.get("editable") or self.view not in ("text", "json"):
            return
        self.edited = True
        self._sync_buttons()
        if self.view == "json":
            self._validate_soon()

    def _cursor(self, line, col) -> None:
        slot = self._slot("cursor")
        if slot is not None:
            slot.textContent = f"Ln {line}, Col {col}"

    def _format(self) -> None:
        if self.view != "json" or self.editor is None:
            return
        try:
            self._fill(pretty_json(str(self.editor.getValue())))
        except ValueError as exc:
            self._note(str(exc), "error")

    def _switch(self, view: str) -> None:
        if view == self.view:
            return
        try:
            self._read()
        except ValueError as exc:
            self._note(str(exc), "error")
            return
        self.view = view
        self._render()

    # -- events -------------------------------------------------------------

    def _on_click(self, event) -> None:
        target = event.target
        button = target.closest("[data-vp]") if hasattr(target, "closest") else None
        if not button or button.disabled:
            return
        action = str(button.getAttribute("data-vp"))
        if action == "close":
            self.close()
        elif action == "view":
            self._switch(str(button.getAttribute("data-view")))
        elif action == "format":
            self._format()
        elif action == "apply":
            self.apply()
        elif action == "discard":
            self.discard()
        elif action == "null":
            self.set_null()
        elif action == "load":
            picker = self._slot("file")
            picker.value = ""
            picker.click()
        elif action == "save":
            self._save_file()
        elif action == "copy":
            self._run(self._copy(), "copy value")

    def _on_change(self, event) -> None:
        target = event.target
        if str(target.getAttribute("data-vp") or "") == "wrap":
            self.wrap = bool(target.checked)
            if self.editor is not None:
                self.editor.setWrap(self.wrap)

    def _on_grip(self, event) -> None:
        target = event.target
        if str(target.getAttribute("data-vp") or "") != "grip":
            return
        event.preventDefault()
        right = float(self.results.getBoundingClientRect().right)
        room = float(self.results.getBoundingClientRect().width)

        def move(ev) -> None:
            width = int(right - float(ev.clientX))
            self.width = max(MIN_WIDTH, min(width, int(room) - 200))
            self.results.style.setProperty("--bk-vp-w", f"{self.width}px")

        def up(_ev) -> None:
            js.document.removeEventListener("pointermove", move_proxy)
            js.document.removeEventListener("pointerup", up_proxy)
            self.host.classList.remove("bk-vp-dragging")
            move_proxy.destroy()
            up_proxy.destroy()
            if self.on_width is not None:
                self.on_width(self.width)

        move_proxy = create_proxy(move)
        up_proxy = create_proxy(up)
        self.host.classList.add("bk-vp-dragging")
        js.document.addEventListener("pointermove", move_proxy)
        js.document.addEventListener("pointerup", up_proxy)

    def _on_bad_image(self, _event) -> None:
        if self.view != "image" or not self._slot("img").getAttribute("src"):
            return
        self.bad_image = True
        self.view = "hex"
        self._render()
        self._note("The bytes start like a picture, but the browser cannot show them.", "info")

    def _on_file(self, _event) -> None:
        files = self._slot("file").files
        if files is None or files.length == 0:
            return
        self._run(self._load_file(files.item(0)), "load file")

    async def _load_file(self, file) -> None:
        ctx = self.ctx
        if ctx is None or not ctx.get("editable"):
            return
        size = int(file.size)
        if size > VIEW_MAX_BYTES:
            self._note(f"{file.name} is {size_text(size, True)}; the panel takes files up to "
                       f"{size_text(VIEW_MAX_BYTES, True)}.", "error")
            return
        data = (await file.arrayBuffer()).to_bytes()
        if self.ctx is not ctx:
            return
        if self.binary:
            self.work = data
        else:
            try:
                self.work = data.decode("utf-8")
            except UnicodeDecodeError:
                self._note(f"{file.name} is not UTF-8 text; this column holds text.", "error")
                return
        self.size = len(self.work)
        self.edited = False
        self.too_large = False
        self.bad_image = False
        self.view = self._default_view()
        self._render()
        self._note(f"Loaded {file.name}. Apply puts it in the cell.", "info")

    def _save_file(self) -> None:
        ctx = self.ctx or {}
        stem = ctx.get("filename") or "value"
        try:
            self._read()
        except ValueError as exc:
            self._note(str(exc), "error")
            return
        if self.work is None:
            if ctx.get("download") and (self.too_large or self.cut_left):
                _download(ctx["download"])
            return
        if self.binary:
            data, mime, ext = self.work, image_type(self.work) or "application/octet-stream", ".bin"
            if mime.startswith("image/"):
                ext = "." + mime.split("/")[1].replace("x-icon", "ico").replace("jpeg", "jpg")
        else:
            data = self.work.encode("utf-8")
            is_json = ctx.get("json") or looks_like_json(self.work)
            mime = "application/json" if is_json else "text/plain"
            ext = ".json" if is_json else ".txt"
        blob = js.Blob.new(to_js([to_js(data)]), to_js({"type": mime},
                                                       dict_converter=js.Object.fromEntries))
        url = js.URL.createObjectURL(blob)
        _download(url, stem + ext)
        revoke = create_proxy(lambda: js.URL.revokeObjectURL(url))
        self._proxies.append(revoke)
        js.setTimeout(revoke, 10_000)

    async def _copy(self) -> None:
        try:
            self._read()
        except ValueError as exc:
            self._note(str(exc), "error")
            return
        if self.work is None:
            return
        text = bytes_text(self.work) if self.binary else self.work
        try:
            await js.navigator.clipboard.writeText(text)
            self.app._toast("Copied the value.", kind="success")
        except Exception:  # noqa: BLE001 - permission or an insecure context
            self.app._toast("The browser refused the clipboard.", kind="error")

    # -- JSON checking while typing -------------------------------------------

    def _validate_soon(self) -> None:
        if self.editor is None or int(self.editor.length()) > JSON_LIVE_MAX:
            return
        if self._validate_proxy is None:
            self._validate_proxy = create_proxy(self._validate)
            self._proxies.append(self._validate_proxy)
        if self._timer is not None:
            js.clearTimeout(self._timer)
        self._timer = js.setTimeout(self._validate_proxy, 500)

    def _validate(self) -> None:
        self._timer = None
        if self.view != "json" or self.editor is None:
            return
        try:
            load_json(str(self.editor.getValue()))
            self._state("Valid JSON", "ok")
        except ValueError as exc:
            self._state(str(exc), "error")

    # -- drawing ----------------------------------------------------------------

    def _loading(self) -> None:
        self._slot("empty").hidden = False
        self._slot("empty").textContent = "Loading the whole value…"
        self._slot("editor").hidden = True
        self._slot("image").hidden = True

    def _note(self, text: str, kind: str = "info") -> None:
        note = self._slot("note")
        if note is None:
            return
        note.hidden = not text
        note.className = f"bk-vp-note bk-vp-{kind}"
        note.textContent = text

    def _state(self, text: str, kind: str = "") -> None:
        slot = self._slot("state")
        if slot is not None:
            slot.textContent = text
            slot.className = f"bk-vp-{kind}" if kind else ""

    def _render(self) -> None:
        ctx = self.ctx
        title, kind_label = self._slot("title"), self._slot("type")
        empty, editor_host, image = self._slot("empty"), self._slot("editor"), self._slot("image")
        self._note("")
        self._state("")
        self._slot("cursor").textContent = ""
        if ctx is None:
            title.textContent = "Value"
            kind_label.textContent = ""
            empty.hidden = False
            empty.textContent = "Select a cell to see its value."
            editor_host.hidden = image.hidden = True
            self._slot("size").textContent = ""
            self._sync_buttons()
            return
        title.textContent = ctx.get("column") or ""
        title.title = ctx.get("column") or ""
        badges = [ctx.get("type") or ""]
        if ctx.get("pk"):
            badges.append("PK")
        kind_label.textContent = " · ".join(b for b in badges if b)
        for button in self.host.querySelectorAll('[data-vp="view"]'):
            view = str(button.getAttribute("data-view"))
            button.setAttribute("aria-pressed", "true" if view == self.view else "false")
            button.disabled = not self._view_ok(view)
        self._button("format").hidden = not (self.view == "json" and ctx.get("editable"))
        if self.error:
            empty.hidden = False
            empty.textContent = self.error
            editor_host.hidden = image.hidden = True
            self._slot("size").textContent = ""
            self._sync_buttons()
            return
        self._slot("size").textContent = ("NULL" if self.work is None and not self.too_large
                                          else size_text(self._size(), self.binary))
        if self.too_large:
            empty.hidden = False
            empty.innerHTML = (
                f'<span class="mdi mdi-weight"></span> This value is '
                f'{_esc(size_text(self.size, self.binary))}, more than the panel shows '
                f'({_esc(size_text(VIEW_MAX_BYTES, self.binary))}). Save it to a file instead.')
            editor_host.hidden = image.hidden = True
            self._sync_buttons()
            return
        if self.view == "image" and image_type(self.work) and not self.bad_image:
            mime = image_type(self.work)
            self._slot("img").src = f"data:{mime};base64," + base64.b64encode(self.work).decode()
            image.hidden = False
            editor_host.hidden = empty.hidden = True
            self._sync_buttons()
            return
        image.hidden = True
        self._slot("img").removeAttribute("src")
        if self.editor is None:
            empty.hidden = False
            empty.textContent = "Loading the editor…"
            editor_host.hidden = True
            self._sync_buttons()
            return
        empty.hidden = True
        editor_host.hidden = False
        text, language, editable = self._editor_text()
        self.editor.setLanguage(language)
        self.editor.setReadOnly(not editable)
        self.editor.setWrap(self.wrap and self.view != "hex")  # a dump's columns must line up
        self._button("wrap").disabled = self.view == "hex"
        self._fill(text)
        self.edited = False
        notes = []
        if ctx.get("reason"):
            notes.append(ctx["reason"])
        if self.cut_left:
            notes.append(ctx.get("cut_note") or "Only the start of the value is shown.")
        if self.view == "json" and isinstance(self.work, str):
            try:
                load_json(self.work)
                self._state("Valid JSON", "ok")
            except ValueError as exc:
                self._state(str(exc), "error")
        if self.view == "text" and self.binary and editable:
            notes.append("Edit the bytes as hex; spaces and line breaks are ignored.")
        if self.work is None and editable:
            notes.append("The cell is NULL; typing gives it a value.")
        if notes:
            self._note(" ".join(notes), "info")
        self._sync_buttons()

    def _size(self) -> int:
        if self.work is None:
            return 0
        return len(self.work)

    def _view_ok(self, view: str) -> bool:
        if self.too_large or self.error:
            return False
        if view == "json":
            return not self.binary
        if view == "image":
            return self.binary and bool(image_type(self.work)) and not self.bad_image
        return True

    def _editor_text(self) -> tuple:
        """``(text, language, editable)`` for the current view."""
        ctx = self.ctx or {}
        editable = bool(ctx.get("editable"))
        value = self.work
        if self.view == "hex":
            data = value if isinstance(value, bytes) else (value or "").encode("utf-8")
            return hex_dump(data), "text", False
        if self.view == "json":
            try:
                return pretty_json(value or ""), "json", editable
            except ValueError:
                return value or "", "json", editable
        if self.binary:
            return bytes_text(value) or "", "text", editable
        return value or "", "text", editable

    def _sync_buttons(self) -> None:
        ctx = self.ctx or {}
        editable = bool(ctx.get("editable")) and not self.error
        dirty = self.dirty
        apply_btn, discard = self._button("apply"), self._button("discard")
        apply_btn.hidden = discard.hidden = not ctx.get("editable")
        apply_btn.disabled = not (editable and dirty)
        discard.disabled = not dirty
        null = self._button("null")
        null.hidden = not ctx.get("editable")
        null.disabled = not editable or (self.work is None and not self.too_large) \
            or ctx.get("nullable") is False
        null.title = ("The column is NOT NULL" if ctx.get("nullable") is False
                      else "Set the cell to NULL")
        self._button("load").hidden = not ctx.get("editable")
        self._button("load").disabled = not editable
        has_value = self.work is not None or (self.too_large and ctx.get("download"))
        self._button("save").disabled = not has_value or bool(self.error)
        self._button("copy").disabled = self.work is None or bool(self.error)
        if dirty:
            self._state("Changed: Ctrl+Enter applies", "warn")


def _download(url: str, filename: str = "") -> None:
    anchor = js.document.createElement("a")
    anchor.href = url
    anchor.download = filename
    anchor.style.display = "none"
    js.document.body.appendChild(anchor)
    anchor.click()
    anchor.remove()
