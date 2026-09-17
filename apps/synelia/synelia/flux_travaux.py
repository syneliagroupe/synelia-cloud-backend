"""Définitions enregistrées par le worker : workflow `TravailWorkflow`, activité `executer_etapes`.

Deux contraintes du SDK dictent l'emplacement de ce module :
- `@workflow.run` est refusé sur une classe locale : la classe doit être définie au niveau module ;
- le bac à sable réimporte ce module (et ses paquets parents) à chaque tâche de workflow, d'où un
  module de premier niveau plutôt que `synelia.travaux`, dont le `__init__` charge le moteur et
  ses dépendances non déterministes.

L'activité importe `worker_ctx` à l'exécution : les activités tournent hors bac à sable.

Nécessite l'extra `temporal` (`uv sync --extra temporal`)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import activity, workflow


@activity.defn(name="executer_etapes")
async def executer_etapes(entree: dict[str, Any]) -> str:
    from synelia.travaux.worker_ctx import executer_depuis_worker

    return await executer_depuis_worker(entree["travail_id"], entree.get("depuis"))


@workflow.defn(name="TravailWorkflow")
class TravailWorkflow:
    def __init__(self) -> None:
        self._relance = False

    @workflow.signal
    def relancer(self) -> None:
        self._relance = True

    @workflow.run
    async def run(self, entree: dict[str, Any]) -> str:
        statut = await workflow.execute_activity(
            executer_etapes,
            entree,
            start_to_close_timeout=timedelta(hours=2),
            heartbeat_timeout=timedelta(minutes=5),
        )
        while statut in {"failed", "rolled_back"}:
            await workflow.wait_condition(lambda: self._relance)
            self._relance = False
            statut = await workflow.execute_activity(
                executer_etapes,
                {"travail_id": entree["travail_id"], "depuis": None},
                start_to_close_timeout=timedelta(hours=2),
            )
        return statut
