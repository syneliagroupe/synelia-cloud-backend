#!/usr/bin/env python3
"""Lab : reapplique compose + Traefik + wp-config Synelia pour forcer HTTPS WordPress."""
from __future__ import annotations

import asyncio
import os
import sys

HOTE = os.environ["WP_HOTE"]
ORG_ID = os.environ.get("ORG_ID", "01a0e6a1-f8d6-7e30-b323-9804228763fb")


async def main() -> int:
    from unittest.mock import MagicMock

    from synelia_db import rls
    from synelia_db.session import session
    from synelia_kernel.config import Reglages
    from synelia.deps.contexte import Contexte, Principal
    from synelia.deps.pagination import PageParams
    from synelia.modules.web_hebergement.service import (
        _RACINE_DOCKER,
        _slug_site,
        _wordpress_config_extra,
        amont_ssh,
        application_pour_site,
        construire_site_stack,
        depot,
        depot_sites,
        ip_gestion_hebergement,
        zone_vps_secrets,
    )

    async with session() as db:
        await rls.poser(db, ORG_ID)
        req = MagicMock()
        req.client.host = "127.0.0.1"
        req.headers = {}
        ctx = Contexte(request=req, session=db, reglages=Reglages(), correlation_id="lab")
        ctx.principal = Principal(
            utilisateur_id=None,
            email="lab@local",
            nom="Lab",
            org_id=ORG_ID,
            role="super_admin",
        )
        page = PageParams(page=1, par_page=50, tri=None, ordre=None, q=None)
        sites = (
            await depot_sites.lister(
                ctx, page, filtre=lambda s: s.hote == HOTE and s.type == "wordpress"
            )
        )["donnees"]
        if not sites:
            print(f"Aucun site WordPress pour {HOTE}", file=sys.stderr)
            return 1
        site = sites[0]
        hebergement = await depot.obtenir(ctx, site.hebergementId)
        zone = await zone_vps_secrets(ctx)
        ip = await ip_gestion_hebergement(ctx, hebergement)
        cle = zone.get("ssh_prive") or ""
        if not ip or not cle:
            print("SSH indisponible", file=sys.stderr)
            return 1
        try:
            secrets = await depot_sites.secrets(ctx, site.id)
            mdp = secrets.get("mot_de_passe") or "unused"
        except Exception:  # noqa: BLE001
            mdp = "unused"
        app = application_pour_site(site.type, site.hote)
        compose, routage, fichiers = construire_site_stack(
            app, site.hote, site.phpVersion or "8.3", mdp, site.id
        )
        sid = _slug_site(site.id)
        app_svc = f"app-{sid}"
        racine = f"{_RACINE_DOCKER}/sites/{site.id}"
        ssh = amont_ssh()
        await asyncio.to_thread(
            ssh.ecrire_fichier, ip, cle, f"{racine}/docker-compose.yml", compose
        )
        await asyncio.to_thread(
            ssh.ecrire_fichier,
            ip,
            cle,
            f"{_RACINE_DOCKER}/traefik-dynamic/site-{site.id}.yml",
            routage,
        )
        extra = _wordpress_config_extra(site.hote)
        synelia_php = "<?php\n" + extra.replace(";", ";\n") + "\n"
        await asyncio.to_thread(
            ssh.ecrire_fichier, ip, cle, f"{racine}/www/wp-config-synelia-ssl.php", synelia_php
        )
        url = f"https://{site.hote}"
        patch = f"""set -e
cd {racine}
docker compose up -d --force-recreate {app_svc}
if docker compose exec -T {app_svc} test -f /var/www/html/wp-config.php; then
  docker compose exec -T {app_svc} grep -q wp-config-synelia-ssl.php /var/www/html/wp-config.php || \\
    docker compose exec -T {app_svc} sed -i "/<?php/a require_once __DIR__ . '/wp-config-synelia-ssl.php';" /var/www/html/wp-config.php
  docker compose exec -T {app_svc} php -r "define('WP_USE_THEMES', false); require '/var/www/html/wp-load.php'; update_option('home', '{url}'); update_option('siteurl', '{url}');"
fi
"""
        out = await asyncio.to_thread(ssh.executer, ip, cle, patch)
        logs = await asyncio.to_thread(
            ssh.executer, ip, cle, f"cd {racine} && docker compose logs {app_svc} 2>&1 | tail -20"
        )
        print(out or f"OK WordPress HTTPS {site.hote} ({app_svc})")
        if logs:
            print(logs)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
