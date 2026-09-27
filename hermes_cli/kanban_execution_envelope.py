"""Reusable fail-closed validation for Kanban execution envelopes.

This module is deliberately pure. CLI/dispatcher callers provide durable card
fields and receive actionable diagnostics without filesystem or database writes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping


@dataclass(frozen=True)
class ExecutionEnvelopeResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    correction: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "errors": list(self.errors), "correction": self.correction}


def _value(source: Mapping[str, Any] | Any, name: str, default: Any = None) -> Any:
    if isinstance(source, Mapping):
        return source.get(name, default)
    return getattr(source, name, default)


def validate_execution_envelope(source: Mapping[str, Any] | Any) -> ExecutionEnvelopeResult:
    """Validate required dispatch/review metadata; never silently repair it."""
    errors: list[str] = []
    project_id = str(_value(source, "project_id") or "").strip()
    repo_raw = str(_value(source, "repo_path") or "").strip()
    workspace_raw = str(_value(source, "workspace_path") or "").strip()
    workspace_kind = str(_value(source, "workspace_kind") or "").strip()
    branch = str(_value(source, "branch_name") or "").strip()
    assignee = str(_value(source, "assignee") or "").strip()
    reviewer = str(_value(source, "reviewer") or "").strip()
    skills = _value(source, "skills")
    dependencies = _value(source, "dependencies")
    proof = str(_value(source, "proof_command") or "").strip()
    stop = str(_value(source, "stop_condition") or "").strip()
    idem = str(_value(source, "idempotency_key") or "").strip()

    if not project_id:
        errors.append("project_id is required")
    if workspace_kind != "worktree":
        errors.append("workspace_kind must be 'worktree'")
    repo = Path(repo_raw).expanduser() if repo_raw else None
    workspace = Path(workspace_raw).expanduser() if workspace_raw else None
    if workspace is None or not workspace.is_absolute():
        errors.append("workspace_path must be absolute")
    if repo is None or not repo.is_absolute():
        errors.append("repo_path must be absolute")
    if workspace is not None and repo is not None and workspace.is_absolute() and repo.is_absolute():
        try:
            repo_resolved = repo.resolve()
            workspace_resolved = workspace.resolve()
            if workspace_resolved == repo_resolved or workspace_resolved.parent.name != ".worktrees":
                errors.append(
                    "workspace_path must be an isolated worktree, not the repository root; "
                    "use repo/.worktrees/<task-id> under the canonical repository"
                )
            elif repo_resolved not in workspace_resolved.parents:
                errors.append("workspace_path must be under repo_path/.worktrees/<task-id>")
        except OSError as exc:
            errors.append(f"cannot resolve repo/worktree paths: {exc}")
    if not branch:
        errors.append("branch_name is required")
    if not assignee:
        errors.append("assignee is required")
    if not reviewer:
        errors.append("reviewer is required")
    elif assignee.casefold() == reviewer.casefold():
        errors.append("reviewer must differ from assignee")
    if not isinstance(skills, (list, tuple)) or not [item for item in skills if str(item).strip()]:
        errors.append("skills are required")
    if dependencies is None:
        errors.append("dependencies must be explicit (use [] when none)")
    if not proof:
        errors.append("proof_command is required")
    if not stop:
        errors.append("stop_condition is required")
    if not idem:
        errors.append("idempotency_key is required")

    correction = (
        "Correct card with explicit project/repo, workspace "
        "<repo>/.worktrees/<task-id>, unique branch, distinct owner/reviewer, "
        "skills, dependencies, proof_command, stop_condition, and idempotency_key."
    )
    return ExecutionEnvelopeResult(ok=not errors, errors=errors, correction=correction if errors else "")


def normalized_fingerprint(source: Mapping[str, Any] | Any) -> str:
    """Stable duplicate key for active outcome cards."""
    import re

    project = str(_value(source, "project_id") or "").strip().casefold()
    outcome = str(_value(source, "outcome") or _value(source, "title") or "").strip().casefold()
    origin = str(_value(source, "source_fingerprint") or _value(source, "source") or "").strip().casefold()
    clean = lambda value: re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    return ":".join((clean(project), clean(outcome), clean(origin)))


def envelope_from_task(task: Any, *, reviewer: str | None = None) -> dict[str, Any]:
    """Project a ``kanban_db.Task`` into the durable envelope shape.

    ``scope_manifest`` is the single extensible home for proof/stop/source
    evidence; repo is inferred only from the canonical ``.worktrees`` layout.
    """
    manifest = getattr(task, "scope_manifest", None)
    manifest = manifest if isinstance(manifest, dict) else {}
    workspace = Path(str(getattr(task, "workspace_path", "") or "")).expanduser()
    repo = manifest.get("repo_path")
    if not repo and workspace.is_absolute() and workspace.parent.name == ".worktrees":
        repo = str(workspace.parent.parent)
    return {
        "project_id": getattr(task, "project_id", None),
        "repo_path": repo,
        "workspace_kind": getattr(task, "workspace_kind", None),
        "workspace_path": getattr(task, "workspace_path", None),
        "branch_name": getattr(task, "branch_name", None),
        "assignee": getattr(task, "assignee", None),
        "reviewer": reviewer or manifest.get("reviewer"),
        "skills": getattr(task, "skills", None),
        "dependencies": manifest.get("dependencies"),
        "proof_command": manifest.get("proof_command"),
        "stop_condition": manifest.get("stop_condition"),
        "idempotency_key": getattr(task, "idempotency_key", None),
        "outcome": manifest.get("outcome") or getattr(task, "title", None),
        "source_fingerprint": manifest.get("source_fingerprint"),
    }
