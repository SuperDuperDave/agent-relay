#!/usr/bin/env python3
"""Native provider integration outside the confined ledger worker.

Launch preserves interactive provider behavior. Peer makes one bounded Claude
print call and returns its result; it never infers ledger acknowledgement or
workflow completion. Provider configuration comes from the installed worker.
"""

import argparse
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
        raise LaunchError("Relay v0.1.0 requires a supported x86-64 Linux environment; see docs/SUPPORT.md.")
    # Relay must see the supplied components before any normalization: resolving
    # an alias here would erase a symlink its enrollment boundary should refuse.
    checkout = Path(repo)
    if not checkout.is_absolute():
        checkout = Path.cwd() / checkout
    if not checkout.is_dir():
        raise LaunchError("--repo must identify the enrolled checkout directory.")
    if relay is None:
        import pwd
        relay = Path(pwd.getpwuid(os.getuid()).pw_dir) / ".local/bin/relay"
    launcher = executable(relay, "Relay")
    provider_path = executable(provider, client)
    command = [launcher, "--repo", str(checkout), "--json", "provider-config", "--client", client]
    try:
        result = subprocess.run(command, cwd=checkout, stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=15, check=False)
    except subprocess.TimeoutExpired:
        raise LaunchError("Relay configuration timed out; observation is unavailable. Inspect installed status before retrying.") from None
    except UnicodeError:
        raise LaunchError("Relay returned unreadable configuration output; observation is unavailable.") from None
    if result.returncode != 0:
        # Give the exact native command for diagnosis without copying arbitrary
        # diagnostics into the structured launch plan.
        raise LaunchError(
            f"Relay refused launch preparation (exit {result.returncode}); run: {shlex.join(command)}")
    try:
        plan = json.loads(result.stdout)
    except (ValueError, RecursionError):
        raise LaunchError("Relay did not return a valid configuration plan; no provider was started.") from None
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
        raise LaunchError("The hook command does not match the selected Relay and checkout.")
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
    parser = argparse.ArgumentParser(description="Review invocation-only Relay hooks and start an interactive provider.")
    parser.add_argument("client", choices=("codex", "claude"))
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="enrolled checkout; default: current directory")
    parser.add_argument("--relay", type=Path, help="reviewed absolute installed launcher; default: OS-account installation")
    parser.add_argument("--provider", type=Path, help="reviewed absolute provider entry point; default: PATH lookup")
    parser.add_argument("--json", action="store_true", help="print a plan without starting a provider or asking for input")
    args = parser.parse_args(argv)
    try:
        plan = prepare(args.client, args.repo, args.relay, args.provider)
        if args.json:
            print(json.dumps(plan, sort_keys=True))
            return 0
        print(json.dumps(plan, indent=2))
        print("Review the provider path, checkout and Relay hook command above.")
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
            print(f"relay launch: {message}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("relay launch: interrupted; inspect the provider if it had already started.", file=sys.stderr)
        return 130


_MAX_TASK = 64 * 1024
_MAX_RESULT = 16 * 1024 * 1024


def _session(value):
    try:
        if str(uuid.UUID(value)) != value:
            raise ValueError()
    except (ValueError, AttributeError):
        raise argparse.ArgumentTypeError("use the exact lowercase session UUID returned by the previous call") from None
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


def _stop(process):
    # This call owns this process group only. Give the provider its normal
    # SIGTERM cleanup before escalation; never touch another native session.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    # Descendants can outlive the leader; terminate only the group we created.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def _interpret(directory, envelope):
    path = directory / "stdout.json"
    if path.stat().st_size > _MAX_RESULT:
        envelope["message"] = "Provider output exceeded the summary bound; inspect retained output before continuing."
        return
    try:
        native = json.loads(path.read_bytes())
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
        envelope["message"] = "Returned session identity does not match the requested peer; do not automatically resume it."
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
    envelope["message"] = "Assess the answer and durable Relay evidence; a returned turn is not workflow completion."


def peer_main(argv=None):
    parser = argparse.ArgumentParser(description="Call one native Claude turn and return its result to the initiating task.")
    parser.add_argument("client", choices=("claude",))
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="enrolled peer checkout")
    parser.add_argument("--relay", type=Path, help="reviewed absolute installed Relay launcher")
    parser.add_argument("--provider", type=Path, help="reviewed absolute provider entry point; default: PATH")
    parser.add_argument("--task-file", required=True, help="UTF-8 task packet; - reads stdin, at most 64 KiB")
    parser.add_argument("--resume", type=_session, help="exact peer session UUID from a previous result; no latest-session lookup")
    parser.add_argument("--output-dir", type=Path, help="new private evidence directory; default: retained temporary directory")
    parser.add_argument("--timeout", type=_positive, default=600, help="call wall-time limit in seconds (default: 600)")
    parser.add_argument("--max-turns", type=_positive, help="optional native turn limit; omitted by default")
    parser.add_argument("--dry-run", action="store_true", help="validate task/configuration and print a plan; no provider or evidence writes")
    parser.add_argument("--json", action="store_true", help="return a structured result; this DOES launch unless --dry-run is used")
    args = parser.parse_args(argv)
    session = args.resume or str(uuid.uuid4())
    envelope = {"schema": 1, "provider": "claude", "state": "unavailable",
                "requested_session_id": session, "session_id": None,
                "provider_started": False, "process_exit_code": None,
                "evidence_directory": None, "result": None, "needs_attention": True,
                "hook_delivery": "unknown", "provider_tools": "unknown",
                "relay_acknowledgement": "not_checked", "workflow_completion": "not_checked",
                "authentication": "inherited from provider; not verified",
                "elapsed_seconds": None, "usage": None, "actual_billed_cost": "unknown"}
    directory = None
    process = None
    code = 1
    stage = "task_read"
    try:
        task = _task(args.task_file)
        stage = "relay_configuration"
        plan = prepare("claude", args.repo, args.relay, args.provider)
        native = [*plan["argv"], "--print", "--output-format", "json",
                  "--permission-prompts", "none"]
        if args.max_turns is not None:
            native.extend(["--max-turns", str(args.max_turns)])
        native.extend(["--resume" if args.resume else "--session-id", session])
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
        _record(directory, "request.json", {"schema": 1, "argv": native, "repo": plan["repo"],
                                           "requested_session_id": session, "resumed": bool(args.resume),
                                           "task_sha256": hashlib.sha256(task).hexdigest(),
                                           "timeout_seconds": args.timeout})
        with _private_file(directory, "task.txt") as stream:
            stream.write(task)
        # This durable breadcrumb survives an interrupted caller. Provider stdout
        # and stderr can contain private task context; they are never auto-published.
        print(f"relay peer: session {session}; local evidence {directory}", file=sys.stderr, flush=True)
        started = time.monotonic()
        with (_private_file(directory, "stdout.json") as output,
              _private_file(directory, "stderr.txt") as errors):
            try:
                stage = "provider_spawn"
                process = subprocess.Popen(native, cwd=plan["repo"], stdin=subprocess.PIPE,
                                           stdout=output, stderr=errors, start_new_session=True)
                envelope.update(state="uncertain", provider_started=True)
                stage = "provider_call"
                process.communicate(input=task, timeout=args.timeout)
            except (subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
                if process is not None:
                    _stop(process)
                envelope["message"] = "Call interrupted or timed out; work may have occurred. Inspect evidence and Relay state before any follow-up."
                code = 130 if isinstance(exc, KeyboardInterrupt) else 1
            finally:
                envelope["elapsed_seconds"] = round(time.monotonic() - started, 3)
                if process is not None:
                    envelope["process_exit_code"] = process.returncode
        if "message" not in envelope:
            stage = "result_read"
            _interpret(directory, envelope)
            code = 0 if envelope["state"] == "returned" else 1
    except (LaunchError, OSError, UnicodeError) as exc:
        envelope["unavailable_stage"] = stage
        envelope["message"] = (str(exc) if isinstance(exc, LaunchError) else
                               f"Unavailable during {stage}; inspect the selected path or retained evidence.")
    except (KeyboardInterrupt, EOFError):
        if process is not None:
            _stop(process)
        envelope["message"] = "Interrupted; inspect any retained evidence before retrying."
        code = 130
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
        print(f"Relay peer: {envelope['state']}")
        if envelope["result"]:
            print(envelope["result"])
        print(envelope.get("message", "Inspect the peer result."))
        if envelope["session_id"]:
            print(f"Peer session: {envelope['session_id']}")
        if envelope["evidence_directory"]:
            print(f"Local evidence: {envelope['evidence_directory']}")
        if envelope.get("permission_denials"):
            print("Permission requests were denied; review them in the local result before continuing.")
    return code
