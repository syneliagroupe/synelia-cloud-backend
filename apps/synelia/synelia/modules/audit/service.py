"""Journal d'audit : projection des lignes `Audit` vers `EvenementAudit` du contrat, et export
réel (CSV/JSON) du journal, déposé dans MinIO et servi en téléchargement (`GET /audit/exports/{id}`)."""

from __future__ import annotations

import csv
import io
import json
import unicodedata
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from synelia_contract.rbac import ROLES_ORDRE
from synelia_db.modeles import Audit, Organisation, Travail, Utilisateur
from synelia_kernel import erreurs
from synelia_kernel.dates import depuis_iso, iso
from synelia_openstack.minio import choisir_minio

from synelia.audit import vers_contrat as vers_contrat_complet
from synelia.deps.contexte import Contexte
from synelia.travaux import Executeur, executeur

RESULTATS = {"succes": "ok", "ok": "ok", "refus": "refuse", "refuse": "refuse"}
RESULTATS_INVERSE = {"ok": ("succes", "ok"), "refuse": ("refus", "refuse")}

BUCKET_EXPORTS = "synelia-audit-exports"


def resultat_contrat(r: str | None) -> str:
    return RESULTATS.get(r or "", "erreur")


def utc(d: datetime | None) -> datetime | None:
    if d is None:
        return None
    return d.astimezone(UTC) if d.tzinfo else d.replace(tzinfo=UTC)


def vers_contrat(a: Audit, org_nom: str | None, noms: dict[str, str]) -> dict[str, Any]:
    details = a.details or {}
    acteur = a.acteur or "systeme"
    if acteur.startswith("cle:"):
        type_acteur = "api"
    elif a.acteur_id:
        type_acteur = "user"
    else:
        type_acteur = "systeme"
    role = details.get("role") if details.get("role") in ROLES_ORDRE else "org_admin"
    detail = details.get("message") or details.get("motif")
    if detail is None and details:
        detail = json.dumps(details, ensure_ascii=False, default=str)[:1000]
    return {
        "id": a.id,
        "ts": a.date,
        "orgId": a.org_id,
        "orgNom": org_nom,
        "actor": {
            "id": a.acteur_id or acteur,
            "nom": noms.get(a.acteur_id or "", acteur),
            "email": acteur if "@" in acteur else None,
            "type": type_acteur,
        },
        "role": role,
        "scope": {"type": "org", "id": a.org_id, "label": org_nom or a.org_id or "plateforme"},
        "action": a.action,
        "target": a.cible or (f"{a.cible_type}:{a.cible_id}" if a.cible_type else a.action),
        "result": resultat_contrat(a.resultat),
        "detail": detail,
        "ip": a.ip,
        # champs de recherche (ignorés par le modèle de réponse)
        "acteur": acteur,
    }


async def noms_acteurs(ctx: Contexte, lignes: list[Audit]) -> dict[str, str]:
    ids = {a.acteur_id for a in lignes if a.acteur_id}
    if not ids:
        return {}
    return {
        u.id: u.nom
        for u in (
            await ctx.session.execute(select(Utilisateur).where(Utilisateur.id.in_(ids)))
        ).scalars()
    }


COLONNES_EXPORT = (
    "id",
    "date",
    "acteur",
    "acteurId",
    "action",
    "cibleType",
    "cibleId",
    "cible",
    "resultat",
    "ip",
    "hashPrecedent",
    "hash",
)


def cle_export(org_id: str | None, travail_id: str, extension: str) -> str:
    return f"{org_id or 'plateforme'}/{travail_id}.{extension}"


def _generer_csv(lignes: list[Audit]) -> bytes:
    """Point-virgule et BOM, comme `src/lib/export.ts` côté frontend : ce qu'attend un tableur
    configuré en français. `hash`/`hashPrecedent` en dernières colonnes permettent à un tiers de
    revérifier la chaîne sans repasser par l'API (voir `synelia.audit.verifier_chaine`)."""
    tampon = io.StringIO()
    ecrivain = csv.writer(tampon, delimiter=";")
    ecrivain.writerow(COLONNES_EXPORT)
    for a in lignes:
        d = vers_contrat_complet(a)
        d["date"] = iso(d["date"]) if d.get("date") else ""
        ecrivain.writerow([d.get(c) if d.get(c) is not None else "" for c in COLONNES_EXPORT])
    return ("\ufeff" + tampon.getvalue()).encode("utf-8")


def _generer_json(lignes: list[Audit]) -> bytes:
    return json.dumps(
        [vers_contrat_complet(a) for a in lignes], ensure_ascii=False, default=str, indent=2
    ).encode("utf-8")


CONTENT_TYPES = {"csv": "text/csv; charset=utf-8", "json": "application/json"}


@executeur("audit.export")
class ExecuteurAuditExport(Executeur):
    """L'effet réel a lieu ici (`terminer`), pas dans les étapes simulées à durée fixe : on relit
    les lignes de la période, on produit un fichier réel, et on le dépose dans MinIO à la clé que
    `GET /audit/exports/{travailId}` sait retrouver."""

    async def terminer(self, ctx: Contexte, travail: Travail) -> None:
        entree = travail.entree or {}
        format_ = entree.get("format") or "csv"
        if format_ == "pdf":
            raise erreurs.non_porte(
                "L'export PDF signé n'est pas fabriqué par cette plateforme : demandez un CSV "
                "ou un JSON, tous deux vérifiables indépendamment (voir /audit/integrite)."
            )
        depuis = depuis_iso(entree["depuis"]) if entree.get("depuis") else None
        jusqua = depuis_iso(entree["jusqua"]) if entree.get("jusqua") else None
        q = select(Audit).where(Audit.org_id == travail.org_id)
        if depuis:
            q = q.where(Audit.date >= depuis)
        if jusqua:
            q = q.where(Audit.date <= jusqua)
        lignes = list((await ctx.session.execute(q.order_by(Audit.date))).scalars().all())
        contenu = _generer_csv(lignes) if format_ == "csv" else _generer_json(lignes)
        minio = choisir_minio()
        minio.creer_bucket(BUCKET_EXPORTS)
        minio.deposer_objet(
            BUCKET_EXPORTS,
            cle_export(travail.org_id, travail.id, format_),
            contenu,
            CONTENT_TYPES[format_],
        )


async def telecharger_export(ctx: Contexte, travail: Travail) -> tuple[bytes, str, str] | None:
    """Relit l'objet déposé par `ExecuteurAuditExport` pour ce travail. `None` si le travail n'est
    pas encore terminé (le client réessaiera) ou si le format demandé était le PDF, jamais produit."""
    format_ = (travail.entree or {}).get("format") or "csv"
    if format_ not in CONTENT_TYPES:
        return None
    minio = choisir_minio()
    contenu = minio.recuperer_objet(BUCKET_EXPORTS, cle_export(travail.org_id, travail.id, format_))
    if contenu is None:
        return None
    org = await ctx.session.get(Organisation, travail.org_id) if travail.org_id else None
    brut = org.nom if org else travail.org_id or "plateforme"
    # Un `Content-Disposition: filename="..."` n'est pas censé porter du texte hors ASCII : un nom
    # d'organisation accentué ("Synelia (démo)") corromprait l'en-tête pour un client strict.
    ascii_ = unicodedata.normalize("NFKD", brut).encode("ascii", "ignore").decode("ascii")
    slug = "".join(c if c.isalnum() else "-" for c in ascii_).strip("-")
    nom = f"audit-{slug or 'export'}-{travail.id[:8]}.{format_}"
    return contenu, CONTENT_TYPES[format_], nom
