from __future__ import annotations

import secrets as _secrets
from typing import Any

from fastapi import APIRouter, Body, status
from sqlalchemy import select
from synelia_contract import modeles as m
from synelia_contract import rbac
from synelia_db.modeles import (
    Invitation,
    Membership,
    Organisation,
    SessionAuth,
    Utilisateur,
    VerificationEmail,
)
from synelia_kernel import agentmail, courriel, erreurs
from synelia_kernel.chiffrement import dechiffrer
from synelia_kernel.dates import dans, maintenant
from synelia_kernel.ids import jeton_opaque

from synelia.audit import journaliser
from synelia.deps import CtxPublic
from synelia.deps.contexte import Contexte, Ctx
from synelia.modules.auth import service
from synelia.securite import (
    emettre_acces,
    hacher_jeton,
    hacher_mot_de_passe,
    ip_autorisee,
    politiques_securite,
    role_effectif_equipe,
    verifier_mot_de_passe,
    verifier_totp,
)

router = APIRouter(prefix="/auth", tags=["Authentification"])


async def _utilisateur_par_email(ctx: Contexte, email: str) -> Utilisateur | None:
    return (
        await ctx.session.execute(select(Utilisateur).where(Utilisateur.email == email.lower()))
    ).scalar_one_or_none()


@router.post("/connexion", response_model=m.Session, response_model_exclude_none=True)
async def se_connecter(ctx: CtxPublic, corps: m.DemandeConnexion) -> Any:
    u = await _utilisateur_par_email(ctx, str(corps.email))
    if u is None or not verifier_mot_de_passe(
        corps.motDePasse.get_secret_value(), u.mot_de_passe_hash
    ):
        raise erreurs.non_authentifie("Identifiants incorrects.")
    if u.statut == "suspendu":
        raise erreurs.interdit("Compte suspendu.", code="compte_suspendu")
    if u.statut == "verification_requise":
        raise erreurs.interdit(
            "Email non vérifié : saisissez le code reçu par email pour activer le compte.",
            code="email_non_verifie",
        )
    org = u.org_active_id
    if org:
        o = await ctx.session.get(Organisation, org)
        # Une organisation suspendue coupe l'accès de ses membres — l'équipe Synelia garde
        # le sien pour pouvoir la consulter/la réactiver (jamais bloquée par sa propre action).
        if (
            o is not None
            and o.statut == "suspendue"
            and role_effectif_equipe(u.equipe or {}) not in rbac.ROLES_EQUIPE
        ):
            await journaliser(
                ctx,
                action="auth.connexion_refusee_organisation_suspendue",
                cible_type="utilisateur",
                cible_id=u.id,
                cible=u.email,
                org_id=org,
                resultat="refus",
            )
            raise erreurs.interdit(
                "Organisation suspendue : connexion impossible.", code="organisation_suspendue"
            )
        restriction = politiques_securite(o.politiques if o else None).get("restrictionIp", {})
        if restriction.get("actif") and not ip_autorisee(
            ctx.ip, restriction.get("plages", []), "portail"
        ):
            await journaliser(
                ctx,
                action="auth.connexion_refusee_ip",
                cible_type="utilisateur",
                cible_id=u.id,
                cible=u.email,
                org_id=org,
                resultat="refus",
                details={"ip": ctx.ip},
            )
            raise erreurs.interdit(
                "Connexion refusée : adresse IP non autorisée.", code="ip_non_autorisee"
            )
    mfa = await service.mfa_exigee(ctx.session, u, org)
    rep = await service.ouvrir_session(
        ctx.session,
        u,
        ip=ctx.ip,
        user_agent=ctx.entete("user-agent"),
        org_id=org,
        mfa_validee=not mfa,
    )
    await journaliser(
        ctx,
        action="auth.connexion",
        cible_type="utilisateur",
        cible_id=u.id,
        cible=u.email,
        org_id=org,
        details={"mfaRequis": mfa},
    )
    return rep


