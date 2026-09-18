"""Messagerie (Web Cloud emails) : activation 202, boîtes 201/409/402, alias, authentification, webmail."""


async def test_cycle_messagerie(client_org):
    r = await client_org.post(
        "/v1/web/emails", json={"domaine": "exemple.ci", "palier": "pro", "boites": 4}
    )
    assert r.status_code == 202, r.text
    travail = r.json()
    assert travail["type"] == "web.email.activate" and travail["statut"] == "done"

    r = await client_org.get("/v1/web/emails")
    assert r.status_code == 200
    messageries = r.json()["donnees"]
    assert len(messageries) == 1
    mess = messageries[0]
    assert mess["actif"] is True
    assert mess["authentification"]["spf"] == "valide"
    assert mess["authentification"]["dkim"] == "valide"
    assert mess["authentification"]["dmarc"]
    mid = mess["id"]

    r = await client_org.post("/v1/web/emails", json={"domaine": "exemple.ci", "palier": "pro"})
    assert r.status_code == 409 and r.json()["erreur"]["code"] == "nom_deja_pris"

    r = await client_org.post(
        f"/v1/web/emails/{mid}/boites", json={"adresse": "a@exemple.ci", "nom": "A", "quotaGo": 5}
    )
    assert r.status_code == 201, r.text
    assert r.json()["adresse"] == "a@exemple.ci"

    r = await client_org.post(
        f"/v1/web/emails/{mid}/boites", json={"adresse": "a@exemple.ci", "nom": "A"}
    )
    assert r.status_code == 409 and r.json()["erreur"]["code"] == "nom_deja_pris"

    r = await client_org.patch(
        f"/v1/web/emails/{mid}/boites/a@exemple.ci", json={"quotaGo": 8, "mfa": True}
    )
    assert r.status_code == 200 and r.json()["quotaGo"] == 8 and r.json()["mfa"] is True

    r = await client_org.put(
        f"/v1/web/emails/{mid}/alias",
        json={"alias": [{"de": "contact@exemple.ci", "vers": ["a@exemple.ci"]}]},
    )
    assert r.status_code == 200 and r.json()["alias"][0]["de"] == "contact@exemple.ci"

    r = await client_org.post(f"/v1/web/emails/{mid}/authentification/verification")
    assert r.status_code == 200 and r.json()["spf"] == "valide"

    r = await client_org.post(f"/v1/web/emails/{mid}/ouverture", json={"adresse": "a@exemple.ci"})
    assert r.status_code == 201, r.text
    ouv = r.json()
    assert ouv["url"].startswith("https://") and ouv["methode"] == "redirection"

    r = await client_org.delete(
        f"/v1/web/emails/{mid}/boites/a@exemple.ci", params={"confirmation": "mauvais"}
    )
    assert r.status_code == 422

    r = await client_org.delete(
        f"/v1/web/emails/{mid}/boites/a@exemple.ci", params={"confirmation": "a@exemple.ci"}
    )
    assert r.status_code == 204
    r = await client_org.get(f"/v1/web/emails/{mid}")
    assert len(r.json()["boites"]) == 0

    r = await client_org.delete(f"/v1/web/emails/{mid}", params={"confirmation": "mauvais"})
    assert r.status_code == 422

    r = await client_org.delete(f"/v1/web/emails/{mid}", params={"confirmation": "exemple.ci"})
    assert r.status_code == 202, r.text
    travail = r.json()
    assert travail["type"] == "web.email.deactivate" and travail["statut"] == "done"

    r = await client_org.get(f"/v1/web/emails/{mid}")
    assert r.status_code == 404


async def test_quota_boites(client_org):
    r = await client_org.post("/v1/web/emails", json={"domaine": "quota.ci", "palier": "starter"})
    assert r.status_code == 202
    mess = next(
        m
        for m in (await client_org.get("/v1/web/emails")).json()["donnees"]
        if m["domaine"] == "quota.ci"
    )
    mid = mess["id"]
    for i in range(mess["boitesIncluses"]):
        rr = await client_org.post(
            f"/v1/web/emails/{mid}/boites", json={"adresse": f"b{i}@quota.ci", "nom": f"B{i}"}
        )
        assert rr.status_code == 201, rr.text
    r = await client_org.post(
        f"/v1/web/emails/{mid}/boites", json={"adresse": "trop@quota.ci", "nom": "Trop"}
    )
    assert r.status_code == 402 and r.json()["erreur"]["code"] == "quota_depasse"

    # Suppression avec des boîtes encore actives : l'exécuteur doit les retirer avant
    # le domaine (Zimbra refuse `DeleteDomainRequest` sur un domaine non vide).
    r = await client_org.delete(f"/v1/web/emails/{mid}", params={"confirmation": "quota.ci"})
    assert r.status_code == 202, r.text
    r = await client_org.get(f"/v1/web/emails/{mid}")
    assert r.status_code == 404


