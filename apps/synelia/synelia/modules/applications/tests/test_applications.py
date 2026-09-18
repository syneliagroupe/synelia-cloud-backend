"""Applications PaaS : cycle application → environnement → composant, variables, analyse, canvas."""


async def _creer_application(client_org):
    r = await client_org.post(
        "/v1/applications",
        json={
            "espaceId": "espace-demo",
            "nom": "mon-app",
            "source": "git",
            "repo": {
                "provider": "github",
                "url": "https://github.com/acme/mon-app",
                "branche": "main",
            },
            "cible": "vm",
            "domainePrincipal": "mon-app.synelia.app",
        },
    )
    assert r.status_code == 202, r.text
    travail = r.json()
    assert travail["type"] == "application.create" and travail["statut"] == "done"
    assert all(t["statut"] == "ok" for t in travail["taches"])
    r = await client_org.get("/v1/applications")
    assert r.status_code == 200
    apps = [a for a in r.json()["donnees"] if a["nom"] == "mon-app"]
    assert len(apps) == 1 and apps[0]["sante"] == "sain"
    return apps[0]


async def test_cycle_application(client_org):
    app = await _creer_application(client_org)
    assert app["nom"] == "mon-app" and app["domainePrincipal"] == "mon-app.synelia.app"

    r = await client_org.get(f"/v1/applications/{app['id']}")
    assert r.status_code == 200 and r.json()["id"] == app["id"]

    r = await client_org.patch(
        f"/v1/applications/{app['id']}",
        json={
            "espaceId": app["espaceId"],
            "nom": "mon-app",
            "source": "git",
            "cible": "vm",
            "description": "nouvelle description",
        },
    )
    assert r.status_code == 200 and r.json()["description"] == "nouvelle description"

    r = await client_org.delete(f"/v1/applications/{app['id']}", params={"confirmation": "mauvais"})
    assert r.status_code == 422

    r = await client_org.delete(f"/v1/applications/{app['id']}", params={"confirmation": "mon-app"})
    assert r.status_code == 202 and r.json()["statut"] == "done"

    r = await client_org.get("/v1/applications")
    assert not any(a["id"] == app["id"] for a in r.json()["donnees"])


async def test_analyse_depot(client_org):
    r = await client_org.post(
        "/v1/applications/analyse-depot",
        json={"provider": "github", "url": "https://github.com/acme/next-app", "branche": "dev"},
    )
    assert r.status_code == 200, r.text
    a = r.json()
    assert a["builderPropose"] == "nixpacks" and a["branche"] == "dev"
    assert any("Next.js" in c["constat"] for c in a["constats"])


