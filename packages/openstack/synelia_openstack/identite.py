"""Tenancy : organisation → domaine Keystone, Espace → projet + réseau + quotas + application credential."""

from __future__ import annotations

import ipaddress
from typing import Any

from synelia_kernel.ids import jeton_opaque, nouvel_id


class IdentiteSimule:
    """Aucun amont : renvoie des identifiants plausibles, instantanément."""

    def creer_domaine(self, nom: str) -> str:
        return f"dom-{nouvel_id()[:8]}"

    def creer_projet(self, domaine_id: str, nom: str, region: str) -> str:
        return f"proj-{nouvel_id()[:8]}"

    def poser_quotas(self, projet_id: str, vcpu: int, ram_go: int, stockage_to: float) -> None:
        return None

    def creer_reseau(self, projet_id: str, nom: str, cidr: str) -> dict[str, Any]:
        ipaddress.ip_network(cidr)
        return {
            "reseau_id": f"net-{nouvel_id()[:8]}",
            "sous_reseau_id": f"sub-{nouvel_id()[:8]}",
            "routeur_id": f"rtr-{nouvel_id()[:8]}",
        }

    def supprimer_reseau(self, reseau_id: str, routeur_id: str) -> None:
        return None

    def creer_reseau_secondaire(self, projet_id: str | None, nom: str, cidr: str) -> dict[str, Any]:
        """Réseau interne supplémentaire au sein d'un projet déjà existant (pas de routeur)."""
        ipaddress.ip_network(cidr)
        return {"reseau_id": f"net-{nouvel_id()[:8]}", "sous_reseau_id": f"sub-{nouvel_id()[:8]}"}

    def supprimer_reseau_secondaire(self, reseau_id: str) -> None:
        return None

    def creer_ip_flottante(self, projet_id: str | None) -> dict[str, Any]:
        return {"id": f"fip-{nouvel_id()[:8]}", "adresse": None}

    def supprimer_ip_flottante(self, ip_id: str) -> None:
        return None

    def associer_ip_flottante(self, ip_id: str, serveur_id: str) -> str | None:
        return None

    def dissocier_ip_flottante(self, ip_id: str) -> None:
        return None

    def creer_application_credential(self, projet_id: str, domaine_id: str | None = None) -> dict[str, str]:
        return {"id": f"ac-{nouvel_id()[:8]}", "secret": jeton_opaque(24)}

    def supprimer_projet(self, projet_id: str) -> None:
        return None


