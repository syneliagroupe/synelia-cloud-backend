from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, status
from synelia_contract import modeles as m
from synelia_kernel import erreurs
from synelia_kernel.dates import maintenant
from synelia_kernel.ids import nouvel_id

from synelia.audit import journaliser
from synelia.depot import Depot
from synelia.deps import Contexte, Page, exige
from synelia.modules.deploiements import service
from synelia.modules.deploiements.service import (
    _CLE_APPROBATION,
    depot_deploy,
    depot_env,
)
from synelia.travaux import demarrer_travail

router = APIRouter(tags=["Déploiements"])

_ETAPES_DEPLOIEMENT = [
    {"nom": "build", "dureeS": 4},
    {"nom": "scan", "dureeS": 3},
    {"nom": "provision", "dureeS": 5},
    {"nom": "deploy", "dureeS": 6},
]


async def _deploiement_depuis_id(
    ctx: Contexte, deploiement_id: str, *, exiger_approbation: bool = False
) -> m.Deploiement:
    d = await depot_deploy.obtenir(ctx, deploiement_id)
    return d


@router.get(
    "/deploiements", response_model=m.DeploiementsGetResponse, response_model_exclude_none=True
)
async def lister_deploiements(
    page: Page,
    appId: str | None = None,
    envId: str | None = None,
    statut: str | None = None,
    branche: str | None = None,
    ctx: Contexte = Depends(exige(None)),
) -> Any:  # noqa: N803, PLR0917
    return await depot_deploy.lister(
        ctx,
        page,
        filtre=lambda d: (
            (not appId or d.appId == appId)
            and (not envId or d.envId == envId)
            and (not statut or d.statut == statut)
            and (not branche or d.branche == branche)
        ),
        tri_defaut="startedAt",
    )


@router.post(
    "/deploiements",
    response_model=m.Deploiement,
    status_code=status.HTTP_202_ACCEPTED,
    response_model_exclude_none=True,
)
async def lancer_deploiement(
    corps: m.DeploiementDemande, ctx: Contexte = Depends(exige("app.deploy"))
) -> Any:
    env = await depot_env.obtenir(ctx, corps.envId)
    version = corps.commit or corps.image or f"v{maintenant().strftime('%Y%m%d%H%M%S')}"
    d = m.Deploiement(
        id=nouvel_id(),
        envId=env.id,
        envNom=env.nom,
        appId=env.appId,
        version=version,
        commit=corps.commit,
        commitMessage=corps.message,
        branche=corps.branche,
        auteur=ctx.principal.email if ctx.principal else "systeme",
        statut="queued",
        etapes=[
            m.Etape(nom=n, statut="pending", logRef=f"/deploiement/{deploiement_ref()}/etapes/{n}")
            for n in ("build", "scan", "provision", "deploy")
        ],
        findings=[],
        startedAt=maintenant(),
        dureeS=None,
    )
    await depot_deploy.creer(ctx, d, parent_id=env.id)
    awaiting = bool(env.protection and env.protection.approbationRequise)
    if awaiting:
        await service._poser_drapeau(ctx, d.id, _CLE_APPROBATION, True)
    await journaliser(
        ctx, action="deploiement.lancement", cible_type="deploiement", cible_id=d.id, cible=version
    )
    if awaiting:
        return await _deploiement_depuis_id(ctx, d.id)
    await demarrer_travail(
        ctx,
        "app.deploy",
        version,
        cible_type="deploiement",
        cible_id=d.id,
        entree=corps.model_dump(mode="json"),
        etapes=[
            {"nom": "Build de l'image", "dureeS": 4},
            {"nom": "Scan de vulnérabilités", "dureeS": 3},
            {"nom": "Provisionnement", "dureeS": 5},
            {"nom": "Déploiement", "dureeS": 6},
        ],
        contexte={},
    )
    return await _deploiement_depuis_id(ctx, d.id)


def deploiement_ref() -> str:
    import uuid

    return str(uuid.uuid4())[:8]


@router.get(
    "/deploiements/{deploiementId}", response_model=m.Deploiement, response_model_exclude_none=True
)
async def obtenir_deploiement(deploiementId: str, ctx: Contexte = Depends(exige(None))) -> Any:  # noqa: N803
    return await depot_deploy.obtenir(ctx, deploiementId)


