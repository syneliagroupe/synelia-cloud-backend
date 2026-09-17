"""Admin catalogue : offres, familles, fiches de service, modèles, cycle de facturation plateforme."""

from synelia.modules.admin_catalogue.service import CATALOGUE_REEL


async def test_catalogue_reel_semé(client):
    """Le catalogue de démarrage (`semer_catalogue_reel`) est présent dès le premier
    démarrage, indépendamment du jeu de démo — chaque offre correspond à une ressource
    réellement provisionnable (Espace Cloud, hébergement Web Cloud, base managée)."""
    r = await client.get("/v1/admin/catalogue/offres", params={"parPage": 100})
    assert r.status_code == 200, r.text
    offres = {o["code"]: o for o in r.json()["donnees"]}
    for attendu in CATALOGUE_REEL:
        assert attendu["code"] in offres, f"offre {attendu['code']} absente du catalogue"
        offre = offres[attendu["code"]]
        assert offre["categorie"] == attendu["categorie"]
        assert offre["prix"] == attendu["prix"]
        assert offre["statut"] == "publiee"


async def test_offres_crud(client):
    r = await client.get("/v1/admin/catalogue/offres")
    assert r.status_code == 200 and len(r.json()["donnees"]) >= 1

    r = await client.post(
        "/v1/admin/catalogue/offres",
        json={
            "code": "premium",
            "nom": "Espace Premium",
            "categorie": "espace_cloud",
            "specs": "16 vCPU · 64 Go",
            "caracteristiques": ["IPv4"],
            "prix": 180000,
        },
    )
    assert r.status_code == 201, r.text
    offre = r.json()
    oid = offre["id"]
    assert offre["souscriptionsActives"] == 0

    r = await client.get(f"/v1/admin/catalogue/offres/{oid}")
    assert r.status_code == 200

    r = await client.patch(
        f"/v1/admin/catalogue/offres/{oid}",
        json={
            "code": "premium",
            "nom": "Espace Premium Plus",
            "categorie": "espace_cloud",
            "specs": "16 vCPU · 64 Go",
            "prix": 180000,
        },
    )
    assert r.status_code == 200 and r.json()["nom"] == "Espace Premium Plus"

    # Un brouillon jamais souscrit se supprime.
    r = await client.delete(f"/v1/admin/catalogue/offres/{oid}")
    assert r.status_code == 204

    r = await client.get(f"/v1/admin/catalogue/offres/{oid}")
    assert r.status_code == 404


async def test_offre_publiee_ne_se_supprime_pas(client):
    """Une offre publiée a été vendue : elle se déprécie, elle ne se supprime jamais."""
    r = await client.post(
        "/v1/admin/catalogue/offres",
        json={
            "code": "essentiel",
            "nom": "Espace Essentiel",
            "categorie": "espace_cloud",
            "specs": "2 vCPU · 8 Go",
            "prix": 20000,
        },
    )
    assert r.status_code == 201, r.text
    oid = r.json()["id"]

    r = await client.post(
        f"/v1/admin/catalogue/offres/{oid}/publication", json={"statut": "publiee"}
    )
    assert r.status_code == 200 and r.json()["statut"] == "publiee"

    r = await client.delete(f"/v1/admin/catalogue/offres/{oid}")
    assert r.status_code == 409 and r.json()["erreur"]["code"] == "offre_publiee"

    r = await client.post(
        f"/v1/admin/catalogue/offres/{oid}/publication", json={"statut": "depreciee"}
    )
    assert r.status_code == 200 and r.json()["statut"] == "depreciee"

    r = await client.delete(f"/v1/admin/catalogue/offres/{oid}")
    assert r.status_code == 409 and r.json()["erreur"]["code"] == "offre_publiee"


async def test_familles(client):
    r = await client.get("/v1/admin/catalogue/familles")
    assert r.status_code == 200, r.text
    assert len(r.json()) >= 1


async def test_fiche_service(client):
    fiche = {
        "slug": "nextcloud",
        "nom": "Nextcloud",
        "solutionOSS": "Nextcloud",
        "categorie": "collaboration",
        "description": "Cloud privé de fichiers",
        "pitch": "Partagez et collaborez",
        "modes": ["dedie", "mutualise"],
        "paliers": [
            {
                "code": "s",
                "nom": "Small",
                "specs": "2 vCPU",
                "prixMois": 5000,
                "limites": ["50 users"],
            }
        ],
        "sla": "99.9",
        "backupPolicyDefault": "quotidienne",
        "reversibilite": {"formats": ["tar.gz", "zip"], "delaiJours": 7, "docUrl": "https://docs"},
        "versionsSupportees": ["28", "29"],
        "certifie": True,
    }
    r = await client.put("/v1/admin/catalogue/services/nextcloud", json=fiche)
    assert r.status_code == 200, r.text
    assert r.json()["slug"] == "nextcloud"

    r = await client.post(
        "/v1/admin/catalogue/services/nextcloud/versions",
        json={"version": "29.0.1", "rupture": False, "statut": "disponible"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["version"] == "29.0.1"


async def test_modele_applicatif(client):
    modele = {
        "slug": "wordpress",
        "nom": "WordPress",
        "solution": "WordPress",
        "categorie": "web",
        "phrase": "Blog et site vitrine",
        "version": "6.5",
        "ressources": {"cpu": 1.0, "ramMo": 1024, "diskGo": 10},
        "dependances": [{"nom": "MySQL", "type": "base", "detail": "base de données"}],
        "variables": [{"cle": "WP_TITLE", "secret": False, "obligatoire": True}],
        "volumes": [{"chemin": "/var/www", "tailleGo": 10, "role": "data"}],
        "ports": [{"conteneur": 80, "protocole": "http", "role": "web"}],
        "sousDomaine": "wordpress",
        "sauvegardeParDefaut": {
            "frequence": "quotidienne",
            "retentionJours": 30,
            "inclut": ["bdd", "fichiers"],
        },
        "prixIndicatif": 0,
        "certifie": False,
        "horsPerimetre": "Les mises à jour du thème restent manuelles.",
    }
    r = await client.put("/v1/admin/modeles/wordpress", json=modele)
    assert r.status_code == 200, r.text
    assert r.json()["slug"] == "wordpress"


async def test_cycle_facturation(client):
    r = await client.post("/v1/admin/facturation/cycle", json={"periode": "2026-09"})
    assert r.status_code == 202, r.text

    r = await client.post("/v1/admin/facturation/cycle", json={"periode": "2026-09"})
    assert r.status_code == 409 and r.json()["erreur"]["code"] == "cycle_deja_lance"

    r = await client.get("/v1/admin/facturation/cycles")
    assert r.status_code == 200 and len(r.json()["donnees"]) == 1


async def test_factures_plateforme(client):
    r = await client.get("/v1/admin/facturation/factures")
    assert r.status_code == 200 and len(r.json()["donnees"]) >= 1


async def test_impayes(client):
    r = await client.get("/v1/admin/facturation/impayes")
    assert r.status_code == 200

    # Test relances with non-existent facture: should return echecs=1, envoyees=0
    # (the new behavior after commit 499f5c0 only counts factures that actually exist)
    r = await client.post(
        "/v1/admin/facturation/impayes/relances", json={"factures": ["x"], "niveau": "rappel"}
    )
    assert r.status_code == 200
    assert r.json()["envoyees"] == 0
    assert r.json()["echecs"] == 1


async def test_marges(client):
    r = await client.get("/v1/admin/facturation/marges")
    assert r.status_code == 200
    assert all(x["marge"] == 0.0 for x in r.json())
