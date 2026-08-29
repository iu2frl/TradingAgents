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

## Docker

The image is `python:3.12-slim` based and builds for **linux/amd64** and
**linux/arm64**. It starts the loop automatically and publishes the dashboard.

```bash
docker compose up -d portfolio-runner   # -> http://localhost:8765
```

Or standalone, from the repository root:

```bash
docker build -f portfolio_runner/Dockerfile -t tradingagents-portfolio .
docker run -d --env-file .env -p 8765:8765 tradingagents-portfolio
```

Multi-arch publish:

```bash
docker buildx build --platform linux/amd64,linux/arm64 \
  -f portfolio_runner/Dockerfile -t <registry>/tradingagents-portfolio:latest --push .
```

Container notes:

- runs as non-root `appuser`, binds `0.0.0.0:8765` inside the container
- `HEALTHCHECK` polls `/api/state`
- `docker stop` triggers SIGTERM, which the loop handles for a clean shutdown
- point `TRADINGAGENTS_LLM_BACKEND_URL` at a reachable host — `localhost`
  inside the container is the container itself, use `host.docker.internal`
  (Docker Desktop) or a compose service name

## Cadence: why decisions are daily, marks are 3h

The agents reason over **daily** data: `propagate()` takes a `YYYY-MM-DD` date,
prices/indicators come from daily bars, and the reflection layer scores each
decision over a ~5-day holding window. Re-running the full graph every 3h feeds
the agents the same daily bar and mostly samples LLM noise, at 8x the token cost.

So the loop is split:

| Pass | Default | Cost | What it does |
|---|---|---|---|
| Decision | 16:30 America/New_York | full agent graph per symbol | re-rates the pool, buys/sells |
| Mark | 3h | quotes only, no LLM | refreshes prices, P&L, equity curve |

The decision pass is anchored to a fixed wall-clock time (`PORTFOLIO_DECISION_AT`,
`PORTFOLIO_DECISION_TZ`) shortly after the US close, so the session being analysed
is always complete. `trade_date` is not the calendar date: it is the date of the
last **finished** daily bar, and orders fill at that bar's close, so the price the
portfolio pays is the price the agents reasoned about.

Weekends and holidays need no calendar: a non-trading day produces no bar, so the
session date does not advance and the cycle is skipped with
`No session after <date>, decisions skipped`. If the runner is offline for a
while it resumes at the latest session rather than replaying the gap.

## State and restarts

The book is persisted to `PORTFOLIO_STATE_FILE`
(default `~/.tradingagents/portfolio_state.json`, inside the mounted volume) after
every trade, rating and cycle boundary. Writes go to a temp file and are renamed
into place, so a crash mid-write cannot truncate it. On startup the runner adopts
the file if present: positions, ledger, realized P&L, cycle number, equity curve
and the last processed session all survive a restart or an image update.

`starting_cash` comes from the state file when one exists, so the P&L baseline is
never reset by a changed `PORTFOLIO_CASH_BUDGET`. Restoring `last_session` also
prevents a restart from re-trading a session that was already executed.

`restore()` accepts either a saved state file or a raw `/api/state` response, so a
running instance can be migrated by saving its own dashboard payload:

```bash
curl -s http://localhost:8765/api/state > portfolio_state.json
# stop the container, copy the file into the volume as portfolio_state.json, restart
```

Delete the file to start a fresh book.

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
