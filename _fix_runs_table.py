with open('strategy.html', 'r', encoding='utf-8') as f:
    content = f.read()

orig = len(content)

# ── 1. Main runs table header ──────────────────────────────────────────────
old_hdr = ('<th style="padding: 10px;">Trades</th>'
           '<th style="padding: 10px;">P&L</th>'
           '<th style="padding: 10px;">Win%</th>'
           '<th style="padding: 10px;">Date</th>'
           '<th style="padding: 10px; width: 200px; min-width: 200px; text-align: center;">Actions</th>')
new_hdr = ('<th style="padding: 10px;">Trades</th>'
           '<th style="padding: 10px;">P&L</th>'
           '<th style="padding: 10px;">Entry Time</th>'
           '<th style="padding: 10px;">Exit Time</th>'
           '<th style="padding: 10px; width: 200px; min-width: 200px; text-align: center;">Actions</th>')
if old_hdr in content:
    content = content.replace(old_hdr, new_hdr, 1)
    print("Main header OK")
else:
    print("Main header NOT found")

# ── 2. Main runs table forEach preamble + win%/date cells ─────────────────
# Replace from "runs.forEach(r => {" up through the date cell
old_rows_preamble = (
    'runs.forEach(r => {\n'
    '    const pnlColor = (r.total_pnl || 0) >= 0 ? \'var(--success)\' : \'var(--danger)\';\n'
    '    const winRate = r.stats ? r.stats.win_rate : 0;\n'
    '    const instName = getInstrumentName(r.instrument);\n'
    '    html += `<tr style="border-bottom: 1px solid rgba(37,42,56,0.2);" data-run-mode="${_normalizeMode(r.mode)}">\n'
    '      <td style="padding: 10px; color: var(--muted);">${r.id}</td>\n'
    '      <td style="padding: 10px;">${_getModeBadge(r.mode)}</td>\n'
    '      <td style="padding: 10px; font-weight: 600; color: var(--accent); cursor: pointer;" onclick="viewRun(${r.id})">${r.run_name || \'Unnamed\'}</td>\n'
    '      <td style="padding: 10px;">${instName || \'-\'}</td>\n'
    '      <td style="padding: 10px; font-size: 12px;">${r.from_date || \'\'} \u2192 ${r.to_date || \'\'}</td>\n'
    '      <td style="padding: 10px; font-weight: 600;">${r.trade_count || 0}</td>\n'
    '      <td style="padding: 10px; font-weight: 700; color: ${pnlColor}; font-family: \'JetBrains Mono\', monospace;">${fmt(r.total_pnl || 0)}</td>\n'
    '      <td style="padding: 10px; font-weight: 600; color: ${winRate >= 50 ? \'var(--success)\' : \'var(--danger)\'};">${winRate}%</td>\n'
    '      <td style="padding: 10px; font-size: 12px; color: var(--muted);">${(r.created_at || \'\').substring(0, 16)}</td>'
)
new_rows_preamble = (
    'runs.forEach(r => {\n'
    '    const pnlColor = (r.total_pnl || 0) >= 0 ? \'var(--success)\' : \'var(--danger)\';\n'
    '    const instName = getInstrumentName(r.instrument);\n'
    '    const _rTrades = r.trades || [];\n'
    '    const _fmtRT = (ts) => { if (!ts) return \'\u2014\'; const s = String(ts); const m = s.match(/(\\d{2}:\\d{2})/); return m ? m[1] : s.substring(11, 16); };\n'
    '    const rEntryTime = _rTrades.length ? _fmtRT(_rTrades[0].entry_time) : _fmtRT(r.created_at);\n'
    '    const rExitTime = _rTrades.length ? _fmtRT(_rTrades[_rTrades.length-1].exit_time) : \'\u2014\';\n'
    '    html += `<tr style="border-bottom: 1px solid rgba(37,42,56,0.2);" data-run-mode="${_normalizeMode(r.mode)}">\n'
    '      <td style="padding: 10px; color: var(--muted);">${r.id}</td>\n'
    '      <td style="padding: 10px;">${_getModeBadge(r.mode)}</td>\n'
    '      <td style="padding: 10px; font-weight: 600; color: var(--accent); cursor: pointer;" onclick="viewRun(${r.id})">${r.run_name || \'Unnamed\'}</td>\n'
    '      <td style="padding: 10px;">${instName || \'-\'}</td>\n'
    '      <td style="padding: 10px; font-size: 12px;">${r.from_date || \'\'} \u2192 ${r.to_date || \'\'}</td>\n'
    '      <td style="padding: 10px; font-weight: 600;">${r.trade_count || 0}</td>\n'
    '      <td style="padding: 10px; font-weight: 700; color: ${pnlColor}; font-family: \'JetBrains Mono\', monospace;">${fmt(r.total_pnl || 0)}</td>\n'
    '      <td style="padding: 10px; font-family: \'JetBrains Mono\'; font-size: 12px; color: var(--muted);">${rEntryTime}</td>\n'
    '      <td style="padding: 10px; font-family: \'JetBrains Mono\'; font-size: 12px; color: var(--muted);">${rExitTime}</td>'
)
if old_rows_preamble in content:
    content = content.replace(old_rows_preamble, new_rows_preamble, 1)
    print("Main rows OK")
