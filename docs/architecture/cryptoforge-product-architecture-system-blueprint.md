# CryptoForge Product Architecture and System Blueprint

**Document status:** Complete production architecture reference
**Architecture snapshot date:** 14 August 2026
**Source baseline:** CryptoForge `dff2131`
**Audience:** Product owners, developers, operators, security reviewers, support staff, and future architects

This is the independent architecture document for CryptoForge. It describes only the crypto platform and its buyer-side Cascade executor. Shared hosting comparisons belong in the separate [estate overview](./cryptoforge-philforge-product-architecture-system-blueprint.md).

---

# Chapter 1: The Eagle's View

## 1.1 Product mission

CryptoForge is a crypto strategy, trading, portfolio, and campaign platform. It supports historical testing, paper trading, direct live trading, scalp workflows, and the Cascade strategy. Its broker layer can work with crypto spot markets and perpetual futures, depending on the selected adapter.

CryptoForge also acts as a signal control plane for paid Cascade buyers. The server publishes signed strategy geometry. A separate executor runs on each buyer's own computer and places orders in that buyer's Binance or CoinDCX account. CryptoForge does not need the buyer's exchange key, balance, order IDs, fills, or private P&L.

The product has two distinct execution models:

| Execution model | Where orders run | Who owns credentials | What CryptoForge owns |
|---|---|---|---|
| Operator trading | CryptoForge server | Platform operator | Strategy, engine, broker adapter, orders, reconciliation, recovery, and ledger |
| Buyer Cascade | Buyer's desktop executor | Buyer | Signed public strategy geometry, subscription entitlement, replayable feed, and publisher health |

## 1.2 Product profile

| Concern | CryptoForge design |
|---|---|
| Asset classes | Crypto spot pairs and crypto perpetual futures |
| Market schedule | Continuous, 24 hours a day and 7 days a week |
| Main direct users | Platform operator, strategy developer, backtester, paper trader, live trader, scalp trader, and Cascade operator |
| Additional users | Paid Cascade buyers running the desktop executor |
| Default broker | Binance Spot |
| Other adapters | CoinDCX Spot, CoinDCX Futures, and Delta Exchange |
| Public origins | `crypto.philforge.in` and `philforge.in/crypto` |
| Application ports | Blue-green pair `9000` and `9001` |
| Runtime model | One active FastAPI/Uvicorn worker with in-process asynchronous engines |
| Primary database | Local SQLite JSON-document store in WAL mode |
| Market transport | Delta WebSocket where supported, with broker-neutral REST ticker polling as fallback |
| Browser transport | Authenticated REST plus `/ws` WebSocket |
| Buyer transport | Signed and sequenced `/ws/cascade-feed` WebSocket |
| Front end | Server-hosted HTML, CSS, plain JavaScript, Canvas/SVG charts, service worker, and PWA manifest |
| Hosting | AWS Linux virtual machine behind Nginx and systemd |

The timing values below are engine cadences, not execution guarantees:

| Workload | Typical design cadence |
|---|---|
| Scalp monitor | 250-millisecond internal monitor target |
| Generic ticker fallback | Normally 1 second, with a 500-millisecond lower bound |
| Cascade screen status | Commonly refreshed by the browser every 3 seconds |
| Standard live or paper strategy | Normally checks on a 30-second poll while respecting candle-close rules |
| Buyer executor | Default configuration checks every 20 seconds; feed entitlement is rechecked on heartbeat |

Actual order time depends on network delay, broker throttling, order type, liquidity, and exchange load.

## 1.3 System stack

| Layer | Technology | Responsibility |
|---|---|---|
| Public edge | Nginx | Route HTTPS and WebSocket traffic to the active CryptoForge port |
| Application | Python 3.11, FastAPI, Starlette, Pydantic, Uvicorn | Authenticate requests, validate inputs, expose APIs, and own engine lifecycles |
| Concurrency | Python `asyncio`, threads for blocking broker calls | Run monitors without making the whole server wait on one broker request |
| Front end | HTML, CSS, plain JavaScript | Forms, dashboards, ledgers, charts, admin tools, and PWA behavior |
| Charting | Canvas and SVG | Render native candles, Cascade geometry, trendlines, fills, and equity curves |
| Analytics | Pandas and NumPy | Normalize candles, calculate indicators, run simulations, and produce metrics |
| State | SQLite document store with WAL | Store sessions, strategies, runs, engine state, feed events, billing events, and notifications |
| Caches | Local market-data files | Avoid downloading the same historical candles repeatedly |
| Market APIs | Binance, CoinDCX, Delta, CoinGecko, and selected FX providers | Prices, candles, account data, order routing, market overview, and INR display conversion |
| Alerts | Telegram and Discord helpers | Report important engine, account, and operational events |
| Quality gates | Ruff, Bandit, Gitleaks, dependency audit, pytest, Playwright | Stop unsafe or broken revisions before deployment |
| Operations | GitHub Actions, SSH, systemd, Nginx | Test, deploy, restart, health-check, and isolate the service |

CryptoForge does not require Kafka, RabbitMQ, or a Redis event stream. Optional local Redis support can strengthen rate limits and login lockouts, but it is not the trading queue. Engine tasks and registries live inside the one active Python process.

## 1.4 Central wiring map

### 1.4.1 Operator command path

**Browser action → HTTPS → Nginx → active Uvicorn worker → session and rate-limit checks → payload validation → engine/risk logic → broker adapter → exchange REST API → normalized broker result → SQLite/recovery state → REST response and WebSocket event → browser**

