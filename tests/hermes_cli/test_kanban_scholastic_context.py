"""Scholastic sidecar enrichment: remote contract, bounded context, spawn preservation."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_sidecar_route as route


def _task(title: str, body: dict):
    return SimpleNamespace(title=title, body=json.dumps(body))


def test_scholastic_marker_and_payload_are_validated():
    body = {"question": "q", "context_pack": {"facts": ["one"]}}
    assert route.classify_task_for_sidecar(
        _task("sidecar:second_brain_scholastic_context", body)
    ) == (route.SCHOLASTIC_CONTEXT_OPERATION, body)
    assert route.classify_task_for_sidecar(_task("ordinary", body)) is None
    assert route.classify_task_for_sidecar(
        _task("sidecar:second_brain_scholastic_context", {"question": "q"})
    ) is None


def test_scholastic_remote_route_uses_real_card_payload(monkeypatch):
    body = {"question": "q", "context_pack": {"facts": ["one"]}}
    calls = []
    monkeypatch.setattr(route, "_sidecar_service_config", lambda: {"url": "http://sidecar"})
    monkeypatch.setattr(route, "_load_sidecar_modules", lambda: None)
    monkeypatch.setattr(
        "hermes_cli.sidecar_client.list_operations",
        lambda cfg: {"operations": [{
            "operation": route.SCHOLASTIC_CONTEXT_OPERATION,
            "enabled": True,
            "idea_id": 7,
            "input_schema": "scholastic-input",
            "output_schema": "scholastic-output",
        }]},
    )

    def execute(envelope, cfg):
        calls.append(json.loads(envelope))
        return {"status": "ok", "payload": {"context_pack": {"facts": ["two"]}}}

    monkeypatch.setattr("hermes_cli.sidecar_client.execute_remote", execute)
    result = route.try_sidecar_route(_task("sidecar:second_brain_scholastic_context", body))
    assert result["_backend"] == "remote"
    assert result["operation"] == route.SCHOLASTIC_CONTEXT_OPERATION
    assert calls[0]["operation"] == route.SCHOLASTIC_CONTEXT_OPERATION
    assert calls[0]["input"] == body


def test_enrichment_replaces_context_idempotently_and_worker_sees_it(tmp_path, monkeypatch, all_assignees_spawnable):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    monkeypatch.setattr(kbd._sidecar_route, "sidecar_routing_enabled", lambda: True)
    enriched = {"facts": ["from sidecar"], "source": "scholastic"}
    monkeypatch.setattr(
        kbd._sidecar_route,
        "try_sidecar_route",
        lambda task: {
            "status": "ok",
            "operation": route.SCHOLASTIC_CONTEXT_OPERATION,
            "_backend": "remote",
            "payload": {"context_pack": enriched, "objections": ["unresolved"]},
        },
    )
    spawned = []

    def fake_spawn(task, workspace, board=None):
        spawned.append(task.id)
        return None

    with kbc.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="sidecar:second_brain_scholastic_context",
            body=json.dumps({"question": "q", "context_pack": {"facts": ["old"]}}),
            assignee="alice",
        )
        kbd.dispatch_once(conn, spawn_fn=fake_spawn)
        task = kb.get_task(conn, task_id)
        assert task.status == "running"
        assert spawned == [task_id]
        stored = json.loads(task.body)
        assert stored["context_pack"] == enriched
        context = kb.build_worker_context(conn, task_id)
        assert "from sidecar" in context
        assert "unresolved" not in context
        assert kbd._persist_scholastic_context(conn, task_id, enriched) is True
        assert json.loads(kb.get_task(conn, task_id).body)["context_pack"] == enriched


def test_context_failure_falls_through_to_normal_spawn(tmp_path, monkeypatch, all_assignees_spawnable):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    monkeypatch.setattr(kbd._sidecar_route, "sidecar_routing_enabled", lambda: True)
    monkeypatch.setattr(kbd._sidecar_route, "try_sidecar_route", lambda task: None)
    spawned = []

    with kbc.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="sidecar:second_brain_scholastic_context",
            body=json.dumps({"context_pack": {"facts": ["one"]}}),
            assignee="alice",
        )
        kbd.dispatch_once(conn, spawn_fn=lambda task, workspace, board=None: spawned.append(task.id))
        assert spawned == [task_id]
        assert kb.get_task(conn, task_id).status == "running"
