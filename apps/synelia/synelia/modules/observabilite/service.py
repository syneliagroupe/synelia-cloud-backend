"""Observabilité : alertes, événements de supervision, journaux, métriques."""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from synelia_contract import modeles as m
from synelia_db.modeles import Travail
from synelia_kernel.dates import maintenant
from synelia_kernel.ids import nouvel_id
from synelia_openstack import fournisseur
from synelia_openstack.victoria import VictoriaReel, VictoriaSimule

from synelia.depot import Depot
from synelia.deps.contexte import Contexte

depot = Depot(
    "regle_alerte", m.RegleAlerte, libelle="Règle d'alerte", champs_recherche=("cible", "metrique")
)


def victoria() -> VictoriaSimule:
    return fournisseur(VictoriaSimule, VictoriaReel)


# Traduction `métrique bornée du portail` -> requête PromQL réelle contre le stack
# victoria-metrics-k8s-stack. Couvre les métriques d'infrastructure (nœuds du cluster
# workload) déjà scrapées par vmagent sans rien de plus côté backend, plus les métriques
# HTTP émises par l'instrumentation OTel de l'API elle-même (`synelia.otel`).
_PROMQL: dict[str, str] = {
    "cpu": '100 * (1 - avg(rate(node_cpu_seconds_total{mode="idle"}[5m])))',
    "ram": "100 * (1 - (avg(node_memory_MemAvailable_bytes) / avg(node_memory_MemTotal_bytes)))",
    "disque": (
        '100 * (1 - (avg(node_filesystem_avail_bytes{fstype!="tmpfs"}) '
        '/ avg(node_filesystem_size_bytes{fstype!="tmpfs"})))'
    ),
    "reseau_entrant": 'sum(rate(node_network_receive_bytes_total{device!="lo"}[5m])) * 8 / 1e6',
    # Noms exacts vérifiés en direct sur ce déploiement (`opentelemetry-instrumentation-fastapi`
    # 0.65b0, sémantique HTTP historique par défaut — pas la nouvelle sémantique stable, qui
    # nommerait ces séries `http_server_request_duration_seconds*`) : ne pas deviner, revérifier
    # `curl .../api/v1/label/__name__/values` après toute mise à jour de cette dépendance.
    "rps": 'sum(rate(http_server_duration_milliseconds_count{job="synelia-api"}[5m]))',
    "latence_p95": (
        "histogram_quantile(0.95, sum(rate("
        'http_server_duration_milliseconds_bucket{job="synelia-api"}[5m])) by (le))'
    ),
    "erreurs_5xx": (
        'sum(rate(http_server_duration_milliseconds_count{job="synelia-api",'
        'http_status_code=~"5.."}[5m])) * 60'
    ),
}


_METRIQUE_PROMQL: dict[str, str] = {
    "processeur": _PROMQL["cpu"],
    "cpu": _PROMQL["cpu"],
    "mémoire": _PROMQL["ram"],
    "memoire": _PROMQL["ram"],
    "ram": _PROMQL["ram"],
    "disque": _PROMQL["disque"],
    "latence": _PROMQL["latence_p95"],
    "erreur": _PROMQL["erreurs_5xx"],
}


def _expr_regle(regle: m.RegleAlerte) -> str:
    base = next((v for k, v in _METRIQUE_PROMQL.items() if k in regle.metrique.lower()), None)
    seuil = re.search(r"([<>]=?)\s*(-?\d+(?:[.,]\d+)?)", regle.seuil or "")
    op = seuil.group(1) if seuil else ">"
    valeur = seuil.group(2).replace(",", ".") if seuil else "0"
    if base:
        return f"({base}) {op} {valeur}"
    # Métrique non cartographiée dans `_METRIQUE_PROMQL` : alerte honnête « la cible ne
    # répond plus » plutôt qu'un faux seuil raccroché à une série qui n'existe pas.
    return f'up{{synelia_cible="{regle.cible}"}} == 0'


def _duree_regle(plage: str | None) -> str:
    duree = re.search(r"(\d+)\s*(min|h|s)\b", plage or "")
    if not duree:
        return "5m"
    return f"{duree.group(1)}{ {'min': 'm', 'h': 'h', 's': 's'}[duree.group(2)] }"


