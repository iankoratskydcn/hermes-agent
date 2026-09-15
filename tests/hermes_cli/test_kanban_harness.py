from hermes_cli.kanban_harness import obligation_matrix, sanitize, run_harness


def test_obligation_matrix_ands_multiple_tests_and_fails_missing():
    report = {"tests": [
        {"req": ["REQ-A"], "outcome": "passed"},
        {"req": ["REQ-A"], "outcome": "failed", "longrepr": "CANARY"},
        {"req": ["REQ-EXTRA"], "outcome": "passed"},
    ]}
    assert obligation_matrix(["REQ-A", "REQ-B"], report) == {"REQ-A": "FAIL", "REQ-B": "FAIL"}


def test_sanitize_never_copies_test_details():
    result = sanitize({"tests": [{"req": "REQ-A", "outcome": "failed", "message": "CANARY"}]}, ["REQ-A"])
    assert result == {"results": {"REQ-A": "FAIL"}}
    assert "CANARY" not in str(result)


def test_run_harness_uses_injected_executor_and_returns_matrix(tmp_path):
    def executor(snapshot, report_path, **kwargs):
        assert snapshot == str(tmp_path)
        open(report_path, "w", encoding="utf-8").write('{"tests": [{"req": ["REQ-A"], "outcome": "passed"}]}')
    assert run_harness("task", str(tmp_path), ["REQ-A"], executor=executor) == {"results": {"REQ-A": "PASS"}}


def test_run_harness_canary_in_failed_assertion_never_reaches_tool_result(tmp_path):
    canary = "CONTRACT_ASSERTION_CANARY_DO_NOT_LEAK_7f4d"

    def executor(snapshot, report_path, **kwargs):
        assert snapshot == str(tmp_path)
        # This models the JSON report produced by the sandboxed test runner.  The
        # assertion is deliberately failed and the canary appears in several
        # report-only fields, including a nested exception chain.
        report = {
            "tests": [{
                "req": ["REQ-A"], "outcome": "failed",
                "longrepr": f"AssertionError: {canary}",
                "call": {"traceback": [{"message": canary}]},
            }],
            "stdout": canary,
            "logs": [{"message": canary}],
        }
        import json
        with open(report_path, "w", encoding="utf-8") as stream:
            json.dump(report, stream)

    result = run_harness("task", str(tmp_path), ["REQ-A"], executor=executor)
    assert result["results"]["REQ-A"] == "FAIL"
    assert canary not in str(result)
