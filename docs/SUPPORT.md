# Support and troubleshooting

Relay 0.1.0 is an [MIT-licensed](../LICENSE) preview exercised on the Linux/WSL2
profile below. Installation, scripted demonstration and bounded native provider
work have separate evidence; hosted CI and final public release validation
remain pending.

## Exercised profile

The recorded installed-runtime and no-account witnesses use x86-64 Linux under
WSL2, ext4, Python 3.12.3, SQLite 3.45.1, Git 2.43.0 and bubblewrap 0.9.0.
The installed worker requires Landlock ABI3 or newer, procfs, supported statx
creation-time evidence and the reviewed Linux syscall/device behavior.
It runs as an ordinary trusted OS user, not root.

The complete tests/demo additionally require /usr/bin/python3, /usr/bin/git,
/usr/bin/bwrap, a POSIX shell and enabled unprivileged user, mount, PID and
network namespaces. Nested namespaces must be permitted for nested witnesses.
An LSM policy, container boundary or host restriction can deny them even when
the bwrap executable is installed.

For one concrete policy mechanism, see Ubuntu's
[AppArmor namespace restrictions](https://documentation.ubuntu.com/security/security-features/privilege-restriction/apparmor/).
The appropriate policy depends on the environment; this preview does not alter it.

Fixture namespaces map their ordinary account to UID/GID1000. This is not an
instruction to renumber the host user. Other host profiles need their own
execution evidence. Native Windows, macOS, ARM Linux, network filesystems,
containers and GitHub-hosted runners are not verified merely by being listed as
possible environments. The hosted workflow remains unexecuted until publication.

No provider account is needed for the complete default suite or scripted demo.
The [native walkthrough and evidence](PROVIDERS.md#bounded-native-workflow) cover
Codex 0.153.4 and Claude 2.1.267 with manual reviewer wakes, explicit author task
instructions and controller Git commits. Trying that workflow requires your own provider
access and reviewed permissions; the historical credential-free probes retain
their narrower scope.

## Diagnose without changing live configuration

For a first look, run the [no-account demo](DEMO.md) on the supported profile.
The [strict suite](CI.md) checks the full local acceptance contract. Both use
disposable fixtures without enrolling this checkout or enabling provider hooks.
For an installed command, start with `~/.local/bin/relay runtime status`, using
the exact launcher path printed during installation if it differs. Project
status requires the separately [initialized repository](engineering/INSTALLATION.md#choose-and-initialize-a-repository).

| Observation | Meaning and next action |
|---|---|
| Missing /usr/bin/bwrap | The namespace test dependency is absent. Install the distribution package through your normal reviewed administration process; do not substitute a random downloaded binary. |
| Operation not permitted during namespace setup | A kernel, LSM or container policy may deny the requested namespaces. Preserve the error category and investigate that environment. Do not disable host protections globally to obtain a pass. |
| Tests skipped, zero discovered, or fewer run than discovered | The strict acceptance gate must fail. Read the test diagnostics and fix the cause; do not relabel the reduced run as complete coverage. |
| Landlock or creation-time evidence unavailable | The installed write boundary cannot be established on that profile. Use a verified environment/filesystem; there is no permissive fallback. |
| Unenrolled or ambiguous repository | Follow [explicit initialization](engineering/INSTALLATION.md#choose-and-initialize-a-repository) for a new chosen repository. Preserve existing state when identity is ambiguous; do not forge markers or delete state to bypass a refusal. |
| Initialized repository moved on the same filesystem | Stop all its Relay users and follow [explicit rebind](engineering/REBIND.md). The original physical objects must remain; copied/restored ledgers and cross-filesystem migration are unsupported. |
| Interrupted rebind or inconsistent transition history | Preserve retained records. Only a valid unfinished transition to its exact pinned target is retryable; malformed, conflicting or incomplete history remains blocked without pruning. |
| Unknown relay command, changed selector or uncertain install | Preserve existing files and inspect using a separately reviewed bootstrap. Use fresh exact observations, not deletion or blind retry. |
| Provider login or hook-trust failure | This is distinct from SSH connectivity and ledger installation. The no-account tests cannot establish provider authentication or trusted hook execution. |

For a damaged launcher, follow [installation recovery](engineering/INSTALLATION.md).
For removal, follow [verified code uninstall](engineering/UNINSTALL.md).
Neither workflow is a general ledger purge, provider logout, automatic claim
expiry or permission to delete unknown state. If no separately verified release
or bootstrap is available, preserve the evidence and stop modifying the install.

## Useful, safe support information

Share the command name, exit category, platform/architecture, relevant dependency
versions, test counts and sanitized JSON outcome. Identify the reviewed commit
and whether the issue is install, enrollment, test environment or provider trust.
A success toast or green transport connection is not a durable-write witness.

Do not post auth files, environment dumps, raw provider transcripts, live SQLite
databases, private receiver URLs, personal Git metadata or unreviewed full logs.
Use a minimal disposable reproduction with artificial identifiers. Public issue
and private security-reporting channels must be established with the approved
repository; no contact endpoint or response-time promise exists yet.

## Trust limits

Relay coordinates cooperating agents under one trusted OS account. Session
labels are not hostile-user authentication; claims do not grant shell or cloud
permissions. Retained-byte verification and exact file-write confinement are not
a general malicious-code sandbox. Physical power-loss behavior and every
same-account namespace race are not proven.

See [architecture](ARCHITECTURE.md) and the [acceptance matrix](engineering/REQUIREMENTS.md)
for exercised guarantees and remaining work. Support claims grow from new
execution evidence, not from widening a version string or ignoring a refusal.
