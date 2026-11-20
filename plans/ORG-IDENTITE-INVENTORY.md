# Organisation & Identité — inventaire des fonctionnalités (état réel, vérifié le 2026-09-24)

Source : lecture directe de `apps/synelia/synelia/modules/{organisations,espaces,projets,membres,
compte,auth,securite,conformite,audit}` + `uv run synelia capacites`. Pas d'appel réel effectué
cette session (pas de test pytest lancé) : `.env` a `SYNELIA_FOURNISSEUR=openstack` (creds live) et
la consigne de cette session était d'éviter tout risque de toucher l'infra réelle via des fixtures
partagées (`client_org` provisionne une VM dans certains tests) — statuts déduits du code + du
registre de capacités, pas revérifiés par exécution.

Légende : **réel** = agit vraiment (DB, amont OpenStack/MinIO, crypto) ; **persisté** = stocke une
configuration fidèlement mais n'appelle aucun système externe pour la faire vivre (ex. SSO) ;
**non porté** = endpoint qui refuse explicitement (pas un faux succès) ; **pas d'amont** = module
pur DB/RBAC sans notion de "réel vs simulé", confirmé en lisant le code plutôt que supposé.

Registre `uv run synelia capacites` (extrait pertinent à cette universe) :
```
identite.tenancy    reel      —
securite.sso        persiste  —   Persistance Organisation.sso JSONB, pas d'appel IdP — test honnête (false)
minio.objet         simule    SYNELIA_MINIO_URL   Reel complet mais gate jamais posée dans CI
```

## 1. organisations — `/organisations`, `/utilisateurs`
Pas d'`amont()` : module 100 % DB (table `Organisation`, `Membership`, `Utilisateur`) + RBAC.
**Statut : pas d'amont, réel au sens DB.**

- [x] `GET /organisations`, `POST`, `GET/{id}`, `PATCH/{id}` — CRUD DB direct, filtré par
  `est_admin_plateforme` / `roles_par_org`. Réel.
- [x] `POST /organisations/{id}/emprunt-identite` — réel : ouvre une vraie session (`auth.ouvrir_session`)
  scoped à l'organisation cible, journalisée avec motif/ticket.
- [x] `POST` / `DELETE /organisations/{id}/suspension` — réel, bascule `statut`, exige confirmation
  nominative (`exiger_confirmation`).
- [x] `GET /organisations/{id}/synthese` — agrège compteurs (espaces/vm/clusters), quota/usage réel
  (somme des `Espace.quota`), impayés (factures `Ressource` type plateforme filtrées par `orgId`),
  tickets ouverts. Dépend de `facturation.metrologie.consommation` (module optionnel, importé en
  différé — si le module `facturation` est absent, `depenseMois` etc. remontent 0 silencieusement :
  pas testé cette session si c'est le cas actuellement).
- [x] `GET /utilisateurs` — liste les utilisateurs de l'org active. Réel.
- **Point notable** : la création d'organisation avec administrateur lève temporairement la RLS
  (`rls.sans_org`) pour insérer le `Membership` dans l'org qui vient d'être créée — commentaire dans
  le code explique que sans ça Postgres refusait l'insert en 500 (RLS `org_id = current_setting`).
  Pas un bug, juste une bascule RLS documentée et à surveiller si RLS change ailleurs.

## 2. espaces — `/espaces`
Amont : `IdentiteOpenStack`/`IdentiteSimule` via `fournisseur(...)`, gate = `SYNELIA_FOURNISSEUR=openstack`
(capacités : `identite.tenancy reel`). **Statut : réel quand le gate est posé** (il l'est dans `.env`
de ce lab), simulé sinon.

- [x] `GET /espaces`, `GET/{id}` — DB + `usage` recalculé dynamiquement (`service.usage`).
- [x] `POST /espaces` — crée la ligne DB puis `demarrer_travail(ctx, "espace.create", ...)` ;
  executor enregistré (`@executeur("espace.create")` dans `espaces/service.py:210`). Pas de job
  fantôme.
- [x] `PATCH /espaces/{id}` — DB.
- [x] `DELETE /espaces/{id}` — vérifie que l'espace est vide (aucune VM) avant de lancer
  `espace.delete` (executor enregistré, `service.py:291`).
- [x] `PUT /espaces/{id}/quota` — **réel et honnête** : appelle
  `service.amont().poser_quotas(...)` sur le vrai projet Keystone/Nova/Cinder avant d'écrire en DB ;
  le commentaire du code explique explicitement qu'un appel manquant laisserait croire à une
  augmentation de quota qui n'existe pas côté amont — corrigé/documenté, pas un trou.
- [x] `GET /espaces/{id}/consommation` — délègue à `facturation.metrologie` (module optionnel).
- [x] `GET /espaces/{id}/placements` — DB (`Depot("placement", ...)`).
- **Aucun endpoint orphelin** : les deux seuls types de travail démarrés par ce module
  (`espace.create`, `espace.delete`) ont chacun un `@executeur` enregistré dans le même fichier.

## 3. projets — `/projets`, `/projets/{id}/services`, `/projets/{id}/domaines`, zone, routage
Amont : `amont_compute`/`amont_network`/`amont_identite`/`amont_ssh`, chacun
`fournisseur(*Simule, *OpenStack|SshReel)`, même gate `SYNELIA_FOURNISSEUR=openstack`. Réutilise
volontairement l'infrastructure de `web_hebergement` (VM + LB Octavia partagé, même clé SSH de
zone) plutôt que d'en construire une deuxième — documenté en tête de `service.py`.

