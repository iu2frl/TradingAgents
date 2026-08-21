"""Thread-safe in-memory portfolio database.

Everything lives in process memory: positions, the full operation ledger, the
equity curve and the activity log. The engine writes to it from the worker
thread while the HTTP server reads snapshots for the UI, so every public method
is guarded by a single re-entrant lock.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Position:
    symbol: str
    qty: float = 0.0
    avg_cost: float = 0.0
    last_price: float = 0.0

    @property
    def market_value(self) -> float:
        return self.qty * self.last_price

    @property
    def cost_basis(self) -> float:
        return self.qty * self.avg_cost

    @property
    def unrealized_pnl(self) -> float:
        return self.market_value - self.cost_basis

    @property
    def unrealized_pct(self) -> float:
        return (self.unrealized_pnl / self.cost_basis * 100.0) if self.cost_basis else 0.0


@dataclass
class Trade:
    timestamp: str
    action: str
    symbol: str
    qty: float
    price: float
    value: float
    realized_pnl: float = 0.0
    rating: str = ""


@dataclass
class PortfolioStore:
    starting_cash: float = 500.0
    cash: float = field(init=False)
    pool: list[str] = field(default_factory=list)
    positions: dict[str, Position] = field(default_factory=dict)
    trades: list[Trade] = field(default_factory=list)
    realized_pnl: float = 0.0
    cycle: int = 0
    last_cycle_started: str | None = None
    last_cycle_finished: str | None = None
    next_cycle_at: str | None = None
    status: str = "idle"

    def __post_init__(self) -> None:
        self.cash = float(self.starting_cash)
        self._lock = threading.RLock()
        self._equity_curve: list[dict] = []
        self._events: deque[dict] = deque(maxlen=300)
        self._ratings: dict[str, dict] = {}
        self._last_prices: dict[str, float] = {}
        self._record_equity()

    def set_pool(self, symbols: list[str]) -> None:
        with self._lock:
            self.pool = list(symbols)

    # ------------------------------------------------------------------ log
    def log(self, level: str, message: str) -> None:
        with self._lock:
            self._events.appendleft({"ts": _now(), "level": level, "message": message})

    def set_status(self, status: str) -> None:
        with self._lock:
            self.status = status

    def set_next_run(self, when: datetime) -> None:
        with self._lock:
            self.next_cycle_at = when.astimezone(timezone.utc).isoformat(timespec="seconds")

    def start_cycle(self) -> int:
        with self._lock:
            self.cycle += 1
            self.last_cycle_started = _now()
            self.status = "running"
            return self.cycle

    def finish_cycle(self) -> None:
        with self._lock:
            self.last_cycle_finished = _now()
            self.status = "idle"
            self._record_equity()

    def mark_only(self) -> None:
        """Record an equity point from a price refresh, without a decision pass."""
        with self._lock:
            self._record_equity()

    # --------------------------------------------------------------- prices
    def mark_price(self, symbol: str, price: float) -> None:
        with self._lock:
            if price <= 0:
                return
            self._last_prices[symbol] = price
            pos = self.positions.get(symbol)
            if pos is not None:
                pos.last_price = price

    def record_rating(self, symbol: str, action: str, rating: str, price: float | None) -> None:
        with self._lock:
            self._ratings[symbol] = {
                "symbol": symbol,
                "action": action,
                "rating": rating,
                "price": price,
                "ts": _now(),
            }

    # ---------------------------------------------------------------- trades
    def buy(self, symbol: str, price: float, cash_amount: float, rating: str = "") -> Trade | None:
        """Buy ``cash_amount`` worth of ``symbol``, clamped to available cash."""
        with self._lock:
            if price <= 0 or self.cash <= 0:
                return None

            spend = min(cash_amount, self.cash)
            qty = spend / price
            if qty <= 0:
                return None

            self._last_prices[symbol] = price
            self.cash -= spend
            pos = self.positions.get(symbol)
            if pos is None:
                self.positions[symbol] = Position(symbol, qty, price, price)
            else:
                total_qty = pos.qty + qty
                pos.avg_cost = (pos.cost_basis + spend) / total_qty
                pos.qty = total_qty
                pos.last_price = price

            trade = Trade(_now(), "BUY", symbol, qty, price, spend, 0.0, rating)
            self.trades.insert(0, trade)
            self._record_equity()
            return trade

    def sell(self, symbol: str, price: float, rating: str = "") -> Trade | None:
        """Close the whole position in ``symbol`` and bank the realized P&L."""
        with self._lock:
            pos = self.positions.get(symbol)
            if pos is None or pos.qty <= 0 or price <= 0:
                return None

            proceeds = pos.qty * price
            realized = proceeds - pos.cost_basis
            self._last_prices[symbol] = price
            self.cash += proceeds
            self.realized_pnl += realized
            qty = pos.qty
            del self.positions[symbol]

            trade = Trade(_now(), "SELL", symbol, qty, price, proceeds, realized, rating)
            self.trades.insert(0, trade)
            self._record_equity()
            return trade

    # -------------------------------------------------------------- metrics
    def _equity(self) -> float:
        return self.cash + sum(p.market_value for p in self.positions.values())

    def _record_equity(self) -> None:
        point = {"ts": _now(), "equity": round(self._equity(), 2)}
        if self._equity_curve and self._equity_curve[-1]["equity"] == point["equity"]:
            self._equity_curve[-1] = point
            return
        self._equity_curve.append(point)
        if len(self._equity_curve) > 500:
            del self._equity_curve[0]

    def snapshot(self) -> dict:
        """Immutable view of the whole portfolio, ready for JSON serialization."""
        with self._lock:
            equity = self._equity()
            unrealized = sum(p.unrealized_pnl for p in self.positions.values())
            total_pnl = equity - self.starting_cash
            symbols = sorted(set(self.pool) | set(self.positions))
            return {
                "status": self.status,
                "cycle": self.cycle,
                "last_cycle_started": self.last_cycle_started,
                "last_cycle_finished": self.last_cycle_finished,
                "next_cycle_at": self.next_cycle_at,
                "starting_cash": round(self.starting_cash, 2),
                "cash": round(self.cash, 2),
                "invested": round(sum(p.cost_basis for p in self.positions.values()), 2),
                "equity": round(equity, 2),
                "realized_pnl": round(self.realized_pnl, 2),
                "unrealized_pnl": round(unrealized, 2),
                "total_pnl": round(total_pnl, 2),
                "total_pnl_pct": round(total_pnl / self.starting_cash * 100.0, 2)
                if self.starting_cash
                else 0.0,
                "pool": [self._pool_row(sym) for sym in symbols],
                "positions": [
                    {
                        "symbol": p.symbol,
                        "qty": round(p.qty, 6),
                        "avg_cost": round(p.avg_cost, 4),
                        "last_price": round(p.last_price, 4),
                        "market_value": round(p.market_value, 2),
                        "unrealized_pnl": round(p.unrealized_pnl, 2),
                        "unrealized_pct": round(p.unrealized_pct, 2),
                    }
                    for p in sorted(self.positions.values(), key=lambda x: x.symbol)
                ],
                "ratings": sorted(self._ratings.values(), key=lambda r: r["symbol"]),
                "trades": [t.__dict__ for t in self.trades[:100]],
                "trade_count": len(self.trades),
                "equity_curve": list(self._equity_curve),
                "events": list(self._events)[:80],
            }

    def _pool_row(self, symbol: str) -> dict:
        """One row per tracked symbol, whether or not it is currently held."""
        pos = self.positions.get(symbol)
        rating = self._ratings.get(symbol, {})
        last = pos.last_price if pos else self._last_prices.get(symbol, 0.0)
        return {
            "symbol": symbol,
            "held": pos is not None,
            "qty": round(pos.qty, 6) if pos else 0.0,
            "avg_cost": round(pos.avg_cost, 4) if pos else 0.0,
            "last_price": round(last, 4),
            "market_value": round(pos.market_value, 2) if pos else 0.0,
            "unrealized_pnl": round(pos.unrealized_pnl, 2) if pos else 0.0,
            "unrealized_pct": round(pos.unrealized_pct, 2) if pos else 0.0,
            "action": rating.get("action", "—"),
            "rating": rating.get("rating", ""),
        }
