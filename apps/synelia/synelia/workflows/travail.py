"""Workflow Temporal pur (sandbox) — aucun import non déterministe au niveau module."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import workflow


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
            "executer_etapes",
            entree,
            start_to_close_timeout=timedelta(hours=2),
            heartbeat_timeout=timedelta(minutes=5),
        )
        while statut in {"failed", "rolled_back"}:
            await workflow.wait_condition(lambda: self._relance)
            self._relance = False
            statut = await workflow.execute_activity(
                "executer_etapes",
                {"travail_id": entree["travail_id"], "depuis": None},
                start_to_close_timeout=timedelta(hours=2),
            )
        return statut
