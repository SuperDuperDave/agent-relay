# Verified runtime generations - internal implementation

Historical implementation checkpoint from Multithread's Agent Relay development.
The original module names, closed-member counts and remaining gates below retain
that scope. For current installation and compatibility behavior, use
[installation](INSTALLATION.md); internal `relay_*` namespaces remain unchanged.

The stdlib-only `src/relay_bootstrap.py` implements approved-source capture,
immutable runtime generation storage, explicit activation, and retained-byte
Python loading. Its newer public release interface also installs a coherent
bootstrap/runtime launcher; see INSTALLATION.md. Installation does not enroll
projects, configure provider hooks or contact an agent. The separate verified dispatcher now
connects enrollment to ledger opening in internal command tests. This remains an
implementation building block, not public installation readiness.

## Trust and ownership

The installing OS account and bootstrap file are trusted. The bootstrap remains
outside the generation it verifies; it never imports a candidate module to
authenticate that candidate. `Installation.for_account()` derives the runtime
root from the OS account database, ignoring HOME, XDG, and Relay environment
overrides. Internal tests inject a disposable root through the Python API; no
public command-line root override is provided. The public release-pair installer
uses its own account-owned installation root and actual launcher, rather than
publishing this earlier runtime-only activation as a bootstrap-upgrade policy.

The input is a separately approved release manifest plus source members.
`manifest_for` is a release-build helper, not permission to approve whatever a
mutable source tree happens to contain. `snapshot_source` reads only the nine
closed payload members and compares their retained bytes with the supplied
manifest before creating installation state. Unlisted source files are never
copied. A future public installer must bind this input to the selected release
and its provenance; that release-selection interface remains a gate.

The bootstrap itself stays outside the runtime closure. The closure was explicitly
expanded from six to nine members to include admission, confinement and dispatcher.
This unreleased compatibility change is tested as a new generation; compatibility
with older six-member internal generations is not implied.

## Installation and activation

Runtime directories are account-owned and private. Directory ancestry is held
open and rechecked; reads, writes, mkdir, rename, and fsync use descriptors and
relative names. Symlinks, unsafe ancestry, unexpected members, hard-linked
payload files, and corrupt metadata refuse. The current support profile is
Linux/WSL with a single trusted OS account, not hostile-account isolation or
native Windows parity.

Directory creation cannot atomically return an open directory handle. As with
enrollment, malicious same-account replacement before first acquisition is not
covered by the retained-descriptor guarantee; the account itself is trusted.

The canonical manifest's SHA-256 names one generation. Installation reserves
that directory exclusively, writes the exact retained payload, and writes its
manifest last. A preexisting complete generation must verify before reuse; an
empty, partial, or unknown tree is not overwritten. File contents and containing
directory links are fsynced. An interrupted installation can leave a partial
generation that requires explicit recovery; it cannot become active through the
normal API.

Activation verifies the complete generation, takes a stable private flock, and
compares the expected activation identity. Each activation receives a new ID,
including rollback to earlier content. Thus A-to-B-to-A does not make a stale
request against the first A current again. The lock inode is never replaced or
unlinked by normal operations and is revalidated before publication.

The pending active record remains open through atomic replacement. Its inode
and exact bytes are checked before publication and against the published record
before success is returned. An independently discovered pending-file substitution
bug prompted this retained-record check and two regression tests. Cleanup removes
only a still-owned pending file; an unknown replacement is preserved. Errors
after possible publication report an uncertain outcome, never that nothing
happened. Callers must inspect current activation before retrying.

## Execution

The retained loader requires Python `-I -S -B`: isolation, no site startup, and
no bytecode writes. Isolated mode alone does not disable system site startup.
It rejects nonstandard import hooks/search paths and every preloaded Relay
namespace member, including descendants.

The entire generation is checked before installing the importer. Package
initializers and modules execute from those retained bytes. Replacing a source
file after verification cannot change the code that executes. Unknown modules
under the Relay namespaces are refused rather than handed to filesystem
importers. Ordinary stdlib imports remain available. The actual extracted core
and enrollment modules load through this path. The separate installed-command suite
also initializes and exercises real fixture ledgers through that verified code,
without importing modules from the fixture repository. See LEDGER-ADMISSION.

## Evidence and remaining gates

`tests/runtime/test_bootstrap.py` covers approved-manifest mismatch, source
mutation, source/runtime symlinks and hard links, unknown/partial generations,
recalculated local manifests, retained package/module bytes, activation ABA,
concurrent updates, replaced or permission-changed locks/ancestry, substituted
pending records, unknown-file preservation, preloaded/import-hook rejection,
startup environment canaries, and abrupt process exit during installation and
after activation replacement. Process exit proves inspectable visibility, not
physical power-loss durability.

The earlier generation checkpoint passed 31 bootstrap, 41 enrollment and 103
core tests (175 total). Subsequent installed-command witnesses are additional;
the current exact sanitized receipt is in `development-test-result.json`.

The newer public layer now binds explicit release digests to bootstrap/runtime
pairs, provides no-write plans and coherent activation/rollback, and passes an
actual source-absent launcher test in a disposable account filesystem view.
Independent review's prepared-pair substitution and displaced-file deletion
reproductions were repaired and rechecked. See INSTALLATION.md.

Still required: published release provenance and clean public-clone proof;
full recovery/rebind/uninstall behavior;
expanded crash and generation-lifecycle tests; real Codex/Claude contracts and
workflow; clean-machine/public-clone proof. Passing these internal tests does
not discharge the full release acceptance matrix.
