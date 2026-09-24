# Web Cloud — inventaire des fonctionnalités (état réel, vérifié le 2026-09-24)

Source : lecture directe de `apps/synelia/synelia/modules/web_*` + `uv run synelia capacites` +
tests réels exécutés dans cette session contre le lab OpenStack (`192.168.26.0/24`) et l'API OVH.
Ne pas supposer un statut : chaque ligne a été vérifiée dans le code ou par un appel réel.

Légende : **réel** = appelle un amont véritable (OpenStack/OVH/Designate/Zimbra/SSH) ;
**simulé** = classe `*Simule`, aucun appel externe, données fabriquées ; **partiel** = mélange
des deux selon l'endpoint ; **non vérifié** = pas encore audité dans cette session.

## 1. web_domaines — domaines & registrar
Backend : `RegistrarOvh` (nouveau, cette session) via `SYNELIA_REGISTRAR_URL` + 3 identifiants OVH.
**Statut global : réel**, vérifié par un achat effectif (`synelia-test-check-9284.xyz`, €1.89, ordre OVH 259106580).

- [x] `GET /web/domaines/disponibilite` — **corrigé cette session** : appelle maintenant `registrar.verifier()` réel. Bug trouvé et corrigé en cours de route : OVH renvoie `orderable: true` aussi pour un domaine déjà pris ailleurs (`action: "transfer"` au lieu de `"create"`) — sans filtrer sur `action`, `google.com` ressortait "disponible". Revérifié en direct après fix : `google.com` → indisponible, notre domaine possédé → indisponible, un nom neuf → disponible avec vrai prix OVH.
- [x] `POST /web/domaines` (commander) — réel, corrigé cette session (l'exécuteur n'appelait jamais `amont().commander()` avant ; maintenant si).
- [x] `GET /web/domaines`, `GET /web/domaines/{id}` — réel (lecture DB + agrégats).
- [x] `PATCH /web/domaines/{id}` — réel (DB).
- [x] `POST /web/domaines/{id}/code-auth` — réel (appelle `amont().code_auth()`).
- [x] `POST /web/domaines/{id}/renouvellement` — réel, corrigé cette session (idem commander, jamais appelé l'amont avant).
- [x] `POST /web/domaines/transferts` — réel, corrigé cette session (idem).
- [x] `GET /web/domaines/disponibilite` — appelle `service.amont().verifier()` (OVH quand branché), plus seulement la base locale.

## 2. web_dns — zones & enregistrements DNS
Backend : `DesignateOpenStack` via `SYNELIA_FOURNISSEUR=openstack`. **Statut : réel**, vérifié (zone créée, enregistrement A ajouté, réellement via Designate — NS renvoyés : `ns1.lab.local.`, interne au lab, pas délégué publiquement).

- [x] `GET /web/dns`, `POST /web/dns` (créer zone) — réel, testé.
- [x] `GET /web/dns/modeles` — statique (catalogue de modèles DNS courrier/DMARC/etc.), pas d'amont à tester.
- [x] `GET/DELETE /web/dns/{zoneId}` — réel.
- [x] `PUT /web/dns/{zoneId}/dnssec` — non testé cette session.
- [x] `POST /web/dns/{zoneId}/enregistrements`, `PUT .../enregistrements` (bulk) — testé (POST), PUT bulk non testé.
- [x] `PATCH/DELETE /web/dns/{zoneId}/enregistrements/{id}` — non testé.
- [x] `POST /web/dns/{zoneId}/modeles/{modeleId}/application` — non testé (applique un modèle DNS prédéfini).
- **Écart** : NS publics ≠ OVH — délégation réelle (pointer les NS OVH du domaine vers ceux de Designate) non faite, nécessite un appel OVH `/domain/zone/{domain}/nameServers` ou équivalent, non implémenté.

## 3. web_hebergement — VPS hosting, bases, sites, fichiers, tâches
Backend : `ComputeOpenStack`/`NetworkOpenStack`/`SshReel`/`IdentiteOpenStack` via `SYNELIA_FOURNISSEUR=openstack`.
**Statut : réel mais actuellement bloqué en bout de chaîne** — VM Nova créée avec succès (vérifié 2×
réellement), mais le routage sur le load balancer partagé Octavia échoue : panne réseau VXLAN
diagnostiquée (tunnel ctrl1↔comp1 manquant, partiellement réparé, ARP vers l'amphore encore mort).
Root cause documentée dans la conversation ; pas encore résolue.

- [x] `POST /web/hebergements` (créer) — réel, VM créée ; bloque à l'étape 3 (LB). Corrigé cette session : capacité disque comp1 épuisée (+200 Go ajoutés côté hyperviseur dev01).
- [x] `GET /web/hebergements`, `GET/{id}` — réel + reconcile-on-read testé (a correctement détecté et nettoyé 6 lignes orphelines pendant cette session).
- [ ] `PATCH /web/hebergements/{id}` — non testé.
- [ ] `DELETE /web/hebergements/{id}` — testé indirectement (nettoyage), a échoué une fois sur "introuvable" car la ligne était déjà auto-nettoyée par reconcile-on-read — comportement correct, pas un bug.
- [ ] `PUT /web/hebergements/{id}/acces` — non testé.
- [ ] `POST /web/hebergements/{id}/attachement-domaine` — non testé.
- [ ] `GET/POST/PATCH/DELETE .../comptes-fichiers` — non testé (FTP/SFTP).
- [ ] `GET .../metriques` — non testé.
- [ ] `PUT .../php` — non testé.
- [ ] `POST .../redemarrage` — non testé.
- [ ] `GET .../services-partages` — non testé.
- [ ] `GET/POST/PATCH/DELETE .../taches` (cron) — non testé.
- [ ] `POST .../taches/{id}/execution` — non testé.
- [ ] Sites (`POST /web/sites`, installer une app) — non testé, dépend d'un hébergement `en_ligne` (bloqué par le point ci-dessus).
- [ ] Bases de données (`GET/POST/PATCH/DELETE /web/bases...`) — non testé.

## 4. web_ssl — certificats
Backend : `AcmeReel` via `SYNELIA_ACME_URL` (registre indique **simulé par défaut**, jamais câblé sur ce lab — aucun partenaire ACME/CA configuré). **Statut : simulé** (confirmé par le registre de capacités, pas re-testé directement cette session).

- [ ] Tous les endpoints (`/web/ssl/offres`, commander, lister, obtenir, patch, supprimer, renouveler, valider) — non re-testés cette session ; d'après le registre, tourne en simulé tant que `SYNELIA_ACME_URL` n'est pas posée.

## 5. web_emails — messagerie (Zimbra)
Backend : `zimbra.ZimbraReel` via `SYNELIA_ZIMBRA_URL`+identifiants (registre indique **réel**, "déployé sur vm-admin"). **Statut : probablement réel** d'après le registre, non re-testé cette session.

- [ ] Tous les endpoints (créer/lister/modifier/supprimer messagerie, alias, comptes, ouverture SSO) — non re-testés cette session.

## 6. web_smtp — relais SMTP sortant
Backend : `relais_smtp.RelaisSmtpReel` via `SYNELIA_ZIMBRA_SMTP_HOTE` (registre indique **réel** pour `relais_smtp.envoi`). **Statut : probablement réel**, non re-testé cette session.

- [ ] Tous les endpoints (relais, clés SMTP, test, webhooks) — non re-testés cette session.

## 7. web_drive — stockage collaboratif (type Nextcloud sur le VPS)
Backend : `SshReel` (déploiement via SSH sur le VPS d'hébergement, pas un service séparé). **Statut : réel mais dépend d'un hébergement `en_ligne`** — donc actuellement bloqué par le même problème Octavia que web_hebergement (aucun VPS n'a encore atteint `en_ligne` dans cette session).

- [ ] Tous les endpoints — non testés (bloqués par la dépendance hébergement).

## 8. web_backup — sauvegardes
Backend : délègue à `web_hebergement.service.amont()` (Nova/Glance), pas d'`amont()` propre.
**Statut : réel et honnête**, audité en détail cette session (lecture complète de `service.py`) :
- `web.backup.run` : crée un vrai snapshot Glance de la VM (`amont().instantane`), stocke l'id en secret. Sans serveur réel (démo), dégrade proprement sans inventer un succès.
- `web.backup.testrestauration` : vérifie que l'image Glance existe toujours et est `active` — pas juste "on suppose que oui".
- `web.backup.restore` : restaure réellement via `amont().restaurer` — mais **seulement en granularité `"complete"`** (VM entière). Le registre de capacités le disait "partiel", et c'est confirmé : `perimetre` (fichiers/bases/configuration/messagerie séparés) est un champ du contrat qui **ne correspond à aucune implémentation** — seule une restauration VM entière existe. Écart de premier principe : soit l'API ne devrait pas laisser croire à une restauration sélective, soit il manque une implémentation par granularité (export SQL, rsync fichiers, etc. séparés du snapshot VM).

- [x] Restauration avec `granularite != "complete"` — corrigé : job `failed` + message explicite (plus de faux succès silencieux).

---

## Plan de travail (dans l'ordre)

1. **Débloquer Octavia/VXLAN** (bloque hebergement, drive, sites, et les tests intégration `test_cycle_sauvegarde` / `test_cycle_hebergement`) — cf. `docs/runbooks/lab-openstack.md` et `scripts/fix-octavia-lb.sh`.
2. Une fois hébergement `en_ligne` : tester sites, bases, comptes-fichiers, tâches, drive.
3. Re-tester web_ssl, web_emails, web_smtp pour confirmer leur statut réel/simulé (registre à jour, pas re-validé par appel direct cette session).
4. Optionnel (discuté, pas encore autorisé) : délégation DNS publique réelle vers OVH pour le domaine de test.
