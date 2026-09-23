#!/usr/bin/env python3
"""Lab : supprime les routeurs vps-zone-net-rtr orphelins (consomment le pool external-net).

Garde les routeurs du projet seed ``espace-vps-zone`` (détectés par nom de projet).
Usage depuis vm-admin avec credentials plateforme dans l'environnement worker :

  docker cp scripts/lab/cleanup-orphan-vps-zone-routers.py synelia-cloud-backend-worker-1:/tmp/
  docker exec synelia-cloud-backend-worker-1 python3 /tmp/cleanup-orphan-vps-zone-routers.py
"""

from __future__ import annotations

import os
import sys

from openstack import connection


def main() -> int:
    conn = connection.Connection(
        auth_url=os.environ["SYNELIA_OS_AUTH_URL"],
        auth_type="v3applicationcredential",
        application_credential_id=os.environ["SYNELIA_OS_APPLICATION_CREDENTIAL_ID"],
        application_credential_secret=os.environ["SYNELIA_OS_APPLICATION_CREDENTIAL_SECRET"],
        region_name=os.environ.get("SYNELIA_OS_REGION", "RegionOne"),
    )
    seed = list(conn.identity.projects(name="espace-vps-zone"))
    keep: set[str] = set()
    if seed:
        pid = seed[0].id
        for r in conn.network.routers(project_id=pid, name="vps-zone-net-rtr"):
            keep.add(r.id)
        print(f"seed project {pid}: keep {len(keep)} router(s)", flush=True)
    else:
        print("warning: espace-vps-zone project not found — aborting", flush=True)
        return 1

    deleted = errors = 0
    for r in conn.network.routers():
        if r.name != "vps-zone-net-rtr" or r.id in keep:
            continue
        try:
            for p in conn.network.ports(device_id=r.id):
                try:
                    conn.network.remove_interface_from_router(r.id, port_id=p.id)
                except Exception as exc:  # noqa: BLE001
                    print(f"  skip detach {p.id[:8]} on {r.id[:8]}: {exc}", flush=True)
            if r.external_gateway_info:
                try:
                    conn.network.update_router(r.id, external_gateway_info=None)
                except Exception as exc:  # noqa: BLE001
                    print(f"  skip clear gw {r.id[:8]}: {exc}", flush=True)
            conn.network.delete_router(r.id, ignore_missing=True)
            deleted += 1
        except Exception as exc:
            errors += 1
            print(f"ERR {r.id[:8]}: {exc}", flush=True)

    nets = list(conn.network.networks(name="external-net"))
    ports = list(conn.network.ports(network_id=nets[0].id)) if nets else []
    print(f"done deleted={deleted} errors={errors} external-net_ports={len(ports)}", flush=True)
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
