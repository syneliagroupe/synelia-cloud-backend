"""Modules /vms : cycle de vie d'une machine virtuelle, services amont, instantanés, lot."""


async def _espace_demo(client) -> str:
    r = await client.get("/v1/espaces")
    assert r.status_code == 200
    espaces = r.json()["donnees"]
    demo = next(e for e in espaces if e["code"] == "demo-abj")
    return demo["id"]


async def _gabarit_id(client, nom: str = "small") -> str:
    r = await client.get("/v1/catalogue/gabarits")
    assert r.status_code == 200
    gabarits = r.json()
    cible = nom.lower()
    for g in gabarits:
        if g["id"].lower() in {cible, f"g1.{cible}"} or g["nom"].lower() == cible:
            return g["id"]
    for g in gabarits:
        if cible in g["nom"].lower() or cible in g["id"].lower():
            return g["id"]
    raise AssertionError(f"gabarit {nom!r} introuvable: {[(g['id'], g['nom']) for g in gabarits]}")


async def _image_id(client) -> str:
    r = await client.get("/v1/catalogue/images")
    assert r.status_code == 200
    images = r.json()
    assert images, "No images available in catalogue"
    return images[0]["id"]


async def _creer_vm(client, espace_id: str, nom: str = "vm-test") -> str:
    gabarit = await _gabarit_id(client)
    image_id = await _image_id(client)
    corps = {"espaceId": espace_id, "nom": nom, "imageId": image_id, "gabarit": gabarit}
    r = await client.post("/v1/vms", json=corps)
    assert r.status_code == 202, r.text
    assert r.json()["statut"] == "done"
    r2 = await client.get("/v1/vms")
    vms = r2.json()["donnees"]
    vm = next(v for v in vms if v["nom"] == nom)
    assert vm["statut"] == "running"
    return vm["id"]


async def test_creer_et_lister_vm(client):
    espace_id = await _espace_demo(client)
    vid = await _creer_vm(client, espace_id, "web-nouveau")
    r = await client.get(f"/v1/vms/{vid}")
    assert r.status_code == 200
    vm = r.json()
    assert vm["statut"] == "running"
    assert vm["espaceId"] == espace_id
    assert vm["vcpu"] == 1 and vm["ramGo"] == 2
    assert any(i["type"] == "privee" for i in vm["ips"])


async def test_creer_vm_explicite_et_image_inconnue(client):
    espace_id = await _espace_demo(client)
    image_id = await _image_id(client)
    r = await client.post(
        "/v1/vms",
        json={
            "espaceId": espace_id,
            "nom": "vm-specs",
            "imageId": image_id,
            "vcpu": 1,
            "ramGo": 2,
            "diskGo": 20,
        },
    )
    assert r.status_code == 202, r.text
    r = await client.post(
        "/v1/vms",
        json={
            "espaceId": espace_id,
            "nom": "vm-bad-img",
            "imageId": "inexistante",
            "vcpu": 1,
            "ramGo": 2,
            "diskGo": 20,
        },
    )
    assert r.status_code == 422 and r.json()["erreur"]["code"] == "validation"
    r = await client.post(
        "/v1/vms", json={"espaceId": espace_id, "nom": "vm-rien", "imageId": "debian-12"}
    )
    assert r.status_code == 422


async def test_creer_vm_nom_deja_pris(client):
    espace_id = await _espace_demo(client)
    await _creer_vm(client, espace_id, "dup")
    gabarit = await _gabarit_id(client)
    image_id = await _image_id(client)
    r = await client.post(
        "/v1/vms",
        json={
            "espaceId": espace_id,
            "nom": "dup",
            "imageId": image_id,
            "gabarit": gabarit,
        },
    )
    assert r.status_code == 409 and r.json()["erreur"]["code"] == "nom_deja_pris"


async def test_modifier_vm(client):
    espace_id = await _espace_demo(client)
    vid = await _creer_vm(client, espace_id, "patch-me")
    r = await client.patch(f"/v1/vms/{vid}", json={"nom": "patch-me", "tags": ["web", "prod"]})
    assert r.status_code == 200
    assert set(r.json()["tags"]) == {"web", "prod"}
    await _creer_vm(client, espace_id, "autre")
    r = await client.patch(f"/v1/vms/{vid}", json={"nom": "autre"})
    assert r.status_code == 409 and r.json()["erreur"]["code"] == "nom_deja_pris"


