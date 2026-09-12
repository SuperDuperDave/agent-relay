"""Held enrollment and account-pinned SQLite objects for one worker command.

The account and verified runtime are trusted. Names can change concurrently:
descriptor checks detect this and Landlock prevents SQLite sidecar writes from
following those names into a different file set. Neither mechanism replaces
the other. Ordinary commands never create or adopt database files.
"""

import os
from pathlib import Path
import sqlite3
import stat

from relay_core.protocol import StateError
from relay_core.store import RelayPaths
from .enrollment import (
    _Custody, _fingerprint, _resolve_workspace, _read_json, _publish_new,
    _fsync_directory, EnrollmentError,
)
from .confinement import restrict_file_writes, birth_time

FILES = ("relay.sqlite3", "relay.sqlite3-wal", "relay.sqlite3-shm")


def _checkpoint(stage):
    """Internal deterministic crash/race seam, not a public configuration."""


def _private_file(info):
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
        raise StateError("ledger object must be a private, singly linked regular file")


def _ledger_identities(anchor, enrollment_id):
    if (set(anchor) != {"v", "enrollment_id", "files"}
            or type(anchor["v"]) is not int or anchor["v"] != 1
            or anchor["enrollment_id"] != enrollment_id
            or not isinstance(anchor["files"], dict) or set(anchor["files"]) != set(FILES)):
        raise StateError("unsupported account ledger anchor")
    identities = anchor["files"]
    for identity in identities.values():
        if (not isinstance(identity, dict) or set(identity) != {
                "device", "inode", "birth_seconds", "birth_nanoseconds"}
                or any(type(value) is not int or value < 0 for value in identity.values())):
            raise StateError("invalid ledger object identity")
    return identities


def _verify_ledger_files(state, caps, identities):
    if set(caps) != set(FILES) or set(identities) != set(FILES):
        raise StateError("complete ledger object custody is required")
    if set(os.listdir(state.fd)) != {"enrollment.json", *FILES}:
        raise StateError("ledger file set changed or contains unknown objects")
    for name, fd in caps.items():
        held = os.fstat(fd)
        named = os.stat(name, dir_fd=state.fd, follow_symlinks=False)
        _private_file(held)
        _private_file(named)
        expected = identities[name]
        if (_fingerprint(held) != (expected["device"], expected["inode"])
                or any(expected[key] != value for key, value in birth_time(fd).items())
                or _fingerprint(named) != _fingerprint(held)):
            raise StateError("ledger object identity changed; outcome may be uncertain")


