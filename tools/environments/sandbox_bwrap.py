"""Bubblewrap and Landlock capability helpers for local command execution.

This module deliberately owns policy-independent argv construction.  Callers decide
whether a command needs a sandbox; a required sandbox never falls back to Popen.
"""
from __future__ import annotations

import ctypes
import errno
import functools
import os
import platform
import shutil
import subprocess
from pathlib import Path


MIN_LANDLOCK_ABI = 2
# Linux's landlock_create_ruleset syscall number is stable across the supported
# architectures (x86_64, arm64, arm, riscv, ppc64); keep this explicit rather
# than depending on a third-party binding.
_LANDLOCK_CREATE_RULESET = 444
_LANDLOCK_ADD_RULE = 445
_LANDLOCK_RESTRICT_SELF = 446
_LANDLOCK_CREATE_RULESET_VERSION = 1
_LANDLOCK_RULE_TYPE_PATH_BENEATH = 1
_LANDLOCK_ACCESS_FS_READ = (1 << 2) | (1 << 3) | (1 << 4) | (1 << 5) | (1 << 6) | (1 << 7)
_LANDLOCK_ACCESS_FS_WRITE = (1 << 0) | (1 << 1) | (1 << 8) | (1 << 9) | (1 << 10)


class _PathBeneath(ctypes.Structure):
    _fields_ = [("parent_fd", ctypes.c_int), ("allowed_access", ctypes.c_uint64)]


class SandboxUnavailable(RuntimeError):
    """A requested sandbox cannot be established; execution must not continue."""


def _linux() -> bool:
    return platform.system() == "Linux"


