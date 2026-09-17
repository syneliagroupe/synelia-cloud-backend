from __future__ import annotations

import ipaddress
from typing import Any

from fastapi import APIRouter, Depends, Response, status
from synelia_contract import modeles as m
from synelia_kernel import erreurs
from synelia_kernel.dates import maintenant
from synelia_kernel.ids import nouvel_id

from synelia.audit import journaliser
from synelia.deps import Contexte, Page, exige, exiger_confirmation
from synelia.modules.reseau.service import (
    ajouter_regle_amont,
    associer_ip_amont,
    attacher_groupe_amont,
    creer_groupe_amont,
    creer_reseau_amont,
    depot_groupe,
    depot_ip,
    depot_lb,
    depot_reseau,
    depot_vpn,
    dissocier_ip_amont,
    liberer_ip_amont,
    metriques_vides,
    reserver_ip_amont,
    resoudre_cible_attachement_ip,
    sante_defaut,
    supprimer_groupe_amont,
    supprimer_lb_amont,
    supprimer_regle_amont,
    supprimer_reseau_amont,
    synchroniser_pool_amont,
)
from synelia.travaux import demarrer_travail

router_reseaux = APIRouter(prefix="/reseaux", tags=["Réseau"])
router_ips = APIRouter(prefix="/ips", tags=["Réseau"])
router_groupes = APIRouter(prefix="/groupes-securite", tags=["Réseau"])
router_lb = APIRouter(prefix="/load-balancers", tags=["Réseau"])
router_vpn = APIRouter(prefix="/vpn", tags=["Réseau"])

_rbac_lecture = "org.dashboard.view"


# ── Réseaux ────────────────────────────────────────────────────────────────
@router_reseaux.get("", response_model=m.ReseauxGetResponse, response_model_exclude_none=True)
async def lister_reseaux(
    page: Page,
    espaceId: str | None = None,
    ctx: Contexte = Depends(exige(_rbac_lecture, lecture=True)),
) -> Any:  # noqa: N803
    return await depot_reseau.lister(
        ctx, page, filtre=lambda r: not espaceId or r.espaceId == espaceId, tri_defaut="nom"
    )


@router_reseaux.post(
    "",
    response_model=m.Reseau,
    status_code=status.HTTP_201_CREATED,
    response_model_exclude_none=True,
)
async def creer_reseau(
    corps: m.ReseauCreation, ctx: Contexte = Depends(exige("network.manage"))
) -> Any:
    await depot_reseau.exiger_nom_libre(ctx, corps.nom)
    _valider_cidr(corps.cidr)
    secrets_amont = await creer_reseau_amont(ctx, corps.espaceId, corps.nom, corps.cidr)
    reseau = m.Reseau(
        id=nouvel_id(),
        espaceId=corps.espaceId,
        nom=corps.nom,
        cidr=corps.cidr,
        dnsInterne=corps.dnsInterne or False,
        workloads=0,
        vlan=corps.vlan,
    )
    await depot_reseau.creer(ctx, reseau, secrets=secrets_amont)
    await journaliser(
        ctx, action="reseau.creation", cible_type="reseau", cible_id=reseau.id, cible=reseau.nom
    )
    return reseau


@router_reseaux.get("/{reseauId}", response_model=m.Reseau, response_model_exclude_none=True)
async def obtenir_reseau(
    reseauId: str, ctx: Contexte = Depends(exige(_rbac_lecture, lecture=True))
) -> Any:  # noqa: N803
    return await depot_reseau.obtenir(ctx, reseauId)


@router_reseaux.patch("/{reseauId}", response_model=m.Reseau, response_model_exclude_none=True)
async def modifier_reseau(
    reseauId: str, corps: m.ReseauCreation, ctx: Contexte = Depends(exige("network.manage"))
) -> Any:  # noqa: N803
    r = await depot_reseau.obtenir(ctx, reseauId)
    if corps.nom and corps.nom != r.nom:
        await depot_reseau.exiger_nom_libre(ctx, corps.nom)
    if corps.cidr:
        _valider_cidr(corps.cidr)
    await depot_reseau.modifier(ctx, reseauId, corps)
    await journaliser(
        ctx,
        action="reseau.modification",
        cible_type="reseau",
        cible_id=reseauId,
        details=corps.model_dump(mode="json", exclude_none=True),
    )
    return await depot_reseau.obtenir(ctx, reseauId)


