#!/usr/bin/env python3
"""Run a real installed Relay workflow with scripted actors, without accounts."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SCENARIO = ROOT / "examples/demo_scenario.py"
PYTHON = "/usr/bin/python3"
BWRAP = "/usr/bin/bwrap"


def checked(command, *, env, expected=0, timeout=60):
    result = subprocess.run(command, env=env, text=True, capture_output=True, timeout=timeout)
    if result.returncode != expected:
        raise RuntimeError(
            f"demo subprocess exited {result.returncode} (expected {expected}): "
            + (result.stderr or result.stdout)[-4000:]
        )
    if result.stderr:
        raise RuntimeError("unexpected demo diagnostics: " + result.stderr[-4000:])
    return json.loads(result.stdout)


def run_demo():
    if sys.platform != "linux" or platform.machine() != "x86_64":
        raise RuntimeError("this preview demo requires x86-64 Linux or WSL2")
    if os.getuid() == 0:
        raise RuntimeError("run as an ordinary Linux user, not root")
    if not all(Path(p).is_file() for p in (PYTHON, BWRAP, "/usr/bin/git")):
        raise RuntimeError("requires /usr/bin/python3, /usr/bin/git and /usr/bin/bwrap (bubblewrap)")
    # Only files in this newly allocated disposable tree are ever writable in
    # the sandbox. No real home, provider credentials or enclosing Git is bound.
    old_umask = os.umask(0o077)
    try:
        with tempfile.TemporaryDirectory(prefix="relay-demo-", dir="/tmp") as temporary:
            base = Path(temporary)
            for name in ("home", "tmp", "etc"):
                (base / name).mkdir(mode=0o700)
            (base / "etc/passwd").write_text(
                "relay-demo:x:1000:1000:Disposable demo:/home/relay-demo:/bin/sh\n")
            (base / "etc/group").write_text("relay-demo:x:1000:\n")
            (base / "etc/nsswitch.conf").write_text("passwd: files\ngroup: files\nhosts: files\n")
            env = {"PATH": "/usr/bin:/bin", "LC_ALL": "C.UTF-8",
                   "HOME": str(base / "home"), "GIT_CONFIG_NOSYSTEM": "1",
                   "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
                   "GIT_TERMINAL_PROMPT": "0"}
            release = checked(
                [PYTHON, "-I", "-S", "-B", str(ROOT / "src/relay_bootstrap.py"),
                 "build-release", "--output", str(base / "bundle"), "--version", "0.0.0-demo"],
                env=env)
            if release["approved"]:
                raise RuntimeError("a build digest is not approval")
            release_id = release["release_id"]

            def phase(name, *, source=False, expected=0):
                args = [
                    BWRAP, "--die-with-parent", "--new-session", "--unshare-user",
                    "--uid", "1000", "--gid", "1000", "--unshare-pid", "--unshare-net",
                    "--unshare-ipc", "--unshare-uts", "--hostname", "relay-demo",
                    "--clearenv", "--setenv", "PATH", "/usr/bin:/bin",
                    "--setenv", "LC_ALL", "C.UTF-8", "--setenv", "HOME", "/tmp/unused-home",
                    "--setenv", "GIT_CONFIG_NOSYSTEM", "1",
                    "--setenv", "GIT_CONFIG_GLOBAL", "/dev/null",
                    "--setenv", "GIT_CONFIG_SYSTEM", "/dev/null",
                    "--setenv", "GIT_TERMINAL_PROMPT", "0",
                    "--ro-bind", "/usr", "/usr", "--symlink", "usr/bin", "/bin",
                    "--symlink", "usr/lib", "/lib", "--symlink", "usr/lib64", "/lib64",
                    "--ro-bind", str(base / "etc"), "/etc", "--dir", "/home",
                    "--bind", str(base / "home"), "/home/relay-demo",
                    "--bind", str(base / "tmp"), "/tmp",
                    "--ro-bind", str(SCENARIO), "/scenario.py",
                ]
                if source:
                    args += ["--ro-bind", str(ROOT / "src/relay_bootstrap.py"), "/bootstrap.py",
                             "--ro-bind", str(base / "bundle"), "/bundle"]
                args += [
                    "--proc", "/proc", "--dev", "/dev", "--remount-ro", "/proc",
                    "--remount-ro", "/dev", "--remount-ro", "/", "--chdir", "/tmp",
                    "--", PYTHON, "-I", "-S", "-B", "/scenario.py", name, release_id,
                    os.readlink("/proc/self/ns/net"),
                ]
                return checked(args, env=env, expected=expected)

            installed = phase("prepare", source=True)
            stopped = phase("produce", expected=75)
            resumed = phase("consume")
            if installed["baseline_tests_failed"] != 2 or not stopped["pending_before_stop"]:
                raise RuntimeError("demo did not exercise its advertised starting failure")
            if stopped["artifact"] != resumed["artifact"]:
                raise RuntimeError("review consumed a different artifact")
            return {
                "schema": 1, "result": "pass", "actors": "scripted codex/claude labels",
                "provider_calls": 0, "network": "separate namespace; loopback only",
                "host_installation_used": False, "source_absent_during_workflow": True,
                "release_sha256": release_id,
                "scenario_sha256": hashlib.sha256(SCENARIO.read_bytes()).hexdigest(),
                "baseline_tests": {"run": 4, "failures": installed["baseline_tests_failed"]},
                **resumed,
            }
    finally:
        os.umask(old_umask)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit a stable, machine-readable evidence summary")
    args = parser.parse_args()
    try:
        if not (sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode) or sys.flags.optimize:
            raise RuntimeError("invoke with /usr/bin/python3 -I -S -B examples/no_account_demo.py")
        result = run_demo()
    except (RuntimeError, OSError, subprocess.SubprocessError, ValueError, KeyError) as error:
        print("Relay demo did not pass: " + str(error), file=sys.stderr)
        print("Requires enabled rootless user/mount/network namespaces and the installed Linux runtime profile. "
              "No unisolated fallback is attempted.", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, sort_keys=True, indent=2))
    else:
        print("Relay: durable handoff demo (scripted actors; no provider accounts)")
        print("  1. Installed an offline preview in a disposable Linux account.")
        print("  2. Reproduced two failing progress-indicator tests; committed the fix.")
        print("  3. Stopped the producer after its handoff, before any notification.")
        print("  4. Restarted from the ledger; two simulated wakes left the handoff pending.")
        print("  5. Refused a competing claim and a wrong-session release; no automatic expiry.")
        print("  6. Reviewed the exact commit in a second worktree; four tests passed.")
        print("  7. Explicitly acknowledged once; retries added no duplicate handoff or ACK.")
        print(f"PASS: {result['events']} real ledger events; SQLite integrity {result['sqlite_integrity']}.")
        print("Artifact: git:" + result["artifact"])
        print("Temporary account, worktrees and installation removed. Your Relay setup was not used.")
        print("This demonstrates protocol mechanics, not live Codex/Claude behavior.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
