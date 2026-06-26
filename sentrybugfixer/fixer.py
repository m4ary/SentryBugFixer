"""Orchestrates a single fix: fetch issue -> clone -> agent fix -> branch/commit/push -> open MR."""

import logging
import re
import traceback

from . import github_client, gitlab_client, pricing, sentry_client
from .agent import run_agent
from .config import settings
from .db import Database
from .events import broker

logger = logging.getLogger("sbf.fix")


def branch_name(issue_id: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9._-]+", "-", str(issue_id)).strip("-")
    return f"issue/{safe or 'unknown'}"


def run_fix_job(db: Database, job_id: str, project: dict, issue_id: str, instructions: str = "") -> None:
    """Run end-to-end. Updates the job row in the DB as it progresses (meant to run in a thread)."""

    def log(line: str) -> None:
        logger.info("[job %s] %s", job_id, line)        # stream to console
        db.append_log(job_id, line)                     # persist for the dashboard
        broker.publish(job_id, {"type": "log", "line": line})  # live to WebSocket clients

    def set_status(status: str, **extra) -> None:
        db.update_job(job_id, status=status, **extra)
        broker.publish(job_id, {"type": "status", "status": status, **extra})

    usage = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}

    def record_usage() -> float:
        cost = pricing.cost_usd(
            settings.model, usage["input"], usage["output"], usage["cache_read"], usage["cache_write"]
        )
        db.update_job(
            job_id,
            model=settings.model,
            input_tokens=usage["input"],
            output_tokens=usage["output"],
            cache_read_tokens=usage["cache_read"],
            cache_write_tokens=usage["cache_write"],
            cost_usd=round(cost, 6),
        )
        log(
            f"Usage: in={usage['input']} out={usage['output']} "
            f"(cache read={usage['cache_read']} write={usage['cache_write']}) — cost ≈ ${cost:.4f}"
        )
        return cost

    try:
        set_status("running")
        log(f"Fetching Sentry issue {issue_id} ...")
        issue = sentry_client.fetch_issue(issue_id)
        db.update_job(job_id, issue_title=issue.title)
        log(f"Issue: {issue.short_id or issue.id} — {issue.title}")

        repo_dir = settings.repos_dir / f"{project['id']}-{issue.id}"
        # Transcript lives OUTSIDE the repo so it isn't committed; its presence means a prior
        # attempt was interrupted and can be resumed (reusing the existing clone + edits).
        transcript = settings.repos_dir / f"{project['id']}-{issue.id}.transcript.json"

        use_github = bool(project.get("github_url"))
        repo_url = project["github_url"] if use_github else project["gitlab_url"]
        git = github_client if use_github else gitlab_client

        if repo_dir.exists() and transcript.exists():
            log("Resuming previous attempt (reusing clone, edits and transcript) ...")
            repo = repo_dir
        else:
            log(f"Cloning {repo_url} (branch {project['default_branch']}) ...")
            repo = git.clone(repo_url, repo_dir, branch=project["default_branch"])

        task = issue.to_task()
        if instructions:
            task += f"\n\n## Additional instructions from the user\n{instructions}"
            log(f"User instructions: {instructions}")
        log("Running the bug-fixing agent ...")
        summary = run_agent(task, repo, on_log=log, transcript_path=transcript, usage=usage)
        cost = record_usage()

        if not git.has_changes(repo):
            log("Agent made no changes — nothing to submit.")
            transcript.unlink(missing_ok=True)
            set_status("no_changes", cost_usd=round(cost, 6))
            return

        branch = branch_name(issue.id)
        message = f"Fix {issue.short_id or issue.id}: {issue.title}\n\nAuto-fixed from Sentry issue {issue.permalink}"
        log(f"Committing and pushing branch {branch} ...")
        git.commit_and_push(repo, branch, message)

        if use_github:
            log("Opening GitHub pull request ...")
            description = f"Automated fix for Sentry issue [{issue.short_id or issue.id}]({issue.permalink}).\n\n{summary}"
            mr_url = github_client.open_pull_request(
                repo_url,
                source_branch=branch,
                target_branch=project["default_branch"],
                title=f"Fix {issue.short_id or issue.id}: {issue.title}",
                description=description,
            )
            log(f"Pull request opened: {mr_url}")
        else:
            log("Opening GitLab merge request ...")
            description = f"Automated fix for Sentry issue [{issue.short_id or issue.id}]({issue.permalink}).\n\n{summary}"
            mr_url = gitlab_client.open_merge_request(
                repo_url,
                source_branch=branch,
                target_branch=project["default_branch"],
                title=f"Fix {issue.short_id or issue.id}: {issue.title}",
                description=description,
            )
            log(f"Merge request opened: {mr_url}")
        transcript.unlink(missing_ok=True)  # completed cleanly — a future re-fix starts fresh
        set_status("success", mr_url=mr_url, cost_usd=round(cost, 6))
    except Exception as e:  # noqa: BLE001 - surface any failure to the dashboard
        log(f"ERROR: {e}")
        log(traceback.format_exc())
        record_usage()  # tokens were still spent before the failure — record them
        set_status("error")
