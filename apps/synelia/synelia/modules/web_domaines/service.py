from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from sqlalchemy import select
from synelia_contract import modeles as m
from synelia_db.modeles import Ressource, Travail
from synelia_kernel import erreurs
from synelia_kernel.dates import maintenant
from synelia_openstack.registrar import RegistrarSimule

from synelia.depot import Depot
from synelia.deps.contexte import Contexte
from synelia.travaux import Executeur, executeur

depot = Depot("web_domaine", m.Domaine, libelle="Domaine", champ_nom="nom")

AGREGATS = {
    "hebergement": ("web_hebergement", m.Hebergement),
    "zone": ("dns_zone", m.ZoneDns),
    "messagerie": ("web_messagerie", m.Messagerie),
    "drive": ("web_drive", m.Drive),
    "certificats": ("web_certificat", m.Certificat),
    "sites": ("web_site", m.SiteWeb),
}

PRIX_TLD = {".com": 9500, ".net": 8500, ".org": 8000, ".ci": 6500, ".africa": 7000}


def amont() -> RegistrarSimule:
    # URL-gaté (comme ACME/Zimbra/relais), pas `fournisseur()` : aucun partenaire
    # registrar n'existe sur le lab — basculer sur `RegistrarOvh` au seul motif
    # `SYNELIA_FOURNISSEUR=openstack` donnerait un faux « réel » (la classe reste le
    # simulé sous un autre nom tant qu'aucune URL partenaire n'est posée).
    from synelia_openstack.registrar import choisir_registrar

    return choisir_registrar()


async def exiger_nom_libre_global(ctx: Contexte, nom: str) -> None:
    r = await ctx.session.execute(
        select(Ressource).where(
            Ressource.type == "web_domaine", Ressource.nom == nom, Ressource.supprime_le.is_(None)
        )
    )
    if r.scalars().first() is not None or await depot.par_nom(ctx, nom) is not None:
        raise erreurs.nom_deja_pris(nom)


def prix_tld(extension: str) -> int:
    return PRIX_TLD.get(extension, 8000)


def expiration_dans(annees: int) -> date:
    return maintenant().date() + timedelta(days=365 * max(1, annees))


def tld_de(nom: str) -> str:
    return f".{nom.rsplit('.', 1)[-1].lower()}" if "." in nom else ""


async def agregats(ctx: Contexte, nom: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for cle, (type_, modele) in AGREGATS.items():
        if cle == "certificats":
            items = await Depot(type_, modele).tous(
                ctx,
                filtre=lambda c: (
                    getattr(c, "hote", None) == nom
                    or getattr(c, "validationDomaine", None) == nom
                    or getattr(c, "hebergementId", None) is not None
                ),
            )
            out[cle] = items or None
        elif cle == "sites":
            sites = await Depot("web_site", m.SiteWeb).tous(ctx)
            out[cle] = [s for s in sites if s.hote == nom] or None
        else:
            res = await Depot(type_, modele).tous(
                ctx, filtre=lambda r: getattr(r, "domaine", getattr(r, "nom", None)) == nom
            )
            out[cle] = res[0] if res else None
    return out


def _duree_annees(travail: Travail, defaut: int = 1) -> int:
    return int((travail.entree or {}).get("dureeAnnees") or defaut)


@executeur("domaine.commander")
class ExecuteurDomaineCommander(Executeur):
    async def terminer(self, ctx: Contexte, travail: Travail) -> None:
        d = await depot.obtenir(ctx, travail.cible_id or "")
        annees = _duree_annees(travail)
        resultat = amont().commander(d.nom, annees)
        if resultat.get("code_auth"):
            await depot.definir_secrets(ctx, d.id, {"code_auth": resultat["code_auth"]})
        await depot.remplacer(
            ctx,
            d.id,
            d.model_copy(
                update={"expiration": expiration_dans(annees), "provisionnement": "automatique"}
            ),
        )


@executeur("domaine.transferer")
class ExecuteurDomaineTransferer(Executeur):
    async def terminer(self, ctx: Contexte, travail: Travail) -> None:
        d = await depot.obtenir(ctx, travail.cible_id or "")
        code_auth = (travail.entree or {}).get("codeAuth", "")
        resultat = amont().transferer(d.nom, code_auth)
        if resultat.get("code_auth"):
            await depot.definir_secrets(ctx, d.id, {"code_auth": resultat["code_auth"]})
        await depot.remplacer(ctx, d.id, d.model_copy(update={"provisionnement": "automatique"}))


@executeur("domaine.renouveler")
class ExecuteurDomaineRenouveler(Executeur):
    async def terminer(self, ctx: Contexte, travail: Travail) -> None:
        d = await depot.obtenir(ctx, travail.cible_id or "")
        annees = _duree_annees(travail)
        amont().renouveler(d.nom, annees)
        aujourdhui = maintenant().date()
        base = d.expiration if d.expiration and d.expiration >= aujourdhui else aujourdhui
        nouvelle = base + timedelta(days=365 * max(1, annees))
        await depot.remplacer(ctx, d.id, d.model_copy(update={"expiration": nouvelle}))
