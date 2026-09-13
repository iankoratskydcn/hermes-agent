"""Bridge into the standalone ``decision-hud`` plugin's ``db.py`` — the
Rule-1 dispatch-side batch-approval gate (F1).

decision-hud (``~/.hermes/plugins/decision-hud/``) is a standalone plugin,
not a hermes-agent package: it is not on ``sys.path`` and has no
``__init__.py`` chain back to this repo. It also is NOT git-tracked at that
path (an operator-managed install), so it cannot be imported as
``hermes_cli.something`` nor vendored here without duplicating its
approve/reject/pending logic — the exact thing F1 says not to do. This
module locates ``db.py`` by absolute file path (honouring
``get_hermes_home()`` so profiles resolve their own plugin tree) and loads
it via ``importlib.util``, then calls its ``require_batch_approval()``
directly. Every dispatch-side caller (``kanban_db_dispatch.py``) goes
through :func:`check_batch_approval` so there is exactly one integration
point to keep in sync with decision-hud's schema.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import logging
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Optional

_MODULE_CACHE_KEY = "_hermes_decision_hud_db_bridge"
# v6 is the first schema with the project_id contract used by this bridge.
_MIN_DECISION_HUD_SCHEMA_VERSION = 6
_REVISION_RECORD_NAME = "decision_hud_bridge_revision.json"
_LOG = logging.getLogger(__name__)


def _decision_hud_db_path() -> Path:
    from hermes_constants import get_hermes_home
    return get_hermes_home() / "plugins" / "decision-hud" / "db.py"


def _revision_record_path() -> Path:
    """Return the profile-scoped, operator-local TOFU record path."""
    from hermes_constants import get_hermes_home

    return Path(get_hermes_home()) / "decision_hud" / _REVISION_RECORD_NAME


def _atomic_write_revision_record(path: Path, record: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(record, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def _observe_plugin_revision(db_path: Path) -> None:
    """Record the first source hash; warn, but never block, on later changes."""
    digest = hashlib.sha256(db_path.read_bytes()).hexdigest()
    record_path = _revision_record_path()
    previous: Optional[dict[str, object]] = None
    try:
        previous = json.loads(record_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        pass
    except (OSError, json.JSONDecodeError) as exc:
        _LOG.warning("Decision HUD revision record unreadable (%s); replacing it", exc)
    if isinstance(previous, dict) and previous.get("sha256") not in (None, digest):
        _LOG.warning(
            "Decision HUD plugin source hash changed: %s -> %s (%s)",
            previous.get("sha256"), digest, db_path,
        )
    _atomic_write_revision_record(
        record_path,
        {"path": str(db_path), "sha256": digest},
    )


def _assert_supported_schema(module: object) -> None:
    """Reject a plugin whose live SQLite schema is too old, before migration."""
    try:
        database_path = Path(module.db_path())  # type: ignore[attr-defined]
    except Exception as exc:
        raise ImportError(f"decision-hud database path unavailable: {exc}") from exc
    if not database_path.exists() or database_path.stat().st_size == 0:
        # Fresh installs are initialized by the plugin's normal connect() path.
        return
    conn = sqlite3.connect(str(database_path))
    try:
        row = conn.execute("PRAGMA user_version").fetchone()
        version = int(row[0]) if row else 0
    finally:
        conn.close()
    if version < _MIN_DECISION_HUD_SCHEMA_VERSION:
        raise ImportError(
            "decision-hud schema too old: "
            f"{version}; bridge requires >= {_MIN_DECISION_HUD_SCHEMA_VERSION}"
        )


def _source_digest(db_path: Path) -> str:
    """Hash the source so an in-place replacement cannot reuse stale code."""
    return hashlib.sha256(db_path.read_bytes()).hexdigest()


def _load_decision_hud_db():
    """Import and validate the profile-scoped standalone ``db.py``."""
    db_path = _decision_hud_db_path().expanduser().resolve()
    if not db_path.is_file():
        raise ImportError(f"decision-hud not installed: {db_path} not found")
    module_cache_key = f"{_MODULE_CACHE_KEY}:{db_path}"
    cached = sys.modules.get(module_cache_key)
    digest = _source_digest(db_path)
    if cached is not None:
        # Validate both identity and current source before using a cached module.
        # The file may have been atomically replaced in place while this process
        # remained alive; schema validation alone cannot detect stale code.
        if (
            getattr(cached, "__hermes_decision_hud_path__", None) == str(db_path)
            and getattr(cached, "__hermes_decision_hud_digest__", None) == digest
        ):
            _assert_supported_schema(cached)
            return cached
        sys.modules.pop(module_cache_key, None)
    spec = importlib.util.spec_from_file_location(module_cache_key, db_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load decision-hud db module from {db_path}")
    module = importlib.util.module_from_spec(spec)
    setattr(module, "__hermes_decision_hud_path__", str(db_path))
    setattr(module, "__hermes_decision_hud_digest__", digest)
    sys.modules[module_cache_key] = module
    try:
        spec.loader.exec_module(module)
        _assert_supported_schema(module)
        _observe_plugin_revision(db_path)
    except Exception:
        # Don't cache a half-initialised or incompatible module on failure.
        sys.modules.pop(module_cache_key, None)
        raise
    return module


def push_task_missing_constraint(
    *, project: str, task_id: str, question: str, urgency: str = "normal",
) -> tuple[bool, str]:
    """Push a Rule-4 ``missing_constraint`` decision-hud card for one task.

    ``(True, "")`` on a successful push. ``(False, reason)`` for every
    failure mode, INCLUDING decision-hud's own duplicate-open-constraint
    ``ValueError`` (a task that already has an unresolved missing_constraint
    row for this ``(project, task_id)`` must not get a second card — the
    caller (the retry-cap dispatch gate) treats that as "already escalated,
    nothing new to do" rather than an error to surface). Never raises —
    matches :func:`check_batch_approval`'s never-raises contract so the
    dispatch-side caller's fail-closed default applies uniformly.
    """
    try:
        db = _load_decision_hud_db()
    except Exception as exc:
        return False, f"decision-hud unavailable: {exc}"
    conn: Optional[sqlite3.Connection] = None
    try:
        conn = db.connect()
        db.push_missing_constraint(
            conn, project_id=project, task_id=task_id, question=question, urgency=urgency,
        )
        return True, ""
    except ValueError as exc:
        # Duplicate open constraint for this (project, task_id) — not an
        # error, just "already escalated, don't push a second card".
        return False, str(exc)
    except Exception as exc:
        return False, f"push_missing_constraint errored: {exc}"
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def check_constraint_resolved(*, project: str, task_id: str) -> bool:
    """``True`` iff decision-hud has a RESOLVED ``missing_constraint``
    decision for this exact ``(project, task_id)`` (the most recent row —
    see decision-hud's ``require_constraint_resolved`` docstring: an old
    resolved escalation never masks a fresh unresolved one). ``False`` for
    pending/missing/any error — every failure mode (plugin not installed, DB
    locked/corrupt, schema mismatch) folds into ``False`` so the dispatch-
    side caller's fail-closed default applies uniformly, mirroring
    :func:`check_batch_approval`'s never-raises contract.
    """
    try:
        db = _load_decision_hud_db()
    except Exception:
        return False
    conn: Optional[sqlite3.Connection] = None
    try:
        conn = db.connect()
        db.require_constraint_resolved(conn, project_id=project, task_id=task_id)
        return True
    except db.ConstraintNotResolved:
        return False
    except Exception:
        return False
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def check_batch_approval(*, project: str, batch_id: str) -> tuple[bool, str]:
    """``(True, "")`` iff decision-hud has an ``approve``-resolved
    ``batch_approval`` decision for this exact ``(project, batch_id)``;
    ``(False, reason)`` for pending/rejected/missing/any error. Never
    raises — every failure mode (plugin not installed, DB locked/corrupt,
    schema mismatch) is folded into a ``(False, reason)`` result so the
    dispatch-side caller's fail-closed default applies uniformly.
    """
    try:
        db = _load_decision_hud_db()
    except Exception as exc:
        return False, f"decision-hud unavailable: {exc}"
    conn: Optional[sqlite3.Connection] = None
    try:
        conn = db.connect()
        db.require_batch_approval(conn, project_id=project, batch_id=batch_id)
        return True, ""
    except db.BatchNotApproved as exc:
        return False, str(exc)
    except Exception as exc:
        return False, f"batch approval check errored: {exc}"
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def push_problem_report(
    *, project: str, problem: str, context: Optional[str] = None, reporter: Optional[str] = None,
) -> tuple[bool, str]:
    """File a raw problem report in decision-hud (Rule 6 EARS-gate refusal
    path). ``(True, report_id)`` on success, ``(False, reason)`` on any
    failure — mirrors :func:`check_batch_approval`'s never-raises contract
    so a decision-hud outage never crashes the caller (the decomposer
    proceeds with ``ears_sentence=None`` regardless; see
    ``kanban_decompose.py``'s EARS-gate docstring for the fail-open
    rationale).
    """
    try:
        db = _load_decision_hud_db()
    except Exception as exc:
        return False, f"decision-hud unavailable: {exc}"
    conn: Optional[sqlite3.Connection] = None
    try:
        conn = db.connect()
        row = db.push_problem_report(conn, project_id=project, problem=problem, context=context, reporter=reporter)
        return True, str(row.get("id", ""))
    except Exception as exc:
        return False, f"problem report push failed: {exc}"
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