async def test_supprimer_vm_confirmation(client):
    espace_id = await _espace_demo(client)
    vid = await _creer_vm(client, espace_id, "a-supprimer")
    r = await client.delete(f"/v1/vms/{vid}", params={"confirmation": "mauvais"})
    assert r.status_code == 422 and r.json()["erreur"]["code"] == "confirmation_invalide"
    r = await client.delete(f"/v1/vms/{vid}", params={"confirmation": "a-supprimer"})
    assert r.status_code == 202 and r.json()["statut"] == "done"
    r = await client.get("/v1/vms")
    assert all(v["nom"] != "a-supprimer" for v in r.json()["donnees"])


async def test_arret_demarrage_redemarrage(client):
    espace_id = await _espace_demo(client)
    vid = await _creer_vm(client, espace_id, "power")
    r = await client.post(f"/v1/vms/{vid}/arret", json={})
    assert r.status_code == 202 and r.json()["statut"] == "done"
    r = await client.get(f"/v1/vms/{vid}")
    assert r.json()["statut"] == "stopped"
    r = await client.post(f"/v1/vms/{vid}/arret", json={})
    assert r.status_code == 409 and r.json()["erreur"]["code"] == "etat_deja_atteint"
    r = await client.post(f"/v1/vms/{vid}/demarrage", json={})
    assert r.status_code == 202
    r = await client.get(f"/v1/vms/{vid}")
    assert r.json()["statut"] == "running"
    r = await client.post(f"/v1/vms/{vid}/demarrage", json={})
    assert r.status_code == 409
    r = await client.post(f"/v1/vms/{vid}/redemarrage", json={})
    assert r.status_code == 202 and r.json()["statut"] == "done"


async def test_console_vm(client):
    espace_id = await _espace_demo(client)
    vid = await _creer_vm(client, espace_id, "console")
    r = await client.post(f"/v1/vms/{vid}/console")
    assert r.status_code == 201
    corps = r.json()
    assert corps["protocole"] == "vnc" and corps["url"]


async def test_journaux_vm(client):
    espace_id = await _espace_demo(client)
    vid = await _creer_vm(client, espace_id, "journaux")
    r = await client.get(f"/v1/vms/{vid}/journaux")
    assert r.status_code == 200
    corps = r.json()
    assert corps["lignes"] and all(
        x["niveau"] in ("INFO", "WARN", "ERROR", "DEBUG") for x in corps["lignes"]
    )


async def test_materiel_vm(client):
    espace_id = await _espace_demo(client)
    vid = await _creer_vm(client, espace_id, "materiel")
    corps = {"scsiControllers": 1, "nics": 2, "usb": True, "secureBoot": True}
    r = await client.put(f"/v1/vms/{vid}/materiel", json=corps)
    assert r.status_code == 202 and r.json()["statut"] == "done"
    r = await client.get(f"/v1/vms/{vid}")
    assert r.json()["hardware"]["nics"] == 2 and r.json()["hardware"]["usb"] is True
    corps2 = {"scsiControllers": 2, "nics": 2, "usb": True, "secureBoot": True}
    r = await client.put(f"/v1/vms/{vid}/materiel", json=corps2)
    assert r.status_code == 422 and r.json()["erreur"]["code"] == "non_porte"


async def test_metriques_vm(client):
    espace_id = await _espace_demo(client)
    vid = await _creer_vm(client, espace_id, "metriques")
    r = await client.get(f"/v1/vms/{vid}/metriques", params={"fenetre": "7j"})
    assert r.status_code == 200
    corps = r.json()
    assert all(s["fenetre"] == "7j" for s in corps["series"])
    assert any(s["metrique"] == "cpu" for s in corps["series"])


async def test_migration_vm(client):
    espace_id = await _espace_demo(client)
    vid = await _creer_vm(client, espace_id, "migrate")
    r = await client.post(f"/v1/vms/{vid}/migration", json={"site": "ABJ"})
    assert r.status_code == 202 and r.json()["statut"] == "done"
    r = await client.post(f"/v1/vms/{vid}/migration", json={"site": "GBM"})
    assert r.status_code == 422 and r.json()["erreur"]["code"] == "non_porte"


