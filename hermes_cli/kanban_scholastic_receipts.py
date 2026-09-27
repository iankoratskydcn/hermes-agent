"""Board-scoped durable Scholastic receipt authority for Kanban."""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from hermes_cli import kanban_db
from hermes_cli.kanban_db_connect import write_txn

RECEIPT_SCHEMA_VERSION = "scholastic-receipt-v1"
_WRITE_STATES = frozenset(("created", "replayed"))
_REQUIRED_RECEIPT_KEYS = frozenset(("receipt_schema_version", "receipt", "receipt_hash"))
_REQUIRED_PAYLOAD_KEYS = frozenset((
    "identity", "input_hash", "output_hash", "context_hash", "status", "reason_code",
    "confidence", "started_at", "completed_at", "latency_ms", "wire_status",
    "operational_status", "domain_status", "domain_validated", "provenance",
))
_STATUS_REASONS = {
    "ok": {"accepted"},
    "escalate": {"unsupported", "invalid_input", "invalid_schema", "low_confidence", "policy_blocked"},
    "error": {"adapter_error"},
    "timeout": {"timeout"},
}
_OPERATIONAL_BY_WIRE = {"ok": "SUCCEEDED", "escalate": "ESCALATED", "error": "ERROR", "timeout": "TIMEOUT"}


class ReceiptError(ValueError):
    """Base class for malformed or unauthorized receipt operations."""


class ReceiptConflictError(ReceiptError):
    """Receipt identity already exists with a different canonical payload."""


class ReceiptUnavailableError(ReceiptError):
    """Receipt authority could not complete the requested operation."""


@dataclass(frozen=True)
class ReceiptWriteResult:
    state: str
    receipt: dict[str, Any]


