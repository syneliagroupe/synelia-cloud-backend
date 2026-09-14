"""Row de la facturation : estimation, consommation, factures, paiement, prépayé, SLA, souscriptions, devis."""

from __future__ import annotations

import json
import os
import re
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, Request, Response, status
from synelia_contract import modeles as m
from synelia_db.modeles import Utilisateur
from synelia_kernel import courriel, erreurs
from synelia_kernel.dates import maintenant
from synelia_kernel.ids import nouvel_id

from synelia.audit import journaliser
from synelia.depot import Depot
from synelia.deps import Page, exige
from synelia.deps.contexte import Contexte
from synelia.modules.facturation import metrologie, paystack, service, tarification
from synelia.modules.facturation.service import crediter
from synelia.travaux import demarrer_travail

router = APIRouter(prefix="/facturation", tags=["Facturation"])

_RE_PERIODE = re.compile(r"^[0-9]{4}-(0[1-9]|1[0-2])$")


@router.get("/consommation", response_model=m.Consommation, response_model_exclude_none=True)
async def obtenir_consommation(
    periode: str | None = None, ctx: Contexte = Depends(exige("invoice.view", lecture=True))
) -> Any:
    return await metrologie.consommation(ctx, periode or maintenant().strftime("%Y-%m"))


@router.post(
    "/consommation/export",
    response_model=m.FacturationConsommationExportPostResponse,
    status_code=status.HTTP_202_ACCEPTED,
    response_model_exclude_none=True,
)
async def exporter_consommation(
    corps: m.FacturationConsommationExportPostRequest,
    ctx: Contexte = Depends(exige("invoice.view", lecture=True)),
) -> Any:
    if not _RE_PERIODE.match(corps.periode or ""):
        raise erreurs.validation(
            "Periode invalide, attendu AAAA-MM.", {"periode": "Format attendu : AAAA-MM."}
        )
    await metrologie.consommation(ctx, corps.periode)
    travail = await demarrer_travail(
        ctx,
        "facturation.export",
        f"Export {corps.format} {corps.periode}",
        entree=corps.model_dump(mode="json"),
        etapes=[
            {"nom": "Générer le fichier", "dureeS": 3},
            {"nom": "Publier l'URL de téléchargement", "dureeS": 2},
        ],
    )
    await journaliser(
        ctx,
        action="facturation.export",
        cible_type="travail",
        cible_id=travail["id"],
        details={"format": corps.format, "periode": corps.periode},
    )
    return {"url": f"/v1/travaux/{travail['id']}/export", "expire": None}


@router.get(
    "/devis", response_model=m.FacturationDevisGetResponse, response_model_exclude_none=True
)
async def lister_devis(
    page: Page,
    statut: str | None = None,
    ctx: Contexte = Depends(exige("invoice.view", lecture=True)),
) -> Any:
    return await Depot("devis", m.Devis).lister(
        ctx, page, filtre=lambda d: not statut or d.statut == statut, tri_defaut="numero"
    )


@router.post(
    "/devis/{devisId}/acceptation", response_model=m.Devis, response_model_exclude_none=True
)
async def accepter_devis(devisId: str, ctx: Contexte = Depends(exige("payment.update"))) -> Any:  # noqa: N803
    depot = Depot("devis", m.Devis)
    devis = await depot.obtenir(ctx, devisId)
    if devis.statut != "envoye":
        raise erreurs.conflit(
            "Ce devis n'est plus en attente d'acceptation.", code="devis_non_accepte"
        )
    await depot.definir_statut(ctx, devisId, "accepte")
    offre = await Depot("offre", m.Offre, plateforme=True).trouver(ctx, devis.id)
    souscription = m.Souscription(
        id=nouvel_id(),
        orgId=ctx.org_id,
        cible=m.Cible1(
            type="offer",
            ref=offre.id if offre else devis.id,
            label=offre.nom if offre else devis.objet,
        ),
        quantite=1,
        prixApplique=devis.montant,
        debut=date.today(),
        periodicite="mensuelle",
    )
    await Depot("souscription", m.Souscription).creer(ctx, souscription)
    await journaliser(ctx, action="devis.acceptation", cible_type="devis", cible_id=devisId)
    return await depot.obtenir(ctx, devisId)


