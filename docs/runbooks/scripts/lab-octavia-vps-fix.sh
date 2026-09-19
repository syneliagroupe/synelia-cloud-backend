#!/bin/bash
# Purge stale Octavia pools on vps-zone-lb and pin ACTIVE (lab only). See lab-openstack.md.
set -euo pipefail
LB="${VPS_ZONE_LB_ID:-fd03a60f-ded4-4fbf-8555-589b1212b591}"
source /etc/kolla/admin-openrc.sh
OCTPW=$(grep ^octavia_database_password: /etc/kolla/passwords.yml | awk '{print $2}')

echo "=== stop octavia workers ==="
docker stop octavia_worker octavia_housekeeping 2>/dev/null || true

echo "=== purge DB pools/l7 on $LB ==="
docker exec mariadb mariadb -uoctavia -p"$OCTPW" octavia <<SQL
DELETE FROM l7rule WHERE l7policy_id IN (
  SELECT id FROM l7policy WHERE listener_id IN (SELECT id FROM listener WHERE load_balancer_id='$LB')
);
DELETE FROM l7policy WHERE listener_id IN (SELECT id FROM listener WHERE load_balancer_id='$LB');
DELETE FROM member WHERE pool_id IN (SELECT id FROM pool WHERE load_balancer_id='$LB');
DELETE FROM pool WHERE load_balancer_id='$LB';
UPDATE amphora SET status='ALLOCATED' WHERE load_balancer_id='$LB';
UPDATE load_balancer SET provisioning_status='ACTIVE' WHERE id='$LB';
SELECT COUNT(*) AS pools_left FROM pool WHERE load_balancer_id='$LB';
SQL

docker restart octavia_api
sleep 15

TOKEN=$(docker exec -e OS_AUTH_URL -e OS_USERNAME -e OS_PASSWORD -e OS_PROJECT_NAME \
  -e OS_USER_DOMAIN_NAME -e OS_PROJECT_DOMAIN_NAME -e OS_IDENTITY_API_VERSION \
  kolla_toolbox openstack token issue -f value -c id)
BASE=https://octavia.openstack-lab.dev01.ovh.smile.ci/v2.0/lbaas
curl -sS -H "X-Auth-Token: $TOKEN" "$BASE/loadbalancers/$LB" | python3 -c \
  "import json,sys; lb=json.load(sys.stdin)['loadbalancer']; print('LB',lb['provisioning_status'],lb['operating_status'])"

if [ "${LAB_OCTAVIA_START_WORKERS:-0}" = "1" ]; then
  docker start octavia_worker octavia_housekeeping 2>/dev/null || true
fi
echo "OCTAVIA_FIX_DONE"
