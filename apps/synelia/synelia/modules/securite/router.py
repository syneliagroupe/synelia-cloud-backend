"""Sécurité & accès : clés d'API, politiques, sessions actives, SSO."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import select
from synelia_contract import modeles as m
from synelia_contract.rbac import permissions_effectives
from synelia_db.modeles import CleApi, Organisation, SessionAuth, Utilisateur
from synelia_kernel import erreurs
from synelia_kernel.dates import maintenant
from synelia_kernel.ids import jeton_opaque, nouvel_id, prefixe_lisible

from synelia.audit import journaliser
from synelia.deps import Contexte, Page, exige, exiger_confirmation
from synelia.deps.pagination import filtrer_trier_paginer
from synelia.securite import hacher_jeton, politiques_securite

router = APIRouter(prefix="/securite", tags=["Sécurité & accès"])

# `reauthentificationActionsSensibles` : la session courante doit avoir été ouverte (ou
# avoir validé son MFA) il y a moins de ce délai pour exécuter les actions les plus
# sensibles (rotation/révocation de clé d'API, révocation de toutes les sessions).
REAUTH_FENETRE_MIN = 15


def politiques_contrat(o: Organisation) -> dict[str, Any]:
    return politiques_securite(o.politiques)


def cle_contrat(c: CleApi) -> dict[str, Any]:
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


async def _org(ctx: Contexte) -> Organisation:
    o = await ctx.session.get(Organisation, ctx.org_id)
    if o is None:
        raise erreurs.introuvable("Organisation", ctx.org_id)
    return o


def _valider_portee(ctx: Contexte, portee: list[str]) -> None:
    autorisees = {a for a, p in permissions_effectives(ctx.role).items() if p != "none"}
    for action in portee:
        if action == "*":
            continue
        if action not in autorisees:
            raise erreurs.validation(
                f"Portée non autorisée pour le rôle {ctx.role}.", {"portee": action}
            )


async def _exiger_reauth_recente(ctx: Contexte) -> None:
    """Applique réellement `reauthentificationActionsSensibles` quand la politique l'active."""
    o = await _org(ctx)
    if not politiques_contrat(o).get("session", {}).get("reauthentificationActionsSensibles"):
        return
    sid = ctx.principal.session_id if ctx.principal else None
    s = await ctx.session.get(SessionAuth, sid) if sid else None
    if s is None or s.cree_le < maintenant() - timedelta(minutes=REAUTH_FENETRE_MIN):
        raise erreurs.interdit(
            "Cette action exige une réauthentification récente.",
            code="reauthentification_requise",
        )


async def _obtenir_cle(ctx: Contexte, cle_id: str) -> CleApi:
    c = (
        await ctx.session.execute(
            select(CleApi).where(CleApi.id == cle_id, CleApi.org_id == ctx.org_id)
        )
    ).scalar_one_or_none()
    if c is None:
        raise erreurs.introuvable("Clé d'API", cle_id)
    return c


def _nouveau_secret(prefixe: str) -> str:
    return f"{prefixe}.{jeton_opaque()}"


@router.get(
    "/cles-api", response_model=m.SecuriteClesApiGetResponse, response_model_exclude_none=True
)
async def lister_cles_api(
    page: Page,
    statut: str | None = None,
    ctx: Contexte = Depends(exige("sso.configure", lecture=True)),
) -> Any:
    q = select(CleApi).where(CleApi.org_id == ctx.org_id)
    if statut:
        if statut == "active":
            q = q.where(CleApi.revoquee_le.is_(None))
        elif statut == "revoquee":
            q = q.where(CleApi.revoquee_le.is_not(None))
    cles = [
        cle_contrat(c)
        for c in (await ctx.session.execute(q.order_by(CleApi.cree_le.desc()))).scalars()
    ]
    if statut == "expiree":
        now = maintenant()
        cles = [c for c in cles if c["expire"] and c["expire"] < now]
    return filtrer_trier_paginer(cles, page, champs_recherche=("nom", "prefixe"), tri_defaut="nom")


