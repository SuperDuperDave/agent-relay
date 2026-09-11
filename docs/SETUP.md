# Set up Agent Relay

Use this guide to install the **v0.1.0 preview**, enroll a Git repository and
prepare Codex and Claude launches. Start in the repository you want the agents
to work on, using an ordinary x86-64 Linux account with a functioning provider
environment. WSL2 is the documented Windows route.

The installed runtime requires Python 3.12 at `/usr/bin/python3`, Git, Landlock
ABI3+, procfs and supported filesystem birth times; ext4 is exercised. Native
Windows, native macOS and ARM Linux are unsupported by this write profile.
Bubblewrap and rootless/nested namespaces are **extra demo/test prerequisites**,
not a requirement to install Relay. See the [exact support scope](SUPPORT.md#exercised-profile).

## Ask your coding agent

Paste this into a functioning coding agent already working in your chosen Git
repository. It authorizes installation and enrollment; provider launches remain
a separate action.

```text
Set up Agent Relay v0.1.0 for this Git repository using
https://github.com/SuperDuperDave/agent-relay/blob/main/docs/SETUP.md.
Follow its release-archive route. I authorize the reviewed account-local Relay
installation, this repository's enrollment, and launch preparation for my
existing Codex and Claude installations. Confirm the Git root/common directory
and prerequisites first. Preserve existing ledger work, provider settings,
sign-ins and permissions; do not replace a different active release or unknown
command. Do not provision providers/OS settings or launch model sessions.
Finish with the readiness stages, exact next launch commands and any missing
prerequisite or native action I must complete.
```

The same steps below work for a human operator. An agent should continue through
the authorized steps it can verify, then report the specific next action for
anything missing. If the intended repository is ambiguous, identify it with the
operator before enrollment.

## 1. Identify the repository and prerequisites

From the intended checkout, inspect:

```sh
git rev-parse --show-toplevel
git rev-parse --path-format=absolute --git-common-dir
uname -sm
id -u
/usr/bin/python3 --version
git --version
```

Record the absolute checkout and common directory. Linked worktrees must resolve
to the same common directory to share coordination. Use the normal OS account;
the installer derives its account paths from the OS account database, not a
substituted `HOME` value.

Locate any existing Relay installation and provider executables. Inspect unknown
commands before executing them. For reviewed Codex/Claude executables, record
their paths and versions without reading credential files. Missing providers do
not prevent Relay installation or enrollment; prepare only the available ones.
The native workflow evidence covers Codex 0.153.4 and Claude 2.1.267, so record
version differences instead of assuming equivalent hook behavior.

## 2. Install the reviewed release archive

Follow [Install from the release archive](engineering/INSTALLATION.md#install-from-the-release-archive)
using the two assets on the [v0.1.0 release](https://github.com/SuperDuperDave/agent-relay/releases/tag/v0.1.0).
Download to a new empty directory, check the archive checksum, inspect its file
list, then extract and check `SHA256SUMS`. Review the extracted source binding
and bundle before running its `runtime/bootstrap.py`.

Use the documented `plan` command with the reviewed runtime `release_id`.
This identifier is different from the archive checksum. The plan provides the
exact launcher path and expected activation. For a first installation,
`expected_activation: null` permits the documented `install` command with
`--expected-activation none`. Reuse an already healthy matching installation.
Replacing a different active release needs an explicit operator decision and a
fresh [upgrade plan](engineering/INSTALLATION.md#upgrades-and-explicit-rollback).
Preserve unknown commands or uncertain state and use the documented inspection
and recovery path.

Keep the launcher path from the plan; the installer does not edit `PATH`.
Set these two variables to the actual paths before continuing:

```sh
relay_launcher='/absolute/path/from/installation-plan/relay'
relay_checkout='/absolute/path/to/your/repository'
"$relay_launcher" runtime status
```

Read the returned state and runtime identity. A command's successful exit alone
does not establish a healthy installation.

## 3. Explicitly enroll this repository

```sh
"$relay_launcher" --repo "$relay_checkout" init
"$relay_launcher" --repo "$relay_checkout" --json doctor
"$relay_launcher" --repo "$relay_checkout" status
"$relay_launcher" --repo "$relay_checkout" brief --agent codex
"$relay_launcher" --repo "$relay_checkout" brief --agent claude
```

`init` creates this repository's ledger and account enrollment, or reopens the
same healthy enrollment. Installation alone does not enroll a project. Preserve
existing claims and pending work; setup needs no test handoff, acknowledgement
or claim release. A refusal or unreadable state remains unresolved; do not
delete state or forge enrollment markers to make it pass.

Read `doctor`'s `ok`, integrity and path identity results. It checks the admitted
ledger; it does not diagnose provider authentication, tools or hook delivery.

## 4. Prepare invocation-only provider launches

For each available provider, generate and review its plan:

```sh
"$relay_launcher" --repo "$relay_checkout" --json provider-config --client codex
"$relay_launcher" --repo "$relay_checkout" --json provider-config --client claude
```

The [launch helper](../examples/launch_provider.py) discovers a provider on
`PATH` and passes its generated `native_arguments` as an argv list. It comes
with the current Relay source checkout, **not the v0.1.0 runtime archive**.
If needed, clone the source into a new directory outside the enrolled project:

```sh
git clone https://github.com/SuperDuperDave/agent-relay.git agent-relay-source
cd agent-relay-source
```

Review the helper before running it. From that source checkout, prepare a launch
without starting a provider:

```sh
/usr/bin/python3 -I -S -B examples/launch_provider.py codex \
  --repo "$relay_checkout" --relay "$relay_launcher" --json
```

Use `claude` for the other provider. Add `--provider /absolute/path/to/provider`
to select a reviewed executable explicitly. `--json` prints the plan and never
launches a provider. To start an interactive session, rerun without `--json`:
the helper displays the plan and requires you to type `launch`. It retains the
provider's normal environment and permissions. Keep the source helper's absolute
path in any launch command prepared for use from a different directory.

The [lower-level provider example](PROVIDERS.md#invocation-only-opt-in) shows the
same argv-based launch directly. Never pass the JSON through shell `eval`.

Preserve normal provider sign-in, permissions and unrelated settings. Review
existing hooks to avoid duplicate Relay handlers or conflicting overrides.
Preparing arguments does not activate hooks in an already-running session.
The next provider process needs those arguments and any native hook review;
inspect the exact displayed command before granting trust. Relay does not
install providers, repair their execution environment or bypass their policies.

An agent performing setup should give the operator exact launch commands without
starting another model session unless that launch is separately authorized.
Use normal provider interfaces for any sign-in or native trust action.

## 5. Report readiness and the next action

Report each stage separately, with the relevant local command result. Retain
receipts locally; review and sanitize them before sharing.

| Stage | Evidence to report | If incomplete |
|---|---|---|
| Runtime verified | Healthy installed status and reviewed release identity | Install a missing prerequisite through the normal administration process, or inspect the specific installation refusal. Landlock/filesystem failures require a compatible environment; there is no permissive fallback. |
| Repository verified | Exact Git identity, successful enrollment, healthy `doctor`, status and both briefs | Identify the intended repository or follow the documented enrollment/recovery action while preserving state. |
| Provider launch prepared | Reviewed executable path/version and matching invocation plan | For a missing executable, use the provider's normal installation process, then prepare the invocation. Report a missing provider independently of Relay readiness; sign-in is a separate native action. |
| Hook/context delivery observed | Actual native session observation and model-visible Relay identity/contract | Launch normally with the reviewed arguments, complete any native sign-in/trust action, and inspect actual delivery. A generated plan or zero hook exit does not establish delivery. |
| Provider tools observed | An explicitly authorized, harmless file read/write in that provider session | Resolve provider capability through its normal support path; an installation check does not prove tool execution. |

Mark unobserved stages **not yet checked**. Hook delivery and tool execution
need separate observation; neither establishes that a coding/review workflow
completed. A harmless capability check can use a disposable file without
publishing a handoff or changing existing claims.

When ready, try the [two-worktree review walkthrough](PROVIDERS.md#try-a-review-across-two-worktrees)
with a small bounded task, manual wakes and explicit Git operations. The
[no-account demo](DEMO.md) is an optional scripted introduction on its additional
namespace profile. It does not establish provider readiness.
