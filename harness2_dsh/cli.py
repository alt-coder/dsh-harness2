"""`harness2-dsh`: run Harness2 adaptation with DeepSeek Harness (DSH) as the agent.

    harness2-dsh doctor
    harness2-dsh run --harness ./harness --task ./tasks/ledger --k 1

The base harness directory holds `AGENTS.md` plus optional `.agents/skills/`,
`scripts/`, and `sub_agents/`. Each task directory holds `task.md` plus any source
files the work needs; repeat `--task` to run a stream, where later tasks are refined
with the library of harnesses adopted on earlier ones. Each rollout stages both into
a fresh workspace and runs one `dsh --profile headless` session; Propose-Probe-Compose then runs with DSH as the
editor too.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from importlib import metadata
from pathlib import Path

from harness2.core.schedule import mode_token, run_domain

from .adapter import DshAdapter
from .headless import dsh_command, run_headless
from .prompt_patch import install as install_prompt_surface
from .proposer import DshProposer
from .spec_helpers import channel_for

VERSION = "0.2.0"

# What a failed headless session most often means, and what to do about it.
HINTS = {
    "MISSING_CREDENTIAL": "the headless model has no API key. Export it (for example "
    "DEEPSEEK_API_KEY) or store it on the dsh web Models page, then rerun.",
    "ACCOUNT_SIGN_IN_REQUIRED": "headless runs cannot use the DeepSeek account sign-in. Pick an "
    "API-key route, e.g. --provider deepseek-official --model deepseek-flash with DEEPSEEK_API_KEY set.",
}


def _model_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--provider",
        default=os.environ.get("HARNESS2_DSH_PROVIDER"),
        help="dsh provider route for every session, e.g. deepseek-official "
        "(default: the headless profile's own model)",
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("HARNESS2_DSH_MODEL"),
        help="model id on that route, e.g. deepseek-flash; give it with --provider",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="harness2-dsh",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"harness2-dsh {VERSION}")
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="check that dsh, Harness2, and a headless model work")
    _model_args(doctor)

    run = sub.add_parser("run", help="run one adaptation sweep over one or more tasks")
    run.add_argument("--harness", type=Path, required=True, help="base harness directory")
    run.add_argument(
        "--task",
        type=Path,
        action="append",
        required=True,
        help="task directory holding task.md (repeat for a stream, run in the order given)",
    )
    run.add_argument("--out", type=Path, default=Path("harness2-runs"), help="output root")
    run.add_argument("-k", "--k", type=int, default=1, help="task-level recursion budget")
    run.add_argument(
        "--mode",
        choices=("parallel", "sequential", "both"),
        default="parallel",
        help="recursion mode (default: parallel)",
    )
    _model_args(run)
    run.add_argument("--timeout", type=int, default=900, help="per-session timeout in seconds")
    run.add_argument("--domain", default="work", help="the domain word used in editor prompts")
    run.add_argument(
        "--work",
        default="professional work with files",
        help="one line describing the kind of work the task is",
    )
    run.add_argument("--dry-run", action="store_true", help="show the plan without model calls")
    return parser


def route_label(args: argparse.Namespace) -> str:
    """The model label carried into Harness2's audit records and prompts."""
    return f"dsh/{args.model}" if args.model else "dsh/headless-default"


