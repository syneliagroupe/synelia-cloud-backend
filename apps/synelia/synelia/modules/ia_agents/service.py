"""IA & Agents (MVP) : catalogue de modèles + agents invoqués via LiteLLM/OpenRouter.

Un seul aller-retour par invocation : consigne système + message de l'appelant, une réponse.
Pas de mémoire de conversation, pas d'anonymisation, pas de journal d'usage détaillé — laissés
pour une itération suivante. Le catalogue est scellé (`plateforme=True`) : les modèles ne
dépendent d'aucune organisation, seuls les agents en dépendent."""

from __future__ import annotations

import os
import time
from typing import Any

import httpx
from synelia_contract import modeles as m
from synelia_db.modeles import Organisation, Ressource, Utilisateur
from synelia_kernel import erreurs
from synelia_kernel.dates import maintenant

from synelia.demo import peupleur
from synelia.depot import Depot
from synelia.deps.contexte import Contexte

depot_modeles = Depot(
    "modele_ia",
    m.ModeleIA,
    plateforme=True,
    libelle="Modèle IA",
    champs_recherche=("nom", "slug", "editeur"),
)
depot_agents = Depot(
    "agent_ia", m.AgentIA, libelle="Agent IA", champs_recherche=("nom", "modele")
)

# Taux de change indicatif, l'API OpenRouter facture en USD. Pas d'appel FX en direct pour ce MVP.
TAUX_USD_XOF = 610

ENV_URL = "SYNELIA_LITELLM_URL"
ENV_CLE = "SYNELIA_LITELLM_MASTER_KEY"