@router.post(
    "/cles-api",
    response_model=m.CleApiSecret,
    status_code=status.HTTP_201_CREATED,
    response_model_exclude_none=True,
)
async def creer_cle_api(
    corps: m.CleApiCreation, ctx: Contexte = Depends(exige("sso.configure"))
) -> Any:
    _valider_portee(ctx, corps.portee)
    prefixe = prefixe_lisible("syn")
    secret = _nouveau_secret(prefixe)
    c = CleApi(
        id=nouvel_id(),
        org_id=ctx.org_id,
        nom=corps.nom,
        prefixe=prefixe,
        secret_hash=hacher_jeton(secret),
        portee=list(corps.portee),
        role_emetteur=ctx.role,
        cree_par=ctx.utilisateur_id,
        expire_le=corps.expire,
        ips_autorisees=list(corps.ipsAutorisees or []),
    )
    ctx.session.add(c)
    await ctx.session.flush()
    await journaliser(
        ctx, action="securite.cle_creation", cible_type="cle_api", cible_id=c.id, cible=c.nom
    )
    return {"cle": cle_contrat(c), "secret": secret}


@router.get("/cles-api/{cleId}", response_model=m.CleApi, response_model_exclude_none=True)
async def obtenir_cle_api(
    cleId: str, ctx: Contexte = Depends(exige("sso.configure", lecture=True))
) -> Any:  # noqa: N803
    return cle_contrat(await _obtenir_cle(ctx, cleId))


@router.patch("/cles-api/{cleId}", response_model=m.CleApi, response_model_exclude_none=True)
async def modifier_cle_api(
    cleId: str, corps: m.CleApiCreation, ctx: Contexte = Depends(exige("sso.configure"))
) -> Any:  # noqa: N803
    c = await _obtenir_cle(ctx, cleId)
    if corps.nom:
        c.nom = corps.nom
    if corps.portee is not None:
        _valider_portee(ctx, corps.portee)
        c.portee = list(corps.portee)
    if corps.expire is not None:
        c.expire_le = corps.expire
    if corps.ipsAutorisees is not None:
        c.ips_autorisees = list(corps.ipsAutorisees)
    await ctx.session.flush()
    await journaliser(
        ctx, action="securite.cle_modification", cible_type="cle_api", cible_id=c.id, cible=c.nom
    )
    return cle_contrat(c)


@router.delete("/cles-api/{cleId}", status_code=status.HTTP_204_NO_CONTENT)
async def revoquer_cle_api(
    cleId: str, confirmation: str | None = None, ctx: Contexte = Depends(exige("sso.configure"))
) -> Response:  # noqa: N803
    c = await _obtenir_cle(ctx, cleId)
    exiger_confirmation(c.nom, confirmation)
    await _exiger_reauth_recente(ctx)
    c.revoquee_le = maintenant()
    await ctx.session.flush()
    await journaliser(
        ctx, action="securite.cle_revocation", cible_type="cle_api", cible_id=c.id, cible=c.nom
    )
    return Response(status_code=204)


@router.post(
    "/cles-api/{cleId}/rotation", response_model=m.CleApiSecret, response_model_exclude_none=True
)
async def rotationner_cle_api(
    cleId: str,
    corps: m.SecuriteClesApiCleIdRotationPostRequest,
    ctx: Contexte = Depends(exige("sso.configure")),
) -> Any:  # noqa: N803
    c = await _obtenir_cle(ctx, cleId)
    if c.revoquee_le is not None:
        raise erreurs.conflit("Une clé révoquée ne peut pas être tournée.", code="cle_revoquee")
    await _exiger_reauth_recente(ctx)
    prefixe = c.prefixe
    secret = _nouveau_secret(prefixe)
    c.secret_hash = hacher_jeton(secret)
    await ctx.session.flush()
    await journaliser(
        ctx,
        action="securite.cle_rotation",
        cible_type="cle_api",
        cible_id=c.id,
        cible=c.nom,
        details={"delaiGraceHeures": corps.delaiGraceHeures},
    )
    return {"cle": cle_contrat(c), "secret": secret}


@router.get("/politiques", response_model=m.PolitiquesSecurite, response_model_exclude_none=True)
async def obtenir_politiques_securite(
    ctx: Contexte = Depends(exige("sso.configure", lecture=True)),
) -> Any:
    o = await _org(ctx)
    return politiques_contrat(o)


