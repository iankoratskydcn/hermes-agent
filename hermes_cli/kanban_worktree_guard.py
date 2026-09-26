"""Small, fail-closed gate for worktree-backed task handoffs."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
from typing import Any


@dataclass(frozen=True)
class WorktreeCheck:
    ok: bool
    reason: str = ""


def _git(path: Path, *args: str) -> tuple[int, str, str]:
    env = os.environ.copy()
    env.update({"GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"})
    try:
        proc = subprocess.run(
            ["git", "-C", str(path), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            env=env,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, "", f"git invocation failed: {type(exc).__name__}: {exc}"
    return proc.returncode, proc.stdout, proc.stderr.strip()


def verify_task_worktree(task: Any) -> WorktreeCheck:
    """Verify the git state required before a worktree task changes lanes.

    Non-worktree tasks retain their existing behavior. Worktree tasks must have
    a registered path, a symbolic branch matching ``task.branch_name``, and no
    staged, unstaged, conflicted, or untracked files.
    """
    if getattr(task, "workspace_kind", None) != "worktree":
        return WorktreeCheck(True)

    raw_path = getattr(task, "workspace_path", None)
    if not isinstance(raw_path, str) or not raw_path.strip():
        return WorktreeCheck(False, "worktree path is missing")
    path = Path(raw_path).expanduser()
    if not path.is_dir():
        return WorktreeCheck(False, f"worktree path does not exist: {path}")

    code, out, err = _git(path, "rev-parse", "--is-inside-work-tree")
    if code != 0 or out.strip() != "true":
        return WorktreeCheck(False, err or "path is not a git worktree")

    expected = getattr(task, "branch_name", None)
    if not isinstance(expected, str) or not expected.strip():
        return WorktreeCheck(False, "expected worktree branch is missing")
    code, branch, err = _git(path, "symbolic-ref", "--quiet", "--short", "HEAD")
    if code != 0 or not branch.strip():
        return WorktreeCheck(False, "worktree HEAD is detached")
    actual = branch.strip()
    if actual != expected and f"refs/heads/{actual}" != expected:
        return WorktreeCheck(
            False,
            f"worktree branch mismatch: expected {expected!r}, found {actual!r}",
        )

    code, listing, err = _git(path, "worktree", "list", "--porcelain")
    if code != 0:
        return WorktreeCheck(False, err or "could not read registered worktrees")
    registered = any(
        line == f"worktree {path.resolve()}"
        for line in listing.splitlines()
    )
    if not registered:
        return WorktreeCheck(False, "path is not a registered linked worktree")

    code, status, err = _git(path, "status", "--porcelain", "--untracked-files=all")
    if code != 0:
        return WorktreeCheck(False, err or "could not read worktree status")
    if status:
        return WorktreeCheck(False, f"worktree is dirty: {status.splitlines()[0]}")
    return WorktreeCheck(True)
