"""Behavior contracts for the Decision HUD bridge revision guard."""

from __future__ import annotations

import json
import logging
import sqlite3
import sys
from pathlib import Path

import pytest

from hermes_cli.plugin_bridges import decision_hud as bridge


@pytest.fixture
def staged_plugin(tmp_path, monkeypatch):
    plugin = tmp_path / "db.py"
    record = tmp_path / "bridge-revision.json"
    monkeypatch.setattr(bridge, "_decision_hud_db_path", lambda: plugin)
    monkeypatch.setattr(bridge, "_revision_record_path", lambda: record)
    monkeypatch.delitem(sys.modules, bridge._MODULE_CACHE_KEY, raising=False)
    return plugin, record


def _write_plugin(path: Path, version: int, marker: str = "") -> None:
    database = path.parent / "queue.db"
    source = f'''\
import sqlite3

_DB = {str(database)!r}
_MARKER = {marker!r}


def db_path():
    return _DB


def connect():
    return sqlite3.connect(_DB)
'''
    path.write_text(source, encoding="utf-8")
    conn = sqlite3.connect(database)
    try:
        conn.execute(f"PRAGMA user_version = {version}")
        conn.commit()
    finally:
        conn.close()


def test_supported_schema_loads_and_records_first_hash(staged_plugin):
    plugin, record = staged_plugin
    _write_plugin(plugin, bridge._MIN_DECISION_HUD_SCHEMA_VERSION)

    module = bridge._load_decision_hud_db()

    assert module.connect().execute("PRAGMA user_version").fetchone()[0] == 6
    stored = json.loads(record.read_text(encoding="utf-8"))
    assert stored["path"] == str(plugin)
    assert len(stored["sha256"]) == 64


def test_schema_below_floor_is_rejected_and_not_cached(staged_plugin):
    plugin, _record = staged_plugin
    _write_plugin(plugin, bridge._MIN_DECISION_HUD_SCHEMA_VERSION - 1)

    with pytest.raises(ImportError, match=r"schema too old.*5.*requires >= 6"):
        bridge._load_decision_hud_db()

    assert bridge._MODULE_CACHE_KEY not in sys.modules


def test_hash_change_logs_but_does_not_block_reload(staged_plugin, caplog):
    plugin, record = staged_plugin
    _write_plugin(plugin, bridge._MIN_DECISION_HUD_SCHEMA_VERSION, marker="first")
    bridge._load_decision_hud_db()
    first_hash = json.loads(record.read_text(encoding="utf-8"))["sha256"]

    _write_plugin(plugin, bridge._MIN_DECISION_HUD_SCHEMA_VERSION, marker="second")
    for key in list(sys.modules):
        if key == bridge._MODULE_CACHE_KEY or key.startswith(f"{bridge._MODULE_CACHE_KEY}:"):
            sys.modules.pop(key, None)
    with caplog.at_level(logging.WARNING, logger=bridge.__name__):
        module = bridge._load_decision_hud_db()

    assert module is not None
    assert "source hash changed" in caplog.text
    assert json.loads(record.read_text(encoding="utf-8"))["sha256"] != first_hash


def test_schema_failure_is_folded_into_fail_closed_batch_check(staged_plugin):
    plugin, _record = staged_plugin
    _write_plugin(plugin, bridge._MIN_DECISION_HUD_SCHEMA_VERSION - 1)

    allowed, reason = bridge.check_batch_approval(project="p", batch_id="b")

    assert allowed is False
    assert "schema too old" in reason
