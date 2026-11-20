# Bases & Modèles — inventaire des fonctionnalités (état réel, vérifié le 2026-09-24)

Source : lecture directe de `apps/synelia/synelia/modules/bases/` et
`apps/synelia/synelia/modules/modeles/` (router.py + service.py) + `uv run synelia capacites`.
Aucun appel externe/OpenStack réel effectué (`.env` a `SYNELIA_FOURNISSEUR=openstack`, donc
lab live joignable en théorie — analyse volontairement limitée à la lecture de code par
prudence, conformément à la consigne). Ne pas supposer un statut : chaque ligne est vérifiée
dans le code.

Légende : **réel** = appelle un amont véritable (OpenStack) ; **simulé** = classe `*Simule`,
aucun appel externe ; **stub/non implémenté** = le endpoint existe et répond 2xx mais ne fait
rien d'utile (faux succès silencieux) ; **partiel** = mélange selon le endpoint.

## 1. bases — bases de données managées (PostgreSQL/MySQL/MariaDB/MongoDB/Redis)

Ce module n'est **pas** une offre DBaaS OpenStack (pas de Trove) : c'est une VM Nova
provisionnée par `bases.service` elle-même (via `ComputeOpenStack`/`ComputeSimule`, le même
`amont()` que `vms`/`web_hebergement`), avec le moteur de base de données lancé en conteneur
Docker via cloud-init (`construire_cloud_init`). Autrement dit « base managée » = une VM
générique + `docker run postgres/mysql/mongo/redis`, pas un service Trove/RDS dédié. Le
registre `synelia capacites` ne liste d'ailleurs aucune entrée `bases.*` propre — le module
consomme intégralement `compute.serveur` (**réel** dans le registre).

Backend : `fournisseur(ComputeSimule, ComputeOpenStack)` via `SYNELIA_FOURNISSEUR`.
**Statut global : réel pour le cycle de vie du serveur (create/delete/replica), mais deux
endpoints sont des stubs qui répondent succès sans rien faire de correct.**

