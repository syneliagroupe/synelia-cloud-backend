"""CLI : `synelia api|worker|scheduler|contrat|amorcer`."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import typer

cli = typer.Typer(help="Synelia Cloud — backend", no_args_is_help=True)
contrat = typer.Typer(help="Contrat OpenAPI : synchronisation et couverture")
cli.add_typer(contrat, name="contrat")

RACINE = Path(__file__).resolve().parents[3]


@cli.command()
def api(
    hote: str = "0.0.0.0",
    port: int = int(os.environ.get("PORT", "4000")),
    rechargement: bool = False,
    travailleurs: int = int(os.environ.get("SYNELIA_API_WORKERS", "1")),
) -> None:
    """Sert l'API (uvicorn). `SYNELIA_API_WORKERS` (défaut 1) : au-delà de 1, chaque worker
    uvicorn a son propre pool SQLAlchemy, seau de limitation de débit (`deps/limitation.py`) et
    quota IA en mémoire (`modules/ia_agents/cles.py`) — voir étape 1.7 de
    docs/PLAN-ARCHITECTURE-SUITE.md. Après (c) du plan (le worker des travaux séparé) : avant,
    plusieurs workers auraient multiplié les jobs orphelins à chaque redéploiement."""
    import uvicorn

    if rechargement and travailleurs > 1:
        raise typer.BadParameter("--rechargement et plusieurs travailleurs sont incompatibles (uvicorn).")
    uvicorn.run(
        "synelia.asgi:app",
        host=hote,
        port=port,
        reload=rechargement,
        factory=False,
        workers=travailleurs,
    )


@cli.command()
def worker() -> None:
    """Worker des travaux : Temporal si `SYNELIA_TEMPORAL_ADRESSE`, sinon moteur Local (poll de
    `travaux`, cf. `synelia.travaux.local`) — dev01 (`docker-compose.dev01.yml`, service
    `worker`). Même façade CLI, deux moteurs : le module `synelia.travaux.moteur` le promet déjà
    (`MoteurLocal`/`MoteurTemporal`)."""
    from synelia_kernel.config import reglages

    if reglages().temporal_adresse:

        async def _run_temporal() -> None:
            from temporalio.client import Client
            from temporalio.worker import Worker

            from synelia.app import routeurs_modules
            from synelia.travaux import temporal

            routeurs_modules()  # importe les modules → enregistre les exécuteurs
            r = reglages()
            client = await Client.connect(
                r.temporal_adresse or "localhost:7233", namespace=r.temporal_espace
            )
            wfs, acts = temporal.definitions()
            w = Worker(client, task_queue=temporal.FILE, workflows=wfs, activities=acts)
            typer.echo(f"worker sur {temporal.FILE} ({r.temporal_adresse})")
            await w.run()

        asyncio.run(_run_temporal())
        return

    from synelia.travaux import local

    asyncio.run(local.demarrer())


@cli.command("relais-smtp")
def relais_smtp() -> None:
    """Relais SMTP réel (module web_smtp) : AUTH + quota + relais vers l'amont sur le port 587."""
    from synelia.relais_smtp import demarrer

    demarrer()


@cli.command()
def scheduler() -> None:
    """Déclare les Temporal Schedules (cycle de facturation, relances, ACME…). Idempotent."""
    from synelia.planification import declarer

    asyncio.run(declarer())


@cli.command()
def amorcer() -> None:
    """Crée les tables manquantes et les données d'amorçage."""

    async def _run() -> None:
        from synelia_db.session import initialiser_schema

        from synelia import amorcage

        await initialiser_schema()
        await amorcage.amorcer()

    asyncio.run(_run())
    typer.echo("amorçage terminé")


@contrat.command("sync")
def contrat_sync(frontend: str = "../synelia-cloud") -> None:
    """Copie openapi.json, RBAC, workflows, configurations depuis le frontend ; régénère les modèles."""
    subprocess.run(
        [sys.executable, str(RACINE / "tools" / "contrat_sync.py"), frontend], check=True
    )


@contrat.command("diff")
def contrat_diff(strict: bool = False) -> None:
    """Couverture : opérations du contrat présentes dans l'application."""
    from tools_diff import executer  # type: ignore[import-not-found]

    raise SystemExit(executer(strict))


def main() -> None:
    sys.path.insert(0, str(RACINE / "tools"))
    cli()


if __name__ == "__main__":
    main()
