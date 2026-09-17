import pytest

pytestmark = pytest.mark.anyio


async def _espace(client, code="projet-abj"):
    r = await client.post(
        "/v1/espaces",
        json={
            "code": code,
            "offerId": "offre-standard",
            "site": "ABJ",
            "cidr": "10.10.0.0/16",
            "quota": {"vcpu": 32, "ramGo": 64, "stockageTo": 4},
        },
    )
    assert r.status_code == 202, r.text
    r = await client.get("/v1/espaces")
    return r.json()["donnees"][0]["id"]


async def _projet(client, espace_id, nom="Blog"):
    r = await client.post(
        "/v1/projets", json={"nom": nom, "description": "Blog d'équipe", "espaceId": espace_id}
    )
    assert r.status_code == 201, r.text
    return r.json()


async def _service(client, projet_id, nom="web", type_="application"):
    r = await client.post(
        f"/v1/projets/{projet_id}/services",
        json={
            "nom": nom,
            "type": type_,
            "environnement": "production",
            "ressources": {"cpu": 1, "ramMo": 1024, "diskGo": 10},
        },
    )
    assert r.status_code == 202, r.text
    return r


async def test_cycle_projet(client):
    espace_id = await _espace(client)
    await _projet(client, espace_id, "CRM")

    r = await client.get("/v1/projets")
    assert r.status_code == 200 and len(r.json()["donnees"]) == 1
    pid = r.json()["donnees"][0]["id"]

    r = await client.get(f"/v1/projets/{pid}")
    assert r.status_code == 200 and r.json()["nom"] == "CRM"

    r = await client.post("/v1/projets", json={"nom": "CRM", "espaceId": espace_id})
    assert r.status_code == 409 and r.json()["erreur"]["code"] == "nom_deja_pris"

    r = await client.patch(f"/v1/projets/{pid}", json={"nom": "CRM 2", "espaceId": espace_id})
    assert r.status_code == 200 and r.json()["nom"] == "CRM 2"

    r = await client.delete(f"/v1/projets/{pid}", params={"confirmation": "mauvais"})
    assert r.status_code == 422

    r = await client.delete(f"/v1/projets/{pid}", params={"confirmation": "CRM 2"})
    assert r.status_code == 202
    r = await client.get("/v1/projets")
    assert r.json()["pagination"]["total"] == 0


async def test_synthese_projets(client):
    espace_id = await _espace(client)
    await _projet(client, espace_id, "Espace")
    r = await client.get("/v1/projets/synthese")
    assert r.status_code == 200
    corps = r.json()
    assert corps["projets"] == 1 and corps["services"] == 0
    assert isinstance(corps["coutMensuel"], int)


async def test_cycle_service(client):
    espace_id = await _espace(client)
    projet = await _projet(client, espace_id)
    pid = projet["id"]

    r = await _service(client, pid, "front", "application")
    travail = r.json()
    assert travail["statut"] == "done" and all(t["statut"] == "ok" for t in travail["taches"])

    r = await client.get(f"/v1/projets/{pid}/services")
    assert r.status_code == 200 and len(r.json()) == 1

    sid = r.json()[0]["id"]
    r = await client.get(f"/v1/projets/{pid}/services/{sid}")
    # Sans `source`, il n'y a rien de réel à exécuter : le service reste `stopped`
    # plutôt que d'afficher un faux « running » (cf. `image_service`).
    assert r.status_code == 200 and r.json()["statut"] == "stopped"

    r = await client.patch(
        f"/v1/projets/{pid}/services/{sid}",
        json={
            "nom": "front",
            "type": "application",
            "environnement": "production",
            "ressources": {"cpu": 2, "ramMo": 2048, "diskGo": 20},
        },
    )
    assert r.status_code == 200 and r.json()["ressources"]["cpu"] == 2

    r = await client.post(f"/v1/projets/{pid}/services/{sid}/arret")
    assert r.status_code == 202
    r = await client.get(f"/v1/projets/{pid}/services/{sid}")
    assert r.json()["statut"] == "stopped"

    r = await client.post(f"/v1/projets/{pid}/services/{sid}/demarrage")
    assert r.status_code == 202

    r = await client.post(f"/v1/projets/{pid}/services/{sid}/redemarrage")
    assert r.status_code == 202

    r = await client.delete(f"/v1/projets/{pid}/services/{sid}", params={"confirmation": "front"})
    assert r.status_code == 202
    r = await client.get(f"/v1/projets/{pid}/services")
    assert len(r.json()) == 0


