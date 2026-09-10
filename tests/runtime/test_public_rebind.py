"""Public same-filesystem relocation with actual ledger, claim and pending handoff."""
import json
import subprocess
import unittest

import test_public_profile as profile


_MOVE = profile._COMMON + r"""
assert not pathlib.Path("/source").exists() and not pathlib.Path("/bundle").exists()
mode = "MAIN_OR_PARENT"
parent = project / "relocation-parent"
parent.mkdir()
repo = parent / "workspace"
subprocess.run(["/usr/bin/git", "init", "-q", "--template=", str(repo)],
               check=True, capture_output=True)
(repo / "fixture.txt").write_text("immutable relocation fixture\n")
(repo / ".git/hooks").mkdir()
(repo / ".git/hooks/pre-commit").write_text("# synthetic hook; preserve\n")
subprocess.run(["/usr/bin/git", "-C", str(repo), "add", "fixture.txt"],
               check=True, capture_output=True)
subprocess.run(["/usr/bin/git", "-C", str(repo), "-c", "user.name=Fixture",
                "-c", "user.email=fixture@example.invalid", "-c", "core.hooksPath=/dev/null",
                "-c", "commit.gpgsign=false", "commit", "-q", "-F", "-"],
               input="Immutable relocation fixture\n", text=True, check=True, capture_output=True)
oid = subprocess.check_output(["/usr/bin/git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
def relay(*arguments, target=None, success=True):
    result = subprocess.run([str(launcher), "--repo", str(target or repo), "--json", *arguments],
                            cwd=project, text=True, capture_output=True, timeout=20)
    if success:
        assert result.returncode == 0 and not result.stderr, (arguments, result.stdout, result.stderr)
        return json.loads(result.stdout)
    assert result.returncode != 0 and result.stdout == "", (arguments, result)
    return result.returncode
def pending():
    return relay("channel-pending", "--agent", "claude", "--source-agent", "codex",
                 "--work-id", "rebind-proof", "--limit", "1")

# Independently observe actual inode/device and statx birth evidence, not just bytes.
import ctypes, struct
def object_tree(root):
    values = {}
    for path in [root, *sorted(root.rglob("*"))]:
        info = path.lstat()
        content = (os.readlink(path) if path.is_symlink() else
                   hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None)
        values[str(path.relative_to(root))] = [
            info.st_dev, info.st_ino, info.st_mode, info.st_nlink, info.st_mtime_ns, content]
    return values
def ledger_births(root):
    result = {}
    libc = ctypes.CDLL(None, use_errno=True)
    statx = libc.statx
    statx.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_uint, ctypes.c_void_p]
    statx.restype = ctypes.c_int
    for name in ("relay.sqlite3", "relay.sqlite3-wal", "relay.sqlite3-shm"):
        fd = os.open(root / ".relay" / name, os.O_PATH | os.O_NOFOLLOW)
        try:
            buffer = ctypes.create_string_buffer(256)
            assert statx(fd, b"", 0x1000 | 0x100, 0x800, ctypes.byref(buffer)) == 0
            assert struct.unpack_from("=I", buffer.raw, 0)[0] & 0x800
            result[name] = list(struct.unpack_from("=qI", buffer.raw, 0x50))
        finally:
            os.close(fd)
    return result

initialized = relay("init")
claim = relay("claim", "code:rebind-fixture", "--agent", "codex", "--session", "rebind-author",
              "--purpose", "Keep ownership through relocation")["claim"]
handoff = relay("signal", "work.handoff", "--agent", "codex", "--session", "rebind-author",
                "--target", "claude", "--work-id", "rebind-proof", "--scope", "code:rebind-fixture",
                "--summary", "Review the immutable relocation fixture", "--commit", oid)["event"]
events = relay("events")
claims = relay("status")["active_claims"]
waiting = pending()
assert len(events) == 2 and len(claims) == 1
assert waiting["pending"] == {"seq": handoff["seq"], "kind": "work.handoff"}
registry = home / ".local/share/relay/enrollments"
anchor = registry / ("ledger-" + initialized["enrollment_id"] + ".json")
anchor_before = [anchor.stat().st_ino, anchor.read_bytes(), anchor.stat().st_mtime_ns]
before_project = object_tree(repo)
before_births = ledger_births(repo)
before_installation = object_tree(installation)
before_foreign = object_tree(foreign)
before_bin = object_tree(home / ".local/bin")
old = repo
if mode == "main":
    repo = parent / "moved-workspace"
    old.rename(repo)
else:
    new_parent = project / "moved-parent"
    parent.rename(new_parent)
    repo = new_parent / old.name
assert not old.exists() and object_tree(repo) == before_project
assert ledger_births(repo) == before_births
before_account = object_tree(home)
relay("status", success=False)
relay("init", success=False)
assert object_tree(home) == before_account and object_tree(repo) == before_project
plan = relay("rebind-plan", "--from-repo", str(old))
assert plan["generation"] == 1 and plan["next_generation"] == 2
assert plan["enrollment_id"] == initialized["enrollment_id"] and plan["requires_quiescence"]
assert object_tree(home) == before_account and object_tree(repo) == before_project
# Explicit acknowledgement is required even after a successful plan.
relay("rebind", "--from-repo", str(old), "--expected-binding", plan["expected_binding"], success=False)
assert object_tree(home) == before_account and object_tree(repo) == before_project
applied = relay("rebind", "--from-repo", str(old), "--expected-binding", plan["expected_binding"],
                "--confirm-quiescent")
assert applied["rebound"] and applied["generation"] == 2
assert applied["enrollment_id"] == initialized["enrollment_id"]
# Before any SQLite reopen, preserve exact project objects, bytes and birth witnesses.
assert object_tree(repo) == before_project and ledger_births(repo) == before_births
assert [anchor.stat().st_ino, anchor.read_bytes(), anchor.stat().st_mtime_ns] == anchor_before
assert object_tree(installation) == before_installation
assert object_tree(foreign) == before_foreign and object_tree(home / ".local/bin") == before_bin
assert pathlib.Path(applied["retained_binding"]).is_file()
before_retry = object_tree(home)
relay("rebind", "--from-repo", str(old), "--expected-binding", plan["expected_binding"],
      "--confirm-quiescent", success=False)
assert object_tree(home) == before_retry and object_tree(repo) == before_project

# Each call is a fresh installed process with neither checkout nor bundle available.
assert relay("init") == initialized
assert relay("events") == events and relay("status")["active_claims"] == claims
assert pending() == waiting and relay("doctor")["ok"]
assert relay("claim", "code:rebind-fixture", "--agent", "claude", "--session", "contender",
             "--purpose", "Must remain excluded", success=False) == 73
assert relay("release", claim["claim_id"], "--agent", "codex", "--session", "wrong",
             success=False) == 73
follow_on = relay("signal", "work.intent", "--agent", "codex", "--session", "rebind-author",
                  "--work-id", "after-relocation", "--summary", "Legitimate write after relocation")
after = relay("events")
assert len(after) == 3 and follow_on["event"]["seq"] == 3
assert relay("status")["active_claims"] == claims and pending() == waiting
assert not any(row["kind"] in ("delivery.acknowledged", "claim.released") for row in after)
import sqlite3
with sqlite3.connect((repo / ".relay/relay.sqlite3").as_uri() + "?mode=ro", uri=True) as ledger:
    ledger.execute("PRAGMA query_only=ON")
    assert ledger.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert ledger.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 3
ledger.close()

# Returning to the original pathname is a fresh explicit generation, not revival.
before_return = object_tree(repo)
before_return_births = ledger_births(repo)
previous = repo
old.parent.mkdir(parents=True, exist_ok=True)
repo.rename(old)
repo = old
account_before_return = object_tree(home)
relay("events", success=False)
assert object_tree(home) == account_before_return and object_tree(repo) == before_return
return_plan = relay("rebind-plan", "--from-repo", str(previous))
assert return_plan["generation"] == 2 and return_plan["next_generation"] == 3
assert object_tree(home) == account_before_return and object_tree(repo) == before_return
returned = relay("rebind", "--from-repo", str(previous), "--expected-binding",
                 return_plan["expected_binding"], "--confirm-quiescent")
assert returned["generation"] == 3 and returned["enrollment_id"] == initialized["enrollment_id"]
assert returned["binding_id"] != applied["binding_id"]
assert object_tree(repo) == before_return and ledger_births(repo) == before_return_births
assert [anchor.stat().st_ino, anchor.read_bytes(), anchor.stat().st_mtime_ns] == anchor_before
assert relay("events") == after and relay("status")["active_claims"] == claims
assert pending() == waiting and relay("doctor")["ok"]
assert relay("init") == initialized
assert object_tree(installation) == before_installation and object_tree(foreign) == before_foreign
assert object_tree(home / ".local/bin") == before_bin
print(json.dumps({"public_rebind": True, "move": mode, "source_absent": True,
                  "same_object_and_birth_evidence": True, "same_enrollment": True,
                  "active_claim_and_pending_handoff_preserved": True,
                  "no_implicit_ack_or_release": True, "subsequent_write": True,
                  "move_back_requires_fresh_generation": True,
                  "generation": 3, "events": 3, "sqlite_integrity": "ok", "provider_calls": 0}))
"""


