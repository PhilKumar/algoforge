"""How much capital a book needs at each lot size, and why small size is punished.

Shared by every tearsheet that trades a whole number of lots, so the four sheets
answer the question the same way (Phil, 2026-08-24: "I want this one to be added
in all the 4 tearsheets").

THE ONE FACT THAT MAKES THE TABLE INTERESTING. Under
`cascade_costs.NiftyOptionCostSchedule`, brokerage is `brokerage_per_order`
(Rs 20) times the number of orders, and `brokerage_per_lot` is ZERO. So the
brokerage on a round trip is the same Rs 40 whether you buy one lot or eight,
and GST at 18% rides on it -- Rs 47.20 that does not move. Everything else
(STT, exchange, SEBI, stamp, and their GST) is a fraction of turnover and
therefore scales exactly with the lot count.

That gives an EXACT decomposition rather than a re-run:

    net(m) = m * gross - (flat + m * (costs_at_one_lot - flat))

which is why return-on-capital climbs with size while the edge itself does not
change at all. Nothing here is a re-simulation; it is the same book re-costed.
"""

from __future__ import annotations

import pathlib
import sys

_REPO = pathlib.Path(__file__).resolve().parent.parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from cascade_costs import NiftyOptionCostSchedule  # noqa: E402

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from i18n import t  # noqa: E402

# The module emits `class='trow-live'`, and the parent defines that rule in its
# EXTRA_CSS -- which sits outside the <style> block the children borrow. So any
# sheet using section() must carry this, or its live row is marked in the markup
# and invisible on the page. `--accent-soft` IS in the borrowed block.
SIZING_CSS = ".trow-live th, .trow-live td { background:var(--accent-soft); font-weight:700; }"

DEFAULT_MULTIPLES = (1, 2, 3, 4, 6, 8)
BUFFER = 0.30  # the parent's own margin over peak + worst drawdown


def flat_per_round(orders: int = 2, schedule: NiftyOptionCostSchedule | None = None) -> float:
    """The part of a round trip's cost that does NOT move with size."""
    s = schedule or NiftyOptionCostSchedule()
    brokerage = s.brokerage_per_order * orders
    return brokerage * (1 + s.gst_rate)


def scale(rows: list[dict], multiples=DEFAULT_MULTIPLES, *, years: float, orders: int = 2) -> list[dict]:
    """Re-cost a one-lot book at each multiple.

    Each row needs `gross` (before charges), `costs` (at one lot) and `capital`
    (premium outstanding at one lot). Rows are taken in the order given, which
    must be the order they were traded -- the drawdown depends on it.
    """
    flat = flat_per_round(orders)
    out = []
    for m in multiples:
        equity = peak_eq = 0.0
        dd = 0.0
        peak_cap = 0.0
        net_total = 0.0
        charges = 0.0
        for x in rows:
            scaling = max(0.0, float(x["costs"]) - flat)
            cost = flat + m * scaling
            net = m * float(x["gross"]) - cost
            net_total += net
            charges += cost
            equity += net
            peak_eq = max(peak_eq, equity)
            dd = min(dd, equity - peak_eq)
            peak_cap = max(peak_cap, m * float(x["capital"]))
        funded = (peak_cap + abs(dd)) * (1 + BUFFER)
        per_year = net_total / years if years else 0.0
        gross_total = sum(float(x["gross"]) for x in rows) * m
        out.append(
            {
                "lots": m,
                "peak": peak_cap,
                "dd": dd,
                "funded": funded,
                "net": net_total,
                "per_year": per_year,
                "roi": round(100 * per_year / funded) if funded else 0,
                "charges": charges,
                "charge_share": round(100 * charges / gross_total, 1) if gross_total else 0.0,
            }
        )
    return out


