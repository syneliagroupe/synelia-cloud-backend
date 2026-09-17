"""Stockage : volumes (bloc), buckets (objets), clés S3."""


async def _espace(client) -> str:
    existants = (await client.get("/v1/espaces")).json()["donnees"]
    for e in existants:
        if e["code"] == "demo-abj":
            return e["id"]
    r = await client.post(
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
    return (await client.get("/v1/espaces")).json()["donnees"][0]["id"]


async def test_cycle_volume(client):
    espace_id = await _espace(client)
    vid = None
    corps = {
        "espaceId": espace_id,
        "nom": "data-01",
        "tailleGo": 20,
        "classe": "ssd",
        "chiffre": True,
    }
    r = await client.post("/v1/volumes", json=corps)
    assert r.status_code == 202, r.text
    travail = r.json()
    assert travail["type"] == "volume.create" and travail["statut"] == "done"

    r = await client.get("/v1/volumes")
    assert r.status_code == 200
    vols = [v for v in r.json()["donnees"] if v["nom"] == "data-01"]
    assert len(vols) == 1 and vols[0]["nom"] == "data-01"
    vid = vols[0]["id"]

    r = await client.get(f"/v1/volumes/{vid}")
    assert r.status_code == 200 and r.json()["classe"] == "ssd"

    r = await client.put(f"/v1/volumes/{vid}/attachement", json={"vmId": "vm-demo-web"})
    assert r.status_code == 202, r.text
    assert r.json()["type"] == "volume.attach" and r.json()["statut"] == "done"

    r = await client.put(f"/v1/volumes/{vid}/attachement", json={"vmId": "vm-demo-web"})
    assert r.status_code == 409

    r = await client.delete(f"/v1/volumes/{vid}", params={"confirmation": "data-01"})
    assert r.status_code == 409  # encore attaché

    r = await client.delete(f"/v1/volumes/{vid}/attachement")
    assert r.status_code == 202 and r.json()["statut"] == "done"

    r = await client.delete(f"/v1/volumes/{vid}", params={"confirmation": "le-mauvais"})
    assert r.status_code == 422

    r = await client.delete(f"/v1/volumes/{vid}", params={"confirmation": "data-01"})
    assert r.status_code == 204

    r = await client.get("/v1/volumes")
    assert all(v["id"] != vid for v in r.json()["donnees"])


async def test_volume_quota_depasse(client):
    espace_id = await _espace(client)
    corps = {
        "espaceId": espace_id,
        "nom": "gros-01",
        "tailleGo": 3000,
        "classe": "hdd",
        "chiffre": False,
    }
    r = await client.post("/v1/volumes", json=corps)
    assert r.status_code == 202, r.text
    vid = (await client.get("/v1/volumes")).json()["donnees"][0]["id"]

    r = await client.post(f"/v1/volumes/{vid}/extension", json={"tailleGo": 3100})
    assert r.status_code == 402 and r.json()["erreur"]["code"] == "quota_depasse"


async def test_cycle_bucket(client):
    espace_id = await _espace(client)
    corps = {
        "espaceId": espace_id,
        "nom": "archives-prod",
        "region": "ABJ",
        "classe": "froid",
        "versioning": True,
        "policy": "prive",
    }
    r = await client.post("/v1/buckets", json=corps)
    assert r.status_code == 201, r.text
    bucket = r.json()
    assert bucket["nom"] == "archives-prod" and bucket["versioning"] is True

    bid = bucket["id"]
    r = await client.get(f"/v1/buckets/{bid}/usage")
    assert r.status_code == 200 and r.json()["objets"] == 0

    r = await client.patch(
        f"/v1/buckets/{bid}",
        json={
            "espaceId": espace_id,
            "nom": "archives-prod",
            "region": "ABJ",
            "classe": "chaud",
            "policy": "prive",
        },
    )
    assert r.status_code == 200 and r.json()["classe"] == "chaud"

    r = await client.delete(f"/v1/buckets/{bid}", params={"confirmation": "archives-prod"})
    assert r.status_code == 204

    r = await client.get("/v1/buckets")
    assert r.json()["pagination"]["total"] == 0


async def test_cycle_cle_s3(client):
    r = await client.post(
        "/v1/cles-s3", json={"nom": "ci-deploy", "buckets": ["archives-prod"], "droits": "lecture"}
    )
    assert r.status_code == 201, r.text
    corps = r.json()
    assert corps["accessKeyId"] and corps["secretAccessKey"]
    assert corps["endpoint"].startswith("https://")

    cid = corps["cle"]["id"]

    r = await client.get(f"/v1/cles-s3/{cid}")
    assert r.status_code == 200 and r.json()["droits"] == "lecture"

    r = await client.delete(f"/v1/cles-s3/{cid}", params={"confirmation": "ci-deploy"})
    assert r.status_code == 204

    r = await client.get("/v1/cles-s3")
    assert r.json()["pagination"]["total"] == 0
