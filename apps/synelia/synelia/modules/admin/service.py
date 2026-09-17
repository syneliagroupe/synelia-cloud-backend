"""Module admin (pilotage plateforme) : dépôts plateforme, agrégations inter-organisations, exécuteurs."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from synelia_contract import modeles as m
from synelia_db.modeles import Audit, Ressource, Travail, Utilisateur
from synelia_kernel import erreurs
from synelia_kernel.dates import depuis_iso, maintenant
from synelia_kernel.ids import nouvel_id
from synelia_openstack import fournisseur
from synelia_openstack.compute import ComputeOpenStack, ComputeSimule

from synelia.depot import Depot
from synelia.deps.contexte import Contexte
from synelia.travaux import Executeur, executeur

BACKEND_REEL_ID = "backend-abj"  # seul backend réellement adossé au lab OpenStack.
# `backend-gbm` (18 hosts, 768 vCPU, 6 TiB, statut « maintenance ») a été retiré de
# l'amorçage le 2026-09-09 : ce second socle n'a jamais existé dans le lab OpenStack réel et
# les chiffres étaient inventés — un datacenter fantôme avec des nombres précis est plus
# trompeur qu'un site absent. Le site physique GBM (Grand-Bassam) reste un choix valide
# ailleurs dans le produit (sélection d'Espace, DRP) : seul ce backend fictif est retiré.


def amont() -> ComputeSimule:
    return fournisseur(ComputeSimule, ComputeOpenStack)


depot_backend = Depot("backend", m.Backend, plateforme=True, libelle="Backend", champ_nom="code")
depot_placement = Depot("placement", m.Placement, plateforme=True, libelle="Placement")
depot_fenetre = Depot(
    "fenetre_patching",
    m.FenetrePatching,
    plateforme=True,
    libelle="Fenêtre de patching",
    champ_nom="libelle",
)
depot_lead = Depot("lead", m.Lead, plateforme=True, libelle="Lead", champ_nom="nom")
depot_campagne_maj = Depot(
    "campagne_maj",
    m.CampagneMaj,
    plateforme=True,
    libelle="Campagne de mise à jour",
    champ_nom="nom",
)
depot_campagne_migration = Depot(
    "campagne_migration",
    m.CampagneMigration,
    plateforme=True,
    libelle="Campagne de migration",
    champ_nom="nom",
)
depot_incident = Depot(
    "incident", m.Incident, plateforme=True, libelle="Incident", champ_nom="titre"
)
depot_statut_service = Depot(
    "statut_service", m.StatutService, plateforme=True, libelle="Service", champ_nom="nom"
)


def utc(d: datetime | None) -> datetime | None:
    if d is None:
        return None
    return d.astimezone(UTC) if d.tzinfo else d.replace(tzinfo=UTC)


async def lignes_type(ctx: Contexte, type_: str, org_id: str | None = None) -> list[Ressource]:
    # `Ressource.supprime_le` (soft delete) doit être exclu ici comme il l'est déjà dans
    # `Depot._requete` — sinon un espace ou un ticket supprimé reste compté pour toujours
    # dans les agrégations plateforme (`espacesTotal` gonflé de vieilles suppressions).
    q = select(Ressource).where(Ressource.type == type_, Ressource.supprime_le.is_(None))
    if org_id is not None:
        q = q.where(Ressource.org_id == org_id)
    q = q.order_by(Ressource.cree_le.desc())
    return list((await ctx.session.execute(q)).scalars().all())


async def amacer_backends(ctx: Contexte) -> list[m.Backend]:
    """Crée les backends par défaut si la table est vide (actif ABJ + maintenance GBM), puis
    rafraîchit la capacité de `backend-abj` depuis la statistique Nova réelle du lab (mode
    OpenStack réel seulement — en simulation, `capacite_plateforme()` renvoie `None` et les
    valeurs de secours ci-dessous restent inchangées, comme avant)."""
    existants = await depot_backend.tous(ctx)
    if not existants:
        base = [
            ("backend-abj", "openstack-abj", "ABJ", 24, "en_ligne", 1024, 8192, 1024),
        ]
        for id_, code, site, hosts, statut, vcpu, ram, stockage in base:
            await depot_backend.creer(
                ctx,
                m.Backend(
                    id=id_,
                    code=code,
                    type="openstack",
                    site=site,
                    hosts=hosts,
                    statut=statut,
                    usage=m.Usage(vcpuPct=0, ramPct=0, stockagePct=0),
                    capacite=m.Quota(vcpu=vcpu, ramGo=ram, stockageTo=stockage),
                    souverain=True,
                ),
            )
        existants = await depot_backend.tous(ctx)

    # Nettoyage d'une base déjà amorcée avant le 2026-09-09 : `backend-gbm` a pu y être créé
    # par un amorçage antérieur, avant le retrait ci-dessus — on le supprime pour de bon plutôt
    # que de laisser un fantôme visible tant que la table n'est pas vidée manuellement.
    fantome = next((b for b in existants if b.id == "backend-gbm"), None)
    if fantome is not None:
        await depot_backend.supprimer(ctx, "backend-gbm")
        existants = [b for b in existants if b.id != "backend-gbm"]

    # `amont().capacite_plateforme` (openstacksdk, synchrone) est déchargé via
    # `asyncio.to_thread` : même garde que `vms.service`, sans quoi un appel amont lent
    # gèlerait la boucle asyncio — donc l'API entière, tous tenants confondus.
    reel = await asyncio.to_thread(amont().capacite_plateforme)
    if reel is not None:
        actuel = next((b for b in existants if b.id == BACKEND_REEL_ID), None)
        cap = m.Quota(vcpu=reel["vcpu"], ramGo=reel["ramGo"], stockageTo=reel["stockageTo"])
        if actuel is not None and (actuel.hosts != reel["hosts"] or actuel.capacite != cap):
            await depot_backend.modifier(
                ctx,
                BACKEND_REEL_ID,
                {"hosts": reel["hosts"], "capacite": cap.model_dump(mode="json")},
            )
            existants = await depot_backend.tous(ctx)
    return existants


async def sante_integrations(ctx: Contexte) -> list[dict[str, Any]]:
    """Statuts d'intégration réels (au lieu de « ok » figé) : chaque ligne est vérifiée par un
    appel bon marché, ou honnêtement marquée `non_configure` quand aucune intégration réelle
    n'existe côté code (Centreon : aucun client n'a jamais été écrit, `lien_centreon()` renvoie
    toujours `None`)."""
    import os

    import httpx
    from synelia_kernel.config import reglages
    from synelia_openstack.victoria import ENV_GRAFANA_URL, ENV_LOGS_URL

    # Le contrat borne le statut à {"ok", "degrade", "panne"} : une intégration jamais
    # câblée (pas d'URL, pas d'adresse) est « degrade » — non vérifiable, pas forcément en
    # panne — une intégration jointe mais qui répond mal ou pas du tout est « panne ».
    horodatage = maintenant()

    def ligne(nom: str, statut: str) -> dict[str, Any]:
        return {"nom": nom, "statut": statut, "dernierControle": horodatage}

    # OpenStack : réutilise l'appel Nova déjà fait par `amacer_backends` (pas de second appel).
    reel_os = await asyncio.to_thread(amont().capacite_plateforme)
    statut_os = "ok" if reel_os is not None else "panne"

    # VictoriaLogs : ping HTTP court si l'URL est configurée, sinon non-vérifiable (pas « ok »).
    url_logs = os.environ.get(ENV_LOGS_URL)
    if not url_logs:
        statut_logs = "degrade"
    else:
        try:
            r = await asyncio.to_thread(httpx.get, f"{url_logs}/health", timeout=3)
            statut_logs = "ok" if r.status_code < 500 else "panne"
        except httpx.HTTPError:
            statut_logs = "panne"

    # Grafana : idem, une simple présence de l'URL ne suffit pas à dire « ok ».
    url_grafana = os.environ.get(ENV_GRAFANA_URL)
    if not url_grafana:
        statut_grafana = "degrade"
    else:
        try:
            r = await asyncio.to_thread(httpx.get, f"{url_grafana}/api/health", timeout=3)
            statut_grafana = "ok" if r.status_code < 500 else "panne"
        except httpx.HTTPError:
            statut_grafana = "panne"

    # Temporal : n'est même utilisé que si `temporal_adresse` est renseigné (sinon l'exécuteur
    # local — `travaux/local.py` — fait le travail réellement, sans Temporal à sonder ici).
    adresse_temporal = reglages().temporal_adresse
    if not adresse_temporal:
        statut_temporal = "degrade"
    else:
        try:
            from temporalio.client import Client

            await asyncio.wait_for(
                Client.connect(adresse_temporal, namespace=reglages().temporal_espace), timeout=3
            )
            statut_temporal = "ok"
        except Exception:  # noqa: BLE001 — n'importe quel échec de connexion = panne
            statut_temporal = "panne"

    return [
        # Centreon : aucun client n'a jamais été écrit (`lien_centreon()` renvoie toujours
        # `None` côté `synelia_openstack.victoria`) — « degrade » assumé, jamais « ok » fabriqué.
        ligne("Centreon", "degrade"),
        ligne("Grafana", statut_grafana),
        ligne("VictoriaLogs", statut_logs),
        ligne("OpenStack", statut_os),
        ligne("Temporal", statut_temporal),
    ]


async def acces_refuses_24h(ctx: Contexte) -> int:
    """Actions RBAC refusées sur les dernières 24 h, journalisées par `journaliser()`
    comme n'importe quelle autre entrée d'audit — utilisé par `/admin/sante` et
    `/admin/tableau-de-bord`, qui renvoyaient `0` en dur jusqu'ici."""
    seuil = utc(maintenant()) - timedelta(hours=24)
    q = (
        select(func.count())
        .select_from(Audit)
        .where(Audit.resultat.in_(("refus", "refuse")), Audit.date >= seuil)
    )
    return int((await ctx.session.execute(q)).scalar_one())


