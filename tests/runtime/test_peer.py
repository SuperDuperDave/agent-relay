"""Native peer boundaries using disposable executables; no real provider access."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from relay_runtime import cli, provider as peer


class PeerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="relay-peer-test-", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.repo = self.base / "checkout 雪 ;$(touch injected)"
        self.repo.mkdir()
        self.relay, self.provider = self.base / "relay entry", self.base / "provider entry"
        self.task = self.base / "task.txt"
        self.task.write_text("Review the scoped change.\n", encoding="utf-8")
        self.receipt, self.calls = self.base / "receipt.json", self.base / "calls.txt"
        self.response = self.base / "response.json"
        self.response.write_text("{}")
        self.environment = {"PATH": "/usr/bin:/bin", "LC_ALL": "C.UTF-8",
                            "HOME": str(self.base / "home"),
                            "CLAUDE_CONFIG_DIR": str(self.base / "normal-profile"),
                            "RELAY_TEST_NATIVE_ENVIRONMENT": "ordinary inherited fixture"}
        hook = shlex.join([str(self.relay), "--repo", str(self.repo), "provider-hook", "--client", "claude"])
        hooks = {event: [{"hooks": [{"type": "command", "command": hook, "timeout": 3}]}]
                 for event in ("SessionStart", "UserPromptSubmit", "Stop", "SessionEnd")}
        self.native_arguments = ["--settings", json.dumps({"hooks": hooks})]
        plan = {"schema": 1, "provider": "claude", "repo": str(self.repo), "hook_command": hook,
                "native_arguments": self.native_arguments, "launches_provider": False,
                "changes_provider_settings": False, "changes_permissions": False}
        self.executable(self.relay, "import json, sys\nfrom pathlib import Path\n"
                        f"Path({str(self.base / 'relay-argv.json')!r}).write_text(json.dumps(sys.argv))\n"
                        f"print({json.dumps(plan)!r})\n")
        self.executable(self.provider, "import json, os, sys, time\nfrom pathlib import Path\n"
                        f"with open({str(self.calls)!r}, 'a') as stream: stream.write('call\\n')\n"
                        "task = sys.stdin.buffer.read()\n"
                        f"Path({str(self.base / 'received-task.txt')!r}).write_bytes(task)\n"
                        f"receipt = {{'argv': sys.argv, 'cwd': os.getcwd(), 'env': {{key: os.environ.get(key) for key in {list(self.environment)!r}}}}}\n"
                        f"Path({str(self.receipt)!r}).write_text(json.dumps(receipt))\n"
                        f"spec = json.loads(Path({str(self.response)!r}).read_text())\n"
                        "session_flag = '--resume' if '--resume' in sys.argv else '--session-id'\n"
                        "native = {'type': 'result', 'subtype': 'success', 'is_error': False,\n"
                        "          'session_id': sys.argv[sys.argv.index(session_flag) + 1],\n"
                        "          'result': 'Useful peer answer 雪', 'permission_denials': [],\n"
                        "          'terminal_reason': 'completed'}\n"
                        "native.update(spec.get('native', {}))\n"
                        "for key in spec.get('remove', []): native.pop(key, None)\n"
                        "print('artificial provider diagnostic', file=sys.stderr, flush=True)\n"
                        "if spec.get('sleep'): time.sleep(20)\n"
                        "print(spec['raw'] if 'raw' in spec else json.dumps(native), flush=True)\n"
                        "sys.exit(spec.get('exit', 0))\n")
        self.count = 0

    @staticmethod
    def executable(path, source):
        path.write_text(f"#!{sys.executable}\n" + source, encoding="utf-8")
        path.chmod(0o700)

    def invoke(self, *extra, stdin=None, output=None):
        self.count += 1
        directory = output or self.base / f"evidence-{self.count}"
        arguments = ["claude", "--repo", str(self.repo), "--relay", str(self.relay),
                     "--provider", str(self.provider), "--task-file", "-" if stdin is not None else str(self.task),
                     "--output-dir", str(directory), "--json", *extra]
        stdout, stderr = io.StringIO(), io.StringIO()
        with (redirect_stdout(stdout), redirect_stderr(stderr),
              mock.patch.object(peer.sys, "stdin", mock.Mock(buffer=io.BytesIO(stdin or b""))),
              mock.patch.dict(os.environ, self.environment, clear=True)):
            code = peer.peer_main(arguments)
        return code, json.loads(stdout.getvalue()), stderr.getvalue()

    def configure(self, **spec):
        self.response.write_text(json.dumps(spec))

    def test_literal_task_environment_hooks_and_private_evidence(self):
        task = "Inspect 雪 and café.\n`touch injected` $(touch injected) ' \\\n".encode()
        code, result, _ = self.invoke(stdin=task)
        self.assertEqual(0, code)
        self.assertEqual(task, (self.base / "received-task.txt").read_bytes())
        receipt = json.loads(self.receipt.read_text())
        self.assertEqual(self.environment, receipt["env"])
        self.assertEqual(str(self.repo), receipt["cwd"])
        self.assertEqual([str(self.provider), *self.native_arguments, "--print", "--output-format", "json",
                          "--permission-prompts", "none", "--session-id",
                          result["session_id"]], receipt["argv"])
        self.assertEqual([str(self.relay), "--repo", str(self.repo), "--json", "provider-config",
                          "--client", "claude"], json.loads((self.base / "relay-argv.json").read_text()))
        self.assertFalse((self.repo / "injected").exists())
        self.assertFalse(result["needs_attention"])
        self.assertEqual("not_checked", result["workflow_completion"])
        evidence = Path(result["evidence_directory"])
        self.assertEqual(task, (evidence / "task.txt").read_bytes())
        for path in evidence.iterdir():
            self.assertEqual(0o600, path.stat().st_mode & 0o777)
        self.assertEqual(0o700, evidence.stat().st_mode & 0o777)

    def test_followup_resumes_exact_peer_and_reads_task_file(self):
        _, first, _ = self.invoke()
        self.task.write_text("Follow up on the original answer. 雪", encoding="utf-8")
        code, result, _ = self.invoke("--resume", first["session_id"], "--max-turns", "37")
        self.assertEqual(0, code)
        self.assertEqual(first["session_id"], result["session_id"])
        argv = json.loads(self.receipt.read_text())["argv"]
        self.assertEqual(["--resume", first["session_id"]], argv[-2:])
        self.assertEqual(1, argv.count("--max-turns"))
        self.assertEqual("37", argv[argv.index("--max-turns") + 1])
        self.assertNotIn("--continue", argv)
        self.assertNotIn("--session-id", argv)
        self.assertEqual(self.task.read_bytes(), (self.base / "received-task.txt").read_bytes())

    def test_dry_run_has_no_provider_or_evidence_writes(self):
        evidence = self.base / "dry-evidence"
        code, result, _ = self.invoke("--dry-run", output=evidence)
        self.assertEqual(0, code)
        self.assertEqual("call_prepared", result["state"])
        self.assertFalse(result["provider_started"])
        self.assertFalse(self.calls.exists())
        self.assertFalse(evidence.exists())

    def test_existing_evidence_directory_is_untouched(self):
        evidence = self.base / "existing"
        evidence.mkdir()
        (evidence / "keep.txt").write_bytes(b"original evidence")
        before = {path.name: path.read_bytes() for path in evidence.iterdir()}
        code, result, _ = self.invoke(output=evidence)
        self.assertEqual(1, code)
        self.assertFalse(result["provider_started"])
        self.assertEqual(before, {path.name: path.read_bytes() for path in evidence.iterdir()})
        self.assertFalse(self.calls.exists())

    def test_bad_tasks_refuse_before_config_or_provider(self):
        for body in (b" \n", b"bad\0task", b"\xff", b"x" * (64 * 1024 + 1)):
            with self.subTest(body=body[:20]):
                code, result, _ = self.invoke(stdin=body)
                self.assertEqual(1, code)
                self.assertFalse(result["provider_started"])
                self.assertFalse((self.base / "relay-argv.json").exists())
                self.assertFalse(self.calls.exists())

    def test_timeout_retains_side_effects_and_never_retries(self):
        self.configure(sleep=True)
        code, result, _ = self.invoke("--timeout", "1")
        self.assertEqual(1, code)
        self.assertEqual("uncertain", result["state"])
        self.assertTrue(result["provider_started"])
        self.assertIsNone(result["session_id"])
        self.assertEqual("call\n", self.calls.read_text())
        self.assertEqual(self.task.read_bytes(), (self.base / "received-task.txt").read_bytes())
        evidence = Path(result["evidence_directory"])
        self.assertIn("artificial provider diagnostic", (evidence / "stderr.txt").read_text())
        self.assertEqual(result["requested_session_id"], json.loads((evidence / "request.json").read_text())["requested_session_id"])

    def test_unreadable_missing_and_mismatched_results_remain_uncertain(self):
        cases = ({"raw": ""}, {"raw": "not JSON"}, {"raw": "[]"},
                 {"remove": ["result"]}, {"remove": ["session_id"]},
                 {"native": {"session_id": "00000000-0000-4000-8000-000000000001"}},
                 {"native": {"is_error": "false"}}, {"native": {"permission_denials": "denied"}},
                 {"native": {"errors": [False]}}, {"native": {"result": "\ud800"}})
        for spec in cases:
            with self.subTest(spec=spec):
                self.configure(**spec)
                code, result, _ = self.invoke()
                self.assertEqual(1, code)
                self.assertEqual("uncertain", result["state"])
                self.assertTrue(result["needs_attention"])
        self.assertEqual(len(cases), len(self.calls.read_text().splitlines()))

    def test_denials_retain_useful_answer_without_claiming_completion(self):
        self.configure(native={"permission_denials": [{"tool_name": "Bash", "tool_use_id": "fixture-call",
                                                        "tool_input": {"command": "artificial private command"}}]})
        code, result, _ = self.invoke()
        self.assertEqual(0, code)
        self.assertEqual("Useful peer answer 雪", result["result"])
        self.assertTrue(result["needs_attention"])
        self.assertEqual("not_checked", result["workflow_completion"])
        self.assertEqual([{"tool_name": "Bash", "tool_use_id": "fixture-call"}], result["permission_denials"])
        self.assertNotIn("artificial private command", json.dumps(result))
        self.assertIn("artificial private command", (Path(result["evidence_directory"]) / "stdout.json").read_text())

    def test_nonzero_exit_and_provider_errors_preserve_answer(self):
        for spec in ({"exit": 4}, {"native": {"is_error": True, "subtype": "error_during_execution"}}):
            with self.subTest(spec=spec):
                self.configure(**spec)
                code, result, _ = self.invoke()
                self.assertEqual(1, code)
                self.assertEqual("provider_error", result["state"])
                self.assertEqual("Useful peer answer 雪", result["result"])

    def test_resultless_native_errors_are_returned_with_explicit_bounds(self):
        for errors in (["Artificial provider failure"], ["Artificial failure " * 200] * 9):
            with self.subTest(count=len(errors)):
                self.configure(remove=["result"], native={"is_error": True,
                               "subtype": "error_during_execution", "errors": errors})
                code, result, _ = self.invoke()
                self.assertEqual(1, code)
                self.assertEqual("provider_error", result["state"])
                self.assertIsNone(result["result"])
                self.assertTrue(result["needs_attention"])
                self.assertTrue(result["provider_errors"][0].startswith("Artificial"))
                self.assertLessEqual(len(result["provider_errors"]), 8)
                self.assertTrue(all(len(error) <= 2000 for error in result["provider_errors"]))
                self.assertEqual(len(errors) > 1, result["provider_errors_truncated"])
                raw = json.loads((Path(result["evidence_directory"]) / "stdout.json").read_text())
                self.assertEqual(errors, raw["errors"])

    def test_result_record_failure_retains_returned_text(self):
        original = peer._record
        def record(directory, name, value):
            if name == "result.json":
                raise OSError("artificial full disk")
            return original(directory, name, value)
        with mock.patch.object(peer, "_record", side_effect=record):
            code, result, _ = self.invoke()
        self.assertEqual(1, code)
        self.assertEqual("returned", result["state"])
        self.assertEqual("Useful peer answer 雪", result["result"])
        self.assertIn("unavailable", result["evidence_recording"])
        self.assertEqual("call\n", self.calls.read_text())

    def test_installed_dispatch_preserves_native_environment_boundary(self):
        with (mock.patch.dict(os.environ, self.environment, clear=True),
              mock.patch.object(cli, "_closed_environment", side_effect=AssertionError("confined native call")),
              mock.patch.object(cli, "abi_version", side_effect=AssertionError("worker admission")),
              mock.patch.object(peer, "peer_main", return_value=7) as call):
            code = cli.main(["--repo", str(self.repo), "--json", "peer", "claude", "--task-file", str(self.task)])
        self.assertEqual(7, code)
        call.assert_called_once_with(["claude", "--task-file", str(self.task), "--repo", str(self.repo), "--json"])

    def test_installed_peer_help_exits_without_native_execution(self):
        output = io.StringIO()
        with (redirect_stdout(output), mock.patch.dict(os.environ, self.environment, clear=True),
              mock.patch.object(peer.subprocess, "Popen") as native,
              mock.patch.object(peer.subprocess, "run") as config,
              self.assertRaises(SystemExit) as stopped):
            cli.main(["peer", "--help"])
        self.assertEqual(0, stopped.exception.code)
        self.assertIn("--task-file", output.getvalue())
        native.assert_not_called()
        config.assert_not_called()


if __name__ == "__main__":
    unittest.main()
