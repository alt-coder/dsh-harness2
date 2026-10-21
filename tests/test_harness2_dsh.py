"""Offline tests for the adapter and CLI: no dsh session or model is started."""

from __future__ import annotations

import contextlib
import io
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness2_dsh import cli, headless
from harness2_dsh.adapter import DshAdapter

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


class ModelOverlayTest(unittest.TestCase):
    def test_unset_means_profile_default(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(headless.model_overlay(), [])

    def test_provider_and_model_become_one_patch_row(self):
        env = {"HARNESS2_DSH_PROVIDER": "deepseek-official", "HARNESS2_DSH_MODEL": "deepseek-flash"}
        with mock.patch.dict(os.environ, env, clear=True):
            flag, path = headless.model_overlay()
        self.assertEqual(flag, "--patch")
        text = Path(path).read_text(encoding="utf-8")
        self.assertIn("id: agent-default-model", text)
        self.assertIn('provider: "deepseek-official"', text)
        self.assertIn('model: "deepseek-flash"', text)

    def test_half_a_choice_is_refused(self):
        with mock.patch.dict(os.environ, {"HARNESS2_DSH_PROVIDER": "x"}, clear=True):
            with self.assertRaises(ValueError):
                headless.model_overlay()


class LauncherTest(unittest.TestCase):
    def test_explicit_path_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / "my dsh"
            exe.write_text("#!/bin/sh\n")
            with mock.patch.dict(os.environ, {"HARNESS2_DSH_BIN": str(exe)}, clear=True):
                self.assertEqual(headless.dsh_command(), [str(exe)])

    def test_dsh_on_path_then_npx(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"PATH": tmp}, clear=True):
                self.assertEqual(headless.dsh_command(), headless.NPX_DSH)
                exe = Path(tmp) / "dsh"
                exe.write_text("#!/bin/sh\n")
                exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
                self.assertEqual(headless.dsh_command(), [str(exe)])


class AdapterTest(unittest.TestCase):
    def test_tasks_form_one_stream_keyed_by_directory_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            tasks = []
            for name in ("alpha", "beta"):
                d = Path(tmp) / name
                d.mkdir()
                (d / "task.md").write_text(f"do {name}\n")
                (d / f"{name}.csv").write_text("a\n")
                tasks.append(d)
            adapter = DshAdapter(runs_root=Path(tmp) / "runs", task_dirs=tasks, base_dir=EXAMPLES / "harness")
            self.assertEqual(adapter.tasks(), ["alpha", "beta"])
            self.assertEqual(adapter.task_prompt("beta"), "do beta")
            ws = Path(tmp) / "ws"
            ws.mkdir()
            adapter._copy_task_files("beta", ws)
            self.assertEqual(sorted(p.name for p in ws.iterdir()), ["beta.csv"])

    def test_duplicate_task_names_are_refused(self):
        with self.assertRaises(ValueError):
            DshAdapter(
                runs_root=Path("/tmp/x"),
                task_dirs=[Path("/a/ledger"), Path("/b/ledger")],
                base_dir=EXAMPLES / "harness",
            )


class AdoptedTest(unittest.TestCase):
    def test_selected_final_is_found_even_when_the_base_was_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = Path(tmp) / "dsh-dsh-agg1" / "ledger"
            (task / "harnesses" / "agg1").mkdir(parents=True)
            (task / "harnesses" / "agg1" / "AGENTS.md").write_text("# rules\n")
            (task / "summary.json").write_text(
                json.dumps({"selections": {"agg": {"chosen_label": "agg1", "chosen_tree_is_base": True}}})
            )
            found = cli._adopted(Path(tmp), ["ledger"])
            self.assertEqual(found, {"ledger": {"agg": task / "harnesses" / "agg1"}})


class CliTest(unittest.TestCase):
    def run_cli(self, *argv: str) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            with mock.patch.dict(os.environ, {}, clear=False):
                code = cli.main(list(argv))
        return code, out.getvalue()

    def test_dry_run_plans_a_stream(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, text = self.run_cli(
                "run", "--harness", str(EXAMPLES / "harness"),
                "--task", str(EXAMPLES / "tasks" / "ledger"),
                "--provider", "deepseek-official", "--model", "deepseek-flash",
                "--out", tmp, "--k", "2", "--mode", "both", "--dry-run",
            )
        self.assertEqual(code, 0, text)
        plan = json.loads(text)
        self.assertEqual(plan["task_ids"], ["ledger"])
        self.assertEqual(plan["modes"], ["agg", "seq"])
        self.assertEqual(plan["model"], "dsh/deepseek-flash")

    def test_provider_needs_a_model(self):
        code, text = self.run_cli(
            "run", "--harness", str(EXAMPLES / "harness"),
            "--task", str(EXAMPLES / "tasks" / "ledger"), "--provider", "deepseek-official", "--dry-run",
        )
        self.assertEqual(code, 2)
        self.assertIn("--provider and --model", text)

    def test_missing_task_file_is_reported(self):
        code, text = self.run_cli("run", "--harness", str(EXAMPLES / "harness"), "--task", str(EXAMPLES), "--dry-run")
        self.assertEqual(code, 2)
        self.assertIn("has no task.md", text)


if __name__ == "__main__":
    unittest.main()
