"""Integration tests: the rolling-window provider-quota guard wired into
kanban_db_dispatch.py's per-task dispatch path.

Mirrors ``tests/hermes_cli/test_kanban_gate_precheck_dispatch_integration.py``
— exercises the REAL ``dispatch_once()`` -> ``_dispatch_lane_task()`` path
against a real sqlite DB, proving ``kanban.provider_budgets`` actually gates
dispatch and is a true no-op when unset, matching this repo's default-safe
precedent for opt-in dispatch guards.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _config_with(monkeypatch, provider_budgets):
    import hermes_cli.config as cfgmod

    cfg = {"kanban": {"provider_budgets": provider_budgets}}
    monkeypatch.setattr(cfgmod, "load_config", lambda *a, **k: cfg)


def _fake_spawn_factory(spawns: list):
    def fake_spawn(task, workspace, board=None):
        spawns.append(task.id)
        return 42
    return fake_spawn


def _seed_rate_limited_run(conn, *, profile, ended_at):
    """Insert a closed task_runs row shaped like a real rate-limited exit
    (``_classify_worker_exit`` -> ``outcome='rate_limited'``), independent of
    any particular task so the pooling behavior is isolated from task state."""
    conn.execute(
        "INSERT INTO task_runs (task_id, profile, status, outcome, started_at, ended_at) "
        "VALUES (?, ?, 'done', 'rate_limited', ?, ?)",
        (f"seed-{profile}-{ended_at}", profile, ended_at - 1, ended_at),
    )
    conn.commit()


class TestProviderBudgetDefaultOff:
    def test_unset_config_does_not_block_dispatch(self, kanban_home, all_assignees_spawnable, monkeypatch):
        import hermes_cli.config as cfgmod
        monkeypatch.setattr(cfgmod, "load_config", lambda *a, **k: {})

        spawns: list = []
        with kbc.connect() as conn:
            now = int(time.time())
            _seed_rate_limited_run(conn, profile="alice", ended_at=now - 5)
            _seed_rate_limited_run(conn, profile="alice", ended_at=now - 2)
            task_id = kb.create_task(conn, title="t", assignee="alice")
            result = kbd.dispatch_once(conn, spawn_fn=_fake_spawn_factory(spawns))

        assert spawns == [task_id]
        assert result.skipped_gate_precheck == []  # sanity: unrelated guard untouched

    def test_empty_provider_budgets_does_not_block_dispatch(
        self, kanban_home, all_assignees_spawnable, monkeypatch,
    ):
        _config_with(monkeypatch, {})

        spawns: list = []
        with kbc.connect() as conn:
            task_id = kb.create_task(conn, title="t", assignee="alice")
            result = kbd.dispatch_once(conn, spawn_fn=_fake_spawn_factory(spawns))

        assert spawns == [task_id]


class TestProviderBudgetEnabled:
    def test_over_budget_account_blocks_dispatch(self, kanban_home, all_assignees_spawnable, monkeypatch):
        _config_with(monkeypatch, {
            "acct-a": {"profiles": ["alice"], "max_rate_limit_hits": 2},
        })

        spawns: list = []
        with kbc.connect() as conn:
            now = int(time.time())
            _seed_rate_limited_run(conn, profile="alice", ended_at=now - 10)
            _seed_rate_limited_run(conn, profile="alice", ended_at=now - 5)
            task_id = kb.create_task(conn, title="t", assignee="alice")
            result = kbd.dispatch_once(conn, spawn_fn=_fake_spawn_factory(spawns))

        assert spawns == []
        assert result.provider_budget_blocked == [(task_id, "provider_budget_exhausted:acct-a")]

    def test_budget_pools_across_every_profile_in_the_account(
        self, kanban_home, all_assignees_spawnable, monkeypatch,
    ):
        """The real gap this closes: a fan-out task for 'bob' must be blocked
        by rate-limit hits recorded under 'alice', because they share one
        provider account's credentials."""
        _config_with(monkeypatch, {
            "acct-a": {"profiles": ["alice", "bob"], "max_rate_limit_hits": 2},
        })

        spawns: list = []
        with kbc.connect() as conn:
            now = int(time.time())
            _seed_rate_limited_run(conn, profile="alice", ended_at=now - 10)
            _seed_rate_limited_run(conn, profile="bob", ended_at=now - 5)
            task_id = kb.create_task(conn, title="t", assignee="alice")
            result = kbd.dispatch_once(conn, spawn_fn=_fake_spawn_factory(spawns))

        assert spawns == []
        assert result.provider_budget_blocked == [(task_id, "provider_budget_exhausted:acct-a")]

    def test_unmanaged_assignee_dispatches_normally(self, kanban_home, all_assignees_spawnable, monkeypatch):
        _config_with(monkeypatch, {
            "acct-a": {"profiles": ["alice"], "max_rate_limit_hits": 1},
        })

        spawns: list = []
        with kbc.connect() as conn:
            now = int(time.time())
            _seed_rate_limited_run(conn, profile="alice", ended_at=now - 5)
            task_id = kb.create_task(conn, title="t", assignee="carol")
            result = kbd.dispatch_once(conn, spawn_fn=_fake_spawn_factory(spawns))

        assert spawns == [task_id]
        assert result.provider_budget_blocked == []

    def test_window_aging_out_auto_resumes_dispatch(self, kanban_home, all_assignees_spawnable, monkeypatch):
        """No mutation needed to un-stick the account — the very next tick
        after the window elapses just dispatches again."""
        _config_with(monkeypatch, {
            "acct-a": {"profiles": ["alice"], "max_rate_limit_hits": 1, "window_seconds": 60},
        })

        spawns: list = []
        with kbc.connect() as conn:
            now = int(time.time())
            _seed_rate_limited_run(conn, profile="alice", ended_at=now - 120)  # outside the 60s window
            task_id = kb.create_task(conn, title="t", assignee="alice")
            result = kbd.dispatch_once(conn, spawn_fn=_fake_spawn_factory(spawns))

        assert spawns == [task_id]
        assert result.provider_budget_blocked == []

    def test_blocked_task_recorded_as_a_dispatch_event(self, kanban_home, all_assignees_spawnable, monkeypatch):
        """Same 'hermes kanban tail' visibility contract as respawn_guarded."""
        _config_with(monkeypatch, {
            "acct-a": {"profiles": ["alice"], "max_rate_limit_hits": 1},
        })

        with kbc.connect() as conn:
            now = int(time.time())
            _seed_rate_limited_run(conn, profile="alice", ended_at=now - 5)
            task_id = kb.create_task(conn, title="t", assignee="alice")
            kbd.dispatch_once(conn, spawn_fn=_fake_spawn_factory([]))
            events = kb.list_events(conn, task_id)

        kinds = [e.kind for e in events]
        assert "provider_budget_blocked" in kinds
