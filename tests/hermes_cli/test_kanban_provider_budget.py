"""Unit tests for the rolling-window provider-quota guard.

Pure-function tests against ``hermes_cli.kanban_provider_budget`` — no DB, no
dispatcher wiring. Mirrors the test shape of
``tests/hermes_cli/test_kanban_gate_precheck.py`` (another opt-in dispatch
guard living in its own module).
"""

from __future__ import annotations

import sqlite3

import pytest

from hermes_cli import kanban_provider_budget as pb


# ---------------------------------------------------------------------------
# provider_budgets_config: config normalization
# ---------------------------------------------------------------------------


class TestProviderBudgetsConfig:
    def test_missing_kanban_cfg_returns_empty(self):
        assert pb.provider_budgets_config(None) == {}

    def test_missing_provider_budgets_key_returns_empty(self):
        assert pb.provider_budgets_config({}) == {}

    def test_non_dict_provider_budgets_returns_empty(self):
        assert pb.provider_budgets_config({"provider_budgets": "nonsense"}) == {}

    def test_normalizes_one_account(self):
        cfg = {
            "provider_budgets": {
                "acct-a": {"profiles": ["alice", "bob"], "max_rate_limit_hits": 3},
            },
        }
        out = pb.provider_budgets_config(cfg)
        assert out == {
            "acct-a": {
                "profiles": ["alice", "bob"],
                "max_rate_limit_hits": 3,
                "window_seconds": pb.DEFAULT_WINDOW_SECONDS,
            },
        }

    def test_defaults_max_rate_limit_hits_and_window(self):
        cfg = {"provider_budgets": {"acct-a": {"profiles": ["alice"]}}}
        out = pb.provider_budgets_config(cfg)
        assert out["acct-a"]["max_rate_limit_hits"] == pb.DEFAULT_MAX_RATE_LIMIT_HITS
        assert out["acct-a"]["window_seconds"] == pb.DEFAULT_WINDOW_SECONDS

    def test_account_with_no_profiles_is_dropped(self):
        """An account naming no profiles can never be resolved from an
        assignee, so it is dropped rather than silently doing nothing."""
        cfg = {"provider_budgets": {"acct-a": {"profiles": []}}}
        assert pb.provider_budgets_config(cfg) == {}

    def test_non_dict_account_entry_is_dropped(self):
        cfg = {"provider_budgets": {"acct-a": "nonsense"}}
        assert pb.provider_budgets_config(cfg) == {}

    def test_non_positive_thresholds_fall_back_to_defaults(self):
        cfg = {
            "provider_budgets": {
                "acct-a": {"profiles": ["alice"], "max_rate_limit_hits": 0, "window_seconds": -5},
            },
        }
        out = pb.provider_budgets_config(cfg)
        assert out["acct-a"]["max_rate_limit_hits"] == pb.DEFAULT_MAX_RATE_LIMIT_HITS
        assert out["acct-a"]["window_seconds"] == pb.DEFAULT_WINDOW_SECONDS


# ---------------------------------------------------------------------------
# resolve_account_for_assignee
# ---------------------------------------------------------------------------


class TestResolveAccountForAssignee:
    def test_assignee_in_no_account_returns_none(self):
        accounts = {"acct-a": {"profiles": ["alice"], "max_rate_limit_hits": 3, "window_seconds": 3600}}
        assert pb.resolve_account_for_assignee(accounts, "carol") is None

    def test_assignee_in_one_account_returns_its_name(self):
        accounts = {"acct-a": {"profiles": ["alice", "bob"], "max_rate_limit_hits": 3, "window_seconds": 3600}}
        assert pb.resolve_account_for_assignee(accounts, "bob") == "acct-a"

    def test_empty_accounts_returns_none(self):
        assert pb.resolve_account_for_assignee({}, "alice") is None


# ---------------------------------------------------------------------------
# check_provider_budget: the real DB-backed gate
# ---------------------------------------------------------------------------