@router.post("/mfa", response_model=m.Session, response_model_exclude_none=True)
async def valider_mfa(ctx: CtxPublic, corps: m.AuthMfaPostRequest) -> Any:
    s = await service.valider_defi(ctx.session, corps.defiMfa)
    if s is None or s.expire_le < maintenant():
        raise erreurs.non_authentifie("Défi MFA inconnu ou expiré.")
    u = await ctx.session.get(Utilisateur, s.utilisateur_id)
    assert u is not None
    secret = dechiffrer(u.mfa_secret_chiffre) if u.mfa_secret_chiffre else None
    totp_valide = secret is not None and verifier_totp(secret, corps.code)
    code_secours_utilise = None
    if not totp_valide:
        # `POST /moi/mfa` génère et présente huit codes de secours à usage unique
        # (hachés dans `preferences.codes_secours_hash`), mais jusqu'ici rien ne les
        # vérifiait jamais ici : un compte qui perd l'accès à son application TOTP
        # n'avait donc aucun moyen réel de se reconnecter malgré la promesse de l'UI
        # (« codes de secours, utilisables une fois chacun »). Chaque code haché est
        # comparé, puis retiré de la liste (nouveau dict réassigné, pas de mutation
        # en place, pour que SQLAlchemy détecte le changement sur la colonne JSON).
        hachages = (u.preferences or {}).get("codes_secours_hash") or []
        for hachage in hachages:
            if verifier_mot_de_passe(corps.code, hachage):
                code_secours_utilise = hachage
                break
    if not totp_valide and code_secours_utilise is None:
        raise erreurs.validation(
            "Code invalide.", {"code": "Le code à six chiffres ne correspond pas."}
        )
    if code_secours_utilise is not None:
        restants = [h for h in u.preferences["codes_secours_hash"] if h != code_secours_utilise]
        u.preferences = {**u.preferences, "codes_secours_hash": restants}
    s.mfa_validee = True
    s.mfa_defi = None
    await ctx.session.flush()
    acces = emettre_acces({"sub": u.id, "org": s.org_id, "role": s.role, "sid": s.id})
    await journaliser(
        ctx,
        action="auth.mfa_validee",
        cible_type="utilisateur",
        cible_id=u.id,
        cible=u.email,
        org_id=s.org_id,
        details={"codeSecoursUtilise": code_secours_utilise is not None},
    )
    return {
        "accessToken": acces,
        "refreshToken": jeton_opaque(),  # la rotation réelle passe par /auth/rafraichir
        "expiresIn": ctx.reglages.acces_duree_s,
        "utilisateur": service.utilisateur_contrat(u),
        "organisations": await service.appartenances(ctx.session, u),
        "organisationActive": s.org_id,
        "roleActif": s.role,
        "mfaRequis": False,
    }


@router.post("/rafraichir", response_model=m.Session, response_model_exclude_none=True)
async def rafraichir_session(ctx: CtxPublic, corps: m.AuthRafraichirPostRequest) -> Any:
    s = (
        await ctx.session.execute(
            select(SessionAuth).where(
                SessionAuth.rafraichissement_hash == hacher_jeton(corps.refreshToken)
            )
        )
    ).scalar_one_or_none()
    if s is None or s.expire_le < maintenant():
        raise erreurs.non_authentifie("Jeton de rafraîchissement inconnu ou expiré.")
    if s.revoquee_le is not None:
        # réutilisation d'un jeton déjà tourné : on révoque toute la famille
        for autre in (
            await ctx.session.execute(select(SessionAuth).where(SessionAuth.famille == s.famille))
        ).scalars():
            autre.revoquee_le = maintenant()
        await journaliser(
            ctx,
            action="auth.rafraichissement_reutilisation",
            cible_type="utilisateur",
            cible_id=s.utilisateur_id,
            org_id=s.org_id,
            resultat="alerte",
            details={"famille": s.famille},
        )
        raise erreurs.non_authentifie("Réutilisation détectée : sessions révoquées.")
    u = await ctx.session.get(Utilisateur, s.utilisateur_id)
    assert u is not None
    s.revoquee_le = maintenant()
    rep = await service.ouvrir_session(
        ctx.session,
        u,
        ip=ctx.ip,
        user_agent=ctx.entete("user-agent"),
        org_id=s.org_id,
        famille=s.famille,
        emprunt=s.emprunt,
    )
    await journaliser(
        ctx,
        action="auth.rafraichissement",
        cible_type="utilisateur",
        cible_id=u.id,
        cible=u.email,
        org_id=s.org_id,
    )
    return rep