1. The operator selects a symbol, mode, strategy, and risk settings.
2. Browser JavaScript sends a same-origin request.
3. Nginx forwards it to port 9000 or 9001, whichever is active.
4. FastAPI checks the session, origin, rate limit, and request body.
5. The route starts a new engine or asks an existing engine to act.
6. The engine checks market data, mode, capital, current orders, stops, targets, and campaign state.
7. The broker adapter translates CryptoForge symbols and order meaning into the selected exchange contract.
8. The exchange returns an acceptance, rejection, fill, partial fill, or pending order.
9. Live paths verify or reconcile the result against the broker's orders, fills, balances, or positions.
10. The application stores the important result and pushes the new state to the browser.

### 1.4.2 Market-data path

| Input | Processing | Output |
|---|---|---|
| Exchange ticker or WebSocket update | Normalize symbol and price fields; mark freshness and source | Scalp and live-monitor price state |
| Historical candle request | Check broker/symbol/interval cache; fetch missing span; normalize OHLCV | Chart data, indicators, and backtest frames |
| Delta WebSocket candle | Authenticate when required; subscribe; reconnect with backoff; dispatch callback | Real-time candle or ticker event |
| Broker-neutral fallback | Poll the active broker's ticker method | Fresh price when a native stream is unavailable |

### 1.4.3 Backtest path

**Instrument and rules → candle cache → missing-data fetch → indicator calculation → forward-only simulation → fees/leverage/liquidation logic → trades and equity curve → performance metrics → saved run**

The backtest does not place a broker order. It estimates behavior from historical candles. A live fill can still differ because the exchange controls acceptance, queue position, slippage, and final fill price.

## 1.5 Runtime engines

| Engine family | Purpose | Main input | Main output |
|---|---|---|---|
| Backtest | Recreate a strategy over historical candles | Cached/fetched OHLCV and strategy contract | Trades, equity, metrics, warnings, and run history |
| Paper | Follow a live market without sending orders | Closed candles plus optional fast ticker | Simulated fills, P&L, events, and restart state |
| Live | Trade through the active broker adapter | Closed candles, ticker, broker account, and risk settings | Real orders, fills, positions, alerts, and recovery state |
| Scalp | Manage fast manual or semi-automatic entries and exits | Fresh ticker, target, stop, and broker state | Fast trade monitor, broker actions, and trade ledger |
| Cascade | Build mother-candle geometry and manage laddered campaigns | Native OHLC, fib structure, trendlines, and capital model | Campaigns, orders/fills in operator mode, or public geometry in buyer-feed mode |
| Rule 30/70 | Run its locked paper strategy per instrument | Market candles and rule state | Paper trades, state, and journal |

An engine is registered under a run or campaign identity. The browser can close after startup; the server task continues. On process shutdown, the platform stores enough state to recover monitoring safely.

## 1.6 Persistence and recovery

CryptoForge's SQLite store holds JSON documents in named buckets. This supports different engine shapes without forcing unrelated engines into one rigid table.

| State group | Examples |
|---|---|
| Identity and access | Sessions and login protection state |
| Product setup | Strategies, selected broker, and application settings |
| Trading history | Runs, scalp trades, closed campaigns, and journal-derived records |
| Active recovery | Scalp runtime and Cascade runtime snapshots |
| Communication | Notifications, buyer feed events, feed subscribers, and billing events |
| Visual fidelity | Frozen Cascade chart snapshots for completed history |

The store uses SQLite WAL mode, a busy timeout, and restricted file permissions. A Cascade file lock acts as a single-writer fence during process overlap. If a process cannot get the lock, it may observe but must not place orders.

## 1.7 Buyer Cascade architecture

### 1.7.1 Publisher contract

Only public candle-derived facts can leave CryptoForge. Feed builders construct explicit fields, and a second denylist scan rejects account-specific names at any depth.

Never-published examples include:

- Capital, available balance, and pool size
- Quantities, fills, positions, and order IDs
- Average entry, realized P&L, and account fees
- Take-profit order price or broker-side recovery state
- Operator event logs and deployment mode

### 1.7.2 Signed delivery

1. The buyer creates a local public/private identity pair.
2. CryptoForge registers only the public key and subscription identity.
3. The publisher signs serialized feed messages with an operational Ed25519 key.
4. A root-authorized key set lets the executor decide which signing key is valid.
5. Every message has a sequence so a buyer can detect missing or reordered events.
6. Important events are retained for seven days; heartbeats are transient.
7. Entitlement is checked at connection and again at each heartbeat.
8. The executor applies its own capital, fee rate, minimum quantity, and exchange rules.

### 1.7.3 Local execution boundary

The desktop executor stores the buyer's exchange credentials locally, preferably in macOS Keychain or Windows Credential Manager. It talks directly to Binance or CoinDCX. It keeps its own ledger, manages already-held positions when a subscription ends, and can recover from a short feed interruption using the signed sequence and local state.

## 1.8 Deployment and operations

| Phase | Behavior |
|---|---|
| Quality gate | CI, security checks, unit tests, and browser tests must pass |
| Safety inspection | The active runtime is checked for positions and resting orders |
| Standby | New code starts on the unused port and must pass health checks |
| Cutover | Nginx is validated and switched to the healthy port |
| Cleanup | Old service stops; systemd boot ownership follows the active port |
| Verification | Revision, active service, and health are confirmed |

