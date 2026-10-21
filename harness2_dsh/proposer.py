"""A Harness2 Proposer whose sessions are DSH headless runs.

`Proposer.call` already owns the hard parts -- copying the mission context into an
isolated directory, retries, the audit trail, harvesting the edited tree. It reaches a
model through one of two transports: `_claude_cli` or `_opencode`. This subclass
replaces the transport and keeps everything else.
"""

from __future__ import annotations

from pathlib import Path

from harness2.core.proposer import Proposer

from .headless import run_headless

EDITOR_POINTER = (
    "Read MISSION.md in this directory and do what it asks. Use your tools to read and "
    "edit the files here in place. When you are done, reply with the output MISSION.md "
    "requests as your final message, and also save that same text verbatim to "
    "output/answer.md."
)


class DshProposer(Proposer):
    """Runs improver/selector sessions through `dsh --profile headless --json`."""

    def _opencode(self, cwd: Path, mission: str, system: str, wall: int) -> tuple[str, float, str]:
        cwd = Path(cwd)
        text = (f"{system}\n\n---\n\n" if system else "") + mission
        (cwd / "MISSION.md").write_text(text, encoding="utf-8")
        (cwd / "output").mkdir(exist_ok=True)

        result = run_headless(EDITOR_POINTER, cwd, timeout=wall)
        if result.text.strip():
            (cwd / "output" / "answer.md").write_text(result.text, encoding="utf-8")
        if not result.text.strip():
            tail = (result.stderr or f"rc={result.rc}")[-400:]
            return "", 0.0, f"headless: {tail}"
        return result.text, 0.0, ""


__all__ = ["DshProposer"]
