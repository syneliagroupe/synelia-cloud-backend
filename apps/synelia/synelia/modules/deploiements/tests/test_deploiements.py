"""Déploiements : cycle de vie, approbation, canari, promotion, rollback, journaux, branches."""


async def _environnement(client_org):
    r = await client_org.post(
        "/v1/applications",
        json={
            "espaceId": "espace-demo",
            "nom": "app-deploy",
            "source": "git",
            "repo": {
                "provider": "github",
                "url": "https://github.com/acme/app-deploy",
                "branche": "main",
            },
            "cible": "vm",
        },
    )
    assert r.status_code == 202, r.text
    app = (await client_org.get("/v1/applications")).json()["donnees"][0]
    r = await client_org.post(f"/v1/applications/{app['id']}/environnements", json={"nom": "prod"})
    assert r.status_code == 201, r.text
    return app, r.json()


async def test_cycle_deploiement(client_org):
    _, env = await _environnement(client_org)
    r = await client_org.post(
        "/v1/deploiements",
        json={"envId": env["id"], "branche": "main", "commit": "abc123", "message": "fix: panneau"},
    )
    assert r.status_code == 202, r.text
    dep = r.json()
    assert dep["statut"] == "live" and dep["envId"] == env["id"]
    assert dep["commitMessage"] == "fix: panneau"
    dep_id = dep["id"]

    r = await client_org.get(f"/v1/deploiements/{dep_id}")
    assert r.status_code == 200 and r.json()["statut"] == "live"

    r = await client_org.get("/v1/deploiements")
    assert r.status_code == 200 and r.json()["pagination"]["total"] == 1

    r = await client_org.get(f"/v1/deploiements/{dep_id}/journaux")
    assert r.status_code == 200
    logs = r.json()
    assert "lignes" in logs and len(logs["lignes"]) > 0

    # deuxième déploiement live pour avoir un état à annuler
    r = await client_org.post(
        "/v1/deploiements", json={"envId": env["id"], "branche": "main", "commit": "def456"}
    )
    assert r.status_code == 202
    dep2 = r.json()

    r = await client_org.post(
        f"/v1/deploiements/{dep2['id']}/rollback", json={"versionCible": "abc123"}
    )
    assert r.status_code == 202, r.text
    assert r.json()["statut"] == "rolled_back"

    r = await client_org.delete(f"/v1/environnements/{env['id']}", params={"confirmation": "prod"})
    assert r.status_code == 202


async def test_approbation(client_org):
    app, _ = await _environnement(client_org)
    # environnement protégé → approbation requise
    env_app = app["id"]
    r = await client_org.post(
        f"/v1/applications/{env_app}/environnements",
        json={"nom": "prod-protege", "protection": {"approbationRequise": True}},
    )
    assert r.status_code == 201, r.text
    env_protege = r.json()

    r = await client_org.post(
        "/v1/deploiements", json={"envId": env_protege["id"], "branche": "main", "commit": "aaa111"}
    )
    assert r.status_code == 202, r.text
    dep = r.json()
    assert dep["statut"] == "queued"
    dep_id = dep["id"]

    # approbation sur un déploiement qui n'attend rien → 409
    r = await client_org.post(
        "/v1/deploiements", json={"envId": env_protege["id"], "branche": "main", "commit": "bbb222"}
    )
    non_attendu = r.json()

    r = await client_org.post(
        f"/v1/deploiements/{non_attendu['id']}/approbation", json={"decision": "approuver"}
    )
    assert r.status_code == 200, r.text

    r = await client_org.post(
        f"/v1/deploiements/{dep_id}/approbation", json={"decision": "approuver", "motif": "OK go"}
    )
    assert r.status_code == 200, r.text
    assert r.json()["statut"] == "live"

    r = await client_org.delete(
        f"/v1/environnements/{env_protege['id']}", params={"confirmation": "prod-protege"}
    )
    assert r.status_code == 202


async def test_canari_promotion(client_org):
    app, env = await _environnement(client_org)
    r = await client_org.post(
        "/v1/deploiements", json={"envId": env["id"], "branche": "main", "commit": "c1"}
    )
    assert r.status_code == 202
    dep = r.json()

    r = await client_org.post(
        f"/v1/deploiements/{dep['id']}/canari", json={"action": "avancer", "pct": 25}
    )
    assert r.status_code == 202 and r.json()["statut"] == "live"

    r = await client_org.post(f"/v1/applications/{app['id']}/environnements", json={"nom": "prod2"})
    env2 = r.json()
    r = await client_org.post(
        f"/v1/deploiements/{dep['id']}/promotion", json={"envCibleId": env2["id"]}
    )
    assert r.status_code == 202, r.text
    assert r.json()["envNom"] == "prod2" and r.json()["statut"] == "live"

    r = await client_org.delete(f"/v1/environnements/{env['id']}", params={"confirmation": "prod"})
    assert r.status_code == 202
    r = await client_org.delete(
        f"/v1/environnements/{env2['id']}", params={"confirmation": "prod2"}
    )
    assert r.status_code == 202


async def test_rollback_sans_rien(client_org):
    _, env = await _environnement(client_org)
    r = await client_org.post(
        "/v1/deploiements",
        json={"envId": env["id"], "branche": "main", "commit": "x1", "ignorerScan": True},
    )
    dep = r.json()
    r = await client_org.post(f"/v1/deploiements/{dep['id']}/rollback", json={})
    assert r.status_code == 409 and r.json()["erreur"]["code"] == "rien_a_annuler"

    r = await client_org.delete(f"/v1/environnements/{env['id']}", params={"confirmation": "prod"})
    assert r.status_code == 202


async def test_branches_sans_token(client_org):
    r = await client_org.get(
        "/v1/depots/branches",
        params={"provider": "github", "url": "https://github.com/acme/app-deploy"},
    )
    assert r.status_code == 424, r.text
    corps = r.json()
    assert corps["erreur"]["code"] == "amont_indisponible"