The deployment exits without restarting active trading when the book is not safe to hand over. The systemd service binds only to loopback, restarts on failure, starts memory pressure at 280 MB, and enforces a 340 MB hard limit.

## 1.9 Security and architecture invariants

1. CryptoForge and PhilForge must remain separate web origins.
2. Exactly one CryptoForge process may own broker writes for a campaign.
3. The browser never receives exchange secrets or signing private keys.
4. Buyer account state never crosses the signed signal feed.
5. A stale or unavailable feed may block a live entry; it must not silently invent a price.
6. Switching broker while orders or holdings are active must be refused.
7. A health response proves process availability, not broker reconciliation by itself.
8. A deployment must not restart live trading only to publish a cosmetic change.
9. Historical and live results must state their data source and any gaps.
10. Horizontal scaling is unsafe until durable job ownership, distributed fencing, shared state, and idempotent order commands exist.

## 1.10 Evidence map

| Subject | Source area |
|---|---|
| Application gateway and auth | `app.py` |
| Broker contract and adapters | `broker/` |
| Standard engines | `engine/live.py`, `engine/paper_trading.py`, `engine/scalp.py` |
| Cascade engine and single writer | `engine/cascade.py` |
| Buyer feed | `engine/cascade_feed.py`, feed and billing routes in `app.py` |
| Buyer executor | `executor/`, `CASCADE_SIGNAL_FORMAT.md` |
| Persistence | `state_store.py` |
| Front end | `strategy.html`, `login.html`, `static/` |
| Deployment | `deploy/`, `.github/workflows/` |

---

**CryptoForge Chapter 1 is complete.**

---

# Chapter 2: Full Site Map and Routing Graph

## 2.1 Route hierarchy

CryptoForge is a single-page terminal served by FastAPI. Most named product pages are client-side workspaces inside `/app`; they do not cause a full browser navigation. REST and WebSocket routes provide the data and commands behind those workspaces.

- Public edge → `/` → sign-in page when no valid session exists; authenticated users continue to `/app`.
- Terminal → `/app` → Journal, Portfolio, Strategies, Dashboard, Scalp, Live, Builder, Market, and Results workspaces.
- Strategies workspace → Cascade Campaigns and Rule 30/70 sub-workspaces.
- Terminal overlays → Appearance, Broker and API Console, strategy deployment, strategy detail, run detail, Cascade chart, and Cascade round ledger.
- Service plane → `/api/*` authenticated JSON routes and `/ws` browser events.
- Buyer plane → `/ws/cascade-feed`, signed key-set discovery, subscriber administration, entitlement, and billing webhook routes.
- Operations plane → health, readiness, state backup and restore, production-readiness audit, and emergency stop.

## 2.2 Screen-route catalog

| Route or workspace | Access | Primary page |
|---|---|---|
| `/` | Public shell; terminal sign-in is required | Unlock and session creation |
| `/app` → Journal | Authenticated | Trade ledger, equity curve, ROI, performance by coin, and converted trades |
| `/app` → Portfolio | Authenticated | Wallet, holdings, positions, orders, analytics, and monthly/YTD history |
| `/app` → Strategies → Cascade | Authenticated operator | Capital groups, campaign start, active and closed campaigns, charts, rounds, events, and buyer feed controls |
| `/app` → Strategies → Rule 30/70 | Authenticated operator | Instrument selection, paper service, armed orders, open ladders, activity, and journal |
| `/app` → Dashboard | Authenticated | Broker state, engine state, recent runs, and operator context |
| `/app` → Scalp | Authenticated operator | Fast entry/exit, active positions, targets, stops, reconciliation, events, and history |
| `/app` → Live | Authenticated operator | Standard paper and live engine summaries |
| `/app` → Builder | Authenticated | Strategy contract, indicators, conditions, execution assumptions, save, test, and deploy |
| `/app` → Market | Authenticated | Ranked crypto market board and refreshed market context |
| `/app` → Results | Authenticated | Run archive, filtering, comparison, export, inspection, and deletion |
| Appearance overlay | Authenticated | Colour and font preferences stored in the browser |
| Broker and API Console | Authenticated admin/operator | Adapter selection, credential settings, connection checks, and runtime health |

## 2.3 Service-route catalog

Every FastAPI route in the source baseline belongs to one of the groups below.

