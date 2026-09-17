"""Journal d'audit append-only, hash chaîné."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from synelia_db.modeles import Audit
from synelia_kernel.config import reglages
from synelia_kernel.dates import iso, maintenant
from synelia_kernel.journal import journal

if TYPE_CHECKING:
    from synelia.deps.contexte import Contexte

log = journal("audit")


def empreinte(precedent: str | None, org: str | None, ligne: Audit) -> str:
    """Empreinte SHA-256 d'une ligne, chaînée à l'empreinte précédente. Utilisée à l'écriture
    (`journaliser`) comme à la vérification (`verifier_chaine`) : les deux doivent recalculer
    exactement le même hash à partir des mêmes champs pour que la chaîne ait un sens."""
    charge = json.dumps(
        [
            precedent,
            org,
            iso(ligne.date),
            ligne.acteur,
            ligne.action,
            ligne.cible_type,
            ligne.cible_id,
            ligne.resultat,
            ligne.details or {},
        ],
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(charge.encode()).hexdigest()


async def journaliser(
    ctx: Contexte,
    *,
    action: str,
    cible_type: str | None = None,
    cible_id: str | None = None,
    cible: str | None = None,
    resultat: str = "succes",
    details: dict[str, Any] | None = None,
    org_id: str | None = None,
) -> Audit:
    p = ctx.principal
    org = org_id or (p.org_id if p else None)
    precedent = (
        await ctx.session.execute(
            select(Audit.hash).where(Audit.org_id == org).order_by(desc(Audit.date)).limit(1)
        )
    ).scalar_one_or_none()
    ligne = Audit(
        org_id=org,
        date=maintenant(),
        acteur_id=p.utilisateur_id if p else None,
        acteur=p.email if p else "systeme",
        action=action,
        cible_type=cible_type,
        cible_id=cible_id,
        cible=cible,
        resultat=resultat,
        ip=ctx.ip,
        correlation_id=ctx.correlation_id,
        details=details or {},
        hash_precedent=precedent,
    )
    ligne.hash = empreinte(precedent, org, ligne)
    ctx.session.add(ligne)
    await ctx.session.flush()
    return ligne


async def verifier_chaine(ctx: Contexte, org_id: str | None = None) -> dict[str, Any]:
    """Rejoue la chaîne de hachage d'une organisation (par date croissante) et recalcule chaque
    empreinte à partir des champs enregistrés : une ligne modifiée, supprimée ou insérée hors
    séquence casse la chaîne à partir de ce point, et c'est immédiatement détectable — c'est tout
    l'intérêt d'un hash chaîné plutôt qu'une simple empreinte par ligne.

    Périmètre exact de cette garantie : détecte toute altération faite **sans** le droit de
    réécrire la chaîne — c'est-à-dire tout ce que peut faire le rôle applicatif `synelia_app`
    (`SELECT, INSERT` seulement depuis §3 de `docs/PLAN-ARCHITECTURE-SUITE.md` — une insertion
    hors séquence reste exclue de sa portée) et toute corruption accidentelle. Ne prouve rien
    contre un acteur disposant du superutilisateur Postgres ou de l'hôte : celui-ci peut
    recalculer la chaîne entière après coup. Contre lui, seule la comparaison avec un ancrage
    externe fait foi (`ancrer`, journal structuré `audit.ancrage` / courriel quotidien, hors du
    rôle applicatif — voir `apps/synelia/synelia/travaux/local.py::ancrage_quotidien`)."""
    p = ctx.principal
    org = org_id or (p.org_id if p else None)
    lignes = (
        (await ctx.session.execute(select(Audit).where(Audit.org_id == org).order_by(Audit.date)))
        .scalars()
        .all()
    )
    precedent: str | None = None
    for n, ligne in enumerate(lignes, start=1):
        if ligne.hash_precedent != precedent:
            return {
                "intacte": False,
                "entreesVerifiees": n - 1,
                "totalEntrees": len(lignes),
                "ruptureId": ligne.id,
                "ruptureDate": ligne.date,
                "raison": "hash_precedent ne correspond pas à l'empreinte de la ligne antérieure",
            }
        attendu = empreinte(precedent, org, ligne)
        if ligne.hash != attendu:
            return {
                "intacte": False,
                "entreesVerifiees": n - 1,
                "totalEntrees": len(lignes),
                "ruptureId": ligne.id,
                "ruptureDate": ligne.date,
                "raison": "empreinte recalculée différente de l'empreinte enregistrée",
            }
        precedent = ligne.hash
    return {
        "intacte": True,
        "entreesVerifiees": len(lignes),
        "totalEntrees": len(lignes),
        "ruptureId": None,
        "ruptureDate": None,
        "raison": None,
        "empreinteFinale": precedent,
    }


def vers_contrat(a: Audit) -> dict[str, Any]:
    return {
        "id": a.id,
        "orgId": a.org_id,
        "date": a.date,
        "acteur": a.acteur,
        "acteurId": a.acteur_id,
        "action": a.action,
        "cible": a.cible or (f"{a.cible_type}:{a.cible_id}" if a.cible_type else None),
        "cibleType": a.cible_type,
        "cibleId": a.cible_id,
        "resultat": a.resultat,
        "ip": a.ip,
        "correlationId": a.correlation_id,
        "details": a.details or {},
        "hashPrecedent": a.hash_precedent,
        "hash": a.hash,
    }


async def tetes_de_chaine(session: AsyncSession) -> list[dict[str, Any]]:
    """Une ligne par organisation (plus la plateforme, `org_id` NULL) : nombre de lignes et
    empreinte de la plus récente — même ordre que `journaliser` (`order_by(desc(date)).limit(1)`
    par org). Base de l'ancrage quotidien hors-rôle (`ancrer`, §3 de
    `docs/PLAN-ARCHITECTURE-SUITE.md`) : la valeur de `empreinteDeTete` d'une organisation doit
    toujours égaler `empreinteFinale` de `verifier_chaine` pour cette même organisation."""
    comptes = (
        await session.execute(select(Audit.org_id, func.count()).group_by(Audit.org_id))
    ).all()
    tetes: list[dict[str, Any]] = []
    for org_id, n in comptes:
        empreinte_tete = (
            await session.execute(
                select(Audit.hash)
                .where(Audit.org_id == org_id)
                .order_by(desc(Audit.date))
                .limit(1)
            )
        ).scalar_one_or_none()
        tetes.append({"orgId": org_id, "nombreDeLignes": n, "empreinteDeTete": empreinte_tete})
    return tetes


async def ancrer(session: AsyncSession) -> None:
    """Ancrage quotidien, hors du rôle Postgres applicatif (`synelia_app` n'a que
    `SELECT, INSERT` sur `audit` depuis §3 du plan d'architecture) : émis (i) toujours en ligne
    de journal structuré — les logs Docker sont écrits par le démon sous root, hors de portée du
    rôle Postgres et de l'utilisateur `synelia` du conteneur — et (ii) par courriel si
    `SYNELIA_AUDIT_ANCRAGE_EMAIL` est configurée. Best-effort comme le reste de
    `synelia_kernel.courriel` : une panne d'envoi (délivrabilité externe depuis dev01 incertaine,
    cf. mémoire SPF/DKIM Zimbra) est journalisée, jamais levée — le journal structuré reste
    l'ancrage minimal garanti, indépendant du courriel. Pas de nouvelle ligne `audit` pour
    l'ancrage lui-même (ce serait circulaire)."""
    tetes = await tetes_de_chaine(session)
    log.info("audit.ancrage", tetes=tetes)
    email = reglages().audit_ancrage_email
    if not email:
        return
    from synelia_kernel import courriel

    paragraphes = [
        f"{t['orgId'] or 'plateforme'} · {t['nombreDeLignes']} lignes · {t['empreinteDeTete']}"
        for t in tetes
    ]
    try:
        await courriel.envoyer(
            email,
            sujet=f"[Synelia] Ancrage du journal d'audit — {iso(maintenant())[:10]}",
            titre="Ancrage quotidien du journal d'audit",
            paragraphes=paragraphes,
        )
    except Exception:  # noqa: BLE001
        log.warning("audit.ancrage_courriel_echoue", exc_info=True)
