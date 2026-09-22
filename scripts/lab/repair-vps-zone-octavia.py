#!/usr/bin/env python3
"""Lab : recrée le LB vps-zone (VIP Neutron manquant) et re-pose pools L7 des hébergements en_ligne."""

from __future__ import annotations

import asyncio
import os

OLD_LB = os.environ.get("VPS_ZONE_LB_ID", "6bcc4ab4-3f67-4b60-a9fc-62600328012d")
FIP = os.environ.get("VPS_ZONE_LB_FIP", "192.168.20.231")
ESPACE = os.environ.get("SYNELIA_VPS_ZONE_ESPACE_ID", "b2468d93-699a-4b40-aabc-317bd67928ae")
LISTENER_HTTP = None  # filled after create


async def main() -> int:
    from types import SimpleNamespace

    from synelia.deps.contexte import Contexte, Principal
    from synelia.modules.espaces.service import (
        _ctx_amorcage_plateforme,
        depot_plateforme,
    )
    from synelia.modules.web_hebergement.service import (
        amont_network,
        zone_vps_secrets,
    )
    from synelia.modules.web_hebergement.service import (
        depot as depot_heb,
    )
    from synelia_db.session import fabrique, initialiser_schema
    from synelia_kernel.config import reglages
    from synelia_openstack.network import NetworkOpenStack

    await initialiser_schema()
    async with fabrique()() as session:
        ctx_plat = _ctx_amorcage_plateforme(session)
        zone = await depot_plateforme.secrets(ctx_plat, ESPACE)
        projet_id = zone["projet_id"]
        reseau_id = zone["reseau_id"]
        sous_reseau_id = zone.get("sous_reseau_id") or ""

        n_plat = NetworkOpenStack()
        if OLD_LB and os.environ.get("SKIP_LB_DELETE") != "1":
            print(f"suppression LB {OLD_LB}…")
            await asyncio.to_thread(n_plat.supprimer_load_balancer, OLD_LB)

        print("création vps-zone-lb…")
        lb = await asyncio.to_thread(
            n_plat.creer_load_balancer,
            projet_id=projet_id,
            nom="vps-zone-lb",
            reseau_id=reseau_id,
            layer="l7",
            exposure="public",
            listeners=[{"protocole": "http", "port": 80}],
        )
        new_lb = lb["id"]
        listener_id = lb.get("listener_id") or ""
        print(
            "LB",
            new_lb,
            "listener",
            listener_id,
            "vip",
            lb.get("vip"),
            "fip",
            lb.get("fip_adresse"),
        )

        # Réutiliser le FIP lab historique si présent
        from synelia_openstack.fabrique import connexion

        conn = connexion()
        fip_obj = None
        for f in conn.network.ips(floating_ip_address=FIP):
            fip_obj = f
            break
        if fip_obj and not fip_obj.port_id:
            addr = await asyncio.to_thread(
                n_plat.associer_ip_flottante_lb_existante, fip_obj.id, new_lb
            )
            print("FIP réassocié", FIP, "->", addr)
        elif not lb.get("fip_adresse"):
            print("WARN: pas de FIP publique sur le LB")

        patch = {
            "lb_id": new_lb,
            "lb_listener_id": listener_id,
            "sous_reseau_id": sous_reseau_id,
        }
        await depot_plateforme.definir_secrets(ctx_plat, ESPACE, patch)
        await session.commit()
        print("secrets zone mis à jour", patch)

        org_id = reglages().vps_zone_org_id or "01a0b10b-8f4d-7633-bd9a-75379da461a3"
        ctx_org = Contexte(
            request=SimpleNamespace(
                headers={}, client=None, state=SimpleNamespace(correlation_id="repair-octavia")
            ),
            session=session,
            reglages=reglages(),
            correlation_id="repair-octavia",
            principal=Principal(
                utilisateur_id=None,
                email="repair@synelia.cloud",
                nom="Repair Octavia",
                org_id=org_id,
                role="platform_operator",
                equipe=True,
                role_equipe="platform_operator",
            ),
        )
        zone = await zone_vps_secrets(ctx_org)
        n = amont_network()

        hebergements = await depot_heb.tous(
            ctx_org, filtre=lambda h: h.serveur.statut == "en_ligne"
        )
        print(f"{len(hebergements)} hébergement(s) en_ligne à re-router")

        for h in hebergements:
            hid = h.id
            ip = h.serveur.ip or ""
            if not ip:
                print("SKIP", hid, "pas d'IP")
                continue
            try:
                sec = await depot_heb.secrets(ctx_org, hid)
            except Exception:
                sec = {}
            for key in ("lb_pool_id", "lb_membre_id", "lb_policy_id"):
                oid = sec.get(key)
                if not oid:
                    continue
                try:
                    if key == "lb_policy_id":
                        await asyncio.to_thread(
                            n.supprimer_regle_hote,
                            oid,
                            loadbalancer_id=zone["lb_id"],
                        )
                    elif key == "lb_membre_id" and sec.get("lb_pool_id"):
                        await asyncio.to_thread(
                            n.supprimer_membre,
                            sec["lb_pool_id"],
                            oid,
                            loadbalancer_id=zone["lb_id"],
                        )
                    elif key == "lb_pool_id":
                        await asyncio.to_thread(
                            n.supprimer_pool, oid, loadbalancer_id=zone["lb_id"]
                        )
                except Exception as exc:  # noqa: BLE001
                    print("  cleanup", key, oid, exc)

            pool = await asyncio.to_thread(
                n.creer_pool,
                loadbalancer_id=zone["lb_id"],
                nom=f"pool-{hid[:8]}",
            )
            membre = await asyncio.to_thread(
                n.ajouter_membre,
                pool_id=pool["id"],
                adresse=ip,
                port=80,
                subnet_id=zone.get("sous_reseau_id"),
                loadbalancer_id=zone["lb_id"],
            )
            regle = await asyncio.to_thread(
                n.ajouter_regle_hote,
                listener_id=zone.get("lb_listener_id"),
                loadbalancer_id=zone["lb_id"],
                pool_id=pool["id"],
                hote=h.domaineProvisoire,
            )
            await depot_heb.definir_secrets(
                ctx_org,
                hid,
                {
                    "lb_pool_id": pool["id"],
                    "lb_membre_id": membre["id"],
                    "lb_policy_id": regle["policy_id"],
                },
            )
            print("OK", h.domaine, h.domaineProvisoire, ip, pool["id"][:8])

        await session.commit()
        print("\nAjoutez dans .env :")
        print(f"  SYNELIA_VPS_ZONE_LB_ID={new_lb}")
        print(f"  SYNELIA_VPS_ZONE_LB_LISTENER_ID={listener_id}")
        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
