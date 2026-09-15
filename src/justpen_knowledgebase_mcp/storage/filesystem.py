"""Descriptor-based locality checks for the supported 64-bit Unix platforms."""

from __future__ import annotations

import ctypes
import sys

from ..errors import ConfigurationError


class _DarwinStatFS(ctypes.Structure):
    # Darwin SDK sys/mount.h __DARWIN_STRUCT_STATFS64 (arm64 and x86_64).
    _fields_ = [
        ("bsize", ctypes.c_uint32),
        ("iosize", ctypes.c_int32),
        ("blocks", ctypes.c_uint64),
        ("bfree", ctypes.c_uint64),
        ("bavail", ctypes.c_uint64),
        ("files", ctypes.c_uint64),
        ("ffree", ctypes.c_uint64),
        ("fsid", ctypes.c_int32 * 2),
        ("owner", ctypes.c_uint32),
        ("kind", ctypes.c_uint32),
        ("flags", ctypes.c_uint32),
        ("subtype", ctypes.c_uint32),
        ("typename", ctypes.c_char * 16),
        ("mountpoint", ctypes.c_char * 1024),
        ("source", ctypes.c_char * 1024),
        ("flags_ext", ctypes.c_uint32),
        ("reserved", ctypes.c_uint32 * 7),
    ]


class _LinuxStatFS(ctypes.Structure):
    # Linux asm-generic/statfs.h; supported Linux targets use 64-bit longs.
    _fields_ = [
        ("kind", ctypes.c_long),
        ("bsize", ctypes.c_long),
        ("blocks", ctypes.c_ulong),
        ("bfree", ctypes.c_ulong),
        ("bavail", ctypes.c_ulong),
        ("files", ctypes.c_ulong),
        ("ffree", ctypes.c_ulong),
        ("fsid", ctypes.c_int32 * 2),
        ("namelen", ctypes.c_long),
        ("frsize", ctypes.c_long),
        ("flags", ctypes.c_long),
        ("spare", ctypes.c_long * 4),
    ]


# Linux uapi/linux/magic.h: ext2/3/4, XFS, Btrfs, tmpfs, overlayfs, ZFS.
_LOCAL_LINUX = frozenset({0xEF53, 0x58465342, 0x9123683E, 0x01021994, 0x794C7630, 0x2FC12FC1})


def require_local(platform: str, kind: int, flags: int) -> None:
    """Reject unknown and network mounts instead of guessing their lock semantics."""
    if (platform == "darwin" and flags & 0x1000) or (platform == "linux" and kind in _LOCAL_LINUX):
        return
    raise ConfigurationError("CONFIGURATION: unsupported or non-local filesystem")


def validate_local_directory(fd: int) -> None:
    """Query the already pinned directory; do not resolve another path spelling."""
    if ctypes.sizeof(ctypes.c_void_p) != 8 or sys.platform not in {"darwin", "linux"}:
        raise ConfigurationError("CONFIGURATION: unsupported filesystem platform")
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        result = _DarwinStatFS()
        function = libc.fstatfs64
    else:
        result = _LinuxStatFS()
        function = libc.fstatfs
    function.argtypes = [ctypes.c_int, ctypes.c_void_p]
    function.restype = ctypes.c_int
    if function(fd, ctypes.byref(result)) != 0:
        raise ConfigurationError("CONFIGURATION: filesystem locality unavailable")
    require_local(sys.platform, result.kind, result.flags)
