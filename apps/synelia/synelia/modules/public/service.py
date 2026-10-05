"""Vitrine publique : catalogue de services managés, offres, tarifs, demandes entrants (leads)."""

from __future__ import annotations

from typing import Any

from synelia_catalogue import configuration, configurations
from synelia_openstack.compute import GABARITS

CATEGORIES = ["collaboration", "communication", "metier", "web", "donnees", "technique"]

# slug → (nom, solutionOSS, categorie, pitch, paliers FCFA)
_SERVICES: dict[str, dict[str, Any]] = {
    "drive-pro": {
        "nom": "Drive Pro",
        "solutionOSS": "Nextcloud",
        "categorie": "collaboration",
        "pitch": "Partage et synchro de fichiers souverains, remplaçant Google Drive / Dropbox.",
        "paliers": [
            {
                "code": "s",
                "nom": "Starter",
                "specs": "1 To, 10 sièges",
                "prixSiege": 2500,
                "prixMois": 25000,
                "limites": ["1 To", "10 sièges"],
                "recommande": True,
            },
            {
                "code": "m",
                "nom": "Équipe",
                "specs": "5 To, 50 sièges",
                "prixSiege": 2000,
                "prixMois": 100000,
                "limites": ["5 To", "50 sièges"],
            },
            {
                "code": "l",
                "nom": "Grande équipe",
                "specs": "20 To illimité",
                "prixSiege": 1800,
                "prixMois": None,
                "limites": ["20 To", "illimité"],
            },
        ],
    },
    "email-pro": {
        "nom": "E-mail Pro",
        "solutionOSS": "Postal",
        "categorie": "communication",
        "pitch": "Messagerie professionnelle à votre domaine, hébergée en Côte d'Ivoire.",
        "paliers": [
            {
                "code": "s",
                "nom": "Starter",
                "specs": "5 boîtes",
                "prixSiege": 1500,
                "prixMois": 7500,
                "limites": ["5 boîtes"],
                "recommande": True,
            },
            {
                "code": "m",
                "nom": "Équipe",
                "specs": "25 boîtes",
                "prixSiege": 1300,
                "prixMois": 32500,
                "limites": ["25 boîtes"],
            },
            {
                "code": "l",
                "nom": "Grande équipe",
                "specs": "100 boîtes",
                "prixSiege": 1200,
                "prixMois": 120000,
                "limites": ["100 boîtes"],
            },
        ],
    },
    "visio": {
        "nom": "Visio",
        "solutionOSS": "Jitsi Meet",
        "categorie": "communication",
        "pitch": "Visioconférence sécurisée, sans limite de durée, sur des serveurs locaux.",
        "paliers": [
            {
                "code": "m",
                "nom": "Équipe",
                "specs": "25 participants",
                "prixSiege": 0,
                "prixMois": 30000,
                "limites": ["25 participants"],
                "recommande": True,
            },
            {
                "code": "l",
                "nom": "Evénements",
                "specs": "100 participants",
                "prixSiege": 0,
                "prixMois": 90000,
                "limites": ["100 participants"],
            },
        ],
    },
    "ged": {
        "nom": "GED",
        "solutionOSS": "Paperless-ngx",
        "categorie": "collaboration",
        "pitch": "Gestion électronique de documents : classement, OCR et recherche instantanée.",
        "paliers": [
            {
                "code": "s",
                "nom": "Starter",
                "specs": "50k documents",
                "prixSiege": 0,
                "prixMois": 45000,
                "limites": ["50k documents"],
                "recommande": True,
            },
            {
                "code": "m",
                "nom": "Équipe",
                "specs": "250k documents",
                "prixSiege": 0,
                "prixMois": 120000,
                "limites": ["250k documents"],
            },
        ],
    },
    "erp": {
        "nom": "ERP",
        "solutionOSS": "Odoo Community",
        "categorie": "metier",
        "pitch": "Gestion intégrée : comptabilité, ventes, achats et inventaire en un seul outil.",
        "paliers": [
            {
                "code": "s",
                "nom": "Starter",
                "specs": "10 utilisateurs",
                "prixSiege": 0,
                "prixMois": 60000,
                "limites": ["10 utilisateurs"],
                "recommande": True,
            },
            {
                "code": "m",
                "nom": "Équipe",
                "specs": "50 utilisateurs",
                "prixSiege": 0,
                "prixMois": 150000,
                "limites": ["50 utilisateurs"],
            },
        ],
    },
    "crm": {
        "nom": "CRM",
        "solutionOSS": "EspoCRM",
        "categorie": "metier",
        "pitch": "Pilotez vos prospects, opportunités et campagnes depuis un CRM centralisé.",
        "paliers": [
            {
                "code": "s",
                "nom": "Starter",
                "specs": "10 utilisateurs",
                "prixSiege": 0,
                "prixMois": 45000,
                "limites": ["10 utilisateurs"],
                "recommande": True,
            },
            {
                "code": "m",
                "nom": "Équipe",
                "specs": "50 utilisateurs",
                "prixSiege": 0,
                "prixMois": 110000,
                "limites": ["50 utilisateurs"],
            },
        ],
    },
    "wordpress": {
        "nom": "WordPress",
        "solutionOSS": "WordPress",
        "categorie": "web",
        "pitch": "Sites vitrines et boutiques, managés et sauvegardés automatiquement.",
        "paliers": [
            {
                "code": "s",
                "nom": "Starter",
                "specs": "1 site",
                "prixSiege": 0,
                "prixMois": 12000,
                "limites": ["1 site"],
                "recommande": True,
            },
            {
                "code": "m",
                "nom": "Boutique",
                "specs": "WooCommerce",
                "prixSiege": 0,
                "prixMois": 25000,
                "limites": ["WooCommerce"],
            },
        ],
    },
    "prestashop": {
        "nom": "PrestaShop",
        "solutionOSS": "PrestaShop",
        "categorie": "web",
        "pitch": "E-commerce managé, sécurisé et prêt pour le paiement mobile money.",
        "paliers": [
            {
                "code": "m",
                "nom": "Boutique",
                "specs": "e-commerce",
                "prixSiege": 0,
                "prixMois": 25000,
                "limites": ["e-commerce"],
                "recommande": True,
            },
        ],
    },
    "bi": {
        "nom": "Business Intelligence",
        "solutionOSS": "Apache Superset",
        "categorie": "donnees",
        "pitch": "Tableaux de bord et dataviz sur vos données, hébergés localement.",
        "paliers": [
            {
                "code": "m",
                "nom": "Équipe",
                "specs": "20 utilisateurs",
                "prixSiege": 0,
                "prixMois": 90000,
                "limites": ["20 utilisateurs"],
                "recommande": True,
            },
        ],
    },
    "forge": {
        "nom": "Forge DevOps",
        "solutionOSS": "Gitea",
        "categorie": "technique",
        "pitch": "Hébergement Git privé, CI/CD et registre de conteneurs intégrés.",
        "paliers": [
            {
                "code": "m",
                "nom": "Équipe",
                "specs": "20 devs",
                "prixSiege": 0,
                "prixMois": 40000,
                "limites": ["20 devs"],
                "recommande": True,
            },
        ],
    },
    "coffre": {
        "nom": "Coffre-fort numérique",
        "solutionOSS": "Vaultwarden",
        "categorie": "technique",
        "pitch": "Gestionnaire de mots de passe d'entreprise, chiffré de bout en bout.",
        "paliers": [
            {
                "code": "s",
                "nom": "Starter",
                "specs": "25 utilisateurs",
                "prixSiege": 800,
                "prixMois": 20000,
                "limites": ["25 utilisateurs"],
                "recommande": True,
            },
            {
                "code": "l",
                "nom": "Grande équipe",
                "specs": "illimité",
                "prixSiege": 600,
                "prixMois": None,
                "limites": ["illimité"],
            },
        ],
    },
    "automatisation": {
        "nom": "Automatisation",
        "solutionOSS": "n8n",
        "categorie": "technique",
        "pitch": "Intégrez et automatisez vos flux métier sans code, sur vos données.",
        "paliers": [
            {
                "code": "m",
                "nom": "Équipe",
                "specs": "10 workflows",
                "prixSiege": 0,
                "prixMois": 35000,
                "limites": ["10 workflows"],
                "recommande": True,
            },
        ],
    },
    "analytics-web": {
        "nom": "Analytics Web",
        "solutionOSS": "Matomo",
        "categorie": "donnees",
        "pitch": "Mesure d'audience respectueuse des données, sans partage à des tiers.",
        "paliers": [
            {
                "code": "s",
                "nom": "Starter",
                "specs": "100k vues/mois",
                "prixSiege": 0,
                "prixMois": 18000,
                "limites": ["100k vues/mois"],
                "recommande": True,
            },
            {
                "code": "m",
                "nom": "Équipe",
                "specs": "1M vues/mois",
                "prixSiege": 0,
                "prixMois": 55000,
                "limites": ["1M vues/mois"],
            },
        ],
    },
}

