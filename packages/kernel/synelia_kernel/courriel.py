"""Courriels transactionnels (réinitialisation de mot de passe, invitations, MFA) : envoyés
via le compte SMTP administratif réel (`SYNELIA_SMTP_ADMIN_*`), pas le relais SMTP produit
(`relais_smtp.py`, qui sert les organisations clientes). Sans configuration, l'envoi est un
no-op silencieux et l'appelant garde son repli de journalisation dev existant.

Le corps est un HTML habillé à la charte (mêmes teintes que le portail), inliné via
`premailer` — les clients de messagerie ignorent les feuilles de style externes et beaucoup
n'aiment pas non plus un `<style>` non inliné."""

from __future__ import annotations

import asyncio
import os
import smtplib
import ssl
from email.message import EmailMessage

from jinja2 import Template
from premailer import transform

ENV_HOTE = "SYNELIA_SMTP_ADMIN_HOTE"
ENV_UTILISATEUR = "SYNELIA_SMTP_ADMIN_UTILISATEUR"
ENV_MOT_DE_PASSE = "SYNELIA_SMTP_ADMIN_MOT_DE_PASSE"

# Mêmes teintes que `src/app/globals.css` du portail : violet (p-700/p-600), magenta
# réservé au bouton d'action (m-600, même usage que le bouton « Ouvrir » d'un service
# managé), encre (ink) pour le texte, gris (g-500/g-100) pour le secondaire et les fonds.
_GABARIT = Template(
    """<!doctype html>
<html lang="fr">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"></head>
<style>
  body { margin: 0; padding: 0; background: #efeff4; font-family: 'Open Sans', Arial, sans-serif; }
  .enveloppe { max-width: 480px; margin: 0 auto; padding: 32px 16px; }
  .carte { background: #ffffff; border-radius: 10px; overflow: hidden; border: 1px solid #c8c8d4; }
  .entete { background: #4b2882; padding: 24px 32px; }
  .marque { font-family: 'Montserrat', Arial, sans-serif; font-weight: 700; font-size: 18px; color: #ffffff; letter-spacing: 0.02em; }
  .corps { padding: 32px; color: #2b1b4d; font-size: 14px; line-height: 1.6; }
  .titre { font-family: 'Montserrat', Arial, sans-serif; font-weight: 700; font-size: 20px; color: #2b1b4d; margin: 0 0 16px; }
  .bouton { display: inline-block; background: #c0297a; color: #ffffff !important; text-decoration: none; font-weight: 600; font-size: 14px; padding: 12px 24px; border-radius: 8px; margin: 16px 0; }
  .pied { padding: 20px 32px; color: #67677c; font-size: 12px; line-height: 1.5; border-top: 1px solid #efeff4; }
  .lien-secours { word-break: break-all; color: #6b3fa0; font-size: 12px; }
</style>
<body>
  <div class="enveloppe">
    <div class="carte">
      <div class="entete"><span class="marque">Synelia Cloud</span></div>
      <div class="corps">
        <p class="titre">{{ titre }}</p>
        {% for p in paragraphes %}<p>{{ p }}</p>{% endfor %}
        {% if bouton_url %}
        <p style="text-align:center;"><a class="bouton" href="{{ bouton_url }}">{{ bouton_texte }}</a></p>
        <p class="lien-secours">Le bouton ne fonctionne pas ? Copiez ce lien : {{ bouton_url }}</p>
        {% endif %}
      </div>
      <div class="pied">
        Cet e-mail vous est envoyé par Synelia Cloud. Si vous n'êtes pas à l'origine de cette
        demande, vous pouvez l'ignorer sans risque.
      </div>
    </div>
  </div>
</body>
</html>"""
)


def configure() -> bool:
    return bool(os.environ.get(ENV_HOTE) and os.environ.get(ENV_UTILISATEUR))


def _rendre_html(titre: str, paragraphes: list[str], bouton_texte: str, bouton_url: str) -> str:
    brut = _GABARIT.render(
        titre=titre, paragraphes=paragraphes, bouton_texte=bouton_texte, bouton_url=bouton_url
    )
    return str(transform(brut))


def _envoyer_bloquant(destinataire: str, sujet: str, texte: str, html: str) -> None:
    hote = os.environ[ENV_HOTE]
    utilisateur = os.environ[ENV_UTILISATEUR]
    mot_de_passe = os.environ.get(ENV_MOT_DE_PASSE, "")
    msg = EmailMessage()
    msg["Subject"] = sujet
    msg["From"] = f"Synelia Cloud <{utilisateur}>"
    msg["To"] = destinataire
    msg.set_content(texte)
    msg.add_alternative(html, subtype="html")
    with smtplib.SMTP(hote, 587, timeout=15) as client:
        client.ehlo()
        if client.has_extn("STARTTLS"):
            client.starttls(context=ssl.create_default_context())
            client.ehlo()
        if mot_de_passe:
            client.login(utilisateur, mot_de_passe)
        client.send_message(msg)


async def envoyer(
    destinataire: str,
    sujet: str,
    titre: str,
    paragraphes: list[str],
    *,
    bouton_texte: str = "",
    bouton_url: str = "",
) -> None:
    """Best-effort : une panne d'envoi ne doit jamais faire échouer le flux appelant
    (mot de passe oublié, invitation) — seulement être journalisée en amont_indisponible
    si l'appelant choisit de la relever."""
    if not configure():
        return
    texte = "\n\n".join([titre, *paragraphes, bouton_url] if bouton_url else [titre, *paragraphes])
    html = _rendre_html(titre, paragraphes, bouton_texte, bouton_url)
    await asyncio.to_thread(_envoyer_bloquant, destinataire, sujet, texte, html)
