#!/usr/bin/env python3
"""Lab : nettoie le projet OpenStack zone VPS (espace-vps-zone).

Symptôme : `hebergement.creer` → Nova `No valid host was found` alors que le lab a encore
de la capacité — des VM de test (projets, hébergements pytest) saturent le tenant.

Usage (vm-admin) :
  uv run python scripts/lab/prune-vps-zone-project-vms.py
  uv run python scripts/lab/prune-vps-zone-project-vms.py --dry-run
  uv run python scripts/lab/prune-vps-zone-project-vms.py --hebergement-test
"""

from __future__ import annotations

import argparse
import asyncio

from synelia.deps.contexte import Contexte, Principal
from synelia.modules.web_hebergement.service import zone_vps_secrets
from synelia_db.session import fabrique, initialiser_schema
from synelia_kernel.config import reglages
from synelia_openstack.fabrique import connexion
from types import SimpleNamespace

# Aligné sur `web_hebergement.service._HEBERGEMENTS_PROTEGES` (serveurs lab de référence).
_SRV_PROTEGES = {"srv-01a073a1", "srv-01a076fe"}


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--hebergement-test",
        action="store_true",
        help="Supprime aussi les VM hébergement pytest (`srv-*`) sauf les serveurs protégés.",
    )
    args = parser.parse_args()
    await initialiser_schema()
    async with fabrique()() as session:
        org = reglages().vps_zone_org_id
        if not org:
            print("SYNELIA_VPS_ZONE_ORG_ID absent — rien à faire.")
            return 0
        ctx = Contexte(
            request=SimpleNamespace(
                headers={}, client=None, state=SimpleNamespace(correlation_id="prune-vps")
            ),
            session=session,
            reglages=reglages(),
            correlation_id="prune-vps",
            principal=Principal(
                utilisateur_id=None,
                email="lab@synelia.cloud",
                nom="Lab prune",
                org_id=org,
                role="platform_operator",
                equipe=True,
                role_equipe="platform_operator",
            ),
        )
        zone = await zone_vps_secrets(ctx)
        pid = zone.get("projet_id")
        if not pid:
            print("Zone VPS non provisionnée (projet_id absent).")
            return 1
        c = connexion()
        serveurs = list(c.compute.servers(all_projects=True, project_id=pid))
        cibles = [s for s in serveurs if (s.name or "").startswith("vm-projet-")]
        if args.hebergement_test:
            cibles.extend(
                s
                for s in serveurs
                if (s.name or "").startswith("srv-") and (s.name or "") not in _SRV_PROTEGES
            )
        cibles = list({s.id: s for s in cibles}.values())
        print(
            f"projet {pid} — {len(cibles)} VM à supprimer "
            f"(vm-projet-*{' + srv-* test' if args.hebergement_test else ''})"
        )
        for s in cibles:
            if args.dry_run:
                print("  dry-run delete", s.name, s.id)
                continue
            print("  delete", s.name, s.id)
            c.compute.delete_server(s.id)
            try:
                c.compute.wait_for_delete(s, wait=180)
            except Exception as exc:  # noqa: BLE001
                print("    wait:", exc)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
