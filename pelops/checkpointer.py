"""Dual-mode SQLite checkpointer for the Pelops agent.

LangGraph ships two checkpointers for SQLite:

  * `SqliteSaver` (sync): works with `agent.invoke()` (Telegram's path).
    Raises `NotImplementedError` on any async method, so it cannot be used
    with `agent.astream_events()` (Chainlit's path).

  * `AsyncSqliteSaver` (async): works with async paths but calls
    `asyncio.get_running_loop()` at construction time. We build the agent
    from a SYNC factory (`build_agent()` is `@lru_cache`'d), so this fails.

`DualModeSqliteSaver` is a `SqliteSaver` whose async methods delegate to
the sync implementations via `asyncio.to_thread`. That lets the SAME
checkpointer back BOTH transports without us touching the agent factory.

Yes, the async methods run on a thread pool. For our chat workload (a
few SQLite writes per turn, a few ms each), this is fine. If we ever
move to a workload where checkpoint I/O is the bottleneck, swap in
`AsyncSqliteSaver` once we have an async entry point.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from langgraph.checkpoint.base import (
    ChannelVersions,
    CheckpointMetadata,
    CheckpointTuple,
)
from langgraph.checkpoint.serde.types import TASKS
from langgraph.checkpoint.sqlite import SqliteSaver


class DualModeSqliteSaver(SqliteSaver):
    """A `SqliteSaver` with usable async methods.

    Each async method runs the corresponding sync method in a thread pool
    via `asyncio.to_thread`. `alist` is special: the sync `list` returns
    an iterator, so we materialize it into a thread and yield from the
    list -- this is fine because checkpoint histories are short.
    """

    async def aget_tuple(self, config: dict[str, Any]) -> CheckpointTuple | None:
        return await asyncio.to_thread(self.get_tuple, config)

    async def alist(
        self,
        config: dict[str, Any] | None,
        *,
        filter: dict[str, Any] | None = None,
        before: dict[str, Any] | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        # Materialize the sync iterator in a worker thread, then yield.
        def _collect():
            return list(self.list(config, filter=filter, before=before, limit=limit))

        rows = await asyncio.to_thread(_collect)
        for row in rows:
            yield row

    async def aput(
        self,
        config: dict[str, Any],
        checkpoint: dict[str, Any],
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(self.put, config, checkpoint, metadata, new_versions)

    async def aput_writes(
        self,
        config: dict[str, Any],
        writes: list[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        await asyncio.to_thread(self.put_writes, config, writes, task_id, task_path)

    # `get_next_version` is sync-only in both bases; LangGraph calls it
    # without an `a` prefix even from async paths.


def build(db_path: str | Path) -> DualModeSqliteSaver:
    """Construct a DualModeSqliteSaver pointing at `db_path`.

    Uses a single SQLite connection in WAL mode with `check_same_thread=False`
    because LangGraph may invoke us from worker threads (sync invoke) AND
    from the event loop (async astream_events). WAL gives us safe
    concurrent reads against the writer.
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    saver = DualModeSqliteSaver(conn)
    # Pre-create the checkpoint tables so the first call does not race.
    saver.setup()
    return saver


# Re-export for convenience; callers import a single name.
__all__ = ["TASKS", "DualModeSqliteSaver", "build"]
