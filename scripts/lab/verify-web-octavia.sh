#!/usr/bin/env bash
# Vérifie HTTP d'un hébergement Web Cloud via Octavia (lab ctrl1 + namespace DHCP tenant).
# Usage (depuis vm-admin) :
#   HEB_ID=01a0ca40-b7d4-7720-a658-a7e523992e3e ./scripts/lab/verify-web-octavia.sh
#   ./scripts/lab/verify-web-octavia.sh h-01a0ca40.cloud.dev01.ovh.smile.ci
set -euo pipefail
HOST="${1:-h-${HEB_ID:0:8}.cloud.dev01.ovh.smile.ci}"
LB_FIP="${VPS_ZONE_LB_FIP:-192.168.20.231}"
CTRL="${OPENSTACK_CTRL:-192.168.26.235}"
NET_ID="${VPS_ZONE_NET_ID:-3f2feb5e-43ff-4b1f-9147-8fda08afbc0e}"
QDHCP="qdhcp-${NET_ID}"
SSH=(env -u SSH_AUTH_SOCK ssh -o PubkeyAuthentication=no -o StrictHostKeyChecking=accept-new "root@${CTRL}")

echo "Host: ${HOST}"
echo "=== Octavia FIP ${LB_FIP} (depuis ctrl1) ==="
"${SSH[@]}" "curl -sS -m 12 -w '\nhttp_code=%{http_code}\n' -H 'Host: ${HOST}' 'http://${LB_FIP}/'" | tail -15 || true

echo "=== Octavia VIP 10.90.2.173 (netns ${QDHCP}) ==="
"${SSH[@]}" "ip netns exec ${QDHCP} curl -sS -m 12 -w '\nhttp_code=%{http_code}\n' -H 'Host: ${HOST}' 'http://10.90.2.173/'" | tail -15 || true

echo "=== OpenStack checks (depuis poste avec SDK / toolbox) ==="
echo "  - FIP ${LB_FIP} doit être associé au vip_port_id du LB ${VPS_ZONE_LB_ID:-6bcc4ab4-3f67-4b60-a9fc-62600328012d}"
echo "  - Pool pool-<8chars heb> : membres ACTIVE, pas ERROR/NO_MONITOR"
echo "  - Si échec : docs/runbooks/lab-openstack.md § Octavia vps-zone-lb"