@router_reseaux.delete("/{reseauId}", status_code=status.HTTP_204_NO_CONTENT)
async def supprimer_reseau(
    reseauId: str, confirmation: str | None = None, ctx: Contexte = Depends(exige("network.manage"))
) -> Response:  # noqa: N803
    r = await depot_reseau.obtenir(ctx, reseauId)
    exiger_confirmation(r.nom, confirmation)
    await supprimer_reseau_amont(ctx, reseauId)
    await journaliser(
        ctx, action="reseau.suppression", cible_type="reseau", cible_id=reseauId, cible=r.nom
    )
    await depot_reseau.supprimer(ctx, reseauId)
    return Response(status_code=204)


# ── IP publiques ───────────────────────────────────────────────────────────
@router_ips.get("", response_model=m.IpsGetResponse, response_model_exclude_none=True)
async def lister_ips(
    page: Page,
    espaceId: str | None = None,
    attachee: bool | None = None,
    ctx: Contexte = Depends(exige(_rbac_lecture, lecture=True)),
) -> Any:  # noqa: N803
    return await depot_ip.lister(
        ctx,
        page,
        filtre=lambda ip: (
            (not espaceId or ip.espaceId == espaceId)
            and (attachee is None or bool(ip.attachedTo) == attachee)
        ),
        tri_defaut="adresse",
    )


@router_ips.post(
    "",
    response_model=m.IpPublique,
    status_code=status.HTTP_201_CREATED,
    response_model_exclude_none=True,
)
async def reserver_ip(
    corps: m.IpPubliqueReservation, ctx: Contexte = Depends(exige("network.manage"))
) -> Any:
    amont_ip = await reserver_ip_amont(ctx, corps.espaceId)
    ip = m.IpPublique(
        id=nouvel_id(),
        espaceId=corps.espaceId,
        adresse=amont_ip["adresse"],
        ptr=corps.ptr,
        attachedTo=None,
        attachedLabel=None,
        antiDdos=corps.antiDdos,
    )
    await depot_ip.creer(ctx, ip, secrets={"ip_flottante_id": amont_ip["id"]})
    await journaliser(
        ctx, action="ip.reservation", cible_type="ip_publique", cible_id=ip.id, cible=ip.adresse
    )
    return ip


@router_ips.get("/{ipId}", response_model=m.IpPublique, response_model_exclude_none=True)
async def obtenir_ip(ipId: str, ctx: Contexte = Depends(exige(_rbac_lecture, lecture=True))) -> Any:  # noqa: N803
    return await depot_ip.obtenir(ctx, ipId)


@router_ips.patch("/{ipId}", response_model=m.IpPublique, response_model_exclude_none=True)
async def modifier_ip(
    ipId: str, corps: m.IpPubliqueReservation, ctx: Contexte = Depends(exige("network.manage"))
) -> Any:  # noqa: N803
    await depot_ip.obtenir(ctx, ipId)
    await depot_ip.modifier(ctx, ipId, corps)
    await journaliser(
        ctx,
        action="ip.modification",
        cible_type="ip_publique",
        cible_id=ipId,
        details=corps.model_dump(mode="json", exclude_none=True),
    )
    return await depot_ip.obtenir(ctx, ipId)


@router_ips.delete("/{ipId}", status_code=status.HTTP_204_NO_CONTENT)
async def liberer_ip(
    ipId: str, confirmation: str | None = None, ctx: Contexte = Depends(exige("network.manage"))
) -> Response:  # noqa: N803
    ip = await depot_ip.obtenir(ctx, ipId)
    exiger_confirmation(ip.adresse, confirmation)
    await liberer_ip_amont(ctx, ipId)
    await journaliser(
        ctx, action="ip.liberation", cible_type="ip_publique", cible_id=ipId, cible=ip.adresse
    )
    await depot_ip.supprimer(ctx, ipId)
    return Response(status_code=204)


