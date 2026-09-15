from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Response, status
from synelia_contract import modeles as m
from synelia_kernel import erreurs
from synelia_kernel.dates import maintenant
from synelia_kernel.ids import nouvel_id

from synelia.audit import journaliser
from synelia.depot import Depot
from synelia.deps import Contexte, Page, exige, exiger_confirmation
from synelia.modules.espaces.service import verifier_quota
from synelia.modules.stockage import service
from synelia.modules.stockage.service import depot_bucket, depot_cle, depot_volume
from synelia.travaux import demarrer_travail

router = APIRouter(prefix="/volumes", tags=["Stockage"])


@router.get("", response_model=m.VolumesGetResponse, response_model_exclude_none=True)
async def lister_volumes(
    page: Page,
    espaceId: str | None = None,
    classe: str | None = None,
    attache: str | None = None,
    ctx: Contexte = Depends(exige("org.dashboard.view", lecture=True)),
) -> Any:
    return await depot_volume.lister(
        ctx,
        page,
        filtre=lambda v: (
            (not espaceId or v.espaceId == espaceId)
            and (not classe or v.classe == classe)
            and (
                attache is None
                or (attache == "true" and bool(v.attachedTo))
                or (attache == "false" and not v.attachedTo)
            )
        ),
        tri_defaut="nom",
    )


@router.post(
    "",
    response_model=m.TravailProvisioning,
    status_code=status.HTTP_202_ACCEPTED,
    response_model_exclude_none=True,
)
async def creer_volume(
    corps: m.VolumeCreation, ctx: Contexte = Depends(exige("vm.create_delete"))
) -> Any:
    await depot_volume.exiger_nom_libre(ctx, corps.nom)
    vol = m.Volume(
        id=nouvel_id(),
        espaceId=corps.espaceId,
        nom=corps.nom,
        tailleGo=corps.tailleGo,
        classe=corps.classe,
        chiffre=bool(corps.chiffre),
        ephemere=False,
        iops=service.iops_pour(corps.classe),
        attachedTo=corps.attacherA,
        attachedLabel=None,
        montage=corps.montage,
    )
    await depot_volume.creer(ctx, vol)
    await journaliser(
        ctx, action="volume.creation", cible_type="volume", cible_id=vol.id, cible=vol.nom
    )
    return await demarrer_travail(
        ctx,
        "volume.create",
        vol.nom,
        cible_type="volume",
        cible_id=vol.id,
        entree=corps.model_dump(mode="json"),
        etapes=[
            {"nom": "Provisionner le volume Cinder", "dureeS": 8},
            {"nom": "Vérifier la répartition", "dureeS": 4},
            {"nom": "Rendre disponible", "dureeS": 2},
        ],
    )


@router.get("/{volumeId}", response_model=m.Volume, response_model_exclude_none=True)
async def obtenir_volume(
    volumeId: str, ctx: Contexte = Depends(exige("org.dashboard.view", lecture=True))
) -> Any:  # noqa: N803
    return await depot_volume.obtenir(ctx, volumeId)


@router.patch("/{volumeId}", response_model=m.Volume, response_model_exclude_none=True)
async def modifier_volume(
    volumeId: str, corps: m.VolumeCreation, ctx: Contexte = Depends(exige("vm.create_delete"))
) -> Any:  # noqa: N803
    await depot_volume.obtenir(ctx, volumeId)
    await depot_volume.modifier(ctx, volumeId, corps.model_dump(exclude_none=True))
    await journaliser(
        ctx,
        action="volume.modification",
        cible_type="volume",
        cible_id=volumeId,
        details=corps.model_dump(mode="json", exclude_none=True),
    )
    return await depot_volume.obtenir(ctx, volumeId)


