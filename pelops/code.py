"""Code search backed by Semble.

A thin wrapper around `semble.SembleIndex` so the agent can search a
repository with natural-language queries -- "where is the heartbeat
parser", "show me how vstash_recall handles errors" -- and get back
exact code snippets at ~98% fewer tokens than grep+read.

Indexes are cached per-repo for the lifetime of the process; a repo
change requires a restart. For Pelops's workload (one stilt repo,
maybe a couple of others) this is fine; we are not building a code
search service.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

_log = logging.getLogger("pelops.code")


@lru_cache(maxsize=8)
def _index_for(repo_path: str):
    """Cache-on-first-use: build a Semble index for a repo path.

    `lru_cache` keyed by string so repeated calls with the same path
    return the same index object. Limit 8 means up to 8 different
    repos can be indexed before the LRU starts evicting -- plenty
    for Pelops's use cases.
    """
    from semble import SembleIndex

    _log.info("code: building Semble index for %s", repo_path)
    return SembleIndex.from_path(repo_path)


def search(
    query: str,
    repo: str | None = None,
    top_k: int = 5,
) -> list[dict]:
    """Search code in a repository.

    Args:
        query: natural-language description of the code to find.
        repo: absolute path to the repo. Defaults to the directory
            containing `pelops/` (the stilt repo).
        top_k: max number of results.

    Returns:
        List of dicts: `{file, start_line, end_line, snippet}`.
        Empty list when the query has no relevant matches.
    """
    if repo is None:
        # Stilt repo root: two parents up from this file (pelops/code.py).
        repo = str(Path(__file__).resolve().parent.parent)
    index = _index_for(repo)
    raw = index.search(query, top_k=top_k)
    out: list[dict] = []
    for r in raw:
        chunk = getattr(r, "chunk", None)
        if chunk is None:
            continue
        # `or 0` guards against an attribute that exists but is None
        # (Semble's chunk fields are normally ints, but be defensive).
        out.append(
            {
                "file": str(getattr(chunk, "file_path", "?") or "?"),
                "start_line": int(getattr(chunk, "start_line", 0) or 0),
                "end_line": int(getattr(chunk, "end_line", 0) or 0),
                "snippet": str(getattr(chunk, "content", "") or "")[:2000],
            }
        )
    return out