| Route family | Methods and paths | Responsibility |
|---|---|---|
| Identity | `POST /api/auth/login`; `GET /api/auth/status`; `POST /api/auth/logout` | Create, inspect, and end a server-side session |
| Process health | `GET /health`; `GET /api/health`; `GET /api/ready`; `GET /api/audit/production-readiness` | Separate liveness, readiness, and deeper production checks |
| Operations | `GET /api/ops/state/summary`; `GET /api/ops/state/backup`; `POST /api/ops/state/restore`; `POST /api/emergency-stop` | Inspect durable state, create or restore a snapshot, and stop trading engines |
| Dashboard and admin | `GET /api/dashboard/summary`; `GET /api/admin/config`; `PUT /api/admin/config`; `GET /api/admin/health` | Supply the control-room view and controlled configuration |
| Broker setup | `GET /api/broker/settings`; `PUT /api/broker/settings`; `POST /api/broker/check`; `POST /api/broker/connect` | Select, configure, test, and connect an exchange adapter |
| Product and market reference | `GET /api/products`; `GET /api/leverage/{symbol}`; `GET /api/cryptos`; `GET /api/market/top25`; `GET /api/ticker`; `GET /api/ticker/{symbol}`; `GET /api/funding/{symbol}` | Normalize instruments, leverage, rankings, price, and funding context |
| Standard simulation and engines | `POST /api/backtest`; `POST /api/paper/start`; `POST /api/paper/stop`; `GET /api/paper/status`; `POST /api/live/start`; `POST /api/live/stop`; `GET /api/live/status` | Run historical, paper, and direct-live strategy contracts |
| Direct broker view | `POST /api/orders/place`; `GET /api/orders`; `GET /api/positions`; `GET /api/wallet`; `GET /api/broker/trades` | Place an explicit order and read the active account state |
| Portfolio and engines | `GET /api/portfolio/summary`; `GET /api/portfolio/history`; `GET /api/engines/all` | Reconcile and aggregate current and historical account state |
| Strategy library | `GET /api/strategies`; `POST /api/strategies`; `PUT /api/strategies/{sid}`; `DELETE /api/strategies/{sid}`; `GET /api/strategies/{sid}/versions`; `POST /api/validate-strategy` | Validate, version, save, update, and remove strategy contracts |
| Run archive | `GET /api/runs`; `GET /api/runs/{rid}`; `DELETE /api/runs/{rid}`; `GET /api/runs/{rid}/csv` | Store and retrieve test, paper, live, and scalp results |
| Candle cache and exports | `GET /api/cache/status`; `DELETE /api/cache`; `GET /api/paper/trades/csv`; `GET /api/live/trades/csv` | Inspect cached history and export engine ledgers |
| Scalp | `GET /api/scalp/status`; `GET /api/scalp/diagnostics`; `GET /api/scalp/trades`; `GET /api/scalp/activity`; `POST /api/scalp/enter`; `POST /api/scalp/exit`; `PUT /api/scalp/trades/{trade_id}/targets`; `POST /api/scalp/trades/{trade_id}/add`; `POST /api/scalp/reconcile` | Manage fast positions while keeping broker and local ledgers aligned |
| Journal and notifications | `GET /api/journal/trades`; `GET /api/notifications`; `POST /api/notifications/ack` | Present durable trading evidence and acknowledgement state |
| Browser stream | `WS /ws` | Push engine, trade, notification, and status events to the operator terminal |
| Buyer feed administration | `GET /api/cascade/feed/subscribers`; `POST /api/cascade/feed/subscribers`; `DELETE /api/cascade/feed/subscribers/{buyer_id}`; `POST /api/cascade/feed/subscribers/{buyer_id}/status`; `GET /api/cascade/feed/published-symbols`; `POST /api/cascade/feed/published-symbols`; `GET /api/cascade/feed/keys` | Control entitlement, publication scope, and feed trust keys |
| Buyer feed delivery | `WS /ws/cascade-feed`; `POST /api/billing/razorpay/webhook` | Deliver signed events and update entitlement from verified billing events |
| Cascade campaign | `GET /api/cascade/status`; `POST /api/cascade/campaigns`; `POST /api/cascade/capital-groups`; `POST /api/cascade/campaigns/{campaign_id}/stop`; `POST /api/cascade/reconcile-ended`; `POST /api/cascade/campaigns/{campaign_id}/liquidate`; `POST /api/cascade/campaigns/{campaign_id}/mode`; `POST /api/cascade/campaigns/{campaign_id}/mc-kind`; `POST /api/cascade/campaigns/{campaign_id}/recalculate`; `GET /api/cascade/campaigns/{campaign_id}/chart`; `POST /api/cascade/campaigns/{campaign_id}/restructure`; `GET /api/cascade/campaigns/{campaign_id}/events`; `DELETE /api/cascade/campaigns/{campaign_id}`; `DELETE /api/cascade/closed/{campaign_id}`; `POST /api/cascade/reconcile` | Own the complete campaign lifecycle, structure, ledger, and recovery actions |
| Rule 30/70 | `GET /api/rule3070/status`; `GET /api/rule3070/journal`; `GET /api/rule3070/chart`; `POST /api/rule3070/select`; `POST /api/rule3070/start`; `POST /api/rule3070/stop`; `POST /api/rule3070/reset` | Run the isolated paper rule per selected instrument |

## 2.4 Universal layout and state synchronization

The global terminal contains the brand/header bar, emergency stop, broker status, quick-asset selector, navigation bar, page container, notifications, and modal layer. `strategy.html` provides the semantic structure. `static/cryptoforge-app.js` owns page switching, polling, forms, charts, and API calls. Shared CSS defines the Cascade-style table and panel language.

State arrives by two paths:

1. REST polling loads a complete current snapshot for pages that can be opened after a long absence.
2. `/ws` pushes events that make active pages react faster.
3. A page remains correct if one WebSocket event is missed because the next snapshot replaces stale display state.
4. The UI does not own engine truth. Closing a tab does not stop a server engine.
5. The client keeps only presentation preferences and temporary form state locally; orders, campaigns, fills, and run records remain server or broker owned.

---

# Chapter 3: Page-by-Page Deep Dive — CryptoForge

## 3.1 Unlock page — `/`

### Intent and wire

Create an authenticated operator session without exposing terminal data to an anonymous browser.

### Layout components

PhilForge-family logo, credential form, validation message, appearance control, and PWA metadata.

### Wiring and data patterns

The form posts to `POST /api/auth/login`. A successful server-side session allows `/app`; status and logout use the matching auth routes. Lockout and rate-limit state stay on the server.

