"""Codex App Server peer contracts using disposable JSONL executables only."""

from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from relay_runtime import provider as peer

THREAD = "10000000-0000-4000-8000-000000000001"
OTHER_THREAD = "20000000-0000-4000-8000-000000000002"
TURN = "30000000-0000-4000-8000-000000000003"
OTHER_TURN = "40000000-0000-4000-8000-000000000004"
ANSWER = "Scoped final answer 雪"


SERVER = r'''
import json, os, sys, time
from pathlib import Path
spec = json.loads(Path(SPEC_PATH).read_text())
with open(CALLS_PATH, 'a') as stream:
    stream.write('call\n')
Path(RECEIPT_PATH).write_text(json.dumps({
    'argv': sys.argv, 'cwd': os.getcwd(), 'pid': os.getpid(), 'pgid': os.getpgrp(),
    'env': {key: os.environ.get(key) for key in ENV_KEYS},
}))

def receive():
    line = sys.stdin.buffer.readline()
    if not line:
        raise SystemExit(0)
    message = json.loads(line)
    with open(REQUESTS_PATH, 'a') as stream:
        stream.write(json.dumps(message) + '\n')
    return message

def emit(value):
    print(json.dumps(value), flush=True)

def send_result(message, result):
    identifier = message['id']
    if spec.get('wrong_response_id') == message['method']:
        identifier = 'unrelated-response-id'
    if spec.get('error_response') == message['method']:
        emit({'id': identifier, 'error': {'code': -32000, 'message': 'fixture RPC failure'}})
        raise SystemExit(0)
    emit({'id': identifier, 'result': result})

while True:
    message = receive()
    method = message.get('method')
    if method == 'initialize':
        send_result(message, {'userAgent': 'fixture-app-server', 'platformFamily': 'unix',
                              'platformOs': 'linux', 'codexHome': os.environ['CODEX_HOME']})
    elif method == 'initialized':
        pass
    elif method == 'hooks/list':
        hooks = [{'eventName': event, 'command': HOOK_COMMAND, 'handlerType': 'command',
                  'source': 'sessionFlags', 'enabled': True, 'trustStatus': 'trusted',
                  'timeoutSec': 3, 'matcher': None, 'async': False}
                 for event in ('sessionStart','sessionEnd','userPromptSubmit','stop','interrupt')]
        for hook in hooks: hook.update(spec.get('hook_updates', {}))
        send_result(message, spec.get('hooks_result', {'data': [{'cwd': os.getcwd(), 'hooks': hooks}]}))
    elif method in ('thread/start', 'thread/resume'):
        thread = {'id': THREAD_ID, 'cwd': os.getcwd(), 'sessionId': THREAD_ID, 'turns': []}
        thread.update(spec.get('thread_updates', {}))
        result = {'thread': thread, 'cwd': os.getcwd(), 'model': 'fixture-native-selection',
                  'modelProvider': 'fixture-native-provider', 'approvalPolicy': 'on-request',
                  'approvalsReviewer': spec.get('reviewer', 'user'),
                  'sandbox': {'type': 'workspaceWrite', 'writableRoots': [os.getcwd()],
                              'networkAccess': False, 'excludeTmpdirEnvVar': False,
                              'excludeSlashTmp': False}}
        result.update(spec.get('thread_result_updates', {}))
        send_result(message, result)
    elif method == 'turn/start':
        for event in spec.get('before_turn_response', []):
            emit(event)
        turn = {'id': TURN_ID, 'items': [], 'status': 'inProgress', 'error': None}
        turn.update(spec.get('turn_updates', {}))
        send_result(message, {'turn': turn})
        if 'raw_hex' in spec:
            sys.stdout.buffer.write(bytes.fromhex(spec['raw_hex']))
            sys.stdout.buffer.flush()
            raise SystemExit(0)
        if 'oversized_line' in spec:
            sys.stdout.buffer.write(b'{' + b'x' * spec['oversized_line'])
            sys.stdout.buffer.flush()
            raise SystemExit(0)
        for event in spec.get('events', []):
            emit(event)
            if 'id' in event and 'method' in event:
                response = receive()
                if response.get('id') != event['id']:
                    raise SystemExit('response routed to wrong server request')
        if spec.get('sleep'):
            time.sleep(20)
        if spec.get('exit_after_events', True):
            raise SystemExit(spec.get('exit', 0))
    elif method == 'turn/interrupt':
        send_result(message, {})
    else:
        raise SystemExit('unexpected client method')
'''