@router.post("/estimation", response_model=m.EstimationCout, response_model_exclude_none=True)
async def estimer_cout(corps: m.DemandeEstimation, ctx: Contexte = Depends(exige(None))) -> Any:
    return tarification.estimer(corps)


@router.get(
    "/factures", response_model=m.FacturationFacturesGetResponse, response_model_exclude_none=True
)
async def lister_factures(
    page: Page,
    statut: str | None = None,
    periode: str | None = None,
    devise: str | None = None,
    ctx: Contexte = Depends(exige("invoice.view", lecture=True)),
) -> Any:
    return await Depot("facture", m.Facture).lister(
        ctx,
        page,
        filtre=lambda f: (
            (not statut or f.statut == statut)
            and (not periode or f.periode == periode)
            and (not devise or f.devise == devise)
        ),
        tri_defaut="numero",
    )


@router.get("/factures/{factureId}", response_model=m.Facture, response_model_exclude_none=True)
async def obtenir_facture(
    factureId: str, ctx: Contexte = Depends(exige("invoice.view", lecture=True))
) -> Any:  # noqa: N803
    return await Depot("facture", m.Facture).obtenir(ctx, factureId)


@router.post(
    "/factures/{factureId}/paiement",
    response_model=m.FacturationFacturesFactureIdPaiementPostResponse,
    response_model_exclude_none=True,
)
async def payer_facture(
    factureId: str,
    corps: m.FacturationFacturesFactureIdPaiementPostRequest,
    ctx: Contexte = Depends(exige("payment.update")),
) -> Any:  # noqa: N803
    depot = Depot("facture", m.Facture)
    facture = await depot.obtenir(ctx, factureId)
    if facture.statut == "payee":
        raise erreurs.conflit("Cette facture est déjà payée.", code="facture_deja_payee")
    await crediter(ctx, ctx.org_id, f"Paiement facture {facture.numero}", facture.total)
    facture = await depot.definir_statut(ctx, factureId, "payee")
    await journaliser(ctx, action="facture.paiement", cible_type="facture", cible_id=factureId)
    if ctx.principal and ctx.principal.utilisateur_id:
        u = await ctx.session.get(Utilisateur, ctx.principal.utilisateur_id)
        if u is not None:
            await courriel.envoyer(
                u.email,
                f"Paiement reçu — facture {facture.numero}",
                f"Bonjour {u.nom},",
                [
                    f"Nous avons bien reçu le paiement de la facture {facture.numero}, "
                    f"d'un montant de {facture.total} {facture.devise}.",
                    "Vous pouvez la retrouver à tout moment dans votre espace Facturation.",
                ],
                bouton_texte="Voir la facture",
                bouton_url=f"{ctx.reglages.url_frontend}/app/facturation/factures/{factureId}",
            )
    return {"facture": facture, "urlRedirection": None, "statut": "payee"}


_PDF = "%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 595 842]/Contents 4 0 R>>endobj\n4 0 obj<</Length 80>>stream\nBT /F1 14 Tf 60 780 Td (FACTURE {numero}) Tj 0 -20 Td (Total: {total} FCFA) Tj ET\nendstream endobj\ntrailer<</Root 1 0 R>>\n%%EOF"


@router.get("/factures/{factureId}/pdf")
async def obtenir_pdf_facture(
    factureId: str, ctx: Contexte = Depends(exige("invoice.view", lecture=True))
) -> Response:  # noqa: N803
    facture = await Depot("facture", m.Facture).obtenir(ctx, factureId)
    pdf = _PDF.format(numero=facture.numero, total=facture.total).encode("latin-1")
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{facture.numero}.pdf"'},
    )