PAGES_LEGALES = {
    "cgu": {
        "slug": "cgu",
        "titre": "Conditions générales d'utilisation",
        "miseAJour": "2026-02-01",
        "version": "3.2",
        "contenuMarkdown": "# Conditions générales d'utilisation\n\n…\n",
    },
    "confidentialite": {
        "slug": "confidentialite",
        "titre": "Politique de confidentialité",
        "miseAJour": "2026-02-01",
        "version": "2.4",
        "contenuMarkdown": "# Politique de confidentialité\n\n…\n",
    },
    "mentions-legales": {
        "slug": "mentions-legales",
        "titre": "Mentions légales",
        "miseAJour": "2026-01-15",
        "version": "1.8",
        "contenuMarkdown": "# Mentions légales\n\n…\n",
    },
    "sla": {
        "slug": "sla",
        "titre": "Engagements de service (SLA)",
        "miseAJour": "2026-03-01",
        "version": "1.5",
        "contenuMarkdown": "# Engagements de service\n\n…\n",
    },
}


def souverainete(sites: list[dict[str, Any]], backends: list[Any]) -> dict[str, Any]:
    """Page souveraineté dérivée du back-office : sites physiques et socles réellement déclarés."""
    villes = " et ".join(s["ville"] for s in sites) or "la Côte d'Ivoire"
    capacite = sum(b.capacite.vcpu for b in backends) or 1
    return {
        "niveaux": [
            {
                "niveau": "Données",
                "titre": "Les données restent en Côte d'Ivoire",
                "description": f"Hébergement à {villes}, sous juridiction ivoirienne.",
                "atteint": all(b.souverain for b in backends),
            },
            {
                "niveau": "Logiciels",
                "titre": "Socle open source",
                "description": "OpenStack, Nextcloud, Odoo, WordPress : le code reste auditable et reproductible.",
                "atteint": True,
            },
            {
                "niveau": "Compétences",
                "titre": "Équipes locales",
                "description": "Ingénierie, support et maintenance assurés par des équipes basées à Abidjan.",
                "atteint": True,
            },
        ],
        "trajectoireSortie": [
            {
                "backend": b.code,
                "part": f"{round(100 * b.capacite.vcpu / capacite)}%",
                "cible": "Socle souverain open source",
                "avancement": 0.0,
            }
            for b in backends
            if not b.souverain
        ],
        "hebergementDonnees": f"Côte d'Ivoire ({villes})",
        "juridiction": "Côte d'Ivoire — Loi n°2013-450 (données à caractère personnel)",
    }


