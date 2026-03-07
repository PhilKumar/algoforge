import re

with open("strategy.html", "r", encoding="utf-8") as f:
    content = f.read()

orig = len(content)
changes = []

# ── 1. Fix .btn base: use CSS custom property for color ───────────────────
old_btn = (
    "  .btn {\n    background: var(--btn-bg, linear-gradient(180deg, #2f72d8 0%, #2458ab 100%));\n    color: #fff;"
)
new_btn = "  .btn {\n    background: var(--btn-bg, linear-gradient(180deg, #2f72d8 0%, #2458ab 100%));\n    color: var(--btn-color, #fff);"
if old_btn in content:
    content = content.replace(old_btn, new_btn, 1)
    changes.append("btn base color -> var OK")
else:
    changes.append("btn base color NOT found")

# ── 2. Fix filter button initial inline styles to use CSS vars ────────────
# All button
old_all = 'data-filter="all" onclick="filterRuns(\'all\', this)" style="font-size: 11px; padding: 5px 12px; background: var(--accent); color: #000;"'
new_all = 'data-filter="all" onclick="filterRuns(\'all\', this)" style="font-size: 11px; padding: 5px 12px; --btn-bg: var(--accent); --btn-color: #000; --btn-border: rgba(0,200,150,0.5);"'
if old_all in content:
    content = content.replace(old_all, new_all, 1)
    changes.append("All filter OK")

# Backtest
old_bt = 'data-filter="backtest" onclick="filterRuns(\'backtest\', this)" style="font-size: 11px; padding: 5px 12px; background: rgba(59,130,246,0.15); color: rgb(96,165,250); border: 1px solid rgba(59,130,246,0.3);"'
new_bt = 'data-filter="backtest" onclick="filterRuns(\'backtest\', this)" style="font-size: 11px; padding: 5px 12px; --btn-bg: linear-gradient(180deg, rgba(59,130,246,0.25) 0%, rgba(40,90,180,0.4) 100%); --btn-color: rgb(96,165,250); --btn-border: rgba(59,130,246,0.5);"'
if old_bt in content:
    content = content.replace(old_bt, new_bt, 1)
    changes.append("Backtest filter OK")

# Paper
old_pp = 'data-filter="paper" onclick="filterRuns(\'paper\', this)" style="font-size: 11px; padding: 5px 12px; background: rgba(245,158,11,0.15); color: rgb(251,191,36); border: 1px solid rgba(245,158,11,0.3);"'
new_pp = 'data-filter="paper" onclick="filterRuns(\'paper\', this)" style="font-size: 11px; padding: 5px 12px; --btn-bg: linear-gradient(180deg, rgba(245,158,11,0.25) 0%, rgba(180,120,8,0.4) 100%); --btn-color: rgb(251,191,36); --btn-border: rgba(245,158,11,0.5);"'
if old_pp in content:
    content = content.replace(old_pp, new_pp, 1)
    changes.append("Paper filter OK")

# Live
old_lv = 'data-filter="live" onclick="filterRuns(\'live\', this)" style="font-size: 11px; padding: 5px 12px; background: rgba(139,92,246,0.15); color: rgb(167,139,250); border: 1px solid rgba(139,92,246,0.3);"'
new_lv = 'data-filter="live" onclick="filterRuns(\'live\', this)" style="font-size: 11px; padding: 5px 12px; --btn-bg: linear-gradient(180deg, rgba(139,92,246,0.25) 0%, rgba(100,60,200,0.4) 100%); --btn-color: rgb(167,139,250); --btn-border: rgba(139,92,246,0.5);"'
if old_lv in content:
    content = content.replace(old_lv, new_lv, 1)
    changes.append("Live filter OK")

# ── 3. Fix filterRuns() JS to use CSS vars ────────────────────────────────
old_fjs = """  document.querySelectorAll('.runs-filter-btn').forEach(b => {
    b.classList.remove('active');
    b.style.background = '';
    const f = b.getAttribute('data-filter');
    if (f === 'all') { b.style.background = 'rgba(255,255,255,0.08)'; b.style.color = 'var(--muted)'; }
    else if (f === 'backtest') { b.style.background = 'rgba(59,130,246,0.15)'; b.style.color = 'rgb(96,165,250)'; }
    else if (f === 'paper') { b.style.background = 'rgba(245,158,11,0.15)'; b.style.color = 'rgb(251,191,36)'; }
    else if (f === 'live') { b.style.background = 'rgba(139,92,246,0.15)'; b.style.color = 'rgb(167,139,250)'; }
  });
  if (btn) {
    btn.classList.add('active');
    if (mode === 'all') { btn.style.background = 'var(--accent)'; btn.style.color = '#000'; }
    else if (mode === 'backtest') { btn.style.background = 'rgb(59,130,246)'; btn.style.color = '#fff'; }
    else if (mode === 'paper') { btn.style.background = 'rgb(245,158,11)'; btn.style.color = '#000'; }
    else if (mode === 'live') { btn.style.background = 'rgb(139,92,246)'; btn.style.color = '#fff'; }
  }"""
