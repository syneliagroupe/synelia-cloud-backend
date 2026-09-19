#!/bin/bash
# Delete orphan Neutron routers vps-zone-net-rtr (lab ctrl1 / kolla_toolbox).
set -uo pipefail
source /etc/kolla/admin-openrc.sh
os() {
  docker exec -e OS_AUTH_URL -e OS_USERNAME -e OS_PASSWORD -e OS_PROJECT_NAME \
    -e OS_USER_DOMAIN_NAME -e OS_PROJECT_DOMAIN_NAME -e OS_IDENTITY_API_VERSION \
    kolla_toolbox openstack "$@"
}
deleted=0
failed=0
mapfile -t routers < <(os router list --name vps-zone-net-rtr -f value -c ID)
echo "start count=${#routers[@]}"
for rid in "${routers[@]}"; do
  pids=$(os router show "$rid" -f json 2>/dev/null | python3 -c "
import json,sys
r=json.load(sys.stdin)
print(' '.join(i['port_id'] for i in r.get('interfaces_info') or [] if i.get('port_id')))
" 2>/dev/null || true)
  for pid in $pids; do os router remove port "$rid" "$pid" >/dev/null 2>&1 || true; done
  os router unset --external-gateway "$rid" >/dev/null 2>&1 || true
  if os router delete "$rid" >/dev/null 2>&1; then deleted=$((deleted + 1)); else failed=$((failed + 1)); fi
done
echo "FINAL deleted=$deleted failed=$failed remaining=$(os router list --name vps-zone-net-rtr -f value -c ID | wc -l)"
