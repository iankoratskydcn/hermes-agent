"""Integration tests for Wave2/2c: the deterministic gate precheck wired
into kanban_db_dispatch.py's per-task dispatch path.

Independent of the pure-function unit tests in
``tests/hermes_cli/test_kanban_gate_precheck.py`` — these exercise the REAL
``dispatch_once()`` -> ``_dispatch_lane_task()`` path against a real board.json
and a real sqlite DB, proving the board flag actually gates dispatch (and is
a true no-op when the board hasn't opted in, matching F1/F5's default-safe
precedent).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def fresh_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in (
        "HERMES_KANBAN_DB", "HERMES_KANBAN_WORKSPACES_ROOT",
        "HERMES_KANBAN_HOME", "HERMES_KANBAN_BOARD",
        "HERMES_DELEGATED_CHILD_CONTEXT",
    ):
        monkeypatch.delenv(var, raising=False)
    try:
        import hermes_constants
        hermes_constants._cached_default_hermes_root = None  # type: ignore[attr-defined]
    except Exception:
        pass
    kb._INITIALIZED_PATHS.clear()
    # F1 is default-gated and orthogonal to what this suite tests — bypass
    # it so the gate-precheck flag is the only thing under test, matching
    # the established pattern (see fix/kanban-gate-test-bypass commits).
    monkeypatch.setattr(kbd, "batch_approval_gate_ok", lambda board=None: (True, "test-bypass"))
    return home


class TestGatePrecheckDefaultOff:
    def test_flag_absent_does_not_block_a_task_that_would_fail_precheck(self, fresh_home):
        """Default False (unset board.json): even a task whose ears_sentence
        is garbage must dispatch normally — the precheck is a true no-op
        until an operator opts in, per F1/F5's default-safe precedent
        (this feature's risk profile is materially different from F1's, so
        it is deliberately opt-in rather than default-gated like F1)."""
        kb.create_board("proj")
        with kbc.connect(board="proj") as conn:
            task_id = kb.create_task(
                conn, title="t", assignee="default", board="proj",
                ears_sentence="not even close to an EARS sentence",
            )
            spawned = []
            result = kbd.dispatch_once(
                conn, board="proj",
                spawn_fn=lambda task, ws, board=None: spawned.append(task.id) or 1,
            )
        assert result.gate_blocked is None
        assert spawned == [task_id]
        assert result.skipped_gate_precheck == []


class TestGatePrecheckEnabled:
    def test_malformed_ears_sentence_blocks_dispatch_when_enabled(self, fresh_home):
        kb.create_board("proj")
        kb.write_board_metadata("proj", gate_precheck_enabled=True)
        with kbc.connect(board="proj") as conn:
            task_id = kb.create_task(
                conn, title="t", assignee="default", board="proj",
                ears_sentence="not even close to an EARS sentence",
            )
            spawned = []
            result = kbd.dispatch_once(
                conn, board="proj",
                spawn_fn=lambda task, ws, board=None: spawned.append(task.id) or 1,
            )
        assert spawned == []
        assert result.skipped_gate_precheck == [
            (task_id, "ears_sentence does not look EARS-shaped: missing the "
                       "mandatory 'shall' response keyword"),
        ]

    def test_wellformed_ears_sentence_dispatches_when_enabled(self, fresh_home):
        kb.create_board("proj")
        kb.write_board_metadata("proj", gate_precheck_enabled=True)
        with kbc.connect(board="proj") as conn:
            task_id = kb.create_task(
                conn, title="t", assignee="default", board="proj",
                ears_sentence="When the user clicks submit, the system shall save the form.",
            )
            spawned = []
            result = kbd.dispatch_once(
                conn, board="proj",
                spawn_fn=lambda task, ws, board=None: spawned.append(task.id) or 1,
            )
        assert spawned == [task_id]
        assert result.skipped_gate_precheck == []

    def test_unset_ears_sentence_is_not_gated_and_dispatches(self, fresh_home):
        """not_gated is a DISPATCHABLE status — this precheck validates
        shape when evidence is present, it never forces Rule 6 population
        as a hard gate (owner's dispatch-time-only enforcement decision)."""
        kb.create_board("proj")
        kb.write_board_metadata("proj", gate_precheck_enabled=True)
        with kbc.connect(board="proj") as conn:
            task_id = kb.create_task(conn, title="t", assignee="default", board="proj")
            spawned = []
            result = kbd.dispatch_once(
                conn, board="proj",
                spawn_fn=lambda task, ws, board=None: spawned.append(task.id) or 1,
            )
        assert spawned == [task_id]
        assert result.skipped_gate_precheck == []

    def test_fast_lane_bypasses_ears_check_when_enabled(self, fresh_home):
        """A single-file doc-glob task with no behavior-change language
        fast-lanes even when its (malformed) ears_sentence would otherwise
        block it — critique_06's "README typo forced through every gate"
        failure mode must not reproduce here."""
        kb.create_board("proj")
        kb.write_board_metadata("proj", gate_precheck_enabled=True)
        with kbc.connect(board="proj") as conn:
            task_id = kb.create_task(
                conn, title="update README typo", assignee="default", board="proj",
                ears_sentence="not an EARS sentence at all",
                scope_paths=["README.md"],
            )
            spawned = []
            result = kbd.dispatch_once(
                conn, board="proj",
                spawn_fn=lambda task, ws, board=None: spawned.append(task.id) or 1,
            )
        assert spawned == [task_id]
        assert result.skipped_gate_precheck == []

    def test_oversized_scope_blocks_dispatch_when_enabled(self, fresh_home):
        kb.create_board("proj")
        kb.write_board_metadata("proj", gate_precheck_enabled=True)
        with kbc.connect(board="proj") as conn:
            task_id = kb.create_task(
                conn, title="t", assignee="default", board="proj",
                ears_sentence="The system shall do the thing.",
                scope_paths=[f"file{i}.py" for i in range(6)],
            )
            spawned = []
            result = kbd.dispatch_once(
                conn, board="proj",
                spawn_fn=lambda task, ws, board=None: spawned.append(task.id) or 1,
            )
        assert spawned == []
        assert result.skipped_gate_precheck[0][0] == task_id
        assert "likely decomposable" in result.skipped_gate_precheck[0][1]
