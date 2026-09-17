"""Clés d'accès IA (`CleIA`) : allowlist de modèles, débit, quota de jetons, budget mensuel et
résidence des données — un contrat réellement appliqué, pas un affichage.

Débit (`debitMaxParMinute`) : seau à jetons en mémoire, une entrée par clé — même mécanique que
`synelia.deps.limitation`. ponytail : plafond process-local ; avec `SYNELIA_API_WORKERS=N`
(étape 1.7 de docs/PLAN-ARCHITECTURE-SUITE.md), chaque worker uvicorn a son propre seau, donc
la limite effective est ×N. `SYNELIA_VALKEY_URL` existe déjà dans la configuration pour un seau
partagé le jour où ça compte réellement.

Quota (`quotaJetonsMois`) et budget (`budgetMensuel`) : compteurs réels, incrémentés après
chaque appel réel, remis à zéro au changement de mois (comparaison de période stockée dans les
secrets chiffrés de la ressource — jamais dans `donnees`, qui est réécrit en entier à chaque
`modifier()`). `auDepassement` décide : `bloquer` refuse l'appel (402), `alerter` le laisse
passer et journalise un événement d'audit dédié."""

from __future__ import annotations

from collections import defaultdict
from time import monotonic
from typing import Any

from synelia_contract import modeles as m
from synelia_db.modeles import Ressource
from synelia_kernel import erreurs
from synelia_kernel.chiffrement import dechiffrer
from synelia_kernel.dates import maintenant
from synelia_kernel.ids import jeton_opaque, nouvel_id, prefixe_lisible

from synelia.audit import journaliser
from synelia.depot import Depot
from synelia.deps.contexte import Contexte
from synelia.securite import hacher_jeton

depot_cles = Depot("cle_ia", m.CleIA, libelle="Clé IA", champs_recherche=("nom", "usage"))

ENTETE_CLE_IA = "X-Cle-IA"

# ponytail : seau à jetons en mémoire, voir note de module.
_SEAUX: dict[str, tuple[float, float]] = defaultdict(lambda: (0.0, 0.0))


def _nouveau_secret(prefixe: str | None = None) -> tuple[str, str]:
    prefixe = prefixe or prefixe_lisible("cia")
    return prefixe, f"{prefixe}.{jeton_opaque()}"


async def creer(ctx: Contexte, corps: m.CleIACreation) -> dict[str, Any]:
    prefixe, secret = _nouveau_secret()
    cle = m.CleIA(
        id=nouvel_id(),
        nom=corps.nom,
        prefixe=prefixe,
        espaceId=corps.espaceId,
        usage=corps.usage or "",
        modelesAutorises=list(corps.modelesAutorises or ["tous"]),
        quotaJetonsMois=corps.quotaJetonsMois if corps.quotaJetonsMois is not None else 1_000_000,
        jetonsConsommes=0,
        debitMaxParMinute=corps.debitMaxParMinute if corps.debitMaxParMinute is not None else 60,
        budgetMensuel=corps.budgetMensuel or 0,
        budgetConsomme=0,
        auDepassement=corps.auDepassement or "bloquer",
        residenceMax=corps.residenceMax or "interne",
        statut="active",
        creeeLe=maintenant(),
        creeePar=ctx.utilisateur_id,
    )
    cle = await depot_cles.creer(ctx, cle)
    await depot_cles.definir_secrets(
        ctx, cle.id, {"secret_hash": hacher_jeton(secret), "periode": _periode_courante()}
    )
    await journaliser(
        ctx, action="ia.cle_creation", cible_type="cle_ia", cible_id=cle.id, cible=cle.nom
    )
    return {"cle": cle, "secret": secret}


async def rotation(ctx: Contexte, cle_id: str) -> dict[str, Any]:
    """Invalide l'ancien secret et en émet un nouveau, sans toucher au quota, au budget ni au
    statut de la clé — même geste que `POST /securite/cles-api/{cleId}/rotation`, en plus simple :
    une clé IA n'a qu'un seul secret vivant à la fois, donc pas de délai de grâce à gérer."""
    cle = await depot_cles.obtenir(ctx, cle_id)
    if cle.statut != "active":
        raise erreurs.conflit("Seule une clé active peut être tournée.", code="cle_ia_non_active")
    _, secret = _nouveau_secret(cle.prefixe)
    await depot_cles.definir_secrets(ctx, cle_id, {"secret_hash": hacher_jeton(secret)})
    await journaliser(
        ctx, action="ia.cle_rotation", cible_type="cle_ia", cible_id=cle_id, cible=cle.nom
    )
    return {"cle": cle, "secret": secret}


def _periode_courante() -> str:
    return maintenant().strftime("%Y-%m")


async def cle_depuis_entete(ctx: Contexte, secret: str | None) -> tuple[Ressource, m.CleIA] | None:
    """`None` si aucun en-tête `X-Cle-IA` fourni — comportement historique inchangé. Sinon la
    clé doit exister, être active, sans quoi c'est un 401 franc (jamais un appel silencieux
    sans application des quotas)."""
    if not secret:
        return None
    hash_ = hacher_jeton(secret)
    for r in await depot_cles.lignes(ctx):
        if (r.secrets or {}).get("secret_hash") is None:
            continue
        try:
            if dechiffrer(r.secrets["secret_hash"]) != hash_:
                continue
        except Exception:  # noqa: BLE001, S112
            continue
        if r.statut != "active":
            raise erreurs.non_authentifie("Clé IA révoquée ou suspendue.")
        return r, m.CleIA.model_validate(r.donnees)
    raise erreurs.non_authentifie("Clé IA inconnue.")


