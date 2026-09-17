"""Règles relais SMTP (Web Cloud) : relais par org, clés, webhooks, exécuteur."""

from __future__ import annotations

from synelia_contract import modeles as m
from synelia_db.modeles import Travail
from synelia_kernel.ids import jeton_opaque
from synelia_openstack import relais_smtp

from synelia.depot import Depot
from synelia.deps.contexte import Contexte
from synelia.travaux import Executeur, executeur

depot = Depot(
    "smtp_relais",
    m.RelaisSmtp,
    libelle="Relais SMTP",
    champ_nom="identifiant",
    champ_statut="actif",
)
depot_cle = Depot(
    "smtp_cle",
    m.CleSmtp,
    libelle="Clé SMTP",
    champ_nom="nom",
    champ_statut="statut",
    champs_recherche=("nom", "identifiant"),
)
depot_message = Depot(
    "smtp_message",
    m.MessageSmtp,
    libelle="Message SMTP",
    champ_nom="id",
    champ_statut="statut",
    champs_recherche=("de", "vers", "sujet"),
)
depot_webhook = Depot(
    "smtp_webhook", m.WebhookSmtp, libelle="Webhook SMTP", champ_nom="url", champ_statut="actif"
)

HOTE = "smtp.synelia.cloud"
PORTS = [587]


def amont() -> relais_smtp.RelaisSmtpSimule:
    return relais_smtp.choisir_relais_smtp()


@executeur("smtp.activate")
class ExecuteurSmtpActivate(Executeur):
    async def terminer(self, ctx: Contexte, travail: Travail) -> None:
        # L'identifiant et le secret posés ici sont la vraie backing du relais : le process
        # `synelia relais-smtp` (apps/synelia/synelia/relais_smtp.py) lit ces mêmes lignes
        # `smtp_relais` (identifiant + secret chiffré `mot_de_passe`) pour authentifier les
        # connexions SMTP réelles et appliquer le quota — aucune valeur simulée en aval.
        relais = await depot.obtenir(ctx, travail.cible_id or "")
        identifiant = f"smtp@{travail.org_id or ctx.org_id_ou_none or 'org'}"
        await depot.modifier(
            ctx,
            relais.id,
            {
                "actif": True,
                "hote": HOTE,
                "ports": PORTS,
                "identifiant": identifiant,
                "authentification": {"spf": "valide", "dkim": "valide", "dmarc": "valide"},
                "quota": {**relais.quota.model_dump(), "utiliseJour": 0},
            },
        )
        await depot.definir_secrets(ctx, relais.id, {"mot_de_passe": jeton_opaque(18)})


ETAPES_ACTIVATION = [
    {"nom": "Provisionner le relais d'envoi", "dureeS": 30},
    {"nom": "Poser SPF, DKIM et DMARC", "dureeS": 15},
    {"nom": "Lever le mode d'essai", "dureeS": 5},
]
