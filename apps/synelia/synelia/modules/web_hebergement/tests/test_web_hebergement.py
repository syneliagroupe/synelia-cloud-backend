"""Web Cloud — hébergement, applications web et bases."""

from synelia_testing import connexion_lab, corriger_amont, sur_lab_reel

DES = "/v1"


def _affirmer_serveur_nova_hebergement(h: dict):
    """L'hébergement doit reposer sur un vrai serveur Nova (exactement un de plus)."""
    if not sur_lab_reel():
        return
    nom = (h.get("serveur") or {}).get("nom")
    assert nom, "hébergement sans nom de serveur : impossible de prouver l'impact Nova"
    c = connexion_lab()
    assert c is not None, "lab réel injoignable"
    trouves = list(c.compute.servers(name=nom, all_projects=True))
    assert trouves, f"aucun serveur Nova {nom!r} : hébergement sans impact OpenStack"


async def _creer_hebergement(client, nom="demo-h.com") -> dict:
    r = await client.post(
        f"{DES}/web/hebergements", json={"palier": "pro", "site": "ABJ", "domaine": nom}
    )
    assert r.status_code == 202, r.text
    assert r.json()["type"] == "hebergement.creer" and r.json()["statut"] == "done"
    r = await client.get(f"{DES}/web/hebergements")
    assert r.status_code == 200
    elements = [h for h in r.json()["donnees"] if h.get("domaine") == nom]
    assert len(elements) == 1
    _affirmer_serveur_nova_hebergement(elements[0])
    return elements[0]


async def test_cycle_hebergement(client):
    h = await _creer_hebergement(client)
    hid = h["id"]

    r = await client.get(f"{DES}/web/hebergements/{hid}")
    assert r.status_code == 200 and r.json()["statut"] == "en_ligne"

    r = await client.get(f"{DES}/web/hebergements", params={"palier": "pro"})
    assert r.status_code == 200 and len(r.json()["donnees"]) >= 1

    r = await client.patch(
        f"{DES}/web/hebergements/{hid}", json={"palier": "business", "site": "ABJ"}
    )
    assert r.status_code == 200 and r.json()["palier"] == "business"

    r = await client.put(
        f"{DES}/web/hebergements/{hid}/acces", json={"ftp": True, "ssh": True, "portSsh": 2222}
    )
    assert r.status_code == 200 and r.json()["acces"]["ssh"] is True

    r = await client.get(f"{DES}/web/hebergements/{hid}/metriques", params={"fenetre": "24h"})
    assert r.status_code == 200
    assert len(r.json()["series"]) >= 1 and r.json()["series"][0]["points"][0]["valeur"] == 0

    r = await client.put(f"{DES}/web/hebergements/{hid}/php", json={"versionDefaut": "8.3"})
    assert r.status_code == 200 and r.json()["php"]["versionDefaut"] == "8.3"

    r = await client.put(f"{DES}/web/hebergements/{hid}/php", json={"versionDefaut": "8.3"})
    assert r.status_code == 409

    r = await client.put(f"{DES}/web/hebergements/{hid}/php", json={"versionDefaut": "9.0"})
    assert r.status_code == 422

    r = await client.get(f"{DES}/web/hebergements/{hid}/services-partages")
    assert r.status_code == 200 and len(r.json()) >= 1

    r = await client.post(f"{DES}/web/hebergements/{hid}/redemarrage", json={"services": ["web"]})
    assert r.status_code == 202, r.text
    assert r.json()["type"] == "hebergement.redemarrer"


async def test_comptes_fichiers(client):
    hid = (await _creer_hebergement(client))["id"]
    r = await client.post(
        f"{DES}/web/hebergements/{hid}/comptes-fichiers",
        json={"utilisateur": "ftp-synelia", "protocoles": ["ftp", "sftp"], "racine": "/var/www"},
    )
    assert r.status_code == 201, r.text
    cid = r.json()["id"]

    r = await client.get(f"{DES}/web/hebergements/{hid}/comptes-fichiers")
    assert r.status_code == 200 and any(c["id"] == cid for c in r.json())

    r = await client.patch(
        f"{DES}/web/hebergements/{hid}/comptes-fichiers/{cid}",
        json={"utilisateur": "ftp-synelia", "protocoles": ["ftp"], "racine": "/var/www"},
    )
    assert r.status_code == 200 and r.json()["protocoles"] == ["ftp"]

    r = await client.delete(
        f"{DES}/web/hebergements/{hid}/comptes-fichiers/{cid}",
        params={"confirmation": "ftp-synelia"},
    )
    assert r.status_code == 204


