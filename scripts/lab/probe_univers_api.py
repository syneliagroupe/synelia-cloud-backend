#!/usr/bin/env python3
"""Probe GET /v1/* list endpoints per universe (demo prep). Usage on vm-admin:
  API=http://127.0.0.1:4000/v1 python3 scripts/lab/probe_univers_api.py
"""
# ruff: noqa: S310, S108

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

API = os.environ.get("API", "http://127.0.0.1:4000/v1").rstrip("/")
EMAIL = os.environ.get("PROBE_EMAIL", "admin@synelia.cloud")
MDP = os.environ.get("PROBE_MDP", "Synelia!2026")

# (universe, section, frontend_href, method, path, note)
PROBES: list[tuple[str, str, str, str, str, str]] = [
    # Global
    ("Global", "Tableau de bord", "/app", "GET", "/tableau-de-bord", "synthese client"),
    ("Global", "Supervision", "/app/observabilite", "GET", "/observabilite/alertes", "liste alertes"),
    ("Global", "Facturation", "/app/facturation", "GET", "/facturation/factures", ""),
    ("Global", "Support", "/app/support", "GET", "/support/tickets", ""),
    ("Global", "Documentation", "/app/docs", "GET", "/docs/parcours", "catalogue parcours"),
    ("Global", "Paramètres", "/app/parametres", "GET", "/moi", "compte actif"),
    # Infrastructure
    ("Infrastructure", "Accueil", "/app/infrastructure", "GET", "/espaces", "via espaces"),
    ("Infrastructure", "Espaces Cloud", "/app/espaces", "GET", "/espaces", ""),
    ("Infrastructure", "VM", "/app/vms", "GET", "/vms", ""),
    ("Infrastructure", "VM catalogue", "/app/vms", "GET", "/catalogue/gabarits", ""),
    ("Infrastructure", "Kubernetes", "/app/kubernetes", "GET", "/kubernetes", ""),
    ("Infrastructure", "Load balancers", "/app/reseau/lb", "GET", "/load-balancers", ""),
    ("Infrastructure", "Réseau", "/app/reseau", "GET", "/reseaux", ""),
    ("Infrastructure", "IP", "/app/reseau", "GET", "/ips", ""),
    ("Infrastructure", "SG", "/app/reseau", "GET", "/groupes-securite", ""),
    ("Infrastructure", "VPN", "/app/reseau", "GET", "/vpn", ""),
    ("Infrastructure", "Volumes", "/app/stockage", "GET", "/volumes", ""),
    ("Infrastructure", "S3", "/app/objet", "GET", "/buckets", ""),
    ("Infrastructure", "Clés S3", "/app/objet", "GET", "/cles-s3", ""),
    ("Infrastructure", "Bases", "/app/bases", "GET", "/bases", ""),
    ("Infrastructure", "Sauvegarde plans", "/app/sauvegarde", "GET", "/sauvegarde/plans", ""),
    ("Infrastructure", "PRA", "/app/pra", "GET", "/pra", ""),
    ("Infrastructure", "Conformité sauvegarde", "/app/sauvegarde", "GET", "/sauvegarde/conformite", ""),
    # Applications
    ("Applications", "Accueil", "/app/applications", "GET", "/projets/synthese", "synthese projets"),
    ("Applications", "Projets", "/app/applications/projets", "GET", "/projets", ""),
    ("Applications", "Déploiements", "/app/applications/deploiements", "GET", "/deploiements", ""),
    ("Applications", "Domaines applicatifs", "/app/applications/routage", "GET", "/domaines-applicatifs", ""),
    ("Applications", "Zone applicative", "/app/applications/routage", "GET", "/zone-applicative", ""),
    ("Applications", "Routage", "/app/applications/routage", "GET", "/routage", ""),
    ("Applications", "Modèles", "/app/applications", "GET", "/modeles", "catalogue déploiement"),
    # IA
    ("IA & Agents", "Modèles", "/app/ia/modeles", "GET", "/ia/modeles", ""),
    ("IA & Agents", "Agents", "/app/ia/agents", "GET", "/ia/agents", ""),
    ("IA & Agents", "Orchestration", "/app/ia/orchestration", "GET", "/ia/flux", ""),
    ("IA & Agents", "Connaissances", "/app/ia/connaissances", "GET", "/ia/connaissances", ""),
    ("IA & Agents", "Clés IA", "/app/ia/integrations", "GET", "/ia/cles", ""),
    # Web Cloud
    ("Web Cloud", "Domaines", "/app/web/domaines", "GET", "/web/domaines", ""),
    ("Web Cloud", "Dispo domaine", "/app/web/domaines", "GET", "/web/domaines/disponibilite?nom=test-demo.ci", ""),
    ("Web Cloud", "Hébergements", "/app/web/hebergement", "GET", "/web/hebergements", ""),
    ("Web Cloud", "Bases web", "/app/web/bases", "GET", "/web/bases", ""),
    ("Web Cloud", "Emails", "/app/web/emails", "GET", "/web/emails", ""),
    ("Web Cloud", "Drive", "/app/web/drive", "GET", "/web/drive", ""),
    ("Web Cloud", "Sites", "/app/web/applications", "GET", "/web/sites", ""),
    ("Web Cloud", "SSL", "/app/web/ssl", "GET", "/web/ssl", ""),
    ("Web Cloud", "Backup", "/app/web/backup", "GET", "/web/backup", ""),
    ("Web Cloud", "DNS", "/app/web/domaines", "GET", "/web/dns", ""),
    ("Web Cloud", "SMTP clés", "/app/smtp", "GET", "/web/smtp/cles", ""),
    ("Web Cloud", "SMTP webhooks", "/app/smtp", "GET", "/web/smtp/webhooks", ""),
    # IAM
    ("IAM", "Membres", "/app/membres", "GET", "/membres", ""),
    ("IAM", "Invitations", "/app/membres", "GET", "/invitations", ""),
    ("IAM", "Sécurité sessions", "/app/securite", "GET", "/securite/sessions", ""),
    ("IAM", "Clés API org", "/app/securite", "GET", "/securite/cles-api", ""),
    ("IAM", "Audit", "/app/securite", "GET", "/audit", ""),
    ("IAM", "SSO", "/app/securite", "GET", "/securite/sso", ""),
    # Admin (super)
    ("Super admin", "Vue plateforme", "/admin", "GET", "/admin/tableau-de-bord", ""),
    ("Super admin", "Organisations", "/admin/organisations", "GET", "/organisations", ""),
    ("Super admin", "Catalogue offres", "/admin/catalogue", "GET", "/admin/catalogue/offres", ""),
    ("Super admin", "Backends", "/admin/capacite", "GET", "/admin/backends", ""),
    ("Super admin", "Capacité agrégée", "/admin/capacite", "GET", "/admin/capacite", "vue agrégée backends"),
    ("Super admin", "Jetons admin", "/admin", "GET", "/admin/jetons", ""),
    ("Super admin", "Tickets plateforme", "/admin/tickets", "GET", "/admin/tickets", ""),
    ("Super admin", "Travaux plateforme", "/admin", "GET", "/admin/travaux", ""),
    # Public
    ("Vitrine", "Datacenters", "/", "GET", "/public/datacenters", "sans auth"),
]