@router.post("/factures/{factureId}/paystack/initier")
async def initier_paiement_paystack(
    factureId: str, ctx: Contexte = Depends(exige("payment.update"))
) -> dict[str, Any]:  # noqa: N803
    """Prépare le popup Inline.js côté client : une référence propre à Synelia (pas celle
    de Paystack), pour retrouver la facture au retour sans dépendre d'un état côté serveur."""
    facture = await Depot("facture", m.Facture).obtenir(ctx, factureId)
    if facture.statut == "payee":
        raise erreurs.conflit("Cette facture est déjà payée.", code="facture_deja_payee")
    cle_publique = os.environ.get("PAYSTACK_PUBLIC_KEY", "")
    u = await ctx.session.get(Utilisateur, ctx.utilisateur_id) if ctx.utilisateur_id else None
    return {
        "reference": paystack.generer_reference(ctx.org_id, factureId),
        "clePublique": cle_publique,
        "montant": facture.total,
        "devise": facture.devise,
        "email": u.email if u else ctx.principal.email if ctx.principal else "",
        # XOF n'a pas de sous-unité mais l'API Paystack attend systématiquement le montant
        # x100 — vérifié en sandbox : envoyer la valeur brute la divise par cent à l'affichage.
        "montantMineur": facture.total * 100,
        "canaux": ["card", "mobile_money"],
    }


@router.post("/paystack/prepayer")
async def initier_prepaiement_paystack(
    corps: dict[str, Any], ctx: Contexte = Depends(exige(None))
) -> dict[str, Any]:
    """Paiement exigé avant la création d'un Espace Cloud ou d'un domaine — aucune facture
    n'existe encore pour ce montant, donc pas de `factureId` à référencer (voir l'endpoint
    précédent, qui suppose une facture)."""
    montant = int(corps.get("montant") or 0)
    if montant <= 0:
        raise erreurs.validation("Montant invalide.", {"montant": "Doit être supérieur à zéro."})
    cle_publique = os.environ.get("PAYSTACK_PUBLIC_KEY", "")
    u = await ctx.session.get(Utilisateur, ctx.utilisateur_id) if ctx.utilisateur_id else None
    return {
        "reference": paystack.generer_reference_prepaiement(ctx.org_id, montant),
        "clePublique": cle_publique,
        "montant": montant,
        "devise": "XOF",
        "email": u.email if u else ctx.principal.email if ctx.principal else "",
        "montantMineur": montant * 100,
        "canaux": ["card", "mobile_money"],
    }


@router.get("/paystack/verifier/{reference}")
async def verifier_paiement_paystack(reference: str) -> dict[str, Any]:
    """Appelé par le callback du popup Inline.js juste après le paiement : revérifie
    auprès de Paystack (jamais en faisant confiance au client) puis crédite. Volontairement
    public — la vérification, pas une signature, est ce qui rend l'appel sûr — de sorte que
    la démo n'échoue pas si le webhook n'atteint jamais ce labo."""
    donnees = await paystack.verifier_aupres_de_paystack(reference)
    if donnees is None:
        raise erreurs.introuvable("Transaction Paystack", reference)
    confirme = await paystack.traiter_evenement_charge_reussie(donnees)
    return {"reference": reference, "statut": "payee" if confirme or donnees.get("status") == "success" else "en_attente"}


@router.post("/paystack/webhook", status_code=status.HTTP_200_OK)
async def webhook_paystack(requete: Request) -> dict[str, str]:
    """Chemin redondant du précédent : si Paystack arrive à joindre dev01, aussi bien.
    Toujours répondre 200 une fois le corps lu, signature valide ou non — sinon Paystack
    réessaie pendant des jours sur une erreur qui ne se corrigera jamais toute seule."""
    corps_brut = await requete.body()
    signature = requete.headers.get("x-paystack-signature")
    if not paystack.verifier_signature(corps_brut, signature):
        return {"statut": "signature_invalide"}
    evenement = json.loads(corps_brut)
    if evenement.get("event") == "charge.success":
        await paystack.traiter_evenement_charge_reussie(evenement.get("data", {}))
    return {"statut": "recu"}


@router.get("/moyens-paiement", response_model=list[m.MoyenPaiement])
async def lister_moyens_paiement(ctx: Contexte = Depends(exige("payment.update"))) -> Any:
    return await Depot("moyen_paiement", m.MoyenPaiement).tous(ctx)


@router.post(
    "/moyens-paiement",
    response_model=m.MoyenPaiement,
    status_code=status.HTTP_201_CREATED,
    response_model_exclude_none=True,
)
async def ajouter_moyen_paiement(
    corps: m.MoyenPaiementCreation, ctx: Contexte = Depends(exige("payment.update"))
) -> Any:
    depot = Depot("moyen_paiement", m.MoyenPaiement)
    libelle = corps.libelle or corps.type.replace("_", " ").title()
    detail = None
    if corps.numero and len(corps.numero) >= 4:
        detail = f"•••• {corps.numero[-4:]}"
    moyen = m.MoyenPaiement(
        id=nouvel_id(),
        type=corps.type,
        libelle=libelle,
        detail=detail,
        defaut=bool(corps.defaut),
        expire=corps.expiration,
        statut="actif",
    )
    await depot.creer(ctx, moyen)
    await journaliser(
        ctx, action="moyen_paiement.creation", cible_type="moyen_paiement", cible_id=moyen.id
    )
    return moyen


