#!/usr/bin/env python3
"""Native provider integration outside the confined ledger worker.

Launch preserves interactive provider behavior. Peer makes one bounded native
call and returns its result; it never infers ledger acknowledgement or
workflow completion. Provider configuration comes from the installed worker.
"""

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import platform
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
import uuid
from . import account_launcher


class LaunchError(Exception):
    pass


def check_native_arguments(client, command, arguments):
    """Accept only the invocation-only hook shape emitted by public schema 1."""
    events = ["SessionStart", "UserPromptSubmit", "Stop", "SessionEnd"]
    if client == "codex":
        events.append("Interrupt")
    expected = {name: [{"hooks": [{"type": "command", "command": command, "timeout": 3}]}]
                for name in events}
    try:
        if client == "claude":
            if len(arguments) != 2 or arguments[0] != "--settings":
                raise ValueError()
            parsed = json.loads(arguments[1])
        else:
            if len(arguments) != 2 * len(events):
                raise ValueError()
            hooks = {}
            for index in range(0, len(arguments), 2):
                if arguments[index] != "-c":
                    raise ValueError()
                entry = tomllib.loads(arguments[index + 1])
                value = entry.get("hooks")
                if set(entry) != {"hooks"} or not isinstance(value, dict) or set(hooks) & set(value):
                    raise ValueError()
                hooks.update(value)
            parsed = {"hooks": hooks}
        if parsed != {"hooks": expected}:
            raise ValueError()
    except (ValueError, TypeError, RecursionError):
        raise LaunchError("Native arguments do not match the invocation-only hook plan; no provider was started.") from None


def executable(value, name):
    selected = str(value) if value is not None else shutil.which(name)
    if not selected:
        raise LaunchError(f"{name} was not found; select its reviewed absolute path with --provider.")
    path = Path(selected)
    if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
        raise LaunchError(f"{name} must be an executable at an absolute path.")
    # Preserve the entry path: resolving a provider's symlink can change how its
    # ordinary launcher finds companion executables or selects configuration.
    return str(path)


def prepare(client, repo, relay, provider):
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        raise LaunchError("This Multithread release requires a supported x86-64 Linux environment; see docs/SUPPORT.md.")
    # Multithread must see the supplied components before any normalization: resolving
    # an alias here would erase a symlink its enrollment boundary should refuse.
    checkout = Path(repo)
    if not checkout.is_absolute():
        checkout = Path.cwd() / checkout
    if not checkout.is_dir():
        raise LaunchError("--repo must identify the enrolled checkout directory.")
    if relay is None:
        relay = account_launcher()
    launcher = executable(relay, "Multithread")
    provider_path = executable(provider, client)
    command = [launcher, "--repo", str(checkout), "--json", "provider-config", "--client", client]
    if Path(launcher).name == "multithread":
        command.extend(["--launcher-name", "multithread"])
    try:
        result = subprocess.run(command, cwd=checkout, stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=15, check=False)
    except subprocess.TimeoutExpired:
        raise LaunchError("Multithread configuration timed out; observation is unavailable. Inspect installed status before retrying.") from None
    except UnicodeError:
        raise LaunchError("Multithread returned unreadable configuration output; observation is unavailable.") from None
    if result.returncode != 0:
        # Give the exact native command for diagnosis without copying arbitrary
        # diagnostics into the structured launch plan.
        raise LaunchError(
            f"Multithread refused launch preparation (exit {result.returncode}); run: {shlex.join(command)}")
    try:
        plan = json.loads(result.stdout)
    except (ValueError, RecursionError):
        raise LaunchError("Multithread did not return a valid configuration plan; no provider was started.") from None
    if not isinstance(plan, dict) or type(plan.get("schema")) is not int or plan["schema"] != 1:
        raise LaunchError("Unsupported configuration plan; no provider was started.")
    if plan.get("provider") != client or plan.get("repo") != str(checkout):
        raise LaunchError("The configuration plan does not match this provider and checkout.")
    if any(plan.get(key) is not False for key in
           ("launches_provider", "changes_provider_settings", "changes_permissions")):
        raise LaunchError("The configuration plan exceeds invocation-only setup.")
    hook_command = plan.get("hook_command")
    if not isinstance(hook_command, str):
        raise LaunchError("The configuration plan has an invalid hook command.")
    try:
        hook = shlex.split(hook_command)
    except ValueError:
        raise LaunchError("The configuration plan has an invalid hook command.") from None
    if hook != [launcher, "--repo", str(checkout), "provider-hook", "--client", client]:
        raise LaunchError("The hook command does not match the selected Multithread and checkout.")
    arguments = plan.get("native_arguments")
    if (not isinstance(arguments, list) or not arguments
            or any(not isinstance(arg, str) or "\0" in arg for arg in arguments)):
        raise LaunchError("The configuration plan has invalid native arguments.")
    check_native_arguments(client, hook_command, arguments)
    return {"schema": 1, "state": "launch_prepared", "provider": client,
            "repo": str(checkout), "argv": [provider_path, *arguments],
            "relay_plan": plan, "provider_started": False,
            "hook_delivery": "unknown", "provider_tools": "unknown"}


