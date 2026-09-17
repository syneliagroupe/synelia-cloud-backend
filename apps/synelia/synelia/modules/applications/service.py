from __future__ import annotations

import asyncio

from synelia_contract import modeles as m
from synelia_db.modeles import Travail
from synelia_openstack import fournisseur
from synelia_openstack.k8s_workload import obtenir as k8s
from synelia_openstack.plateforme_k8s import DepotsReel, DepotsSimule

from synelia.depot import Depot
from synelia.deps.contexte import Contexte
from synelia.travaux import Executeur, executeur

depot_app = Depot(
    "application", m.ApplicationPaas, champ_nom="nom", champs_recherche=("nom", "domainePrincipal")
)
depot_env = Depot("environnement", m.Environnement, champ_nom="nom")
depot_comp = Depot("composant", m.Composant, champ_nom="nom")


def depots() -> DepotsSimule:
    return fournisseur(DepotsSimule, DepotsReel)


async def _appliquer_composant_k8s(comp: m.Composant, replicas: int = 1) -> None:
    """Applique un `Composant` de kind `k8s` sur le cluster PaaS (namespace = environnement).

    `k8s_client` est synchrone/bloquant : exécuté tel quel dans la coroutine, il bloquerait
    toute la boucle asyncio — donc toute l'API, pour tous les tenants — jusqu'à sa fin (même
    bug vécu en direct et corrigé dans `synelia.modules.vms.service` via `asyncio.to_thread`
    systématique sur les appels amont ; même garde ici et dans `projets.service`).
    """
    if comp.kind != "k8s" or not comp.emplacement.namespace:
        return
    a = k8s()
    await asyncio.to_thread(a.creer_namespace, comp.emplacement.namespace)
    env = {v.cle: v.valeur for v in comp.envVars if not v.secret and v.valeur is not None}
    ports = [p.interne for p in comp.ports] or None
    await asyncio.to_thread(
        a.appliquer_deployment,
        comp.emplacement.namespace,
        comp.nom,
        comp.image,
        replicas=replicas,
        env=env,
        ports=ports,
    )


async def _supprimer_composant_k8s(comp: m.Composant) -> None:
    if comp.kind != "k8s" or not comp.emplacement.namespace:
        return
    await asyncio.to_thread(k8s().supprimer_deployment, comp.emplacement.namespace, comp.nom)


SANTE_NULLE = m.Sante(cpu=0.0, ram=0.0, latenceMs=0.0, erreursPct=0.0)


def analyser(ctx: Contexte, corps: m.ApplicationsAnalyseDepotPostRequest) -> m.AnalyseDepot:
    d = depots().analyser(corps.provider, corps.url, corps.branche)
    return m.AnalyseDepot(
        depot=corps.url,
        branche=corps.branche or "main",
        commit=None,
        constats=[
            m.Constat(
                fichier=".<racine>",
                constat=f"Framework pressenti : {d['framework'] or 'inconnu'}.",
                niveau="info",
            )
        ],
        builderPropose=d["builder"],
        ciblePropose=d["cible"],
        servicesDetectes=None,
        variablesRequises=None,
    )


# ── travaux applicatifs ────────────────────────────────────────────────
@executeur("application.create")
class ExecuteurAppCreate(Executeur):
    async def terminer(self, ctx: Contexte, travail: Travail) -> None:
        app = await depot_app.obtenir(ctx, travail.cible_id or "")
        await depot_app.modifier(
            ctx, app.id, {"sante": "sain", "environnements": app.environnements}
        )


@executeur("application.delete")
class ExecuteurAppDelete(Executeur):
    async def terminer(self, ctx: Contexte, travail: Travail) -> None:
        app = await depot_app.obtenir(ctx, travail.cible_id or "")
        await depot_app.supprimer(ctx, app.id, logique=True)


@executeur("environnement.delete")
class ExecuteurEnvDelete(Executeur):
    async def terminer(self, ctx: Contexte, travail: Travail) -> None:
        await depot_env.supprimer(ctx, travail.cible_id or "", logique=True)


@executeur("composant.creer")
class ExecuteurComposantCreer(Executeur):
    async def terminer(self, ctx: Contexte, travail: Travail) -> None:
        comp = await depot_comp.obtenir(ctx, travail.cible_id or "")
        await _appliquer_composant_k8s(comp)
        await depot_comp.definir_statut(ctx, travail.cible_id or "", "deployed")
        await depot_env.definir_statut(ctx, travail.contexte.get("env_id") or "", "running")


@executeur("composant.modifier")
class ExecuteurComposantModifier(Executeur):
    compensable = True

    async def terminer(self, ctx: Contexte, travail: Travail) -> None:
        comp = await depot_comp.obtenir(ctx, travail.cible_id or "")
        await _appliquer_composant_k8s(comp)


@executeur("composant.supprimer")
class ExecuteurComposantSupprimer(Executeur):
    async def terminer(self, ctx: Contexte, travail: Travail) -> None:
        comp = await depot_comp.obtenir(ctx, travail.cible_id or "")
        await _supprimer_composant_k8s(comp)
        await depot_comp.supprimer(ctx, travail.cible_id or "", logique=True)


@executeur("composant.arret")
class ExecuteurComposantArret(Executeur):
    async def terminer(self, ctx: Contexte, travail: Travail) -> None:
        comp = await depot_comp.obtenir(ctx, travail.cible_id or "")
        await _appliquer_composant_k8s(comp, replicas=0)
        await depot_comp.definir_statut(ctx, travail.cible_id or "", "stopped")


@executeur("composant.redemarrage")
class ExecuteurComposantRedemarrage(Executeur):
    async def terminer(self, ctx: Contexte, travail: Travail) -> None:
        comp = await depot_comp.obtenir(ctx, travail.cible_id or "")
        await _appliquer_composant_k8s(comp)
        await depot_comp.definir_statut(ctx, travail.cible_id or "", "deployed")


@executeur("composant.dimensionnement")
class ExecuteurComposantDimensionnement(Executeur):
    async def etape(self, ctx: Contexte, travail: Travail, index: int, nom: str) -> str | None:
        c = await depot_comp.obtenir(ctx, travail.cible_id or "")
        demandes = dict(travail.contexte).get("demandes", {})
        if index == 0:
            return f"Redimensionnement de {c.nom} vers {demandes.get('replicas', '—')} réplica(s)"
        if index == 1:
            return f"Application cpu={demandes.get('cpu')} · ram={demandes.get('ramMo')} Mo · disk={demandes.get('diskGo')} Go"
        return None

    async def terminer(self, ctx: Contexte, travail: Travail) -> None:
        demandes = dict(travail.contexte).get("demandes", {})
        c = await depot_comp.obtenir(ctx, travail.cible_id or "")
        ressources = c.ressources.model_copy(
            update={
                "cpu": demandes.get("cpu", c.ressources.cpu),
                "ramMo": demandes.get("ramMo", c.ressources.ramMo),
                "diskGo": demandes.get("diskGo", c.ressources.diskGo),
            }
        )
        await depot_comp.modifier(ctx, c.id, {"ressources": ressources.model_dump()})
        await _appliquer_composant_k8s(
            c.model_copy(update={"ressources": ressources}), replicas=demandes.get("replicas") or 1
        )
        await depot_comp.definir_statut(ctx, c.id, "deployed")
