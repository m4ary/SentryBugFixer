"""Minimal LLM coding agent.

The design is borrowed from mini-swe-agent: a linear message history and a single
`bash` tool, where every action is run via subprocess in the repository directory.
The agent edits files until the model stops requesting tools (end_turn) or a limit
is hit. Git/branch/MR are handled deterministically by the caller, not the agent.
"""

import json
import subprocess
from collections.abc import Callable
from pathlib import Path

import anthropic

from .config import settings


def _to_dict(block) -> dict:
    """Serialize an SDK content block (or pass through a dict) so it can be persisted and re-sent."""
    if hasattr(block, "model_dump"):
        return block.model_dump(mode="json", exclude_none=True)
    return block


def _load_transcript(path: Path | None) -> list[dict] | None:
    if path and path.exists():
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return None
    return None


def _save_transcript(path: Path | None, messages: list[dict]) -> None:
    if path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(messages))

BASH_TOOL = {
    "name": "bash",
    "description": (
        "Run a bash command in the repository working directory and return its stdout, stderr and exit code. "
        "Use it to read files, search, edit files (sed/cat heredocs), and run tests."
    ),
    "input_schema": {
        "type": "object",
        "properties": {"command": {"type": "string", "description": "The bash command to execute."}},
        "required": ["command"],
    },
}

SYSTEM_PROMPT = (
    "You are an autonomous bug-fixing engineer working inside a cloned git repository. "
    "You can only act through the `bash` tool. Work step by step:\n"
    "1. Explore the repo and locate the code referenced by the Sentry stack trace.\n"
    "2. Reproduce the bug if feasible.\n"
    "3. Make the minimal, correct edit that fixes the root cause.\n"
    "4. Verify by running the relevant tests or a reproduction script.\n"
    "Keep changes focused. Do NOT run git commit/push or open merge requests — that is handled for you. "
    "When the fix is complete and verified, reply with a short summary and DO NOT call the tool again."
)

INSTANCE_TEMPLATE = (
    "Fix the following bug reported by Sentry. The repository is already checked out in your working directory.\n\n"
    "<sentry_report>\n{task}\n</sentry_report>\n\n"
    "Begin by inspecting the repository."
)

MAX_OUTPUT_CHARS = 12000


def run_bash(command: str, cwd: Path) -> str:
    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=settings.bash_timeout,
        )
    except subprocess.TimeoutExpired:
        return f"<timeout after {settings.bash_timeout}s>"
    out = (proc.stdout or "") + (proc.stderr or "")
    if len(out) > MAX_OUTPUT_CHARS:
        head, tail = out[: MAX_OUTPUT_CHARS // 2], out[-MAX_OUTPUT_CHARS // 2 :]
        out = f"{head}\n... [{len(out) - MAX_OUTPUT_CHARS} chars elided] ...\n{tail}"
    return f"<returncode>{proc.returncode}</returncode>\n{out}".strip()


def run_agent(
    task: str,
    repo: Path,
    on_log: Callable[[str], None] | None = None,
    transcript_path: Path | None = None,
    usage: dict | None = None,
) -> str:
    """Run the fix loop. Returns the agent's final text summary.

    If ``transcript_path`` points at a saved transcript from a prior incomplete run, the loop
    resumes from there instead of starting over — saving the tokens already spent. The transcript
    is saved at each clean step boundary (after tool results are appended), so a crash loses at
    most one step.
    """
    if not settings.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set.")

    def log(msg: str) -> None:
        if on_log:
            on_log(msg)

    client = anthropic.Anthropic(
        api_key=settings.anthropic_api_key,
        timeout=settings.request_timeout,  # fail fast instead of hanging on the SDK's 10-min default
        max_retries=2,
    )

    messages = _load_transcript(transcript_path)
    if messages:
        steps_done = sum(1 for m in messages if m.get("role") == "assistant")
        log(f"Resuming from saved transcript — {steps_done} step(s) already completed.")
    else:
        messages = [{"role": "user", "content": INSTANCE_TEMPLATE.format(task=task)}]
        steps_done = 0
    log(f"Using model {settings.model} (effort={settings.effort}), up to {settings.max_steps} steps.")

    final_text = ""
    for step in range(steps_done + 1, settings.max_steps + 1):
        log(f"[step {step}] thinking…")  # printed BEFORE the model call so it never looks frozen
        buf: list[str] = []

        def flush(s=step, b=buf) -> None:
            text = "".join(b).strip()
            b.clear()
            if text:
                log(f"[step {s}] {text}")

        # Stream so the connection stays alive (avoids idle stalls on long thinking/output)
        # and so progress shows up live in the log.
        try:
            with client.messages.stream(
                model=settings.model,
                max_tokens=16000,
                system=SYSTEM_PROMPT,
                tools=[BASH_TOOL],
                thinking={"type": "adaptive"},
                output_config={"effort": settings.effort},
                messages=messages,
            ) as stream:
                for text in stream.text_stream:
                    buf.append(text)
                    if "\n" in text:
                        flush()
                flush()
                resp = stream.get_final_message()
        except anthropic.APIStatusError as e:
            log(f"[step {step}] model error {e.status_code}: {e.message}")
            raise

        u = getattr(resp, "usage", None)
        if u:
            log(f"[step {step}] done (in={u.input_tokens}, out={u.output_tokens})")
            if usage is not None:
                usage["input"] = usage.get("input", 0) + (u.input_tokens or 0)
                usage["output"] = usage.get("output", 0) + (u.output_tokens or 0)
                usage["cache_read"] = usage.get("cache_read", 0) + (getattr(u, "cache_read_input_tokens", 0) or 0)
                usage["cache_write"] = usage.get("cache_write", 0) + (getattr(u, "cache_creation_input_tokens", 0) or 0)
        # Store assistant content as plain dicts so the transcript is JSON-serializable & resumable.
        messages.append({"role": "assistant", "content": [_to_dict(b) for b in resp.content]})

        tool_uses = [b for b in resp.content if getattr(b, "type", None) == "tool_use"]
        text_blocks = [b.text for b in resp.content if getattr(b, "type", None) == "text"]
        if text_blocks:
            final_text = "\n".join(text_blocks)  # already streamed to the log above

        if not tool_uses:  # model is done
            _save_transcript(transcript_path, messages)
            log(f"Agent finished after {step} step(s).")
            break

        results = []
        for tu in tool_uses:
            command = tu.input.get("command", "")
            log(f"[step {step}] $ {command}")
            output = run_bash(command, repo)
            results.append({"type": "tool_result", "tool_use_id": tu.id, "content": output})
        messages.append({"role": "user", "content": results})
        _save_transcript(transcript_path, messages)  # clean boundary: safe to resume here
    else:
        log(f"Reached step limit ({settings.max_steps}).")

    return final_text
