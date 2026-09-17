from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from synelia_contract import modeles as m
from synelia_db.modeles import Ressource, Travail
from synelia_kernel import erreurs
from synelia_kernel.config import reglages
from synelia_kernel.journal import journal
from synelia_openstack import fournisseur
from synelia_openstack.identite import IdentiteOpenStack, IdentiteSimule

from synelia.depot import Depot
from synelia.deps.contexte import Contexte
from synelia.travaux import Executeur, demarrer_travail, executeur

log = journal("espaces")

depot = Depot(
    "espace",
    m.EspaceCloud,
    libelle="Espace Cloud",
    champ_nom="code",
    champs_recherche=("code", "offreNom", "site"),
)

# Même type/table que `depot` ci-dessus, mais sans filtre d'organisation (`org_id IS NULL`,
# la convention "plateforme", cf. docstring de `Ressource`) : réservé à un accès par id fixe
# connu du serveur — **jamais** un id fourni par un client, ce serait un contournement de
# l'isolation par organisation. N'existe que pour la zone VPS partagée, cf. `semer_zone_vps`
# et `web_hebergement.service.zone_vps_secrets`.
depot_plateforme = Depot(
    "espace",
    m.EspaceCloud,
    plateforme=True,
    libelle="Espace Cloud",
    champ_nom="code",
    champs_recherche=("code", "offreNom", "site"),
)

# Id fixe et stable de l'unique Espace Cloud partagé (réseau privé + load balancer Octavia
# public) que consomment `web_hebergement.zone_vps_secrets()` et les services `projets` en
# cible `vm` — c'est l'id réel déjà provisionné sur le lab (voir `docs/runbooks/lab-openstack.md`
# et la mémoire de session « infra-universe-real-vs-simulated »). Utilisé par `semer_zone_vps`
# en repli quand `SYNELIA_VPS_ZONE_ESPACE_ID` n'est pas positionné, pour retomber
# systématiquement sur le même Espace plutôt que d'en provisionner un second pour de vrai.
ESPACE_ZONE_VPS_ID = "01a072e1-e303-76c2-bb8a-c3142f1e8f01"


def amont() -> IdentiteSimule:
    return fournisseur(IdentiteSimule, IdentiteOpenStack)


async def usage(ctx: Contexte, espace_id: str) -> dict[str, float]:
    vms = await Depot("vm", m.Vm).tous(
        ctx, filtre=lambda v: v.espaceId == espace_id and v.statut != "error"
    )
    volumes = await Depot("volume", m.Volume).tous(
        ctx, filtre=lambda v: getattr(v, "espaceId", None) == espace_id
    )
    return {
        "vcpu": sum(v.vcpu for v in vms),
        "ramGo": sum(v.ramGo for v in vms),
        "stockageTo": round(
            (sum(v.diskGo for v in vms) + sum(getattr(v, "tailleGo", 0) or 0 for v in volumes))
            / 1024,
            2,
        ),
    }


async def verifier_quota(
    ctx: Contexte, espace_id: str, vcpu: int = 0, ram_go: int = 0, disk_go: int = 0
) -> m.EspaceCloud:
    e = await depot.obtenir(ctx, espace_id)
    u = await usage(ctx, espace_id)
    if (
        u["vcpu"] + vcpu > e.quota.vcpu
        or u["ramGo"] + ram_go > e.quota.ramGo
        or u["stockageTo"] + disk_go / 1024 > e.quota.stockageTo
    ):
        raise erreurs.quota_depasse(
            "Le quota de l'Espace Cloud est atteint.",
            detail=f"usage={u} quota={e.quota.model_dump()}",
        )
    return e