@router.post("/deconnexion", response_model=m.AuthDeconnexionPostResponse)
async def se_deconnecter(ctx: Ctx) -> Any:
    p = ctx.principal
    assert p is not None
    if p.session_id:
        s = await ctx.session.get(SessionAuth, p.session_id)
        if s:
            s.revoquee_le = maintenant()
    await journaliser(ctx, action="auth.deconnexion", cible_type="session", cible_id=p.session_id)
    return {"ferme": True}


@router.post(
    "/inscription",
    response_model=m.VerificationEmailEtat,
    status_code=status.HTTP_202_ACCEPTED,
    response_model_exclude_none=True,
)
async def s_inscrire(ctx: CtxPublic, corps: m.Inscription) -> Any:
    if not corps.accepteConditions:
        raise erreurs.validation(
            "Les conditions doivent être acceptées.", {"accepteConditions": "requis"}
        )
    if await _utilisateur_par_email(ctx, str(corps.email)):
        raise erreurs.conflit("Un compte existe déjà avec cet email.", code="email_deja_utilise")
    mdp = corps.motDePasse.get_secret_value()
    if len(mdp) < 8:
        raise erreurs.validation("Mot de passe trop court.", {"motDePasse": "8 caractères minimum"})
    # Le compte naît non vérifié : aucune session n'est ouverte tant que l'email
    # n'est pas prouvé (POST /auth/verification-email). Les comptes existants
    # (statut `actif`, seed admin, invitations acceptées) ne sont pas concernés.
    u = Utilisateur(
        email=str(corps.email).lower(),
        nom=corps.nom,
        mot_de_passe_hash=hacher_mot_de_passe(mdp),
        idp_source="local",
        statut="verification_requise",
    )
    ctx.session.add(u)
    await ctx.session.flush()
    org_id = None
    if corps.organisation:
        o = corps.organisation
        if (
            await ctx.session.execute(select(Organisation).where(Organisation.nom == o.nom))
        ).scalar_one_or_none():
            raise erreurs.nom_deja_pris(o.nom)
        org = Organisation(
            nom=o.nom,
            pays=o.pays,
            secteur=o.secteur,
            tva=o.tva,
            tenant_plan=o.tenantPlan,
            statut="active",
        )
        ctx.session.add(org)
        await ctx.session.flush()
        ctx.session.add(
            Membership(utilisateur_id=u.id, org_id=org.id, role="org_admin", scope_type="org")
        )
        u.org_active_id = org.id
        org_id = org.id
    etat = await _emettre_code(ctx, u)
    await ctx.session.flush()
    await journaliser(
        ctx,
        action="auth.inscription",
        cible_type="utilisateur",
        cible_id=u.id,
        cible=u.email,
        org_id=org_id,
    )
    return etat


CODE_VERIFICATION_DUREE_S = 900
CODE_VERIFICATION_ESSAIS_MAX = 5
CODE_VERIFICATION_RENVOI_DELAI_S = 60


def _nouveau_code() -> str:
    return f"{_secrets.randbelow(900000) + 100000:06d}"


