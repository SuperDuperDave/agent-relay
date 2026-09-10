"""Closed public releases and coherent launcher activation in disposable roots."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
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


class DistributionTests(unittest.TestCase):
    def setUp(self):
        previous = os.umask(0o077)
        self.addCleanup(os.umask, previous)
        temporary = tempfile.TemporaryDirectory(prefix="relay-distribution-test-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.release_path = self.base / "release"
        self.release = subject.build_release(SOURCE, self.release_path, "0.1.0-dev")
        self.distribution = subject.Distribution(self.base / "account" / "installation",
                                                self.base / "account" / "bin")

    def install(self, expected=None):
        return self.distribution.install(self.release_path, self.release.digest,
                                         expected_activation=expected)

    def test_bundle_is_closed_bootstrap_and_runtime_pair(self):
        self.assertEqual({"bootstrap.py", "release.json", "payload"},
                         {path.name for path in self.release_path.iterdir()})
        loaded = subject.read_release(self.release_path, self.release.digest)
        self.assertEqual(self.release, loaded)
        self.assertEqual(set(subject.PAYLOAD_FILES), set(loaded.bodies))
        self.assertEqual(hashlib.sha256(loaded.record).hexdigest(), loaded.digest)

    def test_plan_creates_no_account_state_or_enrollment(self):
        plan = self.distribution.plan(self.release_path, self.release.digest)
        self.assertIsNone(plan["expected_activation"])
        self.assertFalse(plan["enrolls_projects"])
        self.assertFalse(plan["changes_hooks"])
        self.assertFalse((self.base / "account").exists())

    def test_unapproved_or_changed_release_refuses_before_destination_write(self):
        for name in ("bootstrap.py", "payload/relay_core/protocol.py"):
            with self.subTest(name=name):
                path = self.release_path / name
                original = path.read_bytes()
                path.write_bytes(original + b"\n# unapproved change\n")
                with self.assertRaises(subject.BootstrapError):
                    self.install()
                self.assertFalse((self.base / "account").exists())
                path.write_bytes(original)
        with self.assertRaises(subject.BootstrapError):
            self.distribution.install(self.release_path, "0" * 64, expected_activation=None)

    def test_unknown_bundle_members_refuse(self):
        path = self.release_path / "private-transcript"
        path.write_bytes(b"synthetic must-not-copy")
        with self.assertRaises(subject.BootstrapError):
            self.install()
        self.assertFalse((self.base / "account").exists())

    def test_unknown_existing_command_is_preserved_before_installation(self):
        self.distribution.bin_directory.mkdir(parents=True)
        command = self.distribution.bin_directory / "relay"
        command.write_bytes(b"existing unrelated tool")
        before = command.stat()
        with self.assertRaises(subject.BootstrapError):
            self.install()
        self.assertEqual(b"existing unrelated tool", command.read_bytes())
        self.assertEqual(before.st_mtime_ns, command.stat().st_mtime_ns)
        self.assertFalse(self.distribution.root.exists())

    def test_existing_normal_bin_permissions_are_not_repaired(self):
        self.distribution.bin_directory.mkdir(parents=True)
        self.distribution.bin_directory.chmod(0o755)
        self.install()
        self.assertEqual(0o755, self.distribution.bin_directory.stat().st_mode & 0o777)

    def test_public_install_selects_one_coherent_approved_pair(self):
        result = self.install()
        active = result["activation"]
        self.assertEqual(self.release.digest, active["release_id"])
        self.assertEqual(active, self.distribution.status()["activation"])
        wrapper = Path(active["target"])
        self.assertEqual(0o700, wrapper.stat().st_mode & 0o777)
        text = wrapper.read_text()
        self.assertIn("/usr/bin/python3 -I -S -B", text)
        self.assertIn(self.release.bootstrap_sha, text)
        self.assertIn(self.release.digest, text)
        self.assertFalse(list(self.base.rglob("enrollment.json")))
        self.assertFalse(list(self.base.rglob("relay.sqlite3")))

    def test_stale_install_and_rollback_refuse_with_unique_activation_ids(self):
        first = self.install()["activation"]
        second = self.distribution.activate(self.release.digest,
                                            expected_activation=first["activation_id"])["activation"]
        self.assertNotEqual(first["activation_id"], second["activation_id"])
        with self.assertRaises(subject.BootstrapError):
            self.distribution.activate(self.release.digest,
                                       expected_activation=first["activation_id"])
        self.assertEqual(second, self.distribution.status()["activation"])

    def test_new_bootstrap_and_runtime_are_selected_together_and_rollback(self):
        first = self.install()["activation"]
        revised_source = self.base / "revised-source"
        revised_source.mkdir()
        for name in subject.PAYLOAD_FILES:
            target = revised_source / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((SOURCE / name).read_bytes())
        (revised_source / "relay_core/__init__.py").write_bytes(b"# synthetic revised core initializer\n")
        boot = self.base / "revised-bootstrap.py"
        boot.write_bytes((SOURCE / "relay_bootstrap.py").read_bytes() + b"\n# synthetic new bootstrap\n")
        second_path = self.base / "release-two"
        second = subject.build_release(revised_source, second_path, "0.2.0-dev", bootstrap_path=boot)
        new = self.distribution.install(second_path, second.digest,
                                        expected_activation=first["activation_id"])["activation"]
        self.assertIn(second.bootstrap_sha, Path(new["target"]).read_text())
        old = self.distribution.activate(self.release.digest,
                                         expected_activation=new["activation_id"])["activation"]
        self.assertEqual(self.release.digest, old["release_id"])
        self.assertNotEqual(first["activation_id"], old["activation_id"])

    def test_concurrent_installers_have_one_activation_winner(self):
        def attempt(_):
            try:
                return self.install()
            except subject.BootstrapError:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, range(2)))
        self.assertEqual(1, sum(value is not None for value in results))
        self.assertTrue(self.distribution.status()["installed"])

    def test_partial_installed_release_is_not_adopted_or_overwritten(self):
        partial = self.distribution.root / "releases" / self.release.digest
        partial.mkdir(parents=True)
        sentinel = partial / "unknown"
        sentinel.write_bytes(b"preserve")
        with self.assertRaises(subject.BootstrapError):
            self.install()
        self.assertEqual(b"preserve", sentinel.read_bytes())
        self.assertFalse((self.distribution.bin_directory / "relay").exists())

    def test_displaced_unknown_launcher_is_preserved_on_exchange_race(self):
        first = self.install()["activation"]
        command = self.distribution.bin_directory / "relay"
        def replace(stage):
            if stage == "before-launcher-exchange":
                command.unlink()
                command.write_bytes(b"foreign replacement must survive")
        with mock.patch.object(subject, "_checkpoint", side_effect=replace):
            with self.assertRaisesRegex(subject.BootstrapError, "displaced object preserved"):
                self.distribution.activate(self.release.digest,
                                           expected_activation=first["activation_id"])
        displaced = list(self.distribution.bin_directory.glob(".relay-switch-*"))
        self.assertEqual(1, len(displaced))
        self.assertEqual(b"foreign replacement must survive", displaced[0].read_bytes())

    def test_interrupted_preparation_preserves_old_active_pair(self):
        first = self.install()["activation"]
        def stop(stage):
            if stage == "launcher-prepared":
                raise OSError("synthetic preparation interruption")
        with mock.patch.object(subject, "_checkpoint", side_effect=stop):
            with self.assertRaises(OSError):
                self.distribution.activate(self.release.digest,
                                           expected_activation=first["activation_id"])
        self.assertEqual(first, self.distribution.status()["activation"])

    def test_symlinked_payload_and_hardlinked_bootstrap_refuse(self):
        member = self.release_path / "payload/relay_core/protocol.py"
        saved = self.base / "saved.py"
        member.rename(saved)
        member.symlink_to(saved)
        with self.assertRaises(OSError):
            self.install()
        member.unlink()
        saved.rename(member)
        os.link(self.release_path / "bootstrap.py", self.base / "extra-bootstrap-link")
        with self.assertRaises(subject.BootstrapError):
            self.install()

    def test_corrupt_installed_bootstrap_is_not_a_healthy_status(self):
        active = self.install()["activation"]
        boot = self.distribution.root / "releases" / active["release_id"] / "bootstrap.py"
        boot.write_bytes(b"raise AssertionError('must never execute')\n")
        with self.assertRaises(subject.BootstrapError):
            self.distribution.status()

    def test_source_builder_never_overwrites_existing_output(self):
        before = self.release_path.stat()
        with self.assertRaises(FileExistsError):
            subject.build_release(SOURCE, self.release_path, "0.1.0-dev")
        self.assertEqual(before.st_ino, self.release_path.stat().st_ino)

    def test_plan_does_not_mistake_missing_installed_member_for_absence(self):
        partial = self.distribution.root / "releases" / self.release.digest
        partial.mkdir(parents=True)
        for name in ("release.json", "bootstrap.py"):
            (partial / name).write_bytes((self.release_path / name).read_bytes())
        (partial / "payload").mkdir()
        (partial / "payload/relay_core").mkdir()
        (partial / "payload/relay_runtime").mkdir()
        with self.assertRaises(subject.BootstrapError):
            self.distribution.plan(self.release_path, self.release.digest)
        self.assertFalse(self.distribution.bin_directory.exists())

    def test_shell_launcher_quotes_space_and_apostrophe_in_account_path(self):
        distribution = subject.Distribution(self.base / "an account's space" / "installation",
                                            self.base / "an account's space" / "bin")
        result = distribution.install(self.release_path, self.release.digest, expected_activation=None)
        wrapper = result["activation"]["target"]
        result = subprocess.run(["/bin/sh", "-n", wrapper], capture_output=True, text=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_cli_refuses_nonisolated_python_startup(self):
        result = subprocess.run(
            ["/usr/bin/python3", "-B", str(SOURCE / "relay_bootstrap.py"), "status"],
            capture_output=True, text=True, timeout=15,
            env={"PATH": "/usr/bin:/bin"})
        self.assertNotEqual(0, result.returncode)
        self.assertIn("-I -S -B", result.stderr)

    def test_abrupt_process_cuts_leave_inspectable_coherent_activation(self):
        for stage in ("release-installed", "launcher-prepared", "before-launcher-exchange", "launcher-published"):
            with self.subTest(stage=stage):
                account = self.base / stage
                distribution = subject.Distribution(account / "installation", account / "bin")
                first = distribution.install(self.release_path, self.release.digest,
                                             expected_activation=None)["activation"]
                script = f"""
