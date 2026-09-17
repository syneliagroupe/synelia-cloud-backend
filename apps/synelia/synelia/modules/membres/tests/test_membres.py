"""Membres & invitations : ajout, retrait (avec confirmation), relance, révocation.

Le client est l'admin plateforme (super_admin) : on crée une organisation puis on y
travaille via `X-Organisation-Id` pour sceller les opérations sur les membres de celle-ci."""

ORG_NOM = "Membre Org"


async def _creer_org(client) -> tuple[str, str]:
    r = await client.post(
        "/v1/organisations",
        json={
            "nom": ORG_NOM,
            "pays": "CI",
            "administrateur": {"nom": "Kouassi Y.", "email": "kouassi@membre.ci"},
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["id"], "kouassi@membre.ci"


def _headers(org_id: str) -> dict[str, str]:
    return {"X-Organisation-Id": org_id}


async def test_cycle_membre(client):
    oid, email = await _creer_org(client)

    r = await client.get("/v1/membres", headers=_headers(oid))
    assert r.status_code == 200
    membres = r.json()["donnees"]
    assert any(mm["role"] == "org_admin" for mm in membres)
    mem = membres[0]
    memId = mem["id"]

    r = await client.get(f"/v1/membres/{memId}", headers=_headers(oid))
    assert r.status_code == 200 and r.json()["userId"] == mem["userId"]

    # Un second admin est nécessaire avant de pouvoir rétrograder le premier — sinon
    # `dernier_admin` bloque (voir test_patch_dernier_admin_bloque).
    r = await client.post(
        "/v1/auth/inscription",
        json={
            "email": "co-admin@membre.ci",
            "nom": "Co Admin",
            "motDePasse": "CoAdmin!2026",
            "accepteConditions": True,
        },
    )
    assert r.status_code == 201, r.text
    co_admin_id = r.json()["utilisateur"]["id"]
    r = await client.post(
        "/v1/membres",
        json={"userId": co_admin_id, "role": "org_admin", "scopeType": "org"},
        headers=_headers(oid),
    )
    assert r.status_code == 201, r.text

    r = await client.patch(
        f"/v1/membres/{memId}", json={"role": "read_only"}, headers=_headers(oid)
    )
    assert r.status_code == 200 and r.json()["role"] == "read_only"

    r = await client.delete(
        f"/v1/membres/{memId}", params={"confirmation": "mauvais"}, headers=_headers(oid)
    )
    assert r.status_code == 422 and r.json()["erreur"]["code"] == "confirmation_invalide"

    r = await client.delete(
        f"/v1/membres/{memId}", params={"confirmation": email}, headers=_headers(oid)
    )
    assert r.status_code == 204


async def test_patch_dernier_admin_bloque(client):
    """Rétrograder le seul `org_admin` d'une organisation doit être refusé — même garde que
    le retrait (`DELETE`), appliquée au changement de rôle (`PATCH`)."""
    oid, _ = await _creer_org(client)

    r = await client.get("/v1/membres", headers=_headers(oid))
    mem = r.json()["donnees"][0]
    assert mem["role"] == "org_admin"

    r = await client.patch(
        f"/v1/membres/{mem['id']}", json={"role": "read_only"}, headers=_headers(oid)
    )
    assert r.status_code == 409, r.text
    assert r.json()["erreur"]["code"] == "dernier_admin"

    r = await client.get(f"/v1/membres/{mem['id']}", headers=_headers(oid))
    assert r.status_code == 200 and r.json()["role"] == "org_admin"


async def test_role_change_effet_immediat_sans_relogin(client):
    """Un changement de rôle doit s'appliquer dès la requête suivante du membre déjà
    connecté — pas seulement à l'expiration/rafraîchissement de son jeton déjà émis
    (régression du bug : `claims.get("role")` figé à la connexion primait sur la base)."""
    r = await client.post(
        "/v1/auth/inscription",
        json={
            "email": "cible@membre.ci",
            "nom": "Cible",
            "motDePasse": "Cible!2026",
            "accepteConditions": True,
            "organisation": {"nom": "Org Cible", "pays": "CI"},
        },
    )
    assert r.status_code == 201, r.text
    session = r.json()
    membre_jeton = session["accessToken"]
    oid = session["organisationActive"]
    membre_headers = {"Authorization": f"Bearer {membre_jeton}"}

    # Encore `org_admin` selon son propre jeton : l'action réservée aux admins passe.
    r = await client.get("/v1/membres", headers=membre_headers)
    assert r.status_code == 200, r.text
    memId = next(mm["id"] for mm in r.json()["donnees"] if mm["role"] == "org_admin")

    # Un second admin, sinon `dernier_admin` bloque la rétrogradation.
    r = await client.post(
        "/v1/auth/inscription",
        json={
            "email": "co-admin-cible@membre.ci",
            "nom": "Co Admin Cible",
            "motDePasse": "CoAdmin!2026",
            "accepteConditions": True,
        },
    )
    assert r.status_code == 201, r.text
    co_admin_id = r.json()["utilisateur"]["id"]
    r = await client.post(
        "/v1/membres",
        json={"userId": co_admin_id, "role": "org_admin", "scopeType": "org"},
        headers=_headers(oid),
    )
    assert r.status_code == 201, r.text

    # L'équipe plateforme rétrograde le membre — via la base, pas via son jeton.
    r = await client.patch(f"/v1/membres/{memId}", json={"role": "read_only"}, headers=_headers(oid))
    assert r.status_code == 200 and r.json()["role"] == "read_only"

    # Même jeton qu'avant (jamais raffraîchi/relogué) : l'action réservée aux admins doit
    # désormais être refusée, immédiatement — pas seulement dans 15 minutes.
    r = await client.get("/v1/membres", headers=membre_headers)
    assert r.status_code == 403, r.text
    assert r.json()["erreur"]["code"] == "interdit"


async def test_cycle_invitation(client):
    oid, _ = await _creer_org(client)

    r = await client.post(
        "/v1/invitations",
        json={"email": "nov@invite.ci", "role": "operator", "scopeType": "org"},
        headers=_headers(oid),
    )
    assert r.status_code == 201, r.text
    inv = r.json()
    assert inv["statut"] == "en_attente" and inv["email"] == "nov@invite.ci"
    iid = inv["id"]

    r = await client.post(
        "/v1/invitations",
        json={"email": "nov@invite.ci", "role": "operator", "scopeType": "org"},
        headers=_headers(oid),
    )
    assert r.status_code == 409 and r.json()["erreur"]["code"] == "invitation_existante"

    r = await client.get("/v1/invitations", headers=_headers(oid))
    assert r.status_code == 200 and any(i["id"] == iid for i in r.json()["donnees"])

    r = await client.post(f"/v1/invitations/{iid}/relance", headers=_headers(oid))
    assert r.status_code == 200 and r.json()["statut"] == "en_attente"

    r = await client.delete(f"/v1/invitations/{iid}", headers=_headers(oid))
    assert r.status_code == 204