async def tickets_sla_risque(ctx: Contexte) -> int:
    """Tickets plateforme dont le SLA restant tombe sous 30 min — même seuil que le
    filtre `slaRisque` de `GET /admin/tickets`."""
    n = 0
    for r in await lignes_type(ctx, "ticket"):
        sla = (r.donnees or {}).get("slaRestantMin")
        if sla is not None and sla <= 30:
            n += 1
    return n


async def usage_plateforme(ctx: Contexte) -> dict[str, float]:
    """Consommation agrégée des machines `vm` à travers toutes les organisations."""
    vcpu = ram_go = disk_go = 0
    for r in await lignes_type(ctx, "vm"):
        vcpu += int(r.donnees.get("vcpu") or 0)
        ram_go += int(r.donnees.get("ramGo") or 0)
        disk_go += int(r.donnees.get("diskGo") or 0)
    return {
        "vcpu": vcpu,
        "ramGo": ram_go,
        "stockageTo": round(disk_go / 1024, 2),
    }


def _equipe(u: Utilisateur) -> dict[str, Any] | None:
    return u.equipe if isinstance(u.equipe, dict) and u.equipe.get("role") else None


async def membres_equipe(ctx: Contexte) -> list[Utilisateur]:
    tous = list((await ctx.session.execute(select(Utilisateur))).scalars().all())
    return [u for u in tous if _equipe(u)]


