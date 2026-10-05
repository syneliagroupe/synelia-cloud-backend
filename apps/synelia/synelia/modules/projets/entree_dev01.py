"""Entrée publique d'un domaine applicatif : vhost Apache sur dev01 → IP flottante du service.

Même edge que les domaines Web Cloud (`scripts/dev01/install-vps-web-entree-domaine.sh`) : le
DNS du domaine pointe sur dev01, Apache y termine TLS (Let's Encrypt, webroot) et proxifie en
HTTP vers le load balancer Kubernetes. Sans edge configuré (tests, mode simulé) : no-op.
"""

from __future__ import annotations

import base64
import logging
import re
import socket

from synelia_kernel import erreurs
from synelia_kernel.config import reglages

from synelia.modules.web_hebergement.sftp_dev01 import _edge_actif, _ssh_hote

_HTTP = """<VirtualHost *:80>
  ServerName {hote}
  Alias /.well-known/acme-challenge/ /var/www/html/.well-known/acme-challenge/
  ProxyPass /.well-known !
  ProxyPreserveHost On
  ProxyPass        / http://{ip}:80/ connectiontimeout=5 timeout=120 retry=0
  ProxyPassReverse / http://{ip}:80/
  <Directory /var/www/html/.well-known/acme-challenge/>
    Require all granted
  </Directory>
</VirtualHost>
"""

_HTTP_REDIRECTION = """<VirtualHost *:80>
  ServerName {hote}
  Alias /.well-known/acme-challenge/ /var/www/html/.well-known/acme-challenge/
  <Directory /var/www/html/.well-known/acme-challenge/>
    Require all granted
  </Directory>
  RewriteEngine on
  RewriteCond %{{REQUEST_URI}} !^/.well-known/acme-challenge/
  RewriteRule ^ https://%{{HTTP_HOST}}%{{REQUEST_URI}} [R=308,L]
</VirtualHost>
"""

_HTTPS = """<VirtualHost *:443>
  ServerName {hote}
  SSLEngine on
  SSLCertificateFile /etc/letsencrypt/live/{hote}/fullchain.pem
  SSLCertificateKeyFile /etc/letsencrypt/live/{hote}/privkey.pem
  RequestHeader set X-Forwarded-Proto "https"
  RequestHeader set X-Forwarded-Port "443"
  ProxyPreserveHost On
  ProxyPass        / http://{ip}:80/ connectiontimeout=5 timeout=120 retry=0
  ProxyPassReverse / http://{ip}:80/
</VirtualHost>
"""


def _conf(hote: str, suffixe: str = "") -> str:
    # `hote` finit dans une commande shell sur dev01 : jamais autre chose qu'un nom DNS.
    if not re.fullmatch(r"[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?", hote) or ".." in hote:
        raise erreurs.conflit(f"Nom d'hôte invalide : `{hote}`.", code="hote_invalide")
    return f"/etc/httpd/conf.d/app-domaine-{hote}{suffixe}.conf"


def _ecrire_et_recharger(hote: str, suffixe: str, contenu: str) -> None:
    """Écrit le vhost puis recharge Apache ; une conf invalide est retirée — dev01 héberge
    d'autres sites, un `configtest` cassé ne doit jamais rester en place."""
    chemin = _conf(hote, suffixe)
    b64 = base64.b64encode(contenu.encode()).decode()
    _ssh_hote(
        f"echo {b64} | base64 -d > {chemin} && (apachectl configtest && apachectl graceful"
        f" || (rm -f {chemin}; exit 1))"
    )


def verifier_dns(hote: str) -> None:
    """Le nom doit déjà résoudre vers l'entrée dev01 (A ou CNAME aboutissant à cette IP)."""
    attendu = reglages().domaine_dns_entree_a
    if not _edge_actif() or not attendu:
        return
    try:
        adresses = {i[4][0] for i in socket.getaddrinfo(hote, 80, socket.AF_INET)}
    except OSError:
        adresses = set()
    if attendu not in adresses:
        raise erreurs.conflit(
            f"`{hote}` ne résout pas vers l'entrée ({attendu}) : créez l'enregistrement DNS "
            f"puis relancez (résolu : {', '.join(sorted(adresses)) or 'rien'}).",
            code="dns_non_pointe",
        )


def publier(hote: str, ip: str) -> None:
    if _edge_actif():
        _ecrire_et_recharger(hote, "", _HTTP.format(hote=hote, ip=ip))


def emettre_certificat(hote: str, ip: str) -> None:
    if not _edge_actif():
        return
    _conf(hote)
    _ssh_hote(
        f"test -f /etc/letsencrypt/live/{hote}/fullchain.pem || certbot certonly --webroot"
        f" -w /var/www/html -d {hote} --non-interactive --agree-tos -m demo@synelia.cloud"
        " --key-type ecdsa"
    )
    _ecrire_et_recharger(hote, "-le-ssl", _HTTPS.format(hote=hote, ip=ip))
    _ecrire_et_recharger(hote, "", _HTTP_REDIRECTION.format(hote=hote))


def publier_site_web(hote: str) -> None:
    """Site d'hébergement sur un domaine client : vhost edge → LB partagé, puis TLS. Les
    `*.cloud.dev01…` sont déjà couverts par le vhost wildcard ; le certificat est tenté sans
    bloquer (DNS pas encore pointé → HTTP seul, relançable)."""
    ip = reglages().vps_zone_lb_fip
    if not ip or hote.endswith(".cloud.dev01.ovh.smile.ci"):
        return
    publier(hote, ip)
    try:
        emettre_certificat(hote, ip)
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).warning("certificat edge non émis pour %s", hote, exc_info=True)


def retirer(hote: str) -> None:
    if _edge_actif():
        _ssh_hote(
            f"rm -f {_conf(hote)} {_conf(hote, '-le-ssl')} && apachectl graceful;"
            f" certbot delete --cert-name {hote} --non-interactive >/dev/null 2>&1; true"
        )
