"""Public external-bundle recovery with actual enrolled ledger and active claim."""
import json
import subprocess
import unittest

import test_public_profile as profile


_RECOVERY = profile._COMMON + r"""
public = ["/usr/bin/python3", "-I", "-S", "-B", "/source/src/relay_bootstrap.py"]
base = [str(launcher), "--repo", str(project), "--json"]
old = call([str(launcher), "runtime", "status"])["activation"]
initialized = call(base + ["init"])
call(base + ["signal", "work.intent", "--agent", "codex", "--session", "recovery-fixture",
             "--work-id", "recovery-proof", "--summary", "Keep pending work through code recovery"])
claim = call(base + ["claim", "code:recovery-fixture", "--agent", "codex",
                    "--session", "recovery-fixture", "--purpose", "Preserve exact ownership"])["claim"]
events = call(base + ["events"])
claims = call(base + ["status"])["active_claims"]
assert len(events) == 2 and len(claims) == 1 and claims[0]["claim_id"] == claim["claim_id"]
registry = home / ".local/share/relay/enrollments"
unrelated = home / ".local/bin/unrelated-fixture"
unrelated.write_text("preserve operator command\n")
settings = home / ".config/provider-fixture"
settings.mkdir(parents=True)
(settings / "hooks.json").write_text('{"synthetic":"preserve settings"}\n')
damaged = installation / "releases" / old["release_id"]
(damaged / "bootstrap.py").write_text(
    'from pathlib import Path\nPath("/tmp/foreign-home/suspect-executed").write_text("bad")\n')
before_damage = snapshot(damaged)
before_launch = snapshot(pathlib.Path(old["target"]).parent)
before_project = snapshot(project)
before_registry = snapshot(registry)
before_foreign = snapshot(foreign)
before_settings = snapshot(settings)
before_home = snapshot(home)
refused = subprocess.run([str(launcher), "runtime", "status"], text=True, capture_output=True, timeout=15)
assert refused.returncode != 0 and refused.stdout == "", "suspect installed bootstrap was accepted"
inspection = call(public + ["inspect"])
assert inspection["state"] == "degraded" and inspection["activation"] is None
assert len(inspection["releases"]) == 1 and inspection["releases"][0]["state"] == "unverified"
assert not inspection["recovery_options"], "fixture accidentally retained a working recovery candidate"
observation = inspection["selector"]["observation"]
retained = subprocess.run(public + ["recover", "--release-sha256", old["release_id"],
                                   "--expected-selector", observation], text=True, capture_output=True, timeout=15)
assert retained.returncode != 0 and retained.stdout == ""
plan = call(public + ["recover-plan", "--release", "/bundle", "--approve-sha256", sys.argv[2]])
assert plan["release_id"] != old["release_id"] and plan["expected_selector"] == observation
assert not plan["release_already_installed"] and not plan["repairs_damaged_files"]
assert snapshot(home) == before_home and snapshot(project) == before_project, "diagnosis/plan wrote state"
recovered = call(public + ["recover-install", "--release", "/bundle", "--approve-sha256", sys.argv[2],
                          "--expected-selector", observation])
assert recovered["installed"] and recovered["recovered_from_bundle"]
assert not recovered["repairs_damaged_files"] and not recovered["enrollment_changed"]
assert not recovered["hooks_changed"] and not recovered["network_access"]
active = call([str(launcher), "runtime", "status"])["activation"]
assert active == recovered["activation"] and active["release_id"] == sys.argv[2]
assert active["activation_id"] != old["activation_id"]
assert os.readlink(recovered["retained_selector"]) == old["target"]
assert snapshot(damaged) == before_damage and snapshot(pathlib.Path(old["target"]).parent) == before_launch
# Before reopening SQLite, assert exact actual DB/WAL/SHM and identity-file preservation.
assert snapshot(project) == before_project and snapshot(registry) == before_registry
assert snapshot(foreign) == before_foreign and snapshot(settings) == before_settings
assert unrelated.read_text() == "preserve operator command\n"
before_retry = snapshot(home)
stale = subprocess.run(public + ["recover-install", "--release", "/bundle", "--approve-sha256", sys.argv[2],
                                "--expected-selector", observation], text=True, capture_output=True, timeout=15)
assert stale.returncode != 0 and stale.stdout == "" and snapshot(home) == before_retry
assert call(base + ["init"]) == initialized
assert call(base + ["events"]) == events
assert call(base + ["status"])["active_claims"] == claims
assert call(base + ["doctor"])["ok"]
import sqlite3
with sqlite3.connect((project / ".relay/relay.sqlite3").as_uri() + "?mode=ro", uri=True) as ledger:
    ledger.execute("PRAGMA query_only=ON")
    assert ledger.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert ledger.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 2
assert snapshot(registry) == before_registry and snapshot(foreign) == before_foreign
# The next distinct process has neither source nor bundle mounted.
pathlib.Path("/tmp/external-recovery-proof.json").write_text(json.dumps({
    "initialized": initialized, "events": events, "claims": claims,
    "release_id": active["release_id"], "activation_id": active["activation_id"]}))
print(json.dumps({"public_external_recovery": True, "verified_retained_candidates_before": 0,
                  "damaged_objects_retained": True, "ledger_bytes_preserved_before_reopen": True,
                  "same_enrollment": True, "work_intent_and_active_claim_preserved": True,
                  "stale_retry_refused": True, "events": 2, "sqlite_integrity": "ok", "provider_calls": 0}))
"""