async def test_redimensionner_vm(client):
    # Lab: medium/large → NoValidHost. micro→small stays schedulable.
    espace_id = await _espace_demo(client)
    gabarit = await _gabarit_id(client, "micro")
    image_id = await _image_id(client)
    r = await client.post(
        "/v1/vms",
        json={"espaceId": espace_id, "nom": "resize", "imageId": image_id, "gabarit": gabarit},
    )
    assert r.status_code == 202 and r.json()["statut"] == "done"
    vid = next(v["id"] for v in (await client.get("/v1/vms")).json()["donnees"] if v["nom"] == "resize")
    r = await client.post(
        f"/v1/vms/{vid}/redimensionnement", json={"vcpu": 1, "ramGo": 2, "diskGo": 20}
    )
    assert r.status_code == 202 and r.json()["statut"] == "done"
    r = await client.get(f"/v1/vms/{vid}")
    assert r.json()["vcpu"] == 1 and r.json()["ramGo"] == 2 and r.json()["diskGo"] == 20
    r = await client.post(f"/v1/vms/{vid}/redimensionnement", json={"diskGo": 15})
    assert r.status_code == 422


async def test_redimensionner_sans_gabarit_echoue_franchement(client):
    """Un triplet sans gabarit correspondant est rejeté 422 au routeur (Nova ne sait
    redimensionner que vers un gabarit existant) — et non plus un travail `done` qui
    ne touchait que la fiche DB."""
    espace_id = await _espace_demo(client)
    vid = await _creer_vm(client, espace_id, "resize-fantome")
    avant = (await client.get(f"/v1/vms/{vid}")).json()
    r = await client.post(
        f"/v1/vms/{vid}/redimensionnement", json={"vcpu": 3, "ramGo": 6, "diskGo": 60}
    )
    assert r.status_code == 422
    apres = (await client.get(f"/v1/vms/{vid}")).json()
    assert (apres["vcpu"], apres["ramGo"], apres["diskGo"]) == (
        avant["vcpu"],
        avant["ramGo"],
        avant["diskGo"],
    )


async def test_lot_vms(client):
    espace_id = await _espace_demo(client)
    image_id = await _image_id(client)
    machines = [
        {
            "nom": "compose-web",
            "quantite": 2,
            "imageId": image_id,
            "vcpu": 1,
            "ramGo": 2,
            "diskGo": 20,
        }
    ]
    r = await client.post("/v1/vms/lot", json={"espaceId": espace_id, "machines": machines})
    assert r.status_code == 202 and r.json()["statut"] == "done"
    r = await client.get("/v1/vms", params={"espaceId": espace_id, "q": "compose-web"})
    noms = [v["nom"] for v in r.json()["donnees"]]
    assert "compose-web1" in noms and "compose-web2" in noms


async def test_instantanes_vm(client):
    espace_id = await _espace_demo(client)
    vid = await _creer_vm(client, espace_id, "instantane")
    r = await client.post(f"/v1/vms/{vid}/instantanes", json={"nom": "snap-1", "avecMemoire": True})
    assert r.status_code == 202 and r.json()["statut"] == "done"
    r = await client.get(f"/v1/vms/{vid}/instantanes")
    assert r.status_code == 200
    snap = r.json()[0]
    assert snap["nom"] == "snap-1"
    snap_id = snap["id"]
    r = await client.post(
        f"/v1/vms/{vid}/instantanes/{snap_id}", params={"confirmation": "mauvais"}
    )
    assert r.status_code == 422
    r = await client.post(f"/v1/vms/{vid}/instantanes/{snap_id}", params={"confirmation": "snap-1"})
    assert r.status_code == 202 and r.json()["statut"] == "done"
    r = await client.delete(f"/v1/vms/{vid}/instantanes/{snap_id}")
    assert r.status_code == 204
    r = await client.get(f"/v1/vms/{vid}/instantanes")
    assert r.json() == []


def test_mapper_statut_nova():
    from synelia.modules.vms.service import _mapper_statut_nova

    assert _mapper_statut_nova("absente") == "error"
    assert _mapper_statut_nova("ERROR") == "error"
    # Les transitions vivantes ne sont pas traduites : elles sont portées par les propres
    # travaux de l'application (et l'amont simulé ne retient aucun état — traduire `ACTIVE`
    # en `running` annulerait l'arrêt que `vm.power.stop` vient de poser).
    assert _mapper_statut_nova("ACTIVE") is None
    assert _mapper_statut_nova("SHUTOFF") is None
    assert _mapper_statut_nova("BUILDING") is None
    assert _mapper_statut_nova("PAUSED") is None
    assert _mapper_statut_nova("SHELVED") is None


