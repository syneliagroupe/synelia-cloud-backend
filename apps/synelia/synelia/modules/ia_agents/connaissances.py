"""Bases de connaissances : ingestion réelle (Docling → découpage → BGE-M3 → Qdrant) et
recherche. Réel seulement si `SYNELIA_QDRANT_URL` est défini (comme le reste du socle : un
seul signal, les autres variables — Docling, Infinity — ont des valeurs par défaut adaptées
à docker-compose). Sans cette variable, tout reste simulé et sans réseau : les fragments d'un
document restent en clair (chiffrés au repos) sur la ressource, la recherche se fait par
recouvrement de mots — jamais de faux vecteur qui ferait croire à une vraie similarité.

Seul le mode de découpage `general` est réellement implémenté (paragraphes → fenêtres de mots
avec chevauchement). `parent_enfant` et `qr` se figent au choix mais retombent sur `general` :
un TODO honnête plutôt qu'une fausse spécialisation."""

from __future__ import annotations

import json
import os
from typing import Any

import httpx
from synelia_contract import modeles as m
from synelia_db.modeles import Travail
from synelia_kernel import erreurs
from synelia_kernel.chiffrement import dechiffrer
from synelia_kernel.dates import maintenant

from synelia.depot import Depot
from synelia.deps.contexte import Contexte
from synelia.travaux import Executeur, executeur

depot_connaissance = Depot(
    "connaissance_ia",
    m.BaseConnaissance,
    libelle="Base de connaissances",
    champs_recherche=("nom",),
)

ENV_DOCLING_URL = "SYNELIA_DOCLING_URL"
ENV_EMBEDDINGS_URL = "SYNELIA_EMBEDDINGS_URL"
ENV_EMBEDDINGS_MODELE = "SYNELIA_EMBEDDINGS_MODELE"
ENV_QDRANT_URL = "SYNELIA_QDRANT_URL"

MODELE_EMBEDDING_DEFAUT = "BAAI/bge-m3"
SLUG_MODELE_EMBEDDING = "synelia/bge-m3"


def _reel() -> bool:
    return bool(os.environ.get(ENV_QDRANT_URL))


def _docling_url() -> str:
    return os.environ.get(ENV_DOCLING_URL, "http://docling:5001").rstrip("/")


def _embeddings_url() -> str:
    return os.environ.get(ENV_EMBEDDINGS_URL, "http://infinity:7997").rstrip("/")


def _embeddings_modele() -> str:
    return os.environ.get(ENV_EMBEDDINGS_MODELE, MODELE_EMBEDDING_DEFAUT)


def _qdrant_url() -> str:
    return os.environ.get(ENV_QDRANT_URL, "http://qdrant:6333").rstrip("/")


def nom_collection(connaissance_id: str) -> str:
    return f"kb_{connaissance_id}"


# ─── Découpage (mode `general`) ────────────────────────────────────────


def decouper_general(texte: str, taille_mots: int = 220, chevauchement_mots: int = 40) -> list[str]:
    """Fenêtres de mots avec chevauchement — déterministe, pas de dépendance à un tokenizer."""
    mots = texte.split()
    if not mots:
        return []
    pas = max(taille_mots - chevauchement_mots, 1)
    fragments = []
    for depart in range(0, len(mots), pas):
        fragment = " ".join(mots[depart : depart + taille_mots])
        if fragment:
            fragments.append(fragment)
        if depart + taille_mots >= len(mots):
            break
    return fragments


# ─── Docling (extraction) ──────────────────────────────────────────────


async def extraire_texte(
    nom_fichier: str, *, texte: str | None = None, contenu_base64: str | None = None, url: str | None = None
) -> str:
    if texte:
        return texte
    if not _reel():
        if contenu_base64:
            import base64

            try:
                return base64.b64decode(contenu_base64).decode("utf-8", errors="ignore")
            except Exception:  # noqa: BLE001
                return ""
        return f"[extraction simulée de {url or nom_fichier} — SYNELIA_QDRANT_URL absent]"

    source: dict[str, Any] = (
        {"kind": "http", "url": url}
        if url
        else {"kind": "file", "base64_string": contenu_base64 or "", "filename": nom_fichier}
    )
    corps = {"options": {"to_formats": ["md"]}, "sources": [source]}
    try:
        async with httpx.AsyncClient(base_url=_docling_url(), timeout=180) as client:
            r = await client.post("/v1/convert/source", json=corps)
    except httpx.HTTPError as exc:
        raise erreurs.amont_indisponible("docling", str(exc)) from exc
    if r.status_code >= 400:
        raise erreurs.amont_indisponible("docling", f"HTTP {r.status_code} : {r.text[:200]}")
    donnees = r.json()
    if donnees.get("status") not in ("success", "partial_success"):
        erreurs_docling = donnees.get("errors") or []
        raise erreurs.amont_indisponible(
            "docling", f"Extraction échouée : {erreurs_docling[:1] or 'raison inconnue'}"
        )
    return donnees["document"]["md_content"] or ""


