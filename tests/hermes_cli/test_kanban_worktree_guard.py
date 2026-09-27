from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import subprocess

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_worktree_guard
from hermes_cli.kanban_worktree_guard import WorktreeCheck, verify_task_worktree


@dataclass
class TaskStub:
    workspace_kind: str
    workspace_path: str | None
    branch_name: str | None


def _git(cwd: Path, *args: str) -> None:
    env = os.environ.copy()
    env.update({
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
    })
    subprocess.run(["git", "-C", str(cwd), *args], check=True, env=env, capture_output=True)


def _linked_worktree(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "README").write_text("ok\n")
    _git(repo, "add", "README")
    _git(repo, "commit", "-q", "-m", "initial")
    branch = "task/clean"
    linked = tmp_path / "linked"
    _git(repo, "worktree", "add", "-q", "-b", branch, str(linked), "HEAD")
    return linked, branch


def test_non_worktree_tasks_are_unchanged():
    checked = verify_task_worktree(TaskStub("scratch", None, None))
    assert checked.ok


def test_clean_linked_worktree_passes(tmp_path):
    linked, branch = _linked_worktree(tmp_path)
    checked = verify_task_worktree(TaskStub("worktree", str(linked), branch))
    assert checked.ok, checked.reason


def test_dirty_worktree_is_rejected(tmp_path):
    linked, branch = _linked_worktree(tmp_path)
    (linked / "untracked.txt").write_text("not committed\n")
    checked = verify_task_worktree(TaskStub("worktree", str(linked), branch))
    assert not checked.ok
    assert "dirty" in checked.reason


def test_detached_or_wrong_branch_is_rejected(tmp_path):
    linked, branch = _linked_worktree(tmp_path)
    _git(linked, "checkout", "--detach", "-q")
    checked = verify_task_worktree(TaskStub("worktree", str(linked), branch))
    assert not checked.ok
    assert "detached" in checked.reason


@pytest.fixture
def kanban_db(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    kb.init_db()
    return home


def test_complete_task_records_and_rejects_bad_handoff(kanban_db, monkeypatch):
    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="handoff", assignee="builder")
        monkeypatch.setattr(
            kanban_worktree_guard,
            "verify_task_worktree",
            lambda _task: WorktreeCheck(False, "worktree is dirty"),
        )
        assert not kb.complete_task(conn, task_id, result="done")
        event = conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        assert event["kind"] == "completion_blocked_worktree"


def test_request_review_records_and_rejects_bad_handoff(kanban_db, monkeypatch):
    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="handoff", assignee="builder")
        monkeypatch.setattr(
            kanban_worktree_guard,
            "verify_task_worktree",
            lambda _task: WorktreeCheck(False, "worktree is detached"),
        )
        ok, reason = kb.request_review(
            conn, task_id, reviewer="reviewer", with_reason=True,
        )
        assert not ok
        assert reason == "worktree is detached"
        event = conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        assert event["kind"] == "review_blocked_worktree"