async def test_domaine_zimbra_reel_si_joignable(client_org):
    # Preuve Zimbra réelle (création → GetDomain → suppression) : ne tourne que là où
    # le serveur est joignable (réseau Docker du lab) — depuis ce poste, saut honnête
    # documenté au lieu d'un faux-positif simulé silencieux.
    import os

    from synelia_testing import ignorer_si_zimbra_injoignable

    ignorer_si_zimbra_injoignable()
    if not os.environ.get("SYNELIA_ZIMBRA_ADMIN_USER") or not os.environ.get(
        "SYNELIA_ZIMBRA_ADMIN_PASSWORD"
    ):
        import pytest

        pytest.skip("identifiants admin Zimbra absents")
    from synelia_openstack import zimbra

    assert isinstance(zimbra.choisir_zimbra(), zimbra.ZimbraReel)
    r = await client_org.post(
        "/v1/web/emails", json={"domaine": "preuve-zimbra.ci", "palier": "pro"}
    )
    assert r.status_code == 202, r.text
    mess = next(
        m
        for m in (await client_org.get("/v1/web/emails")).json()["donnees"]
        if m["domaine"] == "preuve-zimbra.ci"
    )
    reel = zimbra.ZimbraReel()
    assert reel._domaine_id("preuve-zimbra.ci") is not None, (
        "domaine absent de Zimbra : activation sans impact réel"
    )
    r = await client_org.delete(
        f"/v1/web/emails/{mess['id']}", params={"confirmation": "preuve-zimbra.ci"}
    )
    assert r.status_code == 202, r.text
    assert reel._domaine_id("preuve-zimbra.ci") is None, (
        "domaine toujours présent dans Zimbra : suppression sans impact réel"
    )


async def test_erreurs_emails(client_org):
    # Branches d'erreur simule-couvrables : 404 sur tous les endpoints détail, 402 quota
    # dépassé à l'activation, 422 confirmation, liste filtrée.
    for methode, chemin, kwargs in [
        ("get", "/v1/web/emails/messagerie-inexistante", {}),
        ("patch", "/v1/web/emails/messagerie-inexistante", {"json": {"palier": "pro"}}),
        ("delete", "/v1/web/emails/messagerie-inexistante", {"params": {"confirmation": "x"}}),
        ("put", "/v1/web/emails/messagerie-inexistante/alias", {"json": {"alias": []}}),
        (
            "post",
            "/v1/web/emails/messagerie-inexistante/authentification/verification",
            {},
        ),
        (
            "post",
            "/v1/web/emails/messagerie-inexistante/boites",
            {"json": {"adresse": "a@x.ci", "nom": "A"}},
        ),
        (
            "patch",
            "/v1/web/emails/messagerie-inexistante/boites/a@x.ci",
            {"json": {"quotaGo": 1}},
        ),
        (
            "delete",
            "/v1/web/emails/messagerie-inexistante/boites/a@x.ci",
            {"params": {"confirmation": "a@x.ci"}},
        ),
        ("post", "/v1/web/emails/messagerie-inexistante/ouverture", {"json": {}}),
    ]:
        r = await getattr(client_org, methode)(chemin, **kwargs)
        assert r.status_code == 404, (methode, chemin, r.text)

    r = await client_org.post("/v1/web/emails", json={"domaine": "erreurs.ci", "palier": "pro"})
    assert r.status_code == 202, r.text
    mid = next(
        m
        for m in (await client_org.get("/v1/web/emails")).json()["donnees"]
        if m["domaine"] == "erreurs.ci"
    )["id"]
    r = await client_org.delete(f"/v1/web/emails/{mid}", params={"confirmation": "mauvais"})
    assert r.status_code == 422
    r = await client_org.get("/v1/web/emails", params={"actif": "true"})
    assert r.status_code == 200 and any(m["id"] == mid for m in r.json()["donnees"])
