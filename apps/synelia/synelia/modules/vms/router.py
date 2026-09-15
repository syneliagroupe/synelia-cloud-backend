from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, Response, status
from synelia_contract import modeles as m
from synelia_kernel import erreurs
from synelia_kernel.dates import maintenant
from synelia_kernel.ids import nouvel_id

from synelia.audit import journaliser
from synelia.deps import Contexte, Page, exige, exiger_confirmation
from synelia.modules.espaces.service import verifier_quota
from synelia.modules.vms import service
from synelia.modules.vms.service import amont, depot, instantane_depot, reconcilier_statut
from synelia.travaux import demarrer_travail

router = APIRouter(prefix="/vms", tags=["Machines virtuelles"])

_MATERIEL_DEFAUT = m.MateielVirtuel(scsiControllers=1, nics=1, usb=False, secureBoot=False)

_SERIES = [
    ("cpu", "%"),
    ("ram", "%"),
    ("disque", "Go"),
    ("reseau_entrant", "Mo/s"),
]


def _gabarit_pour_specs(vcpu: int, ram_go: int, disk_go: int, flore: dict[str, Any]) -> str:
    # Nova ne sait créer un serveur que vers un gabarit existant (pas de vcpu/ram/disque
    # arbitraires) : sans cette résolution, l'amont réel recevait un `flavorRef` vide et
    # rejetait la requête en pleine exécution du job (`flavorRef: None is not of type
    # 'string'`), au lieu d'un rejet propre à la validation — constaté en direct.
    correspondant = next(
        (
            g
            for g in flore.values()
            if g["vcpu"] == vcpu and g["ramGo"] == ram_go and g["diskGo"] == disk_go
        ),
        None,
    )
    if correspondant is None:
        raise erreurs.validation(
            "Aucun gabarit du catalogue ne correspond à ce vcpu/ramGo/diskGo.",
            champs={"gabarit": "Indiquez un gabarit existant du catalogue."},
        )
    return str(correspondant["id"])


async def _specs(corps: m.VmCreation) -> tuple[str | None, int, int, int]:
    flore = {g["id"]: g for g in await asyncio.to_thread(amont().gabarits)}
    if corps.gabarit:
        g = flore.get(corps.gabarit)
        if not g:
            raise erreurs.validation(
                "Gabarit inconnu.", champs={"gabarit": "Identifiant inexistant."}
            )
        return corps.gabarit, g["vcpu"], g["ramGo"], g["diskGo"]
    if corps.vcpu is not None and corps.ramGo is not None and corps.diskGo is not None:
        flavor = _gabarit_pour_specs(corps.vcpu, corps.ramGo, corps.diskGo, flore)
        return flavor, corps.vcpu, corps.ramGo, corps.diskGo
    raise erreurs.validation(
        "Indiquez un gabarit ou vcpu/ramGo/diskGo.",
        champs={"gabarit": "ou vcpu/ramGo/diskGo requis."},
    )


async def _image_par_id(image_id: str) -> dict[str, Any]:
    images = {i["id"]: i for i in await asyncio.to_thread(amont().images)}
    img = images.get(image_id)
    if not img:
        raise erreurs.validation(
            "Image système inconnue.", champs={"imageId": "Identifiant inexistant."}
        )
    return img


async def _vm(ctx: Contexte, vm_id: str) -> m.Vm:
    return await depot.obtenir(ctx, vm_id)