class RebindIdentity:
    """Read-only moved-object witness; never grants admission or opens SQLite.

    Uses the same anchor and file-identity predicates as normal admission. Its
    caller must separately validate both enrollment markers and publish the
    account binding before a later ordinary Admission may open the ledger.
    """

    def __init__(self, custody, workspace, enrollment_id, registry_directory):
        self.custody = custody
        self.caps = {}
        try:
            self.state = custody.get(workspace.state, private=True)
            self.identities = _ledger_identities(
                _read_json(registry_directory, f"ledger-{enrollment_id}.json"), enrollment_id)
            for name in FILES:
                self.caps[name] = os.open(name, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC,
                                          dir_fd=self.state.fd)
            self.verify()
        except BaseException:
            self.close()
            raise

    def verify(self):
        self.custody.verify()
        _verify_ledger_files(self.state, self.caps, self.identities)
        self.custody.verify()

    def close(self):
        for fd in self.caps.values():
            os.close(fd)
        self.caps.clear()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class Admission:
    def __init__(self, registry, repo, *, initialize=False):
        self.custody = _Custody()
        self.caps = {}
        self.fresh = False
        self.worker_pid = None
        self.read_only = False
        self.connected = False
        self.parent_pid = os.getpid()
        try:
            self.workspace = _resolve_workspace(repo, self.custody)
            self.enrollment = registry._lookup(repo, self.workspace, self.custody)
            self.state = self.custody.get(self.workspace.state, private=True)
            self.registry = self.custody.get(registry.root, private=True)
            self.anchor_name = f"ledger-{self.enrollment.enrollment_id}.json"
            if self.registry.exists(self.anchor_name):
                anchor = _read_json(self.registry, self.anchor_name)
                self.identities = _ledger_identities(anchor, self.enrollment.enrollment_id)
            else:
                if not initialize:
                    raise StateError("ledger is not initialized; explicit init is required")
                if set(os.listdir(self.state.fd)) != {"enrollment.json"}:
                    raise StateError("unanchored ledger files exist; explicit recovery is required")
                self.fresh = True
                self.identities = {}
                for name in FILES:
                    self.custody.verify()
                    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                                 | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=self.state.fd)
                    try:
                        info = os.fstat(fd)
                        _private_file(info)
                        self.identities[name] = {"device": info.st_dev, "inode": info.st_ino,
                                                 **birth_time(fd)}
                        os.fsync(fd)
                    finally:
                        os.close(fd)
                    _checkpoint(f"reserved-{name}")
                _fsync_directory(self.state)
            for name in FILES:
                fd = os.open(name, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=self.state.fd)
                self.caps[name] = fd
            self.verify()
            _checkpoint("admission-held")
        except BaseException:
            self.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        for fd in self.caps.values():
            os.close(fd)
        self.caps.clear()
        self.custody.__exit__()

    def verify(self):
        try:
            self.custody.verify()
            _verify_ledger_files(self.state, self.caps, self.identities)
        except (OSError, EnrollmentError) as exc:
            raise StateError("enrollment custody changed; outcome may be uncertain") from exc

    def confine_worker(self, *, read_only=False):
        self.verify()
        # Read-only queries may need writable WAL coordination, but cannot write
        # the database inode or execute mutating SQL (mode=ro and query_only).
        writable = [fd for name, fd in self.caps.items() if not read_only or name != FILES[0]]
        restrict_file_writes(writable, parent_pid=self.parent_pid)
        self.worker_pid = os.getpid()
        self.read_only = read_only
        self.verify()

    def resolve_paths(self, repo, state_home, *, create_state=True):
        if state_home is not None or "RELAY_HOME" in os.environ:
            raise StateError("installed Multithread does not accept state-directory overrides")
        workspace = _resolve_workspace(repo or os.getcwd(), self.custody)
        if (workspace.common != self.workspace.common
                or workspace.common_device != self.workspace.common_device
                or workspace.common_inode != self.workspace.common_inode):
            raise StateError("requested workspace differs from admitted enrollment")
        self.verify()
        return RelayPaths(workspace.root, workspace.common, workspace.state,
                          workspace.state / FILES[0], workspace.state / "hook-errors.log")

    def connect(self, paths, *, read_only, timeout):
        if self.worker_pid != os.getpid() or self.connected:
            raise StateError("ledger connection requires a fresh confined worker")
        if (paths.database != self.workspace.state / FILES[0]
                or paths.git_common_dir != self.workspace.common
                or bool(read_only) != self.read_only):
            raise StateError("connection does not match admitted workspace or access mode")
        self.verify()
        self.connected = True
        _checkpoint("before-sqlite-open")
        connection = sqlite3.connect(
            f"{paths.database.as_uri()}?mode={'ro' if read_only else 'rw'}",
            uri=True, timeout=timeout, isolation_level=None,
        )
        try:
            self.verify()
            connection.execute("PRAGMA temp_store = MEMORY")
            if self.fresh:
                if any(os.fstat(fd).st_size for fd in self.caps.values()):
                    raise StateError("fresh initialization objects are not empty")
                # Only unpublished empty state: SQLite can establish its first
                # WAL header without creating an unpinned rollback journal.
                connection.execute("PRAGMA journal_mode = OFF")
            elif str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower() != "wal":
                raise StateError("installed ledger is not in the required WAL mode")
            elif int(connection.execute("PRAGMA user_version").fetchone()[0]) != 2:
                # Existing installed state is never silently initialized or
                # migrated by a normal command; upgrades need explicit policy.
                raise StateError("installed ledger schema needs explicit recovery or upgrade")
            self.verify()
            return _GuardedConnection(connection, self)
        except BaseException:
            connection.close()
            raise

    def publish_initialized(self):
        if os.getpid() != self.parent_pid:
            raise StateError("only the controller can publish ledger enrollment")
        self.verify()
        if not self.fresh:
            return
        for name in FILES:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=self.state.fd)
            try:
                if _fingerprint(os.fstat(fd)) != _fingerprint(os.fstat(self.caps[name])):
                    raise StateError("ledger changed before publication durability check")
                os.fsync(fd)
            finally:
                os.close(fd)
        _fsync_directory(self.state)
        self.verify()
        _checkpoint("before-ledger-publication")
        _publish_new(self.registry, self.anchor_name, {
            "v": 1, "enrollment_id": self.enrollment.enrollment_id, "files": self.identities,
        }, stage="ledger-anchor")
        self.verify()
        # Retain and validate the actual published anchor, not just our input.
        record = _read_json(self.registry, self.anchor_name)
        if record != {"v": 1, "enrollment_id": self.enrollment.enrollment_id, "files": self.identities}:
            raise StateError("ledger anchor publication changed; outcome is uncertain")
        self.custody.verify()


class _GuardedConnection:
    def __init__(self, connection, admission):
        self.connection = connection
        self.admission = admission

    def execute(self, *args, **kwargs):
        self.admission.verify()
        _checkpoint("before-sqlite-execute")
        result = self.connection.execute(*args, **kwargs)
        _checkpoint("after-sqlite-execute")
        self.admission.verify()
        return result

    @property
    def row_factory(self):
        return self.connection.row_factory

    @row_factory.setter
    def row_factory(self, value):
        self.connection.row_factory = value

    @property
    def in_transaction(self):
        return self.connection.in_transaction

    def close(self):
        # Always close the original connection, even after namespace loss.
        # Object-level confinement remains enforced through SQLite close.
        self.connection.close()
