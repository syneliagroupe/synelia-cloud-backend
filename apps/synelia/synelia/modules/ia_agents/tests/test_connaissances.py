import pytest

pytestmark = pytest.mark.anyio

TEXTE = (
    "Les employés de Synelia Cloud disposent de vingt-six jours de congés payés par an. "
    "Toute demande de congés doit être soumise deux semaines à l'avance via le portail RH. "
    "Les congés maladie ne sont pas déduits du solde de congés payés annuel."
)


async def test_ingestion_puis_recherche_simulees(client):
    """Sans SYNELIA_QDRANT_URL (cas des tests) : ingestion et recherche entièrement simulées,
    sans réseau — mais avec un vrai découpage et une vraie recherche par recouvrement de mots."""
    r = await client.post(
        "/v1/ia/connaissances",
        json={
            "nom": "RH",
            "espaceId": "espace-demo-abj",
            "source": {"type": "drive", "libelle": "Drive RH"},
        },
    )
    assert r.status_code == 201, r.text
    kb = r.json()
    assert kb["statut"] == "jamais_indexee"
    assert kb["documents"] == 0

    r = await client.post(
        f"/v1/ia/connaissances/{kb['id']}/documents", json={"nom": "conges.md", "texte": TEXTE}
    )
    assert r.status_code == 202, r.text
    # SYNELIA_TRAVAUX_EN_LIGNE=1 en test : le travail s'exécute avant la réponse.
    assert r.json()["statut"] == "done", r.json()

    r = await client.get(f"/v1/ia/connaissances/{kb['id']}")
    assert r.status_code == 200
    corps = r.json()
    assert corps["documents"] == 1
    assert corps["fragments"] >= 1
    assert corps["statut"] == "a_jour"
    assert corps["modeleEmbedding"] == "simule"

    r = await client.post(
        f"/v1/ia/connaissances/{kb['id']}/rechercher",
        json={"query": "congés maladie déduits", "topK": 3},
    )
    assert r.status_code == 200, r.text
    fragments = r.json()["fragments"]
    assert fragments
    assert "maladie" in fragments[0]["texte"]
    assert fragments[0]["document"] == "conges.md"
    assert fragments[0]["score"] > 0
    assert fragments[0]["citations"]  # citations=True par défaut sur la base créée ci-dessus


async def test_recherche_sans_resultat_pertinent(client):
    r = await client.post(
        "/v1/ia/connaissances",
        json={
            "nom": "RH bis",
            "espaceId": "espace-demo-abj",
            "source": {"type": "web", "libelle": "Site RH"},
        },
    )
    kb_id = r.json()["id"]
    await client.post(f"/v1/ia/connaissances/{kb_id}/documents", json={"nom": "x.md", "texte": TEXTE})

    r = await client.post(
        f"/v1/ia/connaissances/{kb_id}/rechercher", json={"query": "météo à Abidjan demain"}
    )
    assert r.status_code == 200
    assert r.json()["fragments"] == []


async def test_ingestion_sans_contenu_422(client):
    r = await client.post(
        "/v1/ia/connaissances",
        json={
            "nom": "Vide",
            "espaceId": "espace-demo-abj",
            "source": {"type": "s3", "libelle": "Bucket"},
        },
    )
    kb_id = r.json()["id"]
    r = await client.post(f"/v1/ia/connaissances/{kb_id}/documents", json={"nom": "rien.md"})
    assert r.status_code == 422


async def test_suppression_connaissance(client):
    r = await client.post(
        "/v1/ia/connaissances",
        json={
            "nom": "À supprimer",
            "espaceId": "espace-demo-abj",
            "source": {"type": "git", "libelle": "Dépôt"},
        },
    )
    kb_id = r.json()["id"]
    r = await client.delete(f"/v1/ia/connaissances/{kb_id}")
    assert r.status_code == 422, "sans confirmation, la suppression doit être refusée"
    r = await client.delete(
        f"/v1/ia/connaissances/{kb_id}", params={"confirmation": "À supprimer"}
    )
    assert r.status_code == 204
    r = await client.get(f"/v1/ia/connaissances/{kb_id}")
    assert r.status_code == 404
