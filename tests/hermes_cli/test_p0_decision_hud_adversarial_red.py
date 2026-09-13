"""Independent RED tests for P0 decision-hud dispatch race/mutation blockers."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def isolated_kanban(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    return home


def _board_with_gate(board: str = "p0") -> str:
    kb.create_board(board)
    gate = {"project": "p0-project", "batch_id": "approved-batch"}
    kb.write_board_metadata(board, dispatch_enabled=True, batch_approval_gate=gate)
    with kbc.connect(board=board) as conn:
        return kb.create_task(conn, title="p0 task", assignee="worker", board=board)


def _durable_db_snapshot(conn):
    tables = [
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        ).fetchall()
        if not row["name"].startswith("sqlite_")
    ]
    snapshot = {}
    for table in tables:
        columns = [row["name"] for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall()]
        order = ", ".join(f'"{column}"' for column in columns)
        rows = conn.execute(f'SELECT {order} FROM "{table}" ORDER BY rowid').fetchall()
        snapshot[table] = [tuple(row[column] for column in columns) for row in rows]
    return snapshot


def test_contended_real_dispatch_is_skipped_without_spawn_or_gate_consumption(
    isolated_kanban, monkeypatch
):
    task_id = _board_with_gate()
    monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: lambda _name: True)
    monkeypatch.setattr(kbd, "batch_approval_gate_ok", lambda _board: (True, "approved"))

    metadata_writes = 0
    metadata_lock = threading.Lock()
    real_write_metadata = kb.write_board_metadata

    def counted_write_metadata(*args, **kwargs):
        nonlocal metadata_writes
        with metadata_lock:
            metadata_writes += 1
        return real_write_metadata(*args, **kwargs)

    monkeypatch.setattr(kb, "write_board_metadata", counted_write_metadata)
    winner_spawned = threading.Event()
    release_winner = threading.Event()
    spawn_calls: list[str] = []
    loser_spawn_calls: list[str] = []
    results: dict[str, object] = {}
    errors: list[BaseException] = []

    def spawn(task, workspace_path, board=None):
        spawn_calls.append(task.id)
        winner_spawned.set()
        assert release_winner.wait(timeout=5)
        return 4242

    def run(name: str, spawn_fn):
        try:
            with kbc.connect(board="p0") as conn:
                results[name] = kbd.dispatch_once(conn, board="p0", spawn_fn=spawn_fn)
        except BaseException as exc:  # pragma: no cover - expose thread failures
            errors.append(exc)

    first = threading.Thread(target=run, args=("winner", spawn))
    first.start()
    assert winner_spawned.wait(timeout=5), "first real dispatch must reach spawn while holding the lock"

    second = threading.Thread(
        target=run,
        args=("loser", lambda task, *_args, **_kwargs: loser_spawn_calls.append(task.id) or 9999),
    )
    second.start()
    second.join(timeout=5)
    assert not second.is_alive(), "contended dispatch must not wait for the canonical lock"

    loser = results["loser"]
    assert loser.skipped_locked is True
    assert loser.spawned == []
    assert loser_spawn_calls == []
    assert metadata_writes == 1, "only the winning dispatch may consume the approval gate"

    release_winner.set()
    first.join(timeout=10)
    assert errors == []
    assert spawn_calls == [task_id]


def test_approved_dry_run_is_durable_state_noop_including_reclaim_state(
    isolated_kanban, monkeypatch
):
    task_id = _board_with_gate()
    monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: lambda _name: True)
    monkeypatch.setattr(kbd, "batch_approval_gate_ok", lambda _board: (True, "approved"))

    with kbc.connect(board="p0") as conn:
        # Leave a genuinely expired running claim beside the ready task so the
        # reclaim phase has durable state it would mutate on a real tick.
        stale_id = kb.create_task(
            conn, title="stale task", assignee="worker", board="p0", initial_status="running",
        )
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'running', claim_expires = 0, "
                "claim_lock = 'host:stale-worker', worker_pid = NULL "
                "WHERE id = ?",
                (stale_id,),
            )
        before_db = _durable_db_snapshot(conn)
        before_gate = kb.read_board_metadata("p0")
        spawned: list[str] = []

        result = kbd.dispatch_once(
            conn,
            board="p0",
            dry_run=True,
            spawn_fn=lambda task, _workspace, board=None: spawned.append(task.id) or 123,
        )

        after_db = _durable_db_snapshot(conn)
        after_gate = kb.read_board_metadata("p0")

    assert result.reclaimed == 0
    assert result.reconciled_orphans == []
    assert result.crashed == []
    assert result.stale == []
    assert result.timed_out == []
    assert result.auto_blocked == []
    assert result.rate_limited == []
    assert spawned == []
    assert after_db == before_db
    assert after_gate == before_gate
    assert after_gate["batch_approval_gate"] == {
        "project": "p0-project",
        "batch_id": "approved-batch",
    }


def test_gate_replacement_between_approval_read_and_consume_fails_closed(
    isolated_kanban, monkeypatch
):
    task_id = _board_with_gate()
    monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: lambda _name: True)
    approval_read = threading.Event()
    replacement_written = threading.Event()
    replacement = {"project": "replacement-project", "batch_id": "replacement-batch"}

    def approval_read_then_pause(board):
        observed = kb.read_board_metadata(board)["batch_approval_gate"]
        assert observed == {"project": "p0-project", "batch_id": "approved-batch"}
        approval_read.set()
        assert replacement_written.wait(timeout=5)
        return True, "approved"

    monkeypatch.setattr(kbd, "batch_approval_gate_ok", approval_read_then_pause)
    spawned: list[str] = []
    result_box: list = []
    errors: list[BaseException] = []

    def dispatch():
        try:
            with kbc.connect(board="p0") as conn:
                result_box.append(
                    kbd.dispatch_once(
                        conn,
                        board="p0",
                        spawn_fn=lambda task, _workspace, board=None: spawned.append(task.id) or 4242,
                    )
                )
        except BaseException as exc:  # pragma: no cover - expose thread failures
            errors.append(exc)

    worker = threading.Thread(target=dispatch)
    worker.start()
    assert approval_read.wait(timeout=5)
    kb.write_board_metadata("p0", batch_approval_gate=replacement)
    replacement_written.set()
    worker.join(timeout=10)

    assert errors == []
    assert len(result_box) == 1
    assert result_box[0].gate_blocked == "batch_approval_consumption_failed"
    assert spawned == []
    assert kb.read_board_metadata("p0")["batch_approval_gate"] == replacement
    with kbc.connect(board="p0") as conn:
        assert kb.get_task(conn, task_id).status == "ready"
