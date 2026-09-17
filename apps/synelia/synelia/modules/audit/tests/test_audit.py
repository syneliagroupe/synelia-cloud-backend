"""Audit : journalisation des événements, filtres, export."""

from synelia.modules.audit import service


async def test_lister_audit(client):
    # créer une ressource pour générer des événements
    r = await client.post(
        "/v1/organisations",
        json={"nom": "Audit Org", "pays": "CI"},
    )
    assert r.status_code == 201

    r = await client.get("/v1/audit")
    assert r.status_code == 200
    corps = r.json()
    assert corps["pagination"]["total"] >= 1
    ev = corps["donnees"][0]
    assert ev["actor"]["type"] == "user"
    assert ev["result"] in ("ok", "refuse", "erreur")
    assert ev["scope"]["type"] == "org"

    r = await client.get("/v1/audit", params={"action": "organisation.creation"})
    assert r.status_code == 200
    assert any(e["action"] == "organisation.creation" for e in r.json()["donnees"])


async def test_export_audit(client):
    # génère quelques événements réels à exporter
    r = await client.post("/v1/organisations", json={"nom": "Export Org", "pays": "CI"})
    assert r.status_code == 201

    r = await client.post(
        "/v1/audit/export",
        json={"depuis": "2024-01-01T00:00:00Z", "jusqua": "2026-12-31T23:59:59Z", "format": "csv"},
    )
    assert r.status_code == 202, r.text
    corps = r.json()
    assert corps["travailId"]
    assert corps["urlTelechargement"]
    assert corps["expire"]

    # le travail tourne en ligne en test : le fichier est déjà déposé quand la réponse revient
    r = await client.get(f"/v1/audit/exports/{corps['travailId']}")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    texte = r.content.decode("utf-8-sig")
    lignes = texte.strip().splitlines()
    assert lignes[0].split(";") == list(service.COLONNES_EXPORT)
    assert len(lignes) > 1


async def test_export_audit_json(client):
    r = await client.post(
        "/v1/audit/export",
        json={"depuis": "2024-01-01T00:00:00Z", "jusqua": "2026-12-31T23:59:59Z", "format": "json"},
    )
    assert r.status_code == 202, r.text
    travail_id = r.json()["travailId"]

    r = await client.get(f"/v1/audit/exports/{travail_id}")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/json")
    donnees = r.json()
    assert isinstance(donnees, list)
    assert donnees
    assert donnees[0]["hash"]


async def test_export_audit_pdf_refuse(client):
    r = await client.post(
        "/v1/audit/export",
        json={"depuis": "2024-01-01T00:00:00Z", "jusqua": "2026-12-31T23:59:59Z", "format": "pdf"},
    )
    assert r.status_code == 202, r.text
    travail_id = r.json()["travailId"]
    r = await client.get(f"/v1/travaux/{travail_id}")
    assert r.status_code == 200, r.text
    assert r.json()["statut"] == "failed"


async def test_integrite_audit(client):
    r = await client.post("/v1/organisations", json={"nom": "Intègre Org", "pays": "CI"})
    assert r.status_code == 201

    r = await client.get("/v1/audit/integrite")
    assert r.status_code == 200, r.text
    corps = r.json()
    assert corps["intacte"] is True
    assert corps["entreesVerifiees"] >= 1
    assert corps["ruptureId"] is None
