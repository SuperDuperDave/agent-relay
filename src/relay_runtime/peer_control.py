"""Private, per-call steering requests; native acceptance is not consumption.

This mailbox coordinates cooperating processes under the same OS account. It
does not authenticate agents, reopen a provider, or replay dispatched input.
"""

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pwd
import shlex
import stat
import sys
import time
import uuid


_WAIT_SECONDS = 5
_POLL_SECONDS = 0.1
_MAX_TEXT = 64 * 1024
_MAX_REQUESTS = 256
_MAX_RECORD = _MAX_TEXT + 8192
_FINAL = frozenset({"accepted", "consumed", "rejected", "uncertain"})


class ControlError(Exception):
    def __init__(self, message, state="unavailable"):
        super().__init__(message)
        self.state = state


def _uuid(value):
    try:
        if str(uuid.UUID(value)) != value:
            raise ValueError()
    except (ValueError, AttributeError, TypeError):
        raise ControlError("Use a canonical lowercase request UUID.") from None
    return value


def _identity(value):
    return isinstance(value, str) and 0 < len(value) <= 256 and not any(
        ord(character) < 32 or ord(character) == 127 for character in value)


def _target_identity(provider, session, turn):
    return _identity(session) and (_identity(turn) if provider == "codex" else turn is None)


def _text(value):
    try:
        valid = isinstance(value, str) and value.strip() and "\0" not in value and len(value.encode("utf-8")) <= _MAX_TEXT
    except UnicodeError:
        valid = False
    if not valid:
        raise ControlError("Input must contain nonempty UTF-8 text without NUL bytes, at most 64 KiB.", "rejected")
    return value


def _bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate member")
        result[key] = value
    return result


