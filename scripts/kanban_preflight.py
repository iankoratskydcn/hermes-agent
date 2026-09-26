#!/usr/bin/env python3
"""Preflight and optionally repair one Kanban card.

Dry-run is default. Apply archives the malformed card and creates one corrected
replacement, preserving the old card's audit trail and adding cross-links in
comments. Example:

  python3 scripts/kanban_preflight.py t_cf18a306
  python3 scripts/kanban_preflight.py t_cf18a306 --apply --reviewer reviewer \
    --skill verification-gate --proof-command 'scripts/run_tests.sh tests/hermes_cli/' \
    --stop-condition 'block on missing authority, credentials, or failed tests'
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


# Allow direct execution from a checkout.
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import projects_db
from hermes_cli.kanban_execution_envelope import validate_execution_envelope


def _project_repo(project_id: str | None) -> str | None:
    if not project_id:
        return None
    try:
        with projects_db.connect_closing() as conn:
            project = projects_db.get_project(conn, project_id)
        return str(project.primary_path) if project and project.primary_path else None
    except Exception:
        return None


def _envelope(task, *, reviewer: str | None, proof_command: str | None,
              stop_condition: str | None, skills: list[str] | None) -> dict:
    manifest = task.scope_manifest or {}
    if not isinstance(manifest, dict):
        manifest = {}
    return {
        "project_id": task.project_id,
        "repo_path": _project_repo(task.project_id) or manifest.get("repo_path"),
        "workspace_kind": task.workspace_kind,
        "workspace_path": task.workspace_path,
        "branch_name": task.branch_name,
        "assignee": task.assignee,
        "reviewer": reviewer or manifest.get("reviewer"),
        "skills": skills if skills is not None else task.skills,
        "dependencies": manifest.get("dependencies", []),
        "proof_command": proof_command or manifest.get("proof_command"),
        "stop_condition": stop_condition or manifest.get("stop_condition"),
        "idempotency_key": task.idempotency_key,
    }


def preflight(task, **kwargs) -> dict:
    envelope = _envelope(task, **kwargs)
    result = validate_execution_envelope(envelope)
    return {"task_id": task.id, "title": task.title, "envelope": envelope, **result.as_dict()}


def apply_repair(conn, task, *, reviewer: str, proof_command: str,
                 stop_condition: str, skills: list[str]) -> str:
    repo = _project_repo(task.project_id)
    if not repo:
        raise ValueError("cannot apply: project_id has no resolvable canonical repo")
    replacement_key = f"repair:{task.project_id}:{task.id}"
    replacement_path = str(Path(repo).resolve() / ".worktrees" / task.id)
    manifest = dict(task.scope_manifest or {})
    manifest.update({
        "repo_path": str(Path(repo).resolve()),
        "reviewer": reviewer,
        "proof_command": proof_command,
        "stop_condition": stop_condition,
        "dependencies": manifest.get("dependencies", []),
        "repaired_from": task.id,
    })
    parents = kb.parent_ids(conn, task.id)
    kb.archive_task(conn, task.id)
    replacement_id = kb.create_task(
        conn,
        title=task.title,
        body=task.body,
        assignee=task.assignee,
        created_by="kanban-preflight",
        workspace_kind="worktree",
        workspace_path=replacement_path,
        branch_name=f"{Path(repo).name}/{task.id}",
        project_id=task.project_id,
        parents=parents,
        triage=True,
        idempotency_key=replacement_key,
        skills=skills,
        completion_contract=task.completion_contract,
        scope_manifest=manifest,
        initial_status="blocked",
    )
    kb.add_comment(conn, task.id, "kanban-preflight", f"Archived as malformed execution envelope; replacement {replacement_id} created.")
    kb.add_comment(conn, replacement_id, "kanban-preflight", f"Replaces archived card {task.id}; audit preserved. Review before unblock/dispatch.")
    return replacement_id


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task_id", nargs="?")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--reviewer")
    parser.add_argument("--proof-command")
    parser.add_argument("--stop-condition")
    parser.add_argument("--skill", action="append", dest="skills")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)
    if args.self_test:
        root = Path.cwd() / "repo"
        result = validate_execution_envelope({
            "project_id": "p", "repo_path": str(root), "workspace_kind": "worktree",
            "workspace_path": str(root), "branch_name": "b", "assignee": "a", "reviewer": "r",
            "skills": ["s"], "dependencies": [], "proof_command": "true",
            "stop_condition": "block", "idempotency_key": "k",
        })
        assert not result.ok and any("repo/.worktrees/<task-id>" in item for item in result.errors)
        print("self-test passed")
        return 0
    if not args.task_id:
        parser.error("task_id is required unless --self-test is used")
    with kbc.connect_closing() as conn:
        task = kb.get_task(conn, args.task_id)
        if task is None:
            print(f"no such task: {args.task_id}", file=sys.stderr)
            return 2
        report = preflight(task, reviewer=args.reviewer, proof_command=args.proof_command,
                           stop_condition=args.stop_condition, skills=args.skills)
        print(json.dumps(report, indent=2, ensure_ascii=False))
        if report["ok"] or not args.apply:
            return 0 if report["ok"] else 1
        if not args.reviewer or not args.proof_command or not args.stop_condition or not args.skills:
            print("apply refused: provide --reviewer, --proof-command, --stop-condition, and at least one --skill", file=sys.stderr)
            return 2
        replacement = apply_repair(conn, task, reviewer=args.reviewer, proof_command=args.proof_command,
                                   stop_condition=args.stop_condition, skills=args.skills)
        print(f"created corrected replacement {replacement}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
