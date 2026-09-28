"""Services managés : catalogue (fiches, configuration, contrat) et cycle de vie des souscriptions."""

from __future__ import annotations

from typing import Any

import synelia_catalogue as _catalogue
from synelia_contract import modeles as m
from synelia_db.modeles import Ressource, Travail, Utilisateur
from synelia_kernel import erreurs
from synelia_kernel.dates import maintenant
from synelia_kernel.ids import nouvel_id

from synelia.demo import peupleur
from synelia.depot import Depot
from synelia.deps.contexte import Contexte
from synelia.travaux import Executeur, executeur

depot_service = Depot(
    "service_manage",
    m.ServiceManage,
    libelle="Service managé",
    champ_nom="nom",
    champs_recherche=("nom", "catalogSlug", "domaine"),
)
depot_siege = Depot("siege", m.Siege, libelle="Siège", champ_nom="userId")
depot_export = Depot("service_export", m.ExportService, libelle="Export", champ_nom="format")

# ── catalogue statique : slug → fiche  ───────────────────────────────────────
_NOMS = {
    "drive-pro": "Drive Pro",
    "email-pro": "E-mail Pro",
    "visio": "Visio",
    "ged": "GED",
    "erp": "ERP",
    "crm": "CRM",
    "wordpress": "WordPress",
    "prestashop": "Boutique PrestaShop",
    "bi": "BI Metabase",
    "forge": "Forge GitLab",
    "coffre": "Coffre-fort",
    "automatisation": "Automatisation",
    "analytics-web": "Analyse web",
}

_SOLUTION_OSS = {
    "drive-pro": "Nextcloud",
    "email-pro": "Stalwart",
    "visio": "Jitsi Meet",
    "ged": "Mayan EDMS",
    "erp": "Odoo",
    "crm": "EspoCRM",
    "wordpress": "WordPress",
    "prestashop": "PrestaShop",
    "bi": "Metabase",
    "forge": "GitLab CE",
    "coffre": "Vaultwarden",
    "automatisation": "n8n",
    "analytics-web": "Matomo",
}

_CATEGORIE = {
    "drive-pro": "collaboration",
    "email-pro": "communication",
    "visio": "communication",
    "ged": "metier",
    "erp": "metier",
    "crm": "metier",
    "wordpress": "web",
    "prestashop": "web",
    "bi": "donnees",
    "forge": "technique",
    "coffre": "technique",
    "automatisation": "technique",
    "analytics-web": "donnees",
}

_MODES = {
    "drive-pro": ["dedie", "mutualise"],
    "email-pro": ["dedie", "mutualise"],
    "visio": ["mutualise"],
    "ged": ["dedie", "mutualise"],
    "erp": ["dedie"],
    "crm": ["dedie", "mutualise"],
    "wordpress": ["dedie", "mutualise"],
    "prestashop": ["dedie"],
    "bi": ["mutualise"],
    "forge": ["dedie"],
    "coffre": ["mutualise"],
    "automatisation": ["dedie", "mutualise"],
    "analytics-web": ["mutualise"],
}

_SLA = {
    "drive-pro": "99,9 % · sauvegarde quotidienne",
    "email-pro": "99,99 % · filtre anti-spam",
    "visio": "99,9 % · relay TURN",
    "ged": "99,9 % · sauvegarde quotidienne",
    "erp": "99,5 % · fenêtre de nuit",
    "crm": "99,9 % · sauvegarde quotidienne",
    "wordpress": "99,9 % · WAF + cache",
    "prestashop": "99,5 % · infra gérée",
    "bi": "99,9 % · snapshot quotidien",
    "forge": "99,9 % · sauvegarde quotidienne",
    "coffre": "99,9 % · sauvegarde chiffrée",
    "automatisation": "99,9 % · sauvegarde quotidienne",
    "analytics-web": "99,9 % · sauvegarde quotidienne",
}

