# Portfolio Runner

Automated research loop on top of TradingAgents. It rates a pool of tickers,
paper-trades the result into an **in-memory** portfolio (nothing is ever sent to
a broker), and serves a live dashboard with P&L.

## Run

```bash
cp portfolio_runner/.env.example .env   # then edit
python -m portfolio_runner.main
```

Open http://127.0.0.1:8765.

## Cadence: why decisions are daily, marks are 3h

The agents reason over **daily** data: `propagate()` takes a `YYYY-MM-DD` date,
prices/indicators come from daily bars, and the reflection layer scores each
decision over a ~5-day holding window. Re-running the full graph every 3h feeds
the agents the same daily bar and mostly samples LLM noise, at 8x the token cost.

So the loop is split:

| Pass | Default | Cost | What it does |
|---|---|---|---|
| Decision | 24h | full agent graph per symbol | re-rates the pool, buys/sells |
| Mark | 3h | quotes only, no LLM | refreshes prices, P&L, equity curve |

Set `PORTFOLIO_DECISION_INTERVAL_HOURS=3` if you explicitly want the research
signal re-sampled every 3 hours.

## Resilience

LLM endpoints and data vendors fail routinely, so:

- every external call retries with exponential backoff + jitter (`PORTFOLIO_RETRY_ATTEMPTS`)
- the LLM client is rebuilt after a failed analysis attempt
- a symbol that keeps failing is skipped, the cycle continues
- a failed cycle never kills the runner; it logs and waits for the next pass
- all failures appear in the dashboard activity feed

## Layout

| File | Role |
|---|---|
| `store.py` | thread-safe in-memory portfolio, ledger, equity curve |
| `engine.py` | retrying analysis + trade execution |
| `server.py` | stdlib HTTP server (`/`, `/api/state`) |
| `static/index.html` | dashboard, polls every 5s |
| `main.py` | orchestration loop |

State is memory-only and resets on restart, by design.

> Research tool, not investment advice. No broker integration.