def _adopted(runs_root: Path, task_ids: list[str]) -> dict[str, dict[str, Path]]:
    """The harness Harness2 selected, per task and mode.

    The library only stores harnesses that carry a transferable lesson, so a step
    that kept the base harness never reaches it; the per-task selection always names
    the chosen final, whose tree sits under that task's `harnesses/`.
    """
    found: dict[str, dict[str, Path]] = {}
    for tid in task_ids:
        for summary_path in sorted(runs_root.glob(f"*/{tid}/summary.json")):
            try:
                summary = json.loads(summary_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            for mode, sel in (summary.get("selections") or {}).items():
                label = (sel or {}).get("chosen_label")
                harness = summary_path.parent / "harnesses" / str(label)
                if label and (harness / "AGENTS.md").is_file():
                    found.setdefault(tid, {})[mode] = harness
    return found


def _select_model(args: argparse.Namespace) -> str | None:
    """Export the model choice for every session; return an error message if it is invalid."""
    if bool(args.provider) != bool(args.model):
        return "--provider and --model must be given together"
    if args.provider:
        os.environ["HARNESS2_DSH_PROVIDER"] = args.provider
        os.environ["HARNESS2_DSH_MODEL"] = args.model
    return None


def _doctor(args: argparse.Namespace) -> int:
    ok = True

    def report(passed: bool, label: str, detail: str) -> None:
        nonlocal ok
        ok = ok and passed
        print(f"[{'ok' if passed else 'FAIL'}] {label}: {detail}")

    report(True, "python", sys.version.split()[0])
    try:
        report(True, "harness2", metadata.version("harness2"))
    except metadata.PackageNotFoundError:
        report(False, "harness2", "not installed in this environment")

    launcher = dsh_command()
    try:
        proc = subprocess.run(
            [*launcher, "--version"], capture_output=True, text=True, timeout=300
        )
        version = (proc.stdout or proc.stderr).strip().splitlines()[-1:] or ["?"]
        report(proc.returncode == 0, "dsh", f"{' '.join(launcher)} -> {version[0]}")
    except (OSError, subprocess.SubprocessError) as exc:
        report(False, "dsh", f"{' '.join(launcher)} failed: {exc}. Install Node.js 20+ or set HARNESS2_DSH_BIN.")
        return 1

    route = f"{args.provider}/{args.model}" if args.provider else "headless profile default"
    with tempfile.TemporaryDirectory(prefix="harness2-dsh-doctor-") as tmp:
        result = run_headless("Reply with just the word OK.", Path(tmp), timeout=300)
    if result.ok:
        report(True, "headless session", f"{route} answered {result.text.strip()[:40]!r}")
    else:
        detail = (result.stderr or f"exit code {result.rc}").strip().splitlines()[-1:] or [""]
        hint = next((h for code, h in HINTS.items() if code in result.stderr), "")
        report(False, "headless session", f"{route}: {detail[0][:400]}" + (f"\n       fix: {hint}" if hint else ""))
    print("ready" if ok else "not ready")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    problem = _select_model(args)
    if problem:
        print(f"harness2-dsh: {problem}", file=sys.stderr)
        return 2
    if args.command == "doctor":
        return _doctor(args)
    if args.k < 1:
        print("harness2-dsh: --k must be positive", file=sys.stderr)
        return 2
    harness_dir = args.harness.expanduser().resolve()
    task_dirs = [t.expanduser().resolve() for t in args.task]
    if not (harness_dir / "AGENTS.md").is_file():
        print(f"harness2-dsh: {harness_dir} has no AGENTS.md", file=sys.stderr)
        return 2
    for task_dir in task_dirs:
        if not (task_dir / "task.md").is_file():
            print(f"harness2-dsh: {task_dir} has no task.md", file=sys.stderr)
            return 2
    task_ids = [t.name for t in task_dirs]
    if len(set(task_ids)) != len(task_ids):
        print("harness2-dsh: task directories must have distinct names", file=sys.stderr)
        return 2

    out = args.out.expanduser().resolve()
    modes = ("agg", "seq") if args.mode == "both" else (mode_token(args.mode),)
    plan = {
        "harness": str(harness_dir),
        "tasks": [str(t) for t in task_dirs],
        "task_ids": task_ids,
        "k": args.k,
        "modes": list(modes),
        "model": route_label(args),
        "out": str(out),
    }
    if args.dry_run:
        print(json.dumps(plan, indent=2))
        return 0

    install_prompt_surface()
    adapter = DshAdapter(
        runs_root=out / "runs",
        task_dirs=task_dirs,
        base_dir=harness_dir,
        model=route_label(args),
        timeout=args.timeout,
        domain=args.domain,
        work=args.work,
    )
    proposer = DshProposer(channel_for(route_label(args)), audit_dir=out / "proposer")
    result = run_domain(
        adapter.spec.prompts.domain_word,
        task_ids,
        args.k,
        adapter=adapter,
        prop=proposer,
        runs_root=adapter.spec.runs_root,
        library_root=out / "library" / f"k{args.k}",
        modes=modes,
    )
    exports: dict[str, dict[str, str]] = {}
    for tid, by_mode in _adopted(adapter.spec.runs_root, task_ids).items():
        for mode, src in by_mode.items():
            dest = out / "adopted" / tid / mode
            shutil.rmtree(dest, ignore_errors=True)
            shutil.copytree(src, dest)
            exports.setdefault(tid, {})[mode] = str(dest)
    summary = {
        **plan,
        "done": result.get("done"),
        "failed": result.get("failed"),
        "tasks": result.get("tasks"),
        "editor_sessions": len(proposer._calls),
        "adopted": exports,
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return int((result.get("failed") or 0) > 0)


if __name__ == "__main__":
    raise SystemExit(main())
