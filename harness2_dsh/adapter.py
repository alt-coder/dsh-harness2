"""A Harness2 benchmark adapter whose rollouts are DSH headless sessions.

The adapter supplies only what `harness2.core.spec.BenchmarkAdapter` asks for: a task
list, a prompt, a base harness, one execution per rollout, and the artifacts. The
method itself (Propose-Probe-Compose, the library, selection) stays Harness2's.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from harness2.core import tree as harness_tree
from harness2.core.spec import BenchPrompts, BenchSpec, ChannelSpec

from .headless import HeadlessResult, render_trajectory, run_headless
from .surface import DSH_SURFACE, dsh_tree_entries

ANSWER = "answer.md"
TASK_FILE = "task.md"

ANSWER_SUFFIX = (
    "\n\nWork in the current directory. When the work is done, write your final answer "
    f"verbatim to `{ANSWER}` in this directory, then reply with it as your final message."
)


class DshSubstrate:
    """The DSH workspace-agent surface; execution is the adapter's own job here."""

    name = "dsh"

    def surface(self):
        return DSH_SURFACE


class DshAdapter:
    """Task directories in, one staged workspace + headless rollout per execution out.

    Each task directory's name is its task id; several tasks form one Harness2
    stream, so later tasks see the library entries adopted on earlier ones.
    """

    def __init__(
        self,
        *,
        runs_root: Path,
        task_dirs: list[Path],
        base_dir: Path,
        model: str = "dsh-headless",
        timeout: int = 900,
        domain: str = "work",
        work: str = "professional work with files",
    ):
        self.runs_root = Path(runs_root)
        self.task_dirs = {Path(d).name: Path(d) for d in task_dirs}
        if len(self.task_dirs) != len(task_dirs):
            raise ValueError("task directories must have distinct names; the name is the task id")
        self.base_dir = Path(base_dir)
        self.timeout = timeout
        self.substrate = DshSubstrate()
        channel = ChannelSpec("opencode", model)
        self.spec = BenchSpec(
            name="dsh",
            runs_root=self.runs_root,
            install_mode="workspace_agents_md",
            prompts=BenchPrompts(work=work, domain_word=domain),
            channels={"proposer": channel, "selector": channel},
        )

    # --- the task ---------------------------------------------------------

    def tasks(self, split: str = "all") -> list[str]:
        return list(self.task_dirs)

    def domain_of(self, task_id: str) -> str:
        return self.spec.prompts.domain_word

    def task_prompt(self, task_id: str) -> str:
        path = self.task_dirs[task_id] / TASK_FILE
        if not path.is_file():
            raise FileNotFoundError(f"{path} is missing; a task directory needs {TASK_FILE}")
        return path.read_text(encoding="utf-8").strip()

    def task_files(self, task_id: str) -> Path | None:
        return self.task_dirs[task_id]

    def base_harness(self) -> Path:
        return self.base_dir

    def validate_tree(self, harness: harness_tree.Tree) -> None:
        errs: list[str] = []
        agents = harness.get("AGENTS.md", "")
        if not agents.strip():
            errs.append("AGENTS.md is empty; it is the only always-on file and must say what to do.")
        for rel in harness:
            try:
                ok = harness_tree.is_allowed(DSH_SURFACE, rel)
            except ValueError:
                ok = False
            if not ok:
                errs.append(f"{rel} is off the DSH surface; edit only AGENTS.md, .agents/skills/, "
                            "scripts/, or sub_agents/.")
        if errs:
            raise ValueError("\n".join(errs))

    # --- one rollout ------------------------------------------------------

    def workspace_of(self, run_id: str) -> Path:
        return self.runs_root / "rollouts" / run_id / "workspace"

    def execute(
        self, task_id: str, harness_dir: Path | None, run_id: str, *, timeout: int | None = None
    ) -> Path:
        run_dir = self.runs_root / "rollouts" / run_id
        answer = run_dir / ANSWER
        if answer.is_file() and answer.read_text(encoding="utf-8").strip():
            return run_dir  # idempotent by run_id
        workspace = run_dir / "workspace"
        shutil.rmtree(workspace, ignore_errors=True)
        workspace.mkdir(parents=True, exist_ok=True)
        self._materialise(harness_dir or self.base_harness(), workspace)
        self._copy_task_files(task_id, workspace)
        _init_git(workspace)

        result = run_headless(
            self.task_prompt(task_id) + ANSWER_SUFFIX,
            workspace,
            timeout=int(timeout or self.timeout),
        )
        self._write_artifacts(run_dir, workspace, result)
        return run_dir

    def _materialise(self, harness_dir: Path, workspace: Path) -> None:
        src_root = Path(harness_dir)
        for entry in dsh_tree_entries():
            src = src_root / entry
            if not src.exists():
                continue
            dest = workspace / entry
            if src.is_dir():
                shutil.copytree(src, dest)
            else:
                shutil.copy2(src, dest)

    def _copy_task_files(self, task_id: str, workspace: Path) -> None:
        for item in sorted(self.task_dirs[task_id].iterdir()):
            if item.name in (TASK_FILE, ANSWER):
                continue
            dest = workspace / item.name
            if item.is_dir():
                shutil.copytree(item, dest, dirs_exist_ok=True)
            else:
                shutil.copy2(item, dest)

    def _write_artifacts(self, run_dir: Path, workspace: Path, result: HeadlessResult) -> None:
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "events.ndjson").write_text(
            "\n".join(json.dumps(e, ensure_ascii=False) for e in result.events) + "\n",
            encoding="utf-8",
        )
        (run_dir / "trajectory.md").write_text(
            render_trajectory(result, obs_trunc=self.spec.obs_trunc), encoding="utf-8"
        )
        if result.stderr.strip():
            (run_dir / "stderr.log").write_text(result.stderr, encoding="utf-8")
        answer = run_dir / ANSWER
        if not (answer.is_file() and answer.read_text(encoding="utf-8").strip()):
            answer.write_text(result.text or "", encoding="utf-8")
        (run_dir / "session.json").write_text(
            json.dumps(
                {
                    "session_id": result.session_id,
                    "returncode": result.rc,
                    "delivered": self.delivered(run_dir),
                    "tools": [
                        {"tool": name, "status": status}
                        for name, _args, status in result.tools()
                    ],
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    # --- what the method reads back ---------------------------------------

    def delivered(self, run_dir: Path) -> bool:
        path = Path(run_dir) / ANSWER
        return path.is_file() and bool(path.read_text(encoding="utf-8").strip())

    def trajectory(self, run_dir: Path, *, obs_trunc: int) -> str:
        path = Path(run_dir) / "trajectory.md"
        if not path.is_file():
            return ""
        text = path.read_text(encoding="utf-8")
        return text[: obs_trunc * 4] if len(text) > obs_trunc * 4 else text

    def deliverable_text(self, run_dir: Path) -> str:
        return (Path(run_dir) / ANSWER).read_text(encoding="utf-8")

    def deliverable_files(self, run_dir: Path) -> dict[str, str]:
        run_dir = Path(run_dir)
        out: dict[str, str] = {}
        if (run_dir / ANSWER).is_file():
            out[ANSWER] = self.deliverable_text(run_dir)
        workspace = run_dir / "workspace"
        if workspace.is_dir():
            for item in sorted(workspace.rglob("*")):
                if not item.is_file() or ".git/" in item.as_posix():
                    continue
                if item.name == TASK_FILE:
                    continue
                rel = item.relative_to(workspace).as_posix()
                if rel == ANSWER or rel.startswith("."):
                    continue
                try:
                    out[rel] = item.read_text(encoding="utf-8")[:20_000]
                except UnicodeDecodeError:
                    continue
        return out

    def score(self, run_dir: Path, task_id: str) -> dict | None:
        """No judge here: this integration runs the adaptation loop, not grading."""
        return None


def _init_git(workspace: Path) -> None:
    """Make the staged workspace its own project root.

    DSH resolves `.agents/skills` from the nearest ancestor holding `.git`; without
    this, a run staged inside another repository would read that repository's skills
    instead of the harness under refinement.
    """
    if (workspace / ".git").exists():
        return
    try:
        subprocess.run(
            ["git", "init", "-q", str(workspace)],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        pass


__all__ = ["DshAdapter", "DshSubstrate"]