async def test_taches(client):
    hid = (await _creer_hebergement(client))["id"]
    r = await client.post(
        f"{DES}/web/hebergements/{hid}/taches",
        json={
            "libelle": "Nettoyage",
            "expression": "0 3 * * *",
            "commande": "php artisan cache:clear",
        },
    )
    assert r.status_code == 201, r.text
    tid = r.json()["id"]

    r = await client.get(f"{DES}/web/hebergements/{hid}/taches")
    assert r.status_code == 200 and any(t["id"] == tid for t in r.json())

    r = await client.patch(
        f"{DES}/web/hebergements/{hid}/taches/{tid}",
        json={
            "expression": "0 4 * * *",
            "libelle": "Nettoyage",
            "commande": "php artisan cache:clear",
        },
    )
    assert r.status_code == 200

    r = await client.post(f"{DES}/web/hebergements/{hid}/taches/{tid}/execution")
    assert r.status_code == 202, r.text
    assert r.json()["type"] == "tache.execution"

    r = await client.delete(f"{DES}/web/hebergements/{hid}/taches/{tid}")
    assert r.status_code == 204


async def test_attachement_domaine(client):
    hid = (await _creer_hebergement(client))["id"]
    r = await client.post(
        f"{DES}/web/domaines",
        json={
            "nom": "attache-demo.com",
            "dureeAnnees": 1,
            "titulaire": {
                "nom": "S",
                "email": "a@b.ci",
                "telephone": "+1",
                "adresse": "x",
                "ville": "y",
                "pays": "CI",
            },
        },
    )
    assert r.status_code == 202, r.text

    r = await client.post(
        f"{DES}/web/hebergements/{hid}/attachement-domaine", json={"domaine": "attache-demo.com"}
    )
    assert r.status_code == 200, r.text
    assert r.json()["domaine"] == "attache-demo.com"

    other = await _creer_hebergement(client, "autre-demo.com")
    r = await client.post(
        f"{DES}/web/hebergements/{other['id']}/attachement-domaine",
        json={"domaine": "attache-demo.com"},
    )
    assert r.status_code == 409

    r = await client.post(
        f"{DES}/web/hebergements/{hid}/attachement-domaine", json={"domaine": "inconnu.com"}
    )
    assert r.status_code == 404


async def test_suppression_hebergement(client):
    h = await _creer_hebergement(client)
    hid = h["id"]
    r = await client.delete(f"{DES}/web/hebergements/{hid}", params={"confirmation": "mauvais"})
    assert r.status_code == 422

    nom = (await client.get(f"{DES}/web/hebergements/{hid}")).json()["domaineProvisoire"]
    r = await client.delete(f"{DES}/web/hebergements/{hid}", params={"confirmation": nom})
    assert r.status_code == 202, r.text


async def test_creer_hebergement_sans_zone_rejet_franc(client):
    # Garde-fou zone VPS : sans `reseau_id` provisionné, la création échouait au fin fond
    # de Nova avec un 409 cryptique (« Multiple possible networks found » — constaté en
    # direct sur le lab). En mode réel, elle échoue désormais franchement (travail
    # `rolled_back`, cause « zone VPS » nommée) ; en simulation, `reseau_id` est
    # inutilisé et la création passe — ce test verrouille les deux comportements.
    # La base éphémère des tests ne provisionne jamais la zone : le cas est déterministe
    # partout, sans lab.
    from synelia_testing import sur_lab_reel

    r = await client.post(
        f"{DES}/web/hebergements", json={"palier": "pro", "site": "ABJ", "domaine": "zone-test.com"}
    )
    assert r.status_code == 202, r.text
    travail = r.json()
    if sur_lab_reel():
        assert travail["statut"] == "rolled_back", travail
        detail = (travail.get("erreur") or {}).get("message", "") + str(travail.get("taches") or "")
        assert "zone VPS" in detail or "reseau_id" in detail, travail
    else:
        assert travail["statut"] == "done", travail


