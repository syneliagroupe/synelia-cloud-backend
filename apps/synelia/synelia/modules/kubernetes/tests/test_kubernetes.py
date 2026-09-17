"""Module /kubernetes : cycle de vie d'un cluster (création, kubeconfig, mise à jour, pools, modules, versions)."""


async def _espace_demo(client) -> str:
    r = await client.get("/v1/espaces")
    assert r.status_code == 200
    demo = next(e for e in r.json()["donnees"] if e["code"] == "demo-abj")
    return demo["id"]


def _corps_cluster(espace_id: str, nom: str = "k8s-test") -> dict:
    return {
        "espaceId": espace_id,
        "nom": nom,
        "version": "1.32.2",
        "site": "ABJ",
        "controlPlane": {"mode": "single"},
        "pools": [{"nom": "workers", "nodes": 2, "flavor": "k8s.worker", "type": "standard"}],
        "modules": ["ingress-nginx", "cert-manager"],
    }


async def _creer_cluster(client, espace_id: str, nom: str = "k8s-test") -> str:
    r = await client.post("/v1/kubernetes", json=_corps_cluster(espace_id, nom))
    assert r.status_code == 202, r.text
    assert r.json()["statut"] == "done"
    r2 = await client.get("/v1/kubernetes")
    clusters = r2.json()["donnees"]
    cluster = next(c for c in clusters if c["nom"] == nom)
    assert cluster["statut"] == "running"
    return cluster["id"]


async def test_versions_et_modules(client):
    r = await client.get("/v1/kubernetes/versions")
    assert r.status_code == 200
    versions = r.json()
    assert ["1.31.4", "1.32.2", "1.33.0"] == [v["version"] for v in versions]
    assert any(v["statut"] == "recommandee" for v in versions)

    r = await client.get("/v1/kubernetes/modules")
    assert r.status_code == 200
    slugs = {item["slug"] for item in r.json()}
    assert {"cni", "ingress-nginx", "monitoring", "cert-manager", "autoscaler"} <= slugs


async def test_cycle_cluster(client):
    espace_id = await _espace_demo(client)
    cid = await _creer_cluster(client, espace_id, "k8s-prod")

    r = await client.get(f"/v1/kubernetes/{cid}")
    assert r.status_code == 200
    cluster = r.json()
    assert cluster["espaceId"] == espace_id
    assert cluster["version"] == "1.32.2"
    assert cluster["controlPlane"]["mode"] == "single"
    assert cluster["pools"][0]["nom"] == "workers"

    r = await client.get(f"/v1/kubernetes/{cid}/kubeconfig")
    assert r.status_code == 200
    kube = r.json()
    assert kube["utilisateur"] == "admin"
    assert f"https://{cid}.k8s.synelia.cloud:6443" in kube["contenu"]

    r = await client.post(f"/v1/kubernetes/{cid}/mise-a-jour", json={"version": "1.33.0"})
    assert r.status_code == 202 and r.json()["statut"] == "done"
    r = await client.get(f"/v1/kubernetes/{cid}")
    assert r.json()["version"] == "1.33.0"


async def test_creer_cluster_nom_deja_pris(client):
    espace_id = await _espace_demo(client)
    nom = "k8s-dup"
    await _creer_cluster(client, espace_id, nom)
    r = await client.post("/v1/kubernetes", json=_corps_cluster(espace_id, nom))
    assert r.status_code == 409 and r.json()["erreur"]["code"] == "nom_deja_pris"