async def _envoyer_code_verification(ctx: CtxPublic, u: Utilisateur, code: str) -> None:
    """AgentMail en priorité, repli SMTP admin (`courriel`), best-effort dans tous
    les cas : un échec d'envoi ne fait jamais échouer l'inscription (le renvoi
    permet de réessayer), il est seulement journalisé."""
    try:
        await agentmail.envoyer_code(u.email, code)
        return
    except Exception as exc:  # noqa: BLE001 — repli ci-dessous
        await journaliser(
            ctx,
            action="auth.code_envoi_agentmail_echec",
            cible_type="utilisateur",
            cible_id=u.id,
            cible=u.email,
            resultat="echec",
            details={"erreur": str(exc)[:200]},
        )
    try:
        await courriel.envoyer(
            u.email,
            "Synelia Cloud — vérifiez votre email",
            "Vérifiez votre email",
            [f"Votre code de vérification : {code}", "Ce code expire dans 15 minutes."],
        )
    except Exception as exc:  # noqa: BLE001 — best-effort, renvoi possible
        await journaliser(
            ctx,
            action="auth.code_envoi_smtp_echec",
            cible_type="utilisateur",
            cible_id=u.id,
            cible=u.email,
            resultat="echec",
            details={"erreur": str(exc)[:200]},
        )


async def _emettre_code(ctx: CtxPublic, u: Utilisateur) -> dict[str, Any]:
    """Invalide les codes précédents encore actifs, émet un code frais, l'envoie."""
    precedents = (
        await ctx.session.execute(
            select(VerificationEmail).where(
                VerificationEmail.utilisateur_id == u.id,
                VerificationEmail.consommee_le.is_(None),
            )
        )
    ).scalars()
    for p in precedents:
        p.consommee_le = maintenant()
    code = _nouveau_code()
    v = VerificationEmail(
        utilisateur_id=u.id,
        code_hash=hacher_jeton(code),
        expire_le=dans(CODE_VERIFICATION_DUREE_S),
    )
    ctx.session.add(v)
    await _envoyer_code_verification(ctx, u, code)
    return {
        "email": u.email,
        "expire": v.expire_le,
        "essaisRestants": CODE_VERIFICATION_ESSAIS_MAX,
    }


async def _verification_active(ctx: CtxPublic, u: Utilisateur) -> VerificationEmail | None:
    return (
        (
            await ctx.session.execute(
                select(VerificationEmail)
                .where(
                    VerificationEmail.utilisateur_id == u.id,
                    VerificationEmail.consommee_le.is_(None),
                )
                .order_by(VerificationEmail.cree_le.desc())
            )
        )
        .scalars()
        .first()
    )


@router.post(
    "/verification-email",
    response_model=m.Session,
    response_model_exclude_none=True,
)
async def verifier_email(ctx: CtxPublic, corps: m.VerificationEmailConfirmation) -> Any:
    u = await _utilisateur_par_email(ctx, str(corps.email))
    if u is None:
        raise erreurs.non_authentifie("Identifiants incorrects.")
    if u.statut != "verification_requise":
        raise erreurs.conflit("Email déjà vérifié.", code="email_deja_verifie")
    v = await _verification_active(ctx, u)
    if v is None or v.expire_le < maintenant():
        raise erreurs.validation("Code expiré : demandez un nouveau code.", {"code": "code_expire"})
    if v.essais >= CODE_VERIFICATION_ESSAIS_MAX:
        raise erreurs.interdit("Trop de tentatives : demandez un nouveau code.", code="code_bloque")
    if hacher_jeton(corps.code.strip()) != v.code_hash:
        v.essais += 1
        await ctx.session.flush()
        raise erreurs.interdit("Code incorrect.", code="code_invalide")
    v.consommee_le = maintenant()
    u.statut = "actif"
    await ctx.session.flush()
    rep = await service.ouvrir_session(
        ctx.session, u, ip=ctx.ip, user_agent=ctx.entete("user-agent"), org_id=u.org_active_id
    )
    await journaliser(
        ctx,
        action="auth.email_verifie",
        cible_type="utilisateur",
        cible_id=u.id,
        cible=u.email,
        org_id=u.org_active_id,
    )
    return rep


