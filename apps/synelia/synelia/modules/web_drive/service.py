"""Règles drive (Web Cloud) : dépôt, exécuteurs d'activation/désactivation.

Un domaine n'a qu'un seul serveur (VPS) : Drive n'obtient pas sa propre VM, il s'installe
comme une application Docker de plus sur la VM d'hébergement **déjà en service** pour ce
domaine — exactement le mécanisme déjà réel de `web_hebergement.ExecuteurSiteInstaller`
(SSH, `docker-compose.yml` dédié, route Traefik supplémentaire, règle L7 de plus sur le pool
existant), juste appelé depuis l'exécuteur `web.drive.*` plutôt que `site.installer` : Drive
garde son propre dépôt (quotas, sièges, facturation), pas un `SiteWeb`."""

from __future__ import annotations

import asyncio

from synelia_contract import modeles as m
from synelia_db.modeles import Travail
from synelia_kernel import erreurs
from synelia_kernel.ids import jeton_opaque
from synelia_openstack.ssh import SshReel

from synelia.depot import Depot
from synelia.deps.contexte import Contexte
from synelia.modules.web_hebergement.service import (
    amont,
    amont_network,
    amont_ssh,
    construire_site_stack,
    hebergement_pour_domaine,
    ip_gestion_hebergement,
    serveur_id,
    zone_vps_secrets,
)
from synelia.modules.web_hebergement.service import depot as depot_hebergements
from synelia.travaux import Executeur, executeur

# Même chemin que `web_hebergement._RACINE_DOCKER` (constante privée, pas exportée : la
# racine des VM de la zone VPS ne varie pas, un simple littéral suffit ici plutôt que de
# lever sa visibilité pour une seule chaîne).
_RACINE_DOCKER = "/srv/synelia"

depot = Depot(
    "web_drive",
    m.Drive,
    libelle="Drive",
    champ_nom="domaine",
    champ_statut="actif",
    champs_recherche=("domaine",),
)
depot_siege = Depot("web_drive_siege", m.Siege, libelle="Siège drive", champ_nom="userId")

PALIERS = {
    "starter": {"sieges": 10, "prixSiege": 1500},
    "pro": {"sieges": 50, "prixSiege": 1000},
    "business": {"sieges": 200, "prixSiege": 800},
}


def palier(cle: str) -> dict:
    return PALIERS.get(cle, PALIERS["starter"])


def _racine(drive_id: str) -> str:
    return f"{_RACINE_DOCKER}/sites/{drive_id}"


async def _hebergement_ou_erreur(ctx: Contexte, drive: m.Drive) -> m.Hebergement:
    hebergement = await hebergement_pour_domaine(ctx, drive.domaine)
    if hebergement is None:
        raise erreurs.conflit(
            "Aucun hébergement web (VPS) actif pour ce domaine. Activez d'abord "
            "l'hébergement web sur ce domaine avant d'y installer Drive.",
            code="hebergement_requis",
        )
    return hebergement


