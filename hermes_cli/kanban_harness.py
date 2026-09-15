"""Dispatcher-owned contract-test harness and obligation result reduction.

The harness delegates execution to the existing DockerEnvironment; it deliberately
never returns pytest output to the caller.  Only the sanitized obligation matrix is
allowed across the tool boundary.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Callable, Mapping


class HarnessError(RuntimeError):
    """A harness failure that must be reported without test output."""


def obligation_matrix(obligations: list[str], report: Mapping[str, Any]) -> dict[str, str]:
    """Map the card's frozen REQ-IDs to PASS/FAIL, ignoring unrecognized markers.

    Multiple tests for one requirement are ANDed. Missing requirements fail closed.
    """
    allowed = {str(value) for value in obligations}
    seen: dict[str, bool] = {req: False for req in allowed}
    failed: set[str] = set()
    for test in report.get("tests", []) if isinstance(report.get("tests", []), list) else []:
        if not isinstance(test, Mapping):
            continue
        reqs = test.get("req") or test.get("requirements") or test.get("markers") or []
        if isinstance(reqs, str):
            reqs = [reqs]
        outcome = str(test.get("outcome", test.get("status", ""))).upper()
        passed = outcome in {"PASS", "PASSED", "SUCCESS"}
        for req in reqs if isinstance(reqs, list) else []:
            req = str(req)
            if req in allowed:
                seen[req] = True
                if not passed:
                    failed.add(req)
    return {req: ("PASS" if seen[req] and req not in failed else "FAIL")
            for req in sorted(allowed)}


def sanitize(report: Mapping[str, Any], obligations: list[str], schema: str = "matrix") -> dict[str, Any]:
    """Return the sole dev-facing shape; never copy report text, traces, or logs."""
    if schema == "full":
        raise HarnessError("full feedback is not available for contract tests")
    if schema not in {"matrix", "obligations", ""}:
        raise HarnessError("unsupported feedback schema")
    return {"results": obligation_matrix(obligations, report)}


def _default_executor(snapshot_path: str, report_path: str, *, image: str, timeout: int) -> None:
    """Run pytest inside the already-hardened DockerEnvironment primitive."""
    from tools.environments.docker import DockerEnvironment
    env = DockerEnvironment(
        image=image, task_id="qa-harness", timeout=timeout, network=False,
        host_cwd=snapshot_path, auto_mount_cwd=True, persist_across_processes=False,
    )
    try:
        result = env.execute(
            "pytest --json-report --json-report-file=/tmp/contract-report.json",
            cwd="/workspace",
        )
        if result.get("returncode", 1) not in (0, 1):
            raise HarnessError("contract-test execution failed")
        # Read the report through the sandbox's existing execution channel.  Do not
        # expose result['output']; it may contain assertion messages.
        report_result = env.execute("cat /tmp/contract-report.json", cwd="/workspace")
        if report_result.get("returncode", 1) != 0:
            raise HarnessError("contract-test report unavailable")
        Path(report_path).write_text(report_result.get("output", ""), encoding="utf-8")
    finally:
        env.cleanup(force_remove=True)


def run_harness(task_id: str, snapshot_path: str, obligations: list[str], *,
                executor: Callable[..., None] | None = None, image: str | None = None,
                timeout: int = 300, schema: str = "matrix") -> dict[str, Any]:
    """Execute and reduce one isolated contract-test attempt.

    ``executor`` is an explicit seam for dispatcher tests; production uses the
    existing DockerEnvironment and adds no sandbox implementation.
    """
    del task_id
    if not isinstance(obligations, list) or any(not str(item).strip() for item in obligations):
        raise HarnessError("obligations must be a non-empty list of identifiers")
    with tempfile.TemporaryDirectory(prefix="hermes-contract-") as tmp:
        report_path = str(Path(tmp) / "report.json")
        runner = executor or _default_executor
        try:
            runner(snapshot_path, report_path, image=image or os.environ.get("HERMES_CONTRACT_TEST_IMAGE", "python:3.12-slim"), timeout=timeout)
            report = json.loads(Path(report_path).read_text(encoding="utf-8"))
        except HarnessError:
            raise
        except Exception as exc:
            raise HarnessError("contract-test harness failed") from exc
        if not isinstance(report, Mapping):
            raise HarnessError("contract-test report has invalid shape")
        return sanitize(report, [str(item) for item in obligations], schema)