async def test_pools_cycle(client):
    espace_id = await _espace_demo(client)
    cid = await _creer_cluster(client, espace_id, "k8s-pools")

    corps = {"nom": "gpu", "nodes": 1, "flavor": "large", "type": "gpu"}
    r = await client.post(f"/v1/kubernetes/{cid}/pools", json=corps)
    assert r.status_code == 202 and r.json()["statut"] == "done"
    r = await client.get(f"/v1/kubernetes/{cid}/pools")
    noms = [p["nom"] for p in r.json()]
    assert "gpu" in noms

    r = await client.patch(f"/v1/kubernetes/{cid}/pools/gpu", json={**corps, "nodes": 3})
    assert r.status_code == 202 and r.json()["statut"] == "done"
    r = await client.get(f"/v1/kubernetes/{cid}/pools")
    gpu = next(p for p in r.json() if p["nom"] == "gpu")
    assert gpu["nodes"] == 3

    r = await client.delete(f"/v1/kubernetes/{cid}/pools/gpu", params={"confirmation": "mauvais"})
    assert r.status_code == 422 and r.json()["erreur"]["code"] == "confirmation_invalide"
    r = await client.delete(f"/v1/kubernetes/{cid}/pools/gpu", params={"confirmation": "gpu"})
    assert r.status_code == 202 and r.json()["statut"] == "done"
    r = await client.get(f"/v1/kubernetes/{cid}/pools")
    assert all(p["nom"] != "gpu" for p in r.json())


async def test_modules_cluster(client):
    espace_id = await _espace_demo(client)
    cid = await _creer_cluster(client, espace_id, "k8s-modules")
    r = await client.put(
        f"/v1/kubernetes/{cid}/modules", json={"modules": ["monitoring", "autoscaler"]}
    )
    assert r.status_code == 202 and r.json()["statut"] == "done"
    r = await client.get(f"/v1/kubernetes/{cid}")
    assert set(r.json()["modules"]) == {"monitoring", "autoscaler"}


async def test_supprimer_cluster_confirmation(client):
    espace_id = await _espace_demo(client)
    cid = await _creer_cluster(client, espace_id, "k8s-a-supprimer")
    r = await client.delete(f"/v1/kubernetes/{cid}", params={"confirmation": "mauvais"})
    assert r.status_code == 422
    r = await client.delete(f"/v1/kubernetes/{cid}", params={"confirmation": "k8s-a-supprimer"})
    assert r.status_code == 202 and r.json()["statut"] == "done"
    r = await client.get("/v1/kubernetes")
    assert all(c["nom"] != "k8s-a-supprimer" for c in r.json()["donnees"])


def test_mapper_statut_magnum():
    from synelia.modules.kubernetes.service import _mapper_statut_magnum

    assert _mapper_statut_magnum("CREATE_IN_PROGRESS") == "provisioning"
    assert _mapper_statut_magnum("CREATE_COMPLETE") == "running"
    assert _mapper_statut_magnum("CREATE_FAILED") == "degraded"
    assert _mapper_statut_magnum("UPDATE_IN_PROGRESS") == "updating"
    assert _mapper_statut_magnum("UPDATE_COMPLETE") == "running"
    assert _mapper_statut_magnum("UPDATE_FAILED") == "degraded"
    # `DELETE_COMPLETE` = Magnum ne connaît plus du tout le cluster (jamais créé pour de vrai,
    # ou supprimé hors bande) : `degraded` est l'état sincère, pas `None` (qui laissait la
    # ressource figée `provisioning` indéfiniment — bug réel trouvé sur `paas-shared-cluster2`).
    assert _mapper_statut_magnum("DELETE_COMPLETE") == "degraded"


