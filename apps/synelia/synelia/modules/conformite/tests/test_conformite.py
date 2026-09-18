"""Tests du module Conformité : anomalies, attestations, rapports.

Reste sur `client` (admin plateforme) : anomalies et attestations sont **seedées** par le
peupleur de démo ; une organisation cliente fraîche les voit vides mais fonctionnelles.
L'accès `org_admin` aux endpoints est couvert par `test_rbac_client.py`."""


async def test_lister_anomalies(client):
    r = await client.get("/v1/anomalies")
    assert r.status_code == 200, r.text
    corps = r.json()
    assert "donnees" in corps and "pagination" in corps
    assert len(corps["donnees"]) == 2


async def test_traiter_anomalie(client):
    r = await client.get("/v1/anomalies")
    anomalie = r.json()["donnees"][0]
    a_id = anomalie["id"]

    r = await client.post(
        f"/v1/anomalies/{a_id}/correctif", json={"decision": "appliquer", "motif": "plan home"}
    )
    assert r.status_code == 202, r.text
    assert r.json()["type"] == "conformite.correctif" and r.json()["statut"] == "done"

    r = await client.get("/v1/anomalies")
    updated = next(a for a in r.json()["donnees"] if a["id"] == a_id)
    assert updated["statut"] == "corrigee"


async def test_generer_et_lister_attestations(client):
    r = await client.post("/v1/attestations/hebergement", json={"periode": "2026-09"})
    assert r.status_code == 202, r.text
    attestation = r.json()
    assert attestation["id"] == "hebergement"
    assert attestation["type"] == "hebergement" and attestation["disponible"] is True

    r = await client.get("/v1/attestations")
    assert r.status_code == 200, r.text
    hebergement = next(a for a in r.json() if a["id"] == "hebergement")
    assert hebergement["disponible"] is True and hebergement["periode"] == "2026-08"


async def test_rapports_conformite(client):
    r = await client.post(
        "/v1/conformite/rapports",
        json={"referentiel": "3-2-1", "periode": "2026-09", "perimetre": ["vm-app-01"]},
    )
    assert r.status_code == 202, r.text
    assert r.json()["type"] == "conformite.rapport" and r.json()["statut"] == "done"

    r = await client.get("/v1/conformite/rapports")
    assert r.status_code == 200, r.text
    rapports = r.json()
    assert any(rp["referentiel"] == "3-2-1" and rp["periode"] == "2026-09" for rp in rapports)
