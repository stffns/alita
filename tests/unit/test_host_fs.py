"""Tests for the allowlisted host-FS writer."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

from pelops import host_fs

_VSTASH_AVAILABLE = importlib.util.find_spec("vstash") is not None
_requires_vstash = pytest.mark.skipif(
    not _VSTASH_AVAILABLE,
    reason="vstash-local not installed (CI skip)",
)


@pytest.fixture
def sandbox_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point `host_write_dirs` at a single throwaway directory.

    Reaches into Settings.load via the env var (which the field reads
    via `alias`), and clears the lru_cache so the new value is honored.
    """
    monkeypatch.setenv("GROQ_API_KEY", "dummy")
    monkeypatch.setenv("ALITA_HOST_WRITE_DIRS", str(tmp_path))
    from pelops.config import get_settings

    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


# ---------- happy path -------------------------------------------------


def test_write_inside_allowed_root(sandbox_root: Path):
    target = sandbox_root / "hello.txt"
    resolved = host_fs.write(str(target), "world")
    assert resolved == target.resolve()
    assert target.read_text() == "world"


def test_write_creates_missing_subdirs(sandbox_root: Path):
    target = sandbox_root / "nested" / "deep" / "file.py"
    host_fs.write(str(target), "x = 1")
    assert target.read_text() == "x = 1"


def test_write_overwrites_existing_regular_file(sandbox_root: Path):
    target = sandbox_root / "log.txt"
    target.write_text("first")
    host_fs.write(str(target), "second")
    assert target.read_text() == "second"


def test_write_is_atomic_on_failure(sandbox_root: Path, monkeypatch: pytest.MonkeyPatch):
    """If os.replace fails the original file must NOT be touched."""
    target = sandbox_root / "atomic.txt"
    target.write_text("ORIGINAL")

    def boom(*args, **kwargs):
        raise OSError("replace failed")

    monkeypatch.setattr(host_fs.os, "replace", boom)
    with pytest.raises(OSError, match="replace failed"):
        host_fs.write(str(target), "NEW")
    # Original still intact.
    assert target.read_text() == "ORIGINAL"
    # And the tmp file is cleaned up.
    leftovers = list(sandbox_root.glob(".alita-write-*"))
    assert leftovers == [], f"tempfile leak: {leftovers}"


# ---------- safety: path policy ----------------------------------------


def test_reject_outside_allowed_root(sandbox_root: Path, tmp_path_factory):
    """A path under a different directory must be refused."""
    other_root = tmp_path_factory.mktemp("not-allowed")
    with pytest.raises(host_fs.HostFsError, match="not under any allowed root"):
        host_fs.write(str(other_root / "boom.txt"), "x")


def test_reject_traversal(sandbox_root: Path):
    sneaky = f"{sandbox_root}/../../etc/passwd"
    with pytest.raises(host_fs.HostFsError, match="traversal"):
        host_fs.write(sneaky, "x")


def test_reject_relative_without_tilde(sandbox_root: Path):
    with pytest.raises(host_fs.HostFsError, match="must be absolute"):
        host_fs.write("foo.txt", "x")


def test_reject_empty_path(sandbox_root: Path):
    with pytest.raises(host_fs.HostFsError, match="empty path"):
        host_fs.write("", "x")
    with pytest.raises(host_fs.HostFsError, match="empty path"):
        host_fs.write("   ", "x")


def test_reject_non_regular_target(sandbox_root: Path):
    """A symlink pre-existing at the target must not be silently overwritten.

    More importantly, if the symlink points outside the allowed root,
    `resolved` (which collapses symlinks) lands outside and the
    allowlist check rejects BEFORE we hit the regular-file check.
    """
    target = sandbox_root / "linky"
    elsewhere = sandbox_root.parent / "outside-target"
    elsewhere.write_text("not yours")
    os.symlink(elsewhere, target)
    with pytest.raises(host_fs.HostFsError):
        host_fs.write(str(target), "evil")
    # The link target was untouched.
    assert elsewhere.read_text() == "not yours"


def test_reject_when_no_dirs_configured(sandbox_root: Path, monkeypatch: pytest.MonkeyPatch):
    """An env that points only at non-existent dirs must refuse all writes."""
    monkeypatch.setenv("ALITA_HOST_WRITE_DIRS", str(sandbox_root / "does-not-exist"))
    from pelops.config import get_settings

    get_settings.cache_clear()
    try:
        with pytest.raises(host_fs.HostFsError, match="no host-write directories"):
            host_fs.write(str(sandbox_root / "x.txt"), "x")
    finally:
        get_settings.cache_clear()


def test_tilde_expansion(sandbox_root: Path, monkeypatch: pytest.MonkeyPatch):
    """`~/foo.txt` should expand under HOME and only land if HOME is allowed."""
    # Make HOME point at our sandbox so a `~/` path resolves under it.
    monkeypatch.setenv("HOME", str(sandbox_root))
    resolved = host_fs.write("~/tilde.txt", "tilde")
    assert resolved == (sandbox_root / "tilde.txt").resolve()
    assert (sandbox_root / "tilde.txt").read_text() == "tilde"


# ---------- tool wrapper -----------------------------------------------


@_requires_vstash
def test_host_write_file_tool_success(sandbox_root: Path):
    from pelops.tools import host_write_file

    target = sandbox_root / "ok.txt"
    out = host_write_file.invoke({"path": str(target), "content": "yo"})
    assert "Saved to" in out
    assert str(target) in out
    assert "(2 bytes)" in out
    assert target.read_text() == "yo"


@_requires_vstash
def test_host_write_file_tool_policy_refusal(sandbox_root: Path):
    from pelops.tools import host_write_file

    out = host_write_file.invoke({"path": "/etc/passwd", "content": "x"})
    assert out.startswith("Error:")
    assert "not under any allowed root" in out
