#!/usr/bin/env python3
"""Isolate where SentryBugFixer hangs. Run: ./.venv/bin/python selftest.py

Checks, in order:
  1. Is ANTHROPIC_API_KEY set?
  2. Can we reach the Anthropic API at all (cheap call, 60s timeout)?
  3. Does the configured model + adaptive thinking + effort work?
Each step prints PASS/FAIL with the actual error, so you see exactly what's wrong.
"""

import sys
import time

from sentrybugfixer.config import settings


def main() -> int:
    print(f"model   = {settings.model}")
    print(f"effort  = {settings.effort}")
    print(f"timeout = {settings.request_timeout}s\n")

    if not settings.anthropic_api_key:
        print("FAIL: ANTHROPIC_API_KEY is empty. Set it in .env (next to dev.sh).")
        return 1
    print(f"PASS: ANTHROPIC_API_KEY is set (…{settings.anthropic_api_key[-4:]})")

    import anthropic

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key, timeout=60.0, max_retries=0)

    # Step 1: barest possible call — does the network/auth/model work at all?
    print("\n→ Step 1: minimal call (no tools, no thinking) …")
    t = time.time()
    try:
        r = client.messages.create(
            model=settings.model,
            max_tokens=16,
            messages=[{"role": "user", "content": "Reply with the word OK."}],
        )
        print(f"PASS in {time.time() - t:.1f}s: {r.content[0].text!r}")
    except anthropic.APIStatusError as e:
        print(f"FAIL ({time.time() - t:.1f}s): HTTP {e.status_code} — {e.message}")
        _hint(e.status_code)
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"FAIL ({time.time() - t:.1f}s): {type(e).__name__} — {e}")
        print("  → A timeout/connection error here means the API is unreachable from this machine")
        print("    (network, firewall, or proxy). Set ANTHROPIC_BASE_URL / HTTPS_PROXY if you use one.")
        return 1

    # Step 2: the exact call the agent makes (tools + adaptive thinking + effort).
    print("\n→ Step 2: full agent-style call (tools + adaptive thinking + effort) …")
    t = time.time()
    try:
        r = client.messages.create(
            model=settings.model,
            max_tokens=1024,
            system="You are a test.",
            tools=[{"name": "noop", "description": "does nothing",
                    "input_schema": {"type": "object", "properties": {}}}],
            thinking={"type": "adaptive"},
            output_config={"effort": settings.effort},
            messages=[{"role": "user", "content": "Say OK, do not call the tool."}],
        )
        print(f"PASS in {time.time() - t:.1f}s: stop_reason={r.stop_reason}")
        print("\nAll checks passed — the agent's API path works. If the app still hangs,")
        print("the issue is downstream (git clone or GitLab). Check the job log in the dashboard.")
        return 0
    except anthropic.APIStatusError as e:
        print(f"FAIL ({time.time() - t:.1f}s): HTTP {e.status_code} — {e.message}")
        print("  → Step 1 worked but this didn't, so it's 'output_config'/'thinking' or the effort value.")
        print("    Try SBF_EFFORT=medium, or remove thinking/effort from agent.py.")
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"FAIL ({time.time() - t:.1f}s): {type(e).__name__} — {e}")
        return 1


def _hint(code: int) -> None:
    hints = {
        401: "  → Bad/expired ANTHROPIC_API_KEY.",
        403: "  → Key lacks permission for this model.",
        404: f"  → Model {settings.model!r} not available on this key. Try SBF_MODEL=claude-sonnet-4-6.",
        429: "  → Rate limited. Wait and retry.",
    }
    print(hints.get(code, "  → See the message above."))


if __name__ == "__main__":
    sys.exit(main())
