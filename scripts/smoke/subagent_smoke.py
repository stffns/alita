"""Verify the researcher sub-agent is reachable via the task tool."""

from __future__ import annotations

import time

from pelops.agent import ask, build_agent


def main() -> None:
    print("Building agent...")
    agent = build_agent()
    print("Subagents available:")
    for sa in agent.config.get("subagents", []) if hasattr(agent, "config") else []:
        print(f"  - {sa}")

    print("\n=== Test: delegate to researcher ===")
    t0 = time.time()
    out = ask(
        "Delega al sub-agente `researcher` esta pregunta: "
        "Cuales son las 3 caracteristicas mas nuevas de deepagents en su release 0.6.x? "
        "Quiero el brief en el formato estructurado del researcher (TL;DR + Findings + Sources). "
        "Despues de obtener el brief, guardalo en memoria con titulo 'deepagents-0.6.x-features'."
    )
    print(f"\n({time.time() - t0:.1f}s)")
    print(out)


if __name__ == "__main__":
    main()