_REVERSIBILITE: dict[str, dict[str, Any]] = {
    "drive-pro": {
        "formats": ["zip", "tar.gz", "WebDAV"],
        "delaiJours": 15,
        "docUrl": "https://docs.synelia.cloud/reversibilite/drive-pro",
    },
    "email-pro": {
        "formats": ["mbox", "ldif", "PST"],
        "delaiJours": 15,
        "docUrl": "https://docs.synelia.cloud/reversibilite/email-pro",
    },
    "visio": {
        "formats": ["tar.gz"],
        "delaiJours": 7,
        "docUrl": "https://docs.synelia.cloud/reversibilite/visio",
    },
    "ged": {
        "formats": ["zip", "csv"],
        "delaiJours": 15,
        "docUrl": "https://docs.synelia.cloud/reversibilite/ged",
    },
    "erp": {
        "formats": ["dump", "zip"],
        "delaiJours": 21,
        "docUrl": "https://docs.synelia.cloud/reversibilite/erp",
    },
    "crm": {
        "formats": ["csv", "zip"],
        "delaiJours": 15,
        "docUrl": "https://docs.synelia.cloud/reversibilite/crm",
    },
    "wordpress": {
        "formats": ["zip", "dump"],
        "delaiJours": 15,
        "docUrl": "https://docs.synelia.cloud/reversibilite/wordpress",
    },
    "prestashop": {
        "formats": ["zip", "dump"],
        "delaiJours": 21,
        "docUrl": "https://docs.synelia.cloud/reversibilite/prestashop",
    },
    "bi": {
        "formats": ["csv", "zip"],
        "delaiJours": 15,
        "docUrl": "https://docs.synelia.cloud/reversibilite/bi",
    },
    "forge": {
        "formats": ["git", "tar.gz"],
        "delaiJours": 15,
        "docUrl": "https://docs.synelia.cloud/reversibilite/forge",
    },
    "coffre": {
        "formats": ["vaultwarden-json", "csv"],
        "delaiJours": 15,
        "docUrl": "https://docs.synelia.cloud/reversibilite/coffre",
    },
    "automatisation": {
        "formats": ["json", "zip"],
        "delaiJours": 15,
        "docUrl": "https://docs.synelia.cloud/reversibilite/automatisation",
    },
    "analytics-web": {
        "formats": ["csv", "zip"],
        "delaiJours": 15,
        "docUrl": "https://docs.synelia.cloud/reversibilite/analytics-web",
    },
}

_VERSION_PAR_DEFAUT = {
    "drive-pro": "NC 28",
    "email-pro": "Stalwart 0.11",
    "visio": "Jitsi 2.0",
    "ged": "Mayan 4.5",
    "erp": "Odoo 17",
    "crm": "EspoCRM 8.4",
    "wordpress": "WP 6.6",
    "prestashop": "PS 8.2",
    "bi": "Metabase 0.50",
    "forge": "GitLab 17.4",
    "coffre": "Vaultwarden 1.32",
    "automatisation": "n8n 1.62",
    "analytics-web": "Matomo 5.1",
}

