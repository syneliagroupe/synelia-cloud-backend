"""Sauvegarde : plans, exécutions, points de restauration, restaurations, conformité 3-2-1."""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from typing import Any

from synelia_contract import modeles as m
from synelia_db.modeles import Travail
from synelia_kernel.dates import maintenant
from synelia_kernel.ids import nouvel_id
from synelia_openstack import fournisseur
from synelia_openstack.backup import BackupOpenStack, BackupSimule

from synelia.depot import Depot
from synelia.deps.contexte import Contexte
from synelia.modules.stockage.service import (
    amont_cinder,
    depot_volume,
    identifiants_espace,
    volume_id_reel,
)
from synelia.modules.vms.service import depot as depot_vms
from synelia.travaux import Executeur, executeur

depot = Depot(
    "plan_sauvegarde", m.PlanSauvegarde, libelle="Plan de sauvegarde", champs_recherche=("nom",)
)
points = Depot(
    "point_restauration",
    m.PointRestauration,
    libelle="Point de restauration",
    champs_recherche=("resourceNom", "planNom"),
)
restaurations = Depot(
    "restauration", m.Restauration, libelle="Restauration", champs_recherche=("ressourceNom",)
)


def amont() -> BackupSimule:
    return fournisseur(BackupSimule, BackupOpenStack)


def plan_vers_modele(
    corps: m.PlanSauvegardeCreation, ctx: Contexte, ressources: int
) -> m.PlanSauvegarde:
    return m.PlanSauvegarde(
        id=nouvel_id(),
        orgId=ctx.org_id,
        nom=corps.nom,
        scope=corps.scope,
        frequence=corps.frequence,
        mode=corps.mode,
        retentionJours=corps.retentionJours,
        immutable=bool(corps.immutable),
        destinations=corps.destinations,
        prochaineExecution=maintenant() + timedelta(hours=24),
        chiffrement=m.Chiffrement(
            mode=(corps.chiffrement.mode if corps.chiffrement else "synelia"),
            kmsRef=corps.chiffrement.kmsRef if corps.chiffrement else None,
        ),
        ressourcesProtegees=ressources,
        dernierResultat="ok",
    )


def _nouveau_point(ctx: Contexte, plan: m.PlanSauvegarde) -> m.PointRestauration:
    main = maintenant()
    destination = plan.destinations[0].type if plan.destinations else "local"
    ressource_type = plan.scope.type if plan.scope.type in {"vm", "ressource"} else "vm"
    return m.PointRestauration(
        id=nouvel_id(),
        planId=plan.id,
        planNom=plan.nom,
        resourceId=plan.scope.valeur,
        resourceNom=plan.scope.valeur,
        resourceType=ressource_type,
        date=main,
        tailleGo=round(2.4 + plan.ressourcesProtegees * 0.6, 1),
        type="complete" if plan.mode == "complete" else "incrementale",
        immuableJusquau=main + timedelta(days=plan.retentionJours) if plan.immutable else None,
        verifie=False,
        destination=destination,
        expiration=main + timedelta(days=plan.retentionJours),
    )


async def _volumes_du_scope(ctx: Contexte, plan: m.PlanSauvegarde) -> list[m.Volume]:
    """Volumes réels attachés à la ressource visée, si `scope.valeur` désigne une VM connue.
    Sinon `[]` : le scope couvre autre chose qu'une VM (tag, espace, service), pas encore
    câblé sur du stockage bloc réel."""
    vm = await depot_vms.trouver(ctx, plan.scope.valeur)
    if vm is None:
        return []
    return [v for v in await depot_volume.tous(ctx) if v.attachedTo == vm.id]