@router.put(
    "/politiques", response_model=m.SecuritePolitiquesPutResponse, response_model_exclude_none=True
)
async def modifier_politiques_securite(
    corps: m.PolitiquesSecurite, ctx: Contexte = Depends(exige("sso.configure"))
) -> Any:
    o = await _org(ctx)
    actuelles = politiques_contrat(o)
    nouvelles = corps.model_dump(mode="json", exclude_none=True)
    o.politiques = nouvelles
    sessions_invalidees = 0
    if actuelles.get("session", {}).get("dureeMaxMin") != nouvelles.get("session", {}).get(
        "dureeMaxMin"
    ):
        lignes = (
            (
                await ctx.session.execute(
                    select(SessionAuth).where(
                        SessionAuth.org_id == ctx.org_id, SessionAuth.revoquee_le.is_(None)
                    )
                )
            )
            .scalars()
            .all()
        )
        for s in lignes:
            s.revoquee_le = maintenant()
        sessions_invalidees = len(lignes)
    await ctx.session.flush()
    await journaliser(
        ctx,
        action="securite.politiques_maj",
        cible_type="organisation",
        cible_id=o.id,
        cible=o.nom,
        details={"sessionsInvalidees": sessions_invalidees},
    )
    return {"politiques": politiques_contrat(o), "sessionsInvalidees": sessions_invalidees}


@router.get(
    "/sessions", response_model=m.SecuriteSessionsGetResponse, response_model_exclude_none=True
)
async def lister_sessions_actives(
    page: Page,
    userId: str | None = None,  # noqa: N803
    idpSource: str | None = None,  # noqa: N803
    ctx: Contexte = Depends(exige("sso.configure", lecture=True)),
) -> Any:
    q = (
        select(SessionAuth, Utilisateur)
        .join(Utilisateur, Utilisateur.id == SessionAuth.utilisateur_id)
        .where(
            SessionAuth.org_id == ctx.org_id,
            SessionAuth.revoquee_le.is_(None),
            SessionAuth.expire_le > maintenant(),
        )
    )
    if userId:
        q = q.where(SessionAuth.utilisateur_id == userId)
    if idpSource:
        q = q.where(Utilisateur.idp_source == idpSource)
    lignes = (await ctx.session.execute(q.order_by(SessionAuth.cree_le.desc()))).all()
    courante = ctx.principal.session_id if ctx.principal else None
    sessions = [_session_contrat(s, u, courante) for s, u in lignes]
    return filtrer_trier_paginer(
        sessions,
        page,
        champs_recherche=("utilisateurNom", "email"),
        tri_defaut="derniereActivite",
        ordre_defaut="desc",
    )


def _session_contrat(s: SessionAuth, u: Utilisateur | None, courante: str | None) -> dict[str, Any]:
    return {
        "id": s.id,
        "userId": s.utilisateur_id,
        "utilisateurNom": u.nom if u else None,
        "email": u.email if u else None,
        "ip": s.ip or "-",
        "agent": s.user_agent,
        "idpSource": u.idp_source if u else None,
        "debut": s.cree_le or maintenant(),
        "derniereActivite": s.derniere_activite_le or s.cree_le or maintenant(),
        "courante": s.id == courante,
    }


@router.delete(
    "/sessions", response_model=m.SecuriteSessionsDeleteResponse, response_model_exclude_none=True
)
async def revoquer_toutes_sessions(
    confirmation: str | None = None, ctx: Contexte = Depends(exige("sso.configure"))
) -> Any:
    o = await _org(ctx)
    exiger_confirmation(o.nom, confirmation)
    await _exiger_reauth_recente(ctx)
    courante = ctx.principal.session_id if ctx.principal else None
    lignes = (
        (
            await ctx.session.execute(
                select(SessionAuth).where(
                    SessionAuth.org_id == ctx.org_id, SessionAuth.revoquee_le.is_(None)
                )
            )
        )
        .scalars()
        .all()
    )
    revoquees = 0
    for s in lignes:
        if s.id == courante:
            continue
        s.revoquee_le = maintenant()
        revoquees += 1
    await ctx.session.flush()
    await journaliser(
        ctx,
        action="securite.sessions_revocation",
        cible_type="organisation",
        cible_id=o.id,
        cible=o.nom,
        details={"revoquees": revoquees},
    )
    return {"revoquees": revoquees}


