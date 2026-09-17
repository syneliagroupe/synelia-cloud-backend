"""Serveur relais SMTP réel (module web_smtp) : AUTH + quota + relais vers l'amont.

Process séparé (`synelia relais-smtp`), branché sur la même base Postgres que l'API. Écoute en
clair sur un port de soumission (587 par défaut, configurable), impose STARTTLS avant `AUTH
PLAIN`/`AUTH LOGIN`, vérifie l'identifiant contre les enregistrements réels `smtp_relais` /
`smtp_cle` posés par le module `web_smtp` (mêmes secrets chiffrés que `Depot.definir_secrets`),
applique le quota journalier de l'enregistrement correspondant, puis relaie le message vers
l'amont (Zimbra, ou tout hôte SMTP configurable) via `smtplib`.

Aucune route HTTP : ce n'est pas un module du contrat, c'est l'infrastructure qui donne une
« vraie backing » aux identifiants déjà provisionnés par `web_smtp.service.ExecuteurSmtpActivate`
et par `POST /v1/web/smtp/cles`.
"""

from __future__ import annotations

import asyncio
import hmac
import os
import smtplib
import ssl
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from aiosmtpd.controller import UnthreadedController
from aiosmtpd.smtp import SMTP, AuthResult, Envelope, Session
from sqlalchemy import select
from synelia_contract import modeles as m
from synelia_db.modeles import Audit, Ressource
from synelia_db.session import fermer, initialiser_schema
from synelia_db.session import session as db_session
from synelia_kernel.chiffrement import dechiffrer
from synelia_kernel.dates import iso, maintenant
from synelia_kernel.ids import nouvel_id
from synelia_kernel.journal import journal

log = journal("relais_smtp")

HOTE_ECOUTE = os.environ.get("SYNELIA_SMTP_RELAIS_HOTE", "0.0.0.0")
PORT_ECOUTE = int(os.environ.get("SYNELIA_SMTP_RELAIS_PORT", "587"))
INTERVALLE_CACHE_S = float(os.environ.get("SYNELIA_SMTP_RELAIS_CACHE_S", "3"))

_CACHE: dict[str, dict[str, Any]] = {}
_TACHE_CACHE: asyncio.Task[None] | None = None  # référence gardée pour éviter le GC de la tâche


def _amont_hote_port() -> tuple[str, int]:
    """Hôte SMTP en amont : Zimbra (partagé, toutes orgs) si configuré, sinon un repli."""
    hote = os.environ.get("SYNELIA_ZIMBRA_SMTP_HOTE") or os.environ.get(
        "SYNELIA_SMTP_RELAIS_AMONT_HOTE"
    )
    if not hote:
        zimbra_url = os.environ.get("SYNELIA_ZIMBRA_URL")
        hote = urlparse(zimbra_url).hostname if zimbra_url else None
    hote = hote or "localhost"
    port = int(
        os.environ.get("SYNELIA_ZIMBRA_SMTP_PORT")
        or os.environ.get("SYNELIA_SMTP_RELAIS_AMONT_PORT")
        or "1025"
    )
    return hote, port


# ── certificat auto-signé (STARTTLS) ──────────────────────────────────────────
def _contexte_tls() -> ssl.SSLContext:
    dossier = Path(tempfile.gettempdir()) / "synelia-relais-smtp"
    dossier.mkdir(parents=True, exist_ok=True)
    chemin_cert, chemin_cle = dossier / "cert.pem", dossier / "cle.pem"
    if not chemin_cert.exists() or not chemin_cle.exists():
        _generer_certificat_autosigne(chemin_cert, chemin_cle)
    contexte = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    contexte.load_cert_chain(str(chemin_cert), str(chemin_cle))
    return contexte


