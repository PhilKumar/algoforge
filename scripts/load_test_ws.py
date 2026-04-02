#!/usr/bin/env python3
"""Multi-user WebSocket load and isolation probe for PhilForge.

Typical usage on the main production-shaped environment:
  python3 scripts/load_test_ws.py \
    --base-url https://philforge.in \
    --credential admin:123456 \
    --credential phil:654321 \
    --credential user3:abcdef \
    --credential user4:qwerty \
    --credential user5:zxcvbn \
    --start-paper \
    --duration 12
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx
import websockets


def _ws_url(base_url: str) -> str:
    parsed = urlparse(base_url.rstrip("/"))
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return f"{scheme}://{parsed.netloc}/ws"


def _cookie_header(client: httpx.AsyncClient) -> str:
    return "; ".join(f"{cookie.name}={cookie.value}" for cookie in client.cookies.jar)


def _paper_payload(run_name: str, instrument: str) -> dict[str, Any]:
    return {
        "run_name": run_name,
        "folder": "Load Test",
        "instrument": instrument,
        "indicators": [],
        "entry_conditions": [],
        "exit_conditions": [],
        "legs": [
            {
                "option_type": "CE",
                "transaction_type": "BUY",
                "strike_type": "atm",
                "strike_value": 0,
                "lots": 1,
                "sl_pct": 10,
                "target_pct": 10,
                "trail_pct": 0,
                "sqoff_time": "15:20",
            }
        ],
        "max_trades_per_day": 1,
        "market_open": "09:15",
        "market_close": "15:25",
        "initial_capital": 500000,
    }


@dataclass
class ProbeResult:
    username: str
    ok: bool
    messages: int
    isolated: bool
    note: str


async def _login(client: httpx.AsyncClient, username: str, password: str) -> dict[str, Any]:
    resp = await client.post("/api/auth/login", json={"username": username, "password": password})
    data = resp.json()
    if resp.status_code != 200:
        raise RuntimeError(data.get("detail") or data.get("message") or f"Login failed for {username}")
    return data


async def _start_paper_run(client: httpx.AsyncClient, username: str, instrument: str) -> str:
    run_name = f"WS_LOAD_{username}_{int(time.time())}"
    resp = await client.post("/api/paper/start", json=_paper_payload(run_name, instrument))
    data = resp.json()
    if resp.status_code != 200 or data.get("status") != "started":
        raise RuntimeError(data.get("detail") or data.get("message") or f"Paper start failed for {username}")
    return run_name


async def _stop_paper_run(client: httpx.AsyncClient, run_name: str) -> None:
    try:
        await client.post("/api/paper/stop", json={"run_id": run_name})
    except Exception:
        return


async def _probe_user(
    base_url: str,
    username: str,
    password: str,
    duration: float,
    start_paper: bool,
    instrument: str,
) -> ProbeResult:
    ws_url = _ws_url(base_url)
    run_name = ""
    async with httpx.AsyncClient(base_url=base_url, follow_redirects=True, timeout=20.0) as client:
        await _login(client, username, password)
        if start_paper:
            run_name = await _start_paper_run(client, username, instrument)
        cookie = _cookie_header(client)
        if not cookie:
            raise RuntimeError(f"No session cookie returned for {username}")

        messages = 0
        isolated = True
        note = "ok"
        started = time.monotonic()
        try:
            async with websockets.connect(
                ws_url,
                extra_headers={"Cookie": cookie},
                open_timeout=10,
                close_timeout=5,
                max_size=2_000_000,
            ) as ws:
                while time.monotonic() - started < duration:
                    raw = await asyncio.wait_for(ws.recv(), timeout=3.0)
                    if isinstance(raw, bytes):
                        payload = json.loads(raw.decode("utf-8"))
                    else:
                        payload = json.loads(raw)
                    messages += 1

                    if run_name:
                        paper_engines = payload.get("paper_engines") or {}
                        if paper_engines and any(rid != run_name for rid in paper_engines):
                            isolated = False
                            note = f"Received foreign paper run ids: {sorted(paper_engines)}"
                            break
                        payload_run_id = str(payload.get("run_id", "") or "")
                        if payload_run_id and payload.get("source") == "paper" and payload_run_id != run_name:
                            isolated = False
                            note = f"Received foreign paper event: {payload_run_id}"
                            break
        finally:
            if run_name:
                await _stop_paper_run(client, run_name)
            await client.post("/api/auth/logout")

    if messages == 0:
        return ProbeResult(
            username=username, ok=False, messages=0, isolated=False, note="No websocket messages received"
        )
    return ProbeResult(username=username, ok=isolated, messages=messages, isolated=isolated, note=note)


async def main() -> int:
    parser = argparse.ArgumentParser(description="Probe concurrent WebSocket sessions for PhilForge user isolation.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="PhilForge base URL")
    parser.add_argument(
        "--credential",
        action="append",
        default=[],
        help="Username and password in username:password form. Repeat this flag for multiple users.",
    )
    parser.add_argument("--duration", type=float, default=10.0, help="How long each websocket probe stays connected")
    parser.add_argument("--start-paper", action="store_true", help="Start one paper engine per user before probing")
    parser.add_argument("--instrument", default="26000", help="Instrument to use when --start-paper is enabled")
    args = parser.parse_args()

    if len(args.credential) < 1:
        parser.error("Provide at least one --credential user:password pair")

    credentials: list[tuple[str, str]] = []
    for item in args.credential:
        if ":" not in item:
            parser.error(f"Invalid credential format: {item}")
        username, password = item.split(":", 1)
        credentials.append((username.strip(), password))

    tasks = [
        _probe_user(args.base_url, username, password, args.duration, args.start_paper, args.instrument)
        for username, password in credentials
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    failures = 0
    for result in results:
        if isinstance(result, Exception):
            failures += 1
            print(f"[FAIL] {result}")
            continue
        line = f"[{'PASS' if result.ok else 'FAIL'}] {result.username}: {result.messages} ws messages, isolated={result.isolated}"
        if result.note != "ok":
            line += f" ({result.note})"
        print(line)
        if not result.ok:
            failures += 1

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
