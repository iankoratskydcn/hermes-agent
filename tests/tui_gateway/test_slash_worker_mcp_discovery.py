"""Integration coverage for profile-local MCP discovery in slash workers."""

from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import textwrap
import threading
import time

import pytest
import yaml

_mcp_server_mod = pytest.importorskip("mcp.server")

if not hasattr(_mcp_server_mod, "MCPServer"):
    # `mcp.server.MCPServer` replaced `mcp.server.fastmcp.FastMCP` in mcp 2.0.
    # Skip rather than fail on a FastMCP-era SDK: the probe below is written
    # against the 2.x API, and the pinned version provides it.
    pytest.skip(
        "profile-local MCP discovery probe requires mcp >= 2.0 (MCPServer)",
        allow_module_level=True,
    )




_MAX_STDERR_BYTES = 16_384
_MAX_STDERR_LINES = 64


def _read_bounded_stderr(stream, diagnostics: dict[str, object]) -> None:
    retained: list[str] = []
    retained_bytes = 0
    total_bytes = 0
    total_lines = 0
    truncated = False
    for line in stream:
        encoded = line.encode("utf-8", errors="replace")
        total_bytes += len(encoded)
        total_lines += 1
        if len(retained) >= _MAX_STDERR_LINES or retained_bytes >= _MAX_STDERR_BYTES:
            truncated = True
            continue
        available = _MAX_STDERR_BYTES - retained_bytes
        if len(encoded) > available:
            retained.append(encoded[:available].decode("utf-8", errors="replace"))
            retained_bytes += available
            truncated = True
        else:
            retained.append(line.rstrip("\n"))
            retained_bytes += len(encoded)
    diagnostics.update(
        stderr="\n".join(retained),
        stderr_bytes=total_bytes,
        stderr_lines=total_lines,
        stderr_truncated=truncated,
    )


def _format_timeout_diagnostics(
    diagnostics: dict[str, object], proc: subprocess.Popen[str], checkpoints: dict[str, float]
) -> str:
    elapsed = {name: round(value - checkpoints["popen"], 3) for name, value in checkpoints.items()}
    return (
        "slash worker produced no /tools response within 10 seconds; "
        f"pid={proc.pid} poll={proc.poll()} checkpoints={elapsed} "
        f"stderr_bytes={diagnostics.get('stderr_bytes', 0)} "
        f"stderr_lines={diagnostics.get('stderr_lines', 0)} "
        f"stderr_truncated={diagnostics.get('stderr_truncated', False)} "
        f"stderr={diagnostics.get('stderr', '')!r}"
    )


def test_profile_local_mcp_tool_is_visible_in_slash_worker(tmp_path):
    profile_home = tmp_path / "profile-home"
    profile_home.mkdir()
    marker = "profile-local-61922"
    server = tmp_path / "mcp_probe.py"
    server.write_text(
        textwrap.dedent(
            f"""
            from mcp.server import MCPServer

            mcp = MCPServer("profileprobe")

            @mcp.tool()
            def hermes_61922_profile_probe() -> str:
                return {marker!r}

            if __name__ == "__main__":
                mcp.run(transport="stdio")
            """
        ),
        encoding="utf-8",
    )
    (profile_home / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "mcp_servers": {
                    "profileprobe": {
                        "enabled": True,
                        "command": sys.executable,
                        "args": [str(server)],
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    env = os.environ.copy()
    for key in list(env):
        if key.endswith("_API_KEY") or key.endswith("_TOKEN"):
            env.pop(key)
    env["HERMES_HOME"] = str(profile_home)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
    env["HERMES_SLASH_WATCHDOG_GRACE_S"] = "0"
    env["HERMES_SLASH_WATCHDOG_POLL_S"] = "0.05"
    proc = subprocess.Popen(
        [
            sys.executable,
            "-u",
            "-m",
            "tui_gateway.slash_worker",
            "--session-key",
            "agent:main:tui:dm:mcp-profile-test",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        cwd=tmp_path,
    )
    checkpoints = {"popen": time.monotonic()}
    diagnostics: dict[str, object] = {}
    output: queue.Queue[str] = queue.Queue()
    stderr_thread: threading.Thread | None = None
    try:
        assert proc.stdin is not None
        assert proc.stdout is not None
        assert proc.stderr is not None
        stderr_thread = threading.Thread(
            target=_read_bounded_stderr,
            args=(proc.stderr, diagnostics),
            daemon=True,
        )
        stderr_thread.start()
        checkpoints["stderr_reader_start"] = time.monotonic()
        stdout = proc.stdout
        threading.Thread(
            target=lambda: output.put(stdout.readline()),
            daemon=True,
        ).start()
        checkpoints["stdout_reader_start"] = time.monotonic()
        proc.stdin.write(json.dumps({"id": 1, "command": "/tools"}) + "\n")
        proc.stdin.flush()
        checkpoints["request_flush"] = time.monotonic()
        try:
            line = output.get(timeout=10)
        except queue.Empty:
            checkpoints["timeout"] = time.monotonic()
            pytest.fail(_format_timeout_diagnostics(diagnostics, proc, checkpoints))
        response = json.loads(line)
        assert response["ok"] is True
        assert "mcp__profileprobe__hermes_61922_profile_probe" in response["output"]
    finally:
        checkpoints.setdefault("cleanup_start", time.monotonic())
        if proc.poll() is None:
            proc.terminate()
            checkpoints["terminate"] = time.monotonic()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                checkpoints["kill"] = time.monotonic()
                proc.wait(timeout=5)
        else:
            checkpoints.setdefault("terminate", time.monotonic())
        checkpoints["reap"] = time.monotonic()
        if stderr_thread is not None:
            stderr_thread.join(timeout=1)
