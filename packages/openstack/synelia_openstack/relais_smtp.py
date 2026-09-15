"""Amont du relais SMTP produit (module web_smtp).

Remplace l'ancienne paire `PostalSimule`/`PostalReel` (`postal.py`, supprimée) qui visait un
produit externe hypothétique — « Postal » — jamais déployé sur cet environnement (`SYNELIA_POSTAL_URL`
n'était jamais défini, donc toujours simulé). Le VRAI relais SMTP de ce déploiement existe déjà :
c'est `apps/synelia/synelia/relais_smtp.py`, un serveur SMTP+AUTH sur mesure qui tourne en process
séparé (`docker-compose.relais-smtp.dev01.yml`), branché sur la même base que l'API. Il n'expose
aucune API HTTP — juste le protocole SMTP.

Paire `RelaisSmtpSimule` / `RelaisSmtpReel`. Le réel n'est appelé que si `SYNELIA_RELAIS_SMTP_HOTE`
est défini (hôte joignable depuis le conteneur API — nom de conteneur sur le réseau Docker partagé
`synelia-backend-dev01_default`, pas forcément le même hôte/port que ceux affichés aux clients,
`service.HOTE`/`service.PORTS`). `envoyer_test` s'y connecte comme un vrai client SMTP (`smtplib`),
authentifié avec les identifiants réels de l'org (posés par `ExecuteurSmtpActivate`), et envoie un
vrai message — pas un HTTP POST vers une API imaginaire.

`creer_cle`/`regenerer_identifiants` restent des générateurs de jeton opaque, simulé ou réel : ce
jeton devient réel dès qu'il est stocké (chiffré) et relu par `relais_smtp.py` pour authentifier une
connexion — aucun appel amont n'est nécessaire pour ça, contrairement à `envoyer_test`."""

from __future__ import annotations

import os
import smtplib
import ssl
from email.message import EmailMessage
from typing import Any

from synelia_kernel import erreurs
from synelia_kernel.ids import jeton_opaque

ENV_HOTE = "SYNELIA_RELAIS_SMTP_HOTE"
ENV_PORT = "SYNELIA_RELAIS_SMTP_PORT"


class RelaisSmtpSimule:
    def creer_cle(self) -> str:
        return jeton_opaque(24)

    def regenerer_identifiants(self) -> str:
        return jeton_opaque(18)

    def envoyer_test(
        self,
        de: str,
        destinataire: str,
        *,
        identifiant: str | None = None,
        mot_de_passe: str | None = None,
    ) -> dict[str, Any]:
        return {"envoye": True, "code": "250", "detail": "Message d'essai remis (simulé)."}


class RelaisSmtpReel(RelaisSmtpSimule):
    def __init__(self) -> None:
        self.hote = os.environ[ENV_HOTE]
        self.port = int(os.environ.get(ENV_PORT, "587"))

    def envoyer_test(
        self,
        de: str,
        destinataire: str,
        *,
        identifiant: str | None = None,
        mot_de_passe: str | None = None,
    ) -> dict[str, Any]:
        if not identifiant or not mot_de_passe:
            raise erreurs.amont_indisponible(
                "relais_smtp",
                "Aucun identifiant SMTP actif pour cette organisation : activez le relais "
                "avant de le tester.",
            )
        message = EmailMessage()
        message["Subject"] = "Synelia Cloud — message d'essai du relais SMTP"
        message["From"] = de or identifiant
        message["To"] = destinataire
        message.set_content(
            "Ceci est un message d'essai envoyé réellement via le relais SMTP Synelia Cloud."
        )
        contexte = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        contexte.check_hostname = False
        contexte.verify_mode = ssl.CERT_NONE  # noqa: S501 — certificat auto-signé, relais interne
        try:
            with smtplib.SMTP(self.hote, self.port, timeout=15) as client:
                client.ehlo()
                if client.has_extn("STARTTLS"):
                    client.starttls(context=contexte)
                    client.ehlo()
                client.login(identifiant, mot_de_passe)
                refuses = client.sendmail(message["From"], [destinataire], message.as_bytes())
            if refuses:
                return {"envoye": False, "code": "550", "detail": str(refuses)}
            return {
                "envoye": True,
                "code": "250",
                "detail": f"Relayé réellement via {self.hote}:{self.port}.",
            }
        except smtplib.SMTPRecipientsRefused as exc:
            # Le relais a répondu, il a juste refusé : ce n'est pas une panne d'amont.
            return {"envoye": False, "code": "550", "detail": str(exc.recipients)}
        except smtplib.SMTPResponseException as exc:
            # Bug réel trouvé en testant en direct : un refus explicite du relais (quota
            # journalier dépassé, identifiants invalides — code 552/535...) remontait comme
            # `amont_indisponible` (424, « le relais ne répond pas »), alors que le relais a
            # bien répondu. Le relais a *répondu*, avec un code métier : ce n'est pas une panne
            # d'intégration amont, ne pas le classer comme telle.
            return {
                "envoye": False,
                "code": str(exc.smtp_code),
                "detail": exc.smtp_error.decode(errors="replace")
                if isinstance(exc.smtp_error, bytes)
                else str(exc.smtp_error),
            }
        except (OSError, smtplib.SMTPException) as exc:
            raise erreurs.amont_indisponible(
                "relais_smtp", f"{self.hote}:{self.port} indisponible : {exc}"
            ) from exc


def choisir_relais_smtp() -> RelaisSmtpSimule:
    if os.environ.get(ENV_HOTE):
        return RelaisSmtpReel()
    return RelaisSmtpSimule()
