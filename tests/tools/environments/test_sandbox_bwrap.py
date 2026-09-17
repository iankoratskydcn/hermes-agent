from pathlib import Path
from unittest.mock import Mock

import pytest

from tools.environments import sandbox_bwrap


def test_bwrap_argv_has_isolation_and_mount_classes(tmp_path):
    ro = tmp_path / "ro"
    rw = tmp_path / "rw"
    ro.mkdir(); rw.mkdir()
    argv = sandbox_bwrap.build_bwrap_argv(
        read_paths=[str(ro)], write_paths=[str(rw)], toolchain_ro_dirs=[],
        network=False, cmd=["/bin/bash", "-c", "true"])
    assert "--unshare-all" in argv
    assert "--unshare-user" in argv
    assert "--disable-userns" in argv
    assert "--die-with-parent" in argv
    assert "--unshare-net" in argv
    assert ["--ro-bind", str(ro), str(ro)] == argv[argv.index("--ro-bind"):argv.index("--ro-bind") + 3]
    assert ["--bind", str(rw), str(rw)] == argv[argv.index("--bind"):argv.index("--bind") + 3]


def test_landlock_abi_gate(monkeypatch):
    monkeypatch.setattr(sandbox_bwrap, "landlock_abi", lambda: 1)
    assert not sandbox_bwrap.landlock_available()
    with pytest.raises(sandbox_bwrap.SandboxUnavailable, match="minimum ABI"):
        sandbox_bwrap.apply_landlock(ro_paths=[], rw_paths=[])


def test_landlock_wrapper_executes_command(monkeypatch, tmp_path):
    monkeypatch.setattr(sandbox_bwrap, "landlock_abi", lambda: 2)
    command = sandbox_bwrap.build_bwrap_argv(
        read_paths=["/usr", str(Path.cwd())], write_paths=[str(tmp_path)],
        toolchain_ro_dirs=[], network=False, cmd=["/usr/bin/printf", "OK"])
    argv = sandbox_bwrap.build_landlock_wrapper_argv(
        ro_paths=["/usr", str(Path.cwd())],
        rw_paths=[str(tmp_path)], cmd=command)
    separator = argv.index("--")
    assert argv[separator + 1].endswith("python3.11")
    assert argv[-2:] == ["/usr/bin/printf", "OK"]


def test_require_fails_closed_without_bwrap(monkeypatch):
    monkeypatch.setattr(sandbox_bwrap, "bwrap_available", lambda: False)
    with pytest.raises(sandbox_bwrap.SandboxUnavailable, match="bubblewrap"):
        sandbox_bwrap.require_capabilities()


def test_local_off_routes_original_args(monkeypatch, tmp_path):
    from tools.environments.local import LocalEnvironment
    env = object.__new__(LocalEnvironment)
    env.cwd = str(tmp_path)
    monkeypatch.setattr(env, "_sandbox_mode", lambda: "off")
    args = ["bash", "-c", "true"]
    assert env._sandbox_command(args) is args


def test_local_require_fails_closed_on_windows(monkeypatch, tmp_path):
    from tools.environments import local
    env = object.__new__(local.LocalEnvironment)
    env.cwd = str(tmp_path)
    monkeypatch.setattr(env, "_sandbox_mode", lambda: "require")
    monkeypatch.setattr(local, "_IS_WINDOWS", True)
    with pytest.raises(sandbox_bwrap.SandboxUnavailable, match="Windows"):
        env._sandbox_command(["bash", "-c", "true"])


def test_local_require_executes_through_bwrap_after_capability_gate(monkeypatch, tmp_path):
    from tools.environments.local import LocalEnvironment
    env = object.__new__(LocalEnvironment)
    env.cwd = str(tmp_path)
    monkeypatch.setattr(env, "_sandbox_mode", lambda: "require")
    monkeypatch.setattr(sandbox_bwrap, "require_capabilities", lambda: None)
    monkeypatch.setattr(sandbox_bwrap, "bwrap_available", lambda: True)
    monkeypatch.setattr(sandbox_bwrap, "build_bwrap_argv", lambda **kwargs: ["bwrap", "--", *kwargs["cmd"]])
    monkeypatch.setattr(sandbox_bwrap, "build_landlock_wrapper_argv", lambda **kwargs: kwargs["cmd"])
    args = ["bash", "-c", "true"]
    assert env._sandbox_command(args)[:2] == ["bwrap", "--"]


def test_local_available_keeps_best_effort_bwrap_behavior(monkeypatch, tmp_path):
    from tools.environments.local import LocalEnvironment
    env = object.__new__(LocalEnvironment)
    env.cwd = str(tmp_path)
    monkeypatch.setattr(env, "_sandbox_mode", lambda: "available")
    monkeypatch.setattr(sandbox_bwrap, "bwrap_available", lambda: False)
    args = ["bash", "-c", "true"]
    assert env._sandbox_command(args) is args

