"""Automated research runner: analyse a stock pool, paper-trade the result into
an in-memory portfolio, and serve a live dashboard.

Run with:  python -m portfolio_runner.main
"""

from __future__ import annotations

import os
import signal
import threading
import traceback
from datetime import date, datetime, timedelta, timezone

from dotenv import load_dotenv

from .engine import TradingEngine
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


def main() -> None:
    symbols = [
        s.strip().upper()
        for s in os.getenv("PORTFOLIO_SYMBOLS", "AAPL,MSFT,NVDA,AMZN,GOOGL").split(",")
        if s.strip()
    ]
    # Decisions follow the daily bars the agents reason over; marks only refresh P&L.
    decision_every = timedelta(hours=_env_float("PORTFOLIO_DECISION_INTERVAL_HOURS", 24.0))
    mark_every = timedelta(hours=_env_float("PORTFOLIO_MARK_INTERVAL_HOURS", 3.0))
    host = os.getenv("PORTFOLIO_HOST", "127.0.0.1")
    port = _env_int("PORTFOLIO_PORT", 8765)

    store = PortfolioStore(starting_cash=_env_float("PORTFOLIO_CASH_BUDGET", 500.0))
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
    print(f"Decisions every {decision_every} · marks every {mark_every}")

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: _shutdown.set())

    next_decision = datetime.now(timezone.utc)
    while not _shutdown.is_set():
        now = datetime.now(timezone.utc)
        try:
            if now >= next_decision:
                engine.run_cycle(date.today().strftime("%Y-%m-%d"))
                next_decision = datetime.now(timezone.utc) + decision_every
                store.set_next_run(next_decision)
            else:
                engine.refresh_prices()
                store.mark_only()
        except Exception as exc:  # a failed pass must never kill the runner
            store.set_status("idle")
            store.log("error", f"Pass aborted: {exc}")
            traceback.print_exc()

        _shutdown.wait(mark_every.total_seconds())

    store.log("info", "Shutting down")
    server.shutdown()
    print("Stopped.")


if __name__ == "__main__":
    main()