def launch_main(argv=None):
    parser = argparse.ArgumentParser(prog="multithread launch", description="Review invocation-only Multithread hooks and start an interactive provider.")
    parser.add_argument("client", choices=("codex", "claude"))
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="enrolled checkout; default: current directory")
    parser.add_argument("--multithread", "--relay", dest="relay", type=Path, help="reviewed absolute installed launcher; --relay is a compatibility spelling")
    parser.add_argument("--provider", type=Path, help="reviewed absolute provider entry point; default: PATH lookup")
    parser.add_argument("--json", action="store_true", help="print a plan without starting a provider or asking for input")
    args = parser.parse_args(argv)
    try:
        plan = prepare(args.client, args.repo, args.relay, args.provider)
        if args.json:
            print(json.dumps(plan, sort_keys=True))
            return 0
        print(json.dumps(plan, indent=2))
        print("Review the provider path, checkout and Multithread hook command above.")
        print("Existing provider settings and permissions remain in effect. Native hook trust is a separate step.")
        if not sys.stdin.isatty():
            raise LaunchError("Use --json to prepare a plan here, or run this command in an interactive terminal to launch.")
        if input("Type launch to start this provider: ").strip() != "launch":
            print("Provider was not started.")
            return 0
        # Normal inherited environment and terminal: no provider sandbox wrapper,
        # credentials/config relocation, shell eval, or permission-policy flags.
        return subprocess.call(plan["argv"], cwd=plan["repo"])
    except (LaunchError, OSError, KeyError) as exc:
        message = str(exc) if isinstance(exc, LaunchError) else "A selected path or command is unavailable; check your arguments."
        if args.json:
            print(json.dumps({"schema": 1, "state": "unavailable", "provider_started": False,
                              "hook_delivery": "unknown", "provider_tools": "unknown", "message": message}))
        else:
            print(f"multithread launch: {message}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("multithread launch: interrupted; inspect the provider if it had already started.", file=sys.stderr)
        return 130


_MAX_TASK = 64 * 1024
_MAX_RESULT = 16 * 1024 * 1024


def _session(value):
    try:
        if str(uuid.UUID(value)) != value:
            raise ValueError()
    except (ValueError, AttributeError, TypeError):
        raise argparse.ArgumentTypeError("use the exact lowercase session UUID returned by the previous call") from None
    return value


def _native_identity(value):
    if not isinstance(value, str) or not 0 < len(value) <= 256 or any(
            ord(character) < 32 or ord(character) == 127 for character in value):
        raise argparse.ArgumentTypeError("use the exact native identity returned by the previous call")
    return value


def _positive(value):
    number = int(value)
    if not 1 <= number <= 3600:
        raise argparse.ArgumentTypeError("use an integer from 1 through 3600")
    return number


def _task(path):
    if path == "-":
        body = sys.stdin.buffer.read(_MAX_TASK + 1)
    else:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise LaunchError("The task must be a regular UTF-8 file or stdin (-).")
            body = stream.read(_MAX_TASK + 1)
    if len(body) > _MAX_TASK:
        raise LaunchError("The task exceeds 64 KiB; reference larger artifacts from a scoped task instead.")
    try:
        text = body.decode("utf-8")
    except UnicodeError:
        raise LaunchError("The task must be UTF-8 text.") from None
    if not text.strip() or "\0" in text:
        raise LaunchError("The task must contain nonempty text without NUL bytes.")
    return body


def _private_file(directory, name):
    fd = os.open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    return os.fdopen(fd, "wb")


def _record(directory, name, value):
    with _private_file(directory, name) as stream:
        stream.write(json.dumps(value, ensure_ascii=True, sort_keys=True, indent=2).encode("utf-8"))
        stream.flush()
        os.fsync(stream.fileno())


def _drain(observer):
    from .peer_control import ControlError
    try:
        return observer.drain()
    except (ControlError, OSError):
        # An unavailable receipt must not prevent termination of our process.
        # Freeze interpretation; retain whatever raw output can still be read.
        observer.interpret = False
        observer.envelope.update(needs_attention=True,
                                 evidence_recording="Native cleanup or input receipt observation is unavailable; inspect retained evidence.")
        return False


def _wait(process, timeout, observer=None):
    if observer is None:
        return process.wait(timeout=timeout)
    deadline = time.monotonic() + timeout
    while True:
        progressed = _drain(observer)
        if process.poll() is not None and (observer.eof or observer.truncated):
            return process.returncode
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(process.args, timeout)
        if not progressed:
            time.sleep(min(0.01, remaining))


def _stop(process, observer=None):
    # This call owns this process group only. Give the provider its normal
    # SIGTERM cleanup before escalation; never touch another native session.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        _wait(process, 5, observer)
    except subprocess.TimeoutExpired:
        pass
    # Descendants can outlive the leader; terminate only the group we created.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    if observer is None:
        process.wait()
    else:
        try:
            _wait(process, 1, observer)
        except subprocess.TimeoutExpired:
            # A descriptor can outlive the process group that owns this call.
            # Reap the owned leader independently; incomplete stdout is an
            # observation limit, not grounds to suppress a validated answer.
            observer.envelope.update(needs_attention=True,
                                     stdout_completion="incomplete after owned cleanup")
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                observer.envelope["owned_process_cleanup"] = "termination requested; process exit remains unverified"
        finally:
            _drain(observer)


def _call_problem(envelope, message):
    # A validated native terminal observation survives a separate cleanup fault.
    if envelope["state"] not in ("returned", "provider_error"):
        envelope["state"] = "uncertain"
    envelope.update(needs_attention=True, message=message)


@contextmanager
def _call_signals():
    """Route ordinary caller termination through owned-process cleanup."""
    state = {"signal": None, "starting": False, "stopping": False}

    def interrupt(number, frame):
        if not state["stopping"]:
            state["signal"] = number
            if not state["starting"]:
                raise KeyboardInterrupt

    previous = {}
    try:
        for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            handler = signal.getsignal(number)
            if handler != signal.SIG_IGN:
                previous[number] = signal.signal(number, interrupt)
        yield state
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def _interpret(directory, envelope):
    path = directory / "stdout.json"
    # Bound the read itself, not just a prior stat: a native descendant may
    # still hold the output descriptor. Bind the summary to the bytes observed.
    with path.open("rb") as stream:
        body = stream.read(_MAX_RESULT + 1)
    envelope["stdout_observation"] = {"bytes": len(body), "sha256": hashlib.sha256(body).hexdigest(),
                                      "truncated": len(body) > _MAX_RESULT}
    if len(body) > _MAX_RESULT:
        envelope["message"] = "Provider output exceeded the summary bound; inspect retained output before continuing."
        return
    try:
        native = json.loads(body)
        # JSON can represent lone surrogates that cannot be delivered as UTF-8.
        # Preserve the raw result, but do not turn it into a success then fail
        # while recording or returning the interpreted evidence.
        json.dumps(native, ensure_ascii=False).encode("utf-8")
    except (ValueError, UnicodeError, RecursionError):
        envelope["message"] = "No readable final provider result; inspect retained output before continuing."
        return
    if (not isinstance(native, dict) or native.get("type") != "result"
            or type(native.get("is_error")) is not bool
            or not isinstance(native.get("subtype"), str)):
        envelope["message"] = "Unsupported final provider result; inspect retained output before continuing."
        return
    if native.get("session_id") != envelope["requested_session_id"]:
        try:
            envelope["observed_session_id"] = _session(native.get("session_id"))
        except argparse.ArgumentTypeError:
            pass
        envelope["message"] = "Returned session identity does not match the requested peer. The unverified answer and denials remain in stdout.json; inspect them without automatically resuming either identity."
        return
    text = native.get("result")
    denials = native.get("permission_denials", [])
    errors = native.get("errors", [])
    if ((not native["is_error"] and native["subtype"] == "success" and not isinstance(text, str))
            or (text is not None and not isinstance(text, str)) or not isinstance(denials, list)
            or any(not isinstance(item, dict) for item in denials)
            or not isinstance(errors, list) or any(not isinstance(item, str) for item in errors)):
        envelope["message"] = "Unsupported result or permission-denial shape; inspect retained output."
        return
    envelope.update({
        "state": "provider_error" if (native["is_error"] or native["subtype"] != "success"
                                      or envelope["process_exit_code"] != 0) else "returned",
        "session_id": native["session_id"], "result": text,
        "provider_subtype": native["subtype"], "provider_is_error": native["is_error"],
        "terminal_reason": native.get("terminal_reason"),
        "provider_errors": [item[:2000] for item in errors[:8]],
        "provider_errors_truncated": len(errors) > 8 or any(len(item) > 2000 for item in errors),
        # Tool inputs remain in private raw output, not the routine summary.
        "permission_denials": [{key: item.get(key) for key in ("tool_name", "tool_use_id")}
                               for item in denials],
        "usage": native.get("usage"), "provider_turns": native.get("num_turns"),
        "provider_duration_ms": native.get("duration_ms"),
        "estimated_cost_usd": native.get("total_cost_usd"),
        "actual_billed_cost": "unknown",
    })
    envelope["needs_attention"] = bool(denials) or envelope["state"] != "returned" or (
        native.get("terminal_reason") not in (None, "end_turn", "completed"))
    envelope["message"] = "Assess the answer and durable Multithread evidence; a returned turn is not workflow completion."


def peer_main(argv=None):
    raw = list(sys.argv[1:] if argv is None else argv)
    if raw and raw[0] == "control":
        from .peer_control import control_main
        return control_main(raw[1:])
    parser = argparse.ArgumentParser(prog="multithread peer", description="Call a native provider and return its observed result to the initiating task.")
    parser.add_argument("client", choices=("claude", "codex"))
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="enrolled peer checkout")
    parser.add_argument("--multithread", "--relay", dest="relay", type=Path, help="reviewed absolute installed launcher; --relay is a compatibility spelling")
    parser.add_argument("--provider", type=Path, help="reviewed absolute provider entry point; default: PATH")
    parser.add_argument("--task-file", required=True, help="UTF-8 task packet; - reads stdin, at most 64 KiB")
    parser.add_argument("--resume", type=_native_identity, help="exact peer session identity from a previous result; no latest-session lookup")
    parser.add_argument("--output-dir", type=Path, help="new private evidence directory; default: retained temporary directory")
    parser.add_argument("--timeout", type=_positive, default=600, help="call wall-time limit in seconds (default: 600)")
    parser.add_argument("--max-turns", type=_positive, help="optional Claude native turn limit; omitted by default")
    parser.add_argument("--live-input", action="store_true", help="enable Claude session input while this call runs; queued input may start later turns within the call timeout. Codex always exposes exact-turn input")
    parser.add_argument("--dry-run", action="store_true", help="validate task/configuration and print a plan; no provider or evidence writes")
    parser.add_argument("--json", action="store_true", help="return a structured result; this DOES launch unless --dry-run is used; exit 0 means a returned turn, so also check needs_attention and task evidence")
    args = parser.parse_args(raw)
    if args.client == "codex" and args.max_turns is not None:
        parser.error("--max-turns is a Claude option; Codex returns one native turn with its normal tool loop")
    if args.client == "claude" and args.resume is not None:
        try:
            _session(args.resume)
        except argparse.ArgumentTypeError as exc:
            parser.error(str(exc))
    with _call_signals() as interruption:
        return _run_peer(args, interruption)


