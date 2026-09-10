"""Approved external recovery witnesses using disposable installation roots only.

These are bootstrap API/custody tests, not OS-account or live-ledger proofs.
Crash children run actual staging/publication code and terminate at its checkpoints.
"""

import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_recovery as fixture

subject = fixture.subject
snapshot = fixture.snapshot
SOURCE = fixture.SOURCE
REFUSALS = (subject.BootstrapError, OSError)

_CUT_PROGRAM = """
import importlib.util, os, pathlib, sys
spec = importlib.util.spec_from_file_location('trusted_external_recovery', sys.argv[1])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
manager = module.Distribution(pathlib.Path(sys.argv[2]), pathlib.Path(sys.argv[3]))
def cut(stage):
    if stage == sys.argv[4]:
        os._exit(89)
module._checkpoint = cut
if sys.argv[5] == 'recover':
    manager.recover_install(pathlib.Path(sys.argv[6]), sys.argv[7], expected_selector=sys.argv[8])
else:
    manager.uninstall(expected_plan=sys.argv[6])
raise AssertionError('requested process-cut checkpoint was not reached')
"""


class ExternalRecoveryTests(unittest.TestCase):
    def setUp(self):
        previous = os.umask(0o077)
        self.addCleanup(os.umask, previous)
        temporary = tempfile.TemporaryDirectory(prefix="relay-external-recovery-", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.original_bundle = self.base / "original-bundle"
        self.original = subject.build_release(SOURCE, self.original_bundle, "0.1.0-external-test")
        self.replacement_bundle = self.base / "replacement-bundle"
        self.replacement = subject.build_release(SOURCE, self.replacement_bundle, "0.2.0-external-test")
        self.case = self.make_account("account")

    def make_account(self, label, *, damaged=True):
        account = self.base / label
        manager = subject.Distribution(account / "installation", account / "bin")
        active = manager.install(self.original_bundle, self.original.digest,
                                 expected_activation=None)["activation"]
        registry = account / "enrollments"
        project = account / "project"
        state, hooks = project / ".relay", project / ".git/hooks"
        for directory in (registry, state, hooks):
            directory.mkdir(parents=True)
        for path in (registry / "anchor.json", state / "enrollment.json",
                     state / "relay.sqlite3", state / "relay.sqlite3-wal",
                     state / "relay.sqlite3-shm", hooks / "pre-commit",
                     manager.bin_directory / "unrelated-command"):
            path.write_bytes(b"synthetic protected fixture; never live account state\n")
        installed = manager.root / "releases" / self.original.digest
        marker = account / "suspect-code-executed"
        if damaged:
            (installed / "bootstrap.py").write_text(
                f"from pathlib import Path\nPath({str(marker)!r}).write_text('must not execute')\n")
        observation = manager.inspect()["selector"]["observation"]
        protected = {path: snapshot(path) for path in (
            registry, project, installed, Path(active["target"]).parent,
            manager.bin_directory / "unrelated-command",
            self.original_bundle, self.replacement_bundle)}
        return SimpleNamespace(account=account, manager=manager, active=active,
                               observation=observation, protected=protected, marker=marker)

    def assert_preserved(self, case):
        for path, before in case.protected.items():
            with self.subTest(protected=path.relative_to(self.base)):
                self.assertEqual(before, snapshot(path))
        self.assertFalse(case.marker.exists(), "suspect installed code was executed")

    def assert_refuses_without_writes(self, operation):
        before = snapshot(self.base)
        with self.assertRaises(REFUSALS):
            operation()
        self.assertEqual(before, snapshot(self.base))

    def apply(self, case=None):
        case = case or self.case
        return case.manager.recover_install(self.replacement_bundle, self.replacement.digest,
                                            expected_selector=case.observation)

    def cut(self, case, stage, *, uninstall_token=None):
        arguments = (["uninstall", uninstall_token] if uninstall_token else
                     ["recover", str(self.replacement_bundle), self.replacement.digest, case.observation])
        child = subprocess.run(
            ["/usr/bin/python3", "-I", "-S", "-B", "-c", _CUT_PROGRAM,
             str(SOURCE / "relay_bootstrap.py"), str(case.manager.root),
             str(case.manager.bin_directory), stage, *arguments],
            env={"PATH": "/usr/bin:/bin", "LC_ALL": "C.UTF-8",
                 "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
                 "GIT_CONFIG_SYSTEM": "/dev/null", "GIT_TERMINAL_PROMPT": "0"},
            capture_output=True, text=True, timeout=20)
        self.assertEqual(89, child.returncode, child.stdout + child.stderr)
        self.assertEqual("", child.stdout)
        self.assertEqual("", child.stderr)

    def test_only_installed_release_is_damaged_and_external_plan_is_read_only(self):
        case = self.case
        inspected = case.manager.inspect()
        self.assertEqual("degraded", inspected["state"])
        self.assertIsNone(inspected["activation"])
        self.assertEqual([self.original.digest], [row["release_id"] for row in inspected["releases"]])
        self.assertEqual("unverified", inspected["releases"][0]["state"])
        self.assert_refuses_without_writes(lambda: case.manager.recover(
            self.original.digest, expected_selector=case.observation))
        before = snapshot(self.base)
        plan = case.manager.recovery_plan(self.replacement_bundle, self.replacement.digest)
        self.assertEqual(before, snapshot(self.base))
        self.assertEqual(self.replacement.digest, plan["release_id"])
        self.assertEqual(self.replacement.version, plan["version"])
        self.assertEqual(self.replacement.bootstrap_sha, plan["bootstrap_sha256"])
        self.assertEqual(hashlib.sha256(self.replacement.runtime_manifest).hexdigest(), plan["runtime_sha256"])
        self.assertEqual(case.observation, plan["expected_selector"])
        self.assertFalse(plan["release_already_installed"])
        self.assertEqual(str(case.manager.bin_directory / "relay"), plan["launcher"])
        self.assertEqual([str(case.manager.root), plan["launcher"]], plan["writes"])
        for field in ("repairs_damaged_files", "enrollment_changed", "hooks_changed",
                      "network_access", "stops_running_commands"):
            self.assertIs(False, plan[field])
        self.assert_preserved(case)

    def test_approved_distinct_bundle_recovers_without_repairing_old_bytes(self):
        case = self.case
        result = self.apply()
        self.assertIs(True, result["installed"])
        self.assertIs(True, result["recovered_from_bundle"])
        active = result["activation"]
        self.assertEqual(self.replacement.digest, active["release_id"])
        self.assertNotEqual(case.active["activation_id"], active["activation_id"])
        self.assertEqual(active, case.manager.inspect()["activation"])
        retained = Path(result["retained_selector"])
        self.assertTrue(retained.is_symlink())
        self.assertEqual(case.active["target"], os.readlink(retained))
        installed = case.manager.release(self.replacement.digest)
        self.assertEqual(self.replacement.record, installed.record)
        self.assertEqual(self.replacement.bootstrap, installed.bootstrap)
        self.assertEqual(self.replacement.runtime_manifest, installed.runtime_manifest)
        self.assertEqual(dict(self.replacement.bodies), dict(installed.bodies))
        for field in ("repairs_damaged_files", "enrollment_changed", "hooks_changed",
                      "network_access", "stops_running_commands", "enrolls_projects", "changes_hooks"):
            self.assertIs(False, result[field])
        self.assert_preserved(case)

    def test_bad_approval_and_malformed_external_bundles_refuse_before_writes(self):
        bad = self.base / "malformed-bundle"
        shutil.copytree(self.replacement_bundle, bad)
        (bad / "release.json").write_bytes(b"{}")
        malformed_digest = hashlib.sha256(b"{}").hexdigest()
        extra = self.base / "extra-member-bundle"
        shutil.copytree(self.replacement_bundle, extra)
        (extra / "unreviewed-member").write_bytes(b"unknown fixture bytes")
        tampered = self.base / "tampered-bundle"
        shutil.copytree(self.replacement_bundle, tampered)
        (tampered / "payload/relay_runtime/cli.py").write_bytes(b"raise AssertionError('never execute')\n")
        cases = [(self.replacement_bundle, value) for value in (None, "short", "0" * 64)]
        cases += [(bad, malformed_digest), (extra, self.replacement.digest),
                  (tampered, self.replacement.digest)]
        for bundle, approval in cases:
            with self.subTest(bundle=bundle.name, approval=approval):
                self.assert_refuses_without_writes(lambda: self.case.manager.recovery_plan(bundle, approval))
                self.assert_refuses_without_writes(lambda: self.case.manager.recover_install(
                    bundle, approval, expected_selector=self.case.observation))
        self.assert_preserved(self.case)

    def test_malformed_stale_missing_and_unknown_selector_refuse_without_writes(self):
        for observation in (None, "short", "0" * 64):
            with self.subTest(observation=observation):
                self.assert_refuses_without_writes(lambda: self.case.manager.recover_install(
                    self.replacement_bundle, self.replacement.digest, expected_selector=observation))
        result = self.apply()
        self.assert_refuses_without_writes(lambda: self.apply())
        self.assertEqual(result["activation"], self.case.manager.inspect()["activation"])
        for variant in ("missing", "regular-file", "foreign-symlink"):
            with self.subTest(selector=variant):
                case = self.make_account("selector-" + variant)
                command = case.manager.bin_directory / "relay"
                command.rename(case.account / "saved-selector")
                if variant == "regular-file":
                    command.write_bytes(b"unknown command must not be replaced")
                elif variant == "foreign-symlink":
                    foreign = case.account / "foreign-command"
                    foreign.write_bytes(b"foreign fixture executable")
                    command.symlink_to(foreign)
                self.assert_refuses_without_writes(lambda: case.manager.recovery_plan(
                    self.replacement_bundle, self.replacement.digest))
                self.assert_refuses_without_writes(lambda: self.apply(case))
                self.assert_preserved(case)

    def test_corrupt_same_digest_destination_is_preserved_not_repaired(self):
        case = self.case
        self.assert_refuses_without_writes(lambda: case.manager.recovery_plan(
            self.original_bundle, self.original.digest))
        self.assert_refuses_without_writes(lambda: case.manager.recover_install(
            self.original_bundle, self.original.digest, expected_selector=case.observation))
        self.assert_preserved(case)

    def test_unsafe_containers_and_locks_refuse_before_staging(self):
        for variant in ("symlink-launches", "file-launches", "symlink-lock", "readonly-lock", "missing-root"):
            with self.subTest(variant=variant):
                case = self.make_account("unsafe-" + variant)
                if variant.endswith("launches"):
                    target = case.manager.root / "launches"
                    target.rename(case.account / "retained-launches")
                    if variant == "file-launches":
                        target.write_bytes(b"foreign launches container")
                    else:
                        target.symlink_to(case.account / "retained-launches", target_is_directory=True)
                elif variant == "missing-root":
                    case.manager.root.rename(case.account / "retained-installation")
                else:
                    lock = case.manager.root / "activation.lock"
                    if variant == "readonly-lock":
                        lock.chmod(0o400)
                    else:
                        lock.rename(case.account / "saved-lock")
                        foreign = case.account / "foreign-lock"
                        foreign.write_bytes(b"unrelated lock bytes")
                        lock.symlink_to(foreign)
                self.assert_refuses_without_writes(lambda: case.manager.recovery_plan(
                    self.replacement_bundle, self.replacement.digest))
                self.assert_refuses_without_writes(lambda: self.apply(case))

    def test_pending_uninstall_refuses_external_recovery_without_writes(self):
        case = self.make_account("pending-uninstall", damaged=False)
        token = case.manager.uninstall_plan()["expected_plan"]
        self.cut(case, "uninstall-receipt-written", uninstall_token=token)
        self.assertEqual("in_progress", case.manager.uninstall_plan()["state"])
        self.assert_refuses_without_writes(lambda: case.manager.recovery_plan(
            self.replacement_bundle, self.replacement.digest))
        self.assert_refuses_without_writes(lambda: self.apply(case))
        self.assert_preserved(case)

    def test_crashes_after_verified_staging_retry_with_original_selector(self):
        for stage in ("release-installed", "launcher-prepared", "before-launcher-exchange"):
            with self.subTest(stage=stage):
                case = self.make_account("cut-" + stage)
                self.cut(case, stage)
                self.assertEqual(case.observation, case.manager.inspect()["selector"]["observation"])
                self.assertEqual(self.replacement.digest, case.manager.release(self.replacement.digest).digest)
                staged = case.manager.root / "releases" / self.replacement.digest
                before_staged = snapshot(staged)
                before_plan = snapshot(self.base)
                plan = case.manager.recovery_plan(self.replacement_bundle, self.replacement.digest)
                self.assertIs(True, plan["release_already_installed"])
                self.assertEqual(before_plan, snapshot(self.base))
                result = self.apply(case)
                self.assertEqual(self.replacement.digest, result["activation"]["release_id"])
                self.assertEqual(before_staged, snapshot(staged))
                self.assert_preserved(case)

    def test_partial_reserved_digest_refuses_retry_but_distinct_bundle_can_recover(self):
        case = self.case
        self.cut(case, "release-reserved")
        partial = case.manager.root / "releases" / self.replacement.digest
        self.assertTrue(partial.is_dir())
        self.assertEqual([], list(partial.iterdir()))
        self.assertEqual(case.observation, case.manager.inspect()["selector"]["observation"])
        self.assert_refuses_without_writes(lambda: case.manager.recovery_plan(
            self.replacement_bundle, self.replacement.digest))
        self.assert_refuses_without_writes(lambda: self.apply())
        before_partial = snapshot(partial)
        third_bundle = self.base / "third-bundle"
        third = subject.build_release(SOURCE, third_bundle, "0.3.0-external-test")
        result = case.manager.recover_install(third_bundle, third.digest, expected_selector=case.observation)
        self.assertEqual(third.digest, result["activation"]["release_id"])
        self.assertEqual(before_partial, snapshot(partial))
        self.assert_preserved(case)

    def test_crash_after_publication_is_active_and_stale_retry_refuses(self):
        case = self.case
        self.cut(case, "launcher-published")
        inspected = case.manager.inspect()
        # The new selector is valid; intentionally retained old damage still
        # makes the complete inventory degraded rather than falsely all-clean.
        self.assertEqual("degraded", inspected["state"])
        releases = {row["release_id"]: row["state"] for row in inspected["releases"]}
        self.assertEqual("unverified", releases[self.original.digest])
        self.assertEqual("verified", releases[self.replacement.digest])
        self.assertIn("releases/" + self.original.digest,
                      [issue["scope"] for issue in inspected["issues"]])
        self.assertEqual(self.replacement.digest, inspected["activation"]["release_id"])
        self.assertNotEqual(case.observation, inspected["selector"]["observation"])
        self.assert_refuses_without_writes(lambda: self.apply())
        self.assertEqual(inspected["activation"], case.manager.inspect()["activation"])
        self.assert_preserved(case)

    def test_supplied_bootstrap_is_captured_as_bytes_and_never_executed(self):
        marker = self.base / "external-bootstrap-executed"
        source = self.base / "canary-bootstrap.py"
        body = (f"from pathlib import Path\nPath({str(marker)!r}).write_text('must not execute')\n").encode()
        source.write_bytes(body)
        bundle = self.base / "canary-bundle"
        release = subject.build_release(SOURCE, bundle, "0.4.0-external-canary", bootstrap_path=source)
        before = snapshot(self.base)
        plan = self.case.manager.recovery_plan(bundle, release.digest)
        self.assertEqual(before, snapshot(self.base))
        self.assertEqual(release.digest, plan["release_id"])
        self.assertFalse(marker.exists())
        changed = []
        def change_source_after_capture(stage):
            if stage == "release-reserved":
                (bundle / "bootstrap.py").write_bytes(b"source changed after approved bytes were captured\n")
                changed.append(stage)
        with mock.patch.object(subject, "_checkpoint", side_effect=change_source_after_capture):
            result = self.case.manager.recover_install(
                bundle, release.digest, expected_selector=self.case.observation)
        self.assertEqual(["release-reserved"], changed)
        self.assertTrue(result["installed"])
        self.assertEqual(release.digest, result["activation"]["release_id"])
        installed = self.case.manager.release(release.digest)
        self.assertEqual(body, installed.bootstrap)
        self.assertEqual(release.record, installed.record)
        self.assertEqual(dict(release.bodies), dict(installed.bodies))
        self.assertFalse(marker.exists(), "management executed the supplied bundle bootstrap")
        with self.assertRaises(subject.BootstrapError):
            subject.read_release(bundle, release.digest)
        self.assert_preserved(self.case)

    def test_publication_races_preserve_unknown_command_exactly_once(self):
        for stage in ("launcher-prepared", "before-launcher-exchange", "launcher-published"):
            with self.subTest(stage=stage):
                case = self.make_account("race-" + stage)
                command = case.manager.bin_directory / "relay"
                witness = {}
                def substitute(position):
                    if position != stage:
                        return
                    command.rename(case.account / "saved-selector")
                    command.write_bytes(b"unknown concurrent command must remain recoverable\n")
                    witness["info"] = command.stat()
                with mock.patch.object(subject, "_checkpoint", side_effect=substitute):
                    with self.assertRaises(REFUSALS) as failure:
                        self.apply(case)
                self.assertIn("info", witness)
                before = witness["info"]
                candidates = [path for path in case.manager.bin_directory.iterdir()
                              if not path.is_symlink() and path.is_file()
                              and (path.stat().st_dev, path.stat().st_ino) == (before.st_dev, before.st_ino)]
                self.assertEqual(1, len(candidates))
                preserved = candidates[0]
                after = preserved.stat()
                self.assertEqual((before.st_dev, before.st_ino, before.st_mode, before.st_size,
                                  before.st_mtime_ns, before.st_nlink),
                                 (after.st_dev, after.st_ino, after.st_mode, after.st_size,
                                  after.st_mtime_ns, after.st_nlink))
                self.assertEqual(b"unknown concurrent command must remain recoverable\n", preserved.read_bytes())
                self.assertEqual(1, after.st_nlink)
                if preserved != command:
                    self.assertIn(preserved.name, str(failure.exception))
                self.assertTrue((case.account / "saved-selector").is_symlink())
                self.assert_preserved(case)


if __name__ == "__main__":
    unittest.main()
