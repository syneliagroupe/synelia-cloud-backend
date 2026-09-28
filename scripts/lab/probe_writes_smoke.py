#!/usr/bin/env python3
"""Safe write/delete smoke on lab (one feature chain per universe)."""

# ruff: noqa: S310
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

API = os.environ.get("API", "http://127.0.0.1:4000/v1").rstrip("/")
EMAIL = os.environ.get("PROBE_EMAIL", "admin@synelia.cloud")
MDP = os.environ.get("PROBE_MDP", "Synelia!2026")


def req(
    method: str,
    path: str,
    token: str,
    body: dict | None = None,
    org_id: str | None = None,
) -> tuple[int, dict | str]:
    url = f"{API}{path}"
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Accept": "application/json", "Authorization": f"Bearer {token}"}
    if org_id:
        headers["X-Organisation-Id"] = org_id
    if body is not None:
        headers["Content-Type"] = "application/json"
    r = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(r, timeout=120) as resp:
            raw = resp.read().decode()
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, raw


def login() -> str:
    c, d = req("POST", "/auth/connexion", "", {"email": EMAIL, "motDePasse": MDP})
    assert c == 200 and isinstance(d, dict)
    return d["accessToken"]


def premiere_org_id(token: str) -> str | None:
    c, d = req("GET", "/organisations", token)
    if c != 200 or not isinstance(d, dict):
        return os.environ.get("PROBE_ORG_ID")
    rows = d.get("donnees") or []
    if rows and isinstance(rows[0], dict):
        return rows[0]["id"]
    return os.environ.get("PROBE_ORG_ID")


def poll_travail(token: str, tid: str, max_s: int = 180) -> dict:
    import time

    for _ in range(max_s):
        c, d = req("GET", f"/travaux/{tid}", token)
        if c != 200:
            return {"statut": "poll_fail", "code": c, "body": d}
        if d.get("statut") in ("done", "failed", "rolled_back", "cancelled"):
            return d
        time.sleep(1)
    return {"statut": "timeout"}