@router.post(
    "/deploiements/{deploiementId}/approbation",
    response_model=m.Deploiement,
    response_model_exclude_none=True,
)
async def approuver_deploiement(
    deploiementId: str,
    corps: m.DeploiementsDeploiementIdApprobationPostRequest,
    ctx: Contexte = Depends(exige("app.deploy")),
) -> Any:  # noqa: N803
    d = await depot_deploy.obtenir(ctx, deploiementId)
    awaiting = await service._drapeau(ctx, d.id, _CLE_APPROBATION)
    if not awaiting:
        raise erreurs.conflit(
            "Ce déploiement n'attend pas d'approbation.", code="pas_d_approbation_en_attente"
        )
    if corps.decision == "refuser":
        await service._poser_drapeau(ctx, d.id, _CLE_APPROBATION, False)
        await depot_deploy.modifier(ctx, d.id, {"statut": "failed"})
        await journaliser(
            ctx,
            action="deploiement.refus",
            cible_type="deploiement",
            cible_id=d.id,
            details={"motif": corps.motif},
        )
        return await _deploiement_depuis_id(ctx, d.id)
    await service._poser_drapeau(ctx, d.id, _CLE_APPROBATION, False)
    await journaliser(
        ctx,
        action="deploiement.approbation",
        cible_type="deploiement",
        cible_id=d.id,
        details={"motif": corps.motif},
    )
    await demarrer_travail(
        ctx,
        "app.deploy",
        d.version,
        cible_type="deploiement",
        cible_id=d.id,
        etapes=[
            {"nom": "Build de l'image", "dureeS": 4},
            {"nom": "Scan de vulnérabilités", "dureeS": 3},
            {"nom": "Provisionnement", "dureeS": 5},
            {"nom": "Déploiement", "dureeS": 6},
        ],
    )
    return await _deploiement_depuis_id(ctx, d.id)


@router.post(
    "/deploiements/{deploiementId}/canari",
    response_model=m.Deploiement,
    status_code=status.HTTP_202_ACCEPTED,
    response_model_exclude_none=True,
)
async def regler_canari_deploiement(
    deploiementId: str,
    corps: m.DeploiementsDeploiementIdCanariPostRequest,
    ctx: Contexte = Depends(exige("app.deploy")),
) -> Any:  # noqa: N803
    d = await depot_deploy.obtenir(ctx, deploiementId)
    env = await depot_env.obtenir(ctx, d.envId)
    canari = env.canari or m.Canari(pct=0.0, seuil5xx=1.0, fenetreS=300)
    if corps.action == "terminer":
        pct = 100.0
    elif corps.action == "annuler":
        pct = 0.0
    else:
        pct = float(corps.pct if corps.pct is not None else canari.pct)
        if corps.action == "avancer":
            pct = min(100.0, max(0.0, canari.pct + (corps.pct or 10)))
    await depot_env.modifier(
        ctx,
        d.envId,
        {
            "canari": env.canari.model_copy(update={"pct": pct}).model_dump(mode="json")
            if env.canari
            else m.Canari(pct=pct, seuil5xx=1.0, fenetreS=300).model_dump(mode="json")
        },
    )
    await journaliser(
        ctx,
        action="deploiement.canari",
        cible_type="deploiement",
        cible_id=d.id,
        details={"action": corps.action, "pct": pct},
    )
    return await _deploiement_depuis_id(ctx, d.id)


@router.get(
    "/deploiements/{deploiementId}/journaux",
    response_model=m.ExtraitLogs,
    response_model_exclude_none=True,
)
async def obtenir_journaux_deploiement(
    deploiementId: str, etape: str | None = None, ctx: Contexte = Depends(exige(None))
) -> Any:  # noqa: N803
    d = await depot_deploy.obtenir(ctx, deploiementId)
    lignes = []
    refs = [e for e in d.etapes if not etape or e.nom == etape]
    for e in refs:
        etat = "ok" if e.statut == "ok" else ("en cours" if e.statut == "running" else "en attente")
        lignes.append(
            m.LigneLog(
                ts=maintenant(),
                niveau="INFO",
                source=e.nom,
                message=f"[{e.nom}] étape {e.logRef} — statut : {etat}",
            )
        )
    tronque = len(lignes) > 20
    return m.ExtraitLogs(lignes=lignes[:20], tronque=tronque, lienVictoriaLogs=None)


