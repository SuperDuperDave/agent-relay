"""Independent publication-audit controls; every Git repository is disposable."""

import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("publication_audit_under_test", ROOT / "tools/publication_audit.py")
subject = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = subject
SPEC.loader.exec_module(subject)

MANIFEST = "tools/publication-manifest.json"
SENSITIVE = "sk-proj-" + "A1b2C3d4E5f6G7h8" * 5


class PublicationTests(unittest.TestCase):
    def setUp(self):
        previous = os.umask(0o077)
        self.addCleanup(os.umask, previous)
        temporary = tempfile.TemporaryDirectory(prefix="relay-publication-test-", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        self.home = self.base / "home"
        self.home.mkdir()
        self.env = {
            "PATH": "/usr/bin:/bin", "LC_ALL": "C.UTF-8", "HOME": str(self.home),
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null", "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.hooksPath",
            "GIT_CONFIG_VALUE_0": "/dev/null", "GIT_AUTHOR_NAME": "Fixture",
            "GIT_AUTHOR_EMAIL": "fixture@example.invalid", "GIT_COMMITTER_NAME": "Fixture",
            "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
            "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+0000",
            "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+0000",
        }
        self.git("init", "-q", "--template=")
        self.write("README.md", b"Public fixture.\n")
        self.manifest(["README.md"])
        self.initial = self.commit()

    def git(self, *args, data=None, extra_env=None):
        env = dict(self.env)
        env.update(extra_env or {})
        result = subprocess.run(["/usr/bin/git", "-C", str(self.repo), *args],
                                env=env, input=data, capture_output=True, timeout=15)
        self.assertEqual(0, result.returncode, result.stderr.decode("utf-8", "replace"))
        return result.stdout.decode("utf-8").strip()

    def write(self, path, body):
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)

    def manifest(self, files, exceptions=None):
        self.write(MANIFEST, json.dumps({"schema": 1, "files": files,
                                       "exceptions": exceptions or []}).encode("utf-8"))

    def commit(self, message="Fixture commit\n"):
        self.git("add", "--all")
        self.git("commit", "-q", "--no-gpg-sign", "--allow-empty", "-F", "-", data=message.encode())
        return self.git("rev-parse", "HEAD")

    def audit(self, revision):
        output, errors = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                result = subject.audit(self.repo, revision)
        except subject.AuditError as exc:
            self.assertNotIn(SENSITIVE, str(exc))
            raise
        finally:
            self.assertEqual("", output.getvalue(), "library audit unexpectedly printed output")
            self.assertEqual("", errors.getvalue(), "library audit unexpectedly printed diagnostics")
        self.assertIs(result["historical_blobs_scanned"], False)
        self.assertIs(result["existing_history_export_allowed"], False)
        self.assertIs(result["publication_authorized"], False)
        self.assertEqual(revision, result["revision"])
        self.assertNotIn(SENSITIVE, json.dumps(result, sort_keys=True))
        return result

    def exception(self, path, line):
        return {"path": path, "rule": "credential-token",
                "line_sha256": hashlib.sha256(line).hexdigest(),
                "reason": "synthetic-test-fixture"}

    def test_selected_commit_secret_survives_dirty_clean_worktree_and_manifest(self):
        selected = ("value: " + SENSITIVE + "\n").encode()
        self.write("selected.txt", selected)
        self.manifest(["README.md", "selected.txt"])
        revision = self.commit()
        manifest_body = (self.repo / MANIFEST).read_bytes()
        self.write("selected.txt", b"Clean worktree must not replace committed input.\n")
        self.manifest(["README.md"])
        result = self.audit(revision)
        self.assertFalse(result["snapshot_checks_passed"])
        self.assertTrue(any(f["path"] == "selected.txt" and f["rule"] == "credential-token"
                            for f in result["findings"]))
        self.assertEqual(hashlib.sha256(manifest_body).hexdigest(), result["manifest_sha256"])
        info = result["files"]["selected.txt"]
        self.assertEqual(hashlib.sha256(selected).hexdigest(), info["sha256"])
        self.assertEqual(len(selected), info["size"])
        self.assertEqual(self.git("rev-parse", revision + ":selected.txt"), info["oid"])
        self.assertEqual("100644", info["mode"])

    def test_unselected_committed_and_untracked_private_inputs_are_not_read(self):
        self.write("private.bin", b"\xff\x00" + SENSITIVE.encode())
        revision = self.commit()
        omitted_oid = self.git("rev-parse", revision + ":private.bin")
        self.write("untracked-private.bin", b"\xff\x00" + SENSITIVE.encode())
        protected = {self.repo / "private.bin", self.repo / "untracked-private.bin"}
        original_open, original_run = Path.open, subprocess.run
        def guarded_open(path, *args, **kwargs):
            if path in protected:
                raise AssertionError("auditor read an omitted worktree file")
            return original_open(path, *args, **kwargs)
        def guarded_run(args, *positional, **kwargs):
            arguments = [str(arg) for arg in args]
            data = kwargs.get("input", b"")
            if "cat-file" in arguments and (omitted_oid in arguments or
                    omitted_oid.encode() in (data.encode() if isinstance(data, str) else data or b"")):
                raise AssertionError("auditor read an omitted blob")
            return original_run(args, *positional, **kwargs)
        with mock.patch.object(Path, "open", guarded_open), mock.patch.object(subprocess, "run", guarded_run):
            result = self.audit(revision)
        self.assertTrue(result["snapshot_checks_passed"])
        self.assertEqual({"README.md"}, set(result["files"]))

    def test_each_sensitive_category_is_detected_without_values_in_report(self):
        windows = chr(92).join(("C:", "Users", "fixture-person", "private.txt"))
        cases = [
            ("credential-token", SENSITIVE),
            ("private-key", "-" * 5 + "BEGIN PRIVATE KEY" + "-" * 5),
            ("bearer-token", "Bearer " + "B" * 32),
            ("credential-assignment", 'api_key = "' + "K" * 32 + '"'),
            ("private-receiver-url", "https://" + "claude.ai" + "/code/" + "F" * 24),
            ("personal-home-path", "/home/" + "fixture-person" + "/private.txt"),
            ("personal-home-path", "/Users/" + "fixture-person" + "/private.txt"),
            ("personal-home-path", windows),
            ("personal-home-path", json.dumps(windows)),
            ("email-address", "fixture-private" + "@example.invalid"),
        ]
        self.manifest(["README.md", "selected.txt"])
        for index, (rule, sensitive) in enumerate(cases):
            with self.subTest(rule=rule, case=index):
                self.write("selected.txt", (sensitive + "\n").encode())
                result = self.audit(self.commit())
                self.assertFalse(result["snapshot_checks_passed"])
                self.assertTrue(any(f["path"] == "selected.txt" and f["rule"] == rule
                                    for f in result["findings"]))
                self.assertNotIn(sensitive, json.dumps(result, sort_keys=True))
                self.assertNotIn(json.dumps(sensitive)[1:-1], json.dumps(result, sort_keys=True))

    def test_revision_requires_full_immutable_commit_not_ref_short_or_blob(self):
        blob = self.git("rev-parse", self.initial + ":README.md")
        for revision in ("HEAD", self.initial[:12], "refs/heads/main", blob, "0" * len(self.initial)):
            with self.subTest(revision_kind=revision[:12]), self.assertRaises(subject.AuditError):
                self.audit(revision)
        self.assertTrue(self.audit(self.initial)["snapshot_checks_passed"])

    def test_replacement_ref_cannot_hide_original_and_shallow_history_refuses(self):
        selected = (SENSITIVE + "\n").encode()
        self.write("README.md", selected)
        original = self.commit()
        self.write("README.md", b"Clean replacement.\n")
        replacement = self.commit()
        self.git("replace", original, replacement)
        # Prove the installed fixture replacement really affects ordinary Git.
        self.assertEqual("Clean replacement.", self.git("show", original + ":README.md"))
        result = self.audit(original)
        self.assertFalse(result["snapshot_checks_passed"])
        self.assertTrue(any(f["path"] == "README.md" and f["rule"] == "credential-token"
                            for f in result["findings"]))
        self.assertEqual(hashlib.sha256(selected).hexdigest(), result["files"]["README.md"]["sha256"])
        (self.repo / ".git/shallow").write_text(original + "\n")
        with self.assertRaises(subject.AuditError):
            self.audit(original)

    def test_selected_symlink_and_gitlink_refuse_without_following(self):
        target = self.base / "foreign-canary"
        target.write_bytes(SENSITIVE.encode())
        before = target.stat()
        (self.repo / "selected-link").symlink_to(target)
        self.manifest(["README.md", "selected-link"])
        with self.assertRaises(subject.AuditError):
            self.audit(self.commit())
        self.assertEqual((before.st_ino, before.st_mode, before.st_mtime_ns),
                         (target.stat().st_ino, target.stat().st_mode, target.stat().st_mtime_ns))
        self.assertEqual(SENSITIVE.encode(), target.read_bytes())
        self.git("update-index", "--add", "--cacheinfo", "160000," + self.initial + ",vendor/module")
        self.manifest(["README.md", "vendor/module"])
        self.git("add", MANIFEST)
        self.git("commit", "-q", "--no-gpg-sign", "-F", "-", data=b"Fixture gitlink\n")
        with self.assertRaises(subject.AuditError):
            self.audit(self.git("rev-parse", "HEAD"))

    def test_duplicate_unsafe_and_unknown_manifest_fields_refuse(self):
        variants = [
            b'{"schema":1,"schema":1,"files":["README.md"],"exceptions":[]}',
            json.dumps({"schema": 1, "files": ["README.md", "README.md"], "exceptions": []}).encode(),
            json.dumps({"schema": 1, "files": ["README.md"], "exceptions": [], "permit_all": True}).encode(),
        ]
        for path in ("/README.md", "../README.md", "./README.md", "docs/../README.md", "*.md", "line\nbreak.md"):
            variants.append(json.dumps({"schema": 1, "files": [path], "exceptions": []}).encode())
        for index, raw in enumerate(variants):
            with self.subTest(case=index):
                self.write(MANIFEST, raw)
                with self.assertRaises(subject.AuditError):
                    self.audit(self.commit())

    def test_deep_json_refuses_as_audit_error_and_generic_cli_json(self):
        # Below the byte limit: this must reach the parser's recursion boundary.
        body = b"[" * 20000 + json.dumps(SENSITIVE).encode() + b"]" * 20000
        self.assertLess(len(body), subject.MAX_BLOB)
        self.write(MANIFEST, body)
        revision = self.commit()
        with self.assertRaises(subject.AuditError):
            self.audit(revision)

        result = subprocess.run(
            ["/usr/bin/python3", "-I", "-S", "-B", str(ROOT / "tools/publication_audit.py"),
             "--repo", str(self.repo), "--revision", revision],
            cwd=self.repo, env=self.env, text=True, capture_output=True, timeout=15)
        self.assertEqual(2, result.returncode)
        self.assertEqual("", result.stderr)
        for forbidden in (SENSITIVE, "Traceback", "RecursionError"):
            self.assertNotIn(forbidden, result.stdout)
        self.assertEqual({
            "schema": 1, "snapshot_checks_passed": False,
            "publication_authorized": False,
            "error": "audit refused; incomplete or unsupported input",
        }, json.loads(result.stdout))

    def test_exact_synthetic_exception_does_not_cover_changed_line(self):
        line = ("synthetic: " + SENSITIVE).encode()
        self.write("selected.txt", line + b"\n")
        exception = self.exception("selected.txt", line)
        self.manifest(["README.md", "selected.txt"], [exception])
        accepted = self.audit(self.commit())
        self.assertTrue(accepted["snapshot_checks_passed"])
        self.assertEqual(1, accepted["exceptions_used"])
        with self.subTest(refusal="duplicate exception"):
            self.manifest(["README.md", "selected.txt"], [exception, dict(exception)])
            with self.assertRaises(subject.AuditError):
                self.audit(self.commit())
        with self.subTest(refusal="multiple exact occurrences"):
            self.manifest(["README.md", "selected.txt"], [exception])
            self.write("selected.txt", (line + b"\n") * 2)
            with self.assertRaises(subject.AuditError):
                self.audit(self.commit())
        self.write("selected.txt", line + b" changed\n")
        with self.assertRaises(subject.AuditError):
            self.audit(self.commit())

        # A stale exception also must not approve identical bytes at another path.
        self.write("selected.txt", b"Clean content.\n")
        self.write("other.txt", line + b"\n")
        self.manifest(["README.md", "selected.txt", "other.txt"],
                      [self.exception("selected.txt", line)])
        with self.assertRaises(subject.AuditError):
            self.audit(self.commit())

    def test_merge_second_parent_metadata_detected_without_value_disclosure(self):
        tree = self.git("rev-parse", self.initial + "^{tree}")
        first = self.git("commit-tree", tree, "-p", self.initial, data=b"Ordinary first parent\n")
        second = self.git("commit-tree", tree, "-p", self.initial,
                          data=("Private fixture metadata\n\n" + SENSITIVE + "\n").encode(),
                          extra_env={"GIT_AUTHOR_NAME": SENSITIVE, "GIT_COMMITTER_NAME": SENSITIVE})
        merged = self.git("commit-tree", tree, "-p", first, "-p", second, data=b"Ordinary merge\n")
        result = self.audit(merged)
        self.assertTrue(result["snapshot_checks_passed"])
        self.assertTrue(any(f["rule"] == "credential-token" for f in result["history_findings"]))
        self.assertIn(second, json.dumps(result["history_findings"]))

    def test_deleted_historical_secret_never_authorizes_existing_history(self):
        self.write("README.md", (SENSITIVE + "\n").encode())
        old = self.commit()
        self.write("README.md", b"Current snapshot is clean.\n")
        current = self.commit()
        result = self.audit(current)
        self.assertTrue(result["snapshot_checks_passed"])
        self.assertEqual([], result["findings"])
        self.assertFalse(self.audit(old)["snapshot_checks_passed"])

    def test_missing_link_selected_path_binary_and_oversize_refuse(self):
        self.write("README.md", b"[Required guide](docs/missing.md)\n")
        result = self.audit(self.commit())
        self.assertFalse(result["snapshot_checks_passed"])
        self.assertTrue(any(f["rule"] == "missing-local-link" for f in result["findings"]))
        self.manifest(["absent.txt"])
        with self.assertRaises(subject.AuditError):
            self.audit(self.commit())
        self.manifest(["README.md"])
        for label, body in (("binary", b"\xff\x00fixture"),
                            ("oversize", b"x" * (subject.MAX_BLOB + 1))):
            with self.subTest(kind=label):
                self.write("README.md", body)
                with self.assertRaises(subject.AuditError):
                    self.audit(self.commit())
