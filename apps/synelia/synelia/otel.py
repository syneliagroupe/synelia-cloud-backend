"""Télémétrie OpenTelemetry — no-op tant que `SYNELIA_OTEL_ENDPOINT` n'est pas configuré.

`opentelemetry.metrics.get_meter(...)` renvoie une implémentation no-op tant qu'aucun
`MeterProvider` n'a été positionné (motif « proxy » de l'API OTel) : importer ce module et
créer ses instruments au chargement ne part vers aucun réseau et ne coûte rien tant que
`configurer()` n'a pas été appelée avec un endpoint réel — même motif que les autres
intégrations optionnelles de ce dépôt (`SYNELIA_LITELLM_URL`, `SYNELIA_VICTORIAMETRICS_URL`)."""

from __future__ import annotations

import os
import socket

from opentelemetry import metrics

ENV_ENDPOINT = "SYNELIA_OTEL_ENDPOINT"

meter = metrics.get_meter("synelia.travaux")

compteur_travaux = meter.create_counter(
    "synelia_travaux_total",
    description="Travaux de provisioning terminés, par type et statut.",
)
histogramme_duree_travaux = meter.create_histogram(
    "synelia_travaux_duree_secondes",
    unit="s",
    description="Durée d'un travail de provisioning terminé, par type.",
)


def configurer(app: object, *, version: str, env: str) -> None:
    """Branche l'export OTLP réel + l'instrumentation FastAPI vers `SYNELIA_OTEL_ENDPOINT`.

    No-op silencieux si la variable n'est pas définie : c'est le cas par défaut en test et
    en développement local, seul dev01 la positionne vers le collecteur OTel du cluster."""
    endpoint = os.environ.get(ENV_ENDPOINT)
    if not endpoint:
        return
    from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.resources import Resource

    ressource = Resource.create(
        {
            "service.name": "synelia-api",
            "service.version": version,
            "deployment.environment": env,
            # Avec `SYNELIA_API_WORKERS=N` (étape 1.7 de docs/PLAN-ARCHITECTURE-SUITE.md),
            # plusieurs processus uvicorn partagent le même `service.name` : sans cet
            # identifiant, VictoriaMetrics reçoit des compteurs entrelacés entre workers.
            "service.instance.id": f"{socket.gethostname()}:{os.getpid()}",
        }
    )
    lecteur = PeriodicExportingMetricReader(
        OTLPMetricExporter(endpoint=endpoint, insecure=True), export_interval_millis=15000
    )
    metrics.set_meter_provider(MeterProvider(resource=ressource, metric_readers=[lecteur]))
    FastAPIInstrumentor.instrument_app(app)
