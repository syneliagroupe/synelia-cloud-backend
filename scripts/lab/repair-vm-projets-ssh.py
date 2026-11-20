#!/usr/bin/env python3
"""Rattrape les secrets SSH/LB manquants sur les projets applicatifs cible `vm`.

Usage (vm-admin ou poste avec .env lab) :
  uv run python scripts/lab/repair-vm-projets-ssh.py
  uv run python scripts/lab/repair-vm-projets-ssh.py --projet-id <uuid>
"""

from __future__ import annotations

import argparse
import asyncio
import os

from synelia.deps.contexte import Contexte, Principal
from synelia.deps.pagination import PageParams
from synelia.modules.projets import service as projets_service
from synelia.modules.projets.service import depot_projet
from synelia_db.session import fabrique, initialiser_schema
from synelia_kernel.config import reglages
from types import SimpleNamespace


async def _ctx(session, org_id: str | None) -> Contexte:
    return Contexte(
        request=SimpleNamespace(
            headers={}, client=None, state=SimpleNamespace(correlation_id="repair-vm-projets")
        ),
        session=session,
        reglages=reglages(),
        correlation_id="repair-vm-projets",
        principal=Principal(
            utilisateur_id=None,
            email="lab@synelia.cloud",
            nom="Repair VM projets",
            org_id=org_id,
            role="platform_operator",
            equipe=True,
            role_equipe="platform_operator",
        ),
    )


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--projet-id", action="append", dest="projet_ids")
    args = parser.parse_args()
    await initialiser_schema()
    async with fabrique()() as session:
        org_id = os.environ.get("REPAIR_ORG_ID") or reglages().vps_zone_org_id
        ctx = await _ctx(session, org_id)
        if args.projet_ids:
            projets = [await depot_projet.obtenir(ctx, pid) for pid in args.projet_ids]
        else:
            page = await depot_projet.lister(ctx, PageParams(page=1, par_page=500))
            projets = [p for p in page["donnees"] if getattr(p, "cible", "k8s") == "vm"]
        from synelia.modules.web_hebergement import service as web_heb

        zone = await web_heb.zone_vps_secrets(ctx)
        if not zone.get("projet_id"):
            print("Zone VPS non provisionnée — rien à réparer.")
            return 1
        ok = 0
        for projet in projets:
            try:
                secrets = await depot_projet.secrets(ctx, projet.id)
            except Exception:
                secrets = {}
            if not secrets.get("vm_serveur_id"):
                print(f"skip {projet.nom} ({projet.id}) — pas de VM")
                continue
            print(f"repair {projet.nom} ({projet.id}) …", flush=True)
            try:
                await projets_service._assurer_vm_projet(ctx, projet)  # noqa: SLF001
                ok += 1
                print("  ok")
            except Exception as exc:  # noqa: BLE001
                print(f"  FAIL: {exc}")
        await session.commit()
        print(f"\n{ok}/{len(projets)} projet(s) traités.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
