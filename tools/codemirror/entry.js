// Basilisk's SQL editor: CodeMirror 6 with @codemirror/lang-sql, bundled into
// one classic script that sets window.BkEditor. Built by build.sh into
// appcode/vendor/codemirror/.
//
// Why a classic script and a global: the page loads it on demand by appending
// a <script> and waiting for its load event (Monguana's pattern). Pyodide then
// calls BkEditor.create() through the FFI. Nothing here uses eval, workers or
// remote resources, so pytincture's CSP (script-src 'self' …) is satisfied.
//
// The API is value-in / value-out: Python never touches CodeMirror's state.

import { EditorView, keymap, placeholder as placeholderExt, drawSelection, highlightActiveLine,
  lineNumbers, highlightActiveLineGutter } from "@codemirror/view";
import { EditorState, EditorSelection, Prec, Compartment } from "@codemirror/state";
import { defaultKeymap, history, historyKeymap, indentWithTab } from "@codemirror/commands";
import { sql, PostgreSQL, MySQL, MSSQL, SQLite, StandardSQL } from "@codemirror/lang-sql";
import { autocompletion, completionKeymap, closeBrackets, closeBracketsKeymap,
  completionStatus, closeCompletion } from "@codemirror/autocomplete";
import { syntaxHighlighting, HighlightStyle, bracketMatching, indentOnInput }
  from "@codemirror/language";
import { search, searchKeymap, highlightSelectionMatches } from "@codemirror/search";
import { tags as t } from "@lezer/highlight";

const VERSION = "2";

const DIALECTS = { PostgreSQL, MySQL, MSSQL, SQLite, StandardSQL };

// ── Look ─────────────────────────────────────────────────────────────────────
// Basilisk's slate + amber palette (basilisk.py _CSS).

const theme = EditorView.theme({
  "&": {
    color: "#e2e8f0",
    backgroundColor: "#0b1220",
    fontSize: "13px",
    height: "100%",
  },
  "&.cm-focused": { outline: "none" },
  ".cm-scroller": {
    fontFamily: "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
    lineHeight: "1.5",
    overflow: "auto",
  },
  ".cm-content": { padding: "8px 0", caretColor: "#fbbf24" },
  ".cm-line": { padding: "0 10px" },
  "&.cm-focused .cm-cursor": { borderLeftColor: "#fbbf24" },
  "&.cm-focused .cm-selectionBackground, .cm-selectionBackground, ::selection": {
    backgroundColor: "#3b3416 !important",
  },
  ".cm-activeLine": { backgroundColor: "rgba(148,163,184,.06)" },
  ".cm-gutters": { backgroundColor: "#0b1220", color: "#475569", border: "none",
    borderRight: "1px solid #1f2937" },
  ".cm-activeLineGutter": { backgroundColor: "#111827", color: "#94a3b8" },
  ".cm-placeholder": { color: "#64748b" },
  ".cm-matchingBracket": { backgroundColor: "rgba(245,158,11,.25)", outline: "none" },
  ".cm-selectionMatch": { backgroundColor: "rgba(245,158,11,.12)" },
  ".cm-searchMatch": { backgroundColor: "rgba(56,189,248,.25)", outline: "1px solid #0ea5e9" },
  ".cm-searchMatch.cm-searchMatch-selected": { backgroundColor: "rgba(245,158,11,.35)" },
  ".cm-panels": { backgroundColor: "#111827", color: "#e2e8f0", borderTop: "1px solid #334155" },
  ".cm-panels input, .cm-panels button": { font: "12px system-ui, sans-serif" },
  ".cm-textfield": { backgroundColor: "#0f172a", color: "#e2e8f0", border: "1px solid #334155",
    borderRadius: "4px" },
  ".cm-button": { backgroundImage: "none", backgroundColor: "#1e293b", color: "#e2e8f0",
    border: "1px solid #334155", borderRadius: "4px" },
  ".cm-tooltip": {
    backgroundColor: "#111827", border: "1px solid #334155", color: "#e2e8f0",
    borderRadius: "6px", overflow: "hidden",
  },
  ".cm-tooltip-autocomplete > ul": {
    fontFamily: "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
    fontSize: "12px", maxHeight: "18em",
  },
  ".cm-tooltip-autocomplete > ul > li[aria-selected]": {
    backgroundColor: "#92400e", color: "#fffbeb",
  },
  ".cm-completionDetail": { color: "#94a3b8", fontStyle: "normal", marginLeft: "1em" },
  ".cm-completionMatchedText": { textDecoration: "none", color: "#fcd34d" },
  // The statement the last run used, or the error position.
  ".bk-cm-mark": { backgroundColor: "rgba(245,158,11,.10)" },
  ".bk-cm-error": { textDecoration: "underline wavy #f87171", textUnderlineOffset: "3px" },
}, { dark: true });