@router.get("", response_model=m.VmsGetResponse, response_model_exclude_none=True)
async def lister_vms(  # noqa: PLR0917
    page: Page,
    espaceId: str | None = None,
    site: str | None = None,
    statut: str | None = None,
    tag: str | None = None,
    applicationId: str | None = None,
    ctx: Contexte = Depends(exige("org.dashboard.view", lecture=True)),
) -> Any:
    resultat = await depot.lister(
        ctx,
        page,
        filtre=lambda v: (
            (not espaceId or v.espaceId == espaceId)
            and (not site or v.site == site)
            and (not statut or v.statut == statut)
            and (not tag or tag in (v.tags or []))
            and (not applicationId or v.applicationId == applicationId)
        ),
        tri_defaut="nom",
    )
    # Reconcile-on-read : sans ça une VM resterait affichée `running` dans la liste même après
    # la disparition de son serveur Nova (supprimé hors bande, ex. nettoyage du lab — cf.
    # `reconcilier_statut`).
    resultat["donnees"] = [await reconcilier_statut(ctx, v) for v in resultat["donnees"]]
    return resultat


@router.post(
    "",
    response_model=m.TravailProvisioning,
    status_code=status.HTTP_202_ACCEPTED,
    response_model_exclude_none=True,
)
async def creer_vm(corps: m.VmCreation, ctx: Contexte = Depends(exige("vm.create_delete"))) -> Any:
    espace = await verifier_quota(
        ctx, corps.espaceId, corps.vcpu or 0, corps.ramGo or 0, corps.diskGo or 0
    )
    flavor, vcpu, ram_go, disk_go = await _specs(corps)
    image = await _image_par_id(corps.imageId)
    await depot.exiger_nom_libre(ctx, corps.nom, parent_id=corps.espaceId)
    vm = m.Vm(
        id=nouvel_id(),
        espaceId=corps.espaceId,
        nom=corps.nom,
        os=image["id"],
        vcpu=vcpu,
        ramGo=ram_go,
        diskGo=disk_go,
        ips=[],
        statut="creating",
        hardware=corps.hardware or _MATERIEL_DEFAUT,
        site=corps.site or espace.site,
        tags=corps.tags,
        flavor=flavor,
        backupPlanId=corps.backupPlanId,
    )
    await depot.creer(ctx, vm, parent_id=corps.espaceId)
    await journaliser(ctx, action="vm.creation", cible_type="vm", cible_id=vm.id, cible=vm.nom)
    return await demarrer_travail(
        ctx,
        "vm.create",
        vm.nom,
        cible_type="vm",
        cible_id=vm.id,
        entree=corps.model_dump(mode="json"),
    )


@router.get("/{vmId}", response_model=m.Vm, response_model_exclude_none=True)
async def obtenir_vm(
    vmId: str, ctx: Contexte = Depends(exige("org.dashboard.view", lecture=True))
) -> Any:  # noqa: N803
    return await reconcilier_statut(ctx, await _vm(ctx, vmId))


@router.patch("/{vmId}", response_model=m.Vm, response_model_exclude_none=True)
async def modifier_vm(
    vmId: str, corps: m.VmModification, ctx: Contexte = Depends(exige("vm.create_delete"))
) -> Any:  # noqa: N803
    vm = await _vm(ctx, vmId)
    if corps.nom and corps.nom != vm.nom:
        await depot.exiger_nom_libre(ctx, corps.nom, parent_id=vm.espaceId)
    await depot.modifier(ctx, vmId, corps)
    await journaliser(
        ctx,
        action="vm.modification",
        cible_type="vm",
        cible_id=vmId,
        details=corps.model_dump(mode="json", exclude_none=True),
    )
    return await _vm(ctx, vmId)


@router.delete(
    "/{vmId}",
    response_model=m.TravailProvisioning,
    status_code=status.HTTP_202_ACCEPTED,
    response_model_exclude_none=True,
)
async def supprimer_vm(
    vmId: str, confirmation: str | None = None, ctx: Contexte = Depends(exige("vm.create_delete"))
) -> Any:  # noqa: N803
    vm = await _vm(ctx, vmId)
    exiger_confirmation(vm.nom, confirmation)
    await journaliser(ctx, action="vm.suppression", cible_type="vm", cible_id=vmId, cible=vm.nom)
    return await demarrer_travail(
        ctx, "vm.delete", vm.nom, cible_type="vm", cible_id=vmId, etapes=service.ETAPES_SUPPRESSION
    )


