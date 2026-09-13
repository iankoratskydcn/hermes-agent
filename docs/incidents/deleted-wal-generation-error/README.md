# Incident: recurring DeletedWalGenerationError on state.db

Investigation swarm run 2026-09-13. 19 investigation lanes (L01-L19) + 5 adversarial
review lanes (A01-A05). Full raw lane outputs preserved as JSON in this directory;
`ledger.md` is the dispatch plan, `consolidated.md` is the mid-swarm synthesis written
before the adversarial pass (kept for audit trail — superseded by A05's synthesis below).

## UPDATE (post-swarm, same day): the primary code defect was already fixed upstream

Before implementing L09/A01's proposed fix (migrate `cli.py`'s bare `SessionDB()` to
the shared registry), a diff against `upstream/main` found commit `876e444e4e`
("fix(cli): open the session store through the state.db registry, not a bare
SessionDB()", landed 2026-09-12 — one day before this swarm ran) already makes
exactly that change, across all three sites the swarm identified
(`cli.py:_init_session_store`, `cli_agent_setup_mixin.py`, `cli_commands_mixin.py`).
It was written for an unrelated symptom (a 0.7-2s CLI startup freeze between banner
and first prompt, caused by a redundant `/proc`-wide deleted-WAL scan), but the fix
shape is identical and closes this incident's primary reproducible defect as a side
effect. Confirmed live: a parent CLI handle (via `_init_session_store`) and a
`delegate_tool` child spawned from it now share the exact same `sqlite3` connection
object (`test_delegate_tool_child_shares_one_connection_with_the_cli_session`,
verified RED on `876e444e4e~1`, GREEN on this branch).

**What actually shipped in this branch:** the missing regression test only — no
production code change, since the fix already exists upstream. L01's secondary
finding (the write-time guard's PID-blindness vs. the open-time guard's PID
exemption) was investigated for a Phase 2 fix but **abandoned after empirical
testing found no reproducible trigger**: `PRAGMA wal_checkpoint(TRUNCATE)` (the one
documented legitimate same-process rotation path, used by `vacuum()`) did not
actually change the sidecar inode identity in direct testing — SQLite reused the
same `-wal`/`-shm` files rather than unlinking/recreating them. The asymmetry is
real in the code's *structure* (confirmed by direct reading), but every attempt to
make it manifest as an actual generation-loss event through a legitimate
same-process operation failed; only a genuinely external `unlink()` reproduces
generation loss, and by definition nothing in-process can positively attribute an
external process's action as "trusted." Per this repo's own contribution rubric
("speculative infrastructure... with no concrete consumer" is explicitly rejected
even when well-built), adding "same-process adoption" complexity to
corruption-prevention code with no reproducible trigger was correctly abandoned
rather than shipped. If a future incident DOES reproduce this asymmetry as a live
problem, redo this investigation with the actual reproduction steps in hand.

**Lesson for future incident response: diff against upstream before implementing a
fix a large investigation converged on.** 19 lanes + 5 adversarial reviewers
correctly diagnosed the mechanism without anyone checking whether it was already
fixed one commit up the tree.

## tl;dr (see A05.json for the full adversarial synthesis)

`hermes_state_registry.py`'s writer-handle dedup is **process-local only** (plain dict +
threading.Lock, no cross-process primitive). The two most concrete, independently
re-verified (L09 -> A01) code-level defects:

1. `cli.py`'s long-lived interactive-session `SessionDB()` is constructed bare
   (unregistered, unresolved `db_path`). When that session delegates to a subagent,
   `delegate_tool.py`'s child-session lookup resolves through
   `hermes_state_registry.acquire()` against the same physical file — since the parent
   was never registered, this mints a **second, genuinely independent writer connection
   on the same inode**, within one related process tree. Reproduced/confirmed twice.

2. The write-time guard (`_wal_generation_was_lost`/`_halt_if_db_generation_changed` in
   `hermes_state.py`) is a pure `(st_dev, st_ino)` stat check with **zero same-process
   sibling awareness**, unlike the open-time guard which explicitly exempts its own PID.

**Important negative result (A03):** a real cross-process repro (two genuinely separate
`python3` OS processes via `subprocess.Popen`, one holding a long-lived writer, the other
opening+writing+cleanly-closing against the same file) did **NOT** reproduce the error,
3/3 runs. `SessionDB.close()` deliberately uses `PRAGMA wal_checkpoint(PASSIVE)`, not
`TRUNCATE`, specifically to avoid tearing a live sibling's WAL. This disconfirms the
naive "any other process's clean close rotates the generation" framing from the initial
consolidated theory — the actual trigger in real incidents is narrower, most concretely
the CLI/delegate_tool double-writer-on-one-inode scenario above, or an as-yet-unreproduced
transient all-writers-momentarily-zero window during service restarts (the observed
incidents cluster right after gateway/dashboard restarts).

`DeletedWalGenerationError`'s fail-closed, manual-restart-required behavior is
**intentional and correct** (L16, confidence 0.98) — it exists specifically to prevent
the wrong-page/split-brain corruption class from issue #105670 and must not be weakened
or auto-recovered.

## Planned fix (see PR on branch `fix/state-db-registry-cli-double-writer`)

Phase 0: failing regression tests reproducing the CLI/delegate_tool double-writer
scenario and the restart-race window, before any code change (TDD).
Phase 1: migrate `cli.py`'s long-lived `SessionDB()` to `hermes_state_registry.acquire()`
/`release_or_close()`, same contract gateway/tui_gateway/cron already use.
Phase 2 (separate, lower-risk-gated PR): add a monotonic per-path generation token so the
write-time guard can positively attribute (not merely assume) a same-process sibling
rotation before adopting it — per A04's explicit caution against blind adoption.

## Files

- `ledger.md` — original 20-lane dispatch plan
- `consolidated.md` — mid-swarm synthesis (pre-adversarial-review; see A05 for the
  corrected/final version)
- `L01.json`-`L19.json` — investigation lane raw findings (L07 reconstructed from
  inline-reported JSON; its file write never landed during the run)
- `A01.json`-`A05.json` — adversarial review lane raw findings; A05 is the final
  synthesis and most defensible root-cause statement