# ─── Catalogue seedé ───────────────────────────────────────────────────
# Les quatre premiers sont confirmés réels sur OpenRouter (voir `litellm/config.yaml`).
# Les quatre suivants sont des marques du catalogue marketing sans équivalent chat
# OpenRouter confirmé (embedding/reranker/transcription/vision) : non invocables.
SEMENCES: list[m.ModeleIA] = [
    m.ModeleIA(
        id="m-llama-70b",
        slug="meta-llama/llama-3.3-70b-instruct",
        nom="Llama 3.3 70B Instruct",
        editeur="Meta",
        famille="texte",
        hebergement="externe",
        residence="Passerelle OpenRouter — fournisseur variable selon la route.",
        licence="Llama 3.3 Community",
        contexteJetons=128_000,
        prixEntree=420,
        prixSortie=840,
        unite="jeton",
        statut="disponible",
        usages=["Rédaction", "Synthèse de documents", "Support de niveau 1"],
        description="Modèle généraliste, appelé réellement via LiteLLM/OpenRouter.",
        invocable=True,
    ),
    m.ModeleIA(
        id="m-deepseek-flash",
        slug="deepseek/deepseek-v4-flash",
        nom="DeepSeek V4 Flash",
        editeur="DeepSeek",
        famille="texte",
        hebergement="externe",
        residence="Passerelle OpenRouter — fournisseur variable selon la route.",
        licence="DeepSeek",
        contexteJetons=128_000,
        prixEntree=60,
        prixSortie=120,
        unite="jeton",
        statut="disponible",
        usages=["Réponses courtes", "Volume élevé", "Secours si le garde-fou OpenRouter bloque les autres modèles"],
        description=(
            "Vérifié en direct comme réellement autorisé sous le garde-fou restrictif du "
            "compte OpenRouter partagé (2026-09-07) — appelé réellement via LiteLLM/OpenRouter."
        ),
        invocable=True,
    ),
    m.ModeleIA(
        id="m-glm-flash",
        slug="z-ai/glm-5.3-flash",
        nom="GLM 5.3 Flash",
        editeur="Z.ai",
        famille="texte",
        hebergement="externe",
        residence="Passerelle OpenRouter — fournisseur variable selon la route.",
        licence="Z.ai",
        contexteJetons=128_000,
        prixEntree=65,
        prixSortie=130,
        unite="jeton",
        statut="disponible",
        usages=["Réponses courtes", "Volume élevé", "Modèle par défaut de la démo"],
        description=(
            "Vérifié en direct comme réellement autorisé sous le garde-fou restrictif du "
            "compte OpenRouter partagé (2026-09-09) — appelé réellement via LiteLLM/OpenRouter. "
            "Modèle par défaut demandé pour la démo."
        ),
        invocable=True,
    ),
    m.ModeleIA(
        id="m-mistral-small",
        slug="mistralai/mistral-small-3.2-24b-instruct",
        nom="Mistral Small 3.2 24B",
        editeur="Mistral AI",
        famille="texte",
        hebergement="externe",
        residence="Passerelle OpenRouter — fournisseur variable selon la route.",
        licence="Apache 2.0",
        contexteJetons=128_000,
        prixEntree=180,
        prixSortie=360,
        unite="jeton",
        statut="disponible",
        usages=["Classification", "Réponses courtes", "Volume élevé"],
        description="Bon défaut pour le gros du trafic, appelé réellement via LiteLLM/OpenRouter.",
        invocable=True,
    ),
    m.ModeleIA(
        id="m-qwen-32b",
        slug="qwen/qwen3-32b",
        nom="Qwen3 32B",
        editeur="Alibaba Cloud",
        famille="texte",
        hebergement="externe",
        residence="Passerelle OpenRouter — fournisseur variable selon la route.",
        licence="Apache 2.0",
        contexteJetons=128_000,
        prixEntree=240,
        prixSortie=480,
        unite="jeton",
        statut="disponible",
        usages=["Raisonnement pas à pas", "Mathématiques", "Traduction"],
        description="Raisonne explicitement avant de répondre, appelé réellement via LiteLLM/OpenRouter.",
        invocable=True,
    ),
    m.ModeleIA(
        id="m-codestral",
        slug="mistralai/codestral-2508",
        nom="Codestral 25.08",
        editeur="Mistral AI",
        famille="code",
        hebergement="externe",
        residence="Passerelle OpenRouter — fournisseur variable selon la route.",
        licence="Mistral AI Non-Production puis licence commerciale",
        contexteJetons=256_000,
        prixEntree=240,
        prixSortie=480,
        unite="jeton",
        statut="disponible",
        usages=["Complétion de code", "Revue de diff", "Migration de scripts"],
        description="Spécialisé code, appelé réellement via LiteLLM/OpenRouter.",
        invocable=True,
    ),
    m.ModeleIA(
        id="m-gpt",
        slug="openai/gpt-5.1",
        nom="GPT-5.1",
        editeur="OpenAI",
        famille="texte",
        hebergement="externe",
        residence="États-Unis — OpenAI, sans résidence garantie.",
        licence="Conditions OpenAI",
        contexteJetons=400_000,
        prixEntree=1_520,
        prixSortie=12_100,
        unite="jeton",
        statut="disponible",
        usages=["Tâches longues", "Raisonnement complexe"],
        description="Appelé réellement via LiteLLM/OpenRouter. Le contenu quitte le territoire.",
        invocable=True,
    ),
    m.ModeleIA(
        id="m-claude",
        slug="anthropic/claude-sonnet-4.5",
        nom="Claude Sonnet 4.5",
        editeur="Anthropic",
        famille="texte",
        hebergement="externe",
        residence="Union européenne — région Anthropic eu-central.",
        licence="Conditions Anthropic",
        contexteJetons=200_000,
        prixEntree=1_820,
        prixSortie=9_080,
        unite="jeton",
        statut="disponible",
        usages=["Analyse de contrats", "Assistance au code"],
        description="Appelé réellement via LiteLLM/OpenRouter.",
        invocable=True,
    ),
    m.ModeleIA(
        id="m-mistral-large",
        slug="mistralai/mistral-large-2407",
        nom="Mistral Large",
        editeur="Mistral AI",
        famille="texte",
        hebergement="externe",
        residence="France — Mistral AI, région eu-west.",
        licence="Conditions Mistral AI",
        contexteJetons=128_000,
        prixEntree=1_210,
        prixSortie=3_630,
        unite="jeton",
        statut="disponible",
        usages=["Français soutenu", "Documents administratifs"],
        description=(
            "Équivalent réel le plus proche de « Mistral Large 3 » du catalogue marketing : "
            "appelé via LiteLLM/OpenRouter sous le slug `mistralai/mistral-large-2407`."
        ),
        invocable=True,
    ),
    m.ModeleIA(
        id="m-gemini",
        slug="google/gemini-2.5-pro",
        nom="Gemini 2.5 Pro",
        editeur="Google",
        famille="texte",
        hebergement="externe",
        residence="États-Unis — Google Cloud, multi-région.",
        licence="Conditions Google Cloud",
        contexteJetons=1_000_000,
        prixEntree=760,
        prixSortie=6_050,
        unite="jeton",
        statut="disponible",
        usages=["Très longs contextes", "Analyse de corpus"],
        description="Appelé réellement via LiteLLM/OpenRouter.",
        invocable=True,
    ),
    m.ModeleIA(
        id="m-embed",
        slug="synelia/bge-m3",
        nom="BGE-M3",
        editeur="BAAI",
        famille="embedding",
        hebergement="souverain",
        residence="Abidjan — datacenter Synelia",
        licence="MIT",
        contexteJetons=8_192,
        prixEntree=45,
        prixSortie=0,
        unite="jeton",
        statut="disponible",
        usages=["Bases de connaissances", "Recherche sémantique"],
        description=(
            "Modèle d'embedding : hors périmètre de la passerelle **chat** (`invoquer`), mais "
            "réellement appelé par le pipeline d'ingestion/recherche des bases de connaissances "
            "via Infinity (`SYNELIA_EMBEDDINGS_URL`) — voir `modules/ia_agents/connaissances.py`."
        ),
        invocable=False,
    ),
    m.ModeleIA(
        id="m-rerank",
        slug="synelia/bge-reranker-v2-m3",
        nom="BGE Reranker v2-m3",
        editeur="BAAI",
        famille="reranker",
        hebergement="souverain",
        residence="Abidjan — datacenter Synelia",
        licence="Apache 2.0",
        contexteJetons=8_192,
        prixEntree=60,
        prixSortie=0,
        unite="jeton",
        statut="disponible",
        usages=["Reclassement des fragments"],
        description="Reranker : hors périmètre de la passerelle chat de ce MVP.",
        invocable=False,
    ),
    m.ModeleIA(
        id="m-whisper",
        slug="synelia/whisper-large-v3",
        nom="Whisper large-v3",
        editeur="OpenAI (poids ouverts)",
        famille="transcription",
        hebergement="souverain",
        residence="Abidjan — datacenter Synelia",
        licence="MIT",
        contexteJetons=0,
        prixEntree=12,
        prixSortie=0,
        unite="minute",
        statut="disponible",
        usages=["Comptes rendus de réunion", "Centres d'appel"],
        description="Transcription audio : hors périmètre de la passerelle chat de ce MVP.",
        invocable=False,
    ),
    m.ModeleIA(
        id="m-pixtral",
        slug="synelia/pixtral-12b",
        nom="Pixtral 12B",
        editeur="Mistral AI",
        famille="vision",
        hebergement="souverain",
        residence="Abidjan — datacenter Synelia",
        licence="Apache 2.0",
        contexteJetons=128_000,
        prixEntree=190,
        prixSortie=380,
        unite="jeton",
        statut="apercu",
        usages=["Lecture de pièces jointes"],
        description="Aucun équivalent OpenRouter confirmé à ce jour : non invocable.",
        invocable=False,
    ),
]


