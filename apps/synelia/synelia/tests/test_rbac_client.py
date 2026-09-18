"""RBAC vu d'un compte CLIENT réel (`client_org`, rôle `org_admin`) — jamais admin.

Motif : l'admin plateforme court-circuite `rbac.autorise` (`ctx.principal.
est_admin_plateforme`), donc un endpoint cassé pour un rôle réel restait vert tant que
la suite tournait en `super_admin`. Ce fichier verrouille les deux côtés : le client
passe sur ses chemins, se fait refuser là où c'est l'équipe Synelia."""


async def test_client_accede_a_ses_chemins(client_org):
    for chemin in (
        "/v1/espaces",
        "/v1/vms",
        "/v1/web/hebergements",
        "/v1/web/domaines",
        "/v1/web/dns",
        "/v1/web/emails",
        "/v1/web/ssl",
        "/v1/web/backup",
        "/v1/web/bases",
        "/v1/audit",
        "/v1/anomalies",
        "/v1/attestations",
        "/v1/moi",
    ):
        r = await client_org.get(chemin)
        assert r.status_code == 200, (chemin, r.status_code, r.text)


async def test_client_refuse_sur_admin(client_org):
    for chemin in ("/v1/admin/capacite", "/v1/admin/espaces", "/v1/admin/sante"):
        r = await client_org.get(chemin)
        assert r.status_code == 403, (chemin, r.status_code, r.text)
        assert r.json()["erreur"]["code"] == "interdit"


async def test_client_ne_voit_pas_les_ressources_plateforme(client_org):
    # L'espace partagé `vps-zone` (infrastructure Synelia) n'apparaît jamais côté client.
    r = await client_org.get("/v1/espaces")
    assert r.status_code == 200
    assert all(e["code"] != "vps-zone" for e in r.json()["donnees"])


async def test_role_actif_est_org_admin(client_org):
    r = await client_org.get("/v1/moi")
    assert r.status_code == 200
    assert r.json()["roleActif"] == "org_admin"
    assert r.json()["utilisateur"]["email"] == "org-admin@test-client.ci"