- [x] `GET /projets/synthese`, `GET /projets`, `GET/{id}` — DB.
- [x] `POST /projets` — DB (pas de `demarrer_travail` à la création du projet lui-même, seulement
  au niveau service — cohérent : un "projet" est un conteneur logique, les VM ne naissent qu'avec
  le premier service).
- [x] `PATCH /projets/{id}` — DB.
- [x] `DELETE /projets/{id}` — `demarrer_travail(ctx, "projet.delete", ...)`, executor enregistré
  (`projets/service.py:705`).
- [x] `GET /projets/{id}/services` — DB.
- [x] `POST /projets/{id}/services` (créer) — `demarrer_travail(ctx, "projet_service.create", ...)`,
  executor enregistré (`:647`).
- [x] `PATCH/{id}/services/{sid}` — DB.
- [x] `DELETE /projets/{id}/services/{sid}` — `demarrer_travail(ctx, "projet_service.delete", ...)`,
  executor enregistré (`:691`).
- [x] `POST .../services/{sid}/arret` — `_operation_service(..., "projet_service.stopped", ...)`,
  executor enregistré (`:669`).
- [x] `POST .../services/{sid}/demarrage` et `.../redemarrage` — les **deux** réutilisent le type
  `"projet_service.create"` (même executor que la création). Ce n'est pas un job manquant (executor
  bien présent), mais c'est un raccourci sémantique à vérifier : "redémarrer" et "créer" partagent
  la même logique d'exécution — à confirmer que l'exécuteur `projet_service.create` est bien
  idempotent pour un service déjà existant (pas audité en détail son corps `service.py:647`, non
  relu ligne à ligne cette session faute de temps — **point à ré-auditer**).
- [x] `POST .../services/{sid}/execution` (job ponctuel type cron ?) — vérifié dans le code
  (`:519`), type `"projet_service.create"` également.
- [x] `POST /projets/{id}/domaines/{domId}/certificat` — `demarrer_travail(ctx,
  "domaine_certificat.emission", ...)`, executor enregistré (`:716`).
