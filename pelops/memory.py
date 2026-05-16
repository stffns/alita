"""vstash-backed memory accessor (singleton per process)."""

from __future__ import annotations

from functools import lru_cache

from vstash import Memory

from pelops.config import Settings


@lru_cache(maxsize=1)
def get_memory() -> Memory:
    s = Settings.load()
    return Memory(project=s.vstash_project, db=str(s.vstash_db))