_PALIERS: dict[str, list[dict[str, Any]]] = {
    "drive-pro": [
        {
            "code": "starter",
            "nom": "Starter",
            "specs": "10 sièges · 500 Go/siège",
            "prixSiege": 3500,
            "limites": ["10 sièges"],
            "siegesMax": 10,
            "recommande": False,
        },
        {
            "code": "business",
            "nom": "Business",
            "specs": "50 sièges · 1 To/siège · restauration granulaire",
            "prixSiege": 3500,
            "limites": ["50 sièges"],
            "siegesMax": 50,
            "recommande": True,
        },
        {
            "code": "entreprise",
            "nom": "Entreprise",
            "specs": "Sièges illimités · supports dédiés",
            "prixSiege": 4500,
            "limites": ["illimité"],
            "siegesMax": None,
            "recommande": False,
        },
    ],
    "email-pro": [
        {
            "code": "starter",
            "nom": "Starter",
            "specs": "10 boîtes · 20 Go/boîte",
            "prixSiege": 2000,
            "limites": ["10 sièges"],
            "siegesMax": 10,
            "recommande": False,
        },
        {
            "code": "business",
            "nom": "Business",
            "specs": "100 boîtes · 50 Go/boîte · anti-spam",
            "prixSiege": 2000,
            "limites": ["100 sièges"],
            "siegesMax": 100,
            "recommande": True,
        },
        {
            "code": "entreprise",
            "nom": "Entreprise",
            "specs": "Boîtes illimitées · archivage",
            "prixSiege": 2500,
            "limites": ["illimité"],
            "siegesMax": None,
            "recommande": False,
        },
    ],
    "visio": [
        {
            "code": "starter",
            "nom": "Starter",
            "specs": "25 participants · 1 h/visio",
            "prixMois": 25000,
            "limites": ["25 participants"],
            "siegesMax": None,
            "recommande": False,
        },
        {
            "code": "entreprise",
            "nom": "Entreprise",
            "specs": "100 participants · relay TURN",
            "prixMois": 60000,
            "limites": ["100 participants"],
            "siegesMax": None,
            "recommande": True,
        },
    ],
    "ged": [
        {
            "code": "starter",
            "nom": "Starter",
            "specs": "20 utilisateurs · 500 to",
            "prixSiege": 4000,
            "limites": ["20 sièges"],
            "siegesMax": 20,
            "recommande": False,
        },
        {
            "code": "business",
            "nom": "Business",
            "specs": "100 utilisateurs · OCR",
            "prixSiege": 4000,
            "limites": ["100 sièges"],
            "siegesMax": 100,
            "recommande": True,
        },
    ],
    "erp": [
        {
            "code": "standard",
            "nom": "Standard",
            "specs": "1 site · 25 utilisateurs",
            "prixMois": 200000,
            "limites": ["25 utilisateurs"],
            "siegesMax": 25,
            "recommande": True,
        },
    ],
    "crm": [
        {
            "code": "starter",
            "nom": "Starter",
            "specs": "10 utilisateurs",
            "prixSiege": 3000,
            "limites": ["10 sièges"],
            "siegesMax": 10,
            "recommande": False,
        },
        {
            "code": "business",
            "nom": "Business",
            "specs": "50 utilisateurs · automatisations",
            "prixSiege": 3000,
            "limites": ["50 sièges"],
            "siegesMax": 50,
            "recommande": True,
        },
    ],
    "wordpress": [
        {
            "code": "starter",
            "nom": "Starter",
            "specs": "1 site · 10 Go · 20k visites",
            "prixMois": 15000,
            "limites": ["1 site"],
            "siegesMax": None,
            "recommande": False,
        },
        {
            "code": "business",
            "nom": "Business",
            "specs": "1 site · 50 Go · cache + WAF",
            "prixMois": 45000,
            "limites": ["1 site"],
            "siegesMax": None,
            "recommande": True,
        },
    ],
    "prestashop": [
        {
            "code": "standard",
            "nom": "Standard",
            "specs": "1 boutique · 50 Go",
            "prixMois": 55000,
            "limites": ["1 boutique"],
            "siegesMax": None,
            "recommande": True,
        },
    ],
    "bi": [
        {
            "code": "starter",
            "nom": "Starter",
            "specs": "10 utilisateurs",
            "prixSiege": 2500,
            "limites": ["10 sièges"],
            "siegesMax": 10,
            "recommande": False,
        },
        {
            "code": "business",
            "nom": "Business",
            "specs": "50 utilisateurs",
            "prixSiege": 2500,
            "limites": ["50 sièges"],
            "siegesMax": 50,
            "recommande": True,
        },
    ],
    "forge": [
        {
            "code": "standard",
            "nom": "Standard",
            "specs": "1 instance · 25 collaborateurs",
            "prixMois": 75000,
            "limites": ["25 collaborateurs"],
            "siegesMax": 25,
            "recommande": True,
        },
    ],
    "coffre": [
        {
            "code": "starter",
            "nom": "Starter",
            "specs": "10 utilisateurs",
            "prixSiege": 1500,
            "limites": ["10 sièges"],
            "siegesMax": 10,
            "recommande": False,
        },
        {
            "code": "business",
            "nom": "Business",
            "specs": "50 utilisateurs",
            "prixSiege": 1500,
            "limites": ["50 sièges"],
            "siegesMax": 50,
            "recommande": True,
        },
    ],
    "automatisation": [
        {
            "code": "starter",
            "nom": "Starter",
            "specs": "1k exécutions/mois",
            "prixSiege": 2000,
            "limites": ["1k exécutions"],
            "siegesMax": 10,
            "recommande": False,
        },
        {
            "code": "business",
            "nom": "Business",
            "specs": "10k exécutions/mois",
            "prixSiege": 2000,
            "limites": ["10k exécutions"],
            "siegesMax": 50,
            "recommande": True,
        },
    ],
    "analytics-web": [
        {
            "code": "starter",
            "nom": "Starter",
            "specs": "3 sites · 100k pages/mois",
            "prixMois": 12000,
            "limites": ["3 sites"],
            "siegesMax": None,
            "recommande": False,
        },
        {
            "code": "business",
            "nom": "Business",
            "specs": "10 sites · 1M pages/mois",
            "prixMois": 35000,
            "limites": ["10 sites"],
            "siegesMax": None,
            "recommande": True,
        },
    ],
}

