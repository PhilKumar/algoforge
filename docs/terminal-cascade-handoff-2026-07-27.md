# Terminal and Cascade Handoff

**Scope:** PhilForge Terminal and Cascade work completed on 25-26 July 2026.

**Included:**

- Terminal cash Cascade for BEES and equities.
- Cascade tab NIFTY options paper Cascade.
- Cascade Signal Replay / 1H Candle Entry paper flow.

**Excluded:** Every other PhilForge tab, general deployment work, and unrelated
working-tree changes.

## Current Snapshot

- Committed and pushed head: `afcdb7f` (`Make Terminal Cascade instruments expandable`).
- The recorded blue-green deployment of that commit succeeded. This document is
  not a current production-health probe.
- Terminal and options Cascade are hard paper-only. No work in this scope added
  a broker live-order path.
- The current working tree has unrelated modified/generated files. Do not bulk
  stage, reset, or clean it while working on these features.

## System Map

```text
Terminal
  Terminal form / instrument strip
    -> /api/terminal/cascade/*
      -> Dhan signal and trade candles
        -> CashCascadePaperEngine
          -> per-user persisted paper runtime
            -> poll closed bars every 12 seconds

Cascade: NIFTY options paper
  Cascade form
    -> /api/cascade/paper/*
      -> NiftyIndexCascadeGeometry
        -> fixed CE paper adapter / paper fills / costs / rounds
          -> per-user persisted paper runtime

Cascade: 1H Signal Replay
  Candle Entry form
    -> /api/candle-entry/paper/start
      -> current-day quote-backed paper campaign, or
      -> historical signal-only replay with no invented option P&L
```

## Terminal Cash Cascade

### Delivered behavior

The cash engine is in `engine/cascade_equity.py` (`CashCascadePaperEngine`).
The HTTP integration is in `app.py` under `/api/terminal/cascade/*`.

- `NIFTYBEES` uses NIFTY as the signal series and NIFTYBEES as the trade/TP
  series.
- `BANKBEES` uses BANKNIFTY as the signal series.
- Other equities use their own series for signal and trade.
- The mother candle must be completed, NSE-session aligned, and no older than
  14 calendar days. `5m`, `15m`, and `1h` are supported; 1H mothers must be
  aligned at `:15` IST.
- Start replays Dhan candles from the mother through now. The running paper
  engine then polls closed bars every 12 seconds.
- Inputs: capital, target fraction, timeframe, and CNC/MTF product type.
- State includes geometry, rungs, fills, costs, open/closed rounds, carry and
  pending amounts. It is persisted per user and restored after app restart.
- A user can run multiple campaigns at once, one per normalized symbol.
- Every action is paper-only: no Dhan cash order is submitted by this engine.

### Routes

| Method | Route | Purpose |
| --- | --- | --- |
| GET | `/api/terminal/cascade/status` | Returns all campaigns for the user. |
| GET | `/api/terminal/cascade/chart` | Replays and returns a chart for a symbol/mother. |
| POST | `/api/terminal/cascade/start` | Starts a paper campaign. |
| POST | `/api/terminal/cascade/stop` | Stops polling for a symbol. |
| POST | `/api/terminal/cascade/kill` | Paper-closes/cancels a symbol campaign. |
| DELETE | `/api/terminal/cascade?symbol=...` | Removes a symbol campaign. |

### UI delivered

- `046b3e5` changed the original single campaign card into a two-column
  workspace with campaign setup left and a scrollable multi-scrip instrument
  flow right.
- Each instrument is now a native `<details>` panel. Clicking the strip opens
  the flow-down body; the open-symbol set survives periodic status rerenders.
- Chart, Stop, and Delete are side-by-side icon controls. Their clicks do not
  toggle the `<details>` strip.
- The action controls have blue/red glossy gradients, a highlight sheen, inset
  shadow, hover lift, and pressed state.
- `Structure, fibs and order details` is a separate collapsible details block.

### Chart behavior

The Terminal chart endpoint now returns `chart_mode: "native_ohlc"`. It uses
real Dhan `open/high/low/close` values in the payload and renderer. The SVG
draws body plus wick per candle, mother-candle highlight, fib lines, fills, and
trendlines.

The valid trendline rule is in `find_index_valid_anchor2` in
`engine/cascade_options.py`:

1. Anchor one is the mother high.
2. Search backwards through red-candle opens for anchor two.
3. Reject an anchor if a preceding candle **close** crossed above the candidate
   line beyond the configured tolerance. Wick-only crossings are allowed.
4. Draw fibs only when a valid shelf/leg is formed. Duplicate same-shelf lines
   are visual-only dashed geometry and have no fib rungs.

The frontend projects anchors on the visible candle sequence rather than linear
calendar time, so overnight/weekend gaps cannot pull a line away from its
mother-high and red-open anchors.

### Terminal failures and corrections

| Problem | Cause | Correction |
| --- | --- | --- |
| Candles looked Heikin-Ashi-like. | The display used gap-adjusted opens. | `52a20e5` changed Terminal charts to native OHLC. |
| Trendlines appeared broken/misaligned after non-trading gaps. | The SVG x-coordinate used elapsed time while bars visually compressed gaps. | `a8cb087` maps timestamps to candle positions before projecting a line. |
| Chart/Stop/Delete appeared vertically stacked. | A generic CSS grid selector styled the action container. | Selector now excludes `.terminal-cascade-card-actions`. |
| Instrument area was not an actual flow-down window. | It was a fixed card/scroll layout only. | `afcdb7f` uses `<details>/<summary>` with retained open state. |
| Buttons did not follow the requested glossy site treatment. | Generic button styling remained. | Added scoped glossy icon-control CSS. |

