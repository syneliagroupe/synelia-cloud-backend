from __future__ import annotations

from typing import Any

from synelia_contract import modeles as m
from synelia_db.modeles import Travail
from synelia_openstack import fournisseur
from synelia_openstack.plateforme_k8s import DepotsReel, DepotsSimule

from synelia.depot import Depot
from synelia.deps.contexte import Contexte
from synelia.travaux import Executeur, executeur

depot_deploy = Depot(
    "deploiement",
    m.Deploiement,
    champ_nom="version",
    champs_recherche=("version", "branche", "envNom"),
)
depot_env = Depot("environnement", m.Environnement)
depot_app = Depot("application", m.ApplicationPaas)

_CLE_APPROBATION = "approbation_requise"
_CLE_LIVE = "aete_live"


def depots() -> DepotsSimule:
    return fournisseur(DepotsSimule, DepotsReel)


async def _drapeau(ctx: Contexte, deploiement_id: str, cle: str) -> bool:
    secrets = await depot_deploy.secrets(ctx, deploiement_id)
    return secrets.get(cle) == "1"


async def _poser_drapeau(ctx: Contexte, deploiement_id: str, cle: str, valeur: bool) -> None:
    await depot_deploy.definir_secrets(ctx, deploiement_id, {cle: "1" if valeur else "0"})


async def _a_jour_locaux(
    ctx: Contexte, d: m.Deploiement, statut: str | None = None
) -> m.Deploiement:
    patch: dict[str, Any] = {}
    if statut:
        patch["statut"] = statut
    etapes = []
    for e in d.etapes:
        f = e.model_copy()
        if statut == "live":
            f.statut = "ok"
        elif e.nom == "build":
            f.statut = (
                "running"
                if statut in {"building", "scanning", "provisioning", "deploying"}
                else "pending"
            )
        elif e.nom in {"scan", "provision"} and statut in {"scanning", "provisioning", "deploying"}:
            f.statut = "ok"
        elif e.nom == "deploy" and statut == "deploying":
            f.statut = "running"
        etapes.append(f)
    patch["etapes"] = [e.model_dump(mode="json") for e in etapes]
    patch["dureeS"] = d.dureeS or 0
    return await depot_deploy.modifier(ctx, d.id, patch)


@executeur("app.deploy")
class ExecuteurAppDeploy(Executeur):
    async def etape(self, ctx: Contexte, travail: Travail, index: int, nom: str) -> str | None:
        d = await depot_deploy.obtenir(ctx, travail.cible_id or "")
        status = ["building", "scanning", "provisioning", "deploying"][index]
        await _a_jour_locaux(ctx, d, status)
        if index == 1:
            return "Analyse des vulnérabilités terminée, aucun correctif bloquant"
        return None

    async def terminer(self, ctx: Contexte, travail: Travail) -> None:
        d = await depot_deploy.obtenir(ctx, travail.cible_id or "")
        finale = await _a_jour_locaux(ctx, d, "live")
        await _poser_drapeau(ctx, d.id, _CLE_LIVE, True)
        preview = None
        if d.branche and d.branche != "main":
            preview = f"https://{d.branche}.{finale.appId}.synelia.app"
        await depot_deploy.modifier(
            ctx, d.id, {"statut": "live", "previewUrl": preview, "dureeS": 12}
        )


@executeur("app.rollback")
class ExecuteurAppRollback(Executeur):
    async def terminer(self, ctx: Contexte, travail: Travail) -> None:
        await depot_deploy.modifier(ctx, travail.cible_id or "", {"statut": "rolled_back"})
