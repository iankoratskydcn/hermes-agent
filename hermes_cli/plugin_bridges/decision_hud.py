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