def main() -> int:
    token = login()
    results: list[tuple[str, str, bool, str]] = []

    def record(univers: str, feat: str, ok: bool, detail: str) -> None:
        results.append((univers, feat, ok, detail))
        print(f"{'OK' if ok else 'FAIL'}\t{univers}\t{feat}\t{detail[:100]}", flush=True)

    # Infra: SG create/delete
    c, d = req(
        "POST",
        "/groupes-securite",
        token,
        {"nom": "demo-probe-sg", "description": "smoke", "espaceId": "espace-demo-abj"},
    )
    if c in (200, 201, 202):
        gid = d.get("id") or (d.get("donnees") or {}).get("id")
        if not gid and isinstance(d, dict) and "id" in str(d):
            gid = d.get("id")
        if isinstance(d, dict) and d.get("statut"):
            t = poll_travail(token, d["id"])
            gid = t.get("cibleId") or d.get("cible_id")
        c2, _ = req(
            "DELETE",
            f"/groupes-securite/{d.get('id', gid) if isinstance(d, dict) else gid}?confirmation=demo-probe-sg",
            token,
        )
        record(
            "Infrastructure", "SG create→delete", c2 in (200, 204, 202), f"create={c} delete={c2}"
        )
    else:
        record("Infrastructure", "SG create→delete", False, f"create={c} {d}")

    # Infra: FIP create/delete
    c, d = req("POST", "/ips", token, {"espaceId": "espace-demo-abj", "site": "ABJ"})
    if c in (200, 201, 202):
        tid = d.get("id")
        t = poll_travail(token, tid) if tid else d
        fid = None
        if isinstance(t, dict):
            for task in t.get("taches") or []:
                if "192.168" in str(task.get("message", "")):
                    pass
        c_l, lst = req("GET", "/ips", token)
        adresse = d.get("adresse") if isinstance(d, dict) else None
        if c_l == 200 and isinstance(lst, dict):
            for row in lst.get("donnees") or []:
                if adresse and row.get("adresse") == adresse:
                    fid = row["id"]
                    adresse = row.get("adresse")
                    break
        if fid:
            c2, _ = req("DELETE", f"/ips/{fid}?confirmation={adresse}", token)
            record("Infrastructure", "FIP create→delete", c2 in (200, 204, 202), f"delete={c2}")
        else:
            record(
                "Infrastructure",
                "FIP create→delete",
                t.get("statut") == "done",
                str(t.get("statut")),
            )
    else:
        record("Infrastructure", "FIP create→delete", False, f"create={c}")

    # IA: agent create → invoke skip → delete
    c, d = req(
        "POST",
        "/ia/agents",
        token,
        {"nom": "Probe Demo", "consigne": "Réponds OK", "modele": "z-ai/glm-5.3-flash"},
    )
    if c in (200, 201):
        aid = d.get("id")
        q = urllib.parse.urlencode({"confirmation": "Probe Demo"})
        c2, _ = req("DELETE", f"/ia/agents/{aid}?{q}", token)
        record("IA & Agents", "Agent create→delete", c2 in (200, 204), f"create={c}")
    else:
        record("IA & Agents", "Agent create→delete", False, str(d)[:80])

    # Web DNS zone create/delete
    c, d = req(
        "POST", "/web/dns", token, {"domaine": "probe-demo-smoke.ci", "espaceId": "espace-demo-abj"}
    )
    if c in (200, 201, 202):
        tid = d.get("id")
        t = poll_travail(token, tid) if tid else {}
        zid = t.get("cibleId") if isinstance(t, dict) else None
        if not zid and c == 201:
            zid = d.get("id")
        c_l, lst = req("GET", "/web/dns", token)
        zid2 = None
        if c_l == 200:
            for z in (lst.get("donnees") if isinstance(lst, dict) else []) or []:
                if z.get("domaine") == "probe-demo-smoke.ci":
                    zid2 = z["id"]
        zdel = zid2 or zid
        if zdel:
            c2, _ = req("DELETE", f"/web/dns/{zdel}?confirmation=probe-demo-smoke.ci", token)
            record("Web Cloud", "DNS zone create→delete", c2 in (200, 204, 202), f"delete={c2}")
        else:
            record("Web Cloud", "DNS zone create→delete", False, str(t)[:80])
    else:
        record("Web Cloud", "DNS zone create→delete", False, f"create={c}")

    # Infra: RustFS bucket + clé S3 (sync, amont réel si SYNELIA_MINIO_URL posée)
    espace = os.environ.get("PROBE_ESPACE_ID", "espace-demo-abj")
    org_id = premiere_org_id(token)
    bucket_nom = "probe-rustfs-smoke"
    if org_id:
        c, d = req(
            "POST",
            "/buckets",
            token,
            {
                "espaceId": espace,
                "nom": bucket_nom,
                "region": "ABJ",
                "classe": "chaud",
                "versioning": False,
                "policy": "prive",
            },
            org_id=org_id,
        )
        if c == 201 and isinstance(d, dict):
            bid = d["id"]
            c_k, cle = req(
                "POST",
                "/cles-s3",
                token,
                {"nom": "probe-rustfs-key", "buckets": [bucket_nom], "droits": "lecture"},
                org_id=org_id,
            )
            ok_key = False
            if c_k == 201 and isinstance(cle, dict):
                cid = (cle.get("cle") or {}).get("id")
                if cid:
                    c_r, _ = req(
                        "DELETE",
                        f"/cles-s3/{cid}?confirmation=probe-rustfs-key",
                        token,
                        org_id=org_id,
                    )
                    ok_key = c_r == 204
            c_b, _ = req(
                "DELETE",
                f"/buckets/{bid}?confirmation={bucket_nom}",
                token,
                org_id=org_id,
            )
            record(
                "Infrastructure",
                "RustFS bucket+clé S3",
                c_b == 204 and ok_key,
                f"bucket={c} key={c_k} revoke={ok_key} delete={c_b}",
            )
        else:
            record("Infrastructure", "RustFS bucket+clé S3", False, f"create={c} {str(d)[:80]}")
    else:
        record("Infrastructure", "RustFS bucket+clé S3", False, "org_id introuvable")

    # Applications: environnement (direct write)
    c, d = req("POST", "/environnements", token, {"nom": "probe-env"})
    if c in (200, 201):
        eid = d.get("id")
        c2, _ = req("DELETE", f"/environnements/{eid}?confirmation=probe-env", token)
        record("Applications", "Environnement create→delete", c2 in (200, 204, 202), f"delete={c2}")
    else:
        record("Applications", "Environnement create→delete", False, f"create={c}")

    fails = sum(1 for *_, ok, _ in results if not ok)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
