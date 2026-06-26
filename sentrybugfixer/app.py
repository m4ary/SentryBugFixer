"""FastAPI app: dashboard API, static dashboard, and the Sentry webhook receiver."""

import asyncio
import logging
import sys
import threading
import uuid
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, model_validator

from . import __version__, github_client, gitlab_client, pricing, sentry_client
from .config import settings
from .db import Database
from .events import broker
from .fixer import branch_name, run_fix_job


def _setup_logging() -> None:
    """Stream fix-job logs to the console and silence the dashboard's polling spam."""
    sbf = logging.getLogger("sbf")
    if not sbf.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("%(asctime)s [%(name)s] %(message)s", "%H:%M:%S"))
        sbf.addHandler(handler)
        sbf.setLevel(logging.INFO)
        sbf.propagate = False

    class _DropPolling(logging.Filter):
        # Hide the constant GET /api/jobs and /api/projects/.../issues polling from the access log.
        _NOISY = ("/api/jobs", "/api/projects")

        def filter(self, record: logging.LogRecord) -> bool:
            msg = record.getMessage()
            return not ('"GET ' in msg and any(p in msg for p in self._NOISY))

    logging.getLogger("uvicorn.access").addFilter(_DropPolling())


_setup_logging()
settings.ensure_dirs()
db = Database(settings.db_path)

# Jobs left 'running'/'queued' by a previous process are dead — mark them interrupted so they
# don't block dedup and can be resumed by clicking Fix it again.
_interrupted = db.mark_stale_jobs_interrupted()
if _interrupted:
    logging.getLogger("sbf.app").info("Marked %d stale job(s) as interrupted (resume by re-running).", _interrupted)

app = FastAPI(title="SentryBugFixer")
_STATIC = Path(__file__).resolve().parent / "static"


class ProjectIn(BaseModel):
    name: str
    gitlab_url: str = ""
    github_url: str = ""
    sentry_url: str
    default_branch: str = "main"

    @model_validator(mode="after")
    def check_repo_url(self):
        if not self.gitlab_url and not self.github_url:
            raise ValueError("Either gitlab_url or github_url must be provided")
        return self


class FixIn(BaseModel):
    issue_id: str
    instructions: str = ""


def _start_fix(project: dict, issue_id: str, issue_title: str = "", instructions: str = "") -> dict:
    # Dedup: if a fix for this issue is already queued/running, return it instead of starting another.
    existing = db.find_active_job(project["id"], str(issue_id))
    if existing:
        return existing
    job_id = uuid.uuid4().hex[:12]
    db.create_job(job_id, project["id"], issue_id, issue_title, instructions)
    thread = threading.Thread(target=run_fix_job, args=(db, job_id, project, issue_id, instructions), daemon=True)
    thread.start()
    return db.get_job(job_id)


def _get_pr_state(project: dict, branch: str) -> str | None:
    """Return PR/MR state for the branch, dispatching to GitHub or GitLab."""
    if project.get("github_url"):
        return github_client.get_pr_state(project["github_url"], branch)
    return gitlab_client.get_mr_state(project.get("gitlab_url", ""), branch)


def _issue_action(project: dict, issue_id: str) -> dict:
    """Decide what the dashboard should offer for an issue: review | running | resume | fix."""
    mr_url = db.last_mr_url_for_issue(project["id"], issue_id)
    if mr_url:
        state = _get_pr_state(project, branch_name(issue_id))
        if state in ("closed", "merged"):
            # PR/MR is finished → close (resolve) the Sentry issue so it drops off the list.
            sentry_client.resolve_issue(issue_id)
            return {"action": "resolved", "mr_url": mr_url}
        if state in ("opened", "open", "locked"):
            return {"action": "review", "mr_url": mr_url}

    latest = db.latest_job_for_issue(project["id"], issue_id)
    if latest and latest["status"] in ("queued", "running"):
        return {"action": "running", "job_id": latest["id"]}

    transcript = settings.repos_dir / f"{project['id']}-{issue_id}.transcript.json"
    if transcript.exists():
        return {"action": "resume", "job_id": latest["id"] if latest else None}
    return {"action": "fix"}


# --- Projects ---
@app.get("/api/projects")
def get_projects():
    return db.list_projects()


@app.post("/api/projects")
def create_project(p: ProjectIn):
    return db.add_project(p.name, p.gitlab_url, p.sentry_url, p.default_branch, p.github_url)


@app.put("/api/projects/{project_id}")
def edit_project(project_id: int, p: ProjectIn):
    if not db.get_project(project_id):
        raise HTTPException(404, "project not found")
    return db.update_project(
        project_id,
        name=p.name,
        gitlab_url=p.gitlab_url,
        github_url=p.github_url,
        sentry_url=p.sentry_url,
        default_branch=p.default_branch,
    )


