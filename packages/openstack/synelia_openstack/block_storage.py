"""Cinder : volumes de stockage en bloc (création, attachement, extension, suppression)."""

from __future__ import annotations

from typing import Any

from synelia_kernel.ids import nouvel_id


class BlockStorageSimule:
    def creer_volume(self, **kw: Any) -> dict[str, Any]:
        return {
            "id": f"vol-{nouvel_id()[:8]}",
            "statut": "available",
            "taille_go": kw.get("taille_go", 10),
        }

    def attacher(
        self,
        volume_id: str,
        vm_id: str,
        montage: str | None = None,
        identifiants: dict[str, Any] | None = None,
    ) -> None:
        return None

    def detacher(
        self, volume_id: str, vm_id: str, identifiants: dict[str, Any] | None = None
    ) -> None:
        return None

    def etendre(
        self, volume_id: str, taille_go: int, identifiants: dict[str, Any] | None = None
    ) -> None:
        return None

    def supprimer(self, volume_id: str, identifiants: dict[str, Any] | None = None) -> None:
        return None

    def creer_snapshot(
        self, volume_id: str, nom: str, identifiants: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return {"id": f"snap-{nouvel_id()[:8]}", "statut": "available"}

    def supprimer_snapshot(
        self, snapshot_id: str, identifiants: dict[str, Any] | None = None
    ) -> None:
        return None

    def restaurer_snapshot(
        self, snapshot_id: str, nom: str, identifiants: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return {"id": f"vol-{nouvel_id()[:8]}", "statut": "available"}

    def statut_snapshot(
        self, snapshot_id: str, identifiants: dict[str, Any] | None = None
    ) -> str:
        return "available"


class BlockStorageOpenStack(BlockStorageSimule):
    def _c(self):  # type: ignore[no-untyped-def]
        from synelia_openstack.fabrique import connexion

        return connexion()

    def _connexion(self, identifiants: dict[str, Any] | None = None):  # type: ignore[no-untyped-def]
        """Connexion scellée au projet de l'Espace Cloud si un *application credential* est fourni."""
        ident = identifiants or {}
        if ident.get("application_credential_id"):
            from synelia_openstack.fabrique import connexion_avec

            return connexion_avec(
                ident["application_credential_id"], ident["application_credential_secret"]
            )
        return self._c()

    def creer_volume(self, **kw: Any) -> dict[str, Any]:
        # Cinder ne connaît pas de champ `encrypted` sur la création : le chiffrement est un
        # attribut du `volume_type` (posé côté admin), pas un indicateur par requête — l'envoyer
        # fait rejeter l'appel par l'API réelle (« Additional properties are not allowed »).
        c = self._connexion(kw.get("identifiants"))
        v = c.block_storage.create_volume(
            size=kw.get("taille_go", 10),
            name=kw.get("nom"),
            volume_type=kw.get("classe"),
        )
        v = c.block_storage.wait_for_status(v, "available", wait=600)
        return {"id": v.id, "statut": v.status, "taille_go": v.size}

    def attacher(
        self,
        volume_id: str,
        vm_id: str,
        montage: str | None = None,
        identifiants: dict[str, Any] | None = None,
    ) -> None:
        # Attachement réel côté Nova (pas `block_storage.attach_volume`, qui n'est que l'action
        # bas niveau Cinder `os-attach` sans lien avec l'instance) : Nova réserve puis attache le
        # volume à l'hyperviseur et notifie Cinder.
        self._connexion(identifiants).compute.create_volume_attachment(vm_id, volume=volume_id)

    def detacher(
        self, volume_id: str, vm_id: str, identifiants: dict[str, Any] | None = None
    ) -> None:
        self._connexion(identifiants).compute.delete_volume_attachment(
            vm_id, volume_id, ignore_missing=True
        )

    def etendre(
        self, volume_id: str, taille_go: int, identifiants: dict[str, Any] | None = None
    ) -> None:
        self._connexion(identifiants).block_storage.extend_volume(volume_id, taille_go)

    def supprimer(self, volume_id: str, identifiants: dict[str, Any] | None = None) -> None:
        # Même raison que `ComputeOpenStack.supprimer_serveur` : `ignore_missing=True` seul ne
        # confirme que l'acceptation du DELETE par Cinder, pas la disparition réelle du volume
        # (ex. déjà en `error`/`error_deleting`) — sans vérification, l'appelant marquerait la
        # suppression « ok » alors que le volume (et sa facturation) survit.
        from synelia_openstack.erreurs import traduire

        c = self._connexion(identifiants)
        try:
            c.block_storage.delete_volume(volume_id, ignore_missing=True)
            c.block_storage.wait_for_delete(c.block_storage.get_volume(volume_id), wait=120)
        except Exception as exc:
            from synelia_kernel import erreurs as _e

            if isinstance(exc, _e.AppError):
                raise
            if type(exc).__name__ == "ResourceNotFound":
                return  # déjà absent avant même la vérification : succès
            raise traduire(exc, "Volume") from None

    def creer_snapshot(
        self, volume_id: str, nom: str, identifiants: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        # `force=True` : un volume de données réellement utilisé pour une sauvegarde est presque
        # toujours attaché (`in-use`), pas `available` — sans ce drapeau, Cinder refuse
        # l'instantané (« Invalid volume: ... status must be available »), ce qui rendrait le
        # chemin de sauvegarde réel inutilisable dans le cas même qu'il est censé couvrir.
        c = self._connexion(identifiants)
        snap = c.block_storage.create_snapshot(volume_id=volume_id, name=nom, force=True)
        snap = c.block_storage.wait_for_status(snap, "available", wait=600)
        return {"id": snap.id, "statut": snap.status}

    def supprimer_snapshot(
        self, snapshot_id: str, identifiants: dict[str, Any] | None = None
    ) -> None:
        from synelia_openstack.erreurs import traduire

        c = self._connexion(identifiants)
        try:
            c.block_storage.delete_snapshot(snapshot_id, ignore_missing=True)
            c.block_storage.wait_for_delete(c.block_storage.get_snapshot(snapshot_id), wait=120)
        except Exception as exc:
            from synelia_kernel import erreurs as _e

            if isinstance(exc, _e.AppError):
                raise
            if type(exc).__name__ == "ResourceNotFound":
                return  # déjà absent avant même la vérification : succès
            raise traduire(exc, "Instantané de volume") from None

    def restaurer_snapshot(
        self, snapshot_id: str, nom: str, identifiants: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        # Cinder sait créer un volume neuf directement à partir d'un instantané.
        c = self._connexion(identifiants)
        v = c.block_storage.create_volume(snapshot_id=snapshot_id, name=nom)
        v = c.block_storage.wait_for_status(v, "available", wait=600)
        return {"id": v.id, "statut": v.status, "taille_go": v.size}

    def statut_snapshot(
        self, snapshot_id: str, identifiants: dict[str, Any] | None = None
    ) -> str:
        return str(self._connexion(identifiants).block_storage.get_snapshot(snapshot_id).status)
