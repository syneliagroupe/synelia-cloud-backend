"""Relais SMTP (Web Cloud) : relais, clés, identifiants, messages, test, webhooks."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Response, status
from synelia_contract import modeles as m
from synelia_kernel import erreurs
from synelia_kernel.dates import maintenant
from synelia_kernel.ids import jeton_opaque, nouvel_id

from synelia.audit import journaliser
from synelia.deps import Contexte, Page, exige, exiger_confirmation
from synelia.modules.web_smtp import service
from synelia.modules.web_smtp.service import depot, depot_cle, depot_message, depot_webhook
from synelia.travaux import demarrer_travail

router = APIRouter(prefix="/web/smtp", tags=["Web Cloud — relais SMTP"])


def _relais_defaut(ctx: Contexte) -> m.RelaisSmtp:
    return m.RelaisSmtp(
        id=nouvel_id(),
        orgId=ctx.org_id,
        hote="",
        ports=[],
        identifiant="",
        domainesAutorises=[],
        authentification=m.Authentification1(spf="absent", dkim="absent", dmarc=""),
        quota=m.Quota2(parJour=0, parHeure=0, utiliseJour=0),
        reputation=m.Reputation(tauxRemise=0.0, tauxRebond=0.0, plaintes=0.0, listeNoire=False),
        ipDediee=None,
        actif=False,
    )


async def _relais(ctx: Contexte) -> m.RelaisSmtp:
    liste = await depot.tous(ctx)
    return liste[0] if liste else _relais_defaut(ctx)


@router.get("", response_model=m.RelaisSmtp, response_model_exclude_none=True)
async def obtenir_relais_smtp(ctx: Contexte = Depends(exige(None))) -> Any:
    return await _relais(ctx)


@router.post(
    "",
    response_model=m.TravailProvisioning,
    status_code=status.HTTP_202_ACCEPTED,
    response_model_exclude_none=True,
)
async def activer_relais_smtp(
    corps: m.WebSmtpPostRequest, ctx: Contexte = Depends(exige("marketplace.subscribe"))
) -> Any:
    if await depot.tous(ctx):
        raise erreurs.conflit(
            "Un relais SMTP est déjà actif pour cette organisation.", code="relais_deja_actif"
        )
    relais = m.RelaisSmtp(
        id=nouvel_id(),
        orgId=ctx.org_id,
        hote="",
        ports=[],
        identifiant="",
        domainesAutorises=corps.domainesAutorises,
        authentification=m.Authentification1(spf="absent", dkim="absent", dmarc=""),
        quota=m.Quota2(
            parJour=corps.quotaJour or 1000,
            parHeure=max(100, (corps.quotaJour or 1000) // 24),
            utiliseJour=0,
        ),
        reputation=m.Reputation(tauxRemise=0.0, tauxRebond=0.0, plaintes=0.0, listeNoire=False),
        ipDediee="ip.synelia.cloud" if corps.ipDediee else None,
        actif=False,
    )
    await depot.creer(ctx, relais)
    await journaliser(
        ctx,
        action="smtp.activation",
        cible_type="smtp_relais",
        cible_id=relais.id,
        details=corps.model_dump(mode="json"),
    )
    return await demarrer_travail(
        ctx,
        "smtp.activate",
        "relais SMTP",
        cible_type="smtp_relais",
        cible_id=relais.id,
        entree=corps.model_dump(mode="json"),
        etapes=service.ETAPES_ACTIVATION,
    )


@router.patch("", response_model=m.RelaisSmtp, response_model_exclude_none=True)
async def modifier_relais_smtp(
    corps: m.WebSmtpPatchRequest, ctx: Contexte = Depends(exige("service.admin"))
) -> Any:
    relais = await _relais(ctx)
    if relais.actif is False and corps.actif is not False:
        raise erreurs.introuvable("Relais SMTP", relais.id)
    modifs = {
        k: v for k, v in corps.model_dump(mode="json", exclude_unset=True).items() if v is not None
    }
    # Bug réel trouvé en vérifiant en direct : `quotaJour` est un champ plat du contrat
    # (`WebSmtpPatchRequest`) mais `RelaisSmtp.quota` est un objet imbriqué — `Depot.modifier`
    # ne fait qu'une fusion superficielle, donc la clé `quotaJour` atterrissait à côté de
    # `quota` sans jamais le modifier. Le `PATCH` répondait `200` avec le nouveau quota, mais
    # `GET /web/smtp` (et le relais réel, qui applique `quota.parJour`) continuaient de
    # montrer l'ancien : un réglage qui ne se réglait pas, silencieusement.
    if "quotaJour" in modifs:
        quota_jour = modifs.pop("quotaJour")
        modifs["quota"] = {
            **relais.quota.model_dump(),
            "parJour": quota_jour,
            "parHeure": max(100, quota_jour // 24),
        }
    await depot.modifier(ctx, relais.id, modifs)
    await journaliser(
        ctx,
        action="smtp.modification",
        cible_type="smtp_relais",
        cible_id=relais.id,
        details=modifs,
    )
    return await depot.obtenir(ctx, relais.id)


# ── clés ────────────────────────────────────────────────────────────────
@router.get("/cles", response_model=list[m.CleSmtp], response_model_exclude_none=True)
async def lister_cles_smtp(statut: str | None = None, ctx: Contexte = Depends(exige(None))) -> Any:
    return await depot_cle.tous(ctx, filtre=(lambda c: c.statut == statut) if statut else None)


@router.post(
    "/cles",
    response_model=m.CleSmtpSecret,
    status_code=status.HTTP_201_CREATED,
    response_model_exclude_none=True,
)
async def creer_cle_smtp(
    corps: m.CleSmtpCreation, ctx: Contexte = Depends(exige("secrets.update"))
) -> Any:
    cle = m.CleSmtp(
        id=nouvel_id(),
        nom=corps.nom,
        identifiant=f"cle-{jeton_opaque(10)}",
        domainesAutorises=corps.domainesAutorises,
        quotaJour=corps.quotaJour,
        utiliseJour=0,
        creeeLe=maintenant(),
        derniereUtilisation=None,
        statut="active",
    )
    mot_de_passe = service.amont().creer_cle()
    await depot_cle.creer(ctx, cle, secrets={"mot_de_passe": mot_de_passe})
    await journaliser(
        ctx, action="smtp.cle.creation", cible_type="smtp_cle", cible_id=cle.id, cible=cle.nom
    )
    return m.CleSmtpSecret(cle=cle, hote=service.HOTE, ports=service.PORTS, motDePasse=mot_de_passe)


@router.patch("/cles/{cleSmtpId}", response_model=m.CleSmtp, response_model_exclude_none=True)
async def modifier_cle_smtp(
    cleSmtpId: str,
    corps: m.WebSmtpClesCleSmtpIdPatchRequest,
    ctx: Contexte = Depends(exige("secrets.update")),
) -> Any:  # noqa: N803
    await depot_cle.obtenir(ctx, cleSmtpId)
    modifs = {
        k: v for k, v in corps.model_dump(mode="json", exclude_unset=True).items() if v is not None
    }
    await depot_cle.modifier(ctx, cleSmtpId, modifs)
    await journaliser(
        ctx,
        action="smtp.cle.modification",
        cible_type="smtp_cle",
        cible_id=cleSmtpId,
        details=modifs,
    )
    return await depot_cle.obtenir(ctx, cleSmtpId)


@router.delete("/cles/{cleSmtpId}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def revoquer_cle_smtp(
    cleSmtpId: str,
    confirmation: str | None = None,
    ctx: Contexte = Depends(exige("secrets.update")),
) -> Any:  # noqa: N803
    cle = await depot_cle.obtenir(ctx, cleSmtpId)
    exiger_confirmation(cle.nom, confirmation)
    await depot_cle.modifier(ctx, cleSmtpId, {"statut": "revoquee"})
    await journaliser(
        ctx, action="smtp.cle.revocation", cible_type="smtp_cle", cible_id=cle.id, cible=cle.nom
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ── identifiants ────────────────────────────────────────────────────────
@router.post(
    "/identifiants",
    response_model=m.WebSmtpIdentifiantsPostResponse,
    response_model_exclude_none=True,
)
async def regenerer_identifiants_smtp(
    corps: m.WebSmtpIdentifiantsPostRequest, ctx: Contexte = Depends(exige("secrets.update"))
) -> Any:
    relais = await _relais(ctx)
    if not relais.actif:
        raise erreurs.introuvable("Relais SMTP", relais.id)
    exiger_confirmation("regenerer", corps.confirmation)
    mot_de_passe = service.amont().regenerer_identifiants()
    await depot.definir_secrets(ctx, relais.id, {"mot_de_passe": mot_de_passe})
    await journaliser(
        ctx, action="smtp.identifiants.regeneration", cible_type="smtp_relais", cible_id=relais.id
    )
    return m.WebSmtpIdentifiantsPostResponse(
        hote=service.HOTE,
        ports=service.PORTS,
        identifiant=relais.identifiant,
        motDePasse=mot_de_passe,
    )


# ── messages & test ─────────────────────────────────────────────────────
@router.get(
    "/messages", response_model=m.WebSmtpMessagesGetResponse, response_model_exclude_none=True
)
async def lister_messages_smtp(
    page: Page,
    statut: str | None = None,
    depuis: str | None = None,
    destinataire: str | None = None,
    ctx: Contexte = Depends(exige(None)),
) -> Any:
    def _filtre(msg: m.MessageSmtp) -> bool:
        if statut and msg.statut != statut:
            return False
        if destinataire and destinataire not in msg.vers:
            return False
        return True

    return await depot_message.lister(ctx, page, filtre=_filtre, tri_defaut="ts")


@router.post("/test", response_model=m.WebSmtpTestPostResponse, response_model_exclude_none=True)
async def tester_relais_smtp(
    corps: m.WebSmtpTestPostRequest, ctx: Contexte = Depends(exige("service.admin"))
) -> Any:
    # En mode réel (RelaisSmtpReel), le test s'authentifie contre le vrai relais SMTP
    # (apps/synelia/synelia/relais_smtp.py) avec les identifiants réels de l'org, posés par
    # `ExecuteurSmtpActivate` — mêmes identifiant/secret que ceux relus par le daemon.
    relais = await _relais(ctx)
    identifiant = mot_de_passe = None
    if relais.actif and relais.identifiant:
        secrets = await depot.secrets(ctx, relais.id)
        identifiant = relais.identifiant
        mot_de_passe = secrets.get("mot_de_passe")
    resultat = service.amont().envoyer_test(
        corps.de or "", corps.destinataire, identifiant=identifiant, mot_de_passe=mot_de_passe
    )
    await journaliser(ctx, action="smtp.test", cible_type="smtp_relais", cible_id=ctx.org_id)
    return {
        "envoye": resultat.get("envoye", True),
        "code": resultat.get("code"),
        "detail": resultat.get("detail"),
        "correlationId": ctx.correlation_id,
    }


# ── webhooks ────────────────────────────────────────────────────────────
@router.get("/webhooks", response_model=list[m.WebhookSmtp], response_model_exclude_none=True)
async def lister_webhooks_smtp(ctx: Contexte = Depends(exige(None))) -> Any:
    return await depot_webhook.tous(ctx)


@router.post(
    "/webhooks",
    response_model=m.WebhookSmtp,
    status_code=status.HTTP_201_CREATED,
    response_model_exclude_none=True,
)
async def creer_webhook_smtp(
    corps: m.WebhookSmtpCreation, ctx: Contexte = Depends(exige("service.admin"))
) -> Any:
    webhook = m.WebhookSmtp(
        id=nouvel_id(),
        url=corps.url,
        evenements=corps.evenements,
        actif=True if corps.actif is None else corps.actif,
        secretDefini=bool(corps.secret),
        dernierEnvoi=None,
        dernierCode=None,
        echecsConsecutifs=0,
    )
    secrets = {"secret": corps.secret} if corps.secret else None
    await depot_webhook.creer(ctx, webhook, secrets=secrets)
    await journaliser(
        ctx,
        action="smtp.webhook.creation",
        cible_type="smtp_webhook",
        cible_id=webhook.id,
        cible=webhook.url,
    )
    return webhook


@router.patch(
    "/webhooks/{webhookId}", response_model=m.WebhookSmtp, response_model_exclude_none=True
)
async def modifier_webhook_smtp(
    webhookId: str, corps: m.WebhookSmtpCreation, ctx: Contexte = Depends(exige("service.admin"))
) -> Any:  # noqa: N803
    await depot_webhook.obtenir(ctx, webhookId)
    modifs = {
        k: v for k, v in corps.model_dump(mode="json", exclude_unset=True).items() if v is not None
    }
    if "secret" in modifs and modifs["secret"] is not None:
        secrets = {"secret": modifs.pop("secret")}
        await depot_webhook.definir_secrets(ctx, webhookId, secrets)
        modifs["secretDefini"] = True
    await depot_webhook.modifier(ctx, webhookId, modifs)
    await journaliser(
        ctx,
        action="smtp.webhook.modification",
        cible_type="smtp_webhook",
        cible_id=webhookId,
        details={k: v for k, v in modifs.items() if v is not None},
    )
    return await depot_webhook.obtenir(ctx, webhookId)


@router.delete("/webhooks/{webhookId}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def supprimer_webhook_smtp(
    webhookId: str, ctx: Contexte = Depends(exige("service.admin"))
) -> Any:  # noqa: N803
    webhook = await depot_webhook.obtenir(ctx, webhookId)
    await depot_webhook.supprimer(ctx, webhookId, logique=False)
    await journaliser(
        ctx,
        action="smtp.webhook.suppression",
        cible_type="smtp_webhook",
        cible_id=webhook.id,
        cible=webhook.url,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/webhooks/{webhookId}/test",
    response_model=m.WebSmtpWebhooksWebhookIdTestPostResponse,
    response_model_exclude_none=True,
)
async def tester_webhook_smtp(
    webhookId: str, ctx: Contexte = Depends(exige("service.admin"))
) -> Any:  # noqa: N803
    webhook = await depot_webhook.obtenir(ctx, webhookId)
    await depot_webhook.modifier(
        ctx,
        webhookId,
        {
            "dernierEnvoi": maintenant(),
            "dernierCode": 200,
            "echecsConsecutifs": 0,
        },
    )
    await journaliser(
        ctx,
        action="smtp.webhook.test",
        cible_type="smtp_webhook",
        cible_id=webhook.id,
        cible=webhook.url,
    )
    return m.WebSmtpWebhooksWebhookIdTestPostResponse(envoye=True, code=200)
