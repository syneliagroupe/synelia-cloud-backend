#!/bin/bash
# Reconstruit et relance le frontend dev01 depuis un commit du dépôt frontend (défaut : HEAD de branchement-api).
set -euo pipefail
REF=${1:-branchement-api}
SRC=/var/lib/synelia-cloud/synelia-cloud
rm -rf /var/lib/synelia-cloud/front-deploy && mkdir -p /var/lib/synelia-cloud/front-deploy
git -C "$SRC" archive "$REF" | tar -x -C /var/lib/synelia-cloud/front-deploy
cd /var/lib/synelia-cloud/deploy-dev01 && sudo docker compose up -d --build
for i in $(seq 1 20); do curl -s -m 5 -o /dev/null -w "%{http_code}" http://127.0.0.1:3010/login | grep -q 200 && { echo "frontend dev01 en ligne ($REF)"; exit 0; }; sleep 3; done
echo "le frontend ne répond pas" >&2; exit 1
