"""HTTP client for a remote sidecar_service instance (config: ``sidecar_service``).

Used by ``hermes_cli/kanban_sidecar_route.py`` when ``sidecar_service.url`` is set.
ponytail: stdlib ``urllib.request`` only, mirroring the sidecars repo's own
``sidecar_service/remote_client.py`` -- no new dependency for one POST + one GET.
Never raises: any network error, timeout, non-2xx, or non-JSON body returns None so
callers fall through to their existing behavior. Never logs the API key.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any, Optional

_OPERATIONS_CACHE_TTL_S = 30.0
_operations_cache: dict[str, Any] = {"result": None, "fetched_at": 0.0, "url": None}


def _resolve_api_key(api_key_env: str) -> str:
    from agent.secret_scope import get_secret

    return (get_secret(api_key_env) or "").strip()


def _request(url: str, api_key: str, timeout_s: float, data: Optional[bytes] = None) -> Optional[dict]:
    req = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET")
    req.add_header("Authorization", f"Bearer {api_key}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            if resp.status < 200 or resp.status >= 300:
                return None
            body = resp.read().decode("utf-8")
    except (urllib.error.URLError, TimeoutError, OSError, UnicodeDecodeError):
        return None
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def execute_remote(envelope: str, sidecar_cfg: dict) -> Optional[dict]:
    """POST a ``sidecar-operation/1`` envelope (JSON string) to ``<url>/v1/execute``.

    ``sidecar_cfg`` is the ``sidecar_service`` config dict (``url``, ``api_key_env``,
    ``timeout_s``). Returns the parsed result dict on a 2xx JSON-object response,
    else None.
    """
    url = str(sidecar_cfg.get("url") or "").strip()
    if not url:
        return None
    api_key = _resolve_api_key(str(sidecar_cfg.get("api_key_env") or "SIDECAR_SERVICE_API_KEY"))
    timeout_s = float(sidecar_cfg.get("timeout_s") or 5)
    return _request(f"{url.rstrip('/')}/v1/execute", api_key, timeout_s, data=envelope.encode("utf-8"))


def list_operations(sidecar_cfg: dict) -> Optional[dict]:
    """GET ``<url>/v1/operations``, cached for ``_OPERATIONS_CACHE_TTL_S`` seconds
    per URL. Returns the parsed dict, else None."""
    url = str(sidecar_cfg.get("url") or "").strip()
    if not url:
        return None
    now = time.monotonic()
    if (
        _operations_cache["url"] == url
        and _operations_cache["result"] is not None
        and now - _operations_cache["fetched_at"] < _OPERATIONS_CACHE_TTL_S
    ):
        return _operations_cache["result"]
    api_key = _resolve_api_key(str(sidecar_cfg.get("api_key_env") or "SIDECAR_SERVICE_API_KEY"))
    timeout_s = float(sidecar_cfg.get("timeout_s") or 5)
    result = _request(f"{url.rstrip('/')}/v1/operations", api_key, timeout_s)
    if result is not None:
        _operations_cache.update(result=result, fetched_at=now, url=url)
    return result
