"""Canonical, redacted intervention events and fail-open producer seam."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ALLOWED_KINDS = frozenset(("model", "config", "provider"))
_REDACTED = "[REDACTED]"
_SECRET_KEY = re.compile(r"(?:api[_-]?key|access[_-]?token|auth(?:orization)?|password|secret|credential|private[_-]?key)", re.I)
_SECRET_VALUE = re.compile(r"(?:sk-[A-Za-z0-9_-]{12,}|Bearer\s+[A-Za-z0-9._~+/=-]{8,}|(?:token|secret|password)\s*[:=]\s*\S+)", re.I)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS intervention_events (
    event_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    before_json TEXT NOT NULL,
    after_json TEXT NOT NULL,
    source TEXT NOT NULL,
    actor TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    timestamp TEXT NOT NULL,
    created_ns INTEGER NOT NULL
)
"""


def _redact(value: Any, *, key: str = "") -> Any:
    if _SECRET_KEY.search(key):
        return _REDACTED
    if isinstance(value, dict):
        return {str(k): _redact(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, tuple):
        return [_redact(item) for item in value]
    if isinstance(value, str) and _SECRET_VALUE.search(value):
        return _REDACTED
    return value


def _timestamp(value: str | datetime | None) -> str:
    if value is None:
        return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError("timestamp must be ISO-8601") from exc
    else:
        raise ValueError("timestamp must be ISO-8601")
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _event_id(idempotency_key: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "hermes:intervention:" + idempotency_key))


class InterventionEventStore:
    """Small SQLite authority for canonical intervention events."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path, timeout=0.05) as conn:
            conn.execute(_SCHEMA)
            conn.commit()

    def append(self, event: dict[str, Any]) -> dict[str, Any]:
        with sqlite3.connect(self.path, timeout=0.05) as conn:
            existing = conn.execute(
                "SELECT event_id, kind, before_json, after_json, source, actor, idempotency_key, timestamp "
                "FROM intervention_events WHERE idempotency_key = ?", (event["idempotency_key"],)
            ).fetchone()
            if existing is not None:
                stored = self._row(existing)
                if any(stored[key] != event[key] for key in (
                    "event_id", "kind", "before", "after", "source", "actor", "idempotency_key"
                )):
                    raise ValueError("idempotency_key conflict")
                return stored
            conn.execute(
                "INSERT INTO intervention_events "
                "(event_id, kind, before_json, after_json, source, actor, idempotency_key, timestamp, created_ns) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (event["event_id"], event["kind"], json.dumps(event["before"], sort_keys=True),
                 json.dumps(event["after"], sort_keys=True), event["source"], event["actor"],
                 event["idempotency_key"], event["timestamp"], time.time_ns()),
            )
            conn.commit()
        return event

    def list_events(self) -> list[dict[str, Any]]:
        with sqlite3.connect(self.path, timeout=0.05) as conn:
            rows = conn.execute(
                "SELECT event_id, kind, before_json, after_json, source, actor, idempotency_key, timestamp "
                "FROM intervention_events ORDER BY created_ns"
            ).fetchall()
        return [self._row(row) for row in rows]

    @staticmethod
    def _row(row: tuple[Any, ...]) -> dict[str, Any]:
        return {
            "event_id": row[0], "kind": row[1], "before": json.loads(row[2]), "after": json.loads(row[3]),
            "source": row[4], "actor": row[5], "idempotency_key": row[6], "timestamp": row[7],
        }


def capture_intervention_event(
    store: InterventionEventStore,
    *, kind: str, before: Any, after: Any, source: str, actor: str,
    idempotency_key: str, timestamp: str | datetime | None = None,
) -> dict[str, Any] | None:
    """Build and persist one event; producer failures never escape into the caller."""
    try:
        if kind not in ALLOWED_KINDS:
            raise ValueError(f"unsupported intervention kind: {kind}")
        if not isinstance(source, str) or not source.strip() or not isinstance(actor, str) or not actor.strip():
            raise ValueError("source and actor are required")
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ValueError("idempotency_key is required")
        event = {
            "event_id": _event_id(idempotency_key), "kind": kind,
            "before": _redact(before), "after": _redact(after),
            "source": source.strip(), "actor": actor.strip(),
            "idempotency_key": idempotency_key.strip(), "timestamp": _timestamp(timestamp),
        }
        return store.append(event)
    except ValueError:
        raise
    except Exception:
        logger.debug("intervention event capture failed", exc_info=True)
        return None


def default_store() -> InterventionEventStore:
    from hermes_constants import get_hermes_home
    return InterventionEventStore(get_hermes_home() / "intervention_events.db")


def capture_model_intervention(*, before: dict[str, Any], after: dict[str, Any], idempotency_key: str) -> dict[str, Any] | None:
    return capture_intervention_event(
        default_store(), kind="model", before=before, after=after,
        source="model_switch", actor="system", idempotency_key=idempotency_key,
    )


def capture_config_intervention(*, before: Any, after: Any, source: str, actor: str, idempotency_key: str) -> dict[str, Any] | None:
    return capture_intervention_event(
        default_store(), kind="config", before=before, after=after,
        source=source, actor=actor, idempotency_key=idempotency_key,
    )


def capture_provider_intervention(*, before: Any, after: Any, source: str, actor: str, idempotency_key: str) -> dict[str, Any] | None:
    return capture_intervention_event(
        default_store(), kind="provider", before=before, after=after,
        source=source, actor=actor, idempotency_key=idempotency_key,
    )


__all__ = [
    "ALLOWED_KINDS", "InterventionEventStore", "capture_intervention_event", "capture_model_intervention",
    "capture_config_intervention", "capture_provider_intervention", "default_store",
]
