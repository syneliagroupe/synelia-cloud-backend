"""Tests du tableau de bord et du copilote."""


async def test_tableau_de_bord(client_org):
    r = await client_org.get("/v1/tableau-de-bord")
    assert r.status_code == 200, r.text
    corps = r.json()
    assert "espaces" in corps and "vms" in corps
    assert "quota" in corps and "usage" in corps
    assert corps["depenseMois"] >= 0
    assert "evenements" in corps and "travauxEnCours" in corps


async def test_copilote(client_org):
    r = await client_org.post("/v1/copilote", json={"question": "Combien de VMs j'ai ?"})
    assert r.status_code == 200, r.text
    corps = r.json()
    assert "reponse" in corps
    assert "vm" in corps["reponse"].lower() or "machine" in corps["reponse"].lower()


async def test_copilote_accents(client_org):
    # « dépense » (accentué) doit matcher le même mot-clé que « depense ».
    r = await client_org.post("/v1/copilote", json={"question": "Combien je dépense ce mois ?"})
    assert r.status_code == 200, r.text
    assert "FCFA" in r.json()["reponse"]


async def test_copilote_suggestions(client_org):
    r = await client_org.get("/v1/copilote/suggestions")
    assert r.status_code == 200
    assert len(r.json()) == 3
