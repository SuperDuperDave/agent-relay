# No-account CI

Run the strict local suite and no-account demo below to check your checkout.
The GitHub Actions workflow is prepared but has not yet run. Local runtime and
native-provider results do not establish hosted-runner compatibility.

## One strict local command

From a trusted checkout on the [supported test profile](SUPPORT.md):

```sh
/usr/bin/python3 -I -S -B tests/run_tests.py --suite all --strict
/usr/bin/python3 -I -S -B examples/no_account_demo.py --json
```

Strict mode requires nonzero test discovery, every discovered test to run, and
zero failures, errors, skips, expected failures or unexpected successes.
Its JSON reports discovered_tests, tests, strict and each result category.
The exit status is nonzero for incomplete coverage even if unittest says OK.
This prevents a missing namespace prerequisite from silently skipping the
installed-runtime witnesses. Nine independent tests exercise the real runner
in tiny copied-project fixtures, including early stop and import failure.

Without --strict, the runner keeps unittest's ordinary skip/expected-failure
semantics for targeted development. Use the complete strict command as the
release/CI gate, not a core-only run or a success message copied from old logs.

The runner launches a child with fresh home/XDG/tmp/Git-template directories,
an explicit environment allowlist and disabled inherited Git configuration and
hooks. It does not pass provider secrets or execute the optional native probes.
That environment isolation is **not** a host filesystem/network sandbox.
The public installation tests and demonstration separately use disposable
namespace profiles; their exact isolation claims are documented in the receipts.

## Hosted workflow

[ci.yml](../.github/workflows/ci.yml) targets a fresh GitHub-hosted ubuntu-24.04
runner. It installs the distribution's bubblewrap package, then runs the strict
suite, demonstration and immutable selection audit. GitHub documents these
[hosted VMs and their administrative privileges](https://docs.github.com/en/actions/reference/runners/github-hosted-runners).
Dependency installation is
only a future hosted-job step; this workflow does not change a developer's
machine, AppArmor policy or kernel settings.

The workflow uses push/pull_request/manual events, read-only repository-content
permission, a full-SHA-pinned checkout action and persist-credentials:false.
There are no provider secrets, caches, artifact uploads, deployments, write
permissions or privileged pull_request_target/workflow_run triggers.
These choices follow [GitHub's secure-use guidance](https://docs.github.com/en/actions/reference/security/secure-use).
The pin resolves to the official [checkout v7.0.1 commit](https://github.com/actions/checkout/commit/3d3c42e5aac5ba805825da76410c181273ba90b1);
updates require review rather than following a mutable tag.

Use /usr/bin/python3 explicitly. The launcher and namespace witnesses invoke
that system path and mount /usr, so selecting another interpreter through PATH
or setup-python would not change the interpreter exercising the installed code.

The [runner image inventory](https://github.com/actions/runner-images/blob/main/images/ubuntu/Ubuntu2404-Readme.md)
is a changing dependency description, not a compatibility witness. Hosted
kernel/LSM policy, nested namespaces, Landlock and fixture-filesystem creation
times must actually pass. Unsupported profiles fail; the workflow does not
disable host protections, skip required checks or fall back to an unisolated demo.

Checkout fetches complete history because the immutable auditor refuses shallow
history. The audit reads the manifest from the exact event commit and reports
only categories, locations and hashes. A clean selected snapshot is not rights
clearance, approval of all historical blobs or permission to publish; see the
[publication boundary](engineering/PUBLICATION.md).

## Evidence required before calling CI green

After an approved public repository exists, inspect the actual event commit,
runner image, dependency versions, full discovered/run counts, demo artifact
and SQLite outcome, and immutable audit report. Record the workflow URL and
tested commit. A cancelled, skipped, partial or never-started job is not a pass.

Fork pull requests run only on disposable GitHub-hosted runners with the limited
permissions above. Do not move untrusted contribution jobs onto a persistent
self-hosted machine holding live workspaces, credentials or Relay state.
