"""Facturation : estimation, consommation, factures, paiement, prépayé, SLA, souscriptions, devis."""


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


async def test_consommation_couvre_volumes_lb_et_web_cloud(client):
    """Régression : un volume Cinder, un load balancer Octavia et une VM d'hébergement Web
    Cloud sont de vraies ressources OpenStack — elles doivent alourdir la facture, pas
    disparaître dans un angle mort de la métrologie."""
    espace_id = await _espace(client)
    total_avant = (
        await client.get("/v1/facturation/consommation", params={"periode": "2026-08"})
    ).json()["total"]

    r = await client.post(
        "/v1/volumes",
        json={
            "espaceId": espace_id,
            "nom": "data-conso",
            "tailleGo": 100,
            "classe": "ssd",
            "chiffre": True,
        },
    )
    assert r.status_code == 202, r.text
    total_apres_volume = (
        await client.get("/v1/facturation/consommation", params={"periode": "2026-08"})
    ).json()["total"]
    assert total_apres_volume > total_avant, "un volume Cinder doit être facturé"

    r = await client.post(
        "/v1/load-balancers",
        json={
            "espaceId": espace_id,
            "nom": "lb-conso",
            "layer": "l7",
            "exposure": "public",
            "algo": "round_robin",
            "listeners": [{"protocole": "http", "port": 80}],
        },
    )
    assert r.status_code == 202, r.text
    total_apres_lb = (
        await client.get("/v1/facturation/consommation", params={"periode": "2026-08"})
    ).json()["total"]
    assert total_apres_lb > total_apres_volume, "un load balancer doit être facturé"

    from synelia_testing import enregistrer_domaine

    await enregistrer_domaine(client, "conso-web.test")
    r = await client.post(
        "/v1/web/hebergements",
        json={"palier": "pro", "site": "ABJ", "domaine": "conso-web.test"},
    )
    assert r.status_code == 202, r.text
    total_apres_web = (
        await client.get("/v1/facturation/consommation", params={"periode": "2026-08"})
    ).json()["total"]
    assert total_apres_web > total_apres_lb, "une VM d'hébergement Web Cloud doit être facturée"


async def test_offre_souscrite_dans_la_facture(client):
    """L'abonnement réel de l'organisation (`tenantPlan` → code d'une Offre du catalogue)
    doit apparaître comme ligne de base sur la facture, en plus de la consommation."""
    r = await client.get("/v1/admin/catalogue/offres", params={"categorie": "espace_cloud"})
    assert r.status_code == 200, r.text
    codes = {o["code"]: o for o in r.json()["donnees"]}
    assert "espace-pro" in codes

    r = await client.patch(f"/v1/organisations/{client.org_id}", json={"tenantPlan": "espace-pro"})
    assert r.status_code == 200 and r.json()["tenantPlan"] == "espace-pro"

    r = await client.post("/v1/admin/facturation/cycle", json={"periode": "2026-09"})
    assert r.status_code == 202, r.text

    r = await client.get("/v1/admin/facturation/cycles", params={"periode": "2026-09"})
    assert r.status_code == 200, r.text
    cycle = r.json()["donnees"][0]
    assert cycle["statut"] == "termine"
    assert cycle["facturesEmises"] >= 1
    assert cycle["montantTotal"] > 0

    r = await client.get(
        "/v1/admin/facturation/factures",
        params={"orgId": client.org_id, "periode": "2026-08"},
    )
    assert r.status_code == 200, r.text
    # La démo (`facturation.service.demo`) a déjà posé une facture fixe `facture-demo` pour
    # cette même période : celle du cycle qu'on vient de lancer est la nouvelle, distincte.
    factures = [f for f in r.json()["donnees"] if f["id"] != "facture-demo"]
    assert len(factures) == 1, r.text
    lignes = factures[0]["lignes"]
    assert any(ligne["libelle"].startswith("Abonnement Espace Pro") for ligne in lignes)
    assert any(ligne["libelle"].startswith("Consommation") for ligne in lignes)
    assert factures[0]["sousTotal"] == sum(ligne["total"] for ligne in lignes)


