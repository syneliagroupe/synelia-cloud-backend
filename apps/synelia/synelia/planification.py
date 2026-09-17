"""Temporal Schedules : une seule horloge, visible dans l'UI Temporal."""

from __future__ import annotations

from datetime import timedelta

PLANIFICATIONS = [
    ("facturation-cycle-mensuel", "facturation.cycle", "0 2 1 * *"),
    ("facturation-relances-quotidiennes", "facturation.relances", "0 6 * * *"),
    ("metrologie-releve-horaire", "metrologie.releve", "0 * * * *"),
    ("ssl-renouvellement", "web.ssl.renew", "0 3 * * *"),
    ("credentials-rotation", "securite.rotation_credentials", "0 4 1 */6 *"),
    ("reconciliation-amont", "infra.reconciliation", "30 * * * *"),
]


async def declarer() -> None:
    from synelia_kernel.config import reglages
    from temporalio.client import Client, Schedule, ScheduleActionStartWorkflow, ScheduleSpec

    from synelia.travaux.temporal import FILE

    r = reglages()
    client = await Client.connect(
        r.temporal_adresse or "localhost:7233", namespace=r.temporal_espace
    )
    for ident, type_travail, cron in PLANIFICATIONS:
        action = ScheduleActionStartWorkflow(
            "TravailPlanifieWorkflow",
            {"type": type_travail},
            id=f"planifie-{type_travail}",
            task_queue=FILE,
            execution_timeout=timedelta(hours=3),
        )
        try:
            await client.create_schedule(
                ident, Schedule(action=action, spec=ScheduleSpec(cron_expressions=[cron]))
            )
        except Exception:  # noqa: BLE001, S110 — déjà déclarée
            pass
