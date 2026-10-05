"""Facturation : estimation, consommation, factures, paiement, prépayé, SLA, souscriptions, devis."""

from datetime import date, timedelta


async def _espace(client) -> str:
    existants = (await client.get("/v1/espaces")).json()["donnees"]
    for e in existants:
        if e["code"] == "demo-abj":
            return e["id"]
    r = await client.post(
        "/v1/espaces",
        json={
            "code": "demo-abj",
            "offerId": "offre-espace-pro",
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
    assert any("consommation" in ligne["libelle"].lower() for ligne in lignes)
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


async def test_consommation_avant_creation_espace(client):
    # L'espace de démo est créé "maintenant" (au démarrage du test) : une période
    # antérieure au mois courant est donc entièrement avant sa création et ne doit
    # fabriquer aucune consommation, même si le snapshot courant des VM est non nul.
    mois_precedent = (date.today().replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    r = await client.get(f"/v1/espaces/espace-demo-abj/consommation?periode={mois_precedent}")
    assert r.status_code == 200, r.text
    jours = r.json()["jours"]
    assert jours, "la période devrait contenir des jours"
    assert all(j["montant"] == 0 and j["vcpuHeures"] == 0 for j in jours)
    assert r.json()["total"] == 0


async def test_consommation_exclut_vm_en_erreur(client):
    """Une VM au statut 'error' (provisionnement échoué) ne doit pas être facturée."""
    from synelia_contract import modeles as m
    from synelia_db.modeles import Ressource
    from synelia_db.session import session as db_session
    from synelia_kernel.ids import nouvel_id

    hardware = m.MateielVirtuel(scsiControllers=1, nics=1, usb=False, secureBoot=False)

    def _vm(nom: str, statut: str) -> m.Vm:
        return m.Vm(
            id=nouvel_id(),
            espaceId="espace-test",
            nom=nom,
            os="ubuntu-24.04",
            vcpu=4,
            ramGo=8,
            diskGo=100,
            ips=[],
            statut=statut,
            hardware=hardware,
            site="ABJ",
        )

    en_cours = _vm("vm-running", "running")
    en_erreur = _vm("vm-erreur", "error")

    periode = date.today().strftime("%Y-%m")
    avant = (await client.get(f"/v1/facturation/consommation?periode={periode}")).json()["jours"][0]

    async with db_session() as s:
        for vm in (en_cours, en_erreur):
            s.add(
                Ressource(
                    id=vm.id,
                    org_id=client.org_id,
                    type="vm",
                    nom=vm.nom,
                    statut=vm.statut,
                    donnees=vm.model_dump(mode="json"),
                )
            )
        await s.commit()

    r = await client.get(f"/v1/facturation/consommation?periode={periode}")
    assert r.status_code == 200, r.text
    apres = r.json()["jours"][0]
    # Seule la VM "running" doit alourdir la consommation ; la VM "error" est ignorée.
    assert apres["vcpuHeures"] - avant["vcpuHeures"] == en_cours.vcpu * 24
    assert apres["ramGoHeures"] - avant["ramGoHeures"] == en_cours.ramGo * 24


async def test_consommation_export(client):
    r = await client.post(
        "/v1/facturation/consommation/export", json={"periode": "2026-08", "format": "csv"}
    )
    assert r.status_code == 202, r.text
    url = r.json()["url"]
    assert "url" in r.json()

    # Avant correctif : aucun exécuteur `facturation.export` n'existait, et
    # `GET /travaux/{id}/export` n'existait pas du tout — l'URL rendue par l'API ne menait
    # jamais nulle part. On vérifie maintenant que le fichier est réellement produit et
    # téléchargeable.
    travail_id = url.split("/")[3]
    r = await client.get(f"/v1/travaux/{travail_id}")
    assert r.status_code == 200 and r.json()["statut"] == "done", r.text

    r = await client.get(url)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/csv")
    assert b"date" in r.content and b"montant" in r.content


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

    # Aucune opération mesurée sur 30 jours pour cette organisation : `sla_engagements`
    # considère l'engagement respecté (pas de manquement constaté) — la réclamation doit
    # être refusée, pas créditée à l'aveugle (fraude interne triviale sinon).
    r = await client.post(
        "/v1/facturation/sla/reclamations",
        json={"periode": "2026-08", "composant": "compute", "motif": "Coupure 2h"},
    )
    assert r.status_code == 409 and r.json()["erreur"]["code"] == "sla_non_manque"

    r = await client.post(
        "/v1/facturation/sla/reclamations",
        json={"periode": "2026-08", "composant": "inconnu", "motif": "x"},
    )
    assert r.status_code == 422

    # Manquement réel : assez de travaux `failed` récents sur une cible "compute" pour faire
    # passer la dispo constatée sous l'engagement (99.9 %) — la réclamation doit alors passer.
    from synelia_db.modeles import Travail
    from synelia_db.session import fabrique
    from synelia_kernel.dates import maintenant
    from synelia_kernel.ids import nouvel_id

    org_id = client.org_id
    async with fabrique()() as s:
        for i in range(20):
            s.add(
                Travail(
                    id=nouvel_id(),
                    org_id=org_id,
                    type="vm.create",
                    label="x",
                    statut="failed" if i == 0 else "done",
                    cible_type="vm",
                    started_at=maintenant(),
                    taches=[],
                )
            )
        await s.commit()

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
    # Ne pas figer vcpu/ramGo/diskGo en dur : le catalogue simulé (CI,
    # s1.small/g1.medium/...) et le catalogue réel du lab (k8s.worker/
    # k8s.master seulement) divergent et aucune combinaison fixe ne
    # correspond aux deux — déjà vu deux fois de suite en écrivant ce
    # correctif. Reprendre le premier gabarit réellement au catalogue,
    # quel que soit l'environnement.
    gabarits = (await client.get("/v1/catalogue/gabarits")).json()
    assert gabarits, "Aucun gabarit au catalogue"
    g = gabarits[0]
    r = await client.post(
        "/v1/vms",
        json={
            "espaceId": espace_id,
            "nom": "vm-vent",
            "imageId": image_id,
            "vcpu": g["vcpu"],
            "ramGo": g["ramGo"],
            "diskGo": g["diskGo"],
        },
    )
    assert r.status_code == 202, r.text
    r = await client.get("/v1/facturation/ventilation?axe=espace")
    assert r.status_code == 200, r.text
    labels = [ligne["label"] for ligne in r.json()["lignes"]]
    assert "demo-abj" in labels, labels
    assert espace_id not in labels, labels


async def test_ventilation_egale_prevision_mensuelle(client):
    """Une seule grille : la ventilation (par axe) et la projection de fin de mois racontent
    le même montant mensuel, à l'arrondi journalier près."""
    await _espace(client)
    mois = date.today().strftime("%Y-%m")
    prevision = (await client.get(f"/v1/facturation/consommation?periode={mois}")).json()[
        "prevision"
    ]
    assert prevision > 0
    for axe in ("espace", "famille", "application", "site"):
        r = await client.get(f"/v1/facturation/ventilation?axe={axe}")
        assert r.status_code == 200, r.text
        assert abs(r.json()["total"] - prevision) <= 31, axe