class IdentiteOpenStack(IdentiteSimule):
    """openstacksdk : Keystone + Neutron + quotas Nova/Cinder/Neutron."""

    def _conn(self, region: str | None = None):
        from synelia_openstack.fabrique import connexion

        return connexion(region)

    def creer_domaine(self, nom: str) -> str:
        c = self._conn()
        d = c.identity.find_domain(nom) or c.identity.create_domain(name=nom, enabled=True)
        return d.id

    def creer_projet(self, domaine_id: str, nom: str, region: str) -> str:
        c = self._conn(region)
        p = c.identity.find_project(nom, domain_id=domaine_id) or c.identity.create_project(
            name=nom, domain_id=domaine_id, enabled=True
        )
        return p.id

    def poser_quotas(self, projet_id: str, vcpu: int, ram_go: int, stockage_to: float) -> None:
        c = self._conn()
        c.set_compute_quotas(projet_id, cores=vcpu, ram=ram_go * 1024, instances=max(10, vcpu))
        c.set_volume_quotas(projet_id, gigabytes=int(stockage_to * 1024), volumes=max(20, vcpu * 2))

    def _reseau_externe(self, c) -> Any:  # type: ignore[no-untyped-def]
        return next((n for n in c.network.networks(is_router_external=True)), None)

    def creer_reseau(self, projet_id: str, nom: str, cidr: str) -> dict[str, Any]:
        c = self._conn()
        net = c.network.create_network(name=nom, project_id=projet_id)
        sub = c.network.create_subnet(
            name=f"{nom}-sub", network_id=net.id, ip_version=4, cidr=cidr, project_id=projet_id
        )
        ext = self._reseau_externe(c)
        rtr = c.network.create_router(
            name=f"{nom}-rtr",
            project_id=projet_id,
            external_gateway_info={"network_id": ext.id} if ext else None,
        )
        c.network.add_interface_to_router(rtr, subnet_id=sub.id)
        return {"reseau_id": net.id, "sous_reseau_id": sub.id, "routeur_id": rtr.id}

    def supprimer_reseau(self, reseau_id: str, routeur_id: str) -> None:
        c = self._conn()
        rtr = c.network.find_router(routeur_id, ignore_missing=True)
        if rtr is not None:
            for port in c.network.ports(device_id=rtr.id):
                if port.device_owner and "router_interface" in port.device_owner:
                    c.network.remove_interface_from_router(
                        rtr, subnet_id=port.fixed_ips[0]["subnet_id"]
                    )
            c.network.delete_router(rtr, ignore_missing=True)
        c.network.delete_network(reseau_id, ignore_missing=True)

    def creer_reseau_secondaire(self, projet_id: str | None, nom: str, cidr: str) -> dict[str, Any]:
        """Réseau interne supplémentaire au sein d'un projet déjà existant : network + subnet
        seulement, pas de routeur (ce n'est pas une nouvelle sortie externe, juste un réseau
        privé de plus dans un projet qui en a déjà un via `creer_reseau`)."""
        c = self._conn()
        net = c.network.create_network(name=nom, project_id=projet_id)
        sub = c.network.create_subnet(
            name=f"{nom}-sub", network_id=net.id, ip_version=4, cidr=cidr, project_id=projet_id
        )
        return {"reseau_id": net.id, "sous_reseau_id": sub.id}

    def supprimer_reseau_secondaire(self, reseau_id: str) -> None:
        self._conn().network.delete_network(reseau_id, ignore_missing=True)

    def creer_ip_flottante(self, projet_id: str | None) -> dict[str, Any]:
        c = self._conn()
        ext = self._reseau_externe(c)
        if ext is None:
            from synelia_kernel import erreurs

            raise erreurs.amont_indisponible("réseau externe")
        fip = c.network.create_ip(floating_network_id=ext.id, project_id=projet_id)
        return {"id": fip.id, "adresse": fip.floating_ip_address}

    def supprimer_ip_flottante(self, ip_id: str) -> None:
        self._conn().network.delete_ip(ip_id, ignore_missing=True)

    def associer_ip_flottante(self, ip_id: str, serveur_id: str) -> str | None:
        """Associe une IP flottante déjà allouée au port du serveur Nova (une seule interface
        dans notre cas — VM d'hébergement mono-réseau)."""
        c = self._conn()
        port = next(iter(c.network.ports(device_id=serveur_id)), None)
        if port is None:
            return None
        fip = c.network.update_ip(ip_id, port_id=port.id)
        return fip.floating_ip_address

    def dissocier_ip_flottante(self, ip_id: str) -> None:
        self._conn().network.update_ip(ip_id, port_id=None)

    def creer_application_credential(self, projet_id: str, domaine_id: str | None = None) -> dict[str, str]:
        """Un utilisateur de service par projet (jamais d'humain dans Keystone), rôle `member`,
        puis une *application credential* scellée au projet : c'est elle que le backend utilisera."""
        import openstack  # type: ignore[import-not-found]

        from synelia_kernel.config import reglages

        c = self._conn()
        projet = c.identity.get_project(projet_id)
        dom = domaine_id or projet.domain_id
        nom = f"svc-{projet_id[:8]}"
        mdp = jeton_opaque(24)
        user = c.identity.find_user(nom, domain_id=dom)
        if user is None:
            user = c.identity.create_user(name=nom, password=mdp, domain_id=dom, enabled=True, description="service Synelia")
        else:
            c.identity.update_user(user, password=mdp)
        role = c.identity.find_role("member") or c.identity.find_role("Member")
        c.identity.assign_project_role_to_user(projet, user, role)
        r = reglages()
        scope = openstack.connect(
            load_yaml_config=False,
            load_envvars=False,
            auth_type="v3password",
            auth_url=r.os_auth_url,
            username=nom,
            password=mdp,
            user_domain_id=dom,
            project_id=projet_id,
            region_name=r.os_region,
        )
        ac = scope.identity.create_application_credential(user=user.id, name=f"synelia-{projet_id[:8]}")
        return {"id": ac.id, "secret": ac.secret, "utilisateur_id": user.id}

    def supprimer_projet(self, projet_id: str) -> None:
        self._conn().identity.delete_project(projet_id, ignore_missing=True)