async def membre_equipe(ctx: Contexte, membreId: str) -> Utilisateur:  # noqa: N803
    for u in await membres_equipe(ctx):
        if u.id == membreId:
            return u
    raise erreurs.introuvable("Membre de l'équipe", membreId)


def elevation_contrat(e: dict[str, Any], membre: str | None = None) -> dict[str, Any]:
    role = e.get("role")
    expire = e.get("expire")
    accorde = e.get("accordePar")
    actif = bool(e.get("actif", True))
    if expire:
        actif = actif and depuis_iso(expire) > maintenant()
    duree = e.get("duree") or "4 h"
    out = {
        "id": e["id"],
        "qui": e.get("qui", ""),
        "quand": e.get("quand"),
        "duree": duree,
        "motif": e.get("motif", ""),
        "actif": actif,
        "membreId": membre,
        "role": role,
        "ticketId": e.get("ticketId"),
        "expire": expire,
        "accordePar": accorde,
        "actionsJournalisees": e.get("actionsJournalisees"),
    }
    return {k: v for k, v in out.items() if v is not None}


@executeur("admin.maj.lancement")
class ExecuteurMaj(Executeur):
    async def terminer(self, ctx: Contexte, travail: Travail) -> None:
        campagne = await depot_campagne_maj.obtenir(ctx, travail.cible_id or "")
        await depot_campagne_maj.definir_statut(ctx, campagne.id, "terminee")


@executeur("admin.migration.lancement")
class ExecuteurMigration(Executeur):
    async def terminer(self, ctx: Contexte, travail: Travail) -> None:
        campagne = await depot_campagne_migration.obtenir(ctx, travail.cible_id or "")
        await depot_campagne_migration.definir_statut(ctx, campagne.id, "terminee")


@executeur("admin.tests_restauration")
class ExecuteurTestsRestauration(Executeur):
    pass


@executeur("capacite.rebalance")
class ExecuteurRebalance(Executeur):
    async def terminer(self, ctx: Contexte, travail: Travail) -> None:
        entree = travail.entree or {}
        espace_id = entree.get("espaceId")
        if espace_id:
            placements = entree.get("placements")
            if placements is not None:
                existants = await depot_placement.tous(
                    ctx, filtre=lambda p: p.espaceId == espace_id
                )
                for p in existants:
                    await depot_placement.supprimer(ctx, p.id)
                for pl in placements:
                    await depot_placement.creer(
                        ctx,
                        m.Placement(
                            id=nouvel_id(),
                            espaceId=espace_id,
                            backendId=pl["backendId"],
                            percent=pl["percent"],
                        ),
                    )
