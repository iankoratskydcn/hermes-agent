"""hermes_cli/sidecar_client.py: never-raises HTTP client for a remote sidecar_service.

Proof obligations: ok result parsed; network error/timeout/non-2xx/garbage body -> None;
API key resolved via agent.secret_scope.get_secret (never bare os.getenv) and never appears
in any exception message; list_operations() caches per-URL for the TTL window.
"""
from __future__ import annotations

import json
import socket
import urllib.error
from unittest.mock import patch

from hermes_cli import sidecar_client

_CFG = {"url": "http://192.0.2.1:8765", "api_key_env": "SIDECAR_SERVICE_API_KEY", "timeout_s": 5}


def _resp(body: dict, status: int = 200):
    class _R:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps(body).encode("utf-8")

    r = _R()
    r.status = status
    return r


def _clear_cache():
    sidecar_client._operations_cache.update(result=None, fetched_at=0.0, url=None)


def test_execute_remote_ok_result_used():
    with patch.object(sidecar_client, "_resolve_api_key", return_value="tok"), \
         patch("urllib.request.urlopen", return_value=_resp({"status": "ok", "payload": {"x": 1}})):
        result = sidecar_client.execute_remote(json.dumps({"operation": "foo"}), _CFG)
    assert result == {"status": "ok", "payload": {"x": 1}}


def test_execute_remote_network_error_returns_none():
    with patch.object(sidecar_client, "_resolve_api_key", return_value="tok"), \
         patch("urllib.request.urlopen", side_effect=urllib.error.URLError("unreachable")):
        assert sidecar_client.execute_remote(json.dumps({"operation": "foo"}), _CFG) is None


def test_execute_remote_timeout_returns_none():
    with patch.object(sidecar_client, "_resolve_api_key", return_value="tok"), \
         patch("urllib.request.urlopen", side_effect=socket.timeout("timed out")):
        assert sidecar_client.execute_remote(json.dumps({"operation": "foo"}), _CFG) is None


def test_execute_remote_non_2xx_returns_none():
    with patch.object(sidecar_client, "_resolve_api_key", return_value="tok"), \
         patch("urllib.request.urlopen", return_value=_resp({"status": "error"}, status=500)):
        assert sidecar_client.execute_remote(json.dumps({"operation": "foo"}), _CFG) is None


def test_execute_remote_garbage_body_returns_none():
    class _Garbage:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        status = 200

        def read(self):
            return b"not json{{{"

    with patch.object(sidecar_client, "_resolve_api_key", return_value="tok"), \
         patch("urllib.request.urlopen", return_value=_Garbage()):
        assert sidecar_client.execute_remote(json.dumps({"operation": "foo"}), _CFG) is None


def test_execute_remote_url_unset_returns_none_without_network_call():
    with patch("urllib.request.urlopen") as mock_urlopen:
        assert sidecar_client.execute_remote("{}", {"url": ""}) is None
    mock_urlopen.assert_not_called()


def test_list_operations_caches_within_ttl():
    _clear_cache()
    with patch.object(sidecar_client, "_resolve_api_key", return_value="tok"), \
         patch("urllib.request.urlopen", return_value=_resp({"operations": []})) as mock_urlopen:
        first = sidecar_client.list_operations(_CFG)
        second = sidecar_client.list_operations(_CFG)
    assert first == {"operations": []} == second
    assert mock_urlopen.call_count == 1  # second call served from cache


def test_api_key_never_appears_in_exception_message():
    with patch.object(sidecar_client, "_resolve_api_key", return_value="super-secret-token"), \
         patch("urllib.request.urlopen", side_effect=urllib.error.URLError("boom")):
        try:
            sidecar_client.execute_remote("{}", _CFG)
        except Exception as exc:  # pragma: no cover - execute_remote never raises
            assert "super-secret-token" not in str(exc)


def test_non_http_scheme_rejected_without_network_call():
    """file:// (or any non-http(s) scheme) must never reach urlopen: urlopen honors
    whatever scheme a URL declares, so an unchecked scheme could read local files or
    hit a non-HTTP service instead of the intended API -- and a file:// response has
    no .status, which crashed this function uncaught before the scheme check existed."""
    for scheme_url in ("file:///etc/hostname", "ftp://127.0.0.1:8765", "100.66.2.21:8765", ""):
        with patch.object(sidecar_client, "_resolve_api_key", return_value="tok"), \
             patch("urllib.request.urlopen") as mock_urlopen:
            assert sidecar_client.execute_remote(json.dumps({"operation": "foo"}),
                                                  {**_CFG, "url": scheme_url}) is None
        mock_urlopen.assert_not_called()


def test_resolve_api_key_uses_secret_scope_not_bare_env(monkeypatch):
    monkeypatch.setenv("SIDECAR_SERVICE_API_KEY", "leaked-if-bare-getenv")
    with patch("agent.secret_scope.get_secret", return_value="scoped-value") as mock_get_secret:
        key = sidecar_client._resolve_api_key("SIDECAR_SERVICE_API_KEY")
    mock_get_secret.assert_called_once_with("SIDECAR_SERVICE_API_KEY")
    assert key == "scoped-value"