@router_ips.put(
    "/{ipId}/attachement", response_model=m.IpPublique, response_model_exclude_none=True
)
async def attacher_ip(
    ipId: str,
    corps: m.IpsIpIdAttachementPutRequest,
    ctx: Contexte = Depends(exige("network.manage")),
) -> Any:  # noqa: N803
    ip = await depot_ip.obtenir(ctx, ipId)
    if ip.attachedTo:
        raise erreurs.conflit(
            "Cette IP est déjà attachée à une ressource.", code="ip_deja_attachee"
        )
    cible_type, label = await resoudre_cible_attachement_ip(ctx, corps.cibleId)
    from synelia_openstack.erreurs import traduire

    try:
        await associer_ip_amont(ctx, ipId, corps.cibleId, cible_type)
    except erreurs.AppError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise traduire(exc, "IP publique") from None
    changement: dict[str, Any] = {"attachedTo": corps.cibleId, "attachedLabel": label}
    if corps.ptr is not None:
        changement["ptr"] = corps.ptr
    await depot_ip.modifier(ctx, ipId, changement)
    await journaliser(
        ctx, action="ip.attachement", cible_type="ip_publique", cible_id=ipId, cible=label
    )
    return await depot_ip.obtenir(ctx, ipId)


@router_ips.delete(
    "/{ipId}/attachement", response_model=m.IpPublique, response_model_exclude_none=True
)
async def detacher_ip(ipId: str, ctx: Contexte = Depends(exige("network.manage"))) -> Any:  # noqa: N803
    ip = await depot_ip.obtenir(ctx, ipId)
    await dissocier_ip_amont(ctx, ipId)
    await depot_ip.remplacer(
        ctx, ipId, ip.model_copy(update={"attachedTo": None, "attachedLabel": None})
    )
    await journaliser(
        ctx, action="ip.detachement", cible_type="ip_publique", cible_id=ipId, cible=ip.adresse
    )
    return await depot_ip.obtenir(ctx, ipId)


# ── Groupes de sécurité ────────────────────────────────────────────────────
@router_groupes.get(
    "", response_model=m.GroupesSecuriteGetResponse, response_model_exclude_none=True
)
async def lister_groupes_securite(
    page: Page,
    espaceId: str | None = None,
    ctx: Contexte = Depends(exige(_rbac_lecture, lecture=True)),
) -> Any:  # noqa: N803
    return await depot_groupe.lister(
        ctx, page, filtre=lambda g: not espaceId or g.espaceId == espaceId, tri_defaut="nom"
    )


@router_groupes.post(
    "",
    response_model=m.GroupeSecurite,
    status_code=status.HTTP_201_CREATED,
    response_model_exclude_none=True,
)
async def creer_groupe_securite(
    corps: m.GroupeSecuriteCreation, ctx: Contexte = Depends(exige("network.manage"))
) -> Any:
    await depot_groupe.exiger_nom_libre(ctx, corps.nom)
    pp = corps.defaultPolicy
    politique = (
        m.DefaultPolicy(ingress=pp.ingress or "deny", egress=pp.egress or "deny")
        if pp
        else m.DefaultPolicy(ingress="deny", egress="deny")
    )
    groupe = m.GroupeSecurite(
        id=nouvel_id(),
        espaceId=corps.espaceId,
        nom=corps.nom,
        description=corps.description,
        defaultPolicy=politique,
        rules=list(corps.rules or []),
        attaches=0,
    )
    from synelia_openstack.erreurs import traduire

    try:
        gid = await creer_groupe_amont(ctx, corps.espaceId, groupe.nom, groupe.description)
    except erreurs.AppError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise traduire(exc, "Groupe de sécurité") from None
    await depot_groupe.creer(ctx, groupe, secrets={"groupe_id": gid})
    for regle in groupe.rules:
        await ajouter_regle_amont(ctx, groupe.id, regle)
    await journaliser(
        ctx,
        action="groupe.creation",
        cible_type="groupe_securite",
        cible_id=groupe.id,
        cible=groupe.nom,
    )
    return groupe


