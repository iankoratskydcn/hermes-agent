"""Scope-controlled Docker bind mounts.

This module deliberately has no Docker or subprocess dependency: it translates a
validated host path scope into explicit read-only/read-write bind arguments.
"""

import os
from pathlib import Path


def _scope_paths(scope: dict | None, key: str) -> list[str]:
    if scope is None:
        return []
    if not isinstance(scope, dict):
        raise ValueError("docker scope must be a mapping with read and write path lists")
    paths = scope.get(key, [])
    if isinstance(paths, str) or not isinstance(paths, (list, tuple)):
        raise ValueError(f"docker scope {key!r} must be a list of relative paths")
    return [p for p in paths if isinstance(p, str)]


def _host_path(project_root: str, relative: str) -> tuple[str, str]:
    if not relative or os.path.isabs(relative):
        raise ValueError(f"docker scope path must be relative to project root: {relative!r}")
    root = os.path.realpath(os.path.abspath(os.path.expanduser(project_root)))
    host = os.path.realpath(os.path.join(root, relative))
    try:
        common = os.path.commonpath((root, host))
    except ValueError:
        common = ""
    if common != root:
        raise ValueError(f"docker scope path escapes project root: {relative!r}")
    container_relative = Path(relative).as_posix().lstrip("./")
    if not container_relative or container_relative == ".":
        raise ValueError("docker scope path must not be the project root itself")
    return host, container_relative


def scope_bind_args(
    scope: dict | None, project_root: str, container_root: str = "/workspace"
) -> tuple[list[str], bool]:
    """Return explicit ``-v`` args and whether the container rootfs may be read-only.

    ``None`` is the legacy mode and emits no arguments.  A supplied scope is
    strict: every path is relative to *project_root*, symlink escapes are rejected,
    and read/write overlaps are rejected rather than silently choosing a mode.
    """
    if scope is None:
        return [], False
    read = _scope_paths(scope, "read")
    write = _scope_paths(scope, "write")
    if set(read) & set(write):
        raise ValueError("docker scope path cannot be both read-only and read-write")

    args: list[str] = []
    seen: set[str] = set()
    for relative, mode in [(p, "ro") for p in read] + [(p, "rw") for p in write]:
        host, normalized = _host_path(project_root, relative)
        if normalized in seen:
            raise ValueError(f"duplicate docker scope path: {relative!r}")
        seen.add(normalized)
        destination = f"{container_root.rstrip('/')}/{normalized}"
        args.extend(["-v", f"{host}:{destination}:{mode}"])

    # With a declared write set, all writable state is represented by explicit
    # rw binds (Docker's tmpfs security mounts remain available); an empty set
    # intentionally does not claim a writable rootfs.
    return args, bool(write)
