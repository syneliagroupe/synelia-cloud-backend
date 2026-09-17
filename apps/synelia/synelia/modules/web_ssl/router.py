"""SSL (Web Cloud) : offres, commande, validation, renouvellement."""

from __future__ import annotations

import asyncio
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, Response, status
from synelia_contract import modeles as m
from synelia_kernel import erreurs
from synelia_kernel.ids import nouvel_id

from synelia.audit import journaliser
from synelia.deps import Contexte, Page, exige, exiger_confirmation
from synelia.modules.web_ssl import service
from synelia.modules.web_ssl.service import depot, offre
from synelia.travaux import demarrer_travail

router = APIRouter(prefix="/web/ssl", tags=["Web Cloud — SSL"])


@router.get("/offres", response_model=m.WebSslOffresGetResponse, response_model_exclude_none=True)
async def lister_offres_certificat(ctx: Contexte = Depends(exige(None))) -> Any:
    return [m.WebSslOffresGetResponseItem(**o) for o in service.OFFRES]


@router.get("", response_model=m.WebSslGetResponse, response_model_exclude_none=True)
async def lister_certificats(
    page: Page,
    hebergementId: str | None = None,
    etat: str | None = None,
    type: str | None = None,
    expireAvant: str | None = None,
    ctx: Contexte = Depends(exige(None)),
) -> Any:  # noqa: A002, PLR0917
    def _filtre(c: m.Certificat) -> bool:
        if hebergementId and c.hebergementId != hebergementId:
            return False
        if etat and c.etat != etat:
            return False
        if type and c.type != type:
            return False
        if expireAvant and hasattr(c, "expire") and c.expire:
            if c.expire.isoformat() > expireAvant:
                return False
        return True

    return await depot.lister(ctx, page, filtre=_filtre, tri_defaut="hote")


@router.post(
    "",
    response_model=m.TravailProvisioning,
    status_code=status.HTTP_202_ACCEPTED,
    response_model_exclude_none=True,
)
async def commander_certificat(
    corps: m.CertificatCommande, ctx: Contexte = Depends(exige("marketplace.subscribe"))
) -> Any:
    await depot.exiger_nom_libre(ctx, corps.hote)
    o = offre(corps.type)
    duree = service.duree_jours(corps.type, corps.dureeAnnees)
    cert = m.Certificat(
        id=nouvel_id(),
        hote=corps.hote,
        hotesSupplementaires=corps.hotesSupplementaires,
        type=corps.type,
        emetteur=o["emetteur"],
        emisLe=date.today(),
        expire=service.nova_expiration(corps.type, corps.dureeAnnees),
        renouvellementAuto=True if corps.renouvellementAuto is None else corps.renouvellementAuto,
        prixAnnuel=o["prixAnnuel"],
        etat="en_emission",
        hebergementId=corps.hebergementId,
        algorithme="RSA-2048",
        validationDomaine=corps.validationDomaine,
    )
    await depot.creer(ctx, cert)
    await journaliser(
        ctx,
        action="web.ssl.commande",
        cible_type="web_certificat",
        cible_id=cert.id,
        cible=cert.hote,
        details=corps.model_dump(mode="json", exclude_none=True),
    )
    return await demarrer_travail(
        ctx,
        "web.ssl.commande",
        cert.hote,
        cible_type="web_certificat",
        cible_id=cert.id,
        entree=corps.model_dump(mode="json"),
        etapes=service.ETAPES_COMMANDE,
        contexte={"duree_jours": duree},
    )


@router.get("/{certificatId}", response_model=m.Certificat, response_model_exclude_none=True)
async def obtenir_certificat(certificatId: str, ctx: Contexte = Depends(exige(None))) -> Any:  # noqa: N803
    return await depot.obtenir(ctx, certificatId)


@router.patch("/{certificatId}", response_model=m.Certificat, response_model_exclude_none=True)
async def modifier_certificat(
    certificatId: str,
    corps: m.WebSslCertificatIdPatchRequest,
    ctx: Contexte = Depends(exige("service.admin")),
) -> Any:  # noqa: N803
    await depot.obtenir(ctx, certificatId)
    modifs = {
        k: v for k, v in corps.model_dump(mode="json", exclude_unset=True).items() if v is not None
    }
    await depot.modifier(ctx, certificatId, modifs)
    await journaliser(
        ctx,
        action="web.ssl.modification",
        cible_type="web_certificat",
        cible_id=certificatId,
        details=modifs,
    )
    return await depot.obtenir(ctx, certificatId)


@router.delete("/{certificatId}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def revoquer_certificat(
    certificatId: str,
    confirmation: str | None = None,
    ctx: Contexte = Depends(exige("service.admin")),
) -> Any:  # noqa: N803
    cert = await depot.obtenir(ctx, certificatId)
    exiger_confirmation(cert.hote, confirmation)
    await asyncio.to_thread(service.amont().revoquer, cert.hote)
    await depot.modifier(ctx, certificatId, {"etat": "revoque"})
    await journaliser(
        ctx,
        action="web.ssl.revocation",
        cible_type="web_certificat",
        cible_id=cert.id,
        cible=cert.hote,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{certificatId}/renouvellement",
    response_model=m.TravailProvisioning,
    status_code=status.HTTP_202_ACCEPTED,
    response_model_exclude_none=True,
)
async def renouveler_certificat(
    certificatId: str,
    corps: m.WebSslCertificatIdRenouvellementPostRequest,
    ctx: Contexte = Depends(exige("service.admin")),
) -> Any:  # noqa: N803
    cert = await depot.obtenir(ctx, certificatId)
    duree = service.duree_jours(cert.type, corps.dureeAnnees)
    await depot.modifier(ctx, certificatId, {"etat": "en_emission"})
    await journaliser(
        ctx,
        action="web.ssl.renouvellement",
        cible_type="web_certificat",
        cible_id=cert.id,
        cible=cert.hote,
        details=corps.model_dump(mode="json", exclude_none=True),
    )
    return await demarrer_travail(
        ctx,
        "web.ssl.renew",
        cert.hote,
        cible_type="web_certificat",
        cible_id=cert.id,
        contexte={"duree_jours": duree},
    )


@router.post(
    "/{certificatId}/validation",
    response_model=m.WebSslCertificatIdValidationPostResponse,
    response_model_exclude_none=True,
)
async def relancer_validation_certificat(
    certificatId: str, ctx: Contexte = Depends(exige("service.admin"))
) -> Any:  # noqa: N803
    cert = await depot.obtenir(ctx, certificatId)
    if cert.etat == "actif":
        raise erreurs.conflit("Ce certificat est déjà valide.", code="certificat_deja_valide")
    resultat = await asyncio.to_thread(service.amont().valider, cert.hote)
    enregistrement = m.EnregistrementDnsCreation(
        type="TXT", nom=f"_acme-challenge.{cert.hote}", valeur=f"tok-{nouvel_id()[:8]}"
    )
    await journaliser(
        ctx,
        action="web.ssl.validation",
        cible_type="web_certificat",
        cible_id=cert.id,
        cible=cert.hote,
    )
    return {
        "etat": resultat.get("etat", "ok"),
        "methode": cert.validationDomaine,
        "enregistrement": enregistrement,
        "detail": resultat.get("detail"),
    }
