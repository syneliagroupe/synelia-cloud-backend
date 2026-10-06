"""Clés SSH publiques du compte, stockées dans `Utilisateur.preferences["cles_ssh"]`."""

from __future__ import annotations

import base64
import hashlib
from typing import Any

TYPES = (
    "ssh-ed25519",
    "ssh-rsa",
    "ecdsa-sha2-nistp256",
    "ecdsa-sha2-nistp384",
    "ecdsa-sha2-nistp521",
)


def analyser(publique: str) -> tuple[str, str, str]:
    """`(ligne normalisée, type, empreinte SHA256)` ; `ValueError` si la clé est invalide."""
    parts = publique.strip().split()
    if len(parts) < 2 or parts[0] not in TYPES:
        raise ValueError("Clé publique SSH attendue (ssh-ed25519, ssh-rsa, ecdsa-…).")
    try:
        brut = base64.b64decode(parts[1], validate=True)
    except ValueError as e:
        raise ValueError("Clé publique SSH illisible (base64 invalide).") from e
    if not brut.startswith(len(parts[0]).to_bytes(4, "big") + parts[0].encode()):
        raise ValueError("Le type annoncé ne correspond pas au contenu de la clé.")
    empreinte = "SHA256:" + base64.b64encode(hashlib.sha256(brut).digest()).decode().rstrip("=")
    return f"{parts[0]} {parts[1]}", parts[0], empreinte


def lister(preferences: dict[str, Any] | None) -> list[dict[str, Any]]:
    return list((preferences or {}).get("cles_ssh") or [])
