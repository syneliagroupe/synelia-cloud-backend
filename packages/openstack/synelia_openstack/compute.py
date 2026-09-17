"""Nova + Glance : gabarits, images, serveurs."""

from __future__ import annotations

import re
from typing import Any

from synelia_kernel.ids import nouvel_id

GABARITS = [
    {
        "id": "s1.small",
        "nom": "S1 Small",
        "vcpu": 1,
        "ramGo": 2,
        "diskGo": 20,
        "famille": "economique",
        "prixMensuel": 9000,
        "sitesDisponibles": ["ABJ", "GBM"],
    },
    {
        "id": "g1.medium",
        "nom": "G1 Medium",
        "vcpu": 2,
        "ramGo": 4,
        "diskGo": 40,
        "famille": "generique",
        "prixMensuel": 19000,
        "sitesDisponibles": ["ABJ", "GBM"],
    },
    {
        "id": "g1.large",
        "nom": "G1 Large",
        "vcpu": 4,
        "ramGo": 8,
        "diskGo": 80,
        "famille": "generique",
        "prixMensuel": 38000,
        "sitesDisponibles": ["ABJ", "GBM"],
    },
    {
        "id": "g1.xlarge",
        "nom": "G1 XLarge",
        "vcpu": 8,
        "ramGo": 16,
        "diskGo": 160,
        "famille": "generique",
        "prixMensuel": 76000,
        "sitesDisponibles": ["ABJ"],
    },
    {
        "id": "c1.large",
        "nom": "C1 Calcul",
        "vcpu": 8,
        "ramGo": 8,
        "diskGo": 80,
        "famille": "calcul",
        "prixMensuel": 64000,
        "sitesDisponibles": ["ABJ"],
    },
    {
        "id": "m1.large",
        "nom": "M1 Mémoire",
        "vcpu": 4,
        "ramGo": 32,
        "diskGo": 80,
        "famille": "memoire",
        "prixMensuel": 72000,
        "sitesDisponibles": ["ABJ"],
    },
    {
        "id": "gpu1.a10",
        "nom": "GPU A10",
        "vcpu": 8,
        "ramGo": 32,
        "diskGo": 200,
        "famille": "gpu",
        "prixMensuel": 420000,
        "sitesDisponibles": ["ABJ"],
    },
]
IMAGES = [
    {
        "id": "ubuntu-24.04",
        "nom": "Ubuntu Server",
        "famille": "linux",
        "version": "24.04 LTS",
        "architecture": "x86_64",
        "tailleGo": 3,
        "sitesDisponibles": ["ABJ", "GBM"],
        "logicielsPreinstallables": ["docker", "nginx", "postgresql"],
    },
    {
        "id": "debian-12",
        "nom": "Debian",
        "famille": "linux",
        "version": "12",
        "architecture": "x86_64",
        "tailleGo": 2,
        "sitesDisponibles": ["ABJ", "GBM"],
    },
    {
        "id": "rocky-9",
        "nom": "Rocky Linux",
        "famille": "linux",
        "version": "9",
        "architecture": "x86_64",
        "tailleGo": 2,
        "sitesDisponibles": ["ABJ", "GBM"],
    },
    {
        "id": "alma-9",
        "nom": "AlmaLinux",
        "famille": "linux",
        "version": "9",
        "architecture": "x86_64",
        "tailleGo": 2,
        "sitesDisponibles": ["ABJ"],
    },
    {
        "id": "windows-2022",
        "nom": "Windows Server",
        "famille": "windows",
        "version": "2022",
        "architecture": "x86_64",
        "tailleGo": 12,
        "licencePayante": True,
        "coutLicenceMensuel": 25000,
        "sitesDisponibles": ["ABJ"],
    },
    {
        "id": "freebsd-14",
        "nom": "FreeBSD",
        "famille": "bsd",
        "version": "14",
        "architecture": "x86_64",
        "tailleGo": 2,
        "sitesDisponibles": ["ABJ"],
    },
]