import importlib.util, os, pathlib, sys
spec = importlib.util.spec_from_file_location("trusted_distribution_fixture", {str(SOURCE / 'relay_bootstrap.py')!r})
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
def cut(position):
    if position == {stage!r}:
        os._exit(89)
module._checkpoint = cut
distribution = module.Distribution(pathlib.Path({str(distribution.root)!r}), pathlib.Path({str(distribution.bin_directory)!r}))
distribution.install({str(self.release_path)!r}, {self.release.digest!r}, expected_activation={first['activation_id']!r})
raise AssertionError("cut was not reached")
"""
                result = subprocess.run(["/usr/bin/python3", "-I", "-S", "-B", "-c", script],
                                        text=True, capture_output=True, timeout=20,
                                        env={"PATH": "/usr/bin:/bin", "LC_ALL": "C.UTF-8"})
                self.assertEqual(89, result.returncode, result.stderr)
                self.assertEqual("", result.stdout)
                after = distribution.status()["activation"]
                self.assertEqual(self.release.digest, after["release_id"])
                if stage == "launcher-published":
                    self.assertNotEqual(first["activation_id"], after["activation_id"])
                    self.assertEqual(first["activation_id"], after["previous_id"])
                    with self.assertRaises(subject.BootstrapError):
                        distribution.activate(self.release.digest, expected_activation=first["activation_id"])
                else:
                    self.assertEqual(first, after)
                distribution.activate(self.release.digest, expected_activation=after["activation_id"])

    def test_interruption_before_first_publication_never_activates_partial_release(self):
        account = self.base / "first-cut"
        distribution = subject.Distribution(account / "installation", account / "bin")
        def stop(stage):
            if stage == "launcher-prepared":
                raise OSError("synthetic preparation failure")
        with mock.patch.object(subject, "_checkpoint", side_effect=stop):
            with self.assertRaises(OSError):
                distribution.install(self.release_path, self.release.digest, expected_activation=None)
        self.assertFalse(distribution.status()["installed"])
        self.assertEqual(self.release.digest, distribution.release(self.release.digest).digest)
        distribution.install(self.release_path, self.release.digest, expected_activation=None)
        self.assertTrue(distribution.status()["installed"])

    def test_coherently_rewritten_prepared_pair_refuses_before_publication(self):
        first = self.install()["activation"]
        other_path = self.base / "other-approved-release"
        other = subject.build_release(SOURCE, other_path, "0.2.0-approved-fixture")
        prior = self.distribution.install(other_path, other.digest,
                                          expected_activation=first["activation_id"])["activation"]
        prior_directories = set((self.distribution.root / "launches").iterdir())
        def rewrite(stage):
            if stage != "launcher-prepared":
                return
            pending = (set((self.distribution.root / "launches").iterdir()) - prior_directories).pop()
            record_path = pending / "activation.json"
            value = json.loads(record_path.read_bytes())
            value["release_id"] = other.digest
            record_path.write_bytes(subject._canonical(value))
            (pending / "relay").write_bytes(subject._launcher_body(
                self.distribution.root, other.digest, other.bootstrap_sha, value["activation_id"]))
        with mock.patch.object(subject, "_checkpoint", side_effect=rewrite):
            with self.assertRaises(subject.BootstrapError):
                self.distribution.activate(self.release.digest, expected_activation=prior["activation_id"])
        self.assertEqual(prior, self.distribution.status()["activation"])

    def test_displaced_selectors_are_never_check_then_unlinked(self):
        first = self.install()["activation"]
        original = os.unlink
        def no_selector_delete(path, *args, **kwargs):
            if str(path).startswith(".relay-switch-"):
                raise AssertionError("unsafe selector cleanup attempted")
            return original(path, *args, **kwargs)
        with mock.patch.object(subject.os, "unlink", side_effect=no_selector_delete):
            result = self.distribution.activate(self.release.digest,
                                                 expected_activation=first["activation_id"])
        retained = Path(result["retained_selector"])
        self.assertTrue(retained.is_symlink())
        self.assertEqual(first["target"], os.readlink(retained))