async def test_service_avec_image_reelle_tourne(client):
    """Une `source` de type `image` a quelque chose de réel à exécuter : `running`."""
    espace_id = await _espace(client)
    projet = await _projet(client, espace_id)
    pid = projet["id"]

    r = await client.post(
        f"/v1/projets/{pid}/services",
        json={
            "nom": "web",
            "type": "application",
            "environnement": "production",
            "ressources": {"cpu": 1, "ramMo": 512, "diskGo": 10},
            "source": {"type": "image", "ref": "nginx:alpine"},
        },
    )
    assert r.status_code == 202
    sid = (await client.get(f"/v1/projets/{pid}/services")).json()[0]["id"]
    r = await client.get(f"/v1/projets/{pid}/services/{sid}")
    assert r.json()["statut"] == "running"


async def test_service_base_moteur_tourne(client):
    """Un service `base` avec un `moteur` connu se traduit en image officielle : `running`."""
    espace_id = await _espace(client)
    projet = await _projet(client, espace_id)
    pid = projet["id"]

    r = await client.post(
        f"/v1/projets/{pid}/services",
        json={
            "nom": "pg",
            "type": "base",
            "environnement": "production",
            "ressources": {"cpu": 2, "ramMo": 4096, "diskGo": 100},
            "moteur": "postgresql",
            "version": "16.4",
        },
    )
    assert r.status_code == 202
    sid = (await client.get(f"/v1/projets/{pid}/services")).json()[0]["id"]
    r = await client.get(f"/v1/projets/{pid}/services/{sid}")
    corps = r.json()
    assert corps["statut"] == "running"
    assert corps["base"]["port"] == 5432
    # Bug réel vécu en direct (VM de projet) : un service `postgresql` nommé `pg` — le choix
    # le plus naturel — donne `pg_user`, refusé par `initdb` (préfixe `pg_` réservé aux rôles
    # système), conteneur en boucle d'échec. `utilisateur_base()` évite ce préfixe.
    assert not corps["base"]["utilisateur"].startswith("pg_")


async def test_execution_cron_et_refus(client):
    espace_id = await _espace(client)
    projet = await _projet(client, espace_id)
    pid = projet["id"]

    r = await client.post(
        f"/v1/projets/{pid}/services",
        json={
            "nom": "cron",
            "type": "cron",
            "environnement": "production",
            "cron": {"expression": "0 2 * * *", "commande": "backup"},
        },
    )
    assert r.status_code == 202
    services = await client.get(f"/v1/projets/{pid}/services")
    sid = services.json()[0]["id"]

    r = await client.post(f"/v1/projets/{pid}/services/{sid}/execution")
    assert r.status_code == 202

    r = await _service(client, pid, "app", "application")
    sid2 = (await client.get(f"/v1/projets/{pid}/services")).json()[0]["id"]
    r2 = await client.post(f"/v1/projets/{pid}/services/{sid2}/execution")
    assert r2.status_code == 409


