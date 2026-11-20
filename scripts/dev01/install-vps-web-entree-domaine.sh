#!/usr/bin/env bash
# Installe sur dev01.ovh.smile.ci le reverse-proxy Apache pour un domaine Web Cloud client.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
DEMO_DOMAIN="${DEMO_DOMAIN:?DEMO_DOMAIN requis (ex. demo-28d14e3a.com)}"
LB_FIP="${VPS_ZONE_LB_FIP:?VPS_ZONE_LB_FIP requis}"
DEV01_HOST="${DEV01_SSH_HOST:-dev01.ovh.smile.ci}"
DEV01_USER="${DEV01_SSH_USER:-atlas}"

subst() {
  sed "s/__DEMO_DOMAIN__/${DEMO_DOMAIN}/g; s/__LB_FIP__/${LB_FIP}/g"
}

HTTP_CONF="$(subst < "${ROOT}/scripts/dev01/vps-web-entree-domaine.conf")"
SSL_CONF="$(subst < "${ROOT}/scripts/dev01/vps-web-entree-domaine-le-ssl.conf")"
REMOTE_BASE="/etc/httpd/conf.d/vps-web-entree-${DEMO_DOMAIN//./-}"

echo "→ ${DEV01_USER}@${DEV01_HOST} : ${DEMO_DOMAIN} → http://${LB_FIP}:80"

ssh "${DEV01_USER}@${DEV01_HOST}" "sudo mkdir -p /var/www/html/.well-known/acme-challenge"

ssh "${DEV01_USER}@${DEV01_HOST}" "sudo tee ${REMOTE_BASE}.conf" >/dev/null <<<"${HTTP_CONF}"

if ! ssh "${DEV01_USER}@${DEV01_HOST}" "sudo test -f /etc/letsencrypt/live/${DEMO_DOMAIN}/fullchain.pem"; then
  echo "Certificat absent — certbot webroot pour ${DEMO_DOMAIN} et ses sous-domaines applicatifs."
  ssh "${DEV01_USER}@${DEV01_HOST}" "sudo apachectl configtest && sudo kill -USR1 \$(sudo cat /run/httpd/httpd.pid)"
  ssh "${DEV01_USER}@${DEV01_HOST}" "sudo certbot certonly --webroot -w /var/www/html \
    -d ${DEMO_DOMAIN} \$(for s in blog drive docs pages dolibarr grav matomo opencart bookstack moodle; do printf -- '-d %s.${DEMO_DOMAIN} ' \$s; done) \
    --non-interactive --agree-tos -m demo@synelia.cloud --key-type ecdsa || true"
fi

if ssh "${DEV01_USER}@${DEV01_HOST}" "sudo test -f /etc/letsencrypt/live/${DEMO_DOMAIN}/fullchain.pem"; then
  ssh "${DEV01_USER}@${DEV01_HOST}" "sudo tee ${REMOTE_BASE}-le-ssl.conf" >/dev/null <<<"${SSL_CONF}"
else
  echo "WARN: pas de certificat — vhost HTTPS non installé (HTTP seulement)." >&2
fi

ssh "${DEV01_USER}@${DEV01_HOST}" "sudo apachectl configtest && sudo kill -USR1 \$(sudo cat /run/httpd/httpd.pid)"
echo "OK — tester : curl -sS https://${DEMO_DOMAIN}/ | head"