@functools.lru_cache(maxsize=1)
def bwrap_available() -> bool:
    """Return true only when bwrap is discoverable and can actually start."""
    if not _linux():
        return False
    binary = shutil.which("bwrap")
    if not binary:
        return False
    try:
        return subprocess.run(
            [binary, "--version"], stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, check=False, timeout=2,
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _libc():
    libc = ctypes.CDLL(None, use_errno=True)
    syscall = libc.syscall
    syscall.restype = ctypes.c_long
    return syscall


def landlock_abi() -> int | None:
    """Probe the kernel Landlock ABI, returning None when unavailable.

    The VERSION operation accepts a null ruleset pointer and zero size.  errno
    ENOSYS/EOPNOTSUPP means the running kernel has no Landlock support.  Other
    failures are also treated as unavailable: capability probes must fail closed.
    """
    if not _linux():
        return None
    try:
        result = _libc()(
            _LANDLOCK_CREATE_RULESET,
            None, 0, _LANDLOCK_CREATE_RULESET_VERSION,
        )
        if result < 0:
            return None
        return int(result)
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def landlock_available(*, minimum_abi: int = MIN_LANDLOCK_ABI) -> bool:
    """Whether Landlock meets the pinned minimum ABI (currently ABI 2)."""
    abi = landlock_abi()
    return abi is not None and abi >= minimum_abi


def require_capabilities() -> None:
    """Raise a clear error unless both required enforcement layers are usable."""
    if not bwrap_available():
        raise SandboxUnavailable("sandbox required but bubblewrap (bwrap) is unavailable")
    abi = landlock_abi()
    if abi is None or abi < MIN_LANDLOCK_ABI:
        found = "unavailable" if abi is None else f"ABI {abi}"
        raise SandboxUnavailable(
            f"sandbox required but Landlock {found}; minimum ABI is {MIN_LANDLOCK_ABI}")


def apply_landlock(*, ro_paths: list[str], rw_paths: list[str],
                   minimum_abi: int = MIN_LANDLOCK_ABI) -> None:
    """Install a Landlock path-beneath ruleset in the current process.

    This is intended for an exec shim/launcher: restriction is irreversible for
    the calling thread and descendants.  Every probe or syscall failure raises,
    never silently executing without the backstop.
    """
    abi = landlock_abi()
    if abi is None or abi < minimum_abi:
        raise SandboxUnavailable(f"Landlock ABI {abi!r} is below minimum ABI {minimum_abi}")
    class _RulesetAttr(ctypes.Structure):
        _fields_ = [("handled_access_fs", ctypes.c_uint64)]
    handled = _LANDLOCK_ACCESS_FS_READ | _LANDLOCK_ACCESS_FS_WRITE
    syscall = _libc()
    ruleset = _RulesetAttr(handled)
    fd = syscall(_LANDLOCK_CREATE_RULESET, ctypes.byref(ruleset), ctypes.sizeof(ruleset), 0)
    if fd < 0:
        raise SandboxUnavailable(f"Landlock ruleset creation failed (errno {ctypes.get_errno()})")
    opened: list[int] = []
    try:
        for path in ro_paths + rw_paths:
            try:
                parent_fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
            except OSError as exc:
                raise SandboxUnavailable(f"cannot open Landlock path {path!r}: {exc}") from exc
            opened.append(parent_fd)
            access = _LANDLOCK_ACCESS_FS_READ
            if path in rw_paths:
                access |= _LANDLOCK_ACCESS_FS_WRITE
            rule = _PathBeneath(parent_fd, access)
            if syscall(_LANDLOCK_ADD_RULE, fd, _LANDLOCK_RULE_TYPE_PATH_BENEATH,
                       ctypes.byref(rule), 0) < 0:
                raise SandboxUnavailable(f"Landlock rule installation failed (errno {ctypes.get_errno()})")
        # Unprivileged callers need no_new_privs before restrict_self.
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(38, 1, 0, 0, 0) != 0:  # PR_SET_NO_NEW_PRIVS
            raise SandboxUnavailable(f"could not set no_new_privs (errno {ctypes.get_errno()})")
        if syscall(_LANDLOCK_RESTRICT_SELF, fd, 0) < 0:
            raise SandboxUnavailable(f"Landlock restriction failed (errno {ctypes.get_errno()})")
    finally:
        for parent_fd in opened:
            os.close(parent_fd)
        os.close(fd)


def _existing(paths: list[str]) -> list[str]:
    return [str(Path(p)) for p in paths if p and os.path.exists(p)]


def build_bwrap_argv(*, read_paths: list[str], write_paths: list[str],
                     toolchain_ro_dirs: list[str], network: bool,
                     cmd: list[str]) -> list[str]:
    """Build a fail-closed bubblewrap argv prefix around *cmd*.

    Only existing host paths are mounted; callers should supply canonical paths.
    The writable set is intentionally explicit and takes precedence over read-only
    mounts so a path cannot be accidentally made writable by a broad RO list.
    """
    if not cmd:
        raise ValueError("sandbox command must not be empty")
    argv = [shutil.which("bwrap") or "bwrap", "--unshare-all",
            "--disable-userns", "--die-with-parent", "--new-session",
            "--clearenv", "--setenv", "PATH", "/usr/local/bin:/usr/bin:/bin",
            "--setenv", "HOME", "/work", "--setenv", "TERM",
            os.environ.get("TERM", "dumb"), "--proc", "/proc", "--dev", "/dev",
            "--tmpfs", "/tmp"]
    writable = set(_existing(write_paths))
    for path in _existing(read_paths + toolchain_ro_dirs):
        if path not in writable:
            argv += ["--ro-bind", path, path]
    for path in _existing(write_paths):
        argv += ["--bind", path, path]
    if not network:
        argv.append("--unshare-net")
    return [*argv, "--", *cmd]


def build_landlock_wrapper_argv(*, ro_paths: list[str], rw_paths: list[str],
                                cmd: list[str]) -> list[str]:
    """Return *cmd* unchanged until the native Landlock exec shim is shipped.

    Capability gating remains active now; this explicit seam prevents callers from
    mistaking a future shim's absence for an enforced policy.
    """
    del ro_paths, rw_paths
    return list(cmd)