@router_groupes.get(
    "/{groupeId}", response_model=m.GroupeSecurite, response_model_exclude_none=True
)
async def obtenir_groupe_securite(
    groupeId: str, ctx: Contexte = Depends(exige(_rbac_lecture, lecture=True))
) -> Any:  # noqa: N803
    return await depot_groupe.obtenir(ctx, groupeId)


@router_groupes.patch(
    "/{groupeId}", response_model=m.GroupeSecurite, response_model_exclude_none=True
)
async def modifier_groupe_securite(
    groupeId: str, corps: m.GroupeSecuriteCreation, ctx: Contexte = Depends(exige("network.manage"))
) -> Any:  # noqa: N803
    g = await depot_groupe.obtenir(ctx, groupeId)
    if corps.nom and corps.nom != g.nom:
        await depot_groupe.exiger_nom_libre(ctx, corps.nom)
    changement: dict[str, Any] = {}
    if corps.nom is not None:
        changement["nom"] = corps.nom
    if corps.description is not None:
        changement["description"] = corps.description
    if corps.defaultPolicy is not None:
        changement["defaultPolicy"] = m.DefaultPolicy(
            ingress=corps.defaultPolicy.ingress or g.defaultPolicy.ingress,
            egress=corps.defaultPolicy.egress or g.defaultPolicy.egress,
        ).model_dump()
    if corps.rules is not None:
        changement["rules"] = [r.model_dump() for r in corps.rules]
    await depot_groupe.modifier(ctx, groupeId, changement)
    await journaliser(
        ctx,
        action="groupe.modification",
        cible_type="groupe_securite",
        cible_id=groupeId,
        details=corps.model_dump(mode="json", exclude_none=True),
    )
    return await depot_groupe.obtenir(ctx, groupeId)


@router_groupes.delete("/{groupeId}", status_code=status.HTTP_204_NO_CONTENT)
async def supprimer_groupe_securite(
    groupeId: str, confirmation: str | None = None, ctx: Contexte = Depends(exige("network.manage"))
) -> Response:  # noqa: N803
    g = await depot_groupe.obtenir(ctx, groupeId)
    exiger_confirmation(g.nom, confirmation)
    if g.attaches:
        # Neutron refuse de supprimer un security group encore attaché à un port (constaté en
        # direct : `ConflictException` brute remontée en 500 sans ce garde-fou) — même famille
        # de contrôle que `espace_non_vide`/`volume_attache` ailleurs dans l'API.
        raise erreurs.conflit(
            "Le groupe de sécurité est encore attaché à une ou plusieurs ressources.",
            code="groupe_attache",
        )
    await supprimer_groupe_amont(ctx, groupeId)
    await journaliser(
        ctx,
        action="groupe.suppression",
        cible_type="groupe_securite",
        cible_id=groupeId,
        cible=g.nom,
    )
    await depot_groupe.supprimer(ctx, groupeId)
    return Response(status_code=204)


@router_groupes.put(
    "/{groupeId}/attachements", response_model=m.GroupeSecurite, response_model_exclude_none=True
)
async def attacher_groupe_securite(
    groupeId: str,
    corps: m.GroupesSecuriteGroupeIdAttachementsPutRequest,
    ctx: Contexte = Depends(exige("network.manage")),
) -> Any:  # noqa: N803
    await depot_groupe.obtenir(ctx, groupeId)
    await attacher_groupe_amont(ctx, groupeId, corps.cibles)
    await depot_groupe.modifier(ctx, groupeId, {"attaches": len(corps.cibles)})
    await journaliser(
        ctx, action="groupe.attachement", cible_type="groupe_securite", cible_id=groupeId
    )
    return await depot_groupe.obtenir(ctx, groupeId)