async def test_reconciliation_statut_vm_orpheline(client, monkeypatch):
    # La ligne en base peut survivre à son infra réelle : une VM Nova supprimée hors bande
    # (nettoyage manuel du lab, travail tombé sans compensation) continuait d'afficher
    # `running` dans les listes et les tableaux de bord, et l'écart ne se voyait qu'au
    # premier usage (SSH, console…). Décision propriétaire : un orphelin confirmé est
    # **supprimé** par le chemin métier du DELETE (exécuteur `vm.delete`), pas seulement
    # marqué en erreur — la ligne disparaît réellement.
    from synelia.modules.vms import service as vms_service

    espace_id = await _espace_demo(client)
    vid = await _creer_vm(client, espace_id, "vm-reconcile")

    # Serveur toujours connu de Nova (simulé : ACTIVE) : la lecture ne change rien.
    r = await client.get(f"/v1/vms/{vid}")
    assert r.status_code == 200 and r.json()["statut"] == "running"

    # Nova ne connaît plus le serveur : suppression réelle déclenchée à la lecture.
    supprime = []
    monkeypatch.setattr(
        vms_service.ComputeSimule,
        "statut_serveur",
        lambda self, serveur_id, identifiants=None: "absente",
    )
    monkeypatch.setattr(
        vms_service.ComputeSimule,
        "supprimer_serveur",
        lambda self, serveur_id: supprime.append(serveur_id),
    )
    r = await client.get(f"/v1/vms/{vid}")
    # La lecture répond avec le marquage sincère posé avant le lancement du travail
    # (en mode en ligne, le travail `vm.delete` a déjà tourné et retiré la ligne).
    assert r.status_code == 200 and r.json()["statut"] == "error"
    assert len(supprime) == 1  # le serveur amont restant a bien visé par le chemin métier

    # La ligne a réellement disparu : plus de zombie dans la liste, détail en 404.
    r = await client.get("/v1/vms", params={"statut": "error"})
    assert all(v["id"] != vid for v in r.json()["donnees"])
    r = await client.get(f"/v1/vms/{vid}")
    assert r.status_code == 404


async def test_reconciliation_vm_orpheline_nova_deleted_pas_supprimee_attendu(client, monkeypatch):
    # Un serveur fraîchement supprimé reste un temps visible de Nova (ligne soft-delete,
    # statut `DELETED`, purge asynchrone) : c'est déjà un orphelin confirmé (pas
    # d'hyperviseur, pas d'IP) — la suppression réelle part sans attendre la purge.
    from synelia.modules.vms import service as vms_service

    espace_id = await _espace_demo(client)
    vid = await _creer_vm(client, espace_id, "vm-nova-deleted")

    monkeypatch.setattr(
        vms_service.ComputeSimule,
        "statut_serveur",
        lambda self, serveur_id, identifiants=None: "DELETED",
    )
    supprime = []
    monkeypatch.setattr(
        vms_service.ComputeSimule,
        "supprimer_serveur",
        lambda self, serveur_id: supprime.append(serveur_id),
    )
    r = await client.get(f"/v1/vms/{vid}")
    assert r.status_code == 200 and r.json()["statut"] == "error"
    assert len(supprime) == 1
    r = await client.get("/v1/vms", params={"statut": "error"})
    assert all(v["id"] != vid for v in r.json()["donnees"])
    r = await client.get(f"/v1/vms/{vid}")
    assert r.status_code == 404


async def test_reconciliation_vm_orpheline_nova_error_pas_supprimee(client, monkeypatch):
    # Nova `ERROR` : le serveur existe toujours (build raté, hyperviseur) — ce n'est PAS un
    # orphelin : la ligne est marquée `error` mais jamais supprimée automatiquement.
    from synelia.modules.vms import service as vms_service

    espace_id = await _espace_demo(client)
    vid = await _creer_vm(client, espace_id, "vm-nova-error")

    def _interdit(self, serveur_id):
        raise AssertionError("Un serveur Nova `ERROR` existe : jamais supprimé automatiquement")

    monkeypatch.setattr(
        vms_service.ComputeSimule,
        "statut_serveur",
        lambda self, serveur_id, identifiants=None: "ERROR",
    )
    monkeypatch.setattr(vms_service.ComputeSimule, "supprimer_serveur", _interdit)
    r = await client.get(f"/v1/vms/{vid}")
    assert r.status_code == 200 and r.json()["statut"] == "error"
    r = await client.get("/v1/vms", params={"statut": "error"})
    assert any(v["id"] == vid for v in r.json()["donnees"])