def section(
    sizes: list[dict], *, r, cls, live_lots: int, anchor: str = "capital-needed", note_en: str = "", note_ta: str = ""
) -> str:
    """The section, in the parent sheet's own shape so the four match."""
    live_attr = " class='trow-live'"  # 3.11 forbids a backslash inside an f-string expression
    body = "".join(
        f"<tr{live_attr if s['lots'] == live_lots else ''}>"
        f"<th scope='row'>{s['lots']} lot{'s' if s['lots'] > 1 else ''}"
        f"{' &larr; live' if s['lots'] == live_lots else ''}</th>"
        f"<td>{r(s['peak'])}</td><td class='neg'>{r(s['dd'])}</td>"
        f"<td><strong>{r(s['funded'])}</strong></td>"
        f"<td class='{cls(s['net'])}'>{r(s['net'])}</td><td>{r(s['per_year'])}</td>"
        f"<td>{s['roi']}%</td></tr>"
        for s in sizes
    )
    one, top = sizes[0], sizes[-1]
    spread = top["roi"] - one["roi"]
    if spread >= 8:
        verdict = t(
            "Practical reading: <strong>1 lot is viable but inefficient</strong> and is best treated as a proving size. Every added lot improves the cost ratio, with most of the gain captured by 3&ndash;4 lots.",
            "நடைமுறை: <strong>1 lot சாத்தியம், ஆனால் திறனற்றது</strong> &mdash; நிரூபிக்கும் அளவாகவே. ஒவ்வொரு கூடுதல் lot-ம் செலவு விகிதத்தை மேம்படுத்துகிறது; பெரும்பகுதி 3&ndash;4 lot-இல்.",
        )
    else:
        verdict = t(
            f"Practical reading: size barely matters here. Return per rupee moves only {spread} point{'s' if spread != 1 else ''} from 1 lot to {top['lots']}, because the premium on an in-the-money contract is large enough that {r(flat_per_round())} of flat cost is already small against it. <strong>Trade this at the size the account can carry, not the size the cost ratio wants.</strong>",
            f"நடைமுறை: இங்கு அளவு பெரிதாக பொருட்படுத்தப்படுவதில்லை. 1 lot-இலிருந்து {top['lots']} வரை வருவாய் {spread} புள்ளிகள் மட்டுமே நகர்கிறது. <strong>செலவு விகிதம் கேட்கும் அளவில் அல்ல, கணக்கு தாங்கும் அளவில் வர்த்தகம் செய்யுங்கள்.</strong>",
        )
    extra = f"<p style='font-size:13.5px'>{note_en and t(note_en, note_ta or note_en)}</p>" if note_en else ""
    return f"""
<section id="{anchor}">
  <div class="shead"><div><h2>{t("How much capital this needs", "இதற்கு எவ்வளவு மூலதனம் தேவை")}</h2>
    <p>{
        t(
            "Options are bought, not written, so the money at risk is the premium paid &mdash; there is no margin call. The account still has to carry two things at once: the largest premium ever outstanding in a single day, and the deepest drawdown.",
            "ஆப்ஷன்கள் வாங்கப்படுகின்றன, விற்கப்படுவதில்லை. எனவே ரிஸ்கில் இருக்கும் பணம் கட்டிய பிரீமியம் மட்டுமே &mdash; மார்ஜின் கால் கிடையாது. இருப்பினும் கணக்கு இரண்டையும் ஒரே நேரத்தில் தாங்க வேண்டும்: ஒரு நாளில் நிலுவையில் இருந்த மிகப்பெரிய பிரீமியம், மற்றும் மிக ஆழமான இறக்கம்.",
        )
    }</p></div></div>
  <div class="tblwrap"><table>
    <thead><tr><th scope="col">{t("Size", "அளவு")}</th><th scope="col">{t("Peak deployed", "உச்ச பயன்பாடு")}</th>
      <th scope="col">{t("Max drawdown", "அதிகபட்ச இறக்கம்")}</th>
      <th scope="col">{t("Account to fund", "தேவையான கணக்கு")}</th><th scope="col">{t("Net", "நிகரம்")}</th>
      <th scope="col">{t("Per year", "ஆண்டுக்கு")}</th><th scope="col">{t("Return", "வருவாய்")}</th></tr></thead>
    <tbody>{body}</tbody>
  </table></div>
  <div class="split" style="margin-top:14px">
    <div class="panel">
      <h3>{t("Reading the table", "அட்டவணையை எப்படிப் படிப்பது")}</h3>
      <p style="font-size:13.5px">{
        t(
            "<strong>Account to fund</strong> is peak premium outstanding plus the worst drawdown, plus a 30% buffer. It is the number that keeps the strategy alive through its own worst stretch without a top-up.",
            "<strong>தேவையான கணக்கு</strong> = உச்ச நிலுவை பிரீமியம் + மோசமான இறக்கம் + 30% இடைவெளி. சொந்த மோசமான காலத்தையும் top-up இல்லாமல் கடக்க வைக்கும் எண் இதுவே.",
        )
    }</p>
      <p style="font-size:13.5px">{
        t(
            f"<strong>The floor is 1 lot at about {r(one['funded'])}.</strong> Below that the position cannot be split further &mdash; one NIFTY lot is the smallest tradable unit.",
            f"<strong>தளம் = 1 lot, சுமார் {r(one['funded'])}.</strong> அதற்குக் கீழே position-ஐப் பிரிக்க முடியாது &mdash; ஒரு NIFTY lot தான் மிகச்சிறிய அலகு.",
        )
    }</p>
      <p style="font-size:13.5px;margin-bottom:0">{
        t(
            f"<strong>Live today is {live_lots} lot{'s' if live_lots > 1 else ''}</strong>, which wants about {r(next(s for s in sizes if s['lots'] == live_lots)['funded'])} funded.",
            f"<strong>இன்று நேரடி {live_lots} lot</strong>, சுமார் {r(next(s for s in sizes if s['lots'] == live_lots)['funded'])} தேவை.",
        )
    }</p>
    </div>
    <div class="panel">
      <h3>{t("Small size is punished, and by how much", "சிறிய அளவு தண்டிக்கப்படுகிறது &mdash; எவ்வளவு என்பதுடன்")}</h3>
      <p style="font-size:13.5px">{
        t(
            f"Return per rupee funded is {one['roi']}% a year at 1 lot and {top['roi']}% at {top['lots']} &mdash; not because the edge changes, but because {r(flat_per_round())} of flat brokerage and GST is the same on both. At 1 lot charges eat {one['charge_share']}% of gross profit; at {top['lots']} lots, {top['charge_share']}%.",
            f"ஒரு ரூபாய்க்கான வருவாய் 1 lot-இல் {one['roi']}%, {top['lots']} lot-இல் {top['roi']}% &mdash; edge மாறுவதால் அல்ல, {r(flat_per_round())} நிலையான புரோக்கரேஜ் இரண்டிலும் ஒன்றே என்பதால். 1 lot-இல் கட்டணங்கள் மொத்த லாபத்தில் {one['charge_share']}%; {top['lots']} lot-இல் {top['charge_share']}%.",
        )
    }</p>
      <p style="font-size:13.5px;margin-bottom:0">{verdict}</p>
    </div>
  </div>
  {extra}
</section>"""