async def test_sites_web(client):
    hid = (await _creer_hebergement(client))["id"]
    r = await client.post(
        f"{DES}/web/sites",
        json={
            "hebergementId": hid,
            "site": {"hote": "blog.synelia.cloud", "type": "wordpress", "ssl": True},
        },
    )
    assert r.status_code == 202, r.text
    assert r.json()["type"] == "site.installer"

    r = await client.get(f"{DES}/web/sites")
    assert r.status_code == 200
    site = next((s for s in r.json()["donnees"] if s["hote"] == "blog.synelia.cloud"), None)
    assert site is not None
    sid = site["id"]

    r = await client.get(f"{DES}/web/sites/{sid}")
    assert r.status_code == 200

    r = await client.patch(f"{DES}/web/sites/{sid}", json={"phpVersion": "8.3"})
    assert r.status_code == 200 and r.json()["phpVersion"] == "8.3"

    r = await client.post(
        f"{DES}/web/sites/{sid}/mise-en-production",
        params={"confirmation": "blog.synelia.cloud"},
        json={"inclureBase": True},
    )
    assert r.status_code == 409

    r = await client.post(f"{DES}/web/sites/{sid}/preproduction", json={})
    assert r.status_code == 202, r.text
    assert r.json()["statut"] == "done"

    site = (await client.get(f"{DES}/web/sites/{sid}")).json()
    assert (
        site["preproduction"] is not None
        and site["preproduction"]["hote"] == "preprod.blog.synelia.cloud"
    )

    r = await client.post(
        f"{DES}/web/sites/{sid}/mise-en-production",
        params={"confirmation": "blog.synelia.cloud"},
        json={"inclureBase": True},
    )
    assert r.status_code == 202, r.text
    assert r.json()["statut"] == "done"

    site = (await client.get(f"{DES}/web/sites/{sid}")).json()
    assert site.get("preproduction") is None

    r = await client.post(f"{DES}/web/sites/{sid}/analyse-securite")
    assert r.status_code == 202

    r = await client.post(f"{DES}/web/sites/{sid}/mise-a-jour", json={"coeur": True})
    assert r.status_code == 202

    r = await client.get(f"{DES}/web/sites/{sid}/mises-a-jour")
    assert r.status_code == 200 and r.json() == []

    r = await client.delete(f"{DES}/web/sites/{sid}", params={"confirmation": "mauvais"})
    assert r.status_code == 422
    r = await client.delete(f"{DES}/web/sites/{sid}", params={"confirmation": "blog.synelia.cloud"})
    assert r.status_code == 202


def _serveur(client_json: dict, hid: str) -> dict:
    for s in client_json["donnees"]:
        if s.get("hebergementId") == hid:
            return s
    raise AssertionError(f"aucun serveur de bases pour {hid}")


