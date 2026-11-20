#!/usr/bin/env bash
# Corrige un o-hm0 (interface santé Octavia sur ctrl1) dont l'adresse MAC a dérivé de celle
# du port Neutron `octavia-listen-port-ctrl1` — la table anti-spoofing de Neutron (br-int,
# table 25, `in_port=<o-hm0>,dl_src=<mac attendue>`) rejette alors silencieusement tout le
# trafic de santé/heartbeat vers les amphores, sans aucune erreur visible côté Octavia (juste
# des LB bloqués en PENDING_UPDATE / amphores qui restent BOOTING indéfiniment).
#
# Symptôme : `ping <ip amphore>` depuis ctrl1 → "Destination Host Unreachable", alors que le
# tunnel VXLAN ctrl1↔comp1 fonctionne pour toutes les autres VNI.
#
# Usage : ./scripts/fix-octavia-hm0-mac.sh [ctrl1_ip]
set -euo pipefail

CTRL1="${1:-192.168.26.235}"

if [ -z "${CTRL1_ROOT_PASSWORD:-}" ]; then
  echo "Définir CTRL1_ROOT_PASSWORD (mot de passe root partagé de ctrl1)." >&2
  exit 1
fi

ssh_ctrl1() {
  env -u SSH_AUTH_SOCK sshpass -p "$CTRL1_ROOT_PASSWORD" \
    ssh -o PubkeyAuthentication=no -o StrictHostKeyChecking=accept-new "root@${CTRL1}" "$1"
}

echo "== MAC actuelle de o-hm0 =="
ssh_ctrl1 "ip link show o-hm0 | grep link/ether"

echo "== MAC attendue par Neutron (via table 25 de br-int) =="
attendue=$(ssh_ctrl1 "docker exec openvswitch_vswitchd ovs-ofctl dump-flows br-int table=25 2>&1 | grep -oP 'dl_src=\K[0-9a-f:]+' | head -1")
if [ -z "$attendue" ]; then
  echo "Aucune règle anti-spoofing trouvée pour o-hm0 — rien à corriger (ou table 25 vide)." >&2
  exit 1
fi
echo "$attendue"

echo "== Correction =="
ssh_ctrl1 "ip link set o-hm0 down && ip link set o-hm0 address $attendue && ip link set o-hm0 up && ip neigh flush dev o-hm0"

echo "== Vérification =="
ssh_ctrl1 "ip link show o-hm0 | grep link/ether"
echo "Testez : ssh root@${CTRL1} ping -c3 <ip amphore>"
