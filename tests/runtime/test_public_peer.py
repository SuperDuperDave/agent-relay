"""Source-absent installed peer/launch commands with a synthetic native process."""

import json
import subprocess
import unittest

import test_public_profile as profile


_FAKE_PROVIDER = r"""
import argparse, json, os, pathlib, shlex, subprocess, sys
parser = argparse.ArgumentParser()
parser.add_argument("--settings", required=True)
parser.add_argument("--print", action="store_true", required=True)
parser.add_argument("--output-format", choices=("json",), required=True)
parser.add_argument("--permission-prompts", choices=("none",), required=True)
parser.add_argument("--max-turns", type=int, required=True)
parser.add_argument("--session-id", required=True)
args = parser.parse_args()
assert args.max_turns == 3
assert os.getcwd() == "/tmp/project"
assert os.environ["HOME"] == "/tmp/foreign-home"
assert os.environ["PYTHONPATH"] == "/tmp/project"
assert os.environ["XDG_CONFIG_HOME"] == "/tmp/foreign-home/config"
task = sys.stdin.buffer.read()
pathlib.Path("/tmp/fake-native-task.txt").write_bytes(task)
with pathlib.Path("/tmp/fake-native-calls.txt").open("a") as calls:
    calls.write("called\n")
settings = json.loads(args.settings)
assert set(settings) == {"hooks"}
names = ("SessionStart", "UserPromptSubmit", "Stop", "SessionEnd")
assert set(settings["hooks"]) == set(names)
contexts = {}
for name in names:
    entries = settings["hooks"][name]
    assert len(entries) == 1 and set(entries[0]) == {"hooks"}
    handlers = entries[0]["hooks"]
    assert len(handlers) == 1 and handlers[0]["type"] == "command"
    assert handlers[0]["timeout"] == 3
    command = shlex.split(handlers[0]["command"])
    assert command[-3:] == ["provider-hook", "--client", "claude"]
    payload = {"hook_event_name": name, "session_id": args.session_id,
               "prompt_id": "synthetic-peer-" + name, "turn_id": "synthetic-peer-turn",
               "cwd": "/tmp/foreign-home", "prompt": task.decode("utf-8")}
    completed = subprocess.run(command, input=json.dumps(payload), text=True,
                               capture_output=True, timeout=10)
    assert completed.returncode == 0 and not completed.stderr, completed
    if name in ("SessionStart", "UserPromptSubmit"):
        context = json.loads(completed.stdout)["hookSpecificOutput"]
        assert context["hookEventName"] == name
        assert "RELAY AGENT CONTRACT v1" in context["additionalContext"]
        assert args.session_id in context["additionalContext"]
        assert "Preserve this pending handoff" in context["additionalContext"]
        contexts[name] = context["additionalContext"]
    else:
        assert completed.stdout == ""
pathlib.Path("/tmp/fake-native-receipt.json").write_text(json.dumps({
    "session_id": args.session_id, "contexts": contexts, "argv": sys.argv[1:],
    "inherited_environment": True, "hooks_executed": list(names),
}))
print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                  "session_id": args.session_id, "result": "Useful synthetic peer answer",
                  "permission_denials": [], "terminal_reason": "completed", "num_turns": 1}))
"""


