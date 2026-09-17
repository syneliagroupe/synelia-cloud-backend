"""Identifiants : UUID v7, triables, sans coordination."""

from __future__ import annotations

import secrets

from uuid_utils import uuid7


def nouvel_id() -> str:
    return str(uuid7())


def jeton_opaque(octets: int = 32) -> str:
    return secrets.token_urlsafe(octets)


def prefixe_lisible(prefixe: str = "syn") -> str:
    return f"{prefixe}_{secrets.token_hex(4)}"


def slug_court(id_: str) -> str:
    """Nom court, mais réellement unique, dérivé d'un UUIDv7 (`nouvel_id()`) : ses 8 premiers
    caractères hex n'encodent que les 32 bits de poids fort d'un timestamp milliseconde sur 48
    bits — **identiques pour tout ce qui est créé dans la même fenêtre d'environ 65 secondes**
    (2**16 ms), un cas fréquent en usage réel (plusieurs ressources créées coup sur coup). Bug
    vécu et corrigé une première fois pour `web_hebergement._slug_site` (deux sites installés à
    ~25 s d'intervalle avaient reçu le même nom `app-01a07704`, chacun écrasant la route Traefik
    de l'autre) : les 12 derniers caractères hex (sans tirets) tombent dans `rand_b`/`var` de
    l'UUIDv7, la partie réellement aléatoire, et restent donc uniques même à la même
    milliseconde. Toute nouvelle ressource qui doit un nom court réellement collision-safe à
    partir d'un `nouvel_id()` doit passer par ici plutôt que retronquer naïvement l'id."""
    return id_.replace("-", "")[-12:]
