"""Recovery/disable witnesses; every installation and protected object is disposable."""

import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


SOURCE = Path(__file__).resolve().parents[2] / "src"
sys.path.insert(0, str(SOURCE))
import relay_bootstrap as subject


def snapshot(root):
    """Identity/content/mode/mtime witness; reads intentionally ignore atime."""
    if not root.exists():
        return None
    result = {}
    for path in [root, *sorted(root.rglob("*"))]:
        info = path.lstat()
        content = (os.readlink(path) if path.is_symlink()
                   else hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None)
        result[str(path.relative_to(root))] = (
            info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, content)
    return result


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        previous = os.umask(0o077)
        self.addCleanup(os.umask, previous)
        temporary = tempfile.TemporaryDirectory(prefix="relay-recovery-test-", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.release_path = self.base / "release"
        self.release = subject.build_release(SOURCE, self.release_path, "0.1.0-recovery-test")
        self.manager = subject.Distribution(self.base / "account/installation",
                                            self.base / "account/bin")

    def install(self):
        return self.manager.install(self.release_path, self.release.digest,
                                    expected_activation=None)["activation"]

    def selector(self):
        return self.manager.inspect()["selector"]

    def test_empty_inspection_and_disable_plan_are_read_only(self):
        before = snapshot(self.base)
        result = self.manager.inspect()
        self.assertEqual("absent", result["state"])
        self.assertIsNone(result["selector"])
        self.assertIsNone(result["activation"])
        self.assertEqual([], result["releases"])
        self.assertEqual([], result["launches"])
        plan = self.manager.disable_plan()
        self.assertFalse(plan["can_disable"])
        self.assertEqual(before, snapshot(self.base))
        self.assertFalse((self.base / "account").exists())

    def test_partial_and_unknown_artifacts_are_inspected_without_execution_or_repair(self):
        release_id, launch_id = "a" * 64, "b" * 32
        partial = self.manager.root / "releases" / release_id
        launch = self.manager.root / "launches" / launch_id
        partial.mkdir(parents=True)
        launch.mkdir(parents=True)
        marker = self.base / "suspect-code-executed"
        poison = f"from pathlib import Path\nPath({str(marker)!r}).write_text('must not execute')\n"
        (partial / "bootstrap.py").write_text(poison)
        (partial / "release.json").write_text("not valid metadata")
        (launch / "relay").write_text(poison)
        (launch / "activation.json").write_text("{}")
        (self.manager.root / "unknown-operator-note").write_bytes(b"preserve unknown artifact")
        before = snapshot(self.base)
        result = self.manager.inspect()
        self.assertEqual("degraded", result["state"])
        self.assertIsNone(result["selector"])
        self.assertIsNone(result["activation"])
        self.assertEqual("unverified", next(x for x in result["releases"]
                                           if x["release_id"] == release_id)["state"])
        self.assertEqual("unverified", next(x for x in result["launches"]
                                           if x["activation_id"] == launch_id)["state"])
        self.assertTrue(result["issues"])
        self.assertFalse(marker.exists())
        self.assertEqual(before, snapshot(self.base))

    def test_damaged_active_release_or_launch_recovers_to_verified_release(self):
        first = self.install()
        other_path = self.base / "second-release"
        other = subject.build_release(SOURCE, other_path, "0.2.0-recovery-test")
        broken = self.manager.install(other_path, other.digest,
                                      expected_activation=first["activation_id"])["activation"]
        marker = self.base / "suspect-code-executed"
        bootstrap = self.manager.root / "releases" / other.digest / "bootstrap.py"
        bootstrap.write_text(f"from pathlib import Path\nPath({str(marker)!r}).write_text('unsafe')\n")
        before_bad = snapshot(bootstrap.parent)
        inspection = self.manager.inspect()
        self.assertEqual("degraded", inspection["state"])
        self.assertIsNone(inspection["activation"])
        self.assertEqual(broken["activation_id"], inspection["selector"]["activation_id"])
        self.assertEqual("verified", next(x for x in inspection["releases"]
                                         if x["release_id"] == self.release.digest)["state"])
        self.assertEqual("unverified", next(x for x in inspection["releases"]
                                           if x["release_id"] == other.digest)["state"])
        recovered = self.manager.recover(
            self.release.digest, expected_selector=inspection["selector"]["observation"])
        self.assertTrue(recovered["installed"])
        self.assertEqual(self.release.digest, recovered["activation"]["release_id"])
        self.assertNotEqual(broken["activation_id"], recovered["activation"]["activation_id"])
        after = self.manager.inspect()
        self.assertEqual(recovered["activation"], after["activation"])
        self.assertEqual(before_bad, snapshot(bootstrap.parent))
        self.assertFalse(marker.exists())

        # A recognized selector remains recoverable when its own launch record,
        # rather than its release, is damaged. Recovery must not repair it.
        launch = Path(recovered["activation"]["target"]).parent
        (launch / "activation.json").write_text("damaged launch metadata")
        before_launch = snapshot(launch)
        damaged = self.manager.inspect()
        self.assertEqual("degraded", damaged["state"])
        self.assertIsNone(damaged["activation"])
        repaired_selection = self.manager.recover(
            self.release.digest, expected_selector=damaged["selector"]["observation"])
        self.assertEqual(self.release.digest, repaired_selection["activation"]["release_id"])
        self.assertNotEqual(recovered["activation"]["activation_id"],
                            repaired_selection["activation"]["activation_id"])
        self.assertEqual(before_launch, snapshot(launch))

    def test_disable_retains_objects_and_reenable_preserves_enrollment_ledgers_and_hooks(self):
        active = self.install()
        registry = self.base / "account/enrollments"
        state = self.base / "project/.relay"
        hooks = self.base / "project/.git/hooks"
        for folder in (registry, state, hooks):
            folder.mkdir(parents=True)
        for path in (registry / "fixture-anchor.json", state / "enrollment.json",
                     state / "relay.sqlite3", state / "relay.sqlite3-wal",
                     state / "relay.sqlite3-shm", hooks / "pre-commit",
                     self.manager.bin_directory / "unrelated-command"):
            path.write_bytes(b"synthetic protected fixture; never a live ledger")
        protected = {p: snapshot(p) for p in (registry, self.base / "project",
                                              self.manager.bin_directory / "unrelated-command")}
        release_before = snapshot(self.manager.root / "releases")
        launches_before = snapshot(self.manager.root / "launches")
        before_plan = snapshot(self.base)
        plan = self.manager.disable_plan()
        self.assertTrue(plan["can_disable"])
        self.assertEqual(self.selector()["observation"], plan["expected_selector"])
        self.assertEqual(before_plan, snapshot(self.base))
        result = self.manager.disable(expected_selector=plan["expected_selector"])
        self.assertTrue(result["disabled"])
        command = self.manager.bin_directory / "relay"
        self.assertFalse(os.path.lexists(command))
        retained = Path(result["retained_selector"])
        self.assertTrue(retained.is_symlink())
        self.assertEqual(active["target"], os.readlink(retained))
        self.assertEqual(release_before, snapshot(self.manager.root / "releases"))
        self.assertEqual(launches_before, snapshot(self.manager.root / "launches"))
        self.assertIsNone(self.manager.inspect()["selector"])
        self.assertFalse(self.manager.disable_plan()["can_disable"])
        enabled = self.manager.activate(self.release.digest, expected_activation=None)
        self.assertTrue(enabled["installed"])
        self.assertNotEqual(active["activation_id"], enabled["activation"]["activation_id"])
        self.assertTrue(retained.is_symlink())
        for path, before in protected.items():
            self.assertEqual(before, snapshot(path))

    def test_stale_and_missing_selector_observations_refuse_without_mutation(self):
        first = self.install()
        stale = self.selector()["observation"]
        self.manager.activate(self.release.digest, expected_activation=first["activation_id"])
        before = snapshot(self.base)
        for method in (
            lambda: self.manager.disable(expected_selector=stale),
            lambda: self.manager.recover(self.release.digest, expected_selector=stale),
        ):
            with self.subTest(operation=method):
                with self.assertRaises(subject.BootstrapError):
                    method()
                self.assertEqual(before, snapshot(self.base))
        current = self.selector()["observation"]
        self.manager.disable(expected_selector=current)
        disabled = snapshot(self.base)
        with self.assertRaises(subject.BootstrapError):
            self.manager.disable(expected_selector=current)
        with self.assertRaises(subject.BootstrapError):
            self.manager.recover(self.release.digest, expected_selector=current)
        self.assertEqual(disabled, snapshot(self.base))

    def test_unknown_existing_command_is_not_disabled_or_recovered_over(self):
        self.install()
        old = self.selector()["observation"]
        command = self.manager.bin_directory / "relay"
        command.rename(self.base / "saved-owned-selector")
        command.write_bytes(b"unrelated operator command")
        before = snapshot(self.base)
        inspection = self.manager.inspect()
        self.assertEqual("degraded", inspection["state"])
        self.assertIsNone(inspection["selector"])
        with self.assertRaises(subject.BootstrapError):
            self.manager.disable_plan()
        for method in (
            lambda: self.manager.disable(expected_selector=old),
            lambda: self.manager.recover(self.release.digest, expected_selector=old),
        ):
            with self.assertRaises(subject.BootstrapError):
                method()
        self.assertEqual(before, snapshot(self.base))

    def test_disable_process_death_has_inspectable_before_and_after_outcomes(self):
        for stage in ("before-launcher-disable", "launcher-disabled"):
            with self.subTest(stage=stage):
                account = self.base / stage
                manager = subject.Distribution(account / "installation", account / "bin")
                active = manager.install(self.release_path, self.release.digest,
                                         expected_activation=None)["activation"]
                observation = manager.inspect()["selector"]["observation"]
                script = f"""
import importlib.util, os, pathlib, sys
spec = importlib.util.spec_from_file_location("trusted_recovery_fixture", {str(SOURCE / 'relay_bootstrap.py')!r})
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
def cut(position):
    if position == {stage!r}:
        os._exit(89)
module._checkpoint = cut
manager = module.Distribution(pathlib.Path({str(manager.root)!r}), pathlib.Path({str(manager.bin_directory)!r}))
manager.disable(expected_selector={observation!r})
raise AssertionError("disable checkpoint was not reached")
"""
                child = subprocess.run(
                    ["/usr/bin/python3", "-I", "-S", "-B", "-c", script],
                    env={"PATH": "/usr/bin:/bin", "LC_ALL": "C.UTF-8"},
                    text=True, capture_output=True, timeout=20)
                self.assertEqual(89, child.returncode, child.stdout + child.stderr)
                self.assertEqual("", child.stdout)
                inspected = manager.inspect()
                if stage == "before-launcher-disable":
                    self.assertEqual(active, inspected["activation"])
                    self.assertEqual(observation, inspected["selector"]["observation"])
                    expected = active["activation_id"]
                else:
                    self.assertIsNone(inspected["selector"])
                    self.assertIsNone(inspected["activation"])
                    retained = list(manager.bin_directory.glob(".relay-disabled-*"))
                    self.assertEqual(1, len(retained))
                    self.assertTrue(retained[0].is_symlink())
                    self.assertEqual(active["target"], os.readlink(retained[0]))
                    expected = None
                self.assertEqual(self.release.digest, manager.release(self.release.digest).digest)
                self.assertTrue(manager.activate(self.release.digest, expected_activation=expected)["installed"])

    def test_disable_readiness_reports_unusable_root_and_locks_without_mutation(self):
        for case in ("symlink-lock", "owner-readonly-lock", "missing-root", "missing-lock"):
            with self.subTest(case=case):
                account = self.base / case
                manager = subject.Distribution(account / "installation", account / "bin")
                manager.install(self.release_path, self.release.digest, expected_activation=None)
                lock = manager.root / "activation.lock"
                if case == "symlink-lock":
                    lock.rename(account / "saved-lock")
                    foreign = account / "foreign-lock-target"
                    foreign.write_bytes(b"foreign lock target must never be opened for writing")
                    lock.symlink_to(foreign)
                elif case == "owner-readonly-lock":
                    lock.chmod(0o400)
                elif case == "missing-root":
                    manager.root.rename(account / "retained-installation")
                else:
                    lock.rename(account / "saved-lock")
                before = snapshot(account)
                inspection = manager.inspect()
                plan = manager.disable_plan()
                if case == "missing-lock":
                    self.assertEqual("active", inspection["state"])
                    self.assertEqual([], inspection["issues"])
                    self.assertTrue(plan["can_disable"])
                    self.assertEqual([], plan["issues"])
                    self.assertFalse(lock.exists(), "read-only readiness created a lock")
                else:
                    self.assertEqual("degraded", inspection["state"])
                    self.assertTrue(inspection["issues"])
                    self.assertFalse(plan["can_disable"])
                    self.assertTrue(plan["issues"])
                self.assertEqual(before, snapshot(account))
                if case == "missing-lock":
                    # A missing lock in safe custody is a normal apply-time
                    # creation, unlike an existing unsafe or unwritable object.
                    result = manager.disable(expected_selector=plan["expected_selector"])
                    self.assertTrue(result["disabled"])
                    self.assertTrue(lock.is_file())
                    self.assertEqual(0o600, lock.stat().st_mode & 0o777)


    def test_before_disable_replacement_is_preserved_with_uncertain_outcome(self):
        self.install()
        observation = self.selector()["observation"]
        command = self.manager.bin_directory / "relay"
        witness = {}
        def substitute(stage):
            if stage != "before-launcher-disable":
                return
            command.rename(self.base / "saved-owned-selector")
            command.write_bytes(b"foreign replacement must stay recoverable")
            witness["info"] = command.stat()
        with mock.patch.object(subject, "_checkpoint", side_effect=substitute):
            with self.assertRaises(subject.BootstrapError) as failure:
                self.manager.disable(expected_selector=observation)
        candidates = list(self.manager.bin_directory.iterdir())
        foreign = [p for p in candidates if not p.is_symlink()
                   and p.is_file() and p.stat().st_ino == witness["info"].st_ino]
        self.assertEqual(1, len(foreign))
        path = foreign[0]
        after = path.stat()
        before = witness["info"]
        self.assertEqual((before.st_dev, before.st_ino, before.st_mode, before.st_mtime_ns),
                         (after.st_dev, after.st_ino, after.st_mode, after.st_mtime_ns))
        self.assertEqual(b"foreign replacement must stay recoverable", path.read_bytes())
        self.assertEqual(1, after.st_nlink)
        if path != command:
            self.assertIn(path.name, str(failure.exception))
        self.assertTrue((self.base / "saved-owned-selector").is_symlink())


if __name__ == "__main__":
    unittest.main()
