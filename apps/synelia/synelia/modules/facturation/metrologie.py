"""Métrologie : usage horaire → consommation journalière → montant FCFA.

Couvre les ressources réellement provisionnées côté OpenStack cette session : les VM d'un
Espace Cloud (`vm`), les volumes Cinder qui leur sont attachés (`volume` — stockage distinct du
disque racine des VM, jamais compté ailleurs), les Load Balancers Octavia (`load_balancer` —
tarif plat, pas d'usage horaire mesurable), et les VM d'hébergement Web Cloud (VPS/Drive :
`web_hebergement`/`web_drive`) qui ont leur propre VM Nova hors du dépôt `vm`."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from synelia_contract import modeles as m
from synelia_openstack.compute import GABARITS

from synelia.depot import Depot
from synelia.deps.contexte import Contexte

# Grille unique (FCFA HT / mois) : gabarits VM du catalogue, projets, simulateur, métrologie et
# facture en dérivent. Les gabarits de `synelia_openstack.compute.GABARITS` s'en déduisent à
# ~2 % près (1 vCPU + 2 Go + 20 Go ≈ 9 400 pour S1 Small, 9 000 au catalogue).
PRIX = {
    "vcpu_mois": 6500,
    "ram_go_mois": 1400,
    "stockage_go_mois": 5.4,
    "ip_publique_mois": 3500,
    "lb_mois": 18000,
    "k8s_controle_mois": 18000,
    "k8s_controle_ha_mois": 62000,
}

# Stockage bloc par classe (FCFA / Go / mois) ; NVMe est le tarif de référence `stockage_go_mois`.
PRIX_CLASSE_GO = {"nvme": 5.4, "ssd": 3.2, "hdd": 1.1, "archive": 0.32}


def mensuel(vcpu: float, ram_go: float, disque_go: float = 0) -> int:
    """Prix mensuel HT d'une enveloppe de calcul : l'unique formule vCPU + RAM + disque."""
    return round(
        vcpu * PRIX["vcpu_mois"]
        + ram_go * PRIX["ram_go_mois"]
        + disque_go * PRIX["stockage_go_mois"]
    )


# Un `Hebergement`/`Drive` ne porte pas son propre vcpu/ramGo : comme `web_hebergement.service.
# construire_hebergement`, la taille de la VM Nova dépend du palier commercial. Le disque
# d'un Drive vient en revanche de son propre quota réel (`Drive.quota.totalGo`).
SPECS_PALIER_WEB: dict[str, tuple[int, int, int]] = {
    "starter": (1, 2, 40),
    "pro": (2, 4, 80),
    "business": (4, 8, 160),
    "enterprise": (8, 16, 320),
}


def _poste(
    espace: str | None,
    site: str | None,
    application: str | None,
    *,
    vcpu: float = 0,
    ram: float = 0,
    go: float = 0,
    reseau: int = 0,
    forfait: int = 0,
    prix_go: float | None = None,
) -> dict[str, Any]:
    return {
        "espace": espace,
        "site": site,
        "application": application,
        "vcpu": vcpu,
        "ram": ram,
        "go": go,
        "Calcul": mensuel(vcpu, ram) + forfait,
        "Stockage": round(go * prix_go) if prix_go is not None else mensuel(0, 0, go),
        "Réseau": reseau,
    }


async def postes(ctx: Contexte, espace_id: str | None = None) -> list[dict[str, Any]]:
    """Coût mensuel de tout ce qui tourne, par poste (espace, site, application) et par famille
    (Calcul / Stockage / Réseau) : l'unique source de la consommation et de la ventilation."""
    # Imports tardifs : `projets.router` importe cette grille.
    from synelia.modules.kubernetes.service import depot_cluster, depot_pool
    from synelia.modules.projets.service import depot_projet, depot_service

    def dans(eid: str | None) -> bool:
        return espace_id is None or eid == espace_id

    res: list[dict[str, Any]] = []
    for v in await Depot("vm", m.Vm).tous(
        ctx, filtre=lambda v: dans(v.espaceId) and v.statut != "error"
    ):
        ips = sum(1 for ip in v.ips if ip.type == "publique")
        res.append(
            _poste(
                v.espaceId,
                v.site,
                v.applicationNom or v.applicationId,
                vcpu=v.vcpu,
                ram=v.ramGo,
                go=v.diskGo,
                reseau=ips * PRIX["ip_publique_mois"],
            )
        )
    for vol in await Depot("volume", m.Volume).tous(ctx, filtre=lambda x: dans(x.espaceId)):
        res.append(
            _poste(
                vol.espaceId,
                None,
                None,
                go=vol.tailleGo,
                prix_go=PRIX_CLASSE_GO.get(vol.classe, PRIX["stockage_go_mois"]),
            )
        )
    for lb in await Depot("load_balancer", m.LoadBalancer).tous(
        ctx, filtre=lambda x: dans(x.espaceId)
    ):
        res.append(_poste(lb.espaceId, None, None, reseau=PRIX["lb_mois"]))

    for c in await depot_cluster.tous(
        ctx, filtre=lambda c: dans(c.espaceId) and c.statut != "provisioning"
    ):
        cp = PRIX["k8s_controle_ha_mois" if c.controlPlane.mode == "ha" else "k8s_controle_mois"]
        res.append(_poste(c.espaceId, None, c.nom, forfait=cp))
        for pool in await depot_pool.tous(ctx, parent_id=c.id):
            g = next((g for g in GABARITS if g["id"] == pool.flavor), None)
            n = pool.nodes
            res.append(
                _poste(
                    c.espaceId,
                    None,
                    c.nom,
                    vcpu=n * (g["vcpu"] if g else 2),
                    ram=n * (g["ramGo"] if g else 4),
                    go=n * (pool.diskGo or (g["diskGo"] if g else 40)),
                )
            )

    # Web Cloud (VPS, Drive) : pas de VM dans un Espace Cloud.
    if espace_id is None:
        for h in await Depot("web_hebergement", m.Hebergement).tous(ctx):
            res.append(
                _poste(
                    None,
                    None,
                    "Web Cloud",
                    vcpu=h.serveur.vcpu,
                    ram=h.serveur.ramGo,
                    go=h.serveur.diskGo,
                )
            )
        for d in await Depot("web_drive", m.Drive).tous(ctx):
            dv, dr, _dd = SPECS_PALIER_WEB.get(d.palier, (2, 4, 80))
            res.append(_poste(None, None, "Web Cloud", vcpu=dv, ram=dr, go=d.quota.totalGo))
    # Services PaaS : ils tournent dans le cluster partagé, mais se facturent à l'Espace du projet.
    projets = {p.id: p for p in await depot_projet.tous(ctx)}
    for s in await depot_service.tous(ctx, filtre=lambda s: s.statut in ("running", "degraded")):
        p = projets.get(s.projetId)
        if p is None or not dans(p.espaceId):
            continue
        res.append(
            _poste(
                p.espaceId,
                None,
                p.nom,
                vcpu=s.ressources.cpu,
                ram=s.ressources.ramMo / 1024,
                go=s.ressources.diskGo,
            )
        )
    return res


