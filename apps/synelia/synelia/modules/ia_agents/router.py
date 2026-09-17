from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Response, status
from synelia_contract import modeles as m
from synelia_db.modeles import Travail
from synelia_kernel import erreurs
from synelia_kernel.dates import maintenant
from synelia_kernel.ids import nouvel_id

from synelia.audit import journaliser
from synelia.deps import Contexte, Page, exige, exiger_confirmation
from synelia.modules.ia_agents import cles, connaissances, flux, service
from synelia.modules.ia_agents.cles import ENTETE_CLE_IA, depot_cles
from synelia.modules.ia_agents.connaissances import depot_connaissance
from synelia.modules.ia_agents.flux import depot_flux
from synelia.modules.ia_agents.service import depot_agents, depot_modeles
from synelia.travaux import demarrer_travail, reprendre_apres_pause
from synelia.travaux.moteur import vers_contrat

ClIA = Annotated[str | None, Header(alias=ENTETE_CLE_IA)]

router = APIRouter(tags=["IA — Agents"])

# ─── Catalogue de modèles (lecture ; POST réservé au support de nouveaux modèles) ──


@router.get("/ia/modeles", response_model=m.IaModelesGetResponse, response_model_exclude_none=True)
async def lister_modeles_ia(
    page: Page, ctx: Contexte = Depends(exige("org.dashboard.view", lecture=True))
) -> Any:
    await service._semer_modeles(ctx)
    return await depot_modeles.lister(ctx, page, tri_defaut="nom")


@router.post(
    "/ia/modeles",
    response_model=m.ModeleIA,
    status_code=status.HTTP_201_CREATED,
    response_model_exclude_none=True,
)
async def creer_modele_ia(corps: m.ModeleIACreation, ctx: Contexte = Depends(exige(None))) -> Any:
    await service._semer_modeles(ctx)
    donnees = corps.model_dump(exclude={"invocable", "statut"})
    modele = m.ModeleIA(
        id=corps.slug.replace("/", "-"),
        statut=corps.statut or "disponible",
        invocable=corps.invocable if corps.invocable is not None else False,
        **donnees,
    )
    m_ = await depot_modeles.creer(ctx, modele, id_=modele.id)
    await journaliser(ctx, action="ia.modele.creation", cible_type="modele_ia", cible_id=modele.id)
    return m_


@router.get("/ia/modeles/{modeleId}", response_model=m.ModeleIA, response_model_exclude_none=True)
async def obtenir_modele_ia(
    modeleId: str, ctx: Contexte = Depends(exige("org.dashboard.view", lecture=True))
) -> Any:  # noqa: N803
    await service._semer_modeles(ctx)
    return await depot_modeles.obtenir(ctx, modeleId)


# ─── Agents ────────────────────────────────────────────────────────────


@router.get("/ia/agents", response_model=m.IaAgentsGetResponse, response_model_exclude_none=True)
async def lister_agents_ia(
    page: Page, ctx: Contexte = Depends(exige("org.dashboard.view", lecture=True))
) -> Any:
    return await depot_agents.lister(ctx, page, tri_defaut="nom")


@router.post(
    "/ia/agents",
    response_model=m.AgentIA,
    status_code=status.HTTP_201_CREATED,
    response_model_exclude_none=True,
)
async def creer_agent_ia(
    corps: m.AgentIACreation, ctx: Contexte = Depends(exige("ia.agent.write"))
) -> Any:
    modele = await service.obtenir_modele_par_slug(ctx, corps.modele)
    if modele is None:
        raise erreurs.validation(
            "Modèle IA inconnu.",
            champs={"modele": f"Aucun modèle avec le slug « {corps.modele} »."},
        )
    agent = m.AgentIA(
        id=nouvel_id(),
        nom=corps.nom,
        consigne=corps.consigne,
        espaceId=corps.espaceId,
        modele=corps.modele,
        temperature=corps.temperature if corps.temperature is not None else 0.7,
        topP=corps.topP if corps.topP is not None else 1,
        jetonsMax=corps.jetonsMax if corps.jetonsMax is not None else 1024,
        statut="brouillon",
        createdAt=maintenant(),
    )
    agent = await depot_agents.creer(ctx, agent)
    await journaliser(
        ctx, action="agent_ia.creation", cible_type="agent_ia", cible_id=agent.id, cible=agent.nom
    )
    return agent