@pytest.fixture
def conn():
    """A minimal task_runs table — enough columns for the guard's own query,
    isolated from the full kanban schema so this stays a pure unit test."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute(
        "CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT, profile TEXT, "
        "outcome TEXT, ended_at INTEGER)"
    )
    return c


def _insert_run(conn, *, profile, outcome, ended_at):
    conn.execute(
        "INSERT INTO task_runs (task_id, profile, outcome, ended_at) VALUES (?, ?, ?, ?)",
        (f"t-{profile}-{ended_at}", profile, outcome, ended_at),
    )
    conn.commit()


class TestCheckProviderBudget:
    def test_no_accounts_configured_is_a_no_op(self, conn):
        assert pb.check_provider_budget(conn, "alice", kanban_cfg={}, now=1_000_000) is None

    def test_assignee_not_in_any_account_is_a_no_op(self, conn):
        cfg = {"provider_budgets": {"acct-a": {"profiles": ["bob"], "max_rate_limit_hits": 1}}}
        assert pb.check_provider_budget(conn, "alice", kanban_cfg=cfg, now=1_000_000) is None

    def test_below_threshold_is_a_no_op(self, conn):
        now = 1_000_000
        cfg = {"provider_budgets": {"acct-a": {"profiles": ["alice"], "max_rate_limit_hits": 3}}}
        _insert_run(conn, profile="alice", outcome="rate_limited", ended_at=now - 10)
        assert pb.check_provider_budget(conn, "alice", kanban_cfg=cfg, now=now) is None

    def test_at_threshold_trips_the_guard(self, conn):
        now = 1_000_000
        cfg = {"provider_budgets": {"acct-a": {"profiles": ["alice"], "max_rate_limit_hits": 2}}}
        _insert_run(conn, profile="alice", outcome="rate_limited", ended_at=now - 10)
        _insert_run(conn, profile="alice", outcome="rate_limited", ended_at=now - 5)
        reason = pb.check_provider_budget(conn, "alice", kanban_cfg=cfg, now=now)
        assert reason == "provider_budget_exhausted:acct-a"

    def test_hits_pool_across_every_profile_in_the_account(self, conn):
        """The whole point: a fan-out where 5 different tasks/profiles share
        one provider account must trip as one budget, not five independent
        per-task cooldowns."""
        now = 1_000_000
        cfg = {
            "provider_budgets": {
                "acct-a": {"profiles": ["alice", "bob"], "max_rate_limit_hits": 2},
            },
        }
        _insert_run(conn, profile="alice", outcome="rate_limited", ended_at=now - 10)
        _insert_run(conn, profile="bob", outcome="rate_limited", ended_at=now - 5)
        # Checking from bob's dispatch: alice's hit counts against the shared pool.
        reason = pb.check_provider_budget(conn, "bob", kanban_cfg=cfg, now=now)
        assert reason == "provider_budget_exhausted:acct-a"

    def test_hits_outside_the_window_do_not_count(self, conn):
        now = 1_000_000
        cfg = {
            "provider_budgets": {
                "acct-a": {"profiles": ["alice"], "max_rate_limit_hits": 2, "window_seconds": 60},
            },
        }
        _insert_run(conn, profile="alice", outcome="rate_limited", ended_at=now - 1000)
        _insert_run(conn, profile="alice", outcome="rate_limited", ended_at=now - 900)
        assert pb.check_provider_budget(conn, "alice", kanban_cfg=cfg, now=now) is None

    def test_non_rate_limited_outcomes_do_not_count(self, conn):
        now = 1_000_000
        cfg = {"provider_budgets": {"acct-a": {"profiles": ["alice"], "max_rate_limit_hits": 1}}}
        _insert_run(conn, profile="alice", outcome="crashed", ended_at=now - 10)
        _insert_run(conn, profile="alice", outcome="completed", ended_at=now - 5)
        assert pb.check_provider_budget(conn, "alice", kanban_cfg=cfg, now=now) is None

    def test_still_running_rows_null_ended_at_are_ignored(self, conn):
        now = 1_000_000
        cfg = {"provider_budgets": {"acct-a": {"profiles": ["alice"], "max_rate_limit_hits": 1}}}
        conn.execute(
            "INSERT INTO task_runs (task_id, profile, outcome, ended_at) VALUES (?, ?, ?, NULL)",
            ("t-running", "alice", None),
        )
        conn.commit()
        assert pb.check_provider_budget(conn, "alice", kanban_cfg=cfg, now=now) is None

    def test_default_now_uses_wall_clock(self, conn, monkeypatch):
        import time as time_mod

        monkeypatch.setattr(time_mod, "time", lambda: 2_000_000.0)
        cfg = {"provider_budgets": {"acct-a": {"profiles": ["alice"], "max_rate_limit_hits": 1}}}
        _insert_run(conn, profile="alice", outcome="rate_limited", ended_at=2_000_000 - 5)
        assert pb.check_provider_budget(conn, "alice", kanban_cfg=cfg) == "provider_budget_exhausted:acct-a"


# ---------------------------------------------------------------------------
# provider_budgets_enabled: cheap short-circuit for the dispatcher hot path
# ---------------------------------------------------------------------------


class TestProviderBudgetsEnabled:
    def test_no_kanban_cfg_is_disabled(self):
        assert pb.provider_budgets_enabled(None) is False

    def test_empty_provider_budgets_is_disabled(self):
        assert pb.provider_budgets_enabled({"provider_budgets": {}}) is False

    def test_one_valid_account_is_enabled(self):
        cfg = {"provider_budgets": {"acct-a": {"profiles": ["alice"]}}}
        assert pb.provider_budgets_enabled(cfg) is True
