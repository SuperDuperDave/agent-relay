#!/usr/bin/env python3
"""Optional actual-Claude startup probe: disposable account, no credentials/network.

Run manually with python3 -I -S -B and --claude /absolute/path. This is not
discovered by the default unit suite and does not prove model execution.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests/runtime"))
import test_public_profile as profile


SCENARIO = profile._COMMON + r'''
import re, sqlite3
assert not pathlib.Path("/source").exists() and not pathlib.Path("/bundle").exists()
provider = pathlib.Path(PROVIDER_PATH)
assert provider.is_file()
subprocess.run(["/usr/bin/git", "-C", str(project), "-c", "core.hooksPath=/dev/null",
                "-c", "commit.gpgsign=false", "-c", "user.name=Fixture",
                "-c", "user.email=fixture@example.invalid", "commit", "--allow-empty", "-q", "-F", "-"],
               input="Native provider startup fixture\n", text=True, check=True, timeout=10)
base = [str(launcher), "--repo", str(project), "--json"]
call(base + ["init"])
claim = call(base + ["claim", "code:native-startup", "--agent", "codex", "--session", "fixture-owner",
                     "--purpose", "Preserve exact ownership across native provider startup"])["claim"]
handoff = call(base + ["signal", "work.handoff", "--agent", "codex", "--session", "fixture-owner",
                       "--work-id", "native-startup", "--target", "claude", "--commit", "HEAD",
                       "--summary", "Pending synthetic review; startup must not acknowledge"])["event"]
pending_args = base + ["channel-pending", "--agent", "claude", "--source-agent", "codex",
                       "--work-id", "native-startup", "--limit", "1"]
pending = call(pending_args)
claims = call(base + ["status"])["active_claims"]
config = call(base + ["provider-config", "--client", "claude"])
assert config["provider"] == "claude" and config["native_arguments"][0] == "--settings"
assert not config["changes_provider_settings"] and not config["changes_permissions"] and not config["launches_provider"]
native = config["native_arguments"]
assert len(native) == 2 and isinstance(json.loads(native[1])["hooks"], dict)
assert config["hook_command"].startswith(str(launcher) + " --repo " + str(project))
before_installation = snapshot(installation)
before_registry = snapshot(home / ".local/share/relay/enrollments")
before_git = snapshot(project / ".git")
before_foreign = snapshot(foreign)
claude_home = pathlib.Path("/tmp/claude-native-home")
claude_home.mkdir(mode=0o700)
# Excluded synthetic provider settings remain present, including an unrelated
# hook definition. These are fixture data, not substitutes for Relay's argv.
(claude_home / ".claude").mkdir(mode=0o700)
(project / ".claude").mkdir(mode=0o700)
unrelated = json.dumps({"hooks": {"SessionStart": [{"hooks": [{"type": "command",
    "command": "printf unrelated-fixture-hook"}]}]}, "env": {"RELAY_FIXTURE_SETTING": "preserve"}})
user_settings = claude_home / ".claude/settings.json"
user_settings.write_text(unrelated)
(project / ".claude/settings.json").write_text(unrelated)
(project / ".claude/settings.local.json").write_text(unrelated)
before_project_settings = snapshot(project / ".claude")
user_settings_info = user_settings.stat()
before_user_settings = [user_settings_info.st_mode, user_settings_info.st_size,
                        user_settings_info.st_mtime_ns, hashlib.sha256(user_settings.read_bytes()).hexdigest()]
environment = {"PATH": "/usr/bin:/bin", "LC_ALL": "C.UTF-8", "HOME": str(claude_home),
               "XDG_CONFIG_HOME": str(claude_home / "config"), "XDG_DATA_HOME": str(claude_home / "data"),
               "XDG_CACHE_HOME": str(claude_home / "cache"), "GIT_CONFIG_NOSYSTEM": "1",
               "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null", "GIT_TERMINAL_PROMPT": "0"}
version = subprocess.run([str(provider), "--version"], env=environment, capture_output=True,
                         text=True, timeout=5).stdout.strip()
command = [str(provider), "--print", "Offline native hook startup probe; do not use tools.",
           "--output-format", "stream-json", "--verbose", "--debug", "hooks", *native,
           "--setting-sources", "", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
           "--permission-mode", "default"]
completed = subprocess.run(command, env=environment, input="", text=True,
                           capture_output=True, timeout=15)
messages = [json.loads(line) for line in completed.stdout.splitlines() if line.startswith("{")]
initial = next(row for row in messages if row.get("type") == "system" and row.get("subtype") == "init")
final = next(row for row in messages if row.get("type") == "result")
assert completed.returncode != 0 and final.get("is_error") is True
assert "Invalid API key" in final.get("result", "") and "/login" in final.get("result", "")
assert initial.get("apiKeySource") == "none" and initial.get("mcp_servers") == []
assert final.get("duration_api_ms") == 0 and final.get("total_cost_usd") == 0
assert final.get("usage", {}).get("input_tokens") == 0 and final.get("usage", {}).get("output_tokens") == 0
assert not any(row.get("type") == "assistant" and any(block.get("type") == "tool_use"
               for block in row.get("message", {}).get("content", [])) for row in messages)
events = call(base + ["events"])
starts = [row for row in events if row["kind"] == "session.started" and row["agent"] == "claude"]
assert len(starts) == 1, [(row["kind"], row["agent"]) for row in events]
assert starts[0]["seq"] == handoff["seq"] + 1
assert starts[0]["session"] == initial["session_id"]
assert [(row["kind"], row["agent"], row["session"]) for row in events if row["seq"] > handoff["seq"]] == [
    (kind, "claude", initial["session_id"]) for kind in ("session.started", "turn.completed", "session.ended")]
assert pending == call(pending_args) and claims == call(base + ["status"])["active_claims"]
assert snapshot(installation) == before_installation
assert snapshot(home / ".local/share/relay/enrollments") == before_registry
after_git, after_foreign = snapshot(project / ".git"), snapshot(foreign)
git_deltas = {key: [before_git.get(key), after_git.get(key)] for key in set(before_git) | set(after_git)
              if before_git.get(key) != after_git.get(key)}
foreign_deltas = {key: [before_foreign.get(key), after_foreign.get(key)] for key in set(before_foreign) | set(after_foreign)
                  if before_foreign.get(key) != after_foreign.get(key)}
# Native Git inspection can create/remove a transient lock: only the directory
# mtime may move. Every child entry (including hooks/config) stays byte-exact.
assert set(git_deltas) <= {"."} and not foreign_deltas, {"git": git_deltas, "foreign": foreign_deltas}
assert after_git["."][:2] == before_git["."][:2] and after_git["."][3:] == before_git["."][3:]
assert snapshot(project / ".claude") == before_project_settings
user_settings_info = user_settings.stat()
assert [user_settings_info.st_mode, user_settings_info.st_size, user_settings_info.st_mtime_ns,
        hashlib.sha256(user_settings.read_bytes()).hexdigest()] == before_user_settings
assert {path.name for path in (claude_home / ".claude").glob("settings*.json")} == {"settings.json"}
with sqlite3.connect((project / ".relay/relay.sqlite3").as_uri() + "?mode=ro", uri=True) as ledger:
    ledger.execute("PRAGMA query_only=ON")
    assert ledger.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    kinds = dict(ledger.execute("SELECT kind, COUNT(*) FROM events GROUP BY kind").fetchall())
    assert kinds == {kind: 1 for kind in ("claim.acquired", "work.handoff", "session.started", "turn.completed", "session.ended")}
    assert kinds.get("delivery.acknowledged", 0) == 0
    assert kinds.get("claim.released", 0) == 0 and kinds.get("claim.broken", 0) == 0
    assert ledger.execute("SELECT claim_id FROM claims WHERE released_event_seq IS NULL").fetchall() == [(claim["claim_id"],)]
    assert ledger.execute("SELECT session FROM events WHERE kind='session.started' AND agent='claude'").fetchall() == [(initial["session_id"],)]
debug_text = completed.stderr
debug_files = list((claude_home / ".claude/debug").glob("*.txt"))
for path in debug_files:
    assert path.stat().st_size <= 2 * 1024 * 1024
    debug_text += "\n" + path.read_text(errors="replace")
contexts = []
decoder = json.JSONDecoder()
for match in re.finditer(r'\{"hookSpecificOutput"', debug_text):
    try:
        value, _ = decoder.raw_decode(debug_text[match.start():])
    except ValueError:
        continue
    output = value.get("hookSpecificOutput", {})
    if output.get("hookEventName") == "SessionStart":
        context = output.get("additionalContext", "")
        if "RELAY BRIEF v1" in context:
            contexts.append(context)
context = next((text for text in contexts if "last_seq=" + str(starts[0]["seq"]) in text), None)
if context is not None:
    assert len(context.encode("utf-8")) <= 8192 and starts[0]["session"] in context
print(json.dumps({"scope": "actual native Claude -> generated inline public Relay hook -> ledger; no model proof",
                  "native_version": version, "native_exit": completed.returncode,
                  "native_result_subtype": final.get("subtype"), "native_result_is_error": final["is_error"],
                  "authentication_refused": True, "real_credentials": False, "synthetic_key": False,
                  "api_duration_ms": final["duration_api_ms"], "cost_usd": final["total_cost_usd"],
                  "input_tokens": final["usage"]["input_tokens"], "output_tokens": final["usage"]["output_tokens"],
                  "network_interfaces": interfaces, "source_absent": True,
                  "native_arguments_sha256": hashlib.sha256(json.dumps(native, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest(),
                  "session_started_events": len(starts), "session_matches_native": True,
                  "hook_context_stdout_observed_in_native_debug": context is not None,
                  "hook_context_bytes": len(context.encode("utf-8")) if context is not None else None,
                  "debug_context_marker_present": "RELAY BRIEF v1" in debug_text,
                  "debug_file_count": len(debug_files),
                  "event_counts": kinds, "pending_preserved": True, "claim_preserved": True,
                  "installed_code_and_enrollment_preserved": True, "git_entries_and_foreign_home_preserved": True,
                  "git_directory_mtime_changed": bool(git_deltas),
                  "preexisting_provider_settings_and_hook_definitions_preserved": True,
                  "generated_persistent_hook_fragment": False,
                  "sqlite_integrity": "ok", "model_received_context_proved": False}))
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--claude", required=True, help="absolute installed Claude entry path inside /usr")
    args = parser.parse_args()
    selected = Path(args.claude)
    if not selected.is_absolute() or not selected.is_relative_to("/usr"):
        parser.error("this bounded profile accepts an explicit executable entry inside read-only /usr")
    target = selected.resolve(strict=True)
    if not target.is_file() or not target.is_relative_to("/usr"):
        parser.error("executable symlink target must also remain inside read-only /usr")
    executable_sha = hashlib.sha256(target.read_bytes()).hexdigest()
    sources = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
               for p in sorted((ROOT / "src").rglob("*.py"))}
    case = profile.PublicProfileTests("test_public_installed_ledger_in_fresh_rootless_account")
    try:
        case.setUp()
        built = subprocess.run(["/usr/bin/python3", "-I", "-S", "-B", str(profile.SOURCE / "relay_bootstrap.py"),
                                "build-release", "--output", str(case.bundle), "--version", "0.0.0-native-startup-probe"],
                               env=case.env, text=True, capture_output=True, timeout=20)
        if built.returncode:
            raise RuntimeError("disposable release build failed")
        release = json.loads(built.stdout)
        assert not release["approved"]
        case.sandbox(profile._INSTALL, release["release_id"], include_source=True)
        result = case.sandbox("PROVIDER_PATH = " + repr(str(selected)) + "\n" + SCENARIO,
                              release["release_id"], include_source=False)
        assert hashlib.sha256(target.read_bytes()).hexdigest() == executable_sha
        assert sources == {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                           for p in sorted((ROOT / "src").rglob("*.py"))}
        result.update(source_sha256=sources,
                      release_sha256=release["release_id"], native_executable_sha256=executable_sha,
                      probe_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
        print(json.dumps(result, sort_keys=True, indent=2))
        return 0
    finally:
        case.doCleanups()


if __name__ == "__main__":
    raise SystemExit(main())
