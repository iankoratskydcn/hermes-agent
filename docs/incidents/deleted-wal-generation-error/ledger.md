# Swarm ledger — recurring DeletedWalGenerationError investigation
Created: 2026-09-13 ~08:05

## Acceptance question
Why does ~/.hermes/state.db keep re-entering the DeletedWalGenerationError lock
loop shortly after a clean restart of hermes-gateway/hermes-dashboard, with NO
operator interference this time (confirmed: the 07:48-08:00 recurrence happened
with zero raw sqlite3 calls from the operator side)? Find the actual root cause
with file:line evidence, and the smallest safe durable fix (config/operational
vs a real code bug needing a PR upstream).

## Non-goals
- Do not kill/restart live services (gateway, dashboard, desktop, cron) — read-only.
- Do not edit repo code without explicit owner sign-off — this is an audit, not a fix pass.
- Do not delete any more retired-wal/state.db files.

## Lanes (20, waves of 5)
| ID | Angle | Status |
|----|-------|--------|
| L01 | hermes_state_dbfile.py / hermes_state.py generation-guard trigger logic | pending |
| L02 | gateway/run.py SessionDB acquisition — handle count/reuse | pending |
| L03 | tui_gateway/*.py SessionDB usage/lifecycle | pending |
| L04 | hermes_cli/web_routers/*.py (dashboard) SessionDB usage | pending |
| L05 | cron/scheduler.py + cron/jobs.py SessionDB writer usage | pending |
| L06 | decision_hud_mcp.py plugin — does it touch state.db | pending |
| L07 | acp_adapter/session.py SessionDB usage | pending |
| L08 | sessions.auto_prune / maybe_auto_prune_and_vacuum startup rotation | pending |
| L09 | hermes_state registry per-path handle cache correctness | pending |
| L10 | multiplex/profiles path-resolution collision on default state.db | pending |
| L11 | git history / issues for DeletedWalGenerationError, retired-wal, #105670 | pending |
| L12 | inventory every live process resolving to this exact state.db path | pending |
| L13 | minimal repro: two long-lived writer handles in temp HERMES_HOME | pending |
| L14 | is this just normal WAL autocheckpoint misdetected as "deleted"? | pending |
| L15 | fd-limit / inode-reuse filesystem edge cases | pending |
| L16 | existing quarantine/self-heal paths — why didn't they engage | pending |
| L17 | hermes doctor's own competing handle + trigger schedule | pending |
| L18 | background_review/curator auto-spawn opening extra handles | pending |
| L19 | profile HERMES_HOME resolution bug cross-touching default state.db | pending |
| L20 | SYNTHESIS: most likely root cause + smallest safe fix, adversarial review of L01-19 | pending |

## Result artifacts
Each lane writes /home/ian-koratsky/.hermes/claude-code-swarm-wal/<ID>.json
