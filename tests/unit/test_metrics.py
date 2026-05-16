"""Metrics: pricing lookup, summary aggregation."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _use_tmp_db(tmp_db):
    yield


def test_cost_lookup_exact_match():
    from pelops.metrics import _cost

    # claude-sonnet-4.6 at $3 in / $15 out
    # 1M in tokens = $3, 1M out tokens = $15
    assert pytest.approx(_cost("anthropic/claude-sonnet-4.6", 1_000_000, 0)) == 3.0
    assert pytest.approx(_cost("anthropic/claude-sonnet-4.6", 0, 1_000_000)) == 15.0


def test_cost_lookup_strips_date_suffix():
    """OpenRouter often returns model names with snapshot dates like
    `deepseek/deepseek-v4-flash-20260423`. The pricing table only knows the
    bare name. The lookup should still resolve."""
    from pelops.metrics import _cost

    base = _cost("deepseek/deepseek-v4-flash", 1000, 1000)
    snapshot = _cost("deepseek/deepseek-v4-flash-20260423", 1000, 1000)
    assert base == snapshot
    assert base > 0


def test_cost_unknown_model_returns_zero():
    from pelops.metrics import _cost

    assert _cost("some-private-llm", 1000, 1000) == 0.0
    assert _cost(None, 1000, 1000) == 0.0


def test_record_and_summary_roundtrip():
    from pelops import metrics

    metrics.record_turn(
        model="anthropic/claude-sonnet-4.6",
        prompt_tokens=10_000,
        completion_tokens=200,
        duration_ms=1500,
        source="test",
    )
    metrics.record_tool("vstash_recall", 250, ok=True)
    metrics.record_tool("vstash_recall", 200, ok=True)
    metrics.record_tool("research", 5000, ok=False, error="boom")

    s = metrics.summary(hours=1)
    assert s["turns_total"] == 1
    assert s["in_tokens_total"] == 10_000
    assert s["out_tokens_total"] == 200
    # 10k in * 3.00/M + 200 out * 15/M = 0.030 + 0.003 = 0.033
    assert pytest.approx(s["cost_usd_total"], abs=0.001) == 0.033

    tool_names = {t["name"] for t in s["by_tool"]}
    assert tool_names == {"vstash_recall", "research"}
    recall = next(t for t in s["by_tool"] if t["name"] == "vstash_recall")
    assert recall["calls"] == 2
    assert recall["errors"] == 0
    research = next(t for t in s["by_tool"] if t["name"] == "research")
    assert research["errors"] == 1


def test_format_summary_renders_plain_text():
    from pelops import metrics

    metrics.record_turn("anthropic/claude-sonnet-4.6", 1000, 100, 500, "test")
    out = metrics.format_summary(metrics.summary(hours=1))
    assert "Metrics" in out
    assert "anthropic/claude-sonnet-4.6" in out
    assert "cost=$" in out
