"""The value panel's pure helpers (ui/value_panel.py) and the values behind it."""
from __future__ import annotations

import base64
import json

import pytest

from ui import value_panel as vp


def test_image_type_sniffs_pictures_but_never_svg():
    assert vp.image_type(b"\x89PNG\r\n\x1a\n" + b"\0" * 20) == "image/png"
    assert vp.image_type(b"\xff\xd8\xff\xe0rest") == "image/jpeg"
    assert vp.image_type(b"GIF89a....") == "image/gif"
    assert vp.image_type(b"RIFF\0\0\0\0WEBPVP8 ") == "image/webp"
    assert vp.image_type(b"\x89PNG\r\n") is None          # a cut-off header is not a picture
    assert vp.image_type(b"<svg xmlns='http://www.w3.org/2000/svg'/>") is None
    assert vp.image_type(b"") is None and vp.image_type(None) is None


def test_hex_dump_lines_and_limit():
    dump = vp.hex_dump(bytes(range(20)))
    first, second = dump.splitlines()
    assert first.startswith("00000000  00 01 02 03 04 05 06 07  08 09")
    assert first.endswith("................")
    assert second.startswith("00000010  10 11 12 13")
    text = vp.hex_dump(b"Hi!\n")
    assert text.endswith("Hi!.")
    capped = vp.hex_dump(b"x" * 100, limit=32)
    assert capped.splitlines()[-1] == "… the first 32 of 100 bytes"
    assert len(capped.splitlines()) == 3


def test_bytes_round_trip_through_grid_text():
    data = bytes(range(256))
    text = vp.bytes_text(data)
    assert text.startswith("0x000102") and vp.to_bytes(text) == data
    assert vp.to_bytes("0x0a0b…") == b"\x0a\x0b"           # the grid's cut marker
    assert vp.to_bytes("plain") == b"plain"                # typed as text into a binary cell
    assert vp.to_bytes(None) is None
    assert vp.parse_hex("0x 0a0B\n ff") == b"\x0a\x0b\xff"
    with pytest.raises(ValueError, match="even"):
        vp.parse_hex("abc")
    with pytest.raises(ValueError, match="Not hex"):
        vp.parse_hex("zz")


def test_json_view_keeps_the_original_text_when_nothing_changed():
    original = '{"b": 1, "a": [1, 2]}'
    pretty = vp.pretty_json(original)
    assert pretty.startswith("{\n  ")
    assert vp.json_result(pretty, original) == original           # same JSON: untouched
    changed = vp.json_result(pretty.replace("1,", "5,", 1), original)
    assert "\n" not in changed and json.loads(changed) == {"b": 5, "a": [1, 2]}
    multi = '{\n  "a": 1\n}'
    assert vp.json_result('{\n    "a": 2\n}', multi) == '{\n    "a": 2\n}'   # kept as typed
    with pytest.raises(ValueError, match=r"Invalid JSON: .*line 1"):
        vp.json_result("{nope", original)
    assert vp.looks_like_json('  [1, 2]') and not vp.looks_like_json("[1, 2")
    assert not vp.looks_like_json("42") and not vp.looks_like_json(None)


def test_size_and_grid_text():
    assert vp.size_text(1, False) == "1 character"
    assert vp.size_text(2048, True) == "2,048 bytes"
    assert vp.size_text(5 * 1024 * 1024, True) == "5.0 MB"
    assert vp.size_text(40_000, False) == "39.1 K characters"
    long = "x" * (vp.MAX_CELL_CHARS + 5)
    assert vp.grid_text(long) == "x" * vp.MAX_CELL_CHARS + "…"
    assert vp.grid_text("short") == "short" and vp.grid_text(None) is None


def test_full_value_is_uncut_up_to_the_limit():
    from services.values import MAX_CELL_CHARS, VIEW_MAX_BYTES, cell, full_value

    long = "y" * (MAX_CELL_CHARS * 2)
    assert cell(long)[1] is True
    whole = full_value(long)
    assert whole["text"] == long and whole["size"] == len(long) and not whole["too_large"]
    blob = bytes(range(256)) * 40
    data = full_value(blob)
    assert data["binary"] and base64.b64decode(data["b64"]) == blob
    assert full_value(None)["null"]
    assert json.loads(full_value({"k": [1, "é"]})["text"]) == {"k": [1, "é"]}
    huge = full_value(b"\0" * (VIEW_MAX_BYTES + 1))
    assert huge["too_large"] and huge["b64"] is None and huge["size"] == VIEW_MAX_BYTES + 1


def test_save_preview_cuts_a_huge_statement():
    from services.sqlops import PREVIEW_MAX_CHARS, _preview

    assert _preview("UPDATE t SET a = 1;") == "UPDATE t SET a = 1;"
    shown = _preview("UPDATE t SET a = '" + "z" * 100_000 + "';")
    assert len(shown) < PREVIEW_MAX_CHARS + 100 and "characters in all" in shown
