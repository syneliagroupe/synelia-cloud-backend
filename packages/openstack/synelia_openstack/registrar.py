"""Registrar : disponibilité, commande, transfert, renouvellement, code-auth."""

from __future__ import annotations

import hashlib
import os
import time
from typing import Any

import httpx
from synelia_kernel import erreurs
from synelia_kernel.ids import jeton_opaque, nouvel_id

ENV_URL = "SYNELIA_REGISTRAR_URL"
ENV_APPKEY = "SYNELIA_OVH_APPKEY"
ENV_APPSECRET = "SYNELIA_OVH_APPSECRET"
ENV_CONSUMERKEY = "SYNELIA_OVH_CONSUMERKEY"

OCCUPES = {"google.com", "synelia.ci"}


class RegistrarSimule:
    def verifier(self, nom: str) -> bool:
        return nom.lower() not in OCCUPES

    def commander(self, nom: str, duree_annees: int) -> dict[str, Any]:
        return {
            "id": f"dom-{nouvel_id()[:8]}",
            "code_auth": jeton_opaque(12),
            "expiration": duree_annees,
        }

    def transferer(self, nom: str, code_auth: str) -> dict[str, Any]:
        return {"id": f"dom-{nouvel_id()[:8]}", "code_auth": jeton_opaque(12)}

    def renouveler(self, nom: str, duree_annees: int) -> None:
        return None

    def code_auth(self, nom: str) -> dict[str, Any]:
        return {"code": jeton_opaque(12), "expire_heures": 24}


class RegistrarOvh(RegistrarSimule):
    """Registrar réel via l'API OVH (signature applicative), branché seulement
    quand `SYNELIA_REGISTRAR_URL` (endpoint régional, ex. https://eu.api.ovh.com/1.0)
    et les trois identifiants OVH sont posés."""

    def __init__(self) -> None:
        self.base = os.environ[ENV_URL].rstrip("/")
        self.appkey = os.environ[ENV_APPKEY]
        self.appsecret = os.environ[ENV_APPSECRET]
        self.consumerkey = os.environ[ENV_CONSUMERKEY]

    def _decalage(self) -> int:
        # OVH exige un timestamp proche de l'horloge serveur ; l'API expose /auth/time.
        try:
            r = httpx.get(f"{self.base}/auth/time", timeout=10)
            return int(r.text) - int(time.time())
        except httpx.HTTPError:
            return 0

    def _requete(self, methode: str, chemin: str, corps: dict[str, Any] | None = None) -> Any:
        import json as _json

        url = f"{self.base}{chemin}"
        # Séparateurs compacts : doivent matcher octet pour octet ce qu'on envoie sur
        # le fil (httpx sérialise `json=` sans espaces), sinon OVH renvoie "Invalid
        # signature" dès que le corps a plus d'une clé.
        corps_octets = (
            _json.dumps(corps, separators=(",", ":")).encode() if corps is not None else b""
        )
        horodatage = str(int(time.time()) + self._decalage())
        a_signer = "+".join(
            [self.appsecret, self.consumerkey, methode, url, corps_octets.decode(), horodatage]
        )
        signature = "$1$" + hashlib.sha1(a_signer.encode()).hexdigest()  # noqa: S324 — schéma OVH imposé
        entetes = {
            "X-Ovh-Application": self.appkey,
            "X-Ovh-Consumer": self.consumerkey,
            "X-Ovh-Signature": signature,
            "X-Ovh-Timestamp": horodatage,
            "Content-Type": "application/json",
        }
        try:
            r = httpx.request(
                methode,
                url,
                headers=entetes,
                content=corps_octets if corps is not None else None,
                timeout=30,
            )
        except httpx.HTTPError as exc:
            raise erreurs.amont_indisponible("registrar", str(exc)) from exc
        if r.status_code >= 400:
            raise erreurs.amont_indisponible("registrar", f"HTTP {r.status_code}: {r.text[:300]}")
        return r.json() if r.content else None

    def verifier(self, nom: str) -> bool:
        panier = self._requete("POST", "/order/cart", {"ovhSubsidiary": "FR"})
        panier_id = panier["cartId"]
        self._requete("POST", f"/order/cart/{panier_id}/assign")
        try:
            resultat = self._requete(
                "GET", f"/order/cart/{panier_id}/domain?domain={nom}&exclusiveOption=false"
            )
            # OVH répond `orderable: true` aussi pour un domaine déjà enregistré ailleurs —
            # dans ce cas `action` vaut "transfer" (achetable comme transfert, pas comme
            # création). Sans ce filtre, un domaine pris (ex. google.com) ressortait
            # "disponible" : constaté en direct pendant cette session.
            return bool(
                resultat and resultat[0].get("orderable") and resultat[0].get("action") == "create"
            )
        finally:
            self._requete("DELETE", f"/order/cart/{panier_id}")

    def commander(self, nom: str, duree_annees: int) -> dict[str, Any]:
        # Flux panier→checkout OVH réel (paiement débité sur le compte). Aucun appel
        # n'est déclenché sans confirmation explicite en amont dans le module métier.
        panier = self._requete("POST", "/order/cart", {"ovhSubsidiary": "FR"})
        panier_id = panier["cartId"]
        self._requete("POST", f"/order/cart/{panier_id}/assign")
        item = self._requete(
            "POST",
            f"/order/cart/{panier_id}/domain",
            {"domain": nom, "duration": f"P{duree_annees}Y"},
        )
        commande = self._requete(
            "POST", f"/order/cart/{panier_id}/checkout", {"autoPayWithPreferredPaymentMethod": True}
        )
        return {
            "id": str(commande.get("orderId", item.get("itemId", ""))),
            "code_auth": None,
            "expiration": duree_annees,
        }

    def transferer(self, nom: str, code_auth: str) -> dict[str, Any]:
        panier = self._requete("POST", "/order/cart", {"ovhSubsidiary": "FR"})
        panier_id = panier["cartId"]
        self._requete("POST", f"/order/cart/{panier_id}/assign")
        item = self._requete(
            "POST",
            f"/order/cart/{panier_id}/domain",
            {"domain": nom, "authInfo": code_auth},
        )
        commande = self._requete(
            "POST", f"/order/cart/{panier_id}/checkout", {"autoPayWithPreferredPaymentMethod": True}
        )
        return {"id": str(commande.get("orderId", item.get("itemId", ""))), "code_auth": None}

    def renouveler(self, nom: str, duree_annees: int) -> None:
        self._requete(
            "POST", f"/domain/{nom}/serviceInfos/renew", {"period": duree_annees * 12}
        )

    def code_auth(self, nom: str) -> dict[str, Any]:
        self._requete("POST", f"/domain/{nom}/authInfo")
        return {"code": "envoye_par_email_ovh", "expire_heures": 24}


def choisir_registrar() -> RegistrarSimule:
    """Le réel seulement quand un partenaire est configuré — partout ailleurs
    (lab y compris, aucune URL partenaire posée) le simulé, explicitement."""
    if os.environ.get(ENV_URL) and os.environ.get(ENV_APPKEY):
        return RegistrarOvh()
    return RegistrarSimule()
