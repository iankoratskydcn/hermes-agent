"""Independent RED acceptance tests for the three confirmed P0 defects."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli.plugin_bridges import decision_hud as bridge


@pytest.fixture
def isolated_kanban(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    return home


def _new_board_task(board: str = "p0") -> str:
    kb.create_board(board)
    kb.write_board_metadata(board, dispatch_enabled=True)
    with kbc.connect(board=board) as conn:
        return kb.create_task(conn, title="p0 task", assignee="worker", board=board)


def test_db_path_resolution_failure_fails_closed_without_spawning(
    isolated_kanban, monkeypatch
):
    task_id = _new_board_task()
    monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: lambda _name: True)
    monkeypatch.setattr(kbd, "_board_dispatch_gate_ok", lambda _board: True)
    monkeypatch.setattr(kbd, "batch_approval_gate_ok", lambda _board: (True, "approved"))
    # Keep the task's board metadata readable; the injected failure is only the
    # dispatch lock's DB-path resolution seam.
    monkeypatch.setattr(kbd._kb, "read_board_metadata", lambda _board: {"dispatch_enabled": True})
    spawned: list[str] = []

    def spawn(task, workspace_path, board=None):
        spawned.append(task.id)
        return 1234

    # Open the real connection before breaking path resolution: the dispatch
    # boundary must still refuse to run even when its lock-path lookup fails.
    with kbc.connect(board="p0") as conn:
        monkeypatch.setattr(
            kb,
            "kanban_db_path",
            lambda board=None: (_ for _ in ()).throw(OSError("cannot resolve kanban db")),
        )
        result = kbd.dispatch_once(conn, board="p0", spawn_fn=spawn)
        task = kb.get_task(conn, task_id)

    assert spawned == []
    assert result.spawned == []
    assert result.skipped_locked or result.gate_blocked is not None
    assert task.status == "ready"


def test_concurrent_real_dispatch_has_one_gate_consumer_and_one_dispatcher(
    isolated_kanban, monkeypatch
):
    task_id = _new_board_task()
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
    spawn_lock = threading.Lock()

    def spawn(task, workspace_path, board=None):
        with spawn_lock:
            spawn_calls.append(task.id)
        winner_spawned.set()
        assert release_winner.wait(timeout=5)
        return 5678

    results: list = []
    errors: list[BaseException] = []
    results_lock = threading.Lock()

    def dispatch_in_thread():
        try:
            with kbc.connect(board="p0") as conn:
                result = kbd.dispatch_once(conn, board="p0", spawn_fn=spawn)
            with results_lock:
                results.append(result)
        except BaseException as exc:  # pragma: no cover - makes thread failures visible
            with results_lock:
                errors.append(exc)

    threads = [threading.Thread(target=dispatch_in_thread) for _ in range(2)]
    for thread in threads:
        thread.start()
    assert winner_spawned.wait(timeout=5), "one dispatch must reach the spawn seam"
    release_winner.set()
    for thread in threads:
        thread.join(timeout=10)

    assert errors == []
    assert len(results) == 2
    assert spawn_calls == [task_id]
    assert metadata_writes == 1
    assert sum(bool(result.spawned) for result in results) == 1
    assert sum(not result.spawned for result in results) == 1


def _write_profile_plugin(home: Path, marker: str) -> None:
    plugin_dir = home / "plugins" / "decision-hud"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    database = plugin_dir / "queue.db"
    plugin = plugin_dir / "db.py"
    plugin.write_text(
        "import sqlite3\n"
        f"MARKER = {marker!r}\n"
        f"_DB = {str(database)!r}\n"
        "def db_path():\n    return _DB\n"
        "def connect():\n    return sqlite3.connect(_DB)\n",
        encoding="utf-8",
    )
    with sqlite3.connect(database) as conn:
        conn.execute("PRAGMA user_version = 6")


def test_same_process_profile_switch_loads_the_new_decision_hud_module(
    tmp_path, monkeypatch
):
    first_home = tmp_path / "first" / ".hermes"
    second_home = tmp_path / "second" / ".hermes"
    first_home.mkdir(parents=True)
    second_home.mkdir(parents=True)
    _write_profile_plugin(first_home, "first-profile")
    _write_profile_plugin(second_home, "second-profile")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(first_home))
    monkeypatch.delitem(__import__("sys").modules, bridge._MODULE_CACHE_KEY, raising=False)

    first = bridge._load_decision_hud_db()
    monkeypatch.setenv("HERMES_HOME", str(second_home))
    second = bridge._load_decision_hud_db()

    assert first.MARKER == "first-profile"
    assert second.MARKER == "second-profile"
    assert first is not second