def appliquer_regle_k8s(regle: m.RegleAlerte) -> None:
    """Traduit la règle en `VMRule` réel (vmalert/VictoriaMetrics) — no-op si aucun cluster
    PaaS n'est désigné (`SYNELIA_PAAS_CLUSTER_ID`), comme le reste de `k8s_workload`."""
    from synelia_openstack import k8s_workload

    k8s_workload.obtenir().appliquer_regle_alerte(
        regle.id,
        groupe="synelia-regles-alerte",
        alerte=regle.metrique,
        expr=_expr_regle(regle),
        duree=_duree_regle(regle.plage),
        labels={"cible": regle.cible, "canaux": ",".join(regle.canaux)},
        annotations={
            "summary": f"{regle.metrique} {regle.seuil} — {regle.cible}",
            "seuil": regle.seuil,
        },
    )


def supprimer_regle_k8s(regle_id: str) -> None:
    from synelia_openstack import k8s_workload

    k8s_workload.obtenir().supprimer_regle_alerte(regle_id)


def regle_vers_modele(corps: m.RegleAlerteCreation, ctx: Contexte) -> m.RegleAlerte:
    return m.RegleAlerte(
        id=nouvel_id(),
        cible=corps.cible,
        metrique=corps.metrique,
        seuil=corps.seuil,
        canaux=corps.canaux,
        plage=corps.plage or "24/7",
        escalade=corps.escalade,
        actif=bool(corps.actif if corps.actif is not None else True),
    )


async def evenements(ctx: Contexte) -> list[dict[str, Any]]:
    q = (
        select(Travail)
        .where(Travail.org_id == ctx.org_id)
        .order_by(Travail.started_at.desc())
        .limit(8)
    )
    lignes = list((await ctx.session.execute(q)).scalars().all())
    out: list[dict[str, Any]] = []
    for t in lignes:
        echec = t.statut in {"failed", "rolled_back"}
        evenement = m.EvenementSupervision(
            id=t.id,
            ts=t.started_at,
            gravite="majeure" if echec else "info",
            ressource=t.label or t.type,
            message=t.erreur.get("message")
            if t.erreur
            else "Travail de provisioning terminé avec succès.",
            site=None,
        )
        out.append(evenement.model_dump(mode="json"))
    return out


def _nombre_points(fenetre: str) -> int:
    return {"24h": 24, "7j": 7, "30j": 30}.get(fenetre, 24)


def metriques(fenetre: str, metriques_req: list[str] | None) -> dict[str, Any]:
    fenetre = fenetre if fenetre in {"24h", "7j", "30j"} else "24h"
    metriques_choisies = metriques_req or ["cpu", "ram", "disque", "reseau_entrant", "rps"]
    pas = {"24h": 3600, "7j": 86400, "30j": 86400}[fenetre]
    npoints = _nombre_points(fenetre)
    debut = maintenant() - timedelta(hours={"24h": 24, "7j": 168, "30j": 720}[fenetre])
    v = victoria()
    series = []
    for metrique in metriques_choisies:
        promql = _PROMQL.get(metrique)
        # `serie()` renvoie [] hors intégration réelle (VictoriaMetrics non configuré, ou
        # métrique non cartographiée) : on retombe alors sur le squelette à zéro plutôt que
        # de renvoyer une série vide — le format `SparkChart` attend toujours ses points.
        points = (v.serie(promql, fenetre) if promql else []) or [
            {"ts": debut + timedelta(seconds=pas * i), "valeur": 0.0} for i in range(npoints)
        ]
        series.append(
            m.Serie(
                metrique=metrique,
                unite=_unite(metrique),
                fenetre=fenetre,  # type: ignore[arg-type]
                points=[m.PointSerie(**p) for p in points],
            )
        )
    tuiles = [
        m.Tuile(cle="cpu.moyen", libelle="CPU moyen", valeur=v.valeur(_PROMQL["cpu"]), unite="%"),
        m.Tuile(
            cle="ram.utilisation", libelle="RAM utilisée", valeur=v.valeur(_PROMQL["ram"]), unite="%"
        ),
        m.Tuile(
            cle="disque.occupation",
            libelle="Disque occupé",
            valeur=v.valeur(_PROMQL["disque"]),
            unite="%",
        ),
    ]
    lien_grafana = v.lien_grafana()
    liens = m.LiensSortie(grafana=lien_grafana) if lien_grafana else None
    return {"tuiles": tuiles, "series": series, "liens": liens}


def _unite(metrique: str) -> str:
    return {
        "cpu": "%",
        "ram": "%",
        "disque": "%",
        "reseau_entrant": "Mb/s",
        "rps": "req/s",
        "latence_p95": "ms",
        "erreurs_5xx": "err/min",
    }.get(metrique, "unité")
