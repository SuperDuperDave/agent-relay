"""Optional native Codex discovery proof; no credentials, network or model turn."""
import argparse, hashlib, json, pathlib, subprocess, sys
root = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(root / "tests/runtime"))
import test_public_profile as profile

def sandbox(script, binary):
    case = profile.PublicProfileTests("test_public_installed_ledger_in_fresh_rootless_account")
    try:
        case.setUp()
        built = subprocess.run(["/usr/bin/python3", "-I", "-S", "-B", str(profile.SOURCE / "relay_bootstrap.py"),
                                "build-release", "--output", str(case.bundle), "--version", "0.0.0-native-codex"],
                               env=case.env, text=True, capture_output=True, timeout=20)
        assert built.returncode == 0, built.stderr
        release_id = json.loads(built.stdout)["release_id"]
        case.sandbox(profile._INSTALL, release_id, include_source=True)
        args = [
            str(profile.BWRAP), "--die-with-parent", "--new-session", "--unshare-user",
            "--uid", "1000", "--gid", "1000", "--unshare-pid", "--unshare-net",
            "--unshare-ipc", "--unshare-uts", "--hostname", "relay-native-fixture",
            "--clearenv", "--setenv", "PATH", "/usr/bin:/bin", "--setenv", "LC_ALL", "C.UTF-8",
            "--setenv", "HOME", "/home/relay-fixture",
            "--setenv", "CODEX_HOME", "/home/relay-fixture/.codex",
            "--setenv", "GIT_CONFIG_NOSYSTEM", "1", "--setenv", "GIT_CONFIG_GLOBAL", "/dev/null",
            "--setenv", "GIT_CONFIG_SYSTEM", "/dev/null", "--setenv", "GIT_TERMINAL_PROMPT", "0",
            "--ro-bind", "/usr", "/usr", "--symlink", "usr/bin", "/bin",
            "--symlink", "usr/lib", "/lib", "--symlink", "usr/lib64", "/lib64",
            "--ro-bind", str(case.base / "etc"), "/etc", "--dir", "/home",
            "--bind", str(case.base / "home"), "/home/relay-fixture",
            "--bind", str(case.base / "tmp"), "/tmp",
            "--dir", "/opt", "--ro-bind", str(binary), "/opt/codex",
            "--proc", "/proc", "--dev", "/dev", "--remount-ro", "/proc",
            "--remount-ro", "/dev", "--remount-ro", "/", "--chdir", "/tmp/project",
            "--", "/usr/bin/python3", "-I", "-S", "-B", "-c", script,
        ]
        result = subprocess.run(args, env=case.env, text=True, capture_output=True, timeout=60)
        if result.returncode:
            raise RuntimeError("isolated probe failed: " + result.stderr[:4000])
        value = json.loads(result.stdout)
        value["release_id"] = release_id
        return value
    finally:
        case.doCleanups()

