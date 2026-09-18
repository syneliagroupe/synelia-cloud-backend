"""Jetons plateforme (super admin) : `/admin/jetons`.

Les jetons d'organisation restent gérés par leur organisation (`/securite/cles-api`) ; ceux-ci
n'ont pas d'`org_id` et donnent à un script distant les droits du rôle Synelia qui les a émis
(`role_emetteur`), bornés par leur `portee`. Seul un membre de l'équipe Synelia peut les
créer/lister/révoquer — jamais une organisation cliente."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import select
from synelia_contract import modeles as m
from synelia_contract.rbac import ROLES_EQUIPE, permissions_effectives
from synelia_db.modeles import CleApi
from synelia_kernel import erreurs
from synelia_kernel.dates import maintenant
from synelia_kernel.ids import jeton_opaque, nouvel_id, prefixe_lisible

from synelia.audit import journaliser
from synelia.deps import Contexte, Page, exige_admin, exiger_confirmation
from synelia.deps.pagination import filtrer_trier_paginer
from synelia.securite import hacher_jeton

router = APIRouter(prefix="/admin", tags=["Super admin — pilotage"])

# Un jeton plateforme ne peut porter que les actions d'un rôle de l'équipe Synelia : jamais un
# rôle client, même si l'appelant est super admin (sinon un jeton `org_admin` deviendrait une
# clé de plateforme au rabais, hors du modèle d'autorisation).
ROLES_JETON = ROLES_EQUIPE


def jeton_contrat(c: CleApi) -> dict[str, Any]:
    now = maintenant()
    if c.revoquee_le is not None:
        statut = "revoquee"
    elif c.expire_le and c.expire_le < now:
        statut = "expiree"
    else:
        statut = "active"
    return {
        "id": c.id,
        "nom": c.nom,
        "prefixe": c.prefixe,
        "portee": c.portee or [],
        "creeLe": c.cree_le or now,
        "creePar": c.cree_par,
        "expire": c.expire_le,
        "derniereUtilisation": c.derniere_utilisation_le,
        "ipsAutorisees": c.ips_autorisees,
        "statut": statut,
    }


def _role_emetteur(ctx: Contexte) -> str:
    p = ctx.principal
    role = (p.role_equipe if p else None) or (p.role if p else None)
    if role not in ROLES_JETON:
        raise erreurs.interdit("Jeton réservé à l'équipe Synelia.", code="role_plateforme_requis")
    return role


def _valider_portee(role: str, portee: list[str]) -> None:
    autorisees = {a for a, p in permissions_effectives(role).items() if p != "none"}
    for action in portee:
        if action == "*":
            continue
        if action not in autorisees:
            raise erreurs.validation(
                f"Portée non autorisée pour le rôle {role}.", {"portee": action}
            )


def _nouveau_secret(prefixe: str) -> str:
    return f"{prefixe}.{jeton_opaque()}"


async def _obtenir_jeton(ctx: Contexte, jeton_id: str) -> CleApi:
    c = (
        await ctx.session.execute(
            select(CleApi).where(CleApi.id == jeton_id, CleApi.org_id.is_(None))
        )
    ).scalar_one_or_none()
    if c is None:
        raise erreurs.introuvable("Jeton plateforme", jeton_id)
    return c


@router.get(
    "/jetons", response_model=m.SecuriteClesApiGetResponse, response_model_exclude_none=True
)
async def lister_jetons_admin(
    page: Page,
    statut: str | None = None,
    ctx: Contexte = Depends(exige_admin("sso.configure")),
) -> Any:
    q = select(CleApi).where(CleApi.org_id.is_(None))
    if statut == "active":
        q = q.where(CleApi.revoquee_le.is_(None))
    elif statut == "revoquee":
        q = q.where(CleApi.revoquee_le.is_not(None))
    jetons = [
        jeton_contrat(c)
        for c in (await ctx.session.execute(q.order_by(CleApi.cree_le.desc()))).scalars()
    ]
    if statut == "expiree":
        now = maintenant()
        jetons = [j for j in jetons if j["expire"] and j["expire"] < now]
    return filtrer_trier_paginer(
        jetons, page, champs_recherche=("nom", "prefixe"), tri_defaut="nom"
    )


@router.post(
    "/jetons",
    response_model=m.CleApiSecret,
    status_code=status.HTTP_201_CREATED,
    response_model_exclude_none=True,
)
async def creer_jeton_admin(
    corps: m.CleApiCreation, ctx: Contexte = Depends(exige_admin("sso.configure"))
) -> Any:
    role = _role_emetteur(ctx)
    _valider_portee(role, corps.portee)
    prefixe = prefixe_lisible("syn")
    secret = _nouveau_secret(prefixe)
    c = CleApi(
        id=nouvel_id(),
        org_id=None,
        nom=corps.nom,
        prefixe=prefixe,
        secret_hash=hacher_jeton(secret),
        portee=list(corps.portee),
        role_emetteur=role,
        cree_par=ctx.utilisateur_id,
        expire_le=corps.expire,
        ips_autorisees=list(corps.ipsAutorisees or []),
    )
    ctx.session.add(c)
    await ctx.session.flush()
    await journaliser(
        ctx, action="admin.jeton_creation", cible_type="jeton", cible_id=c.id, cible=c.nom
    )
    return {"cle": jeton_contrat(c), "secret": secret}


@router.get("/jetons/{jetonId}", response_model=m.CleApi, response_model_exclude_none=True)
async def obtenir_jeton_admin(
    jetonId: str, ctx: Contexte = Depends(exige_admin("sso.configure"))
) -> Any:  # noqa: N803
    return jeton_contrat(await _obtenir_jeton(ctx, jetonId))


@router.patch("/jetons/{jetonId}", response_model=m.CleApi, response_model_exclude_none=True)
async def modifier_jeton_admin(
    jetonId: str,
    corps: m.CleApiCreation,
    ctx: Contexte = Depends(exige_admin("sso.configure")),
) -> Any:  # noqa: N803
    c = await _obtenir_jeton(ctx, jetonId)
    if c.revoquee_le is not None:
        raise erreurs.conflit("Un jeton révoqué ne peut pas être modifié.", code="jeton_revoque")
    if corps.nom:
        c.nom = corps.nom
    if corps.portee is not None:
        _valider_portee(c.role_emetteur, corps.portee)
        c.portee = list(corps.portee)
    if corps.expire is not None:
        c.expire_le = corps.expire
    if corps.ipsAutorisees is not None:
        c.ips_autorisees = list(corps.ipsAutorisees)
    await ctx.session.flush()
    await journaliser(
        ctx, action="admin.jeton_modification", cible_type="jeton", cible_id=c.id, cible=c.nom
    )
    return jeton_contrat(c)


@router.delete("/jetons/{jetonId}", status_code=status.HTTP_204_NO_CONTENT)
async def revoquer_jeton_admin(
    jetonId: str,
    confirmation: str | None = None,
    ctx: Contexte = Depends(exige_admin("sso.configure")),
) -> Response:  # noqa: N803
    c = await _obtenir_jeton(ctx, jetonId)
    exiger_confirmation(c.nom, confirmation)
    c.revoquee_le = maintenant()
    await ctx.session.flush()
    await journaliser(
        ctx, action="admin.jeton_revocation", cible_type="jeton", cible_id=c.id, cible=c.nom
    )
    return Response(status_code=204)


@router.post(
    "/jetons/{jetonId}/rotation",
    response_model=m.CleApiSecret,
    response_model_exclude_none=True,
)
async def rotationner_jeton_admin(
    jetonId: str,
    corps: m.SecuriteClesApiCleIdRotationPostRequest,
    ctx: Contexte = Depends(exige_admin("sso.configure")),
) -> Any:  # noqa: N803
    c = await _obtenir_jeton(ctx, jetonId)
    if c.revoquee_le is not None:
        raise erreurs.conflit("Un jeton révoqué ne peut pas être tourné.", code="jeton_revoque")
    secret = _nouveau_secret(c.prefixe)
    c.secret_hash = hacher_jeton(secret)
    await ctx.session.flush()
    await journaliser(
        ctx,
        action="admin.jeton_rotation",
        cible_type="jeton",
        cible_id=c.id,
        cible=c.nom,
        details={"delaiGraceHeures": corps.delaiGraceHeures},
    )
    return {"cle": jeton_contrat(c), "secret": secret}
