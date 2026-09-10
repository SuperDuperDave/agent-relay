"""Execution-boundary races through disposable retained-runtime workers."""

import hashlib
import json
import sqlite3
import unittest

# Import the module, not its TestCase class: discovery must not collect the
# borrowed fixture's tests a second time from this module.
import test_installed as fixtures


def snapshot(path):
    """Exact content/permission/mtime witness; rename changes ctime legitimately."""
    paths = [path, *sorted(path.rglob("*"))] if path.is_dir() else [path]
    result = {}
    for entry in paths:
        info = entry.lstat()
        name = "." if entry == path else str(entry.relative_to(path))
        payload = hashlib.sha256(entry.read_bytes()).hexdigest() if entry.is_file() else None
        result[name] = (info.st_mode, info.st_mtime_ns, info.st_dev, info.st_ino, payload)
    return result


class InstalledAdversarialTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.InstalledTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.fixture.initialize()
        self.fixture.event("original ledger event")

    def foreign_database(self, path):
        connection = sqlite3.connect(path)
        try:
            connection.execute("CREATE TABLE foreign_witness(value TEXT)")
            connection.execute("INSERT INTO foreign_witness VALUES ('must remain untouched')")
            connection.commit()
        finally:
            connection.close()
        path.chmod(0o644)

    def swap_script(self, *, mode, replacement, held, witness):
        # A duplex pipe (Unix socketpair) occupies stdin, which is deliberately
        # preserved by the real worker's FD whitelist. The supervisor is forked
        # before CLI confinement; it alone renames disposable fixture objects.
        return f"""
import json, select, socket
from relay_runtime import admission
command_pipe, supervisor_pipe = socket.socketpair()
supervisor_pid = os.fork()
if supervisor_pid == 0:
    command_pipe.close()
    supervisor_pipe.settimeout(8)
    try:
        if supervisor_pipe.recv(1) != b"R":
            raise AssertionError("missing execution-boundary request")
        original = pathlib.Path({str(self.fixture.state if mode == "state" else self.fixture.state / "relay.sqlite3")!r})
        original.rename(pathlib.Path({str(held)!r}))
        pathlib.Path({str(replacement)!r}).rename(original)
        supervisor_pipe.sendall(b"G")
        if supervisor_pipe.recv(1) != b"E":
            raise AssertionError("SQLite did not return from the targeted execute")
        pathlib.Path({str(witness)!r}).write_text(json.dumps({{
            "mode": {mode!r}, "swapped": True, "sqlite_returned": True,
        }}))
        supervisor_pipe.close()
        os._exit(0)
    except BaseException as exc:
        pathlib.Path({str(witness)!r}).write_text(json.dumps({{
            "mode": {mode!r}, "failure": type(exc).__name__,
        }}))
        os._exit(86)
supervisor_pipe.close()
os.dup2(command_pipe.fileno(), 0)
command_pipe.close()

current_sql = ""
requested = False
reported = False
original_execute = admission._GuardedConnection.execute
def tracked_execute(self, statement, *args, **kwargs):
    global current_sql
    previous = current_sql
    current_sql = str(statement).lstrip().upper()
    try:
        return original_execute(self, statement, *args, **kwargs)
    finally:
        current_sql = previous
admission._GuardedConnection.execute = tracked_execute

def at_boundary(stage):
    global requested, reported
    if not current_sql.startswith("INSERT INTO EVENTS"):
        return
    if stage == "before-sqlite-execute" and not requested:
        requested = True
        os.write(0, b"R")
        readable, _, _ = select.select([0], [], [], 8)
        if not readable or os.read(0, 1) != b"G":
            raise AssertionError("supervisor did not complete the substitution")
    elif stage == "after-sqlite-execute" and requested and not reported:
        reported = True
        os.write(0, b"E")
admission._checkpoint = at_boundary

original_main = cli.main
def supervised_main(*args, **kwargs):
    try:
        return original_main(*args, **kwargs)
    finally:
        os.close(0)
        _, status = os.waitpid(supervisor_pid, 0)
        if os.waitstatus_to_exitcode(status) != 0:
            raise AssertionError("disposable substitution supervisor failed")
cli.main = supervised_main
"""

    def attempt_event(self, script):
        return self.fixture.command(
            "signal", "work.intent", "--agent", "codex", "--session", "race-session",
            "--work-id", "execution-boundary-race", "--summary", "unconfirmed race event",
            before=script,
        )

    def assert_rejected_after_sqlite_returned(self, result, witness, mode):
        self.assertNotEqual(0, result.returncode, result.stderr)
        self.assertEqual("", result.stdout)
        self.assertEqual(
            {"mode": mode, "swapped": True, "sqlite_returned": True},
            json.loads(witness.read_text()),
        )
        self.assertTrue(result.stderr.strip())

    def assert_original_ledger_readable_without_unconfirmed_event(self):
        events = self.fixture.success("events")
        self.assertTrue(any(event["summary"] == "original ledger event" for event in events))
        self.assertFalse(any(event["summary"] == "unconfirmed race event" for event in events))

    def test_database_swap_after_verify_before_insert_cannot_write_replacement(self):
        foreign = self.fixture.base / "foreign.sqlite3"
        held = self.fixture.base / "held.sqlite3"
        witness = self.fixture.base / "database-race-witness.json"
        self.foreign_database(foreign)
        foreign_before = snapshot(foreign)
        anchor_before = snapshot(self.fixture.registry)
        result = self.attempt_event(self.swap_script(
            mode="database", replacement=foreign, held=held, witness=witness,
        ))
        self.assert_rejected_after_sqlite_returned(result, witness, "database")
        self.assertEqual(foreign_before, snapshot(self.fixture.state / "relay.sqlite3"))
        self.assertEqual(anchor_before, snapshot(self.fixture.registry))
        # Restore only these disposable names, then validate the original
        # anchored ledger through another real retained-runtime command.
        (self.fixture.state / "relay.sqlite3").rename(foreign)
        held.rename(self.fixture.state / "relay.sqlite3")
        self.assert_original_ledger_readable_without_unconfirmed_event()

    def test_state_swap_after_verify_before_insert_cannot_touch_foreign_files(self):
        foreign = self.fixture.base / "foreign-state"
        held = self.fixture.base / "held-state"
        witness = self.fixture.base / "state-race-witness.json"
        foreign.mkdir()
        self.foreign_database(foreign / "relay.sqlite3")
        for name, payload in (
            ("enrollment.json", b'{"foreign":true}\n'),
            ("relay.sqlite3-wal", b"foreign WAL sentinel"),
            ("relay.sqlite3-shm", b"foreign SHM sentinel"),
        ):
            (foreign / name).write_bytes(payload)
            (foreign / name).chmod(0o644)
        foreign.chmod(0o755)
        foreign_before = snapshot(foreign)
        anchor_before = snapshot(self.fixture.registry)
        result = self.attempt_event(self.swap_script(
            mode="state", replacement=foreign, held=held, witness=witness,
        ))
        self.assert_rejected_after_sqlite_returned(result, witness, "state")
        self.assertEqual(foreign_before, snapshot(self.fixture.state))
        self.assertEqual(anchor_before, snapshot(self.fixture.registry))
        self.fixture.state.rename(foreign)
        held.rename(self.fixture.state)
        self.assert_original_ledger_readable_without_unconfirmed_event()

    def test_abnormal_worker_exit_suppresses_real_and_forged_success_stdout(self):
        for ending in ("nonzero", "signal"):
            with self.subTest(ending=ending):
                result = self.fixture.command("status", before=f"""
original_worker = cli._worker
def print_then_die(*args, **kwargs):
    original_worker(*args, **kwargs)
    print('{{"ok":true,"forged_success":true}}', flush=True)
    if {ending!r} == "signal":
        import signal
        os.kill(os.getpid(), signal.SIGKILL)
    os._exit(91)
cli._worker = print_then_die
""")
                self.assertNotEqual(0, result.returncode)
                self.assertEqual("", result.stdout)
        self.assertTrue(self.fixture.success("doctor")["ok"])


if __name__ == "__main__":
    unittest.main()
