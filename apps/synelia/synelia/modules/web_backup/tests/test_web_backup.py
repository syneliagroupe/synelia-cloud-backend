"""Sauvegarde (Web Cloud) : liste, exécution (ajoute un point), restauration 202/409, test.


Le dépôt de démo fournit une sauvegarde sans point : on teste donc d'abord le 409,
puis l'exécution qui ajoute un point, puis la restauration."""


async def test_cycle_sauvegarde(client):
    r = await client.get("/v1/web/backup")
    assert r.status_code == 200
    sauvegardes = r.json()["donnees"]
    assert len(sauvegardes) == 1
    sauvegarde = sauvegardes[0]
    sid = sauvegarde["id"]
    assert len(sauvegarde["executions"]) == 0

    r = await client.patch(
        f"/v1/web/backup/{sid}", json={"frequence": "hebdomadaire", "retentionJours": 30}
    )
    assert r.status_code == 200 and r.json()["frequence"] == "hebdomadaire"

    r = await client.post(
        f"/v1/web/backup/{sid}/restauration", json={"executionId": "x", "granularite": "complete"}
    )
    assert r.status_code == 409 and r.json()["erreur"]["code"] == "aucun_point_disponible"

    r = await client.post(f"/v1/web/backup/{sid}/execution")
    assert r.status_code == 202, r.text
    assert r.json()["type"] == "web.backup.run" and r.json()["statut"] == "done"

    sauvegarde = (await client.get(f"/v1/web/backup/{sid}")).json()
    assert len(sauvegarde["executions"]) == 1

    r = await client.post(f"/v1/web/backup/{sid}/execution")
    assert r.status_code == 202
    sauvegarde = (await client.get(f"/v1/web/backup/{sid}")).json()
    assert len(sauvegarde["executions"]) == 2

    r = await client.post(f"/v1/web/backup/{sid}/test-restauration")
    assert r.status_code == 202, r.text
    assert r.json()["statut"] == "done"
    sauvegarde = (await client.get(f"/v1/web/backup/{sid}")).json()
    assert sauvegarde["dernierTestRestauration"]["resultat"] == "ok"

    r = await client.post(
        f"/v1/web/backup/{sid}/restauration", json={"executionId": "x", "granularite": "complete"}
    )
    assert r.status_code == 202, r.text
    assert r.json()["type"] == "web.backup.restore" and r.json()["statut"] == "done"


async def test_erreurs_backup(client):
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
        r = await getattr(client, methode)(chemin, **kwargs)
        assert r.status_code == 404, (methode, chemin, r.text)
