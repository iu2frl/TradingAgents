"""Analysis loop: rate each symbol with TradingAgents, then trade on the rating.

LLM endpoints and market-data providers fail routinely (timeouts, 429s, 5xx,
malformed responses), so every external call goes through ``retry`` with
exponential backoff, and a symbol that keeps failing is skipped instead of
aborting the cycle. The graph itself is built lazily and rebuilt after a hard
failure so a broken client can recover on the next pass.
"""

from __future__ import annotations

import os
import random
import time
from collections.abc import Callable
from datetime import datetime
from datetime import time as clock_time
from typing import TypeVar
from zoneinfo import ZoneInfo

import yfinance as yf

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph

from .store import PortfolioStore

T = TypeVar("T")

BUY_RATINGS = {"buy", "overweight"}
SELL_RATINGS = {"sell", "underweight"}

MARKET_TZ = ZoneInfo("America/New_York")
MARKET_CLOSE = clock_time(16, 0)


class RetryError(RuntimeError):
    """Raised when every attempt of a retried operation failed."""


def retry(
    operation: Callable[[], T],
    *,
    attempts: int = 3,
    base_delay: float = 2.0,
    max_delay: float = 30.0,
    label: str = "operation",
    on_error: Callable[[int, Exception], None] | None = None,
) -> T:
    """Run ``operation`` with exponential backoff and jitter."""
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return operation()
        except Exception as exc:  # external endpoints raise anything
            last_exc = exc
            if on_error is not None:
                on_error(attempt, exc)
            if attempt == attempts:
                break
            delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
            time.sleep(delay + random.uniform(0, delay * 0.25))
    raise RetryError(f"{label} failed after {attempts} attempts: {last_exc}") from last_exc


