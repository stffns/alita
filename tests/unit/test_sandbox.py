"""Tests for the Docker-backed sandbox.

Hermetic by default: subprocess calls are patched so we never actually
shell out to docker. The single integration-style test is gated by a
pytest skip when docker is not available on the runner.
"""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from pelops import sandbox


def _fake_completed(stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0):
    m = MagicMock()
    m.stdout = stdout
    m.stderr = stderr
    m.returncode = returncode
    return m


def test_docker_available_true(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sandbox.shutil, "which", lambda _name: "/usr/local/bin/docker")
    assert sandbox._docker_available() is True


def test_docker_available_false(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sandbox.shutil, "which", lambda _name: None)
    assert sandbox._docker_available() is False


def test_run_python_raises_when_docker_missing(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sandbox.shutil, "which", lambda _name: None)
    with pytest.raises(sandbox.SandboxError):
        sandbox.run_python("print('hi')")


def test_run_python_happy_path(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sandbox.shutil, "which", lambda _name: "/usr/local/bin/docker")
    captured: dict = {}

    def fake_run(cmd, capture_output, timeout):
        captured["cmd"] = cmd
        captured["timeout"] = timeout
        return _fake_completed(stdout=b"hello\n", stderr=b"", returncode=0)

    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)
    result = sandbox.run_python("print('hello')", timeout=10)

    assert result["exit_code"] == 0
    assert result["stdout"].strip() == "hello"
    assert result["stderr"] == ""
    assert result["timed_out"] is False
    assert isinstance(result["duration_seconds"], float)
    # Timeout passed to subprocess.run is original + 5s overhead.
    assert captured["timeout"] == 15
    cmd = captured["cmd"]
    assert cmd[0] == "docker" and cmd[1] == "run"
    assert sandbox.DOCKER_IMAGE_PYTHON in cmd
    assert "python" in cmd
    # The mounted script must be read-only.
    mount_arg = cmd[cmd.index("-v") + 1]
    assert mount_arg.endswith(":/script:ro")


def test_isolation_flags_all_present(monkeypatch: pytest.MonkeyPatch):
    """Every isolation flag is the authorization-free model's safety net.

    If a refactor drops one, the threat model changes silently. Assert
    the WHOLE set in one place so 'oops I removed --cap-drop' fails CI.
    """
    monkeypatch.setattr(sandbox.shutil, "which", lambda _name: "/usr/local/bin/docker")
    captured: dict = {}

    def fake_run(cmd, capture_output, timeout):
        captured["cmd"] = cmd
        return _fake_completed(stdout=b"", stderr=b"", returncode=0)

    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)
    sandbox.run_python("pass")

    cmd = captured["cmd"]

    def _flag_value(name: str) -> str | None:
        try:
            return cmd[cmd.index(name) + 1]
        except ValueError:
            return None

    assert "--rm" in cmd
    assert _flag_value("--network") == "none"
    assert "--read-only" in cmd
    tmpfs = _flag_value("--tmpfs")
    assert tmpfs is not None and "noexec" in tmpfs and "nosuid" in tmpfs
    assert _flag_value("--cpus") == "2"
    assert _flag_value("--memory") == "1g"
    assert _flag_value("--memory-swap") == "1g"
    assert _flag_value("--pids-limit") == "256"
    assert _flag_value("--security-opt") == "no-new-privileges"
    assert _flag_value("--cap-drop") == "ALL"
    assert _flag_value("--user") == "65534:65534"
    name = _flag_value("--name")
    assert name is not None and name.startswith("pelops-sbx-")
    # Privileged-mode opt-in must never be present.
    assert "--privileged" not in cmd
    assert "--cap-add" not in cmd


def test_run_python_timeout_kills_container(monkeypatch: pytest.MonkeyPatch):
    """On host-side timeout the container MUST be killed.

    subprocess.run's timeout only kills the docker CLI client; without
    a follow-up `docker kill`, a `while True` would burn 2 CPUs and 1GB
    forever. Regression test for the security review on 2026-05-18.
    """
    monkeypatch.setattr(sandbox.shutil, "which", lambda _name: "/usr/local/bin/docker")
    calls: list[list[str]] = []

    def fake_run(cmd, capture_output, timeout, check=False):
        calls.append(list(cmd))
        # First call: the long-running container that times out.
        if "run" in cmd and "--rm" in cmd:
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout, output=b"partial", stderr=b"")
        # Second call: `docker kill <name>`.
        return _fake_completed(stdout=b"", stderr=b"", returncode=0)

    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)
    result = sandbox.run_python("while True: pass", timeout=1)

    assert result["timed_out"] is True
    assert result["exit_code"] == -1
    assert "partial" in result["stdout"]
    # The second subprocess call must be a `docker kill` targeting the
    # same container name we passed via `--name` in the first call.
    assert len(calls) == 2
    run_cmd, kill_cmd = calls
    container_name = run_cmd[run_cmd.index("--name") + 1]
    assert kill_cmd == ["docker", "kill", container_name]


