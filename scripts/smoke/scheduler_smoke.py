"""Run each scheduler job in sequence to validate the autonomy layer.

Faster than waiting for actual cron firings -- exercises the same code paths.
"""

from __future__ import annotations

import logging
import time

from pelops.memory import get_memory
from pelops.scheduler import job_briefing, job_consolidate, job_health, job_ingest

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("scheduler-smoke")


def docs_count() -> int:
    return len(list(get_memory().list()))


def banner(name: str) -> None:
    print(f"\n{'=' * 10} {name} {'=' * 10}")


def main() -> None:
    banner("Initial vstash state")
    before = docs_count()
    print(f"  docs in vstash: {before}")

    banner("job_health")
    t0 = time.time()
    job_health()
    print(f"  done in {time.time() - t0:.2f}s")

    banner("job_ingest")
    t0 = time.time()
    job_ingest()
    after_ingest = docs_count()
    print(f"  done in {time.time() - t0:.1f}s")
    print(f"  docs in vstash: {before} -> {after_ingest} (+{after_ingest - before})")

    banner("job_consolidate")
    t0 = time.time()
    job_consolidate()
    after_consolidate = docs_count()
    print(f"  done in {time.time() - t0:.1f}s")
    print(
        f"  docs in vstash: {after_ingest} -> {after_consolidate} "
        f"(+{after_consolidate - after_ingest})"
    )

    banner("job_briefing")
    t0 = time.time()
    job_briefing()
    after_briefing = docs_count()
    print(f"  done in {time.time() - t0:.1f}s")
    print(
        f"  docs in vstash: {after_consolidate} -> {after_briefing} "
        f"(+{after_briefing - after_consolidate})"
    )

    banner("Final state")
    print("All docs:")
    for d in get_memory().list():
        title = getattr(d, "title", "?")
        tags = getattr(d, "tags", None)
        print(f"  - {title!r} tags={tags}")


if __name__ == "__main__":
    main()