def _private(info, directory=False):
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    if not kind(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != (0o700 if directory else 0o600):
        raise ControlError("Mailbox objects must be private, owned by this OS account, and have the expected file type.")


def _directory(path):
    """Hold the selected directory without following any pathname symlinks."""
    path = Path(path).absolute()
    if ".." in path.parts:
        raise ControlError("Select the exact call directory without parent-directory components.")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in path.parts[1:]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        _private(os.fstat(fd), True)
        return fd
    except BaseException:
        os.close(fd)
        raise


class _Files:
    def __init__(self, directory, create=False):
        self.directory = Path(directory).absolute()
        self.fds = []
        try:
            parent = _directory(self.directory)
            self.fds.append(parent)
            if create:
                os.mkdir("control", 0o700, dir_fd=parent)
                os.fsync(parent)
            self.root = self.child(parent, "control")
            self.parts = {}
            for name in ("requests", "dispatches", "receipts"):
                if create:
                    os.mkdir(name, 0o700, dir_fd=self.root)
                self.parts[name] = self.child(self.root, name)
            if create:
                os.fsync(self.root)
        except BaseException:
            self.release()
            raise

    def child(self, parent, name):
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        self.fds.append(fd)
        _private(os.fstat(fd), True)
        return fd

    def release(self):
        while self.fds:
            os.close(self.fds.pop())

    @contextmanager
    def locked(self):
        fcntl.flock(self.root, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(self.root, fcntl.LOCK_UN)

    def read(self, directory, name, optional=False):
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=directory)
        except FileNotFoundError:
            if optional:
                return None
            raise
        with os.fdopen(fd, "rb") as stream:
            _private(os.fstat(stream.fileno()))
            data = stream.read(_MAX_RECORD + 1)
        try:
            value = json.loads(data, object_pairs_hook=_object)
            if len(data) > _MAX_RECORD or not isinstance(value, dict) or type(value.get("schema")) is not int or value["schema"] != 1:
                raise ValueError()
            _bytes(value)
        except (ValueError, UnicodeError, RecursionError):
            raise ControlError("Mailbox metadata is malformed or exceeds its bound; preserve it for inspection.") from None
        return value

    def publish(self, directory, name, value, replace=False):
        body = _bytes(value)
        if len(body) > _MAX_RECORD:
            raise ControlError("Mailbox record exceeds its bound.")
        temporary = ".tmp-" + str(uuid.uuid4())
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(body)
                stream.flush()
                os.fsync(stream.fileno())
            if replace:
                self.read(directory, name)  # Never replace an unknown object.
                os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
            else:
                os.link(temporary, name, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
        finally:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass
            os.fsync(directory)

    def call(self):
        value = self.read(self.root, "call.json")
        if (set(value) != {"schema", "call_id", "provider", "supported_steering", "input_mode"}
                or value["provider"] not in ("codex", "claude")
                or type(value["supported_steering"]) is not bool
                or value["supported_steering"] != (value["provider"] == "codex")
                or value["input_mode"] != ("active_turn" if value["provider"] == "codex" else "session")):
            raise ControlError("Unsupported call metadata.")
        _uuid(value["call_id"])
        return value

    def target(self, call):
        value = self.read(self.root, "target.json")
        if (set(value) != {"schema", "call_id", "session_id", "turn_id", "closed", "reason"}
                or value["call_id"] != call["call_id"] or type(value["closed"]) is not bool
                or not isinstance(value["reason"], str) or len(value["reason"]) > 2000
                or not (value["session_id"] is None and value["turn_id"] is None
                        or _target_identity(call["provider"], value["session_id"], value["turn_id"]))):
            raise ControlError("Invalid call target metadata.")
        return value

    def names(self):
        names = []
        with os.scandir(self.parts["requests"]) as entries:
            for index, entry in enumerate(entries):
                if index >= _MAX_REQUESTS * 2:
                    raise ControlError("Mailbox entry bound exceeded; preserve existing requests.")
                if entry.name.startswith(".tmp-"):
                    continue
                if not entry.name.endswith(".json"):
                    raise ControlError("Unexpected request directory entry.")
                _uuid(entry.name[:-5])
                names.append(entry.name)
        if len(names) > _MAX_REQUESTS:
            raise ControlError("Mailbox request capacity exceeded.")
        return names

    def request(self, call, identifier, optional=False):
        value = self.read(self.parts["requests"], _uuid(identifier) + ".json", optional)
        if value is None:
            return None
        if (set(value) != {"schema", "call_id", "request_id", "session_id", "turn_id", "text", "sequence"}
                or value["call_id"] != call["call_id"] or value["request_id"] != identifier
                or not _target_identity(call["provider"], value["session_id"], value["turn_id"])
                or type(value["sequence"]) is not int or not 1 <= value["sequence"] <= _MAX_REQUESTS):
            raise ControlError("Request identity or shape is invalid.")
        _text(value["text"])
        return value

    def binding(self, request):
        return {"schema": 1, **{key: request[key] for key in ("call_id", "request_id", "session_id", "turn_id")},
                "request_sha256": hashlib.sha256(_bytes(request)).hexdigest()}

    def observation(self, request):
        binding = self.binding(request)
        receipt = self.read(self.parts["receipts"], request["request_id"] + ".json", True)
        if receipt is not None:
            if (set(receipt) != set(binding) | {"state", "detail"}
                    or any(receipt.get(key) != value for key, value in binding.items())
                    or not isinstance(receipt["state"], str) or receipt["state"] not in _FINAL
                    or not isinstance(receipt["detail"], str)
                    or len(receipt["detail"]) > 2000):
                raise ControlError("Receipt does not match the immutable request.")
            return receipt
        dispatch = self.read(self.parts["dispatches"], request["request_id"] + ".json", True)
        if dispatch is not None and dispatch != binding:
            raise ControlError("Dispatch record does not match the immutable request.")
        return {**binding, "state": "pending", "dispatch_recorded": dispatch is not None,
                "detail": "No final native acknowledgement receipt is available; do not resend automatically."}

    def resolve(self, request, state, detail):
        if not isinstance(state, str) or state not in _FINAL or not isinstance(detail, str):
            raise ControlError("Invalid final receipt.")
        value = {**self.binding(request), "state": state, "detail": detail[:2000]}
        existing = self.observation(request)
        if existing["state"] != "pending":
            if existing != value:
                raise ControlError("An immutable final receipt already exists; preserve it.")
            return
        if state in ("accepted", "consumed") and not existing["dispatch_recorded"]:
            raise ControlError("Cannot record native acceptance before dispatch.")
        self.publish(self.parts["receipts"], request["request_id"] + ".json", value)


class CallControl:
    def __init__(self, directory: Path, provider: str):
        if provider not in ("codex", "claude"):
            raise ControlError("Unsupported peer provider.")
        self.files = _Files(directory, create=True)
        self.call = {"schema": 1, "call_id": str(uuid.uuid4()), "provider": provider,
                     "supported_steering": provider == "codex",
                     "input_mode": "active_turn" if provider == "codex" else "session"}
        self.target = None
        self.accepting = True
        self.closed = False
        try:
            self.files.publish(self.files.root, "call.json", self.call)
            self.files.publish(self.files.root, "target.json", self._target(False, ""))
        except BaseException:
            self.files.release()
            raise

    def _target(self, closed, reason):
        return {"schema": 1, "call_id": self.call["call_id"],
                "session_id": self.target[0] if self.target else None,
                "turn_id": self.target[1] if self.target else None,
                "closed": closed, "reason": reason[:2000]}

    def set_target(self, session_id, turn_id):
        if self.closed or not self.accepting or not _target_identity(self.call["provider"], session_id, turn_id):
            raise ControlError("Cannot advertise this native steering target.")
        with self.files.locked():
            self.files.target(self.call)
            self.target = (session_id, turn_id)
            self.files.publish(self.files.root, "target.json", self._target(False, ""), replace=True)

    def pending(self):
        if self.closed or not self.accepting:
            return []
        selected = []
        with self.files.locked():
            requests = [self.files.request(self.call, name[:-5]) for name in self.files.names()]
            for request in sorted(requests, key=lambda item: item["sequence"]):
                observation = self.files.observation(request)
                if observation["state"] != "pending" or observation["dispatch_recorded"]:
                    continue
                self.files.publish(self.files.parts["dispatches"], request["request_id"] + ".json", self.files.binding(request))
                selected.append({key: request[key] for key in ("request_id", "text", "session_id", "turn_id")})
                if len(selected) == 8:
                    break
        return selected

    def resolve(self, request_id, state, detail):
        if ((state == "accepted" and self.call["provider"] != "codex")
                or (state == "consumed" and self.call["provider"] != "claude")):
            raise ControlError("This receipt state is not established by this provider's input interface.")
        with self.files.locked():
            self.files.resolve(self.files.request(self.call, request_id), state, detail)

    def _settle(self, dispatched):
        failure = None
        for name in self.files.names():
            try:
                request = self.files.request(self.call, name[:-5])
                observation = self.files.observation(request)
                if observation["state"] == "pending" and (dispatched or not observation["dispatch_recorded"]):
                    state = "uncertain" if observation["dispatch_recorded"] else "rejected"
                    detail = ("Call closed without a native acknowledgement; do not resend automatically."
                              if state == "uncertain" else "Call closed before this request was dispatched.")
                    self.files.resolve(request, state, detail)
            except (ControlError, OSError) as exc:
                failure = failure or exc
        if failure is not None:
            raise failure

    def stop_accepting(self, reason):
        """Close publication while still allowing pending native acknowledgements."""
        if self.closed or not self.accepting:
            return
        with self.files.locked():
            self.files.target(self.call)
            self.files.publish(self.files.root, "target.json", self._target(True, str(reason)), replace=True)
            self.accepting = False
            self._settle(False)

    def close(self, reason):
        if self.closed:
            return
        failure = None
        try:
            try:
                self.stop_accepting(reason)
            except (ControlError, OSError) as exc:
                failure = exc
            with self.files.locked():
                self._settle(True)
        finally:
            self.closed = True
            self.files.release()
        if failure is not None:
            raise failure


class ObservedControl:
    """An input-channel fault stops new dispatch, not the already-authorized task.

    The caller still owns final closure of CallControl. Native observations and
    durable receipt availability remain separate when the mailbox is unavailable.
    """

    def __init__(self, owner, envelope):
        self.owner, self.envelope, self.failed = owner, envelope, False

    def attempt(self, operation, *args):
        if self.failed:
            return None
        try:
            return getattr(self.owner, operation)(*args)
        except (ControlError, OSError):
            self.failed = True
            self.envelope.update(needs_attention=True, control_fault={
                "state": "unavailable", "operation": operation,
                "detail": "Input observation or recording is unavailable; new dispatch stopped. The original native task can continue. Inspect retained evidence before any follow-up."})
            return None

    def pending(self):
        return self.attempt("pending") or []

    def set_target(self, session, turn):
        self.attempt("set_target", session, turn)

    def stop_accepting(self, reason):
        self.attempt("stop_accepting", reason)

    def resolve(self, identifier, state, detail):
        self.attempt("resolve", identifier, state, detail)


def _message(path):
    if path == "-":
        body = sys.stdin.buffer.read(_MAX_TEXT + 1)
    else:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ControlError("Select a regular UTF-8 message file, or - for stdin.")
            body = stream.read(_MAX_TEXT + 1)
    try:
        return _text(body.decode("utf-8"))
    except UnicodeError:
        raise ControlError("The message file is not valid UTF-8.", "rejected") from None


def _send(files, call, args):
    identifier = _uuid(args.request_id)
    if not _target_identity(call["provider"], args.session, args.turn):
        raise ControlError("Codex requires its exact session and turn; Claude accepts session input without a turn precondition. Omit --turn only for Claude.", "rejected")
    request = {"schema": 1, "call_id": call["call_id"], "request_id": identifier,
               "session_id": args.session, "turn_id": args.turn, "text": _message(args.message_file)}
    with files.locked():
        existing = files.request(call, identifier, True)
        if existing is not None:
            if any(existing.get(key) != value for key, value in request.items()):
                raise ControlError("This request UUID already names different input; no new input was submitted.", "rejected")
            request = existing
        else:
            target = files.target(call)
            if (target["closed"]
                    or target["session_id"] != args.session or target["turn_id"] != args.turn):
                raise ControlError("The requested identity is not an advertised open input target.", "rejected")
            count = len(files.names())
            if count >= _MAX_REQUESTS:
                raise ControlError("This call has reached its 256-request capacity.", "rejected")
            request["sequence"] = count + 1
            files.publish(files.parts["requests"], identifier + ".json", request)
    deadline = time.monotonic() + _WAIT_SECONDS
    while True:
        observation = files.observation(request)
        if observation["state"] != "pending" or time.monotonic() >= deadline:
            return observation
        time.sleep(min(_POLL_SECONDS, max(0, deadline - time.monotonic())))


def control_main(argv=None):
    parser = argparse.ArgumentParser(prog="multithread peer control", description=(
        "Send input to an owned peer or inspect its private receipt. Codex targets an exact active turn; "
        "opted-in Claude calls accept session input that may start a later turn. Exit: 0 accepted/consumed/status; "
        "1 rejected/unavailable/uncertain; 2 pending. Neither receipt establishes task completion."))
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("send", "receipt", "status"):
        command = commands.add_parser(name)
        command.add_argument("--call-dir", required=True, type=Path, help="exact peer evidence directory")
        command.add_argument("--json", action="store_true")
        if name != "status":
            command.add_argument("--request-id", required=name == "receipt", help="canonical UUID; send generates one if omitted")
        if name == "send":
            command.add_argument("--message-file", required=True, help="UTF-8 input file, or - for stdin; at most 64 KiB")
            command.add_argument("--session", required=True, help="exact native session identity; no implicit latest target")
            command.add_argument("--turn", help="required exact active turn for Codex; omit for Claude session input")
    args = parser.parse_args(argv)
    files = None
    result = {"schema": 1, "state": "unavailable", "call_directory": str(args.call_dir.absolute())}
    try:
        if args.command != "status":
            args.request_id = _uuid(args.request_id or str(uuid.uuid4()))
            result["request_id"] = args.request_id
            from . import account_launcher
            launcher = str(account_launcher())
            result["inspect_argv"] = [launcher, "peer", "control", "receipt", "--call-dir",
                                      str(args.call_dir.absolute()), "--request-id", args.request_id, "--json"]
            result["inspect_command"] = shlex.join(result["inspect_argv"])
        files = _Files(args.call_dir)
        call = files.call()
        result.update(call)
        if args.command == "status":
            target = files.target(call)
            result.update(state="closed" if target["closed"] else "open", target=target,
                          closed=target["closed"], live_process="not_verified",
                          detail="Target metadata is an observation, not proof that its owner is still running.")
        elif args.command == "send":
            result.update(session_id=args.session, turn_id=args.turn)
            print("multithread peer input: request " + args.request_id + "; call directory " + str(files.directory)
                  + "; submission not yet verified", file=sys.stderr, flush=True)
            result.update(_send(files, call, args))
        else:
            result.update(files.observation(files.request(call, _uuid(args.request_id))))
    except (ControlError, OSError, UnicodeError) as exc:
        result.update(state=exc.state if isinstance(exc, ControlError) else "unavailable",
                      detail=str(exc) if isinstance(exc, ControlError) else "Mailbox observation or recording is unavailable; preserve the call directory and inspect before retrying.")
    finally:
        if files is not None:
            files.release()
    if args.json:
        print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    else:
        print("Peer input: " + result["state"])
        print(result.get("detail", "Inspect the private call evidence."))
        for key in ("call_id", "request_id", "session_id", "turn_id", "inspect_command"):
            if key in result:
                print(key.replace("_", " ").capitalize() + ": " + str(result[key]))
        if "target" in result:
            print("Target: " + json.dumps(result["target"], sort_keys=True))
    return 0 if result["state"] in {"accepted", "consumed", "open", "closed"} else 2 if result["state"] == "pending" else 1