- [x] `GET/POST/PATCH/DELETE /projets/{id}/domaines` — DB.
- [x] `GET /projets/{id}/variables`, `PUT` — DB (secrets/variables d'env).
- [x] Zone applicative (`GET /projets/{id}/zone`), routage (`GET /projets/{id}/routage`) — DB/lecture
  d'état, pas de travail associé.
- **Aucun type de travail orphelin** : les 6 appels `demarrer_travail` du router utilisent
  `projet.delete`, `projet_service.create` (×3), `projet_service.delete`, `projet_service.stopped`,
  `domaine_certificat.emission` — tous ont un `@executeur` correspondant dans `projets/service.py`.

## 4. membres — `/membres`, `/invitations`
Pas d'`amont()` : pur DB + RBAC + email (invitations). **Statut : pas d'amont, réel.**

- [x] `GET/POST /membres`, `GET/PATCH/DELETE /membres/{id}` — DB. Garde-fou réel et bien pensé :
  `_refuser_si_dernier_admin` bloque toute rétrogradation/suppression qui laisserait l'organisation
  sans aucun `org_admin` (compte à zéro avant de refuser, pas un simple "au moins 2").
- [x] `GET/POST /invitations`, `POST .../relance`, `DELETE /invitations/{id}` — DB + envoi email réel
  (`courriel.envoyer`, lien `/invitation/{jeton}` avec jeton opaque haché en DB, jamais stocké en
  clair). Expiration 7 jours réellement appliquée (`Invitation.expire_le`).
- **Pas de gap trouvé** dans ce module.

## 5. compte — `/moi`
Pas d'`amont()` : DB + crypto locale (bcrypt-like pour mot de passe/codes de secours, TOTP pour MFA,
`chiffrer`/`dechiffrer` pour le secret TOTP au repos). **Statut : pas d'amont, réel.**

- [x] `GET/PATCH /moi`, `GET /moi/organisations`, `PUT /moi/organisation-active` — DB, réel
  (bascule d'organisation active vérifie l'appartenance ou le statut admin plateforme, et
  l'existence réelle de l'organisation cible — corrige un 500 RLS brut sinon, documenté dans le code).
