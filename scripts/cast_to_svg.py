#!/usr/bin/env python3
"""Turn a captured terminal run into a self-contained animated SVG.

    scripts/demo.sh --record | python3 scripts/cast_to_svg.py > docs/demo.svg

Why not a GIF: an SVG is a tenth of the size, stays sharp on a phone, needs no
recorder installed, and — the part that matters — is generated from the real
output of a real run, so the numbers on screen cannot drift from the code.

Only the handful of ANSI colours `demo.sh` emits are understood; anything else
is stripped. No dependencies.
"""

from __future__ import annotations

import html
import re
import sys

ANSI = re.compile(r"\033\[([0-9;]*)m")

#: The palette demo.sh speaks: comment, command, and plain output.
COLOURS = {"90": "#7d8590", "36": "#56b6c2", "0": "#e6edf3", "": "#e6edf3"}

CHAR_W = 7.7          # DejaVu Sans Mono at 13px
LINE_H = 19.0
PAD_X, PAD_TOP = 18.0, 44.0
LINE_DELAY = 0.32     # seconds between lines appearing
HOLD = 3.0            # seconds the finished screen stays up before looping


def parse(raw: str) -> list[tuple[str, str]]:
    """(colour, text) per line, with a blank run collapsed to one."""
    out: list[tuple[str, str]] = []
    for line in raw.replace("\r\n", "\n").split("\n"):
        colour, text, pos = COLOURS[""], [], 0
        for m in ANSI.finditer(line):
            text.append(line[pos:m.start()])
            code = m.group(1).split(";")[-1]
            if code in COLOURS and not any(t.strip() for t in text):
                colour = COLOURS[code]          # colour of the line's first run
            pos = m.end()
        text.append(line[pos:])
        joined = "".join(text).rstrip()
        if not joined and out and not out[-1][1]:
            continue
        out.append((colour, joined))
    while out and not out[-1][1]:
        out.pop()
    return out


def render(lines: list[tuple[str, str]], *, title: str = "skillmem") -> str:
    cols = max((len(t) for _, t in lines), default=60)
    width = PAD_X * 2 + cols * CHAR_W
    height = PAD_TOP + len(lines) * LINE_H + PAD_X
    total = len(lines) * LINE_DELAY + HOLD

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width:.0f} {height:.0f}" '
        f'width="{width:.0f}" height="{height:.0f}" font-family="ui-monospace,'
        'SFMono-Regular,Menlo,DejaVu Sans Mono,monospace" font-size="13">',
        f'<rect width="{width:.0f}" height="{height:.0f}" rx="10" fill="#0d1117"/>',
        f'<rect width="{width:.0f}" height="30" rx="10" fill="#161b22"/>',
        '<rect y="20" width="100%" height="10" fill="#161b22"/>',
        '<circle cx="20" cy="15" r="5" fill="#ff5f56"/>',
        '<circle cx="38" cy="15" r="5" fill="#ffbd2e"/>',
        '<circle cx="56" cy="15" r="5" fill="#27c93f"/>',
        f'<text x="{width/2:.0f}" y="19" fill="#7d8590" font-size="11" '
        f'text-anchor="middle">{html.escape(title)}</text>',
    ]

    for i, (colour, text) in enumerate(lines):
        if not text:
            continue
        appear = i * LINE_DELAY
        # One looping timeline per line: hidden, then visible from its cue on.
        k1 = max(appear / total, 0.0001)
        parts.append(
            f'<text x="{PAD_X:.0f}" y="{PAD_TOP + i * LINE_H:.0f}" fill="{colour}" '
            f'xml:space="preserve" opacity="0">{html.escape(text)}'
            f'<animate attributeName="opacity" dur="{total:.2f}s" '
            f'repeatCount="indefinite" values="0;0;1;1" '
            f'keyTimes="0;{k1:.4f};{min(k1 + 0.01, 0.999):.4f};1"/></text>'
        )
    parts.append("</svg>")
    return "\n".join(parts)


def main() -> None:
    raw = sys.stdin.read()
    if not raw.strip():
        sys.exit("nothing on stdin; pipe `scripts/demo.sh --record` into this")
    sys.stdout.write(render(parse(raw)))


if __name__ == "__main__":
    main()
