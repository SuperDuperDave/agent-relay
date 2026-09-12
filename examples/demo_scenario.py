"""Internal fixture stages for no_account_demo.py; never run on a real project."""
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import sqlite3
import subprocess
import sys

HOME = Path("/home/relay-demo")
PROJECT = Path("/tmp/project")
REVIEW = Path("/tmp/review")
RELAY = HOME / ".local/bin/multithread"
RESOURCE = "code:demo-progress"
WORK = "progress-clamp"
BASELINE = """def percent(completed, total):
    if total <= 0:
        raise ValueError("total must be positive")
    return 100 * completed / total
"""
FIX = BASELINE.replace("return 100 * completed / total",
                       "return min(100, max(0, 100 * completed / total))")
TESTS = """import unittest
from progress import percent

class ProgressTests(unittest.TestCase):
    def test_within_range(self):
        self.assertEqual(25, percent(1, 4))
    def test_negative_progress(self):
        self.assertEqual(0, percent(-1, 2))
    def test_excess_progress(self):
        self.assertEqual(100, percent(5, 2))
    def test_invalid_total(self):
        with self.assertRaises(ValueError):
            percent(1, 0)
"""
HOOK = b"#!/bin/sh\nexit 93\n"


def require(value, message):
    if not value:
        raise RuntimeError(message)


def command(args, *, cwd=PROJECT, expected=0, payload=None, env=None):
    result = subprocess.run(args, cwd=cwd, text=True, input=payload,
                            capture_output=True, timeout=20, env=env)
    require(result.returncode == expected,
            f"command {args[:4]} exited {result.returncode}, expected {expected}: "
            + (result.stderr or result.stdout)[-2000:])
    return result


def git(*args, repo=PROJECT, payload=None):
    env = dict(os.environ, GIT_AUTHOR_DATE="2000-01-01T00:00:00+00:00",
               GIT_COMMITTER_DATE="2000-01-01T00:00:00+00:00")
    return command(["/usr/bin/git", "-c", "core.hooksPath=/dev/null",
                    "-c", "user.name=Relay Demo", "-c", "user.email=demo@example.invalid",
                    "-c", "commit.gpgsign=false", *args], cwd=repo, payload=payload, env=env).stdout.strip()


def relay(*args, repo=PROJECT):
    result = command([str(RELAY), "--repo", str(repo), "--json", *args], cwd=repo)
    require(not result.stderr, "Multithread emitted unexpected diagnostics")
    return json.loads(result.stdout)


def refusal(*args, repo=REVIEW):
    result = subprocess.run([str(RELAY), "--repo", str(repo), "--json", *args],
                            cwd=repo, text=True, capture_output=True, timeout=20)
    require(result.returncode == 73 and not result.stdout and result.stderr,
            "expected conflict exit 73 without a success receipt")


def pending():
    return relay("channel-pending", "--agent", "claude", "--source-agent", "codex",
                 "--work-id", WORK, "--limit", "1", repo=REVIEW)


def unit_tests(repo, *, expected):
    result = command(["/usr/bin/python3", "-I", "-S", "-B", "-m", "unittest",
                      "discover", "-s", ".", "-p", "test_progress.py"], cwd=repo, expected=expected)
    require("Ran 4 tests" in result.stderr, "fixture did not execute four tests")
    return result


def prepare(release):
    PROJECT.mkdir(mode=0o700)
    git("init", "-q", "-b", "main", "--template=")
    (PROJECT / "progress.py").write_text(BASELINE)
    (PROJECT / "test_progress.py").write_text(TESTS)
    (PROJECT / ".gitignore").write_text(".relay/\n")
    result = unit_tests(PROJECT, expected=1)
    require("FAILED (failures=2)" in result.stderr, "baseline failure was not the expected bug")
    git("add", "progress.py", "test_progress.py", ".gitignore")
    git("commit", "-q", "-F", "-", payload="Demonstrate progress bounds regression\n")
    git("worktree", "add", "-q", "--detach", str(REVIEW), "HEAD")
    hooks = PROJECT / ".git/hooks"
    hooks.mkdir(mode=0o700)
    (hooks / "pre-commit").write_bytes(HOOK)
    (hooks / "pre-commit").chmod(0o700)
    public = ["/usr/bin/python3", "-I", "-S", "-B", "/bootstrap.py"]
    plan = json.loads(command(public + ["plan", "--release", "/bundle",
                                       "--approve-sha256", release]).stdout)
    require(plan["expected_activation"] is None and not list(HOME.iterdir()),
            "read-only plan wrote account state")
    # Fixture-only approval of this checkout. Never automatic approval of a
    # download or permission to install into the user's actual account.
    command(public + ["install", "--release", "/bundle", "--approve-sha256", release,
                      "--expected-activation", "none"])
    require(relay("init")["initialized"], "fixture enrollment failed")
    require(relay("doctor", repo=REVIEW)["ok"], "linked worktree does not share ledger")
    return {"baseline_tests_failed": 2}


