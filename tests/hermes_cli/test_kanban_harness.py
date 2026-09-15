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
