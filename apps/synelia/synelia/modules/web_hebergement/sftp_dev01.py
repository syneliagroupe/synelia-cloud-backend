"""Exposition SFTP Web Cloud sur dev01 : port TCP public aléatoire → FIP VM:2222 (firewalld)."""

from __future__ import annotations

import os
import random
import shlex

from synelia_kernel import erreurs
from synelia_kernel.config import reglages
from synelia_openstack.ssh import SshReel

_PORT_MIN = 49000
_PORT_MAX = 51999
_SFTP_VM_PORT = 2222


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


def _edge_actif() -> bool:
    r = reglages()
    return bool(r.web_sftp_dev01_firewall or r.openvpn_dev01_firewall)


def hote_public_transfert() -> str:
    r = reglages()
    return r.openvpn_dev01_public_host or r.domaine_dns_entree_a or "dev01.ovh.smile.ci"


def exposer_sftp(dest_fip: str) -> tuple[str, int]:
    """Ouvre un port TCP aléatoire sur dev01 et DNAT vers `dest_fip:2222`."""
    if not _edge_actif():
        return dest_fip, _SFTP_VM_PORT
    port = random.randint(_PORT_MIN, _PORT_MAX)
    public_host = hote_public_transfert()
    script = reglages().web_sftp_dev01_firewall_script
    if script:
        _ssh_hote(f"bash {shlex.quote(script)} add {port} {shlex.quote(dest_fip)}")
    else:
        cmd = (
            f"firewall-cmd --permanent --add-port={port}/tcp && "
            f"firewall-cmd --permanent --add-forward-port="
            f"port={port}:proto=tcp:toport={_SFTP_VM_PORT}:toaddr={dest_fip} && "
            f"firewall-cmd --reload"
        )
        _ssh_hote(cmd)
    return public_host, port


def retirer_sftp(port: int, dest_fip: str) -> None:
    if not _edge_actif():
        return
    script = reglages().web_sftp_dev01_firewall_script
    if script:
        try:
            _ssh_hote(f"bash {shlex.quote(script)} remove {port} {shlex.quote(dest_fip)}")
        except erreurs.AppError:
            pass
        return
    cmd = (
        f"firewall-cmd --permanent --remove-port={port}/tcp; "
        f"firewall-cmd --permanent --remove-forward-port="
        f"port={port}:proto=tcp:toport={_SFTP_VM_PORT}:toaddr={dest_fip}; "
        f"firewall-cmd --reload"
    )
    try:
        _ssh_hote(cmd)
    except erreurs.AppError:
        pass