@router.get("/ia/agents/{agentId}", response_model=m.AgentIA, response_model_exclude_none=True)
async def obtenir_agent_ia(
    agentId: str, ctx: Contexte = Depends(exige("org.dashboard.view", lecture=True))
) -> Any:  # noqa: N803
    return await depot_agents.obtenir(ctx, agentId)


@router.patch("/ia/agents/{agentId}", response_model=m.AgentIA, response_model_exclude_none=True)
async def modifier_agent_ia(
    agentId: str, corps: m.AgentIAModification, ctx: Contexte = Depends(exige("ia.agent.write"))
) -> Any:  # noqa: N803
    if corps.modele and await service.obtenir_modele_par_slug(ctx, corps.modele) is None:
        raise erreurs.validation(
            "Modèle IA inconnu.",
            champs={"modele": f"Aucun modèle avec le slug « {corps.modele} »."},
        )
    await depot_agents.modifier(ctx, agentId, corps)
    agent = await depot_agents.obtenir(ctx, agentId)
    await journaliser(
        ctx,
        action="agent_ia.modification",
        cible_type="agent_ia",
        cible_id=agentId,
        details=corps.model_dump(mode="json", exclude_none=True),
    )
    return agent


@router.delete("/ia/agents/{agentId}", status_code=status.HTTP_204_NO_CONTENT)
async def supprimer_agent_ia(
    agentId: str,
    confirmation: str | None = None,
    ctx: Contexte = Depends(exige("ia.agent.write")),
) -> Response:  # noqa: N803
    agent = await depot_agents.obtenir(ctx, agentId)
    exiger_confirmation(agent.nom, confirmation)
    await depot_agents.supprimer(ctx, agentId)
    await journaliser(
        ctx, action="agent_ia.suppression", cible_type="agent_ia", cible_id=agentId, cible=agent.nom
    )
    return Response(status_code=204)


@router.post(
    "/ia/agents/{agentId}/invoquer",
    response_model=m.AgentInvocationResponse,
    response_model_exclude_none=True,
)
async def invoquer_agent(
    agentId: str,
    corps: m.AgentInvocationRequest,
    ctx: Contexte = Depends(exige(None)),
    x_cle_ia: ClIA = None,
) -> Any:  # noqa: N803
    agent = await depot_agents.obtenir(ctx, agentId)
    modele = await service.obtenir_modele_par_slug(ctx, agent.modele)
    cle_ia = await cles.verifier_et_appliquer(
        ctx,
        x_cle_ia,
        slug_modele=agent.modele,
        hebergement_modele=modele.hebergement if modele else "externe",
    )
    resultat = await service.invoquer(ctx, agent, corps.message)
    if cle_ia is not None:
        await cles.crediter_apres_appel(
            ctx,
            cle_ia,
            jetons=resultat["jetonsEntree"] + resultat["jetonsSortie"],
            cout_fcfa=resultat["coutFcfa"],
        )
    await journaliser(
        ctx,
        action="agent_ia.invocation",
        cible_type="agent_ia",
        cible_id=agentId,
        cible=agent.nom,
        details={
            "jetonsEntree": resultat["jetonsEntree"],
            "jetonsSortie": resultat["jetonsSortie"],
        },
    )
    return resultat


# ─── Flux d'orchestration ────────────────────────────────────────────────