async def test_reconciliation_statut_cluster(client, monkeypatch):
    # `POST /kubernetes` marque son travail `done` en ~2 s sans attendre l'aboutissement réel
    # côté Magnum (correct : un provisioning réel prend plusieurs minutes) — mais rien ne
    # rafraîchissait plus jamais l'état depuis l'amont ensuite : `GET /kubernetes/{id}` restait
    # figé sur `provisioning` indéfiniment. On force ici un statut amont non terminal à la
    # création (comme un vrai `CREATE_IN_PROGRESS` juste après soumission à Magnum), puis on
    # vérifie qu'une lecture ultérieure, une fois l'amont « terminé », rafraîchit bien la DB.
    from synelia.modules.kubernetes import service as k8s_service

    original_creer = k8s_service.MagnumSimule.creer_cluster

    def _creer_cluster_en_cours(self, **kw):
        r = original_creer(self, **kw)
        r["statut"] = "CREATE_IN_PROGRESS"
        return r

    monkeypatch.setattr(k8s_service.MagnumSimule, "creer_cluster", _creer_cluster_en_cours)
    # Sans ce patch, le stub par défaut (`cluster_statut` → `CREATE_COMPLETE`) ferait basculer
    # le cluster dès la première lecture qui suit la création — on veut d'abord observer qu'il
    # reste `provisioning` tant que l'amont l'est réellement.
    monkeypatch.setattr(
        k8s_service.MagnumSimule, "cluster_statut", lambda self, cluster_id: "CREATE_IN_PROGRESS"
    )

    espace_id = await _espace_demo(client)
    r = await client.post("/v1/kubernetes", json=_corps_cluster(espace_id, "k8s-reconcile"))
    assert r.status_code == 202, r.text

    r = await client.get("/v1/kubernetes")
    cluster = next(c for c in r.json()["donnees"] if c["nom"] == "k8s-reconcile")
    assert cluster["statut"] == "provisioning"
    cid = cluster["id"]

    # Toujours en cours côté amont : une relecture ne doit rien changer.
    monkeypatch.setattr(
        k8s_service.MagnumSimule, "cluster_statut", lambda self, cluster_id: "CREATE_IN_PROGRESS"
    )
    r = await client.get(f"/v1/kubernetes/{cid}")
    assert r.status_code == 200 and r.json()["statut"] == "provisioning"

    # L'amont a maintenant terminé : la lecture doit rafraîchir le statut en base.
    monkeypatch.setattr(
        k8s_service.MagnumSimule, "cluster_statut", lambda self, cluster_id: "CREATE_COMPLETE"
    )
    r = await client.get(f"/v1/kubernetes/{cid}")
    assert r.status_code == 200 and r.json()["statut"] == "running"

    # La ressource a bien été persistée à jour, pas seulement renvoyée une fois.
    r = await client.get("/v1/kubernetes", params={"statut": "running"})
    assert any(c["id"] == cid for c in r.json()["donnees"])


async def test_reconciliation_cluster_jamais_abouti_cote_amont(client, monkeypatch):
    # Bug réel trouvé en direct (démo 2026-09-08) : `paas-shared-cluster2` était resté
    # `provisioning` 3 jours, deux `GET` consécutifs renvoyant le même statut figé en ~60 ms —
    # son `magnum_cluster_id` (secret) ne correspondait à aucun cluster Magnum réel (create
    # jamais abouti, avant le fix CAPI du 2026-09-07). `MagnumOpenStack.cluster_statut()` rend
    # alors `DELETE_COMPLETE` (Magnum ne connaît plus la ressource) : avant ce correctif,
    # `_mapper_statut_magnum` ne traduisait pas cette valeur (`None`), donc `reconcilier_statut`
    # ne touchait jamais la ligne malgré l'appel Magnum réel à chaque lecture.
    from synelia.modules.kubernetes import service as k8s_service

    original_creer = k8s_service.MagnumSimule.creer_cluster

    def _creer_cluster_en_cours(self, **kw):
        r = original_creer(self, **kw)
        r["statut"] = "CREATE_IN_PROGRESS"
        return r

    # Comme `test_reconciliation_statut_cluster` : sans ce patch de `creer_cluster`, le stub
    # par défaut renvoie `CREATE_COMPLETE` à la création, et `ExecuteurK8sCreate.terminer` (qui
    # se fie au statut retourné par la soumission, pas encore à `cluster_statut`) marquerait la
    # ressource `running` immédiatement — on ne pourrait alors jamais observer le `provisioning`
    # figé que ce test reproduit.
    monkeypatch.setattr(k8s_service.MagnumSimule, "creer_cluster", _creer_cluster_en_cours)
    monkeypatch.setattr(
        k8s_service.MagnumSimule, "cluster_statut", lambda self, cluster_id: "CREATE_IN_PROGRESS"
    )
    espace_id = await _espace_demo(client)
    r = await client.post("/v1/kubernetes", json=_corps_cluster(espace_id, "k8s-jamais-abouti"))
    assert r.status_code == 202, r.text
    r = await client.get("/v1/kubernetes")
    cluster = next(c for c in r.json()["donnees"] if c["nom"] == "k8s-jamais-abouti")
    assert cluster["statut"] == "provisioning"
    cid = cluster["id"]

    # L'amont ne connaît plus (ou n'a jamais connu) ce cluster : reconciliation attendue vers
    # un statut sincère (`degraded`), pas un `provisioning` figé indéfiniment.
    monkeypatch.setattr(
        k8s_service.MagnumSimule, "cluster_statut", lambda self, cluster_id: "DELETE_COMPLETE"
    )
    r = await client.get(f"/v1/kubernetes/{cid}")
    assert r.status_code == 200 and r.json()["statut"] == "degraded"

    # Persisté, pas seulement renvoyé une fois.
    r = await client.get("/v1/kubernetes", params={"statut": "degraded"})
    assert any(c["id"] == cid for c in r.json()["donnees"])