class ComputeSimule:
    def gabarits(self) -> list[dict[str, Any]]:
        return GABARITS

    def images(self) -> list[dict[str, Any]]:
        return IMAGES

    def creer_serveur(self, **kw: Any) -> dict[str, Any]:
        return {
            "id": f"srv-{nouvel_id()[:8]}",
            "statut": "ACTIVE",
            "ip_privee": f"10.{hash(kw.get('nom', '')) % 250}.0.{hash(kw.get('image_id', '')) % 250 + 2}",
        }

    def assurer_keypair(
        self, nom: str, cle_publique: str, identifiants: dict[str, Any] | None = None
    ) -> str:
        return nom

    def action(self, serveur_id: str, action: str) -> None:
        return None

    def supprimer_serveur(self, serveur_id: str) -> None:
        return None

    def redimensionner(self, serveur_id: str, gabarit_id: str) -> None:
        return None

    def instantane(self, serveur_id: str, nom: str) -> str:
        return f"img-{nouvel_id()[:8]}"

    def restaurer(self, serveur_id: str, image_id: str) -> None:
        return None

    def statut_image(self, image_id: str, identifiants: dict[str, Any] | None = None) -> str:
        return "active"

    def supprimer_image(self, image_id: str) -> None:
        return None

    def statut_serveur(self, serveur_id: str, identifiants: dict[str, Any] | None = None) -> str:
        return "ACTIVE"

    def console(self, serveur_id: str) -> str:
        return f"https://console.synelia.cloud/novnc/{serveur_id}?token={nouvel_id()}"

    def journaux(self, serveur_id: str, lignes: int = 20) -> list[str]:
        return [f"[cloud-init] ligne {i} — démarrage nominal" for i in range(1, lignes + 1)]

    def diagnostics(self, serveur_id: str) -> dict[str, Any] | None:
        return None

    def capacite_plateforme(self) -> dict[str, Any] | None:
        """Capacité agrégée réelle du parc d'hyperviseurs Nova — `None` en simulation :
        rien à interroger, l'appelant garde alors ses valeurs de secours."""
        return None


# Nova refuse (409) une action `stop`/`start` quand le serveur est déjà dans l'état visé
# (`InstanceInvalidState`, message « Cannot '<verbe>' instance ... while it is in vm_state
# <etat> ») — cf. `_deja_dans_etat_cible`. Pas d'entrée pour `redemarrage` : un reboot n'a pas
# d'« état déjà atteint » analogue (un 409 dessus signale un vrai conflit, ex. une autre
# opération en cours), donc rien à absorber.
# Deux vocabulaires distincts côté Nova pour le même état : `vm_state` (interne, utilisé dans le
# texte du message d'erreur — « stopped »/« active ») et `status` (façade API, exposé par
# `Server.status` — « SHUTOFF »/« ACTIVE », le même que celui déjà attendu par `wait_for_server`
# plus bas). Les deux tables ci-dessous font chacune la correspondance dans son vocabulaire ;
# la confirmation en direct (`get_server(...).status`) doit être comparée à la bonne.
_VM_STATE_MESSAGE_CIBLE = {"arret": "stopped", "demarrage": "active"}
_STATUT_API_CIBLE = {"arret": "SHUTOFF", "demarrage": "ACTIVE"}


