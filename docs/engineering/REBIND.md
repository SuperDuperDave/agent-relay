# Move an enrolled repository — development preview

Rebind updates Multithread's account-owned binding after an explicitly planned,
same-filesystem move. It does not copy a ledger, initialize a replacement,
repair Git pointers, release claims or acknowledge pending work.

This workflow applies only to an already initialized main checkout whose Git,
state directory and three SQLite files remain the same physical objects.
Copying, restoring a backup, moving across filesystems and changing OS accounts
are not supported by this command. A matching filename or checksum is not
enough.

## Before moving

1. Use a reviewed installed runtime that supports this binding protocol.
2. Stop all Multithread commands and provider processes using this enrollment,
   including those in linked worktrees. Prevent automatic hooks from restarting.
3. Record the old absolute main-checkout path. Move the checkout or its parent
   with your normal filesystem tools, preserving its objects and permissions.
4. If linked worktrees need Git pointer repair, complete that explicitly with
   Git before resuming those peers. Multithread does not perform it.

The --confirm-quiescent flag records your acknowledgement of step 2. It cannot
prove that processes have stopped, terminate old workers or undo committed
writes. Rebind's management lock coordinates cooperating rebind calls only.

Use the exact installed launcher path from installation. The examples below use
the normal account layout; OLD_ROOT, NEW_ROOT and BINDING_OBSERVATION are
placeholders, not literal values to paste.

## Inspect, then approve one binding

```sh
~/.local/bin/multithread --repo NEW_ROOT rebind-plan --from-repo OLD_ROOT
```

The plan is read-only. It verifies the old binding, absent old path, new main
checkout, markers and actual ledger-file identities without opening SQLite.
Review its enrollment ID, old/new paths, current/next generations, account
write targets and expected_binding. The observation digest is not a secret,
credential or independent authorization.

```sh
~/.local/bin/multithread --repo NEW_ROOT rebind --from-repo OLD_ROOT \
  --expected-binding BINDING_OBSERVATION --confirm-quiescent
```

Apply verifies again under the per-enrollment management lock. Only account
enrollment metadata changes. The enrollment ID, project markers, ledger anchor,
DB/WAL/SHM objects and contents remain untouched by rebind itself. A later
ordinary SQLite read can update reader-coordination sidecars.

A successful result reports the new generation and binding identity and names
retained evidence. Keep that evidence; do not move old binding files into the
active position or delete records to force a retry.

Afterward, use ordinary installed commands to check the actual ledger:

```sh
~/.local/bin/multithread --repo NEW_ROOT doctor
~/.local/bin/multithread --repo NEW_ROOT status
~/.local/bin/multithread --repo NEW_ROOT events
```

Verify the expected pending work and owners before resuming providers.
Regenerate invocation arguments that contain the old --repo path; existing
sessions and launch arguments are not rewritten. Moving back to OLD_ROOT is
another explicit rebind with a new generation, not a rollback to its old record.

## Interruption is not cancellation

A missing success receipt does not tell you whether publication happened.
Preserve files and inspect using the current compatible runtime before retrying.

Each transition durably records its exact predecessor and approved successor
before selecting the successor. Normal commands reject retired or inconsistent
heads. A retry of a recorded but unfinished transition must use its same target
and successor identity; it cannot redirect that approval to another location.
Content-bound immutable records prevent a genuine retained older or alternate
prepared head from becoming authority merely because it was exchanged into
the active pathname.

A completed transition makes the old observation stale. If normal commands
already validate the intended new binding, do not repeat the move. If the old
head remains selected after its successor was recorded, normal ledger access
stays closed; a fresh plan for that exact unfinished transition can support
explicit retry. Unknown, malformed, conflicting or incomplete history stays
preserved and blocked. This preview bounds an enrollment to 128 transitions,
registry inspection to 4,096 entries and each record to 8,192 bytes. Exhausted
bounds refuse; there is no general history repair or automatic pruning.

Do not re-enroll, delete .relay, replace markers, restore an old account record,
or pick a different destination just to bypass a refusal. An occupied old path,
a copied ledger, or unrecognized account metadata requires diagnosis.

## Scope and evidence

Verification is limited to the documented Linux/WSL profile and cooperating
processes under one trusted OS account. Older runtimes cannot enforce an
unrecognized guard protocol; rolling back to unsupported code or manually
restoring account metadata is not supported recovery.

The development tests use disposable initialized repositories with actual
SQLite, claims and pending handoffs. Installed-command tests remove the source
and bundle, check physical object identity and birth evidence before reopen,
and exercise a fresh generation for a return move. Independent tests exercise
process interruption and substitution at publication syscall boundaries.
Consult the source-bound latest development receipt for executed results.

These witnesses are not physical power-loss proof, cross-filesystem migration,
backup recovery, protection from a hostile account owner or proof of a real
provider coding/review session. See [installation](INSTALLATION.md),
[admission](LEDGER-ADMISSION.md) and [support](../SUPPORT.md).
