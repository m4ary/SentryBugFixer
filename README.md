<div align="center">

# 🐞 SentryBugFixer

**Turn a Sentry issue into a GitLab merge request — automatically.**

[![CI](https://github.com/m4ary/SentryBugFixer/actions/workflows/docker.yml/badge.svg)](https://github.com/m4ary/SentryBugFixer/actions/workflows/docker.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-7c5cff.svg)](LICENSE)
[![Image](https://img.shields.io/badge/ghcr.io-sentrybugfixer-2ecc71?logo=docker&logoColor=white)](https://github.com/m4ary/SentryBugFixer/pkgs/container/sentrybugfixer)
[![Python](https://img.shields.io/badge/python-3.12%2B-blue?logo=python&logoColor=white)](#)

</div>

---

SentryBugFixer watches your Sentry issues, and for any one you pick (or via webhook) it
clones the GitLab repo, fixes the bug with an LLM agent, then commits, pushes, and opens a
merge request — all from a small self-hostable dashboard.

```
Sentry issue / webhook  →  clone the GitLab repo  →  LLM agent fixes it  →  branch + commit + push  →  open MR
```

The agent is a minimal LLM + `bash` loop inspired by
[mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent): a linear message history
where every action is a shell command run in the cloned repo.

## Features

- 🖥️ **Dashboard** — register projects (GitLab + Sentry URLs), list unresolved issues, fix any with one click.
- 🔌 **Sentry webhook** — auto-fix issues as they arrive.
- 📡 **Live logs** — stream each fix step over WebSocket in a popup.
- ✍️ **Custom instructions** — add guidance to the agent before a run.
- ♻️ **Resumable** — interrupted/errored fixes continue from the last step instead of re-paying for it.
- 🧮 **Cost tracking** — per-fix token usage and USD cost, with a model price table and per-issue history.
- 🔁 **MR lifecycle** — issues show **Under review** while the MR is open, and are resolved when it closes/merges.
- 🐳 **Production-ready** — multi-arch Docker image (amd64 + arm64), health check, non-root, nginx sample, CI.

## Requirements

- An **Anthropic API key** (`ANTHROPIC_API_KEY`)
- A **Sentry auth token** (`project:read`, `event:read`)
- A **GitLab token** (`api`, `write_repository`)
- For local runs: **Python 3.12+** and **git** on `PATH` (the Docker image bundles git).

Copy `.env.example` to `.env` and fill these in — both run modes read it.

---

## Run with Docker (recommended)

```bash
git clone https://github.com/m4ary/SentryBugFixer.git
cd SentryBugFixer
cp .env.example .env          # fill in your tokens
docker compose up -d          # pulls ghcr.io/m4ary/sentrybugfixer:latest
```

Open **http://localhost:8000** · logs: `docker compose logs -f` · stop: `docker compose down`.

The image is **multi-arch** (works on x86-64 Ubuntu servers and ARM/Apple Silicon), runs as a
non-root user, exposes `/health`, and persists the SQLite db, cloned repos, and resume
transcripts to the `sbf-data` volume — back it up to keep history.

> Pin a version for production: set `image: ghcr.io/m4ary/sentrybugfixer:1.0.0` in
> `docker-compose.yml`. To build from source instead, uncomment `build: .` there and run
> `docker compose up -d --build`.

Plain Docker, without compose:

```bash
docker run -d --name sentrybugfixer -p 8000:8000 \
  --env-file .env -e SBF_DATA_DIR=/data -v sbf-data:/data \
  ghcr.io/m4ary/sentrybugfixer:latest
```

## Run locally

```bash
git clone https://github.com/m4ary/SentryBugFixer.git
cd SentryBugFixer
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # fill in your tokens

python -m sentrybugfixer      # single process (production-style)
# or, for development with auto-reload:
./dev.sh
```

Open **http://127.0.0.1:8000**.

## Using the dashboard

1. **Add a project** — name, GitLab repo URL, Sentry project URL, default branch.
2. **View issues** — lists unresolved Sentry issues for that project.
3. **Fix it** — optionally add instructions, then go. It clones the repo, runs the agent,
   pushes `issue/<id>`, and opens a merge request. Watch progress live; the MR link appears
   when it's done, and the issue flips to **Under review**.

### Sentry webhook (automatic fixing)

Point a Sentry **Internal Integration** (or alert webhook) at:

```
POST  https://<your-host>/webhook/sentry
```

SentryBugFixer matches the webhook's project slug to a configured project and starts a fix
automatically. Set `SENTRY_WEBHOOK_SECRET` to reject unsigned calls (checked against the
`sentry-hook-signature` header).

## Configuration

All settings come from the environment / `.env` (see [`.env.example`](.env.example)):

| Variable | Default | Purpose |
|----------|---------|---------|
| `ANTHROPIC_API_KEY` | — | The bug-fixing agent (**required**) |
| `SBF_MODEL` | `claude-opus-4-8` | Agent model |
| `SBF_EFFORT` | `high` | Thinking/effort: `low`…`max` |
| `SENTRY_AUTH_TOKEN` | — | Read issues (**required**) |
| `SENTRY_HOST` | `https://sentry.io` | For self-hosted Sentry |
| `SENTRY_WEBHOOK_SECRET` | — | Shared secret for the webhook |
| `GITLAB_TOKEN` | — | Clone + open MRs (**required**) |
| `GITLAB_HOST` | `https://gitlab.com` | For self-hosted GitLab |
| `SBF_MAX_STEPS` | `40` | Max agent steps per fix |
| `SBF_BASH_TIMEOUT` | `180` | Per-command timeout (seconds) |
| `SBF_HOST` / `SBF_PORT` | `127.0.0.1` / `8000` | Server bind address |
| `SBF_DATA_DIR` | `./data` | SQLite db, clones, transcripts |

## Architecture

| File | Responsibility |
|------|----------------|
| `sentrybugfixer/agent.py` | Minimal Anthropic agent loop with a single `bash` tool (streaming, resumable) |
| `sentrybugfixer/sentry_client.py` | List issues, fetch issue + stack trace, parse webhooks, resolve issues |
| `sentrybugfixer/gitlab_client.py` | Clone, branch/commit/push, open & inspect merge requests |
| `sentrybugfixer/fixer.py` | Orchestrates one fix end-to-end (background thread) |
| `sentrybugfixer/pricing.py` | Model price table + cost computation |
| `sentrybugfixer/db.py` | SQLite storage for projects and jobs (auto-migrating) |
| `sentrybugfixer/events.py` | Thread → WebSocket pub/sub for live logs |
| `sentrybugfixer/app.py` | FastAPI: dashboard API, webhook, health, WebSocket |
| `sentrybugfixer/static/` | The dashboard (vanilla HTML/CSS/JS) |

## Production notes

- **Run a single instance.** Fix jobs run in background threads and stream over an
  **in-memory** broker, and storage is **SQLite** — do *not* run multiple uvicorn workers or
  replicas; that splits job state and the live log stream. One container handles many fixes.
  To scale further, move the broker to Redis and the db to Postgres (the `Database` class is
  isolated for this).
- **Put it behind HTTPS** (nginx/Caddy/Traefik). A sample with the required `/ws/` WebSocket
  upgrade headers is in [`deploy/nginx.conf`](deploy/nginx.conf).
- **Secrets** come from the environment — never baked into the image (`.env` is in
  `.dockerignore`). Use your platform's secret manager in production.
- **CI** ([`.github/workflows/docker.yml`](.github/workflows/docker.yml)) builds and pushes
  the multi-arch image to GHCR on pushes to `main` and `v*` tags.

## How it works (safety)

- Git operations (branch, commit, push, MR) are done **deterministically in Python** — the
  agent only edits files, it never pushes. Commits are unsigned (`commit.gpgsign=false`) so
  they work in headless/containerized environments.
- Branch name is `issue/<sentry-issue-id>`; pushes use `--force-with-lease`.
- Each fix runs in its own cloned checkout under `SBF_DATA_DIR/repos/`.
- Starting a fix for an issue already queued/running returns the existing job (no duplicates).

## License

[MIT](LICENSE) © Mshary
