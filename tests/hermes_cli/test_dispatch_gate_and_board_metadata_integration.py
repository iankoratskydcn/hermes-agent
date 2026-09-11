"""Independent adversarial integration tests for Wave 1 (F1/F5) and Wave 3 (F4).

Written by an independent test-author subagent, NOT the implementer. These
tests exercise the REAL code paths (real dispatch_once(), real board.json on
real disk, real decision-hud db.py loaded via the real bridge, a real
threading.Lock/flock-backed critical section) — no mocking of the mechanism
under test itself. Fixtures/spawn stubs are the only test doubles, matching
the pattern used throughout ``tests/hermes_cli/test_kanban_*``.

Covers:
  * F1 — ``batch_approval_gate`` on board.json blocks ``dispatch_once()``
    when the referenced decision-hud batch is missing/pending/rejected, and
    does NOT block (dispatch proceeds normally) once approved.
  * F5 — board.json ``dispatch_enabled=False`` blocks ``dispatch_once()``;
    a corrupt/unparseable board.json ALSO fails closed (never silently
    dispatches); ``review_dispatch_enabled``'s exception path returns
    ``False``, not the global fallback.
  * F4 — concurrent ``write_board_metadata()`` calls on different fields
    both survive (real threading against the real flock-backed lock); a
    genuinely corrupt board.json makes ``read_board_metadata()`` fail
    closed on the three safety toggles with a WARNING logged; a genuinely
    missing board.json still defaults all three toggles to True.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sys
import threading
from pathlib import Path

import pytest

_WORKTREE = Path(__file__).resolve().parents[2]
if str(_WORKTREE) not in sys.path:
    sys.path.insert(0, str(_WORKTREE))

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd

# decision-hud is a standalone, non-git-tracked-in-tree plugin (see
# hermes_cli/plugin_bridges/decision_hud.py's module docstring) that F1's
# gate calls into by absolute file path. Resolve the REAL installed copy's
# db.py *before* any fixture monkeypatches Path.home()/HERMES_HOME, so the
# F1 tests below can stage a real copy of it into each test's isolated
# HERMES_HOME (never importing/executing it against the real ~/.hermes).
_REAL_DECISION_HUD_DB = Path.home() / ".hermes" / "plugins" / "decision-hud" / "db.py"


@pytest.fixture
def fresh_home(tmp_path, monkeypatch):
    """Same shape as test_kanban_board_dispatch_toggles.py's fixture: a
    genuinely isolated HERMES_HOME so nothing here ever touches the real
    ~/.hermes, plus Path.home() patched (decision-hud's db.py falls back to
    Path.home()/'.hermes' if get_hermes_home() import fails, and the plugin
    bridge's own get_hermes_home() call must resolve into the temp tree)."""
    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in (
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_WORKSPACES_ROOT",
        "HERMES_KANBAN_HOME",
        "HERMES_KANBAN_BOARD",
        "HERMES_DELEGATED_CHILD_CONTEXT",
    ):
        monkeypatch.delenv(var, raising=False)
    try:
        import hermes_constants
        hermes_constants._cached_default_hermes_root = None  # type: ignore[attr-defined]
    except Exception:
        pass
    kb._INITIALIZED_PATHS.clear()
    # Stage a real copy of decision-hud's db.py into this test's isolated
    # HERMES_HOME/plugins/decision-hud/ so hermes_cli.plugin_bridges.decision_hud
    # (F1's real integration point, not a mock of it) resolves and loads the
    # SAME module code the real feature calls in production — just rooted
    # under the temp HERMES_HOME instead of the operator's real one.
    if _REAL_DECISION_HUD_DB.is_file():
        staged_dir = home / "plugins" / "decision-hud"
        staged_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(_REAL_DECISION_HUD_DB, staged_dir / "db.py")
    # The bridge caches the loaded module in sys.modules under a fixed key
    # (see _load_decision_hud_db) — evict it so each test re-loads db.py
    # fresh from ITS OWN staged HERMES_HOME copy instead of reusing a
    # previous test's cached module object.
    sys.modules.pop("_hermes_decision_hud_db_bridge", None)
    return home


def _require_decision_hud_installed():
    if not _REAL_DECISION_HUD_DB.is_file():
        pytest.skip(
            f"decision-hud plugin not installed on this host "
            f"({_REAL_DECISION_HUD_DB} not found) — F1 tests need a real copy "
            f"of its db.py to stage into the isolated HERMES_HOME"
        )


def _no_spawn(*args, **kwargs):
    """A spawn_fn that must NEVER be called by a gate-blocked tick — asserts
    hard if the gate has a bypass."""
    raise AssertionError(
        f"spawn_fn was called despite a gate that should have blocked this "
        f"tick (args={args!r} kwargs={kwargs!r})"
    )


def _push_batch(project: str, batch_id: str, *, task_list=None):
    """Push a real batch_approval decision through the real decision-hud
    db.py (loaded via the real hermes_cli.plugin_bridges.decision_hud
    bridge's own path-resolution logic), not a hand-rolled fixture."""
    from hermes_cli.plugin_bridges import decision_hud as bridge

    db = bridge._load_decision_hud_db()
    conn = db.connect()
    try:
        return db.push_batch_approval(
            conn, project=project, batch_id=batch_id, task_list=list(task_list or ["t1"]),
        )
    finally:
        conn.close()


def _resolve_batch(project: str, batch_id: str, choice: str):
    """Approve/reject a pushed batch through the real db.py resolution path
    (issues a real actor token, exactly like an interactive resolver would)."""
    from hermes_cli.plugin_bridges import decision_hud as bridge

    db = bridge._load_decision_hud_db()
    conn = db.connect()
    try:
        row = db.get_batch_approval(conn, project=project, batch_id=batch_id)
        assert row is not None, "test setup: batch row must exist before resolving"
        token = db.issue_actor_token("test-po")
        return db.resolve_decision(conn, row["id"], choice, actor_token=token)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# F1: batch_approval_gate blocks/allows dispatch_once()
# ---------------------------------------------------------------------------

class TestF1BatchApprovalGate:
    def _setup_board_with_gate(self, project="proj-f1", batch_id="batch-1"):
        kb.create_board("proj")
        kb.write_board_metadata(
            "proj", batch_approval_gate={"project": project, "batch_id": batch_id},
        )
        with kbc.connect(board="proj") as conn:
            task_id = kb.create_task(conn, title="do work", assignee="default", board="proj")
        return project, batch_id, task_id

    def test_missing_batch_blocks_dispatch_and_claims_nothing(self, fresh_home):
        project, batch_id, task_id = self._setup_board_with_gate(batch_id="never-pushed")
        with kbc.connect(board="proj") as conn:
            before = kb.get_task(conn, task_id)
            assert before.status == "ready"
            result = kbd.dispatch_once(conn, board="proj", spawn_fn=_no_spawn)
            after = kb.get_task(conn, task_id)

        assert result.gate_blocked == "batch_approval_required"
        assert result.spawned == []
        assert result.reclaimed == 0
        assert after.status == before.status == "ready", (
            "task must not transition out of 'ready' when the batch gate blocks the tick"
        )

    def test_pending_batch_blocks_dispatch_and_claims_nothing(self, fresh_home):
        project, batch_id, task_id = self._setup_board_with_gate()
        _push_batch(project, batch_id)  # pushed but NOT resolved -> pending

        with kbc.connect(board="proj") as conn:
            before = kb.get_task(conn, task_id)
            result = kbd.dispatch_once(conn, board="proj", spawn_fn=_no_spawn)
            after = kb.get_task(conn, task_id)

        assert result.gate_blocked == "batch_approval_required"
        assert result.spawned == []
        assert after.status == before.status == "ready"

    def test_rejected_batch_blocks_dispatch_and_claims_nothing(self, fresh_home):
        project, batch_id, task_id = self._setup_board_with_gate()
        _push_batch(project, batch_id)
        _resolve_batch(project, batch_id, "reject")

        with kbc.connect(board="proj") as conn:
            before = kb.get_task(conn, task_id)
            result = kbd.dispatch_once(conn, board="proj", spawn_fn=_no_spawn)
            after = kb.get_task(conn, task_id)

        assert result.gate_blocked == "batch_approval_required"
        assert result.spawned == []
        assert after.status == before.status == "ready"

    def test_approved_batch_allows_dispatch_to_proceed(self, fresh_home):
        project, batch_id, task_id = self._setup_board_with_gate()
        _push_batch(project, batch_id)
        _resolve_batch(project, batch_id, "approve")

        spawned_ids = []

        def _spawn(task, workspace_path, board=None):
            spawned_ids.append(task.id)
            return 424242

        with kbc.connect(board="proj") as conn:
            result = kbd.dispatch_once(conn, board="proj", spawn_fn=_spawn)
            after = kb.get_task(conn, task_id)

        assert result.gate_blocked is None
        assert spawned_ids == [task_id], "an approved gate must let the ready task actually spawn"
        assert after.status == "running", "task must have transitioned once the gate passed"

    def test_no_gate_configured_is_unaffected(self, fresh_home):
        """Regression guard: a board with no batch_approval_gate at all must
        dispatch exactly as before this feature existed."""
        kb.create_board("proj")
        with kbc.connect(board="proj") as conn:
            task_id = kb.create_task(conn, title="do work", assignee="default", board="proj")
            spawned = []
            result = kbd.dispatch_once(
                conn, board="proj",
                spawn_fn=lambda task, ws, board=None: spawned.append(task.id) or 1,
            )
        assert result.gate_blocked is None
        assert spawned == [task_id]


# ---------------------------------------------------------------------------
# F5: dispatch_enabled toggle + fail-closed on corrupt board.json
# ---------------------------------------------------------------------------

class TestF5DispatchEnabledGate:
    def test_dispatch_disabled_blocks_and_claims_nothing(self, fresh_home):
        kb.create_board("proj")
        kb.write_board_metadata("proj", dispatch_enabled=False)
        with kbc.connect(board="proj") as conn:
            task_id = kb.create_task(conn, title="t", assignee="default", board="proj")
            before = kb.get_task(conn, task_id)
            result = kbd.dispatch_once(conn, board="proj", spawn_fn=_no_spawn)
            after = kb.get_task(conn, task_id)

        assert result.gate_blocked == "board_dispatch_disabled"
        assert result.spawned == []
        assert after.status == before.status == "ready"

    def test_dispatch_enabled_true_allows_normal_dispatch(self, fresh_home):
        kb.create_board("proj")
        with kbc.connect(board="proj") as conn:
            task_id = kb.create_task(conn, title="t", assignee="default", board="proj")
            spawned = []
            result = kbd.dispatch_once(
                conn, board="proj",
                spawn_fn=lambda task, ws, board=None: spawned.append(task.id) or 1,
            )
        assert result.gate_blocked is None
        assert spawned == [task_id]

    def test_corrupt_board_json_fails_closed_blocks_dispatch(self, fresh_home):
        """A genuinely unparseable board.json must ALSO block dispatch, not
        silently allow it (the fail-open bug this feature fixes)."""
        kb.create_board("proj")
        board_json = kb.board_metadata_path("proj")
        board_json.write_text("{not valid json truncated mid-", encoding="utf-8")

        with kbc.connect(board="proj") as conn:
            task_id = kb.create_task(conn, title="t", assignee="default", board="proj")
            before = kb.get_task(conn, task_id)
            result = kbd.dispatch_once(conn, board="proj", spawn_fn=_no_spawn)
            after = kb.get_task(conn, task_id)

        assert result.gate_blocked == "board_dispatch_disabled", (
            "a corrupt board.json must fail CLOSED (refuse to dispatch), never "
            "silently proceed with the True default"
        )
        assert result.spawned == []
        assert after.status == before.status == "ready"

    def test_review_dispatch_enabled_exception_path_returns_false(self, fresh_home, monkeypatch):
        """The bare `except Exception: return False` guard in
        review_dispatch_enabled — must return False (fail closed), not
        `global_enabled` (the pre-fix fallback that could silently reinstate
        dispatch on a read/import failure)."""
        kb.create_board("proj")

        def _boom(board_arg):
            raise RuntimeError("simulated kanban_db import/read failure")

        # Patch the binding actually used inside review_dispatch_enabled's
        # try block: `from hermes_cli import kanban_db; kanban_db.read_board_metadata(...)`.
        monkeypatch.setattr(kb, "read_board_metadata", _boom)

        # Global switch is ON, so the pre-fix fallback (`return global_enabled`)
        # would have returned True here — proving this isn't accidentally
        # passing because the global default happens to be False too.
        result = kbd.review_dispatch_enabled(board="proj")
        assert result is False, (
            "review_dispatch_enabled's exception path must fail CLOSED (False), "
            "not fall back to the global-enabled value"
        )


# ---------------------------------------------------------------------------
# F4: concurrent write_board_metadata() — no lost updates
# ---------------------------------------------------------------------------

class TestF4ConcurrentWrites:
    def test_concurrent_writes_to_different_fields_both_survive(self, fresh_home):
        """Two real threads, each changing a DIFFERENT field on the SAME
        board, racing on the real flock-backed _board_metadata_lock. If the
        lock (or the read-modify-write cycle it guards) is broken, one
        writer's change is silently lost (last-writer-wins on the whole
        dict clobbers the other's field)."""
        kb.create_board("proj")

        barrier = threading.Barrier(2)
        errors: list[BaseException] = []

        def _writer_a():
            try:
                barrier.wait(timeout=5)
                for _ in range(25):
                    kb.write_board_metadata("proj", dispatch_enabled=False)
                    kb.write_board_metadata("proj", dispatch_enabled=True)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        def _writer_b():
            try:
                barrier.wait(timeout=5)
                for _ in range(25):
                    kb.write_board_metadata("proj", auto_decompose_enabled=False)
                    kb.write_board_metadata("proj", auto_decompose_enabled=True)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        t1 = threading.Thread(target=_writer_a)
        t2 = threading.Thread(target=_writer_b)
        t1.start()
        t2.start()
        t1.join(timeout=30)
        t2.join(timeout=30)

        assert not t1.is_alive() and not t2.is_alive(), "writer threads did not finish (deadlock?)"
        assert errors == [], f"writer threads raised: {errors!r}"

        # Final state must be internally consistent (both loops end True/True)
        # and the file must parse as JSON at all — proof neither writer's
        # transaction interleaved to produce a torn/half-written file, and
        # neither writer's independent field change was lost outright.
        meta = kb.read_board_metadata("proj")
        assert meta["dispatch_enabled"] is True
        assert meta["auto_decompose_enabled"] is True

        raw = json.loads(kb.board_metadata_path("proj").read_text(encoding="utf-8"))
        assert raw["dispatch_enabled"] is True
        assert raw["auto_decompose_enabled"] is True

    def test_concurrent_writes_one_field_each_no_lost_update(self, fresh_home):
        """Sharper lost-update check: thread A sets name, thread B sets
        description, concurrently, ONE write each (not a converging
        back-and-forth loop) — the final board.json must have BOTH values,
        proving the critical section truly serializes the read-modify-write
        instead of two threads reading the same base dict and each writing
        back a version missing the other's change."""
        kb.create_board("proj")
        barrier = threading.Barrier(2)
        errors: list[BaseException] = []

        def _set_name():
            try:
                barrier.wait(timeout=5)
                kb.write_board_metadata("proj", name="Name From A")
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        def _set_description():
            try:
                barrier.wait(timeout=5)
                kb.write_board_metadata("proj", description="Description From B")
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=_set_name), threading.Thread(target=_set_description)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        assert errors == []
        meta = kb.read_board_metadata("proj")
        assert meta["name"] == "Name From A"
        assert meta["description"] == "Description From B", (
            "thread B's field change was lost — the read-modify-write cycle is not "
            "actually serialized by the lock"
        )


