"""Linux file-object write confinement for a fresh Relay worker only."""

import ctypes
import os
import platform
import stat
import struct


class ConfinementError(RuntimeError):
    pass


# x86-64 Linux syscall numbers; no architecture is inferred from platform name.
_CREATE, _ADD, _RESTRICT = 444, 445, 446
_WRITE_FILE, _TRUNCATE = 1 << 1, 1 << 14
_HANDLED_WRITES = _WRITE_FILE | sum(1 << bit for bit in range(4, 15))


class _Ruleset(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64)]


class _PathRule(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]


def _libc():
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        raise ConfinementError("this installed write profile requires x86-64 Linux")
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    return libc


def abi_version():
    libc = _libc()
    result = libc.syscall(_CREATE, None, 0, 1)
    if result < 3:
        raise ConfinementError("Landlock ABI 3 or newer is required; no unconfined fallback")
    return int(result)


def birth_time(fd):
    """Additional creation witness, never ctime or a unique-generation claim.

    Linux statx UAPI: fixed 256-byte structure, mask at 0x00, btime at
    0x50. AT_EMPTY_PATH queries the retained object, not a pathname.
    Unsupported birth times fail closed in this installed profile.
    """
    libc = _libc()
    try:
        statx = libc.statx
    except AttributeError as exc:
        raise ConfinementError("statx birth-time support is required") from exc
    statx.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                     ctypes.c_uint, ctypes.c_void_p]
    statx.restype = ctypes.c_int
    buffer = ctypes.create_string_buffer(256)
    if statx(fd, b"", 0x1000 | 0x100, 0x800, ctypes.byref(buffer)) != 0:
        raise ConfinementError("cannot read the held object's creation time")
    mask = struct.unpack_from("=I", buffer.raw, 0)[0]
    seconds, nanoseconds = struct.unpack_from("=qI", buffer.raw, 0x50)
    if not mask & 0x800 or seconds <= 0 or not 0 <= nanoseconds < 1_000_000_000:
        raise ConfinementError("filesystem creation-time evidence is unavailable")
    return {"birth_seconds": seconds, "birth_nanoseconds": nanoseconds}


def _open_null_sink():
    """Hold Linux's null character device; mapped inode ownership is not identity."""
    fd = os.open("/dev/null", os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        # Linux dispatches character I/O by rdev; major 1/minor 3 is the
        # data-discarding null driver. User namespaces may display its owner
        # as an overflow UID. No regular-file ownership predicate is changed.
        if not stat.S_ISCHR(info.st_mode) or info.st_rdev != os.makedev(1, 3):
            raise ConfinementError("the required null sink is not the expected kernel device")
        return fd
    except BaseException:
        os.close(fd)
        raise


def restrict_file_writes(writable_fds, *, parent_pid):
    """Irreversible restriction. Call only after fork, never in the controller.

    Handles content writes, truncation, and namespace mutations. Grants only
    content-write/truncate rights to exact regular-file inodes. Reads, network,
    metadata operations, and already-open output descriptors are not a sandbox
    against malicious code; the verified runtime and OS account remain trusted.
    """
    if os.getpid() == parent_pid or os.getppid() != parent_pid:
        raise ConfinementError("refusing confinement outside the fresh worker")
    abi_version()
    libc = _libc()
    ruleset = _Ruleset(_HANDLED_WRITES)
    fd = libc.syscall(_CREATE, ctypes.byref(ruleset), ctypes.sizeof(ruleset), 0)
    if fd < 0:
        raise ConfinementError("cannot create the required filesystem ruleset")
    null_fd = None
    try:
        # Git opens this kernel sink O_RDWR during startup even for read-only
        # commands. Grant the exact held null device, not /dev or any
        # regular file. No stored user data or namespace right is added.
        null_fd = _open_null_sink()
        null_rule = _PathRule(_WRITE_FILE, null_fd)
        if libc.syscall(_ADD, fd, 1, ctypes.byref(null_rule), 0) != 0:
            raise ConfinementError("cannot permit the required null sink")
        for target in writable_fds:
            info = os.fstat(target)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o077):
                raise ConfinementError("writable object is not a private regular file")
            rule = _PathRule(_WRITE_FILE | _TRUNCATE, target)
            if libc.syscall(_ADD, fd, 1, ctypes.byref(rule), 0) != 0:
                raise ConfinementError("cannot install the required file-object rule")
        if libc.prctl(38, 1, 0, 0, 0) != 0:
            raise ConfinementError("cannot set no-new-privileges in the worker")
        if libc.syscall(_RESTRICT, fd, 0) != 0:
            raise ConfinementError("cannot enforce required filesystem confinement")
    finally:
        if null_fd is not None:
            os.close(null_fd)
        os.close(fd)