def _modele_autorise(cle: m.CleIA, slug: str) -> bool:
    autorises = cle.modelesAutorises or []
    return "tous" in autorises or slug in autorises


def _residence_ok(cle: m.CleIA, hebergement_modele: str) -> bool:
    if hebergement_modele == "souverain":
        return True
    return cle.residenceMax in ("publique", "interne")


def _debit_ok(cle_id: str, plafond_par_min: int) -> bool:
    capacite = float(max(plafond_par_min, 1))
    debit_par_s = capacite / 60.0
    jetons, dernier = _SEAUX[cle_id]
    now = monotonic()
    jetons = min(capacite, jetons + (now - dernier) * debit_par_s) if dernier else capacite
    if jetons < 1:
        _SEAUX[cle_id] = (jetons, now)
        return False
    _SEAUX[cle_id] = (jetons - 1, now)
    return True


async def _reinitialiser_si_nouvelle_periode(
    ctx: Contexte, ressource: Ressource, cle: m.CleIA
) -> m.CleIA:
    periode = (ressource.secrets or {}).get("periode")
    try:
        periode_dechiffree = dechiffrer(periode) if periode else None
    except Exception:  # noqa: BLE001
        periode_dechiffree = None
    if periode_dechiffree == _periode_courante():
        return cle
    cle = await depot_cles.modifier(ctx, cle.id, {"jetonsConsommes": 0, "budgetConsomme": 0})
    await depot_cles.definir_secrets(
        ctx, cle.id, {"periode": _periode_courante(), "reste_fcfa": "0"}
    )
    return cle


async def verifier_et_appliquer(
    ctx: Contexte, secret: str | None, *, slug_modele: str, hebergement_modele: str
) -> m.CleIA | None:
    """`None` si pas de clé fournie. Sinon applique l'allowlist, la résidence, le débit puis le
    quota/budget (dans cet ordre : refuser un appel non autorisé ou hors résidence avant même de
    regarder son coût). Lève une `AppError` sur tout refus."""
    trouve = await cle_depuis_entete(ctx, secret)
    if trouve is None:
        return None
    ressource, cle = trouve

    if not _modele_autorise(cle, slug_modele):
        raise erreurs.interdit(
            f"Cette clé IA n'autorise pas le modèle « {slug_modele} ».", code="ia_modele_non_autorise"
        )
    if not _residence_ok(cle, hebergement_modele):
        raise erreurs.interdit(
            f"Résidence maximale de cette clé (« {cle.residenceMax} ») dépassée par un modèle "
            f"hébergé « {hebergement_modele} ».",
            code="ia_residence_depassee",
        )
    if not _debit_ok(cle.id, cle.debitMaxParMinute):
        raise erreurs.trop_de_requetes()

    cle = await _reinitialiser_si_nouvelle_periode(ctx, ressource, cle)
    depasse = cle.jetonsConsommes >= cle.quotaJetonsMois or (
        cle.budgetMensuel > 0 and cle.budgetConsomme >= cle.budgetMensuel
    )
    if depasse:
        if cle.auDepassement == "bloquer":
            raise erreurs.quota_depasse("Quota de jetons ou budget mensuel de cette clé IA dépassé.")
        await journaliser(
            ctx,
            action="ia.cle_depassement_alerte",
            cible_type="cle_ia",
            cible_id=cle.id,
            cible=cle.nom,
            resultat="alerte",
            details={
                "jetonsConsommes": cle.jetonsConsommes,
                "quotaJetonsMois": cle.quotaJetonsMois,
                "budgetConsomme": cle.budgetConsomme,
                "budgetMensuel": cle.budgetMensuel,
            },
        )
    return cle


async def crediter_apres_appel(
    ctx: Contexte, cle: m.CleIA, *, jetons: int, cout_fcfa: float
) -> None:
    """`budgetConsomme` reste un entier FCFA — convention de toute la plateforme (voir
    `facturation/tarification.py`, « estimation en FCFA entiers »). Mais `coutFcfa` d'un appel
    unique est fractionnaire : un modèle bon marché (deepseek…) facturé sous le FCFA arrondirait
    systématiquement à zéro et ne ferait jamais progresser `budgetConsomme`, ce qui rendrait un
    plafond `bloquer` inatteignable. Le reste fractionnaire de chaque appel est donc reporté sur
    le suivant (dans les secrets chiffrés de la ressource, comme `periode`) plutôt qu'arrondi et
    perdu — l'entier crédité ici reste exact en cumul, seul l'affichage est entier."""
    secrets = await depot_cles.secrets(ctx, cle.id)
    reste = float(secrets.get("reste_fcfa") or 0.0)
    total = reste + max(cout_fcfa, 0.0)
    increment, nouveau_reste = int(total), total % 1
    await depot_cles.modifier(
        ctx,
        cle.id,
        {
            "jetonsConsommes": cle.jetonsConsommes + max(jetons, 0),
            "budgetConsomme": cle.budgetConsomme + increment,
            "derniereUtilisation": maintenant(),
        },
    )
    await depot_cles.definir_secrets(ctx, cle.id, {"reste_fcfa": str(nouveau_reste)})
