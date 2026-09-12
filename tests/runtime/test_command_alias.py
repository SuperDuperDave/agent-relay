"""The preferred command follows one verified selector in disposable installs."""

import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
import test_distribution as fixture
from test_uninstall import snapshot

subject = fixture.subject


class CommandAliasTests(unittest.TestCase):
    def setUp(self):
        fixture.DistributionTests.setUp(self)
        self.preferred = self.distribution.bin_directory / "multithread"
        self.compatibility = self.distribution.bin_directory / "relay"

    def install(self, expected=None):
        return self.distribution.install(self.release_path, self.release.digest,
                                         expected_activation=expected)["activation"]

    def assert_same_runtime(self, activation):
        self.assertEqual(str(self.compatibility), os.readlink(self.preferred))
        self.assertEqual(activation["target"], os.readlink(self.compatibility))
        self.assertTrue(self.preferred.samefile(self.compatibility))
        self.assertEqual(Path(activation["target"]), self.preferred.resolve())
        status = self.distribution.status()
        self.assertTrue(status["installed"])
        self.assertTrue(status["preferred_command_available"])
        self.assertEqual(str(self.preferred), status["launcher"])
        self.assertEqual(str(self.compatibility), status["compatibility_launcher"])
        self.assertEqual(activation, status["activation"])

    def test_fresh_install_has_two_names_and_one_verified_selector(self):
        active = self.install()
        self.assert_same_runtime(active)
        info = self.preferred.lstat()
        self.assertEqual(os.getuid(), info.st_uid)
        self.assertEqual(1, info.st_nlink)
        self.assertEqual({"multithread", "relay"},
                         {entry.name for entry in self.distribution.bin_directory.iterdir()})
        self.assertEqual([], self.distribution.inspect()["issues"])
        self.assertFalse(list(self.base.rglob("enrollment.json")))
        self.assertFalse(list(self.base.rglob("relay.sqlite3")))

    def test_update_and_rollback_keep_the_same_alias_and_follow_the_single_selector(self):
        first = self.install()
        alias = self.preferred.lstat()
        other_path = self.base / "release-two"
        other = subject.build_release(fixture.SOURCE, other_path, "0.2.0-alias-fixture")
        second = self.distribution.install(other_path, other.digest,
                                            expected_activation=first["activation_id"])["activation"]
        self.assertEqual(other.digest, second["release_id"])
        self.assert_same_runtime(second)
        self.assertEqual((alias.st_dev, alias.st_ino),
                         (self.preferred.lstat().st_dev, self.preferred.lstat().st_ino))
        self.assertNotEqual(first["target"], second["target"])
        rolled_back = self.distribution.activate(self.release.digest,
                                                 expected_activation=second["activation_id"])["activation"]
        self.assert_same_runtime(rolled_back)
        self.assertEqual(self.release.digest, rolled_back["release_id"])
        self.assertNotEqual(first["activation_id"], rolled_back["activation_id"])
        self.assertEqual((alias.st_dev, alias.st_ino),
                         (self.preferred.lstat().st_dev, self.preferred.lstat().st_ino))

    def test_foreign_preferred_entries_refuse_before_staging_or_changing_any_object(self):
        for kind in ("file", "directory", "foreign-symlink", "relative-symlink"):
            with self.subTest(kind=kind):
                account = self.base / kind
                distribution = subject.Distribution(account / "installation", account / "bin")
                distribution.bin_directory.mkdir(parents=True)
                command = distribution.bin_directory / "multithread"
                if kind == "file":
                    command.write_bytes(b"artificial unrelated command\n")
                elif kind == "directory":
                    command.mkdir()
                    (command / "keep").write_bytes(b"artificial unrelated content\n")
                else:
                    command.symlink_to("relay" if kind == "relative-symlink"
                                       else self.base / "unrelated-target")
                before = snapshot(account)
                with mock.patch.object(distribution, "_stage_release") as stage:
                    with self.assertRaises(subject.BootstrapError):
                        distribution.install(self.release_path, self.release.digest,
                                             expected_activation=None)
                stage.assert_not_called()
                self.assertEqual(before, snapshot(account))
                self.assertFalse(distribution.root.exists())

    def test_missing_alias_is_reported_and_reviewed_reinstall_restores_it(self):
        first = self.install()
        self.preferred.unlink()  # Disposable fixture damage only.
        before = snapshot(self.base)
        status = self.distribution.status()
        self.assertTrue(status["installed"])
        self.assertFalse(status["preferred_command_available"])
        self.assertEqual(first, status["activation"])
        inspected = self.distribution.inspect()
        self.assertEqual("degraded", inspected["state"])
        self.assertTrue(any(issue["scope"] == "command-alias" for issue in inspected["issues"]))
        self.assertEqual(before, snapshot(self.base))
        restored = self.install(expected=first["activation_id"])
        self.assert_same_runtime(restored)
        self.assertEqual([], self.distribution.inspect()["issues"])

    def test_tampered_alias_blocks_management_without_touching_either_target(self):
        active = self.install()
        foreign = self.base / "foreign-tool"
        foreign.write_bytes(b"artificial foreign program\n")
        self.preferred.unlink()
        self.preferred.symlink_to(foreign)
        before = snapshot(self.base)
        with self.assertRaises(subject.BootstrapError):
            self.distribution.status()
        with self.assertRaises(subject.BootstrapError):
            self.install(expected=active["activation_id"])
        with self.assertRaises(subject.BootstrapError):
            self.distribution.activate(self.release.digest, expected_activation=active["activation_id"])
        inspected = self.distribution.inspect()
        self.assertEqual("degraded", inspected["state"])
        self.assertEqual(active, inspected["activation"])
        self.assertFalse(self.distribution.uninstall_plan()["can_uninstall"])
        self.assertEqual(before, snapshot(self.base))

    def test_interrupted_alias_publication_is_inactive_and_can_be_deliberately_resumed(self):
        def cut(stage):
            if stage == "command-alias-published":
                raise OSError("artificial publication interruption")
        with mock.patch.object(subject, "_checkpoint", side_effect=cut):
            with self.assertRaises(OSError):
                self.install()
        self.assertTrue(self.preferred.is_symlink())
        alias = self.preferred.lstat()
        self.assertFalse(self.compatibility.is_symlink())
        status = self.distribution.status()
        self.assertFalse(status["installed"])
        self.assertFalse(status["preferred_command_available"])
        self.assert_same_runtime(self.install())
        self.assertEqual((alias.st_dev, alias.st_ino),
                         (self.preferred.lstat().st_dev, self.preferred.lstat().st_ino))

    def test_uninstall_removes_both_names_and_preserves_records_and_unrelated_state(self):
        self.install()
        sentinel = self.base / "account" / "retained-state"
        sentinel.write_bytes(b"artificial coordination record outside installed code\n")
        sentinel_before = snapshot(sentinel.parent)["retained-state"]
        plan = self.distribution.uninstall_plan()
        self.assertTrue(plan["can_uninstall"], plan)
        self.assertEqual({"relay", "multithread"},
                         {row["path"] for row in plan["targets"] if row["area"] == "bin"})
        result = self.distribution.uninstall(expected_plan=plan["expected_plan"])
        self.assertTrue(result["uninstalled"], result)
        self.assertFalse(self.preferred.is_symlink())
        self.assertFalse(self.compatibility.is_symlink())
        self.assertFalse((self.distribution.root / "releases").exists())
        self.assertEqual(sentinel_before, snapshot(sentinel.parent)["retained-state"])
        self.assertEqual(1, len(list(self.distribution.root.glob("uninstall-*.json"))))
        self.assertEqual(1, len(list(self.distribution.root.glob("uninstalled-*.json"))))
        self.assertEqual([], self.distribution.inspect()["issues"])
        self.assert_same_runtime(self.install())

    def test_interrupted_alias_removal_resumes_from_the_exact_retained_receipt(self):
        self.install()
        plan = self.distribution.uninstall_plan()
        alias_index = next(index for index, row in enumerate(plan["targets"])
                           if row["area"] == "bin" and row["path"] == "multithread")
        script = """
import importlib.util, os, sys
spec = importlib.util.spec_from_file_location('alias_uninstall_fixture', sys.argv[1])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
distribution = module.Distribution(sys.argv[2], sys.argv[3])
def cut(stage):
    if stage != 'uninstall-staged':
        return
    records = list(distribution.root.glob('uninstall-*.json'))
    if not records:
        return
    operation = records[0].name[len('uninstall-'):-len('.json')]
    name = module._uninstall_stage(operation, int(sys.argv[5]), 'multithread')
    if (distribution.bin_directory / name).is_symlink():
        os._exit(89)
module._checkpoint = cut
distribution.uninstall(expected_plan=sys.argv[4])
raise AssertionError('alias removal checkpoint was not reached')
"""
        child = subprocess.run(
            ["/usr/bin/python3", "-I", "-S", "-B", "-c", script,
             str(fixture.SOURCE / "relay_bootstrap.py"), str(self.distribution.root),
             str(self.distribution.bin_directory), plan["expected_plan"], str(alias_index)],
            capture_output=True, text=True, timeout=20,
            env={"PATH": "/usr/bin:/bin", "LC_ALL": "C.UTF-8"},
        )
        self.assertEqual(89, child.returncode, child.stdout + child.stderr)
        pending = self.distribution.uninstall_plan()
        self.assertEqual("in_progress", pending["state"])
        self.assertTrue(pending["can_uninstall"], pending)
        self.assertIsNotNone(pending["pending_receipt"])
        before = snapshot(self.base)
        with self.assertRaises(subject.BootstrapError):
            self.install()
        self.assertEqual(before, snapshot(self.base))
        result = self.distribution.uninstall(expected_plan=pending["expected_plan"])
        self.assertTrue(result["uninstalled"], result)
        self.assertFalse(self.preferred.is_symlink())
        self.assertFalse(self.compatibility.is_symlink())
        self.assertEqual([], list(self.distribution.bin_directory.glob(".relay-uninstall-*")))
        self.assertEqual(1, len(list(self.distribution.root.glob("uninstall-*.json"))))
        self.assertEqual(1, len(list(self.distribution.root.glob("uninstalled-*.json"))))


if __name__ == "__main__":
    unittest.main()