@router.post(
    "/deploiements/{deploiementId}/promotion",
    response_model=m.Deploiement,
    status_code=status.HTTP_202_ACCEPTED,
    response_model_exclude_none=True,
)
async def promouvoir_deploiement(
    deploiementId: str,
    corps: m.DeploiementsDeploiementIdPromotionPostRequest,
    ctx: Contexte = Depends(exige("app.deploy")),
) -> Any:  # noqa: N803
    d = await depot_deploy.obtenir(ctx, deploiementId)
    cible = await depot_env.obtenir(ctx, corps.envCibleId)
    nouveau = d.model_copy(deep=True)
    nouveau.id = nouvel_id()
    nouveau.envId = cible.id
    nouveau.envNom = cible.nom
    nouveau.startedAt = maintenant()
    nouveau.statut = "queued"
    nouveau.etapes = [m.Etape(nom=e.nom, statut="pending", logRef=f"/{e.nom}") for e in d.etapes]
    nouveau.dureeS = None
    await depot_deploy.creer(ctx, nouveau, parent_id=cible.id)
    await journaliser(
        ctx,
        action="deploiement.promotion",
        cible_type="deploiement",
        cible_id=nouveau.id,
        cible=version_cible(d.version),
    )
    await demarrer_travail(
        ctx,
        "app.deploy",
        nouveau.version,
        cible_type="deploiement",
        cible_id=nouveau.id,
        etapes=[
            {"nom": "Build de l'image", "dureeS": 4},
            {"nom": "Scan de vulnérabilités", "dureeS": 3},
            {"nom": "Provisionnement", "dureeS": 5},
            {"nom": "Déploiement", "dureeS": 6},
        ],
    )
    return await _deploiement_depuis_id(ctx, nouveau.id)


def version_cible(version: str) -> str:
    return version


@router.post(
    "/deploiements/{deploiementId}/rollback",
    response_model=m.Deploiement,
    status_code=status.HTTP_202_ACCEPTED,
    response_model_exclude_none=True,
)
async def annuler_deploiement(
    deploiementId: str,
    corps: m.DeploiementsDeploiementIdRollbackPostRequest,
    ctx: Contexte = Depends(exige("app.rollback")),
) -> Any:  # noqa: N803
    d = await depot_deploy.obtenir(ctx, deploiementId)
    precedents = await depot_deploy.tous(
        ctx, filtre=lambda x: x.envId == d.envId and x.statut == "live" and x.id != d.id
    )
    if not precedents:
        raise erreurs.conflit("Aucun déploiement stable à annuler.", code="rien_a_annuler")
    await journaliser(
        ctx,
        action="deploiement.rollback",
        cible_type="deploiement",
        cible_id=d.id,
        cible=corps.versionCible or d.version,
    )
    await depot_deploy.modifier(ctx, d.id, {"statut": "rolled_back"})
    return await _deploiement_depuis_id(ctx, d.id)


# ── dépôts ───────────────────────────────────────────────────────────────
@router.get(
    "/depots/branches", response_model=list[m.BrancheDepot], response_model_exclude_none=True
)
async def lister_branches_depot(
    provider: str | None = None,
    url: str | None = None,
    appId: str | None = None,
    ctx: Contexte = Depends(exige("app.deploy")),
) -> Any:  # noqa: N803
    dep = Depot("application", m.ApplicationPaas)
    if not url and appId:
        app = await dep.obtenir(ctx, appId)
        if app.repo:
            url = app.repo.url
            provider = provider or app.repo.provider
    if not url or not provider:
        raise erreurs.amont_indisponible("github", "Aucun dépôt à énumérer.", donnees_partielles=[])
    try:
        branches = service.depots().branches(provider, url)
    except erreurs.AppError as exc:
        if exc.statut == 424:
            raise erreurs.amont_indisponible(
                "github", str(exc.message), donnees_partielles=[]
            ) from None
        raise
    return [m.BrancheDepot(**b) for b in branches]
