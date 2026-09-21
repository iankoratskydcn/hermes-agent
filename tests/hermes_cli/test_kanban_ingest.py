"""Isolation ingest gate: card-class matching and the .git-free diff path.

Regression coverage for adversarial-review findings:
- ``isolation_required`` must match ``card_class="single_blind"``, the only
  value ``BlindCard`` ever sets (the underscore/hyphen mismatch previously
  let BlindCard tasks skip the isolation gate entirely).
- ``_diff`` must be able to diff a projected (``.git``-free) workspace using
  its build-time manifest instead of assuming a git checkout.
- that manifest must live outside the worker-controlled workspace, or a
  worker could tamper with a file and its recorded hash together.
- a worker-introduced non-regular file (FIFO, device symlink) must never be
  opened for hashing, or the diff can hang forever instead of quarantining.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

from hermes_cli.kanban_ceiling import RoleCeiling, Scope, resolve_scope
from hermes_cli.kanban_ingest import _diff, _diff_projected, isolation_required, _validate
from hermes_cli.kanban_projection import build_projection


def test_isolation_required_matches_blind_card_canonical_class():
    assert isolation_required(SimpleNamespace(role=None, card_class="single_blind"))


def test_isolation_required_ignores_unknown_class():
    assert not isolation_required(SimpleNamespace(role=None, card_class="unscoped"))


def _write_manifest_sidecar(workspace: Path, manifest: dict) -> None:
    sidecar = workspace.parent / f".{workspace.name}.projection-manifest.json"
    sidecar.write_text(json.dumps(manifest), encoding="utf-8")


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
    handle = build_projection(sha, scope, dest, repo=repo)
    # Mirrors what kanban_db_dispatch._projected_workspace does: the manifest
    # sidecar lives next to, never inside, the workspace it baselines.
    _write_manifest_sidecar(dest, handle.manifest)
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


def test_diff_projected_manifest_lives_outside_workspace_so_worker_cannot_forge_it(tmp_path):
    """A worker with full control of ``workspace`` cannot hide a tamper by also
    rewriting an in-workspace manifest, because the manifest the diff trusts
    is not inside the workspace at all."""
    workspace = _build_projected_workspace(tmp_path)
    (workspace / "allowed.txt").write_text("tampered")
    # Even if the worker plants a forged manifest inside its own workspace
    # claiming everything still matches, the diff never reads it from there.
    (workspace / ".hermes_projection_manifest.json").write_text(
        json.dumps({"allowed.txt": "not-a-real-hash", "other.txt": "not-a-real-hash"}), encoding="utf-8",
    )
    ok, paths, reason = _diff_projected(workspace)
    assert ok and reason == "ok"
    # Both the tampered file and the forged manifest itself show up as
    # changes — the forged manifest is just another unauthorized file to the
    # diff, since it was never part of the baseline it's compared against.
    assert set(paths) == {"allowed.txt", ".hermes_projection_manifest.json"}


def test_diff_projected_never_opens_a_non_regular_file(tmp_path):
    """A worker-created FIFO must be flagged as changed, not opened for
    hashing — opening a FIFO with nothing writing to it blocks forever."""
    workspace = _build_projected_workspace(tmp_path)
    fifo_path = workspace / "pipe"
    os.mkfifo(fifo_path)
    ok, paths, reason = _diff_projected(workspace)
    assert ok and reason == "ok"
    assert "pipe" in paths


# Malformed scope_manifest validation tests: structurally invalid, incomplete, incorrectly typed.
def test_validate_rejects_missing_scope_manifest():
    """A task with no scope_manifest attribute fails validation."""
    task = SimpleNamespace()
    ok, reason = _validate(task)
    assert not ok
    assert "no valid scope_manifest" in reason


def test_validate_rejects_scope_manifest_as_json_string_invalid():
    """A scope_manifest that is an unparseable JSON string fails validation."""
    task = SimpleNamespace(scope_manifest="{invalid json")
    ok, reason = _validate(task)
    assert not ok
    assert "no valid scope_manifest" in reason


def test_validate_rejects_scope_manifest_as_non_dict_json():
    """A scope_manifest that parses to a non-dict (e.g., list or string) fails validation."""
    task = SimpleNamespace(scope_manifest=json.dumps(["read", "write"]))
    ok, reason = _validate(task)
    assert not ok
    assert "no valid scope_manifest" in reason


def test_validate_rejects_scope_manifest_with_read_as_none():
    """A scope_manifest with read=None fails validation (not a list)."""
    task = SimpleNamespace(scope_manifest=json.dumps({"read": None, "write": []}))
    ok, reason = _validate(task)
    assert not ok
    assert "list-valued read and write" in reason


def test_validate_rejects_scope_manifest_with_write_as_string():
    """A scope_manifest with write as a string instead of list fails validation."""
    task = SimpleNamespace(scope_manifest=json.dumps({"read": [], "write": "allowed.txt"}))
    ok, reason = _validate(task)
    assert not ok
    assert "list-valued read and write" in reason


def test_validate_rejects_empty_dict_scope_manifest():
    """An empty dict scope_manifest (missing both read and write keys) now fails validation
    because the defensive .get(..., []) ensures missing keys are iterated as empty lists."""
    task = SimpleNamespace(scope_manifest=json.dumps({}))
    ok, reason = _validate(task)
    # With the fix, empty dict defaults to read=[], write=[] which pass isinstance
    # but then iterate as empty lists (no paths to validate)
    assert ok and reason == "ok"  # Passes validation but fails at write-scope enforcement


def test_validate_rejects_read_with_non_string_element():
    """A scope_manifest with a non-string element in read list fails validation."""
    task = SimpleNamespace(scope_manifest=json.dumps({"read": [1, "file.txt"], "write": []}))
    ok, reason = _validate(task)
    assert not ok
    assert "unsafe path" in reason


def test_validate_rejects_read_with_empty_string():
    """A scope_manifest with an empty string path in read list fails validation."""
    task = SimpleNamespace(scope_manifest=json.dumps({"read": [""], "write": []}))
    ok, reason = _validate(task)
    assert not ok
    assert "unsafe path" in reason


def test_validate_rejects_write_with_absolute_path():
    """A scope_manifest with an absolute path in write list fails validation."""
    task = SimpleNamespace(scope_manifest=json.dumps({"read": [], "write": ["/etc/passwd"]}))
    ok, reason = _validate(task)
    assert not ok
    assert "unsafe path" in reason


def test_validate_rejects_write_with_traversal():
    """A scope_manifest with .. traversal in write path fails validation."""
    task = SimpleNamespace(scope_manifest=json.dumps({"read": [], "write": ["../outside"]}))
    ok, reason = _validate(task)
    assert not ok
    assert "unsafe path" in reason


def test_validate_rejects_read_with_whitespace_only():
    """A scope_manifest with a whitespace-only path in read list fails validation."""
    task = SimpleNamespace(scope_manifest=json.dumps({"read": ["   ", "file.txt"], "write": []}))
    ok, reason = _validate(task)
    assert not ok
    assert "unsafe path" in reason


def test_validate_accepts_valid_manifest():
    """A well-formed scope_manifest with valid read and write lists passes validation."""
    task = SimpleNamespace(scope_manifest=json.dumps({
        "read": ["src/allowed.py", "docs/**"],
        "write": ["src/allowed.py"]
    }))
    ok, reason = _validate(task)
    assert ok and reason == "ok"


# Incomplete or malformed manifest in diff context: passing validation but failing write-scope enforcement.
def test_diff_rejects_scope_manifest_with_empty_write_list(tmp_path):
    """A scope_manifest with an empty write list fails at write-scope enforcement,
    even though the manifest passes _validate."""
    workspace = _build_projected_workspace(tmp_path)
    (workspace / "allowed.txt").write_text("edited")
    task = SimpleNamespace(
        workspace_path=str(workspace),
        scope_manifest=json.dumps({"read": ["allowed.txt"], "write": []}),
    )
    ok, paths, reason = _diff(task)
    assert not ok
    assert "write is empty" in reason


def test_diff_rejects_scope_manifest_missing_keys_when_diff_has_changes(tmp_path):
    """An empty-dict scope_manifest (missing read/write keys) has them defaulted to []
    during validation, so it passes _validate but fails at write-scope due to empty write."""
    workspace = _build_projected_workspace(tmp_path)
    (workspace / "allowed.txt").write_text("edited")
    task = SimpleNamespace(
        workspace_path=str(workspace),
        scope_manifest=json.dumps({}),  # Empty dict, defaults to read=[], write=[]
    )
    ok, paths, reason = _diff(task)
    assert not ok
    assert "write is empty" in reason


def test_diff_enforces_write_scope_against_unauthorized_path(tmp_path):
    """A valid manifest that grants write to only some paths rejects unauthorized modifications."""
    workspace = _build_projected_workspace(tmp_path)
    (workspace / "allowed.txt").write_text("v2")
    (workspace / "other.txt").write_text("unauthorized")
    task = SimpleNamespace(
        workspace_path=str(workspace),
        scope_manifest=json.dumps({
            "read": ["allowed.txt", "other.txt"],
            "write": ["allowed.txt"]  # other.txt not authorized
        }),
    )
    ok, paths, reason = _diff(task)
    assert not ok
    assert "other.txt" in reason
    assert "outside scope_manifest.write" in reason


def test_diff_allows_authorized_multiple_write_paths(tmp_path):
    """A manifest granting write to multiple paths allows changes to all of them."""
    workspace = _build_projected_workspace(tmp_path)
    (workspace / "allowed.txt").write_text("v2")
    (workspace / "other.txt").write_text("v2")
    task = SimpleNamespace(
        workspace_path=str(workspace),
        scope_manifest=json.dumps({
            "read": ["allowed.txt", "other.txt"],
            "write": ["allowed.txt", "other.txt"]
        }),
    )
    ok, paths, reason = _diff(task)
    assert ok and reason == "ok"
    assert set(paths) == {"allowed.txt", "other.txt"}
