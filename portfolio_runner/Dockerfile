# Multi-arch (linux/amd64, linux/arm64) image for the research portfolio runner.
# Build from the repository root:
#   docker buildx build --platform linux/amd64,linux/arm64 \
#     -f portfolio_runner/Dockerfile -t tradingagents-portfolio .

FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /build
# Only the packaging inputs, so dependency layers cache across code edits.
COPY pyproject.toml README.md ./
COPY tradingagents ./tradingagents
COPY cli ./cli
RUN pip install --no-cache-dir .

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORTFOLIO_HOST=0.0.0.0 \
    PORTFOLIO_PORT=8765

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

RUN useradd --create-home appuser \
 && install -d -m 0755 -o appuser -g appuser /home/appuser/.tradingagents
USER appuser
WORKDIR /home/appuser/app

COPY --chown=appuser:appuser portfolio_runner ./portfolio_runner

EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8765/api/state', timeout=4).status == 200 else 1)"

# main() installs SIGTERM/SIGINT handlers, so `docker stop` shuts down cleanly.
CMD ["python", "-m", "portfolio_runner.main"]