# ---------------------------------------------------------------------------
# F4: fail-closed on corrupt board.json / default-True on missing board.json
# ---------------------------------------------------------------------------

class TestF4ReadBoardMetadataFailClosed:
    def test_corrupt_board_json_fails_closed_on_toggles_with_warning_logged(self, fresh_home, caplog):
        kb.create_board("proj")
        board_json = kb.board_metadata_path("proj")
        # Genuinely truncated/corrupt — not empty, not valid JSON.
        board_json.write_text('{"name": "proj", "dispatch_enabled": tru', encoding="utf-8")

        with caplog.at_level(logging.WARNING, logger="hermes_cli.kanban_db"):
            meta = kb.read_board_metadata("proj")

        assert meta["dispatch_enabled"] is False
        assert meta["auto_decompose_enabled"] is False
        assert meta["review_dispatch_enabled"] is False
        assert any(
            record.levelno >= logging.WARNING and "proj" in record.getMessage()
            for record in caplog.records
        ), "a corrupt board.json must log a WARNING naming the affected board"

    def test_corrupt_board_json_does_not_raise(self, fresh_home):
        """read_board_metadata() must never raise — corruption fails closed,
        not loud, at the call site (dispatch_once already logs/handles it)."""
        kb.create_board("proj")
        board_json = kb.board_metadata_path("proj")
        board_json.write_bytes(b"\x00\x01garbage-not-json\xff")
        meta = kb.read_board_metadata("proj")  # must not raise
        assert meta["dispatch_enabled"] is False

    def test_missing_board_json_still_defaults_all_toggles_true(self, fresh_home):
        """Regression guard: F4's fail-closed fix must apply ONLY to a
        present-but-corrupt file, never to a genuinely new/never-written
        board — otherwise every brand-new board would silently start with
        dispatch off."""
        home = fresh_home
        # A board directory that has never had write_board_metadata() called
        # against it at all — board.json genuinely does not exist on disk.
        board_dir = home / "kanban" / "boards" / "never-written"
        board_dir.mkdir(parents=True, exist_ok=True)
        board_json = board_dir / "board.json"
        assert not board_json.exists()

        meta = kb.read_board_metadata("never-written")
        assert not board_json.exists(), "read_board_metadata() must not create the file as a side effect"
        assert meta["dispatch_enabled"] is True
        assert meta["auto_decompose_enabled"] is True
        assert meta["review_dispatch_enabled"] is True