@app.delete("/api/projects/{project_id}")
def delete_project(project_id: int):
    db.delete_project(project_id)
    return {"ok": True}


@app.get("/api/projects/{project_id}/issues")
def project_issues(project_id: int, query: str = "is:unresolved"):
    project = db.get_project(project_id)
    if not project:
        raise HTTPException(404, "project not found")
    try:
        issues = sentry_client.list_issues(project["sentry_url"], query=query)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, str(e))
    out = []
    for it in issues:
        info = _issue_action(project, it["id"])
        if info["action"] == "resolved":
            continue  # MR finished → issue resolved; drop it from the list
        out.append({**it, **info})
    return out


# --- Fixing ---
@app.post("/api/projects/{project_id}/fix")
def fix_issue(project_id: int, body: FixIn):
    project = db.get_project(project_id)
    if not project:
        raise HTTPException(404, "project not found")
    return _start_fix(project, body.issue_id, instructions=body.instructions)


@app.get("/api/jobs")
def jobs():
    return db.list_jobs()


@app.get("/api/projects/{project_id}/issues/{issue_id}/jobs")
def issue_history(project_id: int, issue_id: str):
    """Full fix history for one issue (each attempt's status, tokens, cost, MR)."""
    jobs_ = db.jobs_for_issue(project_id, issue_id)
    total_cost = round(sum(j.get("cost_usd", 0) or 0 for j in jobs_), 6)
    total_in = sum(j.get("input_tokens", 0) or 0 for j in jobs_)
    total_out = sum(j.get("output_tokens", 0) or 0 for j in jobs_)
    return {"jobs": jobs_, "total_cost_usd": total_cost, "total_input_tokens": total_in, "total_output_tokens": total_out}


@app.get("/health")
def health():
    """Liveness probe for Docker / load balancers."""
    return {"status": "ok", "version": __version__}


@app.get("/api/models")
def models():
    """List of supported models with USD prices per 1M tokens, and the one currently in use."""
    return {"current": settings.model, "models": pricing.all_models()}


@app.get("/api/jobs/{job_id}")
def job(job_id: str):
    j = db.get_job(job_id)
    if not j:
        raise HTTPException(404, "job not found")
    return j


@app.websocket("/ws/jobs/{job_id}")
async def ws_job(ws: WebSocket, job_id: str):
    """Stream a job's log live. Sends past log lines first, then new ones as they happen."""
    await ws.accept()
    broker.set_loop(asyncio.get_running_loop())

    job_row = db.get_job(job_id)
    if not job_row:
        await ws.send_json({"type": "error", "message": "job not found"})
        await ws.close()
        return

    # Replay history so a late-joining popup sees everything.
    for line in (job_row["log"] or "").splitlines():
        await ws.send_json({"type": "log", "line": line})
    await ws.send_json({"type": "status", "status": job_row["status"], "mr_url": job_row["mr_url"]})

    queue = broker.subscribe(job_id)
    try:
        while True:
            data = await queue.get()
            await ws.send_text(data)
    except WebSocketDisconnect:
        pass
    finally:
        broker.unsubscribe(job_id, queue)


# --- Sentry webhook ---
@app.post("/webhook/sentry")
async def sentry_webhook(request: Request, background: BackgroundTasks):
    if settings.sentry_webhook_secret:
        sig = request.headers.get("sentry-hook-signature", "")
        if sig != settings.sentry_webhook_secret:
            raise HTTPException(401, "invalid webhook signature")

    payload = await request.json()
    parsed = sentry_client.parse_webhook(payload)
    if not parsed:
        return {"ok": False, "reason": "no issue in payload"}

    # Match the webhook to a configured project by Sentry project slug, else fall back to the only project.
    projects = db.list_projects()
    target = None
    slug = parsed.get("project_slug")
    if slug:
        for p in projects:
            try:
                _, proj_slug = sentry_client.parse_sentry_url(p["sentry_url"])
            except Exception:  # noqa: BLE001
                continue
            if proj_slug == slug:
                target = p
                break
    if target is None and len(projects) == 1:
        target = projects[0]
    if target is None:
        return {"ok": False, "reason": "no matching project configured"}

    job = _start_fix(target, parsed["issue_id"], parsed.get("title", ""))
    return {"ok": True, "job_id": job["id"]}


# --- Dashboard (mounted last so it doesn't shadow /api) ---
@app.get("/")
def index():
    return FileResponse(_STATIC / "index.html")


app.mount("/static", StaticFiles(directory=_STATIC), name="static")