ETUDES_CAS = [
    {
        "id": "banque-abj",
        "client": "Banque régionale panafricaine",
        "secteur": "Banque & assurance",
        "resume": "Migration de 400 postes vers une suite bureautique souveraine en 6 mois.",
        "contexte": "Contrainte réglementaire de localisation des données bancaires.",
        "solution": ["Drive Pro", "E-mail Pro", "Office suite open source"],
        "resultats": [
            {"indicateur": "Coût TCO", "valeur": "-38%"},
            {"indicateur": "Temps de migration", "valeur": "6 mois"},
        ],
        "citation": {
            "texte": "Nous avons retrouvé la maîtrise de nos données.",
            "auteur": "DSI",
            "fonction": "Directeur des systèmes d'information",
        },
    },
    {
        "id": "commune-bassam",
        "client": "Commune de Grand-Bassam",
        "secteur": "Administration",
        "resume": "Dématérialisation complète de l'état civil et des autorisations.",
        "contexte": "Archives papier, délais administratifs longs.",
        "solution": ["GED", "Drive Pro"],
        "resultats": [
            {"indicateur": "Délai de traitement", "valeur": "-70%"},
            {"indicateur": "Archives numérisées", "valeur": "100%"},
        ],
        "citation": {
            "texte": "Nos administrés gagnent des jours.",
            "auteur": "Le Maire",
            "fonction": "Maire",
        },
    },
    {
        "id": "telco-santé",
        "client": "Champion national des télécoms",
        "secteur": "Santé",
        "resume": "Hébergement souverain d'une plateforme de télémédecine nationale.",
        "contexte": "Sensibilité des données de santé, exigence de disponibilité.",
        "solution": ["Espace Cloud", "Kubernetes managé", "Base de données"],
        "resultats": [
            {"indicateur": "Disponibilité", "valeur": "99,99%"},
            {"indicateur": "Patients servis", "valeur": "500k"},
        ],
    },
]