async def _semer_modeles(ctx: Contexte) -> None:
    if await depot_modeles.compter(ctx) > 0:
        return
    for modele in SEMENCES:
        await depot_modeles.creer(ctx, modele, id_=modele.id)


async def obtenir_modele_par_slug(ctx: Contexte, slug: str) -> m.ModeleIA | None:
    await _semer_modeles(ctx)
    for modele in await depot_modeles.tous(ctx):
        if modele.slug == slug:
            return modele
    return None


def _url() -> str:
    return os.environ.get(ENV_URL, "http://litellm:4000").rstrip("/")


def _cle() -> str:
    return os.environ.get(ENV_CLE, "")


async def _completer(modele: m.ModeleIA, agent: m.AgentIA, messages: list[dict[str, str]]) -> dict[str, Any]:
    corps = {
        "model": modele.slug,
        "messages": messages,
        "temperature": agent.temperature,
        "top_p": agent.topP,
        "max_tokens": agent.jetonsMax,
    }
    debut = time.perf_counter()
    try:
        async with httpx.AsyncClient(base_url=_url(), timeout=60) as client:
            r = await client.post(
                "/chat/completions",
                json=corps,
                headers={"Authorization": f"Bearer {_cle()}"},
            )
    except httpx.HTTPError as exc:
        raise erreurs.amont_indisponible("litellm", str(exc)) from exc
    latence_ms = int((time.perf_counter() - debut) * 1000)

    if r.status_code >= 400:
        raise erreurs.amont_indisponible(
            "litellm", f"HTTP {r.status_code} : {r.text[:200]}"
        )

    donnees = r.json()
    reponse = donnees["choices"][0]["message"]["content"]
    if reponse is None:
        # Vérifié en direct sur `deepseek/deepseek-v4-flash` : un modèle de raisonnement peut
        # consommer tout `max_tokens` en jetons de raisonnement internes et atteindre
        # `finish_reason: "length"` sans jamais émettre de `content` visible. Un 424 franc qui
        # le dit vaut mieux qu'un 500 brut de validation de réponse (`reponse` exigé `str`).
        raise erreurs.amont_indisponible(
            "litellm",
            f"Le modèle « {modele.slug} » n'a produit aucun contenu visible dans la limite de "
            f"{agent.jetonsMax} jetons (probablement consommés en raisonnement interne) — "
            "augmentez `jetonsMax` sur cet agent.",
        )
    usage = donnees.get("usage") or {}
    jetons_entree = usage.get("prompt_tokens", 0)
    jetons_sortie = usage.get("completion_tokens", 0)
    cout_usd = usage.get("cost")
    if cout_usd is not None:
        cout_fcfa = cout_usd * TAUX_USD_XOF
    else:
        cout_fcfa = (
            jetons_entree * (modele.prixEntree or 0) + jetons_sortie * (modele.prixSortie or 0)
        ) / 1_000_000

    return {
        "reponse": reponse,
        "jetonsEntree": jetons_entree,
        "jetonsSortie": jetons_sortie,
        "coutFcfa": round(cout_fcfa, 4),
        "latenceMs": latence_ms,
    }


