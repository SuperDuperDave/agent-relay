# Null-device identity and rootless public-runtime proof

## Decision

The Linux worker identifies its null sink by a held no-follow file descriptor,
character-device type and exact device number 1:3. It no longer requires the
reported inode owner to be UID0. All regular ledger-file ownership, mode and
link-count checks remain unchanged.

This is a Linux compatibility correction, not a UID65534 exception or an
unconfined fallback. The runtime still grants only WRITE_FILE on the held null
object. No directory, truncation or other-device grant was added.

## Why owner UID was the wrong identity test

User namespaces translate file-owner IDs; an unmapped owner can appear as the
overflow UID, normally 65534. That presentation does not imply a changed device.
See [Linux user namespace ID mapping](https://man7.org/linux/man-pages/man7/user_namespaces.7.html).

Linux character-device dispatch selects operations using the inode's device
number. The [character-device open path](https://raw.githubusercontent.com/torvalds/linux/v6.6/fs/char_dev.c)
uses i_rdev; the [memory-device implementation](https://raw.githubusercontent.com/torvalds/linux/v6.6/drivers/char/mem.c)
selects the null operations at minor3 and discards writes.
The [kernel device-number registry](https://www.kernel.org/doc/Documentation/admin-guide/devices.txt)
identifies major1/minor3 as null.

Engineering inference, independently reviewed: within the existing trusted
kernel and OS-account model, the owner predicate does not further identify
this data-discarding sink. It incorrectly rejected a genuine null node in the
rootless test account. Other device numbers, regular files and symlinks are
still rejected using real descriptor metadata.

The descriptor stays open through rule installation.
[Landlock's path rule](https://www.kernel.org/doc/html/v6.6/userspace-api/landlock.html)
can identify an individual file through its descriptor. Our code does not
validate a pathname and later grant a whole directory.

## Implementation evidence

The source-absent public runtime test failed at init with the old owner check.
After changing only null identity validation, the same test passed with the
real mapped owner still 65534. No host account was created, no administrative
route was used, and no mock changed the successful file type/device number.

Five independently authored tests verify genuine null, actual regular/FIFO/
directory/socket/symlink substitutions, the wrong character device /dev/zero,
descriptor cleanup on refusal/stat failure, and real forked Landlock behavior:
null writes succeed while unrelated regular-file, other-device and namespace
writes are denied. Existing ledger substitution tests remain in the full suite.

The public test builds and installs a closed release into an isolated synthetic
OS account, then removes source and bundle from the execution view. Actual
installed commands refuse unenrolled access, initialize and reopen real SQLite,
share enrollment with a registered linked worktree, enforce competing claims
and exact-holder release, preserve retry identity, record a decision response
and ACK, and record lifecycle once. A separate read-only SQLite connection
checks integrity and event count.

## Limits

This is full public CLI ledger-flow evidence on one WSL2/ext4/Linux profile,
not a native Windows claim, every Linux distribution, or a real provider run.
Actor names and lifecycle JSON in the test are fixture inputs, not actual
Codex/Claude sessions or observed provider hook contracts.

The policy is still not a hostile-code sandbox: reads, network, metadata and
deliberately preopened output descriptors have the documented limits. Kernel
compromise, privileged replacement of device drivers and malicious same-account
code are outside that model. A valid device identity also cannot promise its
pathname will remain usable after another process changes the namespace;
such failures still refuse.

See [ledger admission](LEDGER-ADMISSION.md), [installation](INSTALLATION.md) and
the current test receipt for the complete supported profile and remaining gates.
