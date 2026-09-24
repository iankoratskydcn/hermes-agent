"""Opt-in kanban-task -> sidecar_service auto-routing (Sidecar-Adoption STEP 3, part A).

Mirrors ``hermes_cli/kanban_gate_precheck.py``'s shape: a pure classify step plus an
opt-in config flag (``kanban.sidecar_routing_enabled``, default ``False``), enforced at
the same dispatch-time guard slot as the gate precheck
(``hermes_cli/kanban_db_dispatch.py::_dispatch_lane_task``).

Two dispatch backends, chosen by whether ``sidecar_service.url`` is configured:

- **In-process** (default, url unset): the in-process ``sidecar_suite.contract.execute()``
  call sidecar_service's own stdio MCP entrypoint uses
  (``sidecar_service/mcp_entrypoint.py``). Simplest when this dispatch loop and the
  sidecar_service checkout are on the same host (ponytail rung 1-2: reuse what already
  exists).
- **Remote** (url set): HTTP against a sidecar_service instance on another host (e.g. a
  Tailscale-bound mini PC) via ``hermes_cli/sidecar_client.py``. Operation metadata
  (idea_id, schemas) comes from that instance's own ``GET /v1/operations`` rather than
  the local checkout's registry, since only the remote host's enabled/disabled state is
  authoritative for what it will actually run.

Eligibility registry (``SIDECAR_ELIGIBLE_OPERATIONS``) starts EMPTY on purpose: a
kanban task's only shape is title/body/labels, and none of sidecar_service's 99
registered operations (each with its own strict input schema, see
UNIVERSAL_SIDECAR_OPERATION_CONTRACT.md in the sidecars repo) has an obviously-correct,
safe mapping from "arbitrary task title/body" to its input schema. Inventing one here
risks silently routing a real task through a mismatched deterministic handler and
returning a wrong "done" result -- worse than the full spawn this is meant to save.
STEP 4 (fallthrough tagging) is the intended mechanism for discovering which concrete
task shapes recur often enough to justify a real, reviewed mapping; add entries to the
registry only once a shape has evidence behind it, following the pattern below.
"""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

# task -> input payload (dict) or None if this task doesn't match this operation's shape.
# Populate with real, reviewed mappings as STEP 4's fallthrough report surfaces demand;
# see the module docstring for why this starts empty.
ClassifierFn = Callable[[Any], Optional[dict]]
SIDECAR_ELIGIBLE_OPERATIONS: dict[str, ClassifierFn] = {}

_SIDECAR_REPO_CACHE: dict[str, Any] = {}


def sidecar_routing_enabled(kanban_cfg: Optional[dict] = None) -> bool:
    """Opt-in flag, same lookup pattern as ``kanban_db_dispatch.gate_precheck_enabled``."""
    if kanban_cfg is None:
        try:
            from hermes_cli.config import load_config

            kanban_cfg = load_config().get("kanban") or {}
        except Exception:
            kanban_cfg = {}
    if not isinstance(kanban_cfg, dict):
        kanban_cfg = {}
    return bool(kanban_cfg.get("sidecar_routing_enabled", False))


def _sidecar_service_config() -> dict:
    try:
        from hermes_cli.config import load_config

        cfg = load_config().get("sidecar_service") or {}
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def _sidecar_repo_root() -> Optional[Path]:
    """Locate the sidecars repo from ``mcp_servers.sidecar_service.args`` in config.yaml
    (the same registration STEP 1/2 already rely on) so this module never hardcodes a path."""
    try:
        from hermes_cli.config import load_config

        servers = load_config().get("mcp_servers") or {}
        entrypoint_arg = ((servers.get("sidecar_service") or {}).get("args") or [None])[0]
        if not entrypoint_arg:
            return None
        # .../<repo>/sidecar_service/mcp_entrypoint.py -> <repo>
        return Path(entrypoint_arg).resolve().parent.parent
    except Exception:
        return None


