"""Tests de `/moi/*` : profil, MFA, organisation active."""


async def test_moi_expose_les_permissions_equipe(client):
    r = await client.get("/v1/moi")
    assert r.status_code == 200, r.text
    corps = r.json()
    assert corps["roleActif"] == "super_admin"
    assert "catalog.edit" in corps["permissions"]


async def test_cycle_mfa(client):
    r = await client.post("/v1/moi/mfa", json={"methode": "totp"})
    assert r.status_code == 201, r.text
    assert (await client.get("/v1/moi")).json()["utilisateur"]["mfaEnabled"] is True

    r = await client.delete("/v1/moi/mfa")
    assert r.status_code == 204, r.text
    assert (await client.get("/v1/moi")).json()["utilisateur"]["mfaEnabled"] is False

    # Déjà désactivé : un second appel est un conflit, pas un succès silencieux.
    r = await client.delete("/v1/moi/mfa")
    assert r.status_code == 409


async def test_organisation_active_inexistante_rejetee(client):
    """Un identifiant d'organisation inexistant doit être rejeté avant d'atteindre la RLS
    Postgres de `sessions_auth` (qui répondait par un 500 brut, constaté en direct sur
    dev01) — même pour l'équipe Synelia, non bornée à `roles_par_org`."""
    r = await client.put(
        "/v1/moi/organisation-active", json={"orgId": "org-inconnue", "memoriser": False}
    )
    assert r.status_code == 422, r.text

    org = (await client.get("/v1/moi")).json()["organisationActive"]
    r = await client.put("/v1/moi/organisation-active", json={"orgId": org, "memoriser": False})
    assert r.status_code == 200, r.text


CLE = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl"


async def test_cles_ssh_du_compte(client):
    assert (await client.get("/v1/moi/cles-ssh")).json() == []
    r = await client.post(
        "/v1/moi/cles-ssh", json={"nom": "portable", "publique": CLE + " jean@pc"}
    )
    assert r.status_code == 201, r.text
    cle = r.json()
    assert cle["empreinte"].startswith("SHA256:") and cle["publique"] == CLE
    assert (
        await client.post("/v1/moi/cles-ssh", json={"nom": "bis", "publique": CLE})
    ).status_code == 409
    assert (
        await client.post("/v1/moi/cles-ssh", json={"nom": "x", "publique": "pas une clé"})
    ).status_code == 422
    assert [c["id"] for c in (await client.get("/v1/moi/cles-ssh")).json()] == [cle["id"]]
    assert (await client.delete(f"/v1/moi/cles-ssh/{cle['id']}")).status_code == 204
    assert (await client.delete(f"/v1/moi/cles-ssh/{cle['id']}")).status_code == 404
