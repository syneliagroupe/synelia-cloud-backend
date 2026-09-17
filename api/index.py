"""Point d'entrée Vercel (runtime Python) : l'application FastAPI, tout chemin réécrit ici par vercel.json."""

import sys
from pathlib import Path

RACINE = Path(__file__).resolve().parents[1]
for p in (
    "apps/synelia",
    "packages/kernel",
    "packages/contract",
    "packages/db",
    "packages/catalogue",
    "packages/openstack",
    "packages/testing",
):
    sys.path.insert(0, str(RACINE / p))

from synelia.app import creer_app  # noqa: E402

app = creer_app()
