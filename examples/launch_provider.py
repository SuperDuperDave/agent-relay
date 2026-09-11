#!/usr/bin/env python3
"""Review Relay's invocation-only hooks and start a normal interactive provider.

This operator convenience example requires an installed Relay and an enrolled
checkout. It does not install providers, edit settings, grant trust, deliver
tasks, or prove provider readiness. --json only prepares a launch plan.
"""

import argparse
import json
import os
from pathlib import Path
import platform
import shlex
import shutil
import subprocess
import sys
import tomllib


class LaunchError(Exception):
    pass


def check_native_arguments(client, command, arguments):
    """Accept only the invocation-only hook shape emitted by public schema 1."""
    events = ["SessionStart", "UserPromptSubmit", "Stop", "SessionEnd"]
    if client == "codex":
        events.append("Interrupt")
    expected = {name: [{"hooks": [{"type": "command", "command": command, "timeout": 3}]}]
                for name in events}
    try:
        if client == "claude":
            if len(arguments) != 2 or arguments[0] != "--settings":
                raise ValueError()
            parsed = json.loads(arguments[1])
        else:
            if len(arguments) != 2 * len(events):
                raise ValueError()
            hooks = {}
            for index in range(0, len(arguments), 2):
                if arguments[index] != "-c":
                    raise ValueError()
                entry = tomllib.loads(arguments[index + 1])
                value = entry.get("hooks")
                if set(entry) != {"hooks"} or not isinstance(value, dict) or set(hooks) & set(value):
                    raise ValueError()
                hooks.update(value)
            parsed = {"hooks": hooks}
        if parsed != {"hooks": expected}:
            raise ValueError()
    except (ValueError, TypeError, RecursionError):
        raise LaunchError("Native arguments do not match the invocation-only hook plan; no provider was started.") from None


def executable(value, name):
    selected = str(value) if value is not None else shutil.which(name)
    if not selected:
        raise LaunchError(f"{name} was not found; select its reviewed absolute path with --provider.")
    path = Path(selected)
    if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
        raise LaunchError(f"{name} must be an executable at an absolute path.")
    # Preserve the entry path: resolving a provider's symlink can change how its
    # ordinary launcher finds companion executables or selects configuration.
    return str(path)


def prepare(client, repo, relay, provider):
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        raise LaunchError("Relay v0.1.0 requires a supported x86-64 Linux environment; see docs/SUPPORT.md.")
    # Relay must see the supplied components before any normalization: resolving
    # an alias here would erase a symlink its enrollment boundary should refuse.
    checkout = Path(repo)
    if not checkout.is_absolute():
        checkout = Path.cwd() / checkout
    if not checkout.is_dir():
        raise LaunchError("--repo must identify the enrolled checkout directory.")
    if relay is None:
        import pwd
        relay = Path(pwd.getpwuid(os.getuid()).pw_dir) / ".local/bin/relay"
    launcher = executable(relay, "Relay")
    provider_path = executable(provider, client)
    command = [launcher, "--repo", str(checkout), "--json", "provider-config", "--client", client]
    try:
        result = subprocess.run(command, cwd=checkout, stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=15, check=False)
    except subprocess.TimeoutExpired:
        raise LaunchError("Relay configuration timed out; observation is unavailable. Inspect installed status before retrying.") from None
    except UnicodeError:
        raise LaunchError("Relay returned unreadable configuration output; observation is unavailable.") from None
    if result.returncode != 0:
        # Give the exact native command for diagnosis without copying arbitrary
        # diagnostics into the structured launch plan.
        raise LaunchError(
            f"Relay refused launch preparation (exit {result.returncode}); run: {shlex.join(command)}")
    try:
        plan = json.loads(result.stdout)
    except (ValueError, RecursionError):
        raise LaunchError("Relay did not return a valid configuration plan; no provider was started.") from None
    if not isinstance(plan, dict) or type(plan.get("schema")) is not int or plan["schema"] != 1:
        raise LaunchError("Unsupported configuration plan; no provider was started.")
    if plan.get("provider") != client or plan.get("repo") != str(checkout):
        raise LaunchError("The configuration plan does not match this provider and checkout.")
    if any(plan.get(key) is not False for key in
           ("launches_provider", "changes_provider_settings", "changes_permissions")):
        raise LaunchError("The configuration plan exceeds invocation-only setup.")
    hook_command = plan.get("hook_command")
    if not isinstance(hook_command, str):
        raise LaunchError("The configuration plan has an invalid hook command.")
    try:
        hook = shlex.split(hook_command)
    except ValueError:
        raise LaunchError("The configuration plan has an invalid hook command.") from None
    if hook != [launcher, "--repo", str(checkout), "provider-hook", "--client", client]:
        raise LaunchError("The hook command does not match the selected Relay and checkout.")
    arguments = plan.get("native_arguments")
    if (not isinstance(arguments, list) or not arguments
            or any(not isinstance(arg, str) or "\0" in arg for arg in arguments)):
        raise LaunchError("The configuration plan has invalid native arguments.")
    check_native_arguments(client, hook_command, arguments)
    return {"schema": 1, "state": "launch_prepared", "provider": client,
            "repo": str(checkout), "argv": [provider_path, *arguments],
            "relay_plan": plan, "provider_started": False,
            "hook_delivery": "unknown", "provider_tools": "unknown"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("client", choices=("codex", "claude"))
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="enrolled checkout; default: current directory")
    parser.add_argument("--relay", type=Path, help="reviewed absolute installed launcher; default: OS-account installation")
    parser.add_argument("--provider", type=Path, help="reviewed absolute provider entry point; default: PATH lookup")
    parser.add_argument("--json", action="store_true", help="print a plan without starting a provider or asking for input")
    args = parser.parse_args(argv)
    try:
        plan = prepare(args.client, args.repo, args.relay, args.provider)
        if args.json:
            print(json.dumps(plan, sort_keys=True))
            return 0
        print(json.dumps(plan, indent=2))
        print("Review the provider path, checkout and Relay hook command above.")
        print("Existing provider settings and permissions remain in effect. Native hook trust is a separate step.")
        if not sys.stdin.isatty():
            raise LaunchError("Use --json to prepare a plan here, or run this command in an interactive terminal to launch.")
        if input("Type launch to start this provider: ").strip() != "launch":
            print("Provider was not started.")
            return 0
        # Normal inherited environment and terminal: no provider sandbox wrapper,
        # credentials/config relocation, shell eval, or permission-policy flags.
        return subprocess.call(plan["argv"], cwd=plan["repo"])
    except (LaunchError, OSError, KeyError) as exc:
        message = str(exc) if isinstance(exc, LaunchError) else "A selected path or command is unavailable; check your arguments."
        if args.json:
            print(json.dumps({"schema": 1, "state": "unavailable", "provider_started": False,
                              "hook_delivery": "unknown", "provider_tools": "unknown", "message": message}))
        else:
            print(f"relay launch: {message}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("relay launch: interrupted; inspect the provider if it had already started.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
