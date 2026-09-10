#!/usr/bin/env python3
"""Run development suites in a fresh home with no inherited provider settings."""

import argparse
import json
import os
from pathlib import Path
import platform
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=("all", "core", "runtime"), default="all")
    parser.add_argument("--strict", action="store_true",
                        help="require nonempty complete discovery and zero skips/expected failures")
    parser.add_argument("--isolated-child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not args.isolated_child:
        with tempfile.TemporaryDirectory(prefix="relay-suite-profile-") as temporary:
            profile = Path(temporary)
            for name in ("home", "config", "data", "cache", "tmp", "git-template"):
                (profile / name).mkdir(mode=0o700)
            env = {
                "PATH": "/usr/bin:/bin", "LC_ALL": "C.UTF-8",
                "HOME": str(profile / "home"), "XDG_CONFIG_HOME": str(profile / "config"),
                "XDG_DATA_HOME": str(profile / "data"), "XDG_CACHE_HOME": str(profile / "cache"),
                "TMPDIR": str(profile / "tmp"), "GIT_TEMPLATE_DIR": str(profile / "git-template"),
                "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_CONFIG_SYSTEM": "/dev/null", "GIT_TERMINAL_PROMPT": "0",
                "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.hooksPath",
                "GIT_CONFIG_VALUE_0": "/dev/null", "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONNOUSERSITE": "1",
            }
            return subprocess.run(
                [sys.executable, "-I", "-S", "-B", str(Path(__file__).resolve()),
                 "--isolated-child", "--suite", args.suite, *(["--strict"] if args.strict else [])],
                cwd=ROOT, env=env, check=False,
            ).returncode
    suite = unittest.TestSuite()
    for name in (("core", "runtime") if args.suite == "all" else (args.suite,)):
        suite.addTests(unittest.TestLoader().discover(str(ROOT / "tests" / name), pattern="test_*.py"))
    started = time.monotonic()
    discovered = suite.countTestCases()
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    print(json.dumps({
        "scope": {
            "core": "development coordination-core regression tests; no installed-runtime or provider proof",
            "runtime": "enrollment, installed runtime/recovery/uninstall and no-account workflow; no real-provider proof",
            "all": "development core, installed runtime/recovery/uninstall and no-account workflow; no real-provider proof",
        }[args.suite],
        "suite": args.suite, "tests": result.testsRun, "failures": len(result.failures),
        "errors": len(result.errors), "skipped": len(result.skipped),
        "discovered_tests": discovered, "strict": args.strict,
        "expected_failures": len(result.expectedFailures),
        "unexpected_successes": len(result.unexpectedSuccesses),
        "duration_seconds": round(time.monotonic() - started, 3),
        "python": platform.python_version(), "sqlite": sqlite3.sqlite_version,
        "platform": sys.platform, "isolated_profile": True,
    }, sort_keys=True))
    complete = (discovered > 0 and result.testsRun == discovered
                and not result.skipped and not result.expectedFailures
                and not result.unexpectedSuccesses)
    return 0 if result.wasSuccessful() and (not args.strict or complete) else 1


if __name__ == "__main__":
    raise SystemExit(main())
