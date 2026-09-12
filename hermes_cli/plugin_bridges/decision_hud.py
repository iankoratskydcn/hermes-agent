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

import importlib.util
import sqlite3
import sys
from pathlib import Path
from typing import Optional

_MODULE_CACHE_KEY = "_hermes_decision_hud_db_bridge"


def _decision_hud_db_path() -> Path:
    from hermes_constants import get_hermes_home
    return get_hermes_home() / "plugins" / "decision-hud" / "db.py"


def _load_decision_hud_db():
    """Import decision-hud's ``db.py`` by absolute path, cached in
    ``sys.modules`` under a private key so repeated calls in one process
    (every dispatch tick) don't re-parse the file."""
    cached = sys.modules.get(_MODULE_CACHE_KEY)
    if cached is not None:
        return cached
    db_path = _decision_hud_db_path()
    if not db_path.is_file():
        raise ImportError(f"decision-hud not installed: {db_path} not found")
    spec = importlib.util.spec_from_file_location(_MODULE_CACHE_KEY, db_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load decision-hud db module from {db_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MODULE_CACHE_KEY] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        # Don't cache a half-initialised module on failure.
        sys.modules.pop(_MODULE_CACHE_KEY, None)
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
            conn, project=project, task_id=task_id, question=question, urgency=urgency,
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
        db.require_constraint_resolved(conn, project=project, task_id=task_id)
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
        db.require_batch_approval(conn, project=project, batch_id=batch_id)
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
        row = db.push_problem_report(conn, project=project, problem=problem, context=context, reporter=reporter)
        return True, str(row.get("id", ""))
    except Exception as exc:
        return False, f"problem report push failed: {exc}"
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