const highlight = HighlightStyle.define([
  { tag: t.keyword, color: "#fbbf24", fontWeight: "600" },
  { tag: [t.typeName, t.standard(t.name)], color: "#7dd3fc" },
  { tag: t.string, color: "#86efac" },
  { tag: [t.number, t.bool, t.null], color: "#f9a8d4" },
  { tag: [t.function(t.variableName), t.special(t.name)], color: "#c4b5fd" },
  { tag: [t.name, t.variableName, t.propertyName], color: "#e2e8f0" },
  { tag: t.special(t.string), color: "#e2e8f0" },  // quoted identifiers
  { tag: [t.comment, t.lineComment, t.blockComment], color: "#64748b", fontStyle: "italic" },
  { tag: [t.operator, t.punctuation, t.paren, t.separator, t.bracket], color: "#94a3b8" },
]);

// ── Editors ──────────────────────────────────────────────────────────────────

function sqlConfig(options) {
  const dialect = DIALECTS[options.dialect] || StandardSQL;
  const config = { dialect, upperCaseKeywords: true };
  if (options.schema) config.schema = options.schema;
  if (options.defaultSchema) config.defaultSchema = options.defaultSchema;
  return sql(config);
}

function create(host, options = {}) {
  const call = (name, ...args) => {
    const handler = options[name];
    if (typeof handler === "function") handler(...args);
  };
  const language = new Compartment();
  const readOnly = new Compartment();
  let current = { dialect: options.dialect, schema: options.schema,
    defaultSchema: options.defaultSchema };

  // Ctrl+Enter: the statement at the caret (or the selection). Ctrl+Shift+Enter
  // and Alt+X: the whole script, as in DBeaver. Ctrl+S saves, if asked to.
  const keys = [
    { key: "Mod-Enter", run: () => { call("onRun"); return true; } },
    { key: "Mod-Shift-Enter", run: () => { call("onRunScript"); return true; } },
    { key: "Alt-x", run: () => { call("onRunScript"); return true; } },
    { key: "Mod-Shift-f", run: () => { call("onFormat"); return true; } },
    // DBeaver's key for the execution plan of the statement at the caret.
    { key: "Mod-Shift-e", run: () => { call("onExplain"); return true; }, preventDefault: true },
    { key: "Mod-s", run: () => { call("onSave"); return true; }, preventDefault: true },
  ];

  const extensions = [
    history(),
    drawSelection(),
    indentOnInput(),
    bracketMatching(),
    closeBrackets(),
    highlightSelectionMatches(),
    search({ top: true }),
    language.of(sqlConfig(current)),
    syntaxHighlighting(highlight),
    theme,
    autocompletion({ activateOnTyping: true, icons: false }),
    Prec.highest(keymap.of(keys)),
    Prec.high(keymap.of(completionKeymap)),
    keymap.of([...closeBracketsKeymap, ...defaultKeymap, ...searchKeymap, ...historyKeymap,
      indentWithTab]),
    // Escape that only closes a completion list goes no further: wapyt's
    // modal closes on any Escape reaching the document.
    Prec.highest(EditorView.domEventHandlers({
      keydown: (event, view) => {
        if (event.key === "Escape" && completionStatus(view.state) !== null) {
          closeCompletion(view);
          event.preventDefault();
          event.stopPropagation();
          return true;
        }
        return false;
      },
    })),
    EditorView.updateListener.of((update) => {
      if (update.docChanged) call("onChange", update.state.doc.toString());
      if (update.selectionSet || update.docChanged) {
        const head = update.state.selection.main.head;
        const line = update.state.doc.lineAt(head);
        call("onCursor", line.number, head - line.from + 1);
      }
    }),
    readOnly.of(EditorState.readOnly.of(Boolean(options.readOnly))),
    EditorView.contentAttributes.of({
      "aria-label": options.label || "SQL editor",
      spellcheck: "false", autocorrect: "off", autocapitalize: "off",
    }),
    marks,
  ];
  if (options.placeholder) extensions.push(placeholderExt(options.placeholder));
  if (options.lineNumbers !== false) {
    extensions.push(lineNumbers(), highlightActiveLineGutter(), highlightActiveLine());
  }
  if (options.wrap) extensions.push(EditorView.lineWrapping);

  const view = new EditorView({
    parent: host,
    state: EditorState.create({ doc: options.value || "", extensions }),
  });

  const clamp = (n) => Math.max(0, Math.min(Number(n) || 0, view.state.doc.length));

  return {
    getValue: () => view.state.doc.toString(),
    setValue: (text, caret) => {
      const insert = String(text ?? "");
      const anchor = caret == null ? insert.length : Math.max(0, Math.min(caret, insert.length));
      view.dispatch({ changes: { from: 0, to: view.state.doc.length, insert },
        selection: { anchor } });
    },
    // Insert at the caret, replacing the selection (history, snippets).
    insert: (text) => {
      const range = view.state.selection.main;
      view.dispatch(view.state.update({
        changes: { from: range.from, to: range.to, insert: String(text) },
        selection: { anchor: range.from + String(text).length },
        scrollIntoView: true,
      }));
      view.focus();
    },
    getSelection: () => {
      const range = view.state.selection.main;
      return { from: range.from, to: range.to,
        text: view.state.sliceDoc(range.from, range.to), head: range.head };
    },
    select: (from, to) => {
      view.dispatch({ selection: EditorSelection.single(clamp(from), clamp(to ?? from)),
        scrollIntoView: true });
      view.focus();
    },
    // Tint the range a run used; an error range gets a wavy underline.
    mark: (from, to, kind) => {
      view.dispatch({ effects: setMark.of(from == null ? null
        : { from: clamp(from), to: clamp(to), kind: kind || "run" }) });
    },
    focus: () => view.focus(),
    setSchema: (schema, defaultSchema) => {
      current = { ...current, schema, defaultSchema };
      view.dispatch({ effects: language.reconfigure(sqlConfig(current)) });
    },
    setDialect: (dialect) => {
      current = { ...current, dialect };
      view.dispatch({ effects: language.reconfigure(sqlConfig(current)) });
    },
    setReadOnly: (value) => view.dispatch({
      effects: readOnly.reconfigure(EditorState.readOnly.of(Boolean(value))),
    }),
    destroy: () => view.destroy(),
    dom: view.dom,
  };
}

// ── Marks: the last run's statement and an error position ────────────────────

import { StateField, StateEffect } from "@codemirror/state";
import { Decoration } from "@codemirror/view";

const setMark = StateEffect.define();
const marks = StateField.define({
  create: () => Decoration.none,
  update(value, tr) {
    value = value.map(tr.changes);
    for (const effect of tr.effects) {
      if (!effect.is(setMark)) continue;
      if (!effect.value || effect.value.from >= effect.value.to) {
        value = Decoration.none;
      } else {
        const cls = effect.value.kind === "error" ? "bk-cm-error" : "bk-cm-mark";
        value = Decoration.set([Decoration.mark({ class: cls })
          .range(effect.value.from, effect.value.to)]);
      }
    }
    return value;
  },
  provide: (field) => EditorView.decorations.from(field),
});

window.BkEditor = { VERSION, create };
