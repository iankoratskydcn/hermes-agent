"""Integration test: board-level review_dispatch_enabled gate blocks review spawns.

TBCAF canary (2026-09-26): board metadata review_dispatch_enabled=false was ignored,
allowing stale review card spawn when the board gate was disabled. This test verifies
the compound AND gate: review rows are enumerated only when BOTH profile config
review_dispatch AND board metadata review_dispatch_enabled are true.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _fake_spawn_factory(spawns: list):
    """Capture spawn calls for inspection."""
    def fake_spawn(task, workspace, board=None):
        spawns.append({"task_id": task.id, "board": board, "lane": task.status})
        return 42
    return fake_spawn


def _set_task_status(conn, task_id: str, status: str) -> None:
    """Test helper: set task status directly."""
    conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, task_id))


class TestReviewDispatchBoardGate:
    """Verify board-level review_dispatch_enabled gates review spawns."""

    def test_board_review_dispatch_false_blocks_review_spawn(
        self, kanban_home, all_assignees_spawnable
    ):
        """Board metadata review_dispatch_enabled=false must block review spawn.

        Profile config has review_dispatch enabled (default), but board metadata
        explicitly disables it. Review cards should NOT be enumerated/spawned.
        """
        # Set up: board with review_dispatch_enabled=false
        kb.create_board("tbcaf")
        kb.write_board_metadata("tbcaf", review_dispatch_enabled=False)

        spawns: list = []
        with kbc.connect(board="tbcaf") as conn:
            # Create a review card in the review lane (already reviewed, waiting dispatch)
            review_id = kb.create_task(
                conn,
                title="review-card",
                assignee="alice"
            )
            _set_task_status(conn, review_id, "review")

            # Dispatch should NOT enumerate review rows when board gate is false
            result = kbd.dispatch_once(
                conn, spawn_fn=_fake_spawn_factory(spawns), board="tbcaf"
            )

        # Assertions:
        # - No spawns should have occurred for review lane
        assert len([s for s in spawns if s["lane"] == "review"]) == 0, \
            "Review card must NOT spawn when board review_dispatch_enabled=false"

        # - The review card should still be in review lane, unclaimed
        with kbc.connect(board="tbcaf") as conn:
            row = conn.execute(
                "SELECT status, claim_lock FROM tasks WHERE id = ?",
                (review_id,)
            ).fetchone()
            assert row["status"] == "review", \
                "Card must remain in review lane after dispatch gate blocks it"
            assert row["claim_lock"] is None, \
                "Card must not be claimed when review dispatch is disabled"

    def test_board_review_dispatch_true_allows_review_spawn(
        self, kanban_home, all_assignees_spawnable
    ):
        """Board metadata review_dispatch_enabled=true (or absent) allows spawn."""
        kb.create_board("open-review")
        # Explicitly set to true
        kb.write_board_metadata("open-review", review_dispatch_enabled=True)

        spawns: list = []
        with kbc.connect(board="open-review") as conn:
            review_id = kb.create_task(
                conn,
                title="review-card",
                assignee="alice"
            )
            _set_task_status(conn, review_id, "review")
            result = kbd.dispatch_once(
                conn, spawn_fn=_fake_spawn_factory(spawns), board="open-review"
            )

        # Review card SHOULD be spawned
        review_spawns = [s for s in spawns if s["lane"] == "review"]
        assert len(review_spawns) >= 1, \
            "Review card must spawn when board review_dispatch_enabled=true"
        assert any(s["task_id"] == review_id for s in review_spawns)

    def test_board_metadata_absent_defaults_to_true(
        self, kanban_home, all_assignees_spawnable
    ):
        """Board metadata with no review_dispatch_enabled key defaults to true (legacy)."""
        kb.create_board("legacy")
        # Do NOT set review_dispatch_enabled; key should be absent
        # write_board_metadata without review_dispatch_enabled leaves it missing

        spawns: list = []
        with kbc.connect(board="legacy") as conn:
            review_id = kb.create_task(
                conn,
                title="review-card",
                assignee="alice"
            )
            _set_task_status(conn, review_id, "review")
            result = kbd.dispatch_once(
                conn, spawn_fn=_fake_spawn_factory(spawns), board="legacy"
            )

        # Should allow spawn (default true for backward compatibility)
        review_spawns = [s for s in spawns if s["lane"] == "review"]
        assert len(review_spawns) >= 1, \
            "Absent board metadata key must default to true (legacy boards must keep working)"

    def test_compound_gate_profile_config_and_board_both_required(
        self, kanban_home, all_assignees_spawnable
    ):
        """Verify compound AND gate: both profile config AND board metadata enable spawn.

        Profile config: review_dispatch=true (default)
        Board metadata: review_dispatch_enabled=false

        Result: NO spawn (board false overrides profile true).
        """
        # Disable review dispatch at board level
        kb.create_board("tbcaf-repro")
        kb.write_board_metadata("tbcaf-repro", review_dispatch_enabled=False)

        # Profile config defaults to review_dispatch enabled (true)
        # No need to set it explicitly; the default is enabled.

        spawns: list = []
        with kbc.connect(board="tbcaf-repro") as conn:
            # Multiple review cards to ensure the gate blocks all of them
            review_ids = []
            for i in range(3):
                tid = kb.create_task(
                    conn,
                    title=f"review-{i}",
                    assignee="alice"
                )
                _set_task_status(conn, tid, "review")
                review_ids.append(tid)

            result = kbd.dispatch_once(
                conn, spawn_fn=_fake_spawn_factory(spawns), board="tbcaf-repro"
            )

        # The gate must block ALL review spawns
        review_spawns = [s for s in spawns if s["lane"] == "review"]
        assert len(review_spawns) == 0, \
            "Compound AND gate must block all review spawns when board false, " \
            f"even though profile config enables. Spawned: {spawns}"

        # All review cards remain in review lane, unclaimed
        with kbc.connect(board="tbcaf-repro") as conn:
            for tid in review_ids:
                row = conn.execute(
                    "SELECT status, claim_lock FROM tasks WHERE id = ?",
                    (tid,)
                ).fetchone()
                assert row["status"] == "review"
                assert row["claim_lock"] is None

    def test_review_gate_does_not_affect_ready_lane(
        self, kanban_home, all_assignees_spawnable
    ):
        """Review gate must not block ready tasks from dispatching.

        Verify that disabling review_dispatch_enabled does not affect the ready lane.
        """
        kb.create_board("mixed-lanes")
        kb.write_board_metadata("mixed-lanes", review_dispatch_enabled=False)

        spawns: list = []
        with kbc.connect(board="mixed-lanes") as conn:
            ready_id = kb.create_task(
                conn, title="ready-task", assignee="alice"
            )
            review_id = kb.create_task(
                conn, title="review-task", assignee="alice"
            )
            _set_task_status(conn, review_id, "review")

            result = kbd.dispatch_once(
                conn, spawn_fn=_fake_spawn_factory(spawns), board="mixed-lanes"
            )

        # Ready task SHOULD spawn
        ready_spawns = [s for s in spawns if s["lane"] == "ready"]
        assert len(ready_spawns) >= 1, \
            "Ready task must still spawn when review_dispatch is disabled"

        # Review task must NOT spawn
        review_spawns = [s for s in spawns if s["lane"] == "review"]
        assert len(review_spawns) == 0, \
            "Review task must not spawn when review_dispatch_enabled=false"

    def test_malformed_board_metadata_defaults_to_true(
        self, kanban_home, all_assignees_spawnable
    ):
        """Non-boolean review_dispatch_enabled value defaults safely to true."""
        kb.create_board("malformed")
        # Write invalid data (string instead of bool)
        kb.write_board_metadata("malformed", review_dispatch_enabled="yes")

        spawns: list = []
        with kbc.connect(board="malformed") as conn:
            review_id = kb.create_task(
                conn, title="review-card", assignee="alice"
            )
            _set_task_status(conn, review_id, "review")
            result = kbd.dispatch_once(
                conn, spawn_fn=_fake_spawn_factory(spawns), board="malformed"
            )

        # Must fail closed: treat non-false as true (default)
        # .get(..., True) will treat "yes" (truthy) as enabled → allow spawn
        review_spawns = [s for s in spawns if s["lane"] == "review"]
        assert len(review_spawns) >= 1, \
            "Malformed metadata (truthy non-bool) must allow spawn (fail-open for legacy)"
