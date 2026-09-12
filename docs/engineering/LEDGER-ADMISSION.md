# Installed ledger admission - verified command boundaries

This records a historical enrolled-command integration checkpoint from
Multithread's Agent Relay development. A newer public installation preview is
documented in [INSTALLATION.md](INSTALLATION.md); the provider demonstration was
still open at this checkpoint. See the [current peer guide](../PEER.md) for later
capabilities and separately observed evidence. Internal tests install the closed runtime outside
the fixture Git repository; the newer public-profile test also builds and installs
through public commands and executes the real launcher with source and bundle
absent, using OS-account defaults and a separately enrolled disposable workspace.

## What is connected

The nine-file approved runtime closure includes core, enrollment, admission,
worker confinement and the installed dispatcher. Source execution keeps its
original source-derived workspace guard and diagnostic overrides. Only the
verified installed dispatcher uses a private, one-shot admission binding.

The dispatcher uses the OS account registry by default, rejects global
`--home` and any `RELAY_HOME` presence, and closes its Git environment.
Internal tests inject temporary registries through Python, not a public
argument, HOME change or environment bypass. The public namespace test uses no
registry/root injection and verifies the actual account defaults. Linked worktrees resolve the same
enrolled Git common directory. Installation itself does not enroll anything.

## Initialization and ordinary access

| Observed state | Explicit init | Ordinary command |
|---|---|---|
| No enrollment or state | Reserve private enrollment, then the three new files | Refuse; create nothing |
| Enrolled, marker only | Exclusively reserve DB/WAL/SHM | Refuse as uninitialized |
| Partial/unanchored ledger files | Refuse; preserve for explicit recovery | Refuse; never adopt |
| Complete account ledger anchor | Verify and diagnose without reinitializing | Verify then execute |
| Missing/replaced/unsafe file or identity | Refuse; preserve evidence | Refuse; never recreate a false-empty ledger |

A separate immutable account record pins the enrollment ID and each database,
WAL and shared-memory file's device, inode and exact statx birth seconds and
nanoseconds. Mutable ctime/mtime are not durable identity. Birth time adds
recreation evidence but is a timestamp, not a mathematically unique generation
nonce; this does not protect against an owner able to forge all trusted state.

Init alone creates the three files with O_EXCL and mode0600 in the held private
state directory. OFF mode is used only on these exclusively created, empty,
unpublished objects before establishing WAL; no schema/data SQL runs in OFF.
An existing ledger must already have WAL mode and schema2; normal commands
cannot initialize schema0 or perform an unapproved version migration.

The parent retains enrollment ancestry, markers, registry record and file
capabilities throughout the worker command. After a successful initialization,
it checks those identities again, fsyncs the file set and state directory, then
publishes the account ledger anchor. A crash may leave reserved state or a
visible but not yet fully synced record. Missing or ambiguous publication is
not permission to erase, replay or adopt that state.

## Why a held directory alone was insufficient

An actual SQLite experiment showed that a procfs-held directory path is
canonicalized. After a namespace substitution, SQLite could keep the old
database descriptor while reopening WAL/SHM through the replacement pathname.
The replacement main database was unchanged but foreign sidecars were modified.

The installed worker therefore combines two independent checks:

1. Retained-directory/file checks detect changed logical enrollment and names,
   before/after SQL and before the controller accepts output.
2. Linux Landlock restricts content writes/truncation to the original permitted
   file objects, denying create/delete/rename and foreign regular-file writes
   even if SQLite follows a changed name between checks.

A DB-name-only substitution can still allow an in-flight write to the original
authorized objects. That is not a successful logical operation: later checks
withhold the success receipt and subsequent commands refuse. An uncertain
operation must be inspected, not blindly retried.

## Worker profile and limits

Each command forks a fresh single-threaded worker. Irreversible restrictions
apply only there, not to the controller, SSH session or agent. Unrelated
inherited descriptors are closed before SQLite opens. Source-mode core SQL,
ownership, idempotency and transactional decision-response/ACK rules are kept.

The current profile requires x86-64 Linux, Landlock ABI3 or newer, procfs, Git
at /usr/bin/git, supported statx birth times, and a single trusted OS account.
It was exercised on ext4 under WSL2, Python3.12.3 and SQLite3.45.1. This is not
proof for native Windows, other architectures, network filesystems or every
Linux distribution. Unsupported confinement refuses; there is no silent
unconfined write fallback.

Permitted content-write objects are the admitted ledger files, the exact
held kernel null device (character major1/minor3) required by Git, and preopened
anonymous stdout/stderr buffers. The null-device exception grants no directory
or truncate permission. Read-only commands deny database writes and use
SQLite mode=ro/query_only; WAL shared-memory coordination can still write.
They do not use immutable mode on a changing live ledger.

Null inode owner UID is not a device identity predicate: rootless namespaces can
display the genuine node's owner as an overflow UID. The exact held character
device check and WRITE_FILE-only rule remain; ordinary ledger ownership checks
are unchanged. The rationale and independent negative witnesses are in
[NULL-DEVICE-IDENTITY](NULL-DEVICE-IDENTITY.md).

This is not a general hostile-code sandbox. Reads, network and metadata
operations such as chmod are not covered by this filesystem policy. The
verified runtime and OS account remain trusted. Consequently installed core
paths skip pathname permission repair and do not write hook-failure files.
Provider-hook observation failures degrade on stderr without blocking the
provider or reporting an empty healthy ledger.

The controller bounds and buffers output, checks exit status and final custody,
and only then forwards successful output. Abnormal worker exit suppresses
stdout. This does not turn a receipt into authorization, freeze names after
return, or prove physical power-loss durability.

## Evidence and next gates

The isolated command suite exercises real initialization, reopen, duplicate
events, competing claims and exact-holder release, linked worktrees, lifecycle
sanitization, missing/replaced files, creation evidence, unsupported confinement,
closed inherited descriptors, forbidden foreign writes, crash orphans,
committed-WAL recovery and rollback of uncommitted transactions. The inherited
103 core tests continue to run separately. See the current machine-readable
receipt for exact totals; a test pass only proves its exercised case.

Public release selection, pair upgrades, recovery/disable and fresh-account
installation/ledger commands are now exercised in the isolated namespace profile.
The source-absent public flow covers claims, linked worktrees, decisions, lifecycle
and direct SQLite integrity/count; its source-hash-bound receipt is
public-runtime-result.json. Fixture actor names are not real provider sessions.
Full artifact uninstall, repository rebind, no-valid-retained-release recovery,
CI/support matrix, provider configuration and the real two-agent workflow remain open.
The new nine-member closure is an unreleased compatibility change; old
six-member internal generations are not claimed compatible with this bootstrap.

References: [SQLite URI modes](https://www.sqlite.org/uri.html),
[SQLite moved-database error](https://www.sqlite.org/rescode.html#readonly_dbmoved),
[Linux Landlock userspace API](https://docs.kernel.org/userspace-api/landlock.html).
The statx layout was also checked against the installed Linux UAPI header.
These sources explain mechanisms; the disposable runtime tests provide the
implementation evidence.
