"""Tests for kanban lifecycle plugin hooks.

Verifies that claim/complete/block transitions fire the
kanban_task_claimed / kanban_task_completed / kanban_task_blocked plugin
hooks AFTER the board DB change is committed, with the documented kwargs,
and that a misbehaving hook callback never breaks the transition.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli.plugins import get_plugin_manager


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def captured_hooks(monkeypatch):
    """Register capturing callbacks for the three kanban lifecycle hooks.

    Patches the plugin manager's _hooks dict directly (the same registry
    invoke_hook reads) and restores it afterward.
    """
    mgr = get_plugin_manager()
    events: list[tuple[str, dict]] = []
    saved = {k: list(v) for k, v in mgr._hooks.items()}
    for hook in ("kanban_task_claimed", "kanban_task_completed", "kanban_task_blocked"):
        mgr._hooks.setdefault(hook, []).append(
            lambda _h=hook, **kw: events.append((_h, kw))
        )
    try:
        yield events
    finally:
        mgr._hooks = saved


def test_claim_fires_hook(kanban_home, captured_hooks):
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="t", assignee="worker")
        claimed = kb.claim_task(conn, tid)
        assert claimed is not None
    finally:
        conn.close()
    fired = [e for e in captured_hooks if e[0] == "kanban_task_claimed"]
    assert len(fired) == 1
    kw = fired[0][1]
    assert kw["task_id"] == tid
    assert kw["assignee"] == "worker"
    assert "profile_name" in kw
    assert kw["run_id"] is not None


def test_misbehaving_hook_does_not_break_transition(kanban_home, monkeypatch):
    """A hook callback that raises must not break the board transition."""
    mgr = get_plugin_manager()
    saved = {k: list(v) for k, v in mgr._hooks.items()}

    def _boom(**kw):
        raise RuntimeError("plugin exploded")

    mgr._hooks.setdefault("kanban_task_completed", []).append(_boom)
    try:
        conn = kbc.connect()
        try:
            tid = kb.create_task(conn, title="t", assignee="worker")
            kb.claim_task(conn, tid)
            # Despite the raising hook, completion succeeds and persists.
            assert kb.complete_task(conn, tid, summary="ok") is True
            assert kb.get_task(conn, tid).status == "done"
        finally:
            conn.close()
    finally:
        mgr._hooks = saved


def test_create_task_hook_flushes_after_caller_managed_commit(kanban_home, monkeypatch):
    """A caller-owned BEGIN/COMMIT boundary still delivers after durability."""
    conn = kbc.connect()
    observed: list[bool] = []
    monkeypatch.setattr(
        kb,
        "_fire_kanban_lifecycle_hook",
        lambda event, *_args, **_kwargs: observed.append(conn.in_transaction),
    )
    try:
        conn.execute("BEGIN")
        task_id = kb.create_task(conn, title="external", assignee="worker")
        assert task_id
        assert observed == []
        conn.commit()
        assert observed == [False]
    finally:
        conn.close()


def test_caller_managed_rollback_discards_create_hook(kanban_home, monkeypatch):
    conn = kbc.connect()
    observed: list[str] = []
    monkeypatch.setattr(
        kb,
        "_fire_kanban_lifecycle_hook",
        lambda event, *_args, **_kwargs: observed.append(event),
    )
    try:
        conn.execute("BEGIN")
        task_id = kb.create_task(conn, title="rolled back", assignee="worker")
        conn.rollback()
        assert kb.get_task(conn, task_id) is None
        assert observed == []
    finally:
        conn.close()


def test_nested_rollback_discards_only_inner_lifecycle_hooks(kanban_home, monkeypatch):
    conn = kbc.connect()
    observed: list[str] = []
    monkeypatch.setattr(
        kb,
        "_fire_kanban_lifecycle_hook",
        lambda event, *_args, **_kwargs: observed.append(event),
    )
    try:
        with kbc.write_txn(conn):
            root_id = kb.create_task(conn, title="root", assignee="worker")
            with pytest.raises(RuntimeError, match="inner"):
                with kbc.write_txn(conn, allow_nested=True):
                    kb.create_task(conn, title="inner", assignee="worker")
                    raise RuntimeError("inner")
            assert kb.get_task(conn, root_id) is not None
        assert observed == ["on_kanban_task_created"]
        assert conn.execute("SELECT COUNT(*) FROM tasks WHERE title = 'inner'").fetchone()[0] == 0
    finally:
        conn.close()


def test_deferred_state_keeps_connection_identity(kanban_home):
    conn = kbc.connect()
    try:
        kb._begin_deferred_lifecycle_frame(conn)
        state = kb._deferred_lifecycle_state(conn)
        assert state is not None
        assert state.conn is conn
        assert kb._has_deferred_lifecycle_state(conn)
        kb._discard_deferred_lifecycle_hooks(conn)
        assert not kb._has_deferred_lifecycle_state(conn)
    finally:
        conn.close()