class TradingEngine:
    def __init__(
        self,
        store: PortfolioStore,
        symbols: list[str],
        *,
        buy_amount: float = 75.0,
        max_positions: int = 3,
        max_symbol_cost: float = 150.0,
        attempts: int = 3,
    ):
        self.store = store
        self.symbols = symbols
        self.buy_amount = buy_amount
        self.max_positions = max_positions
        self.max_symbol_cost = max_symbol_cost
        self.attempts = attempts
        self._graph: TradingAgentsGraph | None = None

    # ----------------------------------------------------------------- graph
    def _build_graph(self) -> TradingAgentsGraph:
        config = DEFAULT_CONFIG.copy()
        config["llm_provider"] = os.getenv("TRADINGAGENTS_LLM_PROVIDER", "openai_compatible")
        config["backend_url"] = os.getenv(
            "TRADINGAGENTS_LLM_BACKEND_URL", "http://localhost:8000/v1"
        )
        model = os.getenv("TRADINGAGENTS_LLM_MODEL", "my-model")
        config["deep_think_llm"] = model
        config["quick_think_llm"] = model
        config["temperature"] = 0.0
        config["llm_max_retries"] = int(os.getenv("TRADINGAGENTS_LLM_MAX_RETRIES", "5"))
        return TradingAgentsGraph(debug=False, config=config)

    def _graph_or_build(self) -> TradingAgentsGraph:
        if self._graph is None:
            self._graph = retry(
                self._build_graph,
                attempts=self.attempts,
                label="graph init",
                on_error=lambda n, e: self.store.log(
                    "warn", f"Graph init attempt {n} failed: {e}"
                ),
            )
        return self._graph

    # ---------------------------------------------------------------- prices
    @staticmethod
    def _fetch_price(symbol: str) -> float:
        ticker = yf.Ticker(symbol)
        price = getattr(ticker.fast_info, "last_price", None)
        if not price:
            history = ticker.history(period="1d")
            if history.empty:
                raise ValueError(f"no quote available for {symbol}")
            price = float(history["Close"].iloc[-1])
        price = float(price)
        if price <= 0:
            raise ValueError(f"invalid quote {price} for {symbol}")
        return price

    def price_of(self, symbol: str) -> float:
        return retry(
            lambda: self._fetch_price(symbol),
            attempts=self.attempts,
            base_delay=1.0,
            label=f"quote {symbol}",
            on_error=lambda n, e: self.store.log("warn", f"{symbol}: quote attempt {n} failed: {e}"),
        )

    # -------------------------------------------------------------- sessions
    @staticmethod
    def _completed_sessions(symbol: str) -> list[tuple[str, float]]:
        """Daily closes for sessions that have finished, oldest first.

        A day with no bar is a weekend or a holiday, so the bars themselves act
        as the exchange calendar and no separate calendar dependency is needed.
        """
        history = yf.Ticker(symbol).history(period="1mo", interval="1d")
        if history.empty:
            raise ValueError(f"no daily bars for {symbol}")

        now = datetime.now(MARKET_TZ)
        sessions: list[tuple[str, float]] = []
        for stamp, close in zip(history.index, history["Close"], strict=False):
            day = stamp.date()
            if day > now.date() or (day == now.date() and now.time() < MARKET_CLOSE):
                continue  # session still in progress
            price = float(close)
            if price > 0:
                sessions.append((day.isoformat(), price))
        return sessions

    def last_session(self) -> str:
        """Date of the most recent finished trading session, as YYYY-MM-DD."""

        def _run() -> str:
            sessions = self._completed_sessions(self.symbols[0])
            if not sessions:
                raise ValueError(f"no completed session for {self.symbols[0]}")
            return sessions[-1][0]

        return retry(
            _run,
            attempts=self.attempts,
            base_delay=1.0,
            label="session date",
            on_error=lambda n, e: self.store.log("warn", f"session lookup attempt {n} failed: {e}"),
        )

    def close_on(self, symbol: str, session: str) -> float:
        """Official close for ``session``, so the fill matches the analysed bar."""

        def _run() -> float:
            for day, price in self._completed_sessions(symbol):
                if day == session:
                    return price
            raise ValueError(f"no {session} close for {symbol}")

        return retry(
            _run,
            attempts=self.attempts,
            base_delay=1.0,
            label=f"close {symbol}",
            on_error=lambda n, e: self.store.log("warn", f"{symbol}: close attempt {n} failed: {e}"),
        )

    # -------------------------------------------------------------- decision
    def rating_of(self, symbol: str, trade_date: str) -> str:
        def _run() -> str:
            graph = self._graph_or_build()
            _, decision = graph.propagate(symbol, trade_date)
            return str(decision).strip()

        def _on_error(attempt: int, exc: Exception) -> None:
            self.store.log("warn", f"{symbol}: analysis attempt {attempt} failed: {exc}")
            self._graph = None  # force a clean client on the next attempt

        return retry(
            _run,
            attempts=self.attempts,
            base_delay=5.0,
            label=f"analysis {symbol}",
            on_error=_on_error,
        )

    @staticmethod
    def action_for(rating: str) -> str:
        value = rating.strip().lower()
        if value in BUY_RATINGS:
            return "BUY"
        if value in SELL_RATINGS:
            return "SELL"
        return "HOLD"

    # ----------------------------------------------------------------- cycle
    def refresh_prices(self) -> None:
        for symbol in dict.fromkeys([*self.symbols, *self.store.positions]):
            try:
                self.store.mark_price(symbol, self.price_of(symbol))
            except RetryError as exc:
                self.store.log("warn", f"{symbol}: could not refresh price: {exc}")

    def run_cycle(self, trade_date: str) -> None:
        cycle = self.store.start_cycle()
        self.store.log("info", f"Cycle {cycle} started for session {trade_date}")
        self.refresh_prices()

        for symbol in self.symbols:
            try:
                rating = self.rating_of(symbol, trade_date)
            except RetryError as exc:
                self.store.log("error", f"{symbol}: analysis skipped — {exc}")
                continue

            action = self.action_for(rating)

            try:
                price = self.close_on(symbol, trade_date)
            except RetryError as exc:
                self.store.log(
                    "error", f"{symbol}: no {trade_date} close, decision not executed — {exc}"
                )
                self.store.record_rating(symbol, action, rating, None)
                continue

            self.store.mark_price(symbol, price)
            self.store.record_rating(symbol, action, rating, round(price, 4))
            self._execute(symbol, action, rating, price)

        self.store.finish_cycle()
        self.store.log("info", f"Cycle {cycle} finished")

    def _execute(self, symbol: str, action: str, rating: str, price: float) -> None:
        held = symbol in self.store.positions

        if action == "BUY":
            if not held and len(self.store.positions) >= self.max_positions:
                self.store.log("info", f"{symbol}: BUY skipped, position limit reached")
                return
            position = self.store.positions.get(symbol)
            room = self.max_symbol_cost - (position.cost_basis if position else 0.0)
            if room <= 0:
                self.store.log("info", f"{symbol}: BUY skipped, symbol exposure limit reached")
                return
            trade = self.store.buy(symbol, price, min(self.buy_amount, room), rating)
            if trade is None:
                self.store.log("info", f"{symbol}: BUY skipped, insufficient cash")
            else:
                self.store.log(
                    "trade", f"BUY {symbol} {trade.qty:.4f} @ {price:.2f} ({rating})"
                )
        elif action == "SELL":
            if not held:
                self.store.log("info", f"{symbol}: SELL ignored, no open position")
                return
            trade = self.store.sell(symbol, price, rating)
            if trade is not None:
                self.store.log(
                    "trade",
                    f"SELL {symbol} {trade.qty:.4f} @ {price:.2f} "
                    f"({rating}) P&L {trade.realized_pnl:+.2f}",
                )
        else:
            self.store.log("info", f"{symbol}: HOLD ({rating})")
