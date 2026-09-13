# Consolidated findings — DeletedWalGenerationError swarm (L01-L19)

## Convergent theory (supported by 8+ independent lanes)

**Root cause**: `hermes_state_registry.py`'s handle-dedup is PROCESS-LOCAL ONLY (plain
module-level dict + threading.Lock — L03, L04, L09 confirm no cross-process primitive).
Multiple independent OS processes legitimately open writer handles against the SAME
default `~/.hermes/state.db`:
  - hermes-gateway.service (long-lived)
  - hermes-dashboard.service (long-lived)
  - cron/scheduler.py subprocess — acquires+releases a registry handle PER JOB RUN,
    refcount hits 0 after each job -> real close() -> real WAL checkpoint -> SQLite
    unlinks -wal/-shm on that clean close (confirmed L05, cited in-code comment:
    "Every cron run_agent opens+closes a transient SessionDB, so a TRUNCATE here
    fires a full WAL reset many times/hour")
  - ad-hoc manual `hermes` CLI invocations (bare SessionDB(), never registry-routed —
    L09 confirms CLI's own long-lived handle + delegate_tool child handles bypass
    the registry entirely, documented bug class #98573)
  - tui_gateway's roster-poll aggregation loop (L10) reopens the DEFAULT profile's
    OWN state.db every ~5s via a raw SessionDB(read_only=True) that also bypasses
    the registry
  - L12 (critical): found an UNACCOUNTED orphan PID 1146711 that produced a retired-wal
    capture (trigger=halt) at 07:59:17, invisible to journalctl/systemd — proof there
    really are more writer processes in play than the tracked services alone.

**Mechanism**: any one of these processes' handles cleanly closing rotates the WAL
generation (SQLite's own documented close-time behavior: last-connection-close unlinks
-wal/-shm). Any OTHER process's long-lived handle (gateway/dashboard) still pointing at
the old generation trips the write-time guard (_wal_generation_was_lost /
_halt_if_db_generation_changed in hermes_state.py) on its next write and correctly
refuses — DeletedWalGenerationError. L13's repro confirms this does NOT happen from
pure in-process multi-handle churn alone (clean same-process close does not fool the
guard, per the already-fixed #105567); it requires a genuinely SEPARATE process/handle
rotating the generation, which is exactly what cron/ad-hoc-CLI/tui_gateway-roster-poll
provide.

**The guard itself is NOT a bug** — L16 confirms fail-closed manual-restart-required
behavior is intentional (prevents split-brain/wrong-page checkpointing, the original
#105670 corruption class); L11 confirms no open TODOs/known gaps in the guard code;
L14 confirms the guard correctly ignores the one legitimate same-connection WAL-rotating
operation (VACUUM+TRUNCATE, which re-adopts identity immediately after).

**Ruled out**: decision-hud plugin (L06, separate db), acp_adapter (L07, not running),
periodic auto-prune/vacuum timers (L08, startup-only, no timer), hermes doctor (L17,
manual-only), curator/background_review (L18, in-process threads with session_db=None),
multiplex-profiles contextvar race (L19, multiplex_profiles is actually OFF on this
host — premise unsupported), fd-limits/filesystem exotics (L15, plain ext4, fine).

## Minority / contributing-factor findings worth adversarial scrutiny
- L01: write-time guard lacks same-process-sibling exemption that open-time guard has
  (asymmetry) — is this exploitable independent of the multi-process theory?
- L02: gateway itself is clean/registry-routed — but is dashboard's per-request open/close
  (L04) also refcounting to zero and closing each time, same as cron?
- L12's orphan PID 1146711 — never identified. Could be a leaked/zombie previous gateway
  process, a stray hermes CLI, or something else entirely. This is the single most
  concrete, unexplained data point and deserves priority adversarial attention.
