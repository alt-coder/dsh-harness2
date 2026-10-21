"""Run one non-interactive DSH session and read back its NDJSON event stream.

`dsh --profile headless --json -` answers one task read from stdin and exits, emitting one JSON
object per line: `session`, `status`, `text`, `thinking`, `tool_call`, `tool_result`,
`final`. This module is the only place that knows that wire format.

The headless bundle deliberately disables the interactive credential store ("one-shot
jobs use launch-time credentials"), so a rollout resolves its provider key from the
environment, falling back to the managed `$DSH_HOME/.credentials.yaml` refs.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

DSH_PROFILE = os.environ.get("HARNESS2_DSH_PROFILE", "headless")
CREDENTIAL_NAMES = ("OPENCODE_GO_API_KEY", "DEEPSEEK_API_KEY", "ANTHROPIC_API_KEY")
NPX_DSH = ["npx", "--yes", "@deepseek-ai/dsh"]

_overlays: dict[str, str] = {}


def dsh_command() -> list[str]:
    """The dsh launcher: `$HARNESS2_DSH_BIN`, else `dsh` on PATH, else `npx @deepseek-ai/dsh`."""
    configured = os.environ.get("HARNESS2_DSH_BIN", "").strip()
    if configured:
        return [configured] if os.path.isfile(configured) else shlex.split(configured)
    found = shutil.which("dsh")
    return [found] if found else list(NPX_DSH)


def model_overlay() -> list[str]:
    """`--patch` arguments that pin the headless model, when one is requested.

    `HARNESS2_DSH_PROVIDER` and `HARNESS2_DSH_MODEL` select a provider route and model
    for every rollout and editor session; unset, the headless profile's own default
    model applies. The overlay is a one-row loader patch written once per process.
    """
    provider = os.environ.get("HARNESS2_DSH_PROVIDER", "").strip()
    model = os.environ.get("HARNESS2_DSH_MODEL", "").strip()
    if not provider and not model:
        return []
    if not (provider and model):
        raise ValueError("HARNESS2_DSH_PROVIDER and HARNESS2_DSH_MODEL must be set together")
    # JSON strings are valid YAML scalars, so json.dumps quotes them safely.
    body = (
        "- id: agent-default-model\n"
        "  name: '@deepseek-ai/dsh-agent-default-model'\n"
        "  config:\n"
        f"    provider: {json.dumps(provider)}\n"
        f"    model: {json.dumps(model)}\n"
    )
    path = _overlays.get(body)
    if path is None or not os.path.isfile(path):
        fd, path = tempfile.mkstemp(prefix="harness2-dsh-model-", suffix=".yml")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(body)
        _overlays[body] = path
    return ["--patch", path]


def _dsh_home() -> Path:
    return Path(os.environ.get("DSH_HOME") or (Path.home() / ".dsh"))


def _managed_refs(path: Path | None = None) -> dict[str, str]:
    """Read the `refs:` block of the managed credential document.

    Values are returned but never logged; a missing or malformed file yields {}.
    """
    path = path or (_dsh_home() / ".credentials.yaml")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    refs: dict[str, str] = {}
    in_refs = False
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if re.match(r"^refs:\s*$", raw):
            in_refs = True
            continue
        if in_refs and re.match(r"^\S", raw):
            in_refs = False
        if not in_refs:
            continue
        m = re.match(r"^\s+([A-Za-z_][A-Za-z0-9_]*):\s*(.+?)\s*$", raw)
        if not m:
            continue
        value = m.group(2)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        refs[m.group(1)] = value
    return refs


def headless_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Environment for a headless run: process env first, managed refs as fallback."""
    env = dict(os.environ)
    refs = _managed_refs()
    for name in CREDENTIAL_NAMES:
        if not env.get(name) and refs.get(name):
            env[name] = refs[name]
    env.setdefault("DSH_NO_UPDATE_CHECK", "1")
    if extra:
        env.update(extra)
    return env


@dataclass
class HeadlessResult:
    """One finished (or failed) headless session."""

    text: str = ""
    events: list[dict] = field(default_factory=list)
    rc: int = 0
    stderr: str = ""
    session_id: str = ""

    @property
    def ok(self) -> bool:
        return self.rc == 0 and bool(self.text.strip())

    def tools(self) -> list[tuple[str, dict, str]]:
        """(tool name, input, result status) for every tool call in the session."""
        calls: dict[str, tuple[str, dict]] = {}
        out: list[tuple[str, dict, str]] = []
        for event in self.events:
            if event.get("type") == "tool_call":
                calls[str(event.get("callId"))] = (str(event.get("tool")), event.get("input") or {})
            elif event.get("type") == "tool_result":
                name, args = calls.get(str(event.get("callId")), ("", {}))
                out.append((name, args, str(event.get("status") or "")))
        return out


def run_headless(
    task: str,
    cwd: Path,
    *,
    timeout: int = 900,
    profile: str | None = None,
    env: dict[str, str] | None = None,
    thread: bool = False,
) -> HeadlessResult:
    """Answer `task` in `cwd` through one headless DSH session.

    The task travels on stdin (`-`) so long missions are not argv-limited.
    """
    launcher = dsh_command()
    cmd = [*launcher, "--profile", profile or DSH_PROFILE, *model_overlay(), "--json", "-"]
    try:
        proc = subprocess.run(
            cmd,
            input=task,
            cwd=str(cwd),
            env=env or headless_env(),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return HeadlessResult(rc=124, stderr=f"timeout after {timeout}s: {exc}")
    except FileNotFoundError:
        return HeadlessResult(
            rc=127,
            stderr=f"{launcher[0]!r} not found; install dsh or set HARNESS2_DSH_BIN",
        )
    events: list[dict] = []
    text = ""
    session_id = ""
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        events.append(event)
        kind = event.get("type")
        if kind == "session":
            session_id = str(event.get("sessionId") or "")
        elif kind == "final":
            text = str(event.get("text") or "")
        elif kind == "text":
            text = str(event.get("text") or text)
    return HeadlessResult(
        text=text,
        events=events,
        rc=proc.returncode,
        stderr=(proc.stderr or "")[-2000:],
        session_id=session_id,
    )


def render_trajectory(result: HeadlessResult, *, obs_trunc: int = 50_000) -> str:
    """Render the event stream in Harness2's shared [ACTION]/[OBSERVATION] grammar."""
    lines: list[str] = []
    results = {
        str(e.get("callId")): e for e in result.events if e.get("type") == "tool_result"
    }
    for event in result.events:
        kind = event.get("type")
        if kind == "tool_call":
            args = json.dumps(event.get("input") or {}, ensure_ascii=False)
            lines.append(f"[ACTION] {event.get('tool')} {args}")
            res = results.get(str(event.get("callId")))
            if res is not None:
                body = _stringify(res.get("result"))
                if len(body) > obs_trunc:
                    body = body[:obs_trunc] + f"\n[... {len(body) - obs_trunc} chars truncated]"
                status = res.get("status")
                marker = "[OBSERVATION]" if status in (None, "ok", "success") else f"[ERROR:{status}]"
                lines.append(f"{marker}\n{body}")
        elif kind == "text" and event.get("text"):
            lines.append(f"[ASSISTANT] {event.get('text')}")
    if result.stderr.strip():
        lines.append(f"[ERROR] {result.stderr.strip()[:2000]}")
    return "\n".join(lines) + ("\n" if lines else "")


def _stringify(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        return str(value)


__all__ = [
    "HeadlessResult",
    "dsh_command",
    "model_overlay",
    "headless_env",
    "render_trajectory",
    "run_headless",
]