### Interactive workflow

1. Enter the configured operator identity and secret.
2. Submit once and wait for the normalized result.
3. On success, continue to the terminal.
4. On failure, show a safe message without echoing credentials or internal exceptions.

## 3.2 Journal workspace

### Intent and wire

Make closed trading activity explainable across normal fills and off-order-book conversions.

### Layout components

Equity curve, ROI-by-trade chart, performance-by-coin chart, closed-trade table, conversion table, filters, and refresh control.

### Wiring and data patterns

`GET /api/journal/trades` combines the durable trade journal with normalized exchange records. Chart series are derived in the browser from the returned ledger; the source record remains authoritative.

### Interactive workflow

1. Refresh the journal.
2. Review aggregate curves before individual trades.
3. Filter or inspect rows by symbol and result.
4. Compare exchange conversion exits separately from order-book exits.

## 3.3 Portfolio workspace

### Intent and wire

Show cash, holdings, positions, orders, paper history, live history, and P&L without mixing broker truth with simulated books.

### Layout components

Balance and daily P&L cards, position and order tables, accounting/reconciliation panels, monthly P&L, year-to-date performance, and paper-run ledger.

### Wiring and data patterns

Broker balances, positions, orders, and trades come from adapter routes. `/api/portfolio/summary`, `/api/portfolio/history`, and `/api/engines/all` add normalized history and active-engine context. Freshness labels show when a value was last proved.

### Interactive workflow

1. Connect or verify the selected broker.
2. Refresh the portfolio snapshot.
3. Compare open positions with active engines.
4. Inspect accounting and reconciliation explanations.
5. Sort or export orders and review monthly/YTD totals.

## 3.4 Strategies hub — Cascade

### Intent and wire

Operate mother-candle Cascade campaigns and publish only approved public geometry to entitled buyers.

### Layout components

Paper/live mode choice, capital groups, campaign form, open and closed trade tables, active and closed campaign cards, Canvas/Classic chart, round ledger, event log, published-symbol catalog, and buyer catalog.

### Wiring and data patterns

Campaign routes call the single active Cascade engine. Browser polling refreshes `/api/cascade/status`; chart and event routes load detailed evidence only when opened. Buyer administration writes subscriber and symbol catalogs. `/ws/cascade-feed` is separate from the operator `/ws` channel.

### Interactive workflow

1. Select a capital group, symbol, mother type, and mode.
2. Review calculated geometry and capital allocation.
3. Start the campaign only after broker and mode checks pass.
4. Open the chart, closed rounds, or event log without pausing the engine.
5. Stop to preserve the book, liquidate to close it, restructure only through the explicit route, or reconcile against the broker.
6. Manage which symbols buyers may follow and which buyer identities are active.

## 3.5 Strategies hub — Rule 30/70

### Intent and wire

Run the locked V-Rule as an isolated paper service and collect a decision-quality journal before any live promotion.

### Layout components

Instrument selector, start/stop/reset controls, watch state, open paper ladders, armed entries, engine activity, warm-up ladders, journal, and chart.

### Wiring and data patterns

The route family talks to `engine/rule3070_paper.py`. State is separated by instrument and persists independently of Cascade. The service evaluates confirmed five-minute bars on its own cadence.

### Interactive workflow

1. Select one supported instrument.
2. Start its paper clock.
3. Watch mother, V, armed-entry, fill, and target states.
4. Stop without deleting the journal, or reset only that instrument after review.

## 3.6 Dashboard

### Intent and wire

Provide a first-response control room for broker connectivity, engines, recent runs, and system posture.

### Layout components

Mission/status card, broker card, active-engine panel, recent-runs table, quick actions, operator context, microstructure drill, and execution note.

### Wiring and data patterns

`/api/dashboard/summary`, `/api/engines/all`, auth status, broker checks, notifications, and recent runs feed the page. It is a summary; detailed reconciliation stays on the relevant engine or portfolio page.

### Interactive workflow

1. Confirm broker, feed, and engine status.
2. Open the relevant detailed workspace from a quick action.
3. Review recent run outcomes.
4. Use operator context content between decisions, not as a trade signal.

## 3.7 Scalp workspace

### Intent and wire

Give the operator a fast but auditable entry-and-exit surface for supported crypto products.

### Layout components

Velocity Entry form, active-position table, editable targets and stops, add-to-position control, diagnostics, event log, trade history, reconcile, and emergency stop.

### Wiring and data patterns

Ticker data drives the in-process monitor. Enter and exit routes call the active broker adapter. The reconciliation route compares broker state with the local scalp ledger before further automation proceeds.

### Interactive workflow

1. Verify symbol, side, size, target, stop, and broker connection.
2. Submit an entry once.
3. Monitor acknowledgement and position identity.
4. Adjust targets or add only through the position-specific routes.
5. Exit, reconcile, or use emergency stop when the broker and local view disagree.

## 3.8 Live workspace

### Intent and wire

Show the standard paper and live strategy engines without confusing them with specialist Cascade or Scalp services.

### Layout components

Paper summary, live summary, run identity, strategy, symbol, mode, status, position, P&L, and stop control.

### Wiring and data patterns

Paper and live status routes provide snapshots; `/ws` supplies events. Stop routes act on the named engine. A browser page change does not stop it.

### Interactive workflow

1. Open the page after deploying a saved strategy.
2. Confirm mode and run identity.
3. Observe signal, position, and P&L state.
4. Stop the correct paper or live engine explicitly.