def item(text=ANSWER, *, phase="final_answer", thread=THREAD, turn=TURN, kind="agentMessage"):
    identifier = "fixture-" + hashlib.sha256(repr((text, phase, kind)).encode()).hexdigest()[:16]
    native = {"type": kind, "id": identifier, "text": text, "phase": phase}
    if kind == "reasoning":
        native = {"type": "reasoning", "id": "fixture-reasoning", "summary": [text], "content": []}
    return {"method": "item/completed", "params": {"threadId": thread, "turnId": turn, "item": native}}


def completed(*, thread=THREAD, turn=TURN, status="completed", error=None):
    return {"method": "turn/completed", "params": {"threadId": thread,
            "turn": {"id": turn, "status": status, "items": [], "error": error}}}


class CodexProtocolTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="relay-codex-protocol-", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.repo = self.base / "checkout 雪 ;$(touch injected)"
        self.repo.mkdir()
        self.relay = self.base / "relay entry"
        self.provider = self.base / "codex entry"
        self.task = self.base / "task.txt"
        self.task.write_text("Review 'quoted' 雪.\n$(touch injected) `touch injected`\n", encoding="utf-8")
        self.specification = self.base / "server-spec.json"
        self.receipt = self.base / "native-receipt.json"
        self.requests = self.base / "requests.jsonl"
        self.calls = self.base / "calls.txt"
        self.environment = {"PATH": "/usr/bin:/bin", "LC_ALL": "C.UTF-8",
                            "RELAY_TEST_NATIVE_ENVIRONMENT": "inherited fixture value"}
        for name in ("HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "CODEX_HOME"):
            directory = self.base / name.lower()
            directory.mkdir(mode=0o700)
            self.environment[name] = str(directory)
        hook = shlex.join([str(self.relay), "--repo", str(self.repo), "provider-hook", "--client", "codex"])
        self.native_arguments = []
        events = ["SessionStart", "UserPromptSubmit", "Stop", "SessionEnd", "Interrupt"]
        for event in events:
            self.native_arguments.extend(["-c", "hooks." + event + "=[{hooks=[{type=\"command\",command="
                                          + json.dumps(hook, ensure_ascii=False) + ",timeout=3}]}]"])
        plan = {"schema": 1, "provider": "codex", "repo": str(self.repo), "hook_command": hook,
                "native_arguments": self.native_arguments, "events": events,
                "launches_provider": False, "changes_provider_settings": False, "changes_permissions": False}
        self.executable(self.relay, f"print({json.dumps(plan)!r})\n")
        settings = {"SPEC_PATH": str(self.specification), "RECEIPT_PATH": str(self.receipt),
                    "CALLS_PATH": str(self.calls), "REQUESTS_PATH": str(self.requests),
                    "ENV_KEYS": list(self.environment), "THREAD_ID": THREAD, "TURN_ID": TURN,
                    "HOOK_COMMAND": hook}
        source = "".join(f"{key} = {value!r}\n" for key, value in settings.items()) + SERVER
        self.executable(self.provider, source)
        self.configure()
        self.count = 0

    @staticmethod
    def executable(path, source):
        path.write_text(f"#!{sys.executable}\n" + source, encoding="utf-8")
        path.chmod(0o700)

    def configure(self, **spec):
        self.specification.write_text(json.dumps({"events": [item(), completed()], **spec}))

    def invoke(self, *extra):
        self.count += 1
        self.requests.unlink(missing_ok=True)
        self.calls.unlink(missing_ok=True)
        directory = self.base / f"evidence-{self.count}"
        arguments = ["codex", "--repo", str(self.repo), "--relay", str(self.relay),
                     "--provider", str(self.provider), "--task-file", str(self.task),
                     "--output-dir", str(directory), "--timeout", "2", "--json", *extra]
        stdout, stderr = io.StringIO(), io.StringIO()
        with (redirect_stdout(stdout), redirect_stderr(stderr),
              mock.patch.dict(os.environ, self.environment, clear=True)):
            code = peer.peer_main(arguments)
        self.assertTrue(stdout.getvalue(), stderr.getvalue())
        return code, json.loads(stdout.getvalue()), directory

    def recorded_requests(self):
        return [json.loads(line) for line in self.requests.read_text().splitlines()]

    def assert_attention(self, result, state="uncertain"):
        code, value, directory = result
        self.assertNotEqual(0, code)
        self.assertEqual(state, value["state"])
        self.assertTrue(value["provider_started"])
        self.assertTrue(value["needs_attention"])
        self.assertEqual("call\n", self.calls.read_text(), "uncertain calls must not be retried")
        self.assertEqual("not_checked", value["workflow_completion"])
        self.assertEqual(value, json.loads((directory / "result.json").read_text()))
        return value

    def test_new_thread_returns_only_exact_final_answer_and_preserves_native_context(self):
        code, result, directory = self.invoke()
        self.assertEqual(0, code, result)
        self.assertEqual("codex", result["provider"])
        self.assertEqual("returned", result["state"])
        self.assertEqual(ANSWER, result["result"])
        self.assertIsNone(result["requested_session_id"])
        self.assertEqual(THREAD, result["session_id"])
        self.assertEqual(TURN, result["turn_id"])
        self.assertFalse(result["needs_attention"])
        self.assertEqual("not_checked", result["relay_acknowledgement"])
        self.assertEqual("not_checked", result["workflow_completion"])
        receipt = json.loads(self.receipt.read_text())
        self.assertEqual([str(self.provider), *self.native_arguments, "app-server", "--listen", "stdio://"], receipt["argv"])
        self.assertEqual(self.environment, receipt["env"])
        self.assertEqual(str(self.repo), receipt["cwd"])
        requests = self.recorded_requests()
        self.assertEqual(["initialize", "initialized", "hooks/list", "thread/start", "turn/start"], [request["method"] for request in requests])
        self.assertEqual(str(self.repo), requests[3]["params"]["cwd"])
        self.assertEqual(THREAD, requests[4]["params"]["threadId"])
        self.assertEqual(self.task.read_text(), requests[4]["params"]["input"][0]["text"])
        self.assertEqual('ready', result['hook_readiness']['state'])
        for request in requests:
            self.assertFalse({"sandbox", "sandboxPolicy", "approvalPolicy", "approvalsReviewer", "model", "config"}
                             & set(request.get("params", {})))
        self.assertFalse((self.repo / "injected").exists())
        self.assertEqual(self.task.read_bytes(), (directory / "task.txt").read_bytes())
        raw_output = (directory / "stdout.json").read_bytes()
        self.assertEqual({"bytes": len(raw_output), "sha256": hashlib.sha256(raw_output).hexdigest(),
                          "truncated": False}, result["stdout_observation"])
        self.assertEqual(0o700, directory.stat().st_mode & 0o777)
        for path in directory.rglob("*"):
            self.assertEqual(0o700 if path.is_dir() else 0o600, path.stat().st_mode & 0o777)

    def test_resume_uses_exact_thread_without_latest_selection(self):
        code, result, _ = self.invoke("--resume", THREAD)
        self.assertEqual(0, code, result)
        self.assertEqual(THREAD, result["requested_session_id"])
        self.assertEqual(THREAD, result["session_id"])
        requests = self.recorded_requests()
        resume = next(request for request in requests if request.get("method") == "thread/resume")
        self.assertEqual(THREAD, resume["params"]["threadId"])
        self.assertNotIn("thread/start", [request.get("method") for request in requests])

    def test_resume_preserves_an_opaque_native_thread_identity(self):
        identifier = "native-thread-fixture-opaque"
        self.configure(thread_updates={"id": identifier, "sessionId": identifier},
                       events=[item(thread=identifier), completed(thread=identifier)])
        code, result, _ = self.invoke("--resume", identifier)
        self.assertEqual(0, code, result)
        self.assertEqual(identifier, result["session_id"])
        resume = next(row for row in self.recorded_requests() if row.get("method") == "thread/resume")
        self.assertEqual(identifier, resume["params"]["threadId"])

    def test_hook_readiness_stops_before_a_thread_or_task_when_review_is_needed(self):
        for update in ({'trustStatus': 'modified'}, {'trustStatus': 'untrusted'},
                       {'enabled': False}, {'source': 'user'}, {'matcher': 'unexpected'}):
            with self.subTest(update=update):
                self.configure(hook_updates=update)
                code, result, _ = self.invoke()
                self.assertNotEqual(0, code, result)
                self.assertEqual('not_submitted', result['task_submission'])
                self.assertEqual('needs_review', result['hook_readiness']['state'])
                self.assertEqual(['initialize','initialized','hooks/list'],
                                 [row['method'] for row in self.recorded_requests()])
                self.assertIsNone(result['session_id'])
                self.assertIn('/hooks', result['message'])

    def test_missing_checkout_listing_cannot_imply_hooks_are_ready(self):
        self.configure(hooks_result={'data': []})
        code, result, _ = self.invoke()
        self.assertNotEqual(0, code, result)
        self.assertEqual('not_submitted', result['task_submission'])
        self.assertEqual(['initialize','initialized','hooks/list'],
                         [row['method'] for row in self.recorded_requests()])

    def test_resume_response_cannot_substitute_another_thread(self):
        self.configure(thread_updates={"id": OTHER_THREAD, "sessionId": OTHER_THREAD})
        result = self.assert_attention(self.invoke("--resume", THREAD))
        self.assertNotEqual(OTHER_THREAD, result["session_id"])
        self.assertNotIn("turn/start", [request.get("method") for request in self.recorded_requests()])

    def test_resume_active_or_unavailable_thread_does_not_submit_another_task(self):
        cases = [{"status": {"type": "active", "activeFlags": []}},
                 {"status": {"type": "systemError"}},
                 {"status": {"type": "idle"}, "turns": [
                     {"id": OTHER_TURN, "status": "inProgress", "items": [], "error": None}]}]
        for updates in cases:
            with self.subTest(updates=updates):
                self.configure(thread_updates=updates)
                self.assert_attention(self.invoke("--resume", THREAD))
                self.assertNotIn("turn/start", [request.get("method") for request in self.recorded_requests()])

    def test_thread_working_directory_mismatch_prevents_turn_start(self):
        self.configure(thread_updates={"cwd": str(self.base)}, thread_result_updates={"cwd": str(self.base)})
        self.assert_attention(self.invoke())
        self.assertNotIn("turn/start", [request.get("method") for request in self.recorded_requests()])

    def test_session_tree_identity_is_distinct_from_the_addressed_thread(self):
        self.configure(thread_updates={"sessionId": OTHER_THREAD})
        code, result, _ = self.invoke()
        self.assertEqual(0, code, result)
        self.assertEqual(THREAD, result["session_id"])
        self.assertEqual(OTHER_THREAD, result["native_session_id"])
        request = next(request for request in self.recorded_requests() if request.get("method") == "turn/start")
        self.assertEqual(THREAD, request["params"]["threadId"])

    def test_unrelated_response_ids_never_satisfy_pending_requests(self):
        for method in ("initialize", "thread/start", "turn/start"):
            with self.subTest(method=method):
                self.configure(wrong_response_id=method)
                self.assert_attention(self.invoke())

    def test_foreign_item_thread_or_turn_cannot_become_the_answer(self):
        for thread, turn in ((OTHER_THREAD, TURN), (THREAD, OTHER_TURN)):
            with self.subTest(thread=thread, turn=turn):
                self.configure(events=[item("foreign answer", thread=thread, turn=turn), completed()])
                result = self.assert_attention(self.invoke())
                self.assertNotEqual("foreign answer", result["result"])

    def test_foreign_terminal_thread_or_turn_cannot_complete_the_call(self):
        for thread, turn in ((OTHER_THREAD, TURN), (THREAD, OTHER_TURN)):
            with self.subTest(thread=thread, turn=turn):
                self.configure(events=[item(), completed(thread=thread, turn=turn)])
                self.assert_attention(self.invoke())

    def test_terminal_event_before_turn_identity_is_established_is_uncertain(self):
        self.configure(before_turn_response=[completed()], events=[])
        self.assert_attention(self.invoke())

    def test_matching_final_events_can_arrive_before_turn_start_response(self):
        self.configure(before_turn_response=[item(), completed()], events=[])
        code, result, _ = self.invoke()
        self.assertEqual(0, code, result)
        self.assertEqual(ANSWER, result["result"])
        self.assertEqual(TURN, result["turn_id"])

    def test_reasoning_commentary_and_deltas_do_not_replace_final_answer(self):
        delta = {"method": "item/agentMessage/delta", "params": {
            "threadId": THREAD, "turnId": TURN, "itemId": "fixture-item", "delta": "partial delta"}}
        self.configure(events=[item("reasoning fixture", kind="reasoning"),
                               item("commentary fixture", phase="commentary"), delta, item(), completed()])
        code, result, _ = self.invoke()
        self.assertEqual(0, code, result)
        self.assertEqual(ANSWER, result["result"])

    def test_completed_turn_without_final_answer_is_not_a_returned_answer(self):
        for events in ([completed()], [item("commentary", phase="commentary"), completed()],
                       [item("reasoning", kind="reasoning"), completed()]):
            with self.subTest(events=events):
                self.configure(events=events)
                self.assert_attention(self.invoke())

    def test_absent_or_null_phase_can_supply_the_completed_turn_answer(self):
        for missing in (False, True):
            candidate = item(phase=None)
            if missing:
                del candidate["params"]["item"]["phase"]
            with self.subTest(missing=missing):
                self.configure(events=[candidate, completed()])
                code, result, _ = self.invoke()
                self.assertEqual(0, code, result)
                self.assertEqual(ANSWER, result["result"])

    def test_latest_unknown_phase_is_fallback_but_explicit_final_has_priority(self):
        for explicit in (False, True):
            events = [item("earlier unphased message", phase=None)]
            if explicit:
                events.append(item())
            events.extend([item("latest unphased message", phase=None), completed()])
            with self.subTest(explicit=explicit):
                self.configure(events=events)
                code, result, _ = self.invoke()
                self.assertEqual(0, code, result)
                self.assertEqual(ANSWER if explicit else "latest unphased message", result["result"])

    def test_failed_and_interrupted_turns_are_provider_errors(self):
        for status in ("failed", "interrupted"):
            with self.subTest(status=status):
                error = {"message": "fixture provider failure", "codexErrorInfo": None, "additionalDetails": None}
                self.configure(events=[item("partial useful answer"), completed(status=status, error=error if status == "failed" else None)])
                self.assert_attention(self.invoke(), "provider_error")

    def test_completed_answer_survives_nonzero_process_exit_with_attention(self):
        self.configure(exit=7)
        code, result, directory = self.invoke()
        self.assertEqual(0, code, result)
        self.assertEqual("returned", result["state"])
        self.assertTrue(result["needs_attention"])
        self.assertEqual(ANSWER, result["result"])
        self.assertEqual(THREAD, result["session_id"])
        self.assertEqual(TURN, result["turn_id"])
        self.assertEqual(7, result["process_exit_code"])
        self.assertEqual(result, json.loads((directory / "result.json").read_text()))
        self.assertEqual("call\n", self.calls.read_text())

    def test_rpc_error_never_becomes_a_returned_turn(self):
        for method in ("initialize", "thread/start", "turn/start"):
            with self.subTest(method=method):
                self.configure(error_response=method)
                code, result, _ = self.invoke()
                self.assertNotEqual(0, code)
                self.assertIn(result["state"], ("uncertain", "provider_error"))
                self.assertTrue(result["needs_attention"])
                self.assertEqual("call\n", self.calls.read_text())

    def test_zero_exit_without_turn_completion_remains_uncertain(self):
        self.configure(events=[item("unfinished answer")])
        self.assert_attention(self.invoke())

    def test_timeout_after_partial_output_preserves_evidence_without_retry(self):
        self.configure(events=[item("partial answer before timeout")], sleep=True)
        invocation = self.invoke("--timeout", "1")
        result = self.assert_attention(invocation)
        self.assertNotEqual("returned", result["state"])
        self.assertIn(b"partial answer before timeout", (invocation[2] / "stdout.json").read_bytes())

    def test_caller_termination_retains_partial_output_and_stops_owned_native_process(self):
        self.configure(events=[item("partial answer before termination")], sleep=True)
        evidence = self.base / "terminated-evidence"
        entry = ("import runpy, signal, sys\n"
                 "signal.signal(signal.SIGTERM, signal.SIG_DFL)\n"
                 "sys.argv = sys.argv[1:]\n"
                 "runpy.run_path(sys.argv[0], run_name='__main__')\n")
        wrapper = subprocess.Popen(
            [sys.executable, "-I", "-S", "-B", "-c", entry, str(ROOT / "examples/call_peer.py"),
             "codex", "--repo", str(self.repo), "--relay", str(self.relay), "--provider", str(self.provider),
             "--task-file", str(self.task), "--output-dir", str(evidence), "--timeout", "15", "--json"],
            cwd=ROOT, env=self.environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, start_new_session=True)
        receipt = None
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    receipt = json.loads(self.receipt.read_text())
                    if b"partial answer before termination" in (evidence / "stdout.json").read_bytes():
                        break
                except (FileNotFoundError, json.JSONDecodeError):
                    pass
                if wrapper.poll() is not None:
                    self.fail("peer wrapper exited before the disposable server's partial answer")
                time.sleep(0.01)
            else:
                self.fail("disposable server did not produce partial output before the deadline")
            self.assertNotEqual(wrapper.pid, receipt["pgid"])
            wrapper.send_signal(signal.SIGTERM)
            stdout, stderr = wrapper.communicate(timeout=10)
            self.assertEqual(128 + signal.SIGTERM, wrapper.returncode, stderr)
            result = json.loads(stdout)
            self.assertEqual("uncertain", result["state"])
            self.assertTrue(result["needs_attention"])
            self.assertTrue(result["provider_started"])
            self.assertEqual(result, json.loads((evidence / "result.json").read_text()))
            self.assertEqual("call\n", self.calls.read_text())
            self.assertIn(b"partial answer before termination", (evidence / "stdout.json").read_bytes())
            with self.assertRaises(ProcessLookupError):
                os.kill(receipt["pid"], 0)
        finally:
            if wrapper.poll() is None:
                wrapper.kill()
            wrapper.communicate(timeout=5)
            if receipt is not None:
                try:
                    os.killpg(receipt["pgid"], signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_malformed_nonobject_nonutf8_and_partial_jsonl_are_uncertain(self):
        for raw in (b"{malformed}\n", b"[]\n", b"null\n", b"\xff\n", b'{"method":', b"[" * 2000 + b"]" * 2000 + b"\n"):
            with self.subTest(raw=raw[:80]):
                self.configure(raw_hex=raw.hex())
                self.assert_attention(self.invoke())

    def test_unterminated_oversized_jsonl_is_bounded_and_uncertain(self):
        self.configure(oversized_line=17 * 1024 * 1024)
        result = self.assert_attention(self.invoke())
        self.assertIsNotNone(result.get("stdout_observation"))
        self.assertLessEqual(result["stdout_observation"]["bytes"], 16 * 1024 * 1024 + 1)
        self.assertTrue(result["stdout_observation"]["truncated"])

    def test_usage_notification_retains_native_counts_without_billing_claim(self):
        counts = {"totalTokens": 30, "inputTokens": 20, "cachedInputTokens": 5,
                  "outputTokens": 10, "reasoningOutputTokens": 3}
        usage = {"total": counts, "last": counts, "modelContextWindow": 1000}
        self.configure(events=[{"method": "thread/tokenUsage/updated", "params": {
            "threadId": THREAD, "turnId": TURN, "tokenUsage": usage}}, item(), completed()])
        code, result, _ = self.invoke()
        self.assertEqual(0, code, result)
        self.assertEqual(counts, result["usage"]["total"])
        self.assertEqual(counts, result["usage"]["last"])
        self.assertEqual("unknown", result["actual_billed_cost"])

    def test_user_approval_requests_are_declined_without_permission_expansion(self):
        for method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval", "item/permissions/requestApproval"):
            with self.subTest(method=method):
                params = {"threadId": THREAD, "turnId": TURN, "itemId": "fixture-approval", "startedAtMs": 1}
                if method.endswith("commandExecution/requestApproval"):
                    params.update(command="fixture command", cwd=str(self.repo), proposedExecpolicyAmendment=["fixture"])
                elif method.endswith("fileChange/requestApproval"):
                    params.update(grantRoot=str(self.base))
                else:
                    params.update(cwd=str(self.repo), permissions={"network": {"enabled": True}})
                request = {"id": "server-approval-1", "method": method, "params": params}
                self.configure(events=[request, item(), completed()])
                _, result, _ = self.invoke()
                response = next(message for message in self.recorded_requests() if message.get("id") == request["id"])
                if method.endswith("permissions/requestApproval"):
                    self.assertEqual({}, response["result"]["permissions"])
                    self.assertEqual("turn", response["result"].get("scope", "turn"))
                else:
                    self.assertEqual({"decision": "decline"}, response["result"])
                self.assertTrue(result["needs_attention"])
                self.assertTrue(result["permission_denials"])

    def test_native_auto_review_selection_is_preserved(self):
        self.configure(reviewer="auto_review")
        code, result, _ = self.invoke()
        self.assertEqual(0, code, result)
        for request in self.recorded_requests():
            self.assertNotIn("approvalsReviewer", request.get("params", {}))

    def test_unknown_server_requests_are_not_implicitly_authorized(self):
        request = {"id": "unsupported-server-request", "method": "fixture/unknown/request", "params": {}}
        self.configure(events=[request, item(), completed()])
        _, result, _ = self.invoke()
        response = next(message for message in self.recorded_requests() if message.get("id") == request["id"])
        self.assertIn("error", response)
        self.assertNotIn("result", response)
        self.assertTrue(result["needs_attention"])


if __name__ == "__main__":
    unittest.main()
