"""Tests du support client_org : KB, pièces, cycle de vie d'un ticket."""

import base64

PUB = {"Authorization": ""}


async def _creer_ticket(client_org, sujet="Problème VM") -> dict:
    r = await client_org.post(
        "/v1/support/tickets",
        json={"sujet": sujet, "gravite": "majeure", "contenu": "Ma VM ne démarre pas."},
    )
    assert r.status_code == 201, r.text
    return r.json()


async def test_kb_liste(client_org):
    r = await client_org.get("/v1/support/base-connaissances", headers=PUB)
    assert r.status_code == 200, r.text
    assert r.json()["pagination"]["total"] == 6


async def test_kb_article(client_org):
    r = await client_org.get("/v1/support/base-connaissances/kb-01", headers=PUB)
    assert r.status_code == 200
    assert r.json()["titre"] == "Créer son premier Espace Cloud"
    r = await client_org.get("/v1/support/base-connaissances/inconnu", headers=PUB)
    assert r.status_code == 404


async def test_piece(client_org):
    contenu = base64.b64encode(b"contenu du fichier").decode()
    r = await client_org.post(
        "/v1/support/pieces",
        json={
            "nom": "capture.png",
            "typeMime": "image/png",
            "tailleOctets": 20,
            "contenuBase64": contenu,
        },
    )
    assert r.status_code == 201, r.text
    assert "id" in r.json()


async def test_piece_trop_grosse(client_org):
    contenu = base64.b64encode(b"x" * (11 * 1024 * 1024)).decode()
    r = await client_org.post(
        "/v1/support/pieces",
        json={
            "nom": "gros.bin",
            "typeMime": "application/octet-stream",
            "tailleOctets": 11 * 1024 * 1024,
            "contenuBase64": contenu,
        },
    )
    assert r.status_code == 422


async def test_cycle_ticket(client_org):
    t = await _creer_ticket(client_org)
    tid = t["id"]
    assert t["statut"] == "ouvert" and t["gravite"] == "majeure"

    r = await client_org.get("/v1/support/tickets")
    assert r.status_code == 200
    assert r.json()["pagination"]["total"] == 1

    r = await client_org.get(f"/v1/support/tickets/{tid}")
    assert r.status_code == 200
    assert r.json()["numero"] == t["numero"]

    r = await client_org.patch(f"/v1/support/tickets/{tid}", json={"statut": "attente_client"})
    assert r.status_code == 200, r.text
    assert r.json()["statut"] == "attente_client"

    r = await client_org.post(
        f"/v1/support/tickets/{tid}/messages", json={"contenu": "J'ai redémarré, ça marche."}
    )
    assert r.status_code == 201, r.text
    assert len(r.json()["messages"]) == 2

    r = await client_org.post(
        f"/v1/support/tickets/{tid}/escalade", json={"motif": "Impact production"}
    )
    assert r.status_code == 200, r.text
    assert any("Escalade demandée" in m["contenu"] for m in r.json()["messages"])

    r = await client_org.post(f"/v1/support/tickets/{tid}/escalade", json={"motif": "Encore"})
    assert r.status_code == 409


async def test_verification_sans_auth_interdite(client_org):
    # `/support/tickets` liste les tickets d'une organisation : sans jeton, l'API doit
    # refuser plutôt que renvoyer une liste vide (silence indiscernable d'une fuite).
    r = await client_org.get("/v1/support/tickets", headers=PUB)
    assert r.status_code == 401
