"""Quick smoke test: build agent + send 3 messages exercising different paths."""

from __future__ import annotations

import time

from pelops.agent import ask, build_agent
from pelops.config import Settings


def banner(s: str) -> None:
    print(f"\n{'=' * 8} {s} {'=' * 8}")


def main() -> None:
    s = Settings.load()
    banner("Settings")
    print(f"  chat_model     = {s.chat_model}")
    print(f"  research_model = {s.research_model}")
    print(f"  vstash_db      = {s.vstash_db}")
    print(f"  owner          = {s.owner}")

    banner("Build agent")
    t0 = time.time()
    build_agent()
    print(f"  built in {time.time() - t0:.2f}s")

    banner("Test 1: pure conversational (no tools)")
    t0 = time.time()
    out = ask("Saludame en una sola linea. No uses tools.")
    print(f"  ({time.time() - t0:.2f}s) {out}")

    banner("Test 2: vstash write + read")
    t0 = time.time()
    out = ask(
        "Guarda esta nota en memoria con vstash_remember: "
        "'Jay vive en Alemania, prefiere ASCII puro, sin em dashes.' "
        "Titulo: 'pref_location'. Tags: 'user,prefs'. "
        "Luego confirma en una linea."
    )
    print(f"  ({time.time() - t0:.2f}s) {out}")

    t0 = time.time()
    out = ask("Donde vive Jay? Usa vstash_recall antes de responder.")
    print(f"  ({time.time() - t0:.2f}s) {out}")

    banner("Test 3: research via groq/compound-mini")
    t0 = time.time()
    out = ask(
        "Usa la herramienta `research` con deep=False para averiguar: "
        "que version de Python es la mas reciente estable? Una sola linea con el numero y fuente."
    )
    print(f"  ({time.time() - t0:.2f}s) {out}")

    banner("Done")


if __name__ == "__main__":
    main()
