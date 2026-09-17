"""Moteur Temporal : ouverture et relance du workflow générique `TravailWorkflow`.

Le workflow et son activité vivent dans `synelia.flux_travaux` (le SDK exige des classes de
workflow au niveau module, réimportables dans le bac à sable) ; ici seul le client Temporal est
utilisé, sans importer `temporalio` au chargement du module.

Activé par SYNELIA_TEMPORAL_ADRESSE ; nécessite l'extra `temporal` (`uv sync --extra temporal`)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from synelia_db.modeles import Travail
from synelia_kernel.config import reglages

FILE = "synelia-travaux"


async def _client() -> Any:
    from temporalio.client import Client

    r = reglages()
    return await Client.connect(r.temporal_adresse or "localhost:7233", namespace=r.temporal_espace)


async def lancer(travail: Travail) -> None:
    client = await _client()
    await client.start_workflow(
        "TravailWorkflow",
        {"travail_id": travail.id, "depuis": 0},
        id=travail.id,
        task_queue=FILE,
        execution_timeout=timedelta(hours=6),
    )


async def relancer(travail: Travail) -> None:
    client = await _client()
    handle = client.get_workflow_handle(travail.id)
    try:
        await handle.signal("relancer")
    except Exception:  # noqa: BLE001
        await client.start_workflow(
            "TravailWorkflow",
            {"travail_id": travail.id, "depuis": None},
            id=f"{travail.id}-r{travail.essai}",
            task_queue=FILE,
        )


def definitions() -> tuple[list[Any], list[Any]]:
    """Workflow + activités à enregistrer par le worker (`synelia worker`)."""
    from synelia.flux_travaux import TravailWorkflow, executer_etapes

    return [TravailWorkflow], [executer_etapes]
