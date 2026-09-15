import subprocess
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


def test_local_require_fails_closed_without_landlock_shim(monkeypatch, tmp_path):
    from tools.environments.local import LocalEnvironment
    env = object.__new__(LocalEnvironment)
    env.cwd = str(tmp_path)
    monkeypatch.setattr(env, "_sandbox_mode", lambda: "require")
    monkeypatch.setattr(sandbox_bwrap, "require_capabilities", lambda: None)
    with pytest.raises(sandbox_bwrap.SandboxUnavailable, match="exec shim"):
        env._sandbox_command(["bash", "-c", "true"])

