"""Tests du module Sauvegarde : plans, points, restaurations, conformité 3-2-1."""


def _plan() -> dict:
    return {
        "nom": "sauvegarde-prod",
        "scope": {"type": "ressource", "valeur": "vm-app-01"},
        "frequence": "quotidien",
        "mode": "complete",
        "retentionJours": 30,
        "immutable": True,
        "destinations": [{"type": "local"}, {"type": "autre_site"}],
        "chiffrement": {"mode": "synelia"},
    }


async def test_cycle_plan_sauvegarde(client_org):
    r = await client_org.post("/v1/sauvegarde/plans", json=_plan())
    assert r.status_code == 201, r.text
    plan = r.json()
    assert plan["nom"] == "sauvegarde-prod" and plan["ressourcesProtegees"] == 1

    dup = await client_org.post("/v1/sauvegarde/plans", json=_plan())
    assert dup.status_code == 409

    r = await client_org.get("/v1/sauvegarde/plans")
    assert r.status_code == 200 and r.json()["pagination"]["total"] == 1

    plan_id = plan["id"]

    r = await client_org.patch(
        f"/v1/sauvegarde/plans/{plan_id}", json={**_plan(), "nom": "sauvegarde-prod-v2"}
    )
    assert r.status_code == 200 and r.json()["nom"] == "sauvegarde-prod-v2"


async def test_execution_creer_point_et_verification(client_org):
    r = await client_org.post("/v1/sauvegarde/plans", json=_plan())
    plan_id = r.json()["id"]

    r = await client_org.post(f"/v1/sauvegarde/plans/{plan_id}/execution")
    assert r.status_code == 202, r.text
    travail = r.json()
    assert travail["type"] == "backup.run" and travail["statut"] == "done"

    r = await client_org.get("/v1/sauvegarde/points")
    assert r.status_code == 200
    points = r.json()["donnees"]
    assert len(points) == 1
    point = points[0]
    assert point["planId"] == plan_id and point["verifie"] is False

    r = await client_org.post(f"/v1/sauvegarde/points/{point['id']}/verification")
    assert r.status_code == 202, r.text
    assert r.json()["type"] == "backup.verify"

    r = await client_org.get("/v1/sauvegarde/points")
    assert all(p["verifie"] for p in r.json()["donnees"])


async def test_restauration(client_org):
    await client_org.post("/v1/sauvegarde/plans", json=_plan())
    plan_id = (await client_org.get("/v1/sauvegarde/plans")).json()["donnees"][0]["id"]
    await client_org.post(f"/v1/sauvegarde/plans/{plan_id}/execution")
    point = (await client_org.get("/v1/sauvegarde/points")).json()["donnees"][0]

    corps = {
        "pointId": point["id"],
        "cible": "nouvelle_ressource",
        "nomCible": "vm-recupere",
        "granularite": "fichiers",
    }
    r = await client_org.post("/v1/sauvegarde/restaurations", json=corps)
    assert r.status_code == 202, r.text
    assert r.json()["type"] == "backup.restore"

    r = await client_org.get("/v1/sauvegarde/restaurations")
    assert r.status_code == 200 and len(r.json()["donnees"]) == 1
    res_id = r.json()["donnees"][0]["id"]

    r = await client_org.get(f"/v1/sauvegarde/restaurations/{res_id}")
    assert r.status_code == 200 and r.json()["statut"] == "done"


async def test_conformite_calculee_depuis_plans_points(client_org):
    await client_org.post("/v1/sauvegarde/plans", json=_plan())
    # plan créé sans exécution → pas de point → non protégé (données réelles, aucune valeur inventée)
    r = await client_org.get("/v1/sauvegarde/conformite")
    assert r.status_code == 200, r.text
    lignes = r.json()["donnees"]
    assert len(lignes) == 1
    assert lignes[0]["protection"] in ("protegee", "non_protegee", "echec")
    assert "regle321" in lignes[0]


async def test_supprimer_plan_avec_confirmation(client_org):
    r = await client_org.post("/v1/sauvegarde/plans", json=_plan())
    plan_id = r.json()["id"]

    r = await client_org.delete(
        f"/v1/sauvegarde/plans/{plan_id}", params={"confirmation": "mauvais"}
    )
    assert r.status_code == 422

    r = await client_org.delete(
        f"/v1/sauvegarde/plans/{plan_id}", params={"confirmation": "sauvegarde-prod"}
    )
    assert r.status_code == 204

    r = await client_org.get("/v1/sauvegarde/plans")
    assert r.json()["pagination"]["total"] == 0
