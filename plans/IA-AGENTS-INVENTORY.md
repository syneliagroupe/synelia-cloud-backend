# IA & Agents — inventaire des fonctionnalités (état réel, vérifié le 2026-09-24)

Source : lecture directe de `apps/synelia/synelia/modules/ia_agents/` (`router.py`,
`service.py`, `connaissances.py`, `flux.py`, `cles.py`) + `uv run synelia capacites` +
`packages/contract/synelia_contract/operations.py` + état des conteneurs (`docker compose ps`).
Aucun appel LLM réel effectué dans cette session (coût réel via OpenRouter/LiteLLM) — statut
établi par lecture de code et vérification de l'infrastructure (conteneur `litellm` up depuis
8h), pas par test en direct.

Légende : **réel** = appelle un amont véritable ; **simulé** = aucun appel externe, données
fabriquées, étiqueté comme tel dans le code ; **partiel** = mélange des deux selon la config ;
**non vérifié** = pas testé en direct cette session.

## Correction du constat de `docs/PLAN-DIRECTEUR.md`

Ce doc (architecture Node.js abandonnée) affirmait : *« L'univers IA & Agents (`/app/ia/**`,
9 actions RBAC `ia.*`) n'a aucun chemin dans l'OpenAPI. Il est planifié en dernière phase. »*
**Ce constat est obsolète.** Le module `ia_agents` existe intégralement en Python/FastAPI,
19 endpoints sont déclarés dans `packages/contract/synelia_contract/operations.py` (préfixe
`/ia/…`), et 4 actions RBAC sont utilisées dans le code (`ia.agent.write`, `ia.flow.write`,
`ia.knowledge.write`, `ia.key.manage` — pas 9 ; les lectures utilisent `org.dashboard.view`
partagé avec les autres univers, donc pas d'action RBAC dédiée par ressource en lecture).

## Registre `synelia capacites` : absent pour ce module

`uv run synelia capacites` (registre `synelia_openstack.registre_capacites.REGISTRE`) ne liste
**aucune** entrée pour `ia_agents` — ni `litellm`, ni `qdrant`, ni `docling`, ni `infinity`. Ce
registre est scopé aux connecteurs OpenStack/SaaS classiques (registrar, ACME, compute, Zimbra,
ArgoCD, VictoriaMetrics…) ; `ia_agents` gère ses gates par vérification directe de variable
d'environnement dans son propre code (`_url()`/`_reel()` locaux à `service.py` et
`connaissances.py`), sans s'enregistrer dans ce registre central. Ce n'est pas un bug — juste un
angle mort si quelqu'un utilise `capacites` comme unique source de vérité pour auditer
réel/simulé sur ce module : il faut lire le code directement, ce que ce document fait.

## 1. Catalogue de modèles IA (`/ia/modeles`)
Backend : catalogue **scellé et statique** (`SEMENCES` dans `service.py`, 14 modèles), seedé une
fois par organisation (`_semer_modeles`, idempotent sur `depot_modeles.compter`). Pas d'appel à
un catalogue amont (ni OpenRouter `/models`, ni LiteLLM `/model/info`) : la liste est écrite en
dur dans le code, avec commentaires assumant l'état de vérification manuelle en date du
2026-09-07/09 (« vérifié en direct comme réellement autorisé sous le garde-fou restrictif du
compte OpenRouter partagé »).

