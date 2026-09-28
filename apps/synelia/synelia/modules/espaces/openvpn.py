"""Passerelle OpenVPN par Espace Cloud (provisionnée dans le travail `espace.create`)."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from synelia_contract import modeles as m
from synelia_kernel import erreurs
from synelia_kernel.config import reglages
from synelia_kernel.dates import maintenant
from synelia_kernel.ids import nouvel_id
from synelia_openstack import fournisseur
from synelia_openstack.compute import ComputeOpenStack, ComputeSimule
from synelia_openstack.identite import IdentiteOpenStack, IdentiteSimule
from synelia_openstack.network import NetworkOpenStack, NetworkSimule
from synelia_openstack.openvpn_cloudinit import construire_cloud_init
from synelia_openstack.ssh import SshReel, SshSimule

from synelia.deps.contexte import Contexte
from synelia.modules.reseau.service import depot_vpn

NOM_TUNNEL_SUFFIX = "-nomade"


def _compute() -> ComputeSimule:
    return fournisseur(ComputeSimule, ComputeOpenStack)


def _identite() -> IdentiteSimule:
    return fournisseur(IdentiteSimule, IdentiteOpenStack)


def _network() -> NetworkSimule:
    return fournisseur(NetworkSimule, NetworkOpenStack)


def _ssh() -> SshSimule:
    return fournisseur(SshSimule, SshReel)


def _identifiants_espace(secrets: dict[str, str]) -> dict[str, str]:
    return {
        "application_credential_id": secrets["application_credential_id"],
        "application_credential_secret": secrets["application_credential_secret"],
    }


def _image_et_gabarit() -> tuple[str, str]:
    r = reglages()
    if r.openvpn_image_id and r.openvpn_flavor_id:
        return r.openvpn_image_id, r.openvpn_flavor_id
    c = _compute()
    imgs = c.images()
    img = next((i for i in imgs if "ubuntu-24" in i["nom"].lower()), None)
    if img is None and imgs:
        img = imgs[0]
    gab = next((g for g in c.gabarits() if g.get("nom") == "small"), None)
    if gab is None:
        gabs = c.gabarits()
        gab = gabs[0] if gabs else None
    if not img or not gab:
        raise erreurs.amont_indisponible("openvpn", "image ou gabarit introuvable")
    return img["id"], gab["id"]


async def provisionner_passerelle_openvpn(
    ctx: Contexte,
    espace: m.EspaceCloud,
    secrets_espace: dict[str, str],
) -> dict[str, Any]:
    """Crée VM + FIP + tunnel SSL `type=ssl` en base. No-op simulé hors OpenStack."""
    if (
        espace.code == "vps-zone"
        or reglages().fournisseur != "openstack"
        or not reglages().openvpn_actif
    ):
        return await _provisionner_simule(ctx, espace)

    ident = _identifiants_espace(secrets_espace)
    cle = await asyncio.to_thread(_ssh().generer_cle)
    nom_keypair = f"synelia-vpn-{espace.code}"[:255]
    await asyncio.to_thread(_compute().assurer_keypair, nom_keypair, cle["publique"], ident)
    image_id, flavor_id = await asyncio.to_thread(_image_et_gabarit)
    cloud_init = construire_cloud_init(espace.cidr, cle["publique"])
    nom_vm = f"vpn-{espace.code}"[:255]
    srv = await asyncio.to_thread(
        _compute().creer_serveur,
        nom=nom_vm,
        image_id=image_id,
        gabarit_id=flavor_id,
        reseau_id=secrets_espace["reseau_id"],
        identifiants=ident,
        org_id=espace.orgId,
        espace_id=espace.id,
        cle_ssh=nom_keypair,
        cloud_init=cloud_init,
    )
    net = _network()
    if isinstance(net, NetworkOpenStack):
        await asyncio.to_thread(net.assurer_regle_ssh, srv["id"])
        await asyncio.to_thread(net.assurer_regle_ingress_udp, srv["id"], 1194)

    projet_id = secrets_espace["projet_id"]
    fip = await asyncio.to_thread(_identite().creer_ip_flottante, projet_id)
    publique = await asyncio.to_thread(_identite().associer_ip_flottante, fip["id"], srv["id"])
    if not publique:
        raise erreurs.amont_indisponible("openvpn", "association IP flottante impossible")

    await _attendre_script_openvpn(publique, cle["prive"])

    client_host, client_port = publique, 1194
    if reglages().openvpn_dev01_firewall:
        from synelia.modules.espaces import openvpn_dev01

        client_host, client_port = await asyncio.to_thread(
            openvpn_dev01.exposer_udp_openvpn, publique
        )

    tunnel = m.TunnelVpn(
        id=nouvel_id(),
        espaceId=espace.id,
        nom=f"{espace.code}{NOM_TUNNEL_SUFFIX}",
        type="ssl",
        passerelleDistante=None,
        reseauxAnnonces=[espace.cidr],
        statut="up",
        derniereNegociation=maintenant(),
        profils=[],
    )
    await depot_vpn.creer(
        ctx,
        tunnel,
        secrets={
            "openvpn": "1",
            "serveur_id": srv["id"],
            "fip_id": fip["id"],
            "endpoint": publique,
            "ssh_prive": cle["prive"],
            "client_host": client_host,
            "client_port": str(client_port),
        },
    )
    return {
        "vpn_tunnel_id": tunnel.id,
        "vpn_serveur_id": srv["id"],
        "vpn_fip_id": fip["id"],
        "vpn_endpoint": publique,
        "vpn_ssh_prive": cle["prive"],
        "vpn_client_host": client_host,
        "vpn_client_port": str(client_port),
    }


async def _provisionner_simule(ctx: Contexte, espace: m.EspaceCloud) -> dict[str, Any]:
    tunnel = m.TunnelVpn(
        id=nouvel_id(),
        espaceId=espace.id,
        nom=f"{espace.code}{NOM_TUNNEL_SUFFIX}",
        type="ssl",
        reseauxAnnonces=[espace.cidr],
        statut="up",
        derniereNegociation=maintenant(),
        profils=[],
    )
    await depot_vpn.creer(ctx, tunnel)
    return {"vpn_tunnel_id": tunnel.id}


async def _attendre_script_openvpn(hote: str, cle_privee: str, delai_s: int = 420) -> None:
    """Attend que cloud-init ait posé le script d'émission de profils."""
    if reglages().fournisseur != "openstack":
        return
    fin = time.monotonic() + delai_s
    cmd = "test -x /usr/local/bin/synelia-vpn-issue-client"
    while time.monotonic() < fin:
        try:
            await asyncio.to_thread(_ssh().executer, hote, cle_privee, cmd)
            return
        except Exception:  # noqa: BLE001
            await asyncio.sleep(10)
    raise erreurs.amont_indisponible(
        "openvpn", "cloud-init OpenVPN non prêt (synelia-vpn-issue-client absent)"
    )


