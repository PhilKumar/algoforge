# Paper Session Checklist

This runbook is for one full paper session on the deployed build. Use it before any live canary.

## Monday, March 30, 2026

### Pre-open

Run these checks between `09:05` and `09:12` IST:

1. Confirm the deployed commit is at least `a0c230b`.
2. Confirm the app is healthy:
   ```bash
   curl -s http://127.0.0.1:8000/api/health
   ```
3. Start the paper run fresh for the strategy under test.
4. Verify the paper engine loaded history, not a cold snapshot:
   - `event_log` must show `Candle aggregation: 5m (including N historical candles)` with `N > 0`
   - current indicators must include real values for the strategy dependencies such as `CPR_BC` and `EMA_20_5m`
5. Do not edit the strategy after the session starts. If you change conditions or indicators, restart the paper session and restart the checklist.

### During Session

1. Do not restart the paper engine unless there is a genuine failure.
2. If you do restart it, note the exact restart time in the report.
3. Do not change broker auth or market-data settings during the session.
4. Leave the run active through market close so `/api/paper/status` still has the runtime strategy payload for reconciliation.

### Post-close

Run the reconciliation after market close for the exact session date. On the deployed host:

```bash
PHILFORGE_TOKEN=YOUR_TOKEN ./venv/bin/python scripts/reconcile_paper_session.py \
  --date 2026-03-30 \
  --run-id Strategy_PE \
  --base-url http://127.0.0.1:8000 \
  --output-dir reconciliation_reports
```

Artifacts written under `reconciliation_reports/...`:

- `paper_status.json`
- `ohlcv_export.json`
- `report.json`
- `summary.md`

### Pass Criteria

Treat the paper session as clean only if all of these are true:

1. `Overall: PASS` in the reconciliation report.
2. `Replay missing-data candles` is `0`.
3. Every actual paper entry signal falls inside a replay pass window.
4. If `max_trades/day = 1`, the first actual paper entry must occur inside the first replay entry window.
5. No paper engine error events appear in the report.

### What This Reconciliation Covers

- entry-condition replay versus actual paper entries
- missing-data detection for indicator-dependent conditions
- paper trade exit completeness: `exit_time`, `exit_reason`, `pnl`
- event-log warnings and errors

### What It Does Not Yet Prove

- broker-side fills
- slippage or partial-fill behavior
- live order acknowledgement paths

Those belong to the live canary phase after the paper gate passes.
