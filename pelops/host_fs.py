"""Write files to Jay's real filesystem from the agent.

The deepagents file-system tools (`write_file`, `edit_file`) are sandboxed
under `PROJECT_ROOT` via `FilesystemBackend(virtual_mode=True)`. That is
the right default -- it stops an agent prompt-injection from clobbering
`~/.ssh/authorized_keys`. But it also means a request like "save this
HTML to my Desktop" silently lands at `stilt/Users/.../foo.html`, not
on the actual Desktop, which Jay never sees.

This module exposes a NARROW host-FS writer with a path allowlist.
Default allowed roots are `~/Desktop`, `~/Documents`, `~/Downloads`.
The list is env-configurable via `ALITA_HOST_WRITE_DIRS=dir1,dir2,...`.

Security model (paranoid by intention):

  - Paths are expanded (`~`) and resolved BEFORE the allowlist check.
  - `..` traversal is rejected up front.
  - After resolution, the path's parent MUST be inside an allowed root.
    `resolve()` collapses symlinks; if the user has put a symlink
    inside `~/Desktop` that points to `~/.ssh`, the resolved target
    falls outside the allowed root and the write is rejected.
  - Writes are atomic (tmp file + rename) so an interrupted write
    cannot leave a half-written file in place.
  - We REFUSE to overwrite anything that is not a regular file (e.g.
    a symlink, FIFO, device node).

This is a tool the agent can call freely. It does NOT need user
confirmation per call -- the allowlist is the safety boundary, same
philosophy as the Docker sandbox in `pelops/sandbox.py`.
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

from pelops.config import Settings

_log = logging.getLogger("pelops.host_fs")


class HostFsError(RuntimeError):
    """Raised when a host-FS write is refused (path policy, etc)."""


def _allowed_roots() -> list[Path]:
    """Resolved list of directories the agent may write under.

    Reads `ALITA_HOST_WRITE_DIRS` (CSV) from Settings. Each entry is
    expanded (`~`), made absolute, and resolved. Missing directories
    are dropped silently -- the user may not have a `~/Downloads`.
    """
    raw = Settings.load().host_write_dirs
    roots: list[Path] = []
    for entry in raw:
        p = Path(entry).expanduser()
        try:
            resolved = p.resolve(strict=False)
        except OSError:
            continue
        if resolved.exists() and resolved.is_dir():
            roots.append(resolved)
    return roots


def _validate(path: str) -> Path:
    """Return the resolved Path if writing `path` is allowed, else raise."""
    if not path or not path.strip():
        raise HostFsError("empty path")
    # Reject traversal tokens in the RAW input, before expansion.
    # `Path.resolve()` would collapse these but we want a clean
    # error message and zero ambiguity about user intent.
    if ".." in Path(path).parts:
        raise HostFsError(f"path traversal not allowed: {path!r}")

    requested = Path(path).expanduser()
    resolved = (
        requested.resolve(strict=False)
        if requested.is_absolute()
        # A relative path without `~` is ambiguous (CWD-dependent). The
        # agent should always give us an explicit location.
        else (None)
    )
    if resolved is None:
        raise HostFsError(
            f"path must be absolute or start with ~: {path!r}. "
            f"Allowed roots: {[str(r) for r in _allowed_roots()]}"
        )

    roots = _allowed_roots()
    if not roots:
        raise HostFsError(
            "no host-write directories configured. Set ALITA_HOST_WRITE_DIRS "
            "or rely on the defaults (~/Desktop, ~/Documents, ~/Downloads -- "
            "at least one must exist)."
        )

    parent = resolved.parent
    for root in roots:
        try:
            parent.relative_to(root)
            return resolved
        except ValueError:
            continue
    raise HostFsError(
        f"refused: {resolved} is not under any allowed root {[str(r) for r in roots]}"
    )


def write(path: str, content: str) -> Path:
    """Atomically write `content` to `path` on the host filesystem.

    Args:
        path: Absolute path (or `~`-prefixed) the user wants written.
        content: File body. UTF-8.

    Returns:
        The fully-resolved Path that was written.

    Raises:
        HostFsError: path is empty, traversal, relative without `~`,
            outside the allowlist, or the target exists but is not a
            regular file.
        OSError: the actual write failed (disk full, permission, etc).
    """
    resolved = _validate(path)
    if resolved.exists() and not resolved.is_file():
        raise HostFsError(
            f"refused to overwrite non-regular file at {resolved} (symlink, dir, or special node)."
        )
    resolved.parent.mkdir(parents=True, exist_ok=True)

    # Atomic write: write to a sibling tmp file, fsync, then rename.
    # If the process is killed mid-write the partial bytes live in the
    # tmp file and the destination is untouched.
    fd, tmp_path_str = tempfile.mkstemp(
        prefix=".alita-write-",
        suffix=resolved.suffix,
        dir=str(resolved.parent),
    )
    tmp_path = Path(tmp_path_str)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, resolved)
        _log.info("host_fs: wrote %d chars to %s", len(content), resolved)
        return resolved
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