def produce():
    relay("claim", RESOURCE, "--agent", "codex", "--session", "demo-author",
          "--purpose", "Fix progress bounds in the fixture")
    (PROJECT / "progress.py").write_text(FIX)
    unit_tests(PROJECT, expected=0)
    git("add", "progress.py")
    git("commit", "-q", "-F", "-", payload="Clamp progress to the inclusive zero to one hundred range\n")
    oid = git("rev-parse", "HEAD")
    args = ("signal", "work.handoff", "--agent", "codex", "--session", "demo-author",
            "--target", "claude", "--work-id", WORK, "--scope", RESOURCE,
            "--summary", "Review the progress bounds fix and its four regression tests",
            "--commit", oid)
    event = relay(*args)["event"]
    retry = relay(*args)
    require(retry["duplicate"] and retry["event"]["seq"] == event["seq"], "exact handoff retry duplicated")
    conflicting = list(args)
    conflicting[conflicting.index("--summary") + 1] = "Changed meaning under the same handoff identity"
    refusal(*conflicting, repo=PROJECT)
    require(pending()["pending"] == {"seq": event["seq"], "kind": "work.handoff"},
            "handoff was not durable before producer exit")
    return {"artifact": oid, "pending_before_stop": True}


def consume():
    # No in-memory state or producer output is supplied here: recover from
    # the ledger alone after the prior process exited before notification.
    first = pending()
    require(first["pending"] is not None and not first["more"], "lost handoff after restart")
    seq = first["pending"]["seq"]
    before = relay("events", repo=REVIEW)
    require(not any(row["kind"] == "delivery.acknowledged" for row in before),
            "handoff was acknowledged before review")
    for _ in range(2):
        require(pending() == first, "a simulated wake consumed or changed the handoff")
    require(relay("events", repo=REVIEW) == before, "pending reads appended events")
    handoff = relay("events", "--after", str(seq - 1), "--limit", "1", repo=REVIEW)[0]
    require((handoff["seq"], handoff["kind"], handoff["agent"], handoff["target"],
             handoff["work_id"], handoff["scope"]) ==
            (seq, "work.handoff", "codex", "claude", WORK, RESOURCE), "unexpected handoff route")
    require(re.fullmatch(r"git:[0-9a-f]{40}", handoff["artifact"]), "not a full immutable Git artifact")
    oid = handoff["artifact"][4:]
    require(git("rev-parse", "--verify", oid + "^{commit}", repo=REVIEW) == oid, "missing artifact")
    claims = [event for event in before if event["kind"] == "claim.acquired"]
    require(len(claims) == 1, "expected the producer's unreleased claim")
    # Session labels are not authentication. This scripted owner release is
    # explicit recovery, never something a received wake authorizes.
    claim = relay("status", repo=REVIEW)["active_claims"][0]
    refusal("claim", RESOURCE, "--agent", "claude", "--session", "demo-reviewer",
            "--purpose", "Competing attempt")
    refusal("release", claim["claim_id"], "--agent", "codex", "--session", "wrong-session")
    relay("release", claim["claim_id"], "--agent", "codex", "--session", "demo-author", repo=REVIEW)
    review_claim = relay("claim", RESOURCE, "--agent", "claude", "--session", "demo-reviewer",
                         "--purpose", "Review the exact fixture artifact", repo=REVIEW)["claim"]
    git("checkout", "-q", "--detach", oid, repo=REVIEW)
    require(git("rev-parse", "HEAD", repo=REVIEW) == oid
            and not git("status", "--porcelain", "--untracked-files=no", repo=REVIEW),
            "review tree is not the clean committed artifact")
    require(git("diff-tree", "--no-commit-id", "--name-only", "-r", oid, repo=REVIEW) == "progress.py",
            "handoff changed more than the intended implementation")
    require((REVIEW / "progress.py").read_text() == FIX, "artifact differs from expected fix")
    unit_tests(REVIEW, expected=0)
    require(pending() == first, "testing or claim recovery implicitly consumed the handoff")
    ack_args = ("acknowledge", str(seq), "--agent", "claude", "--session", "demo-reviewer")
    ack = relay(*ack_args, repo=REVIEW)
    retry_args = (*ack_args[:-1], "demo-reviewer-restarted")
    retry = relay(*retry_args, repo=REVIEW)
    require(retry["duplicate"] and retry["event"]["seq"] == ack["event"]["seq"],
            "later-session ACK retry duplicated")
    require(ack["event"]["scope"] == "signal:" + str(seq)
            and ack["event"]["target"] == "claude"
            and ack["event"]["meta"]["signal_event_id"] == handoff["id"],
            "ACK is not linked to the reviewed handoff")
    relay("release", review_claim["claim_id"], "--agent", "claude", "--session", "demo-reviewer",
          repo=REVIEW)
    require(pending()["pending"] is None, "explicitly consumed handoff still pending")
    events = relay("events", repo=REVIEW)
    kinds = [event["kind"] for event in events]
    require(kinds.count("work.handoff") == kinds.count("delivery.acknowledged") == 1,
            "duplicate handoff or ACK in real ledger")
    require(kinds.count("claim.acquired") == kinds.count("claim.released") == 2,
            "incorrect claim lifecycle")
    require(not relay("status", repo=REVIEW)["active_claims"], "demo left an active claim")
    db = PROJECT / ".relay/relay.sqlite3"
    with sqlite3.connect(db.as_uri() + "?mode=ro", uri=True) as connection:
        connection.execute("PRAGMA query_only=ON")
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        count = connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        linked_ack = connection.execute(
            "SELECT target, scope, meta_json FROM events WHERE kind = ?",
            ("delivery.acknowledged",)).fetchall()
        require(len(linked_ack) == 1 and linked_ack[0][:2] == ("claude", "signal:" + str(seq))
                and json.loads(linked_ack[0][2])["signal_event_id"] == handoff["id"],
                "direct SQLite ACK does not match the reviewed handoff")
    require(integrity == "ok" and count == len(events) == 6, "direct SQLite evidence disagrees")
    require((PROJECT / ".git/hooks/pre-commit").read_bytes() == HOOK, "fixture hook changed")
    require(not (REVIEW / ".relay").exists(), "linked worktree created a separate ledger")
    require(not Path("/tmp/unused-home").exists(), "runtime used ambient HOME")
    return {
        "artifact": oid, "artifact_file_sha256": hashlib.sha256((REVIEW / "progress.py").read_bytes()).hexdigest(),
        "review_tests": {"run": 4, "failures": 0}, "events": count, "sqlite_integrity": integrity,
        "producer_exit_before_notification": 75, "fresh_process_recovery": True,
        "simulated_duplicate_wakes": 2, "pending_reads_appended_events": 0,
        "handoff_events": 1, "ack_events": 1, "competing_claim_refused": True,
        "conflicting_handoff_retry_refused": True, "ack_link_verified_in_sqlite": True,
        "wrong_session_release_refused": True, "explicit_owner_release": True,
        "linked_worktree_shared": True, "fixture_hooks_preserved": True,
    }


def main():
    require(sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode
            and not sys.flags.optimize, "isolated Python required")
    require(os.getuid() == os.geteuid() == 1000
            and pwd.getpwuid(os.getuid()).pw_dir == str(HOME), "not the disposable OS account")
    require(os.readlink("/proc/self/ns/net") != sys.argv[3], "network namespace not isolated")
    interfaces = [row.split(":")[0].strip()
                  for row in Path("/proc/net/dev").read_text().splitlines()[2:]]
    require(interfaces == ["lo"], "unexpected network interface")
    require(all(os.statvfs(p).f_flag & os.ST_RDONLY for p in ("/", "/usr", "/etc", "/proc", "/dev")),
            "demo system mounts are not read-only")
    stage, release = sys.argv[1:3]
    if stage == "prepare":
        result = prepare(release)
    else:
        require(not Path("/bootstrap.py").exists() and not Path("/bundle").exists(),
                "workflow can see source bootstrap or bundle")
        result = produce() if stage == "produce" else consume()
    print(json.dumps(result, sort_keys=True), flush=True)
    if stage == "produce":
        os._exit(75)


if __name__ == "__main__":
    main()
