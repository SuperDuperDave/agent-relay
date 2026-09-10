"""Actual runner CLI contracts using tiny disposable suites, never this suite."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "tests/run_tests.py"
PYTHON = "/usr/bin/python3"
PASS = """
import unittest
class Passing(unittest.TestCase):
    def test_pass(self):
        self.assertTrue(True)
"""


class SuiteRunnerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="relay-suite-contract-", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.projects = 0
        for name in ("home", "tmp"):
            (self.base / name).mkdir(mode=0o700)
        self.environment = {
            "PATH": "/usr/bin:/bin", "LC_ALL": "C.UTF-8",
            "HOME": str(self.base / "home"), "TMPDIR": str(self.base / "tmp"),
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null", "GIT_TERMINAL_PROMPT": "0",
        }

    def project(self, core, runtime=""):
        self.projects += 1
        root = self.base / ("project-" + str(self.projects))
        for name in ("core", "runtime"):
            (root / "tests" / name).mkdir(parents=True, mode=0o700)
        (root / "tests/run_tests.py").write_bytes(RUNNER.read_bytes())
        for name, content in (("core", core), ("runtime", runtime)):
            (root / "tests" / name / ("test_fixture_" + name + ".py")).write_text(textwrap.dedent(content))
        return root

    def invoke(self, project, *, strict=True, extra_environment=None):
        args = [PYTHON, "-I", "-S", "-B", str(project / "tests/run_tests.py")]
        if strict:
            args.append("--strict")
        result = subprocess.run(args, cwd=project, env={**self.environment, **(extra_environment or {})},
                                text=True, capture_output=True, timeout=15, stdin=subprocess.DEVNULL)
        self.assertLess(len(result.stdout.encode()) + len(result.stderr.encode()), 128 * 1024)
        report = json.loads(result.stdout)
        self.assertIs(report["strict"], strict)
        self.assertIs(report["isolated_profile"], True)
        self.assertEqual("all", report["suite"])
        self.assertRegex(result.stderr, rf"(?m)^Ran {report['tests']} tests? in [0-9.]+s$")
        return result, report

    def counts(self, report, *, discovered, run, failures=0, errors=0, skipped=0,
               expected_failures=0, unexpected_successes=0):
        expected = {"discovered_tests": discovered, "tests": run, "failures": failures,
                    "errors": errors, "skipped": skipped, "expected_failures": expected_failures,
                    "unexpected_successes": unexpected_successes}
        for key, value in expected.items():
            self.assertIs(type(report[key]), int, key)
            self.assertEqual(value, report[key], key)

    def test_strict_pass_executes_all_discovered_core_and_runtime_tests(self):
        project = self.project(PASS, PASS)
        for strict in (False, True):
            with self.subTest(strict=strict):
                result, report = self.invoke(project, strict=strict)
                self.assertEqual(0, result.returncode, result.stderr)
                self.counts(report, discovered=2, run=2)

    def test_skip_refuses_strict_but_preserves_default_success(self):
        project = self.project("""
            import unittest
            class Skipped(unittest.TestCase):
                @unittest.skip("synthetic unsupported fixture")
                def test_skip(self):
                    self.fail("skipped body must not execute")
        """, PASS)
        for strict in (False, True):
            with self.subTest(strict=strict):
                result, report = self.invoke(project, strict=strict)
                self.assertEqual(1 if strict else 0, result.returncode, result.stderr)
                self.counts(report, discovered=2, run=2, skipped=1)

    def test_zero_discovery_refuses_strict_but_preserves_default_success(self):
        project = self.project("", "")
        for strict in (False, True):
            with self.subTest(strict=strict):
                result, report = self.invoke(project, strict=strict)
                self.assertEqual(1 if strict else 0, result.returncode, result.stderr)
                self.counts(report, discovered=0, run=0)

    def test_early_stop_refuses_incomplete_discovery_without_inventing_failure(self):
        project = self.project("""
            import unittest
            class Stopping(unittest.TestCase):
                def run(self, result=None):
                    value = super().run(result)
                    result.stop()
                    return value
                def test_first(self):
                    self.assertTrue(True)
                def test_second(self):
                    self.fail("suite must stop before this discovered test")
        """)
        for strict in (False, True):
            with self.subTest(strict=strict):
                result, report = self.invoke(project, strict=strict)
                self.assertEqual(1 if strict else 0, result.returncode, result.stderr)
                self.counts(report, discovered=2, run=1)

    def test_assertion_failure_is_nonzero_in_both_modes(self):
        project = self.project("""
            import unittest
            class Failure(unittest.TestCase):
                def test_failure(self):
                    self.fail("synthetic assertion failure")
        """)
        for strict in (False, True):
            with self.subTest(strict=strict):
                result, report = self.invoke(project, strict=strict)
                self.assertEqual(1, result.returncode, result.stderr)
                self.counts(report, discovered=1, run=1, failures=1)

    def test_import_error_is_counted_and_nonzero_in_both_modes(self):
        project = self.project('raise RuntimeError("synthetic import failure")\n')
        for strict in (False, True):
            with self.subTest(strict=strict):
                result, report = self.invoke(project, strict=strict)
                self.assertEqual(1, result.returncode, result.stderr)
                self.counts(report, discovered=1, run=1, errors=1)

    def test_expected_failure_refuses_strict_but_preserves_default_success(self):
        project = self.project("""
            import unittest
            class ExpectedFailure(unittest.TestCase):
                @unittest.expectedFailure
                def test_expected(self):
                    self.fail("synthetic expected failure")
        """, PASS)
        for strict in (False, True):
            with self.subTest(strict=strict):
                result, report = self.invoke(project, strict=strict)
                self.assertEqual(1 if strict else 0, result.returncode, result.stderr)
                self.counts(report, discovered=2, run=2, expected_failures=1)

    def test_unexpected_success_is_nonzero_in_both_modes(self):
        project = self.project("""
            import unittest
            class UnexpectedSuccess(unittest.TestCase):
                @unittest.expectedFailure
                def test_unexpected(self):
                    self.assertTrue(True)
        """)
        for strict in (False, True):
            with self.subTest(strict=strict):
                result, report = self.invoke(project, strict=strict)
                self.assertEqual(1, result.returncode, result.stderr)
                self.counts(report, discovered=1, run=1, unexpected_successes=1)

    def test_child_discards_inherited_provider_git_and_import_environment(self):
        forbidden = (
            "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "CODEX_HOME",
            "CLAUDE_CONFIG_DIR", "SSH_AUTH_SOCK", "RELAY_HOME", "GIT_DIR", "GIT_WORK_TREE",
            "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "PYTHONPATH",
        )
        project = self.project("""
            import os
            from pathlib import Path
            import stat
            import sys
            import unittest
            class ChildEnvironment(unittest.TestCase):
                def test_environment(self):
                    self.assertTrue(sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode)
                    self.assertFalse(sys.flags.optimize)
                    for name in FORBIDDEN:
                        self.assertNotIn(name, os.environ)
                    self.assertEqual("1", os.environ["GIT_CONFIG_NOSYSTEM"])
                    self.assertEqual("/dev/null", os.environ["GIT_CONFIG_GLOBAL"])
                    self.assertEqual("/dev/null", os.environ["GIT_CONFIG_SYSTEM"])
                    self.assertEqual("0", os.environ["GIT_TERMINAL_PROMPT"])
                    self.assertEqual("1", os.environ["GIT_CONFIG_COUNT"])
                    self.assertEqual("core.hooksPath", os.environ["GIT_CONFIG_KEY_0"])
                    self.assertEqual("/dev/null", os.environ["GIT_CONFIG_VALUE_0"])
                    self.assertNotEqual(ORIGINAL_HOME, os.environ["HOME"])
                    home = Path(os.environ["HOME"])
                    self.assertTrue(home.is_dir())
                    self.assertEqual(0o700, stat.S_IMODE(home.stat().st_mode))
                    self.assertEqual([], list(home.iterdir()))
                    for variable, leaf in (("TMPDIR", "tmp"), ("XDG_CONFIG_HOME", "config"),
                                           ("XDG_DATA_HOME", "data"), ("XDG_CACHE_HOME", "cache"),
                                           ("GIT_TEMPLATE_DIR", "git-template")):
                        folder = Path(os.environ[variable])
                        self.assertEqual(home.parent / leaf, folder)
                        self.assertTrue(folder.is_dir())
                        self.assertEqual(0o700, stat.S_IMODE(folder.stat().st_mode))
                    self.assertEqual([], list(Path(os.environ["GIT_TEMPLATE_DIR"]).iterdir()))
        """)
        fixture = project / "tests/core/test_fixture_core.py"
        fixture.write_text("FORBIDDEN = " + repr(forbidden) + "\nORIGINAL_HOME = "
                           + repr(self.environment["HOME"]) + "\n" + fixture.read_text())
        poison = {name: "synthetic-not-inherited" for name in forbidden}
        poison.update(GIT_CONFIG_COUNT="37", GIT_CONFIG_KEY_0="fixture.inherited",
                      GIT_CONFIG_VALUE_0="synthetic-not-inherited")
        result, report = self.invoke(project, extra_environment=poison)
        self.assertEqual(0, result.returncode, result.stderr)
        self.counts(report, discovered=1, run=1)


if __name__ == "__main__":
    unittest.main()
