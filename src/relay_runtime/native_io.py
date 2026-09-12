"""Bounded private observation of one owned native provider's stdout.

The caller owns the process, its argv and its termination. This observer owns
only the raw capture, its digest and the strict JSONL decoding both peer
drivers share, so a terminal native outcome survives that owned cleanup.
"""

import hashlib
import json
import os


MAX_OUTPUT = 16 * 1024 * 1024


class ProtocolError(Exception):
    pass


def identity(value):
    return isinstance(value, str) and 0 < len(value) <= 256 and not any(
        ord(character) < 32 or ord(character) == 127 for character in value)


def _object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate JSON member")
        value[key] = item
    return value


def _constant(value):
    raise ValueError("Non-finite JSON number")


def decode(body):
    """Read one JSONL record without duplicate, non-finite or undeliverable members."""
    try:
        value = json.loads(body, object_pairs_hook=_object, parse_constant=_constant)
        json.dumps(value, ensure_ascii=False).encode("utf-8")
    except (ValueError, UnicodeError, RecursionError):
        raise ProtocolError("Unreadable native JSONL output; inspect retained output before continuing.") from None
    if not isinstance(value, dict):
        raise ProtocolError("Native JSONL record was not an object; inspect retained output.")
    return value


class Observation:
    """Keep the bounded reader alive while the caller stops its owned process."""

    def __init__(self, process, directory, envelope):
        self.process = process
        self.envelope = envelope
        self.driver = None
        self.digest = hashlib.sha256()
        self.observed = 0
        self.truncated = False
        self.buffer = bytearray()
        self.eof = False
        self.interpret = True
        self.closed = False
        fd = os.open(directory / "stdout.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        self.output = os.fdopen(fd, "wb")
        os.set_blocking(process.stdout.fileno(), False)

    def read(self):
        if self.eof or self.truncated:
            return False
        try:
            data = os.read(self.process.stdout.fileno(), min(65536, MAX_OUTPUT + 1 - self.observed))
        except BlockingIOError:
            return False
        if not data:
            self.eof = True
            if self.buffer and self.interpret:
                raise ProtocolError("Native output ended with an incomplete JSONL record; inspect retained output.")
            return False
        self.output.write(data)
        self.output.flush()
        self.digest.update(data)
        self.observed += len(data)
        if self.observed > MAX_OUTPUT:
            self.truncated = True
            raise ProtocolError("Native output exceeded its bound; inspect the retained prefix before continuing.")
        if not self.interpret or self.driver is None:
            return True
        self.buffer.extend(data)
        while b"\n" in self.buffer:
            line, _, remainder = self.buffer.partition(b"\n")
            self.buffer = bytearray(remainder)
            if line.strip():
                previous_session = self.driver.session
                self.driver.message(line)
                if previous_session is None and self.driver.session is not None:
                    # Persist native identity before the queued task can write.
                    os.fsync(self.output.fileno())
        return True

    def fault(self, message):
        self.interpret = False
        self.buffer.clear()
        if self.driver is not None:
            self.driver.problem(message)
        else:
            self.envelope.update(needs_attention=True, message=message)

    def drain(self):
        """Read at most four ready chunks; never wait, send, retry or exceed cap."""
        if self.closed:
            return False
        progressed = False
        if self.driver is not None:
            self.driver.observation_only = True
        for _ in range(4):
            try:
                if not self.read():
                    break
                progressed = True
            except ProtocolError as exc:
                progressed = True
                self.fault(str(exc))
            except OSError:
                self.fault("Native cleanup output could not be observed; preserve the retained prefix.")
                break
        self.snapshot()
        return progressed

    def snapshot(self):
        self.envelope["stdout_observation"] = {"bytes": self.observed, "sha256": self.digest.hexdigest(),
                                               "truncated": self.truncated}
        if self.driver is not None:
            self.driver.preserve(resolve_pending=False)

    def close(self):
        if self.closed:
            return
        try:
            self.drain()
            self.output.flush()
            os.fsync(self.output.fileno())
            self.snapshot()
            if self.driver is not None:
                self.driver.preserve()
        finally:
            self.closed = True
            self.output.close()
            self.process.stdout.close()
            if self.process.stdin is not None:
                self.process.stdin.close()