@router.post(
    "/verification-email/renvoi",
    response_model=m.VerificationEmailEtat,
    status_code=status.HTTP_202_ACCEPTED,
    response_model_exclude_none=True,
)
async def renvoyer_code_verification(ctx: CtxPublic, corps: m.VerificationEmailDemande) -> Any:
    u = await _utilisateur_par_email(ctx, str(corps.email))
    if u is None:
        raise erreurs.non_authentifie("Identifiants incorrects.")
    if u.statut != "verification_requise":
        raise erreurs.conflit("Email déjà vérifié.", code="email_deja_verifie")
    v = await _verification_active(ctx, u)
    if v is not None:
        delai = (maintenant() - v.cree_le).total_seconds()
        if delai < CODE_VERIFICATION_RENVOI_DELAI_S:
            raise erreurs.interdit(
                f"Patientez {int(CODE_VERIFICATION_RENVOI_DELAI_S - delai)} s avant un renvoi.",
                code="renvoi_trop_tot",
            )
    return await _emettre_code(ctx, u)


def _invitation_contrat(i: Invitation, org_nom: str | None) -> dict[str, Any]:
    statut = i.statut
    if statut == "en_attente" and i.expire_le < maintenant():
        statut = "expiree"
    return {
        "id": i.id,
        "email": i.email,
        "orgId": i.org_id,
        "orgNom": org_nom,
        "role": i.role,
        "scopeType": i.scope_type,
        "scopeId": i.scope_id,
        "invitePar": i.invite_par,
        "expire": i.expire_le,
        "statut": statut,
    }


async def _invitation(ctx: Contexte, jeton: str) -> tuple[Invitation, Organisation | None]:
    i = (
        await ctx.session.execute(
            select(Invitation).where(Invitation.jeton_hash == hacher_jeton(jeton))
        )
    ).scalar_one_or_none()
    if i is None:
        raise erreurs.introuvable("Invitation")
    return i, await ctx.session.get(Organisation, i.org_id)


@router.get("/invitations/{jeton}", response_model=m.Invitation, response_model_exclude_none=True)
async def obtenir_invitation(ctx: CtxPublic, jeton: str) -> Any:
    i, o = await _invitation(ctx, jeton)
    return _invitation_contrat(i, o.nom if o else None)


@router.post("/invitations/{jeton}", response_model=m.Session, response_model_exclude_none=True)
async def accepter_invitation(
    ctx: CtxPublic,
    jeton: str,
    corps: m.AuthInvitationsJetonPostRequest = Body(default=m.AuthInvitationsJetonPostRequest()),
) -> Any:
    i, _ = await _invitation(ctx, jeton)
    if i.statut != "en_attente":
        raise erreurs.conflit("Invitation déjà utilisée ou révoquée.", code="invitation_close")
    if i.expire_le < maintenant():
        raise erreurs.conflit("Invitation expirée.", code="invitation_expiree")
    u = await _utilisateur_par_email(ctx, i.email)
    if u is None:
        if not corps.nom or not corps.motDePasse:
            raise erreurs.validation(
                "Nom et mot de passe requis pour créer le compte.",
                {"nom": "requis", "motDePasse": "requis"},
            )
        u = Utilisateur(
            email=i.email.lower(),
            nom=corps.nom,
            mot_de_passe_hash=hacher_mot_de_passe(corps.motDePasse.get_secret_value()),
            statut="actif",
        )
        ctx.session.add(u)
        await ctx.session.flush()
    ctx.session.add(
        Membership(
            utilisateur_id=u.id,
            org_id=i.org_id,
            role=i.role,
            scope_type=i.scope_type,
            scope_id=i.scope_id,
        )
    )
    i.statut = "acceptee"
    u.org_active_id = u.org_active_id or i.org_id
    await ctx.session.flush()
    rep = await service.ouvrir_session(
        ctx.session, u, ip=ctx.ip, user_agent=ctx.entete("user-agent"), org_id=i.org_id
    )
    await journaliser(
        ctx,
        action="invitation.acceptee",
        cible_type="invitation",
        cible_id=i.id,
        cible=i.email,
        org_id=i.org_id,
    )
    return rep