@executeur("backup.run")
class ExecuteurSauvegarde(Executeur):
    async def etape(self, ctx: Contexte, travail: Travail, index: int, nom: str) -> str | None:
        plan = await depot.obtenir(ctx, travail.cible_id or "")
        if index == 0:
            return f"Plan « {plan.nom} » validé, {plan.ressourcesProtegees} ressources couvertes."
        if index == 1:
            volumes = await _volumes_du_scope(ctx, plan)
            if not volumes:
                a = await asyncio.to_thread(amont().executer_plan, plan.nom, plan.ressourcesProtegees)
                travail.contexte = {**dict(travail.contexte), "taille_go": a["taille_go"]}
                return f"Snapshot créé ({a['taille_go']} Go)."
            snapshot_ids: list[str] = []
            volume_ids: list[str] = []
            taille_go = 0.0
            for vol in volumes:
                vid = await volume_id_reel(ctx, vol.id)
                identifiants = await identifiants_espace(ctx, vol.espaceId)
                # `amont_cinder()` (Cinder, openstacksdk synchrone) est déchargé via
                # `asyncio.to_thread` : même garde que `vms.service`, sans quoi un appel amont
                # lent gèlerait la boucle asyncio — donc l'API entière, tous tenants confondus.
                snap = await asyncio.to_thread(
                    amont_cinder().creer_snapshot,
                    vid,
                    f"backup-{plan.nom}-{nouvel_id()[:8]}",
                    identifiants=identifiants,
                )
                snapshot_ids.append(snap["id"])
                volume_ids.append(vol.id)
                taille_go += vol.tailleGo or 10
            travail.contexte = {
                **dict(travail.contexte),
                "taille_go": round(taille_go, 1),
                "snapshot_ids": snapshot_ids,
                "volume_ids": volume_ids,
            }
            return f"{len(snapshot_ids)} snapshot(s) réel(s) créé(s) ({round(taille_go, 1)} Go)."
        return None

    async def terminer(self, ctx: Contexte, travail: Travail) -> None:
        plan = await depot.obtenir(ctx, travail.cible_id or "")
        point = _nouveau_point(ctx, plan)
        if travail.contexte.get("taille_go"):
            point.tailleGo = float(travail.contexte["taille_go"])
        await points.creer(ctx, point)
        if travail.contexte.get("snapshot_ids"):
            # `definir_secrets` chiffre des chaînes (`chiffrer(clair: str)`) : les identifiants
            # sont une liste, donc sérialisés en JSON avant chiffrement, désérialisés à la lecture
            # (`supprimer_snapshots_reels`, `ExecuteurVerification`, `ExecuteurRestauration`).
            await points.definir_secrets(
                ctx,
                point.id,
                {
                    "snapshot_ids": json.dumps(travail.contexte["snapshot_ids"]),
                    "volume_ids": json.dumps(travail.contexte.get("volume_ids", [])),
                },
            )
        await depot.modifier(ctx, plan.id, {"dernierResultat": "ok"})


async def supprimer_snapshots_reels(ctx: Contexte, point_id: str) -> None:
    """Purge les snapshots Cinder réels d'un point de restauration avant que sa ligne ne
    disparaisse — sans quoi `DELETE /sauvegarde/points/{id}` ne fait qu'un soft-delete côté base
    et laisse les instantanés réels orphelins sur le lab (fuite de stockage silencieuse, jamais
    facturée ni nettoyée). Best-effort : un volume déjà supprimé ou un snapshot déjà absent ne
    doit pas empêcher la suppression du point côté application."""
    secrets = await points.secrets(ctx, point_id)
    snapshot_ids = json.loads(secrets.get("snapshot_ids") or "[]")
    volume_ids = json.loads(secrets.get("volume_ids") or "[]")
    if not snapshot_ids or not volume_ids:
        return
    try:
        vol = await depot_volume.obtenir(ctx, volume_ids[0])
    except Exception:  # noqa: BLE001
        return
    identifiants = await identifiants_espace(ctx, vol.espaceId)
    for sid in snapshot_ids:
        try:
            await asyncio.to_thread(amont_cinder().supprimer_snapshot, sid, identifiants=identifiants)
        except Exception:  # noqa: BLE001
            continue


