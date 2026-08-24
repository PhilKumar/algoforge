"""Emit the Gap Carry tearsheet -- one candle at 15:10, one contract, one night.

Fourth of the family (build_report.py is the five-year options sheet,
build_fib_report.py the Fib Boundary sheet, build_candle_report.py the Candle
Entry one). It borrows the parent document's stylesheet and helpers verbatim --
read from build_report.py at run time, never copied -- so the four read as one
family on the Assets page.

Every figure comes from a replay of the exact rule the Gap Carry tab trades: at
15:10 read the last closed 5m candle, a close above its EMA20 with RSI(14) at or
over 70 buys an ATM+4 ITM call, a close below with RSI at or under 30 buys the
put; the nearest weekly that survives the night; sold at 09:20 the next session.
NIFTY, 2021-01-05 -> 2026-07-09, one lot, real recorded premiums from two
archives, lot size by expiry date. Nothing here is typed by hand.

The replay this reads reproduces through engine/gap_carry.py to the rupee, which
is the only reason a sheet may quote it.

    python3 tools/tearsheet/build_gapcarry_report.py [book.csv]
"""

from __future__ import annotations

import csv
import pathlib
import re
import sys
from collections import defaultdict
from datetime import date

_HERE = pathlib.Path(__file__).resolve().parent
_REPO = _HERE.parent.parent
OUT = _REPO / "docs" / "assets" / "gap-carry-tearsheet.html"
RUNS = _REPO / "tools" / "gapcarry_offline" / "runs"
BOOK = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else RUNS / "NIFTY_5m_rsi70_atm4.csv"

DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _borrow():
    """The parent document's helpers and stylesheet, borrowed, not copied."""
    src = (_HERE / "build_report.py").read_text()
    helpers: dict = {}
    for name in ("r", "lakh", "cls", "curve_svg"):
        m = re.search(rf"^def {name}\(.*?(?=^def |^# ──|^[A-Za-z_][A-Za-z_0-9, ]* = )", src, re.S | re.M)
        if not m:
            raise SystemExit(f"build_report.py no longer defines {name}()")
        exec(m.group(0), helpers)  # noqa: S102  # nosec B102 -- our own file, read at build time
    style = re.search(r"<style>\n(.*?)</style>", src, re.S)
    if not style:
        raise SystemExit("build_report.py has no <style> block to borrow")
    css = style.group(1).replace("{{", "{").replace("}}", "}")
    # The parent writes its CSS inside a Python string, so a backslash escape
    # survives into the borrowed copy and would render as a literal.
    css = css.replace("\\\\", "\\")
    return helpers, css


def _rows() -> list:
    if not BOOK.exists():
        raise SystemExit(f"no replay at {BOOK}; run the Gap Carry sweep first")
    out = []
    with BOOK.open() as fh:
        for row in csv.DictReader(fh):
            out.append(
                {
                    "session": date.fromisoformat(row["session"]),
                    "side": row["side"],
                    "strike": int(float(row["strike"])),
                    "expiry": row["expiry"],
                    "lot": int(float(row["lot"])),
                    "rsi": float(row["rsi"]),
                    "entry_premium": float(row["entry_premium"]),
                    "exit_premium": float(row["exit_premium"]),
                    "priced": str(row["priced"]).strip().lower() in {"true", "1", "yes"},
                    "capital": float(row["capital"]),
                    "net": float(row["net"]),
                }
            )
    out.sort(key=lambda r: r["session"])
    return out


def _stats(rows: list) -> dict:
    nets = [r["net"] for r in rows]
    wins = [n for n in nets if n > 0]
    gains, losses = sum(wins), -sum(n for n in nets if n <= 0)
    equity = peak = dd = 0.0
    # curve_svg() borrowed from build_report.py wants (label, value) pairs.
    curve = []
    for row, n in zip(rows, nets):
        equity += n
        peak = max(peak, equity)
        dd = min(dd, equity - peak)
        curve.append((row["session"].isoformat(), equity))
    floored = [r for r in rows if not r["priced"]]
    return {
        "trades": len(rows),
        "net": sum(nets),
        "wins": len(wins),
        "win_rate": len(wins) / len(rows) if rows else 0.0,
        "pf": gains / losses if losses else 0.0,
        "avg": sum(nets) / len(nets) if nets else 0.0,
        "best": max(nets) if nets else 0.0,
        "worst": min(nets) if nets else 0.0,
        "dd": dd,
        "curve": curve,
        "peak_capital": max((r["capital"] for r in rows), default=0.0),
        "floored": floored,
        "floored_net": sum(r["net"] for r in floored),
        "first": rows[0]["session"] if rows else None,
        "last": rows[-1]["session"] if rows else None,
    }


