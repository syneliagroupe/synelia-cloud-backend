#!/usr/bin/env python3
"""Installe et sonde chaque application PHP du marketplace sur un hébergement lab.

  set -a && . .env && set +a
  DEMO_WEB_ORG_ID=... HEBERGEMENT_ID=... DEMO_DOMAIN=demo-28d14e3a.com \\
  LB_FIP=<ip du LB> uv run python scripts/lab/test-marketplace-apps.py
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API = os.environ.get("API", "https://api.cloud.dev01.ovh.smile.ci/v1").rstrip("/")
DOMAIN = os.environ["DEMO_DOMAIN"]
LB_FIP = os.environ["LB_FIP"]

# prefix → phpVersion (contrat site.installer)
APPS: list[tuple[str, str]] = [
    ("matomo", "8.4"),
    ("bookstack", "8.3"),
    ("moodle", "8.3"),
    ("opencart", "8.2"),
    ("grav", "8.4"),
    ("dolibarr", "8.2"),
]


def _req(method: str, path: str, *, token: str, org_id: str, body: dict | None = None):
    url = f"{API}{path}"
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
        "X-Organisation-Id": org_id,
    }
    req = urllib.request.Request(
        url, data=json.dumps(body).encode() if body else None, headers=headers, method=method
    )
    with urllib.request.urlopen(req, timeout=600) as resp:
        raw = resp.read().decode()
        return json.loads(raw) if raw else {}


def _connexion() -> tuple[str, str]:
    org = os.environ["DEMO_WEB_ORG_ID"]
    admin = os.environ.get("PROBE_EMAIL", "admin@synelia.cloud")
    mdp = os.environ.get("SYNELIA_SEED_ADMIN_MOT_DE_PASSE") or os.environ.get("PROBE_MDP", "")
    if not mdp:
        sys.exit("SYNELIA_SEED_ADMIN_MOT_DE_PASSE requis")
    a = _req("POST", "/auth/connexion", token="", org_id=org, body={"email": admin, "motDePasse": mdp})
    e = _req(
        "POST",
        f"/organisations/{org}/emprunt-identite",
        token=a["accessToken"],
        org_id=org,
        body={"motif": "lab test marketplace", "dureeMin": 120, "ecriture": True},
    )
    return e["accessToken"], org


def _supprimer_site(token: str, org: str, site_id: str, hote: str, label: str) -> bool:
    travail = _req(
        "DELETE",
        f"/web/sites/{site_id}?confirmation={urllib.parse.quote(hote, safe='')}",
        token=token,
        org_id=org,
    )
    if not travail.get("id"):
        print(f"suppression {label}: réponse inattendue", travail, file=sys.stderr)
        return False
    t = _attendre_travail(token, org, travail["id"], f"suppr-{label}")
    return t.get("statut") == "done"


def _attendre_travail(token: str, org: str, tid: str, label: str) -> dict:
    for i in range(180):
        t = _req("GET", f"/travaux/{tid}", token=token, org_id=org)
        st = t.get("statut")
        if st in ("done", "failed", "rolled_back"):
            if st != "done":
                print(f"ÉCHEC {label}:", json.dumps(t, indent=2), file=sys.stderr)
                return t
            return t
        if i % 6 == 0:
            print(f"  … {label} ({st})")
        time.sleep(10)
    print(f"timeout {label}", file=sys.stderr)
    return {"statut": "timeout"}


def _sonde_http(hote: str) -> tuple[int, str]:
    req = urllib.request.Request(
        f"http://{LB_FIP}/",
        headers={"Host": hote, "User-Agent": "synelia-lab-marketplace/1"},
        method="GET",
    )
    # Ne pas suivre les redirects vers HTTPS (cert wildcard LB, pas le sous-domaine app).
    opener = urllib.request.build_opener(urllib.request.HTTPHandler())
    try:
        with opener.open(req, timeout=30) as resp:
            body = resp.read(800).decode(errors="replace")
            return resp.status, body[:120].replace("\n", " ")
    except urllib.error.HTTPError as e:
        body = e.read(400).decode(errors="replace")
        return e.code, body[:120].replace("\n", " ")
    except Exception as exc:  # noqa: BLE001
        return 0, str(exc)[:120]


def main() -> int:
    token, org = _connexion()
    hid = os.environ.get("HEBERGEMENT_ID")
    if not hid:
        hebs = _req("GET", "/web/hebergements", token=token, org_id=org)
        hid = next(h["id"] for h in (hebs.get("donnees") or []) if h.get("domaine") == DOMAIN)

    sites = _req("GET", "/web/sites", token=token, org_id=org)
    hosts = {s.get("hote"): s for s in (sites.get("donnees") or [])}

    resultats: list[tuple[str, str, str, str]] = []
    retest = {
        p.strip()
        for p in os.environ.get("RETEST_APPS", "").split(",")
        if p.strip()
    }

    for prefix, php in APPS:
        hote = f"{prefix}.{DOMAIN}"
        print(f"\n=== {prefix} ({hote}) ===")
        if prefix in retest and hote in hosts:
            sid = hosts[hote]["id"]
            print(f"  RETEST: suppression de {hote} ({sid})…")
            if _supprimer_site(token, org, sid, hote, prefix):
                sites = _req("GET", "/web/sites", token=token, org_id=org)
                hosts = {s.get("hote"): s for s in (sites.get("donnees") or [])}
            else:
                resultats.append((prefix, hote, "delete", "failed"))
                continue
        if hote not in hosts or hosts[hote].get("statut") not in ("actif", "installation"):
            travail = _req(
                "POST",
                "/web/sites",
                token=token,
                org_id=org,
                body={
                    "hebergementId": hid,
                    "site": {"hote": hote, "type": "php", "ssl": True, "phpVersion": php},
                },
            )
            if travail.get("statut") == "done":
                print("install déjà terminé (sync)")
            elif travail.get("id"):
                t = _attendre_travail(token, org, travail["id"], prefix)
                if t.get("statut") != "done":
                    resultats.append((prefix, hote, "install", t.get("statut", "?")))
                    continue
            else:
                print("réponse inattendue:", travail, file=sys.stderr)
                resultats.append((prefix, hote, "install", "api"))
                continue
        else:
            print("site déjà présent, statut", hosts[hote].get("statut"))

        # Laisser les conteneurs démarrer (images lourdes)
        attente = 30 if prefix in ("moodle", "bookstack") else 15
        time.sleep(attente)
        for essai in range(10 if prefix == "moodle" else 6):
            code, extrait = _sonde_http(hote)
            if code in (200, 301, 302, 303, 307, 308):
                print(f"HTTP {code} OK — {extrait!r}")
                resultats.append((prefix, hote, "http", f"{code}"))
                break
            if code == 502 and essai < 5:
                print(f"  502, nouvel essai dans 20s…")
                time.sleep(20)
                continue
            print(f"HTTP {code} — {extrait!r}")
            resultats.append((prefix, hote, "http", f"{code}"))
            break

    print("\n--- Résumé ---")
    echecs = 0
    for prefix, hote, phase, val in resultats:
        ok = (phase == "http" and val.isdigit() and int(val) < 400) or val == "done"
        mark = "OK" if ok else "FAIL"
        if not ok:
            echecs += 1
        print(f"{mark:4} {prefix:12} {phase:8} {val}  ({hote})")

    return 1 if echecs else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except urllib.error.HTTPError as e:
        print(e.read().decode(), file=sys.stderr)
        raise SystemExit(1) from e
