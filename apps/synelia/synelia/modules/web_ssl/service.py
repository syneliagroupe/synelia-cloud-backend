"""Règles SSL (Web Cloud) : offre, dépôt, exécuteurs, amont ACME."""

from __future__ import annotations

import asyncio
from datetime import date, timedelta

from synelia_contract import modeles as m
from synelia_db.modeles import Travail
from synelia_openstack import acme

from synelia.depot import Depot
from synelia.deps.contexte import Contexte
from synelia.travaux import Executeur, executeur

depot = Depot(
    "web_certificat",
    m.Certificat,
    libelle="Certificat",
    champ_nom="hote",
    champ_statut="etat",
    champs_recherche=("hote", "hebergementId"),
)

DEFAULT_EMETTEUR = "Sectigo"
ALTERNATE_EMETTEUR = "Let's Encrypt"

OFFRES = [
    {
        "type": "letsencrypt",
        "nom": "DV — Let's Encrypt",
        "emetteur": ALTERNATE_EMETTEUR,
        "prixAnnuel": 0,
        "delaiEmission": "~ 3 min",
        "garantie": "Sans garantie commerciale",
        "caracteristiques": [
            "Validation DNS ou HTTP",
            "Durée 90 jours, renouvellement automatique",
            "Un hôte",
        ],
    },
    {
        "type": "dv",
        "nom": "DV — Validation de domaine",
        "emetteur": DEFAULT_EMETTEUR,
        "prixAnnuel": 35000,
        "delaiEmission": "~ 30 min",
        "garantie": "500 000 FCFA",
        "caracteristiques": ["Validation par email, DNS ou HTTP", "Durée 1 an", "Un hôte"],
    },
    {
        "type": "wildcard",
        "nom": "Wildcard — *.<domaine>",
        "emetteur": DEFAULT_EMETTEUR,
        "prixAnnuel": 75000,
        "delaiEmission": "~ 2 h",
        "garantie": "500 000 FCFA",
        "caracteristiques": ["Sous-domaines illimités", "Validation DNS", "Durée 1 an"],
    },
    {
        "type": "ov",
        "nom": "OV — Validation d'organisation",
        "emetteur": DEFAULT_EMETTEUR,
        "prixAnnuel": 150000,
        "delaiEmission": "1 à 3 jours",
        "garantie": "1 250 000 FCFA",
        "caracteristiques": ["Validation de l'organisation", "Durée 1 an", "Jusqu'à 3 hôtes"],
    },
    {
        "type": "ev",
        "nom": "EV — Validation étendue",
        "emetteur": DEFAULT_EMETTEUR,
        "prixAnnuel": 350000,
        "delaiEmission": "2 à 5 jours",
        "garantie": "1 750 000 FCFA",
        "caracteristiques": [
            "Barre d'adresse verte",
            "Validation étendue d'organisation",
            "Durée 1 an",
        ],
    },
]


def amont() -> acme.AcmeSimule:
    return acme.choisir_acme()


def offre(type_: str) -> dict:
    return next((o for o in OFFRES if o["type"] == type_), OFFRES[0])


def duree_jours(type_: str, duree_annees: int | None) -> int:
    if type_ == "letsencrypt":
        return 90
    return 365 * (duree_annees or 1)


def nova_expiration(type_: str, duree_annees: int | None) -> date:
    return date.today() + timedelta(days=duree_jours(type_, duree_annees))


@executeur("web.ssl.renew")
class ExecuteurCertificatRenew(Executeur):
    async def terminer(self, ctx: Contexte, travail: Travail) -> None:
        c = await depot.obtenir(ctx, travail.cible_id or "")
        duree = travail.contexte.get("duree_jours", duree_jours(c.type, None))
        # Sans cet appel, un renouvellement ne touchait que la fiche DB (date d'expiration
        # avancée) : le certificat réellement en place côté ACME n'était jamais renouvelé —
        # même classe de bug (« faux succès ») que `vm.resize` avant son fix (redimensionnement
        # DB-only, VM Nova inchangée).
        # `AcmeReel` (`httpx.post` synchrone) est déchargé via `asyncio.to_thread` : même garde
        # que `vms.service`, sans quoi un appel amont lent gèlerait la boucle asyncio — donc
        # l'API entière, tous tenants confondus.
        r = await asyncio.to_thread(amont().renouveler, c.hote, 0 if c.type == "letsencrypt" else 1)
        jours = r.get("expirationJours") or duree
        await depot.modifier(
            ctx,
            c.id,
            {
                "etat": "actif",
                "emisLe": date.today(),
                "expire": date.today() + timedelta(days=jours),
            },
        )


@executeur("web.ssl.commande")
class ExecuteurCertificatCommande(Executeur):
    async def etape(self, ctx: Contexte, travail: Travail, index: int, nom: str) -> str | None:
        if index == 0:
            c = await depot.obtenir(ctx, travail.cible_id or "")
            entre = travail.entree or {}
            # `commander` publie l'enregistrement de validation (TXT `_acme-challenge…` en
            # DNS ou jeton HTTP selon `validationDomaine`) auprès de l'autorité, `valider` le
            # fait vérifier : sans ces deux appels, le certificat n'était jamais réellement
            # émis, seule la fiche DB passait à `actif` (constaté ici, même piège que
            # `web_dns` avant son câblage Designate — un connecteur `AcmeReel` déjà écrit,
            # mais jamais appelé par l'exécuteur qui rend le travail « réussi »).
            r = await asyncio.to_thread(
                amont().commander, c.hote, c.type, entre.get("validationDomaine") or "dns"
            )
            await asyncio.to_thread(amont().valider, c.hote)
            ctxt = dict(travail.contexte)
            ctxt["expiration_jours_amont"] = r.get("expirationJours")
            travail.contexte = ctxt
            return f"Certificat commandé et validé auprès de {c.emetteur}"
        return None

    async def terminer(self, ctx: Contexte, travail: Travail) -> None:
        c = await depot.obtenir(ctx, travail.cible_id or "")
        duree = travail.contexte.get("expiration_jours_amont") or travail.contexte.get(
            "duree_jours", duree_jours(c.type, None)
        )
        await depot.modifier(
            ctx,
            c.id,
            {
                "etat": "actif",
                "emisLe": date.today(),
                "expire": date.today() + timedelta(days=duree),
            },
        )


ETAPES_COMMANDE = [
    {"nom": "Publier la demande de signature", "dureeS": 5},
    {"nom": "Soumettre à l'autorité de certification", "dureeS": 12},
    {"nom": "Émettre le certificat", "dureeS": 22},
    {"nom": "Installer et recharger le serveur", "dureeS": 8},
]
