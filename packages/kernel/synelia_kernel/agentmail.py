"""Envoi transactionnel via AgentMail (codes de vérification d'email) : clé API +
inbox en variables d'environnement, jamais en dur. Le flux appelant garde toujours
son repli (`courriel` via SMTP admin) : une panne AgentMail ne doit jamais bloquer
une inscription."""

from __future__ import annotations

import asyncio
import json
import os
import urllib.request

ENV_CLE = "AGENTMAIL_API_KEY"
ENV_INBOX = "AGENTMAIL_INBOX_ID"
BASE = "https://api.agentmail.to/v0"


def configure() -> bool:
    return bool(os.environ.get(ENV_CLE) and os.environ.get(ENV_INBOX))


def _envoyer_bloquant(destinataire: str, sujet: str, texte: str) -> str:
    cle = os.environ[ENV_CLE]
    inbox = os.environ[ENV_INBOX]
    corps = json.dumps({"to": [destinataire], "subject": sujet, "text": texte}).encode()
    requete = urllib.request.Request(  # noqa: S310 — URL https figée (constante BASE)
        f"{BASE}/inboxes/{inbox}/messages/send",
        data=corps,
        headers={"Authorization": f"Bearer {cle}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(requete, timeout=20) as reponse:  # noqa: S310 — idem
        donnees = json.loads(reponse.read().decode())
    return str(donnees.get("message_id") or "")


async def envoyer_code(destinataire: str, code: str) -> str:
    """Envoie le code de vérification ; lève en cas d'échec (l'appelant bascule sur
    le repli SMTP). Ne rien faire si non configuré (lève aussi — l'appelant décide)."""
    if not configure():
        raise RuntimeError("AgentMail non configuré (AGENTMAIL_API_KEY/INBOX_ID).")
    texte = (
        f"Votre code de vérification Synelia Cloud : {code}\n\n"
        "Ce code expire dans 15 minutes. Si vous n'êtes pas à l'origine de cette "
        "demande, ignorez cet e-mail."
    )
    return await asyncio.to_thread(
        _envoyer_bloquant, destinataire, "Synelia Cloud — vérifiez votre email", texte
    )
