"""Moteur Temporal : client + activité ; le workflow vit dans `synelia.workflows.travail`.

Activé par SYNELIA_TEMPORAL_ADRESSE ; nécessite l'extra `temporal` (`uv sync --extra temporal`).

Les classes workflow doivent être définies au niveau module : Temporal refuse
`@workflow.run` sur une classe locale (`<locals>` dans le qualname). Elles doivent
aussi vivre hors de `synelia.travaux` (dont `__init__` importe le moteur non
déterministe) pour passer la sandbox Temporal.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from synelia_db.modeles import Travail
from synelia_kernel.config import reglages
from temporalio import activity

from synelia.workflows.travail import TravailWorkflow

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


@activity.defn(name="executer_etapes")
async def executer_etapes(entree: dict[str, Any]) -> str:
    from synelia.travaux.worker_ctx import executer_depuis_worker

    return await executer_depuis_worker(entree["travail_id"], entree.get("depuis"))


def definitions() -> tuple[list[Any], list[Any]]:
    """Workflow + activités à enregistrer par le worker (`synelia worker`)."""
    return [TravailWorkflow], [executer_etapes]
