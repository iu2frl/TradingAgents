"""Automated research runner: analyse a stock pool, paper-trade the result into
an in-memory portfolio, and serve a live dashboard.

Run with:  python -m portfolio_runner.main
"""

from __future__ import annotations

import os
import signal
import threading
import traceback
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

from .engine import RetryError, TradingEngine
from .server import start_server
from .store import PortfolioStore

load_dotenv()

_shutdown = threading.Event()


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


def _env_time(name: str, default: str) -> time:
    raw = str(os.getenv(name, default))
    try:
        hour, minute = (int(part) for part in raw.split(":", 1))
        return time(hour, minute)
    except ValueError:
        hour, minute = (int(part) for part in default.split(":", 1))
        return time(hour, minute)


def _env_zone(name: str, default: str) -> ZoneInfo:
    try:
        return ZoneInfo(str(os.getenv(name, default)))
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(default)


def _next_decision(after: datetime, at: time, zone: ZoneInfo) -> datetime:
    """Next occurrence of ``at`` in ``zone``, strictly after ``after``."""
    local = after.astimezone(zone)
    target = local.replace(hour=at.hour, minute=at.minute, second=0, microsecond=0)
    if target <= local:
        target += timedelta(days=1)
    return target.astimezone(timezone.utc)


def main() -> None:
    symbols = [
        s.strip().upper()
        for s in os.getenv("PORTFOLIO_SYMBOLS", "AAPL,MSFT,NVDA,AMZN,GOOGL").split(",")
        if s.strip()
    ]
    # Decisions follow the daily bars the agents reason over; marks only refresh P&L.
    decision_at = _env_time("PORTFOLIO_DECISION_AT", "16:30")
    decision_zone = _env_zone("PORTFOLIO_DECISION_TZ", "America/New_York")
    mark_every = timedelta(hours=_env_float("PORTFOLIO_MARK_INTERVAL_HOURS", 3.0))
    host = os.getenv("PORTFOLIO_HOST", "127.0.0.1")
    port = _env_int("PORTFOLIO_PORT", 8765)

    store = PortfolioStore(
        starting_cash=_env_float("PORTFOLIO_CASH_BUDGET", 500.0),
        state_file=Path(
            os.getenv(
                "PORTFOLIO_STATE_FILE",
                str(Path.home() / ".tradingagents" / "portfolio_state.json"),
            )
        ),
    )
    if store.restore_file():
        store.log(
            "info",
            f"Restored cycle {store.cycle}, {len(store.trades)} operations, "
            f"{len(store.positions)} open positions",
        )
    store.set_pool(symbols)
    engine = TradingEngine(
        store,
        symbols,
        buy_amount=_env_float("PORTFOLIO_BUY_AMOUNT", 75.0),
        max_positions=_env_int("PORTFOLIO_MAX_POSITIONS", 3),
        max_symbol_cost=_env_float("PORTFOLIO_MAX_SYMBOL_COST", 150.0),
        attempts=_env_int("PORTFOLIO_RETRY_ATTEMPTS", 3),
    )

    server = start_server(store, host, port)
    store.log("info", f"Dashboard on http://{host}:{port} · pool: {', '.join(symbols)}")
    print(f"Dashboard: http://{host}:{port}")
    print(f"Pool: {', '.join(symbols)}")
    print(
        f"Decisions at {decision_at:%H:%M} {decision_zone.key} · marks every {mark_every}"
    )

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: _shutdown.set())

    next_decision = datetime.now(timezone.utc)
    while not _shutdown.is_set():
        now = datetime.now(timezone.utc)
        try:
            if now >= next_decision:
                session = engine.last_session()
                if session == store.last_session:
                    store.log("info", f"No session after {session}, decisions skipped")
                else:
                    engine.run_cycle(session)
                    store.set_last_session(session)
                next_decision = _next_decision(
                    datetime.now(timezone.utc), decision_at, decision_zone
                )
                store.set_next_run(next_decision)
            else:
                engine.refresh_prices()
                store.mark_only()
        except RetryError as exc:
            # Leave next_decision alone so the pass is retried on the next tick.
            store.set_status("idle")
            store.log("warn", f"Session lookup failed, retrying later: {exc}")
        except Exception as exc:  # a failed pass must never kill the runner
            store.set_status("idle")
            store.log("error", f"Pass aborted: {exc}")
            traceback.print_exc()

        wait = mark_every.total_seconds()
        remaining = (next_decision - datetime.now(timezone.utc)).total_seconds()
        if 0 < remaining < wait:
            wait = remaining
        _shutdown.wait(max(wait, 1.0))

    store.log("info", "Shutting down")
    server.shutdown()
    print("Stopped.")


if __name__ == "__main__":
    main()