@router.get("/ia/flux", response_model=m.IaFluxGetResponse, response_model_exclude_none=True)
async def lister_flux_orchestrations(
    page: Page, ctx: Contexte = Depends(exige("org.dashboard.view", lecture=True))
) -> Any:
    return await depot_flux.lister(ctx, page, tri_defaut="nom")


@router.post(
    "/ia/flux",
    response_model=m.FluxOrchestration,
    status_code=status.HTTP_201_CREATED,
    response_model_exclude_none=True,
)
async def creer_flux_orchestration(
    corps: m.FluxOrchestrationCreation, ctx: Contexte = Depends(exige("ia.flow.write"))
) -> Any:
    flux_ = m.FluxOrchestration(
        id=nouvel_id(),
        nom=corps.nom,
        description=corps.description or "",
        espaceId=corps.espaceId or "",
        statut="brouillon",
        declencheur=corps.declencheur,
        etapes=corps.etapes or [],
        variables=corps.variables or [],
        memoirePartagee=bool(corps.memoirePartagee),
        version="v1",
    )
    flux_ = await depot_flux.creer(ctx, flux_)
    await journaliser(
        ctx, action="flux_ia.creation", cible_type="flux_ia", cible_id=flux_.id, cible=flux_.nom
    )
    return flux_


@router.get(
    "/ia/flux/{fluxId}", response_model=m.FluxOrchestration, response_model_exclude_none=True
)
async def obtenir_flux_orchestration(
    fluxId: str, ctx: Contexte = Depends(exige("org.dashboard.view", lecture=True))
) -> Any:  # noqa: N803
    return await depot_flux.obtenir(ctx, fluxId)


@router.patch(
    "/ia/flux/{fluxId}", response_model=m.FluxOrchestration, response_model_exclude_none=True
)
async def modifier_flux_orchestration(
    fluxId: str,
    corps: m.FluxOrchestrationModification,
    ctx: Contexte = Depends(exige("ia.flow.write")),
) -> Any:  # noqa: N803
    await depot_flux.modifier(ctx, fluxId, corps)
    flux_ = await depot_flux.obtenir(ctx, fluxId)
    await journaliser(
        ctx,
        action="flux_ia.modification",
        cible_type="flux_ia",
        cible_id=fluxId,
        details=corps.model_dump(mode="json", exclude_none=True),
    )
    return flux_


@router.delete("/ia/flux/{fluxId}", status_code=status.HTTP_204_NO_CONTENT)
async def supprimer_flux_orchestration(
    fluxId: str,
    confirmation: str | None = None,
    ctx: Contexte = Depends(exige("ia.flow.write")),
) -> Response:  # noqa: N803
    flux_ = await depot_flux.obtenir(ctx, fluxId)
    exiger_confirmation(flux_.nom, confirmation)
    await depot_flux.supprimer(ctx, fluxId)
    await journaliser(
        ctx, action="flux_ia.suppression", cible_type="flux_ia", cible_id=fluxId, cible=flux_.nom
    )
    # Sinon un travail encore `queued`/`running` (notamment une pause `humain` en attente
    # d'approbation) référençant ce flux resterait un zombie permanent : le flux vient de
    # disparaître, `POST .../reprendre` n'a plus rien à relire. Même transaction que la
    # suppression ci-dessus.
    travaux_annules = await flux.annuler_executions_en_cours(
        ctx,
        fluxId,
        motif=f"Le flux « {flux_.nom} » a été supprimé pendant l'exécution de ce travail "
        "(éventuellement en attente d'approbation) : il ne peut plus aboutir.",
    )
    for travail in travaux_annules:
        await journaliser(
            ctx,
            action="travail.annulation",
            cible_type="travail",
            cible_id=travail.id,
            cible=travail.label,
            details={"cause": "flux_ia.suppression", "fluxId": fluxId},
        )
    return Response(status_code=204)


