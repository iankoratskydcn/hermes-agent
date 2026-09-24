# SSH terminal containment strategy

Status: design-only decision; the local prerequisites are now landed, but SSH implementation remains a separate follow-up (t_f3938e41).

## Decision

Do not claim that local bwrap/Landlock containment applies to `SSHEnvironment`. Those controls execute in the kernel and process namespace of the machine running them. Wrapping the local `ssh` client would contain only the client, not the remote shell. The remote command must be contained by a control installed and executed on the remote host, with remote paths and remote capability checks.

The first safe rung for the current product is fail-closed refusal: an isolated or projected task MUST NOT be dispatched to the SSH backend until a remote containment implementation exists and has been verified. The backend may continue to support ordinary, explicitly non-isolated SSH execution (`terminal.sandbox.mode: off`), but isolation metadata must not be silently ignored or downgraded to that path. The caller should receive a clear unsupported/containment-required error before command execution. This decision still applies after the local bwrap/Landlock implementation landed; the prerequisite removes the dependency block, not the remote trust-boundary gap.

This is deliberately narrower than attempting to retrofit a best-effort wrapper. SSH configuration such as `ForceCommand`, a remote chroot, a dedicated account, or operator-managed containers can provide useful deployment controls, but Hermes cannot assume or verify them from the current SSH API. They therefore cannot be treated as the task's isolation boundary.

## Current evidence

`tools/environments/ssh.py::_run_bash` currently builds `ssh ... bash -c ...` and starts it with `_popen_bash`; it performs no remote bwrap/Landlock probe, wrapper insertion, or isolation refusal. An isolated/projected dispatch reaching this backend therefore currently runs the remote shell without containment unless an upstream caller rejects it. This is a silent security downgrade if isolation is represented only in task metadata and not propagated to backend construction.

The authoritative Plan 04 proposal correctly notes that bwrap must be inserted inside the remote command and use remote path facts. Its proposed `mode: available` fallback is not acceptable for an isolated/projected task: that mode can proceed unsandboxed and must remain unavailable for required isolation.

## Future implementation contract

Implement the smallest explicit seam needed to carry a resolved isolation requirement into backend selection/construction. The local implementation now exposes `sandbox_bwrap.require_capabilities()`, `build_bwrap_argv(...)`, and `build_landlock_wrapper_argv(...)`; those are reusable policy/argv-building pieces only. They are not a remote helper: the wrapper embeds the local Python interpreter path and local absolute paths, and its Landlock syscalls affect only the process that executes it. For SSH:

1. If isolation is required and no approved remote containment helper is available, reject before spawning `ssh ... bash -c`.
2. If a remote containment helper is later approved, establish it on the remote host and probe its capability in the same SSH session/connection lifecycle. The probe must be remote (bwrap availability, Landlock ABI, helper identity/version, and required remote paths), cached only for that backend session, and failure must raise an `EnvironmentConnectionError` or equivalent containment-required error.
3. Wrap the remote command inside the remote shell, not the local SSH argv. Use only remote path facts and a validated read/write scope supplied by the projection layer; never derive remote scope from local filesystem paths.
4. A dropped connection, failed setup, missing helper, ABI mismatch, or wrapper exit before `exec` is an execution failure—not permission to retry unwrapped.
5. Keep ordinary non-isolated SSH behavior unchanged and separately documented. Do not advertise it as sandboxed.

The remote helper may eventually reuse the policy concepts and argv-building logic from `sandbox_bwrap.py`, but it cannot reuse local capability results, local absolute paths, the local Python interpreter, or the local Landlock process. A separate remote-agent/helper delivery mechanism is required; implementing one is the separately tracked follow-up t_f3938e41, not this design card.

## Guarantees and limitations

With this decision, isolated/projected SSH tasks have a fail-closed guarantee: they do not run when Hermes cannot establish a verified remote containment boundary. They do not yet have a positive SSH containment guarantee because the remote helper is not implemented. Local tasks using `terminal.sandbox.mode: require` now have a separately verified bwrap + in-process Landlock path; that proof is local-only and must not be cited as SSH evidence.

Local bwrap/Landlock guarantees do not transfer over SSH. SSH transport encryption/authentication protects the connection, not the remote process filesystem, network, or privilege boundary. Forced-command/chroot/dedicated-user controls remain operator deployment options and are non-authoritative unless a future implementation explicitly verifies and binds them to the task.

Non-goals: implementing remote bwrap installation, a compiled/native remote Landlock helper, SSH forced-command management, chroot provisioning, resource limits, or projection/scope resolution. Those require separate decisions and tests, including remote-host integration tests for happy path, missing capability, and mid-session failure.

Reference: `~/hermes-isolation-work/plans/04-bwrap-landlock-local-ssh.md`, Phase 4. This note intentionally supersedes its best-effort/remote-integration proposal for the interim: no isolated SSH execution until the remote boundary is real and fail-closed.