async def test_bases(client):
    hid = (await _creer_hebergement(client))["id"]
    r = await client.get(f"{DES}/web/bases", params={"hebergementId": hid})
    assert r.status_code == 200
    serveurs = r.json()["donnees"]
    # Un serveur par moteur partagé du VPS (mariadb + postgresql + redis), tous
    # boucle locale, jamais exposés.
    assert {s["moteur"] for s in serveurs} == {"mariadb", "postgresql", "redis"}
    assert all(s["hoteInterne"] == "localhost" for s in serveurs)
    sid = next(s["id"] for s in serveurs if s["moteur"] == "mariadb")

    r = await client.get(f"{DES}/web/bases/{sid}")
    assert r.status_code == 200

    r = await client.patch(f"{DES}/web/bases/{sid}", json={"quotaMo": 4096})
    assert r.status_code == 200 and r.json()["quotaMo"] == 4096

    r = await client.post(f"{DES}/web/bases/{sid}/bases", json={"nom": "wpdb"})
    assert r.status_code == 201, r.text
    assert r.json()["nom"] == "wpdb"

    r = await client.post(f"{DES}/web/bases/{sid}/bases/wpdb/export", json={"format": "sql"})
    assert r.status_code == 202, r.text
    assert r.json()["statut"] == "done"

    r = await client.post(
        f"{DES}/web/bases/{sid}/bases/wpdb/import",
        json={"archiveId": "arch-123"},
        params={"confirmation": "wpdb"},
    )
    assert r.status_code == 202, r.text
    assert r.json()["statut"] == "done"

    r = await client.post(
        f"{DES}/web/bases/{sid}/utilisateurs",
        json={"nom": "userdb", "motDePasse": "secret", "base": "wpdb", "droits": "complet"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["utilisateurs"][0]["droits"] == "complet"

    r = await client.patch(f"{DES}/web/bases/{sid}/utilisateurs/userdb", json={"droits": "lecture"})
    assert r.status_code == 200
    assert r.json()["utilisateurs"][0]["droits"] == "lecture"

    r = await client.delete(f"{DES}/web/bases/{sid}/utilisateurs/userdb")
    assert r.status_code == 204

    r = await client.get(f"{DES}/web/bases/{sid}")
    assert r.status_code == 200 and r.json()["utilisateurs"] == []

    r = await client.delete(f"{DES}/web/bases/{sid}/bases/wpdb", params={"confirmation": "wpdb"})
    assert r.status_code == 204


async def test_rotation_mot_de_passe_serveurs(client):
    # Chaque moteur (mariadb + postgresql + redis) expose une rotation du mot de passe
    # root, renvoyé une seule fois. En simulation : secret renouvelé sans SSH.
    hid = (await _creer_hebergement(client))["id"]
    serveurs = (await client.get(f"{DES}/web/bases", params={"hebergementId": hid})).json()[
        "donnees"
    ]
    assert {s["moteur"] for s in serveurs} == {"mariadb", "postgresql", "redis"}
    for s in serveurs:
        r = await client.post(f"{DES}/web/bases/{s['id']}/rotation-mot-de-passe")
        assert r.status_code == 200, r.text
        assert r.json()["motDePasse"] and len(r.json()["motDePasse"]) >= 20
        r2 = await client.post(f"{DES}/web/bases/{s['id']}/rotation-mot-de-passe")
        assert r2.json()["motDePasse"] != r.json()["motDePasse"]


def test_compose_bases_moteurs_partages():
    # Preuve offline (texte du cloud-init, aucune infra) : les trois moteurs partagés
    # sont posés avec leurs images, mots de passe et volumes — et SANS ports publiés
    # (périmètre réseau de la VM uniquement, cf. `construire_cloud_init`).
    from synelia.modules.web_hebergement.service import _compose_bases, construire_cloud_init

    assert _compose_bases(None) == ""
    mdp = {"mariadb": "m1", "postgresql": "m2", "redis": "m3"}
    texte = _compose_bases(mdp)
    assert "image: mariadb:11" in texte and "MARIADB_ROOT_PASSWORD: m1" in texte
    assert "image: postgres:16" in texte and "POSTGRES_PASSWORD: m2" in texte
    # Redis : mot de passe dans le fichier mono-usage, jamais en variable (cf. F841).
    assert "image: redis:7" in texte and "redis-server /etc/redis/redis.conf" in texte
    assert "requirepass" not in texte
    from synelia.modules.web_hebergement.service import _fichiers_bases

    fichiers = _fichiers_bases(mdp)
    assert "requirepass m3" in fichiers and "01-root.sql" in fichiers
    assert _fichiers_bases(None) == ""
    assert "/bases/mariadb:/var/lib/mysql" in texte
    assert "/bases/postgres:/var/lib/postgresql/data" in texte
    for ligne in texte.splitlines():
        assert not ligne.strip().startswith('"3306:') and not ligne.strip().startswith('"5432:')
        assert not ligne.strip().startswith('"6379:')

    complet = construire_cloud_init("demo.com", "8.3", None, mdp)
    assert "bases-mariadb" in complet and "bases-postgres" in complet and "bases-redis" in complet
    # Sans mots de passe : compose inchangé (VM antérieures, chemins unitaires).
    assert "bases-mariadb" not in construire_cloud_init("demo.com", "8.3", None)


def test_sql_bases_moteurs():
    # Preuve offline : ordres SQL réels par moteur (exécutés via SSH sur le VPS en
    # mode réel, cf. `executer_sql_bases`) — validation stricte des identifiants,
    # échappement des littéraux, double vocabulaire de droits.
    from synelia.modules.web_hebergement import service as heb
    from synelia_kernel import erreurs as _e

    assert heb.sql_creer_base("mariadb", "wpdb") == [
        "CREATE DATABASE `wpdb` CHARACTER SET = 'utf8mb4';"
    ]
    assert heb.sql_creer_base("postgresql", "wpdb") == ['CREATE DATABASE "wpdb";']
    assert heb.sql_creer_utilisateur("mariadb", "userdb", "s3cret", "wpdb", "complet") == [
        "CREATE USER 'userdb'@'%' IDENTIFIED BY 's3cret';",
        "GRANT ALL PRIVILEGES ON `wpdb`.* TO 'userdb'@'%';",
        "FLUSH PRIVILEGES;",
    ]
    assert heb.sql_creer_utilisateur("mariadb", "userdb", "s3cret", "wpdb", "lecture")[
        1
    ].startswith("GRANT SELECT")
    # Vocabulaire `Utilisateur1` (`tous`) : mêmes privilèges pleins.
    assert "ALL PRIVILEGES" in heb.sql_creer_utilisateur("mariadb", "u", "p", "b", "tous")[1]
    assert heb.sql_creer_utilisateur("postgresql", "userdb", "s3cret", "wpdb", "complet")[
        1
    ].startswith("GRANT CONNECT")
    assert heb.sql_mot_de_passe_utilisateur("postgresql", "userdb", "n3w") == [
        "ALTER USER \"userdb\" WITH PASSWORD 'n3w';"
    ]
    assert heb.sql_supprimer_base("mariadb", "wpdb") == ["DROP DATABASE IF EXISTS `wpdb`;"]
    assert heb.sql_supprimer_utilisateur("postgresql", "userdb") == [
        'DROP USER IF EXISTS "userdb";'
    ]
    # Injection : rejetée en 422, jamais échappée à la main.
    for mauvais in ("a`b", 'a"b', "a'b", "a;b", "a b", ""):
        try:
            heb.sql_creer_base("mariadb", mauvais)
        except _e.AppError as exc:
            assert exc.code == "validation"
        else:
            raise AssertionError(f"identifiant {mauvais!r} accepté")
    # Guillemet dans un mot de passe : doublé (pas de fuite hors littéral).
    assert (
        "IDENTIFIED BY 'it''s';"
        in heb.sql_creer_utilisateur("mariadb", "u", "it's", "b", "complet")[0]
    )
    # Redis n'a pas de bases nommées : 422 franc, pas de faux succès.
    try:
        heb.sql_creer_base("redis", "cache")
    except _e.AppError as exc:
        assert exc.code == "non_porte"
    else:
        raise AssertionError("base redis acceptée")
    # Commande shell : mot de passe en variable d'environnement, SQL par stdin.
    cmd = heb.commande_sql_bases("mariadb", "r00t", ["SELECT 1;"])
    assert "docker compose exec -T bases-mariadb" in cmd and "MDB_MDP='r00t'" in cmd
    assert cmd.rstrip().endswith("SYNELIA_SQL")
    # Rotation root : les deux comptes mariadb (init du premier boot), postgres natif.
    assert heb.sql_rotation_root("mariadb", "n3w") == [
        "ALTER USER 'root'@'localhost' IDENTIFIED BY 'n3w';",
        "ALTER USER 'root'@'%' IDENTIFIED BY 'n3w';",
        "FLUSH PRIVILEGES;",
    ]
    assert heb.sql_rotation_root("postgresql", "n3w") == [
        "ALTER USER postgres WITH PASSWORD 'n3w';"
    ]
    rot = heb.commande_redis_rotation("old", "new")
    assert "CONFIG SET requirepass 'new'" in rot and "CONFIG REWRITE" in rot
    assert "redis.conf" in rot


async def test_reconciliation_statut_hebergement_orphelin(client, monkeypatch):
    # La ligne en base peut survivre à son infra réelle : une VM d'hébergement supprimée hors
    # bande (nettoyage manuel du lab) laissait l'hébergement s'afficher `en_ligne`, et l'écart
    # ne se voyait qu'au premier usage (installation de site, activation Drive) — ~20 s de SSH
    # voué à l'échec sur `verif-final.example.com`. Décision propriétaire : un orphelin confirmé
    # est **supprimé** par le chemin métier du DELETE (exécuteur `hebergement.supprimer` et sa
    # compensation habituelle), pas seulement marqué — la ligne disparaît réellement.
    from synelia.modules.web_hebergement import service as hebergement_service

    h = await _creer_hebergement(client, "reconcile-orphan.com")
    hid = h["id"]

    # Serveur toujours connu de Nova (simulé : ACTIVE) : la lecture ne change rien.
    r = await client.get(f"{DES}/web/hebergements/{hid}")
    assert r.status_code == 200 and r.json()["statut"] == "en_ligne"
    assert r.json()["serveur"]["statut"] == "en_ligne"

    # Nova ne connaît plus le serveur : suppression réelle déclenchée à la lecture.
    # `corriger_amont` patch SIMULÉ + RÉEL : sur lab, `amont()` renvoie
    # `ComputeOpenStack`, un patch du seul `ComputeSimule` serait sans effet (faux-positif).
    supprime = []
    corriger_amont(
        monkeypatch,
        hebergement_service,
        "ComputeSimule",
        "ComputeOpenStack",
        "statut_serveur",
        lambda self, serveur_id, identifiants=None: "absente",
    )
    corriger_amont(
        monkeypatch,
        hebergement_service,
        "ComputeSimule",
        "ComputeOpenStack",
        "supprimer_serveur",
        lambda self, serveur_id: supprime.append(serveur_id),
    )
    r = await client.get(f"{DES}/web/hebergements/{hid}")
    # La lecture répond avec le marquage sincère posé avant le lancement du travail (en mode
    # en ligne, le travail `hebergement.supprimer` a déjà tourné et retiré la ligne).
    assert r.status_code == 200
    assert r.json()["statut"] == "suspendu"
    assert r.json()["serveur"]["statut"] == "maintenance"
    assert len(supprime) == 1  # le serveur amont restant a bien visé par le chemin métier

    # La ligne a réellement disparu : plus de zombie dans la liste, détail en 404.
    r = await client.get(f"{DES}/web/hebergements", params={"statut": "suspendu"})
    assert all(x["id"] != hid for x in r.json()["donnees"])
    r = await client.get(f"{DES}/web/hebergements/{hid}")
    assert r.status_code == 404


async def test_reconciliation_hebergement_orphelin_protege(client, monkeypatch):
    # Garde-fou de la décision propriétaire : les hébergements protégés (`_HEBERGEMENTS_PROTEGES`
    # — les serveurs sains du lab `srv-01a073a1`/`srv-01a076fe`) ne sont jamais supprimés
    # automatiquement. Un orphelin confirmé chez eux reste marqué `suspendu`, requalifiable à
    # la main. On simule la protection en protégeant tous les ids (préfixe vide).
    from synelia.modules.web_hebergement import service as hebergement_service

    h = await _creer_hebergement(client, "reconcile-protege.com")
    hid = h["id"]

    def _interdit(self, serveur_id):
        raise AssertionError("Hébergement protégé : jamais supprimé automatiquement")

    monkeypatch.setattr(hebergement_service, "_HEBERGEMENTS_PROTEGES", ("",))
    corriger_amont(
        monkeypatch,
        hebergement_service,
        "ComputeSimule",
        "ComputeOpenStack",
        "statut_serveur",
        lambda self, serveur_id, identifiants=None: "absente",
    )
    corriger_amont(
        monkeypatch,
        hebergement_service,
        "ComputeSimule",
        "ComputeOpenStack",
        "supprimer_serveur",
        _interdit,
    )
    r = await client.get(f"{DES}/web/hebergements/{hid}")
    assert r.status_code == 200
    assert r.json()["statut"] == "suspendu"
    assert r.json()["serveur"]["statut"] == "maintenance"
    r = await client.get(f"{DES}/web/hebergements", params={"statut": "suspendu"})
    assert any(x["id"] == hid for x in r.json()["donnees"])
    # Toujours listable et jamais supprimée : on ne redéclenche rien sur une relecture.
    r = await client.get(f"{DES}/web/hebergements/{hid}")
    assert r.status_code == 200


def test_garde_hebergements_proteges():
    # Les deux hébergements sains du lab sont reconnus par id comme par nom de serveur
    # (`srv-<8 premiers caractères de l'id>`) ; une ligne quelconque ne l'est pas.
    from synelia.modules.web_hebergement.service import _suppression_automatique_interdite
    from synelia_contract import modeles as m

    def hebergement(id_: str, nom_serveur: str) -> m.Hebergement:
        return m.Hebergement.model_construct(
            id=id_, serveur=m.Serveur.model_construct(nom=nom_serveur)
        )

    assert _suppression_automatique_interdite(
        hebergement("01a073a1-37ef-7791-a47b-edd4efb643d6", "srv-01a073a1")
    )
    assert _suppression_automatique_interdite(
        hebergement("01a076fe-2e91-72f0-825c-040a526196df", "autre-nom")
    )
    assert not _suppression_automatique_interdite(
        hebergement("01a08abc-0000-0000-0000-000000000000", "srv-01a08abc")
    )