SLA_ENGAGEMENTS = [
    {
        "composant": "Disponibilité infrastructure",
        "dispo": 99.95,
        "constate": 99.98,
        "reponseCritique": 5,
        "resolutionCritique": 240,
    },
    {
        "composant": "Services managés",
        "dispo": 99.9,
        "constate": 99.95,
        "reponseCritique": 15,
        "resolutionCritique": 360,
    },
    {
        "composant": "Support commercial",
        "dispo": 99.5,
        "constate": 99.8,
        "reponseCritique": 60,
        "resolutionCritique": 720,
    },
]

HYPOTHESES = [
    "Base HT, hors TVA (18 %).",
    "Tous les prix unitaires sont mensuels ; la consommation est proratisée au jour (30 j).",
    "Les gabarits sont facturés au mois calendaire, proratisés au premier du mois.",
]


def fiche_catalogue(slug: str) -> dict[str, Any] | None:
    conf = configuration(slug)
    meta = _SERVICES.get(slug)
    if conf is None or meta is None:
        return None
    return {
        "slug": slug,
        "nom": meta["nom"],
        "solutionOSS": meta["solutionOSS"],
        "categorie": meta["categorie"],
        "description": conf.get("intro") or meta["pitch"],
        "pitch": meta["pitch"],
        "modes": ["dedie"],
        "paliers": meta["paliers"],
        "sla": "99,9 % mensuel",
        "reversibilite": {
            "formats": ["Dump SQL", "Export ZIP", "API"],
            "delaiJours": 30,
            "docUrl": f"/docs/{slug}/reversibilite",
        },
        "versionsSupportees": [meta["solutionOSS"]],
        "certifie": True,
        "migrationEntrante": [meta["solutionOSS"]],
    }


def catalogues() -> list[dict[str, Any]]:
    return [d for s in slugs_ordonnes() if (d := fiche_catalogue(s))]


def slugs_ordonnes() -> list[str]:
    return [c["slug"] for c in configurations() if c.get("slug") in _SERVICES]


def familles_tarifs() -> list[dict[str, Any]]:
    par_famille: dict[str, dict[str, Any]] = {}
    for g in GABARITS:
        f = g["famille"]
        if f == "gpu":  # aucune carte GPU sur cette plateforme
            continue
        fam = par_famille.setdefault(f, {"code": f, "nom": f, "description": None, "offres": []})
        fam["offres"].append(
            {
                "id": g["id"],
                "code": g["id"],
                "nom": g["nom"],
                "categorie": "image_vm",
                "specs": f"{g['vcpu']} vCPU, {g['ramGo']} Go RAM, {g['diskGo']} Go",
                "caracteristiques": [
                    f"{g['vcpu']} vCPU",
                    f"{g['ramGo']} Go RAM",
                    f"{g['diskGo']} Go disque",
                ],
                "prix": g["prixMensuel"],
                "populaire": g["id"] == "g1.medium",
                "statut": "publiee",
                "souscriptionsActives": 0,
            }
        )
    return list(par_famille.values())