@router_groupes.post(
    "/{groupeId}/regles",
    response_model=m.GroupeSecurite,
    status_code=status.HTTP_201_CREATED,
    response_model_exclude_none=True,
)
async def ajouter_regle_securite(
    groupeId: str, corps: m.RegleSecurite, ctx: Contexte = Depends(exige("network.manage"))
) -> Any:  # noqa: N803
    g = await depot_groupe.obtenir(ctx, groupeId)
    if any(r.id == corps.id for r in g.rules):
        raise erreurs.conflit("Une règle porte déjà cet identifiant.", code="regle_existante")
    from synelia_openstack.erreurs import traduire

    try:
        await ajouter_regle_amont(ctx, groupeId, corps)
    except erreurs.AppError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise traduire(exc, "Groupe de sécurité") from None
    regles = [*list(g.rules), corps]
    await depot_groupe.modifier(ctx, groupeId, {"rules": [r.model_dump() for r in regles]})
    await journaliser(
        ctx,
        action="groupe.regle.ajout",
        cible_type="groupe_securite",
        cible_id=groupeId,
        details={"regleId": corps.id},
    )
    return await depot_groupe.obtenir(ctx, groupeId)


@router_groupes.put(
    "/{groupeId}/regles/{regleId}",
    response_model=m.GroupeSecurite,
    response_model_exclude_none=True,
)
async def modifier_regle_securite(
    groupeId: str,
    regleId: str,
    corps: m.RegleSecurite,
    ctx: Contexte = Depends(exige("network.manage")),
) -> Any:  # noqa: N803
    g = await depot_groupe.obtenir(ctx, groupeId)
    if not any(r.id == regleId for r in g.rules):
        raise erreurs.introuvable("Règle de sécurité", regleId)
    # Une règle Neutron est immuable : « modifier » revient à la remplacer côté amont.
    await supprimer_regle_amont(ctx, groupeId, regleId)
    await ajouter_regle_amont(ctx, groupeId, corps)
    regles = [(corps if r.id == regleId else r) for r in g.rules]
    await depot_groupe.modifier(ctx, groupeId, {"rules": [r.model_dump() for r in regles]})
    await journaliser(
        ctx,
        action="groupe.regle.modification",
        cible_type="groupe_securite",
        cible_id=groupeId,
        details={"regleId": regleId},
    )
    return await depot_groupe.obtenir(ctx, groupeId)


@router_groupes.delete("/{groupeId}/regles/{regleId}", status_code=status.HTTP_204_NO_CONTENT)
async def supprimer_regle_securite(
    groupeId: str, regleId: str, ctx: Contexte = Depends(exige("network.manage"))
) -> Response:  # noqa: N803
    g = await depot_groupe.obtenir(ctx, groupeId)
    reste = [r for r in g.rules if r.id != regleId]
    if len(reste) == len(g.rules):
        raise erreurs.introuvable("Règle de sécurité", regleId)
    await supprimer_regle_amont(ctx, groupeId, regleId)
    await depot_groupe.modifier(ctx, groupeId, {"rules": [r.model_dump() for r in reste]})
    await journaliser(
        ctx,
        action="groupe.regle.suppression",
        cible_type="groupe_securite",
        cible_id=groupeId,
        details={"regleId": regleId},
    )
    return Response(status_code=204)


# ── Load balancers ─────────────────────────────────────────────────────────
@router_lb.get("", response_model=m.LoadBalancersGetResponse, response_model_exclude_none=True)
async def lister_load_balancers(
    page: Page,
    espaceId: str | None = None,
    layer: str | None = None,
    exposure: str | None = None,
    ctx: Contexte = Depends(exige(_rbac_lecture, lecture=True)),
) -> Any:  # noqa: N803
    return await depot_lb.lister(
        ctx,
        page,
        filtre=lambda lb: (
            (not espaceId or lb.espaceId == espaceId)
            and (not layer or lb.layer == layer)
            and (not exposure or lb.exposure == exposure)
        ),
        tri_defaut="nom",
    )


def _exiger_amont_lb(corps: m.LoadBalancerCreation) -> None:
    if corps.waf is not None or corps.rateLimit is not None:
        raise erreurs.non_porte(
            "Le WAF et le rate limiting ne sont pas portés par cette plateforme."
        )


