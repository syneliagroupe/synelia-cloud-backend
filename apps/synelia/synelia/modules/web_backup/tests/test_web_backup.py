"""Sauvegarde (Web Cloud) : liste, exécution (ajoute un point), restauration 202/409, test.

Le plan de sauvegarde naît avec l'hébergement (VPS + domaine protégés par défaut) :
un client réel crée donc un hébergement d'abord, puis retrouve son plan — sans quoi
`GET /v1/web/backup` répondait une liste vide à un client pourtant propriétaire d'un
hébergement (constaté en passant la suite en compte client, cf. `client_org`). On teste
donc d'abord le 409 (aucun point), puis l'exécution, puis la restauration."""


async def _creer_hebergement(client_org, domaine: str) -> dict:
    from synelia_testing import enregistrer_domaine

    await enregistrer_domaine(client_org, domaine)
    r = await client_org.post(
        "/v1/web/hebergements", json={"palier": "pro", "site": "ABJ", "domaine": domaine}
    )
    assert r.status_code == 202, r.text
    assert r.json()["statut"] == "done", r.text
    r = await client_org.get("/v1/web/hebergements")
    h = next(x for x in r.json()["donnees"] if x.get("domaine") == domaine)
    return h


async def test_cycle_sauvegarde(client_org):
    h = await _creer_hebergement(client_org, "backup-cycle.com")
    r = await client_org.get("/v1/web/backup", params={"hebergementId": h["id"]})
    assert r.status_code == 200
    sauvegardes = r.json()["donnees"]
    assert len(sauvegardes) == 1, r.text
    sauvegarde = sauvegardes[0]
    sid = sauvegarde["id"]
    assert sauvegarde["hebergementId"] == h["id"]
    assert len(sauvegarde["executions"]) == 0

    r = await client_org.patch(
        f"/v1/web/backup/{sid}", json={"frequence": "hebdomadaire", "retentionJours": 30}
    )
    assert r.status_code == 200 and r.json()["frequence"] == "hebdomadaire"

    r = await client_org.post(
        f"/v1/web/backup/{sid}/restauration", json={"executionId": "x", "granularite": "complete"}
    )
    assert r.status_code == 409 and r.json()["erreur"]["code"] == "aucun_point_disponible"

    r = await client_org.post(f"/v1/web/backup/{sid}/execution")
    assert r.status_code == 202, r.text
    assert r.json()["type"] == "web.backup.run" and r.json()["statut"] == "done"

    sauvegarde = (await client_org.get(f"/v1/web/backup/{sid}")).json()
    assert len(sauvegarde["executions"]) == 1

    r = await client_org.post(f"/v1/web/backup/{sid}/execution")
    assert r.status_code == 202
    sauvegarde = (await client_org.get(f"/v1/web/backup/{sid}")).json()
    assert len(sauvegarde["executions"]) == 2

    r = await client_org.post(f"/v1/web/backup/{sid}/test-restauration")
    assert r.status_code == 202, r.text
    assert r.json()["statut"] == "done"
    sauvegarde = (await client_org.get(f"/v1/web/backup/{sid}")).json()
    assert sauvegarde["dernierTestRestauration"]["resultat"] == "ok"

    r = await client_org.post(
        f"/v1/web/backup/{sid}/restauration", json={"executionId": "x", "granularite": "complete"}
    )
    assert r.status_code == 202, r.text
    assert r.json()["type"] == "web.backup.restore" and r.json()["statut"] == "done"


async def test_erreurs_backup(client_org):
    # Branches d'erreur simule-couvrables : 404 sur les endpoints détail. Le corps PATCH
    # est volontairement valide (`quotidienne`) : avec un corps invalide, la validation
    # Pydantic répondrait 422 avant même le contrôle d'existence — autre branche.
    for methode, chemin, kwargs in [
        ("get", "/v1/web/backup/sauvegarde-inexistante", {}),
        (
            "patch",
            "/v1/web/backup/sauvegarde-inexistante",
            {"json": {"frequence": "quotidienne"}},
        ),
        ("post", "/v1/web/backup/sauvegarde-inexistante/execution", {}),
        (
            "post",
            "/v1/web/backup/sauvegarde-inexistante/restauration",
            {"json": {"executionId": "x", "granularite": "complete"}},
        ),
        ("post", "/v1/web/backup/sauvegarde-inexistante/test-restauration", {}),
    ]:
        r = await getattr(client_org, methode)(chemin, **kwargs)
        assert r.status_code == 404, (methode, chemin, r.text)