def _req(method: str, path: str, token: str | None = None, body: dict | None = None) -> tuple[int, str]:
    url = f"{API}{path}" if path.startswith("/") else f"{API}/{path}"
    data = json.dumps(body).encode() if body else None
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if body:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def login() -> str:
    code, raw = _req("POST", "/auth/connexion", body={"email": EMAIL, "motDePasse": MDP})
    if code != 200:
        raise SystemExit(f"login failed {code}: {raw}")
    data = json.loads(raw)
    return data.get("accessToken") or data.get("jetonAcces") or data["access_token"]


def main() -> int:
    token = login()
    results = []
    for univers, section, href, method, path, note in PROBES:
        auth = None if path.startswith("/public/") else token
        code, snippet = _req(method, path, token=auth)
        ok = 200 <= code < 300
        results.append(
            {
                "univers": univers,
                "section": section,
                "frontend": href,
                "api": f"{method} {path}",
                "status": code,
                "ok": ok,
                "note": note,
                "snippet": snippet[:120],
            }
        )
        mark = "OK" if ok else "FAIL"
        print(f"{mark}\t{code}\t{univers}/{section}\t{path}", flush=True)

    out = os.environ.get("PROBE_JSON", "/tmp/univers_probe.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    fails = sum(1 for r in results if not r["ok"])
    print(f"\n{len(results) - fails}/{len(results)} passed → {out}", flush=True)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
