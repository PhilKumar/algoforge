<div align="center">

# ⚡ AlgoForge &amp; CryptoForge
### High-Frequency Algorithmic Trading Suite

*Multi-asset, multi-mode trading platform — Equities &amp; Options via Dhan · Crypto Perpetuals via Delta Exchange*

---

[![Build](https://img.shields.io/badge/build-passing-brightgreen?style=for-the-badge&logo=github-actions)](https://github.com)
[![Python](https://img.shields.io/badge/Python-3.11-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.110-009688?style=for-the-badge&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![WebSockets](https://img.shields.io/badge/WebSockets-Live_Feed-FF6B6B?style=for-the-badge&logo=socket.io&logoColor=white)](https://websockets.readthedocs.io)
[![Nginx](https://img.shields.io/badge/Nginx-Reverse_Proxy-009639?style=for-the-badge&logo=nginx&logoColor=white)](https://nginx.org)
[![License](https://img.shields.io/badge/License-Private-red?style=for-the-badge)](LICENSE)

</div>

---

## 📖 Table of Contents

1. [Overview](#-overview)
2. [System Architecture](#-system-architecture)
3. [Core Features](#-core-features)
4. [Project Structure](#-project-structure)
5. [Setup Guide](#-setup-guide)
6. [Environment Variables](#-environment-variables)
7. [API Reference](#-api-reference)
8. [Indicators Library](#-indicators-library)
9. [Production Deployment](#-production-deployment)
10. [Monitoring &amp; Alerts](#-monitoring--alerts)

---

## 🎯 Overview

This suite contains **two production-grade trading platforms** that share a common engine architecture:

| Platform | Market | Broker | Port |
|----------|--------|--------|------|
| **AlgoForge** | NSE Equities &amp; Options (F&amp;O) | [Dhan](https://dhanhq.co) REST + WebSocket | `8000` |
| **CryptoForge** | Crypto Perpetual Futures | [Delta Exchange India](https://www.delta.exchange) REST + WebSocket | `9000` |

Both platforms provide a unified workflow: **Strategy Builder → Backtest → Paper Trade → Go Live → Scalp**.

---

## 🏗 System Architecture

```mermaid
graph TD
    subgraph CLIENT["🌐 Client Layer"]
        UI["Browser SPA<br/>strategy.html + login.html<br/>(Vanilla HTML · CSS · JS)"]
    end

    subgraph PROXY["⚡ Reverse Proxy"]
        NGINX["Nginx<br/>Port 80 → AlgoForge :8000<br/>Port 9090 → CryptoForge :9000<br/>TCP tuning · HTTP/1.1 keepalive"]
    end

    subgraph ALGOFORGE["📈 AlgoForge — Equities & Options"]
        AF_APP["FastAPI + Uvicorn<br/>app.py :8000"]
        AF_BT["Backtest Engine<br/>Walk-forward · No look-ahead bias"]
        AF_LIVE["Live Engine<br/>Dhan REST orders"]
        AF_PAPER["Paper Engine<br/>Simulated fills"]
        AF_SCALP["Scalp Engine<br/>scalp.py — Options scalping<br/>Hybrid manual/auto"]
        AF_FEED["Market Feed<br/>Dhan WebSocket → CandleAggregator<br/>1m / 3m / 5m OHLCV"]
        AF_IND["Indicator Engine<br/>SuperTrend · EMA · RSI · CPR · VWAP"]
        AF_TOK["Token Manager<br/>TOTP auto-token<br/>JWT expiry tracking"]
        AF_WS["WebSocket Hub /ws<br/>Real-time P&L stream"]
        AF_ALERT["Alerter<br/>Telegram · Discord<br/>fire-and-forget async"]
    end

    subgraph CRYPTOFORGE["🪙 CryptoForge — Crypto Perpetuals"]
        CF_APP["FastAPI + Uvicorn<br/>app.py :9000"]
        CF_BT["Backtest Engine<br/>Leverage · Fees · Liquidation sim"]
        CF_LIVE["Live Engine<br/>Delta Exchange orders"]
        CF_PAPER["Paper Engine<br/>Simulated fills"]
        CF_SCALP["Scalp Engine<br/>engine/scalp.py"]
        CF_DL["Data Downloader<br/>engine/data_downloader.py<br/>Async paginator · Rate limiter"]
        CF_WS["WebSocket Hub /ws<br/>Real-time trade stream"]
        CF_ALERT["Alerter<br/>Telegram · Discord"]
    end

    subgraph DATA["💾 Data Layer"]
        PG["PostgreSQL / TimescaleDB<br/>candles hypertable<br/>1m · 3m · 5m OHLCV<br/>(optional — CryptoForge)"]
        JSON_FILES["JSON Persistence<br/>strategies.json<br/>runs.json · scalp_trades.json"]
        PROM["Prometheus Metrics<br/>/metrics endpoint<br/>(optional)"]
    end

    subgraph BROKERS["🔌 External APIs"]
        DHAN_REST["Dhan REST API<br/>api.dhan.co"]
        DHAN_WS["Dhan WebSocket<br/>api-feed.dhan.co"]
        DELTA_REST["Delta Exchange India<br/>api.india.delta.exchange"]
        DELTA_WS["Delta WebSocket<br/>socket.delta.exchange"]
        BINANCE["Binance Public API<br/>Candle fallback"]
        YF["Yahoo Finance<br/>Backtest data fallback"]
        TG["Telegram Bot API"]
        DC["Discord Webhook"]
        CG["CoinGecko API<br/>Market overview"]
    end

    UI -->|"HTTPS / WSS"| NGINX
    NGINX -->|"proxy_pass"| AF_APP
    NGINX -->|"proxy_pass"| CF_APP

    AF_APP --> AF_BT & AF_LIVE & AF_PAPER & AF_SCALP
    AF_APP --> AF_WS
    AF_BT & AF_LIVE & AF_PAPER --> AF_IND
    AF_LIVE --> DHAN_REST
    AF_FEED --> DHAN_WS
    AF_BT --> YF
    AF_TOK --> DHAN_REST
    AF_ALERT --> TG & DC
    AF_APP --> JSON_FILES
    AF_APP --> PROM

    CF_APP --> CF_BT & CF_LIVE & CF_PAPER & CF_SCALP
    CF_APP --> CF_WS
    CF_LIVE --> DELTA_REST
    CF_DL --> DELTA_REST & BINANCE
    CF_DL --> PG
    CF_APP --> CG
    CF_ALERT --> TG & DC
    CF_APP --> JSON_FILES
```

### Request Lifecycle

```
Browser → Nginx (TCP-tuned) → Uvicorn (ASGI) → FastAPI Router
    → Engine (async task) → Broker API → WebSocket broadcast → Browser
```

---

## ✨ Core Features

### Shared Capabilities (Both Platforms)

| Feature | Details |
|---------|---------|
| **Strategy Builder** | Visual condition editor — combine any indicator with comparison operators (`crosses_above`, `is_below`, `is`, etc.) |
| **Backtesting Engine** | Walk-forward event loop, look-ahead-bias-free, full fee accounting, monthly P&amp;L heatmap |
| **Paper Trading** | Real-time simulated fills using live market data — zero risk, full realism |
| **Live Trading** | Authenticated broker REST API orders with fill verification + retry logic |
| **Scalp Mode** | Hybrid manual/auto engine — manual entry, auto exit on target/SL/sqoff time |
| **WebSocket Feed** | Sub-second P&amp;L and trade log broadcast to all connected clients |
| **Alerting** | Fire-and-forget async alerts via **Telegram** and **Discord** on orders, errors, and signals |
| **Multi-Engine** | Run multiple concurrent live/paper engines keyed by `run_id` |
| **PIN Auth** | Session-based PIN authentication — server refuses to start without `*_PIN` env var |
| **Graceful Shutdown** | `atexit` hook auto-saves all running engine states to `runs.json` |

### AlgoForge-Specific

| Feature | Details |
|---------|---------|
| **Dhan WebSocket Feed** | Tick-level LTP data → `CandleAggregator` builds OHLCV candles of any timeframe in real-time |
| **TOTP Auto-Token** | Automatically generates fresh Dhan JWT on startup using TOTP secret — no manual token renewal |
| **JWT Expiry Tracking** | Decodes JWT payload to surface expiry date, days remaining, and warning at ≤7 days |
| **Options Scalp Engine** | CE/PE options with per-trade SL/TP in rupees, percentage, or absolute premium |
| **yfinance Fallback** | Backtesting works without Dhan credentials using Yahoo Finance data |
| **Prometheus Metrics** | Optional `/metrics` endpoint via `prometheus_fastapi_instrumentator` |

### CryptoForge-Specific

| Feature | Details |
|---------|---------|
| **Delta Exchange Integration** | HMAC-SHA256 signed REST orders, position management, funding rate history |
| **Leverage Engine** | Configurable 1×–200× leverage with margin and liquidation price estimation |
| **Fee Simulation** | Maker (0.02%) / Taker (0.05%) per-side fees applied in backtests |
| **TimescaleDB Storage** | Optional hypertable storage for 5 years of 1m/3m/5m OHLCV candles with auto-compression |
| **Async Bulk Downloader** | `aiohttp` paginator with semaphore (8 concurrent), exponential backoff, gap detection |
| **Binance Fallback** | Falls back to Binance public `/api/v3/klines` when Delta has no historical data |
| **Testnet Toggle** | Single env var `DELTA_TESTNET=true` switches all API calls to testnet with identical signatures |
| **Market Overview** | CoinGecko top-25 market caps + live ticker strip + funding rate charts |

---

## 📁 Project Structure

### AlgoForge (`/New_Algo`)

```
algoforge/
├── app.py                    # FastAPI backend — all routes, WebSocket hub, auth
├── config.py                 # Env vars, Dhan credentials, indicator defaults
├── scalp.py                  # Scalp engine — options scalping (hybrid manual/auto)
├── alerter.py                # Async Telegram + Discord alerting
├── token_manager.py          # TOTP auto-token generation + JWT expiry tracking
├── requirements.txt
├── .env.example              # Template — copy to .env
├── strategy.html             # Main SPA frontend
├── login.html                # Auth page
├── broker/
│   └── dhan.py               # Dhan REST client + ScripMaster
├── engine/
│   ├── backtest.py           # Walk-forward backtest engine
│   ├── indicators.py         # SuperTrend · EMA · RSI · CPR · VWAP (numpy-optimised)
│   ├── live.py               # Live trading loop — polls Dhan, checks conditions, places orders
│   ├── paper_trading.py      # Paper trading — simulated fills
│   ├── market_feed.py        # Dhan WebSocket feed + CandleAggregator
│   └── data_cache.py         # In-memory OHLCV cache
└── deploy/
    ├── algoforge.service     # systemd unit
    ├── nginx.conf            # Reverse proxy config
    └── deploy.sh             # One-command EC2 setup
```

### CryptoForge (`/CryptoForge`)

```
cryptoforge/
├── app.py                    # FastAPI backend — all routes, WebSocket hub, auth
├── config.py                 # Env vars, Delta credentials, testnet toggle
├── alerter.py                # Async Telegram + Discord alerting
├── requirements.txt
├── strategy.html             # Main SPA frontend
├── login.html                # Auth page
├── broker/
│   └── delta.py              # Delta Exchange REST client + Binance fallback
├── engine/
│   ├── backtest.py           # Backtest — leverage, fees (maker/taker), liquidation sim
│   ├── indicators.py         # Full indicator library
│   ├── live.py               # Live trading engine — real Delta orders
│   ├── paper_trading.py      # Paper trading engine
│   ├── scalp.py              # Crypto scalp engine
│   ├── data_downloader.py    # Async bulk OHLCV downloader + TimescaleDB writer
│   └── ws_feed.py            # Delta WebSocket feed with auto-reconnect
└── deploy/
    ├── cryptoforge.service   # systemd unit
    ├── nginx.conf            # Reverse proxy config
    └── deploy.sh             # One-command EC2 setup
```

---

## 🚀 Setup Guide

### Prerequisites

- Python 3.11+
- `git`
- A Dhan account (AlgoForge) and/or Delta Exchange account (CryptoForge)

### 1. Clone

```bash
# AlgoForge
git clone https://github.com/YOUR_USERNAME/algoforge.git
cd algoforge

# CryptoForge
git clone https://github.com/PhilKumar/cryptoforge.git
cd cryptoforge
```

### 2. Virtual Environment &amp; Dependencies

```bash
python3.11 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install --upgrade pip
pip install -r requirements.txt
```

### 3. Configure Environment

```bash
cp .env.example .env
nano .env                         # fill in your credentials (see next section)
```

### 4. Start the Server

```bash
# Development (auto-reload)
uvicorn app:app --host 127.0.0.1 --port 8000 --reload

# Production (multi-worker)
uvicorn app:app --host 0.0.0.0 --port 8000 --workers 2
```

Open **http://localhost:8000** and log in with your PIN.

> **Backtesting without credentials:** AlgoForge backtests automatically fall back to Yahoo Finance data. CryptoForge backtests fall back to Binance public OHLCV.

---

## 🔑 Environment Variables

### AlgoForge `.env`

```bash
# ── Dhan API ─────────────────────────────────────────
DHAN_CLIENT_ID=your_client_id_here
DHAN_ACCESS_TOKEN=your_access_token_here

# ── TOTP Auto-Token (optional — enables automatic token renewal) ──
# Enable TOTP on Dhan Web and save the secret here
DHAN_PIN=your_dhan_login_pin
DHAN_TOTP_SECRET=your_totp_base32_secret

# ── App ──────────────────────────────────────────────
APP_HOST=127.0.0.1
APP_PORT=8000
DEBUG=false
ALGOFORGE_PIN=your_secure_pin      # required — server won't start without this

# ── Alerts (optional) ────────────────────────────────
TELEGRAM_BOT_TOKEN=123456:ABC-DEF...
TELEGRAM_CHAT_ID=-100123456789
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
```

### CryptoForge `.env`

```bash
# ── Delta Exchange ────────────────────────────────────
DELTA_API_KEY=your_api_key_here
DELTA_API_SECRET=your_api_secret_here
DELTA_TESTNET=false                 # true = testnet | false = production
DELTA_REGION=india                  # 'india' or 'global'

# ── Database (optional — enables TimescaleDB candle storage) ─
DATABASE_URL=postgresql://user:pass@localhost:5432/cryptoforge
USE_TIMESCALEDB=false               # set true to persist candles to DB

# ── App ──────────────────────────────────────────────
APP_HOST=127.0.0.1
APP_PORT=9000
DEBUG=false
CRYPTOFORGE_PIN=your_secure_pin    # required — server refuses to start without this

# ── Alerts (optional) ────────────────────────────────
TELEGRAM_BOT_TOKEN=123456:ABC-DEF...
TELEGRAM_CHAT_ID=-100123456789
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
```

---

## 📡 API Reference

### Authentication

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/auth/login` | Authenticate with PIN, returns session cookie |
| `GET` | `/api/auth/status` | Check session validity |
| `POST` | `/api/auth/logout` | Invalidate session |

### Trading Engines

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/backtest` | Run a full backtest, returns trades + metrics |
| `POST` | `/api/live/start` | Start a live engine instance |
| `POST` | `/api/live/stop` | Stop a live engine instance |
| `GET` | `/api/live/status` | Engine status + real-time P&amp;L |
| `POST` | `/api/paper/start` | Start a paper trading instance |
| `POST` | `/api/paper/stop` | Stop a paper trading instance |
| `GET` | `/api/paper/status` | Paper engine status |
| `POST` | `/api/emergency-stop` | Kill all running engines immediately |

### Scalp Engine

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/scalp/start` | Activate scalp engine |
| `POST` | `/api/scalp/stop` | Deactivate scalp engine |
| `POST` | `/api/scalp/entry` | Place a manual scalp entry |
| `POST` | `/api/scalp/exit/{id}` | Manually exit a scalp position |
| `GET` | `/api/scalp/status` | Active positions + unrealised P&amp;L |
| `GET` | `/api/scalp/trades` | Closed trade history |
| `DELETE` | `/api/scalp/trades` | Clear closed trade history |
| `PUT` | `/api/scalp/trades/{id}/targets` | Update SL/TP for an open position |

### Market Data

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/symbols` | List tradable symbols |
| `GET` | `/api/candles/{symbol}` | OHLCV candle history |
| `GET` | `/api/ticker/{symbol}` | Live ticker |
| `GET` | `/api/tickers/bulk` | All tickers (30s cache) |
| `GET` | `/api/funding/{symbol}` | Funding rate history (CryptoForge) |
| `GET` | `/api/market/top25` | CoinGecko top-25 overview (CryptoForge) |

### Account

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/account/balance` | Wallet balance |
| `GET` | `/api/account/positions` | Open positions |

### Strategy Management

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/strategies` | List saved strategies |
| `POST` | `/api/strategies` | Save a strategy |
| `DELETE` | `/api/strategies/{sid}` | Delete a strategy |
| `GET` | `/api/runs` | Backtest / paper / live run history |
| `DELETE` | `/api/runs/{rid}` | Delete a run |

### System

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/health` | Health check |
| `GET` | `/api/token/status` | JWT expiry info (AlgoForge) |
| `GET` | `/metrics` | Prometheus metrics (if enabled) |
| `WS` | `/ws` | Real-time trade log + P&amp;L WebSocket stream |

---

## 📊 Indicators Library

| Indicator | Parameters | Notes |
|-----------|-----------|-------|
| **SuperTrend** | `period=10`, `multiplier=2.7` | Uses RMA (Wilder's smoothing) — no look-ahead bias |
| **EMA** | `period=17` | Exponential moving average |
| **RSI** | `period=14` | Relative Strength Index |
| **MACD** | `fast=12`, `slow=26`, `signal=9` | Moving Average Convergence Divergence |
| **Bollinger Bands** | `period=20`, `std=2.0` | Upper / middle / lower bands |
| **VWAP** | Session-based | Volume Weighted Average Price |
| **Stochastic RSI** | `period=14` | Stoch of RSI |
| **ADX** | `period=14` | Average Directional Index |
| **CPR** | Daily | Central Pivot Range — narrow / moderate / wide classification |

### Condition Operators

```
crosses_above · crosses_below · is_above · is_below · is · is_not
```

### Available Fields (Strategy Builder)

```
current_close · rsi · ema · supertrend · macd · bollinger_upper
bollinger_lower · vwap · adx · cpr_is_narrow · time_of_day
prev_candle · yesterday_high · yesterday_low · pivot · bc · tc
```

---

## 🖥 Production Deployment

### Infrastructure Overview

```
EC2 Instance (Amazon Linux 2 / Ubuntu 22.04)
├── Port 80   → Nginx → AlgoForge   :8000 (uvicorn)
├── Port 9090 → Nginx → CryptoForge :9000 (uvicorn)
├── systemd manages both services (Restart=always)
└── PostgreSQL / TimescaleDB (optional, CryptoForge candle storage)
```

### One-Command Deploy

```bash
# SSH into EC2
ssh -i ~/.ssh/your-key.pem ec2-user@YOUR_EC2_IP

# Run deployment script (installs Python 3.11, nginx, virtualenv, systemd)
bash deploy/deploy.sh YOUR_ELASTIC_IP
```

### Systemd Service

```ini
# /etc/systemd/system/algoforge.service
[Unit]
Description=AlgoForge Trading Platform
After=network.target

[Service]
User=ec2-user
WorkingDirectory=/home/ec2-user/algoforge
EnvironmentFile=/home/ec2-user/algoforge/.env
ExecStart=/home/ec2-user/algoforge/venv/bin/uvicorn app:app \
    --host 0.0.0.0 --port 8000 --workers 2
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable algoforge
sudo systemctl start algoforge
sudo journalctl -u algoforge -f    # tail logs
```

### Nginx Configuration

```nginx
server {
    listen 80;
    server_name YOUR_ELASTIC_IP;

    # WebSocket upgrade
    location /ws {
        proxy_pass         http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header   Upgrade $http_upgrade;
        proxy_set_header   Connection "upgrade";
        proxy_read_timeout 86400;
    }

    location / {
        proxy_pass         http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header   Host $host;
        proxy_set_header   X-Real-IP $remote_addr;

        # Low-latency TCP tuning
        tcp_nodelay        on;
        tcp_nopush         off;
    }
}
```

### Deployment Workflow

```
Local edit
    │
    ├─ git commit -m "feat: ..."
    ├─ git push origin main
    └─ scp -i ~/.ssh/algocrypto app.py ec2-user@IP:/home/ec2-user/algoforge/
           │
           └─ sudo systemctl restart algoforge   (only for app.py changes)
              # HTML/static changes — no restart needed (read fresh on every request)
```

### Updating Security Group IP (Dynamic IP helper)

```bash
bash deploy/update-sg-ip.sh    # updates AWS SG inbound rule to current public IP
```

---

## 🔔 Monitoring &amp; Alerts

### Telegram / Discord Alerts

Configure once in `.env` — alerts fire automatically on:
- Order placed / filled / rejected
- Engine start / stop / crash
- SL or target hit (scalp mode)
- Broker API errors or timeouts

```bash
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id        # group chat ID or DM chat ID
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
```

### Prometheus + Grafana (Optional)

If `prometheus_fastapi_instrumentator` is installed, a `/metrics` endpoint is exposed automatically.

```bash
pip install prometheus_fastapi_instrumentator
# Metrics available at http://localhost:8000/metrics
```

Point your Grafana datasource to the Prometheus scrape target for latency histograms, request counts, and custom trading metrics.

### Log Tailing

```bash
# systemd journal
sudo journalctl -u algoforge  -f
sudo journalctl -u cryptoforge -f

# Local dev
tail -f server.log
```

---

## 🛡 Error Recovery Matrix

| Failure | Detection | Recovery |
|---------|----------|----------|
| WebSocket disconnect | `on_close` callback | Auto-reconnect with exponential backoff |
| API timeout | `asyncio.timeout` | Retry 3×, then alert |
| 429 Rate Limit | HTTP 429 status | Honour `Retry-After` header, else wait 60s |
| 5xx Server Error | HTTP 5xx status | Exponential backoff, max 60s |
| Invalid signature | 401 response | Regenerate timestamp, retry once |
| Dhan JWT expiry | JWT decode on startup | TOTP auto-token regeneration |
| Process crash | systemd `Restart=always` | Auto-restart in 5 seconds |
| Engine state loss | `atexit` shutdown hook | State serialised to `runs.json` before exit |

---

## 📜 License

Private — All rights reserved. © 2025 AlgoForge / CryptoForge.

---

<div align="center">
Built with ⚡ FastAPI · Python 3.11 · Delta Exchange · Dhan API
</div>