@router.delete("/{volumeId}", response_model=None, status_code=status.HTTP_204_NO_CONTENT)
async def supprimer_volume(
    volumeId: str,
    confirmation: str | None = None,
    ctx: Contexte = Depends(exige("vm.create_delete")),
) -> Any:  # noqa: N803
    vol = await depot_volume.obtenir(ctx, volumeId)
    exiger_confirmation(vol.nom, confirmation)
    if vol.attachedTo:
        raise erreurs.conflit("Le volume est encore attaché à une machine.", code="volume_attache")
    await journaliser(
        ctx, action="volume.suppression", cible_type="volume", cible_id=volumeId, cible=vol.nom
    )
    vid = await service.volume_id_reel(ctx, volumeId)
    if vid and vid != volumeId:
        # `volume_id_reel` retombe sur l'identifiant local quand aucun volume Cinder réel n'a
        # jamais existé (ex. création échouée avant la pose du secret `volume_id`) : appeler
        # l'amont avec cet identifiant local le fait échouer en 404 côté Cinder, ce qui rendait
        # un tel volume définitivement impossible à supprimer depuis l'API (constaté en
        # direct). On ne touche l'amont que si un vrai identifiant Cinder a été résolu — même
        # garde que `vms.service.ExecuteurVmDelete`.
        secrets_espace = await service.identifiants_espace(ctx, vol.espaceId)
        service.amont_cinder().supprimer(vid, identifiants=secrets_espace)
    await depot_volume.supprimer(ctx, volumeId, logique=True)
    return Response(status_code=204)


@router.put(
    "/{volumeId}/attachement",
    response_model=m.TravailProvisioning,
    status_code=status.HTTP_202_ACCEPTED,
    response_model_exclude_none=True,
)
async def attacher_volume(
    volumeId: str,
    corps: m.VolumesVolumeIdAttachementPutRequest,
    ctx: Contexte = Depends(exige("vm.hardware.update")),
) -> Any:  # noqa: N803
    vol = await depot_volume.obtenir(ctx, volumeId)
    await Depot("vm", m.Vm).obtenir(ctx, corps.vmId)
    if vol.attachedTo:
        raise erreurs.conflit(
            "Le volume est déjà attaché à une machine.", code="volume_deja_attache"
        )
    await journaliser(
        ctx,
        action="volume.attachement",
        cible_type="volume",
        cible_id=volumeId,
        cible=vol.nom,
        details={"vmId": corps.vmId},
    )
    return await demarrer_travail(
        ctx,
        "volume.attach",
        vol.nom,
        cible_type="volume",
        cible_id=volumeId,
        entree=corps.model_dump(mode="json"),
        contexte={"vm_id": corps.vmId, "montage": corps.montage},
        etapes=[
            {"nom": "Attacher le volume à la machine", "dureeS": 6},
            {"nom": "Monter le point de montage", "dureeS": 4},
        ],
    )


@router.delete(
    "/{volumeId}/attachement",
    response_model=m.TravailProvisioning,
    status_code=status.HTTP_202_ACCEPTED,
    response_model_exclude_none=True,
)
async def detacher_volume(
    volumeId: str, ctx: Contexte = Depends(exige("vm.hardware.update"))
) -> Any:  # noqa: N803
    vol = await depot_volume.obtenir(ctx, volumeId)
    if not vol.attachedTo:
        raise erreurs.conflit(
            "Le volume n'est attaché à aucune machine.", code="volume_non_attache"
        )
    await journaliser(
        ctx, action="volume.detachement", cible_type="volume", cible_id=volumeId, cible=vol.nom
    )
    return await demarrer_travail(
        ctx,
        "volume.detach",
        vol.nom,
        cible_type="volume",
        cible_id=volumeId,
        contexte={"vm_id": vol.attachedTo},
        etapes=[
            {"nom": "Démonter le point de montage", "dureeS": 4},
            {"nom": "Détacher le volume", "dureeS": 6},
        ],
    )


@router.post(
    "/{volumeId}/extension",
    response_model=m.TravailProvisioning,
    status_code=status.HTTP_202_ACCEPTED,
    response_model_exclude_none=True,
)
async def etendre_volume(
    volumeId: str,
    corps: m.VolumesVolumeIdExtensionPostRequest,
    ctx: Contexte = Depends(exige("vm.hardware.update")),
) -> Any:  # noqa: N803
    vol = await depot_volume.obtenir(ctx, volumeId)
    delta = corps.tailleGo - vol.tailleGo
    if delta <= 0:
        raise erreurs.validation(
            "La nouvelle taille doit être supérieure à la taille actuelle.",
            {"tailleGo": f"doit être > {vol.tailleGo}"},
        )
    await verifier_quota(ctx, vol.espaceId, disk_go=delta)
    await journaliser(
        ctx,
        action="volume.extension",
        cible_type="volume",
        cible_id=volumeId,
        cible=vol.nom,
        details={"tailleGo": corps.tailleGo},
    )
    return await demarrer_travail(
        ctx,
        "volume.extend",
        vol.nom,
        cible_type="volume",
        cible_id=volumeId,
        entree=corps.model_dump(mode="json"),
        contexte={"taille_go": corps.tailleGo},
        etapes=[
            {"nom": "Étendre le volume", "dureeS": 12},
            {"nom": "Agrandir le système de fichiers", "dureeS": 8},
        ],
    )