def _load_sidecar_modules() -> Optional[dict[str, Any]]:
    """Best-effort, memoized import of the sidecar_suite/sidecar_service modules this
    dispatcher needs. Returns None (never raises) when the sidecars repo isn't configured
    or reachable -- callers fall through to a normal spawn on any of this."""
    if "modules" in _SIDECAR_REPO_CACHE:
        return _SIDECAR_REPO_CACHE["modules"]
    modules = None
    repo_root = _sidecar_repo_root()
    if repo_root is not None and repo_root.is_dir():
        repo_str = str(repo_root)
        if repo_str not in sys.path:
            sys.path.insert(0, repo_str)
        try:
            from sidecar_service.registry_loader import build_registry
            from sidecar_service.state import DEFAULT_STATE_PATH, is_operation_enabled, load_state
            from sidecar_suite.contract import execute

            modules = {
                "registry": build_registry(), "execute": execute,
                "load_state": load_state, "is_operation_enabled": is_operation_enabled,
                "state_path": DEFAULT_STATE_PATH,
            }
        except Exception:
            modules = None
    _SIDECAR_REPO_CACHE["modules"] = modules
    return modules


def classify_task_for_sidecar(task: Any) -> Optional[tuple[str, dict]]:
    """``(operation_name, input_payload)`` for the first matching classifier, or None
    when no registered operation's classifier claims this task. Pure, no I/O."""
    for operation, classify in SIDECAR_ELIGIBLE_OPERATIONS.items():
        try:
            input_payload = classify(task)
        except Exception:
            continue  # a broken classifier is a fall-through, never a crash
        if input_payload is not None:
            return operation, input_payload
    return None


def _build_envelope(operation_name: str, idea_id: int, input_schema: str, output_schema: str,
                     input_payload: dict) -> str:
    task_id = str(uuid.uuid4())
    envelope = {
        "task_id": task_id, "run_id": str(uuid.uuid4()), "attempt_id": str(uuid.uuid4()),
        "pairing_key": task_id, "idea_id": idea_id, "operation": operation_name,
        "schema_version": "sidecar-operation/1", "input": input_payload,
        "input_schema": input_schema, "output_schema": output_schema,
        "constraints": {
            "timeout_ms": 5000, "max_input_bytes": 65536, "max_output_bytes": 65536,
            "confidence_threshold": 0.0,
        },
        "routing_hint": "auto", "privacy_policy": "sanitized",
    }
    return json.dumps(envelope)


def try_sidecar_route(task: Any) -> Optional[dict]:
    """Attempt the cheap-first sidecar path for one task: classify, dispatch (remote HTTP
    if ``sidecar_service.url`` is configured, else in-process), return the sidecar's result
    dict (tagged with ``_backend``: ``"remote"``/``"in_process"``) on an ``ok`` outcome, else
    None (fall through to a normal spawn/subagent unchanged). Never raises -- any failure
    here is a fall-through, matching agent/auxiliary_client.py's provider-fallback-chain
    shape ("try cheap first, fall through on ineligibility/failure")."""
    classified = classify_task_for_sidecar(task)
    if classified is None:
        return None
    operation_name, input_payload = classified

    sidecar_cfg = _sidecar_service_config()
    if str(sidecar_cfg.get("url") or "").strip():
        result = _try_remote_route(operation_name, input_payload, sidecar_cfg)
    else:
        result = _try_in_process_route(operation_name, input_payload)
    if result is None or result.get("status") != "ok":
        return None
    return result


def _try_remote_route(operation_name: str, input_payload: dict, sidecar_cfg: dict) -> Optional[dict]:
    from hermes_cli import sidecar_client

    operations = sidecar_client.list_operations(sidecar_cfg)
    if operations is None:
        return None
    op_meta = next(
        (o for o in (operations.get("operations") or []) if o.get("operation") == operation_name),
        None,
    )
    if op_meta is None or not op_meta.get("enabled", False):
        return None
    try:
        envelope = _build_envelope(
            operation_name, op_meta["idea_id"], op_meta["input_schema"], op_meta["output_schema"],
            input_payload,
        )
        result = sidecar_client.execute_remote(envelope, sidecar_cfg)
    except Exception:
        return None
    if result is None:
        return None
    result["_backend"] = "remote"
    return result


def _try_in_process_route(operation_name: str, input_payload: dict) -> Optional[dict]:
    modules = _load_sidecar_modules()
    if modules is None:
        return None
    registry = modules["registry"]
    operation = registry.operations.get(operation_name)
    if operation is None:
        return None
    try:
        state = modules["load_state"](sorted(registry.operations), modules["state_path"])
        if not modules["is_operation_enabled"](state, operation_name):
            return None
        envelope = _build_envelope(
            operation_name, operation.idea_id, operation.input_schema, operation.output_schema, input_payload,
        )
        result = modules["execute"](envelope, registry, event_sink=lambda _e: None)
    except Exception:
        return None
    result["_backend"] = "in_process"
    return result
