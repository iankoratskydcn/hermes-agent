"""SCRATCH self-check — NON-AUTHORITATIVE. Written by the implementer to
drive TDD while building the F1/F5 dispatch gates; NOT the acceptance test
suite and must not be cited as proof of correctness. A separate test-author
agent writes the graded acceptance tests against the committed diff. Kept
here only so a reviewer can rerun the developer's own reasoning; delete or
ignore if unhelpful.

Exercises: F5 board dispatch_enabled gate at dispatch_once(); F1
batch_approval_gate wired to the real decision-hud db.py via the bridge;
review_dispatch_enabled fail-closed on exception. Run manually:

    HERMES_HOME=$(mktemp -d) python3 self_check_wave1_dispatch_gate.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

home = Path(tempfile.mkdtemp())
os.environ["HERMES_HOME"] = str(home)
for v in ("HERMES_KANBAN_DB", "HERMES_KANBAN_WORKSPACES_ROOT", "HERMES_KANBAN_HOME", "HERMES_KANBAN_BOARD"):
    os.environ.pop(v, None)

sys.path.insert(0, str(Path(__file__).resolve().parent))

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd

failures = []


def check(name, cond):
    status = "OK" if cond else "FAIL"
    print(f"[{status}] {name}")
    if not cond:
        failures.append(name)


# --- F5: dispatch_enabled=False on the resolved board blocks dispatch_once ---
kb.create_board("proj")
kb.write_board_metadata("proj", dispatch_enabled=False)
with kbc.connect_closing(board="proj") as conn:
    kb.create_task(conn, title="t1", assignee="nope-profile")
    called = {"n": 0}

    def _spawn(*a, **k):
        called["n"] += 1
        return 999

    res = kbd.dispatch_once(conn, board="proj", spawn_fn=_spawn)
    check("F5: dispatch_enabled=False -> gate_blocked set", res.gate_blocked == "board_dispatch_disabled")
    check("F5: dispatch_enabled=False -> no spawn attempted", called["n"] == 0)

# Re-enable: dispatch proceeds (no batch gate configured)
kb.write_board_metadata("proj", dispatch_enabled=True)
with kbc.connect_closing(board="proj") as conn:
    res = kbd.dispatch_once(conn, board="proj", dry_run=True)
    check("F5: dispatch_enabled=True -> gate not blocked", res.gate_blocked is None)

# --- F1: batch_approval_gate configured but not approved blocks dispatch ---
kb.create_board("gated")
kb.write_board_metadata("gated", batch_approval_gate={"project": "demo", "batch_id": "b1"})
with kbc.connect_closing(board="gated") as conn:
    res = kbd.dispatch_once(conn, board="gated", dry_run=True)
    check(
        "F1: unapproved batch -> gate_blocked=batch_approval_required",
        res.gate_blocked == "batch_approval_required",
    )

ok, reason = kbd.batch_approval_gate_ok("gated")
check("F1: batch_approval_gate_ok returns False with unapproved batch", ok is False)
print(f"    reason: {reason}")

# --- review_dispatch_enabled fails CLOSED on a genuine exception ---
import hermes_cli.kanban_db as kb_mod


class _Boom:
    def get(self, *a, **k):
        raise RuntimeError("boom")


orig = kb_mod.read_board_metadata
kb_mod.read_board_metadata = lambda *a, **k: _Boom()
try:
    result = kbd.review_dispatch_enabled(board="proj")
    check("review_dispatch_enabled fails CLOSED on exception", result is False)
finally:
    kb_mod.read_board_metadata = orig

print()
if failures:
    print(f"{len(failures)} SCRATCH CHECK(S) FAILED: {failures}")
    sys.exit(1)
print("All scratch checks passed (non-authoritative).")
