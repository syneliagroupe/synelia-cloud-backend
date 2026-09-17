from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, status
from synelia_contract import modeles as m
from synelia_kernel.ids import nouvel_id

from synelia.audit import journaliser
from synelia.deps import Contexte, Page, exige, exiger_confirmation
from synelia.modules.bases import service
from synelia.modules.bases.service import PORTS, depot
from synelia.travaux import demarrer_travail

router = APIRouter(prefix="/bases", tags=["Bases managées"])


@router.get("", response_model=m.BasesGetResponse, response_model_exclude_none=True)
async def lister_bases_managees(
    page: Page,
    espaceId: str | None = None,
    moteur: str | None = None,
    statut: str | None = None,
    ctx: Contexte = Depends(exige("org.dashboard.view", lecture=True)),
) -> Any:
    return await depot.lister(
        ctx,
        page,
        filtre=lambda b: (
            (not espaceId or b.espaceId == espaceId)
            and (not moteur or b.moteur == moteur)
            and (not statut or b.statut == statut)
        ),
        tri_defaut="nom",
    )


@router.post(
    "",
    response_model=m.TravailProvisioning,
    status_code=status.HTTP_202_ACCEPTED,
    response_model_exclude_none=True,
)
async def creer_base_managee(
    corps: m.BaseManageeCreation, ctx: Contexte = Depends(exige("vm.create_delete"))
) -> Any:
    await depot.exiger_nom_libre(ctx, corps.nom)
    identifiants = service.generer_identifiants(corps.moteur)
    base = m.BaseManagee(
        id=nouvel_id(),
        espaceId=corps.espaceId,
        nom=corps.nom,
        moteur=corps.moteur,
        version=corps.version,
        palier=corps.palier,
        ha=bool(corps.ha),
        tailleGo=corps.tailleGo or 20,
        connexions=m.Connexions(actives=0, max=_max_connexions(corps.palier)),
        replicas=corps.replicas or 0,
        statut="maintenance",
        pitr=bool(corps.pitr),
        host="",
    )
    await depot.creer(
        ctx,
        base,
        secrets={
            "utilisateur": identifiants["utilisateur"],
            "mot_de_passe": identifiants["mot_de_passe"],
        },
    )
    await journaliser(
        ctx,
        action="base.creation",
        cible_type="base_managee",
        cible_id=base.id,
        cible=base.nom,
        details=corps.model_dump(mode="json"),
    )
    return await demarrer_travail(
        ctx,
        "base.create",
        base.nom,
        cible_type="base_managee",
        cible_id=base.id,
        entree=corps.model_dump(mode="json"),
        etapes=[
            {"nom": f"Provisionner l'instance {corps.moteur}", "dureeS": 45},
            {"nom": "Appliquer la configuration", "dureeS": 15},
            {"nom": "Activer les sauvegardes", "dureeS": 8},
        ],
    )


def _max_connexions(palier: str) -> int:
    return {"s1": 25, "s2": 50, "m1": 100, "m2": 200, "l1": 500, "xl1": 1000}.get(palier, 100)


@router.get("/{baseId}", response_model=m.BaseManagee, response_model_exclude_none=True)
async def obtenir_base_managee(
    baseId: str, ctx: Contexte = Depends(exige("org.dashboard.view", lecture=True))
) -> Any:  # noqa: N803
    return await depot.obtenir(ctx, baseId)


@router.patch("/{baseId}", response_model=m.BaseManagee, response_model_exclude_none=True)
async def modifier_base_managee(
    baseId: str, corps: m.BaseManageeCreation, ctx: Contexte = Depends(exige("vm.create_delete"))
) -> Any:  # noqa: N803
    await depot.obtenir(ctx, baseId)
    await depot.modifier(ctx, baseId, corps.model_dump(exclude_none=True))
    await journaliser(
        ctx,
        action="base.modification",
        cible_type="base_managee",
        cible_id=baseId,
        details=corps.model_dump(mode="json", exclude_none=True),
    )
    return await depot.obtenir(ctx, baseId)


@router.delete(
    "/{baseId}",
    response_model=m.TravailProvisioning,
    status_code=status.HTTP_202_ACCEPTED,
    response_model_exclude_none=True,
)
async def supprimer_base_managee(
    baseId: str, confirmation: str | None = None, ctx: Contexte = Depends(exige("vm.create_delete"))
) -> Any:  # noqa: N803
    base = await depot.obtenir(ctx, baseId)
    exiger_confirmation(base.nom, confirmation)
    await journaliser(
        ctx, action="base.suppression", cible_type="base_managee", cible_id=baseId, cible=base.nom
    )
    return await demarrer_travail(
        ctx,
        "base.delete",
        base.nom,
        cible_type="base_managee",
        cible_id=baseId,
        etapes=[
            {"nom": "Sauvegarder puis supprimer l'instance", "dureeS": 30},
            {"nom": "Libérer les ressources", "dureeS": 6},
        ],
    )


