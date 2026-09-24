#!/usr/bin/env bash
# Débloque un load balancer Octavia coincé en PENDING_UPDATE / pools "immutable" sur le lab.
# Symptôme : `hebergement.creer` échoue à l'étape LB avec "Load Balancer ... is immutable".
# Cause fréquente : pools ERROR/PENDING_CREATE laissés par un run précédent + octavia_worker
# qui remet le LB en PENDING_UPDATE avant qu'on ait pu nettoyer. Procédure documentée dans
# docs/runbooks/lab-openstack.md § Octavia.
#
# Usage : ./scripts/fix-octavia-lb.sh <load_balancer_id> [ctrl1_ip]
set -euo pipefail

LB="${1:?usage: fix-octavia-lb.sh <load_balancer_id> [ctrl1_ip]}"
CTRL1="${2:-192.168.26.235}"

if [ -z "${CTRL1_ROOT_PASSWORD:-}" ]; then
  echo "Définir CTRL1_ROOT_PASSWORD (mot de passe root partagé de ctrl1) avant d'exécuter ce script." >&2
  exit 1
fi

ssh_ctrl1() {
  env -u SSH_AUTH_SOCK sshpass -p "$CTRL1_ROOT_PASSWORD" \
    ssh -o PubkeyAuthentication=no -o StrictHostKeyChecking=accept-new "root@${CTRL1}" "$1"
}

echo "== arrêt octavia_worker / octavia_housekeeping =="
ssh_ctrl1 "docker stop octavia_worker octavia_housekeeping"

echo "== nettoyage des lignes bloquées pour le LB ${LB} =="
ssh_ctrl1 "
OCTPW=\$(grep '^octavia_database_password:' /etc/kolla/passwords.yml | awk '{print \$2}')
docker exec mariadb mariadb -uoctavia -p\"\$OCTPW\" octavia -e \"
  DELETE FROM l7rule WHERE l7policy_id IN (SELECT id FROM l7policy WHERE listener_id IN (SELECT id FROM listener WHERE load_balancer_id='${LB}'));
  DELETE FROM l7policy WHERE listener_id IN (SELECT id FROM listener WHERE load_balancer_id='${LB}');
  DELETE FROM member WHERE pool_id IN (SELECT id FROM pool WHERE load_balancer_id='${LB}');
  DELETE FROM pool WHERE load_balancer_id='${LB}';
  UPDATE amphora SET status='ALLOCATED' WHERE load_balancer_id='${LB}';
  UPDATE load_balancer SET provisioning_status='ACTIVE' WHERE id='${LB}';
\"
"

echo "== redémarrage octavia_api =="
ssh_ctrl1 "docker restart octavia_api"
sleep 10

echo "== reprise octavia_worker / octavia_housekeeping =="
ssh_ctrl1 "docker start octavia_worker octavia_housekeeping"

echo "Terminé. Vérifier : SYNELIA_REGISTRAR_URL=... uv run python -c \"
import openstack, os
c = openstack.connect(auth_url=os.environ['SYNELIA_OS_AUTH_URL'],
    application_credential_id=os.environ['SYNELIA_OS_APPLICATION_CREDENTIAL_ID'],
    application_credential_secret=os.environ['SYNELIA_OS_APPLICATION_CREDENTIAL_SECRET'],
    auth_type='v3applicationcredential', region_name='RegionOne')
lb = c.load_balancer.get_load_balancer('${LB}')
print(lb.provisioning_status, lb.operating_status)
\""
