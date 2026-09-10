#!/usr/bin/env python3
"""Adversarial contract tests for the Relay coordination ledger.

Every test owns a disposable Git repository and Relay state directory.  The
suite must never discover or mutate the real repository's ``.relay``
state.  Tests deliberately exercise the CLI boundary where malformed hook
payloads, concurrent processes, and process death matter; smaller invariants
use the store directly so failures stay legible.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest import mock
from typing import Any, Mapping, Sequence


SOURCE_DIR = Path(__file__).resolve().parents[2] / "src"
if str(SOURCE_DIR) not in sys.path:
    sys.path.insert(0, str(SOURCE_DIR))

from relay_core.cli import (  # noqa: E402
    MAX_BRIEF_CONTEXT_BYTES,
    MAX_JSON_STDIN,
)
from relay_core.protocol import (  # noqa: E402
    ConflictError,
    StateError,
    ValidationError,
    normalize_event,
)
from relay_core.store import RelayStore, resolve_paths  # noqa: E402


CLI = SOURCE_DIR / "relay.py"
_DEFAULT_HOME = object()
#: Distinct from _DEFAULT_HOME so `repo=None` can mean "omit --repo entirely",
#: which is the only way to exercise the ambient `os.getcwd()` fallback.
_DEFAULT_REPO = object()

#: Environment names that could silently redirect a child Relay away from the
#: workspace under test.  Cleared for every subprocess; PATH is preserved so Git
#: still resolves (an emptied environment would fail for the wrong reason).
_SANITIZED_ENV_NAMES = (
    "RELAY_HOME",
    "RELAY_WORKSPACE",
    "CLAUDE_PROJECT_DIR",
    "PYTHONPATH",
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
)

#: The candidate package is a CLOSED set: exactly these files, nothing else.
#: An exclusion list can only refuse what it anticipated, so a recursive copy
#: still admits whatever happens to be on disk — an editor swap file, a coverage
#: artefact, a stray FIFO.  Naming the members instead lets a test assert the
#: destination *equals* the set rather than merely lacks the caches we thought of.
_CANDIDATE_PACKAGE_FILES = ("__init__.py", "cli.py", "protocol.py", "store.py")


def sanitized_env(**overrides: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _SANITIZED_ENV_NAMES}
    env.setdefault("PATH", os.environ.get("PATH", "/usr/bin:/bin"))
    env.update(overrides)
    return env


def _lstat_mode(path: Path) -> int:
    """`lstat` mode for `path`, or 0 when it is missing or unreadable.

    `lstat`, never `stat`: a symlink must be observed AS a symlink.  Following
    one would let a link masquerade as the regular file it points at, which is
    the single thing a sealed candidate set must not allow.
    """

    try:
        return path.lstat().st_mode
    except OSError:
        return 0


def install_candidate_runtime(destination_root: Path, *, source: Path = SOURCE_DIR) -> Path:
    """Copy the candidate entrypoint + the closed package set under `destination_root`.

    Returns the path of the copied `relay.py`.  Copying — rather than
    patching — is what gives the child's `__file__` a Git common directory
    inside the disposable repository.  A mutation run copies MUTATED bytes; if a
    battery ever copies restored bytes it proves nothing, so the copy always
    reads from whatever `source` currently holds.

    Every source is lstat-verified AND fully read before anything is written, so
    a hostile shape — missing/symlinked/non-directory root, missing/symlinked/
    non-regular member or entrypoint — refuses loudly and leaves no partial
    destination behind.  The entrypoint payload is preloaded with the members for
    exactly that reason: reading it after the first `mkdir` would put one read
    after one write and quietly break the guarantee for the file most likely to
    be swapped.  This is deliberately the same shape as the invariant under test:
    identity first, mutation only afterwards.
    """

    entry_source = source / "relay.py"
    if not stat.S_ISREG(_lstat_mode(entry_source)):
        raise AssertionError(
            f"refusing a missing, symlinked, or non-regular entrypoint: {entry_source}"
        )

    package_source = source / "relay_core"
    if not stat.S_ISDIR(_lstat_mode(package_source)):
        raise AssertionError(
            f"refusing a missing, symlinked, or non-directory package root: {package_source}"
        )

    entry_payload = entry_source.read_bytes()
    members = []
    for name in _CANDIDATE_PACKAGE_FILES:
        member = package_source / name
        if not stat.S_ISREG(_lstat_mode(member)):
            raise AssertionError(
                f"refusing a missing, symlinked, or non-regular package file: {member}"
            )
        members.append((name, member.read_bytes()))

    tools = destination_root / "src"
    package = tools / "relay_core"
    package.mkdir(parents=True, exist_ok=True)
    entry = tools / "relay.py"
    entry.write_bytes(entry_payload)
    entry.chmod(0o700)
    for name, payload in members:
        (package / name).write_bytes(payload)
    return entry


_RATCHET_GUARD_NAMES = (
    "ratchet_decision_must_be_admissible",
    "ratchet_empirical_verification_must_be_admissible",
    "ratchet_drop_closure_must_be_exact",
    "ratchet_meta_json_must_be_canonical",
)


def _drop_ratchet_guards(connection: sqlite3.Connection) -> None:
    for name in _RATCHET_GUARD_NAMES:
        connection.execute(f"DROP TRIGGER {name}")


def _ratchet_guard_count(connection: sqlite3.Connection) -> int:
    placeholders = ",".join("?" for _ in _RATCHET_GUARD_NAMES)
    return int(
        connection.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            f"WHERE type = 'trigger' AND name IN ({placeholders})",
            _RATCHET_GUARD_NAMES,
        ).fetchone()[0]
    )


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    return result.stdout.strip()


class RelayTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="relay-test-")
        self.root = Path(self._temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(
            ["git", "init", "-q", "-b", "main", str(self.repo)],
            check=True,
            capture_output=True,
            timeout=15,
        )
        _git(self.repo, "config", "user.name", "Relay Test")
        _git(self.repo, "config", "user.email", "relay-test@example.invalid")
        (self.repo / "README.md").write_text("relay fixture\n", encoding="utf-8")
        _git(self.repo, "add", "README.md")
        _git(self.repo, "commit", "-q", "-m", "seed")
        self.home = self.root / "state"

        # The disposable repository is this test's workspace, so both halves of
        # the binding must point at it: a copied runtime for every subprocess,
        # and the private resolver for everything in-process.  These are the ONLY
        # two seams; nothing patches production behaviour.
        self.repo_common = Path(
            _git(self.repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
        ).resolve()
        self.cli = install_candidate_runtime(self.repo)
        self._binding_patch = mock.patch(
            "relay_core.store._expected_workspace_binding",
            return_value=self.repo_common,
        )
        self._binding_patch.start()
        self.addCleanup(self._binding_patch.stop)

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def open_store(
        self,
        *,
        repo: Path | None = None,
        home: Path | object = _DEFAULT_HOME,
        busy_timeout_ms: int = 5_000,
    ) -> RelayStore:
        selected_home = self.home if home is _DEFAULT_HOME else home
        return RelayStore.open(
            repo=repo or self.repo,
            state_home=selected_home,
            busy_timeout_ms=busy_timeout_ms,
        )

    def cli_command(
        self,
        *args: str,
        repo: Path | object = _DEFAULT_REPO,
        home: Path | object = _DEFAULT_HOME,
        cli: Path | None = None,
    ) -> list[str]:
        command = [sys.executable, str(cli or self.cli)]
        selected_repo = self.repo if repo is _DEFAULT_REPO else repo
        if selected_repo is not None:
            command.extend(("--repo", str(selected_repo)))
        selected_home = self.home if home is _DEFAULT_HOME else home
        if selected_home is not None:
            command.extend(("--home", str(selected_home)))
        command.extend(("--json", *args))
        return command

    def run_cli(
        self,
        *args: str,
        repo: Path | object = _DEFAULT_REPO,
        home: Path | object = _DEFAULT_HOME,
        input_bytes: bytes | None = None,
        timeout: float = 15,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
        cli: Path | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            self.cli_command(*args, repo=repo, home=home, cli=cli),
            input=input_bytes,
            capture_output=True,
            timeout=timeout,
            env=dict(env) if env is not None else sanitized_env(),
            cwd=str(cwd) if cwd is not None else None,
        )

    @staticmethod
    def valid_event(**overrides: Any) -> dict[str, Any]:
        event: dict[str, Any] = {
            "v": 1,
            "id": "evt:contract-0001",
            "kind": "work.intent",
            "agent": "codex",
            "session": "session-test",
            "work_id": "relay-contract",
            "summary": "Exercise the Relay contract",
        }
        event.update(overrides)
        return event

    def assert_cli_error(
        self, result: subprocess.CompletedProcess[bytes], expected_code: int
    ) -> str:
        self.assertEqual(
            result.returncode,
            expected_code,
            msg=f"stdout={result.stdout!r}\nstderr={result.stderr!r}",
        )
        return result.stderr.decode("utf-8", errors="replace")


class EventLedgerTests(RelayTestCase):
    def test_exact_retry_is_idempotent_and_conflicting_id_is_rejected(self) -> None:
        original = self.valid_event()
        with self.open_store() as store:
            first = store.emit(original)
            retry = store.emit(dict(original))

            self.assertFalse(first["duplicate"])
            self.assertTrue(retry["duplicate"])
            self.assertEqual(first["event"]["seq"], retry["event"]["seq"])

            changed = dict(original, summary="Different content under the same ID")
            with self.assertRaisesRegex(ConflictError, "different content"):
                store.emit(changed)

            events = store.events()
            self.assertEqual(1, len(events))
            self.assertEqual("evt:contract-0001", events[0]["id"])

    def test_sql_triggers_make_events_and_claim_history_append_only(self) -> None:
        with self.open_store() as store:
            store.emit(self.valid_event())
            claim = store.claim(
                "deploy:ota",
                agent="codex",
                session="session-test",
                purpose="release candidate",
                claim_id="clm:append-only",
            )
            database = store.paths.database

        connection = sqlite3.connect(database)
        try:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                connection.execute(
                    "UPDATE events SET summary = 'rewritten' WHERE event_id = ?",
                    ("evt:contract-0001",),
                )
            connection.rollback()
            with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                connection.execute(
                    "DELETE FROM events WHERE event_id = ?",
                    ("evt:contract-0001",),
                )
            connection.rollback()
            with self.assertRaisesRegex(sqlite3.IntegrityError, "cannot be deleted"):
                connection.execute(
                    "DELETE FROM claims WHERE claim_id = ?",
                    (claim["claim"]["claim_id"],),
                )
            connection.rollback()
            with self.assertRaisesRegex(sqlite3.IntegrityError, "only transition"):
                connection.execute(
                    "UPDATE claims SET purpose = 'revision' WHERE claim_id = ?",
                    (claim["claim"]["claim_id"],),
                )
            connection.rollback()
            self.assertEqual(
                "Exercise the Relay contract",
                connection.execute(
                    "SELECT summary FROM events WHERE event_id = ?",
                    ("evt:contract-0001",),
                ).fetchone()[0],
            )
        finally:
            connection.close()

    def test_sql_shaped_text_remains_data_and_cannot_mutate_schema(self) -> None:
        payload = self.valid_event(
            id="evt:sql-text-0001",
            summary="'); DROP TABLE events; -- $(touch /tmp/not-a-command)",
        )
        with self.open_store() as store:
            receipt = store.emit(payload)
            self.assertEqual(payload["summary"], receipt["event"]["summary"])
            self.assertEqual("ok", store.integrity_check())
            self.assertEqual(1, len(store.events()))

    def test_handoff_can_reference_an_immutable_git_commit(self) -> None:
        (self.repo / "README.md").write_text("relay fixture\ncommit capsule\n", encoding="utf-8")
        message = self.root / "commit-message.txt"
        message.write_text(
            "Prove Relay commit capsules\n\n"
            "Why:\n- preserve handoff context in Git\n\n"
            "Proof:\n- isolated unit fixture\n\n"
            "Relay-Work-ID: relay-test\n"
            "Relay-Scope: tooling:coordination\n"
            "Relay-Review: requested\n",
            encoding="utf-8",
        )
        _git(self.repo, "add", "README.md")
        _git(self.repo, "commit", "-q", "-F", str(message))
        oid = _git(self.repo, "rev-parse", "HEAD")
        result = self.run_cli(
            "signal",
            "work.handoff",
            "--agent",
            "codex",
            "--session",
            "session-test",
            "--summary",
            "Relay implementation ready for adversarial review",
            "--commit",
            "HEAD",
        )
        self.assertEqual(0, result.returncode, result.stderr.decode())
        receipt = json.loads(result.stdout)
        self.assertEqual(f"git:{oid}", receipt["event"]["artifact"])
        self.assertEqual(oid, receipt["event"]["meta"]["commit_oid"])
        self.assertEqual(
            "Prove Relay commit capsules",
            receipt["event"]["meta"]["commit_subject"],
        )
        self.assertRegex(
            receipt["event"]["meta"]["relay_trailers_sha256"], r"^[0-9a-f]{64}$"
        )

    def test_handoff_retry_after_lost_receipt_is_one_event_and_drift_fails_closed(
        self,
    ) -> None:
        (self.repo / "README.md").write_text(
            "relay fixture\nwrapped lane\n", encoding="utf-8"
        )
        _git(self.repo, "add", "README.md")
        _git(self.repo, "commit", "-q", "-m", "Wrap one bounded lane")
        oid = _git(self.repo, "rev-parse", "HEAD")
        command = [
            "signal",
            "work.handoff",
            "--agent",
            " codex ",
            "--session",
            " wrap-session ",
            "--work-id",
            "session-wrap-ratchet",
            "--target",
            "claude",
            "--scope",
            "tooling:coordination",
            "--summary",
            "Wrapped lane is ready for review",
            "--commit",
            oid,
        ]

        first_without_receipt = self.run_cli(*command)
        self.assertEqual(
            0,
            first_without_receipt.returncode,
            first_without_receipt.stderr.decode(),
        )

        retry = list(command)
        retry[retry.index(" codex ")] = "codex"
        retry[retry.index(" wrap-session ")] = "wrap-session"
        replay = self.run_cli(*retry)
        self.assertEqual(0, replay.returncode, replay.stderr.decode())
        first_receipt = json.loads(first_without_receipt.stdout)
        replay_receipt = json.loads(replay.stdout)
        self.assertFalse(first_receipt["duplicate"])
        self.assertTrue(replay_receipt["duplicate"])
        self.assertEqual(
            first_receipt["event"]["id"], replay_receipt["event"]["id"]
        )
        self.assertTrue(first_receipt["event"]["id"].startswith("handoff:"))
        with self.open_store() as store:
            handoffs = [
                event for event in store.events() if event["kind"] == "work.handoff"
            ]
            self.assertEqual(1, len(handoffs))

        changed_body = list(retry)
        changed_body[changed_body.index("Wrapped lane is ready for review")] = (
            "A contradictory summary for the same semantic handoff"
        )
        conflict = self.run_cli(*changed_body)
        self.assert_cli_error(conflict, 73)

        rerouted = list(retry)
        rerouted[rerouted.index("claude")] = "fable"
        distinct = self.run_cli(*rerouted)
        self.assertEqual(0, distinct.returncode, distinct.stderr.decode())
        distinct_receipt = json.loads(distinct.stdout)
        self.assertNotEqual(
            first_receipt["event"]["id"], distinct_receipt["event"]["id"]
        )

    def test_work_intent_is_stably_idempotent_within_one_session(self) -> None:
        command = (
            "signal",
            "work.intent",
            "--agent",
            "codex",
            "--session",
            "session-test",
            "--work-id",
            "relay-v11",
            "--scope",
            "tooling:coordination",
            "--summary",
            "Close the automatic coordination loop",
        )
        first = self.run_cli(*command)
        retry = self.run_cli(*command)

        self.assertEqual(0, first.returncode, first.stderr.decode())
        self.assertEqual(0, retry.returncode, retry.stderr.decode())
        first_receipt = json.loads(first.stdout)
        retry_receipt = json.loads(retry.stdout)
        self.assertFalse(first_receipt["duplicate"])
        self.assertTrue(retry_receipt["duplicate"])
        self.assertEqual(
            first_receipt["event"]["id"], retry_receipt["event"]["id"]
        )

        normalized_retry = self.run_cli(
            "signal",
            "work.intent",
            "--agent",
            " codex ",
            "--session",
            " session-test ",
            "--work-id",
            " relay-v11 ",
            "--scope",
            "tooling:coordination",
            "--summary",
            "Close the automatic coordination loop",
        )
        self.assertEqual(
            0, normalized_retry.returncode, normalized_retry.stderr.decode()
        )
        normalized_receipt = json.loads(normalized_retry.stdout)
        self.assertTrue(normalized_receipt["duplicate"])
        self.assertEqual(
            first_receipt["event"]["id"], normalized_receipt["event"]["id"]
        )

        changed = self.run_cli(
            *command[:-1], "Contradict the same stable intent identity"
        )
        self.assert_cli_error(changed, 73)

        missing_work_id = self.run_cli(
            "signal",
            "work.intent",
            "--agent",
            "codex",
            "--session",
            "session-test",
            "--summary",
            "This intent has no stable identity",
        )
        self.assert_cli_error(missing_work_id, 64)


class BriefingAndDeliveryTests(RelayTestCase):
    def test_targeted_delivery_is_at_least_once_until_exact_agent_acknowledges(
        self,
    ) -> None:
        with self.open_store() as store:
            intent = store.emit(
                self.valid_event(
                    id="evt:intent-brief-0001",
                    work_id="relay-v11",
                    summary="Implement the bounded Relay inbox",
                )
            )["event"]
            blocked = store.emit(
                self.valid_event(
                    id="evt:blocked-codex-0001",
                    kind="work.blocked",
                    target="codex",
                    work_id="relay-v11",
                    summary="Codex review is required",
                    meta={"reason": "review ownership"},
                )
            )["event"]
            handoff = store.emit(
                self.valid_event(
                    id="evt:handoff-claude-0001",
                    kind="work.handoff",
                    target="claude",
                    summary="Claude owns the follow-up",
                    artifact=f"git:{'a' * 40}",
                )
            )["event"]
            review = store.emit(
                self.valid_event(
                    id="evt:review-codex-0001",
                    kind="review.requested",
                    target="codex",
                    summary="Review the immutable capsule",
                    artifact=f"git:{'b' * 40}",
                )
            )["event"]
            broadcast = store.emit(
                self.valid_event(
                    id="evt:blocked-untargeted-0001",
                    kind="work.blocked",
                    summary="Broadcast coordination signal",
                )
            )["event"]

            first = store.brief("codex")
            replay = store.brief("codex")
            self.assertEqual(
                [blocked["seq"], review["seq"], broadcast["seq"]],
                [item["seq"] for item in first["pending_signals"]],
            )
            self.assertEqual(first["pending_signals"], replay["pending_signals"])
            self.assertEqual(
                [intent["seq"]], [item["seq"] for item in first["recent_intents"]]
            )
            self.assertEqual(
                [handoff["seq"], broadcast["seq"]],
                [item["seq"] for item in store.brief("claude")["pending_signals"]],
            )

        acknowledged = self.run_cli(
            "acknowledge",
            str(blocked["seq"]),
            "--agent",
            "codex",
            "--session",
            "codex-session-one",
        )
        self.assertEqual(0, acknowledged.returncode, acknowledged.stderr.decode())
        first_ack = json.loads(acknowledged.stdout)
        self.assertFalse(first_ack["duplicate"])
        self.assertEqual("delivery.acknowledged", first_ack["event"]["kind"])

        retried = self.run_cli(
            "acknowledge",
            str(blocked["seq"]),
            "--agent",
            "codex",
            "--session",
            "codex-session-two",
        )
        retry_ack = json.loads(retried.stdout)
        self.assertTrue(retry_ack["duplicate"])
        self.assertEqual(first_ack["event"]["seq"], retry_ack["event"]["seq"])

        with self.open_store() as store:
            self.assertEqual(
                [review["seq"], broadcast["seq"]],
                [item["seq"] for item in store.brief("codex")["pending_signals"]],
            )
            broadcast_ack = store.acknowledge(
                broadcast["seq"], agent="codex", session="broadcast-codex"
            )
            self.assertFalse(broadcast_ack["duplicate"])
            self.assertEqual(
                [review["seq"]],
                [item["seq"] for item in store.brief("codex")["pending_signals"]],
            )
            self.assertEqual(
                [handoff["seq"], broadcast["seq"]],
                [item["seq"] for item in store.brief("claude")["pending_signals"]],
            )
            acknowledgement_events = [
                item for item in store.events(limit=100)
                if item["kind"] == "delivery.acknowledged"
            ]
            self.assertEqual(2, len(acknowledgement_events))
            with self.assertRaisesRegex(ConflictError, "targeted to codex"):
                store.acknowledge(
                    review["seq"], agent="claude", session="wrong-target"
                )
            with self.assertRaisesRegex(ConflictError, "not an acknowledgeable"):
                store.acknowledge(
                    intent["seq"], agent="codex", session="wrong-kind"
                )

    def test_brief_formats_are_exact_bounded_private_and_event_explicit(self) -> None:
        with self.open_store() as store:
            for index in range(12):
                store.emit(
                    self.valid_event(
                        id=f"evt:provider-signal-{index:04d}",
                        kind="work.blocked",
                        target="codex",
                        summary=(
                            f"Bounded coordination fact {index}: " + "x" * 440
                        ),
                    )
                )

        stdin_canary = b"RAW_PROMPT_TRANSCRIPT_TOOL_CANARY_92ab1"
        codex = self.run_cli(
            "brief",
            "--agent",
            "codex",
            "--format",
            "codex-hook",
            "--event",
            "SessionStart",
            "--limit",
            "10",
            input_bytes=stdin_canary,
        )
        self.assertEqual(0, codex.returncode, codex.stderr.decode())
        codex_envelope = json.loads(codex.stdout)
        self.assertEqual({"hookSpecificOutput"}, set(codex_envelope))
        self.assertEqual(
            {"hookEventName", "additionalContext"},
            set(codex_envelope["hookSpecificOutput"]),
        )
        self.assertEqual(
            "SessionStart",
            codex_envelope["hookSpecificOutput"]["hookEventName"],
        )
        context = codex_envelope["hookSpecificOutput"]["additionalContext"]
        self.assertLessEqual(len(context.encode("utf-8")), MAX_BRIEF_CONTEXT_BYTES)
        self.assertIn("more queued", context)
        self.assertNotIn(stdin_canary.decode(), context)
        self.assertNotIn("last_assistant_message", context)

        claude = self.run_cli(
            "brief",
            "--agent",
            "codex",
            "--format",
            "claude-hook",
            "--event",
            "UserPromptSubmit",
            "--limit",
            "10",
            input_bytes=stdin_canary,
        )
        claude_envelope = json.loads(claude.stdout)
        self.assertEqual(
            "UserPromptSubmit",
            claude_envelope["hookSpecificOutput"]["hookEventName"],
        )
        self.assertEqual(
            context, claude_envelope["hookSpecificOutput"]["additionalContext"]
        )

        missing_event = self.run_cli(
            "brief", "--agent", "codex", "--format", "codex-hook"
        )
        self.assertIn(
            "--event is required",
            self.assert_cli_error(missing_event, ValidationError.exit_code),
        )
        event_on_plain = self.run_cli(
            "brief", "--agent", "codex", "--event", "SessionStart"
        )
        self.assertIn(
            "only valid for provider",
            self.assert_cli_error(event_on_plain, ValidationError.exit_code),
        )
        too_large = self.run_cli(
            "brief", "--agent", "codex", "--limit", "11"
        )
        self.assertIn(
            "between 1 and 10",
            self.assert_cli_error(too_large, ValidationError.exit_code),
        )

        plain_command = self.cli_command("brief", "--agent", "codex")
        plain_command.remove("--json")
        plain = subprocess.run(
            plain_command,
            input=stdin_canary,
            capture_output=True,
            timeout=15,
        )
        self.assertEqual(0, plain.returncode, plain.stderr.decode())
        self.assertTrue(plain.stdout.startswith(b"RELAY BRIEF v1\n"))
        self.assertNotIn(stdin_canary, plain.stdout)

        with self.open_store() as store:
            self.assertNotIn(stdin_canary, store.paths.database.read_bytes())

    def test_limit_and_compact_status_bound_every_section_and_show_intent(self) -> None:
        with self.open_store() as store:
            for index in range(7):
                store.claim(
                    f"port:{9000 + index}",
                    agent="codex",
                    session=f"claim-session-{index}",
                    purpose=f"fixture {index}",
                    claim_id=f"clm:status-limit-{index}",
                )
            for index in range(12):
                store.emit(
                    self.valid_event(
                        id=f"evt:status-intent-{index:04d}",
                        summary=f"Intent {index}",
                        work_id=f"work-{index}",
                    )
                )

            brief = store.brief("codex", limit=2)
            self.assertEqual(2, len(brief["active_claims"]))
            self.assertEqual(2, len(brief["recent_intents"]))
            self.assertTrue(brief["truncated"]["active_claims"])
            self.assertTrue(brief["truncated"]["recent_intents"])
            self.assertGreater(
                brief["recent_intents"][0]["seq"],
                brief["recent_intents"][1]["seq"],
            )

            compact = store.status(compact=True)
            full = store.status(compact=False)
            self.assertTrue(compact["compact"])
            self.assertEqual(5, len(compact["active_claims"]))
            self.assertEqual(8, len(compact["recent_signals"]))
            self.assertTrue(compact["truncated"]["active_claims"])
            self.assertTrue(compact["truncated"]["recent_signals"])
            self.assertTrue(
                all(item["kind"] == "work.intent" for item in compact["recent_signals"])
            )
            self.assertNotIn("body_hash", compact["recent_signals"][0])
            self.assertNotIn("release_kind", compact["active_claims"][0])
            self.assertEqual(7, len(full["active_claims"]))
            self.assertEqual(12, len(full["recent_signals"]))
            self.assertIn("body_hash", full["recent_signals"][0])


class ClaimAuthorityTests(RelayTestCase):
    def test_cold_n_process_race_has_exactly_one_winner(self) -> None:
        contenders = 12

        def contend(index: int) -> subprocess.CompletedProcess[bytes]:
            return self.run_cli(
                "claim",
                "deploy:ota",
                "--agent",
                f"agent-{index}",
                "--session",
                f"session-{index}",
                "--purpose",
                "cold race",
                "--claim-id",
                f"clm:cold-race-{index}",
                timeout=25,
            )

        # No process initializes the database before this fan-out.
        with ThreadPoolExecutor(max_workers=contenders) as pool:
            results = list(pool.map(contend, range(contenders)))

        winners = [item for item in results if item.returncode == 0]
        losers = [item for item in results if item.returncode != 0]
        self.assertEqual(
            1,
            len(winners),
            msg="\n".join(
                f"rc={item.returncode} stderr={item.stderr.decode(errors='replace')}"
                for item in results
            ),
        )
        self.assertEqual(contenders - 1, len(losers))
        self.assertTrue(
            all(item.returncode == ConflictError.exit_code for item in losers),
            msg="unexpected race failures: "
            + repr([(item.returncode, item.stderr) for item in losers]),
        )

        with self.open_store() as store:
            active = store.active_claims()
            events = store.events()
            self.assertEqual(1, len(active))
            self.assertEqual("deploy:ota", active[0]["resource"])
            self.assertEqual(["claim.acquired"], [event["kind"] for event in events])
            self.assertEqual("ok", store.integrity_check())

    def test_claim_survives_holder_crash_and_never_expires_implicitly(self) -> None:
        script = textwrap.dedent(
            """
            import json
            import sys
            import time
            from relay_core.store import RelayStore

            repo, home = sys.argv[1:3]
            store = RelayStore.open(repo=repo, state_home=home)
            receipt = store.claim(
                "device:s22",
                agent="claude",
                session="crashing-session",
                purpose="on-glass verification",
                claim_id="clm:crash-persistence",
            )
            print(json.dumps(receipt), flush=True)
            time.sleep(300)
            """
        )
        # Import the COPIED package inside the disposable repository, never the
        # shipping tree: a parent-process patch of the binding resolver does not
        # cross Popen, so only a copy that physically lives in this repository
        # binds the child to it.
        environment = sanitized_env(PYTHONPATH=str(self.cli.parent))
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                script,
                str(self.repo),
                str(self.home),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            text=True,
        )
        try:
            assert process.stdout is not None
            line = process.stdout.readline()
            self.assertTrue(line, "holder process never committed its claim")
            self.assertEqual("clm:crash-persistence", json.loads(line)["claim"]["claim_id"])
            process.kill()
            process.wait(timeout=10)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()

        # Time and process death do not grant authority to a competitor.
        time.sleep(0.1)
        competitor = self.run_cli(
            "claim",
            "device:s22",
            "--agent",
            "codex",
            "--session",
            "new-session",
            "--purpose",
            "competing verification",
            "--claim-id",
            "clm:competitor",
        )
        self.assert_cli_error(competitor, ConflictError.exit_code)

        with self.open_store() as store:
            active = store.active_claims()
            self.assertEqual(1, len(active))
            self.assertEqual("clm:crash-persistence", active[0]["claim_id"])
            self.assertEqual("ok", store.integrity_check())

    def test_release_requires_exact_holder_break_is_audited_and_stale_release_is_safe(
        self,
    ) -> None:
        with self.open_store() as store:
            first = store.claim(
                "supabase:live-mutation",
                agent="claude",
                session="session-one",
                purpose="migration",
                claim_id="clm:first-holder",
            )["claim"]

            with self.assertRaisesRegex(ConflictError, "exact claim holder"):
                store.release(
                    first["claim_id"], agent="claude", session="session-other"
                )

            broken = store.break_claim(
                first["claim_id"],
                actor_agent="codex",
                actor_session="recovery-session",
                reason="holder process was confirmed dead",
            )
            self.assertEqual("broken", broken["claim"]["release_kind"])
            self.assertEqual(
                "holder process was confirmed dead",
                broken["claim"]["release_reason"],
            )

            successor = store.claim(
                "supabase:live-mutation",
                agent="codex",
                session="session-two",
                purpose="reconciled migration",
                claim_id="clm:successor",
            )["claim"]

            with self.assertRaisesRegex(ConflictError, "already inactive"):
                store.release(
                    first["claim_id"], agent="claude", session="session-one"
                )

            active = store.active_claims()
            self.assertEqual([successor["claim_id"]], [item["claim_id"] for item in active])
            events = store.events()
            self.assertEqual(
                ["claim.acquired", "claim.broken", "claim.acquired"],
                [event["kind"] for event in events],
            )
            broken_event = events[1]
            self.assertEqual("codex", broken_event["meta"]["actor_agent"])
            self.assertEqual(
                "holder process was confirmed dead", broken_event["meta"]["reason"]
            )

    def test_linked_worktrees_resolve_one_shared_default_ledger(self) -> None:
        linked = self.root / "linked"
        _git(self.repo, "worktree", "add", "-q", "-b", "relay-peer", str(linked), "HEAD")

        primary_paths = resolve_paths(repo=self.repo)
        linked_paths = resolve_paths(repo=linked)
        self.assertEqual(primary_paths.git_common_dir, linked_paths.git_common_dir)
        self.assertEqual(primary_paths.database, linked_paths.database)

        with RelayStore.open(repo=self.repo) as primary:
            primary.claim(
                "integrate:main",
                agent="codex",
                session="primary-session",
                purpose="integration",
                claim_id="clm:cross-worktree",
            )
        with RelayStore.open(repo=linked) as peer:
            active = peer.active_claims()
            self.assertEqual(1, len(active))
            self.assertEqual("integration:main", active[0]["resource"])
            self.assertEqual("clm:cross-worktree", active[0]["claim_id"])
            with self.assertRaises(ConflictError):
                peer.claim(
                    "integration:main",
                    agent="claude",
                    session="peer-session",
                    purpose="parallel integration",
                    claim_id="clm:peer-conflict",
                )

    def test_v1_legacy_main_claim_is_canonicalized_before_v2_admission(self) -> None:
        with self.open_store() as current:
            current.claim(
                "integration:main",
                agent="claude",
                session="legacy-v1-session",
                purpose="legacy integration",
                claim_id="clm:legacy-main",
            )
            database = current.paths.database

        connection = sqlite3.connect(database)
        try:
            trigger_sql = connection.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type = 'trigger' AND name = 'claims_only_release_once'"
            ).fetchone()[0]
            connection.execute("DROP TRIGGER claims_only_release_once")
            connection.execute("DROP TRIGGER claims_reject_legacy_main_insert")
            _drop_ratchet_guards(connection)
            connection.execute(
                "UPDATE claims SET resource = 'integrate:main' "
                "WHERE claim_id = 'clm:legacy-main'"
            )
            connection.execute(trigger_sql)
            connection.execute("PRAGMA user_version = 1")
            connection.commit()
        finally:
            connection.close()

        stale_v1_connection = sqlite3.connect(database)
        with self.open_store() as migrated:
            self.assertEqual(
                2, migrated._db.execute("PRAGMA user_version").fetchone()[0]
            )
            self.assertEqual(
                1,
                migrated._db.execute(
                    "SELECT COUNT(*) FROM sqlite_master "
                    "WHERE type = 'trigger' "
                    "AND name = 'ratchet_decision_must_be_admissible'"
                ).fetchone()[0],
            )
            active = migrated.active_claims()
            self.assertEqual(1, len(active))
            self.assertEqual("integration:main", active[0]["resource"])
            with self.assertRaises(ConflictError):
                migrated.claim(
                    "integration:main",
                    agent="codex",
                    session="v2-session",
                    purpose="competing integration",
                    claim_id="clm:v2-main",
                )

            acquired_seq = active[0]["acquired_event_seq"]
            with self.assertRaisesRegex(
                sqlite3.IntegrityError, "legacy integrate:main claims require a v2 client"
            ):
                stale_v1_connection.execute(
                    """
                    INSERT INTO claims (
                      claim_id, resource, holder_agent, holder_session, purpose,
                      acquired_event_seq
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "clm:stale-v1-main",
                        "integrate:main",
                        "stale-v1",
                        "stale-v1-session",
                        "stale client insertion",
                        acquired_seq,
                    ),
                )
        stale_v1_connection.close()

        guard = sqlite3.connect(database)
        try:
            with self.assertRaisesRegex(
                sqlite3.IntegrityError, "only transition active to released"
            ):
                guard.execute(
                    "UPDATE claims SET purpose = 'rewritten' "
                    "WHERE claim_id = 'clm:legacy-main'"
                )
        finally:
            guard.close()

    def test_v1_dual_active_main_aliases_fail_closed_without_mutation(self) -> None:
        with self.open_store() as current:
            current.claim(
                "integration:main",
                agent="codex",
                session="canonical-session",
                purpose="canonical integration",
                claim_id="clm:canonical-main",
            )
            database = current.paths.database

        connection = sqlite3.connect(database)
        try:
            acquired_seq = connection.execute(
                "SELECT acquired_event_seq FROM claims "
                "WHERE claim_id = 'clm:canonical-main'"
            ).fetchone()[0]
            connection.execute("DROP TRIGGER claims_reject_legacy_main_insert")
            _drop_ratchet_guards(connection)
            connection.execute(
                """
                INSERT INTO claims (
                  claim_id, resource, holder_agent, holder_session, purpose,
                  acquired_event_seq
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    "clm:legacy-main",
                    "integrate:main",
                    "claude",
                    "legacy-session",
                    "legacy integration",
                    acquired_seq,
                ),
            )
            connection.execute("PRAGMA user_version = 1")
            connection.commit()
        finally:
            connection.close()

        with self.assertRaisesRegex(StateError, "both legacy and canonical"):
            self.open_store()

        unchanged = sqlite3.connect(database)
        try:
            self.assertEqual(1, unchanged.execute("PRAGMA user_version").fetchone()[0])
            self.assertEqual(
                [("clm:canonical-main", "integration:main"), ("clm:legacy-main", "integrate:main")],
                unchanged.execute(
                    "SELECT claim_id, resource FROM claims ORDER BY claim_id"
                ).fetchall(),
            )
        finally:
            unchanged.close()


class ValidationBoundaryTests(RelayTestCase):
    def test_delivery_acknowledgement_cannot_be_forged_through_public_emit(self) -> None:
        with self.assertRaisesRegex(ValidationError, "acknowledged is emitted only"):
            normalize_event(
                {
                    "v": 1,
                    "id": "ack:forged-delivery-0001",
                    "kind": "delivery.acknowledged",
                    "agent": "codex",
                    "session": "session-test",
                    "target": "codex",
                    "scope": "signal:1",
                    "summary": "Forged acknowledgement",
                    "meta": {
                        "signal_seq": 1,
                        "signal_event_id": "evt:forged-signal-0001",
                        "target_agent": "codex",
                    },
                }
            )

    def test_git_artifact_and_commit_metadata_cannot_disagree(self) -> None:
        event = self.valid_event(
            kind="work.handoff",
            artifact=f"git:{'a' * 40}",
            meta={"commit_oid": "b" * 40},
        )
        with self.assertRaisesRegex(ValidationError, "same object"):
            normalize_event(event)

    def test_explicit_state_home_refuses_symlinked_parent_component(self) -> None:
        real_parent = self.root / "real-state-parent"
        real_parent.mkdir()
        linked_parent = self.root / "linked-state-parent"
        linked_parent.symlink_to(real_parent, target_is_directory=True)

        with self.assertRaisesRegex(StateError, "symlinked state path component"):
            resolve_paths(repo=self.repo, state_home=linked_parent / "nested")

        self.assertFalse((real_parent / "nested").exists())

    def test_newer_schema_is_refused_without_downgrade(self) -> None:
        self.home.mkdir(mode=0o700)
        database = self.home / "relay.sqlite3"
        connection = sqlite3.connect(database)
        connection.execute("PRAGMA user_version = 99")
        connection.close()

        with self.assertRaisesRegex(StateError, "newer than this client"):
            self.open_store()

        check = sqlite3.connect(database)
        try:
            self.assertEqual(99, check.execute("PRAGMA user_version").fetchone()[0])
        finally:
            check.close()

    def test_secret_shaped_values_and_keys_are_rejected(self) -> None:
        secret = "Bearer TOPSECRET_CANARY_0123456789"
        with self.assertRaisesRegex(ValidationError, "credential"):
            normalize_event(self.valid_event(summary=secret))

        lifecycle = {
            "v": 1,
            "id": "evt:secret-key-0001",
            "kind": "session.started",
            "agent": "codex",
            "session": "session-test",
            "summary": "Started",
            "meta": {"token": "TOPSECRET_CANARY_0123456789"},
        }
        with self.assertRaises(ValidationError):
            normalize_event(lifecycle)

    def test_duplicate_json_keys_nan_and_oversize_input_are_rejected(self) -> None:
        duplicate = (
            b'{"v":1,"id":"evt:duplicate-0001","id":"evt:duplicate-0002",'
            b'"kind":"work.intent","agent":"codex","session":"session-test",'
            b'"summary":"duplicate"}'
        )
        duplicate_result = self.run_cli("emit", input_bytes=duplicate)
        self.assertIn(
            "duplicate JSON key",
            self.assert_cli_error(duplicate_result, ValidationError.exit_code),
        )

        nonfinite = (
            b'{"v":1,"id":"evt:nonfinite-0001","kind":"friction.observed",'
            b'"agent":"codex","session":"session-test","summary":"bad number",'
            b'"meta":{"fingerprint":"bad-number","category":"tooling",'
            b'"cost_seconds":NaN}}'
        )
        nonfinite_result = self.run_cli("emit", input_bytes=nonfinite)
        self.assertIn(
            "non-finite JSON value",
            self.assert_cli_error(nonfinite_result, ValidationError.exit_code),
        )

        oversize = b'{"padding":"' + (b"x" * MAX_JSON_STDIN) + b'"}'
        oversize_result = self.run_cli("emit", input_bytes=oversize)
        self.assertIn(
            "JSON input exceeds",
            self.assert_cli_error(oversize_result, ValidationError.exit_code),
        )

        with self.open_store() as store:
            self.assertEqual([], store.events())


class LifecycleHookTests(RelayTestCase):
    def test_codex_interrupt_is_distinct_idempotent_and_sanitized(self) -> None:
        canaries = (
            "INTERRUPT_TRANSCRIPT_CANARY_62d91",
            "INTERRUPT_MODEL_CANARY_339af",
            "INTERRUPT_PERMISSION_CANARY_71b4c",
        )
        payload = {
            "hook_event_name": "Interrupt",
            "session_id": "codex-session-interrupted-001",
            "turn_id": "turn-interrupted-001",
            "cwd": str(self.repo),
            "transcript_path": f"/tmp/{canaries[0]}.jsonl",
            "model": canaries[1],
            "permission_mode": canaries[2],
        }
        encoded = json.dumps(payload).encode()

        first = self.run_cli("hook", "--client", "codex", input_bytes=encoded)
        second = self.run_cli("hook", "--client", "codex", input_bytes=encoded)
        self.assertEqual(0, first.returncode, first.stderr.decode())
        self.assertEqual(0, second.returncode, second.stderr.decode())

        with self.open_store() as store:
            events = store.events()
            self.assertEqual(1, len(events))
            self.assertEqual("turn.interrupted", events[0]["kind"])
            self.assertNotEqual("turn.completed", events[0]["kind"])
            self.assertEqual("Codex turn interrupted", events[0]["summary"])
            self.assertEqual(
                {"branch", "client", "commit", "worktree"},
                set(events[0]["meta"]),
            )
            database = store.paths.database

        raw_database = database.read_bytes()
        for canary in canaries:
            self.assertNotIn(canary.encode(), raw_database)

    def test_codex_interrupt_requires_turn_id_but_remains_fail_open(self) -> None:
        payload = json.dumps(
            {
                "hook_event_name": "Interrupt",
                "session_id": "codex-session-interrupted-002",
            }
        ).encode()
        result = self.run_cli("hook", "--client", "codex", input_bytes=payload)
        self.assertEqual(0, result.returncode, result.stderr.decode())
        self.assertIn(b"ValidationError", result.stderr)
        with self.open_store() as store:
            self.assertEqual([], store.events())

    def test_codex_stop_turn_id_deduplicates_without_persisting_message(self) -> None:
        canary = "LAST_ASSISTANT_MESSAGE_CANARY_611ae"
        payload = {
            "hook_event_name": "Stop",
            "session_id": "codex-session-001",
            "turn_id": "turn-stable-001",
            "last_assistant_message": canary,
        }
        encoded = json.dumps(payload).encode()

        first = self.run_cli("hook", "--client", "codex", input_bytes=encoded)
        second = self.run_cli("hook", "--client", "codex", input_bytes=encoded)
        self.assertEqual(0, first.returncode, first.stderr.decode())
        self.assertEqual(0, second.returncode, second.stderr.decode())

        with self.open_store() as store:
            events = store.events()
            self.assertEqual(1, len(events))
            self.assertEqual("turn.completed", events[0]["kind"])
            self.assertEqual("codex", events[0]["agent"])
            database = store.paths.database
        self.assertNotIn(canary.encode(), database.read_bytes())

    def test_hook_allowlists_lifecycle_fields_and_never_persists_canaries(self) -> None:
        canaries = (
            "RAW_PROMPT_CANARY_74f321",
            "AUTH_TOKEN_CANARY_91822",
            "TOOL_INPUT_CANARY_55aa1",
            "TRANSCRIPT_CANARY_922de",
        )
        payload = {
            "hook_event_name": "SessionStart",
            "session_id": "claude-session-001",
            "prompt": canaries[0],
            "authorization": canaries[1],
            "tool_input": {"command": canaries[2]},
            "transcript_path": f"/tmp/{canaries[3]}.jsonl",
            "cwd": str(self.repo),
        }
        encoded = json.dumps(payload).encode()

        first = self.run_cli("hook", "--client", "claude", input_bytes=encoded)
        second = self.run_cli("hook", "--client", "claude", input_bytes=encoded)
        self.assertEqual(0, first.returncode, first.stderr.decode())
        self.assertEqual(0, second.returncode, second.stderr.decode())

        with self.open_store() as store:
            events = store.events()
            self.assertEqual(1, len(events), "replayed SessionStart must deduplicate")
            event = events[0]
            self.assertEqual("session.started", event["kind"])
            self.assertEqual("claude", event["agent"])
            self.assertEqual(
                {"branch", "client", "commit", "worktree"}, set(event["meta"])
            )
            database = store.paths.database

        raw_database = database.read_bytes()
        for canary in canaries:
            self.assertNotIn(canary.encode(), raw_database)

    def test_malformed_hook_is_fail_open_and_logs_only_error_class(self) -> None:
        canary = b"MALFORMED_SECRET_CANARY_ef902"
        result = self.run_cli(
            "hook",
            "--client",
            "codex",
            input_bytes=b'{"hook_event_name":"SessionStart","prompt":"' + canary,
        )
        self.assertEqual(0, result.returncode, result.stderr.decode())
        self.assertIn(b"ValidationError", result.stderr)

        paths = resolve_paths(repo=self.repo, state_home=self.home)
        log = paths.hook_error_log.read_bytes()
        self.assertIn(b"codex: ValidationError", log)
        self.assertNotIn(canary, log)
        with self.open_store() as store:
            self.assertEqual([], store.events())

    def test_locked_hook_is_fail_open_without_stealing_or_writing(self) -> None:
        with self.open_store() as store:
            database = store.paths.database
        blocker = sqlite3.connect(database, isolation_level=None, timeout=1)
        try:
            blocker.execute("PRAGMA journal_mode = WAL")
            blocker.execute("BEGIN IMMEDIATE")
            payload = json.dumps(
                {
                    "hook_event_name": "SessionStart",
                    "session_id": "locked-session",
                    "prompt": "LOCKED_CANARY_0123",
                }
            ).encode()
            result = self.run_cli(
                "hook", "--client", "claude", input_bytes=payload, timeout=12
            )
            self.assertEqual(0, result.returncode, result.stderr.decode())
            self.assertIn(b"BusyError", result.stderr)
        finally:
            blocker.execute("ROLLBACK")
            blocker.close()

        paths = resolve_paths(repo=self.repo, state_home=self.home)
        log = paths.hook_error_log.read_text(encoding="utf-8")
        self.assertIn("claude: BusyError", log)
        self.assertNotIn("LOCKED_CANARY", log)
        with self.open_store() as store:
            self.assertEqual([], store.events())

    def test_corrupt_state_hook_is_fail_open_and_does_not_replace_database(self) -> None:
        corrupt_home = self.root / "corrupt-state"
        corrupt_home.mkdir(mode=0o700)
        database = corrupt_home / "relay.sqlite3"
        corrupt_bytes = b"NOT_A_SQLITE_DATABASE_CORRUPTION_CANARY"
        database.write_bytes(corrupt_bytes)
        payload = json.dumps(
            {
                "hook_event_name": "SessionEnd",
                "session_id": "corrupt-session",
                "prompt": "UNPERSISTED_CANARY_14d2",
            }
        ).encode()

        result = self.run_cli(
            "hook",
            "--client",
            "codex",
            home=corrupt_home,
            input_bytes=payload,
        )
        self.assertEqual(0, result.returncode, result.stderr.decode())
        self.assertIn(b"StateError", result.stderr)
        self.assertEqual(corrupt_bytes, database.read_bytes())
        error_log = (corrupt_home / "hook-errors.log").read_text(encoding="utf-8")
        self.assertIn("codex: StateError", error_log)
        self.assertNotIn("UNPERSISTED_CANARY", error_log)


class MetaRatchetTests(RelayTestCase):
    @staticmethod
    def friction_event(event_id: str, cost: int, summary: str) -> dict[str, Any]:
        return {
            "v": 1,
            "id": event_id,
            "kind": "friction.observed",
            "agent": "codex",
            "session": "session-ratchet",
            "target": "unc-apply-patch-acl",
            "summary": summary,
            "meta": {
                "fingerprint": "unc-apply-patch-acl",
                "category": "tooling",
                "cost_seconds": cost,
                "position": "editing an isolated WSL worktree",
                "proposal": "route edits through the supported bridge",
            },
        }

    @staticmethod
    def insert_raw_ratchet_event(
        connection: sqlite3.Connection,
        *,
        event_id: str,
        kind: str,
        fingerprint: str,
        meta_json: str,
        summary: str = "Legacy client appended ratchet state",
    ) -> int:
        cursor = connection.execute(
            """
            INSERT INTO events (
              event_id, protocol_version, kind, agent, session, work_id,
              target, scope, summary, artifact, meta_json, canonical_json,
              body_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                1,
                kind,
                "legacy-client",
                "legacy-session",
                None,
                fingerprint,
                None,
                summary,
                None,
                meta_json,
                meta_json,
                "0" * 64,
            ),
        )
        return int(cursor.lastrowid)

    @staticmethod
    def insert_raw_verification(
        connection: sqlite3.Connection,
        *,
        event_id: str,
        fingerprint: str,
        decision_seq: int,
        outcome: str = "improved",
        drop_compatibility: bool | None = None,
    ) -> None:
        meta: dict[str, Any] = {
            "fingerprint": fingerprint,
            "outcome": outcome,
            "decision_seq": decision_seq,
        }
        if drop_compatibility is not None:
            meta["drop_compatibility"] = drop_compatibility
        meta_json = json.dumps(
            meta,
            sort_keys=True,
            separators=(",", ":"),
        )
        MetaRatchetTests.insert_raw_ratchet_event(
            connection,
            event_id=event_id,
            kind="ratchet.verified",
            fingerprint=fingerprint,
            meta_json=meta_json,
            summary="Legacy client attempted to verify a terminal drop",
        )

    @staticmethod
    def legacy_v2_ratchet_projection(
        connection: sqlite3.Connection,
        fingerprint: str,
    ) -> dict[str, Any]:
        """Run the frozen v2 reducer law against a stale open connection."""

        rows = connection.execute(
            """
            SELECT seq, kind, meta_json
            FROM events
            WHERE kind IN (
              'friction.observed', 'ratchet.decided', 'ratchet.verified'
            )
            ORDER BY seq
            """
        ).fetchall()
        observations: list[dict[str, Any]] = []
        decisions: list[dict[str, Any]] = []
        verifications: list[dict[str, Any]] = []
        for seq, kind, meta_json in rows:
            meta = json.loads(meta_json)
            if meta.get("fingerprint") != fingerprint:
                continue
            event = {"seq": int(seq), "meta": meta}
            if kind == "friction.observed":
                observations.append(event)
            elif kind == "ratchet.decided":
                decisions.append(event)
            else:
                verifications.append(event)

        latest_decision = decisions[-1] if decisions else None
        eligible = [
            event
            for event in verifications
            if latest_decision is not None
            and event["meta"].get("decision_seq") == latest_decision["seq"]
        ]
        latest_verification = eligible[-1] if eligible else None
        recurrence = (
            latest_verification is not None
            and any(
                event["seq"] > latest_verification["seq"]
                for event in observations
            )
        )
        if recurrence:
            state = "recurred"
        elif latest_verification is not None:
            state = latest_verification["meta"]["outcome"]
        elif latest_decision is not None:
            state = "decided"
        else:
            state = "observing"
        actionable = state in {
            "observing",
            "recurred",
            "decided",
            "unchanged",
            "worse",
        }
        return {
            "state": state,
            "actionable": actionable,
            "decision_seq": latest_decision["seq"] if latest_decision else None,
            "has_verification": latest_verification is not None,
        }

    def test_cli_decision_home_does_not_override_relay_state_home(self) -> None:
        fingerprint = "ratchet-home-argparse-collision"
        observed = self.run_cli(
            "friction",
            fingerprint,
            "--agent",
            "codex",
            "--session",
            "real-observation",
            "--category",
            "tooling",
            "--summary",
            "The ratchet destination flag collided with Relay state selection",
            "--position",
            "first cross-agent observer pilot",
            "--proposal",
            "separate argparse destinations while preserving both flag names",
        )
        self.assertEqual(0, observed.returncode, observed.stderr.decode())

        decided = self.run_cli(
            "ratchet",
            "decide",
            fingerprint,
            "--agent",
            "codex",
            "--session",
            "real-observation",
            "--mode",
            "subtract",
            "--home",
            "src/relay_core/cli.py",
            "--summary",
            "Separate state storage from the durable refinement home",
        )
        self.assertEqual(0, decided.returncode, decided.stderr.decode())
        receipt = json.loads(decided.stdout)
        self.assertEqual(
            "src/relay_core/cli.py",
            receipt["event"]["meta"]["home"],
        )
        self.assertTrue((self.home / "relay.sqlite3").is_file())
        with self.open_store() as store:
            self.assertEqual(
                ["friction.observed", "ratchet.decided"],
                [item["kind"] for item in store.events()],
            )

    def test_drop_is_terminal_suppressed_and_reopened_only_by_recurrence(self) -> None:
        help_result = self.run_cli("ratchet", "decide", "--help")
        self.assertEqual(0, help_result.returncode, help_result.stderr.decode())
        help_text = help_result.stdout.decode()
        self.assertIn("SUBTRACT, PROMOTE, or terminal DROP", help_text)
        self.assertIn("{drop,promote,subtract}", help_text)
        verify_help = self.run_cli("ratchet", "verify", "--help")
        self.assertEqual(0, verify_help.returncode, verify_help.stderr.decode())
        self.assertIn(
            "{improved,unchanged,worse}", verify_help.stdout.decode()
        )
        self.assertNotIn("dropped", verify_help.stdout.decode())

        fingerprint = "one-off-provider-transient"
        observed = self.run_cli(
            "friction",
            fingerprint,
            "--agent",
            "claude",
            "--session",
            "session-drop",
            "--category",
            "environment",
            "--summary",
            "A one-off provider restart interrupted a background observer",
        )
        self.assertEqual(0, observed.returncode, observed.stderr.decode())

        decided = self.run_cli(
            "ratchet",
            "decide",
            fingerprint,
            "--agent",
            "claude",
            "--session",
            "session-drop",
            "--mode",
            "drop",
            "--home",
            "relay:friction",
            "--verify-when",
            "only if the provider interruption recurs",
            "--summary",
            "Drop the isolated interruption as non-actionable noise",
        )
        self.assertEqual(0, decided.returncode, decided.stderr.decode())
        decision_receipt = json.loads(decided.stdout)
        self.assertEqual("drop", decision_receipt["event"]["meta"]["mode"])
        self.assertEqual(
            "only if the provider interruption recurs",
            decision_receipt["event"]["meta"]["verify_when"],
        )

        with self.open_store() as store:
            dropped = store.ratchet_review()[0]
            self.assertEqual("dropped", dropped["state"])
            self.assertIsNone(dropped["verification"])
            self.assertEqual(
                decision_receipt["event"]["seq"],
                dropped["terminal_receipt"]["meta"]["decision_seq"],
            )
            self.assertEqual([], store.brief("claude")["ratchet_items"])
            compact = store.status(compact=True)["ratchet"][0]
            self.assertEqual(
                dropped["terminal_receipt"]["seq"],
                compact["terminal_receipt"]["seq"],
            )
            self.assertEqual(
                "only if the provider interruption recurs",
                compact["decision"]["verify_when"],
            )

        rejected = self.run_cli(
            "ratchet",
            "verify",
            fingerprint,
            "--agent",
            "claude",
            "--session",
            "session-drop",
            "--outcome",
            "improved",
            "--summary",
            "A dropped item has no improvement to verify",
        )
        error = self.assert_cli_error(rejected, 73)
        self.assertIn("drop decision", error)
        self.assertIn("terminal and cannot be verified", error)

        redecided = self.run_cli(
            "ratchet",
            "decide",
            fingerprint,
            "--agent",
            "claude",
            "--session",
            "session-drop",
            "--mode",
            "promote",
            "--home",
            "relay:friction",
            "--summary",
            "Try to replace a terminal drop without recurrence",
        )
        decision_error = self.assert_cli_error(redecided, 73)
        self.assertIn("requires a newer friction observation", decision_error)

        recurred = self.run_cli(
            "friction",
            fingerprint,
            "--agent",
            "codex",
            "--session",
            "session-recurrence",
            "--category",
            "environment",
            "--summary",
            "The same provider interruption happened in another session",
        )
        self.assertEqual(0, recurred.returncode, recurred.stderr.decode())
        with self.open_store() as store:
            reopened = store.ratchet_review()[0]
            self.assertEqual("recurred", reopened["state"])
            self.assertEqual(1, reopened["post_verification_recurrence"])
            brief_item = store.brief("codex")["ratchet_items"][0]
            self.assertEqual(fingerprint, brief_item["fingerprint"])
            self.assertEqual("recurred", brief_item["state"])
            self.assertEqual(
                "only if the provider interruption recurs",
                brief_item["decision"]["verify_when"],
            )

    def test_fresh_v2_guards_reject_raw_and_public_drop_verification(self) -> None:
        fingerprint = "unc-apply-patch-acl"
        with self.open_store() as store:
            store.emit(
                self.friction_event(
                    "evt:fresh-v2-friction",
                    5,
                    "One isolated edit bridge failure",
                )
            )
            decision = store.ratchet_decide(
                fingerprint,
                mode="drop",
                home="relay:friction",
                agent="codex",
                session="fresh-v2",
                summary="Drop this isolated failure unless it recurs",
            )["event"]
            terminal_receipt = next(
                event
                for event in store.events()
                if event["meta"].get("drop_compatibility") is True
            )
            self.assertEqual("dropped", terminal_receipt["meta"]["outcome"])
            self.assertEqual(decision["seq"], terminal_receipt["meta"]["decision_seq"])
            self.assertEqual(
                f"drop-close:{decision['seq']}", terminal_receipt["id"]
            )
            self.assertEqual(2, store.diagnostics()["schema_version"])
            self.assertEqual(
                len(_RATCHET_GUARD_NAMES),
                _ratchet_guard_count(store._db),
            )

            other_fingerprint = "ordinary-ratchet-decision"
            store.emit(
                {
                    "v": 1,
                    "id": "evt:ordinary-ratchet-friction",
                    "kind": "friction.observed",
                    "agent": "codex",
                    "session": "fresh-v2",
                    "target": other_fingerprint,
                    "summary": "Ordinary friction remains empirically verifiable",
                    "meta": {
                        "fingerprint": other_fingerprint,
                        "category": "workflow",
                    },
                }
            )
            ordinary_decision = store.ratchet_decide(
                other_fingerprint,
                mode="subtract",
                home="tooling",
                agent="codex",
                session="fresh-v2",
                summary="Remove the ordinary repeated step",
            )["event"]

            raw = sqlite3.connect(store.paths.database)
            try:
                duplicate_fingerprint = "duplicate-ratchet-metadata"
                store.emit(
                    {
                        "v": 1,
                        "id": "evt:duplicate-ratchet-friction",
                        "kind": "friction.observed",
                        "agent": "codex",
                        "session": "fresh-v2",
                        "target": duplicate_fingerprint,
                        "summary": "Exercise raw duplicate-key rejection",
                        "meta": {
                            "fingerprint": duplicate_fingerprint,
                            "category": "tooling",
                        },
                    }
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    self.insert_raw_ratchet_event(
                        raw,
                        event_id="evt:duplicate-mode-bypass",
                        kind="ratchet.decided",
                        fingerprint=duplicate_fingerprint,
                        meta_json=(
                            '{"fingerprint":"duplicate-ratchet-metadata",'
                            '"home":"tooling","mode":"subtract","mode":"drop"}'
                        ),
                    )
                raw.rollback()
                with self.assertRaises(sqlite3.IntegrityError):
                    self.insert_raw_ratchet_event(
                        raw,
                        event_id="evt:duplicate-decision-seq-bypass",
                        kind="ratchet.verified",
                        fingerprint=other_fingerprint,
                        meta_json=(
                            f'{{"decision_seq":{ordinary_decision["seq"]},'
                            '"decision_seq":999999,'
                            f'"fingerprint":"{other_fingerprint}",'
                            '"outcome":"improved"}'
                        ),
                    )
                raw.rollback()
                with self.assertRaisesRegex(
                    sqlite3.IntegrityError,
                    "empirical verification violates",
                ):
                    self.insert_raw_verification(
                        raw,
                        event_id="evt:raw-drop-verify",
                        fingerprint=fingerprint,
                        decision_seq=decision["seq"],
                    )
                raw.rollback()
                with self.assertRaisesRegex(
                    sqlite3.IntegrityError,
                    "invalid ratchet drop compatibility closure",
                ):
                    self.insert_raw_verification(
                        raw,
                        event_id="evt:second-drop-closure",
                        fingerprint=fingerprint,
                        decision_seq=decision["seq"],
                        outcome="dropped",
                        drop_compatibility=True,
                    )
                raw.rollback()
                with self.assertRaisesRegex(
                    sqlite3.IntegrityError,
                    "invalid ratchet drop compatibility closure",
                ):
                    self.insert_raw_verification(
                        raw,
                        event_id="evt:raw-dropped-nondrop",
                        fingerprint=other_fingerprint,
                        decision_seq=ordinary_decision["seq"],
                        outcome="dropped",
                    )
                raw.rollback()
                with self.assertRaisesRegex(sqlite3.IntegrityError, "UNIQUE"):
                    raw.execute(
                        """
                        INSERT INTO events (
                          event_id, protocol_version, kind, agent, session,
                          work_id, target, scope, summary, artifact, meta_json,
                          canonical_json, body_hash
                        )
                        SELECT event_id, protocol_version, kind, agent, session,
                               work_id, target, scope, summary, artifact,
                               meta_json, canonical_json, body_hash
                        FROM events WHERE seq = ?
                        """,
                        (terminal_receipt["seq"],),
                    )
                raw.rollback()
            finally:
                raw.close()

            with self.assertRaisesRegex(
                ValidationError, "drop decision is emitted only"
            ):
                store.emit(
                    {
                        "v": 1,
                        "id": "evt:public-drop-decision",
                        "kind": "ratchet.decided",
                        "agent": "legacy-client",
                        "session": "legacy-session",
                        "target": fingerprint,
                        "summary": "Try to bypass the atomic drop transaction",
                        "meta": {
                            "fingerprint": fingerprint,
                            "mode": "drop",
                            "home": "tooling",
                        },
                    }
                )
            with self.assertRaisesRegex(
                ValidationError, "closure is emitted only"
            ):
                store.emit(
                    {
                        "v": 1,
                        "id": "evt:public-drop-closure",
                        "kind": "ratchet.verified",
                        "agent": "codex",
                        "session": "fresh-v2",
                        "target": fingerprint,
                        "summary": "Try to forge the internal terminal receipt",
                        "meta": {
                            "fingerprint": fingerprint,
                            "outcome": "dropped",
                            "decision_seq": decision["seq"],
                            "drop_compatibility": True,
                        },
                    }
                )

            with self.assertRaisesRegex(
                StateError, "empirical verification violates"
            ):
                store.emit(
                    {
                        "v": 1,
                        "id": "evt:public-drop-verify",
                        "kind": "ratchet.verified",
                        "agent": "legacy-client",
                        "session": "legacy-session",
                        "target": fingerprint,
                        "summary": "Try to bypass the ratchet API with public emit",
                        "meta": {
                            "fingerprint": fingerprint,
                            "outcome": "improved",
                            "decision_seq": decision["seq"],
                        },
                    }
                )
            persisted = store.events()
            self.assertEqual(
                1,
                sum(
                    event["meta"].get("drop_compatibility") is True
                    for event in persisted
                ),
            )
            self.assertFalse(
                {
                    "evt:raw-drop-verify",
                    "evt:second-drop-closure",
                    "evt:raw-dropped-nondrop",
                    "evt:public-drop-decision",
                    "evt:public-drop-closure",
                    "evt:public-drop-verify",
                    "evt:duplicate-mode-bypass",
                    "evt:duplicate-decision-seq-bypass",
                }
                & {event["id"] for event in persisted}
            )

    def test_every_stored_event_has_a_truthful_body_hash(self) -> None:
        fingerprint = "drop-body-hash"
        with self.open_store() as store:
            store.emit(
                {
                    "v": 1,
                    "id": "evt:drop-hash-friction",
                    "kind": "friction.observed",
                    "agent": "codex",
                    "session": "drop-hash",
                    "target": fingerprint,
                    "summary": "Exercise truthful hashes through terminal closure",
                    "meta": {
                        "fingerprint": fingerprint,
                        "category": "verification",
                    },
                }
            )
            store.ratchet_decide(
                fingerprint,
                mode="drop",
                home="relay:friction",
                agent="codex",
                session="drop-hash",
                summary="Drop the isolated hash-check episode",
            )
            rows = store._db.execute(
                "SELECT canonical_json, body_hash FROM events ORDER BY seq"
            ).fetchall()
            self.assertEqual(3, len(rows))
            for row in rows:
                self.assertEqual(
                    hashlib.sha256(row["canonical_json"].encode("utf-8")).hexdigest(),
                    row["body_hash"],
                )

    def test_raw_ratchet_domains_and_verification_cardinality_are_guarded(self) -> None:
        fingerprint = "raw-ratchet-domains"
        with self.open_store() as store:
            store.emit(
                {
                    "v": 1,
                    "id": "evt:raw-domain-friction",
                    "kind": "friction.observed",
                    "agent": "codex",
                    "session": "raw-domains",
                    "target": fingerprint,
                    "summary": "Exercise raw ratchet discriminator bounds",
                    "meta": {
                        "fingerprint": fingerprint,
                        "category": "verification",
                    },
                }
            )
            raw = sqlite3.connect(store.paths.database)
            try:
                for event_id, candidate_fingerprint, mode in (
                    ("evt:raw-invalid-mode", fingerprint, "archive"),
                    ("evt:raw-no-observation", "raw-unobserved-friction", "subtract"),
                ):
                    with self.assertRaisesRegex(
                        sqlite3.IntegrityError, "decision violates"
                    ):
                        self.insert_raw_ratchet_event(
                            raw,
                            event_id=event_id,
                            kind="ratchet.decided",
                            fingerprint=candidate_fingerprint,
                            meta_json=json.dumps(
                                {
                                    "fingerprint": candidate_fingerprint,
                                    "home": "tooling",
                                    "mode": mode,
                                },
                                sort_keys=True,
                                separators=(",", ":"),
                            ),
                        )
                    raw.rollback()

                decision = store.ratchet_decide(
                    fingerprint,
                    mode="subtract",
                    home="tooling",
                    agent="codex",
                    session="raw-domains",
                    summary="Create one empirically verifiable decision",
                )["event"]
                invalid_outcome = json.dumps(
                    {
                        "decision_seq": decision["seq"],
                        "fingerprint": fingerprint,
                        "outcome": "better",
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
                with self.assertRaisesRegex(
                    sqlite3.IntegrityError, "empirical verification violates"
                ):
                    self.insert_raw_ratchet_event(
                        raw,
                        event_id="evt:raw-invalid-outcome",
                        kind="ratchet.verified",
                        fingerprint=fingerprint,
                        meta_json=invalid_outcome,
                    )
                raw.rollback()

                store.ratchet_verify(
                    fingerprint,
                    outcome="improved",
                    agent="codex",
                    session="raw-domains",
                    summary="Close the one empirical decision",
                )
                duplicate_verification = json.dumps(
                    {
                        "decision_seq": decision["seq"],
                        "fingerprint": fingerprint,
                        "outcome": "unchanged",
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
                with self.assertRaisesRegex(
                    sqlite3.IntegrityError, "empirical verification violates"
                ):
                    self.insert_raw_ratchet_event(
                        raw,
                        event_id="evt:raw-duplicate-verification",
                        kind="ratchet.verified",
                        fingerprint=fingerprint,
                        meta_json=duplicate_verification,
                    )
                raw.rollback()
            finally:
                raw.close()

    def test_future_verification_cannot_attach_to_a_later_drop(self) -> None:
        fingerprint = "future-verification-attachment"
        with self.open_store() as store:
            store.emit(
                {
                    "v": 1,
                    "id": "evt:future-verification-friction",
                    "kind": "friction.observed",
                    "agent": "codex",
                    "session": "future-verification",
                    "target": fingerprint,
                    "summary": "Exercise a future decision reference",
                    "meta": {
                        "fingerprint": fingerprint,
                        "category": "verification",
                    },
                }
            )
            with self.assertRaisesRegex(
                StateError, "empirical verification violates"
            ):
                store.emit(
                    {
                        "v": 1,
                        "id": "evt:future-verification",
                        "kind": "ratchet.verified",
                        "agent": "legacy-client",
                        "session": "future-verification",
                        "target": fingerprint,
                        "summary": "Try to pre-attach verification to a future decision",
                        "meta": {
                            "fingerprint": fingerprint,
                            "outcome": "improved",
                            "decision_seq": 2,
                        },
                    }
                )
            decision = store.ratchet_decide(
                fingerprint,
                mode="drop",
                home="relay:friction",
                agent="codex",
                session="future-verification",
                summary="Drop the isolated episode after rejecting forged closure",
            )["event"]
            self.assertEqual(2, decision["seq"])
            self.assertNotIn(
                "evt:future-verification",
                {event["id"] for event in store.events()},
            )

    def test_complete_guard_set_skips_repeat_historical_audit(self) -> None:
        with self.open_store() as current:
            database = current.paths.database
            self.assertEqual(
                len(_RATCHET_GUARD_NAMES),
                _ratchet_guard_count(current._db),
            )

        class FastPathStore(RelayStore):
            def _audit_and_install_v2_ratchet_guards(self) -> None:
                raise AssertionError("complete guard set must skip historical audit")

        with FastPathStore.open(repo=self.repo, state_home=self.home) as reopened:
            self.assertEqual(database, reopened.paths.database)
            self.assertEqual(
                len(_RATCHET_GUARD_NAMES),
                _ratchet_guard_count(reopened._db),
            )

    def test_missing_guard_reaudits_and_fails_closed_on_invalid_history(self) -> None:
        fingerprint = "missing-guard-reaudit"
        with self.open_store() as current:
            current.emit(
                {
                    "v": 1,
                    "id": "evt:missing-guard-friction",
                    "kind": "friction.observed",
                    "agent": "codex",
                    "session": "missing-guard",
                    "target": fingerprint,
                    "summary": "Exercise fail-closed guard repair",
                    "meta": {
                        "fingerprint": fingerprint,
                        "category": "verification",
                    },
                }
            )
            database = current.paths.database

        damaged = sqlite3.connect(database)
        try:
            damaged.execute("DROP TRIGGER ratchet_decision_must_be_admissible")
            self.insert_raw_ratchet_event(
                damaged,
                event_id="evt:invalid-history-behind-missing-guard",
                kind="ratchet.decided",
                fingerprint=fingerprint,
                meta_json=json.dumps(
                    {
                        "fingerprint": fingerprint,
                        "home": "tooling",
                        "mode": "archive",
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            )
            damaged.commit()
        finally:
            damaged.close()

        with self.assertRaisesRegex(StateError, "decisions outside"):
            self.open_store()

        unchanged = sqlite3.connect(database)
        try:
            self.assertEqual(
                len(_RATCHET_GUARD_NAMES) - 1,
                _ratchet_guard_count(unchanged),
            )
        finally:
            unchanged.close()

    def test_v2_in_place_hardening_blocks_stale_writer_without_version_bump(self) -> None:
        with self.open_store() as current:
            database = current.paths.database

        downgrade = sqlite3.connect(database)
        try:
            _drop_ratchet_guards(downgrade)
            downgrade.commit()
        finally:
            downgrade.close()

        stale_v2_connection = sqlite3.connect(database)
        try:
            with self.open_store() as migrated:
                self.assertEqual(2, migrated.diagnostics()["schema_version"])
                self.assertEqual(
                    2,
                    stale_v2_connection.execute("PRAGMA user_version").fetchone()[0],
                )
                migrated.emit(
                    self.friction_event(
                        "evt:mixed-v2-friction",
                        3,
                        "A mixed-version writer exposed a terminal-state gap",
                    )
                )
                decision = migrated.ratchet_decide(
                    "unc-apply-patch-acl",
                    mode="drop",
                    home="relay:friction",
                    agent="codex",
                    session="mixed-v2",
                    summary="Drop the isolated mixed-version episode",
                )["event"]
                legacy_closed = self.legacy_v2_ratchet_projection(
                    stale_v2_connection,
                    "unc-apply-patch-acl",
                )
                self.assertEqual("dropped", legacy_closed["state"])
                self.assertFalse(legacy_closed["actionable"])
                self.assertTrue(legacy_closed["has_verification"])
                self.assertEqual(decision["seq"], legacy_closed["decision_seq"])
                current_closed = migrated.ratchet_review()[0]
                self.assertEqual("dropped", current_closed["state"])
                self.assertIsNone(current_closed["verification"])
                self.assertIsNotNone(current_closed["terminal_receipt"])
                with self.assertRaisesRegex(
                    sqlite3.IntegrityError,
                    "empirical verification violates",
                ):
                    self.insert_raw_verification(
                        stale_v2_connection,
                        event_id="evt:stale-v2-drop-verify",
                        fingerprint="unc-apply-patch-acl",
                        decision_seq=decision["seq"],
                    )
                stale_v2_connection.rollback()
                stale_decision_meta = json.dumps(
                    {
                        "fingerprint": "unc-apply-patch-acl",
                        "home": "tooling",
                        "mode": "subtract",
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
                with self.assertRaisesRegex(
                    sqlite3.IntegrityError,
                    "decision violates",
                ):
                    self.insert_raw_ratchet_event(
                        stale_v2_connection,
                        event_id="evt:stale-v2-redecision",
                        kind="ratchet.decided",
                        fingerprint="unc-apply-patch-acl",
                        meta_json=stale_decision_meta,
                    )
                stale_v2_connection.rollback()
                migrated.emit(
                    self.friction_event(
                        "evt:mixed-v2-recurrence",
                        2,
                        "The mixed-version episode recurred after terminal closure",
                    )
                )
                legacy_reopened = self.legacy_v2_ratchet_projection(
                    stale_v2_connection,
                    "unc-apply-patch-acl",
                )
                self.assertEqual("recurred", legacy_reopened["state"])
                self.assertTrue(legacy_reopened["actionable"])
                self.assertEqual("recurred", migrated.ratchet_review()[0]["state"])
                replacement = migrated.ratchet_decide(
                    "unc-apply-patch-acl",
                    mode="promote",
                    home="tooling",
                    agent="codex",
                    session="mixed-v2",
                    summary="Promote the repeated mixed-version failure",
                )["event"]
                self.assertEqual("promote", replacement["meta"]["mode"])
        finally:
            stale_v2_connection.close()

    def test_v2_hardening_refuses_preexisting_drop_verification_transactionally(
        self,
    ) -> None:
        fingerprint = "unc-apply-patch-acl"
        with self.open_store() as current:
            current.emit(
                self.friction_event(
                    "evt:preexisting-v2-friction",
                    4,
                    "A v2 overlap occurred before the additive guards were installed",
                )
            )
            decision = current.ratchet_decide(
                fingerprint,
                mode="drop",
                home="relay:friction",
                agent="codex",
                session="preexisting-v2",
                summary="Drop the isolated overlap",
            )["event"]
            database = current.paths.database

        v2 = sqlite3.connect(database)
        try:
            _drop_ratchet_guards(v2)
            self.insert_raw_verification(
                v2,
                event_id="evt:preexisting-v2-drop-verify",
                fingerprint=fingerprint,
                decision_seq=decision["seq"],
            )
            v2.commit()
        finally:
            v2.close()

        with self.assertRaisesRegex(
            StateError, "empirical verification outside"
        ):
            self.open_store()

        unchanged = sqlite3.connect(database)
        try:
            self.assertEqual(2, unchanged.execute("PRAGMA user_version").fetchone()[0])
            self.assertEqual(
                0,
                _ratchet_guard_count(unchanged),
            )
            self.assertEqual(4, unchanged.execute("SELECT COUNT(*) FROM events").fetchone()[0])
        finally:
            unchanged.close()

    def test_v2_hardening_refuses_unpaired_drop_even_after_recurrence(self) -> None:
        fingerprint = "unc-apply-patch-acl"
        with self.open_store() as current:
            current.emit(
                self.friction_event(
                    "evt:unpaired-drop-friction",
                    4,
                    "A raw v2 writer created an incomplete terminal state",
                )
            )
            database = current.paths.database

        v2 = sqlite3.connect(database)
        try:
            _drop_ratchet_guards(v2)
            drop_meta = json.dumps(
                {
                    "fingerprint": fingerprint,
                    "home": "relay:friction",
                    "mode": "drop",
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            self.insert_raw_ratchet_event(
                v2,
                event_id="evt:unpaired-v2-drop",
                kind="ratchet.decided",
                fingerprint=fingerprint,
                meta_json=drop_meta,
            )
            recurrence_meta = json.dumps(
                {"category": "tooling", "fingerprint": fingerprint},
                sort_keys=True,
                separators=(",", ":"),
            )
            self.insert_raw_ratchet_event(
                v2,
                event_id="evt:unpaired-v2-recurrence",
                kind="friction.observed",
                fingerprint=fingerprint,
                meta_json=recurrence_meta,
            )
            v2.commit()
        finally:
            v2.close()

        with self.assertRaisesRegex(StateError, "unpaired terminal drop"):
            self.open_store()

        unchanged = sqlite3.connect(database)
        try:
            self.assertEqual(2, unchanged.execute("PRAGMA user_version").fetchone()[0])
            self.assertEqual(
                0,
                _ratchet_guard_count(unchanged),
            )
            self.assertEqual(3, unchanged.execute("SELECT COUNT(*) FROM events").fetchone()[0])
        finally:
            unchanged.close()

    def test_decision_verify_when_is_optional_and_bounded(self) -> None:
        with self.assertRaisesRegex(ValidationError, "verify_when exceeds 200"):
            normalize_event(
                {
                    "v": 1,
                    "kind": "ratchet.decided",
                    "agent": "codex",
                    "session": "session-ratchet",
                    "target": "bounded-verification-condition",
                    "summary": "Record a deliberately oversized verification boundary",
                    "meta": {
                        "fingerprint": "bounded-verification-condition",
                        "mode": "subtract",
                        "home": "tooling",
                        "verify_when": "v" * 201,
                    },
                }
            )

    def test_observe_decide_verify_and_recurrence_form_a_closed_loop(self) -> None:
        with self.open_store() as store:
            with self.assertRaisesRegex(ConflictError, "no friction observation"):
                store.ratchet_decide(
                    "unc-apply-patch-acl",
                    mode="subtract",
                    home="tooling",
                    agent="codex",
                    session="session-ratchet",
                    summary="Premature decision",
                )

            store.emit(
                self.friction_event(
                    "evt:friction-first", 90, "ACL bridge discovery cost 90 seconds"
                )
            )
            observing = store.ratchet_review()[0]
            self.assertEqual("observing", observing["state"])
            self.assertEqual(1, observing["count"])
            self.assertEqual(90, observing["total_cost_seconds"])

            store.ratchet_decide(
                "unc-apply-patch-acl",
                mode="subtract",
                home="src/relay.py",
                agent="codex",
                session="session-ratchet",
                summary="Teach Relay the supported edit bridge",
            )
            self.assertEqual("decided", store.ratchet_review()[0]["state"])

            store.ratchet_verify(
                "unc-apply-patch-acl",
                outcome="improved",
                agent="codex",
                session="session-ratchet",
                summary="Bridge removed the repeated retry",
                before_cost_seconds=90,
                after_cost_seconds=8,
                evidence="git:abcdef1234567",
            )
            improved = store.ratchet_review()[0]
            self.assertEqual("improved", improved["state"])
            self.assertEqual("subtract", improved["decision"]["meta"]["mode"])
            self.assertEqual("improved", improved["verification"]["meta"]["outcome"])

            store.emit(
                self.friction_event(
                    "evt:friction-recurred",
                    12,
                    "A second position still hit the ACL boundary",
                )
            )
            recurred = store.ratchet_review()[0]
            self.assertEqual("recurred", recurred["state"])
            self.assertEqual(2, recurred["count"])
            self.assertEqual(102, recurred["total_cost_seconds"])
            self.assertGreater(
                recurred["last_seq"], recurred["verification"]["seq"]
            )
            with self.assertRaisesRegex(ConflictError, "already verified"):
                store.ratchet_verify(
                    "unc-apply-patch-acl",
                    outcome="improved",
                    agent="codex",
                    session="session-ratchet",
                    summary="Cannot reuse the pre-recurrence decision",
                )


class DurabilityTests(RelayTestCase):
    def test_wal_full_sync_private_modes_and_integrity_are_enforced(self) -> None:
        with self.open_store() as store:
            store.emit(self.valid_event())
            self.assertEqual("wal", store._db.execute("PRAGMA journal_mode").fetchone()[0])
            self.assertEqual(2, store._db.execute("PRAGMA synchronous").fetchone()[0])
            self.assertEqual(1, store._db.execute("PRAGMA foreign_keys").fetchone()[0])
            self.assertEqual(2, store._db.execute("PRAGMA user_version").fetchone()[0])
            self.assertEqual("ok", store.integrity_check())

            paths = store.paths
            self.assertEqual(0o700, stat.S_IMODE(paths.state_dir.stat().st_mode))
            self.assertEqual(0o600, stat.S_IMODE(paths.database.stat().st_mode))
            for suffix in ("-wal", "-shm"):
                sidecar = Path(f"{paths.database}{suffix}")
                if sidecar.exists():
                    self.assertEqual(0o600, stat.S_IMODE(sidecar.stat().st_mode))

        doctor = self.run_cli("doctor")
        self.assertEqual(0, doctor.returncode, doctor.stderr.decode())
        report = json.loads(doctor.stdout)
        self.assertTrue(report["ok"])
        self.assertEqual("ok", report["integrity"])
        self.assertEqual(str(paths.database), report["database"])

    def test_uncommitted_wal_write_is_rolled_back_after_process_death(self) -> None:
        with self.open_store() as store:
            store.emit(self.valid_event())
            database = store.paths.database

        crash_script = textwrap.dedent(
            '''
            import os
            import sqlite3
            import sys

            database = sys.argv[1]
            db = sqlite3.connect(database, isolation_level=None)
            db.execute("PRAGMA journal_mode = WAL")
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                """INSERT INTO events (
                    event_id, protocol_version, kind, agent, session, summary,
                    meta_json, canonical_json, body_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    "evt:uncommitted-crash", 1, "work.intent", "crash", "crash-session",
                    "must roll back", "{}", "{}", "deadbeef",
                ),
            )
            os.kill(os.getpid(), 9)
            '''
        )
        crashed = subprocess.run(
            [sys.executable, "-c", crash_script, str(database)],
            capture_output=True,
            timeout=10,
        )
        self.assertIn(crashed.returncode, (-signal.SIGKILL, 137))

        with self.open_store() as recovered:
            self.assertEqual("ok", recovered.integrity_check())
            self.assertEqual(
                ["evt:contract-0001"],
                [event["id"] for event in recovered.events()],
            )


def _git_toplevel_or_empty(path: Path) -> str:
    """Toplevel of `path`'s repository, or '' when it is outside every repository."""

    result = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        timeout=15,
        env=sanitized_env(),
    )
    return result.stdout.strip() if result.returncode == 0 else ""


class WorkspaceBindingTests(RelayTestCase):
    """Phase 1: Relay refuses a workspace it is not bound to, before any mutation.

    Subprocess cases execute the COPIED candidate runtime inside the disposable
    owner repository.  A parent-process patch of the private resolver does not
    cross `subprocess.run`, so only a copy that physically lives in this
    repository binds a child to it; asserting against the shipping binding would
    prove nothing about the candidate bytes.
    """

    def setUp(self) -> None:
        super().setUp()
        # A second, genuinely unrelated repository - the shape the incident took.
        self.foreign = self.root / "foreign"
        self.foreign.mkdir()
        subprocess.run(
            ["git", "init", "-q", "-b", "main", str(self.foreign)],
            check=True,
            capture_output=True,
            timeout=15,
        )
        _git(self.foreign, "config", "user.name", "Foreign")
        _git(self.foreign, "config", "user.email", "foreign@example.invalid")
        (self.foreign / "README.md").write_text("foreign\n", encoding="utf-8")
        _git(self.foreign, "add", "README.md")
        _git(self.foreign, "commit", "-q", "-m", "seed")
        self.foreign_common = Path(
            _git(self.foreign, "rev-parse", "--path-format=absolute", "--git-common-dir")
        ).resolve()

    # ---- helpers ------------------------------------------------------------

    def snapshot(self, root: Path) -> dict:
        """Byte / tree / mode / database snapshot of a state destination."""

        captured: dict = {}
        if not root.exists():
            return captured
        captured["."] = (stat.S_IMODE(root.stat().st_mode), "dir")
        for path in sorted(root.rglob("*")):
            key = str(path.relative_to(root))
            mode = stat.S_IMODE(path.lstat().st_mode)
            if path.is_symlink():
                captured[key] = (mode, "symlink")
            elif path.is_dir():
                captured[key] = (mode, "dir")
            else:
                captured[key] = (mode, hashlib.sha256(path.read_bytes()).hexdigest())
        return captured

    def assert_refusal(self, result, *, expect_expected: bool) -> str:
        """Universal refusal assertions plus the branch-correct diagnostic."""

        self.assertEqual(74, result.returncode, msg=f"stderr={result.stderr!r}")
        self.assertEqual(b"", result.stdout)
        stderr = result.stderr.decode("utf-8", errors="replace")
        self.assertIn(str(self.foreign_common), stderr)
        if expect_expected:
            self.assertIn(str(self.repo_common), stderr)
        else:
            self.assertIn("no expected workspace binding", stderr)
            self.assertIn("could be derived for the shipping package", stderr)
        return stderr

    # ---- R3 / R8: fresh-target refusals --------------------------------------

    def test_unrelated_cwd_without_repo_or_home_refuses_and_creates_nothing(self) -> None:
        result = self.run_cli(
            "events", "--limit", "1", repo=None, home=None, cwd=self.foreign
        )
        self.assert_refusal(result, expect_expected=True)
        self.assertFalse((self.foreign / ".relay").exists())

    def test_explicit_unrelated_repo_refuses_and_creates_nothing(self) -> None:
        result = self.run_cli("events", "--limit", "1", repo=self.foreign, home=None)
        self.assert_refusal(result, expect_expected=True)
        self.assertFalse((self.foreign / ".relay").exists())

    def test_frozen_path_succeeds_and_creates_state_for_the_owner(self) -> None:
        """The command the refusal cases use must succeed on the correct repo.

        Without this companion a refusal could be passing for an unrelated reason.
        """

        result = self.run_cli("events", "--limit", "1", home=None)
        self.assertEqual(0, result.returncode, msg=f"stderr={result.stderr!r}")
        self.assertTrue((self.repo / ".relay" / "relay.sqlite3").exists())

    # ---- R9 / R10: pre-existing homes survive untouched ----------------------

    def _seed_home(self, home: Path) -> dict:
        with self.open_store(home=home) as store:
            store.emit(self.valid_event())
        self.assertTrue((home / "relay.sqlite3").exists())
        return self.snapshot(home)

    def test_foreign_repo_with_explicit_home_refuses_and_leaves_home_unchanged(self) -> None:
        home = self.root / "explicit-home"
        before = self._seed_home(home)

        result = self.run_cli("events", "--limit", "1", repo=self.foreign, home=home)
        self.assert_refusal(result, expect_expected=True)
        self.assertEqual(before, self.snapshot(home))
        self.assertFalse((self.foreign / ".relay").exists())

        # Positive companion: the same home works from the correct repository, so
        # the refusal is about identity rather than a broken fixture.
        ok = self.run_cli("events", "--limit", "1", home=home)
        self.assertEqual(0, ok.returncode, msg=f"stderr={ok.stderr!r}")

    def test_foreign_repo_with_env_home_refuses_and_leaves_home_unchanged(self) -> None:
        home = self.root / "env-home"
        before = self._seed_home(home)

        env = sanitized_env(RELAY_HOME=str(home))
        result = self.run_cli(
            "events", "--limit", "1", repo=self.foreign, home=None, env=env
        )
        self.assert_refusal(result, expect_expected=True)
        self.assertEqual(before, self.snapshot(home))
        self.assertFalse((self.foreign / ".relay").exists())

        ok = self.run_cli("events", "--limit", "1", home=None, env=env)
        self.assertEqual(0, ok.returncode, msg=f"stderr={ok.stderr!r}")

    # ---- R11: readonly is not exempt ----------------------------------------

    def test_foreign_readonly_refuses_against_a_real_validated_ledger(self) -> None:
        with self.open_store() as store:
            store.emit(self.valid_event())
        self.assertTrue((self.home / "relay.sqlite3").is_file())
        with RelayStore.open_readonly(repo=self.repo, state_home=self.home) as store:
            self.assertEqual("ok", store.integrity_check())
        before = self.snapshot(self.home)

        with self.assertRaises(StateError) as caught:
            resolve_paths(repo=self.foreign, state_home=self.home, create_state=False)
        self.assertIn(str(self.foreign_common), str(caught.exception))
        self.assertIn(str(self.repo_common), str(caught.exception))

        with self.assertRaises(StateError):
            RelayStore.open_readonly(repo=self.foreign, state_home=self.home)

        self.assertEqual(before, self.snapshot(self.home))

    # ---- direct in-process mismatch -----------------------------------------

    def test_direct_resolve_and_open_refuse_a_foreign_workspace(self) -> None:
        with self.assertRaises(StateError):
            resolve_paths(repo=self.foreign)
        with self.assertRaises(StateError):
            RelayStore.open(repo=self.foreign)
        self.assertFalse((self.foreign / ".relay").exists())

    # ---- R16: no derivable binding fails closed ------------------------------

    def test_runtime_outside_every_git_repository_fails_closed(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        cli = install_candidate_runtime(outside)
        self.assertEqual(
            "", _git_toplevel_or_empty(cli.parent), "fixture must sit outside Git"
        )

        result = subprocess.run(
            [
                sys.executable,
                str(cli),
                "--repo",
                str(self.repo),
                "--json",
                "events",
                "--limit",
                "1",
            ],
            capture_output=True,
            timeout=15,
            env=sanitized_env(),
        )
        self.assertEqual(74, result.returncode, msg=f"stderr={result.stderr!r}")
        self.assertEqual(b"", result.stdout)
        stderr = result.stderr.decode("utf-8", errors="replace")
        self.assertIn("no expected workspace binding", stderr)
        self.assertIn("could be derived for the shipping package", stderr)
        self.assertIn(str(cli.parent / "relay_core"), stderr)
        self.assertIn(str(self.repo_common), stderr)
        self.assertFalse((self.repo / ".relay").exists())

    # ---- R5 / R6 / R7 / R13: positives still resolve -------------------------

    def test_ambient_cwd_resolves_for_every_owner_shape(self) -> None:
        """R1, R2, R6, R7 through their specified AMBIENT path: no --repo at all.

        Passing --repo would prove the flag works, not that the ambient
        `os.getcwd()` fallback still resolves for the owner — which is the path
        the incident actually travelled.
        """

        linked = self.root / "linked"
        _git(self.repo, "worktree", "add", "-q", "-b", "peer", str(linked), "HEAD")
        primary_nested = self.repo / "nested"
        primary_nested.mkdir()
        linked_nested = linked / "nested"
        linked_nested.mkdir()

        shapes = {
            "R1 primary root": self.repo,
            "R2 primary nested": primary_nested,
            "R6 linked root": linked,
            "R7 linked nested": linked_nested,
        }
        for label, cwd in shapes.items():
            with self.subTest(shape=label):
                result = self.run_cli(
                    "events", "--limit", "1", repo=None, home=self.home, cwd=cwd
                )
                self.assertEqual(
                    0, result.returncode, msg=f"{label}: stderr={result.stderr!r}"
                )

    def test_linked_worktree_and_nested_paths_still_resolve(self) -> None:
        linked = self.root / "linked-direct"
        _git(self.repo, "worktree", "add", "-q", "-b", "peer-direct", str(linked), "HEAD")
        nested = linked / "nested"
        nested.mkdir()

        for target in (self.repo, linked, nested):
            paths = resolve_paths(repo=target, state_home=self.home)
            self.assertEqual(self.repo_common, paths.git_common_dir)

    def test_cwd_drift_with_explicit_correct_repo_still_resolves(self) -> None:
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        result = self.run_cli("events", "--limit", "1", cwd=elsewhere)
        self.assertEqual(0, result.returncode, msg=f"stderr={result.stderr!r}")

    # ---- R14: mode tightening, explicit AND derived --------------------------

    def test_pre_existing_loose_state_dir_is_tightened_for_both_shapes(self) -> None:
        explicit = self.root / "loose-explicit"
        explicit.mkdir()
        os.chmod(explicit, 0o755)
        self.assertEqual(0o755, stat.S_IMODE(explicit.stat().st_mode))
        resolve_paths(repo=self.repo, state_home=explicit)
        self.assertEqual(0o700, stat.S_IMODE(explicit.stat().st_mode))

        derived = self.repo / ".relay"
        derived.mkdir()
        os.chmod(derived, 0o755)
        self.assertEqual(0o755, stat.S_IMODE(derived.stat().st_mode))
        resolve_paths(repo=self.repo)
        self.assertEqual(0o700, stat.S_IMODE(derived.stat().st_mode))

    # ---- R12: the hook stays fail-open and silent on disk --------------------

    def test_foreign_hook_exits_zero_and_touches_neither_destination(self) -> None:
        """The hook passes no --home, so its owner destination is DERIVED.

        Watching `self.home` here would be vacuous: an unhomed hook would never
        write there even if the guard were absent.  The destinations that matter
        are the foreign derived one (where a leak would land) and the owner's own
        derived `repo/.relay`.
        """

        with self.open_store() as store:
            store.emit(self.valid_event())

        owner_derived = self.repo / ".relay"
        foreign_derived = self.foreign / ".relay"
        before = {
            "owner_derived": self.snapshot(owner_derived),
            "foreign_derived": self.snapshot(foreign_derived),
            "explicit_home": self.snapshot(self.home),
        }

        payload = json.dumps(
            {"hook_event_name": "SessionStart", "session_id": "s-1"}
        ).encode("utf-8")
        result = subprocess.run(
            [
                sys.executable,
                str(self.cli),
                "--repo",
                str(self.foreign),
                "hook",
                "--client",
                "claude",
            ],
            input=payload,
            capture_output=True,
            timeout=15,
            env=sanitized_env(),
        )
        self.assertEqual(0, result.returncode)
        self.assertEqual(b"", result.stdout)
        self.assertNotEqual(b"", result.stderr)

        after = {
            "owner_derived": self.snapshot(owner_derived),
            "foreign_derived": self.snapshot(foreign_derived),
            "explicit_home": self.snapshot(self.home),
        }
        self.assertEqual(before, after)
        self.assertFalse(foreign_derived.exists())
        self.assertFalse((foreign_derived / "hook-errors.log").exists())
        self.assertFalse((owner_derived / "hook-errors.log").exists())
        self.assertFalse((self.home / "hook-errors.log").exists())

    def test_owner_hook_writes_at_its_derived_destination(self) -> None:
        """Positive companion: an unhomed hook DOES reach repo/.relay.

        Without this the refusal above could pass because the hook never writes
        anywhere, rather than because the guard stopped it.
        """

        payload = json.dumps(
            {"hook_event_name": "SessionStart", "session_id": "s-owner"}
        ).encode("utf-8")
        result = subprocess.run(
            [sys.executable, str(self.cli), "--repo", str(self.repo),
             "hook", "--client", "claude"],
            input=payload,
            capture_output=True,
            timeout=15,
            env=sanitized_env(),
        )
        self.assertEqual(0, result.returncode)
        self.assertTrue((self.repo / ".relay" / "relay.sqlite3").is_file())


class CandidatePackageSetTests(unittest.TestCase):
    """The copied candidate is a SEALED set — the fixture's own contract.

    `WorkspaceBindingTests` proves the guard by executing this copy, so the copy
    is load-bearing evidence: anything it silently admits becomes part of what
    the binding proof actually ran.  These laws pin it to exactly the four
    package files, to the shipping package's real membership, to the CURRENT
    source bytes, and make every hostile source shape refuse before writing.

    "Sealed" describes what the copier WRITES.  Executing the candidate later
    lets CPython drop its own `__pycache__` beside it; that is the runtime's
    doing, not the copier's, so every law here inspects a private destination it
    never runs.

    Owner-succeeds / unrelated-refuses is deliberately NOT restated here: R3, R8
    and the frozen-path companion above already prove it against this copier,
    and a duplicate would add lines without adding a witness.
    """

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="relay-candidate-")
        self.root = Path(self._temporary.name)
        self._staged = 0

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def stage_source(self) -> Path:
        """A disposable copy of the real source tree, safe to make hostile.

        An adversary case must never write into the shipping tree, so each one
        corrupts its own staging directory and passes it through `source=`.
        """

        self._staged += 1
        staging = self.root / f"staging-{self._staged}"
        (staging / "relay_core").mkdir(parents=True)
        (staging / "relay.py").write_bytes((SOURCE_DIR / "relay.py").read_bytes())
        for name in _CANDIDATE_PACKAGE_FILES:
            (staging / "relay_core" / name).write_bytes(
                (SOURCE_DIR / "relay_core" / name).read_bytes()
            )
        return staging

    def installed_names(self, destination_root: Path) -> list[str]:
        package = destination_root / "src" / "relay_core"
        return sorted(str(p.relative_to(package)) for p in package.rglob("*"))

    def stage_non_regular(self, path: Path, kind: str) -> None:
        """Replace `path` with a non-regular object of `kind`.

        The FIFO is deliberately mode 0o000.  A named pipe is the sharpest
        non-regular shape, but under a regression that reads BEFORE it lstats,
        `read_bytes()` on a writer-less FIFO blocks forever and unittest has no
        per-test timeout — the law would hang CI instead of going red.  With no
        permission bits the same regression fails immediately with EACCES, so
        the witness survives and the failure mode stays a red.
        """

        path.unlink()
        if kind == "fifo":
            os.mkfifo(path)
            path.chmod(0o000)
        elif kind == "directory":
            path.mkdir()
        else:
            raise AssertionError(f"unknown non-regular kind: {kind}")

    def test_candidate_package_is_exactly_the_closed_file_set(self) -> None:
        destination = self.root / "from-shipping"
        entry = install_candidate_runtime(destination)
        self.assertTrue(entry.is_file())
        self.assertEqual(
            sorted(_CANDIDATE_PACKAGE_FILES), self.installed_names(destination)
        )

    def test_closed_set_matches_the_shipping_package_membership(self) -> None:
        """Bind the allowlist to reality instead of to itself.

        The law above compares the copier's output with the copier's own
        constant, so on its own it can only prove the copier agrees with itself.
        This is the law that fails the day a module joins `relay_core` and the
        candidate would otherwise ship a silently incomplete package.
        """

        shipping = sorted(
            entry.name
            for entry in (SOURCE_DIR / "relay_core").iterdir()
            if entry.is_file() and entry.suffix == ".py"
        )
        self.assertEqual(sorted(_CANDIDATE_PACKAGE_FILES), shipping)

    def test_candidate_carries_the_current_source_bytes(self) -> None:
        """The copy must be of what `source` holds RIGHT NOW.

        The whole mutation battery rests on this: a run that copies restored
        bytes reports green while proving nothing.  Filename and exclusion
        assertions cannot see it — a copier switched to a cached staging
        directory, an import-time read, or `git show HEAD:` would keep every
        other law in this class green while silently making the relocated-runtime
        mutation class unkillable.
        """

        staging = self.stage_source()
        sentinel = b"\n# candidate-freshness-sentinel\n"
        for name in _CANDIDATE_PACKAGE_FILES:
            member = staging / "relay_core" / name
            member.write_bytes(member.read_bytes() + sentinel)
        entry_source = staging / "relay.py"
        entry_source.write_bytes(entry_source.read_bytes() + sentinel)

        destination = self.root / "current-bytes"
        entry = install_candidate_runtime(destination, source=staging)
        package = destination / "src" / "relay_core"
        self.assertEqual(entry_source.read_bytes(), entry.read_bytes())
        for name in _CANDIDATE_PACKAGE_FILES:
            self.assertEqual(
                (staging / "relay_core" / name).read_bytes(),
                (package / name).read_bytes(),
                msg=f"{name} was not copied from the current source bytes",
            )

    def test_temp_cache_and_unrelated_sources_never_reach_the_candidate(self) -> None:
        staging = self.stage_source()
        package_source = staging / "relay_core"
        (package_source / "__pycache__").mkdir()
        (package_source / "__pycache__" / "store.cpython-313.pyc").write_bytes(b"\x00")
        (package_source / "store.py.swp").write_bytes(b"editor swap")
        (package_source / "scratch.tmp").write_bytes(b"temp")
        (package_source / "NOTES.md").write_text("unrelated\n", encoding="utf-8")
        (package_source / "vendor").mkdir()
        (package_source / "vendor" / "extra.py").write_text("x = 1\n", encoding="utf-8")
        (package_source / "linked.py").symlink_to(package_source / "store.py")

        destination = self.root / "from-noisy-source"
        install_candidate_runtime(destination, source=staging)
        self.assertEqual(
            sorted(_CANDIDATE_PACKAGE_FILES), self.installed_names(destination)
        )

    def test_missing_symlinked_or_non_directory_package_root_refuses(self) -> None:
        real_package = self.stage_source() / "relay_core"

        symlinked = self.root / "symlinked-root"
        symlinked.mkdir()
        (symlinked / "relay.py").write_bytes((SOURCE_DIR / "relay.py").read_bytes())
        (symlinked / "relay_core").symlink_to(real_package, target_is_directory=True)

        regular = self.root / "file-root"
        regular.mkdir()
        (regular / "relay.py").write_bytes((SOURCE_DIR / "relay.py").read_bytes())
        (regular / "relay_core").write_text("not a package\n", encoding="utf-8")

        absent = self.root / "absent-root"
        absent.mkdir()
        (absent / "relay.py").write_bytes((SOURCE_DIR / "relay.py").read_bytes())

        for label, source in (
            ("symlink", symlinked),
            ("regular-file", regular),
            ("missing", absent),
        ):
            with self.subTest(root=label):
                destination = self.root / f"dest-root-{label}"
                with self.assertRaises(AssertionError) as caught:
                    install_candidate_runtime(destination, source=source)
                self.assertIn("package root", str(caught.exception))
                self.assertFalse(destination.exists())

    def test_missing_symlinked_or_non_regular_package_file_refuses(self) -> None:
        missing = self.stage_source()
        (missing / "relay_core" / "cli.py").unlink()

        linked = self.stage_source()
        member = linked / "relay_core" / "store.py"
        elsewhere = self.root / "store-elsewhere.py"
        elsewhere.write_bytes(member.read_bytes())
        member.unlink()
        member.symlink_to(elsewhere)

        piped = self.stage_source()
        self.stage_non_regular(piped / "relay_core" / "protocol.py", "fifo")

        foldered = self.stage_source()
        self.stage_non_regular(foldered / "relay_core" / "__init__.py", "directory")

        for label, source in (
            ("missing", missing),
            ("symlink", linked),
            ("fifo", piped),
            ("directory", foldered),
        ):
            with self.subTest(member=label):
                destination = self.root / f"dest-member-{label}"
                with self.assertRaises(AssertionError) as caught:
                    install_candidate_runtime(destination, source=source)
                self.assertIn("package file", str(caught.exception))
                self.assertFalse(destination.exists())

    def test_missing_symlinked_or_non_regular_entrypoint_refuses(self) -> None:
        linked = self.stage_source()
        entry = linked / "relay.py"
        elsewhere = self.root / "entry-elsewhere.py"
        elsewhere.write_bytes(entry.read_bytes())
        entry.unlink()
        entry.symlink_to(elsewhere)

        missing = self.stage_source()
        (missing / "relay.py").unlink()

        foldered = self.stage_source()
        self.stage_non_regular(foldered / "relay.py", "directory")

        for label, source in (
            ("symlink", linked),
            ("missing", missing),
            ("directory", foldered),
        ):
            with self.subTest(entrypoint=label):
                destination = self.root / f"dest-entry-{label}"
                with self.assertRaises(AssertionError) as caught:
                    install_candidate_runtime(destination, source=source)
                self.assertIn("entrypoint", str(caught.exception))
                self.assertFalse(destination.exists())


class DoctorMappingTests(RelayTestCase):
    """Doctor's health verdict and identity fields, through the real dispatch."""

    class _FakeStore:
        def __init__(self, integrity: str, paths) -> None:
            self._integrity = integrity
            self.paths = paths

        def diagnostics(self) -> dict:
            return {"integrity": self._integrity}

    def _doctor(self, integrity: str, *, repo: Path | None = None) -> dict:
        from relay_core import cli as relay_cli

        paths = resolve_paths(repo=repo or self.repo, state_home=self.home)
        namespace = argparse.Namespace(command="doctor")
        return relay_cli._dispatch(self._FakeStore(integrity, paths), namespace)

    def test_healthy_and_unhealthy_map_to_distinct_verdicts(self) -> None:
        self.assertIs(True, self._doctor("ok")["ok"])
        self.assertIs(False, self._doctor("corrupt")["ok"])

    def test_identity_fields_match_independent_git_output(self) -> None:
        report = self._doctor("ok")
        expected_common = Path(
            _git(self.repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
        ).resolve()
        expected_root = Path(
            _git(self.repo, "rev-parse", "--path-format=absolute", "--show-toplevel")
        ).resolve()
        self.assertEqual(str(expected_common), report["git_common_dir"])
        self.assertEqual(str(expected_root), report["repo_root"])
        self.assertNotEqual(report["git_common_dir"], report["repo_root"])

    def test_identity_fields_match_in_a_nested_linked_worktree(self) -> None:
        """Driven through the real doctor dispatch and its RESULT.

        Asserting on a `RelayPaths` value directly would bypass the reporting
        layer, which is exactly where the identity mutation lives.
        """

        linked = self.root / "linked-doctor"
        _git(self.repo, "worktree", "add", "-q", "-b", "doctor-peer", str(linked), "HEAD")
        nested = linked / "deep"
        nested.mkdir()

        report = self._doctor("ok", repo=nested)
        expected_common = Path(
            _git(self.repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
        ).resolve()
        expected_root = Path(
            _git(nested, "rev-parse", "--path-format=absolute", "--show-toplevel")
        ).resolve()
        self.assertEqual(str(expected_common), report["git_common_dir"])
        self.assertEqual(str(expected_root), report["repo_root"])
        self.assertNotEqual(report["git_common_dir"], report["repo_root"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
