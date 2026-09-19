#!/bin/bash
# Purge stale Octavia pools on vps-zone-lb and pin ACTIVE (lab ctrl1 only).
set -euo pipefail
LB="${VPS_ZONE_LB_ID:-fd03a60f-ded4-4fbf-8555-589b1212b591}"
source /etc/kolla/admin-openrc.sh
OCTPW=$(grep ^octavia_database_password: /etc/kolla/passwords.yml | awk '{print $2}')
docker stop octavia_worker octavia_housekeeping 2>/dev/null || true
docker exec mariadb mariadb -uoctavia -p"$OCTPW" octavia <<SQL
DELETE FROM l7rule WHERE l7policy_id IN (
  SELECT id FROM l7policy WHERE listener_id IN (SELECT id FROM listener WHERE load_balancer_id='$LB')
);
DELETE FROM l7policy WHERE listener_id IN (SELECT id FROM listener WHERE load_balancer_id='$LB');
DELETE FROM member WHERE pool_id IN (SELECT id FROM pool WHERE load_balancer_id='$LB');
DELETE FROM pool WHERE load_balancer_id='$LB';
UPDATE amphora SET status='ALLOCATED' WHERE load_balancer_id='$LB';
UPDATE load_balancer SET provisioning_status='ACTIVE' WHERE id='$LB';
SQL
docker restart octavia_api
echo "octavia_vps_zone_fix: DB purged, octavia_api restarted (verify LB ACTIVE before pytest Web Cloud)"