async def test_identifiants_journaux_metriques(client):
    espace_id = await _espace(client)
    projet = await _projet(client, espace_id)
    pid = projet["id"]
    r = await _service(client, pid, "api", "application")
    assert r.status_code == 202
    sid = (await client.get(f"/v1/projets/{pid}/services")).json()[0]["id"]

    r = await client.get(f"/v1/projets/{pid}/services/{sid}/identifiants")
    assert r.status_code == 200
    corps = r.json()
    assert corps["hoteInterne"].endswith(".svc.cluster.local")
    assert corps["port"]

    r = await client.get(f"/v1/projets/{pid}/services/{sid}/journaux")
    assert r.status_code == 200 and r.json() == {"lignes": [], "tronque": False}

    r = await client.get(f"/v1/projets/{pid}/services/{sid}/metriques")
    assert r.status_code == 200 and r.json()["series"] == []


async def test_variables_projet(client):
    espace_id = await _espace(client)
    projet = await _projet(client, espace_id)
    pid = projet["id"]

    r = await client.put(
        f"/v1/projets/{pid}/variables",
        json={
            "variables": [
                {
                    "cle": "DB_URL",
                    "valeur": "postgres://x",
                    "secret": True,
                    "portee": "runtime",
                    "environnements": ["production"],
                }
            ]
        },
    )
    assert r.status_code == 200 and r.json()["appliquees"] == 1

    r = await client.get(f"/v1/projets/{pid}/variables")
    assert r.status_code == 200
    v = r.json()[0]
    assert v["cle"] == "DB_URL" and v["secret"] is True and v["valeur"] is None


async def test_domaines_applicatifs(client):
    espace_id = await _espace(client)
    projet = await _projet(client, espace_id)
    pid = projet["id"]
    r = await _service(client, pid, "web", "application")
    assert r.status_code == 202
    sid = (await client.get(f"/v1/projets/{pid}/services")).json()[0]["id"]

    r = await client.post(
        "/v1/domaines-applicatifs", json={"hote": "foo.apps.synelia.cloud", "serviceId": sid}
    )
    assert r.status_code == 201, r.text
    did = r.json()["id"]
    assert r.json()["verification"]["etat"] == "attente"

    r = await client.get(f"/v1/domaines-applicatifs/{did}")
    assert r.status_code == 200

    r = await client.post(f"/v1/domaines-applicatifs/{did}/certificat")
    assert r.status_code == 409

    r = await client.post(f"/v1/domaines-applicatifs/{did}/verification")
    assert r.status_code == 200 and r.json()["verification"]["etat"] == "ok"

    r = await client.post(f"/v1/domaines-applicatifs/{did}/certificat")
    assert r.status_code == 202

    r = await client.patch(
        f"/v1/domaines-applicatifs/{did}",
        json={"hote": "foo.apps.synelia.cloud", "serviceId": sid, "https": True},
    )
    assert r.status_code == 200 and r.json()["https"] is True

    r = await client.get("/v1/domaines-applicatifs")
    assert r.status_code == 200 and len(r.json()["donnees"]) == 1

    r = await client.delete(
        f"/v1/domaines-applicatifs/{did}", params={"confirmation": "foo.apps.synelia.cloud"}
    )
    assert r.status_code == 204


async def test_zone_applicative(client):
    r = await client.get("/v1/zone-applicative")
    assert r.status_code == 200
    corps = r.json()
    assert corps["zone"] == "apps.synelia.cloud" and corps["wildcard"] == "*.apps.synelia.cloud"
    assert corps["ingress"] and corps["certificat"]["emetteur"]
    assert "total" in corps["quotaDomaines"]


