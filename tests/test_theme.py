"""WCAG contrast for the hub's light and dark colours in static/style.css."""

import re
from pathlib import Path

import pytest

CSS = (Path(__file__).parent.parent / "src/backtrack/static/style.css").read_text()


def tokens():
    light_block = CSS.split("@media (prefers-color-scheme: dark)")[0]
    dark_block = CSS.split("@media (prefers-color-scheme: dark)")[1].split("\n}\n")[0]
    hexes = r"--([a-z0-9-]+):\s*(#[0-9a-fA-F]{6})\b"
    light = dict(re.findall(hexes, light_block))
    return {"light": light, "dark": {**light, **dict(re.findall(hexes, dark_block))}}


def lum(h):
    c = [int(h.lstrip("#")[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def ratio(a, b):
    a, b = lum(a), lum(b)
    return (max(a, b) + 0.05) / (min(a, b) + 0.05)


SURFACES = ["bg", "surface", "surface-2"]
PAIRS = ([(t, s, 4.5) for t in ("text", "muted", "code", "link", "ok", "warn", "err", "info") for s in SURFACES]
         + [("accent-text", "accent", 4.5), ("on-status", "err", 4.5)]
         + [("focus", s, 3.0) for s in SURFACES] + [("border-strong", s, 3.0) for s in SURFACES])


@pytest.mark.parametrize("mode", ["light", "dark"])
def test_contrast(mode):
    v = tokens()[mode]
    fails = [f"{fg} on {bg}: {ratio(v[fg], v[bg]):.2f}" for fg, bg, need in PAIRS if ratio(v[fg], v[bg]) < need]
    assert not fails, fails
