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

from synelia.depot import Depot
from synelia.deps.contexte import Contexte

PRIX = {
    "vcpu_heure": 25,
    "ram_go_heure": 12,
    "stockage_to_jour": 1500,
    "ip_publique_jour": 300,
    "lb_jour": 2000,
}

# Un `Hebergement`/`Drive` ne porte pas son propre vcpu/ramGo : comme `web_hebergement.service.
# construire_hebergement`, la taille de la VM Nova dépend du palier commercial. Le disque
# d'un Drive vient en revanche de son propre quota réel (`Drive.quota.totalGo`).
SPECS_PALIER_WEB: dict[str, tuple[int, int, int]] = {
    "starter": (1, 2, 40),
    "pro": (2, 4, 80),
    "business": (4, 8, 160),
    "enterprise": (8, 16, 320),
}


async def consommation(ctx: Contexte, periode: str, espace_id: str | None = None) -> dict[str, Any]:
    annee, mois = (int(x) for x in periode.split("-")[:2])
    debut = date(annee, mois, 1)
    fin = (debut.replace(day=28) + timedelta(days=4)).replace(day=1)
    aujourdhui = date.today()

    vms = await Depot("vm", m.Vm).tous(
        ctx, filtre=lambda v: espace_id is None or v.espaceId == espace_id
    )
    vcpu = sum(v.vcpu for v in vms)
    ram = sum(v.ramGo for v in vms)
    to = sum(v.diskGo for v in vms) / 1024
    ips = sum(1 for v in vms for ip in v.ips if ip.type == "publique")

    volumes = await Depot("volume", m.Volume).tous(
        ctx, filtre=lambda v: espace_id is None or v.espaceId == espace_id
    )
    to += sum(v.tailleGo for v in volumes) / 1024

    lbs = await Depot("load_balancer", m.LoadBalancer).tous(
        ctx, filtre=lambda lb: espace_id is None or lb.espaceId == espace_id
    )
    nb_lb = len(lbs)

    # Le Web Cloud (hébergement VPS, Drive) n'est pas rattaché à un Espace Cloud : hors
    # périmètre d'une consommation filtrée par Espace.
    if espace_id is None:
        hebergements = await Depot("web_hebergement", m.Hebergement).tous(ctx)
        for h in hebergements:
            vcpu += h.serveur.vcpu
            ram += h.serveur.ramGo
            to += h.serveur.diskGo / 1024

        drives = await Depot("web_drive", m.Drive).tous(ctx)
        for d in drives:
            dv, dr, _dd = SPECS_PALIER_WEB.get(d.palier, (2, 4, 80))
            vcpu += dv
            ram += dr
            to += d.quota.totalGo / 1024

    jours = []
    j = debut
    while j < fin and j <= aujourdhui:
        montant = (
            vcpu * 24 * PRIX["vcpu_heure"]
            + ram * 24 * PRIX["ram_go_heure"]
            + int(to * PRIX["stockage_to_jour"])
            + ips * PRIX["ip_publique_jour"]
            + nb_lb * PRIX["lb_jour"]
        )
        jours.append(
            {
                "date": j,
                "vcpuHeures": vcpu * 24,
                "ramGoHeures": ram * 24,
                "stockageToJour": round(to, 3),
                "egressGo": 0,
                "montant": montant,
            }
        )
        j += timedelta(days=1)
    total = sum(x["montant"] for x in jours)
    nb = max(1, len(jours))
    prevision = int(total / nb * (fin - debut).days)
    return {
        "periode": periode,
        "jours": jours,
        "total": total,
        "prevision": prevision,
        "totalMoisPrecedent": 0,
    }