@router.post(
    "/ia/flux/{fluxId}/executer",
    response_model=m.TravailProvisioning,
    status_code=status.HTTP_202_ACCEPTED,
)
async def executer_flux(
    fluxId: str, corps: m.FluxExecutionRequest, ctx: Contexte = Depends(exige(None))
) -> Any:  # noqa: N803
    flux_ = await depot_flux.obtenir(ctx, fluxId)
    resultat = await flux.demarrer_execution(ctx, flux_, corps.entree, corps.variables)
    await journaliser(
        ctx, action="flux_ia.execution", cible_type="flux_ia", cible_id=fluxId, cible=flux_.nom
    )
    return resultat


@router.post(
    "/ia/flux/{fluxId}/executions/{travailId}/reprendre",
    response_model=m.TravailProvisioning,
    status_code=status.HTTP_202_ACCEPTED,
)
async def reprendre_execution_flux(
    fluxId: str,
    travailId: str,
    corps: m.FluxRepriseRequest,
    ctx: Contexte = Depends(exige("ia.flow.write")),
) -> Any:  # noqa: N803
    travail = await ctx.session.get(Travail, travailId)
    if (
        travail is None
        or travail.type != flux.TYPE_TRAVAIL
        or travail.cible_id != fluxId
        or (travail.org_id and travail.org_id != ctx.org_id_ou_none)
    ):
        raise erreurs.introuvable("Travail", travailId)
    attente = (travail.contexte or {}).get("attente")
    if travail.statut != "running" or not attente:
        raise erreurs.conflit(
            "Ce travail n'est pas en attente d'une validation humaine.", code="pas_en_attente"
        )

    contexte = dict(travail.contexte)
    resultats = dict(contexte.get("resultats") or {})
    cle = attente["cle"]
    resultats[cle] = {
        **(resultats.get(cle) or {}),
        "decision": corps.decision,
        "commentaire": corps.commentaire,
    }
    contexte["resultats"] = resultats
    contexte["attente"] = None
    travail.contexte = contexte

    await reprendre_apres_pause(ctx, travail, attente["etapeIndex"])
    await journaliser(
        ctx,
        action="flux_ia.reprise",
        cible_type="flux_ia",
        cible_id=fluxId,
        details={"travailId": travailId, "decision": corps.decision},
    )
    return vers_contrat(travail)


# ─── Bases de connaissances (Docling → BGE-M3 → Qdrant) ────────────────


@router.get(
    "/ia/connaissances",
    response_model=m.IaConnaissancesGetResponse,
    response_model_exclude_none=True,
)
async def lister_connaissances(
    page: Page, ctx: Contexte = Depends(exige("org.dashboard.view", lecture=True))
) -> Any:
    return await depot_connaissance.lister(ctx, page, tri_defaut="nom")


@router.post(
    "/ia/connaissances",
    response_model=m.BaseConnaissance,
    status_code=status.HTTP_201_CREATED,
    response_model_exclude_none=True,
)
async def creer_connaissance(
    corps: m.BaseConnaissanceCreation, ctx: Contexte = Depends(exige("ia.knowledge.write"))
) -> Any:
    base = m.BaseConnaissance(
        id=nouvel_id(),
        nom=corps.nom,
        espaceId=corps.espaceId,
        source=corps.source,
        documents=0,
        fragments=0,
        modeleEmbedding="",
        dimension=0,
        modeDecoupage=corps.modeDecoupage or "general",
        methodeIndex=corps.methodeIndex or "haute_qualite",
        modeRecherche=corps.modeRecherche or "vectorielle",
        citations=corps.citations if corps.citations is not None else True,
        tailleMo=0,
        frequence=corps.frequence or "manuelle",
        derniereIndexation=maintenant(),
        statut="jamais_indexee",
        clesAutorisees=[],
    )
    base = await depot_connaissance.creer(ctx, base)
    await journaliser(
        ctx,
        action="connaissance_ia.creation",
        cible_type="connaissance_ia",
        cible_id=base.id,
        cible=base.nom,
    )
    return base


