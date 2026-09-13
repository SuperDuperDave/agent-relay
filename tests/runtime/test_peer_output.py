"""Human peer summaries preserve answers while exposing unresolved observations."""

from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import shlex
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from relay_runtime import provider


class PeerOutputTests(unittest.TestCase):
    def display(self, *, report_entry=None, **changes):
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
            provider._display_peer(envelope, report_entry=report_entry)
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

    def test_elapsed_time_and_requested_session_mode_are_typed_observations(self):
        for resumed, mode in ((False, "fresh"), (True, "resume")):
            with self.subTest(resumed=resumed):
                output = self.display(elapsed_seconds=3.25, resumed=resumed)
                self.assertIn("Elapsed seconds:", output)
                self.assertIn("3.25", output)
                mode_line = next(line for line in output.splitlines() if line.startswith("Requested session mode:"))
                self.assertIn(mode, mode_line)
                self.assertNotIn("Needs attention", output)
        self.assertIn("Elapsed seconds:", self.display(elapsed_seconds=0))
        for invalid in (None, True, -1, float("inf"), float("nan"), "ARTIFICIAL-PRIVATE-TIMING", {}):
            with self.subTest(invalid=invalid):
                output = self.display(elapsed_seconds=invalid, resumed=invalid)
                self.assertNotIn("Elapsed seconds:", output)
                if type(invalid) is not bool:
                    self.assertNotIn("Requested session mode:", output)
                self.assertNotIn("ARTIFICIAL-PRIVATE-TIMING", output)

    def test_task_submission_is_shown_only_as_a_typed_attention_observation(self):
        for submission in ("not_submitted", "requested", "accepted"):
            with self.subTest(submission=submission):
                output = self.display(state="uncertain", result=None, needs_attention=True,
                                      task_submission=submission)
                self.assertIn("Recorded task submission: " + submission, output)
                self.assertIn("acceptance is not task completion", output)
                self.assertNotIn("Recorded task submission:", self.display(task_submission=submission))
        for invalid in (None, False, [], {}, "ARTIFICIAL-PRIVATE-SUBMISSION"):
            with self.subTest(invalid=invalid):
                output = self.display(needs_attention=True, task_submission=invalid)
                self.assertIn("Recorded task submission: unknown", output)
                self.assertNotIn("ARTIFICIAL-PRIVATE-SUBMISSION", output)

    def test_optional_native_measurements_do_not_reclassify_a_valid_answer(self):
        native_fields = {"num_turns": ("provider_turns", 2),
                         "duration_ms": ("provider_duration_ms", 1250),
                         "total_cost_usd": ("estimated_cost_usd", 0.0125)}
        valid = {name: expected for name, (_, expected) in native_fields.items()}
        for native_field, (field, _) in native_fields.items():
            for invalid in (-1, True, "12", "ARTIFICIAL-PRIVATE-MEASUREMENT"):
                with self.subTest(field=field, invalid=invalid):
                    envelope = self.interpret(**{**valid, native_field: invalid})
                    self.assertEqual("returned", envelope["state"])
                    self.assertEqual("The reviewed change handles the boundary case.", envelope["result"])
                    self.assertFalse(envelope["needs_attention"])
                    self.assertEqual([], envelope["permission_denials"])
                    self.assertIsNone(envelope[field])
                    self.assertEqual([field], envelope["measurement_errors"])
                    for other, expected in native_fields.values():
                        if other != field:
                            self.assertEqual(expected, envelope[other])
                    output = self.display(**envelope)
                    self.assertIn("Some provider measurements were invalid", output)
                    self.assertNotIn("Needs attention", output)
                    self.assertNotIn("ARTIFICIAL-PRIVATE-MEASUREMENT", output)

    def test_optional_usage_keeps_valid_counters_and_qualifies_private_model_metrics(self):
        private = "ARTIFICIAL-PRIVATE-MODEL-MEASUREMENT"
        envelope = self.interpret(
            usage={"input_tokens": True, "output_tokens": 4,
                   "cache_creation": {"ephemeral_5m_input_tokens": -1, "ephemeral_1h_input_tokens": 9}},
            modelUsage={private: {"inputTokens": private, "outputTokens": 6, "costUSD": 0.125}},
            permission_denials=[{"tool_name": "Write", "tool_use_id": "fixture-tool",
                                "tool_input": private}])
        self.assertEqual("returned", envelope["state"])
        self.assertIsNotNone(envelope["result"])
        self.assertTrue(envelope["needs_attention"])
        self.assertEqual([{"tool_name": "Write", "tool_use_id": "fixture-tool"}], envelope["permission_denials"])
        self.assertEqual({"input_tokens": None, "output_tokens": 4,
                          "cache_creation": {"ephemeral_5m_input_tokens": None, "ephemeral_1h_input_tokens": 9}},
                         envelope["usage"])
        self.assertEqual({"inputTokens": None, "outputTokens": 6, "costUSD": 0.125},
                         envelope["model_usage"][private])
        self.assertEqual({"usage.input_tokens", "usage.cache_creation.ephemeral_5m_input_tokens",
                          "model_usage.model.inputTokens"}, set(envelope["measurement_errors"]))
        self.assertNotIn(private, json.dumps(envelope["measurement_errors"]))
        self.assertNotIn(private, self.display(**envelope))
        self.assertEqual("native_main_loop", envelope["usage_scope_id"])
        self.assertEqual("native_query_cumulative", envelope["model_usage_scope_id"])
        self.assertEqual("cumulative_through_latest_native_result", envelope["cost_scope_id"])

    def test_absent_null_and_unknown_optional_fields_do_not_invent_measurement_errors(self):
        for changes in ({}, {"num_turns": None, "duration_ms": None, "total_cost_usd": None,
                             "usage": None, "modelUsage": None},
                        {"usage": {"input_tokens": None, "future_counter": "uninterpreted"},
                         "modelUsage": {"fixture-model": {"outputTokens": None, "future_counter": "uninterpreted"}}}):
            with self.subTest(changes=changes):
                envelope = self.interpret(**changes)
                self.assertEqual("returned", envelope["state"])
                self.assertFalse(envelope["needs_attention"])
                self.assertFalse(envelope.get("measurement_errors"))
                self.assertNotIn("Some provider measurements were invalid", self.display(**envelope))

    def test_malformed_required_result_fields_still_refuse_an_answer(self):
        for changes in ({"result": None}, {"is_error": "false"}, {"subtype": []},
                        {"errors": "ARTIFICIAL-PRIVATE-ERROR"}, {"errors": [7]},
                        {"permission_denials": ["ARTIFICIAL-PRIVATE-DENIAL"]},
                        {"session_id": "00000000-0000-4000-8000-000000000002"}):
            with self.subTest(changes=changes):
                envelope = self.interpret(num_turns="invalid optional metric", **changes)
                self.assertEqual("uncertain", envelope["state"])
                self.assertIsNone(envelope["result"])
                self.assertTrue(envelope["needs_attention"])
                self.assertFalse(envelope.get("measurement_errors"))

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
        self.assertNotIn("Support report", output)

    def test_attention_links_the_actual_private_receipt_and_read_only_report(self):
        directory = "/tmp/peer's review space"
        with mock.patch.object(provider, "account_launcher", return_value=Path("/tmp/tools/multithread")):
            output = self.display(needs_attention=True, evidence_directory=directory)
        command = next(line.split(": ", 1)[1] for line in output.splitlines()
                       if line.startswith("Support report: "))
        self.assertEqual(["/tmp/tools/multithread", "peer", "report", "--call-dir", directory, "--json"],
                         shlex.split(command))
        self.assertIn("Private result receipt: " + directory + "/result.json", output)
        self.assertIn("does not assess task completion", output)
        self.assertNotIn("--resume", command)

    def test_source_entry_report_does_not_assume_the_selected_installation_supports_it(self):
        entry = ["/usr/bin/python3", "-I", "-S", "-B", "/tmp/reviewed source/examples/call_peer.py"]
        with mock.patch.object(provider, "account_launcher", side_effect=AssertionError("wrong entry")):
            output = self.display(needs_attention=True, report_entry=entry)
        command = next(line.split(": ", 1)[1] for line in output.splitlines()
                       if line.startswith("Support report: "))
        self.assertEqual([*entry, "report", "--call-dir", "/tmp/artificial-peer", "--json"],
                         shlex.split(command))

    def test_unusual_evidence_path_preserves_exact_argv_without_terminal_controls(self):
        directory = "/tmp/peer\n\x1b[31mname"
        output = self.display(needs_attention=True, evidence_directory=directory,
                              report_entry=["/tmp/multithread", "peer"])
        self.assertNotIn("\x1b", output)
        self.assertNotIn(directory, output)
        command = next(line.split(": ", 1)[1] for line in output.splitlines()
                       if line.startswith("Support report (JSON argv): "))
        self.assertEqual(["/tmp/multithread", "peer", "report", "--call-dir", directory, "--json"],
                         json.loads(command))


if __name__ == "__main__":
    unittest.main()
