"""Exposition OpenVPN sur dev01.ovh.smile.ci : port UDP public → FIP VM:1194 (firewalld)."""

from __future__ import annotations

import os
import random
import shlex

from synelia_kernel import erreurs
from synelia_kernel.config import reglages
from synelia_openstack.ssh import SshReel


def _ssh_hote(commande: str) -> str:
    r = reglages()
    cle = r.openvpn_dev01_ssh_key_path
    hote = r.openvpn_dev01_ssh_host or "dev01.ovh.smile.ci"
    if not cle or not os.path.isfile(cle):
        raise erreurs.amont_indisponible(
            "dev01-firewall",
            "clé SSH hôte manquante (SYNELIA_OPENVPN_DEV01_SSH_KEY_PATH)",
        )
    with open(cle, encoding="utf-8") as f:
        prive = f.read()
    return SshReel().executer(hote, prive, commande, "root")


def exposer_udp_openvpn(dest_fip: str) -> tuple[str, int]:
    """Ouvre un port UDP aléatoire sur dev01 et DNAT vers `dest_fip:1194`."""
    r = reglages()
    if not r.openvpn_dev01_firewall:
        return r.openvpn_dev01_public_host or dest_fip, 1194
    port = random.randint(46000, 48999)
    public_host = r.openvpn_dev01_public_host or "dev01.ovh.smile.ci"
    script = r.openvpn_dev01_firewall_script
    if script:
        _ssh_hote(
            f"bash {shlex.quote(script)} add {port} {shlex.quote(dest_fip)}"
        )
    else:
        cmd = (
            f"firewall-cmd --permanent --add-port={port}/udp && "
            f"firewall-cmd --permanent --add-forward-port="
            f"port={port}:proto=udp:toport=1194:toaddr={dest_fip} && "
            f"firewall-cmd --reload"
        )
        _ssh_hote(cmd)
    return public_host, port


def retirer_udp_openvpn(port: int, dest_fip: str) -> None:
    if not reglages().openvpn_dev01_firewall:
        return
    script = reglages().openvpn_dev01_firewall_script
    if script:
        try:
            _ssh_hote(
                f"bash {shlex.quote(script)} remove {port} {shlex.quote(dest_fip)}"
            )
        except erreurs.AppError:
            pass
        return
    cmd = (
        f"firewall-cmd --permanent --remove-port={port}/udp; "
        f"firewall-cmd --permanent --remove-forward-port="
        f"port={port}:proto=udp:toport=1194:toaddr={dest_fip}; "
        f"firewall-cmd --reload"
    )
    try:
        _ssh_hote(cmd)
    except erreurs.AppError:
        pass