@router.get(
    "/{baseId}/identifiants",
    response_model=m.BasesBaseIdIdentifiantsGetResponse,
    response_model_exclude_none=True,
)
async def obtenir_identifiants_base(
    baseId: str, ctx: Contexte = Depends(exige("secrets.update"))
) -> Any:  # noqa: N803
    base = await depot.obtenir(ctx, baseId)
    secrets = await depot.secrets(ctx, baseId)
    port = PORTS.get(base.moteur, 5432)
    uri = _uri(base, secrets.get("utilisateur", ""), port)
    return m.BasesBaseIdIdentifiantsGetResponse(
        host=base.host,
        port=port,
        utilisateur=secrets.get("utilisateur", ""),
        base=base.nom,
        uri=uri,
    )


def _uri(base: m.BaseManagee, utilisateur: str, port: int) -> str:
    scheme = {
        "postgresql": "postgresql",
        "mysql": "mysql",
        "mariadb": "mysql",
        "mongodb": "mongodb",
        "redis": "redis",
    }.get(base.moteur, "postgresql")
    return f"{scheme}://{utilisateur}:***@{base.host}:{port}/{base.nom}"


@router.post(
    "/{baseId}/identifiants/rotation",
    response_model=m.BasesBaseIdIdentifiantsRotationPostResponse,
    response_model_exclude_none=True,
)
async def rotationner_identifiants_base(
    baseId: str,
    corps: m.BasesBaseIdIdentifiantsRotationPostRequest,
    ctx: Contexte = Depends(exige("secrets.update")),
) -> Any:  # noqa: N803
    # Rotation locale du secret : génère et stocke un nouveau mot de passe réel. Le moteur
    # tournant dans le conteneur Docker de la VM garde l'ancien tant qu'il n'est pas mis à jour
    # via une exécution distante (hors périmètre ici, pas d'accès SSH/exec depuis l'API).
    base = await depot.obtenir(ctx, baseId)
    secrets_actuels = await depot.secrets(ctx, baseId)
    utilisateur = secrets_actuels.get("utilisateur") or service.utilisateur_pour_moteur(base.moteur)
    nouveau_mdp = service.nouveau_mot_de_passe()
    await depot.definir_secrets(ctx, baseId, {"mot_de_passe": nouveau_mdp})
    await journaliser(
        ctx,
        action="base.identifiants.rotation",
        cible_type="base_managee",
        cible_id=baseId,
        cible=base.nom,
    )
    return m.BasesBaseIdIdentifiantsRotationPostResponse(
        host=base.host,
        port=PORTS.get(base.moteur, 5432),
        utilisateur=utilisateur,
        motDePasse=nouveau_mdp,
    )


@router.get(
    "/{baseId}/metriques",
    response_model=m.BasesBaseIdMetriquesGetResponse,
    response_model_exclude_none=True,
)
async def obtenir_metriques_base(
    baseId: str, fenetre: str | None = None, ctx: Contexte = Depends(exige(None))
) -> Any:  # noqa: N803
    await depot.obtenir(ctx, baseId)
    return m.BasesBaseIdMetriquesGetResponse(series=[])


@router.post(
    "/{baseId}/replicas",
    response_model=m.TravailProvisioning,
    status_code=status.HTTP_202_ACCEPTED,
    response_model_exclude_none=True,
)
async def ajouter_replica_base(
    baseId: str,
    corps: m.BasesBaseIdReplicasPostRequest,
    ctx: Contexte = Depends(exige("vm.create_delete")),
) -> Any:  # noqa: N803
    base = await depot.obtenir(ctx, baseId)
    await journaliser(
        ctx,
        action="base.replica",
        cible_type="base_managee",
        cible_id=baseId,
        cible=base.nom,
        details=corps.model_dump(mode="json"),
    )
    return await demarrer_travail(
        ctx,
        "base.replica",
        base.nom,
        cible_type="base_managee",
        cible_id=baseId,
        entree=corps.model_dump(mode="json"),
        contexte={"replicas": base.replicas + 1},
        etapes=[
            {"nom": "Provisionner le réplica de lecture", "dureeS": 60},
            {"nom": "Synchroniser les données", "dureeS": 40},
        ],
    )


@router.post(
    "/{baseId}/restauration",
    response_model=m.TravailProvisioning,
    status_code=status.HTTP_202_ACCEPTED,
    response_model_exclude_none=True,
)
async def restaurer_base_dans_le_temps(
    baseId: str,
    corps: m.BasesBaseIdRestaurationPostRequest,
    ctx: Contexte = Depends(exige("backup.restore")),
) -> Any:  # noqa: N803
    base = await depot.obtenir(ctx, baseId)
    await journaliser(
        ctx,
        action="base.restauration",
        cible_type="base_managee",
        cible_id=baseId,
        cible=base.nom,
        details=corps.model_dump(mode="json"),
    )
    return await demarrer_travail(
        ctx,
        "base.restore",
        corps.nomCible,
        cible_type="base_managee",
        cible_id=baseId,
        entree=corps.model_dump(mode="json"),
        etapes=[
            {"nom": "Restaurer l'instantané", "dureeS": 90},
            {"nom": "Vérifier l'intégrité", "dureeS": 20},
        ],
    )
