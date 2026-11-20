#!/usr/bin/env bash
# dev01.ovh.smile.ci (198.244.179.212) : firewalld TCP → SFTP sur la VM (port 2222)
# Usage: sftp-edge-forward.sh add <port> <dest_ip> | remove <port> <dest_ip>
set -euo pipefail
CMD=${1:?add|remove}
PORT=${2:?port}
DEST=${3:-}
TOPORT=2222

case "$CMD" in
  add)
    [[ -n "$DEST" ]] || { echo "dest ip required" >&2; exit 1; }
    firewall-cmd --permanent --add-port="${PORT}/tcp"
    firewall-cmd --permanent --add-forward-port="port=${PORT}:proto=tcp:toport=${TOPORT}:toaddr=${DEST}"
    firewall-cmd --reload
    echo "ok port=${PORT} -> ${DEST}:${TOPORT}"
    ;;
  remove)
    [[ -n "$DEST" ]] || { echo "dest ip required" >&2; exit 1; }
    firewall-cmd --permanent --remove-port="${PORT}/tcp" 2>/dev/null || true
    firewall-cmd --permanent --remove-forward-port="port=${PORT}:proto=tcp:toport=${TOPORT}:toaddr=${DEST}" 2>/dev/null || true
    firewall-cmd --reload
    echo "ok removed ${PORT} -> ${DEST}:${TOPORT}"
    ;;
  *)
    echo "usage: $0 add <port> <dest_ip> | remove <port> <dest_ip>" >&2
    exit 1
    ;;
esac
