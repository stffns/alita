"""Tests for the Docker-backed sandbox.

Hermetic by default: `subprocess.Popen` (for the container) and
`subprocess.run` (for `docker kill` + `docker info`) are patched so we
never actually shell out to docker. The integration-style tests are
gated by a pytest skip when the docker daemon is not reachable.
"""

from __future__ import annotations

import io
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from pelops import sandbox


class _FakePopen:
    """Stand-in for `subprocess.Popen` used by `_run_in_container`.

    Models just enough of the real surface for our tests: `.stdout` and
    `.stderr` are file-like readers the drain threads consume, `.wait`
    returns or raises, `.kill` is a noop tracker.
    """

    def __init__(
        self,
        stdout: bytes = b"",
        stderr: bytes = b"",
        returncode: int = 0,
        wait_raises: type[BaseException] | None = None,
    ):
        self.stdout = io.BytesIO(stdout)
        self.stderr = io.BytesIO(stderr)
        self._returncode = returncode
        self._wait_raises = wait_raises
        self.killed = False

    def wait(self, timeout=None):
        if self._wait_raises is not None:
            exc_cls = self._wait_raises
            self._wait_raises = None  # only raise once
            raise exc_cls("popen", timeout)
        return self._returncode

    def kill(self):
        self.killed = True


def _patch_popen(monkeypatch, **popen_kwargs):
    """Patch `sandbox.subprocess.Popen` to return a `_FakePopen`.

    Returns a `captured` dict that records the cmd passed to Popen so
    tests can assert on the docker argv.
    """
    captured: dict = {}

    def factory(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        captured["popen_kwargs"] = kwargs
        return _FakePopen(**popen_kwargs)

    monkeypatch.setattr(sandbox.subprocess, "Popen", factory)
    return captured


def _patch_subprocess_run(monkeypatch):
    """Patch `subprocess.run` (used for `docker kill`) to record calls."""
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        m = MagicMock()
        m.stdout = b""
        m.stderr = b""
        m.returncode = 0
        return m

    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)
    return calls


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
    captured = _patch_popen(monkeypatch, stdout=b"hello\n", returncode=0)
    _patch_subprocess_run(monkeypatch)  # noop -- no kill expected

    result = sandbox.run_python("print('hello')", timeout=10)

    assert result["exit_code"] == 0
    assert result["stdout"].strip() == "hello"
    assert result["stderr"] == ""
    assert result["timed_out"] is False
    assert isinstance(result["duration_seconds"], float)
    cmd = captured["cmd"]
    assert cmd[0] == "docker" and cmd[1] == "run"
    assert sandbox.DOCKER_IMAGE_PYTHON in cmd
    assert "python" in cmd
    mount_arg = cmd[cmd.index("-v") + 1]
    assert mount_arg.endswith(":/script:ro")
    # Popen must use PIPE for both streams so the drain threads can cap them.
    assert captured["popen_kwargs"]["stdout"] == subprocess.PIPE
    assert captured["popen_kwargs"]["stderr"] == subprocess.PIPE


def test_isolation_flags_all_present(monkeypatch: pytest.MonkeyPatch):
    """Every isolation flag is the authorization-free model's safety net.

    If a refactor drops one, the threat model changes silently. Assert
    the WHOLE set in one place so 'oops I removed --cap-drop' fails CI.
    """
    monkeypatch.setattr(sandbox.shutil, "which", lambda _name: "/usr/local/bin/docker")
    captured = _patch_popen(monkeypatch, returncode=0)
    _patch_subprocess_run(monkeypatch)
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

    Popen.wait's timeout only signals back to us; the daemon-side
    container keeps running. Without `docker kill`, a `while True`
    would burn 2 CPUs and 1GB forever. Regression test for the security
    review on 2026-05-18.
    """
    monkeypatch.setattr(sandbox.shutil, "which", lambda _name: "/usr/local/bin/docker")
    captured = _patch_popen(
        monkeypatch,
        stdout=b"partial",
        wait_raises=subprocess.TimeoutExpired,
    )
    kill_calls = _patch_subprocess_run(monkeypatch)

    result = sandbox.run_python("while True: pass", timeout=1)

    assert result["timed_out"] is True
    assert result["exit_code"] == -1
    assert "partial" in result["stdout"]
    # `docker kill <name>` must target the named container we launched.
    container_name = captured["cmd"][captured["cmd"].index("--name") + 1]
    assert ["docker", "kill", container_name] in kill_calls


def test_run_bash_uses_alpine(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sandbox.shutil, "which", lambda _name: "/usr/local/bin/docker")
    captured = _patch_popen(monkeypatch, stdout=b"ok", returncode=0)
    _patch_subprocess_run(monkeypatch)
    sandbox.run_bash("echo ok")

    cmd = captured["cmd"]
    assert sandbox.DOCKER_IMAGE_BASH in cmd
    assert "sh" in cmd


def test_stream_buffer_caps_host_memory(monkeypatch: pytest.MonkeyPatch):
    """A flood of output must NOT balloon host RAM past _STREAM_BUFFER_CAP.

    Regression test for Gemini's HIGH-priority review on PR #19: with
    `subprocess.run(capture_output=True)` the entire container stdout
    was buffered before truncation. Now `_drain_capped` keeps a hard
    cap and discards overflow while still draining the pipe.
    """
    monkeypatch.setattr(sandbox.shutil, "which", lambda _name: "/usr/local/bin/docker")
    flood = b"A" * 1_000_000  # 1 MB
    _patch_popen(monkeypatch, stdout=flood, returncode=0)
    _patch_subprocess_run(monkeypatch)

    result = sandbox.run_python("print('A' * 1000000)")
    # Surfaced output is capped at MAX_OUTPUT_BYTES (final truncation).
    assert len(result["stdout"]) == sandbox.MAX_OUTPUT_BYTES
    # Sanity: drain function alone caps at _STREAM_BUFFER_CAP. Exercise
    # it directly with a 10MB BytesIO and confirm the host-side buffer
    # never grew past the cap.
    buf = bytearray()
    sandbox._drain_capped(io.BytesIO(b"B" * 10_000_000), buf, sandbox._STREAM_BUFFER_CAP)
    assert len(buf) == sandbox._STREAM_BUFFER_CAP


def test_tempfile_cleanup_on_write_failure(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """If fd.write fails the temp script must still be unlinked.

    Regression test for Gemini's MEDIUM review on PR #19: the original
    code captured `host_path = fd.name` AFTER the write, so a write
    failure left the file on disk AND raised NameError downstream.
    """
    monkeypatch.setattr(sandbox.shutil, "which", lambda _name: "/usr/local/bin/docker")
    # Steer NamedTemporaryFile into a directory we control so we can
    # list its contents after the failing call.
    monkeypatch.setattr(sandbox.tempfile, "tempdir", str(tmp_path))

    # Patch the file object to make fd.write blow up.
    real_named = sandbox.tempfile.NamedTemporaryFile

    def boom_factory(*args, **kwargs):
        kwargs["dir"] = str(tmp_path)
        fd = real_named(*args, **kwargs)
        original_write = fd.write

        def exploding_write(*a, **kw):
            original_write("partial")  # write something so the file exists
            raise OSError("disk on fire")

        fd.write = exploding_write
        return fd

    monkeypatch.setattr(sandbox.tempfile, "NamedTemporaryFile", boom_factory)

    with pytest.raises(OSError, match="disk on fire"):
        sandbox.run_python("print('hi')")

    # No `.src` file should remain in tmp_path after the failure.
    leftovers = list(tmp_path.glob("*.src"))
    assert leftovers == [], f"tempfile leak: {leftovers}"


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
