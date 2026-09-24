"""hermes_cli/kanban_sidecar_route.py: STEP 4 label-based op mapping (Part C).

Proof obligations: correct "sidecar:<op>" title + schema-valid JSON body routes;
missing label, wrong label, malformed JSON body, and schema-invalid body all fall
through to None; a non-ok sidecar result (mocked) never marks a task done (covered
already for the remote/in-process split in test_kanban_sidecar_route_remote.py --
this file is about the classifier gate itself, generic across all 3 registered ops).
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

from hermes_cli import kanban_sidecar_route as route

_REGISTERED_OPS = ("json_field_extract", "code_symbol_extraction", "git_diff_summarization")

_VALID_BODIES = {
    "json_field_extract": {"document": json.dumps({"a": 1}), "fields": ["a"]},
    "code_symbol_extraction": {"language": "python", "source": "def f(): pass"},
    "git_diff_summarization": {"diff": "diff --git a/x b/x\n"},
}


class _FakeOperation(SimpleNamespace):
    """Stand-in for sidecar_suite.contract.Operation: only .validator matters here."""


def _fake_modules(op_name: str, accepts: bool):
    op = _FakeOperation(validator=lambda payload: accepts)
    registry = SimpleNamespace(operations={op_name: op})
    return {"registry": registry}


def test_all_three_ops_are_registered():
    assert set(_REGISTERED_OPS) <= set(route.SIDECAR_ELIGIBLE_OPERATIONS)


def test_missing_label_falls_through():
    for op_name in _REGISTERED_OPS:
        task = SimpleNamespace(title="do the thing", body=json.dumps(_VALID_BODIES[op_name]))
        with patch.object(route, "_load_sidecar_modules", return_value=_fake_modules(op_name, True)):
            assert route.SIDECAR_ELIGIBLE_OPERATIONS[op_name](task) is None


def test_wrong_label_falls_through():
    task = SimpleNamespace(title="sidecar:some_other_op", body=json.dumps(_VALID_BODIES["json_field_extract"]))
    with patch.object(route, "_load_sidecar_modules", return_value=_fake_modules("json_field_extract", True)):
        assert route.SIDECAR_ELIGIBLE_OPERATIONS["json_field_extract"](task) is None


def test_malformed_json_body_falls_through():
    for op_name in _REGISTERED_OPS:
        task = SimpleNamespace(title=f"sidecar:{op_name}", body="{not json")
        with patch.object(route, "_load_sidecar_modules", return_value=_fake_modules(op_name, True)):
            assert route.SIDECAR_ELIGIBLE_OPERATIONS[op_name](task) is None


def test_non_dict_json_body_falls_through():
    task = SimpleNamespace(title="sidecar:json_field_extract", body=json.dumps([1, 2, 3]))
    with patch.object(route, "_load_sidecar_modules", return_value=_fake_modules("json_field_extract", True)):
        assert route.SIDECAR_ELIGIBLE_OPERATIONS["json_field_extract"](task) is None


def test_schema_invalid_body_falls_through():
    for op_name in _REGISTERED_OPS:
        task = SimpleNamespace(title=f"sidecar:{op_name}", body=json.dumps({"totally": "wrong"}))
        with patch.object(route, "_load_sidecar_modules", return_value=_fake_modules(op_name, False)):
            assert route.SIDECAR_ELIGIBLE_OPERATIONS[op_name](task) is None


def test_registry_unreachable_falls_through():
    task = SimpleNamespace(title="sidecar:json_field_extract", body=json.dumps(_VALID_BODIES["json_field_extract"]))
    with patch.object(route, "_load_sidecar_modules", return_value=None):
        assert route.SIDECAR_ELIGIBLE_OPERATIONS["json_field_extract"](task) is None


def test_correct_label_and_valid_body_routes():
    for op_name in _REGISTERED_OPS:
        task = SimpleNamespace(title=f"sidecar:{op_name}", body=json.dumps(_VALID_BODIES[op_name]))
        with patch.object(route, "_load_sidecar_modules", return_value=_fake_modules(op_name, True)):
            result = route.SIDECAR_ELIGIBLE_OPERATIONS[op_name](task)
        assert result == _VALID_BODIES[op_name]
        # classify_task_for_sidecar (the public entry try_sidecar_route uses) agrees
        with patch.object(route, "_load_sidecar_modules", return_value=_fake_modules(op_name, True)):
            assert route.classify_task_for_sidecar(task) == (op_name, _VALID_BODIES[op_name])


def test_title_with_trailing_text_after_marker_still_routes():
    op_name = "json_field_extract"
    task = SimpleNamespace(title=f"sidecar:{op_name} (from ticket #42)", body=json.dumps(_VALID_BODIES[op_name]))
    with patch.object(route, "_load_sidecar_modules", return_value=_fake_modules(op_name, True)):
        assert route.SIDECAR_ELIGIBLE_OPERATIONS[op_name](task) == _VALID_BODIES[op_name]


def test_title_with_marker_as_substring_not_prefix_falls_through():
    # "sidecar:json_field_extraction_extra" must NOT match the "json_field_extract" marker
    op_name = "json_field_extract"
    task = SimpleNamespace(title=f"sidecar:{op_name}_extra", body=json.dumps(_VALID_BODIES[op_name]))
    with patch.object(route, "_load_sidecar_modules", return_value=_fake_modules(op_name, True)):
        assert route.SIDECAR_ELIGIBLE_OPERATIONS[op_name](task) is None


def test_non_ok_sidecar_result_never_marks_done_end_to_end():
    """End-to-end through try_sidecar_route: even with a matching label+valid body and a
    reachable backend, a non-'ok' sidecar result must fall through to None."""
    op_name = "json_field_extract"
    task = SimpleNamespace(title=f"sidecar:{op_name}", body=json.dumps(_VALID_BODIES[op_name]))
    with patch.object(route, "_load_sidecar_modules", return_value=_fake_modules(op_name, True)), \
         patch.object(route, "_sidecar_service_config", return_value={"url": ""}), \
         patch.object(route, "_try_in_process_route", return_value={"status": "escalate", "_backend": "in_process"}):
        assert route.try_sidecar_route(task) is None
