"""Designate : zones DNS et enregistrements."""

from __future__ import annotations

from typing import Any

from synelia_kernel.ids import nouvel_id

NS_DEFAUTS = ["ns1.synelia.cloud", "ns2.synelia.cloud"]


class DesignateSimule:
    def creer_zone(self, nom: str) -> dict[str, Any]:
        return {"id": f"zone-{nouvel_id()[:8]}", "ns": list(NS_DEFAUTS), "email": f"admin@{nom}"}

    def supprimer_zone(self, zone_id: str) -> None:
        return None

    def activer_dnssec(self, zone_id: str, actif: bool) -> None:
        return None

    def creer_enregistrement(
        self, zone_id: str, nom: str, type_: str, valeurs: list[str], ttl: int
    ) -> dict[str, Any]:
        return {"id": f"rs-{nouvel_id()[:8]}"}

    def modifier_enregistrement(
        self, zone_id: str, recordset_id: str, valeurs: list[str], ttl: int
    ) -> None:
        return None

    def supprimer_enregistrement(self, zone_id: str, recordset_id: str) -> None:
        return None


class DesignateOpenStack(DesignateSimule):
    def _c(self):  # type: ignore[no-untyped-def]
        from synelia_openstack.fabrique import connexion

        return connexion()

    def creer_zone(self, nom: str) -> dict[str, Any]:
        # Designate exige un nom de zone pleinement qualifié (terminé par un point) et un
        # email valide (`admin@…`, jamais `admin.…` — testé en direct : `400 Provided object
        # is not valid … is not an email`, la première forme n'a jamais fonctionné).
        c = self._c()
        nom_zone = nom if nom.endswith(".") else f"{nom}."
        z = c.dns.create_zone(name=nom_zone, email=f"admin@{nom}")
        # `dns.wait_for_zone` n'existe pas sur ce proxy (testé en direct — `AttributeError`) :
        # c'est le générique `wait_for_status` (comme Octavia/Nova) qu'il faut appeler. La
        # zone n'expose pas non plus `.nameservers` (autre `AttributeError` constaté en
        # direct) : les serveurs de noms réels se lisent via `zone_nameservers(zone_id)`.
        z = c.dns.wait_for_status(z, status="ACTIVE", failures=["ERROR"], interval=2, wait=60)
        ns = [n.hostname for n in c.dns.zone_nameservers(z.id)] or NS_DEFAUTS
        return {"id": z.id, "ns": list(ns), "email": z.email}

    def supprimer_zone(self, zone_id: str) -> None:
        self._c().dns.delete_zone(zone_id, ignore_missing=True)

    def activer_dnssec(self, zone_id: str, actif: bool) -> None:
        self._c().dns.update_zone(zone_id, attributes={"dnssec": actif})

    def creer_enregistrement(
        self, zone_id: str, nom: str, type_: str, valeurs: list[str], ttl: int
    ) -> dict[str, Any]:
        rs = self._c().dns.create_recordset(
            zone_id, name=nom, type=type_, records=valeurs, ttl=ttl
        )
        return {"id": rs.id}

    def modifier_enregistrement(
        self, zone_id: str, recordset_id: str, valeurs: list[str], ttl: int
    ) -> None:
        # `update_recordset(id, zone=..., **attrs)` échoue (`KeyError: 'zone_id'`, testé en
        # direct) : ce proxy n'accepte pas de reconstruire l'URI depuis un id nu + `zone=`,
        # il faut lui donner la ressource déjà récupérée (`get_recordset`), qui porte son
        # propre `zone_id`.
        c = self._c()
        rs = c.dns.get_recordset(recordset_id, zone_id)
        c.dns.update_recordset(rs, records=valeurs, ttl=ttl)

    def supprimer_enregistrement(self, zone_id: str, recordset_id: str) -> None:
        self._c().dns.delete_recordset(recordset_id, zone=zone_id, ignore_missing=True)
