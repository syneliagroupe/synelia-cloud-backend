"""Tarification : prix unitaires publics étendant la métrologie, estimation en FCFA entiers + TVA."""

from __future__ import annotations

from typing import Any

from synelia_kernel import argent

from synelia.modules.facturation import metrologie

PRIX_UNITAIRES = {
    **metrologie.PRIX,
    "espace_vcpu_mois": 2100,
    "objet_go_mois": 1.5,
    "base_go_mois": 25,
    "siege_mois": 3000,
    "certificat_an": 24000,
}

LITTERAUX_PERIODICITE = {"mensuelle": "Mensuel", "annuelle": "Annuel", None: "Ponctuel"}

HEURES_MOIS = 730  # affichage `totalHoraire` seulement


def _prix_ressource(type_: str, specification: dict[str, Any], quantite: int) -> int:
    q = max(1, quantite)
    if type_ == "vm":
        return (
            metrologie.mensuel(
                int(specification.get("vcpu", 1)),
                int(specification.get("ramGo", 2)),
                int(specification.get("diskGo", 20)),
            )
            * q
        )
    if type_ == "volume":
        prix_go = metrologie.PRIX_CLASSE_GO.get(
            specification.get("classe"), PRIX_UNITAIRES["stockage_go_mois"]
        )
        return round(specification.get("tailleGo", 10) * prix_go) * q
    if type_ == "espace":
        vcpu = int((specification.get("quota") or {}).get("vcpu", 4))
        return vcpu * PRIX_UNITAIRES["espace_vcpu_mois"] * q
    if type_ in ("base", "bucket"):
        go = int(specification.get("tailleGo", 10))
        return (
            round(go * PRIX_UNITAIRES[f"{type_}_go_mois" if type_ == "base" else "objet_go_mois"])
            * q
        )
    if type_ in ("certificat", "domaine"):
        return int(PRIX_UNITAIRES.get(f"{type_}_an", 0)) * q
    if type_ == "siege":
        return int(PRIX_UNITAIRES["siege_mois"]) * q
    if type_ == "k8s":
        controle = PRIX_UNITAIRES[
            "k8s_controle_ha_mois" if specification.get("ha") else "k8s_controle_mois"
        ]
        workers = metrologie.mensuel(
            int(specification.get("vcpu", 2)),
            int(specification.get("ramGo", 4)),
            int(specification.get("diskGo", 40)),
        ) * int(specification.get("noeuds", 0))
        return (controle + workers) * q
    return int(PRIX_UNITAIRES.get(f"{type_}_mois", 0)) * q


def _prix_renomme(type_: str) -> str:
    return {
        "espace": "Espace Cloud",
        "vm": "Machine virtuelle",
        "k8s": "Cluster Kubernetes",
        "volume": "Volume",
        "bucket": "Seau objet",
        "base": "Base de données",
        "service_manage": "Service managé",
        "hebergement": "Hébergement",
        "certificat": "Certificat TLS",
        "siege": "Licence par siège",
    }.get(type_, type_.replace("_", " ").capitalize())


def estimer(demande: Any) -> dict[str, Any]:
    type_ = demande.type
    quantite = demande.quantite or 1
    specification = demande.specification or {}
    periodicite = demande.periodicite

    prix_unit_simple = _prix_ressource(type_, specification, 1)
    total_mensuel = argent.ttc(prix_unit_simple * quantite)
    total_periode = total_mensuel * (12 if periodicite == "annuelle" else 1)

    lignes = [
        {
            "libelle": f"{_prix_renomme(type_)} — {specification.get('nom', specification.get('code', ''))}",
            "quantite": float(quantite),
            "unite": LITTERAUX_PERIODICITE.get(periodicite),
            "prixUnitaire": prix_unit_simple,
            "total": total_periode,
        }
    ]
    return {
        "lignes": lignes,
        "totalMensuel": total_mensuel,
        "totalHoraire": float(prix_unit_simple / HEURES_MOIS) if type_ == "vm" else None,
        "proRataMoisCourant": int(total_periode * 0.5),
        "devise": "XOF",
        "engagement": None,
        "remisePct": None,
        "avertissements": [
            "Egress non couvert. Les licences tierces restent à la charge du client."
        ],
    }
