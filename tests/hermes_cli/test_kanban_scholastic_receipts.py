from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli.kanban_scholastic_receipts import (
    RECEIPT_SCHEMA_VERSION,
    ReceiptConflictError,
    ReceiptError,
    get_scholastic_receipt,
    put_scholastic_receipt,
    receipt_hash,
)


def _conn(tmp_path):
    return kbc.connect(tmp_path / "kanban.db")


def _seed_task(conn, *, task_id="task-1", project_id="project-1", run_id="7"):
    conn.execute(
        "INSERT INTO tasks (id, title, status, created_at, workspace_kind, project_id, current_run_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (task_id, "receipt test", "running", 1, "scratch", project_id, int(run_id)),
    )


def _receipt():
    payload = {
        "identity": {"run_id": "7", "stage_id": 1, "attempt_id": "attempt-1"},
        "status": "ok",
        "wire_status": "ok",
        "operational_status": "SUCCEEDED",
        "domain_status": "ABSTAINED",
        "domain_validated": False,
        "input_hash": {"sha256": "a" * 64},
        "output_hash": {"sha256": "b" * 64},
        "context_hash": {"sha256": "c" * 64},
        "reason_code": "accepted",
        "started_at": "2026-09-27T00:00:00Z",
        "completed_at": "2026-09-27T00:00:01Z",
        "confidence": 1.0,
        "latency_ms": 1.0,
        "provenance": {"execution_path": "deterministic", "model_id": None, "model_digest": None, "model_version": None, "quantization": None, "server_version": None},
    }
    return {"receipt_schema_version": RECEIPT_SCHEMA_VERSION, "receipt": payload, "receipt_hash": receipt_hash(payload)}


def test_table_exists_on_fresh_board(tmp_path):
    conn = _conn(tmp_path)
    assert conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='scholastic_receipts'").fetchone()


def test_create_replay_conflict_and_restart(tmp_path):
    path = tmp_path / "kanban.db"
    conn = kbc.connect(path)
    _seed_task(conn)
    kwargs = dict(board="default", project_id="project-1", task_id="task-1", run_id="7", canonical_stage_id="1", attempt_id="attempt-1")
    first = put_scholastic_receipt(conn, receipt=_receipt(), **kwargs)
    assert first.state == "created"
    second = put_scholastic_receipt(conn, receipt=_receipt(), **kwargs)
    assert second.state == "replayed"
    changed = _receipt()
    changed["receipt"] = {**changed["receipt"], "status": "error", "reason_code": "adapter_error", "wire_status": "error", "operational_status": "ERROR"}
    changed["receipt_hash"] = receipt_hash(changed["receipt"])
    with pytest.raises(ReceiptConflictError):
        put_scholastic_receipt(conn, receipt=changed, **kwargs)
    conn.close()
    reopened = kbc.connect(path)
    assert get_scholastic_receipt(reopened, **kwargs) == _receipt()


def test_invalid_hash_scalar_and_scope_rejected(tmp_path):
    conn = _conn(tmp_path)
    _seed_task(conn)
    kwargs = dict(board="default", project_id="project-1", task_id="task-1", run_id="7", canonical_stage_id="1", attempt_id="attempt-1")
    bad = _receipt()
    bad["receipt_hash"] = "0" * 64
    with pytest.raises(ReceiptError):
        put_scholastic_receipt(conn, receipt=bad, **kwargs)
    with pytest.raises(ReceiptError):
        put_scholastic_receipt(conn, receipt=[], **kwargs)
    with pytest.raises(ReceiptError):
        put_scholastic_receipt(conn, receipt=_receipt(), **{**kwargs, "project_id": "other"})
