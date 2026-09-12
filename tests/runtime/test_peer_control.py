"""Durable steering queue contracts; temporary files, no provider or ledger."""

from contextlib import redirect_stderr, redirect_stdout, suppress
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from relay_runtime import peer_control as control

SESSION = "10000000-0000-4000-8000-000000000001"
TURN = "20000000-0000-4000-8000-000000000002"
OTHER = "30000000-0000-4000-8000-000000000003"


class PeerControlTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="relay-peer-control-", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.directory = self.base / "private evidence"
        self.directory.mkdir(mode=0o700)
        self.owner = control.CallControl(self.directory, "codex")
        self.addCleanup(self.close_fixture)
        self.owner.set_target(SESSION, TURN)
        self.control_dir = self.directory / "control"
        self.message = self.base / "message.txt"
        self.message.write_text("Focus on the boundary. 雪\n$(touch injected) 'quoted'\n", encoding="utf-8")

    def close_fixture(self):
        # Malformed-file tests deliberately make normal closure unavailable.
        with suppress(Exception):
            self.owner.close("Disposable test fixture ended.")

    def invoke(self, command, *extra, stdin=None):
        output, errors = io.StringIO(), io.StringIO()
        arguments = [command, "--call-dir", str(self.directory), "--json", *extra]
        with (redirect_stdout(output), redirect_stderr(errors),
              mock.patch.object(control, "_WAIT_SECONDS", 0),
              mock.patch.object(control.sys, "stdin", mock.Mock(buffer=io.BytesIO(stdin or b"")))):
            code = control.control_main(arguments)
        self.assertTrue(output.getvalue(), errors.getvalue())
        return code, json.loads(output.getvalue())

    def send(self, request_id=None, *, session=SESSION, turn=TURN, stdin=None):
        identifier = request_id or str(uuid.uuid4())
        result = self.invoke("send", "--request-id", identifier, "--session", session,
                             "--turn", turn, "--message-file", "-" if stdin is not None else str(self.message),
                             stdin=stdin)
        return identifier, result

    def receipt(self, identifier):
        return self.invoke("receipt", "--request-id", identifier)

    def queue(self, identifier=None):
        identifier, (code, result) = self.send(identifier)
        self.assertEqual(2, code, result)
        self.assertEqual("pending", result["state"])
        self.assertEqual(identifier, result["request_id"])
        return identifier

    def assert_refused(self, result):
        code, value = result
        self.assertEqual(1, code, value)
        self.assertIn(value["state"], ("rejected", "unavailable", "uncertain"))

    def test_private_call_identity_and_target_are_available_without_consumption_claim(self):
        call = json.loads((self.control_dir / "call.json").read_text())
        self.assertEqual(1, call["schema"])
        self.assertEqual("codex", call["provider"])
        self.assertEqual(str(uuid.UUID(call["call_id"])), call["call_id"])
        original = (self.control_dir / "call.json").read_bytes()
        code, status = self.invoke("status")
        self.assertEqual(0, code, status)
        self.assertEqual(original, (self.control_dir / "call.json").read_bytes())
        self.assertFalse(status.get("consumed", False))
        self.assertEqual(0o700, self.control_dir.stat().st_mode & 0o777)
        for path in self.control_dir.rglob("*"):
            self.assertEqual(0o700 if path.is_dir() else 0o600, path.stat().st_mode & 0o777)

    def test_pending_send_is_not_a_final_receipt_and_dispatch_is_once_only(self):
        identifier = self.queue()
        self.assertFalse((self.control_dir / "receipts" / (identifier + ".json")).exists())
        request = json.loads((self.control_dir / "requests" / (identifier + ".json")).read_text())
        self.assertEqual(self.message.read_text(), request["text"])
        self.assertEqual(SESSION, request["session_id"])
        self.assertEqual(TURN, request["turn_id"])
        pending = self.owner.pending()
        self.assertEqual([identifier], [request["request_id"] for request in pending])
        self.assertEqual([], self.owner.pending())
        self.assertEqual([], self.owner.pending())
        code, result = self.receipt(identifier)
        self.assertEqual(2, code, result)
        self.assertEqual("pending", result["state"])
        self.assertFalse((self.base / "injected").exists())

    def test_claude_session_input_does_not_claim_an_exact_native_turn(self):
        self.owner.close("Switching to an independent Claude fixture.")
        self.directory = self.base / "claude call"
        self.directory.mkdir(mode=0o700)
        self.owner = control.CallControl(self.directory, "claude")
        self.owner.set_target(SESSION, None)
        code, status = self.invoke("status")
        self.assertEqual(0, code)
        self.assertEqual("session", status["input_mode"])
        self.assertFalse(status["supported_steering"])
        identifier = str(uuid.uuid4())
        code, pending = self.invoke("send", "--session", SESSION, "--request-id", identifier,
                                    "--message-file", str(self.message))
        self.assertEqual(2, code, pending)
        self.assertIsNone(pending["turn_id"])
        self.owner.pending()
        with self.assertRaises(control.ControlError):
            self.owner.resolve(identifier, "accepted", "An echo cannot establish exact-turn acceptance.")
        self.owner.resolve(identifier, "consumed", "The native result lists this message UUID.")
        code, receipt = self.receipt(identifier)
        self.assertEqual(0, code, receipt)
        self.assertEqual("consumed", receipt["state"])

    def test_provider_specific_target_requirements_refuse_before_submission(self):
        code, value = self.invoke("send", "--session", SESSION, "--message-file", str(self.message))
        self.assertEqual(1, code, value)
        self.assertEqual([], self.owner.pending())
        self.owner.close("Switching to an independent Claude fixture.")
        self.directory = self.base / "claude call"
        self.directory.mkdir(mode=0o700)
        self.owner = control.CallControl(self.directory, "claude")
        self.owner.set_target(SESSION, None)
        _, result = self.send()
        self.assert_refused(result)
        self.assertEqual([], self.owner.pending())

    def test_dispatch_marker_and_directory_are_synced_before_request_is_returned(self):
        identifier = self.queue()
        fsync = control.os.fsync
        synced = []

        def observe(fd):
            fsync(fd)
            info = os.fstat(fd)
            synced.append((info.st_dev, info.st_ino, stat.S_ISDIR(info.st_mode)))

        with mock.patch.object(control.os, "fsync", side_effect=observe):
            pending = self.owner.pending()
        self.assertEqual([identifier], [request["request_id"] for request in pending])
        marker = self.control_dir / "dispatches" / (identifier + ".json")
        info = marker.stat()
        self.assertIn((info.st_dev, info.st_ino, False), synced)
        directory = marker.parent.stat()
        self.assertIn((directory.st_dev, directory.st_ino, True), synced)
        self.assertEqual(0o600, info.st_mode & 0o777)

    def test_sync_failure_never_yields_a_request_to_the_native_driver(self):
        self.queue()
        with mock.patch.object(control.os, "fsync", side_effect=OSError("synthetic sync failure")):
            with self.assertRaises((OSError, control.ControlError)):
                self.owner.pending()

    def test_each_poll_dispatches_at_most_eight_without_replaying_earlier_requests(self):
        identifiers = {self.queue() for _ in range(19)}
        observed = []
        for _ in range(4):
            batch = self.owner.pending()
            self.assertLessEqual(len(batch), 8)
            observed.extend(request["request_id"] for request in batch)
        self.assertEqual(identifiers, set(observed))
        self.assertEqual(len(identifiers), len(observed))
        self.assertEqual([], self.owner.pending())

    def test_lost_cli_reply_and_same_payload_id_inspect_one_durable_request(self):
        identifier = self.queue()
        request = self.control_dir / "requests" / (identifier + ".json")
        original = request.read_bytes()
        self.queue(identifier)
        self.assertEqual(original, request.read_bytes())
        self.assertEqual(1, len(list((self.control_dir / "requests").iterdir())))
        pending = self.owner.pending()
        self.assertEqual([identifier], [request["request_id"] for request in pending])
        self.owner.resolve(identifier, "accepted", "Native fixture accepted this exact request.")
        _, (code, result) = self.send(identifier)
        self.assertEqual(0, code, result)
        self.assertEqual("accepted", result["state"])
        self.assertFalse(result.get("consumed", False))
        self.assertEqual([], self.owner.pending())

    def test_same_request_id_with_different_payload_is_refused_without_overwrite(self):
        identifier = self.queue()
        request = self.control_dir / "requests" / (identifier + ".json")
        before = request.read_bytes()
        self.message.write_text("A different steering instruction.")
        _, result = self.send(identifier)
        self.assert_refused(result)
        self.assertEqual(before, request.read_bytes())
        self.assertEqual([identifier], [request["request_id"] for request in self.owner.pending()])

    def test_exact_target_mismatches_refuse_before_queue_publication(self):
        for session, turn in ((OTHER, TURN), (SESSION, OTHER)):
            with self.subTest(session=session, turn=turn):
                _, result = self.send(session=session, turn=turn)
                self.assert_refused(result)
        self.assertEqual([], self.owner.pending())
        self.assertEqual([], list((self.control_dir / "requests").iterdir()))

    def test_native_accept_reject_and_unknown_receipts_remain_distinct(self):
        for state, expected_code in (("accepted", 0), ("rejected", 1), ("uncertain", 1)):
            with self.subTest(state=state):
                identifier = self.queue()
                self.owner.pending()
                self.owner.resolve(identifier, state, "Synthetic native outcome.")
                code, result = self.receipt(identifier)
                self.assertEqual(expected_code, code, result)
                self.assertEqual(state, result["state"])
                self.assertFalse(result.get("consumed", False))
                self.assertEqual([], self.owner.pending())

    def test_final_receipt_is_immutable_and_bound_to_exact_call_request_and_payload(self):
        identifier = self.queue()
        self.owner.pending()
        self.owner.resolve(identifier, "accepted", "Synthetic native acceptance.")
        path = self.control_dir / "receipts" / (identifier + ".json")
        original = path.read_bytes()
        with self.assertRaises(control.ControlError):
            self.owner.resolve(identifier, "rejected", "Conflicting later observation.")
        self.assertEqual(original, path.read_bytes())
        for key, value in (("call_id", OTHER), ("request_id", OTHER), ("request_sha256", "0" * 64)):
            with self.subTest(key=key):
                altered = json.loads(original)
                altered[key] = value
                path.write_text(json.dumps(altered))
                try:
                    self.assert_refused(self.receipt(identifier))
                finally:
                    path.write_bytes(original)

    def test_closure_rejects_unsent_and_marks_dispatched_unknown_without_overwriting_acceptance(self):
        accepted, dispatched = self.queue(), self.queue()
        self.owner.pending()
        self.owner.resolve(accepted, "accepted", "Synthetic native acceptance.")
        unsent = self.queue()
        self.owner.close("The owned provider call ended.")
        for identifier, state, code in ((accepted, "accepted", 0), (dispatched, "uncertain", 1), (unsent, "rejected", 1)):
            with self.subTest(identifier=identifier):
                actual_code, result = self.receipt(identifier)
                self.assertEqual(code, actual_code, result)
                self.assertEqual(state, result["state"])
        self.assertEqual([], self.owner.pending())

    def test_no_new_submission_can_be_published_after_closure(self):
        self.owner.close("The owned provider call ended.")
        _, result = self.send()
        self.assert_refused(result)
        self.assertEqual([], list((self.control_dir / "requests").iterdir()))

    def test_terminal_turn_stops_new_input_but_preserves_later_ack_before_final_close(self):
        dispatched = self.queue()
        self.owner.pending()
        unsent = self.queue()
        self.owner.stop_accepting("The native turn completed; acknowledgements are still draining.")
        self.assertEqual([], self.owner.pending())
        code, observation = self.receipt(dispatched)
        self.assertEqual(2, code, observation)
        self.assertEqual("pending", observation["state"])
        code, observation = self.receipt(unsent)
        self.assertEqual(1, code, observation)
        self.assertEqual("rejected", observation["state"])
        _, result = self.send()
        self.assert_refused(result)
        self.owner.resolve(dispatched, "accepted", "Exact native acknowledgement arrived after turn completion.")
        before = (self.control_dir / "receipts" / (dispatched + ".json")).read_bytes()
        self.owner.close("The native transport has closed.")
        code, receipt = self.receipt(dispatched)
        self.assertEqual(0, code, receipt)
        self.assertEqual("accepted", receipt["state"])
        self.assertEqual(before, (self.control_dir / "receipts" / (dispatched + ".json")).read_bytes())

    def test_existing_control_cannot_be_reopened_as_a_fresh_consumer(self):
        identifier = self.queue()
        self.owner.pending()
        marker = self.control_dir / "dispatches" / (identifier + ".json")
        before = marker.read_bytes()
        with self.assertRaises((OSError, control.ControlError)):
            control.CallControl(self.directory, "codex")
        self.assertEqual(before, marker.read_bytes())
        self.assertEqual([], self.owner.pending())

    def test_invalid_empty_nul_nonutf8_and_oversized_messages_never_enter_queue(self):
        for body in (b" \n", b"invalid\0message", b"\xff", b"x" * 65537):
            with self.subTest(size=len(body), prefix=body[:20]):
                _, result = self.send(stdin=body)
                self.assert_refused(result)
        self.assertEqual([], list((self.control_dir / "requests").iterdir()))

    def test_exact_64_kib_message_is_allowed_and_retained_as_literal_input(self):
        body = b"x" * 65536
        identifier, (code, result) = self.send(stdin=body)
        self.assertEqual(2, code, result)
        pending = self.owner.pending()
        self.assertEqual([identifier], [request["request_id"] for request in pending])
        self.assertEqual(body, pending[0]["text"].encode("utf-8"))

    def test_queue_cap_preserves_existing_requests_and_allows_idempotent_inspection(self):
        identifiers = [self.queue() for _ in range(256)]
        _, result = self.send()
        self.assert_refused(result)
        self.assertEqual(256, len(list((self.control_dir / "requests").iterdir())))
        self.queue(identifiers[0])
        self.assertEqual(256, len(list((self.control_dir / "requests").iterdir())))

    def test_symlinked_control_directory_is_unavailable(self):
        original = self.directory / "held-control"
        self.control_dir.rename(original)
        self.control_dir.symlink_to(original, target_is_directory=True)
        self.assert_refused(self.invoke("status"))

    def test_call_directory_alias_parent_components_refuse_before_request_publication(self):
        alias = self.base / "evidence alias"
        alias.symlink_to(self.directory, target_is_directory=True)
        spelling = alias / ".." / self.directory.name
        result = self.invoke("send", "--call-dir", str(spelling), "--request-id", str(uuid.uuid4()),
                             "--session", SESSION, "--turn", TURN, "--message-file", str(self.message))
        self.assert_refused(result)
        self.assertEqual([], list((self.control_dir / "requests").iterdir()))

    def test_public_control_or_metadata_permissions_are_unavailable(self):
        for path in (self.control_dir, self.control_dir / "call.json", self.control_dir / "target.json"):
            with self.subTest(path=path.name):
                before = path.stat().st_mode & 0o777
                path.chmod(0o755 if path.is_dir() else 0o644)
                try:
                    self.assert_refused(self.invoke("status"))
                finally:
                    path.chmod(before)

    def test_symlink_and_nonregular_request_files_are_never_dispatched(self):
        for kind in ("symlink", "fifo"):
            with self.subTest(kind=kind):
                identifier = self.queue()
                request = self.control_dir / "requests" / (identifier + ".json")
                body = request.read_bytes()
                request.unlink()
                target = self.base / (identifier + ".json")
                if kind == "symlink":
                    target.write_bytes(body)
                    request.symlink_to(target)
                else:
                    os.mkfifo(request, mode=0o600)
                try:
                    try:
                        pending = self.owner.pending()
                    except (OSError, control.ControlError):
                        pending = []
                    self.assertNotIn(identifier, [request["request_id"] for request in pending])
                    self.assertFalse((self.control_dir / "dispatches" / (identifier + ".json")).exists())
                finally:
                    request.unlink()

    def test_foreign_owned_call_metadata_is_unavailable(self):
        fstat = control.os.fstat
        metadata = (self.control_dir / "call.json").stat()

        def foreign_metadata(fd):
            info = fstat(fd)
            if (info.st_dev, info.st_ino) == (metadata.st_dev, metadata.st_ino):
                fields = list(info)
                fields[4] = os.getuid() + 1
                return os.stat_result(fields)
            return info

        with mock.patch.object(control.os, "fstat", side_effect=foreign_metadata):
            self.assert_refused(self.invoke("status"))

    def test_concurrent_same_id_senders_publish_one_request(self):
        identifier = str(uuid.uuid4())
        environment = {"PATH": "/usr/bin:/bin", "LC_ALL": "C.UTF-8", "HOME": str(self.base)}
        source = ("import sys\n"
                  f"sys.path.insert(0, {str(ROOT / 'src')!r})\n"
                  "from relay_runtime import peer_control\n"
                  "peer_control._WAIT_SECONDS = 0\n"
                  "raise SystemExit(peer_control.control_main(sys.argv[1:]))\n")
        args = [sys.executable, "-I", "-S", "-B", "-c", source, "send", "--call-dir", str(self.directory),
                "--session", SESSION, "--turn", TURN, "--request-id", identifier,
                "--message-file", str(self.message), "--json"]
        children = [subprocess.Popen(args, env=environment, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(2)]
        try:
            for child in children:
                stdout, stderr = child.communicate(timeout=5)
                self.assertEqual(2, child.returncode, stderr)
                self.assertEqual("pending", json.loads(stdout)["state"])
            self.assertEqual(1, len(list((self.control_dir / "requests").iterdir())))
            self.assertEqual([identifier], [request["request_id"] for request in self.owner.pending()])
            self.assertEqual([], self.owner.pending())
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.communicate(timeout=5)


if __name__ == "__main__":
    unittest.main()