@router_lb.post(
    "",
    response_model=m.TravailProvisioning,
    status_code=status.HTTP_202_ACCEPTED,
    response_model_exclude_none=True,
)
async def creer_load_balancer(
    corps: m.LoadBalancerCreation, ctx: Contexte = Depends(exige("lb.create"))
) -> Any:
    await depot_lb.exiger_nom_libre(ctx, corps.nom)
    _exiger_amont_lb(corps)
    lb = m.LoadBalancer(
        id=nouvel_id(),
        espaceId=corps.espaceId,
        nom=corps.nom,
        layer=corps.layer,
        exposure=corps.exposure,
        vip="",
        algo=corps.algo or "round_robin",
        sticky=corps.sticky,
        listeners=[
            m.Listener(
                protocole=ln.protocole or "tcp",
                port=ln.port or 80,
                certId=ln.certId,
                tlsMin=ln.tlsMin,
            )
            for ln in corps.listeners or []
        ],
        pool=[],
        healthCheck=sante_defaut(),
        waf=None,
        rateLimit=None,
        metriques=metriques_vides(),
        reglesL7=None,
    )
    await depot_lb.creer(ctx, lb)
    await journaliser(
        ctx, action="lb.creation", cible_type="load_balancer", cible_id=lb.id, cible=lb.nom
    )
    return await demarrer_travail(
        ctx,
        "lb.create",
        lb.nom,
        cible_type="load_balancer",
        cible_id=lb.id,
        entree=corps.model_dump(mode="json"),
    )


@router_lb.get("/{lbId}", response_model=m.LoadBalancer, response_model_exclude_none=True)
async def obtenir_load_balancer(
    lbId: str, ctx: Contexte = Depends(exige(_rbac_lecture, lecture=True))
) -> Any:  # noqa: N803
    return await depot_lb.obtenir(ctx, lbId)


@router_lb.patch("/{lbId}", response_model=m.LoadBalancer, response_model_exclude_none=True)
async def modifier_load_balancer(
    lbId: str, corps: m.LoadBalancerCreation, ctx: Contexte = Depends(exige("lb.create"))
) -> Any:  # noqa: N803
    lb = await depot_lb.obtenir(ctx, lbId)
    if corps.nom and corps.nom != lb.nom:
        await depot_lb.exiger_nom_libre(ctx, corps.nom)
    _exiger_amont_lb(corps)
    changement = corps.model_dump(mode="json", exclude_none=True)
    if "cibles" in changement:
        changement.pop("cibles")
    if "listeners" in changement:
        changement["listeners"] = [
            m.Listener(
                protocole=ln.protocole or "tcp",
                port=ln.port or 80,
                certId=ln.certId,
                tlsMin=ln.tlsMin,
            ).model_dump()
            for ln in corps.listeners or []
        ]
    await depot_lb.modifier(ctx, lbId, changement)
    await journaliser(
        ctx,
        action="lb.modification",
        cible_type="load_balancer",
        cible_id=lbId,
        details=corps.model_dump(mode="json", exclude_none=True),
    )
    return await depot_lb.obtenir(ctx, lbId)


@router_lb.delete("/{lbId}", status_code=status.HTTP_204_NO_CONTENT)
async def supprimer_load_balancer(
    lbId: str, confirmation: str | None = None, ctx: Contexte = Depends(exige("lb.create"))
) -> Response:  # noqa: N803
    lb = await depot_lb.obtenir(ctx, lbId)
    exiger_confirmation(lb.nom, confirmation)
    await supprimer_lb_amont(ctx, lbId)
    await journaliser(
        ctx, action="lb.suppression", cible_type="load_balancer", cible_id=lbId, cible=lb.nom
    )
    await depot_lb.supprimer(ctx, lbId)
    return Response(status_code=204)


@router_lb.get(
    "/{lbId}/metriques",
    response_model=m.LoadBalancersLbIdMetriquesGetResponse,
    response_model_exclude_none=True,
)
async def obtenir_metriques_load_balancer(
    lbId: str, fenetre: str | None = None, ctx: Contexte = Depends(exige(None))
) -> Any:  # noqa: N803
    await depot_lb.obtenir(ctx, lbId)
    return {"series": [], "liens": None}


@router_lb.put("/{lbId}/pool", response_model=m.LoadBalancer, response_model_exclude_none=True)
async def modifier_pool_load_balancer(
    lbId: str, corps: m.LoadBalancersLbIdPoolPutRequest, ctx: Contexte = Depends(exige("lb.create"))
) -> Any:  # noqa: N803
    lb = await depot_lb.obtenir(ctx, lbId)
    pool = await synchroniser_pool_amont(ctx, lb, corps.cibles)
    await depot_lb.modifier(ctx, lbId, {"pool": [p.model_dump() for p in pool]})
    await journaliser(ctx, action="lb.pool", cible_type="load_balancer", cible_id=lbId)
    return await depot_lb.obtenir(ctx, lbId)