- [x] `GET/PUT /moi/preferences` — DB.
- [x] `PUT /moi/mot-de-passe` — réel : vérifie l'ancien hash, exige 8 caractères mini.
- [x] `POST /moi/mfa` — réel : génère un vrai secret TOTP (`nouveau_secret_totp`), le chiffre au
  repos, génère 8 codes de secours hachés. **Gap de premier principe repéré** : l'endpoint active
  `mfa_active = True` **immédiatement**, sans exiger que l'appelant confirme d'abord un code TOTP
  valide généré à partir du secret retourné — un utilisateur qui a mal scanné le QR code (ou dont
  l'appli TOTP dérive) se retrouve avec le MFA actif sans jamais avoir prouvé qu'il sait le
  produire, et devra utiliser un code de secours au prochain login. Écart mineur mais réel entre le
  contrat implicite ("activer le MFA" devrait vérifier la boucle complète) et l'implémentation.
  Non corrigé cette session (comportemental, pas un bug franc, nécessite une discussion produit sur
  le flux d'activation).
- [x] `DELETE /moi/mfa` — réel.
- [x] `GET /moi/sessions` — DB, réel (sessions actives non révoquées/non expirées).
- [x] `GET /moi/lanceur` — DB (liste des services managés avec URL d'ouverture construite).
- **Aucun `demarrer_travail`/`@executeur` dans ce module** : confirmé, tout est synchrone DB.

## 6. auth — `/auth/*`
Pas d'`amont()` externe : DB + crypto (bcrypt, TOTP, JWT `emettre_acces`) + email (AgentMail avec
repli SMTP). **Statut : réel pour l'authentification locale ; SSO explicitement non implémenté.**

- [x] `POST /auth/connexion` — réel : vérifie mot de passe, statut compte, statut/suspension de
  l'organisation (avec exception documentée pour l'équipe Synelia), restriction IP par politique,
  exige MFA si actif ou imposé par la politique d'org.
- [x] `POST /auth/mfa` — réel : valide soit un TOTP (`verifier_totp`), soit un code de secours à
  usage unique (retiré de la liste après usage, réassignation du dict pour que SQLAlchemy détecte
  le changement JSON — piège connu, correctement évité). D'après le commentaire du code, la
  vérification des codes de secours ne fonctionnait pas avant (chemin mort), corrigée dans une
  session précédente.
- [x] `POST /auth/rafraichir` — réel, avec **détection de réutilisation de refresh token**
  (rotation family) : si un token déjà tourné est réutilisé, toute la famille de sessions est
  révoquée et journalisée en alerte. Comportement de sécurité correct, pas décoratif.
- [x] `POST /auth/deconnexion` — réel (révoque la session courante).
- [x] `POST /auth/inscription` — réel : compte créé `verification_requise`, aucune session ouverte
  avant vérification email.
- [x] `POST /auth/verification-email`, `POST .../renvoi` — réel : code à 6 chiffres haché en DB,
  expiration 15 min, 5 essais max, délai de renvoi 60 s appliqué.
- [x] `GET/POST /auth/invitations/{jeton}` — réel (accepter une invitation crée/attache le compte).
- [x] `POST /auth/mot-de-passe/oubli`, `/reinitialiser` — réel, jeton opaque haché, 1h d'expiration,
  révoque toutes les sessions actives à la réinitialisation. Note : le jeton en clair est
  journalisé en détail d'audit si `reglages.env != "production"` — comportement voulu pour le dev,
  à vérifier que `env` est bien positionné en prod (hors périmètre de cette session).
- [x] `GET /auth/sso/decouverte` — réel au sens "lit `Organisation.sso` en DB et répond selon
  `actif`", cohérent avec `securite.sso: persiste` du registre — ne prétend pas interroger un IdP.
- [ ] `POST /auth/sso/callback` — **non porté**, lève explicitement `erreurs.non_porte` avec un
  commentaire `# ponytail: l'échange de code OIDC (Authlib) arrive avec la fédération réelle`. Pas
  un faux succès, refus honnête et clairement documenté comme non fait — cohérent avec
  `securite.sso persiste` (persistance de la config, jamais d'appel IdP réel).

## 7. securite — `/securite/*`
Pas d'`amont()` externe : DB + RBAC réel. **Statut : pas d'amont, réel — RBAC vérifié appliqué, pas
décoratif.**

- [x] `exige(action)` (dans `synelia/deps/rbac.py`) — vérifié en lisant le code : refuse en 403 avec
  `rolesRequis`, journalise chaque refus (`rbac.refus`), respecte la portée déclarée d'une clé d'API
  (`p.portee`), et laisse passer l'équipe Synelia (`est_admin_plateforme`) sans re-checker le rôle
  d'org. Pas un simple `pass` — RBAC réellement appliqué à chaque endpoint de ce module (et de tous
  les autres audités).
- [x] `GET/POST /securite/cles-api`, `GET/PATCH/DELETE/{id}`, `POST /{id}/rotation` — DB réel,
  secret retourné une seule fois à la création/rotation (jamais relisible ensuite,
  `secret_hash` seul persiste). `_valider_portee` empêche une clé d'obtenir une portée que le rôle
  émetteur ne possède pas lui-même.
- [x] `GET/PUT /securite/politiques` — réel : un changement de `dureeMaxMin` révoque réellement
  toutes les sessions actives de l'organisation (pas juste un flag ignoré).
- [x] `GET /securite/sessions`, `DELETE /securite/sessions`, `DELETE /{id}` — réel, protège la
  session courante d'auto-révocation.
- [x] `_exiger_reauth_recente` — réel : applique concrètement
  `politiques.session.reauthentificationActionsSensibles` sur rotation/révocation de clé API et
  révocation globale des sessions (vérifie l'âge de la session courante, pas juste présent/absent).
- [x] `GET/PUT /securite/sso` — persistance JSONB honnête (`Organisation.sso`), ne simule aucun appel.
- [x] `POST /securite/sso/test` — **honnête par construction** : répond toujours `succes: false`
  avec un détail explicite ("Aucun courtier d'identité n'est configuré sur cette plateforme")
  plutôt que de fabriquer un succès. Correspond exactement à `securite.sso: persiste` du registre.
- **Aucun gap RBAC trouvé** dans ce module : chaque route passe par `exige(action)` explicite.

## 8. conformite — `/anomalies`, `/attestations`, `/conformite/rapports`
Pas d'`amont()` externe : DB + `@executeur` locaux (pas d'appel OpenStack/MinIO/etc.).
**Statut : pas d'amont, réel au sens "les 3 types de travaux ont un exécuteur qui modifie
vraiment la DB", mais le contenu produit est généré/synthétique (pas un vrai moteur de
conformité).**

