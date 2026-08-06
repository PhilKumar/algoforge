#!/usr/bin/env python3
"""Fold market_movers.css and study_lounge.css into one scoped stylesheet.

Both pages were written as STANDALONE documents, so between them they redefine
`:root`, `*`, `html`, `body`, `body::before/after` and a `.brand-*` topbar --
and their `:root` uses the very same variable names the app does (`--bg`,
`--panel`, `--border`, `--text`, `--accent`).  Linking either file from
strategy.html as-is silently repaints every page in the app.  That is the whole
reason this script exists rather than a `<link>` tag.

What it does, per file:

* every top-level selector is prefixed with the panel's id, so nothing can
  reach outside its own tab;
* `:root` becomes the panel root, so their custom properties still resolve for
  their own markup and stop at its edge;
* the document resets (`*`, `html`, `body`, `body::before`, `body::after`) and
  the standalone topbar (`.brand-*`, `.movers-topbar`, `.study-topbar`) are
  DROPPED -- the app shell already provides both, and keeping them is how a
  page-inside-a-page ends up with two headers and a scrollbar that fights;
* `[data-theme="light"] X` keeps the theme attribute outermost, because it
  lives on `<html>` and prefixing it would never match.

Re-run after editing either source file:

    python3 tools/scope_insights_css.py
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "static" / "insights-panels.css"

SOURCES = [
    (ROOT / "static" / "market_movers.css", "#insights-heatmap"),
    (ROOT / "static" / "study_lounge.css", "#insights-study"),
]

# Rules the app shell already owns. Anything whose selector matches one of
# these is dropped rather than scoped.
DROP = re.compile(
    r"^\s*(\*|html|body|:where\(html\)|"
    r"(\[data-theme=\"light\"\]\s*)?(html|body)(::?[a-z-]+)?|"
    r"(\[data-theme=\"light\"\]\s*)?\.(brand-[a-z-]+|theme-logo|movers-topbar|study-topbar|"
    # The panels' own button skins. Their markup now wears the app's .btn, so
    # these rules would only fight it -- the whole point of the button pass.
    r"app-btn[a-z-]*|shell-btn|"
    r"movers-page|study-page)(\s|$|[.:,]))",
    re.IGNORECASE,
)


# Button skins the panels brought with them. Their markup now wears the app's
# own `.btn`, so any rule mentioning these would only fight it.
DROP_ANYWHERE = re.compile(r"\.(app-btn[a-z-]*|shell-btn)\b")


def scope_selector(selector: str, scope: str) -> str | None:
    """Prefix one comma-separated selector list with the panel scope."""
    parts = []
    for raw in selector.split(","):
        part = raw.strip()
        if not part:
            continue
        if DROP.match(part):
            continue
        # ...and anywhere at all for the button skins: `.spotlight-actions
        # .app-btn` fights the app's .btn just as hard as a bare `.app-btn`.
        if DROP_ANYWHERE.search(part):
            continue
        if part in {":root", ":root:root"}:
            parts.append(scope)
            continue
        # The theme flag sits on <html>, so it has to stay OUTERMOST. Scoping
        # it as a descendant produces a selector that can never match, which is
        # a silent loss of every light-mode rule rather than a visible error.
        theme = re.match(r'^(\[data-theme="[a-z]+"\])\s*(.*)$', part)
        if theme:
            rest = theme.group(2).strip()
            parts.append(f"{theme.group(1)} {scope} {rest}".rstrip())
            continue
        parts.append(f"{scope} {part}")
    return ", ".join(parts) if parts else None


def scope_css(text: str, scope: str) -> str:
    """Walk the sheet brace by brace, scoping every rule that survives."""
    out: list[str] = []
    i = 0
    depth = 0
    at_stack: list[bool] = []
    buffer = ""
    while i < len(text):
        char = text[i]
        if char == "{":
            selector = buffer.strip()
            buffer = ""
            if selector.startswith("@"):
                # @media / @supports: keep the block, scope what is inside it.
                at_stack.append(True)
                out.append(selector + " {")
                depth += 1
            else:
                scoped = scope_selector(selector, scope)
                at_stack.append(False)
                depth += 1
                if scoped is None:
                    # Skip the whole declaration block.
                    j, inner = i + 1, 1
                    while j < len(text) and inner:
                        if text[j] == "{":
                            inner += 1
                        elif text[j] == "}":
                            inner -= 1
                        j += 1
                    i = j
                    depth -= 1
                    at_stack.pop()
                    continue
                out.append(scoped + " {")
            i += 1
            continue
        if char == "}":
            depth -= 1
            if at_stack:
                at_stack.pop()
            out.append(buffer)
            buffer = ""
            out.append("}\n")
            i += 1
            continue
        buffer += char
        i += 1
    out.append(buffer)
    return "".join(out)


def main() -> None:
    chunks = [
        "/* GENERATED by tools/scope_insights_css.py -- do not edit by hand.\n"
        "   Edit static/market_movers.css or static/study_lounge.css and re-run.\n"
        "   Every rule here is scoped to its Insights tab so neither standalone\n"
        "   page's :root can repaint the app. */\n"
    ]
    for path, scope in SOURCES:
        chunks.append(f"\n/* ---- {path.name} -> {scope} ---- */\n")
        chunks.append(scope_css(path.read_text(), scope))
    OUT.write_text("".join(chunks).rstrip() + "\n")
    rules = sum(1 for _ in re.finditer(r"\{", OUT.read_text()))
    print(f"wrote {OUT.relative_to(ROOT)} ({rules} blocks)")


if __name__ == "__main__":
    main()