@router.delete("/sessions/{sessionId}", status_code=status.HTTP_204_NO_CONTENT)
async def revoquer_session(
    sessionId: str, ctx: Contexte = Depends(exige("sso.configure"))
) -> Response:  # noqa: N803
    s = (
        await ctx.session.execute(
            select(SessionAuth).where(SessionAuth.id == sessionId, SessionAuth.org_id == ctx.org_id)
        )
    ).scalar_one_or_none()
    if s is None:
        raise erreurs.introuvable("Session", sessionId)
    if ctx.principal and s.id == ctx.principal.session_id:
        raise erreurs.conflit(
            "Impossible de révoquer la session courante ici.", code="session_courante"
        )
    s.revoquee_le = maintenant()
    await ctx.session.flush()
    await journaliser(
        ctx, action="securite.session_revocation", cible_type="session", cible_id=s.id
    )
    return Response(status_code=204)


def sso_contrat(o: Organisation) -> dict[str, Any]:
    sso = o.sso or {}
    return {
        "actif": bool(sso.get("actif")),
        "protocole": sso.get("protocole"),
        "emetteur": sso.get("emetteur"),
        "clientId": sso.get("clientId"),
        "secretDefini": bool(sso.get("secretHash")),
        "urlMetadonnees": sso.get("urlMetadonnees"),
        "certificatEmpreinte": sso.get("certificatEmpreinte"),
        "domainesVerifies": sso.get("domainesVerifies"),
        "provisioningJustInTime": sso.get("provisioningJustInTime"),
        "correspondanceGroupes": sso.get("correspondanceGroupes"),
        "dernierTest": sso.get("dernierTest"),
    }


@router.get("/sso", response_model=m.ConfigurationSso, response_model_exclude_none=True)
async def obtenir_configuration_sso(
    ctx: Contexte = Depends(exige("sso.configure", lecture=True)),
) -> Any:
    o = await _org(ctx)
    return sso_contrat(o)


@router.put("/sso", response_model=m.ConfigurationSso, response_model_exclude_none=True)
async def modifier_configuration_sso(
    corps: m.ConfigurationSso, ctx: Contexte = Depends(exige("sso.configure"))
) -> Any:
    o = await _org(ctx)
    actuelles = o.sso or {}
    nouvelles = corps.model_dump(mode="json", exclude_none=True)
    if "secret" in nouvelles:
        nouvelles.pop("secret", None)
    charge = {**actuelles, **nouvelles}
    if "secret" in charge:
        # garder le secret existant si non fourni
        charge.pop("secret", None)
    o.sso = charge
    await ctx.session.flush()
    await journaliser(
        ctx, action="securite.sso_maj", cible_type="organisation", cible_id=o.id, cible=o.nom
    )
    return sso_contrat(o)


@router.post(
    "/sso/test", response_model=m.SecuriteSsoTestPostResponse, response_model_exclude_none=True
)
async def tester_sso(ctx: Contexte = Depends(exige("sso.configure"))) -> Any:
    o = await _org(ctx)
    sso = o.sso or {}
    if not sso.get("actif"):
        return {
            "succes": False,
            "etapes": [
                {"nom": "Configuration", "ok": True, "detail": "Configuration présente."},
                {
                    "nom": "Connectivité vers l'émetteur",
                    "ok": False,
                    "detail": "Aucune configuration SSO active.",
                },
                {"nom": "Validation du flux de connexion", "ok": False, "detail": "SSO inactif."},
            ],
        }
    etapes = [
        {
            "nom": "Configuration",
            "ok": True,
            "detail": sso.get("protocole", "o") and f"Protocole {sso.get('protocole')}.",
        },
        {
            "nom": "Connectivité vers l'émetteur",
            "ok": False,
            "detail": "Aucun courtier d’identité n’est configuré sur cette plateforme.",
        },
        {
            "nom": "Validation du flux de connexion",
            "ok": False,
            "detail": "Le flux réel n’est pas encore disponible.",
        },
    ]
    resultat = {"succes": False, "etapes": etapes, "correlationId": ctx.correlation_id}
    o.sso = {
        **(o.sso or {}),
        "dernierTest": {"horodatage": maintenant().isoformat(), "succes": False},
    }
    await ctx.session.flush()
    await journaliser(
        ctx,
        action="securite.sso_test",
        cible_type="organisation",
        cible_id=o.id,
        cible=o.nom,
        resultat="succes",
    )
    return resultat