_DESCRIPTIONS = {
    "drive-pro": "Partage de fichiers et de documents, gouverné par le portail.",
    "email-pro": "Messagerie professionnelle, boîtes et alias gouvernés.",
    "visio": "Visioconférence chiffrée, salles et droits gouvernés.",
    "ged": "Gestion électronique de documents, classement et rétention.",
    "erp": "ERP de gestion intégrée, modules et accès gouvernés.",
    "crm": "CRM pour piloter la relation client et les pipelines.",
    "wordpress": "Site WordPress, plugins et performances gouvernés.",
    "prestashop": "Boutique en ligne PrestaShop hébergée et gérée.",
    "bi": "Business Intelligence, tableaux de bord et sources de données.",
    "forge": "Forge GitLab pour le code, les CI/CD et les dépôts.",
    "coffre": "Gestionnaire de mots de passe chiffré pour l'équipe.",
    "automatisation": "Automatisation des flux de travail (n8n).",
    "analytics-web": "Mesure d'audience web souveraine (Matomo).",
}


def _palier(slug: str, code: str) -> dict[str, Any] | None:
    return next((p for p in _PALIERS.get(slug, []) if p["code"] == code), None)


def _cout(palier: dict[str, Any], sieges: int) -> int:
    if palier.get("prixMois") is not None:
        return int(palier["prixMois"])
    return int((palier.get("prixSiege") or 0) * max(1, sieges))


def fiche(slug: str) -> dict[str, Any] | None:
    cfg = _catalogue.configuration(slug)
    if cfg is None:
        return None
    solution = cfg.get("solution", "")
    description = _DESCRIPTIONS.get(slug, solution)
    return {
        "slug": slug,
        "nom": _NOMS.get(slug, slug),
        "solutionOSS": _SOLUTION_OSS.get(slug, solution),
        "categorie": _CATEGORIE.get(slug, "technique"),
        "description": description,
        "pitch": description,
        "modes": _MODES.get(slug, ["mutualise"]),
        "paliers": [
            {
                "code": p["code"],
                "nom": p["nom"],
                "specs": p["specs"],
                "prixSiege": p.get("prixSiege"),
                "prixMois": p.get("prixMois"),
                "limites": p["limites"],
                "recommande": p.get("recommande"),
            }
            for p in _PALIERS.get(slug, [])
        ],
        "sla": _SLA.get(slug, "99,9 %"),
        "reversibilite": _REVERSIBILITE.get(
            slug, {"formats": ["zip"], "delaiJours": 15, "docUrl": "https://docs.synelia.cloud"}
        ),
        "versionsSupportees": [_VERSION_PAR_DEFAUT.get(slug, "1.0")],
        "certifie": True,
        "logoInitiales": _NOMS.get(slug, slug)[:2].upper(),
    }