_AFTER = profile._COMMON + r"""
assert not pathlib.Path("/source").exists() and not pathlib.Path("/bundle").exists()
proof = json.loads(pathlib.Path("/tmp/external-recovery-proof.json").read_text())
base = [str(launcher), "--repo", str(project), "--json"]
active = call([str(launcher), "runtime", "status"])["activation"]
assert active["release_id"] == proof["release_id"] == sys.argv[2]
assert active["activation_id"] == proof["activation_id"]
assert call(base + ["init"]) == proof["initialized"]
assert call(base + ["events"]) == proof["events"]
assert call(base + ["status"])["active_claims"] == proof["claims"]
assert call(base + ["doctor"])["ok"]
assert not (foreign / "suspect-executed").exists()
print(json.dumps({"source_absent_recovered_launcher": True, "same_ledger_and_claim": True}))
"""


class PublicExternalRecoveryTests(unittest.TestCase):
    def test_no_verified_retained_release_recovers_and_reopens_without_source(self):
        case = profile.PublicProfileTests("test_public_installed_ledger_in_fresh_rootless_account")
        self.addCleanup(case.doCleanups)
        case.setUp()
        releases = []
        for number in (1, 2):
            case.bundle = case.base / ("external-bundle-" + str(number))
            built = subprocess.run(
                ["/usr/bin/python3", "-I", "-S", "-B", str(profile.SOURCE / "relay_bootstrap.py"),
                 "build-release", "--output", str(case.bundle),
                 "--version", "0.0." + str(number) + "-external-recovery"],
                env=case.env, text=True, capture_output=True, timeout=20)
            self.assertEqual(0, built.returncode, built.stdout + built.stderr)
            release = json.loads(built.stdout)
            self.assertFalse(release["approved"])
            releases.append(release["release_id"])
            if number == 1:
                case.sandbox(profile._INSTALL, release["release_id"], include_source=True)
        self.assertNotEqual(*releases)
        result = case.sandbox(_RECOVERY, releases[1], include_source=True)
        self.assertEqual({
            "public_external_recovery": True, "verified_retained_candidates_before": 0,
            "damaged_objects_retained": True, "ledger_bytes_preserved_before_reopen": True,
            "same_enrollment": True, "work_intent_and_active_claim_preserved": True,
            "stale_retry_refused": True, "events": 2, "sqlite_integrity": "ok", "provider_calls": 0,
        }, result)
        self.assertEqual({"source_absent_recovered_launcher": True, "same_ledger_and_claim": True},
                         case.sandbox(_AFTER, releases[1], include_source=False))
