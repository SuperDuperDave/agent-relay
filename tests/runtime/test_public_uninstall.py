"""Actual public code uninstall/reinstall preserves an enrolled SQLite ledger."""
import json
import subprocess
import unittest

import test_public_profile as profile


_ROUND_TRIP = profile._COMMON + r"""
public = ["/usr/bin/python3", "-I", "-S", "-B", "/source/src/relay_bootstrap.py"]
base = [str(launcher), "--repo", str(project), "--json"]
initialized = call(base + ["init"])
intent = call(base + ["signal", "work.intent", "--agent", "codex", "--session", "uninstall-fixture",
                      "--work-id", "uninstall-proof", "--summary", "Keep this real event through code removal"])
claim = call(base + ["claim", "code:uninstall-fixture", "--agent", "codex", "--session", "uninstall-fixture",
                     "--purpose", "Prove code removal does not expire or release claims"])["claim"]
events = call(base + ["events"])
assert len(events) == 2 and intent["event"]["seq"] == 1
active_claims = call(base + ["status"])["active_claims"]
assert len(active_claims) == 1 and active_claims[0]["claim_id"] == claim["claim_id"]
registry = home / ".local/share/relay/enrollments"
unrelated = home / ".local/bin/unrelated-fixture"
unrelated.write_text("not part of Relay\n")
settings = home / ".config/provider-fixture"
settings.mkdir(parents=True)
(settings / "hooks.json").write_text('{"synthetic":"preserve existing settings"}\n')
before_project = snapshot(project)
before_registry = snapshot(registry)
before_foreign = snapshot(foreign)
before_settings = snapshot(settings)
before_home = snapshot(home)
plan = call([str(launcher), "runtime", "uninstall-plan"])
assert plan["can_uninstall"] and not plan["uninstalled"] and plan["targets"]
assert snapshot(home) == before_home and snapshot(project) == before_project, "plan mutated fixture"
removed = call([str(launcher), "runtime", "uninstall", "--expected-plan", plan["expected_plan"]])
assert removed["uninstalled"] and removed["state"] == "complete"
assert not removed["artifact_purge_complete"] and not removed["enrollment_changed"] and not removed["hooks_changed"]
assert not launcher.exists() and not launcher.is_symlink()
assert not (installation / "releases").exists() and not (installation / "launches").exists()
remaining_names = {p.name for p in installation.iterdir()}
assert len(remaining_names) == 3 and "activation.lock" in remaining_names, remaining_names
assert sum(name.startswith("uninstall-") for name in remaining_names) == 1
assert sum(name.startswith("uninstalled-") for name in remaining_names) == 1
# Compare before reopening SQLite: SHM coordination may legitimately change
# when a later actual ledger command opens the preserved database.
assert snapshot(project) == before_project, "uninstall changed actual DB/WAL/SHM, source or Git marker/hooks"
assert snapshot(registry) == before_registry, "uninstall changed account enrollment anchors"
assert snapshot(foreign) == before_foreign and snapshot(settings) == before_settings
assert unrelated.read_text() == "not part of Relay\n"
inspection = call(public + ["inspect"])
assert inspection["state"] == "inactive" and inspection["activation"] is None
finished = call(public + ["uninstall-plan"])
assert finished["uninstalled"] and finished["can_uninstall"] and not finished["targets"]
# Repeat the exact completed operation through reviewed source, not a deleted wrapper.
assert call(public + ["uninstall", "--expected-plan", finished["expected_plan"]])["uninstalled"]
reinstalled = call(public + ["install", "--release", "/bundle", "--approve-sha256", sys.argv[2],
                            "--expected-activation", "none"])
assert reinstalled["installed"]
assert snapshot(project) == before_project and snapshot(registry) == before_registry
assert call(base + ["init"]) == initialized, "reinstall changed enrolled workspace identity"
assert call(base + ["events"]) == events, "reinstall lost or appended ledger history"
assert call(base + ["status"])["active_claims"] == active_claims, "code removal altered resource ownership"
assert call(base + ["doctor"])["ok"]
import sqlite3
with sqlite3.connect((project / ".relay/relay.sqlite3").as_uri() + "?mode=ro", uri=True) as ledger:
    ledger.execute("PRAGMA query_only=ON")
    assert ledger.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert ledger.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 2
assert snapshot(registry) == before_registry and snapshot(foreign) == before_foreign
assert snapshot(settings) == before_settings and unrelated.read_text() == "not part of Relay\n"
print(json.dumps({"public_uninstall": True, "installed_code_removed": True,
                  "retained_metadata_files": 3, "actual_ledger_preserved": True,
                  "same_enrollment_after_reinstall": True, "active_claim_preserved": True,
                  "sqlite_integrity": "ok", "events": 2, "provider_calls": 0}))
"""


class PublicUninstallTests(unittest.TestCase):
    def test_public_uninstall_reinstall_keeps_real_ledger_and_active_claim(self):
        case = profile.PublicProfileTests("test_public_installed_ledger_in_fresh_rootless_account")
        self.addCleanup(case.doCleanups)
        case.setUp()
        built = subprocess.run(
            ["/usr/bin/python3", "-I", "-S", "-B", str(profile.SOURCE / "relay_bootstrap.py"),
             "build-release", "--output", str(case.bundle), "--version", "0.0.0-public-uninstall"],
            env=case.env, text=True, capture_output=True, timeout=20)
        self.assertEqual(0, built.returncode, built.stdout + built.stderr)
        release = json.loads(built.stdout)
        self.assertFalse(release["approved"])
        case.sandbox(profile._INSTALL, release["release_id"], include_source=True)
        result = case.sandbox(_ROUND_TRIP, release["release_id"], include_source=True)
        self.assertEqual({
            "public_uninstall": True, "installed_code_removed": True,
            "retained_metadata_files": 3, "actual_ledger_preserved": True,
            "same_enrollment_after_reinstall": True, "active_claim_preserved": True,
            "sqlite_integrity": "ok", "events": 2, "provider_calls": 0,
        }, result)
