"""Rolling-window provider-quota guard for the kanban dispatcher.

The existing respawn guard (``kanban_db_dispatch.check_respawn_guard``) reacts
to a rate-limit hit AFTER the fact, one task at a time: a worker's own
provider client exits with ``KANBAN_RATE_LIMIT_EXIT_CODE``, the run is
recorded ``outcome='rate_limited'``, and that ONE task gets a per-task
cooldown before it is retried. A fan-out where five different tasks share one
provider account each earn their own private cooldown and independently keep
hammering the same 429 wall — this module closes that gap by pooling
``rate_limited`` outcomes across every profile named in an operator-defined
"account" and refusing new dispatch to the WHOLE account once its rolling
window is over budget.

No new dependencies, no schema change: it reads only the existing
``task_runs.outcome`` / ``task_runs.ended_at`` columns the dispatcher already
writes on every rate-limited exit. Auto-resume is free — the window simply
ages out on a later tick, exactly like the per-task cooldown it complements.

Opt-in via ``kanban.provider_budgets`` in config.yaml (default ``{}`` =
disabled), matching this repo's precedent for new, unproven dispatch
guards (``gate_precheck_enabled``, ``sidecar_routing_enabled``): an operator
must explicitly map profiles to a shared account before this does anything.

Enforcement point: ``hermes_cli/kanban_db_dispatch.py``'s per-task dispatch
guard (``_dispatch_lane_task``), the same call site as the gate precheck.
"""

from __future__ import annotations

import time
from typing import Any, Optional

# Rolling window over which rate-limit hits are pooled per account.
DEFAULT_WINDOW_SECONDS = 3600  # 1 hour

# Rate-limit hits within the window before the WHOLE account is paused.
DEFAULT_MAX_RATE_LIMIT_HITS = 3


def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def provider_budgets_config(kanban_cfg: Optional[dict]) -> dict[str, dict]:
    """Normalize ``kanban.provider_budgets`` into
    ``{account_name: {"profiles": [...], "max_rate_limit_hits": int, "window_seconds": int}}``.

    Malformed or empty input normalizes to ``{}`` (feature off) rather than
    raising: a typo'd config.yaml section must never crash a dispatch tick.
    An account naming no profiles is dropped — it can never match an
    assignee, so keeping it would be a silent no-op that looks configured.
    """
    if not isinstance(kanban_cfg, dict):
        return {}
    raw = kanban_cfg.get("provider_budgets")
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict] = {}
    for name, entry in raw.items():
        if not isinstance(entry, dict):
            continue
        profiles = entry.get("profiles")
        if not isinstance(profiles, (list, tuple)) or not profiles:
            continue
        out[str(name)] = {
            "profiles": list(profiles),
            "max_rate_limit_hits": _positive_int(
                entry.get("max_rate_limit_hits"), DEFAULT_MAX_RATE_LIMIT_HITS,
            ),
            "window_seconds": _positive_int(
                entry.get("window_seconds"), DEFAULT_WINDOW_SECONDS,
            ),
        }
    return out


def provider_budgets_enabled(kanban_cfg: Optional[dict]) -> bool:
    """Cheap check for the dispatcher hot path: any usable account configured?"""
    return bool(provider_budgets_config(kanban_cfg))


def resolve_account_for_assignee(accounts: dict[str, dict], assignee: str) -> Optional[str]:
    """Return the account name whose ``profiles`` list contains ``assignee``,
    or ``None`` when the assignee is not budget-managed. First match wins on
    an (operator error) overlapping config."""
    for name, entry in accounts.items():
        if assignee in entry.get("profiles", ()):
            return name
    return None


def check_provider_budget(
    conn, assignee: str, *, kanban_cfg: Optional[dict], now: Optional[int] = None,
) -> Optional[str]:
    """Return a guard reason if ``assignee``'s provider account is over its
    rolling rate-limit budget, else ``None``.

    Pools ``task_runs`` rows across EVERY profile in the resolved account —
    the whole point is that one task's 429 counts against every other task
    sharing that account's credentials, not just itself. Read-only; the
    dispatcher decides what to do with a non-None reason (skip this tick,
    same shape as ``skipped_gate_precheck`` / ``respawn_guarded``).
    """
    accounts = provider_budgets_config(kanban_cfg)
    if not accounts:
        return None
    account_name = resolve_account_for_assignee(accounts, assignee)
    if account_name is None:
        return None
    account = accounts[account_name]
    now_ts = int(now if now is not None else time.time())
    cutoff = now_ts - account["window_seconds"]
    profiles = account["profiles"]
    placeholders = ",".join("?" for _ in profiles)
    row = conn.execute(
        f"SELECT COUNT(*) AS n FROM task_runs "
        f"WHERE profile IN ({placeholders}) AND outcome = 'rate_limited' "
        f"AND ended_at IS NOT NULL AND ended_at >= ?",
        (*profiles, cutoff),
    ).fetchone()
    hit_count = int(row["n"] if row is not None else 0)
    if hit_count >= account["max_rate_limit_hits"]:
        return f"provider_budget_exhausted:{account_name}"
    return None
