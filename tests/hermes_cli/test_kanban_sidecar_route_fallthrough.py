"""hermes_cli/kanban_sidecar_route.py: STEP 4 fallthrough usage-tagging.

Proof obligations: record_fallthrough is a no-op without a session_id; an unmarked
task tags "unmarked"; a task with a valid "sidecar:<op>" marker but an
ineligible/invalid payload tags "label_mismatch:<op>"; a task with a fully-eligible
marker+payload (classifier matched, meaning the caller only reaches this path because
the sidecar CALL itself failed/returned non-ok) tags "call_failed:<op>". Reuses the
EXISTING record_auxiliary_usage sink -- no new telemetry table.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from hermes_cli import kanban_sidecar_route as route


def _fake_modules(op_name: str, accepts: bool):
    op = SimpleNamespace(validator=lambda payload: accepts)
    registry = SimpleNamespace(operations={op_name: op})
    return {"registry": registry}


def test_no_session_id_is_noop():
    fake_db = MagicMock()
    task = SimpleNamespace(title="do the thing", body=None)
    route.record_fallthrough(task, None, session_db=fake_db)
    fake_db.record_auxiliary_usage.assert_not_called()


def test_unmarked_task_tags_unmarked():
    fake_db = MagicMock()
    task = SimpleNamespace(title="review the PR", body=None)
    route.record_fallthrough(task, "sess-1", session_db=fake_db)
    fake_db.record_auxiliary_usage.assert_called_once_with(
        "sess-1", task="sidecar_fallthrough:unmarked", api_call_count=1,
    )


def test_label_mismatch_tags_operation_name():
    fake_db = MagicMock()
    task = SimpleNamespace(title="sidecar:json_field_extract please", body="{not json")
    route.record_fallthrough(task, "sess-1", session_db=fake_db)
    fake_db.record_auxiliary_usage.assert_called_once_with(
        "sess-1", task="sidecar_fallthrough:label_mismatch:json_field_extract", api_call_count=1,
    )


def test_classifier_match_tags_call_failed():
    # Classifier matches (valid marker + schema-valid payload) -> the only reason
    # record_fallthrough is being called at all is that the sidecar dispatch itself
    # failed/returned non-ok, so this is a call_failed bucket, not a label mismatch.
    fake_db = MagicMock()
    payload = {"document": json.dumps({"a": 1}), "fields": ["a"]}
    task = SimpleNamespace(title="sidecar:json_field_extract", body=json.dumps(payload))
    with patch.object(route, "_load_sidecar_modules", return_value=_fake_modules("json_field_extract", True)):
        route.record_fallthrough(task, "sess-1", session_db=fake_db)
    fake_db.record_auxiliary_usage.assert_called_once_with(
        "sess-1", task="sidecar_fallthrough:call_failed:json_field_extract", api_call_count=1,
    )


def test_never_raises_on_broken_db():
    fake_db = MagicMock()
    fake_db.record_auxiliary_usage.side_effect = RuntimeError("boom")
    task = SimpleNamespace(title="anything", body=None)
    route.record_fallthrough(task, "sess-1", session_db=fake_db)  # must not raise
