"""Frontmatter with JSON-escaped emoji must not kill a migrate run.

Real incident: two session notes whose `description:` was written as a
json.dumps() scalar (ensure_ascii=True → escaped surrogate pair for 🔴) made
`skillmem migrate` fail those files with
``UnicodeEncodeError: surrogates not allowed`` — silently losing the records.
Cyrillic fixtures are intentional: bilingual content is a feature.
"""

from __future__ import annotations

from pathlib import Path

from skillmem.migrate import desurrogate, parse_file

# What PyYAML hands back for description: "🔴 report"
LONE_PAIR = "🔴"
LONE_HALF = "\ud83d"


def test_surrogate_pair_is_recombined():
    assert desurrogate(LONE_PAIR) == "🔴"


def test_lone_surrogate_is_replaced_not_raised():
    out = desurrogate(f"a{LONE_HALF}b")
    out.encode("utf-8")  # must not raise
    assert out.startswith("a") and out.endswith("b")


def test_clean_text_is_untouched():
    assert desurrogate("обычный текст 🔴 ok") == "обычный текст 🔴 ok"


def test_nested_structures_are_walked():
    got = desurrogate({"d": LONE_PAIR, "l": [LONE_PAIR], "n": 5, "b": True})
    assert got == {"d": "🔴", "l": ["🔴"], "n": 5, "b": True}


def test_parse_file_survives_escaped_emoji(tmp_path: Path):
    note = tmp_path / "session-note.md"
    note.write_text(
        '---\n'
        'name: session-note\n'
        'description: "\\ud83d\\udd34 ночной отчёт"\n'
        '---\n\n'
        'body text\n',
        encoding="utf-8",
    )
    meta, body = parse_file(note)
    meta["description"].encode("utf-8")  # the operation that used to explode
    assert meta["description"] == "🔴 ночной отчёт"
    assert body == "body text"
