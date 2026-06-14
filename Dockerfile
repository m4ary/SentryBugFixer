FROM python:3.12-slim

# git is required to clone/commit/push the target repo; ca-certificates for HTTPS to
# Anthropic / Sentry / GitLab.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    GIT_TERMINAL_PROMPT=0 \
    SBF_HOST=0.0.0.0 \
    SBF_PORT=8000 \
    SBF_DATA_DIR=/data

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY sentrybugfixer ./sentrybugfixer

# Run as a non-root user; it owns the persistent data dir.
RUN useradd -m -u 10001 sbf \
    && mkdir -p /data \
    && chown -R sbf:sbf /data /app
USER sbf

VOLUME ["/data"]
EXPOSE 8000

# Single process on purpose: fix jobs run in background threads and stream over an
# in-memory WebSocket broker, and storage is SQLite — multiple workers would split
# that state. Scale by running one container, not multiple uvicorn workers.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('SBF_PORT','8000')+'/health', timeout=3)"

CMD ["python", "-m", "sentrybugfixer"]
