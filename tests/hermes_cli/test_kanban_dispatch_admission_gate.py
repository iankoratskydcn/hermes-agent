"""Rule-1/emergency-stop dispatch admission gate.

``dispatch_once`` is the single real claim/spawn choke point called by every
production entry point: the embedded gateway watcher
(``gateway/kanban_watchers_dispatcher.py``), the dashboard ``POST /dispatch``
route (``plugins/kanban/dashboard/plugin_api.py``), ``hermes kanban dispatch``
/ the standalone daemon (``hermes_cli/kanban_ops.py``). Before this gate, only
the embedded gateway watcher pre-checked ``dispatch_enabled``/ESTOP before
calling ``dispatch_once`` — direct callers (dashboard, CLI, daemon) called
``dispatch_once`` unconditionally and could dispatch work even while an
operator had paused Hermes (``hermes pause``) or explicitly disabled dispatch
for a board. These tests prove the gate is now enforced INSIDE
``dispatch_once`` itself, so no caller can bypass it, and that it fails
CLOSED on error.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent import estop
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    yield home
    # Never leave a real ESTOP sentinel armed for a later test in this process.
    estop.disengage()


@pytest.fixture
def conn(kanban_home):
    with kbc.connect() as c:
        yield c


def _spy_spawn():
    calls: list = []

    def spawn(task, workspace_path, board=None):
        calls.append(getattr(task, "id", task))
        return 999999

    spawn.calls = calls
    return spawn


def test_estop_blocks_dispatch_once_directly(conn):
    """A direct dispatch_once() call — the dashboard/CLI/daemon path, not the
    gateway watcher — must refuse to claim/spawn while ESTOP is engaged."""
    kb.create_task(conn, title="t", assignee="w")
    spawn = _spy_spawn()

    estop.engage(reason="adversarial-test")
    result = kbd.dispatch_once(conn, spawn_fn=spawn)

    assert result.admission_blocked == "estop"
    assert spawn.calls == []
    assert result.spawned == []


def test_resume_lifts_the_estop_admission_block(conn):
    kb.create_task(conn, title="t", assignee="w")
    spawn = _spy_spawn()

    estop.engage(reason="adversarial-test")
    blocked = kbd.dispatch_once(conn, spawn_fn=spawn)
    assert blocked.admission_blocked == "estop"

    estop.disengage()
    result = kbd.dispatch_once(conn, spawn_fn=spawn)

    # The admission gate itself must be lifted; whether a spawn actually
    # happens depends on unrelated dispatch mechanics (assignee/profile
    # existence, capacity) that are out of scope for this admission-gate test.
    assert result.admission_blocked is None


def test_board_dispatch_disabled_blocks_dispatch_once_directly(conn):
    """A direct dispatch_once() call must honor a board's own dispatch_enabled=False
    even when the caller never checked board metadata itself (the dashboard's
    POST /dispatch route calls dispatch_once() with no such pre-check)."""
    kb.create_task(conn, title="t", assignee="w")
    spawn = _spy_spawn()

    kb.write_board_metadata("default", dispatch_enabled=False)
    result = kbd.dispatch_once(conn, spawn_fn=spawn, board="default")

    assert result.admission_blocked == "board_dispatch_disabled"
    assert spawn.calls == []


def test_board_dispatch_reenabled_lifts_the_block(conn):
    kb.create_task(conn, title="t", assignee="w")
    spawn = _spy_spawn()

    kb.write_board_metadata("default", dispatch_enabled=False)
    assert kbd.dispatch_once(conn, spawn_fn=spawn, board="default").admission_blocked == "board_dispatch_disabled"

    kb.write_board_metadata("default", dispatch_enabled=True)
    result = kbd.dispatch_once(conn, spawn_fn=spawn, board="default")

    assert result.admission_blocked is None


def test_dry_run_bypasses_the_admission_gate(conn):
    """dry_run must still work while paused/disabled — it previews, it never
    claims or spawns, so there is nothing for the gate to protect against."""
    kb.create_task(conn, title="t", assignee="w")
    estop.engage(reason="adversarial-test")

    result = kbd.dispatch_once(conn, dry_run=True)

    assert result.admission_blocked is None


def test_admission_check_fails_closed_on_board_metadata_error(conn, monkeypatch):
    """A malformed/unreadable board.json (or any other read_board_metadata
    failure) must BLOCK dispatch, not silently admit it."""
    kb.create_task(conn, title="t", assignee="w")
    spawn = _spy_spawn()

    def boom(board=None):
        raise OSError("simulated corrupt board.json")

    monkeypatch.setattr(kb, "read_board_metadata", boom)
    result = kbd.dispatch_once(conn, spawn_fn=spawn, board="default")

    assert result.admission_blocked == "board_dispatch_disabled"
    assert spawn.calls == []


def test_admission_helper_fails_closed_when_estop_check_raises(monkeypatch):
    """Any unexpected exception resolving the ESTOP state (not ImportError,
    which has its own documented fail-open contract) must block, not admit."""

    def boom():
        raise RuntimeError("simulated estop state corruption")

    monkeypatch.setattr(
        "gateway.kanban_watchers_common._kanban_dispatch_allowed", boom,
    )
    assert kbd._dispatch_admission_blocked_reason(None) == "estop"
