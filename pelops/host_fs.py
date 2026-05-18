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
import shutil
import subprocess
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


def _validate(path: str, *, must_be_inside_root: bool = False) -> Path:
    """Return the resolved Path if `path` is allowed, else raise.

    Args:
        path: User-supplied path (absolute or `~`-prefixed).
        must_be_inside_root: When True, the path itself (not just its
            parent) must be under an allowed root. Used by `ls` and
            `read` where there is no "creating a new sibling" notion.
            Default False matches `write` semantics: the parent must
            be inside an allowed root so a new file inside it lands
            correctly.
    """
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
            "no host directories configured. Set ALITA_HOST_WRITE_DIRS "
            "or rely on the defaults (~/Desktop, ~/Documents, ~/Downloads -- "
            "at least one must exist)."
        )

    # `write` allows targeting a NEW file inside an allowed root, so we
    # check the parent. `read` / `ls` operate on an EXISTING entry, so
    # the entry itself must be under (or equal to) a root.
    check = resolved if must_be_inside_root else resolved.parent
    for root in roots:
        try:
            check.relative_to(root)
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
    # Capture the existing mode (if any) so we can restore it after the
    # atomic rename. `tempfile.mkstemp` creates the temp file at 0600;
    # without this, `os.replace` would SILENTLY downgrade the perms of
    # an existing 0644 file to 0600 on every overwrite -- bug Jay caught
    # 2026-05-18 with metricas-pelops.html landing at rw-------.
    existing_mode: int | None = None
    if resolved.exists() and resolved.is_file():
        existing_mode = resolved.stat().st_mode & 0o7777
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
        # Set the mode BEFORE rename so the destination never appears
        # at the more-restrictive 0600. New files default to 0644
        # (rw-r--r--), the conventional mode for user docs on macOS.
        os.chmod(tmp_path, existing_mode if existing_mode is not None else 0o644)
        os.replace(tmp_path, resolved)
        _log.info("host_fs: wrote %d chars to %s", len(content), resolved)
        return resolved
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


# Cap on the body returned by `read` -- the agent context cannot
# absorb arbitrarily large files cheaply. Past this, the response is
# truncated with a marker so the agent knows there is more.
_READ_BYTE_CAP = 200_000


def read(path: str) -> str:
    """Read `path` from the host filesystem.

    Args:
        path: Absolute path (or `~`-prefixed) of the file to read.

    Returns:
        File contents as UTF-8 (errors='replace'). If the file is
        larger than `_READ_BYTE_CAP`, the result is truncated and a
        single line appended marking the cut.

    Raises:
        HostFsError: path policy violation OR target is not a regular
            file (does not exist, is a directory, etc).
    """
    resolved = _validate(path, must_be_inside_root=True)
    if not resolved.exists():
        raise HostFsError(f"not found: {resolved}")
    if not resolved.is_file():
        raise HostFsError(f"not a regular file: {resolved}")
    raw = resolved.read_bytes()
    if len(raw) <= _READ_BYTE_CAP:
        return raw.decode("utf-8", errors="replace")
    head = raw[:_READ_BYTE_CAP].decode("utf-8", errors="replace")
    return (
        f"{head}\n\n... [truncated: showed first {_READ_BYTE_CAP} of "
        f"{len(raw)} bytes; re-read with a code_execute snippet if "
        f"you need a specific range]"
    )


def ls(path: str) -> list[dict]:
    """List the entries of a directory on the host filesystem.

    Args:
        path: Absolute path (or `~`-prefixed) of the directory.

    Returns:
        A list of dicts: `{name, type, size}`. `type` is one of
        `"file"`, `"dir"`, `"link"`, or `"other"`. `size` is bytes
        for regular files, `None` otherwise. Order: directories
        first, then files, both alphabetical.

    Raises:
        HostFsError: path policy violation OR target is not a directory.
    """
    resolved = _validate(path, must_be_inside_root=True)
    if not resolved.exists():
        raise HostFsError(f"not found: {resolved}")
    if not resolved.is_dir():
        raise HostFsError(f"not a directory: {resolved}")

    entries: list[dict] = []
    for child in resolved.iterdir():
        if child.is_symlink():
            kind = "link"
        elif child.is_dir():
            kind = "dir"
        elif child.is_file():
            kind = "file"
        else:
            kind = "other"
        size: int | None
        try:
            size = child.stat().st_size if kind == "file" else None
        except OSError:
            size = None
        entries.append({"name": child.name, "type": kind, "size": size})
    entries.sort(key=lambda e: (e["type"] != "dir", e["name"].lower()))
    return entries