# ─── Infinity (embeddings BGE-M3, API compatible OpenAI) ──────────────


async def embeddings(textes: list[str]) -> tuple[list[list[float]], int]:
    if not textes:
        return [], 0
    if not _reel():
        raise erreurs.non_porte(
            "Aucun vecteur simulé n'est produit ici — la recherche simulée compare le texte "
            "brut, jamais un faux embedding."
        )
    try:
        async with httpx.AsyncClient(base_url=_embeddings_url(), timeout=120) as client:
            r = await client.post(
                "/embeddings", json={"model": _embeddings_modele(), "input": textes}
            )
    except httpx.HTTPError as exc:
        raise erreurs.amont_indisponible("infinity", str(exc)) from exc
    if r.status_code >= 400:
        raise erreurs.amont_indisponible("infinity", f"HTTP {r.status_code} : {r.text[:200]}")
    donnees = r.json()
    vecteurs = [item["embedding"] for item in donnees["data"]]
    jetons = int((donnees.get("usage") or {}).get("total_tokens", 0))
    return vecteurs, jetons


# ─── Qdrant (index vectoriel) ──────────────────────────────────────────


async def _qdrant_assurer_collection(nom: str, dimension: int) -> None:
    async with httpx.AsyncClient(base_url=_qdrant_url(), timeout=30) as client:
        r = await client.get(f"/collections/{nom}")
        if r.status_code == 200:
            return
        r = await client.put(
            f"/collections/{nom}", json={"vectors": {"size": dimension, "distance": "Cosine"}}
        )
    if r.status_code >= 400:
        raise erreurs.amont_indisponible("qdrant", f"HTTP {r.status_code} : {r.text[:200]}")


async def _qdrant_upsert(nom: str, points: list[dict[str, Any]]) -> None:
    async with httpx.AsyncClient(base_url=_qdrant_url(), timeout=120) as client:
        r = await client.put(
            f"/collections/{nom}/points", params={"wait": "true"}, json={"points": points}
        )
    if r.status_code >= 400:
        raise erreurs.amont_indisponible("qdrant", f"HTTP {r.status_code} : {r.text[:200]}")


async def _qdrant_rechercher(nom: str, vecteur: list[float], top_k: int) -> list[dict[str, Any]]:
    async with httpx.AsyncClient(base_url=_qdrant_url(), timeout=30) as client:
        r = await client.post(
            f"/collections/{nom}/points/search",
            json={"vector": vecteur, "limit": top_k, "with_payload": True},
        )
    if r.status_code >= 400:
        raise erreurs.amont_indisponible("qdrant", f"HTTP {r.status_code} : {r.text[:200]}")
    return r.json()["result"]


async def supprimer_collection(nom: str) -> None:
    try:
        async with httpx.AsyncClient(base_url=_qdrant_url(), timeout=30) as client:
            await client.delete(f"/collections/{nom}")
    except httpx.HTTPError:
        pass  # nettoyage best-effort : la ressource est déjà supprimée côté Synelia


# ─── Fragments simulés (secrets chiffrés du Depot — survivent aux modifier()) ──


async def _fragments_simules(ctx: Contexte, connaissance_id: str) -> list[dict[str, str]]:
    r = await depot_connaissance.ligne(ctx, connaissance_id)
    if r is None:
        return []
    brut = (r.secrets or {}).get("fragments_simules")
    if not brut:
        return []
    try:
        return json.loads(dechiffrer(brut))
    except Exception:  # noqa: BLE001
        return []


async def _ajouter_fragments_simules(
    ctx: Contexte, connaissance_id: str, nouveaux: list[dict[str, str]]
) -> None:
    existants = await _fragments_simules(ctx, connaissance_id)
    existants.extend(nouveaux)
    await depot_connaissance.definir_secrets(
        ctx, connaissance_id, {"fragments_simules": json.dumps(existants)}
    )


# ─── Recherche ──────────────────────────────────────────────────────────


