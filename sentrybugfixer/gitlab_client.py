"""GitLab helpers: parse repo URLs, clone with a token, commit/push, open a merge request."""

import shutil
import subprocess
import urllib.parse
from pathlib import Path

import requests

from .config import settings


def parse_gitlab_url(url: str) -> tuple[str, str]:
    """Return (host, project_path) from a GitLab repo URL.

    Accepts https://gitlab.com/group/subgroup/project(.git) or git@gitlab.com:group/project.git
    """
    url = url.strip()
    if url.startswith("git@"):
        host_part, path = url[4:].split(":", 1)
        host = f"https://{host_part}"
    else:
        parsed = urllib.parse.urlparse(url)
        host = f"{parsed.scheme}://{parsed.netloc}"
        path = parsed.path.lstrip("/")
    path = path.removesuffix(".git").strip("/")
    if not path or "/" not in path:
        msg = f"Could not parse a group/project path from GitLab URL {url!r}."
        raise ValueError(msg)
    return host, path


def _authenticated_url(host: str, project_path: str) -> str:
    if not settings.gitlab_token:
        raise RuntimeError("GITLAB_TOKEN is not set.")
    netloc = host.split("://", 1)[1]
    return f"https://oauth2:{settings.gitlab_token}@{netloc}/{project_path}.git"


def _run(args: list[str], cwd: Path | None = None) -> str:
    proc = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if proc.returncode != 0:
        msg = f"Command failed: {' '.join(args)}\n{proc.stdout}\n{proc.stderr}"
        raise RuntimeError(msg)
    return proc.stdout.strip()


def clone(gitlab_url: str, dest: Path, branch: str | None = None) -> Path:
    host, project_path = parse_gitlab_url(gitlab_url)
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    args = ["git", "clone", "--depth", "50"]
    if branch:
        args += ["--branch", branch]
    args += [_authenticated_url(host, project_path), str(dest)]
    _run(args)
    # Identify the committer for the autonomous fix.
    _run(["git", "config", "user.email", "sentry-bug-fixer@local"], cwd=dest)
    _run(["git", "config", "user.name", "SentryBugFixer"], cwd=dest)
    # Autonomous commits must never be signed: any global commit-signing setup (e.g. 1Password's
    # SSH signer) can't run in this non-interactive context and would fail the commit.
    _run(["git", "config", "commit.gpgsign", "false"], cwd=dest)
    _run(["git", "config", "tag.gpgsign", "false"], cwd=dest)
    return dest


def has_changes(repo: Path) -> bool:
    return bool(_run(["git", "status", "--porcelain"], cwd=repo))


def commit_and_push(repo: Path, branch: str, message: str) -> None:
    _run(["git", "checkout", "-B", branch], cwd=repo)
    _run(["git", "add", "-A"], cwd=repo)
    # `-c commit.gpgsign=false` belt-and-suspenders in case global config still forces signing.
    _run(["git", "-c", "commit.gpgsign=false", "commit", "--no-gpg-sign", "-m", message], cwd=repo)
    _run(["git", "push", "-u", "origin", branch, "--force-with-lease"], cwd=repo)


def open_merge_request(
    gitlab_url: str, source_branch: str, target_branch: str, title: str, description: str = ""
) -> str:
    host, project_path = parse_gitlab_url(gitlab_url)
    api_host = host if host.startswith("http") else settings.gitlab_host
    encoded = urllib.parse.quote(project_path, safe="")
    resp = requests.post(
        f"{api_host}/api/v4/projects/{encoded}/merge_requests",
        headers={"PRIVATE-TOKEN": settings.gitlab_token},
        json={
            "source_branch": source_branch,
            "target_branch": target_branch,
            "title": title,
            "description": description,
            "remove_source_branch": True,
        },
        timeout=30,
    )
    if resp.status_code == 409:  # MR already exists for this source branch
        existing = requests.get(
            f"{api_host}/api/v4/projects/{encoded}/merge_requests",
            headers={"PRIVATE-TOKEN": settings.gitlab_token},
            params={"source_branch": source_branch, "state": "opened"},
            timeout=30,
        )
        existing.raise_for_status()
        items = existing.json()
        if items:
            return items[0]["web_url"]
    resp.raise_for_status()
    return resp.json()["web_url"]


def get_mr_state(gitlab_url: str, source_branch: str) -> str | None:
    """Return the most recent MR's state for a source branch: opened | closed | merged | locked.

    Returns None if there's no MR (or the lookup fails).
    """
    if not settings.gitlab_token:
        return None
    host, project_path = parse_gitlab_url(gitlab_url)
    api_host = host if host.startswith("http") else settings.gitlab_host
    encoded = urllib.parse.quote(project_path, safe="")
    try:
        resp = requests.get(
            f"{api_host}/api/v4/projects/{encoded}/merge_requests",
            headers={"PRIVATE-TOKEN": settings.gitlab_token},
            params={"source_branch": source_branch, "order_by": "updated_at", "per_page": 1},
            timeout=30,
        )
        if not resp.ok:
            return None
        items = resp.json()
        return items[0]["state"] if items else None
    except requests.RequestException:
        return None
