# Workspace enrollment design - descriptor custody primitive

Status: the Linux enrollment primitive is implemented in `src/relay_runtime/enrollment.py`, with original enrollment witnesses in `tests/runtime/test_enrollment.py` and guarded move witnesses in `tests/runtime/test_rebind.py`. Filesystem operations retain directory ancestry and use descriptor-relative access. Admission retains fresh custody through real installed-command SQLite access; see LEDGER-ADMISSION.md. Public installation and same-object rebind now have source-absent command evidence. Native provider integration, exhaustive crash recovery and full R03 acceptance remain separate gates. The initial-enrollment design and its historical evidence below retain their original scope.

## Problem

The source system binds ledger access to the Git common directory containing its Python package. That prevents an accidental call in another repository from creating a false-empty ledger. A normally installed Python package lives outside the user's project, so package installation and project authorization must become separate operations.

Removing the check, equating the requested `--repo` with approval, or trusting a repository's own rewritten hashes would lose the guarantee.

## Proposed boundary

- Install a trusted generic dispatcher outside project Git worktrees. Installation does not enroll a project or modify hooks.
- Explicit `init --repo` enrollment records the canonical Git common-directory identity and a unique enrollment identifier in an account-owned configuration boundary.
- Keep one authoritative active-generation digest. Runtime versions are immutable and activation is compare-and-swap guarded. A project-local manifest is checked data, not independent permission.
- Ordinary commands require an existing matching enrollment before directories, database opens, permission changes, or error logs. Unknown/ambiguous/recreated repositories fail closed.
- Validate the complete runtime manifest and execute the retained verified bytes. Do not hash a file and then import a mutable replacement by filename.
- Production state location comes from enrollment. An arbitrary `--home` or environment variable cannot redirect a valid project into another ledger.
- Resolve Git and imports through controlled process inputs. A hostile working directory, Git redirection variable, or import path must not select identity or executable code.
- Linked worktrees share the same enrolled common directory. A move or replacement requires explicit rebind with the expected old generation; do not silently adopt a new path.
- Hook installation is separately opt-in and scoped. Unknown global configuration is preserved, not overwritten.

## Storage direction

The existing sealed installer has useful invariants but a large fixed-environment deployment topology. Two alternatives remain credible:

1. An immutable installed distribution plus account-owned enrollment, avoiding executable copies in each project.
2. A content-addressed per-project runtime plus an account-owned enrollment anchor.

Proceed with the first direction: account-owned enrollment separate from an installed distribution, without project-local executable copies. Version activation and retained-byte execution are evaluated independently of this primitive; the completed installed client must also prove dispatch and the actual state-open boundary. The second remains a fallback if those guarantees cannot be met simply. No redesign of multi-user authentication or cross-machine synchronization is implied.

## Implemented primitive and explicit limits

- `Registry.for_account()` derives its location from the OS account database, ignoring HOME/XDG. Internal tests inject a temporary registry directly; that is not a supported production override.
- Git resolution uses `/usr/bin/git` and a closed environment, entering the retained requested/owner directory through `/proc/self/fd` with only the necessary descriptor inherited. The current Linux profile requires accessible procfs, an account-owned main `.git` directory without group/other write permissions, or a registered linked worktree with matching forward and backward Git pointers. Both registry and workspace ancestry must be controlled by the account or root, without group/other write permissions; root-owned sticky `/tmp` and `/var/tmp` are the explicit fixture/profile exception. Bare and separate-Git layouts are unsupported. Permissions are never silently changed.
- Explicit enrollment reserves `.relay` with mode 0700, verifies the held new directory is empty, writes a private identity marker there and a matching nonce in `.git/relay-enrollment.json`, then publishes the account-owned record as the sole activation point. No database is opened or initialized. Any preexisting state or Git marker observed before reservation without a matching account anchor refuses. A populated directory substituted before the first state open also refuses without writing its contents.
- The account record pins the canonical Git common directory, its device/inode, a unique enrollment ID, and the derived state directory's device/inode. The Git nonce provides an additional recreation witness rather than treating inode numbers as permanent identities. Linked worktrees share the same enrollment.
- Lookup is read-only and revalidates identities, both markers, strict bounded JSON, private files, ancestry, and the independently derived state path. A forged Git-file alias, copied origin/HEAD, moved checkout, replaced Git/state directory, state redirection, unsafe ancestry, or corrupted anchor refuses.
- Every existing directory from the filesystem root down to each accessed workspace/registry directory is opened with `O_DIRECTORY | O_NOFOLLOW` relative to its retained parent. Directory creation, exclusive temporary-file creation, bounded reads, no-replace hard-link publication, temporary-link removal, and directory fsync use these held descriptors. Namespace edges are compared with held device/inode identities before and after relevant operations and before returning. Identity-file descriptors remain open through validation; replaced files, including identical-byte replacements during a read, refuse.
- Complete record bytes are fsynced before exclusive hard-link publication. Each newly created registry directory link and the reserved state-directory link are synced before account publication. Concurrent initializers never overwrite the winner. A changed ancestor can leave reserved state in the originally held directory, but cannot redirect these writes through its replacement. Failures once publication starts report an uncertain outcome. No cleanup deletes published records or reserved state.
- `lookup` and `enroll` release all descriptors before returning their existing `Enrollment` value. That value is a checked identity snapshot, not a continuing filesystem lock or ledger-opening capability. Installed dispatch obtains fresh custody at the actual state-open boundary; passing the snapshot and later opening its state pathname is insufficient. See the separate installed admission, installation, uninstall and rebind guides for their exercised contracts; exhaustive I/O/power-failure coverage and complete provider integration remain open.

