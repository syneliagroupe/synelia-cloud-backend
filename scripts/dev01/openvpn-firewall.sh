#!/usr/bin/env bash
# dev01.ovh.smile.ci (IP publique 198.244.179.212) : firewalld → FIP OpenVPN:1194
# Usage: openvpn-firewall.sh add <port> <dest_ip> | remove <port>
set -euo pipefail
CMD=${1:?add|remove}
PORT=${2:?port}
DEST=${3:-}

case "$CMD" in
  add)
    [[ -n "$DEST" ]] || { echo "dest ip required" >&2; exit 1; }
    firewall-cmd --permanent --add-port="${PORT}/udp"
    firewall-cmd --permanent --add-forward-port="port=${PORT}:proto=udp:toport=1194:toaddr=${DEST}"
    firewall-cmd --reload
    echo "ok port=${PORT} -> ${DEST}:1194"
    ;;
  remove)
    firewall-cmd --permanent --remove-port="${PORT}/udp" 2>/dev/null || true
    firewall-cmd --permanent --remove-forward-port="port=${PORT}:proto=udp:toport=1194:toaddr=${DEST}" 2>/dev/null || true
    firewall-cmd --reload
    echo "ok removed ${PORT}"
    ;;
  *)
    echo "usage: $0 add <port> <dest_ip> | remove <port> [dest_ip]" >&2
    exit 1
    ;;
esac
