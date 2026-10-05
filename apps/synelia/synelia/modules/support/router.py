"""Support client : base de connaissances, pièces jointes, tickets."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, status
from synelia_contract import modeles as m
from synelia_kernel import erreurs
from synelia_kernel.dates import maintenant
from synelia_kernel.ids import nouvel_id

from synelia.audit import journaliser
from synelia.deps import Ctx, CtxPublic, Page, pagine
from synelia.modules.admin.service import sla_restant_min
from synelia.modules.support.service import ARTICLES_KB, detenteur_pieces, detenteur_tickets

router = APIRouter(prefix="/support", tags=["Support client"])

_SLA = {
    "critique": {"premiereReponseMin": 30, "resolutionMin": 240},
    "majeure": {"premiereReponseMin": 120, "resolutionMin": 480},
    "mineure": {"premiereReponseMin": 480, "resolutionMin": 2880},
    "question": {"premiereReponseMin": 1440, "resolutionMin": 5760},
}


def _vue(t: m.Ticket) -> dict[str, Any]:
    d = t.model_dump(mode="json")
    d["slaRestantMin"] = sla_restant_min(d)
    return d


def _nouveau_numero() -> str:
    return f"T-{maintenant().strftime('%Y%m')}-{nouvel_id()[-4:].upper()}"


@router.get(
    "/base-connaissances",
    response_model=m.SupportBaseConnaissancesGetResponse,
    response_model_exclude_none=True,
)
async def lister_articles_kb(page: Page, ctx: CtxPublic, categorie: str | None = None) -> Any:
    data = [a for a in ARTICLES_KB if (not categorie or a["categorie"] == categorie)]
    return pagine(data, len(data), page)


@router.get(
    "/base-connaissances/{articleId}", response_model=m.ArticleKb, response_model_exclude_none=True
)
async def obtenir_article_kb(articleId: str, ctx: CtxPublic) -> Any:  # noqa: N803
    a = next((x for x in ARTICLES_KB if x["id"] == articleId), None)
    if a is None:
        raise erreurs.introuvable("Article de la base de connaissances", articleId)
    return a


@router.post(
    "/pieces",
    response_model=m.SupportPiecesPostResponse,
    status_code=status.HTTP_201_CREATED,
    response_model_exclude_none=True,
)
async def televerser_piece_jointe(corps: m.SupportPiecesPostRequest, ctx: Ctx) -> Any:
    from synelia_kernel.erreurs import validation

    if corps.tailleOctets > 10 * 1024 * 1024 or len(corps.contenuBase64) > (
        10 * 1024 * 1024 * 4 // 3 + 64
    ):
        raise validation("Pièce jointe trop volumineuse (maximum 10 Mo).", {"piece": "10 Mo max"})
    piece = m.SupportPiecesPostResponse(id=nouvel_id(), nom=corps.nom, url=None, expire=None)
    await detenteur_pieces.creer(ctx, piece)
    await journaliser(
        ctx, action="support.piece", cible_type="ticket_piece", cible_id=piece.id, cible=corps.nom
    )
    return piece


@router.get(
    "/tickets", response_model=m.SupportTicketsGetResponse, response_model_exclude_none=True
)
async def lister_tickets(
    page: Page,
    ctx: Ctx,
    statut: str | None = None,
    gravite: str | None = None,
    ressourceId: str | None = None,
) -> Any:  # noqa: N803
    items = await detenteur_tickets.tous(
        ctx,
        filtre=lambda t: (
            (not statut or t.statut == statut)
            and (not gravite or t.gravite == gravite)
            and (not ressourceId or ressourceId in t.ressourcesLiees)
        ),
    )
    return pagine([_vue(t) for t in items], len(items), page)


@router.post(
    "/tickets",
    response_model=m.Ticket,
    status_code=status.HTTP_201_CREATED,
    response_model_exclude_none=True,
)
async def creer_ticket(corps: m.TicketCreation, ctx: Ctx) -> Any:
    numero = _nouveau_numero()
    now = maintenant()
    t = m.Ticket(
        id=nouvel_id(),
        orgId=ctx.org_id,
        numero=numero,
        sujet=corps.sujet,
        gravite=corps.gravite,
        statut="ouvert",
        slaCible=m.SlaCible(**_SLA[corps.gravite]),
        slaRestantMin=_SLA[corps.gravite]["premiereReponseMin"],
        ressourcesLiees=list(corps.ressourcesLiees or []),
        service=corps.service,
        assigneA=None,
        createdAt=now,
        messages=[
            m.Message(
                auteur="Client",
                role="client",
                date=now,
                contenu=corps.contenu,
                pieces=list(corps.pieces or []),
            )
        ],
    )
    await detenteur_tickets.creer(ctx, t)
    await journaliser(
        ctx, action="support.ticket", cible_type="ticket", cible_id=t.id, cible=t.numero
    )
    return t.model_dump(mode="json")


@router.get("/tickets/{ticketId}", response_model=m.Ticket, response_model_exclude_none=True)
async def obtenir_ticket(ticketId: str, ctx: Ctx) -> Any:  # noqa: N803
    t = await detenteur_tickets.obtenir(ctx, ticketId)
    return _vue(t)


@router.patch("/tickets/{ticketId}", response_model=m.Ticket, response_model_exclude_none=True)
async def modifier_ticket(
    ticketId: str, corps: m.SupportTicketsTicketIdPatchRequest, ctx: Ctx
) -> Any:  # noqa: N803
    await detenteur_tickets.obtenir(ctx, ticketId)
    await detenteur_tickets.modifier(ctx, ticketId, corps)
    await journaliser(
        ctx, action="support.ticket.modification", cible_type="ticket", cible_id=ticketId
    )
    t = await detenteur_tickets.obtenir(ctx, ticketId)
    return _vue(t)


@router.post(
    "/tickets/{ticketId}/escalade", response_model=m.Ticket, response_model_exclude_none=True
)
async def escalader_ticket(
    ticketId: str, corps: m.SupportTicketsTicketIdEscaladePostRequest, ctx: Ctx
) -> Any:  # noqa: N803
    t = await detenteur_tickets.obtenir(ctx, ticketId)
    if any(msg.contenu.startswith("Escalade demandée") for msg in t.messages):
        raise erreurs.conflit("Ce ticket a déjà été escaladé.", code="deja_escalade")
    msg = m.Message(
        auteur="Client",
        role="client",
        date=maintenant(),
        contenu=f"Escalade demandée : {corps.motif}",
        pieces=None,
    )
    updated = t.model_copy(update={"messages": [*t.messages, msg]})
    await detenteur_tickets.remplacer(ctx, ticketId, updated)
    await journaliser(
        ctx,
        action="support.ticket.escalade",
        cible_type="ticket",
        cible_id=ticketId,
        cible=t.numero,
    )
    tt = await detenteur_tickets.obtenir(ctx, ticketId)
    return _vue(tt)


@router.post(
    "/tickets/{ticketId}/messages",
    response_model=m.Ticket,
    status_code=status.HTTP_201_CREATED,
    response_model_exclude_none=True,
)
async def repondre_ticket(ticketId: str, corps: m.MessageTicket, ctx: Ctx) -> Any:  # noqa: N803
    t = await detenteur_tickets.obtenir(ctx, ticketId)
    msg = m.Message(
        auteur="Client",
        role="client",
        date=maintenant(),
        contenu=corps.contenu,
        pieces=list(corps.pieces or []),
    )
    messages = [*t.messages, msg]
    statut = "resolu" if corps.cloture else t.statut
    updated = t.model_copy(update={"messages": messages, "statut": statut})
    await detenteur_tickets.remplacer(ctx, ticketId, updated)
    await journaliser(
        ctx, action="support.ticket.message", cible_type="ticket", cible_id=ticketId, cible=t.numero
    )
    tt = await detenteur_tickets.obtenir(ctx, ticketId)
    return _vue(tt)
