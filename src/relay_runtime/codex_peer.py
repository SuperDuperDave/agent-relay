"""Bounded native Codex App Server transport; no provider policy overrides.

The caller owns the process and cleanup. This module owns only its JSONL pipes,
private stdout observation, and native response interpretation. Native request
acceptance never substitutes for a durable Multithread acknowledgement.
"""

import json
import os
from pathlib import Path
import selectors
import subprocess
import time

from .native_io import MAX_OUTPUT as _MAX_OUTPUT, Observation, ProtocolError as _ProtocolError, decode, identity as _identity


_MAX_PENDING = 128
_MAX_DETAILS = 8


class _Driver:
    def __init__(self, process, task, repo, resume, envelope, timeout, control, expected_hook=None):
        self.process = process
        self.task = task.decode("utf-8")
        self.repo = repo
        self.resume = resume
        self.envelope = envelope
        self.deadline = time.monotonic() + timeout
        self.timeout = timeout
        self.control = control
        self.expected_hook = expected_hook
        self.next_id = 1
        self.pending = {}
        self.outgoing = bytearray()
        self.session = None
        self.turn = None
        self.turn_requested = False
        self.buffered = []
        self.terminal = None
        self.messages = {}
        self.denials = []
        self.unknown_requests = []
        self.errors = []
        self.details_truncated = False
        self.seen_controls = set()
        self.stdin_closed = False
        self.interrupt_requested = False
        self.observation_only = False
        self.outcome_recorded = False
        self.had_problem = False
        self.envelope.update(state="uncertain", requested_session_id=resume,
                             needs_attention=True, task_submission="not_submitted")

    def start_thread(self):
        params = {"cwd": self.repo}
        if self.resume:
            params["threadId"] = self.resume
        self.request("thread/resume" if self.resume else "thread/start", params)

    def hooks_ready(self, result):
        groups = result.get("data")
        if not isinstance(groups, list) or any(not isinstance(group, dict) for group in groups):
            raise _ProtocolError("Native hook readiness could not be read; no task was submitted.")
        groups = [group for group in groups if group.get("cwd") == self.repo]
        if len(groups) != 1 or not isinstance(groups[0].get("hooks"), list):
            raise _ProtocolError("Native hook readiness did not identify the selected checkout; no task was submitted.")
        required = {"sessionStart", "sessionEnd", "userPromptSubmit", "stop", "interrupt"}
        matches = {name: [] for name in required}
        for hook in groups[0]["hooks"]:
            if (isinstance(hook, dict) and hook.get("source") == "sessionFlags"
                    and hook.get("command") == self.expected_hook and hook.get("eventName") in required):
                matches[hook["eventName"]].append(hook)
        ready = {name for name, values in matches.items() if len(values) == 1
                 and values[0].get("enabled") is True and values[0].get("trustStatus") == "trusted"
                 and values[0].get("handlerType") == "command" and values[0].get("async", False) is False
                 and values[0].get("matcher") is None and type(values[0].get("timeoutSec")) is int
                 and values[0]["timeoutSec"] == 3}
        self.envelope["hook_readiness"] = {"state": "ready" if ready == required else "needs_review",
                                            "ready_events": sorted(ready), "unready_events": sorted(required - ready)}
        if ready != required:
            raise _ProtocolError("Public Multithread hooks are not ready; no task was submitted. Use multithread launch codex for this checkout, review the exact Multithread commands in /hooks, then return here. Multithread does not change native hook trust.")

    def remaining(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(self.process.args, self.timeout)
        return remaining

    def send(self, value):
        if self.observation_only:
            return
        body = (json.dumps(value, ensure_ascii=True, separators=(",", ":")) + "\n").encode("utf-8")
        if self.stdin_closed or len(self.outgoing) + len(body) > _MAX_OUTPUT:
            raise _ProtocolError("Native input is unavailable or exceeded its bound; inspect retained evidence.")
        self.outgoing.extend(body)

    def request(self, method, params, control_id=None):
        if self.observation_only:
            return
        if len(self.pending) >= _MAX_PENDING:
            raise _ProtocolError("Too many unresolved native requests; inspect retained evidence before continuing.")
        identifier = self.next_id
        self.next_id += 1
        self.pending[identifier] = (method, control_id)
        self.send({"id": identifier, "method": method, "params": params})

    def detail(self, collection, value):
        if len(collection) < _MAX_DETAILS:
            collection.append(value)
        else:
            self.details_truncated = True

    def interrupt(self):
        if self.turn and self.terminal is None and not self.interrupt_requested:
            self.interrupt_requested = True
            self.request("turn/interrupt", {"threadId": self.session, "turnId": self.turn})

    def server_request(self, message):
        method, identifier = message["method"], message["id"]
        params = message.get("params")
        if not isinstance(params, dict):
            raise _ProtocolError("Malformed native server request; inspect retained output.")
        if self.observation_only:
            self.detail(self.unknown_requests, method[:200])
            return
        if method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
            result = {"decision": "decline"}
        elif method == "item/permissions/requestApproval":
            result = {"permissions": {}, "scope": "turn"}
        elif method == "mcpServer/elicitation/request":
            result = {"action": "decline", "content": None}
        elif method in ("execCommandApproval", "applyPatchApproval"):
            result = {"decision": "abort"}
        else:
            self.detail(self.unknown_requests, method[:200])
            self.send({"id": identifier, "error": {
                "code": -32601, "message": "This unattended client cannot answer the native request."}})
            self.interrupt()
            return
        item = params.get("itemId")
        self.detail(self.denials, {"tool_name": method,
                                 "tool_use_id": item if _identity(item) else None})
        self.send({"id": identifier, "result": result})

    def response(self, message):
        identifier = message["id"]
        if identifier not in self.pending or (("result" in message) == ("error" in message)):
            raise _ProtocolError("Unmatched or malformed native response; inspect retained output.")
        method, control_id = self.pending[identifier]
        if "error" in message:
            error = message["error"]
            if (not isinstance(error, dict) or type(error.get("code")) is not int
                    or not isinstance(error.get("message"), str)):
                raise _ProtocolError("Malformed native request rejection; inspect retained output.")
            if control_id is not None:
                self.control.resolve(control_id, "rejected", "Native provider rejected the exact steering request.")
                self.pending.pop(identifier)
                return
            self.detail(self.errors, error["message"][:2000])
            raise _ProtocolError("Native provider rejected " + method + "; inspect retained output before continuing.")
        result = message["result"]
        if not isinstance(result, dict):
            raise _ProtocolError("Malformed native result; inspect retained output.")
        if method == "initialize":
            self.send({"method": "initialized", "params": {}})
            if self.expected_hook is not None:
                self.request("hooks/list", {"cwds": [self.repo]})
            else:
                self.start_thread()
        elif method == "hooks/list":
            self.hooks_ready(result)
            self.start_thread()
        elif method in ("thread/start", "thread/resume"):
            thread = result.get("thread")
            if not isinstance(thread, dict) or not _identity(thread.get("id")):
                raise _ProtocolError("Native provider returned no valid thread identity; inspect retained output.")
            if self.resume and thread["id"] != self.resume:
                self.envelope["observed_session_id"] = thread["id"]
                raise _ProtocolError("Returned thread identity does not match the requested peer; no task was submitted.")
            if ("cwd" in thread and thread["cwd"] != self.repo
                    or "cwd" in result and result["cwd"] != self.repo):
                raise _ProtocolError("Native thread working directory does not match the enrolled checkout; no task was submitted.")
            self.session = thread["id"]
            self.envelope["session_id"] = self.session
            status = thread.get("status")
            history = thread.get("turns", [])
            if (status is not None and (not isinstance(status, dict)
                                       or status.get("type") not in ("idle", "notLoaded", "systemError", "active"))
                    or not isinstance(history, list) or any(not isinstance(item, dict) for item in history)):
                raise _ProtocolError("Native provider returned an invalid thread state; no task was submitted.")
            if ((isinstance(status, dict) and status["type"] in ("active", "systemError"))
                    or any(item.get("status") == "inProgress" for item in history)):
                raise _ProtocolError("The native thread is active or unavailable; no task was submitted into an existing turn.")
            native_session = thread.get("sessionId")
            if native_session is not None:
                if not _identity(native_session):
                    raise _ProtocolError("Native provider returned an invalid session-tree identity.")
                self.envelope["native_session_id"] = native_session
            reviewer = result.get("approvalsReviewer")
            if reviewer in ("user", "auto_review", "guardian_subagent"):
                self.envelope["native_approvals_reviewer"] = reviewer
            self.turn_requested = True
            self.envelope["task_submission"] = "requested"
            self.request("turn/start", {"threadId": self.session,
                                       "input": [{"type": "text", "text": self.task}]})
        elif method == "turn/start":
            turn = self.validate_turn(result.get("turn"))
            self.turn = turn["id"]
            self.envelope["turn_id"] = self.turn
            self.envelope["task_submission"] = "accepted"
            buffered, self.buffered = self.buffered, []
            for notification in buffered:
                self.notification(notification)
            if self.control is not None and self.terminal is None and not self.observation_only:
                self.control.set_target(self.session, self.turn)
            if self.unknown_requests:
                self.interrupt()
        elif method == "turn/steer":
            if result.get("turnId") != self.turn:
                if control_id is not None:
                    self.control.resolve(control_id, "uncertain", "Native steering response identified a different turn.")
                    self.pending.pop(identifier)
                raise _ProtocolError("Native steering response did not match the targeted turn.")
            if control_id is not None:
                self.control.resolve(control_id, "accepted", "Native provider accepted input into the exact active turn; consumption is not verified.")
        elif method == "turn/interrupt" and result:
            raise _ProtocolError("Malformed native interrupt acknowledgement; inspect retained output.")
        self.pending.pop(identifier)

    @staticmethod
    def validate_turn(turn):
        if (not isinstance(turn, dict) or not _identity(turn.get("id"))
                or turn.get("status") not in ("inProgress", "completed", "failed", "interrupted")
                or not isinstance(turn.get("items"), list)):
            raise _ProtocolError("Malformed native turn; inspect retained output.")
        return turn

    def item(self, item):
        if not isinstance(item, dict) or item.get("type") != "agentMessage":
            return
        if (not _identity(item.get("id")) or not isinstance(item.get("text"), str)
                or item.get("phase") not in (None, "commentary", "final_answer")):
            raise _ProtocolError("Malformed native agent message; inspect retained output.")
        self.messages.pop(item["id"], None)
        self.messages[item["id"]] = {"text": item["text"], "phase": item.get("phase"), "complete": True}

    def notification(self, message):
        method, params = message["method"], message.get("params")
        if not isinstance(params, dict):
            raise _ProtocolError("Malformed native notification; inspect retained output.")
        relevant = ("turn/started", "turn/completed", "item/completed", "item/agentMessage/delta",
                    "thread/tokenUsage/updated", "error")
        if method not in relevant:
            return
        if params.get("threadId") != self.session:
            return
        if self.turn is None:
            if self.turn_requested:
                self.buffered.append(message)
            return
        if method in ("turn/started", "turn/completed"):
            turn = self.validate_turn(params.get("turn"))
            if turn["id"] != self.turn:
                return
            if method == "turn/completed":
                if turn["status"] == "inProgress" or self.terminal is not None:
                    raise _ProtocolError("Inconsistent native turn completion; inspect retained output.")
                error = turn.get("error")
                if turn["status"] == "failed":
                    if not isinstance(error, dict) or not isinstance(error.get("message"), str):
                        raise _ProtocolError("Native failed turn had no valid failure description.")
                    self.detail(self.errors, error["message"][:2000])
                elif error is not None:
                    raise _ProtocolError("Native turn status contradicted its failure description.")
                for item in turn["items"]:
                    self.item(item)
                self.terminal = turn["status"]
                duration = turn.get("durationMs")
                if type(duration) is int and duration >= 0:
                    self.envelope["provider_duration_ms"] = duration
                self.finish(require_answer=False)
                if self.control is not None:
                    self.control.stop_accepting("The native turn has completed.")
            return
        if params.get("turnId") != self.turn:
            return
        if method == "item/completed":
            self.item(params.get("item"))
            if self.terminal is not None:
                self.finish(require_answer=False)
        elif method == "item/agentMessage/delta":
            identifier, delta = params.get("itemId"), params.get("delta")
            if not _identity(identifier) or not isinstance(delta, str):
                raise _ProtocolError("Malformed native text delta; inspect retained output.")
            item = self.messages.setdefault(identifier, {"text": "", "phase": None, "complete": False})
            if not item["complete"]:
                item["text"] += delta
        elif method == "error":
            error = params.get("error")
            if (not isinstance(error, dict) or not isinstance(error.get("message"), str)
                    or type(params.get("willRetry")) is not bool):
                raise _ProtocolError("Malformed native error observation; inspect retained output.")
            self.detail(self.errors, error["message"][:2000])
        elif method == "thread/tokenUsage/updated":
            usage = params.get("tokenUsage")
            names = ("inputTokens", "outputTokens", "cachedInputTokens", "reasoningOutputTokens", "totalTokens")
            if (not isinstance(usage, dict)
                    or any(not isinstance(usage.get(part), dict)
                           or any(type(usage[part].get(name)) is not int or usage[part][name] < 0
                                  for name in names) for part in ("last", "total"))):
                raise _ProtocolError("Malformed native token usage; inspect retained output.")
            self.envelope["usage"] = {part: {name: usage[part][name] for name in names}
                                      for part in ("last", "total")}

    def message(self, body):
        value = decode(body)
        if "id" in value and (type(value["id"]) not in (int, str)
                              or isinstance(value["id"], str) and not _identity(value["id"])):
            raise _ProtocolError("Native record has an invalid request identity.")
        if "method" in value:
            if not isinstance(value["method"], str) or "result" in value or "error" in value:
                raise _ProtocolError("Malformed native request or notification.")
            if "id" in value:
                self.server_request(value)
            else:
                self.notification(value)
        elif "id" in value:
            self.response(value)
        else:
            raise _ProtocolError("Unrecognized native JSONL record; inspect retained output.")

    def poll_control(self):
        if self.control is None or self.turn is None or self.terminal is not None or self.observation_only:
            return
        for request in self.control.pending():
            identifier = request.get("request_id")
            if not _identity(identifier) or identifier in self.seen_controls:
                raise _ProtocolError("Invalid or repeated steering control request; no duplicate input was submitted.")
            if len(self.seen_controls) >= 4096:
                raise _ProtocolError("Steering request bound reached; inspect retained evidence.")
            self.seen_controls.add(identifier)
            if (self.terminal is not None or request.get("session_id") != self.session
                    or request.get("turn_id") != self.turn):
                self.control.resolve(identifier, "rejected", "The requested native turn is not active on this call.")
                continue
            text = request.get("text")
            if not isinstance(text, str) or not text.strip() or "\0" in text or len(text.encode("utf-8")) > 64 * 1024:
                self.control.resolve(identifier, "rejected", "Steering requires bounded nonempty UTF-8 text.")
                continue
            self.request("turn/steer", {"threadId": self.session, "expectedTurnId": self.turn,
                                        "input": [{"type": "text", "text": text}]}, identifier)

    def close_stdin(self):
        if not self.stdin_closed:
            self.stdin_closed = True
            self.process.stdin.close()

    def preserve(self, resolve_pending=True):
        self.envelope["permission_denials"] = self.denials
        self.envelope["unsupported_native_requests"] = self.unknown_requests
        self.envelope["provider_errors"] = self.errors
        self.envelope["provider_errors_truncated"] = self.details_truncated
        if self.envelope.get("state") != "returned":
            partial = "\n\n".join(item["text"] for item in self.messages.values() if item["text"])
            if partial:
                self.envelope["partial_result"] = partial
        for method, identifier in self.pending.values():
            if resolve_pending and identifier is not None:
                self.control.resolve(identifier, "uncertain", "Native steering acknowledgement was not observed; do not resend automatically.")

    def problem(self, message):
        self.had_problem = True
        if not self.outcome_recorded:
            self.envelope.update(state="uncertain", result=None)
        self.envelope.update(needs_attention=True, message=message)

    def finish(self, require_answer=True):
        if self.terminal is None:
            raise _ProtocolError("Native output ended without a matching terminal turn; work may have occurred.")
        self.envelope.update(provider_subtype=self.terminal,
                             provider_is_error=self.terminal != "completed",
                             terminal_reason=self.terminal, provider_turns=1)
        if self.terminal in ("failed", "interrupted"):
            self.envelope.update(state="provider_error", needs_attention=True,
                                 message="The native turn failed or was interrupted; inspect retained output and Multithread evidence.")
            self.outcome_recorded = True
            return
        finals = [item["text"] for item in self.messages.values()
                  if item["complete"] and item["phase"] == "final_answer"]
        if not finals:
            finals = [item["text"] for item in self.messages.values()
                      if item["complete"] and item["phase"] is None]
            # Older providers omit phase on progress as well as the answer.
            # The last completed agent message is the compatibility candidate.
            finals = finals[-1:]
        if not finals:
            if not require_answer:
                return
            raise _ProtocolError("The native turn completed without a validated final answer; partial text is retained.")
        self.envelope.update(state="returned", result="\n\n".join(finals),
                             needs_attention=bool(self.denials or self.unknown_requests or self.had_problem),
                             message="Assess the answer and durable Multithread evidence; a returned turn is not workflow completion.")
        self.outcome_recorded = True


def run(process, task: bytes, repo: str, resume: str | None, directory: Path,
        envelope: dict, timeout: float, control=None, observer=None, expected_hook=None) -> None:
    """Observe a native terminal turn separately from the caller's cleanup."""
    driver = _Driver(process, task, repo, resume, envelope, timeout, control, expected_hook)
    owned_observer = observer is None
    observation = observer if observer is not None else Observation(process, directory, envelope)
    observation.driver = driver
    try:
        with selectors.DefaultSelector() as selector:
            os.set_blocking(process.stdin.fileno(), False)
            selector.register(process.stdout, selectors.EVENT_READ, "stdout")
            writing = False
            driver.request("initialize", {"clientInfo": {"name": "multithread", "title": "Multithread",
                                                         "version": "0.4.0"}})
            while not observation.eof:
                driver.poll_control()
                if driver.outgoing and not writing:
                    selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
                    writing = True
                elif not driver.outgoing and writing:
                    selector.unregister(process.stdin)
                    writing = False
                if driver.terminal and not driver.pending and not driver.outgoing:
                    driver.close_stdin()
                    driver.finish()
                    return
                for key, ready in selector.select(min(0.1, driver.remaining())):
                    if key.data == "stdin":
                        try:
                            count = os.write(process.stdin.fileno(), driver.outgoing)
                        except BlockingIOError:
                            continue
                        if count <= 0:
                            raise _ProtocolError("Native input closed before a request was delivered; inspect retained output.")
                        del driver.outgoing[:count]
                        continue
                    observation.read()
            if driver.pending or driver.outgoing:
                raise _ProtocolError("Native output ended with unresolved requests; their outcomes are uncertain.")
            driver.close_stdin()
            driver.finish()
    except _ProtocolError as exc:
        observation.fault(str(exc))
    except (subprocess.TimeoutExpired, OSError, KeyboardInterrupt):
        driver.problem("Native observation was interrupted or unavailable; preserve partial work and inspect evidence before retrying.")
        raise
    finally:
        driver.observation_only = True
        try:
            observation.snapshot()
        finally:
            driver.close_stdin()
            if owned_observer:
                observation.close()
