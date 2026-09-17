"""Argent : entiers en FCFA hors taxes. Jamais de flottant."""

from __future__ import annotations

TVA_CI_PCT = 18
DEVISE = "XOF"


def tva(montant_ht: int, taux_pct: int = TVA_CI_PCT) -> int:
    return round(montant_ht * taux_pct / 100)


def ttc(montant_ht: int, taux_pct: int = TVA_CI_PCT) -> int:
    return montant_ht + tva(montant_ht, taux_pct)


def heures_vers_mois(prix_heure: int, heures: int = 730) -> int:
    return prix_heure * heures


def arrondi_fcfa(valeur: float) -> int:
    return round(valeur)
