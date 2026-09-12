# Multithread

**Native collaboration for coding agents.**

A Mainthread project · [Project page](https://mainthread.ai/work/relay/)

Give your work additional threads while keeping its purpose, ownership, and
results connected.

Multithread lets coding agents coordinate across sessions and Git worktrees.
Codex can call Claude Code for a scoped task, receive its answer and continue
working.
A durable local ledger keeps resource claims, commit-backed handoffs and explicit
acknowledgements available across interruptions.

Multithread was previously called **Agent Relay**. Existing installations keep
using `relay`; release filenames and the
[repository URL](https://github.com/SuperDuperDave/agent-relay) remain unchanged.
The guides retain Relay where they describe that runtime and its recorded
evidence.

- Call Claude through your existing provider installation and sign-in.
- Claim shared resources with exact ownership that survives interrupted sessions.
- Hand off an immutable Git commit and keep it pending until acknowledged.
- Recover pending work after a restart; notifications and reads never consume it.

**0.2.0 preview.** Includes installed `relay setup`, `relay launch`,
`relay peer claude` and `relay update` commands. Installed native call/return and
exact-session resume were exercised on an existing Linux/WSL2 developer profile.
See [native peer calls and verification scope](docs/PEER.md) and
[hosted checks](docs/CI.md). These observations do not establish fresh-account
onboarding or broader platform support.

[MIT licensed](LICENSE), copyright (c) 2026 David Jones. Created by David Jones
with AI assistance: Codex contributed implementation, testing and release work;
Claude contributed native review.

## Install for your project

Use an ordinary **x86-64 Linux** account with Python 3.12 at `/usr/bin/python3`,
Git at `/usr/bin/git`, Landlock ABI3+, procfs and supported filesystem birth times.
WSL2 with ext4 is the exercised profile. See [support](docs/SUPPORT.md).
Use your existing, functioning Codex/Claude installation and provider access;
Relay does not provision them.

From the Git repository you want to use, run this publisher's installer:

```sh
relay_setup=$(mktemp) &&
  curl -fsSL --proto '=https' \
    https://github.com/SuperDuperDave/agent-relay/releases/latest/download/install.py \
    -o "$relay_setup" &&
  /usr/bin/python3 -I -S -B "$relay_setup" --enroll-repo "$PWD"
```

Trust and review the [publisher and release](https://github.com/SuperDuperDave/agent-relay/releases/latest)
before running its code. The installer verifies its pinned package, shows the
selected release and repository scope, and asks you to type `install` once.
Checksums bind the selected bytes; they are not publisher signatures.
It installs account-local code, explicitly enrolls this project and prints
readiness and exact next commands. A matching healthy installation is reused.
Provider sign-ins, settings and permissions stay unchanged.

Or give your coding agent the [setup prompt](docs/SETUP.md#ask-your-coding-agent).
It covers installation, enrollment and launch preparation without requiring a
Relay source checkout. The [setup guide](docs/SETUP.md) also covers pinned
versions, partial setup and updates.

Check readiness again at any time:

```sh
~/.local/bin/relay setup --repo "$PWD" --check
```

This is read-only. It distinguishes runtime/repository readiness from prepared
or missing providers. Authentication, hook delivery and provider tools remain
unchecked until observed in a native session.

## Collaborate

A thread is a separately scoped agent workstream; the main thread carries the
continuing objective. You might ask an agent to “multithread this investigation”
or “use up to three additional threads.” These are plain-language instructions
about intent and capacity, with a ceiling rather than a quota. They are not CLI
commands or a built-in scheduler; execution depends on available tools, supported
providers and authorized use.

Follow [Call Claude and continue your task](docs/PEER.md) for the automatic
call/return workflow, task scope, results and exact-session follow-up. It uses
Claude's normal environment and permissions. Existing subscription sign-in can
be used; the provider's configuration determines the billing path.

To start an interactive Codex or Claude session with Relay hooks, use the exact
launch command printed by setup, or:

```sh
~/.local/bin/relay launch codex --repo "$PWD"
```

Use `claude` for the other provider. Review the invocation and type `launch`;
adding `--json` prepares the plan without starting a session. A peer call's
`--json` **does execute the call**; use its `--dry-run` to inspect first.
Provider usage must be authorized before calling a peer.

The [manual two-worktree walkthrough](docs/PROVIDERS.md#try-a-review-across-two-worktrees)
remains available for separate sessions with manual wakes and explicit Git work.

## Update

```sh
~/.local/bin/relay update
```

The updater checks the published release and asks once before applying a new
selection. Use `--check` or `--json` to inspect without applying; the JSON result
provides an exact command for an authorized agent to apply an available update.
Updates are explicit, with no background updater. See
[update details](docs/SETUP.md#update-an-existing-installation) for active work,
version selection and rollback.

## Try the no-account demo

The optional demo uses scripts and real SQLite without provider accounts or an
existing Relay installation. It reproduces a bug, commits a fix, recovers a
missed handoff after restart, rejects conflicting claims and reviews the exact
commit in another worktree. It ends with
`PASS: 6 real ledger events; SQLite integrity ok.`

Clone and review the source, then run from its root:

```sh
git clone https://github.com/SuperDuperDave/agent-relay.git
cd agent-relay
/usr/bin/python3 -I -S -B examples/no_account_demo.py
```

The demo additionally requires Bubblewrap at `/usr/bin/bwrap` and enabled
rootless/nested namespaces. These are extra demo/test prerequisites. See the
[demo guide](docs/DEMO.md) for isolation and expected output.

## Further reading

Relay coordinates cooperating agents under one trusted local OS account. Claims
do not grant permissions, and acknowledgement records consumption rather than
approval to merge or deploy. Failed state access is unknown state; claims have
no automatic expiry. Native Windows, macOS, ARM Linux and cross-machine
coordination are outside the exercised profile.

- [Setup and readiness](docs/SETUP.md)
- [Architecture and tradeoffs](docs/ARCHITECTURE.md)
- [Offline installation, source builds and rollback](docs/engineering/INSTALLATION.md)
- [Code uninstall](docs/engineering/UNINSTALL.md) and [repository rebind](docs/engineering/REBIND.md)
- [Supported environments and troubleshooting](docs/SUPPORT.md)
- [CI and checkout verification](docs/CI.md)
- [Release acceptance requirements](docs/engineering/REQUIREMENTS.md)
- [Publication checks and evidence boundaries](docs/engineering/PUBLICATION.md)