def fiches() -> list[dict[str, Any]]:
    return [fiche(s) for s in _catalogue.slugs() if fiche(s)]


def configuration(slug: str) -> dict[str, Any] | None:
    cfg = _catalogue.configuration(slug)
    if cfg is None:
        return None
    return {
        "slug": cfg["slug"],
        "solution": cfg["solution"],
        "intro": cfg["intro"],
        "horsPerimetre": cfg["horsPerimetre"],
        "sections": cfg["sections"],
    }


def _cles_configuration(slug: str) -> set[str]:
    cfg = _catalogue.configuration(slug)
    if not cfg:
        return set()
    return {ch["cle"] for s in cfg["sections"] for ch in s["champs"]}


def versions(slug: str, courante: str | None = None) -> list[dict[str, Any]]:
    base = _VERSION_PAR_DEFAUT.get(slug, "1.0")
    courante_v = courante or base
    suivante = f"{base} · prochaine"
    return [
        {
            "version": courante_v,
            "statut": "courante",
            "rollbackPossible": True,
            "notes": "Version installée",
        },
        {"version": suivante, "statut": "disponible", "rollbackPossible": True, "rupture": False},
    ]


def vers_utilisateur(u: Utilisateur) -> m.Utilisateur:
    return m.Utilisateur(
        id=u.id,
        email=u.email,
        nom=u.nom,
        mfaEnabled=u.mfa_active,
        idpSource=u.idp_source
        if isinstance(u.idp_source, str) and u.idp_source in {"local", "oidc", "saml", "ldap"}
        else "local",
        lastLoginAt=u.dernier_login_le,
        orgId=u.org_active_id,
        fonction=u.fonction,
        statut=u.statut if u.statut in {"actif", "invite", "suspendu"} else "actif",
    )


async def vers_service(ctx: Contexte, s: m.ServiceManage) -> m.ServiceManage:
    sieges = await depot_siege.tous(ctx, parent_id=s.id)
    actifs = sum(1 for x in sieges if x.statut == "actif")
    return s.model_copy(update={"siegesUtilises": actifs})


async def _url_native(slug: str, id8: str) -> str:
    return f"https://{slug}-{id8}.apps.synelia.cloud"


@executeur("service_manage.subscribe")
class ExecuteurServiceSubscribe(Executeur):
    async def terminer(self, ctx: Contexte, travail: Travail) -> None:
        sid = travail.cible_id or ""
        s = await depot_service.obtenir(ctx, sid)
        id8 = (sid or "")[:8]
        await depot_service.definir_statut(
            ctx, sid, "operationnel", urlNative=await _url_native(s.catalogSlug, id8)
        )


@executeur("service_manage.resilier")
class ExecuteurServiceResilier(Executeur):
    async def compenser(self, ctx: Contexte, travail: Travail, index_echoue: int) -> None:
        return None

    async def terminer(self, ctx: Contexte, travail: Travail) -> None:
        await depot_service.supprimer(ctx, travail.cible_id or "", logique=True)


BUCKET_EXPORTS_SERVICES = "synelia-exports-services-manages"


@executeur("service_manage.export")
class ExecuteurServiceExport(Executeur):
    """Avant ce correctif : classe vide — le travail passait `done` en ~2 étapes simulées
    sans jamais produire de fichier, et la ligne `ExportService` restait `en_cours` pour
    toujours (jamais `pret`/`echec`). Corrigé : dépose un export réel (représentation JSON
    du service — configuration/métadonnées, seule donnée dont ce module dispose ; pas de
    contenu applicatif fabriqué) et fait progresser le statut de la ligne d'export."""

    async def terminer(self, ctx: Contexte, travail: Travail) -> None:
        import asyncio
        import json

        sid = travail.cible_id or ""
        s = await depot_service.obtenir(ctx, sid)
        exports = await depot_export.tous(ctx, parent_id=sid)
        export = max(exports, key=lambda e: e.demandeLe, default=None) if exports else None
        if export is None:
            return
        try:
            from synelia.modules.stockage.service import amont_objet

            contenu = json.dumps(s.model_dump(mode="json"), ensure_ascii=False, indent=2).encode()
            cle = f"{sid}/{export.id}.json"
            await asyncio.to_thread(
                amont_objet().deposer_objet,
                BUCKET_EXPORTS_SERVICES,
                cle,
                contenu,
                "application/json",
            )
            await depot_export.modifier(ctx, export.id, {"statut": "pret"})
        except Exception:  # noqa: BLE001 — un export raté est un échec du sien, pas de l'API
            await depot_export.modifier(ctx, export.id, {"statut": "echec"})


