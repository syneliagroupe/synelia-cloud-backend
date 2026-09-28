from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Response, status
from sqlalchemy import select
from synelia_contract import modeles as m
from synelia_db.modeles import Travail
from synelia_kernel import erreurs
from synelia_kernel.dates import depuis_iso

from synelia.audit import journaliser
from synelia.deps import Ctx, Page, exiger_confirmation
from synelia.deps.contexte import Contexte
from synelia.deps.pagination import filtrer_trier_paginer
from synelia.travaux import moteur, vers_contrat

STATUTS_TERMINAUX = {"done", "failed", "rolled_back"}

router = APIRouter(prefix="/travaux", tags=["Travaux de provisioning"])


async def _travail(ctx: Contexte, travail_id: str) -> Travail:
    t = await ctx.session.get(Travail, travail_id)
    if t is None or (
        t.org_id != ctx.org_id_ou_none
        and not (ctx.principal and ctx.principal.est_admin_plateforme)
    ):
        raise erreurs.introuvable("Travail", travail_id)
    return t


@router.get("", response_model=m.TravauxGetResponse, response_model_exclude_none=True)
async def lister_travaux(
    ctx: Ctx,
    page: Page,
    statut: str | None = None,
    type: str | None = None,
    depuis: str | None = None,
) -> Any:  # noqa: A002
    q = select(Travail).where(Travail.org_id == ctx.org_id).order_by(Travail.started_at.desc())
    if statut:
        q = q.where(Travail.statut == statut)
    if type:
        q = q.where(Travail.type == type)
    if depuis:
        q = q.where(Travail.started_at >= depuis_iso(depuis))
    items = [vers_contrat(t) for t in (await ctx.session.execute(q)).scalars()]
    return filtrer_trier_paginer(items, page, champs_recherche=("label", "type"))


@router.get("/{travailId}", response_model=m.TravailProvisioning, response_model_exclude_none=True)
async def obtenir_travail(ctx: Ctx, travailId: str) -> Any:  # noqa: N803
    return vers_contrat(await _travail(ctx, travailId))


@router.get("/{travailId}/export")
async def telecharger_export_travail(ctx: Ctx, travailId: str) -> Response:  # noqa: N803
    """Sert le fichier déposé par un exécuteur d'export (ex. `facturation.export`) — les
    clés `export_bucket`/`export_cle` sont posées dans `travail.contexte` par l'exécuteur.
    Sans ce chemin, `POST /consommation/export` renvoyait une URL qui ne menait jamais nulle
    part (404 franc, pas une réponse mensongère à une URL par ailleurs inexistante)."""
    t = await _travail(ctx, travailId)
    bucket = t.contexte.get("export_bucket")
    cle = t.contexte.get("export_cle")
    if not bucket or not cle:
        raise erreurs.introuvable("Export", travailId)
    import asyncio

    from synelia.modules.stockage.service import amont_objet

    contenu = await asyncio.to_thread(amont_objet().recuperer_objet, bucket, cle)
    if contenu is None:
        raise erreurs.introuvable("Export", travailId)
    content_type = "application/json" if cle.endswith(".json") else "text/csv"
    return Response(
        content=contenu,
        media_type=content_type,
        headers={"Content-Disposition": f'attachment; filename="{cle.rsplit("/", 1)[-1]}"'},
    )


@router.post(
    "/{travailId}/relance",
    response_model=m.TravailProvisioning,
    status_code=status.HTTP_202_ACCEPTED,
    response_model_exclude_none=True,
)
async def relancer_travail(ctx: Ctx, travailId: str) -> Any:  # noqa: N803
    t = await moteur.relancer(ctx, await _travail(ctx, travailId))
    await journaliser(
        ctx, action="travail.relance", cible_type="travail", cible_id=t.id, cible=t.label
    )
    return vers_contrat(t)


@router.post(
    "/{travailId}/annulation",
    response_model=m.TravailProvisioning,
    status_code=status.HTTP_202_ACCEPTED,
    response_model_exclude_none=True,
)
async def annuler_travail(ctx: Ctx, travailId: str) -> Any:  # noqa: N803
    t = await moteur.annuler(ctx, await _travail(ctx, travailId))
    await journaliser(
        ctx, action="travail.annulation", cible_type="travail", cible_id=t.id, cible=t.label
    )
    return vers_contrat(t)


@router.delete("/{travailId}", status_code=status.HTTP_204_NO_CONTENT)
async def purger_travail(ctx: Ctx, travailId: str, confirmation: str | None = None) -> Response:  # noqa: N803
    """Retire une tâche terminée du centre de tâches — le journal d'audit garde la trace."""
    t = await _travail(ctx, travailId)
    exiger_confirmation(t.id, confirmation)
    if t.statut not in STATUTS_TERMINAUX:
        raise erreurs.conflit(
            "Seule une tâche terminée, en échec ou annulée peut être purgée.",
            code="travail_non_termine",
        )
    await ctx.session.delete(t)
    await ctx.session.flush()
    await journaliser(
        ctx, action="travail.purge", cible_type="travail", cible_id=t.id, cible=t.label
    )
    return Response(status_code=204)