async def test_estimation(client):
    r = await client.post(
        "/v1/facturation/estimation",
        json={"type": "vm", "quantite": 1, "specification": {"vcpu": 2, "ramGo": 4, "diskGo": 40}},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["devise"] == "XOF" and body["totalMensuel"] > 0


async def test_consommation(client):
    r = await client.get("/v1/facturation/consommation?periode=2026-08")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "periode" in body and "jours" in body


async def test_consommation_export(client):
    r = await client.post(
        "/v1/facturation/consommation/export", json={"periode": "2026-08", "format": "csv"}
    )
    assert r.status_code == 202, r.text
    assert "url" in r.json()


async def test_factures(client):
    r = await client.get("/v1/facturation/factures")
    assert r.status_code == 200, r.text
    factures = r.json()["donnees"]
    assert len(factures) == 1 and factures[0]["statut"] == "emise"
    fid = factures[0]["id"]

    r = await client.get(f"/v1/facturation/factures/{fid}")
    assert r.status_code == 200 and r.json()["numero"] == "SYN-2026-000001"

    r = await client.get(f"/v1/facturation/factures/{fid}/pdf")
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    assert r.content.startswith(b"%PDF")

    r = await client.post(f"/v1/facturation/factures/{fid}/paiement", json={"moyenId": "moyen-x"})
    assert r.status_code == 200, r.text
    assert r.json()["statut"] == "payee"

    r = await client.post(f"/v1/facturation/factures/{fid}/paiement", json={"moyenId": "moyen-x"})
    assert r.status_code == 409 and r.json()["erreur"]["code"] == "facture_deja_payee"


async def test_moyens_paiement(client):
    r = await client.post(
        "/v1/facturation/moyens-paiement",
        json={"type": "carte", "numero": "4242424242424242", "defaut": True},
    )
    assert r.status_code == 201, r.text
    mid = r.json()["id"]
    r = await client.get("/v1/facturation/moyens-paiement")
    assert r.status_code == 200 and len(r.json()) == 1
    r = await client.patch(f"/v1/facturation/moyens-paiement/{mid}", json={"libelle": "Visa Pro"})
    assert r.status_code == 200 and r.json()["libelle"] == "Visa Pro"
    r = await client.delete(f"/v1/facturation/moyens-paiement/{mid}")
    assert r.status_code == 204


async def test_prepaye_rechargement(client):
    r = await client.post(
        "/v1/facturation/prepaye/rechargement", json={"montant": 25000, "moyenId": "moyen-x"}
    )
    assert r.status_code == 200, r.text
    assert r.json()["statut"] == "credite" and r.json()["solde"] == 25000


async def test_sla(client):
    r = await client.get("/v1/facturation/sla")
    assert r.status_code == 200 and len(r.json()["engagements"]) == 3
    r = await client.post(
        "/v1/facturation/sla/reclamations",
        json={"periode": "2026-08", "composant": "compute", "motif": "Coupure 2h"},
    )
    assert r.status_code == 201 and "reference" in r.json()


async def test_souscriptions(client):
    r = await client.get("/v1/facturation/souscriptions")
    assert r.status_code == 200


async def test_devis_acceptation(client):
    # créer un devis via le dépôt n'est pas exposé ; on teste la route lister
    r = await client.get("/v1/facturation/devis")
    assert r.status_code == 200


async def test_ventilation(client):
    r = await client.get("/v1/facturation/ventilation?axe=espace")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "lignes" in body and body["total"] >= 0


async def test_ventilation_par_espace_affiche_le_code_pas_luuid(client):
    """Régression : la répartition « Par Espace Cloud » affichait l'UUID technique de
    l'Espace comme libellé au lieu de son code lisible (`espace-abj`, par exemple)."""
    espace_id = await _espace(client)
    # Pas d'id de maquette (`debian-12`) : ce dépôt exécute ses tests contre le vrai
    # catalogue Glance (`SYNELIA_FOURNISSEUR=openstack` dans `.env`), dont le seul système
    # actuellement publié sur dev01 est `ubuntu-24.04-v1.33.12` — voir `catalogue/router.py`.
    images = (await client.get("/v1/catalogue/images")).json()
    image_id = images[0]["id"]
    r = await client.post(
        "/v1/vms",
        json={
            "espaceId": espace_id,
            "nom": "vm-vent",
            "imageId": image_id,
            "vcpu": 1,
            "ramGo": 2,
            "diskGo": 20,
        },
    )
    assert r.status_code == 202, r.text
    r = await client.get("/v1/facturation/ventilation?axe=espace")
    assert r.status_code == 200, r.text
    labels = [ligne["label"] for ligne in r.json()["lignes"]]
    assert "demo-abj" in labels, labels
    assert espace_id not in labels, labels
