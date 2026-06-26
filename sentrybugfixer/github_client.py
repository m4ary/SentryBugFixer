"""GitHub helpers: parse repo URLs, clone with a token, commit/push, open a pull request."""

import shutil
import subprocess
import urllib.parse
from pathlib import Path

import requests

from .config import settings


def parse_github_url(url: str) -> tuple[str, str]:
    """Return (host, owner/repo) from a GitHub repo URL.

    Accepts https://github.com/owner/repo(.git) or git@github.com:owner/repo.git
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
        msg = f"Could not parse an owner/repo path from GitHub URL {url!r}."
        raise ValueError(msg)
    return host, path


def _authenticated_url(host: str, repo_path: str) -> str:
    if not settings.github_token:
        raise RuntimeError("GITHUB_TOKEN is not set.")
    netloc = host.split("://", 1)[1]
    return f"https://x-access-token:{settings.github_token}@{netloc}/{repo_path}.git"


def _api_base(host: str, repo_path: str) -> str:
    if host.rstrip("/") == "https://github.com":
        return f"https://api.github.com/repos/{repo_path}"
    # GitHub Enterprise Server
    return f"{host.rstrip('/')}/api/v3/repos/{repo_path}"


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {settings.github_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _run(args: list[str], cwd: Path | None = None) -> str:
    proc = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if proc.returncode != 0:
        msg = f"Command failed: {' '.join(args)}\n{proc.stdout}\n{proc.stderr}"
        raise RuntimeError(msg)
    return proc.stdout.strip()


def clone(github_url: str, dest: Path, branch: str | None = None) -> Path:
    host, repo_path = parse_github_url(github_url)
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    args = ["git", "clone", "--depth", "50"]
    if branch:
        args += ["--branch", branch]
    args += [_authenticated_url(host, repo_path), str(dest)]
    _run(args)
    _run(["git", "config", "user.email", "sentry-bug-fixer@local"], cwd=dest)
    _run(["git", "config", "user.name", "SentryBugFixer"], cwd=dest)
    _run(["git", "config", "commit.gpgsign", "false"], cwd=dest)
    _run(["git", "config", "tag.gpgsign", "false"], cwd=dest)
    return dest


def has_changes(repo: Path) -> bool:
    return bool(_run(["git", "status", "--porcelain"], cwd=repo))


def commit_and_push(repo: Path, branch: str, message: str) -> None:
    _run(["git", "checkout", "-B", branch], cwd=repo)
    _run(["git", "add", "-A"], cwd=repo)
    _run(["git", "-c", "commit.gpgsign=false", "commit", "--no-gpg-sign", "-m", message], cwd=repo)
    _run(["git", "push", "-u", "origin", branch, "--force-with-lease"], cwd=repo)


def open_pull_request(
    github_url: str, source_branch: str, target_branch: str, title: str, description: str = ""
) -> str:
    host, repo_path = parse_github_url(github_url)
    base = _api_base(host, repo_path)
    owner = repo_path.split("/")[0]
    resp = requests.post(
        f"{base}/pulls",
        headers=_headers(),
        json={
            "head": source_branch,
            "base": target_branch,
            "title": title,
            "body": description,
        },
        timeout=30,
    )
    if resp.status_code == 422:  # PR already exists for this branch
        existing = requests.get(
            f"{base}/pulls",
            headers=_headers(),
            params={"head": f"{owner}:{source_branch}", "state": "open", "per_page": 1},
            timeout=30,
        )
        existing.raise_for_status()
        items = existing.json()
        if items:
            return items[0]["html_url"]
    resp.raise_for_status()
    return resp.json()["html_url"]


def get_pr_state(github_url: str, source_branch: str) -> str | None:
    """Return the PR state for a source branch: open | closed | merged.

    Returns None if there's no PR (or the lookup fails).
    """
    if not settings.github_token:
        return None
    host, repo_path = parse_github_url(github_url)
    base = _api_base(host, repo_path)
    owner = repo_path.split("/")[0]
    try:
        resp = requests.get(
            f"{base}/pulls",
            headers=_headers(),
            params={"head": f"{owner}:{source_branch}", "state": "all", "per_page": 1},
            timeout=30,
        )
        if not resp.ok:
            return None
        items = resp.json()
        if not items:
            return None
        pr = items[0]
        if pr.get("merged_at"):
            return "merged"
        return pr["state"]  # "open" or "closed"
    except requests.RequestException:
        return None