async def _controle_etat(ctx: Contexte, vm_id: str, sens: str) -> m.Vm:
    vm = await _vm(ctx, vm_id)
    deja = {("arret", "stopped"), ("demarrage", "running")}
    if sens == "redemarrage" and vm.statut != "running":
        raise erreurs.conflit(
            "On ne redémarre qu'une machine en cours d'exécution.", code="etat_incompatible"
        )
    if (sens, vm.statut) in deja:
        cible = {"arret": "arrêtée", "demarrage": "démarrée", "redemarrage": "redémarrée"}[sens]
        raise erreurs.conflit(f"La machine est déjà {cible}.", code="etat_deja_atteint")
    return vm


@router.post(
    "/{vmId}/arret",
    response_model=m.TravailProvisioning,
    status_code=status.HTTP_202_ACCEPTED,
    response_model_exclude_none=True,
)
async def arreter_vm(
    vmId: str, corps: m.VmsVmIdArretPostRequest, ctx: Contexte = Depends(exige("vm.power"))
) -> Any:  # noqa: N803
    vm = await _controle_etat(ctx, vmId, "arret")
    await journaliser(ctx, action="vm.arret", cible_type="vm", cible_id=vmId, cible=vm.nom)
    return await demarrer_travail(
        ctx,
        "vm.power.stop",
        vm.nom,
        cible_type="vm",
        cible_id=vmId,
        entree=corps.model_dump(mode="json"),
    )


@router.post(
    "/{vmId}/demarrage",
    response_model=m.TravailProvisioning,
    status_code=status.HTTP_202_ACCEPTED,
    response_model_exclude_none=True,
)
async def demarrer_vm(
    vmId: str, corps: m.VmsVmIdDemarragePostRequest, ctx: Contexte = Depends(exige("vm.power"))
) -> Any:  # noqa: N803
    vm = await _controle_etat(ctx, vmId, "demarrage")
    await journaliser(ctx, action="vm.demarrage", cible_type="vm", cible_id=vmId, cible=vm.nom)
    return await demarrer_travail(
        ctx,
        "vm.power.start",
        vm.nom,
        cible_type="vm",
        cible_id=vmId,
        entree=corps.model_dump(mode="json"),
    )


@router.post(
    "/{vmId}/redemarrage",
    response_model=m.TravailProvisioning,
    status_code=status.HTTP_202_ACCEPTED,
    response_model_exclude_none=True,
)
async def redemarrer_vm(
    vmId: str, corps: m.VmsVmIdRedemarragePostRequest, ctx: Contexte = Depends(exige("vm.power"))
) -> Any:  # noqa: N803
    vm = await _controle_etat(ctx, vmId, "redemarrage")
    await journaliser(ctx, action="vm.redemarrage", cible_type="vm", cible_id=vmId, cible=vm.nom)
    return await demarrer_travail(
        ctx,
        "vm.power.reboot",
        vm.nom,
        cible_type="vm",
        cible_id=vmId,
        entree=corps.model_dump(mode="json"),
    )


@router.post(
    "/{vmId}/console",
    response_model=m.ConsoleVm,
    status_code=status.HTTP_201_CREATED,
    response_model_exclude_none=True,
)
async def ouvrir_console_vm(vmId: str, ctx: Contexte = Depends(exige("vm.power"))) -> Any:  # noqa: N803
    from synelia_openstack.erreurs import traduire

    vm = await _vm(ctx, vmId)
    try:
        sid = await service.serveur_id(ctx, vm.id)
        url = await asyncio.to_thread(amont().console, sid)
    except erreurs.AppError:
        raise
    except Exception as exc:  # noqa: BLE001
        exc_str = str(exc)
        if "Guest does not have a console available" in exc_str:
            raise erreurs.non_porte(
                "Console indisponible : la machine virtuelle doit être démarrée avec un accès console pris en charge par l'hyperviseur."
            ) from None
        raise traduire(exc, "Machine virtuelle") from None
    await journaliser(
        ctx, action="vm.console_ouverte", cible_type="vm", cible_id=vmId, cible=vm.nom
    )
    return m.ConsoleVm(url=url, protocole="vnc", expire=maintenant() + timedelta(hours=2))


