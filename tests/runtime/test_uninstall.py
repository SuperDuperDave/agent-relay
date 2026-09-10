"""Installed-code removal only, with all data in disposable fixture roots."""

import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SOURCE = Path(__file__).resolve().parents[2] / "src"
sys.path.insert(0, str(SOURCE))
import relay_bootstrap as subject


def snapshot(root):
    result = {}
    if not root.exists():
        return result
    for path in [root, *sorted(root.rglob("*"))]:
        info = path.lstat()
        body = (os.readlink(path) if stat.S_ISLNK(info.st_mode)
                else hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None)
        result[str(path.relative_to(root))] = (
            info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, body)
    return result


class UninstallTests(unittest.TestCase):
    def setUp(self):
        previous = os.umask(0o077)
        self.addCleanup(os.umask, previous)
        temporary = tempfile.TemporaryDirectory(prefix="relay-uninstall-test-", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.bundle = self.base / "release"
        self.release = subject.build_release(SOURCE, self.bundle, "0.1.0-uninstall")
        self.distribution = subject.Distribution(self.base / "account/installation",
                                                self.base / "account/bin")

    def install(self):
        return self.distribution.install(self.bundle, self.release.digest,
                                         expected_activation=None)["activation"]

    def uninstall(self):
        plan = self.distribution.uninstall_plan()
        self.assertTrue(plan["can_uninstall"], plan)
        result = self.distribution.uninstall(expected_plan=plan["expected_plan"])
        self.assertTrue(result["uninstalled"], result)
        return result

    def cut(self, checkpoint, token):
        program = (
            "import importlib.util, os, sys\n"
            "spec=importlib.util.spec_from_file_location('relay_bootstrap',sys.argv[1])\n"
            "module=importlib.util.module_from_spec(spec)\n"
            "sys.modules[spec.name]=module\n"
            "spec.loader.exec_module(module)\n"
            "def checkpoint(name):\n"
            "    if name==sys.argv[5]: os._exit(89)\n"
            "module._checkpoint=checkpoint\n"
            "module.Distribution(sys.argv[2],sys.argv[3]).uninstall(expected_plan=sys.argv[4])\n")
        child = subprocess.run(
            ["/usr/bin/python3", "-I", "-S", "-B", "-c", program,
             str(SOURCE / "relay_bootstrap.py"), str(self.distribution.root),
             str(self.distribution.bin_directory), token, checkpoint],
            capture_output=True, text=True, timeout=15)
        self.assertEqual(89, child.returncode, child.stdout + child.stderr)
        self.assertEqual("", child.stdout)

    def test_absent_and_exact_plan_are_read_only(self):
        before = snapshot(self.base)
        absent = self.distribution.uninstall_plan()
        self.assertEqual("absent", absent["state"])
        self.assertTrue(absent["uninstalled"])
        self.assertEqual([], absent["targets"])
        self.assertEqual(before, snapshot(self.base))
        self.assertEqual(absent, self.distribution.uninstall(expected_plan=absent["expected_plan"]))
        self.assertEqual(before, snapshot(self.base))
        self.install()
        before = snapshot(self.base)
        first = self.distribution.uninstall_plan()
        self.assertEqual(first, self.distribution.uninstall_plan())
        self.assertEqual("ready", first["state"])
        self.assertEqual([], first["writes"])
        self.assertEqual(21, len(first["targets"]))
        self.assertEqual(before, snapshot(self.base))

    def test_removes_actual_code_and_selectors_preserves_data_then_reinstalls(self):
        first = self.install()
        self.distribution.activate(self.release.digest, expected_activation=first["activation_id"])
        disabled = self.distribution.disable_plan()
        self.distribution.disable(expected_selector=disabled["expected_selector"])
        self.distribution.activate(self.release.digest, expected_activation=None)
        protected = [self.base / "account/enrollments", self.base / "project/.relay",
                     self.base / "project/.git/hooks"]
        for directory in protected:
            directory.mkdir(parents=True)
            for name in ("record.json", "relay.sqlite3", "relay.sqlite3-wal", "relay.sqlite3-shm"):
                (directory / name).write_bytes(b"synthetic protected fixture, not a real ledger\n")
        unrelated = self.distribution.bin_directory / "other-tool"
        unrelated.write_bytes(b"unrelated tool\n")
        before = [snapshot(path) for path in protected] + [unrelated.read_bytes()]
        plan = self.distribution.uninstall_plan()
        targets = [(self.distribution.root if row["area"] == "installation"
                    else self.distribution.bin_directory) / row["path"] for row in plan["targets"]]
        result = self.distribution.uninstall(expected_plan=plan["expected_plan"])
        self.assertTrue(result["uninstalled"], result)
        self.assertFalse(result["artifact_purge_complete"])
        self.assertTrue(all(not p.exists() and not p.is_symlink() for p in targets))
        self.assertEqual(before, [snapshot(path) for path in protected] + [unrelated.read_bytes()])
        names = {p.name for p in self.distribution.root.iterdir()}
        self.assertEqual(3, len(names))
        self.assertIn("activation.lock", names)
        self.assertTrue(any(n.startswith("uninstall-") for n in names))
        self.assertTrue(any(n.startswith("uninstalled-") for n in names))
        self.assertEqual("inactive", self.distribution.inspect()["state"])
        self.assertEqual([], self.distribution.inspect()["issues"])
        self.install()
        self.uninstall()
        self.assertEqual(5, len(list(self.distribution.root.iterdir())))
        self.assertEqual(before, [snapshot(path) for path in protected] + [unrelated.read_bytes()])
        complete = self.distribution.uninstall_plan()
        stable = snapshot(self.base)
        self.assertEqual(complete, self.distribution.uninstall(expected_plan=complete["expected_plan"]))
        self.assertEqual(stable, snapshot(self.base))

    def test_stale_activation_and_changed_code_plans_refuse_without_writes(self):
        first = self.install()
        token = self.distribution.uninstall_plan()["expected_plan"]
        self.distribution.activate(self.release.digest, expected_activation=first["activation_id"])
        before = snapshot(self.base)
        with self.assertRaises(subject.BootstrapError):
            self.distribution.uninstall(expected_plan=token)
        self.assertEqual(before, snapshot(self.base))
        token = self.distribution.uninstall_plan()["expected_plan"]
        code = self.distribution.root / "releases" / self.release.digest / "bootstrap.py"
        code.write_bytes(code.read_bytes() + b"\n# changed bytes\n")
        before = snapshot(self.base)
        self.assertEqual("blocked", self.distribution.uninstall_plan()["state"])
        with self.assertRaises(subject.BootstrapError):
            self.distribution.uninstall(expected_plan=token)
        self.assertEqual(before, snapshot(self.base))

    def test_unknown_files_and_foreign_selector_are_preserved_before_deletion(self):
        self.install()
        for relative in ("operator-note", "releases/operator-note",
                         "releases/" + self.release.digest + "/payload/operator-note"):
            with self.subTest(relative=relative):
                unknown = self.distribution.root / relative
                unknown.write_bytes(b"preserve unrelated bytes")
                before = snapshot(self.base)
                plan = self.distribution.uninstall_plan()
                self.assertEqual("blocked", plan["state"])
                self.assertFalse(plan["can_uninstall"])
                self.assertIsNone(plan["expected_plan"])
                self.assertEqual(before, snapshot(self.base))
                unknown.unlink()  # Test fixture cleanup only.
        command = self.distribution.bin_directory / "relay"
        command.unlink()
        command.write_bytes(b"foreign executable must remain")
        before = snapshot(self.base)
        plan = self.distribution.uninstall_plan()
        self.assertEqual("blocked", plan["state"])
        self.assertEqual(before, snapshot(self.base))

    def test_process_death_cuts_resume_and_block_other_management(self):
        for stage in ("uninstall-receipt-written", "before-uninstall-stage", "uninstall-staged",
                      "before-uninstall-remove", "uninstall-object-removed",
                      "uninstall-before-complete", "uninstall-completed"):
            with self.subTest(stage=stage):
                self.install()
                token = self.distribution.uninstall_plan()["expected_plan"]
                self.cut(stage, token)
                plan = self.distribution.uninstall_plan()
                self.assertTrue(plan["can_uninstall"], plan)
                if stage != "uninstall-completed":
                    self.assertEqual("in_progress", plan["state"])
                    with self.assertRaises(subject.BootstrapError):
                        self.distribution.install(self.bundle, self.release.digest,
                                                  expected_activation=None)
                self.uninstall()
                self.assertFalse((self.distribution.root / "releases").exists())
                self.assertFalse((self.distribution.root / "launches").exists())

    def test_staged_substitution_after_crash_blocks_and_preserves_foreign_object(self):
        self.install()
        self.cut("uninstall-staged", self.distribution.uninstall_plan()["expected_plan"])
        staged = next(self.distribution.bin_directory.glob(".relay-uninstall-*"))
        staged.unlink()
        staged.write_bytes(b"foreign object after interrupted rename")
        before = snapshot(self.base)
        plan = self.distribution.uninstall_plan()
        self.assertEqual("blocked", plan["state"])
        self.assertFalse(plan["can_uninstall"])
        with self.assertRaises(subject.BootstrapError):
            self.distribution.uninstall(expected_plan="0" * 64)
        self.assertEqual(before, snapshot(self.base))

    def test_pre_stage_replacement_is_preserved_and_reports_partial(self):
        self.install()
        plan = self.distribution.uninstall_plan()
        command = self.distribution.bin_directory / "relay"
        observed = {}
        def replace(stage):
            if stage == "before-uninstall-stage" and not observed:
                command.unlink()
                command.write_bytes(b"foreign replacement must not be deleted")
                observed["info"] = command.stat()
        with mock.patch.object(subject, "_checkpoint", side_effect=replace):
            result = self.distribution.uninstall(expected_plan=plan["expected_plan"])
        self.assertEqual("partial", result["state"])
        self.assertFalse(result["uninstalled"])
        staged = list(self.distribution.bin_directory.glob(".relay-uninstall-*"))
        self.assertEqual(1, len(staged))
        self.assertEqual(b"foreign replacement must not be deleted", staged[0].read_bytes())
        info = staged[0].stat()
        self.assertEqual((observed["info"].st_ino, observed["info"].st_mode,
                          observed["info"].st_mtime_ns), (info.st_ino, info.st_mode, info.st_mtime_ns))
        self.assertEqual("blocked", self.distribution.uninstall_plan()["state"])
        self.assertTrue((self.distribution.root / "releases" / self.release.digest / "bootstrap.py").is_file())

    def test_corrupt_receipt_never_authorizes_remaining_deletion(self):
        self.install()
        self.cut("uninstall-receipt-written", self.distribution.uninstall_plan()["expected_plan"])
        receipt = next(self.distribution.root.glob("uninstall-*.json"))
        receipt.write_bytes(receipt.read_bytes() + b" ")
        before = snapshot(self.base)
        plan = self.distribution.uninstall_plan()
        self.assertEqual("blocked", plan["state"])
        with self.assertRaises(subject.BootstrapError):
            self.distribution.uninstall(expected_plan="0" * 64)
        self.assertEqual(before, snapshot(self.base))
