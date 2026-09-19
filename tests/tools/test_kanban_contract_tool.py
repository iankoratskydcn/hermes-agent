"""Contract coverage for the service-gated run_contract_tests tool."""
from __future__ import annotations

import json


def test_run_contract_tests_is_gated_and_uses_frozen_task_obligations(monkeypatch, tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    from pathlib import Path
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kbc.connect()
    try:
        task_id = kb.create_task(
            conn, title="contract worker", assignee="test-worker",
            role="junior-dev", card_class="single_blind",
            obligations=["REQ-A"], feedback_schema="matrix",
        )
        kb.claim_task(conn, task_id)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(tmp_path))

    from tools import kanban_tools as kt
    assert kt._check_contract_tests() is True

    monkeypatch.setattr(
        "hermes_cli.kanban_harness.run_harness",
        lambda task_id, snapshot, obligations: {"results": {obligations[0]: "PASS"}},
    )
    assert json.loads(kt._handle_run_contract_tests({})) == {"results": {"REQ-A": "PASS"}}

    monkeypatch.delenv("HERMES_KANBAN_TASK")
    assert kt._check_contract_tests() is False
