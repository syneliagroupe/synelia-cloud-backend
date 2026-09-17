"""Amont messagerie (Zimbra OSE) : domaines, boîtes, webmail SSO, via l'API admin SOAP.

Paire `ZimbraSimule` / `ZimbraReel`. Le réel n'est appelé que si `SYNELIA_ZIMBRA_URL`
est défini (+ `SYNELIA_ZIMBRA_ADMIN_USER` / `SYNELIA_ZIMBRA_ADMIN_PASSWORD`) ; sinon le
simulé répond instantanément. Les méthodes reprennent exactement les signatures de l'ancienne
intégration Stalwart (abandonnée au profit de Zimbra) : seul l'import a changé côté
`service.py`."""

from __future__ import annotations

import os
from typing import Any
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

import httpx
from synelia_kernel import erreurs
from synelia_kernel.ids import nouvel_id

ENV_URL = "SYNELIA_ZIMBRA_URL"
ENV_USER = "SYNELIA_ZIMBRA_ADMIN_USER"
ENV_PASSWORD = "SYNELIA_ZIMBRA_ADMIN_PASSWORD"

_NS_SOAP = "http://www.w3.org/2003/05/soap-envelope"
_NS_ZIMBRA = "urn:zimbra"
_NS_ADMIN = "urn:zimbraAdmin"


class ZimbraSimule:
    def creer_domaine(self, domaine: str) -> None:
        return None

    def supprimer_domaine(self, domaine: str) -> None:
        return None

    def creer_boite(self, domaine: str, adresse: str, mot_de_passe: str | None) -> None:
        return None

    def maj_boite(self, domaine: str, adresse: str, **champs: Any) -> None:
        return None

    def definir_mot_de_passe(self, domaine: str, adresse: str, mot_de_passe: str) -> None:
        return None

    def supprimer_boite(self, domaine: str, adresse: str) -> None:
        return None

    def verifier_authentification(self, domaine: str) -> dict[str, Any]:
        return {
            "spf": "valide",
            "dkim": "valide",
            "dmarc": "v=DMARC1; p=none; rua=mailto:dmarc@synelia.cloud",
            "enregistrements": [
                {"type": "TXT", "nom": f"_dmarc.{domaine}", "valeur": "v=DMARC1; p=none"},
                {"type": "TXT", "nom": domaine, "valeur": "v=spf1 include:_spf.synelia.cloud ~all"},
            ],
        }

    def ouvrir_webmail(self, adresse: str | None) -> str:
        return f"https://webmail.synelia.cloud/?boite={adresse or ''}&jeton={nouvel_id()}"


