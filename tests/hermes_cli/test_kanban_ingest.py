"""Isolation ingest gate: card-class matching and the .git-free diff path.

Regression coverage for two adversarial-review findings:
- ``isolation_required`` must match ``card_class="single_blind"``, the only
  value ``BlindCard`` ever sets (the underscore/hyphen mismatch previously
  let BlindCard tasks skip the isolation gate entirely).
- ``_diff`` must be able to diff a projected (``.git``-free) workspace using
  its build-time manifest instead of assuming a git checkout.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

from hermes_cli.kanban_ceiling import RoleCeiling, Scope, resolve_scope
from hermes_cli.kanban_ingest import _diff, _diff_projected, isolation_required
from hermes_cli.kanban_projection import build_projection


def test_isolation_required_matches_blind_card_canonical_class():
    assert isolation_required(SimpleNamespace(role=None, card_class="single_blind"))


def test_isolation_required_ignores_unknown_class():
    assert not isolation_required(SimpleNamespace(role=None, card_class="unscoped"))


def _build_projected_workspace(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "allowed.txt").write_text("v1")
    (repo / "other.txt").write_text("o1")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-qm", "init"],
        cwd=repo, check=True,
    )
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    scope = resolve_scope(
        SimpleNamespace(scope_paths=["allowed.txt", "other.txt"]),
        RoleCeiling(frozenset({"allowed.txt", "other.txt"}), frozenset({"allowed.txt", "other.txt"})),
        repo=repo, base_sha=sha,
    )
    dest = tmp_path / "workspace"
    build_projection(sha, scope, dest, repo=repo)
    return dest


def test_diff_projected_clean_workspace_has_no_changes(tmp_path):
    workspace = _build_projected_workspace(tmp_path)
    ok, paths, reason = _diff_projected(workspace)
    assert ok and paths == () and reason == "ok"


def test_diff_projected_detects_edit_add_and_delete(tmp_path):
    workspace = _build_projected_workspace(tmp_path)
    (workspace / "allowed.txt").write_text("v2")  # edit
    (workspace / "other.txt").unlink()  # delete
    (workspace / "new.txt").write_text("new")  # add
    ok, paths, reason = _diff_projected(workspace)
    assert ok and reason == "ok"
    assert set(paths) == {"allowed.txt", "other.txt", "new.txt"}


def test_diff_rejects_projected_workspace_missing_manifest(tmp_path):
    workspace = tmp_path / "no_manifest"
    workspace.mkdir()
    (workspace / "f.txt").write_text("x")
    task = SimpleNamespace(workspace_path=str(workspace), scope_manifest=json.dumps({"read": [], "write": ["*"]}))
    ok, paths, reason = _diff(task)
    assert not ok
    assert "manifest" in reason


def test_diff_projected_enforces_scope_manifest_write(tmp_path):
    workspace = _build_projected_workspace(tmp_path)
    (workspace / "other.txt").write_text("edited")
    task = SimpleNamespace(
        workspace_path=str(workspace),
        scope_manifest=json.dumps({"read": ["allowed.txt", "other.txt"], "write": ["allowed.txt"]}),
    )
    ok, paths, reason = _diff(task)
    assert not ok
    assert "other.txt" in reason
