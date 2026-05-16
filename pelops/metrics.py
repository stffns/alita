"""Cheap, local metrics for Pelops.

Two tables in jobs.db:
  pelops_turns       one row per LLM call (chat completion)
  pelops_tool_calls  one row per tool invocation

The point: we want measurable answers to "what is Pelops costing me",
"which tool is slow", "how often does the agent really call research".
These metrics are local-only and zero-cost; no LangSmith required.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pelops.config import Settings

log = logging.getLogger("pelops.metrics")

# Approximate Groq pricing as of May 2026, per million tokens (USD).
# Tool fees (web search etc) are NOT included -- only token costs.
PRICING_PER_M = {
    # Groq
    "qwen/qwen3-32b": {"in": 0.29, "out": 0.59},
    "qwen-3-32b": {"in": 0.29, "out": 0.59},
    "llama-3.1-8b-instant": {"in": 0.05, "out": 0.08},
    "gpt-oss-20b": {"in": 0.10, "out": 0.50},
    "gpt-oss-120b": {"in": 0.15, "out": 0.75},
    "llama-3.3-70b-versatile": {"in": 0.59, "out": 0.79},
    "llama-3.3-70b": {"in": 0.59, "out": 0.79},
    "groq/compound": {"in": 0.15, "out": 0.75},
    "groq/compound-mini": {"in": 0.15, "out": 0.75},
    # OpenRouter
    "anthropic/claude-sonnet-4.6": {"in": 3.00, "out": 15.00},
    "anthropic/claude-opus-4.7": {"in": 5.00, "out": 25.00},
    "openai/gpt-5.4": {"in": 2.50, "out": 15.00},
    "google/gemini-3.1-pro-preview": {"in": 2.00, "out": 12.00},
    "deepseek/deepseek-v4-flash": {"in": 0.11, "out": 0.22},
    "deepseek/deepseek-v4-pro": {"in": 0.43, "out": 0.87},
    "deepseek/deepseek-v3.2": {"in": 0.25, "out": 0.38},
}


def _db_path() -> Path:
    s = Settings.load()
    return Path(s.vstash_db).parent / "jobs.db"


def _connect() -> sqlite3.Connection:
    from pelops.migrations import migrate

    p = _db_path()
    migrate(p)
    con = sqlite3.connect(str(p), isolation_level=None, timeout=10.0)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=5000")
    return con


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def record_turn(
    model: str | None,
    prompt_tokens: int,
    completion_tokens: int,
    duration_ms: int | None,
    source: str,
) -> None:
    try:
        with _connect() as con:
            con.execute(
                "INSERT INTO pelops_turns "
                "(timestamp, model, prompt_tokens, completion_tokens, duration_ms, source) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    _now_iso(),
                    model or "?",
                    prompt_tokens or 0,
                    completion_tokens or 0,
                    duration_ms,
                    source,
                ),
            )
    except Exception as exc:
        log.warning("record_turn failed: %s", exc)


def record_tool(name: str, duration_ms: int, ok: bool, error: str | None = None) -> None:
    try:
        with _connect() as con:
            con.execute(
                "INSERT INTO pelops_tool_calls "
                "(timestamp, name, duration_ms, ok, error) VALUES (?, ?, ?, ?, ?)",
                (_now_iso(), name, duration_ms, 1 if ok else 0, error),
            )
    except Exception as exc:
        log.warning("record_tool failed: %s", exc)


def _cost(model: str | None, prompt_tokens: int, completion_tokens: int) -> float:
    if not model:
        return 0.0
    # Strip provider snapshot/date suffixes like "-20260423" so pricing
    # lookups match. OpenRouter often returns model names with date stamps.
    base = model
    if ":" in base:
        base = base.split(":", 1)[-1]
    # Try exact, then strip trailing -YYYYMMDD, then prefix-match.
    p = PRICING_PER_M.get(base)
    if not p:
        import re

        stripped = re.sub(r"-(\d{8}|\d{4}-\d{2}-\d{2}|\d{4})$", "", base)
        p = PRICING_PER_M.get(stripped)
    if not p:
        for k, v in PRICING_PER_M.items():
            if base.startswith(k):
                p = v
                break
    if not p:
        return 0.0
    return prompt_tokens / 1_000_000 * p["in"] + completion_tokens / 1_000_000 * p["out"]


def summary(hours: int = 24) -> dict:
    cutoff = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()
    out: dict = {"hours": hours}
    with _connect() as con:
        con.row_factory = sqlite3.Row
        turns = con.execute(
            "SELECT model, sum(prompt_tokens) AS pin, sum(completion_tokens) AS pout, "
            "       count(*) AS n, avg(duration_ms) AS avg_ms "
            "FROM pelops_turns WHERE timestamp >= ? GROUP BY model",
            (cutoff,),
        ).fetchall()
        total_turns = 0
        total_in = 0
        total_out = 0
        total_cost = 0.0
        by_model = []
        for r in turns:
            cost = _cost(r["model"], r["pin"] or 0, r["pout"] or 0)
            by_model.append(
                {
                    "model": r["model"],
                    "calls": r["n"],
                    "in_tokens": r["pin"] or 0,
                    "out_tokens": r["pout"] or 0,
                    "cost_usd": round(cost, 4),
                    "avg_ms": int(r["avg_ms"] or 0),
                }
            )
            total_turns += r["n"]
            total_in += r["pin"] or 0
            total_out += r["pout"] or 0
            total_cost += cost
        out["turns_total"] = total_turns
        out["in_tokens_total"] = total_in
        out["out_tokens_total"] = total_out
        out["cost_usd_total"] = round(total_cost, 4)
        out["by_model"] = by_model

        tools = con.execute(
            "SELECT name, count(*) AS n, avg(duration_ms) AS avg_ms, "
            "       sum(CASE WHEN ok=0 THEN 1 ELSE 0 END) AS errs "
            "FROM pelops_tool_calls WHERE timestamp >= ? "
            "GROUP BY name ORDER BY n DESC",
            (cutoff,),
        ).fetchall()
        out["by_tool"] = [
            {
                "name": r["name"],
                "calls": r["n"],
                "avg_ms": int(r["avg_ms"] or 0),
                "errors": r["errs"] or 0,
            }
            for r in tools
        ]
    return out


def format_summary(s: dict) -> str:
    """Pretty plain-text summary for chat output."""
    lines = [
        f"Metrics (last {s['hours']}h)",
        "",
        f"LLM turns: {s['turns_total']} | "
        f"tokens in: {s['in_tokens_total']:,} | "
        f"out: {s['out_tokens_total']:,} | "
        f"cost: ${s['cost_usd_total']:.4f}",
        "",
        "By model:",
    ]
    for m in s["by_model"]:
        lines.append(
            f"  {m['model']:30s} calls={m['calls']:4d} "
            f"in={m['in_tokens']:,} out={m['out_tokens']:,} "
            f"avg={m['avg_ms']}ms cost=${m['cost_usd']:.4f}"
        )
    lines.append("")
    lines.append("By tool:")
    for t in s["by_tool"]:
        err = f" (errors={t['errors']})" if t["errors"] else ""
        lines.append(f"  {t['name']:20s} calls={t['calls']:4d} avg={t['avg_ms']}ms{err}")
    return "\n".join(lines)