@router_lb.put("/{lbId}/regles-l7", response_model=m.LoadBalancer, response_model_exclude_none=True)
async def modifier_regles_l7(
    lbId: str,
    corps: m.LoadBalancersLbIdReglesL7PutRequest,
    ctx: Contexte = Depends(exige("lb.create")),
) -> Any:  # noqa: N803
    await depot_lb.obtenir(ctx, lbId)
    regles = [
        m.ReglesL7Item(hote=r.hote, chemin=r.chemin, entete=r.entete, cible=r.cible).model_dump()
        for r in corps.regles
    ]
    await depot_lb.modifier(ctx, lbId, {"reglesL7": regles})
    await journaliser(ctx, action="lb.regles_l7", cible_type="load_balancer", cible_id=lbId)
    return await depot_lb.obtenir(ctx, lbId)


# ── VPN ────────────────────────────────────────────────────────────────────
@router_vpn.get("", response_model=m.VpnGetResponse, response_model_exclude_none=True)
async def lister_tunnels(
    page: Page,
    espaceId: str | None = None,
    statut: str | None = None,
    ctx: Contexte = Depends(exige(_rbac_lecture, lecture=True)),
) -> Any:  # noqa: N803
    return await depot_vpn.lister(
        ctx,
        page,
        filtre=lambda t: (
            (not espaceId or t.espaceId == espaceId) and (not statut or t.statut == statut)
        ),
        tri_defaut="nom",
    )


@router_vpn.post(
    "",
    response_model=m.TunnelVpn,
    status_code=status.HTTP_201_CREATED,
    response_model_exclude_none=True,
)
async def creer_tunnel(
    corps: m.TunnelVpnCreation, ctx: Contexte = Depends(exige("network.manage"))
) -> Any:
    await depot_vpn.exiger_nom_libre(ctx, corps.nom)
    tunnel = m.TunnelVpn(
        id=nouvel_id(),
        espaceId=corps.espaceId,
        nom=corps.nom,
        type=corps.type,
        passerelleDistante=corps.passerelleDistante,
        reseauxAnnonces=corps.reseauxAnnonces,
        statut="up",
        derniereNegociation=maintenant(),
        profils=[],
    )
    await depot_vpn.creer(ctx, tunnel)
    await journaliser(
        ctx, action="vpn.creation", cible_type="vpn_tunnel", cible_id=tunnel.id, cible=tunnel.nom
    )
    return tunnel


@router_vpn.get("/{tunnelId}", response_model=m.TunnelVpn, response_model_exclude_none=True)
async def obtenir_tunnel(
    tunnelId: str, ctx: Contexte = Depends(exige(_rbac_lecture, lecture=True))
) -> Any:  # noqa: N803
    return await depot_vpn.obtenir(ctx, tunnelId)


@router_vpn.patch("/{tunnelId}", response_model=m.TunnelVpn, response_model_exclude_none=True)
async def modifier_tunnel(
    tunnelId: str, corps: m.TunnelVpnCreation, ctx: Contexte = Depends(exige("network.manage"))
) -> Any:  # noqa: N803
    t = await depot_vpn.obtenir(ctx, tunnelId)
    if corps.nom and corps.nom != t.nom:
        await depot_vpn.exiger_nom_libre(ctx, corps.nom)
    await depot_vpn.modifier(ctx, tunnelId, corps)
    await journaliser(
        ctx,
        action="vpn.modification",
        cible_type="vpn_tunnel",
        cible_id=tunnelId,
        details=corps.model_dump(mode="json", exclude_none=True),
    )
    return await depot_vpn.obtenir(ctx, tunnelId)