async def _projet_vm(client, espace_id, nom="Site VM"):
    r = await client.post(
        "/v1/projets",
        json={"nom": nom, "description": "Projet cible VM", "espaceId": espace_id, "cible": "vm"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["cible"] == "vm"
    return r.json()


async def test_projet_cible_vm_par_defaut_k8s(client):
    """Sans `cible`, un projet reste en `k8s` : comportement inchangé pour tout projet déjà
    existant ou créé sans y penser."""
    espace_id = await _espace(client)
    projet = await _projet(client, espace_id, "Defaut")
    assert projet["cible"] == "k8s"


async def test_cycle_service_vm(client):
    """Une cible `vm` : deux services du même projet deviennent deux conteneurs Docker sur
    UNE SEULE VM Nova partagée (pas un namespace K8s) — provisionnée paresseusement, au
    premier service ayant réellement une image à exécuter. Reste instantané/sans réseau en
    mode simulé, comme tout le reste de la plateforme."""
    espace_id = await _espace(client)
    projet = await _projet_vm(client, espace_id)
    pid = projet["id"]

    r = await client.post(
        f"/v1/projets/{pid}/services",
        json={
            "nom": "web",
            "type": "application",
            "environnement": "production",
            "ressources": {"cpu": 1, "ramMo": 512, "diskGo": 10},
            "source": {"type": "image", "ref": "nginx:alpine"},
        },
    )
    assert r.status_code == 202, r.text
    assert r.json()["statut"] == "done", r.json()
    web_id = (await client.get(f"/v1/projets/{pid}/services")).json()[0]["id"]
    r = await client.get(f"/v1/projets/{pid}/services/{web_id}")
    assert r.json()["statut"] == "running"

    r = await client.post(
        f"/v1/projets/{pid}/services",
        json={
            "nom": "pg",
            "type": "base",
            "environnement": "production",
            "ressources": {"cpu": 1, "ramMo": 1024, "diskGo": 20},
            "moteur": "postgresql",
            "version": "16.4",
        },
    )
    assert r.status_code == 202, r.text
    assert r.json()["statut"] == "done", r.json()
    services = (await client.get(f"/v1/projets/{pid}/services")).json()
    pg = next(s for s in services if s["nom"] == "pg")
    r = await client.get(f"/v1/projets/{pid}/services/{pg['id']}")
    assert r.json()["statut"] == "running"

    # Un service `base` en cible `vm` reste interne (comme un `ClusterIP` en cible k8s) :
    # jamais le domaine `*.cloud.dev01.ovh.smile.ci` d'un service HTTP.
    r = await client.get(f"/v1/projets/{pid}/services/{pg['id']}/identifiants")
    assert r.status_code == 200
    assert not r.json()["hoteInterne"].endswith(".svc.cluster.local")
    assert not r.json()["hoteInterne"].endswith(".cloud.dev01.ovh.smile.ci")

    r = await client.post(f"/v1/projets/{pid}/services/{web_id}/arret")
    assert r.status_code == 202 and r.json()["statut"] == "done"
    r = await client.get(f"/v1/projets/{pid}/services/{web_id}")
    assert r.json()["statut"] == "stopped"

    r = await client.delete(
        f"/v1/projets/{pid}/services/{web_id}", params={"confirmation": "web"}
    )
    assert r.status_code == 202 and r.json()["statut"] == "done"
    r = await client.delete(
        f"/v1/projets/{pid}/services/{pg['id']}", params={"confirmation": "pg"}
    )
    assert r.status_code == 202 and r.json()["statut"] == "done"
    r = await client.get(f"/v1/projets/{pid}/services")
    assert len(r.json()) == 0

    # Le projet (donc sa VM dédiée) ne se supprime qu'une fois vide, exactement comme en
    # cible k8s.
    r = await client.delete(f"/v1/projets/{pid}", params={"confirmation": "Site VM"})
    assert r.status_code == 202 and r.json()["statut"] == "done"


async def test_routage(client):
    espace_id = await _espace(client)
    projet = await _projet(client, espace_id)
    pid = projet["id"]
    r = await _service(client, pid, "web", "application")
    assert r.status_code == 202
    sid = (await client.get(f"/v1/projets/{pid}/services")).json()[0]["id"]
    await client.post(
        "/v1/domaines-applicatifs", json={"hote": "foo.apps.synelia.cloud", "serviceId": sid}
    )

    r = await client.get("/v1/routage")
    assert r.status_code == 200
    corps = r.json()
    assert len(corps["donnees"]) == 1
    assert corps["donnees"][0]["serviceNom"] == "web"
    assert corps["donnees"][0]["actif"] is True