RPC = r"""
import hashlib, json, os, pathlib, select, signal, subprocess, tempfile, time
codex_home = pathlib.Path("/home/relay-fixture/.codex")
codex_home.mkdir(mode=0o700)
user_hooks = {"hooks": {"SessionStart": [{"hooks": [
    {"type": "command", "command": "/usr/bin/touch /tmp/unrelated-user-hook-fired", "timeout": 3}
]}]}}
user_path = codex_home / "hooks.json"
user_path.write_text(json.dumps(user_hooks))
before = user_path.read_bytes()

def inspect(extra, *, start=False):
    with tempfile.TemporaryFile() as errors:
        proc = subprocess.Popen(["/opt/codex", "app-server", *extra], cwd="/tmp/project",
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors)
        pending = b""
        notifications = []
        value = None
        def send(message):
            proc.stdin.write((json.dumps(message) + "\n").encode())
            proc.stdin.flush()
        def rpc(number, method, params):
            nonlocal pending
            send({"id": number, "method": method, "params": params})
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    message = json.loads(line)
                    if message.get("id") == number:
                        assert "result" in message, message
                        return message["result"]
                    notifications.append(message)
                ready, _, _ = select.select([proc.stdout], [], [], max(0, deadline - time.monotonic()))
                if not ready:
                    break
                data = os.read(proc.stdout.fileno(), 65536)
                if not data:
                    break
                pending += data
                assert len(pending) <= 1024 * 1024, "native RPC buffer exceeded bound"
            raise RuntimeError("native RPC response absent: " + method)
        try:
            initialized = rpc(0, "initialize", {"clientInfo": {"name": "relay_native_fixture", "version": "0.0.0"},
                                               "capabilities": {"experimentalApi": True}})
            send({"method": "initialized", "params": {}})
            listing = rpc(1, "hooks/list", {"cwds": ["/tmp/project"]})
            value = {"native": initialized["userAgent"], "listing": listing}
            if start:
                started = rpc(2, "thread/start", {"cwd": "/tmp/project", "ephemeral": True})
                assert started["thread"]["ephemeral"] and started["thread"]["turns"] == []
                assert started["thread"]["status"]["type"] == "idle"
                assert started["sandbox"] == {"type": "readOnly", "networkAccess": False}
                assert started["approvalPolicy"] == "on-request"
                value["startup"] = {"ephemeral": True, "idle": True, "turns": 0,
                                    "sandbox": "readOnly", "approval_policy": "on-request"}
            return value
        finally:
            # This long-lived daemon has no public shutdown RPC. End only our
            # observation window, not a claimed successful provider lifecycle.
            assert proc.poll() is None, "native app-server exited before deliberate stop"
            proc.send_signal(signal.SIGTERM)
            proc.stdin.close()
            proc.stdin = None
            try:
                tail, _ = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate(timeout=5)
                raise RuntimeError("native app-server did not stop after deliberate SIGTERM")
            assert proc.returncode == -signal.SIGTERM, "unexpected native app-server exit"
            assert len(pending) + len(tail) <= 1024 * 1024, "native shutdown output exceeded bound"
            notifications.extend(json.loads(line) for line in (pending + tail).splitlines() if line)
            hook_notifications = [message for message in notifications
                                  if message.get("method") in {"hook/started", "hook/completed"}]
            assert not hook_notifications, "untrusted fixture unexpectedly emitted native hook-run notifications"
            if value is not None:
                value["graceful_shutdown"] = False
                value["deliberate_stop_method"] = "SIGTERM"
                value["deliberate_stop_exit"] = proc.returncode
                value["native_hook_notifications"] = len(hook_notifications)

assert not pathlib.Path("/source").exists() and not pathlib.Path("/bundle").exists()
assert [row.split(":")[0].strip() for row in pathlib.Path("/proc/net/dev").read_text().splitlines()[2:]] == ["lo"]
repo = pathlib.Path("/tmp/project") / ("quoted '雪 🧪 ;$ &" + chr(127))
repo.mkdir()
base = ["/home/relay-fixture/.local/bin/multithread", "--repo", str(repo), "--json"]
def relay(*args):
    result = subprocess.run(base + list(args), text=True, capture_output=True, timeout=15)
    assert result.returncode == 0 and not result.stderr, (result.returncode, result.stderr)
    return json.loads(result.stdout)
relay("init")
generated = relay("provider-config", "--client", "codex", "--launcher-name", "multithread")
assert not generated["launches_provider"] and not generated["changes_provider_settings"] and not generated["changes_permissions"]
schema_dir = pathlib.Path("/tmp/codex-native-schema")
schema_result = subprocess.run(["/opt/codex", "app-server", "generate-json-schema",
                                "--experimental", "--out", str(schema_dir)],
                               text=True, capture_output=True, timeout=15)
assert schema_result.returncode == 0, "native schema generation failed"
schema_bytes = (schema_dir / "ClientRequest.json").read_bytes()
schema = json.loads(schema_bytes)
methods = [name for item in schema.get("oneOf", [])
           for name in item.get("properties", {}).get("method", {}).get("enum", [])]
hook_methods = sorted(name for name in methods if name.startswith("hooks/"))
assert hook_methods == ["hooks/list"], "native hook API changed; review integration assumptions"
first = inspect(generated["native_arguments"], start=RUN_STARTUP)
changed_args = list(generated["native_arguments"])
changed_args[1] = changed_args[1].replace("/tmp/project", "/tmp/changed-fixture")
assert changed_args != generated["native_arguments"]
second = inspect(changed_args)
removed = inspect([])
def entries(value):
    assert len(value["listing"]["data"]) == 1
    entry = value["listing"]["data"][0]
    assert not entry["errors"] and not entry["warnings"], entry
    return entry["hooks"]
first_hooks, changed_hooks, removed_hooks = map(entries, (first, second, removed))
session_hooks = [h for h in first_hooks if h["source"] == "sessionFlags"]
assert len(session_hooks) == 5 and len(first_hooks) == 6
assert {h["eventName"] for h in session_hooks} == {"sessionStart", "userPromptSubmit", "stop", "sessionEnd", "interrupt"}
assert all(h["command"] == generated["hook_command"] and not h["isManaged"] and h["trustStatus"] == "untrusted" for h in session_hooks)
assert all(not h["async"] for h in session_hooks), "asynchronous hook notification coverage is unsupported"
original_start = next(h for h in session_hooks if h["eventName"] == "sessionStart")
changed_start = next(h for h in changed_hooks if h["source"] == "sessionFlags" and h["eventName"] == "sessionStart")
assert original_start["currentHash"] != changed_start["currentHash"] and changed_start["trustStatus"] == "untrusted"
assert len(removed_hooks) == 1 and removed_hooks[0]["source"] == "user"
assert removed_hooks[0] == next(h for h in first_hooks if h["source"] == "user")
assert not pathlib.Path("/tmp/unrelated-user-hook-fired").exists()
assert user_path.read_bytes() == before
assert relay("events") == [], "untrusted native discovery/startup appended Relay lifecycle state"
version = subprocess.check_output(["/opt/codex", "--version"], text=True).strip()
print(json.dumps({"provider": "codex", "version": version, "source_absent": True,
                  "session_hooks_discovered": len(session_hooks), "unrelated_hook_preserved": True,
                  "changed_definition_hash_changed": True, "session_flags_removed_without_config_edit": True,
                  "quoted_unicode_control_path_roundtrip": True,
                  "startup_requested": RUN_STARTUP,
                  "credential_free_startup": first.get("startup"),
                  "graceful_shutdown": False,
                  "deliberate_stops": [{key: value[key] for key in ("deliberate_stop_method", "deliberate_stop_exit")}
                                       for value in (first, second, removed)],
                  "observation_window": "validated RPC responses through deliberate SIGTERM of the retained live child",
                  "native_hook_notifications": sum(value["native_hook_notifications"] for value in (first, second, removed)),
                  "native_client_request_methods": len(methods), "native_hook_methods": hook_methods,
                  "native_client_request_schema_sha256": hashlib.sha256(schema_bytes).hexdigest(),
                  "trust_status": "untrusted", "trust_changed": False, "hook_effects_observed": False,
                  "ledger_events": 0, "network": "separate namespace; loopback only",
                  "credentials_available": False, "model_turns": 0}, sort_keys=True))
"""

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", required=True, type=pathlib.Path, help="reviewed absolute native Codex executable")
    parser.add_argument("--startup", action="store_true",
                        help="also observe a credential-free idle thread; neither mode proves graceful shutdown")
    args = parser.parse_args()
    if not args.codex.is_absolute():
        parser.error("--codex must be absolute")
    binary = args.codex.resolve(strict=True)
    before = hashlib.sha256(binary.read_bytes()).hexdigest()
    sources = {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
               for p in sorted((root / "src").rglob("*.py"))}
    result = sandbox("RUN_STARTUP = " + repr(args.startup) + "\n" + RPC, binary)
    assert hashlib.sha256(binary.read_bytes()).hexdigest() == before
    result["provider_executable_sha256"] = before
    assert sources == {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
                       for p in sorted((root / "src").rglob("*.py"))}
    result["source_sha256"] = sources
    result["probe_sha256"] = hashlib.sha256(pathlib.Path(__file__).read_bytes()).hexdigest()
    print(json.dumps(result, sort_keys=True))

if __name__ == "__main__":
    main()