@executeur("web.drive.activate")
class ExecuteurDriveActivate(Executeur):
    compensable = True

    async def etape(self, ctx: Contexte, travail: Travail, index: int, nom: str) -> str | None:
        drive = await depot.obtenir(ctx, travail.cible_id or "")
        if index == 0:
            hebergement = await _hebergement_ou_erreur(ctx, drive)
            zone = await zone_vps_secrets(ctx)
            cle_privee = zone.get("ssh_prive")
            ip = await ip_gestion_hebergement(ctx, hebergement)
            # Même garde-fou (et même raison) que `ExecuteurSiteInstaller` : `SshSimule` ne
            # dépend d'aucun identifiant réel.
            if isinstance(amont_ssh(), SshReel) and (not cle_privee or not ip):
                raise erreurs.amont_indisponible(
                    "hébergement (SSH)",
                    "Aucune IP de gestion SSH backend disponible pour cette VM : soit la "
                    "zone VPS n'est pas encore initialisée, soit cette VM a été créée avant "
                    "le câblage SSH/IP flottante (non rattrapable a posteriori).",
                )
            if isinstance(amont_ssh(), SshReel):
                sid = await serveur_id(ctx, hebergement.id)
                if await asyncio.to_thread(amont().statut_serveur, sid) == "absente":
                    raise erreurs.amont_indisponible(
                        "hébergement (VM)",
                        "La VM de cet hébergement n'existe plus côté OpenStack (supprimée hors "
                        "bande) : l'enregistrement est orphelin, à nettoyer avant de réessayer.",
                    )
            mdp = jeton_opaque(16)
            compose, routage, fichiers = construire_site_stack(
                "nextcloud", drive.hote, "", mdp, drive.id
            )
            # SSH réel (`SshReel.executer`/`.ecrire_fichier`) : appels bloquants déchargés via
            # `asyncio.to_thread`, même garde que `web_hebergement.ExecuteurSiteInstaller` — sans
            # ça, l'installation Docker Compose sur la VM cible (par nature lente) gèlerait la
            # boucle asyncio, donc l'API entière, tous tenants confondus.
            ssh = amont_ssh()
            racine = _racine(drive.id)
            await asyncio.to_thread(
                ssh.ecrire_fichier, ip, cle_privee, f"{racine}/docker-compose.yml", compose
            )
            for chemin, contenu in fichiers.items():
                await asyncio.to_thread(ssh.ecrire_fichier, ip, cle_privee, chemin, contenu)
            await asyncio.to_thread(
                ssh.ecrire_fichier,
                ip,
                cle_privee,
                f"{_RACINE_DOCKER}/traefik-dynamic/drive-{drive.id}.yml",
                routage,
            )
            await asyncio.to_thread(
                ssh.executer, ip, cle_privee, f"cd {racine} && docker compose up -d"
            )
            await depot.definir_secrets(
                ctx,
                drive.id,
                {
                    "hebergement_id": hebergement.id,
                    "admin_utilisateur": "admin",
                    "admin_mdp": mdp,
                },
            )
            c = dict(travail.contexte)
            c["hebergement_id"] = hebergement.id
            travail.contexte = c
            return f"Nextcloud installé sur le VPS de {hebergement.domaine}"
        if index == 1:
            hid = travail.contexte.get("hebergement_id")
            hebergement = await depot_hebergements.obtenir(ctx, hid) if hid else None
            if hebergement is None:
                return None
            heb_secrets = await depot_hebergements.secrets(ctx, hebergement.id)
            zone = await zone_vps_secrets(ctx)
            regle = await asyncio.to_thread(
                amont_network().ajouter_regle_hote,
                listener_id=zone.get("lb_listener_id"),
                loadbalancer_id=zone.get("lb_id"),
                pool_id=heb_secrets.get("lb_pool_id"),
                hote=drive.hote,
            )
            await depot.definir_secrets(ctx, drive.id, {"lb_policy_id": regle["policy_id"]})
            c = dict(travail.contexte)
            c["lb_policy_id"] = regle["policy_id"]
            travail.contexte = c
            return f"Domaine {drive.hote} routé sur le load balancer partagé"
        return None

    async def terminer(self, ctx: Contexte, travail: Travail) -> None:
        await depot.modifier(ctx, travail.cible_id or "", {"actif": True})

    async def compenser(self, ctx: Contexte, travail: Travail, index_echoue: int) -> None:
        did = travail.cible_id or ""
        try:
            secrets = await depot.secrets(ctx, did)
        except Exception:  # noqa: BLE001
            secrets = {}
        zone = await zone_vps_secrets(ctx)
        policy_id = travail.contexte.get("lb_policy_id") or secrets.get("lb_policy_id")
        if policy_id:
            await asyncio.to_thread(
                amont_network().supprimer_regle_hote, policy_id, loadbalancer_id=zone.get("lb_id")
            )
        hid = travail.contexte.get("hebergement_id") or secrets.get("hebergement_id")
        hebergement = hid and await depot_hebergements.trouver(ctx, hid)
        cle_privee = zone.get("ssh_prive")
        ip = hebergement and await ip_gestion_hebergement(ctx, hebergement)
        if hebergement and cle_privee and ip:
            racine = _racine(did)
            await asyncio.to_thread(
                amont_ssh().executer,
                ip,
                cle_privee,
                f"cd {racine} && docker compose down -v; rm -rf {racine} "
                f"{_RACINE_DOCKER}/traefik-dynamic/drive-{did}.yml",
            )
        await depot.definir_statut(ctx, did, "suspendu")


@executeur("web.drive.desactiver")
class ExecuteurDriveDesactiver(Executeur):
    async def terminer(self, ctx: Contexte, travail: Travail) -> None:
        did = travail.cible_id or ""
        try:
            secrets = await depot.secrets(ctx, did)
        except Exception:  # noqa: BLE001
            secrets = {}
        zone = await zone_vps_secrets(ctx)
        policy_id = secrets.get("lb_policy_id")
        if policy_id:
            await asyncio.to_thread(
                amont_network().supprimer_regle_hote, policy_id, loadbalancer_id=zone.get("lb_id")
            )
        hid = secrets.get("hebergement_id")
        hebergement = hid and await depot_hebergements.trouver(ctx, hid)
        cle_privee = zone.get("ssh_prive")
        ip = hebergement and await ip_gestion_hebergement(ctx, hebergement)
        if hebergement and cle_privee and ip:
            racine = _racine(did)
            await asyncio.to_thread(
                amont_ssh().executer,
                ip,
                cle_privee,
                f"cd {racine} && docker compose down -v; rm -rf {racine} "
                f"{_RACINE_DOCKER}/traefik-dynamic/drive-{did}.yml",
            )
        await depot_siege.supprimer_enfants(ctx, did)
        await depot.supprimer(ctx, did, logique=True)