@router.get(
    "/ia/connaissances/{connaissanceId}",
    response_model=m.BaseConnaissance,
    response_model_exclude_none=True,
)
async def obtenir_connaissance(
    connaissanceId: str, ctx: Contexte = Depends(exige("org.dashboard.view", lecture=True))
) -> Any:  # noqa: N803
    return await depot_connaissance.obtenir(ctx, connaissanceId)


@router.patch(
    "/ia/connaissances/{connaissanceId}",
    response_model=m.BaseConnaissance,
    response_model_exclude_none=True,
)
async def modifier_connaissance(
    connaissanceId: str,
    corps: m.BaseConnaissanceModification,
    ctx: Contexte = Depends(exige("ia.knowledge.write")),
) -> Any:  # noqa: N803
    await depot_connaissance.modifier(ctx, connaissanceId, corps)
    base = await depot_connaissance.obtenir(ctx, connaissanceId)
    await journaliser(
        ctx,
        action="connaissance_ia.modification",
        cible_type="connaissance_ia",
        cible_id=connaissanceId,
        details=corps.model_dump(mode="json", exclude_none=True),
    )
    return base


@router.delete("/ia/connaissances/{connaissanceId}", status_code=status.HTTP_204_NO_CONTENT)
async def supprimer_connaissance(
    connaissanceId: str,
    confirmation: str | None = None,
    ctx: Contexte = Depends(exige("ia.knowledge.write")),
) -> Response:  # noqa: N803
    base = await depot_connaissance.obtenir(ctx, connaissanceId)
    exiger_confirmation(base.nom, confirmation)
    await depot_connaissance.supprimer(ctx, connaissanceId)
    if connaissances._reel():
        await connaissances.supprimer_collection(connaissances.nom_collection(connaissanceId))
    await journaliser(
        ctx,
        action="connaissance_ia.suppression",
        cible_type="connaissance_ia",
        cible_id=connaissanceId,
        cible=base.nom,
    )
    return Response(status_code=204)


@router.post(
    "/ia/connaissances/{connaissanceId}/documents",
    response_model=m.TravailProvisioning,
    status_code=status.HTTP_202_ACCEPTED,
    response_model_exclude_none=True,
)
async def ingerer_document_connaissance(
    connaissanceId: str,
    corps: m.DocumentConnaissanceCreation,
    ctx: Contexte = Depends(exige("ia.knowledge.write")),
) -> Any:  # noqa: N803
    base = await depot_connaissance.obtenir(ctx, connaissanceId)
    if not (corps.texte or corps.contenuBase64 or corps.url):
        raise erreurs.validation(
            "Fournir au moins un contenu à ingérer.",
            champs={"texte": "Un de `texte`, `contenuBase64` ou `url` est requis."},
        )
    await depot_connaissance.modifier(ctx, connaissanceId, {"statut": "indexation"})
    resultat = await demarrer_travail(
        ctx,
        "connaissance.ingerer_document",
        corps.nom,
        cible_type="connaissance_ia",
        cible_id=connaissanceId,
        entree=corps.model_dump(mode="json"),
        etapes=[
            {"nom": "Extraire le texte (Docling)", "dureeS": 5},
            {"nom": "Découper, vectoriser (BGE-M3) et indexer (Qdrant)", "dureeS": 8},
        ],
    )
    await journaliser(
        ctx,
        action="connaissance_ia.ingestion",
        cible_type="connaissance_ia",
        cible_id=connaissanceId,
        cible=base.nom,
        details={"document": corps.nom},
    )
    return resultat