@executeur("service_manage.mise_a_jour")
class ExecuteurServiceMiseAJour(Executeur):
    async def terminer(self, ctx: Contexte, travail: Travail) -> None:
        sid = travail.cible_id or ""
        nouvelle = travail.contexte.get("nouvelleVersion")
        if nouvelle:
            s = await depot_service.obtenir(ctx, sid)
            # Mémorise la version quittée pour permettre un vrai rollback ensuite — sans
            # ceci, `service_manage.rollback` n'avait littéralement aucune donnée à
            # restaurer (le catalogue de versions est fabriqué à la volée, pas un
            # historique réel).
            await depot_service.definir_secrets(ctx, sid, {"version_precedente": s.version})
            await depot_service.modifier(ctx, sid, {"version": nouvelle})


@executeur("service_manage.rollback")
class ExecuteurServiceRollback(Executeur):
    """Avant ce correctif : classe vide — le travail se déclarait `done` sans jamais
    restaurer quoi que ce soit, alors que le catalogue de versions (fabriqué, cf.
    `versions()`) prétendait toujours `rollbackPossible: True`. Restaure maintenant la
    version mémorisée par `service_manage.mise_a_jour` ; sans elle (aucune mise à jour
    n'a jamais eu lieu), échoue franchement plutôt que de prétendre avoir restauré quelque
    chose."""

    async def terminer(self, ctx: Contexte, travail: Travail) -> None:
        sid = travail.cible_id or ""
        try:
            secrets = await depot_service.secrets(ctx, sid)
        except Exception:  # noqa: BLE001
            secrets = {}
        precedente = secrets.get("version_precedente")
        if not precedente:
            raise erreurs.non_porte(
                "Aucune version antérieure mémorisée pour ce service : rien à restaurer."
            )
        await depot_service.modifier(ctx, sid, {"version": precedente})


@peupleur
async def demo_services(session, org, admin) -> None:  # type: ignore[no-untyped-def]
    sid = nouvel_id()
    id8 = sid[:8]
    s = m.ServiceManage(
        id=sid,
        orgId=org.id,
        catalogSlug="drive-pro",
        nom="Drive Pro",
        mode="mutualise",
        site="ABJ",
        palier="business",
        version="NC 28",
        domaine=f"drive-pro-{id8}.apps.synelia.cloud",
        urlNative=f"https://drive-pro-{id8}.apps.synelia.cloud",
        statut="operationnel",
        siegesSouscrits=2,
        siegesUtilises=2,
        sso=m.Sso(actif=True, clientId=id8, groupMappings=[]),
        uptime30j=99.9,
        parametres={},
        coutMensuel=7000,
        createdAt=maintenant(),
    )
    session.add(
        Ressource(
            id=sid,
            org_id=org.id,
            type="service_manage",
            nom=s.nom,
            statut=s.statut,
            donnees=s.model_dump(mode="json"),
        )
    )
    for i in range(2):
        siege = m.Siege(
            id=nouvel_id(),
            managedServiceId=sid,
            userId=f"demo-user-{i + 1}",
            utilisateur=None,
            statut="actif",
        )
        session.add(
            Ressource(
                id=siege.id,
                org_id=org.id,
                type="siege",
                nom=f"demo-user-{i + 1}",
                parent_id=sid,
                statut="actif",
                donnees=siege.model_dump(mode="json"),
            )
        )
