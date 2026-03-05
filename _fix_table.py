import re

with open('strategy.html', 'r', encoding='utf-8') as f:
    content = f.read()

# Dump the completed trades thead
idx = content.find('completedTradesTable')
if idx != -1:
    t = content.find('<thead>', idx)
    print("THEAD:", repr(content[t:t+700]))

orig_len = len(content)

# 1. Fix empty-state colspan (already done, but idempotent)
content = content.replace(
    'colspan="7" style="text-align:center;padding:20px;color:var(--muted);">No completed trades yet',
    'colspan="9" style="text-align:center;padding:20px;color:var(--muted);">No completed trades yet'
)

# 2. Fix header – find the thead inside the completed trades table
# We locate the unique surrounding context to avoid touching other tables
old_hdr_needle = ('<th style="padding:8px 12px;text-align:left;color:var(--muted);font-size:11px;">Symbol</th>'
                  '<th style="padding:8px 12px;text-align:right;color:var(--muted);font-size:11px;">Type</th>'
                  '<th style="padding:8px 12px;text-align:right;color:var(--muted);font-size:11px;">Entry \u20b9</th>'
                  '<th style="padding:8px 12px;text-align:right;color:var(--muted);font-size:11px;">Exit \u20b9</th>'
                  '<th style="padding:8px 12px;text-align:right;color:var(--muted);font-size:11px;">Qty</th>'
                  '<th style="padding:8px 12px;text-align:right;color:var(--muted);font-size:11px;">P&L</th>'
                  '<th style="padding:8px 12px;text-align:right;color:var(--muted);font-size:11px;">Reason</th>')
new_hdr_needle = ('<th style="padding:8px 12px;text-align:left;color:var(--muted);font-size:11px;">Symbol</th>'
                  '<th style="padding:8px 12px;text-align:left;color:var(--muted);font-size:11px;">Entry Time</th>'
                  '<th style="padding:8px 12px;text-align:left;color:var(--muted);font-size:11px;">Exit Time</th>'
                  '<th style="padding:8px 12px;text-align:right;color:var(--muted);font-size:11px;">Type</th>'
                  '<th style="padding:8px 12px;text-align:right;color:var(--muted);font-size:11px;">Entry \u20b9</th>'
                  '<th style="padding:8px 12px;text-align:right;color:var(--muted);font-size:11px;">Exit \u20b9</th>'
                  '<th style="padding:8px 12px;text-align:right;color:var(--muted);font-size:11px;">Qty</th>'
                  '<th style="padding:8px 12px;text-align:right;color:var(--muted);font-size:11px;">P&L</th>'
                  '<th style="padding:8px 12px;text-align:right;color:var(--muted);font-size:11px;">Reason</th>')

if old_hdr_needle in content:
    content = content.replace(old_hdr_needle, new_hdr_needle, 1)
    print("Header replaced OK")
else:
    print("Header NOT found – dumping nearby context:")
    idx = content.find('>Symbol<')
    if idx != -1:
        print(repr(content[idx-80:idx+300]))

# 3. Fix the row builder for closedHtml
# Find the map callback function
start_marker = "closedHtml = closed.map(t => {"
s = content.find(start_marker)
if s == -1:
    print("Row start marker NOT found")
else:
    # Find end of the map (}).join('')
    e = content.find("}).join('');", s)
    if e == -1:
        print("Row end marker NOT found")
    else:
        old_block = content[s:e + len("}).join('');")]
        print("Old block (first 200 chars):", repr(old_block[:200]))

        new_block = (
            "closedHtml = closed.map(t => {\n"
            "      const pnl = round2(t.pnl || 0);\n"
            "      const fmtTime = (ts) => { if (!ts) return '\u2014'; const ss = String(ts); const m = ss.match(/(\\d{2}:\\d{2}:\\d{2})/); return m ? m[1] : ss.slice(-8); };\n"
            "      return `<tr style=\"border-bottom:1px solid var(--border);\">"
            "<td style=\"padding:8px 12px;\">${t.symbol||t.trading_symbol||'\u2014'}</td>"
            "<td style=\"padding:8px 12px;font-family:'JetBrains Mono';font-size:11px;color:var(--muted);\">${fmtTime(t.entry_time)}</td>"
            "<td style=\"padding:8px 12px;font-family:'JetBrains Mono';font-size:11px;color:var(--muted);\">${fmtTime(t.exit_time)}</td>"
            "<td style=\"padding:8px 12px;text-align:right;color:${t.transaction_type==='BUY'?'var(--success)':'var(--danger)'}\">${t.transaction_type||'\u2014'}</td>"
            "<td style=\"padding:8px 12px;text-align:right;font-family:'JetBrains Mono';\">\u20b9${round2(t.entry_premium||t.entry_price||0).toFixed(2)}</td>"
            "<td style=\"padding:8px 12px;text-align:right;font-family:'JetBrains Mono';\">\u20b9${round2(t.exit_premium||t.exit_price||0).toFixed(2)}</td>"
            "<td style=\"padding:8px 12px;text-align:right;\">${t.lots||t.quantity||'\u2014'}</td>"
            "<td style=\"padding:8px 12px;text-align:right;font-family:'JetBrains Mono';color:${pnl>=0?'var(--success)':'var(--danger)'}\">\u20b9${pnl.toFixed(2)}</td>"
            "<td style=\"padding:8px 12px;text-align:right;font-size:11px;color:var(--muted);\">${t.exit_reason||t.reason||'\u2014'}</td>"
            "</tr>`;\n"
            "    }).join('');"
        )

        content = content[:s] + new_block + content[e + len("}).join('');"):]
        print("Row replaced OK")

with open('strategy.html', 'w', encoding='utf-8') as f:
    f.write(content)

print(f"Done. Size: {orig_len} -> {len(content)}")