async def consommation(ctx: Contexte, periode: str, espace_id: str | None = None) -> dict[str, Any]:
    annee, mois = (int(x) for x in periode.split("-")[:2])
    debut = date(annee, mois, 1)
    fin = (debut.replace(day=28) + timedelta(days=4)).replace(day=1)
    jours_mois = (fin - debut).days
    aujourdhui = date.today()
    espace_cree_le = None
    if espace_id is not None:
        espace = await Depot("espace", m.EspaceCloud).obtenir(ctx, espace_id)
        espace_cree_le = espace.createdAt.date()
    lignes = await postes(ctx, espace_id)
    vcpu = sum(p["vcpu"] for p in lignes)
    ram = sum(p["ram"] for p in lignes)
    go = sum(p["go"] for p in lignes)
    mensuel_total = sum(p["Calcul"] + p["Stockage"] + p["Réseau"] for p in lignes)

    jours = []
    j = debut
    while j < fin and j <= aujourdhui:
        # L'espace n'existait pas encore ce jour-là : pas de fabrication d'usage,
        # même si le snapshot courant des ressources est non nul.
        if espace_cree_le is not None and j < espace_cree_le:
            jours.append(
                {
                    "date": j,
                    "vcpuHeures": 0,
                    "ramGoHeures": 0,
                    "stockageToJour": 0,
                    "egressGo": 0,
                    "montant": 0,
                }
            )
        else:
            jours.append(
                {
                    "date": j,
                    "vcpuHeures": round(vcpu * 24, 1),
                    "ramGoHeures": round(ram * 24, 1),
                    "stockageToJour": round(go / 1024, 3),
                    "egressGo": 0,
                    "montant": round(mensuel_total / jours_mois),
                }
            )
        j += timedelta(days=1)
    total = sum(x["montant"] for x in jours)
    nb = max(1, len(jours))
    prevision = int(total / nb * jours_mois)
    return {
        "periode": periode,
        "jours": jours,
        "total": total,
        "prevision": prevision,
        "totalMoisPrecedent": 0,
    }