_PEER = profile._COMMON + "\nprovider_source = " + repr(_FAKE_PROVIDER) + r"""
assert not pathlib.Path("/source").exists() and not pathlib.Path("/bundle").exists()
base = [str(launcher), "--repo", str(project), "--json"]
git_env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null",
               GIT_CONFIG_SYSTEM="/dev/null", GIT_TERMINAL_PROMPT="0",
               GIT_AUTHOR_DATE="2000-01-01T00:00:00+00:00",
               GIT_COMMITTER_DATE="2000-01-01T00:00:00+00:00")
subprocess.run(["/usr/bin/git", "-C", str(project), "-c", "user.name=Fixture",
                "-c", "user.email=fixture@example.invalid", "-c", "core.hooksPath=/dev/null",
                "-c", "commit.gpgsign=false", "commit", "--allow-empty", "-q", "-F", "-"],
               input="Public peer fixture baseline\n", text=True, env=git_env,
               check=True, capture_output=True)
call(base + ["init"])
handoff = call(base + ["signal", "work.handoff", "--agent", "codex", "--session", "author-fixture",
                       "--work-id", "peer-proof", "--target", "claude", "--commit", "HEAD",
                       "--summary", "Preserve this pending handoff"])["event"]
claim = call(base + ["claim", "code:peer-fixture", "--agent", "codex", "--session", "author-fixture",
                     "--purpose", "A returned provider result must not release this"])["claim"]
provider = pathlib.Path("/tmp/fake-native")
pathlib.Path("/tmp/fake-native.py").write_text(provider_source)
provider.write_text('#!/bin/sh\nexec /usr/bin/python3 -I -S -B /tmp/fake-native.py "$@"\n')
provider.chmod(0o700)
settings = home / ".config/provider-fixture"
settings.mkdir(parents=True)
(settings / "settings.json").write_text('{"synthetic":"preserve existing settings"}\n')
before_settings = snapshot(settings)
before_foreign = snapshot(foreign)
before_hooks = snapshot(project / ".git/hooks")
before_registry = snapshot(home / ".local/share/relay/enrollments")
before_events = call(base + ["events"])
assert len(before_events) == 2

launch = call(base + ["launch", "claude", "--provider", str(provider)])
assert launch["state"] == "launch_prepared" and launch["provider_started"] is False
assert launch["argv"][0] == str(provider) and launch["repo"] == str(project)
assert launch["relay_plan"]["changes_provider_settings"] is False
assert launch["relay_plan"]["changes_permissions"] is False
assert not pathlib.Path("/tmp/fake-native-calls.txt").exists()
assert call(base + ["events"]) == before_events

task = 'Review this synthetic packet: 雪\r\nLiteral shell text: $(touch /tmp/peer-shell-canary) `false`\n'.encode("utf-8")
task_file = pathlib.Path("/tmp/peer-task.txt")
task_file.write_bytes(task)
evidence = pathlib.Path("/tmp/peer-evidence")
completed = subprocess.run(base + ["peer", "claude", "--provider", str(provider),
                                   "--task-file", str(task_file), "--output-dir", str(evidence),
                                   "--max-turns", "3", "--timeout", "20"],
                           cwd=project, text=True, capture_output=True, timeout=30)
assert completed.returncode == 0, (completed.returncode, completed.stdout, completed.stderr)
peer = json.loads(completed.stdout)
assert peer["state"] == "returned" and peer["provider_started"] is True
assert peer["process_exit_code"] == 0 and peer["needs_attention"] is False
assert peer["result"] == "Useful synthetic peer answer"
assert peer["session_id"] == peer["requested_session_id"]
assert peer["workflow_completion"] == "not_checked"
assert peer["relay_acknowledgement"] == "not_checked"
assert peer["hook_delivery"] == peer["provider_tools"] == "unknown"
assert str(evidence) == peer["evidence_directory"]
assert "relay peer: session " + peer["session_id"] in completed.stderr
assert pathlib.Path("/tmp/fake-native-task.txt").read_bytes() == task
assert (evidence / "task.txt").read_bytes() == task
assert json.loads((evidence / "result.json").read_text()) == peer
assert json.loads((evidence / "request.json").read_text())["task_sha256"] == hashlib.sha256(task).hexdigest()
assert pathlib.Path("/tmp/fake-native-calls.txt").read_text() == "called\n"
assert not pathlib.Path("/tmp/peer-shell-canary").exists()
receipt = json.loads(pathlib.Path("/tmp/fake-native-receipt.json").read_text())
assert receipt["session_id"] == peer["session_id"] and receipt["inherited_environment"] is True
assert receipt["hooks_executed"] == ["SessionStart", "UserPromptSubmit", "Stop", "SessionEnd"]
assert set(receipt["contexts"]) == {"SessionStart", "UserPromptSubmit"}
assert all(len(context.encode("utf-8")) <= 8192 for context in receipt["contexts"].values())
events = call(base + ["events"])
assert events[:2] == before_events and len(events) == 5, events
assert [row["kind"] for row in events[2:]] == ["session.started", "turn.completed", "session.ended"]
assert all(row["agent"] == "claude" and row["session"] == peer["session_id"] for row in events[2:])
assert "Review this synthetic packet" not in json.dumps(events)
assert not any(row["kind"] in ("delivery.acknowledged", "claim.released", "work.completed") for row in events)
assert any(row["seq"] == handoff["seq"] for row in call(base + ["brief", "--agent", "claude"])["pending_signals"])
assert call(base + ["status"])["active_claims"][0]["claim_id"] == claim["claim_id"]
assert call(base + ["doctor"])["ok"]
assert snapshot(settings) == before_settings and snapshot(foreign) == before_foreign
assert snapshot(project / ".git/hooks") == before_hooks
assert snapshot(home / ".local/share/relay/enrollments") == before_registry
print(json.dumps({"installed_peer": True, "source_absent": True, "launch_plan_only": True,
                  "exact_task_bytes": True, "exact_peer_identity": True,
                  "public_hooks_executed": 4, "lifecycle_events": 3,
                  "pending_handoff_preserved": True, "active_claim_preserved": True,
                  "workflow_completion_inferred": False, "provider_settings_unchanged": True,
                  "fake_provider_calls": 1, "real_provider_calls": 0}))
"""


class PublicPeerTests(unittest.TestCase):
    def test_source_absent_installed_peer_and_launch(self):
        case = profile.PublicProfileTests("test_public_installed_ledger_in_fresh_rootless_account")
        self.addCleanup(case.doCleanups)
        case.setUp()
        built = subprocess.run(
            ["/usr/bin/python3", "-I", "-S", "-B", str(profile.SOURCE / "relay_bootstrap.py"),
             "build-release", "--output", str(case.bundle), "--version", "0.0.0-public-peer"],
            env=case.env, text=True, capture_output=True, timeout=20)
        self.assertEqual(0, built.returncode, built.stdout + built.stderr)
        release = json.loads(built.stdout)
        self.assertFalse(release["approved"])
        case.sandbox(profile._INSTALL, release["release_id"], include_source=True)
        result = case.sandbox(_PEER, release["release_id"], include_source=False)
        self.assertEqual({
            "installed_peer": True, "source_absent": True, "launch_plan_only": True,
            "exact_task_bytes": True, "exact_peer_identity": True,
            "public_hooks_executed": 4, "lifecycle_events": 3,
            "pending_handoff_preserved": True, "active_claim_preserved": True,
            "workflow_completion_inferred": False, "provider_settings_unchanged": True,
            "fake_provider_calls": 1, "real_provider_calls": 0,
        }, result)