@router.post(
    "/mot-de-passe/oubli", response_model=m.AccuseReception, response_model_exclude_none=True
)
async def demander_reinitialisation(ctx: CtxPublic, corps: m.AuthMotDePasseOubliPostRequest) -> Any:
    u = await _utilisateur_par_email(ctx, str(corps.email))
    if u is not None:
        brut = jeton_opaque()
        u.reinit_jeton_hash = hacher_jeton(brut)
        u.reinit_expire_le = dans(3600)
        lien = f"{ctx.reglages.url_frontend}/reinitialiser-mot-de-passe?jeton={brut}"
        await courriel.envoyer(
            u.email,
            "Réinitialiser votre mot de passe Synelia Cloud",
            f"Bonjour {u.nom},",
            [
                "Une réinitialisation de mot de passe a été demandée pour votre compte. "
                "Ce lien est valable une heure.",
            ],
            bouton_texte="Réinitialiser mon mot de passe",
            bouton_url=lien,
        )
        await journaliser(
            ctx,
            action="auth.reinitialisation_demandee",
            cible_type="utilisateur",
            cible_id=u.id,
            details={"jeton_dev": brut if ctx.reglages.env != "production" else "***"},
        )
    return {
        "reference": ctx.correlation_id,
        "message": "Si un compte existe, un lien de réinitialisation a été envoyé.",
        "delaiReponseHeures": 1,
    }


@router.post(
    "/mot-de-passe/reinitialiser", response_model=m.Session, response_model_exclude_none=True
)
async def reinitialiser_mot_de_passe(
    ctx: CtxPublic, corps: m.AuthMotDePasseReinitialiserPostRequest
) -> Any:
    u = (
        await ctx.session.execute(
            select(Utilisateur).where(Utilisateur.reinit_jeton_hash == hacher_jeton(corps.jeton))
        )
    ).scalar_one_or_none()
    if u is None or not u.reinit_expire_le or u.reinit_expire_le < maintenant():
        raise erreurs.validation(
            "Jeton de réinitialisation invalide ou expiré.", {"jeton": "invalide"}
        )
    u.mot_de_passe_hash = hacher_mot_de_passe(corps.motDePasse.get_secret_value())
    u.reinit_jeton_hash = None
    for s in (
        await ctx.session.execute(
            select(SessionAuth).where(
                SessionAuth.utilisateur_id == u.id, SessionAuth.revoquee_le.is_(None)
            )
        )
    ).scalars():
        s.revoquee_le = maintenant()
    rep = await service.ouvrir_session(
        ctx.session, u, ip=ctx.ip, user_agent=ctx.entete("user-agent")
    )
    await journaliser(
        ctx, action="auth.reinitialisation", cible_type="utilisateur", cible_id=u.id, cible=u.email
    )
    return rep


@router.get("/sso/decouverte", response_model=m.DecouverteSso, response_model_exclude_none=True)
async def decouvrir_sso(ctx: CtxPublic, email: str) -> Any:
    domaine = email.rsplit("@", 1)[-1].lower() if "@" in email else email.lower()
    o = (
        await ctx.session.execute(select(Organisation).where(Organisation.domaine == domaine))
    ).scalar_one_or_none()
    sso = (o.sso or {}) if o else {}
    if not o or not sso.get("actif"):
        return {"federationDisponible": False}
    return {
        "federationDisponible": True,
        "orgId": o.id,
        "orgNom": o.nom,
        "protocole": sso.get("protocole", "oidc"),
        "urlDemarrage": f"{ctx.reglages.url_publique}{ctx.reglages.prefixe_api}/auth/sso/demarrage?org={o.id}",
        "libelleBouton": f"Continuer avec {o.nom}",
    }


@router.post("/sso/callback", response_model=m.Session, response_model_exclude_none=True)
async def terminer_sso(ctx: CtxPublic, corps: m.AuthSsoCallbackPostRequest) -> Any:
    # ponytail: l'échange de code OIDC (Authlib) arrive avec la fédération réelle ; refus explicite d'ici là
    raise erreurs.non_porte("La fédération SSO n'est pas encore activée sur cette plateforme.")