def _generer_certificat_autosigne(chemin_cert: Path, chemin_cle: Path) -> None:
    import datetime as dt

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    cle = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    nom = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "smtp.synelia.cloud")])
    maintenant_dt = dt.datetime.now(dt.UTC)
    certificat = (
        x509.CertificateBuilder()
        .subject_name(nom)
        .issuer_name(nom)
        .public_key(cle.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(maintenant_dt - dt.timedelta(days=1))
        .not_valid_after(maintenant_dt + dt.timedelta(days=3650))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("smtp.synelia.cloud")]), critical=False
        )
        .sign(cle, hashes.SHA256())
    )
    chemin_cle.write_bytes(
        cle.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    chemin_cert.write_bytes(certificat.public_bytes(serialization.Encoding.PEM))


# ── cache des identifiants (rafraîchi en tâche de fond, lu par l'authenticator sync) ──────────
async def _rafraichir_cache() -> None:
    global _CACHE
    nouveau: dict[str, dict[str, Any]] = {}
    async with db_session() as s:
        q = select(Ressource).where(
            Ressource.type.in_(("smtp_relais", "smtp_cle")), Ressource.supprime_le.is_(None)
        )
        for r in (await s.execute(q)).scalars().all():
            identifiant = (r.donnees or {}).get("identifiant")
            if not identifiant:
                continue
            secret_chiffre = (r.secrets or {}).get("mot_de_passe")
            mot_de_passe = None
            if secret_chiffre:
                try:
                    mot_de_passe = dechiffrer(secret_chiffre)
                except Exception:  # noqa: BLE001 — secret corrompu ou clé changée
                    log.warning("relais_smtp.secret_illisible", ressource_id=r.id)
            nouveau[identifiant] = {
                "ressource_id": r.id,
                "type": r.type,
                "org_id": r.org_id,
                "identifiant": identifiant,
                "mot_de_passe": mot_de_passe,
            }
    _CACHE = nouveau


async def _boucle_rafraichissement() -> None:
    while True:
        try:
            await _rafraichir_cache()
        except Exception:  # noqa: BLE001 — ne jamais tuer la boucle de fond
            log.exception("relais_smtp.cache_echec")
        await asyncio.sleep(INTERVALLE_CACHE_S)


def _authentifier(
    server: SMTP, session: Session, envelope: Envelope, mechanism: str, auth_data: Any
) -> AuthResult:
    login = getattr(auth_data, "login", b"").decode(errors="replace")
    mot_de_passe = getattr(auth_data, "password", b"").decode(errors="replace")
    cred = _CACHE.get(login)
    if cred is None or not cred.get("mot_de_passe"):
        # `handled=False` : laisse aiosmtpd envoyer le 535 standard (sinon la connexion
        # reste muette et le client attend une réponse qui ne vient jamais).
        return AuthResult(success=False, handled=False)
    if not hmac.compare_digest(cred["mot_de_passe"], mot_de_passe):
        return AuthResult(success=False, handled=False)
    return AuthResult(success=True, auth_data=cred)


# ── journal d'audit (même schéma haché que `synelia.audit.journaliser`, sans `Contexte`) ──────
#
# Bug réel trouvé en vérifiant la chaîne (`synelia.audit.verifier_chaine`) sur ce lab : cette
# fonction recalculait sa propre empreinte à la main, avec une charge qui omettait `cible_type`/
# `cible_id` — présente ici dans `Audit.cible_type` mais jamais dans le hash. Le commentaire
# promettait « même schéma », le code avait divergé : toute ligne posée par le relais SMTP
# échouait la revérification, pas parce qu'elle avait été altérée mais parce que sa propre
# empreinte n'était pas correcte dès l'écriture. Corrigé en réutilisant `synelia.audit.empreinte`,
# la même fonction que `journaliser`, pour ne plus avoir deux formules qui peuvent diverger.
async def _journaliser(
    s: Any, *, org_id: str | None, action: str, resultat: str, details: dict[str, Any]
) -> None:
    from sqlalchemy import desc

    from synelia.audit import empreinte

    precedent = (
        await s.execute(
            select(Audit.hash).where(Audit.org_id == org_id).order_by(desc(Audit.date)).limit(1)
        )
    ).scalar_one_or_none()
    ligne = Audit(
        org_id=org_id,
        date=maintenant(),
        acteur="systeme:relais-smtp",
        action=action,
        cible_type="smtp_relais",
        resultat=resultat,
        details=details,
        hash_precedent=precedent,
    )
    ligne.hash = empreinte(precedent, org_id, ligne)
    s.add(ligne)


# ── quota : lu et incrémenté depuis la base au moment du relais (pas depuis le cache) ─────────
async def _verifier_et_incrementer_quota(cred: dict[str, Any]) -> tuple[bool, str]:
    async with db_session() as s:
        r = (
            await s.execute(select(Ressource).where(Ressource.id == cred["ressource_id"]))
        ).scalar_one_or_none()
        if r is None or r.supprime_le is not None:
            return False, "identifiant révoqué"
        donnees = dict(r.donnees or {})
        if r.type == "smtp_cle":
            if donnees.get("statut") != "active":
                return False, "clé révoquée"
            relais = (
                (
                    await s.execute(
                        select(Ressource).where(
                            Ressource.type == "smtp_relais",
                            Ressource.org_id == r.org_id,
                            Ressource.supprime_le.is_(None),
                        )
                    )
                )
                .scalars()
                .first()
            )
            if relais is None or not (relais.donnees or {}).get("actif"):
                return False, "relais inactif"
            plafond = donnees.get("quotaJour") or 0
            utilise = donnees.get("utiliseJour") or 0
            if plafond and utilise >= plafond:
                return False, "quota_depasse"
            donnees["utiliseJour"] = utilise + 1
            donnees["derniereUtilisation"] = iso(maintenant())
        else:  # smtp_relais
            if not donnees.get("actif"):
                return False, "relais inactif"
            quota = dict(donnees.get("quota") or {})
            plafond = quota.get("parJour") or 0
            utilise = quota.get("utiliseJour") or 0
            if plafond and utilise >= plafond:
                return False, "quota_depasse"
            quota["utiliseJour"] = utilise + 1
            donnees["quota"] = quota
        r.donnees = donnees
        await s.commit()
        return True, ""


# ── relais vers l'amont (bloquant : exécuté hors boucle via `asyncio.to_thread`) ───────────────
def _contexte_tls_amont() -> ssl.SSLContext:
    # Bug réel trouvé en vérifiant la livraison en direct (2026-09-07) : `create_default_context()`
    # exige une chaîne de confiance publique, or Zimbra (l'amont partagé, `zimbra:587` sur le
    # réseau Docker interne) présente un certificat auto-signé — même motif que
    # `synelia_openstack.zimbra.ZimbraReel` (`verify=False`, cf. son commentaire). Sans ce contexte
    # permissif, STARTTLS échouait systématiquement (`CERTIFICATE_VERIFY_FAILED`) et tout message
    # finissait en `differe`, quels que soient AUTH/quota — ce que le commentaire d'origine
    # attribuait à tort à l'absence de compte de service Zimbra.
    contexte = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    contexte.check_hostname = False
    contexte.verify_mode = ssl.CERT_NONE  # noqa: S501 — certificat auto-signé, amont interne
    return contexte


def _relayer_vers_amont(
    mail_from: str, rcpt_tos: list[str], contenu: bytes
) -> tuple[str, str, str]:
    hote, port = _amont_hote_port()
    identifiant = os.environ.get("SYNELIA_SMTP_RELAIS_AMONT_IDENTIFIANT")
    mot_de_passe = os.environ.get("SYNELIA_SMTP_RELAIS_AMONT_MOT_DE_PASSE")
    try:
        with smtplib.SMTP(hote, port, timeout=10) as client:
            client.ehlo()
            if client.has_extn("STARTTLS"):
                client.starttls(context=_contexte_tls_amont())
                client.ehlo()
            if identifiant and mot_de_passe:
                client.login(identifiant, mot_de_passe)
            refuses = client.sendmail(mail_from, rcpt_tos, contenu)
        if refuses:
            return "rebond", "550", str(refuses)
        return "remis", "250", f"relayé vers {hote}:{port}"
    except (OSError, smtplib.SMTPException) as exc:
        return "differe", "451", f"amont {hote}:{port} indisponible : {exc}"


class GestionnaireRelais:
    """Handler aiosmtpd : AUTH déjà validé par `_authentifier`, ici on impose le quota et on relaie."""

    async def handle_DATA(self, server: SMTP, session: Session, envelope: Envelope) -> str:  # noqa: N802
        cred = getattr(session, "auth_data", None)
        if not cred:
            return "530 5.7.0 Authentification requise"

        ok, motif = await _verifier_et_incrementer_quota(cred)
        org_id, identifiant = cred["org_id"], cred["identifiant"]
        if not ok:
            resultat = "quota_depasse" if motif == "quota_depasse" else "rejete"
            async with db_session() as s:
                await _journaliser(
                    s,
                    org_id=org_id,
                    action="smtp.relais.message",
                    resultat=resultat,
                    details={
                        "identifiant": identifiant,
                        "motif": motif,
                        "de": envelope.mail_from,
                        "vers": list(envelope.rcpt_tos),
                    },
                )
                await s.commit()
            log.info("relais_smtp.refuse", identifiant=identifiant, motif=motif)
            if motif == "quota_depasse":
                return "552 5.7.1 Quota SMTP journalier dépassé"
            return f"550 5.7.1 Relais refusé : {motif}"

        contenu = envelope.original_content or b""
        statut, code, detail = await asyncio.to_thread(
            _relayer_vers_amont, envelope.mail_from or "", list(envelope.rcpt_tos), contenu
        )
        async with db_session() as s:
            for dest in envelope.rcpt_tos:
                msg = m.MessageSmtp(
                    id=nouvel_id(),
                    ts=maintenant(),
                    de=envelope.mail_from or "",
                    vers=dest,
                    statut=statut,
                    code=code,
                    detail=detail,
                )
                s.add(
                    Ressource(
                        id=msg.id,
                        org_id=org_id,
                        type="smtp_message",
                        nom=msg.id,  # `depot_message` a `champ_nom="id"` (service.py) : on s'aligne
                        statut=statut,
                        donnees=msg.model_dump(mode="json"),
                    )
                )
            await _journaliser(
                s,
                org_id=org_id,
                action="smtp.relais.message",
                resultat="accepte",
                details={
                    "identifiant": identifiant,
                    "statut": statut,
                    "de": envelope.mail_from,
                    "vers": list(envelope.rcpt_tos),
                },
            )
            await s.commit()
        log.info("relais_smtp.relaye", identifiant=identifiant, statut=statut)
        return "250 Message accepté pour relais" if statut != "rejete" else f"550 5.4.3 {detail}"


def demarrer() -> None:
    """Point d'entrée synchrone (`synelia relais-smtp`) : construit sa propre boucle asyncio."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(initialiser_schema())
    loop.run_until_complete(_rafraichir_cache())

    controller = UnthreadedController(
        GestionnaireRelais(),
        loop=loop,
        hostname=HOTE_ECOUTE,
        port=PORT_ECOUTE,
        authenticator=_authentifier,
        auth_required=True,
        auth_require_tls=True,
        tls_context=_contexte_tls(),
        ident="Synelia Cloud — relais SMTP",
    )
    controller.begin()
    global _TACHE_CACHE
    _TACHE_CACHE = loop.create_task(_boucle_rafraichissement())
    amont_hote, amont_port = _amont_hote_port()
    log.info(
        "relais_smtp.demarre",
        hote=HOTE_ECOUTE,
        port=PORT_ECOUTE,
        amont=f"{amont_hote}:{amont_port}",
    )
    try:
        loop.run_forever()
    finally:
        controller.end()
        loop.run_until_complete(fermer())
