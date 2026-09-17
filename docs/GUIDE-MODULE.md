# Guide : écrire un module du contrat

Lire en entier avant de coder. Le module de référence est `apps/synelia/synelia/modules/espaces/`.

## Où sont les choses
- Contrat : `packages/contract/synelia_contract/openapi.json`. Index des opérations : `synelia_contract.operations.OPERATIONS`
  (méthode, chemin, `nom_python`, `rbac`, `code_succes`, `corps_modele`, `reponse_modele`, `parametres_requete`).
  **Toujours** nommer la fonction de route `nom_python` et utiliser `corps_modele` / `reponse_modele`.
- Modèles Pydantic générés : `synelia_contract.modeles` (import `from synelia_contract import modeles as m`). Ne jamais les éditer.
  Les objets inline sont nommés `<Chemin><Methode>Request|Response` (ex. `VmsGetResponse` = `{donnees, pagination}`).
  Consulter le nom exact : `uv run python -c "import inspect, synelia_contract.modeles as m; print(inspect.getsource(m.Vm))"`.
- Contexte de requête : `from synelia.deps import Ctx, CtxPublic, Page, Contexte, exige, exige_admin, exiger_confirmation`.
  - `ctx.session` (AsyncSession), `ctx.org_id`, `ctx.principal`, `ctx.correlation_id`, `ctx.reglages`.
  - RBAC : `ctx: Contexte = Depends(exige("vm.create_delete"))` pour une mutation ; `exige("org.dashboard.view", lecture=True)`
    pour un GET ; `exige(None)` quand l'opération n'a pas de `x-rbac` (authentifié seulement) ; `exige_admin("capacity.manage")`
    pour `/admin/**` ; `CtxPublic` pour `/public/**`.
- Persistance : `from synelia.depot import Depot` — `Depot("vm", m.Vm)` puis `lister(ctx, page, filtre=..., tri_defaut=...)`
  (renvoie `{donnees, pagination}`), `obtenir` (404 automatique), `trouver`, `creer`, `modifier(ctx, id, patch_model_or_dict)`,
  `remplacer`, `supprimer(logique=True)`, `definir_statut`, `exiger_nom_libre` (409 `nom_deja_pris`), `secrets/definir_secrets`
  (chiffrés). `plateforme=True` pour les ressources sans organisation (catalogue, backends, offres). Le `type` est une chaîne
  stable en snake_case (`vm`, `volume`, `load_balancer`…) ; deux modules qui partagent une ressource utilisent le même type.
  Schéma : `create_all` fait foi, jamais d'ALTER automatique — ADR 0003 (`docs/ADR/0003-schema-create-all-sans-migrations.md`).
- Travaux (202) : `from synelia.travaux import demarrer_travail, executeur, Executeur` ;
  `return await demarrer_travail(ctx, "vm.create", vm.nom, cible_type="vm", cible_id=vm.id, entree=corps.model_dump(mode="json"))`.
  Les 41 types du catalogue sont dans `synelia_contract.workflows.catalogue()` ; sinon passer `etapes=[{"nom":..., "dureeS":...}]`.
  Un `@executeur("vm.create") class X(Executeur)` peut implémenter `etape(ctx, travail, index, nom)`, `terminer(ctx, travail)`
  et `compenser(...)` (mettre `compensable = True`). Sans exécuteur, le travail réussit en simulation. En test/Vercel,
  le travail s'exécute **en ligne** avant le `202` : la réponse porte déjà `statut: done`.
- Amont : `from synelia_openstack import fournisseur` + une paire `XxxSimule` / `XxxOpenStack` dans
  `packages/openstack/synelia_openstack/<domaine>.py` (voir `compute.py`, `identite.py`). Le simulé renvoie des valeurs
  plausibles instantanément ; le réel utilise `openstacksdk` via `synelia_openstack.fabrique.connexion()`. Aucune connexion
  réelle en test. Pour les amont non OpenStack (Stalwart, Postal, Nextcloud, Plesk, ACME, CinetPay, Stripe, Argo, Harbor…),
  même motif dans `packages/openstack/synelia_openstack/connecteurs_<nom>.py` : simulé + réel (httpx), le réel n'est appelé
  que si sa variable d'environnement d'URL existe.
- Amont SSH (une VM déjà en service, ex. `web_hebergement`/`web_drive`) : deux gardes avant le premier appel réel,
  toutes deux inutiles en mode simulé (`SshSimule`/`ComputeSimule` n'ont besoin d'aucun identifiant réel) —
  `if isinstance(amont_ssh(), SshReel) and (not cle_privee or not ip): raise erreurs.amont_indisponible(...)`, puis
  `if isinstance(amont_ssh(), SshReel) and amont().statut_serveur(sid) == "absente": raise erreurs.amont_indisponible(...)`
  (vécu en direct : sans la seconde garde, un enregistrement orphelin — VM Nova supprimée hors bande — bloque
  ~20 s sur un SSH voué à l'échec avant de retourner une erreur, au lieu d'échouer tout de suite clairement).
