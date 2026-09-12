"""Offline, immutable-source public packaging in disposable repositories."""

import ast
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
import package_release as subject


class PackageReleaseTests(unittest.TestCase):
    def setUp(self):
        previous = os.umask(0o077)
        self.addCleanup(os.umask, previous)
        temporary = tempfile.TemporaryDirectory(prefix="relay-package-test-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.repository = self.base / "repository"
        self.repository.mkdir()
        self.env = {
            "PATH": "/usr/bin:/bin", "LC_ALL": "C.UTF-8",
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_TERMINAL_PROMPT": "0",
            "GIT_AUTHOR_NAME": "Release fixture", "GIT_AUTHOR_EMAIL": "fixture.invalid",
            "GIT_COMMITTER_NAME": "Release fixture", "GIT_COMMITTER_EMAIL": "fixture.invalid",
            "GIT_AUTHOR_DATE": "2001-01-01T00:00:00+0000",
            "GIT_COMMITTER_DATE": "2001-01-01T00:00:00+0000",
        }
        self.git("init", "--template=", "--initial-branch=main")
        for name in subject.SOURCE_FILES:
            target = self.repository / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((ROOT / name).read_bytes())
        # These committed bytes must never enter a runtime download.
        (self.repository / "unselected.txt").write_text("synthetic unselected committed content\n")
        (self.repository / "README.md").write_text("synthetic repository-only documentation\n")
        self.revision = self.commit()

    def git(self, *arguments):
        result = subprocess.run(
            ["/usr/bin/git", "-c", "core.hooksPath=/dev/null", *arguments],
            cwd=self.repository, env=self.env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20, check=True,
        )
        return result.stdout.decode("utf-8").strip()

    def commit(self):
        self.git("add", "--all")
        self.git("commit", "--no-gpg-sign", "-m", "Synthetic release fixture")
        return self.git("rev-parse", "HEAD")

    def package(self, name="assets", revision=None, version="0.2.0"):
        output = self.base / name
        metadata = subject.package_release(self.repository, revision or self.revision, version, output)
        return output, metadata

    def members(self, output, metadata):
        body = (output / metadata["archive"]).read_bytes()
        with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as archive:
            return {member.name: archive.extractfile(member).read() for member in archive.getmembers()}

    def test_archive_is_closed_normalized_and_independently_checksummed(self):
        output, metadata = self.package()
        archive_body = (output / metadata["archive"]).read_bytes()
        prefix = "relay-0.2.0/"
        expected = {prefix + name for name in (
            "LICENSE", "README.md", "SHA256SUMS", "runtime/bootstrap.py", "runtime/release.json",
        )} | {prefix + "runtime/payload/" + name for name in subject.RUNTIME_FILES}
        self.assertEqual({"schema", "version", "source_commit", "archive", "archive_sha256", "release_id", "files"},
                         set(metadata))
        self.assertEqual(self.revision, metadata["source_commit"])
        self.assertEqual("0.2.0", metadata["version"])
        self.assertEqual(1, metadata["schema"])
        self.assertEqual(expected, set(metadata["files"]))
        self.assertEqual(hashlib.sha256(archive_body).hexdigest(), metadata["archive_sha256"])
        self.assertEqual(978307200, int.from_bytes(archive_body[4:8], "little"))
        self.assertEqual(0, archive_body[3], "gzip must omit local filename and optional metadata")
        with tarfile.open(fileobj=io.BytesIO(archive_body), mode="r:gz") as archive:
            infos = archive.getmembers()
            self.assertEqual(sorted(expected), [info.name for info in infos])
            for info in infos:
                self.assertTrue(info.isfile())
                self.assertEqual((0, 0, "", "", 0o644, 978307200),
                                 (info.uid, info.gid, info.uname, info.gname, info.mode, info.mtime))
                self.assertFalse(info.pax_headers)
        members = self.members(output, metadata)
        for name, body in members.items():
            self.assertEqual(hashlib.sha256(body).hexdigest(), metadata["files"][name])
        checksums = {}
        for line in members[prefix + "SHA256SUMS"].decode().splitlines():
            digest, name = line.split("  ", 1)
            checksums[prefix + name] = digest
        self.assertEqual(expected - {prefix + "SHA256SUMS"}, set(checksums))
        for name, digest in checksums.items():
            self.assertEqual(hashlib.sha256(members[name]).hexdigest(), digest)
        self.assertEqual(hashlib.sha256(members[prefix + "runtime/release.json"]).hexdigest(), metadata["release_id"])
        readme = members[prefix + "README.md"].decode()
        self.assertIn(subject.REPOSITORY_URL + "/tree/" + self.revision, readme)
        for document in ("SETUP.md", "SUPPORT.md"):
            self.assertIn(subject.REPOSITORY_URL + "/blob/" + self.revision + "/docs/" + document, readme)
        self.assertIn("/releases/download/v0.2.0/install.py", readme)
        self.assertIn(metadata["release_id"], readme)
        self.assertNotIn(str(self.base), readme)

    def test_repeated_build_is_byte_identical_despite_dirty_working_files(self):
        first, first_metadata = self.package("first")
        for name in subject.SOURCE_FILES:
            (self.repository / name).write_text("synthetic dirty bytes must never be packaged\n")
        self.git("add", "--all")
        (self.repository / "untracked.txt").write_text("synthetic untracked bytes\n")
        second, second_metadata = self.package("second")
        self.assertEqual(first_metadata, second_metadata)
        self.assertEqual({path.name for path in first.iterdir()}, {path.name for path in second.iterdir()})
        for path in first.iterdir():
            self.assertEqual(path.read_bytes(), (second / path.name).read_bytes(), path.name)
        unpacked = self.members(second, second_metadata)
        self.assertFalse(any(b"synthetic dirty bytes" in body for body in unpacked.values()))
        self.assertFalse(any(b"synthetic unselected committed content" in body for body in unpacked.values()))

    def test_commit_change_binds_source_runtime_archive_and_installer(self):
        first, before = self.package("first")
        changed = self.repository / "src/relay_core/__init__.py"
        changed.write_bytes(changed.read_bytes() + b"\n# synthetic committed source change\n")
        new_revision = self.commit()
        second, after = self.package("second", revision=new_revision)
        self.assertEqual(new_revision, after["source_commit"])
        self.assertNotEqual(before["release_id"], after["release_id"])
        self.assertNotEqual(before["archive_sha256"], after["archive_sha256"])
        member = "relay-0.2.0/runtime/payload/relay_core/__init__.py"
        self.assertNotEqual(self.members(first, before)[member], self.members(second, after)[member])
        self.assertIn(new_revision.encode(), (second / "install.py").read_bytes())
        third, repeated = self.package("original", revision=self.revision)
        self.assertEqual(before, repeated)
        self.assertEqual((first / before["archive"]).read_bytes(), (third / repeated["archive"]).read_bytes())

    def test_installer_embeds_exact_manifest_and_all_assets_have_sidecars(self):
        output, metadata = self.package()
        installer = (output / "install.py").read_bytes()
        self.assertEqual(metadata, json.loads((output / "relay-release.json").read_bytes()))
        parsed = ast.parse(installer)
        assignments = [node for node in parsed.body if isinstance(node, ast.Assign)
                       and any(isinstance(target, ast.Name) and target.id == "PINNED_RELEASE"
                               for target in node.targets)]
        self.assertEqual(1, len(assignments))
        self.assertEqual(metadata, ast.literal_eval(assignments[0].value))
        source = self.git("show", self.revision + ":src/relay_runtime/update.py").encode() + b"\n"
        self.assertEqual(source.replace(subject.INSTALLER_MARKER,
                                        b"PINNED_RELEASE = " + repr(metadata).encode("ascii")), installer)
        names = {metadata["archive"], "relay-release.json", "install.py"}
        self.assertEqual(names | {name + ".sha256" for name in names}, {path.name for path in output.iterdir()})
        for name in names:
            self.assertEqual(hashlib.sha256((output / name).read_bytes()).hexdigest() + "  " + name + "\n",
                             (output / (name + ".sha256")).read_text())

    def test_existing_directory_file_or_link_is_preserved(self):
        for kind in ("directory", "file", "link"):
            with self.subTest(kind=kind):
                output = self.base / kind
                if kind == "directory":
                    output.mkdir()
                    (output / "keep").write_bytes(b"retained fixture")
                elif kind == "file":
                    output.write_bytes(b"retained fixture")
                else:
                    output.symlink_to(self.base / "missing")
                before = output.lstat()
                with self.assertRaisesRegex(subject.PackageError, "new directory"):
                    subject.package_release(self.repository, self.revision, "0.2.0", output)
                self.assertEqual(before, output.lstat())
                if kind == "directory":
                    self.assertEqual(b"retained fixture", (output / "keep").read_bytes())
                elif kind == "file":
                    self.assertEqual(b"retained fixture", output.read_bytes())

    def test_mutable_revision_and_nonrelease_versions_refuse_before_output(self):
        for revision in ("HEAD", "main", self.revision[:12], self.git("rev-parse", "HEAD^{tree}")):
            with self.subTest(revision=revision):
                with self.assertRaises(subject.PackageError):
                    self.package(revision=revision)
                self.assertFalse((self.base / "assets").exists())
        for version in ("v0.2.0", "0.2.0-candidate", "01.2.0", "0.2", "0.2.0/elsewhere", "0.2.0\n"):
            with self.subTest(version=version):
                with self.assertRaises(subject.PackageError):
                    self.package(version=version)
                self.assertFalse((self.base / "assets").exists())

    def test_missing_or_symbolic_source_refuses_before_output(self):
        path = self.repository / "src/relay_core/protocol.py"
        path.unlink()
        missing = self.commit()
        with self.assertRaisesRegex(subject.PackageError, "missing required"):
            self.package(revision=missing)
        self.assertFalse((self.base / "assets").exists())
        path.symlink_to("store.py")
        linked = self.commit()
        with self.assertRaisesRegex(subject.PackageError, "ineligible Git member"):
            self.package(revision=linked)
        self.assertFalse((self.base / "assets").exists())

    def test_oversized_source_refuses_before_output(self):
        path = self.repository / "src/relay_core/protocol.py"
        path.write_bytes(b"#" * (subject.MAX_MEMBER + 1))
        revision = self.commit()
        with self.assertRaisesRegex(subject.PackageError, "exceeds the release bound"):
            self.package(revision=revision)
        self.assertFalse((self.base / "assets").exists())

    def test_cli_reports_release_metadata_without_local_build_paths(self):
        output = self.base / "cli-assets"
        result = subprocess.run(
            ["/usr/bin/python3", "-I", "-S", "-B", str(ROOT / "tools/package_release.py"),
             "--revision", self.revision, "--version", "0.2.0", "--output", str(output)],
            cwd=self.repository, env=self.env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr.decode())
        self.assertEqual(b"", result.stderr)
        metadata = json.loads(result.stdout)
        self.assertEqual(metadata, json.loads((output / "relay-release.json").read_bytes()))
        self.assertNotIn(str(self.base).encode(), result.stdout)

    def test_missing_or_ambiguous_installer_marker_refuses_before_output(self):
        path = self.repository / "src/relay_runtime/update.py"
        original = path.read_bytes()
        for body in (original.replace(subject.INSTALLER_MARKER, b"PINNED_RELEASE = {}"),
                     original + b"\n# " + subject.INSTALLER_MARKER + b"\n"):
            path.write_bytes(body)
            revision = self.commit()
            with self.assertRaisesRegex(subject.PackageError, "exactly one"):
                self.package(revision=revision)
            self.assertFalse((self.base / "assets").exists())


if __name__ == "__main__":
    unittest.main()