else:
    # Debug: find the win% line
    idx = content.find('${winRate}%</td>')
    print(f"Main rows NOT found. Win% line at offset {idx}")
    if idx != -1:
        print(repr(content[idx-200:idx+100]))

# ── 3. Portfolio paper runs table rows ────────────────────────────────────
old_paper_rows = (
    'paperRuns.forEach(r => {\n'
    '    const pnlColor = (r.total_pnl || 0) >= 0 ? \'var(--success)\' : \'var(--danger)\';\n'
    '    const winRate = r.stats ? r.stats.win_rate : 0;\n'
    '    const instName = getInstrumentName(r.instrument);\n'
    '    html += `<tr style="border-bottom: 1px solid rgba(37,42,56,0.2);">\n'
    '      <td style="padding: 8px 6px; color: var(--muted); font-size: 11px;">#${r.id}</td>\n'
    '      <td style="padding: 8px 6px; font-weight: 600; color: var(--accent); cursor: pointer;" onclick="viewRun(${r.id})">${r.run_name || \'Unnamed\'}</td>\n'
    '      <td style="padding: 8px 6px;">${instName || \'-\'}</td>\n'
    '      <td style="padding: 8px 6px; font-weight: 600;">${r.trade_count || 0}</td>\n'
    '      <td style="padding: 8px 6px; font-weight: 700; color: ${pnlColor}; font-family: \'JetBrains Mono\', monospace;">${fmt(r.total_pnl || 0)}</td>\n'
    '      <td style="padding: 8px 6px; font-weight: 600; color: ${winRate >= 50 ? \'var(--success)\' : \'var(--danger)\'};">${winRate}%</td>\n'
    '      <td style="padding: 8px 6px; font-size: 11px; color: var(--muted);">${(r.created_at || \'\').substring(0, 16)}</td>'
)
new_paper_rows = (
    'paperRuns.forEach(r => {\n'
    '    const pnlColor = (r.total_pnl || 0) >= 0 ? \'var(--success)\' : \'var(--danger)\';\n'
    '    const instName = getInstrumentName(r.instrument);\n'
    '    const _pTrades = r.trades || [];\n'
    '    const _fmtPT = (ts) => { if (!ts) return \'\u2014\'; const s = String(ts); const m = s.match(/(\\d{2}:\\d{2})/); return m ? m[1] : s.substring(11, 16); };\n'
    '    const pEntryTime = _pTrades.length ? _fmtPT(_pTrades[0].entry_time) : _fmtPT(r.created_at);\n'
    '    const pExitTime = _pTrades.length ? _fmtPT(_pTrades[_pTrades.length-1].exit_time) : \'\u2014\';\n'
    '    html += `<tr style="border-bottom: 1px solid rgba(37,42,56,0.2);">\n'
    '      <td style="padding: 8px 6px; color: var(--muted); font-size: 11px;">#${r.id}</td>\n'
    '      <td style="padding: 8px 6px; font-weight: 600; color: var(--accent); cursor: pointer;" onclick="viewRun(${r.id})">${r.run_name || \'Unnamed\'}</td>\n'
    '      <td style="padding: 8px 6px;">${instName || \'-\'}</td>\n'
    '      <td style="padding: 8px 6px; font-weight: 600;">${r.trade_count || 0}</td>\n'
    '      <td style="padding: 8px 6px; font-weight: 700; color: ${pnlColor}; font-family: \'JetBrains Mono\', monospace;">${fmt(r.total_pnl || 0)}</td>\n'
    '      <td style="padding: 8px 6px; font-family: \'JetBrains Mono\'; font-size: 11px; color: var(--muted);">${pEntryTime}</td>\n'
    '      <td style="padding: 8px 6px; font-family: \'JetBrains Mono\'; font-size: 11px; color: var(--muted);">${pExitTime}</td>'
)
if old_paper_rows in content:
    content = content.replace(old_paper_rows, new_paper_rows, 1)
    print("Portfolio rows OK")
else:
    idx = content.find('${winRate}%</td>')
    print(f"Portfolio rows NOT found. Win% at offset {idx}")
    if idx != -1:
        print(repr(content[idx-300:idx+50]))

with open('strategy.html', 'w', encoding='utf-8') as f:
    f.write(content)

print(f"Done. Size: {orig} -> {len(content)}")
