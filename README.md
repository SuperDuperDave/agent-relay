# Relay

When coding agents work in separate sessions or Git worktrees, a conversation
can lose track of who owns a shared resource and which change needs review.
Relay gives Codex and Claude a durable local coordination ledger for that work.

- Claim shared resources with exact ownership that survives interrupted sessions.
- Hand off an immutable Git commit and keep it pending until deliberately acknowledged.
- Recover pending work after a restart; repeated notifications and reads never consume it.
- Refresh agent context through optional Codex and Claude hooks.

**0.1.0 preview.** The installed runtime, scripted demo and a bounded native
Codex/Claude review workflow are exercised on the documented Linux/WSL2 profile.
See the [provider walkthrough and scope](docs/PROVIDERS.md#bounded-native-workflow).
The public source is [SuperDuperDave/agent-relay](https://github.com/SuperDuperDave/agent-relay).
See [hosted checks](docs/CI.md#hosted-result) for the tested commit and
[v0.1.0](https://github.com/SuperDuperDave/agent-relay/releases/tag/v0.1.0) for package and onboarding results.
[MIT licensed](LICENSE), copyright (c) 2026 David Jones.

Created by David Jones with AI assistance. Codex contributed implementation,
testing and release work; Claude performed the bounded native review described
in the provider walkthrough.

## Set up for your project

Start with the [human and coding-agent setup guide](docs/SETUP.md). It connects
release installation, repository enrollment, ledger checks and provider launches
and explains what is verified at each step. Paste this into a coding agent in
the Git repository you want to use:

```text
Set up Agent Relay v0.1.0 for this repository following
https://github.com/SuperDuperDave/agent-relay/blob/main/docs/SETUP.md.
Check compatibility first, then install the reviewed release, enroll this
repository and prepare launches for my existing providers. Preserve existing
work and permissions. Report verified readiness and the exact remaining steps;
provider launches and native trust remain separate actions.
```

The current runtime requires a compatible **x86-64 Linux** environment; WSL2 is
the documented Windows route. The demo below also requires Bubblewrap and nested
namespaces; those are not prerequisites for normal Relay installation.

After setup, the [interactive launch helper](examples/launch_provider.py) finds
the selected provider and supplies the reviewed hooks without editing its
settings. `--json` prepares a machine-readable plan without starting a provider.

## Try the no-account demo

First check the [platform prerequisites](docs/SUPPORT.md#exercised-profile),
then get the source:

```sh
git clone https://github.com/SuperDuperDave/agent-relay.git
cd agent-relay
```

Review the checkout before running its code. From the repository root, run:

```sh
/usr/bin/python3 -I -S -B examples/no_account_demo.py
```

The demo reproduces a bug, commits a fix, recovers a missed handoff after a
restart, rejects conflicting claims, reviews the exact commit in a second
worktree and verifies one explicit acknowledgement in real SQLite. It ends with
`PASS: 6 real ledger events; SQLite integrity ok.` The actors are scripts;
provider accounts and an existing Relay installation are unnecessary.

Use an ordinary x86-64 Linux/WSL2 account with Python 3.12, Git, bubblewrap at
`/usr/bin/bwrap` and enabled rootless/nested namespaces. The runtime also requires
Landlock ABI3+, procfs and supported filesystem birth times; the exercised
filesystem is ext4. The [demo guide](docs/DEMO.md) explains isolation and expected
output; [support](docs/SUPPORT.md) covers platform prerequisites and diagnosis.

## Build and install the preview

The [installation guide](docs/engineering/INSTALLATION.md#install-from-the-release-archive)
explains the release archive and checksum route. To build from source instead,
review the checkout before running its bootstrap, then create an offline bundle
in a new directory:

```sh
/usr/bin/python3 -I -S -B src/relay_bootstrap.py build-release \
  --output dist/relay-preview --version 0.1.0
```

The JSON output includes `release_id` and `approved: false`. Inspect the bundle
and source before approving that release. Replace `RELEASE_SHA256` below with
its reviewed 64-character `release_id`, then inspect the read-only plan:

```sh
/usr/bin/python3 -I -S -B src/relay_bootstrap.py plan \
  --release dist/relay-preview --approve-sha256 RELEASE_SHA256
```

For a first installation whose plan reports `expected_activation: null`, apply:

```sh
/usr/bin/python3 -I -S -B src/relay_bootstrap.py install \
  --release dist/relay-preview --approve-sha256 RELEASE_SHA256 \
  --expected-activation none
```

Use the exact launcher path printed by the plan. With the ordinary account
layout, inspect the installation and explicitly initialize your chosen Git
repository; replace `/absolute/path/to/your/repository` first:

```sh
~/.local/bin/relay runtime status
~/.local/bin/relay --repo /absolute/path/to/your/repository init
~/.local/bin/relay --repo /absolute/path/to/your/repository status
~/.local/bin/relay --repo /absolute/path/to/your/repository brief --agent codex
```

Installation writes account-owned code; `init` separately creates the chosen
repository's ledger and enrollment. Linked worktrees share that ledger.
Provider hooks are a further opt-in: follow [provider setup](docs/PROVIDERS.md)
to review invocation arguments and native trust. Installation and enrollment
leave provider settings unchanged.

For existing installations, follow the guide's exact-activation
[upgrade and rollback steps](docs/engineering/INSTALLATION.md#upgrades-and-explicit-rollback).
It also covers recovery and disabling. [Code uninstall](docs/engineering/UNINSTALL.md)
preserves project data; [repository rebind](docs/engineering/REBIND.md) supports
explicit moves of the same physical repository objects.

## Verify the checkout

On the supported test profile:

```sh
/usr/bin/python3 -I -S -B tests/run_tests.py --suite all --strict
```

The runner uses temporary homes and disposable repositories, disables inherited
Git configuration/hooks and supplies no provider credentials. It does not enroll
this checkout or launch providers. Strict mode rejects skipped or incomplete
tests. Its environment isolation is not a host filesystem/network sandbox;
individual namespace tests provide their own confinement.
See [CI and verification](docs/CI.md) and the source-bound
[test receipt](docs/engineering/development-test-result.json).

## Scope and further reading

Relay coordinates cooperating agents under one trusted local OS account.
Claims do not grant permissions, and an acknowledgement records consumption
rather than approval to merge or deploy. Failed state access is unknown state;
claims have no automatic expiry. Native Windows, macOS, ARM Linux and
cross-machine coordination are outside the exercised profile. Real provider
use requires your own Codex and Claude access.

- [Architecture and tradeoffs](docs/ARCHITECTURE.md)
- [Provider setup and the two-worktree walkthrough](docs/PROVIDERS.md)
- [Supported environments and troubleshooting](docs/SUPPORT.md)
- [Release acceptance requirements](docs/engineering/REQUIREMENTS.md)
- [Publication checks and evidence boundaries](docs/engineering/PUBLICATION.md)
