"""Atomic, corruption-resistant board.json persistence.

Two hardening changes to :func:`hermes_cli.kanban_db.write_board_metadata`:

1. Writes go through a temp-file + fsync + ``os.replace`` sequence
   (:func:`_atomic_write_board_metadata`) instead of a bare
   ``Path.write_text``, so a process killed mid-write can never leave a
   truncated/partial ``board.json`` that :func:`read_board_metadata` would
   silently treat as malformed and replace with all-``True`` defaults
   (re-enabling dispatch/decompose/review).
2. The read-modify-write cycle is guarded by a bounded, best-effort
   cross-process advisory lock (:func:`_board_metadata_write_lock`) so two
   concurrent writers toggling different fields don't lose one write to a
   stale read.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return home


def test_write_never_leaves_a_truncated_file_on_a_simulated_crash(kanban_home, monkeypatch):
    """A crash after the temp file is written but before os.replace() must
    leave the ORIGINAL board.json fully intact, never a half-written one."""
    kb.write_board_metadata("default", dispatch_enabled=True)
    path = kb.board_metadata_path("default")
    original_bytes = path.read_bytes()

    real_replace = kb.os.replace

    def boom(*a, **kw):
        raise OSError("simulated crash between fsync and replace")

    monkeypatch.setattr(kb.os, "replace", boom)
    with pytest.raises(OSError):
        kb.write_board_metadata("default", dispatch_enabled=False)
    monkeypatch.setattr(kb.os, "replace", real_replace)

    # The original file must be byte-identical — no partial write landed.
    assert path.read_bytes() == original_bytes
    meta = kb.read_board_metadata("default")
    assert meta["dispatch_enabled"] is True

    # No leftover temp file.
    leftovers = list(path.parent.glob(".board.json.tmp-*"))
    assert leftovers == []


def test_written_file_is_valid_json_and_not_appended_or_duplicated(kanban_home):
    kb.write_board_metadata("default", dispatch_enabled=False)
    kb.write_board_metadata("default", auto_decompose_enabled=False)

    path = kb.board_metadata_path("default")
    raw = path.read_text(encoding="utf-8")
    # A non-atomic write_text that raced with a reader could produce
    # doubled/concatenated JSON; a single json.loads must succeed cleanly.
    parsed = json.loads(raw)
    assert parsed["dispatch_enabled"] is False
    assert parsed["auto_decompose_enabled"] is False


def test_concurrent_writers_toggling_different_fields_both_land(kanban_home):
    """Two threads (simulating two processes racing the same board.json)
    each set a DIFFERENT field. Both writes must be observable afterward —
    the advisory lock must prevent one read-modify-write from silently
    clobbering the other's change."""
    kb.write_board_metadata("default")  # seed board.json so both threads read-modify-write

    errors: list[BaseException] = []

    def set_dispatch():
        try:
            for _ in range(10):
                kb.write_board_metadata("default", dispatch_enabled=False)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def set_decompose():
        try:
            for _ in range(10):
                kb.write_board_metadata("default", auto_decompose_enabled=False)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    t1 = threading.Thread(target=set_dispatch)
    t2 = threading.Thread(target=set_decompose)
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert not errors
    meta = kb.read_board_metadata("default")
    # Both fields must reflect their setter's final intent — neither thread's
    # last write may have been silently dropped by the other's stale read.
    assert meta["dispatch_enabled"] is False
    assert meta["auto_decompose_enabled"] is False


def test_lock_is_best_effort_write_proceeds_if_lock_unavailable(kanban_home, monkeypatch):
    """If the advisory lock can never be acquired (simulated permanently-held
    lock), the write must still proceed rather than hang the caller forever."""
    monkeypatch.setattr(kb, "_kb_try_lock_nb", lambda handle: False)
    # Should not raise or hang (bounded by the lock's own deadline).
    result = kb.write_board_metadata("default", dispatch_enabled=False)
    assert result["dispatch_enabled"] is False
    assert kb.read_board_metadata("default")["dispatch_enabled"] is False