class ComputeOpenStack(ComputeSimule):
    def _c(self):  # type: ignore[no-untyped-def]
        from synelia_openstack.fabrique import connexion

        return connexion()

    def gabarits(self) -> list[dict[str, Any]]:
        out = []
        for f in self._c().compute.flavors():
            if not f.is_public:
                # Gabarits privés (ex. l'amphora Octavia) : pas accessibles depuis le
                # projet tenant, Nova rejette la création de serveur avec ce flavor.
                continue
            extra = f.extra_specs or {}
            out.append(
                {
                    "id": f.id,
                    "nom": f.name,
                    "vcpu": f.vcpus,
                    "ramGo": max(1, f.ram // 1024),
                    "diskGo": f.disk,
                    "famille": extra.get("synelia:famille", "generique"),
                    "prixMensuel": int(extra.get("synelia:prix", f.vcpus * 9500)),
                    "sitesDisponibles": ["ABJ"],
                }
            )
        return out

    def images(self) -> list[dict[str, Any]]:
        out = []
        for i in self._c().image.images(visibility="public"):
            if "amphora" in (i.tags or []):
                # Image d'appliance interne d'Octavia (load balancer), pas un OS pour un
                # client : Glance la publie en `visibility=public` (convention amont), mais
                # elle est taguée `amphora` — même angle mort que le gabarit `is_public` déjà
                # filtré ci-dessus pour la même raison (constaté en direct : elle apparaissait
                # dans le catalogue images à côté d'ubuntu-24.04).
                continue
            out.append(
                {
                    "id": i.id,
                    "nom": i.name,
                    "famille": "windows"
                    if "windows" in (i.os_distro or i.name).lower()
                    else "linux",
                    "version": i.os_version or "",
                    "architecture": i.architecture or "x86_64",
                    "tailleGo": max(1, (i.size or 0) // 2**30),
                    "sitesDisponibles": ["ABJ"],
                }
            )
        return out

    def _connexion_pour(self, identifiants: dict[str, Any] | None):  # type: ignore[no-untyped-def]
        ident = identifiants or {}
        if ident.get("application_credential_id"):
            from synelia_openstack.fabrique import connexion_avec

            return connexion_avec(
                ident["application_credential_id"], ident["application_credential_secret"]
            )
        return self._c()

    def creer_serveur(self, **kw: Any) -> dict[str, Any]:
        c = self._connexion_pour(kw.get("identifiants"))
        params: dict[str, Any] = {
            "name": kw["nom"],
            "image_id": kw["image_id"],
            "flavor_id": kw["gabarit_id"],
            "networks": [{"uuid": kw["reseau_id"]}] if kw.get("reseau_id") else "auto",
            "metadata": {"synelia_org": str(kw.get("org_id") or ""), "synelia_espace": str(kw.get("espace_id") or "")},
        }
        if kw.get("cle_ssh"):
            params["key_name"] = kw["cle_ssh"]
        if kw.get("cloud_init"):
            # Nova exige que `user_data` soit du Base64 côté client (openstacksdk ne l'encode
            # pas lui-même, contrairement au CLI `openstack server create --user-data`) : sans
            # cet encodage la valeur est silencieusement perdue (aucune erreur, mais l'instance
            # démarre sans cloud-init).
            import base64

            params["user_data"] = base64.b64encode(kw["cloud_init"].encode()).decode()
        if kw.get("groupes_securite"):
            # openstacksdk/Nova attendent une liste de noms de groupe, pas d'id — la ressource
            # `security_group` du SDK ne s'obtient qu'en listant/cherchant par id, contrairement
            # au réseau qui accepte directement un `uuid`. Une résolution silencieusement
            # ignorée (groupe supprimé entre-temps côté Neutron) ne doit pas faire échouer la
            # création : Nova pose alors le groupe `default` du projet, jamais aucun groupe.
            noms = []
            for gid in kw["groupes_securite"]:
                grp = c.network.find_security_group(gid, ignore_missing=True)
                if grp:
                    noms.append(grp.name)
            if noms:
                params["security_groups"] = [{"name": n} for n in noms]
        s = c.compute.create_server(**params)
        try:
            s = c.compute.wait_for_server(s, wait=600)
        except Exception as exc:
            from synelia_openstack.erreurs import traduire

            # Nova a accepté la création mais le build a échoué avant que l'appelant ait pu
            # persister `serveur_id` (ex. `NoValidHost` : mis en ERROR immédiatement, jamais
            # schedulé) — sans nettoyage ici, cette instance reste orpheline pour toujours,
            # introuvable par la suite puisqu'aucun identifiant n'a été enregistré côté
            # application pour la retrouver. On la supprime nous-mêmes, avec la même
            # connexion que celle qui l'a créée (le bon projet OpenStack, ex. Espace Cloud
            # scellé par application credential), avant de relayer l'erreur d'origine.
            try:
                c.compute.delete_server(s.id, ignore_missing=True)
                c.compute.wait_for_delete(c.compute.get_server(s.id), wait=120)
            except Exception:  # noqa: BLE001, S110 — best effort, l'erreur d'origine prime
                pass
            from synelia_kernel import erreurs as _e

            if isinstance(exc, _e.AppError):
                raise
            raise traduire(exc, "Machine virtuelle") from None
        ip = next((a["addr"] for nets in (s.addresses or {}).values() for a in nets if a.get("version") == 4), None)
        return {"id": s.id, "statut": s.status, "ip_privee": ip}

    def assurer_keypair(
        self, nom: str, cle_publique: str, identifiants: dict[str, Any] | None = None
    ) -> str:
        """Importe la clé publique donnée comme keypair Nova `nom`, si elle n'existe pas déjà
        (idempotent : appelé à chaque création d'hébergement, une seule fois réellement créé
        côté Nova). On importe une clé déjà générée par nous — jamais celle générée par Nova
        elle-même (qui ne renvoie la clé privée qu'une fois, à la création). Le keypair Nova
        est scellé à un projet/utilisateur : il doit être créé avec la **même** connexion
        (`identifiants` — l'application credential de la zone VPS) que celle qui créera le
        serveur, sinon Nova répond `Invalid key_name provided` (keypair invisible depuis le
        projet du serveur, alors qu'il existe bien ailleurs — vu en direct sur le lab)."""
        c = self._connexion_pour(identifiants)
        existante = c.compute.find_keypair(nom, ignore_missing=True)
        if existante is None:
            c.compute.create_keypair(name=nom, public_key=cle_publique)
        return nom

    def _deja_dans_etat_cible(self, exc: Exception, action: str, c, serveur_id: str) -> bool:  # type: ignore[no-untyped-def]
        """Un `stop`/`start` peut échouer 409 alors que le serveur est déjà dans l'état visé —
        dérive DB/Nova (coupure lab avec redémarrage des invités, arrêt fait hors plateforme,
        travail antérieur retombé sans mettre à jour le statut…) constatée en direct (`vm.power.
        stop` sur une VM déjà `SHUTOFF` : 409 `Cannot 'stop' instance ... while it is in
        vm_state stopped`). Ce n'est un succès de fait que si (a) c'est bien ce conflit précis
        (message Nova `InstanceInvalidState` mentionnant l'état visé par CETTE action, pas un
        409 générique — une vraie collision, ex. une autre opération en cours, garde un message
        différent et continue de remonter), et (b) une relecture Nova fraîche confirme l'état
        réel — on ne se fie pas qu'au texte du message, dont le format peut varier selon la
        version Nova."""
        vm_state_cible = _VM_STATE_MESSAGE_CIBLE.get(action)
        if vm_state_cible is None or type(exc).__name__ != "ConflictException":
            return False
        if not re.search(rf"while it is in vm_state {vm_state_cible}\b", str(exc), re.IGNORECASE):
            return False
        srv = c.compute.get_server(serveur_id)
        return str(srv.status).upper() == _STATUT_API_CIBLE[action]

    def action(self, serveur_id: str, action: str) -> None:
        from synelia_openstack.erreurs import traduire

        c = self._c()
        try:
            if action == "arret":
                c.compute.stop_server(serveur_id)
                c.compute.wait_for_server(
                    c.compute.get_server(serveur_id), status="SHUTOFF", wait=300
                )
            elif action == "demarrage":
                c.compute.start_server(serveur_id)
                c.compute.wait_for_server(
                    c.compute.get_server(serveur_id), status="ACTIVE", wait=300
                )
            elif action == "redemarrage":
                c.compute.reboot_server(serveur_id, "SOFT")
                c.compute.wait_for_server(
                    c.compute.get_server(serveur_id), status="ACTIVE", wait=300
                )
            else:
                msg = f"Action inconnue : {action}."
                raise ValueError(msg)
        except Exception as exc:
            from synelia_kernel import erreurs as _e

            if isinstance(exc, _e.AppError):
                raise
            if self._deja_dans_etat_cible(exc, action, c, serveur_id):
                # Déjà dans l'état visé côté Nova : succès de fait, pas un échec à remonter
                # (même motif que `supprimer_serveur` — `NotFound` = déjà supprimé = succès).
                return
            raise traduire(exc, "Machine virtuelle") from None

    def supprimer_serveur(self, serveur_id: str) -> None:
        # `delete_server(ignore_missing=True)` seul ne confirme que l'acceptation de la
        # requête par Nova, pas la disparition réelle de l'instance : un serveur déjà en
        # ERROR (jamais schedulé, hyperviseur injoignable, etc.) peut accepter le DELETE
        # sans jamais être réellement supprimé — l'appelant marquerait alors la suppression
        # « ok » alors que la VM (et sa facturation) survit. On attend donc explicitement
        # sa disparition avant de rendre la main.
        from synelia_openstack.erreurs import traduire

        c = self._c()
        try:
            c.compute.delete_server(serveur_id, ignore_missing=True)
            c.compute.wait_for_delete(c.compute.get_server(serveur_id), wait=120)
        except Exception as exc:
            from synelia_kernel import erreurs as _e

            if isinstance(exc, _e.AppError):
                raise
            if "NotFound" in type(exc).__name__:
                # Couvre `ResourceNotFound` (helpers `find_*`) et `NotFoundException` (`get_*`
                # direct, ex. lors d'une reprise de travail qui rejoue cette étape après un
                # premier passage déjà réussi — la VM n'existe alors plus du tout, ce qui est
                # justement le succès recherché, pas un échec).
                return
            raise traduire(exc, "Machine virtuelle") from None

    def redimensionner(self, serveur_id: str, gabarit_id: str) -> None:
        c = self._c()
        c.compute.resize_server(serveur_id, gabarit_id)
        c.compute.wait_for_server(
            c.compute.get_server(serveur_id), status="VERIFY_RESIZE", wait=600
        )
        c.compute.confirm_server_resize(serveur_id)

    def instantane(self, serveur_id: str, nom: str) -> str:
        return self._c().compute.create_server_image(serveur_id, nom, wait=True).id

    def restaurer(self, serveur_id: str, image_id: str) -> None:
        """Restaure `serveur_id` depuis l'instantané Glance `image_id` (`rebuild_server`) —
        remplace le disque du serveur en place, sans en recréer un nouveau (IP, volumes de
        données et association réseau inchangés)."""
        from synelia_openstack.erreurs import traduire

        c = self._c()
        try:
            c.compute.rebuild_server(serveur_id, image_id)
            c.compute.wait_for_server(c.compute.get_server(serveur_id), status="ACTIVE", wait=600)
        except Exception as exc:
            from synelia_kernel import erreurs as _e

            if isinstance(exc, _e.AppError):
                raise
            raise traduire(exc, "Machine virtuelle") from None

    def statut_image(self, image_id: str, identifiants: dict[str, Any] | None = None) -> str:
        """Statut Glance réel de l'image : `active` seulement si le snapshot est réellement
        restaurable — `queued`/`saving` (encore en cours), `killed`/`deleted` (perdue),
        `absente` si Glance ne la connaît plus du tout (purge de rétention, suppression)."""
        c = self._connexion_pour(identifiants)
        img = c.image.find_image(image_id, ignore_missing=True)
        return str(img.status) if img else "absente"

    def supprimer_image(self, image_id: str) -> None:
        """Supprime réellement l'image Glance d'un instantané — sans cet appel, `DELETE
        .../instantanes/{id}` ne retirait que la ligne DB (constaté en direct : l'image Glance
        d'un snapshot supprimé depuis l'interface restait `active`, capacité jamais rendue au
        cluster). `ignore_missing=True` : une image déjà purgée (politique de rétention Glance,
        ou double-suppression après une reprise de travail) est déjà le résultat recherché, pas
        un échec."""
        self._c().image.delete_image(image_id, ignore_missing=True)

    def statut_serveur(self, serveur_id: str, identifiants: dict[str, Any] | None = None) -> str:
        """Statut Nova réel du serveur — `absente` si Nova ne le connaît plus du tout (supprimé
        hors bande, ex. nettoyage manuel du lab) : permet d'échouer vite et clairement plutôt
        que de tenter un SSH sur une IP dont la VM n'existe plus (`Connection timed out`, ~20 s,
        avant de comprendre que la VM a disparu — vécu en direct sur un hébergement orphelin)."""
        c = self._connexion_pour(identifiants)
        srv = c.compute.find_server(serveur_id, ignore_missing=True)
        return str(srv.status) if srv else "absente"

    # Vhost Apache public sur dev01, reverse-proxy TLS (Let's Encrypt) vers le
    # nova-novncproxy interne du lab (VIP kolla 192.168.26.234:6080, une seule instance pour
    # toutes les VM, différenciées par le `token` de session dans la query) — injoignable tel
    # quel depuis Internet, cf. docs/runbooks/lab-openstack.md.
    _CONSOLE_HOTE_PUBLIC = "console.synelia.dev01.ovh.smile.ci"

    def console(self, serveur_id: str) -> str:
        """Nova renvoie l'URL novnc de son proxy interne (adresse privée du lab, jamais
        atteignable par un client réel hors du réseau du lab) : on ne réécrit que le
        schéma/host/port vers le vhost public de dev01, jamais le chemin ni le `token` de
        session émis par Nova — le jeton casse si altéré."""
        from urllib.parse import urlsplit, urlunsplit

        brute = self._c().compute.create_console(serveur_id, console_type="novnc")["url"]
        parties = urlsplit(brute)
        return urlunsplit(parties._replace(scheme="https", netloc=self._CONSOLE_HOTE_PUBLIC))

    def journaux(self, serveur_id: str, lignes: int = 20) -> list[str]:
        from synelia_openstack.erreurs import traduire

        try:
            sortie = self._c().compute.get_server_console_output(serveur_id, length=lignes)
        except Exception as exc:
            from synelia_kernel import erreurs as _e

            if isinstance(exc, _e.AppError):
                raise
            raise traduire(exc, "Machine virtuelle") from None
        return ((sortie or {}).get("output") or "").splitlines()

    def diagnostics(self, serveur_id: str) -> dict[str, Any] | None:
        """Diagnostics bruts de l'hyperviseur (`GET /servers/{id}/diagnostics`) : compteurs
        cumulés de temps CPU, mémoire et E/S disque/réseau directement depuis libvirt/QEMU —
        pas la forme structurée (`>= microversion 2.48`), le pilote libvirt de ce lab répond
        toujours l'ancien dict à plat (`cpuN_time`, `memory-*`, `vdX_read`/`write`,
        `<tap>_rx`/`tx`), vérifié en direct. `None` si le serveur n'est pas actif (Nova répond
        409 hors `ACTIVE`) ou a disparu (404) : pas une exception qui casserait la tuile pour
        une VM éteinte, la même politique que `statut_serveur`."""
        try:
            r = self._c().compute.get(f"/servers/{serveur_id}/diagnostics")
            r.raise_for_status()
        except Exception:  # noqa: BLE001 — VM éteinte/absente : dégradé, pas une panne
            return None
        return dict(r.json())

    def capacite_plateforme(self) -> dict[str, Any] | None:
        """Capacité agrégée réelle du parc d'hyperviseurs Nova (`/os-hypervisors/statistics`) :
        depuis la microversion 2.88 le détail par hyperviseur ne renvoie plus vcpus/mémoire
        (remplacés par l'API Placement), mais l'agrégat de statistiques reste exposé et donne
        la vraie capacité du lab (jamais les capacités figées d'un jeu de données de départ)."""
        r = self._c().compute.get("/os-hypervisors/statistics")
        r.raise_for_status()
        s = r.json()["hypervisor_statistics"]
        return {
            "hosts": int(s["count"]),
            "vcpu": int(s["vcpus"]),
            "ramGo": max(1, round(s["memory_mb"] / 1024)),
            "stockageTo": round(s["local_gb"] / 1024, 3),
        }
