"""Integration tests for the sidecar-routing branch wired into
kanban_db_dispatch.py's _dispatch_lane_task().

Independent of the pure-function unit tests in
tests/hermes_cli/test_kanban_sidecar_route_remote.py / test_kanban_sidecar_route_labels.py
-- these exercise the REAL dispatch_once() -> _dispatch_lane_task() path against a
real sqlite DB, proving two things the unit tests can't see:

1. sidecar_routing_enabled tasks that fail eligibility/route still fall through
   to a normal spawn (same contract as gate_precheck's sibling branch).
2. try_sidecar_route()'s get_secret() call runs inside a bound profile secret
   scope, exactly like every other dispatch-side secret read documented in
   _worker_profile_scope's docstring. Without that binding, get_secret() raises
   agent.secret_scope.UnscopedSecretError under multiplex -- which propagated
   all the way out of dispatch_once() and crashed the ENTIRE tick, starving
   every other ready task behind it in the same board, not just the sidecar one.
   Regression: a live ready task sat stuck for 100+ dispatcher ticks in
   production before this was caught.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_sidecar_route as sidecar_route


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _config_with_sidecar_routing(monkeypatch, enabled=True):
    import hermes_cli.config as cfgmod

    cfg = {"kanban": {"sidecar_routing_enabled": enabled}}
    monkeypatch.setattr(cfgmod, "load_config", lambda *a, **k: cfg)


def _fake_spawn_factory(spawns: list):
    def fake_spawn(task, workspace, board=None):
        spawns.append(task.id)
        return 42
    return fake_spawn


class TestSidecarRouteSecretScope:
    def test_unscoped_secret_error_falls_through_to_normal_spawn(
        self, kanban_home, all_assignees_spawnable, monkeypatch,
    ):
        """Reproduces the production incident: try_sidecar_route() raising
        UnscopedSecretError (the exact exception get_secret() raises under
        multiplex with no bound profile scope) must be caught at the
        dispatch-lane level and degrade to a normal spawn -- never crash
        dispatch_once() for this task, let alone the whole tick."""
        from agent.secret_scope import UnscopedSecretError

        _config_with_sidecar_routing(monkeypatch, True)

        def _raise(*a, **k):
            raise UnscopedSecretError("SIDECAR_SERVICE_API_KEY")

        monkeypatch.setattr(sidecar_route, "try_sidecar_route", _raise)

        spawns: list = []
        with kbc.connect() as conn:
            task_id = kb.create_task(
                conn, title="sidecar:json_field_extract", assignee="alice",
            )
            result = kbd.dispatch_once(conn, spawn_fn=_fake_spawn_factory(spawns))

        assert spawns == [task_id], (
            "task must still spawn normally when the sidecar route raises"
        )

    def test_second_ready_task_is_not_starved_by_a_raising_sidecar_route(
        self, kanban_home, all_assignees_spawnable, monkeypatch,
    ):
        """The actual production symptom: one bad sidecar-titled task's
        exception must not abort dispatch_once() before it reaches later
        ready rows in the same tick."""
        from agent.secret_scope import UnscopedSecretError

        _config_with_sidecar_routing(monkeypatch, True)
        monkeypatch.setattr(
            sidecar_route, "try_sidecar_route",
            lambda *a, **k: (_ for _ in ()).throw(UnscopedSecretError("x")),
        )

        spawns: list = []
        with kbc.connect() as conn:
            first_id = kb.create_task(
                conn, title="sidecar:json_field_extract", assignee="alice",
            )
            second_id = kb.create_task(conn, title="unrelated task", assignee="bob")
            kbd.dispatch_once(conn, spawn_fn=_fake_spawn_factory(spawns))

        assert set(spawns) == {first_id, second_id}

    def test_successful_sidecar_route_still_completes_without_spawning(
        self, kanban_home, all_assignees_spawnable, monkeypatch,
    ):
        """Control case: an actual successful route must still short-circuit
        the spawn (existing contract), proving the new try/except wrapper
        doesn't change the happy path."""
        _config_with_sidecar_routing(monkeypatch, True)
        monkeypatch.setattr(
            sidecar_route, "try_sidecar_route",
            lambda task: {"operation": "json_field_extract", "_backend": "remote",
                          "payload": {"name": "x"}},
        )

        spawns: list = []
        with kbc.connect() as conn:
            task_id = kb.create_task(
                conn, title="sidecar:json_field_extract", assignee="alice",
            )
            kbd.dispatch_once(conn, spawn_fn=_fake_spawn_factory(spawns))
            done = kb.get_task(conn, task_id)

        assert spawns == []
        assert done.status == "done"

    def test_routing_disabled_is_a_true_noop_even_with_raising_route(
        self, kanban_home, all_assignees_spawnable, monkeypatch,
    ):
        """Default False: the sidecar branch must not even be entered, so a
        route implementation that would raise never gets the chance to."""
        _config_with_sidecar_routing(monkeypatch, False)
        monkeypatch.setattr(
            sidecar_route, "try_sidecar_route",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("must not be called")),
        )

        spawns: list = []
        with kbc.connect() as conn:
            task_id = kb.create_task(
                conn, title="sidecar:json_field_extract", assignee="alice",
            )
            kbd.dispatch_once(conn, spawn_fn=_fake_spawn_factory(spawns))

        assert spawns == [task_id]