async def test_reconciliation_vm_orpheline_zone_vps_protegee(client, monkeypatch):
    # Garde-fou de la décision propriétaire : une VM de l'espace partagé `vps-zone`
    # (infrastructure de plateforme) n'est jamais supprimée automatiquement — un orphelin
    # confirmé y reste marqué `error`, requalifiable à la main. On simule la protection en
    # faisant de l'espace de démo l'espace protégé.
    from synelia.modules import espaces
    from synelia.modules.vms import service as vms_service

    espace_id = await _espace_demo(client)
    vid = await _creer_vm(client, espace_id, "vm-zone-vps")

    def _interdit(self, serveur_id):
        raise AssertionError("L'espace vps-zone n'est jamais supprimé automatiquement")

    monkeypatch.setattr(espaces.service, "ESPACE_ZONE_VPS_ID", espace_id)
    monkeypatch.setattr(
        vms_service.ComputeSimule,
        "statut_serveur",
        lambda self, serveur_id, identifiants=None: "absente",
    )
    monkeypatch.setattr(vms_service.ComputeSimule, "supprimer_serveur", _interdit)
    r = await client.get(f"/v1/vms/{vid}")
    assert r.status_code == 200 and r.json()["statut"] == "error"
    r = await client.get("/v1/vms", params={"statut": "error"})
    assert any(v["id"] == vid for v in r.json()["donnees"])


async def test_reconciliation_vm_orpheline_suppression_deja_en_vol(client, monkeypatch):
    # Un travail `vm.delete` est déjà en vol pour cette VM (DELETE utilisateur, lecture
    # concurrente) : la réconciliation ne redéclenche pas une seconde suppression — elle se
    # borne au marquage sincère, le travail en vol retirera la ligne.
    from synelia.modules.vms import service as vms_service

    espace_id = await _espace_demo(client)
    vid = await _creer_vm(client, espace_id, "vm-delete-en-vol")

    from synelia_db import session as db_session
    from synelia_db.modeles import Travail
    from synelia_kernel.ids import nouvel_id

    async with db_session.fabrique()() as s:
        s.add(
            Travail(
                id=nouvel_id(),
                type="vm.delete",
                label="vm.delete — en vol",
                statut="running",
                cible_id=vid,
            )
        )
        await s.commit()

    def _interdit(self, serveur_id):
        raise AssertionError("Suppression déjà en vol : la réconciliation ne redéclenche pas")

    monkeypatch.setattr(
        vms_service.ComputeSimule,
        "statut_serveur",
        lambda self, serveur_id, identifiants=None: "absente",
    )
    monkeypatch.setattr(vms_service.ComputeSimule, "supprimer_serveur", _interdit)
    r = await client.get(f"/v1/vms/{vid}")
    assert r.status_code == 200 and r.json()["statut"] == "error"
    r = await client.get("/v1/vms", params={"statut": "error"})
    assert any(v["id"] == vid for v in r.json()["donnees"])


async def test_reconciliation_statut_vm_shutoff_pas_un_orphelin(client, monkeypatch):
    # Un serveur éteint hors bande (SHUTOFF) n'est pas un orphelin : les invités du lab
    # s'éteignent la nuit et sont redémarrés — la réconciliation ne doit ni le marquer
    # `error`, ni contredire le statut posé par le propre flux de l'application.
    from synelia.modules.vms import service as vms_service

    espace_id = await _espace_demo(client)
    vid = await _creer_vm(client, espace_id, "vm-shutoff")
    monkeypatch.setattr(
        vms_service.ComputeSimule,
        "statut_serveur",
        lambda self, serveur_id, identifiants=None: "SHUTOFF",
    )
    r = await client.get(f"/v1/vms/{vid}")
    assert r.status_code == 200 and r.json()["statut"] == "running"
    r = await client.get("/v1/vms", params={"statut": "error"})
    assert all(v["id"] != vid for v in r.json()["donnees"])


async def test_reconciliation_vm_sans_serveur_id(client, monkeypatch):
    # Une ligne sans secret `serveur_id` (démo, VM antérieure au câblage Nova) n'a jamais
    # référencé d'infrastructure réelle identifiable : sans cette garde, le contrôle sur
    # l'id applicatif de repli (que Nova ignore toujours) la marquerait orpheline à tort.
    from synelia.modules.vms import service as vms_service

    espace_id = await _espace_demo(client)
    vid = await _creer_vm(client, espace_id, "vm-sans-serveur")

    async def _secrets_vides(ctx, id_, **kw):
        return {}

    def _interdit(self, serveur_id, identifiants=None):
        raise AssertionError("Nova ne doit pas être interrogé sans secret serveur_id")

    monkeypatch.setattr(vms_service.depot, "secrets", _secrets_vides)
    monkeypatch.setattr(vms_service.ComputeSimule, "statut_serveur", _interdit)
    r = await client.get(f"/v1/vms/{vid}")
    assert r.status_code == 200 and r.json()["statut"] == "running"