router_cles = APIRouter(prefix="/cles-s3", tags=["Stockage"])


@router_cles.get("", response_model=m.ClesS3GetResponse, response_model_exclude_none=True)
async def lister_cles_s3(
    page: Page, ctx: Contexte = Depends(exige("org.dashboard.view", lecture=True))
) -> Any:
    return await depot_cle.lister(ctx, page, tri_defaut="nom")


@router_cles.post(
    "",
    response_model=m.CleS3Secret,
    status_code=status.HTTP_201_CREATED,
    response_model_exclude_none=True,
)
async def creer_cle_s3(
    corps: m.CleS3Creation, ctx: Contexte = Depends(exige("vm.create_delete"))
) -> Any:
    await depot_cle.exiger_nom_libre(ctx, corps.nom)
    amont = service.amont_objet()
    if corps.buckets:
        buckets_reels = [service.nom_reel_bucket(ctx, b) for b in corps.buckets]
    else:
        buckets_reels = [f"{service.prefixe_bucket(ctx)}-*"]
    secret = amont.creer_cle_s3(corps.nom, buckets_reels, corps.droits)
    cle = m.CleS3(
        id=nouvel_id(),
        nom=corps.nom,
        portee=_portee(corps.buckets, corps.droits),
        buckets=corps.buckets,
        droits=corps.droits,
        creee=maintenant().date(),
        accessKeyId=secret["access_key_id"],
    )
    await depot_cle.creer(ctx, cle, secrets={"secret_access_key": secret["secret_access_key"]})
    await journaliser(
        ctx, action="cle_s3.creation", cible_type="cle_s3", cible_id=cle.id, cible=cle.nom
    )
    secrets_stockes = await depot_cle.secrets(ctx, cle.id)
    return m.CleS3Secret(
        cle=await depot_cle.obtenir(ctx, cle.id),
        accessKeyId=secret["access_key_id"],
        secretAccessKey=secrets_stockes["secret_access_key"],
        endpoint=secret["endpoint"],
    )


def _portee(buckets: list[str] | None, droits: str) -> str:
    if buckets:
        return f"{droits} sur {', '.join(buckets)}"
    return f"{droits} sur tous les buckets de l'organisation"


@router_cles.get("/{cleS3Id}", response_model=m.CleS3, response_model_exclude_none=True)
async def obtenir_cle_s3(
    cleS3Id: str, ctx: Contexte = Depends(exige("org.dashboard.view", lecture=True))
) -> Any:  # noqa: N803
    return await depot_cle.obtenir(ctx, cleS3Id)


@router_cles.delete("/{cleS3Id}", response_model=None, status_code=status.HTTP_204_NO_CONTENT)
async def revoquer_cle_s3(
    cleS3Id: str,
    confirmation: str | None = None,
    ctx: Contexte = Depends(exige("vm.create_delete")),
) -> Any:  # noqa: N803
    cle = await depot_cle.obtenir(ctx, cleS3Id)
    exiger_confirmation(cle.nom, confirmation)
    await journaliser(
        ctx, action="cle_s3.revocation", cible_type="cle_s3", cible_id=cleS3Id, cible=cle.nom
    )
    if cle.accessKeyId:
        service.amont_objet().revoquer_cle_s3(cle.accessKeyId)
    await depot_cle.supprimer(ctx, cleS3Id, logique=True)
    return Response(status_code=204)


router_buckets = APIRouter(prefix="/buckets", tags=["Stockage"])


@router_buckets.get("", response_model=m.BucketsGetResponse, response_model_exclude_none=True)
async def lister_buckets(
    page: Page,
    region: str | None = None,
    classe: str | None = None,
    ctx: Contexte = Depends(exige("org.dashboard.view", lecture=True)),
) -> Any:
    return await depot_bucket.lister(
        ctx,
        page,
        filtre=lambda b: (not region or b.region == region) and (not classe or b.classe == classe),
        tri_defaut="nom",
    )