async def test_lister_filtres(client):
    espace_id = await _espace_demo(client)
    await _creer_cluster(client, espace_id, "k8s-filtre")
    r = await client.get("/v1/kubernetes", params={"espaceId": espace_id, "statut": "running"})
    assert r.status_code == 200
    assert any(c["nom"] == "k8s-filtre" for c in r.json()["donnees"])


async def test_pools_depot_unique(client):
    """Régression : tous les pools (créés avec le cluster ou après) vivent dans depot_pool.

    Bug fixé : pools initiaux vivaient uniquement dans ClusterK8s.pools, pas accessibles via
    PATCH/DELETE. Pools ajoutés après vivaient dans depot_pool mais ne survivaient pas à un
    rechargement. Désormais tous passent par depot_pool (source de vérité unique)."""
    espace_id = await _espace_demo(client)
    cid = await _creer_cluster(client, espace_id, "k8s-pool-depot")

    # 1. Pools initiaux doivent être visibles et modifiables
    r = await client.get(f"/v1/kubernetes/{cid}/pools")
    assert r.status_code == 200
    pools_initiales = r.json()
    assert len(pools_initiales) == 1
    assert pools_initiales[0]["nom"] == "workers"

    # PATCH un pool créé avec le cluster : avant le fix, cela retournait 404 parce que
    # _pool() ne cherchait que dans depot_pool
    r = await client.patch(
        f"/v1/kubernetes/{cid}/pools/workers",
        json={**pools_initiales[0], "nodes": 5},
    )
    assert r.status_code == 202 and r.json()["statut"] == "done"
    r = await client.get(f"/v1/kubernetes/{cid}/pools")
    workers = next(p for p in r.json() if p["nom"] == "workers")
    assert workers["nodes"] == 5

    # 2. Pool ajouté après : doit survivre un rechargement
    corps_gpu = {"nom": "gpu", "nodes": 1, "flavor": "large", "type": "gpu"}
    r = await client.post(f"/v1/kubernetes/{cid}/pools", json=corps_gpu)
    assert r.status_code == 202 and r.json()["statut"] == "done"

    r = await client.get(f"/v1/kubernetes/{cid}/pools")
    pools_apres_ajout = r.json()
    assert len(pools_apres_ajout) == 2
    assert any(p["nom"] == "gpu" for p in pools_apres_ajout)

    # Recharger le cluster entier : le pool "gpu" doit toujours être là (ne pas disparaître)
    r = await client.get(f"/v1/kubernetes/{cid}")
    cluster = r.json()
    assert len(cluster["pools"]) == 2
    assert any(p["nom"] == "gpu" for p in cluster["pools"])

    # 3. DELETE un pool créé initialement : avant le fix, peut-être n'aurait-il pas marché
    r = await client.delete(
        f"/v1/kubernetes/{cid}/pools/workers", params={"confirmation": "workers"}
    )
    assert r.status_code == 202 and r.json()["statut"] == "done"
    r = await client.get(f"/v1/kubernetes/{cid}/pools")
    assert len(r.json()) == 1
    assert all(p["nom"] != "workers" for p in r.json())
