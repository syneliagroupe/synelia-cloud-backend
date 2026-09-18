"""Stockage : volumes (bloc), buckets (objets), clés S3."""

from synelia_testing import connexion_lab, sur_lab_reel


def _ids_volumes_cinder(nom: str) -> set | None:
    if not sur_lab_reel():
        return None
    c = connexion_lab()
    assert c is not None, "lab réel injoignable"
    # `all_projects=True` : le volume est créé dans le projet de l'Espace, pas celui
    # de la connexion admin (même leçon que Nova : scope par défaut aveugle).
    return {v.id for v in c.block_storage.volumes(name=nom, all_projects=True)}


def _affirmer_volume_cinder(nom: str, avant: set):
    """Le volume doit exister pour de vrai côté Cinder (exactement un de plus)."""
    apres = _ids_volumes_cinder(nom)
    if apres is None:
        return
    assert len(apres - avant) == 1, (
        f"aucun volume Cinder {nom!r} créé : création sans impact OpenStack"
    )


def _affirmer_volume_cinder_absent(nom: str, avant: set):
    # Même asynchronisme que Nova : Cinder purge le volume après le `done` de l'API —
    # on attend la disparition réelle au lieu d'exiger l'immédiat.
    import time

    for _ in range(30):
        apres = _ids_volumes_cinder(nom)
        if apres is None:
            return
        if apres == avant:
            return
        time.sleep(3)
    assert False, f"volume Cinder {nom!r} toujours présent : suppression sans impact OpenStack"


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
    """VM de l'organisation cliente elle-même (jamais la VM de démo seedée, qui
    n'existe que dans l'organisation de démo) : l'attachement d'un volume se teste
    sur une vraie VM du client, comme le fait l'interface.

    Sur lab réel, un build Nova peut légitimement échouer faute de capacité
    (`NoValidHost`, hyperviseurs pleins — constaté) : le test est alors sauté
    honnêtement plutôt que de faire porter l'échec à l'attachement de volume."""
    import pytest
    from synelia_testing import sur_lab_reel

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
    if sur_lab_reel() and r.json().get("statut") != "done":
        pytest.skip(f"création de VM impossible sur le lab (capacité) : {r.json().get('erreur')}")
    vms = (await client_org.get("/v1/vms")).json()["donnees"]
    return next(v["id"] for v in vms if v["nom"] == nom)


async def test_cycle_volume(client_org):
    avant = _ids_volumes_cinder("data-01") or set()
    espace_id = await _espace(client_org)
    vm_id = await _creer_vm(client_org, espace_id, "vm-volume")
    vid = None
    corps = {
        "espaceId": espace_id,
        "nom": "data-01",
        "tailleGo": 20,
        "classe": "ssd",
        "chiffre": True,
    }
    r = await client_org.post("/v1/volumes", json=corps)
    assert r.status_code == 202, r.text
    travail = r.json()
    assert travail["type"] == "volume.create" and travail["statut"] == "done"

    r = await client_org.get("/v1/volumes")
    assert r.status_code == 200
    vols = [v for v in r.json()["donnees"] if v["nom"] == "data-01"]
    assert len(vols) == 1 and vols[0]["nom"] == "data-01"
    vid = vols[0]["id"]
    _affirmer_volume_cinder("data-01", avant)

    r = await client_org.get(f"/v1/volumes/{vid}")
    assert r.status_code == 200 and r.json()["classe"] == "ssd"

    r = await client_org.put(f"/v1/volumes/{vid}/attachement", json={"vmId": vm_id})
    assert r.status_code == 202, r.text
    assert r.json()["type"] == "volume.attach" and r.json()["statut"] == "done"

    r = await client_org.put(f"/v1/volumes/{vid}/attachement", json={"vmId": vm_id})
    assert r.status_code == 409

    r = await client_org.delete(f"/v1/volumes/{vid}", params={"confirmation": "data-01"})
    assert r.status_code == 409  # encore attaché

    r = await client_org.delete(f"/v1/volumes/{vid}/attachement")
    assert r.status_code == 202 and r.json()["statut"] == "done"

    r = await client_org.delete(f"/v1/volumes/{vid}", params={"confirmation": "le-mauvais"})
    assert r.status_code == 422

    r = await client_org.delete(f"/v1/volumes/{vid}", params={"confirmation": "data-01"})
    assert r.status_code == 204

    r = await client_org.get("/v1/volumes")
    assert all(v["id"] != vid for v in r.json()["donnees"])
    _affirmer_volume_cinder_absent("data-01", avant)


async def test_volume_quota_depasse(client_org):
    espace_id = await _espace(client_org)
    corps = {
        "espaceId": espace_id,
        "nom": "gros-01",
        "tailleGo": 3000,
        "classe": "hdd",
        "chiffre": False,
    }
    r = await client_org.post("/v1/volumes", json=corps)
    assert r.status_code == 202, r.text
    vid = (await client_org.get("/v1/volumes")).json()["donnees"][0]["id"]

    r = await client_org.post(f"/v1/volumes/{vid}/extension", json={"tailleGo": 3100})
    assert r.status_code == 402 and r.json()["erreur"]["code"] == "quota_depasse"


async def test_cycle_bucket(client_org):
    espace_id = await _espace(client_org)
    corps = {
        "espaceId": espace_id,
        "nom": "archives-prod",
        "region": "ABJ",
        "classe": "froid",
        "versioning": True,
        "policy": "prive",
    }
    r = await client_org.post("/v1/buckets", json=corps)
    assert r.status_code == 201, r.text
    bucket = r.json()
    assert bucket["nom"] == "archives-prod" and bucket["versioning"] is True

    bid = bucket["id"]
    r = await client_org.get(f"/v1/buckets/{bid}/usage")
    assert r.status_code == 200 and r.json()["objets"] == 0

    r = await client_org.patch(
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

    r = await client_org.delete(f"/v1/buckets/{bid}", params={"confirmation": "archives-prod"})
    assert r.status_code == 204

    r = await client_org.get("/v1/buckets")
    assert r.json()["pagination"]["total"] == 0


async def test_cycle_cle_s3(client_org):
    r = await client_org.post(
        "/v1/cles-s3", json={"nom": "ci-deploy", "buckets": ["archives-prod"], "droits": "lecture"}
    )
    assert r.status_code == 201, r.text
    corps = r.json()
    assert corps["accessKeyId"] and corps["secretAccessKey"]
    assert corps["endpoint"].startswith("https://")

    cid = corps["cle"]["id"]

    r = await client_org.get(f"/v1/cles-s3/{cid}")
    assert r.status_code == 200 and r.json()["droits"] == "lecture"

    r = await client_org.delete(f"/v1/cles-s3/{cid}", params={"confirmation": "ci-deploy"})
    assert r.status_code == 204

    r = await client_org.get("/v1/cles-s3")
    assert r.json()["pagination"]["total"] == 0
