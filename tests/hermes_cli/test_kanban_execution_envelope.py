"""RED contract tests for fail-closed Kanban execution envelopes."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli.kanban_execution_envelope import validate_execution_envelope


def _valid(**overrides):
    value = {
        "project_id": "p_demo",
        "repo_path": "/srv/repo",
        "workspace_kind": "worktree",
        "workspace_path": "/srv/repo/.worktrees/t_demo",
        "branch_name": "demo/t_demo",
        "assignee": "builder",
        "reviewer": "reviewer",
        "skills": ["verification-gate"],
        "dependencies": [],
        "proof_command": "scripts/run_tests.sh tests/hermes_cli/test_kanban_execution_envelope.py",
        "stop_condition": "block on missing authority or credentials",
        "idempotency_key": "kanban:p_demo:demo:t_demo",
    }
    value.update(overrides)
    return value


def test_valid_envelope_passes():
    result = validate_execution_envelope(_valid())
    assert result.ok
    assert result.errors == []


@pytest.mark.parametrize(
    ("field", "value", "needle"),
    [
        ("project_id", None, "project_id is required"),
        ("workspace_path", "/srv/repo", "workspace_path must be an isolated worktree"),
        ("workspace_path", "relative/.worktrees/t_demo", "absolute"),
        ("branch_name", "", "branch_name is required"),
        ("reviewer", "builder", "must differ"),
        ("skills", [], "skills are required"),
        ("proof_command", "", "proof_command is required"),
        ("stop_condition", "", "stop_condition is required"),
        ("idempotency_key", "", "idempotency_key is required"),
    ],
)
def test_missing_or_unsafe_envelope_fails_closed(field, value, needle):
    result = validate_execution_envelope(_valid(**{field: value}))
    assert not result.ok
    assert any(needle in error for error in result.errors)


def test_root_path_mistake_has_actionable_correction(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    result = validate_execution_envelope(_valid(repo_path=str(repo), workspace_path=str(repo)))
    assert not result.ok
    assert any("repo/.worktrees/<task-id>" in error for error in result.errors)
    assert result.correction


def test_namespace_object_supported():
    result = validate_execution_envelope(SimpleNamespace(**_valid()))
    assert result.ok