- [x] `GET /ia/modeles` — réel (lecture DB après seed).
- [x] `POST /ia/modeles` — réel (écriture DB), mais **sans RBAC** : `exige(None)` — n'importe
  quel utilisateur authentifié peut créer une entrée de catalogue de modèles (plateforme,
  partagée entre toutes les organisations). À comparer aux autres routes du module qui exigent
  `ia.agent.write`/`ia.flow.write`/etc. Écart probable — à vérifier si intentionnel (endpoint
  réservé au support selon le commentaire d'en-tête de router.py : « POST réservé au support de
  nouveaux modèles », mais rien dans le code ne l'impose).
- [x] `GET /ia/modeles/{modeleId}` — réel.
- 4 modèles texte confirmés « réellement invocables » (Llama 3.3 70B, DeepSeek V4 Flash, GLM
  5.3 Flash, Mistral Small 3.2) + 6 autres modèles texte/code marqués `invocable=True` sans note
  de vérification aussi récente (GPT-5.1, Claude Sonnet 4.5, Mistral Large, Gemini 2.5 Pro,
  Qwen3 32B, Codestral) + 4 modèles `invocable=False` (embedding BGE-M3 hors passerelle chat,
  reranker, Whisper, Pixtral — « hors périmètre de ce MVP », honnête, pas de faux-semblant).

## 2. Agents IA (`/ia/agents`) — appellent réellement LiteLLM/OpenRouter
Backend : `service._completer()` fait un vrai `POST {SYNELIA_LITELLM_URL}/chat/completions`
avec `Authorization: Bearer {SYNELIA_LITELLM_MASTER_KEY}`. **`.env` contient les vrais
identifiants** (`SYNELIA_OPENROUTER_KEY`, `SYNELIA_LITELLM_MASTER_KEY`, `SYNELIA_LITELLM_URL=
http://litellm:4000`), et le conteneur `litellm` (image `ghcr.io/berriai/litellm:main-stable`)
tourne effectivement dans ce lab (`docker compose ps` : up depuis 8h). **Statut : réel**, câblé
de bout en bout — pas d'appel test effectué cette session pour éviter tout coût, mais le chemin
de code est sans ambiguïté (pas de branche simulée dans `_completer`, contrairement à
`connaissances.py` qui a un `_reel()` explicite).

Détails corrects et honnêtes trouvés dans le code :
- Gestion propre du cas « modèle de raisonnement épuise tout `max_tokens` en jetons de
  raisonnement internes sans jamais produire de `content` visible » (bug vu en direct sur
  `deepseek-v4-flash` d'après le commentaire) → 424 explicite plutôt qu'un 500 de validation.
  C'est un test réel documenté dans le code (pas de cette session).
- Coût FCFA calculé sur `usage.cost` (USD × taux fixe 610) si LiteLLM le fournit, sinon fallback
  sur les prix catalogue × jetons.

- [x] `GET /ia/agents`, `GET /ia/agents/{id}` — réel (DB).
- [x] `POST /ia/agents` — réel (DB), valide le `modele` contre le catalogue.
- [x] `PATCH /ia/agents/{id}` — réel (DB).
- [x] `DELETE /ia/agents/{id}` — réel (DB, avec confirmation par nom).
- [x] `POST /ia/agents/{id}/invoquer` — **réel**, appelle LiteLLM. Applique la clé IA
  (`X-Cle-IA`) si fournie : allowlist de modèle, résidence, débit (seau à jetons en mémoire),
  quota/budget mensuel — tout appliqué avant l'appel, crédité après. Sinon appel anonyme sans
  contrôle (comportement historique explicite, pas un oubli).

## 3. Flux d'orchestration (`/ia/flux`) — moteur de travaux, pas de service séparé
Backend : exécuteur Python natif (`ExecuteurFluxExecuter`, `flux.py`), branché sur le moteur de
travaux générique (`synelia.travaux`). Pas de Mastra/service dédié — commentaire de tête de
module assumé (« la plomberie la plus simple qui fonctionne »).

Statut **réel par type d'étape**, vérifié précisément dans `_dispatch()` :
- `declencheur` — passe-plat (entrée du travail).
- `agent` — **réel**, appelle `service.invoquer`/`invoquer_messages` (LiteLLM).
- `outil` — **simulé, étiqueté** : `OutilAgent` (contrat) ne porte aucun champ URL/endpoint réel
  à appeler ; retourne `{"simule": True, "note": "..."}`. Honnête, pas de faux succès.
- `connaissance` — **réel** : appel HTTP loopback vers `POST /ia/connaissances/{id}/rechercher`
  sur ce même process (avec émission d'un jeton d'accès de courte durée si hors contexte HTTP,
  ex. worker). Réel *dans son propre mode* — délègue au statut réel/simulé de la base de
  connaissances ciblée (voir section 4).
- `routeur` — réel : évalue les conditions (`_condition_vraie`), pas de LLM impliqué —
  comparaison numérique/texte si opérateur présent, sinon correspondance mot-clé naïve
  (imprécision assumée dans le commentaire du module : « c'est la contrepartie d'accepter un
  champ texte libre plutôt qu'un langage d'expression »).
- `boucle` — réel, itère sur une variable liste, borné par `maxIterations`.
- `humain` — réel : pause franche (`PauseHumaine`), le travail reste `running` jusqu'à
  `POST .../reprendre`.
- `code` — `eval()` réel sur une expression, builtins restreints à une poignée de fonctions pures
  (`str`, `int`, `len`, `sorted`…). **Pas un bac à sable** — le code le dit explicitement
  (ponytail : « pas d'isolation process/mémoire/CPU… ne PAS réutiliser tel quel face à du code
  non fiable en production multi-tenant »). Risque de sécurité assumé et documenté, pas caché —
  mais reste un risque réel si un utilisateur non fiable peut définir une étape `code` d'un flux
  dans une organisation partagée (`eval` a accès à `variables`/`entree`, builtins réduits mais
  `__builtins__` explicitement fourni donc pas d'évasion évidente via `__import__` etc., pas
  vérifié plus loin — à auditer si des flux sont un jour ouverts à des utilisateurs non admin).
- `anonymisation` / `habilitation` / `transfert` — **passe-plat, pas un garde-fou réel** —
  le code le dit lui-même : « Ne pas laisser croire que ces étapes protègent quoi que ce soit
  dans cette itération. » Aucun Presidio, aucun filtrage RBAC documentaire, aucun canal
  omnicanal réel. **Écart de premier principe potentiellement sensible** si le studio frontend
  présente ces étapes comme des garanties de conformité (anonymisation RGPD, habilitation
  documentaire) à l'utilisateur final — à vérifier côté UI.

- [x] `GET /ia/flux`, `GET /ia/flux/{id}` — réel (DB).
- [x] `POST /ia/flux` — réel (DB).
- [x] `PATCH /ia/flux/{id}` — réel (DB).
- [x] `DELETE /ia/flux/{id}` — réel (DB) + nettoyage correct des travaux `queued`/`running`
  orphelins (annulés avec motif explicite, pas de zombie).
- [x] `POST /ia/flux/{id}/executer` — réel, démarre un travail (`demarrer_travail`), rejette
  proprement un flux sans étape (évite un `IndexError` non attrapé, cas documenté dans le code).
- [x] `POST /ia/flux/{id}/executions/{travailId}/reprendre` — réel, débloque une pause `humain`.

Exécuteurs enregistrés vs types de travaux démarrés — cohérent :
`demarrer_travail(ctx, "connaissance.ingerer_document", …)` (router.py) ↔
`@executeur("connaissance.ingerer_document")` (connaissances.py) ; et
`demarrer_travail(ctx, TYPE_TRAVAIL="ia.flux.executer", …)` (flux.py) ↔
`@executeur(TYPE_TRAVAIL)` (flux.py). Aucun appel orphelin trouvé.

## 4. Bases de connaissances (`/ia/connaissances`) — RAG réel seulement si `SYNELIA_QDRANT_URL` posée
Backend : pipeline Docling (extraction) → découpage → BGE-M3 via Infinity (embeddings) →
Qdrant (index vectoriel). **Un seul signal de gate** (`connaissances._reel()` teste
`SYNELIA_QDRANT_URL` uniquement, pas Docling/Infinity qui ont des valeurs par défaut adaptées à
docker-compose). **Ni Qdrant, ni Docling, ni Infinity ne tournent dans ce lab**
(`docker compose ps` : absents), et `SYNELIA_QDRANT_URL` n'est pas dans `.env` → **statut réel
de ce lab : simulé**, honnêtement étiqueté dans le code à chaque point de bascule.

- Extraction (`extraire_texte`) sans mode réel : b64 décodé tel quel si fourni, sinon
  `[extraction simulée de {url} — SYNELIA_QDRANT_URL absent]` — placeholder explicite, jamais un
  faux contenu qui se ferait passer pour un vrai texte extrait.
- Recherche (`rechercher`) sans mode réel : **pas de faux vecteur**. Recouvrement de mots >3
  lettres entre la requête et les fragments stockés en clair (chiffrés au repos) dans les
  secrets de la ressource — un score `recouvrement/dénominateur`, pas une similarité cosinus
  simulée. Le code est explicite sur ce choix : « jamais de faux vecteur qui ferait croire à une
  vraie similarité ».
- Découpage : seul le mode `general` (fenêtres de mots avec chevauchement) est implémenté.
  `parent_enfant` et `qr` (choix exposés côté contrat) retombent silencieusement sur `general` —
  **TODO explicite dans le code**, pas une fausse spécialisation.
- `embeddings()` en mode non-réel **lève une erreur** plutôt que de fabriquer un vecteur — bon
  principe (« Aucun vecteur simulé n'est produit ici »).

- [x] `GET /ia/connaissances`, `GET /ia/connaissances/{id}` — réel (DB).
- [x] `POST /ia/connaissances` — réel (DB), `documents=0`/`fragments=0` initiaux, statut
  `jamais_indexee`.
- [x] `PATCH /ia/connaissances/{id}` — réel (DB).
- [x] `DELETE /ia/connaissances/{id}` — réel (DB) + nettoyage Qdrant réel si `_reel()` (best
  effort, avale les erreurs HTTP — commentaire assumé : « la ressource est déjà supprimée côté
  Synelia »).
- [x] `POST /ia/connaissances/{id}/documents` (ingestion) — démarre un travail réel
  (`connaissance.ingerer_document`), 2 étapes (extraction, découpage+index) — **simulé dans ce
  lab** faute de Qdrant, mais le code du travail est identique en réel/simulé (juste la branche
  `_reel()` qui change la destination du stockage).
- [x] `POST /ia/connaissances/{id}/rechercher` — réel appel synchrone, applique aussi une clé IA
  (`hebergement_modele="souverain"` — donc `_residence_ok` toujours vraie pour cet endpoint, une
  clé à résidence stricte type `interne` ne bloquera jamais une recherche documentaire, seulement
  les modèles de chat externes ; cohérent avec l'intention déclarée du champ).

## 5. Clés d'accès IA (`/ia/cles`) — contrat réellement appliqué
Backend : entièrement en DB + secrets chiffrés de la ressource (pas d'amont externe — logique,
c'est un objet de gouvernance interne). **Statut : réel**, quotas/débit/budget vérifiés dans le
code, pas de simulation :
- Débit : seau à jetons **en mémoire process** (`_SEAUX` dict module-level) — **écart connu et
  documenté** : avec `SYNELIA_API_WORKERS=N` la limite effective devient ×N (chaque worker a son
  propre seau). `SYNELIA_VALKEY_URL` existe déjà en config pour un seau partagé futur, non
  câblé. Comportement actuel avec un seul worker (`docker compose ps` montre un seul `api`) :
  correct.
- Quota jetons / budget mensuel : compteurs réels incrémentés après chaque appel réel
  (`crediter_apres_appel`), remise à zéro au changement de mois. Le reste fractionnaire FCFA
  (`reste_fcfa`) est reporté plutôt qu'arrondi et perdu — traite bien le cas des modèles bon
  marché (deepseek…) qui arrondiraient toujours à 0 FCFA sinon.

- [x] `GET /ia/cles`, `GET /ia/cles/{id}` — réel (DB).
- [x] `POST /ia/cles` — réel (DB), génère un secret opaque, hash stocké.
- [x] `PATCH /ia/cles/{id}` — réel (DB).
- [x] `DELETE /ia/cles/{id}` (révocation) — réel (statut `revoquee`, pas de suppression physique
  — cohérent avec `POST .../invoquer` qui vérifie `statut != "active"` → 401).
- [x] `POST /ia/cles/{id}/rotation` — réel, invalide l'ancien secret, quota/budget préservés.

---

## Bugs / écarts trouvés (pas corrigés cette session — inventaire, pas patch)

1. **`POST /ia/modeles` sans RBAC dédié** (`router.py:43`, `exige(None)`) — n'importe quel
   utilisateur authentifié peut modifier le catalogue de modèles *partagé entre toutes les
   organisations* (`plateforme=True`). Le commentaire de tête de section dit « POST réservé au
   support de nouveaux modèles » mais rien ne l'impose dans le code (`exige(None)` accepte tout
   principal authentifié, contrairement à `ia.agent.write`/`ia.flow.write`/`ia.knowledge.write`
   utilisés ailleurs dans le même fichier). À corriger : soit une action RBAC dédiée
   (`ia.model.write`, réservée à un rôle plateforme/support), soit documenter que c'est voulu et
   pourquoi.
2. **Étapes `anonymisation`/`habilitation`/`transfert` d'un flux : passe-plat sans aucun contrôle
   réel**, alors que leurs noms suggèrent des garanties de conformité (RGPD, portée documentaire
   RBAC). Le code le documente honnêtement en commentaire mais rien ne l'exprime côté API/contrat
   (pas de champ `simule: true` dans la réponse d'exécution, contrairement à l'étape `outil` qui,
   elle, retourne explicitement `{"simule": True, ...}`). Risque si le studio IA affiche ces
   étapes comme opérantes à l'utilisateur final.
3. **`registre_capacites` (utilisé par `uv run synelia capacites`) ne couvre pas `ia_agents`** —
   aucune ligne pour litellm/openrouter/qdrant/docling/infinity. Pas un bug fonctionnel (les
   gates fonctionnent quand même via `os.environ` direct dans le module), mais un angle mort pour
   quiconque utilise `capacites` comme check-list d'audit unique — ce document comble le trou
   pour ce module précis, mais le registre central resterait incomplet pour un futur audit rapide.
4. **Étape `code` d'un flux (`eval()`)** : bac à sable volontairement faible (« ponytail »,
   assumé dans le code pour ce labo). Pas re-testé pour une évasion de sandbox cette session —
   à auditer sérieusement avant d'ouvrir la création de flux à des utilisateurs non-admin d'une
   organisation, ou avant du multi-tenant hostile (le commentaire du code le dit lui-même).
5. **Catalogue de modèles figé en dur** (`SEMENCES`) avec des notes de vérification manuelle
   datées (2026-09-07/09) — aucun mécanisme pour re-vérifier automatiquement qu'un modèle reste
   disponible sur OpenRouter/LiteLLM (`invocable=True` peut devenir faux avec le temps sans que
   rien ne le détecte). Pas un bug, mais une dette : un `GET /model/info` LiteLLM périodique
   recalerait le catalogue au lieu de dépendre d'une vérification manuelle documentée en
   commentaire.

## Constat "premier principe" (répond directement à la question de l'audit)

**Les agents IA appellent réellement un LLM.** Ce n'est pas fabriqué : `service._completer()`
fait un vrai `POST /chat/completions` vers LiteLLM (`SYNELIA_LITELLM_URL`), qui route lui-même
vers OpenRouter avec les vraies clés présentes dans `.env`. Le conteneur `litellm` tourne
effectivement dans ce lab. Le coût, la latence, le contenu de la réponse sont réels — rien
n'est canned/statique dans le chemin heureux (`invoquer`/`invoquer_messages`).

**Le RAG (bases de connaissances) est réel *seulement si* `SYNELIA_QDRANT_URL` est posée** — ce
qui n'est pas le cas dans ce lab (ni Qdrant, ni Docling, ni Infinity n'y tournent). Dans l'état
actuel de ce lab, `/ia/connaissances/*` fonctionne en mode simulé honnête (recouvrement de mots,
pas un faux vecteur, fragments en clair chiffrés au repos) — le code est prêt pour du réel
(chemin `_reel()` entièrement écrit, pas un stub), il manque juste l'infrastructure branchée.

## Plan de travail (dans l'ordre)

1. **Corriger le RBAC de `POST /ia/modeles`** (écart n°1) — décider d'une action dédiée ou
   documenter explicitement l'intention si `exige(None)` est voulu.
2. **Étiqueter honnêtement les étapes `anonymisation`/`habilitation`/`transfert`** dans la
   réponse d'exécution d'un flux (comme le fait déjà l'étape `outil` avec `simule: true`), pour
   que le studio frontend ne puisse pas laisser croire à une garantie de conformité inexistante.
3. Brancher `SYNELIA_QDRANT_URL` (+ Docling + Infinity) dans ce lab pour tester le pipeline RAG
   réel de bout en bout (actuellement seulement audité par lecture de code).
4. Envisager un `GET /model/info` LiteLLM périodique pour recaler `invocable` sur le catalogue de
   modèles au lieu d'une vérification manuelle figée en commentaire (écart n°5).
5. Auditer sérieusement le bac à sable `eval()` de l'étape `code` (écart n°4) avant tout accès
   non-admin à la création de flux.
6. Optionnel : enregistrer `litellm`/`qdrant`/`docling`/`infinity` dans
   `registre_capacites.REGISTRE` pour que `uv run synelia capacites` couvre aussi ce module (écart
   n°3) — cohérence d'audit avec les autres univers (web_*, etc.).
