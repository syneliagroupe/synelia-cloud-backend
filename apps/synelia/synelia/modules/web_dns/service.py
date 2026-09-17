from __future__ import annotations

import asyncio

from synelia_contract import modeles as m
from synelia_kernel import erreurs
from synelia_kernel.ids import nouvel_id
from synelia_openstack import fournisseur
from synelia_openstack.designate import DesignateOpenStack, DesignateSimule

from synelia.depot import Depot
from synelia.deps.contexte import Contexte

depot = Depot("dns_zone", m.ZoneDns, libelle="Zone DNS", champ_nom="domaine")


def amont() -> DesignateSimule:
    """Designate — jusqu'ici ce module n'appelait aucun amont : une zone/enregistrement DNS
    créé ici n'existait que dans la base (« faux succès »), la même classe de bug que
    `vm.compose`/`web_ssl` avant leurs fixes. Le connecteur `DesignateSimule`/`DesignateOpenStack`
    existait déjà dans `packages/openstack` mais n'était jamais importé nulle part."""
    return fournisseur(DesignateSimule, DesignateOpenStack)

NS_DEFAUTS = ["ns1.synelia.cloud", "ns2.synelia.cloud"]

MODELES_DNS = [
    m.ModeleDns(
        id="courrier",
        nom="Courrier (MX + SPF + DKIM)",
        description="Ajoute les enregistrements de messagerie indispensables à votre domaine.",
        enregistrements=[
            m.EnregistrementDnsCreation(
                type="MX", nom="@", valeur="mail.synelia.cloud", ttl=3600, priorite=10
            ),
            m.EnregistrementDnsCreation(
                type="TXT", nom="@", valeur="v=spf1 include:spf.synelia.cloud ~all", ttl=3600
            ),
            m.EnregistrementDnsCreation(
                type="CNAME", nom="mail", valeur="mail.synelia.cloud", ttl=3600
            ),
        ],
        remplaceExistants=True,
    ),
    m.ModeleDns(
        id="dmarc",
        nom="DMARC",
        description="Stratégie DMARC de base pour protéger le domaine contre l'usurpation.",
        enregistrements=[
            m.EnregistrementDnsCreation(
                type="TXT",
                nom="_dmarc",
                valeur="v=DMARC1; p=none; rua=mailto:dmarc@synelia.cloud",
                ttl=3600,
            ),
        ],
    ),
    m.ModeleDns(
        id="sous-domaine-www",
        nom="Sous-domaine www",
        description="Redirige www vers le domaine racine.",
        enregistrements=[
            m.EnregistrementDnsCreation(type="CNAME", nom="www", valeur="@", ttl=3600)
        ],
    ),
]


async def creer_zone(ctx: Contexte, domaine: str) -> m.ZoneDns:
    # `amont().creer_zone` (Designate, openstacksdk synchrone, `wait_for_status` jusqu'à 60s)
    # est déchargé via `asyncio.to_thread` : même garde que `vms.service`, sans quoi un appel
    # amont lent gèlerait la boucle asyncio — donc l'API entière, tous tenants confondus.
    r = await asyncio.to_thread(amont().creer_zone, domaine)
    zone = m.ZoneDns(
        id=nouvel_id(),
        orgId=ctx.org_id,
        domaine=domaine,
        dnssec=False,
        ns=list(r.get("ns") or NS_DEFAUTS),
        enregistrements=[],
    )
    await depot.creer(ctx, zone)
    # `zone_id` amont (Designate) : distinct de l'id local dès qu'un vrai backend répond —
    # même motif que `serveur_id` sur les VM (`web_hebergement.service.serveur_id`).
    await depot.definir_secrets(ctx, zone.id, {"zone_id": str(r.get("id") or zone.id)})
    return await depot.obtenir(ctx, zone.id)


async def zone_id_amont(ctx: Contexte, zone: m.ZoneDns) -> str:
    """Identifiant Designate de la zone : dans les secrets (posé à la création), sinon l'id
    local (zone créée avant ce câblage réel, ou mode simulé)."""
    try:
        sec = await depot.secrets(ctx, zone.id)
    except Exception:  # noqa: BLE001
        sec = {}
    return str(sec.get("zone_id") or zone.id)