# --------------------------------------------------------------------- #
# HTML -> PDF via headless Chrome
# --------------------------------------------------------------------- #

# Well-known macOS locations for Chrome-family browsers. We pick whichever
# exists. Linux paths (`google-chrome`, `chromium`, etc.) are appended via
# `shutil.which` so this works in a CI runner with chromium installed.
_CHROME_PATHS = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "/Applications/Arc.app/Contents/MacOS/Arc",
)

# Cap on PDF render time. A page with heavy CSS / external fonts can
# legitimately take ~10s; past 60s something is wrong (network fetch
# stuck, Chrome hung). The container does not have network access here
# so external resources will time out fast.
_PDF_RENDER_TIMEOUT = 60


def _find_chrome() -> str:
    """Locate a Chrome-family binary on the host, or raise."""
    for path in _CHROME_PATHS:
        if Path(path).is_file():
            return path
    for name in ("google-chrome", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            return found
    raise HostFsError(
        "no Chrome-family browser found. Install Google Chrome, or "
        "set up another path. Headless Chrome is the PDF backend."
    )


def export_pdf(html_path: str, output_path: str | None = None) -> Path:
    """Convert an HTML file on disk to a PDF, both inside the allowlist.

    Args:
        html_path: existing .html file under one of the allowed roots.
        output_path: where the PDF should land. Defaults to the same
            directory and stem as `html_path`, with `.pdf` extension.
            Must ALSO be under an allowed root.

    Returns:
        The resolved Path of the written PDF.

    Raises:
        HostFsError: input not found / not html / Chrome missing /
            output policy violation / render failure.
        OSError: if Chrome crashes mid-render.
    """
    src = _validate(html_path, must_be_inside_root=True)
    if not src.exists():
        raise HostFsError(f"source not found: {src}")
    if not src.is_file():
        raise HostFsError(f"source not a regular file: {src}")
    if src.suffix.lower() not in {".html", ".htm"}:
        # The agent should pass an HTML file. Forbidding other suffixes
        # avoids accidentally rendering a binary or a path that looks
        # like HTML but is not.
        raise HostFsError(f"source must be .html or .htm: got {src.suffix!r}")

    if output_path is None:
        dst = src.with_suffix(".pdf")
    else:
        dst = _validate(output_path)  # parent under allowlist
        if dst.suffix.lower() != ".pdf":
            raise HostFsError(f"output must end in .pdf: got {dst.suffix!r}")

    chrome = _find_chrome()
    # Build Chrome argv. `--headless=new` is the supported flag on
    # Chrome 109+ (the older `--headless` is deprecated). `--no-pdf-
    # header-footer` removes the auto-added "page N of M" footer that
    # would otherwise clutter the output. `--disable-gpu` is needed
    # on some macOS configs to keep headless from waiting for GPU.
    argv = [
        chrome,
        "--headless=new",
        "--disable-gpu",
        "--no-pdf-header-footer",
        f"--print-to-pdf={dst}",
        f"file://{src}",
    ]
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            timeout=_PDF_RENDER_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        raise HostFsError(
            f"Chrome did not finish within {_PDF_RENDER_TIMEOUT}s "
            f"(input: {src}). Likely a stuck external resource."
        ) from exc

    if proc.returncode != 0 or not dst.exists():
        stderr = proc.stderr.decode("utf-8", errors="replace")[:300]
        raise HostFsError(f"Chrome exited {proc.returncode} without producing {dst}: {stderr}")
    _log.info("host_fs: rendered %s -> %s (%d bytes)", src, dst, dst.stat().st_size)
    return dst
