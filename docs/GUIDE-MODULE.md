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
- Erreurs : `from synelia_kernel import erreurs` — `introuvable`, `conflit`, `nom_deja_pris`, `validation(message, champs)`,
  `quota_depasse`, `non_porte` (422 « l'amont ne le porte pas »), `amont_indisponible(integration)` (424), `interdit`.
- Audit : `from synelia.audit import journaliser` — `await journaliser(ctx, action="vm.creation", cible_type="vm", cible_id=..., cible=nom)`
  sur chaque mutation.
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