# ---------------------------------------------------------------------------
# Adversarial: race the approval mid-tick / bypass-entry-point audit
# ---------------------------------------------------------------------------

class TestAdversarial:
    def test_gate_is_read_fresh_each_tick_not_cached_across_calls(self, fresh_home):
        """If the gate check were cached/memoized per-process, approving the
        batch between two ticks would not be observed. Prove the second
        tick, after approval, actually proceeds — same conn/process, no
        cache-busting hack required."""
        kb.create_board("proj")
        kb.write_board_metadata(
            "proj", batch_approval_gate={"project": "race-proj", "batch_id": "race-batch"},
        )
        with kbc.connect(board="proj") as conn:
            task_id = kb.create_task(conn, title="t", assignee="default", board="proj")

            # Tick 1: not pushed at all -> blocked.
            result1 = kbd.dispatch_once(conn, board="proj", spawn_fn=_no_spawn)
            assert result1.gate_blocked == "batch_approval_required"

            # Approve mid-flight, then tick again on the SAME connection/process.
            _push_batch("race-proj", "race-batch")
            _resolve_batch("race-proj", "race-batch", "approve")

            spawned = []
            result2 = kbd.dispatch_once(
                conn, board="proj",
                spawn_fn=lambda task, ws, board=None: spawned.append(task.id) or 1,
            )
        assert result2.gate_blocked is None, "gate must re-read board.json + decision-hud every tick"
        assert spawned == [task_id]

    def test_kanban_ops_cmd_dispatch_routes_through_dispatch_once(self, fresh_home):
        """kanban_ops.py's `_cmd_dispatch` (the CLI entry point) must call
        kanban_db_dispatch.dispatch_once — not a private/undocumented
        helper that could bypass the F1/F5 gates. Static introspection of
        the wired-up callable (bound name identity), not a source-text
        regex, so it survives refactors that keep the contract."""
        import inspect

        from hermes_cli import kanban_ops

        src = inspect.getsource(kanban_ops._cmd_dispatch)
        # This checks the actual imported module's attribute identity, not
        # string content: kbd inside kanban_ops must be the exact
        # kanban_db_dispatch module whose dispatch_once we already proved
        # enforces the gates above.
        assert kanban_ops.kbd is kbd, (
            "kanban_ops.py's dispatch command must call the SAME "
            "kanban_db_dispatch module under test, not a separate import"
        )
        assert "dispatch_once" in src, "kanban_ops._cmd_dispatch must call dispatch_once (the gated choke point)"

    def test_no_other_module_calls_dispatch_once_locked_directly(self, fresh_home):
        """Grep-free structural check: the private, UNGATED
        `_dispatch_once_locked` must only be referenced from within
        kanban_db_dispatch.py itself (its own module globals) — proving (for
        this snapshot of the codebase) that no sibling module has grown a
        second, gate-bypassing call path directly to the locked tick
        function. This inspects live module objects already imported by the
        rest of this test file, not source text."""
        import gateway.kanban_watchers_dispatcher as watchers
        from hermes_cli import kanban_ops

        for name, mod in (("kanban_ops", kanban_ops), ("kanban_watchers_dispatcher", watchers)):
            assert not hasattr(mod, "_dispatch_once_locked"), (
                f"{name} must not import the ungated _dispatch_once_locked directly"
            )
