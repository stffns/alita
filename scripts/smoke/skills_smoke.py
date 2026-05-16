"""Verify skills are loaded and the agent can discover/read them."""

from __future__ import annotations

import time

from pelops.agent import ask


def main() -> None:
    print("=== Test 1: ask Pelops what skills it has ===")
    t0 = time.time()
    out = ask(
        "Que skills tienes disponibles? Lista cada una con su nombre y "
        "descripcion en una linea. No leas los archivos completos -- solo "
        "muestrame la metadata que ya tienes cargada."
    )
    print(f"({time.time() - t0:.1f}s)\n{out}\n")

    print("=== Test 2: invoke briefing-builder skill ===")
    t0 = time.time()
    out = ask(
        "Quiero que ejecutes el skill `briefing-builder`. Sigue el workflow "
        "tal como esta escrito en SKILL.md -- lee el archivo primero con "
        "read_file y limit=1000. Si no hay material reciente en vstash, "
        "respondelo asi y termina."
    )
    print(f"({time.time() - t0:.1f}s)\n{out}\n")


if __name__ == "__main__":
    main()
