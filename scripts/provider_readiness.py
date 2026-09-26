#!/usr/bin/env python3
"""Inspect and safely recover provider/auth failures on bounded Kanban cards.

Dry-run is default. Apply never authenticates, copies credentials, retries quota walls,
or bypasses dependency blockers.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import tempfile
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

# Scripts execute with ``scripts/`` as sys.path; make repository imports work
# without requiring an editable install.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli.kanban_db_dispatch import dispatch_once

_MAX_CARDS = 50
_AUTH_RE = re.compile(r"\b(401|unauthori[sz]ed|invalid api key|invalid credential|authentication failed|auth failed)\b", re.I)
_MODEL_RE = re.compile(r"\b(model|deployment)\b.*\b(not found|unknown|does not exist|invalid|unsupported)\b|\b404\b", re.I)
_QUOTA_RE = re.compile(r"\b(402|403|429|quota|rate[ -]?limit|billing|insufficient credits|too many requests)\b", re.I)
_RESPAWN_RE = re.compile(r"respawn|retry cap|circuit breaker|blocked.*auth", re.I)


def classify_failure(error: str | None, *, auth_ok: bool | None = None,
                     auth_configured: bool | None = None, has_pin: bool = False) -> tuple[str, str]:
    """Return stable diagnosis and safe operator remediation; never returns input text."""
    text = str(error or "")
    if _QUOTA_RE.search(text):
        return "rate_limit_quota", "Wait for provider quota reset; do not requeue automatically."
    if _MODEL_RE.search(text):
        if has_pin:
            return "stale_card_pin", "Owner must approve a valid model/provider pin, then retry once."
        return "model_mismatch", "Verify model catalog/provider compatibility; owner must choose replacement."
    if _AUTH_RE.search(text):
        if auth_configured is False or (auth_ok is False and auth_configured is not True):
            return "missing_auth", "Run profile-scoped `hermes auth add <provider>`; never copy root credentials."
        return "invalid_credential", "Run profile-scoped auth diagnostics; replace credential interactively, never in this script."
    if _RESPAWN_RE.search(text):
        return "respawn_guard", "Resolve underlying failure first; guard prevents retry storms."
    return "unknown_failure", "Inspect the redacted worker error and preserve blocker until owner decides."


def _profile_home(profile: str | None) -> Path:
    root = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")).expanduser()
    if not profile or profile == "default":
        return root
    # Named profile roots are anchored at the user's profile registry, not at
    # the caller's currently selected HERMES_HOME.
    return Path.home() / ".hermes" / "profiles" / profile


@contextmanager
def _profile_scope(profile: str | None) -> Iterator[None]:
    old = os.environ.get("HERMES_HOME")
    os.environ["HERMES_HOME"] = str(_profile_home(profile))
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = old


def inspect_profile(profile: str | None, provider: str | None, model: str | None) -> dict[str, Any]:
    """Read profile config/auth status without exposing values."""
    with _profile_scope(profile):
        try:
            from hermes_cli.auth import get_auth_status
            auth = get_auth_status(provider) if provider else {"logged_in": False}
            auth_configured = bool(auth.get("configured"))
            auth_ok = bool(auth.get("logged_in") or auth_configured)
            # Provider status may carry raw service text; retain only presence.
            auth_error = "provider_reported_error" if auth.get("error") else ""
        except Exception as exc:
            auth_ok = auth_configured = None
            auth_error = type(exc).__name__
        try:
            from hermes_cli.config import validate_config_structure, load_config
            issues = validate_config_structure()
            cfg = load_config() or {}
            configured_model = ((cfg.get("model") or {}).get("model") if isinstance(cfg.get("model"), dict) else None)
            configured_provider = ((cfg.get("model") or {}).get("provider") if isinstance(cfg.get("model"), dict) else None)
            config_ok = not bool(issues)
        except Exception as exc:
            configured_model = configured_provider = None
            config_ok, auth_error = False, auth_error or type(exc).__name__
    return {"auth_ok": auth_ok, "auth_configured": auth_configured, "auth_error": auth_error, "config_ok": config_ok,
            "configured_model": configured_model, "configured_provider": configured_provider,
            "requested_model": model, "requested_provider": provider}


def _parent_blocked(conn, task_id: str) -> bool:
    parents = kb.parent_ids(conn, task_id)
    if not parents:
        return False
    placeholders = ",".join("?" for _ in parents)
    rows = conn.execute(f"SELECT id, status FROM tasks WHERE id IN ({placeholders})", parents).fetchall()
    return any(row["status"] not in {"done", "archived"} for row in rows)


def inspect_task(conn, task_id: str) -> dict[str, Any]:
    task = kb.get_task(conn, task_id)
    if task is None:
        raise ValueError(f"unknown task: {task_id}")
    provider = task.provider_override
    model = task.model_override
    profile = task.assignee
    profile_state = inspect_profile(profile, provider, model) if profile else {"auth_ok": None, "config_ok": None}
    diagnosis, remediation = classify_failure(
        task.last_failure_error,
        auth_ok=profile_state.get("auth_ok"),
        auth_configured=profile_state.get("auth_configured"),
        has_pin=bool(model or provider),
    )
    if _parent_blocked(conn, task_id):
        diagnosis, remediation = "dependency_blocker", "Preserve dependency blocker; wait for every parent to complete."
    return {"id": task.id, "title": task.title, "status": task.status, "assignee": profile,
            "model": model, "provider": provider, "failures": task.consecutive_failures,
            "diagnosis": diagnosis, "remediation": remediation, "profile": profile_state,
            "last_failure_present": bool(task.last_failure_error)}


def apply_recovery(conn, report: dict[str, Any], pins: dict[str, tuple[str, str | None]], requeued: set[str]) -> str:
    task_id = report["id"]
    reason = report["diagnosis"]
    if reason in {"rate_limit_quota", "dependency_blocker", "respawn_guard", "unknown_failure"}:
        return "skipped"
    if reason in requeued:
        return "skipped_duplicate_reason"
    if task_id in pins:
        model, provider = pins[task_id]
        kb.set_model_override(conn, task_id, model, provider)
    if report["status"] == "running":
        kb.reclaim_task(conn, task_id, reason=f"provider-readiness:{report['diagnosis']}")
    if report["status"] in {"blocked", "scheduled"}:
        kb.unblock_task(conn, task_id)
    if report["diagnosis"] != "dependency_blocker":
        kb.add_comment(conn, task_id, "provider-readiness", "Provider readiness recovery applied; inspect profile-scoped auth/config before one retry.")
        requeued.add(reason)
        return "requeued_once"
    return "skipped"


def _dispatch_dry_run(conn) -> dict[str, Any]:
    """Run dispatcher inspection against a SQLite backup, never live board state."""
    fd, raw_path = tempfile.mkstemp(prefix="provider-readiness-", suffix=".db")
    os.close(fd)
    path = Path(raw_path)
    old_pin = os.environ.get("HERMES_KANBAN_DB")
    old_child_guard = os.environ.pop("HERMES_DELEGATED_CHILD_CONTEXT", None)
    try:
        backup = sqlite3.connect(path)
        try:
            conn.backup(backup)
            backup.commit()
        finally:
            backup.close()
        os.environ["HERMES_KANBAN_DB"] = str(path)
        isolated = sqlite3.connect(path)
        isolated.row_factory = sqlite3.Row
        try:
            try:
                result = dispatch_once(isolated, dry_run=True, max_spawn=0, reconcile_orphans=False)
            except Exception as exc:
                return {"error": type(exc).__name__, "detail": "dispatch dry-run unavailable in this interpreter"}
        finally:
            isolated.close()
        return {"spawned": len(result.spawned), "guarded": len(result.respawn_guarded),
                "skipped_unassigned": len(result.skipped_unassigned)}
    finally:
        if old_pin is None:
            os.environ.pop("HERMES_KANBAN_DB", None)
        else:
            os.environ["HERMES_KANBAN_DB"] = old_pin
        if old_child_guard is not None:
            os.environ["HERMES_DELEGATED_CHILD_CONTEXT"] = old_child_guard
        path.unlink(missing_ok=True)


def run(task_ids: list[str], *, apply: bool = False, pins: dict[str, tuple[str, str | None]] | None = None) -> dict[str, Any]:
    if not task_ids or len(task_ids) > _MAX_CARDS:
        raise ValueError(f"provide 1-{_MAX_CARDS} task ids")
    pins = pins or {}
    with kbc.connect_closing() as conn:
        reports = [inspect_task(conn, task_id) for task_id in task_ids]
        actions = []
        requeued: set[str] = set()
        if apply:
            for report in reports:
                actions.append({"id": report["id"], "action": apply_recovery(conn, report, pins, requeued)})
        verified = [inspect_task(conn, task_id) for task_id in task_ids]
        dispatch = _dispatch_dry_run(conn)
        return {"dry_run": not apply, "cards": reports, "verified_cards": verified, "actions": actions,
                "dispatch_dry_run": dispatch}


def self_test() -> None:
    cases = (
        ("HTTP 401 Unauthorized: invalid API key", "missing_auth"),
        ("HTTP 429 rate limit exceeded", "rate_limit_quota"),
        ("404 model not found", "stale_card_pin"),
    )
    for error, expected in cases:
        diagnosis, remediation = classify_failure(
            error,
            auth_ok=False if expected == "missing_auth" else None,
            has_pin=expected == "stale_card_pin",
        )
        assert diagnosis == expected
        assert remediation
    print("self-test passed")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task_ids", nargs="*", help="bounded explicit Kanban task IDs")
    parser.add_argument("--self-test", action="store_true", help="run local classifier checks without reading the board")
    parser.add_argument("--apply", action="store_true", help="apply owner-approved recovery; default is report only")
    parser.add_argument("--pin", nargs=3, action="append", metavar=("TASK_ID", "MODEL", "PROVIDER"),
                        help="owner-approved pin; use '-' provider to clear provider override")
    args = parser.parse_args(argv)
    if args.self_test:
        self_test()
        return 0
    if not args.task_ids:
        parser.error("at least one task_id is required unless --self-test is used")
    pins = {task: (model, None if provider == "-" else provider) for task, model, provider in (args.pin or [])}
    try:
        result = run(args.task_ids, apply=args.apply, pins=pins)
    except (ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
