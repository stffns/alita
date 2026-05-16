"""`pelops doctor` -- operational health checks.

Quick sanity sweep when you want to know "is the whole pipeline alive?".
Each check is independent and prints a single line. Exits 0 if all green,
non-zero if any check failed.

Usage:
    python -m pelops.doctor
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from dataclasses import dataclass


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str = ""

    def render(self) -> str:
        mark = "OK " if self.ok else "FAIL"
        return f"[{mark}] {self.name:30s} {self.detail}"


def _check_settings_load() -> CheckResult:
    try:
        from pelops.config import Settings

        s = Settings.load()
        return CheckResult(
            "Settings load",
            ok=True,
            detail=(
                f"chat_model={s.chat_model} owner={s.owner} "
                f"topics={len(s.topics)} feeds={len(s.rss_feeds)}"
            ),
        )
    except Exception as exc:
        return CheckResult("Settings load", ok=False, detail=str(exc))


def _check_groq_reachable() -> CheckResult:
    try:
        import httpx

        from pelops.config import Settings

        s = Settings.load()
        r = httpx.get(
            "https://api.groq.com/openai/v1/models",
            headers={"Authorization": f"Bearer {s.groq_api_key.get_secret_value()}"},
            timeout=5.0,
        )
        if r.status_code == 200:
            n = len(r.json().get("data", []))
            return CheckResult("Groq API reachable", ok=True, detail=f"{n} models")
        return CheckResult("Groq API reachable", ok=False, detail=f"HTTP {r.status_code}")
    except Exception as exc:
        return CheckResult("Groq API reachable", ok=False, detail=str(exc))


def _check_openrouter_reachable() -> CheckResult:
    try:
        import httpx

        from pelops.config import Settings

        s = Settings.load()
        if not s.openrouter_api_key:
            return CheckResult(
                "OpenRouter API reachable",
                ok=True,
                detail="not configured (skip)",
            )
        r = httpx.get(
            "https://openrouter.ai/api/v1/models",
            headers={"Authorization": f"Bearer {s.openrouter_api_key.get_secret_value()}"},
            timeout=5.0,
        )
        if r.status_code == 200:
            n = len(r.json().get("data", []))
            return CheckResult("OpenRouter API reachable", ok=True, detail=f"{n} models")
        return CheckResult("OpenRouter API reachable", ok=False, detail=f"HTTP {r.status_code}")
    except Exception as exc:
        return CheckResult("OpenRouter API reachable", ok=False, detail=str(exc))


def _check_vstash_writable() -> CheckResult:
    try:
        from pelops.memory import get_memory

        mem = get_memory()
        stats = mem.stats()
        return CheckResult(
            "vstash readable",
            ok=True,
            detail=f"docs={getattr(stats, 'documents', '?')}",
        )
    except Exception as exc:
        return CheckResult("vstash readable", ok=False, detail=str(exc))


def _check_jobs_db() -> CheckResult:
    try:
        from pelops import jobs

        pending = jobs.list_pending()
        return CheckResult(
            "jobs.db",
            ok=True,
            detail=f"pending followups={len(pending)}",
        )
    except Exception as exc:
        return CheckResult("jobs.db", ok=False, detail=str(exc))


def _check_watchers_db() -> CheckResult:
    try:
        from pelops import watchers

        active = watchers.list_active()
        return CheckResult(
            "watchers",
            ok=True,
            detail=f"active={len(active)}",
        )
    except Exception as exc:
        return CheckResult("watchers", ok=False, detail=str(exc))


def _check_telegram_token() -> CheckResult:
    from pelops.config import Settings

    s = Settings.load()
    if not s.telegram_bot_token:
        return CheckResult("Telegram token", ok=True, detail="not configured (skip)")
    if not s.telegram_owner_chat_id:
        return CheckResult(
            "Telegram token",
            ok=False,
            detail="TELEGRAM_BOT_TOKEN set but TELEGRAM_OWNER_CHAT_ID missing",
        )
    try:
        import httpx

        token = s.telegram_bot_token.get_secret_value()
        r = httpx.get(f"https://api.telegram.org/bot{token}/getMe", timeout=5.0)
        if r.status_code == 200 and r.json().get("ok"):
            return CheckResult(
                "Telegram token",
                ok=True,
                detail=f"@{r.json()['result'].get('username')} -> chat {s.telegram_owner_chat_id}",
            )
        return CheckResult("Telegram token", ok=False, detail=f"HTTP {r.status_code}")
    except Exception as exc:
        return CheckResult("Telegram token", ok=False, detail=str(exc))


def _check_bot_process() -> CheckResult:
    try:
        import subprocess

        out = subprocess.run(
            ["pgrep", "-fl", "pelops.telegram_bot"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        lines = [
            line
            for line in out.stdout.splitlines()
            if "pelops.telegram_bot" in line and "pgrep" not in line
        ]
        if not lines:
            return CheckResult("Telegram bot process", ok=False, detail="not running")
        return CheckResult(
            "Telegram bot process",
            ok=True,
            detail=f"pid {lines[0].split()[0]}",
        )
    except Exception as exc:
        return CheckResult("Telegram bot process", ok=False, detail=str(exc))


CHECKS: list[Callable[[], CheckResult]] = [
    _check_settings_load,
    _check_groq_reachable,
    _check_openrouter_reachable,
    _check_vstash_writable,
    _check_jobs_db,
    _check_watchers_db,
    _check_telegram_token,
    _check_bot_process,
]


def run() -> int:
    """Run all checks, print results, return exit code."""
    results = []
    started = time.time()
    for fn in CHECKS:
        results.append(fn())
    elapsed_ms = int((time.time() - started) * 1000)

    for r in results:
        print(r.render())

    failed = [r for r in results if not r.ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed in {elapsed_ms}ms")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(run())