async def invoquer(ctx: Contexte, agent: m.AgentIA, message: str) -> dict[str, Any]:
    modele = await obtenir_modele_par_slug(ctx, agent.modele)
    if modele is None or not modele.invocable:
        raise erreurs.non_porte("Ce modèle n'est pas disponible sur cette passerelle.")
    return await _completer(
        modele,
        agent,
        [
            {"role": "system", "content": agent.consigne},
            {"role": "user", "content": message},
        ],
    )


async def invoquer_messages(
    ctx: Contexte, agent: m.AgentIA, messages: list[dict[str, str]]
) -> dict[str, Any]:
    """Comme `invoquer`, mais avec un historique de tours complet — la « mémoire partagée »
    d'un flux d'orchestration : chaque appel d'agent voit les tours précédents, pas seulement
    le dernier message."""
    modele = await obtenir_modele_par_slug(ctx, agent.modele)
    if modele is None or not modele.invocable:
        raise erreurs.non_porte("Ce modèle n'est pas disponible sur cette passerelle.")
    return await _completer(modele, agent, [{"role": "system", "content": agent.consigne}, *messages])


@peupleur
async def demo(session, org: Organisation, admin: Utilisateur) -> None:
    agent = m.AgentIA(
        id="agent-demo-support",
        nom="Assistant support",
        consigne=(
            "Tu es l'assistant support de Synelia Cloud. Réponds en français, "
            "brièvement, et ne demande jamais de mot de passe."
        ),
        espaceId="espace-demo-abj",
        modele="meta-llama/llama-3.3-70b-instruct",
        temperature=0.7,
        topP=1,
        jetonsMax=512,
        statut="publie",
        createdAt=maintenant(),
    )
    session.add(
        Ressource(
            id=agent.id,
            org_id=org.id,
            type="agent_ia",
            nom=agent.nom,
            statut=agent.statut,
            donnees=agent.model_dump(mode="json"),
        )
    )