class PublicRebindTests(unittest.TestCase):
    def check_move(self, mode):
        case = profile.PublicProfileTests("test_public_installed_ledger_in_fresh_rootless_account")
        self.addCleanup(case.doCleanups)
        case.setUp()
        built = subprocess.run(
            ["/usr/bin/python3", "-I", "-S", "-B", str(profile.SOURCE / "relay_bootstrap.py"),
             "build-release", "--output", str(case.bundle), "--version", "0.0.0-public-rebind"],
            env=case.env, text=True, capture_output=True, timeout=20)
        self.assertEqual(0, built.returncode, built.stdout + built.stderr)
        release = json.loads(built.stdout)
        self.assertFalse(release["approved"])
        case.sandbox(profile._INSTALL, release["release_id"], include_source=True)
        result = case.sandbox(_MOVE.replace('"MAIN_OR_PARENT"', repr(mode)), release["release_id"],
                              include_source=False)
        self.assertEqual({
            "public_rebind": True, "move": mode, "source_absent": True,
            "same_object_and_birth_evidence": True, "same_enrollment": True,
            "active_claim_and_pending_handoff_preserved": True,
            "no_implicit_ack_or_release": True, "subsequent_write": True,
            "move_back_requires_fresh_generation": True,
            "generation": 3, "events": 3, "sqlite_integrity": "ok", "provider_calls": 0,
        }, result)

    def test_main_checkout_move_preserves_real_ledger_and_pending_work(self):
        self.check_move("main")

    def test_parent_move_preserves_real_ledger_and_pending_work(self):
        self.check_move("parent")