def _reject_nonfinite(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        raise ReceiptError("receipt contains non-finite number")
    if isinstance(value, dict):
        return {str(k): _reject_nonfinite(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_reject_nonfinite(v) for v in value]
    if isinstance(value, tuple):
        return [_reject_nonfinite(v) for v in value]
    return value


def canonical_json(value: Mapping[str, Any]) -> str:
    if not isinstance(value, Mapping):
        raise ReceiptError("receipt must be an object")
    try:
        checked = _reject_nonfinite(dict(value))
        return json.dumps(checked, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ReceiptError(f"receipt is not canonical JSON: {exc}") from exc


def receipt_hash(receipt: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(receipt).encode("utf-8")).hexdigest()


def _validate_scope(board: str, project_id: str | None, task_id: str, run_id: str, canonical_stage_id: str, attempt_id: str) -> None:
    values = (board, task_id, run_id, canonical_stage_id, attempt_id)
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise ReceiptError("receipt scope fields must be non-empty strings")
    if project_id is not None and (not isinstance(project_id, str) or not project_id.strip()):
        raise ReceiptError("project_id must be a non-empty string or None")


def validate_receipt(receipt: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(receipt, Mapping):
        raise ReceiptError("receipt must be an object")
    data = dict(receipt)
    if set(data) != _REQUIRED_RECEIPT_KEYS:
        raise ReceiptError("receipt must contain exactly schema version, receipt, and receipt_hash")
    if data["receipt_schema_version"] != RECEIPT_SCHEMA_VERSION:
        raise ReceiptError("unsupported receipt schema version")
    if not isinstance(data["receipt"], Mapping):
        raise ReceiptError("receipt payload must be an object")
    payload = dict(_reject_nonfinite(data["receipt"]))
    if set(payload) != _REQUIRED_PAYLOAD_KEYS:
        raise ReceiptError("receipt payload fields are incomplete or unknown")
    if payload["status"] not in _STATUS_REASONS or payload["reason_code"] not in _STATUS_REASONS[payload["status"]]:
        raise ReceiptError("status and reason_code contradict")
    if payload["wire_status"] != payload["status"] or payload["operational_status"] != _OPERATIONAL_BY_WIRE[payload["status"]]:
        raise ReceiptError("status layers are malformed")
    provenance = payload["provenance"]
    if not isinstance(provenance, Mapping) or set(provenance) != {"execution_path", "model_id", "model_digest", "model_version", "quantization", "server_version"}:
        raise ReceiptError("provenance is malformed")
    expected = receipt_hash(payload)
    if data["receipt_hash"] != expected:
        raise ReceiptError("receipt_hash does not match canonical receipt payload")
    return {
        "receipt_schema_version": RECEIPT_SCHEMA_VERSION,
        "receipt": payload,
        "receipt_hash": expected,
    }


def _verify_owner(conn: sqlite3.Connection, *, project_id: str | None, task_id: str, run_id: str) -> None:
    row = conn.execute("SELECT project_id, current_run_id FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise ReceiptError("task does not exist")
    if row["project_id"] != project_id:
        raise ReceiptError("receipt project scope does not match task")
    # Existing Kanban runs are integer IDs. UUID run IDs remain valid for a
    # sidecar-only caller, but numeric IDs must fence to the active task run.
    if run_id.isdigit() and str(row["current_run_id"] or "") != run_id:
        raise ReceiptError("receipt run is not the task's current run")


def put_scholastic_receipt(conn: sqlite3.Connection, *, board: str, project_id: str | None,
                           task_id: str, run_id: str, canonical_stage_id: str,
                           attempt_id: str, receipt: Mapping[str, Any]) -> ReceiptWriteResult:
    _validate_scope(board, project_id, task_id, run_id, canonical_stage_id, attempt_id)
    validated = validate_receipt(receipt)
    key = (board, project_id, task_id, run_id, canonical_stage_id, attempt_id)
    payload_json = canonical_json(validated)
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    with write_txn(conn):
        _verify_owner(conn, project_id=project_id, task_id=task_id, run_id=run_id)
        try:
            conn.execute(
                """INSERT INTO scholastic_receipts
                (board, project_id, task_id, run_id, canonical_stage_id, attempt_id,
                 receipt_schema_version, receipt_json, receipt_hash, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (*key, RECEIPT_SCHEMA_VERSION, payload_json, validated["receipt_hash"], now),
            )
            state = "created"
        except sqlite3.IntegrityError:
            row = conn.execute(
                """SELECT receipt_json FROM scholastic_receipts
                   WHERE board = ? AND project_id IS ? AND task_id = ? AND run_id = ?
                     AND canonical_stage_id = ? AND attempt_id = ?""", key,
            ).fetchone()
            if row is None:
                raise ReceiptUnavailableError("receipt identity disappeared during write")
            stored = json.loads(row["receipt_json"])
            if canonical_json(stored) != payload_json:
                raise ReceiptConflictError("receipt identity already has a different payload")
            state = "replayed"
    stored = get_scholastic_receipt(conn, board=board, project_id=project_id, task_id=task_id,
                                    run_id=run_id, canonical_stage_id=canonical_stage_id, attempt_id=attempt_id)
    if stored is None or stored != validated:
        raise ReceiptUnavailableError("receipt read-back verification failed")
    return ReceiptWriteResult(state=state, receipt=stored)


def get_scholastic_receipt(conn: sqlite3.Connection, *, board: str, project_id: str | None,
                           task_id: str, run_id: str, canonical_stage_id: str,
                           attempt_id: str) -> dict[str, Any] | None:
    _validate_scope(board, project_id, task_id, run_id, canonical_stage_id, attempt_id)
    row = conn.execute(
        """SELECT receipt_json FROM scholastic_receipts
           WHERE board = ? AND project_id IS ? AND task_id = ? AND run_id = ?
             AND canonical_stage_id = ? AND attempt_id = ?""",
        (board, project_id, task_id, run_id, canonical_stage_id, attempt_id),
    ).fetchone()
    if row is None:
        return None
    return validate_receipt(json.loads(row["receipt_json"]))
