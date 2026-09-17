"""Documentation & formation : bac à sable, parcours, progression, sections."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Response, status
from synelia_contract import modeles as m
from synelia_kernel import erreurs
from synelia_kernel.dates import maintenant
from synelia_kernel.ids import nouvel_id

from synelia.audit import journaliser
from synelia.deps import Ctx, CtxPublic
from synelia.modules.docs.service import PARCOURS, SECTIONS, detenteur_bac, detenteur_progression
from synelia.travaux import demarrer_travail

router = APIRouter(prefix="/docs", tags=["Documentation & formation"])


# NB : la progression est scellée par utilisateur (parent_id), pas par un identifiant
# composite "user:slug" — celui-ci dépassait le VARCHAR(36) de `ressources.id` (fait pour
# des UUID) dès qu'on y ajoutait le moindre suffixe, et échouait uniquement sur Postgres
# (SQLite, utilisé par les tests, n'impose pas la longueur).
async def _ligne_progression(ctx: Ctx, parcours_slug: str) -> Any:
    lignes = await detenteur_progression.lignes(ctx, parent_id=ctx.utilisateur_id or "")
    return next((r for r in lignes if r.nom == parcours_slug), None)


@router.get("/bac-a-sable", response_model=m.BacASable, response_model_exclude_none=True)
async def obtenir_bac_a_sable(ctx: Ctx) -> Any:
    lignes = await detenteur_bac.lignes(ctx)
    if not lignes:
        raise erreurs.introuvable("Bac à sable", ctx.org_id)
    return lignes[0].donnees


@router.post(
    "/bac-a-sable",
    response_model=m.TravailProvisioning,
    status_code=status.HTTP_202_ACCEPTED,
    response_model_exclude_none=True,
)
async def creer_bac_a_sable(corps: m.DocsBacASablePostRequest, ctx: Ctx) -> Any:
    existants = await detenteur_bac.lignes(ctx)
    if existants:
        raise erreurs.conflit(
            "Un bac à sable existe déjà pour cette organisation.", code="bac_a_sable_existant"
        )
    bac = m.BacASable(
        id=nouvel_id(),
        statut="en_preparation",
        espaceId=None,
        expire=None,
        quota={"vcpu": 4, "ramGo": 8, "stockageTo": 0.05},
        urlPortail=None,
        reinitialisationsRestantes=3,
    )
    await detenteur_bac.creer(ctx, bac)
    await journaliser(ctx, action="docs.bac_a_sable", cible_type="bac_a_sable", cible_id=bac.id)
    return await demarrer_travail(
        ctx,
        "docs.bac_a_sable",
        "Bac à sable",
        cible_type="bac_a_sable",
        cible_id=bac.id,
        entree=corps.model_dump(mode="json"),
        etapes=[
            {"nom": "Préparer l'environnement isolé", "dureeS": 10},
            {"nom": "Installer les outils de formation", "dureeS": 20},
        ],
    )


@router.delete("/bac-a-sable", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def supprimer_bac_a_sable(ctx: Ctx) -> Any:
    lignes = await detenteur_bac.lignes(ctx)
    if not lignes:
        raise erreurs.introuvable("Bac à sable", ctx.org_id)
    await detenteur_bac.supprimer(ctx, lignes[0].id, logique=True)
    await journaliser(
        ctx, action="docs.bac_a_sable.suppression", cible_type="bac_a_sable", cible_id=lignes[0].id
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/parcours", response_model=list[m.ParcoursFormation], response_model_exclude_none=True)
async def lister_parcours_formation(
    ctx: Ctx, niveau: str | None = None, role: str | None = None
) -> Any:
    data = [
        p
        for p in PARCOURS
        if (not niveau or p["niveau"] == niveau)
        and (not role or role in (p.get("publicVise") or []))
    ]
    return data


def _parcours(slug: str) -> dict[str, Any]:
    p = next((x for x in PARCOURS if x["slug"] == slug), None)
    if p is None:
        raise erreurs.introuvable("Parcours de formation", slug)
    return p


async def _progression(ctx: Ctx, parcours_slug: str) -> m.ProgressionFormation | None:
    ligne = await _ligne_progression(ctx, parcours_slug)
    return m.ProgressionFormation.model_validate(ligne.donnees) if ligne else None


@router.get(
    "/parcours/{parcoursSlug}",
    response_model=m.DocsParcoursParcoursSlugGetResponse,
    response_model_exclude_none=True,
)
async def obtenir_parcours_formation(parcoursSlug: str, ctx: Ctx) -> Any:  # noqa: N803
    p = _parcours(parcoursSlug)
    prog = await _progression(ctx, parcoursSlug)
    return {"parcours": p, "progression": prog.model_dump(mode="json") if prog else None}


@router.post(
    "/parcours/{parcoursSlug}/modules/{moduleSlug}/completion",
    response_model=m.ProgressionFormation,
    response_model_exclude_none=True,
)
async def valider_module_formation(
    parcoursSlug: str,
    moduleSlug: str,
    corps: m.DocsParcoursParcoursSlugModulesModuleSlugCompletionPostRequest,
    ctx: Ctx,
) -> Any:  # noqa: N803
    p = _parcours(parcoursSlug)
    module = next((mo for mo in p["modules"] if mo["slug"] == moduleSlug), None)
    if module is None:
        raise erreurs.introuvable("Module de formation", moduleSlug)
    total = len(p["modules"])
    ligne = await _ligne_progression(ctx, parcoursSlug)
    existant = m.ProgressionFormation.model_validate(ligne.donnees) if ligne else None
    termines = set(existant.modulesTermines) if existant else set()
    termines.add(moduleSlug)
    pct = round(100 * len(termines) / total, 1)
    prog = m.ProgressionFormation(
        parcoursSlug=parcoursSlug,
        modulesTermines=sorted(termines),
        pctComplete=pct,
        commence=(existant.commence if existant else maintenant()),
        termine=(maintenant() if pct >= 100 else (existant.termine if existant else None)),
        attestationUrl=(
            f"/docs/certificats/{parcoursSlug}/{ctx.utilisateur_id}"
            if pct >= 100
            else (existant.attestationUrl if existant else None)
        ),
    )
    if ligne:
        await detenteur_progression.remplacer(ctx, ligne.id, prog)
    else:
        await detenteur_progression.creer(ctx, prog, parent_id=ctx.utilisateur_id)
    await journaliser(ctx, action="docs.module", cible_type="docs_module", cible=moduleSlug)
    return prog.model_dump(mode="json")


@router.get(
    "/progression", response_model=list[m.ProgressionFormation], response_model_exclude_none=True
)
async def lister_ma_progression(ctx: Ctx) -> Any:
    lignes = await detenteur_progression.lignes(ctx, parent_id=ctx.utilisateur_id or "")
    return [ligne.donnees for ligne in lignes]


@router.get("/sections", response_model=m.DocsSectionsGetResponse, response_model_exclude_none=True)
async def lister_sections_documentation(ctx: CtxPublic) -> Any:
    return SECTIONS