@executeur("espace.create")
class ExecuteurEspaceCreate(Executeur):
    compensable = True

    async def etape(self, ctx: Contexte, travail: Travail, index: int, nom: str) -> str | None:
        e = await depot.obtenir(ctx, travail.cible_id or "")
        a = amont()
        c = dict(travail.contexte)
        if index == 0:
            return f"Quota réservé : {e.quota.vcpu} vCPU, {e.quota.ramGo} Go RAM"
        if index == 1:
            # `a.xxx(...)` (openstacksdk, synchrone) est déchargé via `asyncio.to_thread` : même
            # garde que `vms.service`, sans quoi un appel amont lent gèlerait la boucle asyncio
            # — donc l'API entière, tous tenants confondus.
            c["domaine_id"] = c.get("domaine_id") or await asyncio.to_thread(
                a.creer_domaine, f"org-{e.orgId}"
            )
            c["projet_id"] = await asyncio.to_thread(
                a.creer_projet,
                c["domaine_id"],
                f"espace-{e.code}",
                "RegionOne" if e.site == "ABJ" else "GBM",
            )
            await asyncio.to_thread(
                a.poser_quotas, c["projet_id"], e.quota.vcpu, e.quota.ramGo, e.quota.stockageTo
            )
        elif index == 2:
            c.update(await asyncio.to_thread(a.creer_reseau, c["projet_id"], f"{e.code}-net", e.cidr))
            await depot.definir_secrets(
                ctx,
                e.id,
                {
                    "projet_id": c["projet_id"],
                    "reseau_id": c["reseau_id"],
                    "routeur_id": c["routeur_id"],
                },
            )
        elif index == 3:
            ac = await asyncio.to_thread(
                a.creer_application_credential, c["projet_id"], c.get("domaine_id")
            )
            await depot.definir_secrets(
                ctx,
                e.id,
                {
                    "application_credential_id": ac["id"],
                    "application_credential_secret": ac["secret"],
                },
            )
        travail.contexte = c
        return None

    async def compenser(self, ctx: Contexte, travail: Travail, index_echoue: int) -> None:
        pid = travail.contexte.get("projet_id")
        if pid:
            await asyncio.to_thread(amont().supprimer_projet, pid)
        await depot.definir_statut(ctx, travail.cible_id or "", "suspendue")

    async def terminer(self, ctx: Contexte, travail: Travail) -> None:
        await depot.definir_statut(ctx, travail.cible_id or "", "active")


@executeur("espace.delete")
class ExecuteurEspaceDelete(Executeur):
    async def terminer(self, ctx: Contexte, travail: Travail) -> None:
        # Le projet/réseau amont est posé en secrets par ExecuteurEspaceCreate, pas dans
        # `travail.contexte` (qui n'existe que pour ce job-ci, vide pour un `espace.delete`).
        secrets = await depot.secrets(ctx, travail.cible_id or "")
        pid = secrets.get("projet_id")
        if pid:
            rid, rtid = secrets.get("reseau_id"), secrets.get("routeur_id")
            if rid and rtid:
                await asyncio.to_thread(amont().supprimer_reseau, rid, rtid)
            await asyncio.to_thread(amont().supprimer_projet, pid)
        await depot.supprimer(ctx, travail.cible_id or "", logique=True)


async def semer_zone_vps(session: AsyncSession) -> None:
    """Garantit l'existence réelle de l'Espace Cloud partagé de la zone VPS (réseau + LB
    Octavia public, cf. `web_hebergement.zone_vps_secrets`), sans dépendre d'un bootstrap
    manuel one-shot. Appelé à chaque démarrage depuis `synelia.amorcage.amorcer()`,
    indépendamment de `SYNELIA_SEED_DEMO` — même précédent que
    `admin_catalogue.semer_catalogue_reel` (« Catalogue plateforme réel »).

    Idempotent :
    - la ligne existe déjà et porte encore un `org_id` (bootstrap manuel historique, ou
      environnement fraîchement migré) → on ne fait que basculer la colonne de portée sur
      `NULL` (convention « plateforme », cf. docstring de `Ressource`) pour qu'elle cesse
      d'apparaître dans les listes/API clientes. On ne touche à rien côté OpenStack : c'est
      la même infra réelle (réseau, LB, projet) qui continue de tourner, seule la portée
      applicative change.
    - la ligne existe déjà et est déjà `org_id NULL` → rien à faire.
    - la ligne est absente (nouvel environnement / reprise après sinistre) → provisionnée
      pour de vrai avec le même exécuteur que la création normale d'un Espace Cloud
      (`ExecuteurEspaceCreate`), jamais un second chemin de provisioning."""
    r = reglages()
    espace_id = r.vps_zone_espace_id or ESPACE_ZONE_VPS_ID
    ligne = await session.get(Ressource, espace_id)
    if ligne is not None and ligne.type == "espace" and ligne.supprime_le is None:
        if ligne.org_id is not None:
            ligne.org_id = None
            await session.flush()
            log.info("zone_vps.bascule_plateforme", espace_id=espace_id)
        return
    if not r.vps_zone_org_id:
        # Pas d'organisation admin configurée (tests, environnement sans zone VPS) : rien de
        # sûr à provisionner. `zone_vps_secrets()` continue de se comporter comme avant
        # (retourne `{}`) tant que `SYNELIA_VPS_ZONE_ESPACE_ID` n'est pas non plus positionné.
        return
    await _provisionner_zone_vps(session, espace_id, r.vps_zone_org_id)


