"""End-to-end test of `code_execute` from chat.

Three prompts in ascending difficulty exercise: (1) the tool is wired
into the agent's toolset and gets called at all, (2) the agent reads
its output back, and (3) the agent can write non-trivial code, run it,
and report the result.

Writes a markdown report to `scripts/experiments/runs/sandbox_e2e_<ts>.md`.
Run from the repo root with the venv active:

    python scripts/experiments/sandbox_e2e.py
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from pelops.agent import build_agent

PROMPTS = [
    {
        "id": "P1-trivial",
        "label": "Trivial: tool wired in",
        "ask": (
            "Usa code_execute para imprimir exactamente la cadena "
            "'PELOPS-SANDBOX-OK'. Reportame el output."
        ),
        # Pass criteria: code_execute called >=1 times AND PELOPS-SANDBOX-OK
        # appears in either the tool output or the final answer.
        "must_call": "code_execute",
        "must_contain": "PELOPS-SANDBOX-OK",
    },
    {
        "id": "P2-useful",
        "label": "Useful: reads tool output back",
        "ask": (
            "Necesito el sha256 hexdigest de la cadena 'pelops-2026-05-18'. "
            "Usa code_execute con Python, despues reportame el digest."
        ),
        # sha256('pelops-2026-05-18') =
        # 4a7e180233de41a280bec340a82c00355f6f2e7039ec0f8cf51a624d16005d30
        "must_call": "code_execute",
        "must_contain": "4a7e180233de41a280bec340a82c00355f6f2e7039ec0f8cf51a624d16005d30",
    },
    {
        "id": "P3-reasoned",
        "label": "Reasoned: writes code, runs, interprets",
        "ask": (
            "Cuantos numeros primos hay menores a 10000? Escribe un "
            "Python eficiente y corrirlo con code_execute. Reportame "
            "el conteo y cuanto tardo (decimas de segundo estan bien)."
        ),
        "must_call": "code_execute",
        # There are 1229 primes below 10000. Accept either spelling.
        "must_contain": "1229",
    },
]


def _serialize_message(m) -> dict:
    """Compact dict of a langchain message for the report."""
    base = {"type": m.__class__.__name__}
    content = getattr(m, "content", "")
    if isinstance(content, list):
        # Multi-modal blocks; flatten the text parts.
        content = " ".join(
            str(b.get("text", b)) if isinstance(b, dict) else str(b) for b in content
        )
    base["content"] = (content or "")[:2000]
    tcs = getattr(m, "tool_calls", None)
    if tcs:
        base["tool_calls"] = [{"name": tc.get("name"), "args": tc.get("args")} for tc in tcs]
    name = getattr(m, "name", None)
    if name:
        base["name"] = name
    return base


def _extract_code_execute_calls(messages: list) -> list[dict]:
    """Pair every code_execute tool call with the corresponding ToolMessage."""
    calls: list[dict] = []
    for i, m in enumerate(messages):
        for tc in getattr(m, "tool_calls", None) or []:
            if tc.get("name") != "code_execute":
                continue
            # Find the ToolMessage that responded to this call_id.
            tool_msg = next(
                (
                    later
                    for later in messages[i + 1 :]
                    if isinstance(later, ToolMessage) and later.tool_call_id == tc.get("id")
                ),
                None,
            )
            # `tool_msg is None` is legitimate: a recursion-limit hit or an
            # interrupted invocation can leave a tool_call orphaned without
            # its corresponding ToolMessage. Treat that as an empty result
            # so the pass-criteria reflect what actually happened, instead
            # of fabricating a "success" from a missing observation.
            if tool_msg is None:
                result = "(no tool response: chain stopped before observation)"
            else:
                content = getattr(tool_msg, "content", "") or ""
                result = content[:2000] if isinstance(content, str) else str(content)[:2000]
            calls.append({"args": tc.get("args", {}), "result": result})
    return calls


def _final_text(messages: list) -> str:
    """Last AIMessage content as a plain string, empty if none."""
    for m in reversed(messages):
        if isinstance(m, AIMessage):
            c = m.content
            if isinstance(c, list):
                c = " ".join(str(b.get("text", b)) if isinstance(b, dict) else str(b) for b in c)
            if c and c.strip():
                return c
    return ""


def run_one(agent, prompt: dict, thread_id: str) -> dict:
    """Invoke the agent once and harvest everything we care about."""
    started = time.monotonic()
    result = agent.invoke(
        {"messages": [HumanMessage(content=prompt["ask"])]},
        config={
            "configurable": {"thread_id": thread_id},
            "recursion_limit": 50,
        },
    )
    elapsed = time.monotonic() - started
    messages = result["messages"]
    code_calls = _extract_code_execute_calls(messages)
    final = _final_text(messages)
    # `must_contain` MUST appear in a sandbox tool result, NOT in the
    # agent's final text. Otherwise an agent that calls code_execute
    # for anything and then writes the answer from training/memory
    # would false-pass. Reviewer flag, 2026-05-18.
    expected = prompt["must_contain"].lower()
    contains_in_sandbox = any(expected in c["result"].lower() for c in code_calls)
    contains_in_final = expected in final.lower()
    return {
        "prompt": prompt,
        "elapsed_s": round(elapsed, 2),
        "n_messages": len(messages),
        "code_execute_calls": code_calls,
        "final_text": final,
        "tool_called": any(
            tc.get("name") == "code_execute"
            for m in messages
            for tc in (getattr(m, "tool_calls", None) or [])
        ),
        "expected_in_sandbox_output": contains_in_sandbox,
        "expected_in_final_answer": contains_in_final,
        "messages_trace": [_serialize_message(m) for m in messages],
    }


def render_report(runs: list[dict]) -> str:
    """Markdown report of the experiment."""
    lines = []
    ts = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    lines.append(f"# Sandbox E2E experiment -- {ts}")
    lines.append("")
    lines.append(
        "Three prompts in ascending difficulty probe whether `code_execute` "
        "is reachable from chat, returns output the agent can read back, "
        "and supports non-trivial reasoning over the result."
    )
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append(
        "Pass = `tool_called AND expected_in_sandbox_output`. The expected "
        "string must appear in a `code_execute` tool *result*, not in the "
        "agent's final text -- otherwise the model could parrot the value "
        "from training and false-pass."
    )
    lines.append("")
    lines.append("| ID | Label | tool called | expected in sandbox | in final | elapsed |")
    lines.append("|---|---|---|---|---|---|")
    for r in runs:
        p = r["prompt"]
        passed = r["tool_called"] and r["expected_in_sandbox_output"]
        lines.append(
            f"| `{p['id']}` | {p['label']} | "
            f"{'yes' if r['tool_called'] else 'no'} | "
            f"{'yes' if r['expected_in_sandbox_output'] else 'no'} | "
            f"{'yes' if r['expected_in_final_answer'] else 'no'} | "
            f"{r['elapsed_s']}s |" + (" **PASS**" if passed else " **FAIL**")
        )
    lines.append("")
    for r in runs:
        p = r["prompt"]
        lines.append(f"## `{p['id']}` -- {p['label']}")
        lines.append("")
        lines.append(f"**Prompt:** {p['ask']}")
        lines.append("")
        lines.append(
            f"**Elapsed:** {r['elapsed_s']}s  |  "
            f"**Messages:** {r['n_messages']}  |  "
            f"**code_execute calls:** {len(r['code_execute_calls'])}"
        )
        lines.append("")
        for i, c in enumerate(r["code_execute_calls"], 1):
            code = (c["args"] or {}).get("code", "")
            # Clamp the language label so a junk value (newlines, backticks)
            # can never break the markdown fence.
            raw_lang = str((c["args"] or {}).get("language", "python")).strip().lower()
            language = raw_lang if raw_lang in {"python", "bash"} else "text"
            lines.append(f"### Call {i} ({language})")
            lines.append("")
            lines.append("```" + language)
            lines.append(code.rstrip())
            lines.append("```")
            lines.append("")
            lines.append("Result:")
            lines.append("")
            lines.append("```")
            lines.append(c["result"].rstrip())
            lines.append("```")
            lines.append("")
        lines.append("**Final agent answer:**")
        lines.append("")
        lines.append("> " + (r["final_text"] or "(empty)").replace("\n", "\n> "))
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    # Sanity-check that the new tool is in the static toolset BEFORE we
    # build the agent. The compiled graph that `build_agent()` returns
    # does not expose `.tools` cleanly (deepagents wraps everything in a
    # state graph), so inspecting `CHAT_TOOLS` is the cheap pre-flight.
    # Whether the live agent actually invokes code_execute is verified
    # by the prompts themselves (P1 fails if the tool is not wired).
    from pelops.tools import CHAT_TOOLS

    tool_names = {t.name for t in CHAT_TOOLS}
    assert "code_execute" in tool_names, (
        f"code_execute missing from CHAT_TOOLS: {sorted(tool_names)}"
    )
    print(f"CHAT_TOOLS has code_execute (out of {len(tool_names)} tools)")

    print("Building agent...")
    t0 = time.time()
    agent = build_agent()
    print(f"  built in {time.time() - t0:.2f}s")

    runs = []
    for i, prompt in enumerate(PROMPTS, 1):
        print(f"\n--- Prompt {i}/{len(PROMPTS)}: {prompt['id']} ---")
        print(f"  ask: {prompt['ask'][:100]}...")
        r = run_one(agent, prompt, thread_id=f"sandbox-e2e-{prompt['id']}")
        runs.append(r)
        print(
            f"  done in {r['elapsed_s']}s | "
            f"tool_called={r['tool_called']} | "
            f"in_sandbox={r['expected_in_sandbox_output']} | "
            f"n_calls={len(r['code_execute_calls'])}"
        )

    report = render_report(runs)
    out_path = Path(__file__).parent / "runs" / f"sandbox_e2e_{datetime.now(UTC):%Y-%m-%dT%H%M}Z.md"
    # The runs/ dir is committed in this repo, but a fresh clone OR a
    # future relocation of the script must not crash the experiment
    # on a missing parent.
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report, encoding="utf-8")
    raw_path = out_path.with_suffix(".json")
    raw_path.write_text(
        json.dumps(runs, indent=2, default=str),
        encoding="utf-8",
    )
    n_pass = sum(1 for r in runs if r["tool_called"] and r["expected_in_sandbox_output"])
    print(f"\n=== {n_pass}/{len(runs)} prompts passed ===")
    print(f"Report: {out_path}")
    print(f"Raw:    {raw_path}")
    return 0 if n_pass == len(runs) else 1


if __name__ == "__main__":
    raise SystemExit(main())
