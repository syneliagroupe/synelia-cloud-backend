"""VictoriaMetrics / VictoriaLogs : observabilité (journaux, métriques, liens de sortie).

Paire `VictoriaSimule` / `VictoriaReel`. En simulation, on renvoie des séries vides et un
lien prêt à l'emploi ; le réel n'est appelé que si l'URL VictoriaMetrics/VictoriaLogs/Grafana
correspondante est configurée — sinon on retombe silencieusement sur le comportement simulé
(même motif que `synelia.modules.ia_agents` pour LiteLLM : une intégration optionnelle absente
ne doit jamais faire échouer l'écran, elle le dégrade)."""

from __future__ import annotations

import math
import os
import time
from datetime import UTC, datetime
from typing import Any

import httpx

ENV_METRICS_URL = "SYNELIA_VICTORIAMETRICS_URL"
ENV_LOGS_URL = "SYNELIA_VICTORIALOGS_URL"
ENV_GRAFANA_URL = "SYNELIA_GRAFANA_URL"

# Largeur de fenêtre et pas d'échantillonnage par format `SparkChart` — CLAUDE.md du
# frontend borne l'observabilité à ces trois fenêtres, on n'en sert pas d'autres.
_FENETRES: dict[str, tuple[int, int]] = {
    "24h": (24 * 3600, 3600),
    "7j": (7 * 86400, 86400),
    "30j": (30 * 86400, 86400),
}


class VictoriaSimule:
    def lien_logs(self, recherche: str | None = None) -> str:
        base = os.environ.get(ENV_LOGS_URL, "https://victorialogs.synelia.cloud")
        q = f"?query={recherche}" if recherche else ""
        return f"{base}/select/logsql/ui{q}"

    def lien_grafana(self) -> str | None:
        return None

    def lien_centreon(self) -> str | None:
        return None

    def extrait_logs(
        self,
        ressource_id: str | None = None,
        niveau: str | None = None,
        depuis: str | None = None,
        recherche: str | None = None,
    ) -> list[dict[str, Any]]:
        return []

    def serie(self, promql: str, fenetre: str) -> list[dict[str, Any]]:
        """Points `{"ts": iso, "valeur": float}` sur la fenêtre — [] hors intégration réelle."""
        return []

    def valeur(self, promql: str) -> float:
        """Dernière valeur instantanée d'une requête PromQL — `0.0` hors intégration réelle."""
        return 0.0


class VictoriaReel(VictoriaSimule):
    def lien_grafana(self) -> str | None:
        return os.environ.get(ENV_GRAFANA_URL)

    def extrait_logs(
        self,
        ressource_id: str | None = None,
        niveau: str | None = None,
        depuis: str | None = None,
        recherche: str | None = None,
    ) -> list[dict[str, Any]]:
        url = os.environ.get(ENV_LOGS_URL)
        if not url:
            return super().extrait_logs(ressource_id, niveau, depuis, recherche)
        params: dict[str, Any] = {}
        if ressource_id:
            params["_stream_fields"] = ressource_id
        if recherche:
            params["query"] = recherche
        try:
            r = httpx.get(f"{url}/select/logsql/query", params=params, timeout=5)
            r.raise_for_status()
        except httpx.HTTPError:
            return []
        return r.json()

    def serie(self, promql: str, fenetre: str) -> list[dict[str, Any]]:
        url = os.environ.get(ENV_METRICS_URL)
        if not url:
            return []
        duree_s, pas_s = _FENETRES.get(fenetre, _FENETRES["24h"])
        fin = time.time()
        try:
            r = httpx.get(
                f"{url}/api/v1/query_range",
                params={"query": promql, "start": fin - duree_s, "end": fin, "step": pas_s},
                timeout=5,
            )
            r.raise_for_status()
        except httpx.HTTPError:
            return []
        resultats = r.json().get("data", {}).get("result", [])
        if not resultats:
            return []
        return [
            {
                "ts": datetime.fromtimestamp(float(ts), tz=UTC),
                "valeur": _nombre(valeur),
            }
            for ts, valeur in resultats[0].get("values", [])
        ]

    def valeur(self, promql: str) -> float:
        url = os.environ.get(ENV_METRICS_URL)
        if not url:
            return 0.0
        try:
            r = httpx.get(f"{url}/api/v1/query", params={"query": promql}, timeout=5)
            r.raise_for_status()
        except httpx.HTTPError:
            return 0.0
        resultats = r.json().get("data", {}).get("result", [])
        if not resultats:
            return 0.0
        try:
            return _nombre(resultats[0]["value"][1])
        except (KeyError, IndexError):
            return 0.0


def _nombre(valeur: Any) -> float:
    try:
        n = float(valeur)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if math.isnan(n) else n  # VictoriaMetrics renvoie parfois "NaN" en texte
