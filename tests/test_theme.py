"""WCAG contrast for the plain theme, using the same pairs as Orbit's checks/contrast.py."""

import re
from pathlib import Path

import pytest

CSS = (Path(__file__).parent.parent / "src/backtrack/static/plain.css").read_text()


def blocks():
    light = CSS.split("@media")[0]
    dark = CSS.split("@media")[1]
    lv = dict(re.findall(r"--([a-z0-9-]+):\s*(#[0-9a-fA-F]{6,8})", light))
    dv = {**lv, **dict(re.findall(r"--([a-z0-9-]+):\s*(#[0-9a-fA-F]{6,8})", dark))}
    for v in (lv, dv):
        v["link"] = v["soft"]
        v["secondary-content"] = v["base-content"]
    return {"light": lv, "dark": dv}


def lum(h):
    c = [int(h.lstrip("#")[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def ratio(a, b):
    a, b = lum(a), lum(b)
    return (max(a, b) + 0.05) / (min(a, b) + 0.05)


SURF = ["base-300", "base-200", "base-100"]
PAIRS = ([(t, s, 4.5) for t in ("base-content", "muted", "soft", "link", "code-ink", "success", "warning",
                                "error", "info") for s in SURF]
         + [("primary-content", "primary", 4.5), ("secondary-content", "secondary", 4.5)]
         + [(f"{s}-content", s, 4.5) for s in ("success", "warning", "error", "info")]
         + [("glow", s, 3.0) for s in SURF] + [("rim-strong", s, 3.0) for s in SURF])


@pytest.mark.parametrize("mode", ["light", "dark"])
def test_plain_theme_contrast(mode):
    v = blocks()[mode]
    fails = [f"{fg} on {bg}: {ratio(v[fg][:7], v[bg][:7]):.2f}" for fg, bg, need in PAIRS
             if ratio(v[fg][:7], v[bg][:7]) < need]
    assert not fails, fails