@router.patch(
    "/moyens-paiement/{moyenId}", response_model=m.MoyenPaiement, response_model_exclude_none=True
)
async def modifier_moyen_paiement(
    moyenId: str,
    corps: m.FacturationMoyensPaiementMoyenIdPatchRequest,
    ctx: Contexte = Depends(exige("payment.update")),
) -> Any:  # noqa: N803
    depot = Depot("moyen_paiement", m.MoyenPaiement)
    if corps.defaut:
        for autre in await depot.tous(ctx):
            if autre.id != moyenId and autre.defaut:
                await depot.modifier(ctx, autre.id, {"defaut": False})
    m_ = await depot.modifier(ctx, moyenId, corps)
    await journaliser(
        ctx, action="moyen_paiement.modification", cible_type="moyen_paiement", cible_id=moyenId
    )
    return m_


@router.delete("/moyens-paiement/{moyenId}", status_code=status.HTTP_204_NO_CONTENT)
async def supprimer_moyen_paiement(
    moyenId: str, ctx: Contexte = Depends(exige("payment.update"))
) -> Response:  # noqa: N803
    await Depot("moyen_paiement", m.MoyenPaiement).supprimer(ctx, moyenId, logique=True)
    await journaliser(
        ctx, action="moyen_paiement.suppression", cible_type="moyen_paiement", cible_id=moyenId
    )
    return Response(status_code=204)


@router.post(
    "/prepaye/rechargement",
    response_model=m.FacturationPrepayeRechargementPostResponse,
    response_model_exclude_none=True,
)
async def recharger_prepaye(
    corps: m.Rechargement, ctx: Contexte = Depends(exige("payment.update"))
) -> Any:
    if not (1 <= corps.montant <= 100_000_000_000):
        raise erreurs.validation(
            "Montant invalide.", {"montant": "Doit etre un entier positif raisonnable."}
        )
    await crediter(ctx, ctx.org_id, f"Rechargement prépayé {corps.montant} FCFA", corps.montant)
    solde = await service.solde_credit(ctx)
    await journaliser(
        ctx,
        action="prepaye.rechargement",
        cible_type="organisation",
        cible_id=ctx.org_id,
        details={"montant": corps.montant},
    )
    return {"solde": solde, "urlRedirection": None, "statut": "credite"}


@router.get("/sla", response_model=m.FacturationSlaGetResponse, response_model_exclude_none=True)
async def obtenir_sla(ctx: Contexte = Depends(exige("invoice.view", lecture=True))) -> Any:
    return await service.sla_engagements(ctx)


@router.post(
    "/sla/reclamations",
    response_model=m.AccuseReception,
    status_code=status.HTTP_201_CREATED,
    response_model_exclude_none=True,
)
async def reclamer_credit_sla(
    corps: m.ReclamationCredit, ctx: Contexte = Depends(exige("invoice.view", lecture=True))
) -> Any:
    await crediter(ctx, ctx.org_id, f"Crédit SLA {corps.composant} {corps.periode}", 5000)
    ref = nouvel_id()
    await journaliser(
        ctx,
        action="sla.reclamation",
        cible_type="sla",
        cible_id=ref,
        details=corps.model_dump(mode="json", exclude_none=True),
    )
    return {
        "reference": ref,
        "message": "Réclamation enregistrée, un crédit SLA de 5 000 FCFA a été appliqué.",
        "delaiReponseHeures": 48,
    }


@router.get(
    "/souscriptions",
    response_model=m.FacturationSouscriptionsGetResponse,
    response_model_exclude_none=True,
)
async def lister_souscriptions(
    page: Page,
    actives: bool | None = None,
    ctx: Contexte = Depends(exige("invoice.view", lecture=True)),
) -> Any:
    return await Depot("souscription", m.Souscription).lister(
        ctx,
        page,
        filtre=lambda s: actives is None or (s.fin is None if actives else s.fin is not None),
        tri_defaut="debut",
    )


