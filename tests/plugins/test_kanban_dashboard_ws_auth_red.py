"""RED acceptance tests for the Kanban dashboard WebSocket auth boundary."""

from __future__ import annotations

import asyncio
import builtins
import importlib.util
import sys
import types
from pathlib import Path

import pytest


@pytest.fixture
def plugin_api():
    """Load a fresh dashboard plugin module for test isolation."""
    repo_root = Path(__file__).resolve().parents[2]
    plugin_file = repo_root / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location(
        f"hermes_dashboard_plugin_kanban_ws_auth_{id(plugin_file)}_{id(object())}",
        plugin_file,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _FakeWebSocket:
    def __init__(self, **query_params):
        self.query_params = query_params
        self.accept_calls = 0
        self.close_codes: list[int | None] = []

    async def accept(self):
        self.accept_calls += 1

    async def close(self, code=None):
        self.close_codes.append(code)



def test_ws_upgrade_authorized_fails_closed_when_canonical_auth_import_raises(
    plugin_api, monkeypatch
):
    """An unavailable canonical auth gate must not turn into authorization."""
    ws = _FakeWebSocket()
    real_import = builtins.__import__

    def raising_canonical_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "hermes_cli" and "web_server_chat" in fromlist:
            raise RuntimeError("canonical auth unavailable")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", raising_canonical_import)

    assert plugin_api._ws_upgrade_authorized(ws) is False


@pytest.mark.parametrize("authorized", [True, False])
def test_ws_upgrade_authorized_preserves_canonical_auth_decision(
    plugin_api, monkeypatch, authorized
):
    """The dashboard gate must preserve both canonical allow and deny results."""
    fake_auth = types.SimpleNamespace(_ws_auth_ok=lambda ws: authorized)
    monkeypatch.setitem(sys.modules, "hermes_cli.web_server_chat", fake_auth)
    import hermes_cli

    monkeypatch.setattr(hermes_cli, "web_server_chat", fake_auth, raising=False)

    assert plugin_api._ws_upgrade_authorized(_FakeWebSocket()) is authorized


def test_stream_events_closes_unauthorized_websocket_without_accepting(
    plugin_api, monkeypatch
):
    """Unauthorized upgrades receive policy violation and never complete accept."""
    ws = _FakeWebSocket()
    monkeypatch.setattr(plugin_api, "_ws_upgrade_authorized", lambda ws: False)

    asyncio.run(plugin_api.stream_events(ws))

    assert ws.close_codes == [plugin_api.http_status.WS_1008_POLICY_VIOLATION]
    assert ws.accept_calls == 0


def test_stream_events_accepts_authorized_websocket_and_handles_disconnect(
    plugin_api, monkeypatch
):
    """Authorized upgrades retain the existing accept-and-stream lifecycle."""
    ws = _FakeWebSocket()
    monkeypatch.setattr(plugin_api, "_ws_upgrade_authorized", lambda ws: True)

    class FakeTail:
        def __init__(self, board):
            self.board = board
            self.shutdown_calls = 0

        async def poll(self, cursor):
            return cursor, []

        async def shutdown(self):
            self.shutdown_calls += 1

    tail = None

    def make_tail(board):
        nonlocal tail
        tail = FakeTail(board)
        return tail

    monkeypatch.setattr(plugin_api, "_EventTail", make_tail)

    async def receive_disconnect():
        return {"type": "websocket.disconnect"}

    ws.receive = receive_disconnect
    asyncio.run(plugin_api.stream_events(ws))

    assert ws.accept_calls == 1
    assert ws.close_codes == []
    assert tail is not None
    assert tail.shutdown_calls == 1
