"""Human peer summaries preserve answers while exposing unresolved observations."""

from contextlib import redirect_stdout
from copy import deepcopy
import io
from pathlib import Path
import sys
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
            "message": "Assess the answer and durable Relay evidence; a returned turn is not workflow completion.",
        }
        envelope.update(changes)
        before = deepcopy(envelope)
        output = io.StringIO()
        with redirect_stdout(output):
            provider._display_peer(envelope)
        self.assertEqual(before, envelope)
        return output.getvalue()

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