@router_vpn.delete("/{tunnelId}", status_code=status.HTTP_204_NO_CONTENT)
async def supprimer_tunnel(
    tunnelId: str, confirmation: str | None = None, ctx: Contexte = Depends(exige("network.manage"))
) -> Response:  # noqa: N803
    t = await depot_vpn.obtenir(ctx, tunnelId)
    exiger_confirmation(t.nom, confirmation)
    await journaliser(
        ctx, action="vpn.suppression", cible_type="vpn_tunnel", cible_id=tunnelId, cible=t.nom
    )
    await depot_vpn.supprimer(ctx, tunnelId)
    return Response(status_code=204)


@router_vpn.post(
    "/{tunnelId}/profils",
    response_model=m.VpnTunnelIdProfilsPostResponse,
    status_code=status.HTTP_201_CREATED,
    response_model_exclude_none=True,
)
async def creer_profil_vpn(
    tunnelId: str,
    corps: m.VpnTunnelIdProfilsPostRequest,
    ctx: Contexte = Depends(exige("network.manage")),
) -> Any:  # noqa: N803
    t = await depot_vpn.obtenir(ctx, tunnelId)
    if any(p.nom == corps.nom for p in (t.profils or [])):
        raise erreurs.conflit("Ce profil existe déjà.", code="profil_existant")
    profil = m.Profil(nom=corps.nom, utilisateur=corps.utilisateur, cree=maintenant(), revoque=None)
    profils = [*list(t.profils or []), profil]
    await depot_vpn.modifier(ctx, tunnelId, {"profils": [p.model_dump() for p in profils]})
    await journaliser(
        ctx,
        action="vpn.profil.creation",
        cible_type="vpn_tunnel",
        cible_id=tunnelId,
        details={"profil": corps.nom},
    )
    configuration = (
        f"client\n"
        f"dev tun\n"
        f"proto udp\n"
        f"remote vpn.synelia.cloud 1194\n"
        f"auth-user-pass\n"
        f"<ca>\n{certificat_bidon(corps.nom, corps.utilisateur)}\n</ca>\n"
    )
    return m.VpnTunnelIdProfilsPostResponse(nom=corps.nom, configuration=configuration, expire=None)


def certificat_bidon(nom: str, utilisateur: str) -> str:
    return f"-----BEGIN CERTIFICATE-----\n{nom}://{utilisateur}/{nouvel_id()}\n-----END CERTIFICATE-----"


@router_vpn.delete("/{tunnelId}/profils/{profilNom}", status_code=status.HTTP_204_NO_CONTENT)
async def revoquer_profil_vpn(
    tunnelId: str, profilNom: str, ctx: Contexte = Depends(exige("network.manage"))
) -> Response:  # noqa: N803
    t = await depot_vpn.obtenir(ctx, tunnelId)
    profils = list(t.profils or [])
    nouveau = [p.model_copy(update={"revoque": True}) if p.nom == profilNom else p for p in profils]
    if not any(p.nom == profilNom for p in profils):
        raise erreurs.introuvable("Profil VPN", profilNom)
    await depot_vpn.modifier(ctx, tunnelId, {"profils": [p.model_dump() for p in nouveau]})
    await journaliser(
        ctx,
        action="vpn.profil.revocation",
        cible_type="vpn_tunnel",
        cible_id=tunnelId,
        details={"profil": profilNom},
    )
    return Response(status_code=204)


@router_vpn.post(
    "/{tunnelId}/renegociation", response_model=m.TunnelVpn, response_model_exclude_none=True
)
async def renegocier_tunnel_vpn(
    tunnelId: str, ctx: Contexte = Depends(exige("network.manage"))
) -> Any:  # noqa: N803
    t = await depot_vpn.obtenir(ctx, tunnelId)
    await depot_vpn.modifier(ctx, tunnelId, {"derniereNegociation": maintenant(), "statut": "up"})
    await journaliser(
        ctx, action="vpn.renegociation", cible_type="vpn_tunnel", cible_id=tunnelId, cible=t.nom
    )
    return await depot_vpn.obtenir(ctx, tunnelId)


def _valider_cidr(cidr: str) -> None:
    try:
        ipaddress.ip_network(cidr, strict=False)
    except ValueError as exc:
        raise erreurs.validation("CIDR invalide.", {"cidr": str(exc)}) from exc
