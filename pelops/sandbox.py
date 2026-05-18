"""Sandboxed code execution via Docker.

Pelops needs occasional code-execution capability (validate a change,
run a benchmark, smoke-test a snippet) without exposing the host
filesystem, network, or credentials. Each execution runs in an
ephemeral Docker container with the following isolation:

  * `--rm`             container destroyed on exit
  * `--network none`   no host network access
  * `--read-only`      root filesystem is read-only
  * `--tmpfs /tmp`     scratch space limited to a tmpfs
  * `--cpus`           CPU cap
  * `--memory`         memory cap (memory-swap matches -> no swap)
  * stripped env       no host credentials inherit

The agent calls this through the `code_execute` tool in
`pelops/tools.py`. Per the design doc
(`docs/design/sandbox-execution.md`), v1 starts with default-on
autonomy: no per-call permission prompt. The sandbox isolation is
the safety boundary. If you want a kill switch, set
`ALITA_SANDBOX_ENABLED=false` in the env.

Public surface:
    run_python(code: str, timeout: int = 30) -> dict
    run_bash(code: str, timeout: int = 30)   -> dict

Returns a dict: `{stdout, stderr, exit_code, duration_seconds, timed_out}`.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path

_log = logging.getLogger("pelops.sandbox")

# python:3.11-slim is small (~150MB), boots in ~1.5s on a warm Docker.
# Stdlib only by default -- the container has no network so it cannot
# pip install. If a use case needs extra libs, build a custom image and
# point DOCKER_IMAGE at it.
DOCKER_IMAGE_PYTHON = "python:3.11-slim"
DOCKER_IMAGE_BASH = "alpine:3.20"

# Maximum bytes of stdout/stderr we surface back to the caller. Even
# inside a sandbox, an output flood from `yes` would balloon the
# agent's context.
MAX_OUTPUT_BYTES = 8000

# Host-side hard cap on what we ever buffer in RAM per stream. The
# container has 1GB memory but `subprocess.Popen` with PIPE would let
# a `while True: print('A')` script dump GBs into the host before we
# truncate. The reader threads keep draining past this cap (so the
# producer never blocks on a full pipe) but discard the overflow.
_STREAM_BUFFER_CAP = MAX_OUTPUT_BYTES * 2  # 16KB per stream max in host RAM


class SandboxError(RuntimeError):
    """Raised when the sandbox cannot start (Docker missing, etc)."""


def _docker_available() -> bool:
    """Cheap check that the host has a usable docker CLI."""
    return shutil.which("docker") is not None


def _drain_capped(stream, buf: bytearray, cap: int) -> None:
    """Read from `stream` into `buf` until EOF, capped at `cap` bytes.

    Critically, this keeps reading even after the buffer fills -- the
    extra bytes are discarded. If we stopped reading, the producer
    would block when its pipe buffer fills (~64KB on Linux/macOS),
    which would then defeat the timeout (the process would wedge
    forever waiting for us to drain it).
    """
    try:
        while True:
            chunk = stream.read(8192)
            if not chunk:
                return
            room = cap - len(buf)
            if room > 0:
                buf.extend(chunk[:room])
    except (OSError, ValueError):
        # Stream closed under us (e.g. after docker kill). Bail.
        return


def _docker_daemon_running() -> bool:
    """Verify the docker daemon answers `docker info`.

    The CLI being installed is not enough -- Docker Desktop may be
    closed or the engine may be paused. Used by the integration-style
    test's skipif so we do not falsely fail when only the CLI is
    present. ~30ms when the daemon is up.
    """
    if not _docker_available():
        return False
    try:
        result = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            timeout=3,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return result.returncode == 0


def _run_in_container(
    image: str,
    interpreter: list[str],
    code: str,
    timeout: int,
) -> dict:
    """Run `code` through `interpreter` inside an ephemeral container.

    Shared implementation for `run_python` and `run_bash`. Mounts the
    code file read-only at `/script` inside the container; the
    interpreter argv ends with `/script`.

    The isolation flags are not configurable on purpose -- this is the
    safety boundary that justifies the agent being allowed to execute
    code without per-call authorization. If you find yourself wanting
    to relax one, add a separate code path with its own threat model
    review instead of widening this one.
    """
    if not _docker_available():
        raise SandboxError(
            "docker CLI not found on host; sandbox execution unavailable. "
            "Install Docker Desktop or set ALITA_SANDBOX_ENABLED=false."
        )
    # Capture the tempfile name BEFORE writing, so the outer cleanup
    # runs even if the write itself fails. Without this, an exception
    # during fd.write would leave the temp file on disk and surface a
    # NameError to the caller (host_path never bound).
    fd = tempfile.NamedTemporaryFile(mode="w", suffix=".src", delete=False, encoding="utf-8")
    host_path = fd.name

    # Name the container so we can `docker kill` it when the host-side
    # subprocess timeout fires (otherwise the container keeps running
    # past the timeout because subprocess.run only kills the docker
    # CLI client, not the daemon-side container).
    container_name = f"pelops-sbx-{uuid.uuid4().hex[:12]}"

    try:
        try:
            fd.write(code)
            fd.flush()
        finally:
            fd.close()

        docker_cmd = [
            "docker",
            "run",
            "--rm",
            "--name",
            container_name,
            # ---- isolation flags (do not relax without a threat-model
            # review; see the module docstring) -----------------------
            "--network",
            "none",
            "--read-only",
            "--tmpfs",
            "/tmp:size=100m,noexec,nosuid",
            "--cpus",
            "2",
            "--memory",
            "1g",
            "--memory-swap",
            "1g",  # equal to memory => no swap
            "--pids-limit",
            "256",  # fork-bomb cap; CPU/memory caps do not bound process count
            "--security-opt",
            "no-new-privileges",  # block setuid escalation inside container
            "--cap-drop",
            "ALL",  # script execution needs zero Linux capabilities
            "--user",
            "65534:65534",  # nobody:nogroup; script is :ro so readable as non-root
            # ---------------------------------------------------------
            "-v",
            f"{host_path}:/script:ro",
            image,
            *interpreter,
            "/script",
        ]

        # Use Popen + threaded streaming reads so we cap host RAM at
        # _STREAM_BUFFER_CAP per stream. `subprocess.run(capture_output=True)`
        # would buffer the ENTIRE container output in host memory before
        # our truncation runs -- a malicious `while True: print('A')`
        # could OOM the laptop within the timeout window.
        started = time.monotonic()
        out_buf = bytearray()
        err_buf = bytearray()
        proc = subprocess.Popen(
            docker_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        out_thread = threading.Thread(
            target=_drain_capped,
            args=(proc.stdout, out_buf, _STREAM_BUFFER_CAP),
            daemon=True,
        )
        err_thread = threading.Thread(
            target=_drain_capped,
            args=(proc.stderr, err_buf, _STREAM_BUFFER_CAP),
            daemon=True,
        )
        out_thread.start()
        err_thread.start()
        timed_out = False
        try:
            exit_code = proc.wait(timeout=timeout + 5)  # docker startup headroom
        except subprocess.TimeoutExpired:
            timed_out = True
            # The container is still running on the daemon. Kill it by
            # name, then collect the now-exiting Popen process.
            try:
                subprocess.run(
                    ["docker", "kill", container_name],
                    capture_output=True,
                    timeout=10,
                    check=False,
                )
            except Exception:
                _log.exception("failed to kill sandbox container %s", container_name)
            try:
                exit_code = proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                exit_code = -1
        finally:
            # Threads should exit promptly once the pipes close.
            out_thread.join(timeout=5)
            err_thread.join(timeout=5)
        duration = time.monotonic() - started

        return {
            "stdout": bytes(out_buf[:MAX_OUTPUT_BYTES]).decode("utf-8", errors="replace"),
            "stderr": bytes(err_buf[:MAX_OUTPUT_BYTES]).decode("utf-8", errors="replace"),
            "exit_code": -1 if timed_out else exit_code,
            "duration_seconds": round(duration, 2),
            "timed_out": timed_out,
        }
    finally:
        Path(host_path).unlink(missing_ok=True)


def run_python(code: str, timeout: int = 30) -> dict:
    """Execute Python code in a sandboxed container.

    Stdlib only -- the container has no network so `pip install` will
    not work. For now this is by design (security > capability). If
    you genuinely need third-party packages, build a custom image and
    swap DOCKER_IMAGE_PYTHON.
    """
    return _run_in_container(
        DOCKER_IMAGE_PYTHON,
        ["python"],
        code,
        timeout,
    )


def run_bash(code: str, timeout: int = 30) -> dict:
    """Execute bash code in a sandboxed Alpine container.

    Alpine is even smaller than python:3.11-slim and the bash use
    cases (file munging, jq, grep, awk) do not need Python. Note that
    Alpine ships with `sh` (BusyBox), not full bash; portable shell
    idioms only.
    """
    return _run_in_container(
        DOCKER_IMAGE_BASH,
        ["sh"],
        code,
        timeout,
    )