@router_buckets.post(
    "",
    response_model=m.Bucket,
    status_code=status.HTTP_201_CREATED,
    response_model_exclude_none=True,
)
async def creer_bucket(
    corps: m.BucketCreation, ctx: Contexte = Depends(exige("vm.create_delete"))
) -> Any:
    if not corps.nom or not corps.nom.strip():
        raise erreurs.validation(
            "Le nom du bucket est requis.", {"nom": "Champ requis."}
        )
    await depot_bucket.exiger_nom_libre(ctx, corps.nom)
    nom_reel = service.nom_reel_bucket(ctx, corps.nom)
    amont = service.amont_objet()
    amont.creer_bucket(nom=nom_reel, region=corps.region)
    amont.definir_policy_bucket(nom_reel, corps.policy or "prive", corps.policyJson)
    bucket = m.Bucket(
        id=nouvel_id(),
        orgId=ctx.org_id,
        espaceId=corps.espaceId,
        nom=corps.nom,
        region=corps.region,
        classe=corps.classe,
        tailleGo=0.0,
        objets=0,
        versioning=bool(corps.versioning),
        objectLock=conversion(corps.objectLock),
        replication=conversion(corps.replication),
        accessLogs=bool(corps.accessLogs),
        policy=corps.policy or "prive",
    )
    await depot_bucket.creer(ctx, bucket, secrets={"bucket_reel": nom_reel})
    await journaliser(
        ctx, action="bucket.creation", cible_type="bucket", cible_id=bucket.id, cible=bucket.nom
    )
    return await depot_bucket.obtenir(ctx, bucket.id)


def conversion(v):
    if v is None:
        return None
    return v


@router_buckets.get("/{bucketId}", response_model=m.Bucket, response_model_exclude_none=True)
async def obtenir_bucket(
    bucketId: str, ctx: Contexte = Depends(exige("org.dashboard.view", lecture=True))
) -> Any:  # noqa: N803
    return await depot_bucket.obtenir(ctx, bucketId)


@router_buckets.patch("/{bucketId}", response_model=m.Bucket, response_model_exclude_none=True)
async def modifier_bucket(
    bucketId: str, corps: m.BucketCreation, ctx: Contexte = Depends(exige("vm.create_delete"))
) -> Any:  # noqa: N803
    bucket = await depot_bucket.obtenir(ctx, bucketId)
    await depot_bucket.modifier(ctx, bucketId, corps.model_dump(exclude_none=True))
    if corps.policy is not None:
        nom_reel = await service.nom_reel_bucket_existant(ctx, bucketId, bucket.nom)
        service.amont_objet().definir_policy_bucket(nom_reel, corps.policy, corps.policyJson)
    await journaliser(
        ctx,
        action="bucket.modification",
        cible_type="bucket",
        cible_id=bucketId,
        details=corps.model_dump(mode="json", exclude_none=True),
    )
    return await depot_bucket.obtenir(ctx, bucketId)


@router_buckets.delete("/{bucketId}", response_model=None, status_code=status.HTTP_204_NO_CONTENT)
async def supprimer_bucket(
    bucketId: str,
    confirmation: str | None = None,
    ctx: Contexte = Depends(exige("vm.create_delete")),
) -> Any:  # noqa: N803
    bucket = await depot_bucket.obtenir(ctx, bucketId)
    exiger_confirmation(bucket.nom, confirmation)
    await journaliser(
        ctx, action="bucket.suppression", cible_type="bucket", cible_id=bucketId, cible=bucket.nom
    )
    nom_reel = await service.nom_reel_bucket_existant(ctx, bucketId, bucket.nom)
    service.amont_objet().supprimer_bucket(nom_reel)
    await depot_bucket.supprimer(ctx, bucketId, logique=True)
    return Response(status_code=204)


@router_buckets.get(
    "/{bucketId}/usage",
    response_model=m.BucketsBucketIdUsageGetResponse,
    response_model_exclude_none=True,
)
async def obtenir_usage_bucket(
    bucketId: str, fenetre: str | None = None, ctx: Contexte = Depends(exige(None))
) -> Any:  # noqa: N803
    bucket = await depot_bucket.obtenir(ctx, bucketId)
    nom_reel = await service.nom_reel_bucket_existant(ctx, bucketId, bucket.nom)
    usage = service.amont_objet().usage(nom_reel)
    return m.BucketsBucketIdUsageGetResponse(
        tailleGo=usage["taille_go"],
        objets=usage["objets"],
        requetes=usage["requetes"],
        egressGo=usage["egress_go"],
    )