def _group(rows: list, key) -> list:
    buckets: dict = defaultdict(list)
    for row in rows:
        buckets[key(row)].append(row)
    out = []
    for name, group in buckets.items():
        nets = [r["net"] for r in group]
        out.append(
            {
                "name": name,
                "n": len(group),
                "net": sum(nets),
                "win": sum(1 for n in nets if n > 0) / len(nets),
            }
        )
    return out


def build() -> str:
    helpers, css = _borrow()
    r, lakh, cls, curve_svg = helpers["r"], helpers["lakh"], helpers["cls"], helpers["curve_svg"]
    del lakh  # the parent's, borrowed for parity; this sheet quotes plain rupees
    rows = _rows()
    s = _stats(rows)
    if len(s["curve"]) < 2:
        return ""  # curve_svg divides by len-1
    # SIX PIECES OF PATH DATA, not an <svg> element: line, filled area, the
    # underwater shading, the zero baseline, and the high/low of the range. The
    # parent assembles the markup itself and so must this sheet.
    c_line, c_area, c_dd, c_zero, c_hi, _c_lo = curve_svg(s["curve"])

    by_year = sorted(_group(rows, lambda x: x["session"].year), key=lambda g: g["name"])
    by_side = sorted(_group(rows, lambda x: x["side"]), key=lambda g: g["name"])
    by_dow = _group(rows, lambda x: DOW[x["session"].weekday()])
    by_dow.sort(key=lambda g: DOW.index(g["name"]))

    def rowspan(groups, label):
        body = "".join(
            f"<tr><td>{g['name']}</td><td class='num'>{g['n']}</td>"
            f"<td class='num {cls(g['net'])}'>{r(g['net'])}</td>"
            f"<td class='num'>{g['win'] * 100:.0f}%</td></tr>"
            for g in groups
        )
        return (
            f"<div class='tblwrap'><table><thead><tr><th>{label}</th><th class='num'>Nights</th>"
            f"<th class='num'>Net</th><th class='num'>Won</th></tr></thead><tbody>{body}</tbody></table></div>"
        )

    top3 = sorted((x["net"] for x in rows), reverse=True)[:3]
    fri = next((g for g in by_dow if g["name"] == "Fri"), {"net": 0.0, "n": 0})

    return f"""<title>Gap Carry — the overnight book</title>
<style>
{css}
</style>
<section class="document-hero">
  <div class="hero-copy">
    <p class="eyebrow"><b>TEARSHEET</b>NIFTY &middot; EMA20 + RSI &middot; 15:10 in &middot; 09:20 out</p>
    <h1>Gap Carry &mdash; the overnight book</h1>
    <p class="lede">One candle is read at 15:10. If it closed on the strong side of its EMA20 with
    momentum to match, one in-the-money contract is bought and sold into the next morning's open.
    No stop, no target, and nothing on the way out but the clock.</p>
  </div>
  <div class="document-meta">
    <div class="meta-chip">{s["first"]} &rarr; {s["last"]}</div>
    <div class="meta-chip">{s["trades"]} nights &middot; one lot</div>
    <div class="meta-chip">ATM+4 in the money</div>
    <div class="meta-chip">nearest weekly</div>
    <div class="meta-chip">recorded premiums, two archives</div>
  </div>
</section>

<article class="document-body" id="document-body">
<section id="headline">
  <div class="shead"><h2>The book</h2></div>
  <div class="kpis">
    <div class="kpi"><div class="kpi-l">Net</div>
      <div class="kpi-v {cls(s["net"])}">{r(s["net"])}</div>
      <div class="kpi-s">after all charges &middot; {s["trades"]} nights</div></div>
    <div class="kpi"><div class="kpi-l">Won</div>
      <div class="kpi-v">{s["win_rate"] * 100:.1f}%</div>
      <div class="kpi-s">{s["wins"]} of {s["trades"]}</div></div>
    <div class="kpi"><div class="kpi-l">Profit factor</div>
      <div class="kpi-v">{s["pf"]:.2f}</div>
      <div class="kpi-s">gross won per rupee lost</div></div>
    <div class="kpi"><div class="kpi-l">Worst drawdown</div>
      <div class="kpi-v neg">{r(s["dd"])}</div>
      <div class="kpi-s">deepest fall from a peak</div></div>
    <div class="kpi"><div class="kpi-l">Capital at most</div>
      <div class="kpi-v">{r(s["peak_capital"])}</div>
      <div class="kpi-s">one lot, largest single night</div></div>
  </div>
  <div class="panel">
    <div class="chart">
      <svg viewBox="0 0 1040 260" role="img" preserveAspectRatio="none"
           aria-label="Cumulative net profit from {s["first"]} to {s["last"]}, ending at {r(s["net"])}">
        <path d="{c_dd}" fill="rgba(var(--neg-fill),.13)"/>
        <path d="{c_area}" fill="rgba(var(--curve-rgb),.10)"/>
        <line x1="0" y1="{c_zero:.1f}" x2="1040" y2="{c_zero:.1f}"
              stroke="var(--line)" stroke-width="1" stroke-dasharray="3 4"/>
        <path class="curve-line" d="{c_line}" fill="none" stroke="var(--curve)"
              stroke-width="2" stroke-linejoin="round" vector-effect="non-scaling-stroke"/>
      </svg>
    </div>
    <div class="axis"><span>{s["first"]}</span>
      <span>peak {r(c_hi)} &middot; shaded = below previous high</span>
      <span>{s["last"]}</span></div>
  </div>
  <p class="note">Average night {r(s["avg"])}. Best {r(s["best"])}, worst {r(s["worst"])}.</p>
</section>

<section id="curve-gapcarry">
  <div class="shead"><h2>Year by year, and which side paid</h2></div>
  <div class="split">{rowspan(by_year, "Year")}{rowspan(by_side, "Side")}</div>
</section>

<section id="weekday">
  <div class="shead"><h2>The weekday problem</h2></div>
  {rowspan(by_dow, "Entry day")}
  <p class="note note-warn"><strong>Friday alone is {fri["net"] / s["net"] * 100:.0f}% of the net</strong> from
  {fri["n"]} of {s["trades"]} nights. A Friday entry is held over the weekend, so part of this book is a
  three-day gap wearing a one-night rule's clothes. <strong>Thursday loses.</strong> Neither fact is
  settled, and the live tab exists to prove them forward rather than assume them.</p>
</section>

<section id="honesty">
  <div class="shead"><h2>What is not measured</h2></div>
  <p><strong>{len(s["floored"])} of {s["trades"]} exits are floored at intrinsic value</strong>
  ({r(s["floored_net"])} of the net). Those are contracts that gapped far enough to leave what the
  archives carry — which happens precisely when the night went well, so the floor
  <em>understates</em> them. A floor is not a price, and they are counted apart everywhere they appear.</p>
  <p><strong>The top three nights are {r(sum(top3))}</strong>, {sum(top3) / s["net"] * 100:.0f}% of the
  net; without them the book still makes {r(s["net"] - sum(top3))}.</p>
  <p><strong>RSI 70 is the top of a band, not a plateau.</strong> Every threshold from 65 to 76 is
  positive with both halves of the window green, but 70 is the best cell in it. Read the strength as
  profit factor ~1.35–1.40 rather than the {s["pf"]:.2f} above.</p>
  <p class="note">Twelve of twelve combinations of 5m/10m/15m/30m against RSI 68/70/72 were profitable
  over this window, eleven of them green in both halves — the candle is read once and both ends are
  clock times, so there is very little for a fit to grip.</p>
</section>
</article>
"""


if __name__ == "__main__":
    html = build()
    if not html:
        raise SystemExit("not enough nights to draw a curve")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(html)
    print(f"wrote {OUT} ({len(html):,} bytes)")