## 3.9 Strategy Builder

### Intent and wire

Turn plain trading rules into a validated, versioned strategy contract that can be tested before deployment.

### Layout components

Name, folder, symbol, interval, side, capital, leverage, position size, execution-cost assumptions, stop/target/trailing controls, indicator list, entry conditions, exit conditions, save, backtest, and deploy actions.

### Wiring and data patterns

`POST /api/validate-strategy` checks the contract. Strategy CRUD routes store versions. `POST /api/backtest` runs simulation. Deployment starts paper or live through the corresponding engine route.

### Interactive workflow

1. Define the instrument and risk envelope.
2. Add indicators and forward-only entry/exit conditions.
3. Set fees, spread, slippage, funding, and leverage assumptions.
4. Validate and save a version.
5. Backtest and inspect warnings.
6. Deploy to paper first; choose live only after independent review.

## 3.10 Market workspace

### Intent and wire

Provide ranked context for widely traded crypto assets without turning the market board into an order ticket.

### Layout components

Top-25 table, price, 24-hour change, volume, market capitalization, all-time-high context, sorting, and refresh.

### Wiring and data patterns

`/api/market/top25` and `/api/cryptos` normalize public market context. Ticker routes provide selected broker prices. Cached responses reduce external rate pressure.

### Interactive workflow

1. Refresh the board.
2. Sort by the required market field.
3. Use the selected symbol as context for Builder or Scalp.
4. Verify the broker price before any order action.

## 3.11 Results workspace

### Intent and wire

Keep every saved backtest, paper, live, and scalp result inspectable and exportable.

### Layout components

Mode filters, run table, side-by-side comparison, run detail, trade table, charts, CSV export, bulk selection, and delete controls.

### Wiring and data patterns

Run routes read the durable archive. Comparison is a client-side view of selected normalized runs. CSV is generated from the stored run, not from what happens to be visible on screen.

### Interactive workflow

1. Filter the archive by mode.
2. Open one run and inspect its assumptions before its headline P&L.
3. Select comparable runs for side-by-side review.
4. Export evidence or delete only the intended run records.

## 3.12 Operator overlays and administration

### Intent and wire

Keep infrequent but powerful controls outside daily workspaces.

### Layout components

Appearance panel, Broker and API Console, deployment confirmation, strategy detail, run detail, and campaign dialogs.

### Wiring and data patterns

Appearance is browser-local. Broker settings and admin configuration are server-side and protected. Sensitive values are masked on read. Campaign and run dialogs use identity-specific routes rather than broad page state.

### Interactive workflow

1. Open the required overlay from a visible control.
2. Review the target and current mode.
3. Submit one scoped change.
4. Close the overlay and confirm the updated authoritative page state.

---

# Chapter 5: User Experience Fork — Buyer Console and Professional Terminal

## 5.1 Buyer's experience — local Cascade console

The buyer product is a desktop executor, not the operator terminal.

1. Install the signed buyer application on the buyer's computer.
2. Create a local identity and register only its public key with CryptoForge.
3. Store Binance or CoinDCX credentials locally in the operating-system secret store.
4. Select published symbols and set the buyer's own capital, fees, and minimum-order rules.
5. Connect to the signed Cascade feed and verify publisher key, entitlement, and sequence.
6. Preview the received geometry before enabling local execution.
7. Let the executor place orders directly in the buyer's exchange account.
8. Review the local order, fill, balance, and P&L ledger. CryptoForge cannot see this private ledger.
9. If entitlement ends, block new campaigns while continuing to manage already-held positions safely.

## 5.2 Professional's experience — operator terminal

| Professional need | Current implementation | Operating guidance |
|---|---|---|
| Multi-monitor use | The web terminal can be opened in separate authenticated windows | Keep Dashboard/Portfolio on one display and the active specialist engine on another; avoid issuing the same command twice |
| Advanced charting | Canvas and SVG charts show native candles, trendlines, fibs, fills, targets, zoom, and full-screen views | Treat chart pixels as a view of engine data, not a second source of price truth |
| Hotkeys | No global trade-entry or order-cancel hotkey contract is defined | Use explicit buttons and confirmation surfaces; do not assume broker-terminal shortcuts work here |
| Order routing override | Broker selection exists in the protected console; active-state guards restrict unsafe switching | Change adapters only when positions, orders, and engines are flat and reconciled |
| Ledger audit | Journal, Portfolio, Results, Cascade rounds, and broker records expose different layers | Reconcile by run/campaign, exchange order ID, fill, fee, and timestamp |
| Emergency handling | Visible emergency stop plus engine-specific stop, exit, liquidate, and reconcile actions | Choose the narrowest safe action; emergency stop is not a substitute for broker verification |

## 5.3 Basic operator trade workflow

1. Check Dashboard health, broker connection, and active engines.
2. Check Portfolio balance and open exposure.
3. Build or select a strategy and verify risk settings.
4. Backtest with realistic costs and inspect drawdown and trade evidence.
5. Run paper mode and compare its ledger with current market behavior.
6. If live use is authorized, start one named live run.
7. Watch Live, Portfolio, and Journal together.
8. Stop the engine and verify final broker fills and ledger state.

---

# Chapter 6: Integrations, Strategies, and Backtesting Engine

## 6.1 Broker integration layer