- [x] `GET /anomalies` — DB, `exige(None)` (toute personne authentifiée avec un contexte d'org peut
  lire — pas de permission RBAC dédiée trouvée dans `synelia_contract.rbac` pour ce endpoint : à
  confirmer si c'est voulu ou un oubli, car `POST .../correctif` (qui *modifie* l'état) utilise
  aussi `exige(None)`, ce qui laisse n'importe quel membre authentifié déclencher un correctif
  d'anomalie sans permission RBAC spécifique — **potentiel gap RBAC**, à comparer avec
  `attestations`/`rapports` qui exigent, eux, `compliance.export`.
- [x] `POST /anomalies/{id}/correctif` — `demarrer_travail("conformite.correctif", ...)`, executor
  enregistré (`conformite/service.py:45`) : modifie réellement le statut de l'anomalie en DB selon
  la décision (`appliquer`/`ignorer`/autre). Réel mais mécanique simple (pas de vraie
  remédiation automatisée derrière, juste un changement de statut).
- [x] `GET /attestations`, `POST /attestations/{id}` — executor `attestation.generate` enregistré
  (`:53`) : marque `disponible: true` avec une empreinte (`nouvel_id()`, pas un vrai hash
  cryptographique du contenu) — **le mot "empreinte" suggère une preuve d'intégrité, mais c'est un
  UUID aléatoire, pas un hash du document.** Écart de premier principe mineur à signaler si
  l'attestation est censée être vérifiable.
- [x] `GET/POST /conformite/rapports` — executor `conformite.rapport` enregistré (`:64`) : crée un
  enregistrement de rapport avec une URL de téléchargement statique
  (`/v1/conformite/rapports/telechargement`) — **cette URL ne pointe vers aucune route existante
  trouvée dans `conformite/router.py`** (pas de `GET /conformite/rapports/telechargement` défini) :
  à vérifier, potentiel lien mort côté contrat.
- **Aucun type de travail orphelin** (les 3 types ont un exécuteur), mais deux écarts de premier
  principe à signaler : RBAC absent sur le correctif d'anomalie, et "empreinte" d'attestation qui
  n'est pas un vrai hash de contenu.

## 9. audit — `/audit/*`
Pas d'`amont()` propre au sens infra, mais un vrai exécuteur avec effet de bord réel (MinIO).
**Statut : réel**, y compris pour le point le plus sensible (intégrité de la chaîne de hachage).

- [x] `GET /audit` — DB, filtre riche (période, acteur, action, cible, résultat, `scopeType`).
- [x] `GET /audit/integrite` — **réel et le plus rigoureux du lot** : rejoue la chaîne de hachage
  (`verifier_chaine`) et signale la première ligne altérée. Docstring honnête sur les limites :
  *tamper-evident, pas tamper-proof* (protège contre une altération applicative, pas contre un
  superutilisateur Postgres qui bypasserait le rôle `SELECT, INSERT`-only).
  Registre : n'apparaît pas comme capacité à part dans `synelia capacites` (mécanisme interne,
  logique).
