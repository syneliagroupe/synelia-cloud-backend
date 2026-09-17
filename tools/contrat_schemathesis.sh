#!/usr/bin/env bash
# Suite « contrat » : conformité du backend au contrat OpenAPI par Schemathesis.
#
# Cible une instance LOCALE en mode simulé (jamais le lab). Par défaut http://127.0.0.1:4020/v1 :
#   SYNELIA_DATABASE_URL=sqlite+aiosqlite:////tmp/synelia-schemathesis.sqlite3 SYNELIA_ENV=test \
#     nohup uv run synelia api --port 4020 > /tmp/api-4020.log 2>&1 &
# Variables : SYNELIA_SCHEMATHESIS_URL, SCHEMATHESIS_RAPPORTS (dossier des rapports),
#             SCHEMATHESIS_EXEMPLES (défaut 5), SCHEMATHESIS_WORKERS (défaut 4), SCHEMATHESIS_ARGS (extra).
# Usage : tools/contrat_schemathesis.sh [--include-path-regex '^/vms' ...]
set -euo pipefail
RACINE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$RACINE"
export PATH="$HOME/.local/bin:$PATH"

URL="${SYNELIA_SCHEMATHESIS_URL:-http://127.0.0.1:4020/v1}"
RAPPORTS="${SCHEMATHESIS_RAPPORTS:-$RACINE/tests-rapports/contrat}"
EXEMPLES="${SCHEMATHESIS_EXEMPLES:-5}"
WORKERS="${SCHEMATHESIS_WORKERS:-4}"
export SYNELIA_SCHEMATHESIS_URL="$URL"
export SCHEMATHESIS_HOOKS="$RACINE/tools/schemathesis_hooks.py"
mkdir -p "$RAPPORTS"

# L'API doit répondre avant de lancer (jusqu'à 60 s).
for _ in $(seq 1 60); do
  if curl -fsS -m 3 "${URL%/v1}/healthz" >/dev/null 2>&1; then break; fi
  sleep 1
done
curl -fsS -m 3 "${URL%/v1}/healthz" >/dev/null || { echo "API injoignable sur $URL" >&2; exit 2; }

set +e
uv run schemathesis run packages/contract/synelia_contract/openapi.json \
  --url "$URL" \
  --checks all \
  --max-examples "$EXEMPLES" \
  --workers "$WORKERS" \
  --continue-on-failure \
  --generation-with-security-parameters false \
  --request-timeout 60 \
  --suppress-health-check all \
  --report junit,ndjson \
  --report-dir "$RAPPORTS" \
  --report-junit-path "$RAPPORTS/junit.xml" \
  --report-ndjson-path "$RAPPORTS/evenements.ndjson" \
  --no-color \
  ${SCHEMATHESIS_ARGS:-} "$@" | tee "$RAPPORTS/sortie.txt"
code=${PIPESTATUS[0]}
set -e

# Classement des échecs par type (500, schéma de réponse, code non déclaré…).
if [ -f "$RAPPORTS/junit.xml" ]; then
  uv run python tools/contrat_classer.py "$RAPPORTS/junit.xml" | tee "$RAPPORTS/classement.txt"
fi
exit "$code"