| Integration | Role | Credential boundary | Important behavior |
|---|---|---|---|
| Binance Spot | Default direct broker and buyer-executor choice | Server operator key for direct mode; buyer key stays local for buyer mode | HMAC-authenticated orders, balances, symbols, fills, and testnet support |
| CoinDCX Spot | Alternate direct broker and buyer-executor choice | Same server/local split as Binance | Adapter normalizes CoinDCX symbols, orders, fills, and account responses |
| CoinDCX Futures | Alternate derivatives path | Server operator credential | Contract and leverage meaning must be validated before use |
| Delta Exchange | Perpetual-futures adapter and historical/stream source | Server operator credential | Authenticated REST, WebSocket support, leverage, funding, and liquidation-aware simulation |
| CoinGecko and FX sources | Market context and INR display conversion | Public or service credential as configured | Never acts as execution truth |
| Razorpay webhook | Buyer subscription entitlement | Server webhook secret/signature | A verified event may update entitlement; it cannot place a trade |

The adapter contract normalizes products, ticker, candles, leverage, balance, orders, positions, and fills. Latency is measured as an operational observation, not promised by the architecture. Every live command must tolerate rejection, timeout, duplicate response, partial fill, and a later broker truth that differs from the first response.

## 6.2 Algorithmic strategy catalog

| Strategy family | Plain-English behavior | Current boundary |
|---|---|---|
| Strategy Builder momentum | Enters when selected price and indicator conditions show strength, such as price above EMA or Supertrend; exits on the configured reversal, stop, target, or time rule | Generic backtest, paper, and live engine |
| Strategy Builder mean reversion | Enters when a user-defined condition expects price to return toward a reference such as a band, moving average, or pivot | Available only when expressed as validated Builder conditions; no separate named mean-reversion daemon |
| Multi-timeframe CPR/indicator strategy | Combines current candles, EMA, RSI, Supertrend, CPR width/levels, day filters, and Boolean conditions | Versioned Builder contract |
| Scalp | Opens an operator-directed fast position and monitors target, stop, and manual exit against a fresh ticker | Direct broker path with reconciliation |
| Cascade | Anchors a mother high, builds fib/trendline structure, distributes capital over rungs, recalculates targets, and closes rounds or campaigns under explicit rules | Operator paper/live modes plus public geometry feed |
| Rule 30/70 V-Rule | Detects a mother and V structure, alternates 30% and 70% paper allocations within its exposure cap, and targets a recovery level above average entry | Locked paper service |
| Grid trading | Repeated fixed-price grid behavior is not a dedicated engine in this baseline | Do not label Cascade as a generic grid; its rungs come from strategy geometry |
| Arbitrage | Cross-exchange spread capture is not implemented as an execution engine | Market adapters do not by themselves create an arbitrage strategy |

## 6.3 Historical backtest journey

1. Accept a validated strategy, instrument, interval, date range, capital, leverage, and execution assumptions.
2. Resolve the selected broker's product and historical-candle contract.
3. Read the local cache first.
4. Fetch only missing spans and normalize timestamp, open, high, low, close, and volume.
5. Calculate indicators without using future candles.
6. Evaluate entries and exits in chronological order.
7. Apply adverse spread and slippage to entry and exit references.
8. Apply per-side fees, funding assumptions, leverage, margin, and liquidation rules.
9. Record every trade, fee, reason, and equity update.
10. Down-sample only the displayed equity curve; keep trade records intact.
11. Calculate metrics, warnings, and execution-cost totals.
12. Save the run so Results can reproduce its assumptions and evidence.

## 6.4 Validation metrics produced

| Metric | Meaning |
|---|---|
| Total trades, wins, and losses | Sample size and outcome counts |
| Win rate and win/loss ratio | Frequency of profitable outcomes, not their size |
| Average win and average loss | Typical positive and negative trade value |
| Profit factor | Gross profit divided by absolute gross loss |
| Expectancy | Average statistical value of one trade under the tested sample |
| Net and gross P&L | Result before and after modeled costs |
| Total return | Net result relative to starting capital |
| Maximum drawdown | Largest peak-to-trough equity decline in money and percentage terms |
| Sharpe ratio | Return consistency using a 365-day crypto convention |
| Calmar ratio | Annualized return compared with maximum drawdown |
| Fees, funding, spread, and slippage totals | Cost of making the simulation more realistic |
| Equity curve and trade ledger | Time-ordered evidence behind summary metrics |

## 6.5 Backtest interpretation rules

1. A backtest proves how the simulator treated available candles; it does not prove a future fill.
2. Zero fees or zero slippage must be shown as an assumption, not hidden.
3. A high Sharpe ratio with few trades is weak evidence.
4. Liquidation and funding assumptions must match the selected derivatives venue.
5. Paper trading is the next validation stage because it introduces live timing and feed behavior without broker orders.

---

# Chapter 7: Financials, Operational Costs, and Risk Engine

## 7.1 Cost model and ownership

The values below are planning references as of the snapshot date, not invoices. Exchange trading fees depend on venue, product, account tier, maker/taker status, and tax treatment. The operator must compare the configured model with current account statements.