@executeur("backup.verify")
class ExecuteurVerification(Executeur):
    async def terminer(self, ctx: Contexte, travail: Travail) -> None:
        point_id = travail.cible_id or ""
        secrets = await points.secrets(ctx, point_id)
        snapshot_ids = json.loads(secrets.get("snapshot_ids") or "[]")
        volume_ids = json.loads(secrets.get("volume_ids") or "[]")
        verifie = True
        if snapshot_ids and volume_ids:
            vol = await depot_volume.obtenir(ctx, volume_ids[0])
            identifiants = await identifiants_espace(ctx, vol.espaceId)
            statuts = [
                await asyncio.to_thread(
                    amont_cinder().statut_snapshot, sid, identifiants=identifiants
                )
                for sid in snapshot_ids
            ]
            verifie = all(s == "available" for s in statuts)
        await points.modifier(ctx, point_id, {"verifie": verifie})


@executeur("backup.restore")
class ExecuteurRestauration(Executeur):
    async def terminer(self, ctx: Contexte, travail: Travail) -> None:
        restauration_id = travail.cible_id or ""
        restauration = await restaurations.obtenir(ctx, restauration_id)
        point = await points.obtenir(ctx, restauration.pointId)
        secrets = await points.secrets(ctx, point.id)
        snapshot_ids = json.loads(secrets.get("snapshot_ids") or "[]")
        volume_ids = json.loads(secrets.get("volume_ids") or "[]")
        if snapshot_ids and volume_ids:
            vol = await depot_volume.obtenir(ctx, volume_ids[0])
            identifiants = await identifiants_espace(ctx, vol.espaceId)
            restaures = [
                await asyncio.to_thread(
                    amont_cinder().restaurer_snapshot,
                    sid,
                    f"restore-{point.id[:8]}",
                    identifiants=identifiants,
                )
                for sid in snapshot_ids
            ]
            await restaurations.definir_secrets(
                ctx,
                restauration_id,
                {"volumes_restaures": json.dumps([r["id"] for r in restaures])},
            )
        await restaurations.definir_statut(ctx, restauration_id, "done")


async def conformite(ctx: Contexte) -> list[dict[str, Any]]:
    plans = await depot.tous(ctx)
    pts = await points.tous(ctx)
    restaure = await restaurations.tous(ctx)
    lignes: list[dict[str, Any]] = []
    for plan in plans:
        pts_plan = [p for p in pts if p.planId == plan.id]
        destinations = {p.destination for p in pts_plan} | {d.type for d in plan.destinations}
        copies = len(pts_plan)
        supports = len(destinations)
        hors_site = any(d in {"autre_site", "immuable"} for d in destinations)
        if plan.dernierResultat == "echec":
            protection = "echec"
        elif (copies >= 3 or plan.mode == "complete") and supports >= 2 and hors_site:
            protection = "protegee"
        else:
            protection = "non_protegee"
        dernier_succes = next(
            (p.date for p in sorted(pts_plan, key=lambda x: x.date, reverse=True) if p.verifie),
            None,
        )
        test = [r for r in restaure if r.pointId in {p.id for p in pts_plan}]
        dernier_test = None
        if test:
            dernier_test = m.DernierTestRestauration(
                date=max(r.demandeeLe for r in test), succes=False, dureeMin=0
            )
        lignes.append(
            {
                "ressourceId": plan.scope.valeur,
                "ressourceNom": plan.scope.valeur,
                "type": plan.scope.type,
                "protection": protection,
                "dernierSucces": dernier_succes,
                "rpoConstateMin": int((maintenant() - dernier_succes).total_seconds() // 60)
                if dernier_succes
                else None,
                "regle321": m.Regle321(
                    copies=copies >= 3 or plan.mode == "complete",
                    supports=supports >= 2,
                    horsSite=hors_site,
                ),
                "dernierTestRestauration": dernier_test,
            }
        )
    return lignes
