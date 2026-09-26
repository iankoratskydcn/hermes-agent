"""Canonical intervention event contract and producer capture."""

from datetime import datetime, timezone
from pathlib import Path

import pytest

from agent.intervention_events import (
    ALLOWED_KINDS,
    InterventionEventStore,
    capture_intervention_event,
)


def test_event_contract_preserves_before_after_identity_source_actor_and_redacts_secrets(tmp_path: Path):
    store = InterventionEventStore(tmp_path / "events.db")
    event = capture_intervention_event(
        store,
        kind="model",
        before={"model": "old", "api_key": "«redacted:sk-…»"},
        after={"model": "new", "token": "Bearer abc.def.secret"},
        source="model_switch",
        actor="user:ian",
        idempotency_key="switch-1",
        timestamp="2026-09-26T12:00:00Z",
    )

    assert event["before"] == {"model": "old", "api_key": "[REDACTED]"}
    assert event["after"] == {"model": "new", "token": "[REDACTED]"}
    assert event["source"] == "model_switch"
    assert event["actor"] == "user:ian"
    assert event["event_id"]
    assert event["timestamp"] == "2026-09-26T12:00:00Z"
    assert store.list_events() == [event]


def test_idempotency_replays_same_event_without_duplicate(tmp_path: Path):
    store = InterventionEventStore(tmp_path / "events.db")
    args = dict(kind="config", before={"x": 1}, after={"x": 2}, source="config", actor="system", idempotency_key="same")

    first = capture_intervention_event(store, **args)
    second = capture_intervention_event(store, **args)

    assert second == first
    assert len(store.list_events()) == 1


def test_idempotency_rejects_conflicting_payload(tmp_path: Path):
    store = InterventionEventStore(tmp_path / "events.db")
    args = dict(kind="config", before={"x": 1}, after={"x": 2}, source="config", actor="system", idempotency_key="same")

    capture_intervention_event(store, **args)
    with pytest.raises(ValueError, match="idempotency_key conflict"):
        capture_intervention_event(store, **{**args, "after": {"x": 3}})


def test_contract_rejects_unknown_kind_and_invalid_timestamp(tmp_path: Path):
    store = InterventionEventStore(tmp_path / "events.db")
    assert set(ALLOWED_KINDS) == {"model", "config", "provider"}
    with pytest.raises(ValueError):
        capture_intervention_event(store, kind="other", before={}, after={}, source="x", actor="y", idempotency_key="1")
    with pytest.raises(ValueError):
        capture_intervention_event(store, kind="model", before={}, after={}, source="x", actor="y", idempotency_key="2", timestamp="not-time")


def test_timestamp_is_normalized_to_utc(tmp_path: Path):
    store = InterventionEventStore(tmp_path / "events.db")
    event = capture_intervention_event(
        store, kind="provider", before={}, after={}, source="provider", actor="system", idempotency_key="3",
        timestamp=datetime(2026, 9, 26, 8, 0, tzinfo=timezone.utc),
    )
    assert event["timestamp"] == "2026-09-26T08:00:00Z"


def test_capture_failure_is_non_blocking():
    class BrokenStore:
        def append(self, _event):
            raise RuntimeError("disk down")

    assert capture_intervention_event(
        BrokenStore(), kind="model", before={}, after={}, source="x", actor="y", idempotency_key="4"
    ) is None