new_fjs = """  const _setV = (el, bg, clr, bdr) => { el.style.setProperty('--btn-bg', bg); el.style.setProperty('--btn-color', clr); el.style.setProperty('--btn-border', bdr); };
  document.querySelectorAll('.runs-filter-btn').forEach(b => {
    b.classList.remove('active');
    const f = b.getAttribute('data-filter');
    if (f === 'all') _setV(b, 'linear-gradient(180deg, rgba(255,255,255,0.08) 0%, rgba(255,255,255,0.03) 100%)', 'var(--muted)', 'rgba(255,255,255,0.1)');
    else if (f === 'backtest') _setV(b, 'linear-gradient(180deg, rgba(59,130,246,0.25) 0%, rgba(40,90,180,0.4) 100%)', 'rgb(96,165,250)', 'rgba(59,130,246,0.5)');
    else if (f === 'paper') _setV(b, 'linear-gradient(180deg, rgba(245,158,11,0.25) 0%, rgba(180,120,8,0.4) 100%)', 'rgb(251,191,36)', 'rgba(245,158,11,0.5)');
    else if (f === 'live') _setV(b, 'linear-gradient(180deg, rgba(139,92,246,0.25) 0%, rgba(100,60,200,0.4) 100%)', 'rgb(167,139,250)', 'rgba(139,92,246,0.5)');
  });
  if (btn) {
    btn.classList.add('active');
    if (mode === 'all') _setV(btn, 'var(--accent)', '#000', 'rgba(0,200,150,0.5)');
    else if (mode === 'backtest') _setV(btn, 'linear-gradient(180deg, #3b82f6 0%, #2563eb 100%)', '#fff', 'rgba(59,130,246,0.7)');
    else if (mode === 'paper') _setV(btn, 'linear-gradient(180deg, #f59e0b 0%, #d97706 100%)', '#000', 'rgba(245,158,11,0.7)');
    else if (mode === 'live') _setV(btn, 'linear-gradient(180deg, #8b5cf6 0%, #7c3aed 100%)', '#fff', 'rgba(139,92,246,0.7)');
  }"""
if old_fjs in content:
    content = content.replace(old_fjs, new_fjs, 1)
    changes.append("filterRuns JS OK")
else:
    changes.append("filterRuns JS NOT found")

# ── 4. Convert all inline background: and color: on .btn elements to vars ─
# Match: <button ... class="btn..." ... style="... background: X; ..." ...>
# Convert background: X to --btn-bg: X
btn_bg_re = re.compile(r'(<button\b[^>]*?class="btn[^"]*?"[^>]*?style="[^"]*?)\bbackground:\s*([^;"]+);')


def fix_bg(m):
    prefix = m.group(1)
    val = m.group(2).strip()
    if "--btn-bg" in prefix:
        return m.group(0)
    return f"{prefix}--btn-bg: {val};"


prev = content
content, n = btn_bg_re.subn(fix_bg, content)
changes.append(f"bg->--btn-bg: {n} fixes")

# Now convert color: overrides (skip --btn-color: and already done)
btn_clr_re = re.compile(r'(<button\b[^>]*?class="btn[^"]*?"[^>]*?style="[^"]*?)(?<!-)(?<!-)\bcolor:\s*([^;"]+);')


def fix_clr(m):
    prefix = m.group(1)
    val = m.group(2).strip()
    if "--btn-color" in prefix or "--btn-border" in prefix:
        return m.group(0)
    # Only convert standalone color: not --XX-color:
    return f"{prefix}--btn-color: {val};"


content, n2 = btn_clr_re.subn(fix_clr, content)
changes.append(f"color->--btn-color: {n2} fixes")

# ── 5. Fix inline border: overrides on buttons to use --btn-border ────────
btn_bdr_re = re.compile(r'(<button\b[^>]*?class="btn[^"]*?"[^>]*?style="[^"]*?)\bborder:\s*([^;"]+);')


def fix_bdr(m):
    prefix = m.group(1)
    val = m.group(2).strip()
    if "--btn-border" in prefix:
        return m.group(0)
    # Extract color from "1px solid <color>"
    bdr_m = re.match(r"[\d.]+px\s+\w+\s+(.*)", val)
    if bdr_m:
        return f"{prefix}--btn-border: {bdr_m.group(1).strip()};"
    return m.group(0)


content, n3 = btn_bdr_re.subn(fix_bdr, content)
changes.append(f"border->--btn-border: {n3} fixes")

with open("strategy.html", "w", encoding="utf-8") as f:
    f.write(content)

for c in changes:
    print(c)
print(f"Size: {orig} -> {len(content)}")
