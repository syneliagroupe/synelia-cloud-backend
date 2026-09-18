"""Jetons plateforme (super admin) : `/admin/jetons`.

Un jeton plateforme (`CleApi.org_id` NULL) doit réellement authentifier une requête distante via
`X-Api-Key`, donner les droits de l'équipe Synelia bornés par sa portée, et rester invisible aux
organisations clientes.
"""


async def test_cycle_jeton_admin(client):
    r = await client.post(
        "/v1/admin/jetons", json={"nom": "ci-plateforme", "portee": ["sso.configure"]}
    )
    assert r.status_code == 201, r.text
    secret = r.json()["secret"]
    assert secret.startswith(r.json()["cle"]["prefixe"])
    jid = r.json()["cle"]["id"]

    r = await client.get("/v1/admin/jetons")
    assert r.status_code == 200, r.text
    assert any(j["id"] == jid for j in r.json()["donnees"])

    r = await client.get(f"/v1/admin/jetons/{jid}")
    assert r.status_code == 200 and r.json()["statut"] == "active"

    r = await client.patch(
        f"/v1/admin/jetons/{jid}",
        json={"nom": "ci-plateforme-v2", "portee": ["sso.configure", "audit.view"]},
    )
    assert r.status_code == 200 and r.json()["nom"] == "ci-plateforme-v2"

    r = await client.post(f"/v1/admin/jetons/{jid}/rotation", json={})
    assert r.status_code == 200
    assert r.json()["secret"] != secret

    r = await client.delete(f"/v1/admin/jetons/{jid}", params={"confirmation": "mauvais"})
    assert r.status_code == 422 and r.json()["erreur"]["code"] == "confirmation_invalide"

    r = await client.delete(f"/v1/admin/jetons/{jid}", params={"confirmation": "ci-plateforme-v2"})
    assert r.status_code == 204

    r = await client.get(f"/v1/admin/jetons/{jid}")
    assert r.status_code == 200 and r.json()["statut"] == "revoquee"


async def test_jeton_admin_portee_invalide(client):
    r = await client.post("/v1/admin/jetons", json={"nom": "bad", "portee": ["audit.nonexistant"]})
    assert r.status_code == 422 and r.json()["erreur"]["code"] == "validation"


async def test_jeton_admin_authentifie_une_requete_plateforme(client):
    """Le jeton plateforme agit comme un admin plateforme, mais seulement dans sa portée."""
    r = await client.post("/v1/admin/jetons", json={"nom": "script", "portee": ["sso.configure"]})
    assert r.status_code == 201, r.text
    secret = r.json()["secret"]

    r = await client.get("/v1/admin/jetons", headers={"Authorization": "", "X-Api-Key": secret})
    assert r.status_code == 200, r.text

    r = await client.get("/v1/admin/audit", headers={"Authorization": "", "X-Api-Key": secret})
    assert r.status_code == 403
    assert r.json()["erreur"]["code"] == "interdit"


async def test_jeton_admin_accede_une_organisation(client):
    """Avec `X-Organisation-Id`, un jeton plateforme peut agir sur une organisation."""
    r = await client.post("/v1/admin/jetons", json={"nom": "support", "portee": ["sso.configure"]})
    assert r.status_code == 201, r.text
    secret = r.json()["secret"]

    r = await client.get(
        "/v1/securite/cles-api",
        headers={
            "Authorization": "",
            "X-Api-Key": secret,
            "X-Organisation-Id": client.org_id,
        },
    )
    assert r.status_code == 200, r.text


async def test_jetons_organisation_absents_de_la_liste_admin(client):
    r = await client.post(
        "/v1/securite/cles-api", json={"nom": "org-key", "portee": ["sso.configure"]}
    )
    assert r.status_code == 201, r.text
    org_id = r.json()["cle"]["id"]

    r = await client.get("/v1/admin/jetons")
    assert r.status_code == 200
    assert all(j["id"] != org_id for j in r.json()["donnees"])


async def test_jeton_admin_refuse_a_une_organisation(client_org):
    r = await client_org.get("/v1/admin/jetons")
    assert r.status_code == 403
    assert r.json()["erreur"]["code"] == "interdit"
