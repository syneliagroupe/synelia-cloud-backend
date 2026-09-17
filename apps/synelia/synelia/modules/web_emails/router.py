"""Messagerie (Web Cloud emails) : activation, boîtes, alias, authentification, webmail."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, Response, status
from synelia_contract import modeles as m
from synelia_kernel import erreurs
from synelia_kernel.dates import maintenant
from synelia_kernel.ids import nouvel_id

from synelia.audit import journaliser
from synelia.deps import Contexte, Page, exige, exiger_confirmation
from synelia.modules.web_emails import service
from synelia.modules.web_emails.service import depot
from synelia.travaux import demarrer_travail

router = APIRouter(prefix="/web/emails", tags=["Web Cloud — emails"])


def _nouvelle(domaine: str, palier: str) -> m.Messagerie:
    p = service.palier(palier)
    return m.Messagerie(
        id=nouvel_id(),
        domaine=domaine,
        actif=False,
        palier=palier,
        solutionOSS="zimbra",
        hoteWebmail="webmail.synelia.cloud",
        boites=[],
        boitesIncluses=p["boites"],
        alias=[],
        redirections=[],
        authentification=m.Authentification(spf="absent", dkim="absent", dmarc=""),
        antispam=m.Antispam(actif=True, niveau="standard", quarantaine=0),
        prixSiege=p["prixSiege"],
    )


@router.get("", response_model=m.WebEmailsGetResponse, response_model_exclude_none=True)
async def lister_messageries(
    page: Page,
    domaine: str | None = None,
    actif: bool | None = None,
    ctx: Contexte = Depends(exige(None)),
) -> Any:
    return await depot.lister(
        ctx,
        page,
        filtre=lambda e: (
            (not domaine or e.domaine == domaine) and (actif is None or e.actif == actif)
        ),
        tri_defaut="domaine",
    )


@router.post(
    "",
    response_model=m.TravailProvisioning,
    status_code=status.HTTP_202_ACCEPTED,
    response_model_exclude_none=True,
)
async def activer_messagerie(
    corps: m.WebEmailsPostRequest, ctx: Contexte = Depends(exige("marketplace.subscribe"))
) -> Any:
    await depot.exiger_nom_libre(ctx, corps.domaine)
    mess = _nouvelle(corps.domaine, corps.palier)
    await depot.creer(ctx, mess)
    await journaliser(
        ctx,
        action="web.emails.activation",
        cible_type="web_messagerie",
        cible_id=mess.id,
        cible=mess.domaine,
        details=corps.model_dump(mode="json", exclude_none=True),
    )
    return await demarrer_travail(
        ctx,
        "web.email.activate",
        mess.domaine,
        cible_type="web_messagerie",
        cible_id=mess.id,
        entree=corps.model_dump(mode="json"),
    )


@router.get("/{messagerieId}", response_model=m.Messagerie, response_model_exclude_none=True)
async def obtenir_messagerie(messagerieId: str, ctx: Contexte = Depends(exige(None))) -> Any:  # noqa: N803
    return await depot.obtenir(ctx, messagerieId)


@router.patch("/{messagerieId}", response_model=m.Messagerie, response_model_exclude_none=True)
async def modifier_messagerie(
    messagerieId: str,
    corps: m.WebEmailsMessagerieIdPatchRequest,
    ctx: Contexte = Depends(exige("service.admin")),
) -> Any:  # noqa: N803
    await depot.obtenir(ctx, messagerieId)
    modifs = corps.model_dump(mode="json", exclude_unset=True)
    if "antispam" in modifs and modifs["antispam"] is not None:
        modifs["antispam"] = {k: v for k, v in modifs["antispam"].items() if v is not None}
    await depot.modifier(ctx, messagerieId, modifs)
    await journaliser(
        ctx,
        action="web.emails.modification",
        cible_type="web_messagerie",
        cible_id=messagerieId,
        details={k: v for k, v in modifs.items() if v is not None},
    )
    return await depot.obtenir(ctx, messagerieId)


@router.delete(
    "/{messagerieId}",
    response_model=m.TravailProvisioning,
    status_code=status.HTTP_202_ACCEPTED,
    response_model_exclude_none=True,
)
async def supprimer_messagerie(
    messagerieId: str,
    confirmation: str | None = None,
    ctx: Contexte = Depends(exige("marketplace.subscribe")),
) -> Any:  # noqa: N803
    mess = await depot.obtenir(ctx, messagerieId)
    exiger_confirmation(mess.domaine, confirmation)
    await journaliser(
        ctx,
        action="web.emails.suppression",
        cible_type="web_messagerie",
        cible_id=messagerieId,
        cible=mess.domaine,
    )
    return await demarrer_travail(
        ctx,
        "web.email.deactivate",
        mess.domaine,
        cible_type="web_messagerie",
        cible_id=messagerieId,
        etapes=service.ETAPES_SUPPRESSION,
    )


@router.put("/{messagerieId}/alias", response_model=m.Messagerie, response_model_exclude_none=True)
async def modifier_alias_messagerie(
    messagerieId: str,
    corps: m.WebEmailsMessagerieIdAliasPutRequest,
    ctx: Contexte = Depends(exige("service.admin")),
) -> Any:  # noqa: N803
    await depot.obtenir(ctx, messagerieId)
    await depot.modifier(
        ctx, messagerieId, {"alias": corps.alias or [], "redirections": corps.redirections or []}
    )
    await journaliser(
        ctx,
        action="web.emails.alias",
        cible_type="web_messagerie",
        cible_id=messagerieId,
        details=corps.model_dump(mode="json", exclude_none=True),
    )
    return await depot.obtenir(ctx, messagerieId)


@router.post(
    "/{messagerieId}/authentification/verification",
    response_model=m.WebEmailsMessagerieIdAuthentificationVerificationPostResponse,
    response_model_exclude_none=True,
)
async def verifier_authentification_messagerie(
    messagerieId: str, ctx: Contexte = Depends(exige(None))
) -> Any:  # noqa: N803
    mess = await depot.obtenir(ctx, messagerieId)
    resultat = await asyncio.to_thread(service.amont().verifier_authentification, mess.domaine)
    a_creer = (
        [m.EnregistrementDnsCreation(**r) for r in resultat.get("enregistrements", [])]
        if resultat.get("spf") != "valide"
        else None
    )
    await depot.modifier(
        ctx,
        messagerieId,
        {
            "authentification": {
                "spf": resultat.get("spf", "absent"),
                "dkim": resultat.get("dkim", "absent"),
                "dmarc": resultat.get("dmarc", ""),
            }
        },
    )
    await journaliser(
        ctx, action="web.emails.verification", cible_type="web_messagerie", cible_id=messagerieId
    )
    return {
        "spf": resultat.get("spf", "absent"),
        "dkim": resultat.get("dkim", "absent"),
        "dmarc": resultat.get("dmarc", ""),
        "aCreer": a_creer,
    }


@router.post(
    "/{messagerieId}/boites",
    response_model=m.BoiteMail,
    status_code=status.HTTP_201_CREATED,
    response_model_exclude_none=True,
)
async def creer_boite_mail(
    messagerieId: str, corps: m.BoiteMailCreation, ctx: Contexte = Depends(exige("seat.assign"))
) -> Any:  # noqa: N803
    mess = await depot.obtenir(ctx, messagerieId)
    if any(b.adresse == corps.adresse for b in mess.boites):
        raise erreurs.nom_deja_pris(corps.adresse)
    if len(mess.boites) >= mess.boitesIncluses:
        raise erreurs.quota_depasse(
            "Le palier de messagerie ne comprend pas plus de boîtes.",
            detail=f"boitesIncluses={mess.boitesIncluses}",
        )
    boite = m.BoiteMail(
        adresse=corps.adresse,
        nom=corps.nom,
        quotaGo=corps.quotaGo if corps.quotaGo is not None else 10.0,
        utiliseGo=0.0,
        statut="active",
        mfa=bool(corps.mfaObligatoire),
        derniereConnexion=None,
    )
    await asyncio.to_thread(
        service.amont().creer_boite, mess.domaine, corps.adresse, corps.motDePasse
    )
    boites = [*mess.boites, boite]
    await depot.modifier(ctx, messagerieId, {"boites": [b.model_dump(mode="json") for b in boites]})
    await journaliser(
        ctx,
        action="web.emails.boite.creation",
        cible_type="web_messagerie",
        cible_id=mess.id,
        cible=corps.adresse,
    )
    return boite


@router.patch(
    "/{messagerieId}/boites/{adresse}", response_model=m.BoiteMail, response_model_exclude_none=True
)
async def modifier_boite_mail(
    messagerieId: str,
    adresse: str,
    corps: m.WebEmailsMessagerieIdBoitesAdressePatchRequest,
    ctx: Contexte = Depends(exige("seat.assign")),
) -> Any:  # noqa: N803
    mess = await depot.obtenir(ctx, messagerieId)
    boite = next((b for b in mess.boites if b.adresse == adresse), None)
    if boite is None:
        raise erreurs.introuvable("Boîte mail", adresse)
    changement = corps.model_dump(mode="json", exclude_unset=True)
    changement = {k: v for k, v in changement.items() if v is not None}
    # `motDePasse` ne fait pas partie du modèle `BoiteMail` (le portail ne le stocke jamais) :
    # il part vers Zimbra en direct, pas dans le dépôt applicatif.
    mot_de_passe = changement.pop("motDePasse", None)
    if mot_de_passe:
        await asyncio.to_thread(
            service.amont().definir_mot_de_passe, mess.domaine, adresse, mot_de_passe
        )
    nouvelle = boite.model_copy(update=changement)
    boites = [nouvelle if b.adresse == adresse else b for b in mess.boites]
    await depot.modifier(ctx, messagerieId, {"boites": [b.model_dump(mode="json") for b in boites]})
    await journaliser(
        ctx,
        action="web.emails.boite.modification",
        cible_type="web_messagerie",
        cible_id=mess.id,
        cible=adresse,
        details=changement,
    )
    return nouvelle


@router.delete(
    "/{messagerieId}/boites/{adresse}", status_code=status.HTTP_204_NO_CONTENT, response_model=None
)
async def supprimer_boite_mail(
    messagerieId: str,
    adresse: str,
    confirmation: str | None = None,
    ctx: Contexte = Depends(exige("seat.assign")),
) -> Any:  # noqa: N803
    mess = await depot.obtenir(ctx, messagerieId)
    exiger_confirmation(adresse, confirmation)
    boite = next((b for b in mess.boites if b.adresse == adresse), None)
    if boite is None:
        raise erreurs.introuvable("Boîte mail", adresse)
    await asyncio.to_thread(service.amont().supprimer_boite, mess.domaine, adresse)
    boites = [b for b in mess.boites if b.adresse != adresse]
    await depot.modifier(ctx, messagerieId, {"boites": [b.model_dump(mode="json") for b in boites]})
    await journaliser(
        ctx,
        action="web.emails.boite.suppression",
        cible_type="web_messagerie",
        cible_id=mess.id,
        cible=adresse,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{messagerieId}/ouverture",
    response_model=m.OuvertureService,
    status_code=status.HTTP_201_CREATED,
    response_model_exclude_none=True,
)
async def ouvrir_webmail(
    messagerieId: str,
    corps: m.WebEmailsMessagerieIdOuverturePostRequest,
    ctx: Contexte = Depends(exige("service.open")),
) -> Any:  # noqa: N803
    mess = await depot.obtenir(ctx, messagerieId)
    url = await asyncio.to_thread(service.amont().ouvrir_webmail, corps.adresse)
    await journaliser(
        ctx,
        action="web.emails.ouverture",
        cible_type="web_messagerie",
        cible_id=mess.id,
        cible=corps.adresse or mess.domaine,
    )
    return m.OuvertureService(
        url=url, expire=maintenant() + timedelta(seconds=60), methode="redirection"
    )