@router.post(
    "/ia/connaissances/{connaissanceId}/rechercher",
    response_model=m.ConnaissanceRechercheResponse,
    response_model_exclude_none=True,
)
async def rechercher_connaissance(
    connaissanceId: str,
    corps: m.ConnaissanceRechercheRequest,
    ctx: Contexte = Depends(exige(None)),
    x_cle_ia: ClIA = None,
) -> Any:  # noqa: N803
    base = await depot_connaissance.obtenir(ctx, connaissanceId)
    cle_ia = await cles.verifier_et_appliquer(
        ctx,
        x_cle_ia,
        slug_modele=connaissances.SLUG_MODELE_EMBEDDING,
        hebergement_modele="souverain",
    )
    fragments, jetons = await connaissances.rechercher(ctx, base, corps.query, corps.topK or 5)
    if cle_ia is not None:
        modele_embed = await service.obtenir_modele_par_slug(
            ctx, connaissances.SLUG_MODELE_EMBEDDING
        )
        cout_fcfa = jetons * ((modele_embed.prixEntree if modele_embed else 45) / 1_000_000)
        await cles.crediter_apres_appel(ctx, cle_ia, jetons=jetons, cout_fcfa=cout_fcfa)
    await journaliser(
        ctx,
        action="connaissance_ia.recherche",
        cible_type="connaissance_ia",
        cible_id=connaissanceId,
        cible=base.nom,
        details={"jetons": jetons},
    )
    return {"fragments": fragments}


# ─── Clés d'accès IA ────────────────────────────────────────────────────


@router.get("/ia/cles", response_model=m.IaClesGetResponse, response_model_exclude_none=True)
async def lister_cles_ia(
    page: Page, ctx: Contexte = Depends(exige("ia.key.manage", lecture=True))
) -> Any:
    return await depot_cles.lister(ctx, page, tri_defaut="nom")


@router.post(
    "/ia/cles",
    response_model=m.CleIASecret,
    status_code=status.HTTP_201_CREATED,
    response_model_exclude_none=True,
)
async def creer_cle_ia(
    corps: m.CleIACreation, ctx: Contexte = Depends(exige("ia.key.manage"))
) -> Any:
    return await cles.creer(ctx, corps)


@router.get("/ia/cles/{cleId}", response_model=m.CleIA, response_model_exclude_none=True)
async def obtenir_cle_ia(
    cleId: str, ctx: Contexte = Depends(exige("ia.key.manage", lecture=True))
) -> Any:  # noqa: N803
    return await depot_cles.obtenir(ctx, cleId)


@router.patch("/ia/cles/{cleId}", response_model=m.CleIA, response_model_exclude_none=True)
async def modifier_cle_ia(
    cleId: str, corps: m.CleIAModification, ctx: Contexte = Depends(exige("ia.key.manage"))
) -> Any:  # noqa: N803
    await depot_cles.modifier(ctx, cleId, corps)
    cle = await depot_cles.obtenir(ctx, cleId)
    await journaliser(
        ctx,
        action="cle_ia.modification",
        cible_type="cle_ia",
        cible_id=cleId,
        details=corps.model_dump(mode="json", exclude_none=True),
    )
    return cle


@router.delete("/ia/cles/{cleId}", status_code=status.HTTP_204_NO_CONTENT)
async def revoquer_cle_ia(
    cleId: str,
    confirmation: str | None = None,
    ctx: Contexte = Depends(exige("ia.key.manage")),
) -> Response:  # noqa: N803
    cle = await depot_cles.obtenir(ctx, cleId)
    exiger_confirmation(cle.nom, confirmation)
    await depot_cles.modifier(ctx, cleId, {"statut": "revoquee"})
    await journaliser(
        ctx, action="cle_ia.revocation", cible_type="cle_ia", cible_id=cleId, cible=cle.nom
    )
    return Response(status_code=204)


@router.post(
    "/ia/cles/{cleId}/rotation", response_model=m.CleIASecret, response_model_exclude_none=True
)
async def rotationner_cle_ia(
    cleId: str, ctx: Contexte = Depends(exige("ia.key.manage"))
) -> Any:  # noqa: N803
    return await cles.rotation(ctx, cleId)