@router.get("/{vmId}/journaux", response_model=m.ExtraitLogs, response_model_exclude_none=True)
async def obtenir_journaux_vm(
    vmId: str, niveau: str | None = None, ctx: Contexte = Depends(exige(None))
) -> Any:  # noqa: N803
    vm = await _vm(ctx, vmId)
    from synelia_openstack.erreurs import traduire

    try:
        sid = await service.serveur_id(ctx, vm.id)
        lignes = await asyncio.to_thread(amont().journaux, sid)
    except erreurs.AppError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise traduire(exc, "Machine virtuelle") from None
    extrait = [m.LigneLog(ts=maintenant(), niveau="INFO", source="vm", message=ln) for ln in lignes]
    return m.ExtraitLogs(lignes=extrait, tronque=len(extrait) >= 20)


@router.put(
    "/{vmId}/materiel",
    response_model=m.TravailProvisioning,
    status_code=status.HTTP_202_ACCEPTED,
    response_model_exclude_none=True,
)
async def modifier_materiel_vm(
    vmId: str, corps: m.MateielVirtuel, ctx: Contexte = Depends(exige("vm.hardware.update"))
) -> Any:  # noqa: N803
    vm = await _vm(ctx, vmId)
    if corps.scsiControllers != vm.hardware.scsiControllers:
        raise erreurs.non_porte("La modification des contrôleurs SCSI n'est pas supportée à chaud.")
    await depot.modifier(ctx, vmId, {"hardware": corps.model_dump()})
    await journaliser(
        ctx, action="vm.materiel", cible_type="vm", cible_id=vmId, details=corps.model_dump()
    )
    return await demarrer_travail(
        ctx,
        "vm.hardware",
        vm.nom,
        cible_type="vm",
        cible_id=vmId,
        etapes=[
            {"nom": "Appliquer la configuration matérielle", "dureeS": 12},
            {"nom": "Redémarrer si nécessaire", "dureeS": 18},
        ],
    )


@router.get(
    "/{vmId}/metriques",
    response_model=m.VmsVmIdMetriquesGetResponse,
    response_model_exclude_none=True,
)
async def obtenir_metriques_vm(
    vmId: str, fenetre: str | None = None, ctx: Contexte = Depends(exige(None))
) -> Any:  # noqa: N803
    vm = await _vm(ctx, vmId)
    fen = fenetre if fenetre in ("24h", "7j", "30j") else "24h"
    # Point instantané réel (diagnostics Nova/libvirt), pas un historique : rien ne persiste
    # de série dans le temps côté backend, seulement le second relevé qui a servi à calculer
    # le point. `disque` reste toujours vide — les diagnostics donnent des E/S, jamais
    # l'occupation du disque, qu'aucune intégration ne remonte aujourd'hui pour une VM.
    valeurs = await service.diagnostics_instantanes(ctx, vm) if vm.statut == "running" else None
    ts = maintenant()
    series = [
        m.Serie(
            metrique=metrique,
            unite=unite,
            fenetre=fen,
            points=[m.PointSerie(ts=ts, valeur=valeurs[metrique])]
            if valeurs and metrique in valeurs
            else [],
        )
        for metrique, unite in _SERIES
    ]
    return m.VmsVmIdMetriquesGetResponse(series=series)


