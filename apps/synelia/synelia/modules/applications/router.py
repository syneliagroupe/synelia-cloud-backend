"""API expérimentale application/environnement/composant.

Le portail utilise le modèle ``projets``/``deploiements``. Ces routes restent
montées uniquement pour compatibilité du contrat et n'ont aucun appelant frontend.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, status
from synelia_contract import modeles as m
from synelia_kernel.dates import maintenant
from synelia_kernel.ids import nouvel_id

from synelia.audit import journaliser
from synelia.depot import Depot
from synelia.deps import Contexte, Page, exige, exiger_confirmation
from synelia.modules.applications import service
from synelia.modules.applications.service import depot_app, depot_comp, depot_env
from synelia.travaux import demarrer_travail

router = APIRouter(tags=["Applications PaaS"])

BRUITS_CANVAS = [
    {
        "id": "baas.bdd",
        "nom": "Base de données managée",
        "categorie": "Données",
        "image": "database",
        "teinte": "emeraude",
    },
    {
        "id": "baas.cache",
        "nom": "Cache Redis managé",
        "categorie": "Données",
        "image": "zap",
        "teinte": "rose",
    },
    {
        "id": "aai.auth",
        "nom": "Authentification / SSO",
        "categorie": "Sécurité",
        "image": "shield",
        "teinte": "indigo",
    },
    {
        "id": "api.gateway",
        "nom": "Passerelle API",
        "categorie": "Réseau",
        "image": "route",
        "teinte": "ambre",
    },
    {
        "id": "perf.impression",
        "nom": "Tracking / Analytics",
        "categorie": "Observabilité",
        "image": "activity",
        "teinte": "ciel",
    },
    {
        "id": "messagerie.email",
        "nom": "Email transactionnel",
        "categorie": "Communication",
        "image": "mail",
        "teinte": "violet",
    },
    {
        "id": "storage.objet",
        "nom": "Stockage objet S3",
        "categorie": "Stockage",
        "image": "archive",
        "teinte": "cyan",
    },
]


@router.get(
    "/applications", response_model=m.ApplicationsGetResponse, response_model_exclude_none=True
)
async def lister_applications(
    page: Page,
    espaceId: str | None = None,
    sante: str | None = None,
    ctx: Contexte = Depends(exige("org.dashboard.view", lecture=True)),
) -> Any:
    return await depot_app.lister(
        ctx,
        page,
        filtre=lambda a: (
            (not espaceId or a.espaceId == espaceId) and (not sante or a.sante == sante)
        ),
        tri_defaut="nom",
    )


@router.post(
    "/applications",
    response_model=m.TravailProvisioning,
    status_code=status.HTTP_202_ACCEPTED,
    response_model_exclude_none=True,
)
async def creer_application(
    corps: m.ApplicationPaasCreation, ctx: Contexte = Depends(exige("app.deploy"))
) -> Any:
    await depot_app.exiger_nom_libre(ctx, corps.nom)
    if corps.repo:
        await Depot("application", m.ApplicationPaas).exiger_nom_libre(
            ctx, corps.repo.url, parent_id=corps.espaceId
        )
    app = m.ApplicationPaas(
        id=nouvel_id(),
        espaceId=corps.espaceId,
        nom=corps.nom,
        source=corps.source,
        repo=m.Repo(provider=corps.repo.provider, url=corps.repo.url, branche=corps.repo.branche)
        if corps.repo
        else None,
        builder=corps.builder or ("image" if corps.source == "image" else "nixpacks"),
        cible=corps.cible,
        domainePrincipal=corps.domainePrincipal or f"{corps.nom}.synelia.app",
        sante="arrete",
        stack=[],
        dernierDeploiement=maintenant(),
        environnements=0,
        description=corps.description,
    )
    await depot_app.creer(ctx, app)
    etapes = [
        {"nom": "Analyser le dépôt", "dureeS": 3, "message": f"Builder {app.builder}"},
        {"nom": "Créer l'application dans Argo", "dureeS": 8},
        {"nom": "Configurer le domaine et la santé", "dureeS": 4},
    ]
    await journaliser(
        ctx, action="application.creation", cible_type="application", cible_id=app.id, cible=app.nom
    )
    retour = await demarrer_travail(
        ctx,
        "application.create",
        app.nom,
        cible_type="application",
        cible_id=app.id,
        entree=corps.model_dump(mode="json"),
        etapes=etapes,
        contexte={"source": corps.source},
    )
    return retour


@router.get(
    "/applications/{appId}", response_model=m.ApplicationPaas, response_model_exclude_none=True
)
async def obtenir_application(
    appId: str, ctx: Contexte = Depends(exige("org.dashboard.view", lecture=True))
) -> Any:  # noqa: N803
    return await depot_app.obtenir(ctx, appId)


@router.patch(
    "/applications/{appId}", response_model=m.ApplicationPaas, response_model_exclude_none=True
)
async def modifier_application(
    appId: str, corps: m.ApplicationPaasCreation, ctx: Contexte = Depends(exige("app.deploy"))
) -> Any:  # noqa: N803
    app = await depot_app.obtenir(ctx, appId)
    if corps.nom and corps.nom != app.nom:
        await depot_app.exiger_nom_libre(ctx, corps.nom)
    await depot_app.modifier(ctx, appId, corps)
    await journaliser(
        ctx,
        action="application.modification",
        cible_type="application",
        cible_id=appId,
        details=corps.model_dump(mode="json", exclude_none=True),
    )
    return await depot_app.obtenir(ctx, appId)


@router.delete(
    "/applications/{appId}",
    response_model=m.TravailProvisioning,
    status_code=status.HTTP_202_ACCEPTED,
    response_model_exclude_none=True,
)
async def supprimer_application(
    appId: str, confirmation: str | None = None, ctx: Contexte = Depends(exige("app.deploy"))
) -> Any:  # noqa: N803
    app = await depot_app.obtenir(ctx, appId)
    exiger_confirmation(app.nom, confirmation)
    await journaliser(
        ctx,
        action="application.suppression",
        cible_type="application",
        cible_id=appId,
        cible=app.nom,
    )
    return await demarrer_travail(
        ctx,
        "application.delete",
        app.nom,
        cible_type="application",
        cible_id=appId,
        etapes=[{"nom": "Supprimer l'application Argo", "dureeS": 5}],
    )


@router.post(
    "/applications/analyse-depot", response_model=m.AnalyseDepot, response_model_exclude_none=True
)
async def analyser_depot(
    corps: m.ApplicationsAnalyseDepotPostRequest, ctx: Contexte = Depends(exige("app.deploy"))
) -> Any:
    return service.analyser(ctx, corps)


# ── environnements ───────────────────────────────────────────────────────
async def _environnements_app(ctx: Contexte, app_id: str) -> list[m.Environnement]:
    return await depot_env.tous(ctx, parent_id=app_id)


def _env_par_app(ctx: Contexte, app_id: str):
    return depot_env.tous(ctx, parent_id=app_id)


@router.get(
    "/applications/{appId}/environnements",
    response_model=list[m.Environnement],
    response_model_exclude_none=True,
)
async def lister_environnements(appId: str, ctx: Contexte = Depends(exige(None))) -> Any:  # noqa: N803
    await depot_app.obtenir(ctx, appId)
    return await _environnements_app(ctx, appId)


@router.post(
    "/applications/{appId}/environnements",
    response_model=m.Environnement,
    status_code=status.HTTP_201_CREATED,
    response_model_exclude_none=True,
)
async def creer_environnement(
    appId: str, corps: m.EnvironnementCreation, ctx: Contexte = Depends(exige("app.deploy"))
) -> Any:  # noqa: N803
    app = await depot_app.obtenir(ctx, appId)
    await depot_env.exiger_nom_libre(ctx, corps.nom, parent_id=appId)
    env = m.Environnement(
        id=nouvel_id(),
        appId=appId,
        nom=corps.nom,
        domaines=corps.domaines or [f"{corps.nom}.{app.domainePrincipal}"],
        couleur=corps.couleur or "#6366f1",
        statut="building",
        autoDeploy=m.AutoDeploy(
            branche=corps.autoDeploy.branche, previewParPR=corps.autoDeploy.previewParPR
        )
        if corps.autoDeploy
        else None,
        protection=corps.protection,
        sante=service.SANTE_NULLE,
        strategie=corps.strategie,
        canari=corps.canari,
    )
    await depot_env.creer(ctx, env, parent_id=appId)
    if corps.copierDepuis:
        await _copier_variables(ctx, env.id, corps.copierDepuis, appId)
    await depot_app.modifier(
        ctx, appId, {"environnements": len(await _environnements_app(ctx, appId))}
    )
    await journaliser(
        ctx,
        action="environnement.creation",
        cible_type="environnement",
        cible_id=env.id,
        cible=env.nom,
    )
    return env


async def _copier_variables(ctx: Contexte, env_id: str, depuis: str, app_id: str) -> None:
    source = await depot_env.trouver(ctx, depuis, org_id=ctx.org_id)
    if source is None or source.appId != app_id:
        return
    liste = await _lire_variables_brutes(ctx, source.id)
    if liste is not None:
        await _ecrire_variables_brutes(ctx, env_id, liste)


@router.get(
    "/environnements/{envId}", response_model=m.Environnement, response_model_exclude_none=True
)
async def obtenir_environnement(envId: str, ctx: Contexte = Depends(exige(None))) -> Any:  # noqa: N803
    return await depot_env.obtenir(ctx, envId)


@router.patch(
    "/environnements/{envId}", response_model=m.Environnement, response_model_exclude_none=True
)
async def modifier_environnement(
    envId: str, corps: m.EnvironnementCreation, ctx: Contexte = Depends(exige("app.deploy"))
) -> Any:  # noqa: N803
    env = await depot_env.obtenir(ctx, envId)
    if corps.nom and corps.nom != env.nom:
        await depot_env.exiger_nom_libre(ctx, corps.nom, parent_id=env.appId)
    await depot_env.modifier(ctx, envId, corps)
    await journaliser(
        ctx,
        action="environnement.modification",
        cible_type="environnement",
        cible_id=envId,
        details=corps.model_dump(mode="json", exclude_none=True),
    )
    return await depot_env.obtenir(ctx, envId)


@router.delete(
    "/environnements/{envId}",
    response_model=m.TravailProvisioning,
    status_code=status.HTTP_202_ACCEPTED,
    response_model_exclude_none=True,
)
async def supprimer_environnement(
    envId: str, confirmation: str | None = None, ctx: Contexte = Depends(exige("app.deploy"))
) -> Any:  # noqa: N803
    env = await depot_env.obtenir(ctx, envId)
    exiger_confirmation(env.nom, confirmation)
    await journaliser(
        ctx,
        action="environnement.suppression",
        cible_type="environnement",
        cible_id=envId,
        cible=env.nom,
    )
    return await demarrer_travail(
        ctx,
        "environnement.delete",
        env.nom,
        cible_type="environnement",
        cible_id=envId,
        etapes=[{"nom": "Retirer l'environnement", "dureeS": 5}],
    )


# ── variables d'environnement ────────────────────────────────────────────
def _cle_variables() -> str:
    return "variables"


async def _lire_variables_brutes(ctx: Contexte, env_id: str) -> list[dict[str, Any]] | None:
    secrets = await Depot("environnement", m.Environnement).secrets(ctx, env_id)
    blob = secrets.get(_cle_variables())
    if not blob:
        return []
    return json.loads(blob)


async def _ecrire_variables_brutes(ctx: Contexte, env_id: str, liste: list[dict[str, Any]]) -> None:
    await Depot("environnement", m.Environnement).definir_secrets(
        ctx, env_id, {_cle_variables(): json.dumps(liste, ensure_ascii=False)}
    )


@router.get(
    "/environnements/{envId}/variables",
    response_model=list[m.VariableEnvironnement],
    response_model_exclude_none=True,
)
async def lister_variables_environnement(
    envId: str, ctx: Contexte = Depends(exige("secrets.update"))
) -> Any:  # noqa: N803
    await depot_env.obtenir(ctx, envId)
    brutes = await _lire_variables_brutes(ctx, envId)
    resultats = []
    for v in brutes:
        secret = bool(v.get("secret"))
        resultats.append(
            m.VariableEnvironnement(
                cle=v["cle"],
                valeur=("•••••" if secret else v.get("valeur")),
                secret=secret,
                scope=v["scope"],
            )
        )
    return resultats


@router.put(
    "/environnements/{envId}/variables",
    response_model=list[m.VariableEnvironnement],
    response_model_exclude_none=True,
)
async def modifier_variables_environnement(
    envId: str,
    corps: m.EnvironnementsEnvIdVariablesPutRequest,
    ctx: Contexte = Depends(exige("secrets.update")),
) -> Any:  # noqa: N803
    await depot_env.obtenir(ctx, envId)
    brutes = await _lire_variables_brutes(ctx, envId)
    par_cle = {v["cle"]: v for v in brutes}
    for var in corps.variables:
        if var.supprimer:
            par_cle.pop(var.cle, None)
            continue
        existant = par_cle.get(var.cle, {})
        valeur = (
            var.valeur
            if var.valeur is not None
            else (existant.get("valeur") if var.cle in par_cle else None)
        )
        par_cle[var.cle] = {
            "cle": var.cle,
            "valeur": valeur,
            "secret": var.secret if var.secret is not None else existant.get("secret", False),
            "scope": var.scope if var.scope is not None else existant.get("scope", "runtime"),
        }
    await _ecrire_variables_brutes(ctx, envId, list(par_cle.values()))
    await journaliser(
        ctx,
        action="environnement.variables",
        cible_type="environnement",
        cible_id=envId,
        details={"cles": [v.cle for v in corps.variables]},
    )
    return await lister_variables_environnement(envId, ctx=ctx)


# ── composants ───────────────────────────────────────────────────────────
@router.get(
    "/environnements/{envId}/composants",
    response_model=list[m.Composant],
    response_model_exclude_none=True,
)
async def lister_composants(envId: str, ctx: Contexte = Depends(exige(None))) -> Any:  # noqa: N803
    await depot_env.obtenir(ctx, envId)
    return await depot_comp.tous(ctx, parent_id=envId)


@router.post(
    "/environnements/{envId}/composants",
    response_model=m.TravailProvisioning,
    status_code=status.HTTP_202_ACCEPTED,
    response_model_exclude_none=True,
)
async def creer_composant(
    envId: str, corps: m.ComposantCreation, ctx: Contexte = Depends(exige("app.deploy"))
) -> Any:  # noqa: N803
    env = await depot_env.obtenir(ctx, envId)
    await depot_comp.exiger_nom_libre(ctx, corps.nom, parent_id=envId)
    comp = m.Composant(
        id=nouvel_id(),
        envId=envId,
        nom=corps.nom,
        kind=corps.kind,
        role=corps.role,
        image=corps.image,
        version=corps.version or "latest",
        ressources=m.Ressources(
            cpu=corps.ressources.cpu or 1.0,
            ramMo=corps.ressources.ramMo or 512,
            diskGo=corps.ressources.diskGo or 10,
        )
        if corps.ressources
        else m.Ressources(cpu=1.0, ramMo=512, diskGo=10),
        ports=[
            m.Port(interne=p.interne or 80, expose=p.expose, type=p.type or "ClusterIP")
            for p in corps.ports
        ]
        if corps.ports
        else [m.Port(interne=80, type="ClusterIP")],
        envVars=corps.envVars or [],
        storage=[
            m.StorageItem(chemin=s.chemin, tailleGo=s.tailleGo, classe=s.classe)
            for s in corps.storage
        ]
        if corps.storage
        else None,
        emplacement=m.Emplacement(namespace=f"ns-{env.nom}" if corps.kind == "k8s" else None),
        statut="stopped",
        dependances=corps.dependances,
    )
    await depot_comp.creer(ctx, comp, parent_id=envId)
    etapes = [
        {"nom": "Préparer l'image", "dureeS": 3},
        {"nom": "Déployer le composant", "dureeS": 8},
    ]
    await journaliser(
        ctx, action="composant.creation", cible_type="composant", cible_id=comp.id, cible=comp.nom
    )
    return await demarrer_travail(
        ctx,
        "composant.creer",
        comp.nom,
        cible_type="composant",
        cible_id=comp.id,
        entree=corps.model_dump(mode="json"),
        etapes=etapes,
        contexte={"env_id": envId},
    )


@router.get(
    "/composants/{composantId}", response_model=m.Composant, response_model_exclude_none=True
)
async def obtenir_composant(composantId: str, ctx: Contexte = Depends(exige(None))) -> Any:  # noqa: N803
    return await depot_comp.obtenir(ctx, composantId)


@router.patch(
    "/composants/{composantId}",
    response_model=m.TravailProvisioning,
    status_code=status.HTTP_202_ACCEPTED,
    response_model_exclude_none=True,
)
async def modifier_composant(
    composantId: str, corps: m.ComposantCreation, ctx: Contexte = Depends(exige("app.deploy"))
) -> Any:  # noqa: N803
    comp = await depot_comp.obtenir(ctx, composantId)
    if corps.nom and corps.nom != comp.nom:
        await depot_comp.exiger_nom_libre(ctx, corps.nom, parent_id=comp.envId)
    await depot_comp.modifier(ctx, composantId, corps)
    await journaliser(
        ctx,
        action="composant.modification",
        cible_type="composant",
        cible_id=composantId,
        details=corps.model_dump(mode="json", exclude_none=True),
    )
    return await demarrer_travail(
        ctx,
        "composant.modifier",
        comp.nom,
        cible_type="composant",
        cible_id=composantId,
        etapes=[{"nom": "Reconfigurer le composant", "dureeS": 5}],
    )


@router.delete(
    "/composants/{composantId}",
    response_model=m.TravailProvisioning,
    status_code=status.HTTP_202_ACCEPTED,
    response_model_exclude_none=True,
)
async def supprimer_composant(
    composantId: str, confirmation: str | None = None, ctx: Contexte = Depends(exige("app.deploy"))
) -> Any:  # noqa: N803
    comp = await depot_comp.obtenir(ctx, composantId)
    exiger_confirmation(comp.nom, confirmation)
    await journaliser(
        ctx,
        action="composant.suppression",
        cible_type="composant",
        cible_id=composantId,
        cible=comp.nom,
    )
    return await demarrer_travail(
        ctx,
        "composant.supprimer",
        comp.nom,
        cible_type="composant",
        cible_id=composantId,
        etapes=[{"nom": "Retirer le composant", "dureeS": 5}],
    )


@router.post(
    "/composants/{composantId}/arret",
    response_model=m.TravailProvisioning,
    status_code=status.HTTP_202_ACCEPTED,
    response_model_exclude_none=True,
)
async def arreter_composant(
    composantId: str, ctx: Contexte = Depends(exige("component.restart"))
) -> Any:  # noqa: N803
    comp = await depot_comp.obtenir(ctx, composantId)
    await journaliser(
        ctx, action="composant.arret", cible_type="composant", cible_id=composantId, cible=comp.nom
    )
    return await demarrer_travail(
        ctx,
        "composant.arret",
        comp.nom,
        cible_type="composant",
        cible_id=composantId,
        etapes=[{"nom": "Arrêter le composant", "dureeS": 4}],
    )


@router.post(
    "/composants/{composantId}/redemarrage",
    response_model=m.TravailProvisioning,
    status_code=status.HTTP_202_ACCEPTED,
    response_model_exclude_none=True,
)
async def redemarrer_composant(
    composantId: str, ctx: Contexte = Depends(exige("component.restart"))
) -> Any:  # noqa: N803
    comp = await depot_comp.obtenir(ctx, composantId)
    await journaliser(
        ctx,
        action="composant.redemarrage",
        cible_type="composant",
        cible_id=composantId,
        cible=comp.nom,
    )
    return await demarrer_travail(
        ctx,
        "composant.redemarrage",
        comp.nom,
        cible_type="composant",
        cible_id=composantId,
        etapes=[{"nom": "Redémarrer le composant", "dureeS": 6}],
    )


@router.post(
    "/composants/{composantId}/dimensionnement",
    response_model=m.TravailProvisioning,
    status_code=status.HTTP_202_ACCEPTED,
    response_model_exclude_none=True,
)
async def dimensionner_composant(
    composantId: str,
    corps: m.ComposantsComposantIdDimensionnementPostRequest,
    ctx: Contexte = Depends(exige("app.deploy")),
) -> Any:  # noqa: N803
    comp = await depot_comp.obtenir(ctx, composantId)
    await journaliser(
        ctx,
        action="composant.dimensionnement",
        cible_type="composant",
        cible_id=composantId,
        details=corps.model_dump(mode="json", exclude_none=True),
    )
    return await demarrer_travail(
        ctx,
        "composant.dimensionnement",
        comp.nom,
        cible_type="composant",
        cible_id=composantId,
        etapes=[{"nom": "Redimensionner", "dureeS": 3}, {"nom": "Appliquer", "dureeS": 7}],
        contexte={"demandes": corps.model_dump(mode="json", exclude_none=True)},
    )


# ── canvas ───────────────────────────────────────────────────────────────
@router.get(
    "/canvas/briques", response_model=list[m.BriqueCanvas], response_model_exclude_none=True
)
async def lister_briques_canvas(ctx: Contexte = Depends(exige(None))) -> Any:
    return [m.BriqueCanvas(**b) for b in BRUITS_CANVAS]