- [x] `POST /audit/export` — `demarrer_travail("audit.export", ...)`, executor enregistré
  (`audit/service.py:131`). **Réel et honnête** : génère vraiment un CSV/JSON depuis les lignes
  `Audit`, le dépose dans MinIO (`choisir_minio()` — capacité `minio.objet: simule` tant que
  `SYNELIA_MINIO_URL` n'est pas posée en CI, mais "réel complet" selon le registre), et refuse
  explicitement (`non_porte`) le format PDF plutôt que de le simuler : *"L'export PDF signé n'est
  pas fabriqué par cette plateforme"*.
- [x] `GET /audit/exports/{id}` — relit l'objet MinIO déposé par l'exécuteur ; 409 propre si le
  travail n'est pas encore `done`, 404 si le fichier a disparu.
- **Aucun gap trouvé** : c'est le module le plus soigné de l'universe, avec un vrai souci de ne
  jamais promettre plus que ce qui est fait (refus explicite PDF, docstring honnête sur les limites
  de l'intégrité).

---

## Synthèse des écarts trouvés (non corrigés, pour un futur passage)

1. **conformite — RBAC manquant sur `POST /anomalies/{id}/correctif`** : utilise `exige(None)`
   alors que l'action modifie de l'état organisationnel ; comparer avec `attestations`/`rapports`
   qui exigent `compliance.export`. À vérifier si voulu (tout membre peut traiter une anomalie de
   son org) ou oubli.
2. **conformite — "empreinte" d'attestation n'est pas un hash de contenu** : `nouvel_id()` (UUID
   aléatoire) plutôt qu'un hash cryptographique du document généré — le nom du champ promet une
   preuve d'intégrité que l'implémentation ne fournit pas.
3. **conformite — URL de téléchargement de rapport potentiellement morte** :
   `/v1/conformite/rapports/telechargement` renvoyée par l'exécuteur `conformite.rapport` ne
   correspond à aucune route trouvée dans `conformite/router.py` — à vérifier côté contrat/frontend.
4. **compte — activation MFA sans confirmation** : `POST /moi/mfa` active immédiatement sans
   exiger un code TOTP de vérification en retour, contrairement au flux habituel "scan → vérifie →
   active".
5. **projets — `demarrage`/`redemarrage`/`execution` de service partagent le type de travail
   `projet_service.create`** avec la création elle-même : pas un job manquant (l'exécuteur existe),
   mais l'idempotence de cet exécuteur pour un service déjà en cours n'a pas été relue ligne à ligne
   cette session — à ré-auditer.
6. **auth — SSO** : `POST /auth/sso/callback` non porté (refus explicite, cohérent avec
   `securite.sso: persiste` du registre) ; pas un bug mais rappel que toute la partie fédération
   d'identité (OIDC réel) reste à construire.

Aucun endpoint "silencieusement no-op" trouvé dans cette universe (tout type de travail démarré par
un router a un `@executeur` correspondant) — contrairement à ce qu'on pourrait craindre vu la taille
du module `projets`. Le point faible principal de l'universe est `conformite`, qui est fonctionnel
mais dont deux ou trois détails (RBAC, "empreinte", URL de téléchargement) ne correspondent pas
exactement à ce que le nom du champ/endpoint promet.

## Plan de travail (dans l'ordre)

1. Décider si `POST /anomalies/{id}/correctif` doit exiger une permission RBAC dédiée (probablement
   `compliance.remediate` ou réutiliser `compliance.export`) plutôt que `exige(None)`.
2. Remplacer l'« empreinte » d'attestation (`nouvel_id()`) par un vrai hash du contenu généré, ou
   renommer le champ si aucune preuve d'intégrité n'est prévue à court terme.
3. Vérifier/corriger l'URL `/v1/conformite/rapports/telechargement` (route manquante ou lien mort).
4. Décider si l'activation MFA doit exiger la saisie d'un code TOTP de confirmation avant de mettre
   `mfa_active = True`.
5. Relire ligne à ligne l'exécuteur `projet_service.create` pour confirmer son idempotence sur un
   service déjà démarré (cas `redemarrage`/`execution`).
6. Quand la fédération SSO réelle (Authlib/OIDC) sera prête, remplacer le refus explicite de
   `POST /auth/sso/callback` — actuellement le seul point honnêtement "non porté" de l'universe.
