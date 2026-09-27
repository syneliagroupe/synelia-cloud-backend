"""Déploiements : cycle de vie, approbation, canari, promotion, rollback, journaux, branches."""

from synelia_testing import inscrire_et_verifier


async def _environnement(client_org, *, nom: str = "prod", protection: dict | None = None):
    corps: dict = {"nom": nom}
    if protection is not None:
        corps["protection"] = protection
    r = await client_org.post("/v1/environnements", json=corps)
    assert r.status_code == 201, r.text
    return r.json()


async def _autre_approbateur(client_org, *, email: str = "co-approbateur@test-client.ci") -> str:
    """Inscrit un second `org_admin` dans l'organisation de `client_org` et renvoie son jeton
    d'accès — un véritable second principal, distinct de l'auteur des déploiements créés par
    `client_org`, pour les scénarios d'approbation à quatre yeux."""
    session = await inscrire_et_verifier(client_org, email, "Co Approbateur", "CoApprob!2026")
    r = await client_org.post(
        "/v1/membres",
        json={"userId": session["utilisateur"]["id"], "role": "org_admin", "scopeType": "org"},
        headers={"X-Organisation-Id": client_org.org_id},
    )
    assert r.status_code == 201, r.text
    return session["accessToken"]


async def test_cycle_deploiement(client_org):
    env = await _environnement(client_org)
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
    """L'approbation par un principal DIFFÉRENT de l'auteur du déploiement doit réussir —
    voir `test_approbation_auto_approbation_refusee` pour le refus quand c'est le même."""
    env_protege = await _environnement(
        client_org, nom="prod-protege", protection={"approbationRequise": True}
    )
    jeton_approbateur = await _autre_approbateur(client_org)
    entete_approbateur = {"Authorization": f"Bearer {jeton_approbateur}"}

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
        f"/v1/deploiements/{non_attendu['id']}/approbation",
        json={"decision": "approuver"},
        headers=entete_approbateur,
    )
    assert r.status_code == 200, r.text

    r = await client_org.post(
        f"/v1/deploiements/{dep_id}/approbation",
        json={"decision": "approuver", "motif": "OK go"},
        headers=entete_approbateur,
    )
    assert r.status_code == 200, r.text
    assert r.json()["statut"] == "live"

    r = await client_org.delete(
        f"/v1/environnements/{env_protege['id']}", params={"confirmation": "prod-protege"}
    )
    assert r.status_code == 202


async def test_approbation_auto_approbation_refusee(client_org):
    """Segrégation des tâches (contrat : « L'approbateur ne peut pas être l'auteur du
    déploiement ») : l'auteur d'un déploiement ne peut pas l'approuver lui-même, même s'il a
    les droits `app.deploy` — un second principal reste requis."""
    env_protege = await _environnement(
        client_org, nom="prod-protege-auto", protection={"approbationRequise": True}
    )

    r = await client_org.post(
        "/v1/deploiements", json={"envId": env_protege["id"], "branche": "main", "commit": "ccc333"}
    )
    assert r.status_code == 202, r.text
    dep_id = r.json()["id"]

    # L'auteur (client_org) tente d'approuver son propre déploiement → refusé.
    r = await client_org.post(
        f"/v1/deploiements/{dep_id}/approbation", json={"decision": "approuver"}
    )
    assert r.status_code == 403, r.text
    assert r.json()["erreur"]["code"] == "auto_approbation_interdite"

    # Toujours en attente d'approbation : un second principal peut encore la traiter.
    r = await client_org.get(f"/v1/deploiements/{dep_id}")
    assert r.status_code == 200 and r.json()["statut"] == "queued"

    jeton_approbateur = await _autre_approbateur(
        client_org, email="co-approbateur-auto@test-client.ci"
    )
    r = await client_org.post(
        f"/v1/deploiements/{dep_id}/approbation",
        json={"decision": "approuver"},
        headers={"Authorization": f"Bearer {jeton_approbateur}"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["statut"] == "live"

    r = await client_org.delete(
        f"/v1/environnements/{env_protege['id']}", params={"confirmation": "prod-protege-auto"}
    )
    assert r.status_code == 202


async def test_canari_promotion(client_org):
    env = await _environnement(client_org)
    r = await client_org.post(
        "/v1/deploiements", json={"envId": env["id"], "branche": "main", "commit": "c1"}
    )
    assert r.status_code == 202
    dep = r.json()

    r = await client_org.post(
        f"/v1/deploiements/{dep['id']}/canari", json={"action": "avancer", "pct": 25}
    )
    assert r.status_code == 202 and r.json()["statut"] == "live"

    env2 = await _environnement(client_org, nom="prod2")
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


async def test_rollback_passe_par_le_travail_app_rollback(client_org):
    env = await _environnement(client_org, nom="prod-travail")
    r = await client_org.post(
        "/v1/deploiements", json={"envId": env["id"], "branche": "main", "commit": "aaa000"}
    )
    assert r.status_code == 202
    r = await client_org.post(
        "/v1/deploiements", json={"envId": env["id"], "branche": "main", "commit": "bbb111"}
    )
    assert r.status_code == 202
    dep2 = r.json()

    r = await client_org.post(
        f"/v1/deploiements/{dep2['id']}/rollback", json={"versionCible": "aaa000"}
    )
    assert r.status_code == 202, r.text
    assert r.json()["statut"] == "rolled_back"

    # le rollback doit passer par le moteur de travaux (app.rollback), pas par un patch direct
    r = await client_org.get("/v1/travaux", params={"type": "app.rollback"})
    assert r.status_code == 200, r.text
    travaux = r.json()["donnees"]
    assert len(travaux) == 1
    assert travaux[0]["statut"] == "done"

    r = await client_org.delete(
        f"/v1/environnements/{env['id']}", params={"confirmation": "prod-travail"}
    )
    assert r.status_code == 202


async def test_rollback_sans_rien(client_org):
    env = await _environnement(client_org)
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
