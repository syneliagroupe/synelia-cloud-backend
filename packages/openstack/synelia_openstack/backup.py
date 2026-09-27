"""Backup : plan de sauvegarde, points de restauration (Karbor/Cinder), restauration.

Paire `BackupSimule` / `BackupOpenStack`. En simulation, tout réussit instantanément ; en
production, les opérations passent par openstacksdk. Aucune connexion réelle en test."""

from __future__ import annotations

from typing import Any

from synelia_kernel.ids import nouvel_id


class BackupSimule:
    def executer_plan(self, plan: str, ressources: int) -> dict[str, Any]:
        return {
            "id": f"ra-{nouvel_id()[:8]}",
            "statut": "ok",
            "taille_go": round(2.4 + ressources * 0.6, 1),
        }

    def creer_snapshot(
        self, resource_id: str, immuable_jusquau: str | None = None
    ) -> dict[str, Any]:
        return {"snapshot_id": f"snap-{nouvel_id()[:8]}", "immuable_jusquau": immuable_jusquau}

    def verifier_point(self, point_id: str) -> dict[str, Any]:
        return {
            "point_id": point_id,
            "verifie": True,
            "detail": "Cohérence des blocs et du catalogue vérifiée.",
        }

    def restaurer(self, point_id: str, cible: str | None) -> dict[str, Any]:
        return {"restauration_id": f"rs-{nouvel_id()[:8]}", "statut": "ok"}


class BackupOpenStack(BackupSimule):
    def _c(self):  # type: ignore[no-untyped-def]
        from synelia_openstack.fabrique import connexion

        return connexion()

    def executer_plan(self, plan: str, ressources: int) -> dict[str, Any]:
        c = self._c()
        # `hasattr(c, "backup")` était TOUJOURS vrai : `ServiceDescription.__get__`
        # d'openstacksdk ne lève jamais `AttributeError` pour un service absent du
        # catalogue Keystone, il renvoie un shim désactivé (`_ServiceDisabledProxyShim`)
        # — l'appel suivant plantait alors sur ce shim, systématiquement, dès qu'aucune
        # Cinder volume n'était rattachée à la ressource visée (cf. mémoire
        # `backup-service-hasattr-always-true`). `has_service()` interroge le vrai
        # catalogue Keystone et renvoie honnêtement `False` si le service (Karbor/Freezer)
        # n'y figure pas, au lieu de planter.
        if c.has_service("backup"):
            c.backup.create_plan_run(plan_id=plan)
        return super().executer_plan(plan, ressources)