@router.post(
    "/{vmId}/migration",
    response_model=m.TravailProvisioning,
    status_code=status.HTTP_202_ACCEPTED,
    response_model_exclude_none=True,
)
async def migrer_vm(
    vmId: str,
    corps: m.VmsVmIdMigrationPostRequest,
    ctx: Contexte = Depends(exige("vm.hardware.update")),
) -> Any:  # noqa: N803
    vm = await _vm(ctx, vmId)
    if corps.site and corps.site != vm.site:
        raise erreurs.non_porte("La migration entre sites n'est pas supportée.")
    await journaliser(ctx, action="vm.migration", cible_type="vm", cible_id=vmId, cible=vm.nom)
    return await demarrer_travail(
        ctx,
        "vm.migrate",
        vm.nom,
        cible_type="vm",
        cible_id=vmId,
        entree=corps.model_dump(mode="json"),
    )


@router.post(
    "/{vmId}/redimensionnement",
    response_model=m.TravailProvisioning,
    status_code=status.HTTP_202_ACCEPTED,
    response_model_exclude_none=True,
)
async def redimensionner_vm(
    vmId: str, corps: m.VmRedimensionnement, ctx: Contexte = Depends(exige("vm.hardware.update"))
) -> Any:  # noqa: N803
    vm = await _vm(ctx, vmId)
    nouveau = {
        "vcpu": corps.vcpu if corps.vcpu is not None else vm.vcpu,
        "ramGo": corps.ramGo if corps.ramGo is not None else vm.ramGo,
        "diskGo": corps.diskGo if corps.diskGo is not None else vm.diskGo,
    }
    flore = {g["id"]: g for g in await asyncio.to_thread(amont().gabarits)}
    if nouveau["diskGo"] < vm.diskGo:
        raise erreurs.validation(
            "Un disque ne se réduit pas.", champs={"diskGo": "doit être ≥ à la taille actuelle."}
        )
    # Nova ne sait redimensionner que vers un gabarit existant : sans ce rejet propre, le
    # travail partait pour un `etape` qui ne trouvait pas de correspondance, sautait l'appel
    # amont et rendait `done` — la fiche DB mise à jour, la VM Nova inchangée (faux succès,
    # cf. `_specs` pour le même motif à la création).
    _gabarit_pour_specs(nouveau["vcpu"], nouveau["ramGo"], nouveau["diskGo"], flore)
    delta_vcpu = nouveau["vcpu"] - vm.vcpu
    delta_ram = nouveau["ramGo"] - vm.ramGo
    delta_disk = nouveau["diskGo"] - vm.diskGo
    await verifier_quota(
        ctx, vm.espaceId, max(0, delta_vcpu), max(0, delta_ram), max(0, delta_disk)
    )
    await journaliser(
        ctx, action="vm.redimensionnement", cible_type="vm", cible_id=vmId, details=nouveau
    )
    return await demarrer_travail(
        ctx, "vm.resize", vm.nom, cible_type="vm", cible_id=vmId, entree=nouveau
    )


@router.post(
    "/lot",
    response_model=m.TravailProvisioning,
    status_code=status.HTTP_202_ACCEPTED,
    response_model_exclude_none=True,
)
async def creer_vms_en_lot(
    corps: m.VmLotCreation, ctx: Contexte = Depends(exige("vm.create_delete"))
) -> Any:
    total_vcpu = sum((mac.vcpu or 0) * (mac.quantite or 1) for mac in corps.machines)
    total_ram = sum((mac.ramGo or 0) * (mac.quantite or 1) for mac in corps.machines)
    total_disk = sum((mac.diskGo or 0) * (mac.quantite or 1) for mac in corps.machines)
    await verifier_quota(ctx, corps.espaceId, total_vcpu, total_ram, total_disk)
    images = {i["id"] for i in await asyncio.to_thread(amont().images)}
    flore = {g["id"]: g for g in await asyncio.to_thread(amont().gabarits)}
    gabarits: dict[str, str] = {}
    for mac in corps.machines:
        if mac.imageId not in images:
            raise erreurs.validation(
                "Image système inconnue.", champs={"imageId": "Identifiant inexistant."}
            )
        gabarits[mac.nom] = _gabarit_pour_specs(mac.vcpu, mac.ramGo, mac.diskGo, flore)
    await journaliser(ctx, action="vm.compose", cible_type="espace", cible_id=corps.espaceId)
    entree = corps.model_dump(mode="json")
    entree["gabarits"] = gabarits
    return await demarrer_travail(
        ctx,
        "vm.compose",
        f"{len(corps.machines)} machines",
        cible_type="espace",
        cible_id=corps.espaceId,
        entree=entree,
    )


