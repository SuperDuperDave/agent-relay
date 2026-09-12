"""Human peer summaries preserve answers while exposing unresolved observations."""

from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from relay_runtime import provider


class PeerOutputTests(unittest.TestCase):
    def display(self, **changes):
        envelope = {
            "state": "returned", "result": "The reviewed change handles the boundary case.",
            "needs_attention": False, "terminal_reason": "completed",
            "session_id": "artificial-session", "evidence_directory": "/tmp/artificial-peer",
            "message": "Assess the answer and durable Multithread evidence; a returned turn is not workflow completion.",
        }
        envelope.update(changes)
        before = deepcopy(envelope)
        output = io.StringIO()
        with redirect_stdout(output):
            provider._display_peer(envelope)
        self.assertEqual(before, envelope)
        return output.getvalue()

    def interpret(self, *, process_exit_code=0, **changes):
        temporary = tempfile.TemporaryDirectory(prefix="multithread-peer-output-", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name)
        session = "00000000-0000-4000-8000-000000000001"
        native = {"type": "result", "subtype": "success", "is_error": False,
                  "session_id": session, "result": "The reviewed change handles the boundary case.",
                  "terminal_reason": "completed", "errors": [], "permission_denials": []}
        native.update(changes)
        body = json.dumps(native).encode()
        path = directory / "stdout.json"
        path.write_bytes(body)
        envelope = {"state": "uncertain", "requested_session_id": session,
                    "session_id": None, "result": None, "needs_attention": True,
                    "process_exit_code": process_exit_code, "evidence_directory": str(directory)}
        provider._interpret(directory, envelope)
        self.assertEqual(body, path.read_bytes(), "Interpretation changed retained provider evidence")
        return envelope

    def test_clean_return_preserves_answer_without_an_attention_warning(self):
        output = self.display()
        self.assertIn("Multithread peer: returned", output)
        self.assertIn("The reviewed change handles the boundary case.", output)
        self.assertIn("a returned turn is not workflow completion", output)
        self.assertIn("Peer session: artificial-session", output)
        self.assertIn("Local evidence: /tmp/artificial-peer", output)
        self.assertNotIn("Needs attention", output)
        self.assertNotIn("Provider stopping reason", output)
        self.assertNotIn("Next:", output)

    def test_interpreted_clean_result_keeps_the_existing_human_output(self):
        envelope = self.interpret()
        output = self.display(**envelope)
        self.assertEqual("returned", envelope["state"])
        self.assertEqual(
            "Multithread peer: returned\n"
            "The reviewed change handles the boundary case.\n"
            "Assess the answer and durable Multithread evidence; a returned turn is not workflow completion.\n"
            f"Peer session: {envelope['session_id']}\n"
            f"Local evidence: {envelope['evidence_directory']}\n", output)

    def test_provider_error_without_answer_exposes_cause_and_retained_diagnostic(self):
        envelope = self.interpret(subtype="error_max_turns", is_error=True, result=None,
                                  errors=["Reached the configured turn limit"])
        output = self.display(**envelope)
        self.assertEqual("provider_error", envelope["state"])
        self.assertIsNone(envelope["result"])
        self.assertTrue(envelope["needs_attention"])
        self.assertIn("The provider marked its result as an error.", envelope["message"])
        self.assertIn("Provider result: error_max_turns", output)
        self.assertIn("Provider error: Reached the configured turn limit", output)
        self.assertIn("Needs attention: yes.", output)
        self.assertNotIn("Assess the answer", output)
        self.assertNotIn("The reviewed change handles", output)

    def test_provider_error_message_distinguishes_subtype_and_process_exit(self):
        cases = (
            ({"subtype": "error_max_turns", "result": None}, "The provider returned a non-success result."),
            ({"process_exit_code": 4}, "The provider process exited with code 4."),
            ({"process_exit_code": None}, "The provider process exit was not observed."),
        )
        for changes, cause in cases:
            with self.subTest(changes=changes):
                envelope = self.interpret(**changes)
                self.assertEqual("provider_error", envelope["state"])
                self.assertIn(cause, envelope["message"])
                self.assertNotIn("Assess the answer", envelope["message"])
                if changes.get("result", "present") is not None:
                    self.assertIn("The reviewed change handles the boundary case.", self.display(**envelope))

    def test_multiple_provider_errors_report_retained_count_and_bounds(self):
        for errors in (["First diagnostic", "Other diagnostic"],
                       ["First diagnostic " + "x" * 2100, *["Other diagnostic"] * 8]):
            with self.subTest(count=len(errors)):
                envelope = self.interpret(subtype="s" * 2100, is_error=True, result=None, errors=errors)
                output = self.display(**envelope)
                retained = min(8, len(errors))
                self.assertEqual(retained, len(envelope["provider_errors"]))
                self.assertEqual(len(errors) > 8, envelope["provider_errors_truncated"])
                self.assertIn("Provider result: " + "s" * 2000 + " [Detail truncated.]", output)
                self.assertIn("Provider error: " + errors[0][:2000], output)
                self.assertIn(f"showing 1 of {retained} retained diagnostics", output)
                self.assertNotIn("Other diagnostic", output)
                self.assertNotIn("s" * 2001, output)
                if len(errors[0]) > 2000:
                    self.assertNotIn(errors[0][:2001], output)
                self.assertEqual(len(errors) > 8, "details were truncated" in output)

    def test_mismatched_observed_session_is_visible_without_becoming_a_resume_target(self):
        observed = "00000000-0000-4000-8000-000000000002"
        envelope = self.interpret(session_id=observed, result="Unverified foreign answer")
        output = self.display(**envelope)
        self.assertEqual("uncertain", envelope["state"])
        self.assertEqual(observed, envelope["observed_session_id"])
        self.assertIsNone(envelope["session_id"])
        self.assertIsNone(envelope["result"])
        self.assertIn("Requested session (unverified): " + envelope["requested_session_id"], output)
        self.assertIn("Observed session (unverified): " + observed, output)
        self.assertIn("without automatically resuming either identity", output)
        self.assertNotIn("Peer session:", output)
        self.assertNotIn("--resume", output)
        self.assertNotIn("Unverified foreign answer", output)

    def test_timeout_stdout_count_does_not_expose_raw_output_or_imply_idle_provider(self):
        for count in (0, 37):
            with self.subTest(bytes=count):
                output = self.display(
                    state="uncertain", result=None, needs_attention=True, session_id=None,
                    stdout_observation={"bytes": count, "sha256": "artificial-private-digest",
                                        "truncated": False, "scope": "bounded_read",
                                        "raw": "artificial-private-output-secret"},
                    message="Call interrupted or timed out; inspect retained output before any follow-up.",
                )
                if count:
                    self.assertIn(f"{count} bytes observed", output)
                else:
                    self.assertIn("no bytes observed", output)
                    self.assertIn("provider activity is unknown", output)
                self.assertNotIn("artificial-private-output-secret", output)
                self.assertNotIn("artificial-private-digest", output)
                self.assertNotIn("Output capture was truncated", output)

    def test_bounded_stdout_read_is_distinct_from_truncated_capture(self):
        output = self.display(
            state="uncertain", result=None, needs_attention=True, session_id=None,
            stdout_observation={"bytes": 33, "sha256": "artificial-private-digest",
                                "truncated": True, "scope": "bounded_read"},
        )
        self.assertIn("33 bytes observed", output)
        self.assertIn("byte count and digest cover only the read prefix", output)
        self.assertIn("retained stdout.json", output)
        self.assertNotIn("only the captured prefix is available", output)
        self.assertNotIn("Output capture was truncated", output)
        self.assertNotIn("artificial-private-digest", output)

    def test_unavailable_stdout_observation_does_not_claim_empty_output(self):
        output = self.display(
            state="uncertain", result=None, needs_attention=True, session_id=None,
            stdout_observation_error="unavailable; byte count and digest are unknown",
        )
        self.assertIn("Output observation: unavailable; byte count and digest are unknown", output)
        self.assertNotIn("no bytes observed", output)
        self.assertNotIn("0 bytes", output)

    def test_requested_session_stays_unverified_and_bounded(self):
        requested = "artificial-requested-session-" + "x" * 2100
        output = self.display(state="uncertain", result=None, needs_attention=True,
                              session_id=None, requested_session_id=requested)
        self.assertIn("Requested session (unverified): " + requested[:2000] + " [Detail truncated.]", output)
        self.assertNotIn(requested[:2001], output)
        self.assertNotIn("Peer session:", output)
        self.assertNotIn("--resume", output)
        verified = self.display(requested_session_id=requested)
        self.assertIn("Peer session: artificial-session", verified)
        self.assertNotIn("Requested session", verified)

    def test_returned_answer_survives_visible_recording_failure(self):
        output = self.display(needs_attention=True,
                              evidence_recording="unavailable; preserve this returned result")
        self.assertIn("Multithread peer: returned", output)
        self.assertIn("The reviewed change handles the boundary case.", output)
        self.assertIn("Needs attention: yes.", output)
        self.assertIn("Evidence recording: unavailable; preserve this returned result", output)
        self.assertIn("Next: inspect the local evidence and any task artifacts", output)
        self.assertIn("a returned turn is not workflow completion", output)

    def test_partial_input_and_control_fault_keep_their_distinct_causes(self):
        output = self.display(
            state="uncertain", result=None, needs_attention=True,
            native_input_write_error="the native input pipe closed with unwritten bytes",
            control_fault={"state": "unavailable", "operation": "resolve",
                           "detail": "Input observation or recording is unavailable; new dispatch stopped.",
                           "private_input": "artificial-private-content"},
            message="Submitted native input has no observed result covering it; its outcome is unknown. Do not resend it automatically.",
        )
        self.assertIn("Multithread peer: uncertain", output)
        self.assertIn("Native input: the native input pipe closed with unwritten bytes", output)
        self.assertIn("Peer input: Input observation or recording is unavailable; new dispatch stopped.", output)
        self.assertIn("Do not resend it automatically.", output)
        self.assertNotIn("artificial-private-content", output)
        self.assertNotIn("'operation'", output)

    def test_incomplete_cleanup_does_not_hide_or_reclassify_answer(self):
        output = self.display(
            needs_attention=True,
            server_cleanup="owned process stopped after stdin closed",
            owned_process_cleanup="termination requested; process exit remains unverified",
            stdout_completion="incomplete after owned cleanup",
            stdout_observation={"truncated": True, "sha256": "artificial-digest"},
        )
        self.assertIn("Multithread peer: returned", output)
        self.assertIn("The reviewed change handles the boundary case.", output)
        self.assertIn("Provider cleanup: owned process stopped after stdin closed", output)
        self.assertIn("Process exit: termination requested; process exit remains unverified", output)
        self.assertIn("Output completion: incomplete after owned cleanup", output)
        self.assertIn("only the captured prefix is available", output)
        self.assertNotIn("artificial-digest", output)

    def test_non_normal_stopping_reason_is_visible_and_bounded(self):
        output = self.display(needs_attention=True, terminal_reason="awaiting_input")
        self.assertIn("Provider stopping reason: awaiting_input", output)
        output = self.display(needs_attention=True, terminal_reason="x" * 2100)
        self.assertIn("Provider stopping reason: " + "x" * 2000 + " [Detail truncated.]", output)
        self.assertNotIn("x" * 2001, output)
        output = self.display(terminal_reason={"private_input": "artificial-private-content"})
        self.assertNotIn("artificial-private-content", output)

    def test_bounded_additional_result_keeps_its_attribution_and_capture_limit(self):
        output = self.display(
            needs_attention=True, native_results_truncated=True,
            native_results=[{"related": False, "result_excerpt": "A separate background result.",
                             "result_excerpt_truncated": True,
                             "private_native_field": "artificial-private-content"}],
            stdout_observation={"truncated": True},
        )
        self.assertIn("The reviewed change handles the boundary case.", output)
        self.assertIn("Additional native session result (not attributed to this call's submitted input):\nA separate background result.", output)
        self.assertIn("Excerpt truncated", output)
        self.assertIn("Native result history is incomplete", output)
        self.assertIn("only the captured prefix is available", output)
        self.assertNotIn("artificial-private-content", output)

    def test_preparation_failure_does_not_invent_retained_evidence(self):
        output = self.display(state="unavailable", result=None, needs_attention=True,
                              session_id=None, evidence_directory=None,
                              message="The provider executable is unavailable.")
        self.assertIn("The provider executable is unavailable.", output)
        self.assertIn("Next: inspect the reported condition before deciding whether to retry.", output)
        self.assertNotIn("Local evidence:", output)
        self.assertNotIn("Next: inspect the local evidence", output)


if __name__ == "__main__":
    unittest.main()