## Explicit same-object rebind

The original path-hashed account record remains one selected head. Version-two
path aliases route to that original authority; they never independently enroll
a project. A move increments the binding generation and creates a fresh binding
ID, preserving the enrollment ID, Git/state objects and markers. Read-only
RebindIdentity shares Admission's exact DB/WAL/SHM anchor and physical-object
predicates without opening SQLite or granting ledger access.

Before exchanging the head, apply publishes and syncs an immutable transition
that pins both complete heads. Its final name includes the record-content hash.
Lookup checks a bounded contiguous chain rooted at the original version-one
authority and requires the selected head to equal its exact latest successor.
Generation numbers alone are insufficient: two retained preparations can have
the same number but different targets. Both history bytes and per-enrollment
directory inventory remain under descriptor custody through the operation.

A crash after recording the successor but before selecting it closes ordinary
admission. Explicit retry may reuse only that exact pinned target/binding ID;
a changed target, malformed record, duplicate generation or gap refuses. A
last-instant substitution of the prepared exchange source or published guard
cannot make a mismatched retained head acceptable to a fresh current runtime.
Unknown objects are preserved. This does not prevent a hostile account owner
from replacing all configuration/code, make old binaries understand new guards,
stop existing workers or prove physical power-loss outcomes.

The independent 17-test rebind suite includes actual initialized ledgers, nine
publication checkpoints, genuine retained-source substitutions and pinned retry
controls. The public two-test main/parent move-and-return witness uses installed
commands with source/bundle absent, actual pending handoff and claim, independent
birth evidence, and a new legitimate write. See [operator procedure](REBIND.md)
and the [current source-bound results](development-test-result.json), rather than
treating this as copy/restore or general cross-filesystem migration support.

## Deterministic custody and interruption evidence

The 41 enrollment witnesses preserve the original refusal/identity suite and add substitutions at filesystem call boundaries: workspace ancestry before state mkdir, populated state before its first open, state/Git directory before temporary-file open, registry ancestry before account linking (both a symlink and a real directory), registry replacement during directory fsync, and an identical-byte account-record replacement during read. Replacement targets include populated synthetic ledger bytes. Their byte/mode/mtime snapshots remain unchanged. The operation refuses, descriptors are closed, and any original reserved state remains visible for explicit recovery.

Child processes use `os._exit` to bypass cleanup at each named publication checkpoint. Fresh processes then observe the resulting state:

| Cut | Result on ordinary lookup/enroll |
|---|---|
| Registry directory creation, before parent sync | No project enrollment; lookup refuses. A new explicit enroll can finish account scaffolding and reserve still-absent project state. |
| State directory creation, before parent sync | Empty state reservation is an orphan; lookup and enroll refuse without changes. |
| State marker linked, pending link removed before sync, or fully published | State reservation remains non-authoritative; both operations refuse without changes. |
| Git marker linked, pending link removed before sync, or fully published | Matching project markers alone grant no account enrollment; both operations refuse without changes. |
| Account record linked, before temporary-link removal and directory sync | Account file still has two links and is not a valid private record; both operations refuse without removing/adopting it. |
| Account pending link removed, before directory sync | A complete matching record is already visible and readable after process death; exact retry is a no-op if that record is present. The interrupted caller received no durability guarantee. |
| Account record published and directory synced | Complete matching enrollment is readable; explicit retry is an exact no-op, including with populated synthetic state. |

These eleven cuts prove process-interruption behavior on the tested Linux/WSL filesystem, not physical power-loss outcomes or exhaustive syscall failure coverage. Visibility after pending-link removal precedes the final directory fsync; only a completed enrollment call guarantees that sync finished. A later lookup validates whatever identity is present and never treats an absent/incomplete record as an empty ledger.

Same-account hostile namespace mutation cannot be prevented by Unix directory descriptors: names can change immediately after any final check, and mkdir has no atomic create-and-return-directory-descriptor operation. The populated-state check closes the reproduced first-open counterexample, but cannot prove provenance of an empty same-account directory substituted before acquisition. The trusted-account assumption is essential. The guarantee exercised here is descriptor confinement after acquisition plus refusal when retained names/identities change; it is not a claim to freeze a namespace against its owner.

## Required adversarial witnesses

Unenrolled repo; unrelated clone with identical origin/HEAD; linked worktree; replaced `.git` at same path; symlink substitution; changed payload with recalculated local manifest; package update during execution; competing activation; interruption before/after activation; stale rollback; forged HOME/Git/Python environment; redirected state home; moved repo; unknown hook file. Every refusal must check the exact before/after state, including an existing populated ledger.

The threat model trusts the installing OS account and package bootstrap. This is a coordination/isolation boundary, not protection from an attacker who can replace all code and configuration owned by that same account.