@router.get(
    "/{vmId}/instantanes", response_model=list[m.InstantaneVm], response_model_exclude_none=True
)
async def lister_instantanes_vm(vmId: str, ctx: Contexte = Depends(exige(None))) -> Any:  # noqa: N803
    await _vm(ctx, vmId)
    return await instantane_depot.tous(ctx, parent_id=vmId)


@router.post(
    "/{vmId}/instantanes",
    response_model=m.TravailProvisioning,
    status_code=status.HTTP_202_ACCEPTED,
    response_model_exclude_none=True,
)
async def creer_instantane_vm(
    vmId: str,
    corps: m.VmsVmIdInstantanesPostRequest,
    ctx: Contexte = Depends(exige("backup.plan.write")),
) -> Any:  # noqa: N803
    vm = await _vm(ctx, vmId)
    await journaliser(ctx, action="vm.instantane", cible_type="vm", cible_id=vmId, cible=vm.nom)
    return await demarrer_travail(
        ctx,
        "vm.snapshot",
        vm.nom,
        cible_type="vm",
        cible_id=vmId,
        entree=corps.model_dump(mode="json"),
    )


@router.delete(
    "/{vmId}/instantanes/{instantaneId}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
)
async def supprimer_instantane_vm(
    instantaneId: str, ctx: Contexte = Depends(exige("backup.plan.write"))
) -> Any:  # noqa: N803
    await instantane_depot.obtenir(ctx, instantaneId)
    # Sans cet appel, la suppression ne retirait que la ligne DB : l'image Glance réelle
    # capturée par `vm.snapshot` (cf. `ExecuteurVmSnapshot`, secret `image_id`) restait active
    # indéfiniment — un snapshot « supprimé » du point de vue de l'interface continuait de
    # consommer de la capacité Glance. Même motif que `serveur_id` sur `ExecuteurVmDelete`.
    try:
        secrets = await instantane_depot.secrets(ctx, instantaneId)
    except Exception:  # noqa: BLE001
        secrets = {}
    image_id = secrets.get("image_id")
    if image_id:
        await asyncio.to_thread(amont().supprimer_image, image_id)
    await instantane_depot.supprimer(ctx, instantaneId, logique=False)
    await journaliser(
        ctx, action="vm.instantane.suppression", cible_type="instantane_vm", cible_id=instantaneId
    )
    return Response(status_code=204)


@router.post(
    "/{vmId}/instantanes/{instantaneId}",
    response_model=m.TravailProvisioning,
    status_code=status.HTTP_202_ACCEPTED,
    response_model_exclude_none=True,
)
async def restaurer_instantane_vm(
    vmId: str,
    instantaneId: str,
    confirmation: str | None = None,
    ctx: Contexte = Depends(exige("backup.restore")),
) -> Any:  # noqa: N803
    vm = await _vm(ctx, vmId)
    inst = await instantane_depot.obtenir(ctx, instantaneId)
    exiger_confirmation(inst.nom, confirmation)
    await journaliser(
        ctx, action="vm.instantane_restauration", cible_type="vm", cible_id=vmId, cible=vm.nom
    )
    return await demarrer_travail(
        ctx,
        "vm.restore",
        vm.nom,
        cible_type="vm",
        cible_id=vmId,
        entree={"instantaneId": instantaneId},
        etapes=[
            {"nom": "Préparer la restauration", "dureeS": 20},
            {"nom": "Restaurer les volumes", "dureeS": 45},
            {"nom": "Redémarrer la machine", "dureeS": 25},
        ],
    )
