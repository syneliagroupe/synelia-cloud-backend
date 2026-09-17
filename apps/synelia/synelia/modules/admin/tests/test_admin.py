"""Module admin (super admin — pilotage / infrastructure / exploitation / clients)."""

import datetime

MAINTENANT = datetime.datetime.now(datetime.UTC).isoformat()


async def _creer_membre(client, email="support.ci@synelia.cloud"):
    r = await client.post(
        "/v1/admin/equipe",
        json={
            "nom": "Awa S.",
            "email": email,
            "role": "platform_operator",
            "equipe": "Plateforme",
            "privilegie": True,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


async def _creer_ticket(client):
    r = await client.post(
        "/v1/support/tickets",
        json={
            "sujet": "Latence élevée",
            "gravite": "majeure",
            "contenu": "Les VM répondent lentement.",
            "service": "VM",
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


async def _creer_lead(client):
    r = await client.post(
        "/v1/public/contact",
        json={
            "nom": "Koffi D.",
            "email": "koffi@demo.ci",
            "sujet": "commercial",
            "message": "Devis cloud.",
            "organisation": "Demo SA",
        },
    )
    assert r.status_code == 201, r.text
    return r


# ── pilotage ─────────────────────────────────────────────────────────────


async def test_audit_plateforme(client):
    r = await client.get("/v1/admin/audit")
    assert r.status_code == 200, r.text
    assert "pagination" in r.json()


async def test_audit_plateforme_filtre_depuis_jusqua(client):
    # `depuis`/`jusqua` doivent être lus depuis la valeur passée en requête, pas depuis
    # l'horloge serveur — régression : le filtre comparait `Audit.date` à `maintenant()`
    # quelle que soit la date demandée, donc `depuis` dans le passé ne renvoyait rien et
    # `jusqua` dans le passé renvoyait tout.
    await _creer_lead(client)  # garantit au moins une entrée d'audit récente
    r = await client.get("/v1/admin/audit?depuis=2020-01-01T00:00:00Z")
    assert r.status_code == 200, r.text
    assert r.json()["pagination"]["total"] >= 1

    r = await client.get("/v1/admin/audit?jusqua=2020-01-01T00:00:00Z")
    assert r.status_code == 200, r.text
    assert r.json()["pagination"]["total"] == 0


async def test_travaux_plateforme(client):
    r = await client.get("/v1/admin/travaux")
    assert r.status_code == 200, r.text
    assert "pagination" in r.json()


async def test_tableau_de_bord_plateforme(client):
    r = await client.get("/v1/admin/tableau-de-bord")
    assert r.status_code == 200, r.text
    corps = r.json()
    assert "tenantsActifs" in corps and "espacesTotal" in corps


async def test_sante_plateforme(client):
    r = await client.get("/v1/admin/sante")
    assert r.status_code == 200, r.text
    assert "backends" in r.json() and "filesProvisioning" in r.json()


async def test_sites_physiques(client):
    r = await client.get("/v1/admin/sites")
    assert r.status_code == 200, r.text
    assert any(s["code"] == "ABJ" for s in r.json())


# ── infrastructure ───────────────────────────────────────────────────────


async def test_backends_cycle(client):
    r = await client.get("/v1/admin/backends")
    assert r.status_code == 200, r.text
    backends = r.json()["donnees"]
    assert len(backends) >= 1
    bid = backends[0]["id"]

    r = await client.get(f"/v1/admin/backends/{bid}")
    assert r.status_code == 200 and r.json()["id"] == bid

    r = await client.patch(f"/v1/admin/backends/{bid}", json={"statut": "maintenance"})
    assert r.status_code == 200 and r.json()["statut"] == "maintenance"

    r = await client.patch(f"/v1/admin/backends/{bid}", json={"statut": "en_ligne"})
    assert r.status_code == 200


async def test_capacite(client):
    r = await client.get("/v1/admin/capacite")
    assert r.status_code == 200, r.text
    assert "backends" in r.json() and "capaciteParSite" in r.json()


async def test_placements(client):
    r = await client.get("/v1/admin/backends")
    backends = r.json()["donnees"]
    assert len(backends) >= 1
    bid = backends[0]["id"]
    r = await client.put(
        "/v1/admin/placements",
        json={
            "placements": [
                {"id": "pl-demo-abj-1", "espaceId": "demo-abj", "backendId": bid, "percent": 100}
            ]
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()[0]["percent"] == 100


async def test_marketplace_campagnes_cycle(client):
    r = await client.get("/v1/admin/marketplace/campagnes")
    assert r.status_code == 200, r.text

    r = await client.post(
        "/v1/admin/marketplace/campagnes",
        json={
            "nom": "Maj VM 1",
            "catalogSlug": "vm",
            "versionCible": "2.1.0",
            "fenetre": "2026-02-01T00:00:00Z",
            "strategie": "par_vagues",
            "instances": [],
        },
    )
    assert r.status_code == 201, r.text
    cid = r.json()["id"]

    r = await client.post(f"/v1/admin/marketplace/campagnes/{cid}/suspension")
    assert r.status_code == 202, r.text

    r = await client.post(f"/v1/admin/marketplace/campagnes/{cid}/lancement")
    assert r.status_code == 202, r.text


async def test_marketplace_instances(client):
    r = await client.get("/v1/admin/marketplace/instances")
    assert r.status_code == 200, r.text
    assert "pagination" in r.json()


async def test_migration_campagnes_cycle(client):
    r = await client.get("/v1/admin/migration/campagnes")
    assert r.status_code == 200, r.text

    r = await client.post(
        "/v1/admin/migration/campagnes",
        json={
            "nom": "Mig ABJ→GBM",
            "backendSource": "backend-abj",
            "backendCible": "backend-gbm",
            "ressources": [],
            "fenetre": "2026-03-01T00:00:00Z",
            "notifierClients": True,
        },
    )
    assert r.status_code == 201, r.text
    cid = r.json()["id"]

    r = await client.post(f"/v1/admin/migration/campagnes/{cid}/suspension")
    assert r.status_code == 202, r.text

    r = await client.post(f"/v1/admin/migration/campagnes/{cid}/lancement")
    assert r.status_code == 202, r.text

    r = await client.post(
        f"/v1/admin/migration/campagnes/{cid}/rollback", params={"confirmation": "Mig ABJ→GBM"}
    )
    assert r.status_code == 202, r.text


async def test_conformite(client):
    r = await client.get("/v1/admin/conformite")
    assert r.status_code == 200, r.text
    assert len(r.json()["referentiels"]) >= 1


async def test_fenetres_patching_cycle(client):
    r = await client.get("/v1/admin/conformite/fenetres-patching")
    assert r.status_code == 200, r.text

    r = await client.post(
        "/v1/admin/conformite/fenetres-patching",
        json={
            "libelle": "Fenêtre mensuelle",
            "perimetre": "Socle Linux",
            "debut": MAINTENANT,
            "dureeMin": 60,
            "recurrence": "mensuelle",
            "impactClient": "Aucune",
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["statut"] == "planifiee"


async def test_tests_restauration(client):
    r = await client.post(
        "/v1/admin/conformite/tests-restauration",
        json={"perimetre": "toutes", "echantillonPct": 10.0},
    )
    assert r.status_code == 202, r.text


# ── exploitation ─────────────────────────────────────────────────────────


async def test_equipe_cycle(client):
    r = await client.get("/v1/admin/equipe")
    assert r.status_code == 200, r.text

    membre = await _creer_membre(client)
    mid = membre["id"]

    r = await client.patch(
        f"/v1/admin/equipe/{mid}", json={"role": "org_admin", "equipe": "Support"}
    )
    assert r.status_code == 200 and r.json()["role"] == "org_admin"

    r = await client.get(f"/v1/admin/equipe/{mid}/elevation")
    assert r.status_code == 200, r.text

    r = await client.post(
        f"/v1/admin/equipe/{mid}/elevation",
        json={"role": "platform_operator", "motif": "Incident", "dureeMin": 120},
    )
    assert r.status_code == 201, r.text
    assert r.json()["actif"] is True

    r = await client.delete(f"/v1/admin/equipe/{mid}/elevation")
    assert r.status_code == 204

    r = await client.delete(f"/v1/admin/equipe/{mid}")
    assert r.status_code == 204


async def test_equipe_ne_pas_retirer_dernier_super_admin(client):
    mid = None
    for m in (await client.get("/v1/admin/equipe")).json()["donnees"]:
        if m["role"] == "super_admin":
            mid = m["id"]
    assert mid
    r = await client.delete(f"/v1/admin/equipe/{mid}")
    assert r.status_code in (409, 204)


async def test_leads_cycle(client):
    r = await client.get("/v1/admin/leads")
    assert r.status_code == 200, r.text

    await _creer_lead(client)
    r = await client.get("/v1/admin/leads")
    donnees = r.json()["donnees"]
    assert len(donnees) >= 1
    lead = donnees[0]

    r = await client.patch(
        f"/v1/admin/leads/{lead['id']}", json={"statut": "qualifie", "assigneA": "support"}
    )
    assert r.status_code == 200, r.text
    assert r.json()["statut"] == "qualifie"


async def test_incidents_cycle(client):
    r = await client.get("/v1/admin/statut/incidents")
    assert r.status_code == 200, r.text

    r = await client.post(
        "/v1/admin/statut/incidents",
        json={
            "titre": "Panne réseau",
            "gravite": "majeur",
            "services": ["Réseau"],
            "sites": ["ABJ"],
            "message": "Perturbation détectée.",
        },
    )
    assert r.status_code == 201, r.text
    iid = r.json()["id"]

    r = await client.post(
        f"/v1/admin/statut/incidents/{iid}",
        json={"texte": "Rétablissement en cours.", "statut": "surveille"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["statut"] == "surveille"


async def test_statut_services(client):
    r = await client.put(
        "/v1/admin/statut/services",
        json={
            "services": [
                {
                    "nom": "Compute",
                    "categorie": "Compute",
                    "etats": {"ABJ": "operationnel", "GBM": "operationnel"},
                    "uptime90j": 99.9,
                }
            ]
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()[0]["nom"] == "Compute"


async def test_tickets_cycle(client):
    r = await client.get("/v1/admin/tickets")
    assert r.status_code == 200, r.text

    ticket = await _creer_ticket(client)
    tid = ticket["id"]

    r = await client.patch(
        f"/v1/admin/tickets/{tid}", json={"statut": "en_cours", "gravite": "critique"}
    )
    assert r.status_code == 200, r.text
    assert r.json()["statut"] == "en_cours"

    r = await client.post(
        f"/v1/admin/tickets/{tid}/messages", json={"contenu": "Nous investiguons."}
    )
    assert r.status_code == 201, r.text


# ── clients ──────────────────────────────────────────────────────────────


async def test_notifier_organisation(client):
    org_id = client.org_id
    r = await client.post(
        f"/v1/admin/organisations/{org_id}/notification",
        json={
            "sujet": "Maintenance planifiée",
            "message": "Maintenance dimanche.",
            "canaux": ["email"],
            "roles": ["org_admin"],
        },
    )
    assert r.status_code == 200, r.text
    assert "destinataires" in r.json()