async def supprimer_passerelle_openvpn(ctx: Contexte, secrets_espace: dict[str, str]) -> None:
    sid = secrets_espace.get("vpn_serveur_id")
    fid = secrets_espace.get("vpn_fip_id")
    tid = secrets_espace.get("vpn_tunnel_id")
    if tid and reglages().openvpn_dev01_firewall:
        try:
            sec = await depot_vpn.secrets(ctx, tid)
            port = sec.get("client_port")
            fip = sec.get("endpoint")
            if port and fip:
                from synelia.modules.espaces import openvpn_dev01

                await asyncio.to_thread(openvpn_dev01.retirer_udp_openvpn, int(port), fip)
        except erreurs.AppError:
            pass
    if tid:
        try:
            await depot_vpn.supprimer(ctx, tid)
        except erreurs.AppError:
            pass
    if fid:
        await asyncio.to_thread(_identite().supprimer_ip_flottante, fid)
    if sid and reglages().fournisseur == "openstack":
        await asyncio.to_thread(_compute().supprimer_serveur, sid)


async def emettre_profil_openvpn(
    ctx: Contexte, tunnel_id: str, nom_profil: str, utilisateur: str
) -> str:
    secrets = await depot_vpn.secrets(ctx, tunnel_id)
    ssh_host = secrets.get("endpoint") or "vpn.synelia.cloud"
    client_host = secrets.get("client_host") or ssh_host
    client_port = secrets.get("client_port") or "1194"
    if not secrets.get("openvpn") or not secrets.get("ssh_prive"):
        return _ovpn_bidon(nom_profil, utilisateur, client_host, client_port)
    if reglages().fournisseur != "openstack":
        return _ovpn_bidon(nom_profil, utilisateur, client_host, client_port)
    cmd = (
        f"export SYNELIA_VPN_PUBLIC_HOST={client_host!r}; "
        f"export SYNELIA_VPN_PUBLIC_PORT={client_port!r}; "
        f"/usr/local/bin/synelia-vpn-issue-client {nom_profil!r}"
    )
    brut = await asyncio.to_thread(_ssh().executer, ssh_host, secrets["ssh_prive"], cmd, "root")
    idx = brut.find("client\n")
    profil = brut[idx:] if idx >= 0 else brut
    # VM créées avant SYNELIA_VPN_PUBLIC_PORT : forcer remote public (dev01 DNAT).
    ligne_remote = f"remote {client_host} {client_port}\n"
    if profil.startswith("client\n"):
        lignes = profil.splitlines(keepends=True)
        for i, ln in enumerate(lignes):
            if ln.startswith("remote "):
                lignes[i] = ligne_remote
                break
        profil = "".join(lignes)
    return profil


def _ovpn_bidon(nom: str, utilisateur: str, endpoint: str, port: str = "1194") -> str:
    return (
        "client\n"
        "dev tun\n"
        "proto udp\n"
        f"remote {endpoint} {port}\n"
        "auth-user-pass\n"
        f"<ca>\n-----BEGIN CERTIFICATE-----\n{nom}://{utilisateur}/{nouvel_id()}\n"
        "-----END CERTIFICATE-----\n</ca>\n"
    )
