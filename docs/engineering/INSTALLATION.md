# Offline installation — 0.1.0 preview

Relay 0.1.0 is [MIT licensed](../../LICENSE). The public source is
[SuperDuperDave/agent-relay](https://github.com/SuperDuperDave/agent-relay). Choose the release-archive
or source-build route below. [Hosted checks](../CI.md#hosted-result) and the
[bounded native workflow](../PROVIDERS.md#bounded-native-workflow) have separate
scope. Consult [v0.1.0](https://github.com/SuperDuperDave/agent-relay/releases/tag/v0.1.0) for final package
and anonymous-onboarding results.

Use an ordinary x86-64 Linux/WSL account with Python 3.12 and Git. The installed
worker also requires Landlock ABI3+, procfs and supported filesystem birth times.
The exercised profile is WSL2 on ext4; see [support](../SUPPORT.md) for exact
versions and the additional namespace prerequisites for the tests and demo.

## Trust the source before running the installer

The reviewed bootstrap is the initial trust root. A release record pins that
bootstrap and the entire closed runtime together. Its SHA256 detects changed
bytes and identifies the approved selection; it is not a publisher signature
or independent proof that unknown code is safe.

Review the selected source commit and tests before executing its bootstrap.
For an archive, inspect the release's source binding and verification results.
Do not fetch arbitrary code and let that same code approve itself.

The installer is offline and stdlib-only. It never signs in, downloads code,
edits PATH/shell configuration, enrolls a repository or enables provider hooks.

## Install from the release archive

Use this route when the [v0.1.0 release](https://github.com/SuperDuperDave/agent-relay/releases/tag/v0.1.0) lists both
`relay-0.1.0-linux-x86_64.tar.gz` and its `.sha256` file under Assets.
Source publication and green CI alone do not establish a downloaded-package
check; read the release's package and onboarding results first.

Download both files into a new empty directory. For example, with curl installed:

```sh
mkdir relay-download
cd relay-download
curl --fail --location --remote-name https://github.com/SuperDuperDave/agent-relay/releases/download/v0.1.0/relay-0.1.0-linux-x86_64.tar.gz
curl --fail --location --remote-name https://github.com/SuperDuperDave/agent-relay/releases/download/v0.1.0/relay-0.1.0-linux-x86_64.tar.gz.sha256
sha256sum --check relay-0.1.0-linux-x86_64.tar.gz.sha256
tar -tvzf relay-0.1.0-linux-x86_64.tar.gz
```

Review the listed files under the single `relay-0.1.0/` root, then extract and
check the individual file hashes:

```sh
tar -xzf relay-0.1.0-linux-x86_64.tar.gz
cd relay-0.1.0
sha256sum --check SHA256SUMS
```

The installer is `runtime/bootstrap.py` and the bundle is `runtime/`.
Read the extracted README's source binding and runtime release identifier;
`RELEASE_SHA256` below means that reviewed full 64-character identifier, not
the archive checksum. After reviewing the source and bundle, inspect the plan:

```sh
/usr/bin/python3 -I -S -B runtime/bootstrap.py plan \
  --release runtime --approve-sha256 RELEASE_SHA256
```

Only for a first installation whose plan reports `expected_activation: null`:

```sh
/usr/bin/python3 -I -S -B runtime/bootstrap.py install \
  --release runtime --approve-sha256 RELEASE_SHA256 \
  --expected-activation none
```

For an upgrade, use the exact activation from a fresh plan instead of `none`.
Continue with [installed status](#inspect-the-actual-installed-command) and
[explicit repository initialization](#choose-and-initialize-a-repository).
The archive checksum and `SHA256SUMS` detect changed bytes; neither is a publisher
signature or independent approval of the code.

## Build a local preview bundle

From a reviewed source checkout:

```sh
/usr/bin/python3 -I -S -B src/relay_bootstrap.py build-release \
  --output dist/relay-preview --version 0.1.0
```

The destination must not already exist. The builder includes only bootstrap.py,
release.json and the exact approved runtime module set under payload/. It excludes
Git history, audit files, ledgers, credentials, tests and working notes.

The result deliberately says approved:false. Review the artifact and source
provenance before deciding to approve its release_id. Generating a digest is
not that approval. RELEASE_SHA256 below means the complete reviewed 64-character
digest, not a literal value to paste.

## Plan, then explicitly apply

```sh
/usr/bin/python3 -I -S -B src/relay_bootstrap.py plan \
  --release dist/relay-preview --approve-sha256 RELEASE_SHA256
```

Plan validates the supplied bundle and current command ownership without
creating installation state. It reports the bootstrap/runtime identities,
exact expected activation, account installation path, launcher path, and any
retained-selector pattern. Unknown existing relay commands are preserved and
refused, not silently replaced.

For the first installation, the plan's expected_activation is null:

```sh
/usr/bin/python3 -I -S -B src/relay_bootstrap.py install \
  --release dist/relay-preview --approve-sha256 RELEASE_SHA256 \
  --expected-activation none
```

For an upgrade, replace none with the exact current activation ID from a fresh
plan. A stale ID refuses; do not work around that by deleting configuration.

Account roots come from the OS account database, not HOME or XDG overrides.
The normal layout is ~/.local/share/relay/installation for immutable releases
and launch records, and ~/.local/bin/relay for the command selector. Existing
safe bin permissions are preserved. The installer does not change PATH.

## Inspect the actual installed command

Use the exact launcher path printed by plan/install. On the ordinary layout:

```sh
~/.local/bin/relay runtime status
```

The shell wrapper invokes absolute /usr/bin/python3 with -I -S -B. It opens
the installed bootstrap without following symlinks, retains and hashes the
bytes, then executes those same bytes. It does not import code from the target
checkout, depend on the build bundle remaining available, or trust PYTHONPATH.

Before any ledger command, the bootstrap confirms that its launch ID and
release pair are still active, verifies the closed payload, and installs the
retained-byte runtime importer. The actual installed command can be invoked
with source and bundle absent. A modified bootstrap refuses before execution.

## Choose and initialize a repository

After checking runtime status, choose an existing Git repository for Relay.
Replace the example path below with its absolute checkout path and use the
installed launcher printed by plan/install:

```sh
~/.local/bin/relay --repo /absolute/path/to/your/repository init
~/.local/bin/relay --repo /absolute/path/to/your/repository status
~/.local/bin/relay --repo /absolute/path/to/your/repository brief --agent codex
```

`init` explicitly creates the repository ledger and account enrollment. A later
`init` reopens that same healthy enrollment; it does not adopt unknown or copied
state. Registered linked worktrees share the repository's ledger. Installation
alone leaves projects unenrolled, and ordinary ledger commands refuse before
writing when enrollment is missing or ambiguous.

`status` shows active claims and waiting signals. `brief --agent codex` shows the
bounded context for that agent; use `claude` for the other inbox. Read the full
event and inspect its artifact before deliberately acknowledging work. A status
or brief read leaves pending signals unconsumed.

Hooks require a further opt-in. Continue with [provider setup](../PROVIDERS.md)
to generate and review invocation arguments. Initialization does not configure
providers, grant their permissions or approve native hook trust.

## Upgrades and explicit rollback

When upgrading v0.1.0 to a source-built bundle containing native peer support,
use the **incoming reviewed bundle's `bootstrap.py`** for `plan` and `install`.
Its reader recognizes exactly the original and expanded closed module sets;
the older bootstrap cannot read the expanded bundle. The source preview adds
the verified provider adapter without relaxing unknown-member refusal.

Rollback can reactivate the retained old release and its matching bootstrap.
Afterward, use the newer reviewed bootstrap if installation inspection or
uninstall must account for retained newer releases; the old manager does not
understand their expanded payload. Retain both bundles and recovery metadata.

Bootstrap and runtime are stored as one approved release pair. A unique launch
ID names a prepared record and deterministic wrapper for that pair. The one
published command symlink selects both together; bootstrap selection and
runtime selection are not separate commits.

Preparation records/wrappers remain open and are checked against their exact
expected bytes before and after publication. The current activation ID is
compared under the private installation lock. First publication is no-replace.
An upgrade uses atomic exchange and retains the displaced selector under a
hidden .relay-switch-ID name. It is not check-then-unlinked.

Keep these retained objects for recovery. They are not a second active
authority, and invoking an old inactive wrapper directly refuses. If
atomic exchange displaces an object that does not match the inspected previous
selector, it is preserved and the operation reports an uncertain outcome. Later
changes to retained artifacts are not universally detected.

To select a previously installed approved release:

```sh
~/.local/bin/relay runtime activate \
  --release-sha256 PREVIOUS_RELEASE_SHA256 \
  --expected-activation CURRENT_ACTIVATION_ID
```

Rollback gets a new activation ID, so A-to-B-to-A cannot revive a stale
expected ID. Coherent bootstrap changes using the supported release and launcher
protocol are tested. Future unsupported manifest/module/launcher protocols
must refuse until an explicitly compatible installer/migration is available;
arbitrary future bootstrap compatibility is not promised.

## Recover a damaged or interrupted installation

A working launcher can run runtime inspect. If it is damaged or absent, use a
separately reviewed source bootstrap, never the suspect installed bootstrap:

```sh
/usr/bin/python3 -I -S -B src/relay_bootstrap.py inspect
```

Inspection does not load candidate code, acquire an activation lock or create
installation state. It reports the active selection, verified/unverified
release and launch records, retained selector paths, issues and explicit
recovery options. Diagnosis is bounded to 256 entries per inspected directory.
An exceeded bound or invalid member is reported as degraded, not an empty
installation. Retained selectors are reported without being trusted or followed.

Read the JSON state and issues, not just exit status: a successful diagnostic
command can report degraded state. A valid active selection can coexist with
damaged inactive artifacts. The inventory is a best-effort observation, not
an authorization or atomic snapshot; each modifying command verifies again.

After an interrupted install, compare the current activation to the attempted
one. An absent receipt does not prove the selection stayed unchanged. If the
current valid pair is the intended pair, no retry is needed. Otherwise use
activate with a newly observed activation ID, or explicit none if no command
is present, as described below. Partial releases are not silently completed.

If the command is still a recognized owned Relay selector but its release or
launch record is damaged, inspect offers recover options for verified retained
releases. Review the selected release's provenance and use the exact selector
observation printed by inspection:

```sh
/usr/bin/python3 -I -S -B src/relay_bootstrap.py recover \
  --release-sha256 VERIFIED_RETAINED_RELEASE_SHA256 \
  --expected-selector CURRENT_SELECTOR_OBSERVATION
```

The observation is a 64-character identity digest of the selector, not a
secret or a provider token. A changed or missing selector refuses. Recovery
publishes a new launch ID for the selected verified release and retains the
old selector and damaged artifacts; it does not edit or execute damaged code.
An unrelated command or unrecognized symlink is not a recovery target.

### When no retained release is usable

The retained-release command above cannot select damaged code. Use a separately
reviewed source bootstrap and an externally reviewed, compatible replacement
bundle instead. The bootstrap being executed is the trust root; approving a
bundle digest does not authenticate that bootstrap. Nothing is fetched or
automatically approved by recovery.

```sh
/usr/bin/python3 -I -S -B src/relay_bootstrap.py recover-plan \
  --release dist/relay-replacement --approve-sha256 REPLACEMENT_RELEASE_SHA256
/usr/bin/python3 -I -S -B src/relay_bootstrap.py recover-install \
  --release dist/relay-replacement --approve-sha256 REPLACEMENT_RELEASE_SHA256 \
  --expected-selector CURRENT_SELECTOR_OBSERVATION
```

The plan is read-only. It requires an existing safe installation root and a
recognized owned selector, checks the lock, uninstall metadata and destination
containers, and reports the replacement identity and exact selector observation.
Apply captures the approved bundle before destination writes, checks again under
the installation lock, stages a complete release pair and publishes a fresh
launch ID through the same selector exchange. The displaced selector and all
damaged release/launch objects are retained. Enrollment, project DB/WAL/SHM,
claims, provider settings and unrelated commands are not recovery targets.

If the replacement digest's directory already exists and is damaged or partial,
both plan and apply refuse without repairing or overwriting it. Prepare and
independently review a bundle with a distinct release identity, for example a
new reviewed version label. Re-labeling is not an approval or a fix for unsafe
code. Do not delete the damaged directory to force reuse of its identity.

After interruption, inspect before retrying. A reserved but incomplete release
remains preserved and blocked. A complete verified staged release can be reused
while the exact old selector is still current. If publication already occurred,
the old observation refuses; confirm the intended active pair instead of
repeating the operation. A last-moment foreign selector substitution is retained
and reported as uncertain, not silently counted as success.

If the selector is absent, inspect and use the normal plan/install or activate
workflow with explicit expected-activation none. Unknown command occupants,
unsafe/missing installation roots with a remaining selector, incomplete uninstall
operations and unavailable trusted external recovery code require diagnosis;
this command does not rebuild roots, restore backups, repair ledgers or rebind
repositories. Preserve artifacts rather than deleting folders to force a retry.

## Disable and explicitly re-enable the command

Disable is reversible command removal, not a full artifact uninstall. It does
not delete releases, launch records, account enrollments, project ledgers or
provider hooks, and it does not stop commands that are already running. Stop
new work and remove any separately configured hook invocation first; automatic
provider trust/settings cleanup is not performed. With invocation-only opt-in,
start subsequent provider processes without the generated Relay arguments.

```sh
~/.local/bin/relay runtime disable-plan
~/.local/bin/relay runtime disable --expected-selector CURRENT_SELECTOR_OBSERVATION
```

The plan is read-only and checks the existing installation root and lock without
creating or locking anything. Known lock damage or an unavailable required root
reports can_disable:false with issues; a missing lock in a safe writable root
may be created during apply. Disable requires that exact observed recognized
selector and an existing safe installation root. It moves the command symlink without
replacement to a unique .relay-disabled-ID beside it and returns the retained
absolute path. No file is unlinked. Unknown preexisting commands refuse unchanged.

A same-account replacement at the instant of the move can itself be moved,
but it is retained and the operation reports uncertainty with its retained name.
It is not deleted or silently counted as successful disabling. Later changes
are not universally detected. Inspect after interruption; a stale/missing
observation refuses instead of silently retrying an already completed move.

With the normal command absent, use the reviewed source bootstrap to inspect
and select a verified retained release:

```sh
/usr/bin/python3 -I -S -B src/relay_bootstrap.py inspect
/usr/bin/python3 -I -S -B src/relay_bootstrap.py activate \
  --release-sha256 VERIFIED_RETAINED_RELEASE_SHA256 \
  --expected-activation none
```

Re-enabling gets a new launch ID. Direct invocation of an old inactive wrapper
refuses; moving an old symlink back is not the supported recovery workflow.
For actual installed-code removal, use the separate [uninstall workflow](UNINSTALL.md).
It removes verified code and selectors while preserving project/account data
and non-code lock/receipt metadata. It is not a provider-settings or metadata purge.

## Installation and ledger evidence

The [initialization steps](#choose-and-initialize-a-repository) make repository
enrollment explicit. The evidence below covers that installed command boundary;
it does not extend a successful runtime status into permission to write another
project's ledger.

The current ledger worker requires x86-64 Linux, procfs, Landlock ABI3+ and
supported statx creation times. The tested full ledger profile is ext4 on WSL2,
Python3.12.3 and SQLite3.45.1. See [ledger admission](LEDGER-ADMISSION.md).

The rootless namespace proof now exercises public installation and full ledger
commands with actual account defaults, source and bundle absent. It covers
explicit init/reopen, claims and exact ownership, registered linked worktrees,
idempotent decisions/ACKs, lifecycle input and direct SQLite integrity/count.
These are fixture commands, not actual provider sessions or hook integration.

The genuine null device appears owned by UID65534 in that namespace. Independent
kernel review established that this owner presentation does not identify the
null driver. Only that owner predicate was removed; exact held character type,
major1/minor3 and WRITE_FILE-only confinement remain. See
[device identity and evidence](NULL-DEVICE-IDENTITY.md). No administrative account
change or successful file-metadata mock was used to produce the public proof.

## Interrupted operations and remaining limits

An initialized repository moved with the same physical Git/state/ledger objects
uses [explicit rebind](REBIND.md), not installation recovery or another init.
Rebind changes account metadata only after an operator-quiesced move. It pins
the exact successor before selecting it, preserves pending work and claims,
and refuses copied/restored ledgers or ambiguous transition history. Existing
provider arguments and linked-worktree Git pointers are not automatically repaired.

Inspect current runtime status before any retry. A process can die after a
new complete selector becomes visible but before reporting success or finishing
its final directory sync. Never interpret a missing receipt as proof that
nothing changed.

Before publication, a complete or partial release/launch may remain without
becoming active. Existing complete approved releases are verified for reuse;
partial/unknown candidates are not adopted or overwritten. Old releases,
orphan launch records and displaced selectors are deliberately retained.

Inspection, recognized-selector recovery and reversible disable/re-enable are
implemented and exercised through public commands in an isolated OS account.
Verified installed-code removal and reinstall now preserve an actually enrolled
fixture ledger and its active claim, with process-cut retry tests and direct
SQLite checks. See [uninstall scope and evidence](UNINSTALL.md).
External-bundle recovery also passes when the only installed release is damaged:
public commands preserve actual enrollment, DB/WAL/SHM bytes, a work intent and an
active claim, then the recovered launcher reopens the same ledger with source
and bundle absent. Direct SQLite checks confirm integrity and event count.
Independent abrupt-exit and selector-substitution tests supplement this witness.
See the source-bound [latest test receipt](development-test-result.json); older
native/public receipts retain their original tested hashes and are not reissued
as evidence for a changed bootstrap.

Rebind supports same-object moves only, not cross-filesystem migration or backup
restoration.
Recovery always needs trustworthy executable code outside a damaged
installation; no digest removes that requirement. This preview does not delete
ledgers, repair unknown state automatically or establish physical power-loss
guarantees. Remaining release gates are not tasks for users to solve by blindly
removing directories.

The installed provider-hook contract and readonly provider-config generator now
have isolated evidence. Configuration is an invocation-argument plan, not a
settings installer or provider launcher. Historical credential-free probes
verify Codex discovery and Claude startup only; the
[bounded native workflow](../PROVIDERS.md#bounded-native-workflow) has separate
authenticated coding/review evidence. Follow that guide for the public walkthrough.
Installation still changes no provider settings or trust.
