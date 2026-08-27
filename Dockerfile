# syntax=docker/dockerfile:1

FROM python:3.12-slim AS base

# Unbuffered so logs reach `docker logs` immediately rather than sitting in a
# pipe buffer - the difference between seeing a crash and not.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first: this layer is cached until requirements.txt changes, so
# code edits rebuild in seconds.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY dipbot/ ./dipbot/

# The bot only ever needs to write to its data directory.
RUN useradd --create-home --uid 10001 dipbot \
    && mkdir -p /data \
    && chown -R dipbot:dipbot /app /data
USER dipbot

ENV DATABASE_PATH=/data/dipbot.sqlite

# Liveness: the process is healthy if it can still open its own database.
# A crashed asyncio task leaves the process running, so "is it up" is not
# enough on its own - the daily summary and /status cover behaviour.
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import sqlite3,os,sys; \
sys.exit(0 if os.path.exists(os.environ['DATABASE_PATH']) and \
sqlite3.connect(os.environ['DATABASE_PATH']).execute('select 1').fetchone() else 1)"

CMD ["python", "-m", "dipbot.main"]