@router.patch(
    "/souscriptions/{souscriptionId}",
    response_model=m.Souscription,
    response_model_exclude_none=True,
)
async def modifier_souscription(
    souscriptionId: str,
    corps: m.FacturationSouscriptionsSouscriptionIdPatchRequest,
    ctx: Contexte = Depends(exige("payment.update")),
) -> Any:  # noqa: N803
    s = await Depot("souscription", m.Souscription).modifier(ctx, souscriptionId, corps)
    await journaliser(
        ctx, action="souscription.modification", cible_type="souscription", cible_id=souscriptionId
    )
    return s


@router.delete(
    "/souscriptions/{souscriptionId}",
    response_model=m.FacturationSouscriptionsSouscriptionIdDeleteResponse,
    response_model_exclude_none=True,
)
async def resilier_souscription(
    souscriptionId: str,
    confirmation: str | None = None,
    ctx: Contexte = Depends(exige("payment.update")),
) -> Any:  # noqa: N803
    depot = Depot("souscription", m.Souscription)
    s = await depot.obtenir(ctx, souscriptionId)
    if s.fin is not None:
        raise erreurs.conflit(
            "Cette souscription est déjà résiliée.", code="souscription_deja_resiliee"
        )
    fin = date.today().isoformat()
    await depot.modifier(ctx, souscriptionId, {"fin": fin})
    s = await depot.obtenir(ctx, souscriptionId)
    await journaliser(
        ctx,
        action="souscription.resiliation",
        cible_type="souscription",
        cible_id=souscriptionId,
        details={"finEffet": fin},
    )
    return {"souscription": s, "finEffet": fin}


@router.get("/ventilation", response_model=m.Ventilation, response_model_exclude_none=True)
async def obtenir_ventilation(
    axe: str = "espace",
    periode: str | None = None,
    ctx: Contexte = Depends(exige("invoice.view", lecture=True)),
) -> Any:
    vms = await Depot("vm", m.Vm).tous(ctx)
    lignes: dict[str, int] = {}

    def ajouter(label: str, montant: int) -> None:
        lignes[label] = lignes.get(label, 0) + montant

    if axe == "famille":
        # `Famille` = catégorie de coût (Calcul/Stockage/Réseau), pas le champ `famille`
        # d'un gabarit VM (generique/calcul/memoire/gpu/economique) : le contrat documente
        # les deux sous le même mot mais ce showback répond à « où part la dépense »,
        # même découpage que la métrologie (`metrologie.consommation`).
        for v in vms:
            ajouter(
                "Calcul",
                tarification._prix_ressource("vm", {"vcpu": v.vcpu, "ramGo": v.ramGo, "diskGo": 0}, 1),
            )
            ajouter("Stockage", tarification._prix_ressource("volume", {"tailleGo": v.diskGo}, 1))
        volumes = await Depot("volume", m.Volume).tous(ctx)
        for vol in volumes:
            ajouter("Stockage", tarification._prix_ressource("volume", {"tailleGo": vol.tailleGo}, 1))
        lbs = await Depot("load_balancer", m.LoadBalancer).tous(ctx)
        ajouter("Réseau", metrologie.PRIX["lb_jour"] * 30 * len(lbs))
        ips_publiques = sum(1 for v in vms for ip in v.ips if ip.type == "publique")
        ajouter("Réseau", metrologie.PRIX["ip_publique_jour"] * 30 * ips_publiques)
    else:
        for v in vms:
            if axe == "application":
                label = v.applicationNom or v.applicationId or "Général"
            elif axe == "site":
                label = v.site or "Général"
            else:
                label = v.espaceId or "Général"
            prix = tarification._prix_ressource(
                "vm", {"vcpu": v.vcpu, "ramGo": v.ramGo, "diskGo": v.diskGo}, 1
            )
            ajouter(label, prix)

    total = sum(lignes.values())
    if not lignes:
        lignes["Général"] = 0
    partes = [
        m.Ligne2(label=k, montant=v, pct=round(v * 100 / total, 1) if total else 0)
        for k, v in lignes.items()
    ]
    return {"axe": axe, "lignes": partes, "total": total}