def _fqdn(nom: str, domaine: str) -> str:
    """Nom pleinement qualifié attendu par Designate (toujours terminé par un point)."""
    domaine = domaine.rstrip(".")
    if not nom or nom == "@":
        return f"{domaine}."
    return f"{nom.rstrip('.')}.{domaine}."


def _valeur_amont(e: m.EnregistrementDnsCreation, domaine: str) -> str:
    """Formate la donnée d'un enregistrement dans le format brut attendu par Designate :
    TXT entre guillemets, CNAME/NS/l'hôte d'un MX pleinement qualifiés (terminés par un
    point) — `@` désigne l'apex de la zone elle-même dans ces deux derniers cas."""
    valeur = domaine.rstrip(".") + "." if e.valeur == "@" else e.valeur
    if e.type == "TXT":
        return valeur if valeur.startswith('"') else f'"{valeur}"'
    if e.type in ("CNAME", "NS"):
        return valeur if valeur.endswith(".") else f"{valeur}."
    if e.type == "MX":
        cible = valeur if valeur.endswith(".") else f"{valeur}."
        return f"{e.priorite or 10} {cible}"
    return valeur


async def _creer_enregistrement_amont(
    ctx: Contexte, zone: m.ZoneDns, e: m.EnregistrementDnsCreation
) -> m.EnregistrementDns:
    zid = await zone_id_amont(ctx, zone)
    r = await asyncio.to_thread(
        amont().creer_enregistrement,
        zid,
        _fqdn(e.nom, zone.domaine),
        e.type,
        [_valeur_amont(e, zone.domaine)],
        e.ttl or 3600,
    )
    return enregistrement_vers(zone, e, str(r.get("id")) if r.get("id") else None)


def enregistrement_vers(
    zone: m.ZoneDns, e: m.EnregistrementDnsCreation, id_: str | None = None
) -> m.EnregistrementDns:
    return m.EnregistrementDns(
        id=id_ or nouvel_id(),
        type=e.type,
        nom=e.nom,
        valeur=e.valeur,
        ttl=e.ttl or 3600,
        priorite=e.priorite,
    )


async def modifier_enregistrement_amont(
    ctx: Contexte, zone: m.ZoneDns, enregistrement_id: str, e: m.EnregistrementDnsCreation
) -> None:
    zid = await zone_id_amont(ctx, zone)
    await asyncio.to_thread(
        amont().modifier_enregistrement,
        zid,
        enregistrement_id,
        [_valeur_amont(e, zone.domaine)],
        e.ttl or 3600,
    )


async def supprimer_enregistrement_amont(
    ctx: Contexte, zone: m.ZoneDns, enregistrement_id: str
) -> None:
    zid = await zone_id_amont(ctx, zone)
    await asyncio.to_thread(amont().supprimer_enregistrement, zid, enregistrement_id)


async def appliquer_enregistrements(
    ctx: Contexte,
    zone_id: str,
    enregistrements: list[m.EnregistrementDnsCreation],
    remplacer: bool = False,
) -> m.ZoneDns:
    zone = await depot.obtenir(ctx, zone_id)
    if remplacer:
        for ancien in zone.enregistrements:
            try:
                await supprimer_enregistrement_amont(ctx, zone, ancien.id)
            except Exception:  # noqa: BLE001, S110 — best effort, ne bloque pas le remplacement
                pass
    nouveaux = [await _creer_enregistrement_amont(ctx, zone, e) for e in enregistrements]
    liste = nouveaux if remplacer else [*zone.enregistrements, *nouveaux]
    await depot.remplacer(ctx, zone_id, zone.model_copy(update={"enregistrements": liste}))
    return await depot.obtenir(ctx, zone_id)


def verifier_non_duplique(zone: m.ZoneDns, e: m.EnregistrementDnsCreation) -> None:
    if any(r.nom == e.nom and r.type == e.type for r in zone.enregistrements):
        raise erreurs.conflit(
            "Cet enregistrement existe déjà sur la zone.", code="enregistrement_deja_present"
        )
