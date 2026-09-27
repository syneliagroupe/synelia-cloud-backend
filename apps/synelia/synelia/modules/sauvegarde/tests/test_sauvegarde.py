"""Tests du module Sauvegarde : plans, points, restaurations, conformité 3-2-1."""


def _plan(valeur: str = "vm-app-01") -> dict:
    return {
        "nom": "sauvegarde-prod",
        "scope": {"type": "ressource", "valeur": valeur},
        "frequence": "quotidien",
        "mode": "complete",
        "retentionJours": 30,
        "immutable": True,
        "destinations": [{"type": "local"}, {"type": "autre_site"}],
        "chiffrement": {"mode": "synelia"},
    }


async def _espace(client_org) -> str:
    existants = (await client_org.get("/v1/espaces")).json()["donnees"]
    for e in existants:
        if e["code"] == "demo-abj":
            return e["id"]
    r = await client_org.post(
        "/v1/espaces",
        json={
            "code": "demo-abj",
            "offerId": "offre-standard",
            "site": "ABJ",
            "cidr": "10.10.0.0/16",
            "quota": {"vcpu": 16, "ramGo": 64, "stockageTo": 2},
        },
    )
    assert r.status_code == 202, r.text
    return (await client_org.get("/v1/espaces")).json()["donnees"][0]["id"]


async def _creer_vm(client_org, espace_id: str, nom: str) -> str:
    gabarits = (await client_org.get("/v1/catalogue/gabarits")).json()
    images = (await client_org.get("/v1/catalogue/images")).json()
    gabarit = next(
        (g["id"] for g in gabarits if g["id"].lower() in {"small", "g1.small", "s1.small"}),
        gabarits[0]["id"],
    )
    r = await client_org.post(
        "/v1/vms",
        json={"espaceId": espace_id, "nom": nom, "imageId": images[0]["id"], "gabarit": gabarit},
    )
    assert r.status_code == 202, r.text
    vms = (await client_org.get("/v1/vms")).json()["donnees"]
    return next(v["id"] for v in vms if v["nom"] == nom)


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
    """`backup-service-hasattr-always-true` : une VM sans volume Cinder supplémentaire
    attaché (le cas de la quasi-totalité du parc) ne doit plus jamais tomber sur l'appel
    voué à l'échec du service OpenStack « backup » (absent du catalogue Keystone) — elle
    est protégée pour de vrai par un instantané Nova/Glance de la VM entière, comme le
    fait déjà `vm.snapshot`."""
    espace_id = await _espace(client_org)
    vm_id = await _creer_vm(client_org, espace_id, "vm-sans-volume")
    r = await client_org.post("/v1/sauvegarde/plans", json=_plan(valeur=vm_id))
    plan_id = r.json()["id"]

    r = await client_org.post(f"/v1/sauvegarde/plans/{plan_id}/execution")
    assert r.status_code == 202, r.text
    travail = r.json()
    assert travail["type"] == "backup.run" and travail["statut"] == "done", travail

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


async def test_execution_sans_ressource_reelle_echoue_honnetement(client_org):
    """Ni VM identifiable ni volume Cinder attaché (scope qui ne désigne rien de réel) :
    échec honnête et actionnable, plus jamais l'erreur de catalogue de service confuse
    provoquée par l'ancien `hasattr(c, "backup")`, structurellement toujours vrai."""
    r = await client_org.post("/v1/sauvegarde/plans", json=_plan(valeur="rien-de-connu"))
    plan_id = r.json()["id"]

    r = await client_org.post(f"/v1/sauvegarde/plans/{plan_id}/execution")
    assert r.status_code == 202, r.text
    travail = r.json()
    assert travail["statut"] == "failed", travail
    assert "aucune vm" in travail["erreur"]["message"].lower()


async def test_restauration(client_org):
    espace_id = await _espace(client_org)
    vm_id = await _creer_vm(client_org, espace_id, "vm-restauration")
    await client_org.post("/v1/sauvegarde/plans", json=_plan(valeur=vm_id))
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