async def rechercher(
    ctx: Contexte, connaissance: m.BaseConnaissance, requete: str, top_k: int
) -> tuple[list[dict[str, Any]], int]:
    """`(fragments, jetons_utilises)` — jetons réels (Infinity) en mode réel, mots comptés sinon."""
    if _reel():
        (vecteur,), jetons = await embeddings([requete])
        resultats = await _qdrant_rechercher(nom_collection(connaissance.id), vecteur, top_k)
        fragments = [
            {
                "texte": r["payload"]["texte"],
                "score": round(float(r["score"]), 4),
                "document": r["payload"]["document"],
                **({"citations": r["payload"]["texte"][:280]} if connaissance.citations else {}),
            }
            for r in resultats
        ]
        return fragments, jetons

    fragments_dispo = await _fragments_simules(ctx, connaissance.id)
    # Mots de trois lettres ou moins exclus (articles, prépositions) : sans ce filtre, deux
    # textes sans aucun rapport se recouvrent toujours sur « le », « à », « de »…
    mots_requete = {mot for mot in requete.lower().split() if len(mot) > 3}
    notes = []
    for frag in fragments_dispo:
        recouvrement = len(mots_requete & set(frag["texte"].lower().split()))
        if recouvrement:
            notes.append((recouvrement, frag))
    notes.sort(key=lambda paire: -paire[0])
    denominateur = max(len(mots_requete), 1)
    fragments = [
        {
            "texte": frag["texte"],
            "score": round(min(recouvrement / denominateur, 1.0), 4),
            "document": frag["document"],
            **({"citations": frag["texte"][:280]} if connaissance.citations else {}),
        }
        for recouvrement, frag in notes[:top_k]
    ]
    return fragments, len(requete.split())


# ─── Ingestion (job réel : Docling → découpage → embeddings → Qdrant) ──


@executeur("connaissance.ingerer_document")
class ExecuteurIngestionDocument(Executeur):
    compensable = True

    async def etape(self, ctx: Contexte, travail: Travail, index: int, nom: str) -> str | None:
        entree = travail.entree or {}
        if index == 0:
            texte = await extraire_texte(
                entree.get("nom") or "document",
                texte=entree.get("texte"),
                contenu_base64=entree.get("contenuBase64"),
                url=entree.get("url"),
            )
            if not texte.strip():
                raise erreurs.validation("Le document n'a produit aucun texte exploitable.")
            travail.contexte = {**(travail.contexte or {}), "texte": texte}
            return f"{len(texte)} caractère(s) extrait(s)."

        connaissance = await depot_connaissance.obtenir(ctx, travail.cible_id or "")
        texte = dict(travail.contexte or {}).get("texte", "")
        fragments_texte = decouper_general(texte)
        if not fragments_texte:
            raise erreurs.validation("Aucun fragment à indexer : document vide après extraction.")
        nom_doc = entree.get("nom") or "document"

        if _reel():
            vecteurs, _jetons = await embeddings(fragments_texte)
            dimension = len(vecteurs[0])
            collection = nom_collection(connaissance.id)
            await _qdrant_assurer_collection(collection, dimension)
            base_idx = connaissance.fragments
            points = [
                {
                    "id": base_idx + i,
                    "vector": vecteurs[i],
                    "payload": {"texte": fragments_texte[i], "document": nom_doc},
                }
                for i in range(len(fragments_texte))
            ]
            await _qdrant_upsert(collection, points)
        else:
            dimension = 8
            await _ajouter_fragments_simules(
                ctx,
                connaissance.id,
                [{"texte": t, "document": nom_doc} for t in fragments_texte],
            )
        travail.contexte = {
            **dict(travail.contexte or {}),
            "fragments": len(fragments_texte),
            "dimension": dimension,
        }
        return f"{len(fragments_texte)} fragment(s) indexé(s)."

    async def compenser(self, ctx: Contexte, travail: Travail, index_echoue: int) -> None:
        connaissance = await depot_connaissance.trouver(ctx, travail.cible_id or "")
        if connaissance is None:
            return
        message = (travail.erreur or {}).get("message", "Échec de l'ingestion.")
        await depot_connaissance.modifier(ctx, connaissance.id, {"statut": "erreur", "erreur": message})

    async def terminer(self, ctx: Contexte, travail: Travail) -> None:
        connaissance = await depot_connaissance.obtenir(ctx, travail.cible_id or "")
        ctxt = dict(travail.contexte or {})
        taille_mo = len(ctxt.get("texte", "").encode("utf-8")) / 1_000_000
        await depot_connaissance.modifier(
            ctx,
            connaissance.id,
            {
                "documents": connaissance.documents + 1,
                "fragments": connaissance.fragments + int(ctxt.get("fragments", 0)),
                "modeleEmbedding": _embeddings_modele() if _reel() else "simule",
                "dimension": int(ctxt.get("dimension") or connaissance.dimension or 0),
                "tailleMo": round((connaissance.tailleMo or 0) + taille_mo, 4),
                "derniereIndexation": maintenant(),
                "statut": "a_jour",
                "erreur": None,
            },
        )