- [x] `GET /bases`, `GET /bases/{baseId}` — réel (lecture DB).
- [x] `POST /bases` (créer) — réel : crée effectivement un serveur Nova via
  `amont().creer_serveur` avec cloud-init spécifique au moteur (image Docker officielle +
  variables d'env + point de restauration réseau privé uniquement, pas d'IP flottante —
  cohérent avec `sourcesAutorisees`). Exécuteur `base.create` correctement enregistré,
  compensable (rollback via `supprimer_serveur` si l'étape échoue).
  - **Écart de premier principe** : les champs `ha` (haute disponibilité) et `pitr` (point-in-time
    recovery) du contrat de création sont acceptés, stockés (`bool(corps.ha)`, `bool(corps.pitr)`)
    et renvoyés tels quels dans les réponses, mais **ne déclenchent aucun comportement** : pas de
    réplica créé automatiquement pour `ha=true`, pas de plan de snapshot/WAL programmé pour
    `pitr=true`. Un client qui active `ha`/`pitr` à la création obtient une base identique à
    `ha=false`/`pitr=false`, sans erreur ni avertissement — faux calme silencieux.
- [x] `PATCH /bases/{baseId}` — réel (DB uniquement, pas de propagation vers le serveur amont,
  cohérent avec le fait qu'il n'y a rien à reconfigurer côté OpenStack pour un champ métier).
- [x] `DELETE /bases/{baseId}` — réel, exécuteur `base.delete` enregistré, supprime le serveur
  Nova amont puis la ligne (suppression logique).
- [x] `GET /bases/{baseId}/identifiants` — réel (lecture des secrets stockés + construction de
  l'URI de connexion). Pas d'appel amont (attendu : c'est une lecture pure).
- [x] `POST /bases/{baseId}/identifiants/rotation` — **partiel, documenté honnêtement dans le
  code** (commentaire ligne 191-193) : génère et stocke un nouveau mot de passe réel côté API,
  mais ne le pousse **pas** vers le moteur qui tourne dans le conteneur Docker (pas d'accès
  SSH/exec depuis l'API) — donc après rotation, l'ancien mot de passe reste valide côté serveur
  tant qu'aucune action manuelle n'est faite. Le endpoint ne ment pas sur ce qu'il fait (pas de
  faux succès), mais le contrat client peut laisser croire à une rotation effective de bout en
  bout.
- [ ] `GET /bases/{baseId}/metriques` — **stub / non implémenté** : retourne toujours
  `series=[]` inconditionnellement (`return m.BasesBaseIdMetriquesGetResponse(series=[])`,
  ligne 223), sans même consulter `victoria.observabilite` (qui existe pourtant comme amont
  simulable dans le registre de capacités, cf. `web_hebergement`/`victoria`). Le paramètre
  `fenetre` est accepté mais totalement ignoré. Écart de premier principe : l'endpoint existe et
  répond 200 avec succès, mais ne fournit jamais de métriques, quel que soit l'état réel de la
  base.
- [x] `POST /bases/{baseId}/replicas` — réel côté DB (incrémente `replicas`), exécuteur
  `base.replica` enregistré — mais **ne provisionne aucun serveur amont** : `terminer()` se
  contente d'écrire `replicas = replicas + 1` en base, sans jamais appeler `amont().creer_serveur`
  ni aucun équivalent. C'est un **faux succès silencieux** : les étapes annoncées
  (« Provisionner le réplica de lecture », « Synchroniser les données », 100s au total) ne
  correspondent à aucune action réelle exécutée — seul le compteur bouge. Comparable au bug
  historique de web_backup (executor manquant), sauf qu'ici l'exécuteur existe bien mais son
  contenu est un stub qui ne fait pas ce que son nom promet.
- [ ] `POST /bases/{baseId}/restauration` (PITR) — **stub trompeur, bug le plus sérieux du
  module.** Le contrat (`BasesBaseIdRestaurationPostRequest`) exige `instant` (horodatage cible)
  et `nomCible` (nom de la base restaurée). L'exécuteur `base.restore` (service.py:232-242)
  **ignore complètement ces deux champs** : il se contente d'appeler `amont().action(sid,
  "redemarrage")` (un simple redémarrage du serveur Nova existant) puis marque le statut
  `running`. Aucune restauration de données à un instant T n'a lieu, aucune nouvelle base nommée
  `nomCible` n'est créée. Le job se termine en succès (`terminer()` ne signale aucune erreur),
  ce qui constitue un **faux succès silencieux** identique dans l'esprit au bug historique de
  `web_backup` (exécuteur de restauration manquant) — sauf qu'ici l'exécuteur existe et répond
  succès sans avoir rien restauré. Un client qui demande une restauration à J-3 obtient un simple
  redémarrage de sa base actuelle, aucune perte de données récentes n'est annulée.

**Registrations d'exécuteurs vs appels routeur** : les 4 appels `demarrer_travail(ctx,
"base.*", ...)` (`base.create`, `base.delete`, `base.replica`, `base.restore`) ont bien chacun
un `@executeur("base.*")` correspondant — pas de cas de "job démarré sans exécuteur enregistré"
ici (contrairement au précédent web_backup cité dans les consignes). Le problème n'est pas
l'absence d'exécuteur mais le **contenu incomplet** de deux d'entre eux (`replica`, `restore`).

## 2. modeles — catalogue d'applications préconfigurées (gabarits/templates applicatifs)

Ce n'est **pas** un catalogue d'images VM/OS (pas de gabarits Glance) : c'est un catalogue
métier statique de modèles d'applications tierces (WordPress, Next.js, Django, Laravel, Odoo,
n8n, Metabase, GitLab CE) avec leurs ressources par défaut, dépendances, volumes, ports et
politique de sauvegarde par défaut — destiné à préremplir un futur déploiement (`web_hebergement`
/ sites) plutôt qu'à créer une infrastructure lui-même. Aucun amont OpenStack n'est appelé par ce
module — cohérent avec sa nature de catalogue en lecture seule.

**Statut global : réel et honnête, mais périmètre volontairement restreint** : aucune notion de
"simulé" ne s'applique ici, il n'y a pas d'amont externe à simuler. Le module se contente de
servir 8 fiches produit codées en dur (`SEMENCES`) et de calculer une estimation de coût.

- [x] `GET /modeles` — réel : liste les 8 modèles semés au premier appel (`_semer` idempotent,
  ne resème pas si `depot.compter(ctx) > 0`), filtrable par `categorie`/`certifie`/`populaire`.
- [x] `GET /modeles/{slug}` — réel : sert directement la fiche statique en mémoire
  (`SEMENCE_PAR_SLUG`) sans repasser par le dépôt si le slug est connu — évite un aller-retour DB
  inutile, comportement correct mais légèrement incohérent avec le chemin `depot.trouver` utilisé
  pour un slug inconnu (deux chemins de lecture différents selon la source des données, sans
  impact fonctionnel observé).
- [x] `POST /modeles/{slug}/estimation` — réel : calcul de coût pur (`_ht`) à partir des grilles
  tarifaires globales `PRIX` (vCPU/RAM/stockage, partagées avec `web_hebergement`/`facturation`),
  pas de simulation, pas d'amont à appeler. TVA appliquée via `argent.tva`. Le paramètre `sieges`
  applique un tarif fixe de 7500 par siège — cohérent avec les modèles SaaS type GitLab/Odoo qui
  ont une notion d'utilisateurs, moins évident pour WordPress/Next.js où le champ n'a pas de sens
  métier (le endpoint ne le refuse pas, il facture simplement des sièges sur un modèle qui n'en a
  pas — écart mineur, pas un bug bloquant).

Aucun `demarrer_travail` dans ce module : pas de job de provisioning, pas d'exécuteur à
enregistrer — cohérent avec sa nature de catalogue, aucun écart trouvé ici. Le module ne fait
que ce que son contrat promet : afficher un catalogue et estimer un coût, sans jamais prétendre
déployer quoi que ce soit lui-même (le déploiement effectif d'un modèle relèverait de
`web_hebergement`/sites, hors périmètre de ce module).

---

## Plan de travail (dans l'ordre)

1. **`bases` — corriger `POST /{baseId}/restauration`** : soit implémenter une vraie restauration
   PITR (export/import de données à l'instant demandé dans une base `nomCible` séparée, en
   s'inspirant du pattern `web_backup` : snapshot Glance + restauration réelle), soit faire
   échouer explicitement le job (`failed` + message clair) tant que l'implémentation n'existe
   pas — ne jamais renvoyer un succès silencieux qui n'a fait qu'un redémarrage. Priorité haute :
   c'est une fonctionnalité de sécurité des données, la même classe de bug que celui corrigé
   récemment sur `web_backup`.
2. **`bases` — corriger `POST /{baseId}/replicas`** : soit provisionner réellement un serveur
   amont pour le réplica de lecture (appel `amont().creer_serveur` manquant dans
   `ExecuteurBaseReplica.terminer`), soit documenter clairement que la fonctionnalité n'est pas
   implémentée et faire échouer le job.
3. **`bases` — `GET /{baseId}/metriques`** : brancher sur `victoria.observabilite` (déjà simulé
   ailleurs dans le registre) au lieu de renvoyer `series=[]` en dur, ou documenter
   explicitement "non implémenté" dans le contrat/registre plutôt que de laisser croire à un
   endpoint fonctionnel qui retourne juste "pas de données".
4. **`bases` — champs `ha`/`pitr` à la création** : soit les câbler à un comportement réel
   (réplica auto pour `ha`, plan de snapshot pour `pitr`), soit les retirer du contrat de
   création tant qu'ils ne font rien, pour éviter d'induire le client en erreur sur les
   garanties de la base créée.
5. **`bases` — rotation d'identifiants** : documenter clairement côté contrat/doc publique que
   la rotation ne se propage pas au moteur en conteneur (déjà fait en commentaire interne, à
   remonter dans la doc API si elle existe), ou implémenter un canal d'exécution distante
   (SSH/exec) pour appliquer réellement le nouveau mot de passe.
6. **`modeles`** : aucun correctif nécessaire — module conforme à son contrat, périmètre
   volontairement statique/lecture seule.