async def _provisionner_zone_vps(session: AsyncSession, espace_id: str, org_id: str) -> None:
    """Reprise après sinistre / nouvel environnement seulement — sur ce lab la ligne existe
    déjà, cette branche ne s'exécute jamais. Crée l'Espace normalement, scellé par `org_id`
    comme n'importe quelle création cliente (`espaces.router.creer_espace`), pour que les
    étapes internes de `ExecuteurEspaceCreate` (lues via le dépôt client scellé par
    organisation) le retrouvent ; exécute le job réel `espace.create` **en ligne** — jamais
    détaché, la bascule vers `org_id NULL` juste après ne doit jamais arriver avant que la
    dernière étape n'ait fini de relire la ligne par organisation — puis bascule enfin la
    ligne sur la convention plateforme, comme `semer_zone_vps` le fait pour une ligne
    préexistante."""
    import os
    from types import SimpleNamespace

    from synelia_kernel.dates import maintenant

    from synelia.deps.contexte import Contexte as _Contexte
    from synelia.deps.contexte import Principal

    r = reglages()
    faux_request: Any = SimpleNamespace(
        headers={}, client=None, state=SimpleNamespace(correlation_id="amorcage-zone-vps")
    )
    ctx = _Contexte(
        request=faux_request,
        session=session,
        reglages=r,
        correlation_id="amorcage-zone-vps",
        principal=Principal(
            utilisateur_id=None,
            email="amorcage@synelia.cloud",
            nom="Amorçage plateforme",
            org_id=org_id,
            role="platform_operator",
            equipe=True,
            role_equipe="platform_operator",
        ),
    )
    espace = m.EspaceCloud(
        id=espace_id,
        orgId=org_id,
        code="vps-zone",
        offerId="",
        site="ABJ",
        cidr="10.90.0.0/22",
        quota=m.Quota(vcpu=64, ramGo=128, stockageTo=5.0),
        usage=m.Quota(vcpu=0, ramGo=0, stockageTo=0.0),
        projets=0,
        statut="provisioning",
        createdAt=maintenant(),
        dnsInterne=None,
    )
    await depot.creer(ctx, espace, org_id=org_id, id_=espace_id)
    await session.flush()
    # Force l'exécution en ligne (comme en test/Vercel) le temps de ce seul appel : ce
    # bootstrap doit se terminer avant qu'on bascule la ligne sur `org_id NULL`, pas partir en
    # tâche de fond détachée qui pourrait encore lire la ligne par organisation après coup.
    ancienne = os.environ.get("SYNELIA_TRAVAUX_EN_LIGNE")
    os.environ["SYNELIA_TRAVAUX_EN_LIGNE"] = "1"
    try:
        resultat = await demarrer_travail(
            ctx, "espace.create", espace.code, cible_type="espace", cible_id=espace_id, entree={}
        )
    finally:
        if ancienne is None:
            os.environ.pop("SYNELIA_TRAVAUX_EN_LIGNE", None)
        else:
            os.environ["SYNELIA_TRAVAUX_EN_LIGNE"] = ancienne
    if resultat.get("statut") != "done":
        log.error("zone_vps.provisioning_echoue", espace_id=espace_id, statut=resultat.get("statut"))
        return
    ligne = await session.get(Ressource, espace_id)
    if ligne is not None:
        ligne.org_id = None
        await session.flush()
    log.info("zone_vps.provisionnee", espace_id=espace_id)
    # `ExecuteurEspaceCreate` provisionne domaine/projet/réseau/application credential — pas
    # le load balancer Octavia public partagé (`lb_id` dans les secrets) : sur ce lab, il a
    # été créé une seule fois à la main (cf. docstring de `zone_vps_secrets`) et n'a jamais eu
    # besoin d'être recréé depuis. Cette branche ne s'exécutant jamais ici, ce n'est pas
    # comblé automatiquement ; un vrai nouvel environnement devrait encore créer ce LB à la
    # main et poser `lb_id` via `depot_plateforme.definir_secrets` avant le premier hébergement.
    log.warning("zone_vps.lb_id_non_provisionne", espace_id=espace_id)