def test_run_bash_uses_alpine(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sandbox.shutil, "which", lambda _name: "/usr/local/bin/docker")
    captured: dict = {}

    def fake_run(cmd, capture_output, timeout):
        captured["cmd"] = cmd
        return _fake_completed(stdout=b"ok", stderr=b"", returncode=0)

    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)
    sandbox.run_bash("echo ok")

    cmd = captured["cmd"]
    assert sandbox.DOCKER_IMAGE_BASH in cmd
    assert "sh" in cmd


def test_run_python_truncates_huge_output(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sandbox.shutil, "which", lambda _name: "/usr/local/bin/docker")
    big = b"x" * 50_000

    def fake_run(cmd, capture_output, timeout):
        return _fake_completed(stdout=big, stderr=b"", returncode=0)

    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)
    result = sandbox.run_python("print('x' * 50000)")
    assert len(result["stdout"]) == sandbox.MAX_OUTPUT_BYTES


# ---------- Tool wrapper -----------------------------------------------


def test_code_execute_tool_returns_summary(monkeypatch: pytest.MonkeyPatch):
    """The `code_execute` tool formats sandbox results into a summary."""
    from pelops.tools import code_execute

    fake_result = {
        "stdout": "hello\n",
        "stderr": "",
        "exit_code": 0,
        "duration_seconds": 1.23,
        "timed_out": False,
    }
    with patch("pelops.sandbox.run_python", return_value=fake_result) as mocked:
        # `code_execute` is a langchain tool; .invoke() runs the underlying func.
        out = code_execute.invoke({"code": "print('hello')"})
        mocked.assert_called_once()
    assert "exit=0" in out
    assert "duration=1.23s" in out
    assert "hello" in out


def test_code_execute_tool_respects_kill_switch(monkeypatch: pytest.MonkeyPatch):
    from pelops.config import get_settings
    from pelops.tools import code_execute

    monkeypatch.setattr(get_settings(), "sandbox_enabled", False)
    out = code_execute.invoke({"code": "print('blocked')"})
    assert "disabled" in out


def test_code_execute_tool_rejects_unknown_language(monkeypatch: pytest.MonkeyPatch):
    from pelops.tools import code_execute

    out = code_execute.invoke({"code": "x", "language": "ruby"})
    assert "unsupported" in out.lower()


# ---------- Integration (only when docker is available) ---------------


@pytest.mark.skipif(
    not sandbox._docker_daemon_running(),
    reason="docker daemon not reachable on this runner",
)
def test_integration_run_python_real_docker():
    """End-to-end smoke test against a real Docker daemon.

    Skipped on CI (no docker) but useful locally to catch image-tag drift,
    permission issues, or argument ordering bugs.
    """
    result = sandbox.run_python("print(2 + 2)", timeout=20)
    assert result["timed_out"] is False
    assert result["exit_code"] == 0
    assert result["stdout"].strip() == "4"


@pytest.mark.skipif(
    not sandbox._docker_daemon_running(),
    reason="docker daemon not reachable on this runner",
)
def test_integration_boundary_network_blocked():
    """Network must be unreachable from inside the container."""
    code = (
        "import socket\n"
        "try:\n"
        "    socket.gethostbyname('example.com')\n"
        "    print('LEAK')\n"
        "except Exception as e:\n"
        "    print('blocked')\n"
    )
    result = sandbox.run_python(code, timeout=20)
    assert result["exit_code"] == 0
    assert "blocked" in result["stdout"]
    assert "LEAK" not in result["stdout"]


@pytest.mark.skipif(
    not sandbox._docker_daemon_running(),
    reason="docker daemon not reachable on this runner",
)
def test_integration_boundary_root_fs_readonly():
    """Writing outside /tmp must fail (root FS is mounted read-only)."""
    code = (
        "try:\n"
        "    open('/escape.txt', 'w').write('owned')\n"
        "    print('LEAK')\n"
        "except OSError as e:\n"
        "    print('blocked')\n"
    )
    result = sandbox.run_python(code, timeout=20)
    assert result["exit_code"] == 0
    assert "blocked" in result["stdout"]
    assert "LEAK" not in result["stdout"]