def _run_peer(args, interruption):
    from .peer_control import CallControl, ControlError, ObservedControl
    session = args.resume or (str(uuid.uuid4()) if args.client == "claude" else None)
    envelope = {"schema": 1, "provider": args.client, "state": "unavailable",
                "requested_session_id": session, "session_id": None,
                "provider_started": False, "process_exit_code": None,
                "evidence_directory": None, "result": None, "needs_attention": True,
                "hook_delivery": "unknown", "provider_tools": "unknown",
                "relay_acknowledgement": "not_checked", "workflow_completion": "not_checked",
                "authentication": "inherited from provider; not verified",
                "elapsed_seconds": None, "usage": None, "actual_billed_cost": "unknown"}
    directory = None
    process = None
    observer = None
    control = None
    code = 1
    stage = "task_read"
    streaming = args.client == "codex" or args.live_input
    try:
        task = _task(args.task_file)
        stage = "relay_configuration"
        plan = prepare(args.client, args.repo, args.relay, args.provider)
        if args.client == "claude":
            native = [*plan["argv"], "--print", "--output-format",
                      "stream-json" if streaming else "json", "--permission-prompts", "none"]
            if streaming:
                native.extend(["--verbose", "--input-format", "stream-json", "--replay-user-messages"])
            if args.max_turns is not None:
                native.extend(["--max-turns", str(args.max_turns)])
            native.extend(["--resume" if args.resume else "--session-id", session])
        else:
            native = [*plan["argv"], "app-server", "--listen", "stdio://"]
        envelope["repo"] = plan["repo"]
        if args.dry_run:
            print(json.dumps({**envelope, "state": "call_prepared", "argv": native,
                              "task_sha256": hashlib.sha256(task).hexdigest(),
                              "timeout_seconds": args.timeout}, sort_keys=True))
            return 0
        stage = "evidence_setup"
        if args.output_dir is None:
            directory = Path(tempfile.mkdtemp(prefix="relay-peer-"))
        else:
            candidate = args.output_dir.absolute()
            candidate.mkdir(mode=0o700)  # Refuse an existing directory; never overwrite another call.
            directory = candidate
        envelope["evidence_directory"] = str(directory)
        if streaming:
            control = CallControl(directory, args.client)
            envelope["control"] = {"call_id": control.call["call_id"],
                                   "input_mode": control.call["input_mode"],
                                   "call_directory": str(directory)}
        _record(directory, "request.json", {"schema": 1, "argv": native, "repo": plan["repo"],
                                           "requested_session_id": session, "resumed": bool(args.resume),
                                           "task_sha256": hashlib.sha256(task).hexdigest(),
                                           "timeout_seconds": args.timeout})
        with _private_file(directory, "task.txt") as stream:
            stream.write(task)
        # This durable breadcrumb survives an interrupted caller. Provider stdout
        # and stderr can contain private task context; they are never auto-published.
        print(f"multithread peer: session {session or 'assigned by provider'}; local evidence {directory}", file=sys.stderr, flush=True)
        started = time.monotonic()
        from contextlib import nullcontext
        output_context = nullcontext(subprocess.PIPE) if streaming else _private_file(directory, "stdout.json")
        with output_context as output, _private_file(directory, "stderr.txt") as errors:
            try:
                stage = "provider_spawn"
                # Keep a cancellation pending until we own the returned handle.
                # This is parent-only deferral, not a signal mask inherited by
                # the provider and not a change to its execution permissions.
                interruption["starting"] = True
                try:
                    process = subprocess.Popen(native, cwd=plan["repo"], stdin=subprocess.PIPE,
                                               stdout=output, stderr=errors, start_new_session=True)
                finally:
                    interruption["starting"] = False
                if streaming:
                    if args.client == "codex":
                        from . import codex_peer as driver
                    else:
                        from . import claude_peer as driver
                    observer = driver.Observation(process, directory, envelope)
                if interruption["signal"] is not None:
                    raise KeyboardInterrupt
                envelope.update(state="uncertain", provider_started=True)
                stage = "provider_call"
                if not streaming:
                    process.communicate(input=task, timeout=args.timeout)
                else:
                    driver.run(process, task, plan["repo"], args.resume,
                               directory, envelope, args.timeout, control=ObservedControl(control, envelope), observer=observer,
                               **({"expected_hook": plan["relay_plan"]["hook_command"]} if args.client == "codex" else {}))
                    # EOF is the ordinary end of this owned stdio server.
                    # Retain a valid returned turn even if server shutdown
                    # needs cleanup; shutdown is not a second provider turn.
                    try:
                        # Claude may finish a reply while native background
                        # work is still running. Preserve its remaining call
                        # allowance instead of treating five seconds as a task
                        # deadline. Codex's owned server closes after its turn.
                        grace = (max(0, args.timeout - (time.monotonic() - started))
                                 if args.client == "claude" and observer.interpret else 5)
                        _wait(process, grace, observer)
                    except subprocess.TimeoutExpired:
                        interruption["stopping"] = True
                        _stop(process, observer)
                        envelope["server_cleanup"] = "owned process stopped after stdin closed"
                        envelope["needs_attention"] = True
                    code = 0 if envelope["state"] == "returned" else 1
                # The provider has exited. Finish interpreting and recording
                # its actual outcome even if an ordinary signal arrives now.
                interruption["stopping"] = True
            except (subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
                interruption["stopping"] = True
                if process is not None:
                    _stop(process, observer) if observer is not None else _stop(process)
                if process is not None:
                    envelope["provider_started"] = True
                _call_problem(envelope, "Call interrupted or timed out; inspect the observed turn, retained output and Multithread state before any follow-up.")
                code = (128 + (interruption["signal"] or signal.SIGINT)) if isinstance(exc, KeyboardInterrupt) else 1
            finally:
                envelope["elapsed_seconds"] = round(time.monotonic() - started, 3)
                if process is not None:
                    envelope["process_exit_code"] = process.returncode
        if not streaming and "message" not in envelope:
            stage = "result_read"
            _interpret(directory, envelope)
            code = 0 if envelope["state"] == "returned" else 1
        elif streaming and envelope["state"] == "returned" and process.returncode != 0:
            envelope["needs_attention"] = True
            envelope["message"] = "A native turn returned, but the provider process did not exit cleanly; inspect retained evidence."
    except (LaunchError, ControlError, OSError, UnicodeError) as exc:
        envelope["unavailable_stage"] = stage
        envelope["needs_attention"] = True
        envelope["message"] = (str(exc) if isinstance(exc, (LaunchError, ControlError)) else
                               f"Unavailable during {stage}; inspect the selected path or retained evidence.")
    except (KeyboardInterrupt, EOFError):
        interruption["stopping"] = True
        if process is not None:
            _stop(process, observer) if observer is not None else _stop(process)
            envelope["provider_started"] = True
        _call_problem(envelope, "Interrupted; inspect any retained evidence before retrying.")
        code = 128 + (interruption["signal"] or signal.SIGINT)
    finally:
        # Cleanup and the receipt should survive repeated ordinary termination
        # signals. Restore the caller's handlers when peer_main returns.
        interruption["stopping"] = True
        if process is not None:
            if process.poll() is None:
                _stop(process, observer) if observer is not None else _stop(process)
                _call_problem(envelope, "The owned provider required cleanup; inspect its observed turn and retained evidence.")
            envelope.update(provider_started=True, process_exit_code=process.returncode)
        try:
            if observer is not None:
                observer.close()
        except (ControlError, OSError) as exc:
            envelope.update(needs_attention=True, evidence_recording="Observer cleanup or input acknowledgement is unavailable; inspect retained evidence.")
            code = 1
        finally:
            if control is not None:
                try:
                    control.close("The owned provider call has ended.")
                except (ControlError, OSError):
                    envelope.update(needs_attention=True, evidence_recording="Input receipt closure is unavailable; inspect retained evidence.")
                    code = 1
        if streaming and envelope["state"] == "returned":
            code = 1 if "evidence_recording" in envelope else 0
        if "control_fault" in envelope:
            envelope["needs_attention"] = True
            code = 1
    if directory is not None:
        try:
            _record(directory, "result.json", envelope)
        except OSError:
            envelope["needs_attention"] = True
            envelope["evidence_recording"] = "unavailable; preserve this returned result"
            code = 1
    if args.json:
        print(json.dumps(envelope, ensure_ascii=True, sort_keys=True))
    else:
        _display_peer(envelope)
    return code


def _display_peer(envelope):
    """Show the observed answer and outstanding conditions without changing state."""
    print(f"Multithread peer: {envelope['state']}")
    if envelope.get("needs_attention"):
        print("Needs attention: yes.")
    if envelope["result"]:
        print(envelope["result"])
    results = envelope.get("native_results", [])
    if results and not results[-1]["related"] and results[-1].get("result_excerpt"):
        print("Additional native session result (not attributed to this call's submitted input):")
        print(results[-1]["result_excerpt"])
        if results[-1].get("result_excerpt_truncated"):
            print("[Excerpt truncated; inspect the captured native stdout for available detail.]")
    if envelope.get("native_results_truncated"):
        print("Native result history is incomplete; inspect the captured native stdout for available detail.")

    # These are selected diagnostic fields, never whole native objects or tool inputs.
    details = [(label, envelope.get(key)) for key, label in (
        ("evidence_recording", "Evidence recording"),
        ("native_input_write_error", "Native input"),
        ("server_cleanup", "Provider cleanup"),
        ("owned_process_cleanup", "Process exit"),
        ("stdout_completion", "Output completion"),
    )]
    fault = envelope.get("control_fault")
    if isinstance(fault, dict):
        details.append(("Peer input", fault.get("detail")))
    reason = envelope.get("terminal_reason")
    if reason not in (None, "end_turn", "completed"):
        details.append(("Provider stopping reason", reason))
    for label, value in details:
        if isinstance(value, str) and value:
            print(f"{label}: {value[:2000]}" + (" [Detail truncated.]" if len(value) > 2000 else ""))
    observation = envelope.get("stdout_observation")
    if isinstance(observation, dict) and observation.get("truncated"):
        print("Output capture was truncated; only the captured prefix is available.")
    if envelope.get("permission_denials"):
        print("Permission requests were denied; review them in the local result before continuing.")
    print(envelope.get("message", "Inspect the peer result."))
    if envelope["session_id"]:
        print(f"Peer session: {envelope['session_id']}")
    if envelope["evidence_directory"]:
        print(f"Local evidence: {envelope['evidence_directory']}")
    if envelope.get("needs_attention"):
        if envelope["evidence_directory"]:
            print("Next: inspect the local evidence and any task artifacts before deciding on follow-up.")
        else:
            print("Next: inspect the reported condition before deciding whether to retry.")
