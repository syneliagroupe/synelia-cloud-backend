#!/usr/bin/env bash
# Nettoie les données de test du lab : tickets de test et travaux échoués.
#   ./cleanup-test-data.sh           aperçu seul (rien n'est modifié)
#   ./cleanup-test-data.sh --apply   sauvegarde en CSV puis supprime, dans une transaction
set -euo pipefail

PG=${PG_CONTAINER:-synelia-cloud-backend-postgres-1}
psql_() { docker exec -i "$PG" psql -U synelia -d synelia -v ON_ERROR_STOP=1 "$@"; }

TICKETS="type = 'ticket' AND supprime_le IS NULL AND (
  nom ILIKE 'bat-global%' OR nom ILIKE 'Latence API facturation%' OR nom = 'Parcours admin - ticket de test')"
TRAVAUX="statut IN ('failed', 'rolled_back')"

echo "== Tickets de test visés"
psql_ -c "SELECT id, left(nom, 60) AS nom, cree_le::date FROM ressources WHERE $TICKETS ORDER BY cree_le"
echo "== Travaux visés"
psql_ -c "SELECT statut, count(*) FROM travaux WHERE $TRAVAUX GROUP BY 1"

if [[ "${1:-}" != "--apply" ]]; then
  echo "Aperçu seulement. Relancer avec --apply pour supprimer."
  exit 0
fi

SAUVEGARDE=${SAUVEGARDE:-/var/tmp/cleanup-test-data-$(date +%Y%m%d-%H%M%S)}
mkdir -p "$SAUVEGARDE"
psql_ -c "\copy (SELECT * FROM ressources WHERE $TICKETS) TO STDOUT CSV HEADER" >"$SAUVEGARDE/tickets.csv"
psql_ -c "\copy (SELECT * FROM travaux WHERE $TRAVAUX) TO STDOUT CSV HEADER" >"$SAUVEGARDE/travaux.csv"
echo "Sauvegarde : $SAUVEGARDE"

psql_ <<SQL
BEGIN;
UPDATE ressources SET supprime_le = now() WHERE $TICKETS;
DELETE FROM travaux WHERE $TRAVAUX;
COMMIT;
SQL
echo "Terminé."