| Cost item | Current architecture basis | Planning treatment |
|---|---|---|
| Shared Linux host | AWS Lightsail; repository runbook identifies the Mumbai host | Official public-IPv4 Linux bundles list 1 GB at $7/month, 2 GB at $12/month, and 4 GB at $24/month. Use the size actually provisioned. [AWS Lightsail pricing](https://aws.amazon.com/lightsail/pricing/) |
| Snapshot storage | Lightsail instance/disk snapshots | Official rate is $0.05 per GB-month; retention multiplies storage use. [AWS Lightsail pricing](https://aws.amazon.com/lightsail/pricing/) |
| TLS and reverse proxy | Nginx and automated certificate tooling on the host | No separate application licence; domain registration and optional DNS/CDN are external costs |
| Crypto market API | Exchange REST/WebSocket and public market-context providers | No separate Level-1/Level-2 feed invoice is encoded in this repository; confirm exchange API and commercial-use terms |
| Direct trading | Binance, CoinDCX, or Delta account | Variable maker/taker, funding, spread, slippage, conversion, withdrawal, and tax costs |
| Backtest execution | Same host CPU/RAM and local cache | Main scaling driver is candle span × symbols × strategy variants; move heavy jobs off the request loop before adding users |
| Buyer executor | Buyer's computer and internet | Buyer-owned; CryptoForge carries feed publication, entitlement, and retained-event storage |
| Alerts and billing | Telegram/Discord transport and Razorpay | Provider transaction or plan charges are external and must be reconciled with billing reports |

## 7.2 Scaling-cost stages

| Stage | Trigger | Added cost and architecture change |
|---|---|---|
| Current single host | One operator, limited concurrent backtests, controlled buyer feed | One Lightsail bundle plus snapshots and normal provider fees |
| Larger host | Memory pressure, long backtests, or frequent blue-green overlap | Move from 1-2 GB to 4 GB or above; preserve one active engine owner |
| Split compute | Backtests delay API/engine work | Add a separate worker host and durable job queue; do not duplicate live engine ownership |
| Durable shared state | Multiple application nodes become necessary | Managed database, distributed lock/fencing, idempotency store, and centralized logs |
| Buyer-feed scale | Subscriber count or replay storage outgrows one process | Dedicated feed service, durable sequence log, connection monitoring, and bandwidth budget |

## 7.3 Risk matrix

| Risk | Signal | Automated rule | Operator response |
|---|---|---|---|
| Exchange API disconnect | Timeout, authentication error, reconnect loop, or stale timestamp | Block new live entries; retry with bounded backoff; keep current position state for reconciliation | Check exchange status and credentials; compare broker account before resume |
| Stale ticker or candle | Freshness age exceeds engine limit | Refuse price-dependent entry and never invent a fill | Restore feed or use the documented broker fallback |
| Partial or uncertain fill | Accepted order lacks complete fill evidence | Mark pending/partial, avoid duplicate order, query orders and fills | Reconcile by exchange order ID before further action |
| Duplicate engine owner | Writer lock unavailable or overlapping active worker | Observer mode only; broker writes are fenced | Find and stop the stale owner without interrupting the valid one |
| Excess leverage or liquidation | Margin threshold or simulated liquidation rule is reached | Refuse invalid size; apply stop/liquidation handling; alert | Reduce leverage and confirm venue contract rules |
| Slippage and fee drift | Broker ledger differs materially from modeled costs | Surface variance and retain real fills/fees | Update assumptions only after account-statement review |
| Cascade geometry error | Missing mother/native OHLC or invalid rung ordering | Refuse campaign start or recalculate from authoritative candles | Inspect chart evidence and event log |
| Buyer feed gap or bad signature | Sequence gap, unknown key, expired entitlement, or signature failure | Reject event; request replay/key set; block new buyer entries | Verify publisher health and root-authorized key set |
| Platform downtime | Health/readiness failure or systemd restart | systemd restarts service; restore only safe snapshots | Reconcile broker state before enabling automation |
| Unsafe deployment | Active positions/resting orders or failed standby health | Exit deployment without restarting active service | Deploy after flat/reconciled state or use an approved handover plan |
| State corruption | SQLite integrity/backup verification failure | Block restore and keep last verified backup | Recover into an isolated copy and audit journal continuity |
| Credential exposure | Secret appears in client/log/test output | Redact, refuse response, rotate affected key | Revoke immediately and review access logs |
| Market cascade/liquidity shock | Spread, volatility, rejection, or depth moves beyond limits | Stop new entries, retain explicit exit authority, alert | Prefer capital protection and broker-confirmed exits over target assumptions |

## 7.4 Risk-engine decision order

**Identity → mode → engine ownership → broker readiness → data freshness → instrument rules → capital and leverage → current exposure → order idempotency → submit → verify → persist → notify**

No user-interface shortcut may bypass this order. A green screen is not proof of a safe account; broker orders, fills, balances, and the durable ledger must agree.

## 7.5 Blueprint completion and evidence

| Chapter | Coverage | Primary evidence |
|---|---|---|
| 1 | Platform architecture and control flow | `app.py`, `state_store.py`, `engine/`, `broker/`, `deploy/` |
| 2 | Every screen and service route | FastAPI decorators, `strategy.html`, `login.html` |
| 3 | Every user-facing workspace | HTML structure and `static/cryptoforge-app.js` wiring |
| 5 | Buyer and operator experience | `executor/`, terminal UI, feed routes, and runbooks |
| 6 | Integrations, strategies, and backtesting | `broker/`, `engine/backtest.py`, specialist engines, and strategy contracts |
| 7 | Cost and risk controls | official provider references, deployment gates, reconciliation, recovery, and tests |

**The CryptoForge blueprint is complete across Chapters 1, 2, 3, 5, 6, and 7 for the 14 August 2026 repository snapshot. Chapter 4 belongs to the separate PhilForge blueprint. Runtime configuration, broker records, and later code changes remain authoritative.**