- Erreurs : `from synelia_kernel import erreurs` — `introuvable`, `conflit`, `nom_deja_pris`, `validation(message, champs)`,
  `quota_depasse`, `non_porte` (422 « l'amont ne le porte pas »), `amont_indisponible(integration)` (424), `interdit`.
- Audit : `from synelia.audit import journaliser` — `await journaliser(ctx, action="vm.creation", cible_type="vm", cible_id=..., cible=nom)`
  sur chaque mutation. Le journal est *tamper-evident*, pas *tamper-proof* : append-only imposé par les droits
  Postgres (`synelia_app` n'a que `SELECT, INSERT` sur `audit`), chaîne vérifiable par `/audit/integrite`, tête de
  chaîne ancrée chaque jour hors du rôle applicatif ; un superutilisateur Postgres reste hors du modèle de menace.
- Destructif : `exiger_confirmation(nom_attendu, confirmation)` **avant** toute action (paramètre de requête `confirmation`).
- Démo : `from synelia.demo import peupleur` → `@peupleur async def demo(session, org, admin)` crée 2-3 ressources réalistes
  pour l'organisation de démo (utiliser `Depot` avec un `Contexte` minimal n'est pas possible : insérer des `Ressource`
  directement : `session.add(Ressource(id=..., org_id=org.id, type="vm", nom=..., statut=..., donnees=m.Vm(...).model_dump(mode="json")))`).

## Gabarit
```
apps/synelia/synelia/modules/<module>/
├── __init__.py      # from synelia.modules.<module>.router import router ; __all__ = ["router"]  (ou `routers = [r1, r2]`)
├── router.py        # une fonction par opération, nommée par nom_python, response_model=m.<reponse_modele>, status_code=code_succes
├── service.py       # règles, exécuteurs de travaux, dépôts (facultatif si tout tient dans router.py)
└── tests/test_<module>.py  # fixture `client` (admin connecté, org de démo) : un test par opération au moins sur le code succès
```
Exemple minimal de route :
```python
@router.get("", response_model=m.VmsGetResponse, response_model_exclude_none=True)
async def lister_vms(
    page: Page,
    espaceId: str | None = None,
    ctx: Contexte = Depends(exige("org.dashboard.view", lecture=True)),
) -> Any:
    return await depot.lister(
        ctx, page, filtre=lambda v: not espaceId or v.espaceId == espaceId, tri_defaut="nom"
    )
```
Règles :
- `response_model_exclude_none=True` partout ; renvoyer des dicts ou des modèles Pydantic, jamais des ORM.
- Les paramètres de requête gardent le nom du contrat (`espaceId`, `parPage`) — ajouter `# noqa: N803` si besoin.
- `204` → `return Response(status_code=204)`. `201` → `status_code=status.HTTP_201_CREATED`.
- Ce que l'amont ne porte pas → `erreurs.non_porte("…")`, jamais un 200 creux. Pas de valeurs inventées côté lecture :
  une métrique sans source renvoie une série vide ou un 424 `amont_indisponible`, pas un nombre au hasard.
- Ne pas modifier `app.py`, `deps/`, `depot.py`, `travaux/moteur.py`, `synelia_db/modeles/*`, ni les fichiers d'un autre module.
  Besoin d'une table dédiée ? Utiliser `Depot` avec un nouveau `type`.
- Vérifier : `uv run ruff check --fix apps/synelia/synelia/modules/<module> && uv run pytest -q apps/synelia/synelia/modules/<module>`
  puis `uv run python tools/contrat_diff.py | grep -i <tag>` doit montrer le domaine complet.

## Invariants durs (tout écart a déjà coûté une panne réelle)

- **Tout appel SDK OpenStack synchrone va dans `asyncio.to_thread`.** Les exécuteurs de travaux tournent sur la
  boucle asyncio de l'API elle-même : un `amont().creer_serveur(...)` nu y fige l'API **pour tous les tenants**
  pendant toute la durée de l'appel (constaté en direct : un `vm.resize` a gelé même `/public/offres` non
  authentifié 10+ minutes). Motif obligatoire : `srv = await asyncio.to_thread(amont().creer_serveur, nom=..., image_id=...)`.
  Valable aussi pour le SSH réel (`SshReel`) et les clients synchrones (`httpx.post` des connecteurs — Designate,
  Zimbra, ACME — sont bloquants eux aussi, le nom « httpx » ne suffit pas à les rendre asynchrones).
  Balayage complet fait dans `cf8d38f`, `3a90696`, `4f8e850`..`6696a0d` : ne réintroduisez pas le bug.
- **Le statut écrit dans une ressource vient du Literal du contrat.** Écrire `statut="erreur"` alors que
  `ClusterK8s.statut` n'admet que `running|degraded|provisioning|updating` explose à la lecture suivante dans
  `Depot._vers_modele` (validation Pydantic) — vécu en direct dans `ExecuteurK8sCreate.compenser`. Vérifier
  l'union exacte dans `synelia_contract.modeles` avant d'écrire.
- **L'id d'une réponse 202 est celui du travail, pas de la ressource.** Pour retrouver la ressource créée :
  re-lister ou re-lecture par un attribut unique. Ce piège a coûté des 404 à chaque session de test.
- **Réconcilier à la lecture plutôt que laisser mentir.** Une ligne en base peut survivre à son infra réelle
  (VM Nova supprimée hors bande) et continuer de s'afficher `en_ligne`/`running`. Motif établi :
  `ComputeOpenStack.statut_serveur()` / `MagnumOpenStack.cluster_statut()` (`"absente"` si l'amont ne
  connaît plus la ressource) + `reconcilier_statut()` branché sur le GET détail **et** la liste, ne touchant
  que les lignes en statut non terminal (`3675796`). Nouveau type adossé à de l'infra réelle = même motif.
- **Une paire `Simule`/`Reel` ne suffit pas : encore faut-il l'appeler.** La classe de bug la plus fréquente
  de ce dépôt : un exécuteur qui ne touche que `depot.*` et rapporte `done` sans un seul appel amont —
  faux succès indistinguable d'un vrai (vu sur `vm.compose`, `vm.resize`, `/vms/lot`, web_dns, web_ssl,
  `web.backup.restore`…). Toute nouvelle opération de provisioning se vérifie en mode réel : la ressource
  existe-t-elle côté OpenStack après le job ?