async def test_environnements_et_composants(client_org):
    app = await _creer_application(client_org)
    r = await client_org.post(
        f"/v1/applications/{app['id']}/environnements",
        json={
            "nom": "prod",
            "couleur": "#111827",
            "domaines": ["prod.synelia.app"],
            "autoDeploy": {"branche": "main", "previewParPR": False},
        },
    )
    assert r.status_code == 201, r.text
    env = r.json()
    assert env["nom"] == "prod" and env["statut"] == "building"
    env_id = env["id"]

    r = await client_org.get(f"/v1/applications/{app['id']}/environnements")
    assert r.status_code == 200 and len(r.json()) == 1

    r = await client_org.get(f"/v1/environnements/{env_id}")
    assert r.status_code == 200 and r.json()["nom"] == "prod"

    r = await client_org.patch(
        f"/v1/environnements/{env_id}", json={"nom": "prod", "couleur": "#0ea5e9"}
    )
    assert r.status_code == 200 and r.json()["couleur"] == "#0ea5e9"

    r = await client_org.post(
        f"/v1/environnements/{env_id}/composants",
        json={
            "nom": "web",
            "kind": "vm",
            "role": "web",
            "image": "nginx:alpine",
            "version": "1.27",
            "ressources": {"cpu": 2, "ramMo": 2048, "diskGo": 20},
            "ports": [{"interne": 80, "type": "ClusterIP"}],
            "envVars": [{"cle": "PORT", "valeur": "8080", "secret": False, "scope": "runtime"}],
        },
    )
    assert r.status_code == 202, r.text
    assert r.json()["type"] == "composant.creer" and r.json()["statut"] == "done"
    comp_id = None
    r = await client_org.get(f"/v1/environnements/{env_id}/composants")
    composants = r.json()
    assert len(composants) == 1 and composants[0]["statut"] == "deployed"
    comp_id = composants[0]["id"]
    # Régression : `envVars` fourni à la création ne doit pas être jeté au sol
    # (creer_composant forçait `envVars=[]` sans lire `corps.envVars`).
    assert composants[0]["envVars"] == [
        {"cle": "PORT", "valeur": "8080", "secret": False, "scope": "runtime"}
    ]

    r = await client_org.get(f"/v1/composants/{comp_id}")
    assert r.status_code == 200 and r.json()["image"] == "nginx:alpine"

    r = await client_org.patch(
        f"/v1/composants/{comp_id}",
        json={
            "nom": "web",
            "kind": "vm",
            "role": "web",
            "image": "nginx:alpine",
            "version": "1.28",
        },
    )
    assert r.status_code == 202 and r.json()["statut"] == "done"

    r = await client_org.post(f"/v1/composants/{comp_id}/arret")
    assert r.status_code == 202 and r.json()["type"] == "composant.arret"
    r = await client_org.get(f"/v1/composants/{comp_id}")
    assert r.json()["statut"] == "stopped"

    r = await client_org.post(f"/v1/composants/{comp_id}/redemarrage")
    assert r.status_code == 202 and r.json()["type"] == "composant.redemarrage"
    r = await client_org.get(f"/v1/composants/{comp_id}")
    assert r.json()["statut"] == "deployed"

    r = await client_org.post(
        f"/v1/composants/{comp_id}/dimensionnement", json={"cpu": 4, "ramMo": 4096, "replicas": 3}
    )
    assert (
        r.status_code == 202
        and r.json()["type"] == "composant.dimensionnement"
        and r.json()["statut"] == "done"
    )
    r = await client_org.get(f"/v1/composants/{comp_id}")
    assert r.json()["ressources"]["cpu"] == 4 and r.json()["ressources"]["ramMo"] == 4096

    r = await client_org.delete(f"/v1/composants/{comp_id}", params={"confirmation": "web"})
    assert r.status_code == 202 and r.json()["statut"] == "done"
    r = await client_org.get(f"/v1/environnements/{env_id}/composants")
    assert r.json() == []

    r = await client_org.delete(f"/v1/environnements/{env_id}", params={"confirmation": "prod"})
    assert r.status_code == 202 and r.json()["statut"] == "done"


async def test_variables_environnement(client_org):
    app = await _creer_application(client_org)
    r = await client_org.post(
        f"/v1/applications/{app['id']}/environnements", json={"nom": "staging"}
    )
    env_id = r.json()["id"]

    r = await client_org.put(
        f"/v1/environnements/{env_id}/variables",
        json={
            "variables": [
                {"cle": "API_KEY", "valeur": "secret-123", "secret": True, "scope": "runtime"},
                {
                    "cle": "DATABASE_URL",
                    "valeur": "postgres://db",
                    "secret": False,
                    "scope": "runtime",
                },
            ]
        },
    )
    assert r.status_code == 200, r.text
    vars_ = r.json()
    by_cle = {v["cle"]: v for v in vars_}
    assert by_cle["API_KEY"]["valeur"] == "•••••" and by_cle["API_KEY"]["secret"] is True
    assert by_cle["DATABASE_URL"]["valeur"] == "postgres://db"

    r = await client_org.get(f"/v1/environnements/{env_id}/variables")
    assert r.status_code == 200
    by_cle = {v["cle"]: v for v in r.json()}
    assert by_cle["API_KEY"]["valeur"] == "•••••"

    r = await client_org.delete(f"/v1/environnements/{env_id}", params={"confirmation": "staging"})
    assert r.status_code == 202


async def test_canvas_briques(client_org):
    r = await client_org.get("/v1/canvas/briques")
    assert r.status_code == 200
    briques = r.json()
    assert len(briques) > 0 and all(
        {"id", "nom", "categorie", "image"} <= set(b.keys()) for b in briques
    )
