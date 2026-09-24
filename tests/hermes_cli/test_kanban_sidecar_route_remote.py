"""hermes_cli/kanban_sidecar_route.py: remote vs in-process backend selection.

Proof obligations: url unset -> in-process path fully unchanged (existing behavior
preserved); url set -> remote path used, disabled/missing/error remote outcomes fall
through to None (never mark a task done on a bad remote result); ok result carries
_backend so callers (kanban_db_dispatch event payload, delegate routed entry) can record
which backend actually ran.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from hermes_cli import kanban_sidecar_route as route


class _FakeTask(SimpleNamespace):
    title: str = "do the thing"
    body: str = "{}"


def _register_dummy_op(monkeypatch, payload=None):
    def _classify(task):
        return {"value": 1} if payload is None else payload

    monkeypatch.setitem(route.SIDECAR_ELIGIBLE_OPERATIONS, "dummy_op", _classify)


def test_no_eligible_classifier_returns_none_without_touching_backends():
    assert route.classify_task_for_sidecar(_FakeTask()) is None
    with patch.object(route, "_sidecar_service_config") as mock_cfg:
        assert route.try_sidecar_route(_FakeTask()) is None
    mock_cfg.assert_not_called()


def test_url_unset_uses_in_process_path_unchanged(monkeypatch):
    _register_dummy_op(monkeypatch)
    with patch.object(route, "_sidecar_service_config", return_value={"url": ""}), \
         patch.object(route, "_try_remote_route") as mock_remote, \
         patch.object(route, "_try_in_process_route", return_value={"status": "ok", "payload": {}, "_backend": "in_process"}) as mock_in_process:
        result = route.try_sidecar_route(_FakeTask())
    mock_remote.assert_not_called()
    mock_in_process.assert_called_once_with("dummy_op", {"value": 1})
    assert result["_backend"] == "in_process"


def test_url_set_uses_remote_path(monkeypatch):
    _register_dummy_op(monkeypatch)
    cfg = {"url": "http://192.0.2.1:8765", "api_key_env": "X", "timeout_s": 5}
    with patch.object(route, "_sidecar_service_config", return_value=cfg), \
         patch.object(route, "_try_in_process_route") as mock_in_process, \
         patch.object(route, "_try_remote_route", return_value={"status": "ok", "payload": {}, "_backend": "remote"}) as mock_remote:
        result = route.try_sidecar_route(_FakeTask())
    mock_in_process.assert_not_called()
    mock_remote.assert_called_once_with("dummy_op", {"value": 1}, cfg)
    assert result["_backend"] == "remote"


def test_remote_route_list_operations_unreachable_returns_none():
    from hermes_cli import sidecar_client

    with patch.object(sidecar_client, "list_operations", return_value=None):
        assert route._try_remote_route("dummy_op", {}, {"url": "http://x:8765"}) is None


def test_remote_route_disabled_operation_returns_none():
    from hermes_cli import sidecar_client

    ops = {"operations": [{"operation": "dummy_op", "enabled": False, "idea_id": 1,
                            "input_schema": "s1", "output_schema": "s2"}]}
    with patch.object(sidecar_client, "list_operations", return_value=ops):
        assert route._try_remote_route("dummy_op", {}, {"url": "http://x:8765"}) is None


def test_remote_route_missing_operation_returns_none():
    from hermes_cli import sidecar_client

    with patch.object(sidecar_client, "list_operations", return_value={"operations": []}):
        assert route._try_remote_route("dummy_op", {}, {"url": "http://x:8765"}) is None


def test_remote_route_execute_error_returns_none():
    from hermes_cli import sidecar_client

    ops = {"operations": [{"operation": "dummy_op", "enabled": True, "idea_id": 1,
                            "input_schema": "s1", "output_schema": "s2"}]}
    with patch.object(sidecar_client, "list_operations", return_value=ops), \
         patch.object(sidecar_client, "execute_remote", return_value=None):
        assert route._try_remote_route("dummy_op", {}, {"url": "http://x:8765"}) is None


def test_remote_route_non_ok_result_falls_through(monkeypatch):
    _register_dummy_op(monkeypatch)
    from hermes_cli import sidecar_client

    ops = {"operations": [{"operation": "dummy_op", "enabled": True, "idea_id": 1,
                            "input_schema": "s1", "output_schema": "s2"}]}
    cfg = {"url": "http://x:8765"}
    with patch.object(route, "_sidecar_service_config", return_value=cfg), \
         patch.object(sidecar_client, "list_operations", return_value=ops), \
         patch.object(sidecar_client, "execute_remote", return_value={"status": "escalate"}):
        # try_sidecar_route (not just _try_remote_route) must fall through -- a non-ok
        # sidecar result must never mark the task done.
        assert route.try_sidecar_route(_FakeTask()) is None


def test_remote_route_ok_result_tagged_with_backend():
    from hermes_cli import sidecar_client

    ops = {"operations": [{"operation": "dummy_op", "enabled": True, "idea_id": 1,
                            "input_schema": "s1", "output_schema": "s2"}]}
    with patch.object(sidecar_client, "list_operations", return_value=ops), \
         patch.object(sidecar_client, "execute_remote", return_value={"status": "ok", "payload": {"x": 1}}):
        result = route._try_remote_route("dummy_op", {}, {"url": "http://x:8765"})
    assert result == {"status": "ok", "payload": {"x": 1}, "_backend": "remote"}
