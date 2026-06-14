"""Sentry REST client: parse project URLs, list issues, fetch an issue + stack trace, parse webhooks."""

import re
from dataclasses import dataclass

import requests

from .config import settings


def _headers() -> dict:
    if not settings.sentry_token:
        raise RuntimeError("SENTRY_AUTH_TOKEN is not set.")
    return {"Authorization": f"Bearer {settings.sentry_token}"}


def parse_sentry_url(url: str) -> tuple[str, str]:
    """Return (org_slug, project_slug) from a Sentry project URL.

    Accepts:
      https://my-org.sentry.io/projects/my-project/
      https://sentry.io/organizations/my-org/projects/my-project/
      my-org/my-project
    """
    url = url.strip().rstrip("/")
    if "://" not in url and url.count("/") == 1:
        org, project = url.split("/")
        return org, project

    org = None
    m = re.search(r"/organizations/([^/]+)", url)
    if m:
        org = m.group(1)
    else:
        m = re.search(r"https?://([^.]+)\.sentry\.io", url)
        if m and m.group(1) not in ("www", "sentry"):
            org = m.group(1)

    pm = re.search(r"/projects/([^/]+)", url)
    project = pm.group(1) if pm else None

    if not org or not project:
        msg = f"Could not parse org/project from Sentry URL {url!r}. Use e.g. https://my-org.sentry.io/projects/my-project/"
        raise ValueError(msg)
    return org, project


def parse_issue_id(issue: str) -> str:
    issue = str(issue).strip()
    if issue.isdigit():
        return issue
    m = re.search(r"/issues/(\d+)", issue)
    if m:
        return m.group(1)
    msg = f"Could not parse a Sentry issue id from {issue!r}."
    raise ValueError(msg)


@dataclass
class SentryIssue:
    id: str
    short_id: str
    title: str
    culprit: str
    level: str
    permalink: str
    count: str
    raw: dict
    event: dict | None = None

    def to_task(self) -> str:
        lines = [
            f"# Sentry issue {self.short_id or self.id}: {self.title}",
            "",
            f"- Permalink: {self.permalink}" if self.permalink else "",
            f"- Culprit: {self.culprit}" if self.culprit else "",
            f"- Level: {self.level}" if self.level else "",
            f"- Events seen: {self.count}" if self.count else "",
            "",
        ]
        meta = self.raw.get("metadata") or {}
        if meta.get("value"):
            lines += ["## Error", "", f"{meta.get('type', '')}: {meta['value']}".strip(), ""]
        trace = _format_event(self.event)
        if trace:
            lines += ["## Latest event (stack trace)", "", trace]
        return "\n".join(line for line in lines if line is not None)


def _format_event(event: dict | None) -> str:
    if not event:
        return ""
    parts: list[str] = []
    for entry in event.get("entries", []):
        if entry.get("type") != "exception":
            continue
        for value in entry.get("data", {}).get("values", []):
            parts.append(f"{value.get('type', 'Exception')}: {value.get('value', '')}".strip())
            for frame in (value.get("stacktrace") or {}).get("frames") or []:
                location = frame.get("filename") or frame.get("module") or "<unknown>"
                loc = f"{location}:{frame['lineNo']}" if frame.get("lineNo") else location
                parts.append(f"  File {loc}, in {frame.get('function', '<unknown>')}")
                if frame.get("context_line"):
                    parts.append(f"    {frame['context_line'].strip()}")
    return "\n".join(parts)


def list_issues(sentry_url: str, query: str = "is:unresolved", limit: int = 25) -> list[dict]:
    org, project = parse_sentry_url(sentry_url)
    resp = requests.get(
        f"{settings.sentry_host}/api/0/projects/{org}/{project}/issues/",
        headers=_headers(),
        params={"query": query, "limit": limit, "statsPeriod": "14d"},
        timeout=30,
    )
    resp.raise_for_status()
    out = []
    for it in resp.json():
        out.append(
            {
                "id": str(it.get("id")),
                "shortId": it.get("shortId", ""),
                "title": it.get("title", ""),
                "culprit": it.get("culprit", ""),
                "level": it.get("level", ""),
                "count": str(it.get("count", "")),
                "permalink": it.get("permalink", ""),
                "lastSeen": it.get("lastSeen", ""),
            }
        )
    return out


def fetch_issue(issue: str) -> SentryIssue:
    issue_id = parse_issue_id(issue)
    h = _headers()
    resp = requests.get(f"{settings.sentry_host}/api/0/issues/{issue_id}/", headers=h, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    event = None
    ev = requests.get(f"{settings.sentry_host}/api/0/issues/{issue_id}/events/latest/", headers=h, timeout=30)
    if ev.ok:
        event = ev.json()

    return SentryIssue(
        id=str(data.get("id", issue_id)),
        short_id=data.get("shortId", ""),
        title=data.get("title", ""),
        culprit=data.get("culprit", ""),
        level=data.get("level", ""),
        permalink=data.get("permalink", ""),
        count=str(data.get("count", "")),
        raw=data,
        event=event,
    )


def resolve_issue(issue: str) -> bool:
    """Mark a Sentry issue resolved (close it). Best-effort; returns True on success."""
    try:
        issue_id = parse_issue_id(issue)
        resp = requests.put(
            f"{settings.sentry_host}/api/0/issues/{issue_id}/",
            headers=_headers(),
            json={"status": "resolved"},
            timeout=30,
        )
        return resp.ok
    except (requests.RequestException, ValueError, RuntimeError):
        return False


def parse_webhook(payload: dict) -> dict | None:
    """Extract issue id/title and a project hint from a Sentry webhook payload.

    Handles the common shapes (issue alerts and the official Sentry integration's
    `event_alert` / `issue` resources). Returns None if no issue can be found.
    """
    data = payload.get("data", payload)
    issue = data.get("issue") or {}
    event = data.get("event") or {}

    issue_id = issue.get("id")
    if not issue_id and event:
        issue_id = event.get("issue_id") or event.get("groupID") or event.get("issue_id")
    if not issue_id and payload.get("id"):
        issue_id = payload.get("id")
    if not issue_id:
        return None

    return {
        "issue_id": str(issue_id),
        "title": issue.get("title") or event.get("title") or "",
        "project_slug": issue.get("project", {}).get("slug")
        if isinstance(issue.get("project"), dict)
        else (event.get("project") or payload.get("project")),
        "web_url": issue.get("web_url") or issue.get("permalink") or "",
    }