## Cascade Tab: NIFTY Options Paper

### Delivered architecture

The core implementation is in `engine/cascade_options.py`.

- `NiftyIndexCascadeGeometry` is index-space geometry only. It deliberately
  contains no option premium, contract, broker, or order state.
- `CascadeOptionsAdapter` is hard locked to `paper_only=True`; attempting live
  construction raises `PaperOnlyViolation`.
- `NiftyOptionsPaperCascade` converts valid NIFTY geometry into paper CE rungs,
  fills, costs, and rounds.
- A contract is selected once at the mother: CE-only, next weekly expiry,
  default ATM-2 offset, and lot size from ScripMaster.
- The open options-paper campaign persists per user and resumes its 5-minute
  paper polling after app restart.
- Existing navigation was nested under Cascade and start/navigation failure
  messages were hardened.

### Paper endpoints

| Method | Route | Purpose |
| --- | --- | --- |
| GET | `/api/cascade/paper/status` | Returns the paper campaign. |
| GET | `/api/cascade/paper/chart` | Returns NIFTY display candles for a mother. |
| POST | `/api/cascade/paper/start` | Starts NIFTY options paper Cascade. |
| POST | `/api/cascade/paper/stop` | Stops paper monitoring. |
| POST | `/api/cascade/paper/kill` | Paper-closes/cancels the campaign. |
| DELETE | `/api/cascade/paper` | Deletes the campaign. |

## Cascade Tab: Signal Replay / 1H Candle Entry

`OneHourCandleEntryPaper` is a separate CE-only, no-escalation paper campaign.
Two qualifying lower-closing red 1H candles arm a recovery buy-stop based on
the latest qualifying red close.

- A mother from today starts a quote-backed paper campaign and polls closed 1H
  NIFTY bars every 20 seconds.
- A completed historical mother from the prior 15 calendar days runs finite
  `signal_only` replay.
- Historical replay derives the historical weekly expiry and reports index
  entry/target outcome, but never pairs a historical signal with a current
  option LTP. Therefore it returns no fabricated fixed-strike option P&L.
- Historical requests return `status: "replayed"`; they do not start monitoring.
- Current-day requests return `status: "started"`.
- The UI accepts both statuses and labels replay correctly.
- The Candle Entry runtime is currently in-memory only; unlike the 5m options
  campaign, it is not restored after an app restart.

### Replay failure and correction

The backend correctly returned `replayed`, but the old frontend only recognized
`started`, then showed “Campaign did not start.” `3d0832b` fixed the status
handling and made the asset version refresh when the manifest changes.

`8dcb5b3` also standardized the date widgets: date-only fields remain
`YYYY-MM-DD`; mother timestamps remain `YYYY-MM-DDTHH:MM`.

## Remaining Work / Verification Checklist

1. **Cache-bust the latest Terminal UI.** `a8cb087` and `afcdb7f` changed JS/CSS
   but did not bump `static/asset-manifest.json`; the browser can retain older
   controls and layout. Bump the manifest for every static UI deployment and
   verify the actual HTML returns new asset URLs.
2. **Run real browser validation on the authenticated deployed app.** Verify
   strip click expands exactly one flow-down body, controls are glossy and
   horizontally aligned, details arrows rotate, and periodic refresh preserves
   expanded state. Do not validate this through a static server.
3. **Validate the Terminal chart with known Dhan data.** Confirm each displayed
   candle matches native OHLC and every trendline circle is on mother high and
   the intended red-candle open. Confirm fibs are absent for dashed same-shelf
   trendlines.
4. **Decide whether starting a Terminal campaign should auto-open the chart.**
   The source still opens the chart after start; this may conflict with the
   desired instrument-strip-first interaction.
5. **Unify chart fidelity if required.** Terminal is native OHLC, but
   `/api/cascade/paper/chart` still returns `visual_gap_adjusted` display
   candles. Port the native contract to that endpoint if both tabs must have
   identical candlestick fidelity.
6. **Consider Candle Entry persistence** if users expect a 1H campaign to
   survive process restart.

## Relevant Commits

| Commit | Summary |
| --- | --- |
| `1ac6888` | Paper-only NIFTY index geometry port. |
| `ed00e7a` | NIFTY paper round execution. |
| `507864b` | Persistent NIFTY options paper campaign. |
| `232344c` | Historical 1H Candle Entry signal replay. |
| `3d0832b` | Replay status/frontend cache correction. |
| `8dcb5b3` | Calendar picker and placeholder consistency. |
| `5765780` | Terminal cash Cascade for BEES and equities. |
| `52a20e5` | Terminal native-OHLC chart interface correction. |
| `046b3e5` | Multi-scrip Terminal flow. |
| `a8cb087` | Flow/action alignment and bar-axis trendline projection. |
| `afcdb7f` | Expandable Terminal instrument strips and glossy controls. |

## Validation History

- Scoped Cascade engine tests were added/expanded for cash geometry, options
  geometry, paper costs, expiry, and paper routes.
- The historical replay change passed its targeted tests, Python compilation,
  JavaScript syntax validation, and pre-commit checks.
- Later Terminal work passed targeted engine tests, JavaScript syntax checking,
  and diff checks.
- Final post-deployment browser visual regression evidence is still required.
