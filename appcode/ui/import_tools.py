"""
Pure helpers for the Import CSV dialog: reading the start of a file for the
preview, guessing its delimiter, and matching its columns to the table's.

No browser objects, so ``tests/test_import_tools.py`` runs them in CPython.
The server reads the whole file again with the same options
(``services/csv_import.py``); this only drives the preview and the mapping.
"""
from __future__ import annotations

import csv
import io
import re
from typing import Optional

SAMPLE_BYTES = 256 * 1024
PREVIEW_ROWS = 8
DELIMITERS = ((",", "Comma ,"), (";", "Semicolon ;"), ("\t", "Tab"), ("|", "Pipe |"))
ENCODINGS = (("utf-8", "UTF-8"), ("utf-16", "UTF-16"), ("cp1252", "Windows-1252"),
             ("latin-1", "Latin-1 (ISO-8859-1)"))
_CODECS = {"utf-8": "utf-8-sig", "utf-16": "utf-16", "cp1252": "cp1252", "latin-1": "latin-1"}


def decode_sample(raw: bytes, encoding: str, cut: bool) -> str:
    """
    The sample as text. ``cut``: the file goes on past it, so a character
    split at the end (and the unfinished last line) is dropped.
    """
    codec = _CODECS.get(encoding, "utf-8-sig")
    text = raw.decode(codec, errors="replace")
    if cut:
        last = max(text.rfind("\n"), text.rfind("\r"))
        text = text[:last + 1] if last >= 0 else ""
    return text


def guess_encoding(raw: bytes) -> str:
    """UTF-16 when it starts with its byte-order mark, UTF-8 when it decodes, else Windows-1252."""
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return "utf-16"
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        # A character cut at the end of the sample is not a reason to give up.
        if exc.start < len(raw) - 4:
            return "cp1252"
    return "utf-8"


def sniff_delimiter(text: str) -> str:
    """The delimiter that splits the first lines into the same, largest number of fields."""
    lines = [line for line in text.splitlines()[:20] if line.strip()]
    if not lines:
        return ","
    best, best_score = ",", -1
    for delimiter, _label in DELIMITERS:
        try:
            counts = [len(row) for row in csv.reader(lines, delimiter=delimiter)]
        except csv.Error:
            continue
        if not counts or max(counts) < 2:
            continue
        common = max(set(counts), key=counts.count)
        score = counts.count(common) * 1000 + common
        if score > best_score:
            best, best_score = delimiter, score
    return best


def parse_rows(text: str, delimiter: str, quote: str, limit: int = PREVIEW_ROWS + 1) -> list:
    """The first ``limit`` records of ``text`` (blank lines skipped)."""
    stream = io.StringIO(text, newline="")
    if quote:
        reader = csv.reader(stream, delimiter=delimiter, quotechar=quote)
    else:
        reader = csv.reader(stream, delimiter=delimiter, quoting=csv.QUOTE_NONE)
    rows: list = []
    try:
        for row in reader:
            if not row:
                continue
            rows.append(row)
            if len(rows) >= limit:
                break
    except csv.Error:
        pass
    return rows


def source_columns(rows: list, header: bool) -> list:
    """The file's columns: ``[(index, label)]``, labels from the header row when there is one."""
    width = max((len(row) for row in rows), default=0)
    names = rows[0] if header and rows else []
    out = []
    for i in range(width):
        title = names[i].strip() if i < len(names) and names[i].strip() else ""
        out.append((i, f"{i + 1}: {title}" if title else f"Column {i + 1}"))
    return out


def _key(name: str) -> str:
    return re.sub(r"[\s_\-.]+", "", (name or "").strip().lower())


def auto_map(table_columns: list, rows: list, header: bool) -> dict:
    """
    ``{table column: file column index}``: by header name (case, spaces and
    underscores ignored), or by position when the file has no header.
    Computed columns are never mapped.
    """
    width = max((len(row) for row in rows), default=0)
    wanted = [col for col in table_columns if not col.get("computed")]
    mapping: dict = {}
    if header and rows:
        names = {}
        for i, title in enumerate(rows[0]):
            names.setdefault(_key(title), i)
        for col in wanted:
            index = names.get(_key(col["name"]))
            if index is not None:
                mapping[col["name"]] = index
        return mapping
    for i, col in enumerate(wanted):
        if i < width:
            mapping[col["name"]] = i
    return mapping


def preview_cells(rows: list, header: bool, mapping: dict, columns: list,
                  null_text: str, empty_null: bool) -> list:
    """The preview grid: one list per data row, values as the import will read them."""
    body = rows[1:] if header else rows
    out = []
    for row in body[:PREVIEW_ROWS]:
        values: list = []
        for name in columns:
            index = mapping.get(name)
            if index is None:
                values.append(("default", None))
                continue
            text = row[index] if index < len(row) else ""
            if (null_text and text == null_text) or (text == "" and empty_null):
                values.append(("null", None))
            else:
                values.append(("value", text))
        out.append(values)
    return out


def delimiter_label(delimiter: str) -> Optional[str]:
    return dict(DELIMITERS).get(delimiter)


def format_bytes(count) -> str:
    """``1.5 MB``-style sizes for the upload and the import's progress."""
    try:
        count = float(count)
    except (TypeError, ValueError):
        return "?"
    for unit in ("bytes", "KB", "MB", "GB"):
        if count < 1024 or unit == "GB":
            return f"{int(count):,} bytes" if unit == "bytes" else f"{count:,.1f} {unit}"
        count /= 1024
    return f"{count:,.1f} GB"  # pragma: no cover - the loop returns