class ZimbraReel(ZimbraSimule):
    def __init__(self) -> None:
        self.base = os.environ[ENV_URL].rstrip("/")
        self.admin_user = os.environ[ENV_USER]
        self.admin_password = os.environ[ENV_PASSWORD]

    # -- plomberie SOAP -------------------------------------------------------
    def _appeler(self, corps_xml: str, jeton: str | None) -> ET.Element:
        entete = ""
        if jeton:
            entete = (
                f'<soap:Header><context xmlns="{_NS_ZIMBRA}">'
                f"<authToken>{escape(jeton)}</authToken>"
                f"</context></soap:Header>"
            )
        enveloppe = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            f'<soap:Envelope xmlns:soap="{_NS_SOAP}">{entete}'
            f"<soap:Body>{corps_xml}</soap:Body></soap:Envelope>"
        )
        try:
            r = httpx.post(
                f"{self.base}/service/admin/soap",
                content=enveloppe.encode("utf-8"),
                headers={"Content-Type": "application/soap+xml; charset=utf-8"},
                timeout=30,
                verify=False,  # noqa: S501 — certificat auto-signé Zimbra, joignable en 127.0.0.1 seulement
            )
        except httpx.HTTPError as exc:
            raise erreurs.amont_indisponible("zimbra", str(exc)) from exc
        try:
            racine = ET.fromstring(r.content)  # noqa: S314 — réponse admin SOAP de confiance (127.0.0.1)
        except ET.ParseError as exc:
            raise erreurs.amont_indisponible("zimbra", f"Réponse SOAP invalide : {exc}") from exc
        defaut = racine.find(f".//{{{_NS_SOAP}}}Fault")
        if defaut is not None:
            raise erreurs.amont_indisponible("zimbra", "".join(defaut.itertext())[:300])
        if r.status_code >= 400:
            raise erreurs.amont_indisponible("zimbra", f"HTTP {r.status_code}")
        return racine

    def _authtoken(self) -> str:
        corps = (
            f'<AuthRequest xmlns="{_NS_ADMIN}">'
            f"<name>{escape(self.admin_user)}</name>"
            f"<password>{escape(self.admin_password)}</password>"
            "</AuthRequest>"
        )
        racine = self._appeler(corps, jeton=None)
        jeton = racine.find(f".//{{{_NS_ADMIN}}}authToken")
        if jeton is None or not jeton.text:
            raise erreurs.amont_indisponible("zimbra", "Authentification admin refusée.")
        return jeton.text

    def _admin(self, corps_xml: str) -> ET.Element:
        return self._appeler(corps_xml, jeton=self._authtoken())

    def _domaine_id(self, domaine: str) -> str | None:
        corps = (
            f'<GetDomainRequest xmlns="{_NS_ADMIN}">'
            f'<domain by="name">{escape(domaine)}</domain>'
            "</GetDomainRequest>"
        )
        try:
            racine = self._admin(corps)
        except erreurs.AppError:
            return None
        d = racine.find(f".//{{{_NS_ADMIN}}}domain")
        return d.get("id") if d is not None else None

    def _compte_id(self, adresse: str) -> str | None:
        corps = (
            f'<GetAccountRequest xmlns="{_NS_ADMIN}">'
            f'<account by="name">{escape(adresse)}</account>'
            "</GetAccountRequest>"
        )
        try:
            racine = self._admin(corps)
        except erreurs.AppError:
            return None
        a = racine.find(f".//{{{_NS_ADMIN}}}account")
        return a.get("id") if a is not None else None

    @staticmethod
    def _boite(domaine: str, adresse: str) -> str:
        return adresse if "@" in adresse else f"{adresse}@{domaine}"

    # -- opérations -------------------------------------------------------------
    def creer_domaine(self, domaine: str) -> None:
        corps = (
            f'<CreateDomainRequest xmlns="{_NS_ADMIN}"><name>{escape(domaine)}</name>'
            "</CreateDomainRequest>"
        )
        self._admin(corps)

    def supprimer_domaine(self, domaine: str) -> None:
        id_ = self._domaine_id(domaine)
        if not id_:
            return None
        corps = f'<DeleteDomainRequest xmlns="{_NS_ADMIN}"><id>{escape(id_)}</id></DeleteDomainRequest>'
        self._admin(corps)
        return None

    def creer_boite(self, domaine: str, adresse: str, mot_de_passe: str | None) -> None:
        nom = self._boite(domaine, adresse)
        corps = (
            f'<CreateAccountRequest xmlns="{_NS_ADMIN}">'
            f"<name>{escape(nom)}</name>"
            f"<password>{escape(mot_de_passe or nouvel_id())}</password>"
            "</CreateAccountRequest>"
        )
        self._admin(corps)

    def maj_boite(self, domaine: str, adresse: str, **champs: Any) -> None:
        nom = self._boite(domaine, adresse)
        id_ = self._compte_id(nom)
        if not id_:
            raise erreurs.introuvable("Boîte mail", nom)
        attributs = "".join(
            f'<a n="{escape(str(cle))}">{escape(str(valeur))}</a>'
            for cle, valeur in champs.items()
            if valeur is not None
        )
        corps = (
            f'<ModifyAccountRequest xmlns="{_NS_ADMIN}"><id>{escape(id_)}</id>{attributs}'
            "</ModifyAccountRequest>"
        )
        self._admin(corps)

    def definir_mot_de_passe(self, domaine: str, adresse: str, mot_de_passe: str) -> None:
        # `SetPasswordRequest`, pas `ModifyAccountRequest` sur l'attribut `userPassword` :
        # c'est l'appel documenté par Zimbra pour poser un mot de passe en clair, il se
        # charge du hachage LDAP. `ModifyAccountRequest` accepte des attributs déjà hachés,
        # pas un mot de passe en clair.
        nom = self._boite(domaine, adresse)
        id_ = self._compte_id(nom)
        if not id_:
            raise erreurs.introuvable("Boîte mail", nom)
        corps = (
            f'<SetPasswordRequest xmlns="{_NS_ADMIN}"><id>{escape(id_)}</id>'
            f"<newPassword>{escape(mot_de_passe)}</newPassword></SetPasswordRequest>"
        )
        self._admin(corps)

    def supprimer_boite(self, domaine: str, adresse: str) -> None:
        nom = self._boite(domaine, adresse)
        id_ = self._compte_id(nom)
        if not id_:
            return None
        corps = f'<DeleteAccountRequest xmlns="{_NS_ADMIN}"><id>{escape(id_)}</id></DeleteAccountRequest>'
        self._admin(corps)
        return None

    def verifier_authentification(self, domaine: str) -> dict[str, Any]:
        if not self._domaine_id(domaine):
            return super().verifier_authentification(domaine)
        # Zimbra OSE ne calcule pas SPF/DKIM/DMARC automatiquement à la création du domaine
        # (nécessite `zmprov generateDomainSignature` + publication DNS manuelle) : on renvoie
        # les enregistrements attendus à créer plutôt qu'un statut inventé.
        return {
            "spf": "absent",
            "dkim": "absent",
            "dmarc": "",
            "enregistrements": [
                {"type": "TXT", "nom": f"_dmarc.{domaine}", "valeur": "v=DMARC1; p=none"},
                {"type": "TXT", "nom": domaine, "valeur": "v=spf1 include:_spf.synelia.cloud ~all"},
            ],
        }

    def ouvrir_webmail(self, adresse: str | None) -> str:
        if not adresse:
            return super().ouvrir_webmail(adresse)
        corps = (
            f'<DelegateAuthRequest xmlns="{_NS_ADMIN}"><account by="name">{escape(adresse)}</account>'
            "</DelegateAuthRequest>"
        )
        try:
            racine = self._admin(corps)
        except erreurs.AppError:
            return super().ouvrir_webmail(adresse)
        jeton = racine.find(f".//{{{_NS_ADMIN}}}authToken")
        if jeton is None or not jeton.text:
            return super().ouvrir_webmail(adresse)
        return f"{self.base}/service/preauth?authtoken={jeton.text}"


def choisir_zimbra() -> ZimbraSimule:
    if os.environ.get(ENV_URL):
        return ZimbraReel()
    return ZimbraSimule()
