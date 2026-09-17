"""Projets applicatifs : projets, services, domaines, routage et zone applicative."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from synelia_contract import modeles as m
from synelia_db.modeles import Travail
from synelia_kernel import erreurs
from synelia_kernel.ids import slug_court
from synelia_openstack import fournisseur
from synelia_openstack.compute import ComputeOpenStack, ComputeSimule
from synelia_openstack.identite import IdentiteOpenStack, IdentiteSimule
from synelia_openstack.k8s_workload import obtenir as k8s
from synelia_openstack.network import NetworkOpenStack, NetworkSimule
from synelia_openstack.ssh import SshReel, SshSimule

from synelia.depot import Depot
from synelia.deps.contexte import Contexte
from synelia.modules.web_hebergement import service as web_heb
from synelia.travaux import Executeur, executeur

depot_projet = Depot(
    "projet",
    m.Projet,
    libelle="Projet applicatif",
    champ_nom="nom",
    champs_recherche=("nom", "description"),
)
depot_service = Depot(
    "projet_service",
    m.ServiceProjet,
    libelle="Service de projet",
    champ_nom="nom",
    champs_recherche=("nom",),
)
depot_domaine = Depot(
    "domaine_applicatif",
    m.DomaineApplicatif,
    libelle="Domaine applicatif",
    champ_nom="hote",
    champs_recherche=("hote", "chemin"),
)

ZONE = "apps.synelia.cloud"
INGRESS = [
    m.Ingres(site="ABJ", ip="196.201.103.10", ipv6="2c0f:f4c0:1000::10"),
    m.Ingres(site="GBM", ip="197.243.40.10", ipv6="2c0f:f4c1:1000::10"),
]


# ─── Cible « VM + Docker, un seul nœud » ───────────────────────────────────────
#
# Alternative à la cible `k8s` (par défaut) : un projet en cible `vm` obtient sa propre VM
# Nova dédiée (une seule, jamais partagée avec un autre projet), sur laquelle chacun de ses
# services devient un service Docker Compose de plus — le motif déjà prouvé réel pour les
# « Applications » de `web_hebergement` (plusieurs sites Docker Compose sur une même VM
# d'hébergement, ajoutés après coup par SSH, routés par un Traefik en provider fichier et une
# règle L7 de plus sur le load balancer Octavia partagé). On réutilise cette infrastructure
# telle quelle plutôt que d'en construire une deuxième en parallèle :
#
# - même zone VPS partagée (`web_heb.zone_vps_secrets` : réseau + LB Octavia de
#   `SYNELIA_VPS_ZONE_ESPACE_ID`/`SYNELIA_VPS_ZONE_ORG_ID`) — le trafic HTTP d'un service de
#   projet en cible `vm` est, comme celui d'un hébergement Web Cloud, du L7 routé par Host()
#   sur le même LB public partagé ; rien ne les distingue au niveau réseau, pas de raison de
#   les isoler sur un second LB ;
# - même clé SSH de zone (`web_heb.assurer_cle_ssh_zone`) pour l'accès de gestion backend ;
# - même gabarit Nova que l'hébergement (`web_heb.gabarit_pour_palier` : ce lab ne possède que
#   deux gabarits réels, `k8s.worker`/`k8s.master`, aucun dédié à cet usage) ;
# - même convention de domaine que `Hebergement.domaineProvisoire`
#   (`*.cloud.dev01.ovh.smile.ci`) : c'est la seule zone réellement résolue et proxyfiée dans
#   ce lab, en inventer une autre la rendrait injoignable pour de vrai.
#
# Docker Swarm (multi-nœuds) est une option future explicitement hors de ce périmètre : rien
# ici ne suppose qu'une VM de projet est seule dans son réseau `synelia` (`external: true` sur
# chaque stack de service, jamais recréé) ni qu'un projet ne peut avoir qu'une seule VM — un
# futur mode `vm_swarm` pourrait réutiliser `_assurer_vm_projet`/`_installer_service_vm` en les
# rendant multi-VM, sans réécrire cette partie.

RACINE_DOCKER_VM_PROJET = "/srv/synelia"
GABARIT_PALIER_VM_PROJET = "pro"  # 2 vCPU / 4 Go — taille unique pour l'instant, cf. ci-dessus


def amont_compute() -> ComputeSimule:
    return fournisseur(ComputeSimule, ComputeOpenStack)


def amont_network() -> NetworkSimule:
    return fournisseur(NetworkSimule, NetworkOpenStack)


def amont_identite() -> IdentiteSimule:
    return fournisseur(IdentiteSimule, IdentiteOpenStack)


def amont_ssh() -> SshSimule:
    return fournisseur(SshSimule, SshReel)


def nom_vm_projet(projet: m.Projet) -> str:
    return f"vm-projet-{slug_court(projet.id)}"


def nom_conteneur_service_vm(service_id: str) -> str:
    """Nom du conteneur applicatif d'un service sur sa VM de projet — jamais en collision
    avec un autre service de la même VM (`slug_court`, même garantie que
    `web_hebergement._slug_site`)."""
    return f"svc-{slug_court(service_id)}"


def dossier_service_vm(service_id: str) -> str:
    return f"{RACINE_DOCKER_VM_PROJET}/services/{service_id}"


def domaine_service_vm(service_id: str) -> str:
    """Même convention réellement résolue/proxyfiée dans ce lab que
    `Hebergement.domaineProvisoire` (`web_hebergement.construire_hebergement`)."""
    return f"svc-{slug_court(service_id)}.cloud.dev01.ovh.smile.ci"


def expose_http_vm(service: m.ServiceProjet) -> bool:
    """Un service `base` reste interne au réseau Docker `synelia` de sa VM — comme un
    service `base` en cible k8s ne reçoit jamais d'Ingress, seulement un `ClusterIP` : pas de
    route Traefik ni de règle L7 publique pour lui, seulement pour les autres types."""
    return service.type != "base"


def construire_cloud_init_vm_projet(cle_publique: str | None) -> str:
    """`#cloud-config` d'une VM de projet en cible `vm` : Docker + Traefik (provider fichier —
    même bug de compatibilité API Docker déjà rencontré et évité pour `web_hebergement`, cf.
    son `construire_cloud_init`), sans aucun service par défaut. Chaque service du projet est
    ajouté après coup par SSH (`_installer_service_vm`), exactement comme
    `web_hebergement.ExecuteurSiteInstaller` installe une application supplémentaire sur une VM
    d'hébergement déjà en service."""
    compose = f"""services:
  traefik:
    image: traefik:v3.5
    restart: unless-stopped
    command:
      - --providers.file.directory=/etc/traefik/dynamic
      - --providers.file.watch=true
      - --entrypoints.web.address=:80
    ports:
      - "80:80"
    volumes:
      - {RACINE_DOCKER_VM_PROJET}/traefik-dynamic:/etc/traefik/dynamic:ro
    networks:
      - synelia

networks:
  synelia:
    name: synelia
"""
    acces_ssh = (
        f"disable_root: false\nssh_authorized_keys:\n  - {cle_publique}\n" if cle_publique else ""
    )
    return (
        "#cloud-config\n"
        "package_update: true\n"
        f"{acces_ssh}"
        "packages:\n"
        "  - docker.io\n"
        "  - docker-compose-v2\n"
        "write_files:\n"
        f"  - path: {RACINE_DOCKER_VM_PROJET}/docker-compose.yml\n"
        "    content: |\n" + web_heb.indenter(compose, 6) + "\n"
        f"{web_heb.DROP_IN_CONTAINERD}"
        "runcmd:\n"
        "  - systemctl daemon-reload\n"
        "  - systemctl enable --now docker\n"
        f"  - [sh, -c, 'cd {RACINE_DOCKER_VM_PROJET} && docker compose up -d']\n"
    )


def construire_service_stack_vm(
    service: m.ServiceProjet, image: str, env: dict[str, str], port: int, hote: str | None
) -> tuple[str, str | None]:
    """`docker-compose.yml` (+ route Traefik si `hote`) pour un service de plus sur une VM de
    projet déjà en service — même patron que `web_hebergement.construire_site_stack`. Pas de
    volume : ni ce chemin ni le chemin k8s (`appliquer_deployment`, sans PVC) ne persistent le
    stockage d'un conteneur applicatif à ce stade, les deux cibles restent cohérentes."""
    nom = nom_conteneur_service_vm(service.id)
    lignes_env = "\n".join(f"      - {k}={v}" for k, v in env.items())
    compose = f"""services:
  {nom}:
    image: {image}
    restart: unless-stopped
{"    environment:\n" + lignes_env if env else ""}
    networks:
      - synelia

networks:
  synelia:
    external: true
    name: synelia
"""
    if not hote:
        return compose, None
    routage = f"""http:
  routers:
    {nom}:
      rule: "Host(`{hote}`)"
      entryPoints:
        - web
      service: {nom}
  services:
    {nom}:
      loadBalancer:
        servers:
          - url: "http://{nom}:{port}"
"""
    return compose, routage


async def _assurer_vm_projet(ctx: Contexte, projet: m.Projet) -> dict[str, Any]:
    """Provisionne, au premier service réellement exécutable d'un projet en cible `vm`, la VM
    Nova dédiée à ce projet — idempotent (relit toujours les secrets du projet avant de
    recréer quoi que ce soit) : le démarrage/redémarrage d'un service existant, ou l'ajout
    d'un second service, ne recréent jamais une deuxième VM.

    Provisionnement paresseux plutôt qu'à la création du projet : contrairement à un namespace
    Kubernetes (objet API sans coût réel propre), une VM Nova a un coût réel dès sa création —
    la payer avant qu'un premier service ait quoi que ce soit à exécuter serait facturer une
    VM vide.
    """
    secrets = await depot_projet.secrets(ctx, projet.id)
    if secrets.get("vm_serveur_id"):
        return secrets
    zone = await web_heb.zone_vps_secrets(ctx)
    cle = await web_heb.assurer_cle_ssh_zone(ctx)
    # `creer_serveur` (comme les autres appels `amont_*()` de cette fonction) est un appel
    # openstacksdk/SSH synchrone/bloquant : exécuté tel quel dans la coroutine, il bloquerait
    # toute la boucle asyncio — donc toute l'API, pour tous les tenants — jusqu'à sa fin, même
    # bug que celui vécu en direct et corrigé dans `vms.service` (`asyncio.to_thread`
    # systématique sur les appels amont). Mêmes gardes ici.
    srv = await asyncio.to_thread(
        amont_compute().creer_serveur,
        nom=nom_vm_projet(projet),
        image_id=web_heb.image_ubuntu(),
        gabarit_id=web_heb.gabarit_pour_palier(GABARIT_PALIER_VM_PROJET),
        reseau_id=zone.get("reseau_id"),
        identifiants=zone,
        org_id=ctx.org_id_ou_none,
        espace_id=None,
        cle_ssh=cle.get("ssh_cle_nom"),
        cloud_init=construire_cloud_init_vm_projet(cle.get("ssh_publique")),
    )
    ip_privee = srv.get("ip_privee") or f"10.{hash(projet.id) % 250}.0.{hash('vm-projet') % 250 + 2}"
    # IP flottante de gestion SSH backend — même raison que `web_hebergement` : le réseau privé
    # de la zone VPS n'est routable que depuis l'intérieur du lab OpenStack, le trafic HTTP
    # public lui ne passe jamais par elle (uniquement par le load balancer partagé).
    fip = await asyncio.to_thread(amont_identite().creer_ip_flottante, zone.get("projet_id"))
    ip_gestion = await asyncio.to_thread(
        amont_identite().associer_ip_flottante, fip.get("id"), srv["id"]
    )
    await asyncio.to_thread(amont_network().assurer_regle_ssh, srv["id"])
    n = amont_network()
    pool = await asyncio.to_thread(
        n.creer_pool, loadbalancer_id=zone.get("lb_id"), nom=f"pool-projet-{slug_court(projet.id)}"
    )
    membre = await asyncio.to_thread(
        n.ajouter_membre,
        pool_id=pool["id"],
        adresse=ip_privee,
        port=80,
        subnet_id=zone.get("sous_reseau_id"),
        loadbalancer_id=zone.get("lb_id"),
    )
    secrets_maj = {
        "vm_serveur_id": srv["id"],
        "vm_ip_privee": ip_privee,
        "vm_ssh_fip_id": fip.get("id") or "",
        "vm_ssh_ip": ip_gestion or fip.get("adresse") or "",
        "vm_lb_pool_id": pool["id"],
        "vm_lb_membre_id": membre["id"],
    }
    await depot_projet.definir_secrets(ctx, projet.id, secrets_maj)
    # Attend que sshd réponde réellement avant de rendre la main : passer `ACTIVE` côté Nova
    # ne veut dire que « la VM a démarré », pas que cloud-init (paquets Docker inclus) ait fini
    # — contrairement à `web_hebergement.ExecuteurSiteInstaller`, qui installe toujours une
    # application sur une VM d'hébergement déjà en service depuis un moment (jamais au tout
    # premier démarrage), ici la toute première installation d'un service suit IMMÉDIATEMENT
    # la création de la VM : sans cette attente, elle échoue quasiment à coup sûr
    # (« Unable to connect to port 22 »), vécu en le vérifiant en direct sur ce lab.
    await asyncio.to_thread(
        _attendre_ssh_pret, amont_ssh(), secrets_maj["vm_ssh_ip"], cle.get("ssh_prive") or ""
    )
    return {**secrets, **secrets_maj}


def _attendre_ssh_pret(
    ssh: SshSimule, ip: str, cle_privee: str, tentatives: int = 30, intervalle_s: float = 6.0
) -> None:
    """No-op en simulation (`SshSimule` n'ouvre aucune connexion réelle). En réel, ré-essaie
    jusqu'à ce que sshd réponde sur la VM fraîchement créée."""
    if not isinstance(ssh, SshReel) or not ip or not cle_privee:
        return
    derniere: Exception | None = None
    for _ in range(tentatives):
        try:
            ssh.executer(ip, cle_privee, "true")
            return
        except Exception as exc:  # noqa: BLE001 — on ne fait que ré-essayer
            derniere = exc
            time.sleep(intervalle_s)
    raise erreurs.amont_indisponible(
        "projet (SSH)",
        f"La VM du projet ne répond pas en SSH après {tentatives * intervalle_s:.0f}s : {derniere}",
    )


async def _installer_service_vm(
    ctx: Contexte, service: m.ServiceProjet, projet: m.Projet, image: str
) -> None:
    """Installe (ou réinstalle, idempotent) `service` comme service Docker Compose de plus sur
    la VM partagée du projet, par SSH — même patron que
    `web_hebergement.ExecuteurSiteInstaller`."""
    secrets_projet = await _assurer_vm_projet(ctx, projet)
    zone = await web_heb.zone_vps_secrets(ctx)
    cle_privee = zone.get("ssh_prive")
    ip = secrets_projet.get("vm_ssh_ip")
    ssh = amont_ssh()
    if not cle_privee or not ip:
        if isinstance(ssh, SshReel):
            raise erreurs.amont_indisponible(
                "projet (SSH)",
                "Aucune IP de gestion SSH backend disponible pour la VM de ce projet : soit "
                "la zone VPS n'est pas encore initialisée, soit cette VM a été créée avant le "
                "câblage SSH/IP flottante (non rattrapable a posteriori).",
            )
        # Simulation : `SshSimule` n'ouvre aucune connexion réelle, peu importe la valeur —
        # rester instantané et sans réseau, comme le reste de la plateforme en mode simulé
        # (la zone VPS partagée n'a aucune raison d'être configurée en environnement de test).
        cle_privee, ip = cle_privee or "cle-simulee", ip or "127.0.0.1"
    env = env_projet(projet, service)
    if service.type == "base" and service.moteur:
        secrets_service = await depot_service.secrets(ctx, service.id)
        env = {**env, **env_base(service, secrets_service)}
    port = service.portConteneur or PORT_DEFAUT_SERVICE
    expose = expose_http_vm(service)
    hote = domaine_service_vm(service.id) if expose else None
    compose, routage = construire_service_stack_vm(service, image, env, port, hote)
    racine = dossier_service_vm(service.id)
    await asyncio.to_thread(ssh.ecrire_fichier, ip, cle_privee, f"{racine}/docker-compose.yml", compose)
    if routage:
        await asyncio.to_thread(
            ssh.ecrire_fichier,
            ip,
            cle_privee,
            f"{RACINE_DOCKER_VM_PROJET}/traefik-dynamic/service-{service.id}.yml",
            routage,
        )
    await asyncio.to_thread(ssh.executer, ip, cle_privee, f"cd {racine} && docker compose up -d")
    if expose:
        # Idempotent : `projet_service.create` sert aussi démarrage/redémarrage (cf. router),
        # qui repasse ici à chaque fois — une règle L7 déjà posée ne doit pas en reposer une
        # deuxième pour le même hôte.
        secrets_service = await depot_service.secrets(ctx, service.id)
        if not secrets_service.get("lb_policy_id"):
            regle = await asyncio.to_thread(
                amont_network().ajouter_regle_hote,
                listener_id=zone.get("lb_listener_id"),
                loadbalancer_id=zone.get("lb_id"),
                pool_id=secrets_projet.get("vm_lb_pool_id"),
                hote=hote,
            )
            await depot_service.definir_secrets(
                ctx, service.id, {"lb_policy_id": regle["policy_id"]}
            )


async def _appliquer_service_vm(ctx: Contexte, service: m.ServiceProjet, projet: m.Projet) -> bool:
    """Équivalent VM+Docker de `_appliquer_service_k8s` : installe réellement `service` comme
    conteneur Docker Compose sur la VM dédiée du projet (provisionnée au besoin). Renvoie si
    quelque chose tourne vraiment, même convention que le chemin k8s."""
    image = image_service(service)
    if not image:
        return False
    await _installer_service_vm(ctx, service, projet, image)
    return True


async def _arreter_service_vm(ctx: Contexte, service: m.ServiceProjet, projet: m.Projet) -> None:
    secrets_projet = await depot_projet.secrets(ctx, projet.id)
    ip = secrets_projet.get("vm_ssh_ip")
    if not secrets_projet.get("vm_serveur_id") or not ip:
        return  # rien n'a jamais tourné réellement pour ce projet : rien à arrêter
    zone = await web_heb.zone_vps_secrets(ctx)
    cle_privee = zone.get("ssh_prive")
    if not cle_privee:
        return
    racine = dossier_service_vm(service.id)
    await asyncio.to_thread(
        amont_ssh().executer, ip, cle_privee, f"cd {racine} && docker compose stop"
    )


async def _supprimer_service_vm(ctx: Contexte, service: m.ServiceProjet, projet: m.Projet) -> None:
    try:
        secrets_service = await depot_service.secrets(ctx, service.id)
    except Exception:  # noqa: BLE001
        secrets_service = {}
    secrets_projet = await depot_projet.secrets(ctx, projet.id)
    zone = await web_heb.zone_vps_secrets(ctx)
    policy_id = secrets_service.get("lb_policy_id")
    if policy_id:
        await asyncio.to_thread(
            amont_network().supprimer_regle_hote, policy_id, loadbalancer_id=zone.get("lb_id")
        )
    cle_privee = zone.get("ssh_prive")
    ip = secrets_projet.get("vm_ssh_ip")
    if cle_privee and ip and secrets_projet.get("vm_serveur_id"):
        racine = dossier_service_vm(service.id)
        await asyncio.to_thread(
            amont_ssh().executer,
            ip,
            cle_privee,
            f"cd {racine} && docker compose down -v; rm -rf {racine} "
            f"{RACINE_DOCKER_VM_PROJET}/traefik-dynamic/service-{service.id}.yml",
        )


async def _supprimer_vm_projet(ctx: Contexte, projet: m.Projet) -> None:
    """Décommissionne la VM d'un projet en cible `vm`, appelée uniquement quand le projet ne
    contient plus aucun service (le router l'exige avant suppression) : plus aucune règle L7
    de service ne reste donc à défaire ici, seulement la VM elle-même et le pool/membre du
    load balancer partagé."""
    try:
        secrets = await depot_projet.secrets(ctx, projet.id)
    except Exception:  # noqa: BLE001
        secrets = {}
    if not secrets.get("vm_serveur_id"):
        return  # jamais provisionnée réellement (aucun service n'a jamais tourné) : rien à défaire
    fip_id = secrets.get("vm_ssh_fip_id")
    if fip_id:
        await asyncio.to_thread(amont_identite().supprimer_ip_flottante, fip_id)
    await asyncio.to_thread(amont_compute().supprimer_serveur, secrets["vm_serveur_id"])
    zone = await web_heb.zone_vps_secrets(ctx)
    n = amont_network()
    pool_id = secrets.get("vm_lb_pool_id")
    membre_id = secrets.get("vm_lb_membre_id")
    if pool_id and membre_id:
        await asyncio.to_thread(
            n.supprimer_membre, pool_id, membre_id, loadbalancer_id=zone.get("lb_id")
        )
    if pool_id:
        await asyncio.to_thread(n.supprimer_pool, pool_id, loadbalancer_id=zone.get("lb_id"))


def hote_interne(service: m.ServiceProjet, projet: m.Projet) -> str:
    if projet.cible == "vm":
        # Résoluble seulement entre conteneurs de la MÊME VM de projet (réseau Docker
        # `synelia`, DNS interne de Compose par nom de conteneur) — jamais depuis
        # l'extérieur, exactement la même portée qu'un `ClusterIP` Kubernetes en cible k8s.
        return nom_conteneur_service_vm(service.id)
    return f"{service.nom}.{projet.nom}.svc.cluster.local"


def namespace_projet(projet: m.Projet) -> str:
    """Un projet applicatif = un namespace Kubernetes, 1:1, sur le cluster PaaS."""
    return f"projet-{projet.id}"


def nom_k8s_service(service: m.ServiceProjet) -> str:
    """Nom du Deployment/Service Kubernetes pour ce service.

    `service.id` (UUIDv7) commence souvent par un chiffre : valide pour un nom de
    Deployment (DNS-1123) mais pas pour un `Service`, qui exige DNS-1035 (débute par
    une lettre) — vérifié en direct : `01a0771...` fait échouer la création du
    `Service` avec 422. Le préfixe `svc-` couvre les deux.
    """
    return f"svc-{service.id}"


PORT_DEFAUT_SERVICE = 8080

# Image Docker officielle par moteur de base managée — utilisée quand un service
# `base` n'a pas de `source` explicite (c'est le cas normal : une base se choisit par
# moteur/version, pas par image). `clickhouse` n'a pas d'image `clickhouse` officielle
# sous ce nom, elle vit sous `clickhouse/clickhouse-server`.
MOTEUR_IMAGE: dict[str, str] = {
    "postgresql": "postgres",
    "mysql": "mysql",
    "mariadb": "mariadb",
    "mongodb": "mongo",
    "redis": "redis",
    "clickhouse": "clickhouse/clickhouse-server",
}

# Port d'écoute d'usage de chaque moteur — même mapping que le front (`PORT_MOTEUR` de
# `nouveau-service`/`projets/[projet]/vue.tsx`), pour que le port réellement exposé par
# le conteneur corresponde à ce que la fiche du service annonce.
MOTEUR_PORT: dict[str, int] = {
    "postgresql": 5432,
    "mysql": 3306,
    "mariadb": 3306,
    "mongodb": 27017,
    "redis": 6379,
    "clickhouse": 9000,
}


def image_service(service: m.ServiceProjet) -> str | None:
    """L'image réelle à déployer pour ce service, ou `None` s'il n'y a rien à exécuter.

    Deux cas donnent une image réelle : une `source` explicite de type `image` (le
    flux « Modèle du catalogue », ou un appel direct de l'API qui la fournit), ou un
    service `base` dont le `moteur` se traduit en image officielle. Une `source` de
    type `git` (pas de pipeline de build ici) ou l'absence totale de source (coquille
    créée par le flux nom+description seul) ne donnent délibérément aucune image : on
    ne invente pas un déploiement là où le produit n'en promet pas.
    """
    if service.source and service.source.type == "image" and service.source.ref:
        return service.source.ref
    if service.type == "base" and service.moteur:
        image = MOTEUR_IMAGE.get(service.moteur)
        if image:
            return f"{image}:{service.version or 'latest'}"
    return None


def utilisateur_base(nom_service: str) -> str:
    """Nom d'utilisateur de bootstrap d'un service `base`, dérivé de son nom — jamais préfixé
    `pg_` : PostgreSQL le refuse (`initdb: role names cannot begin with "pg_"`, réservé aux
    rôles système). Bug réel vécu en direct : un service `base`/`postgresql` nommé `pg` — le
    choix le plus naturel — donne `pg_user`, rejeté par `initdb`, conteneur en boucle d'échec
    (`docker ps` : `Restarting`). Les autres moteurs (`mysql`/`mariadb`/`mongodb`) n'ont pas
    cette restriction, mais le même préfixage neutre reste inoffensif pour eux."""
    brut = f"{nom_service}_user"
    return f"u_{brut}" if brut.startswith("pg_") else brut


def env_projet(projet: m.Projet, service: m.ServiceProjet) -> dict[str, str]:
    """Variables partagées du projet (`PUT /projets/{id}/variables`) applicables à l'exécution
    de `service` : portée `runtime`, et son environnement dans `environnements`.

    Sans ceci, `PUT /projets/{id}/variables` se contentait de persister les variables en base
    et de les rendre à la lecture (`GET`), sans jamais les faire atteindre le conteneur
    applicatif réellement déployé (Docker Compose sur la VM du projet, ou Deployment k8s) —
    l'écriture semblait réussir mais n'avait aucun effet réel sur ce qui tourne."""
    return {
        v.cle: v.valeur
        for v in projet.variables
        if v.portee == "runtime" and service.environnement in v.environnements and v.valeur is not None
    }


def env_base(service: m.ServiceProjet, secrets: dict[str, str]) -> dict[str, str]:
    """Variables d'environnement d'amorçage de l'image officielle de `service.moteur`."""
    mdp = secrets.get("motDePasse", "")
    utilisateur = secrets.get("utilisateur") or utilisateur_base(service.nom)
    base = secrets.get("base") or service.nom
    if service.moteur == "postgresql":
        return {"POSTGRES_USER": utilisateur, "POSTGRES_PASSWORD": mdp, "POSTGRES_DB": base}
    if service.moteur == "mysql":
        return {
            "MYSQL_ROOT_PASSWORD": mdp,
            "MYSQL_DATABASE": base,
            "MYSQL_USER": utilisateur,
            "MYSQL_PASSWORD": mdp,
        }
    if service.moteur == "mariadb":
        return {
            "MARIADB_ROOT_PASSWORD": mdp,
            "MARIADB_DATABASE": base,
            "MARIADB_USER": utilisateur,
            "MARIADB_PASSWORD": mdp,
        }
    if service.moteur == "mongodb":
        return {
            "MONGO_INITDB_ROOT_USERNAME": utilisateur,
            "MONGO_INITDB_ROOT_PASSWORD": mdp,
            "MONGO_INITDB_DATABASE": base,
        }
    return {}


async def _appliquer_service_k8s(ctx: Contexte, service: m.ServiceProjet, projet: m.Projet) -> bool:
    """Déploie réellement `service` sur le cluster PaaS s'il a une image. Renvoie si
    quelque chose tourne vraiment (pour choisir le statut final : `running`/`stopped`)."""
    image = image_service(service)
    if not image:
        return False
    env = env_projet(projet, service)
    if service.type == "base" and service.moteur:
        secrets = await depot_service.secrets(ctx, service.id)
        env = {**env, **env_base(service, secrets)}
    port = service.portConteneur or (MOTEUR_PORT.get(service.moteur or "")) or PORT_DEFAUT_SERVICE
    # `k8s_client` (comme les appels `amont_*()` de la cible `vm`, cf. plus haut) est synchrone/
    # bloquant : même garde `asyncio.to_thread` pour ne pas geler la boucle asyncio.
    await asyncio.to_thread(k8s().creer_namespace, namespace_projet(projet))
    await asyncio.to_thread(
        k8s().appliquer_deployment,
        namespace_projet(projet),
        nom_k8s_service(service),
        image,
        replicas=1,
        env=env,
        ports=[port],
        cpu=service.ressources.cpu,
        ram_mo=service.ressources.ramMo,
    )
    return True


@executeur("projet_service.create")
class ExecuteurServiceCreate(Executeur):
    """Sert aussi « démarrage » et « redémarrage » (même type de travail, cf. router)."""

    async def terminer(self, ctx: Contexte, travail: Travail) -> None:
        service = await depot_service.obtenir(ctx, travail.cible_id or "")
        projet = await depot_projet.obtenir(ctx, service.projetId)
        try:
            if projet.cible == "vm":
                tourne = await _appliquer_service_vm(ctx, service, projet)
            else:
                tourne = await _appliquer_service_k8s(ctx, service, projet)
        except Exception:
            # Le moteur de travaux marque déjà le travail `failed` (cf. `travaux/moteur.py`),
            # mais sans ceci le `ServiceProjet` restait affiché `building` indéfiniment — statut
            # qui promet une progression en cours, jamais corrigé après un échec réel du
            # provisionnement (ex. Nova `NoValidHost`, vécu en direct sur ce lab).
            await depot_service.definir_statut(ctx, service.id, "failed")
            raise
        await depot_service.definir_statut(ctx, service.id, "running" if tourne else "stopped")


@executeur("projet_service.stopped")
class ExecuteurServiceStopped(Executeur):
    async def terminer(self, ctx: Contexte, travail: Travail) -> None:
        service = await depot_service.obtenir(ctx, travail.cible_id or "")
        projet = await depot_projet.obtenir(ctx, service.projetId)
        image = image_service(service)
        if image and projet.cible == "vm":
            await _arreter_service_vm(ctx, service, projet)
        elif image:
            # Ramène les réplicas à 0 plutôt que de supprimer le Deployment : un
            # redémarrage réapplique juste le même objet à 1 réplica, pas de
            # recréation de zéro (même motif que `ExecuteurComposantArret`).
            await asyncio.to_thread(
                k8s().appliquer_deployment,
                namespace_projet(projet),
                nom_k8s_service(service),
                image,
                replicas=0,
            )
        await depot_service.definir_statut(ctx, service.id, "stopped")


@executeur("projet_service.delete")
class ExecuteurServiceDelete(Executeur):
    async def terminer(self, ctx: Contexte, travail: Travail) -> None:
        service = await depot_service.obtenir(ctx, travail.cible_id or "")
        projet = await depot_projet.obtenir(ctx, service.projetId)
        if projet.cible == "vm":
            await _supprimer_service_vm(ctx, service, projet)
        else:
            await asyncio.to_thread(
                k8s().supprimer_deployment, namespace_projet(projet), nom_k8s_service(service)
            )
        await depot_service.supprimer(ctx, travail.cible_id or "", logique=True)


@executeur("projet.delete")
class ExecuteurProjetDelete(Executeur):
    async def terminer(self, ctx: Contexte, travail: Travail) -> None:
        projet = await depot_projet.obtenir(ctx, travail.cible_id or "")
        if projet.cible == "vm":
            await _supprimer_vm_projet(ctx, projet)
        else:
            await asyncio.to_thread(k8s().supprimer_namespace, namespace_projet(projet))
        await depot_projet.supprimer(ctx, travail.cible_id or "", logique=True)


@executeur("domaine_certificat.emission")
class ExecuteurCertificat(Executeur):
    async def terminer(self, ctx: Contexte, travail: Travail) -> None:
        await depot_domaine.modifier(
            ctx,
            travail.cible_id or "",
            {
                "certificat": m.Certificat1(etat="actif", emetteur="Let's Encrypt").model_dump(
                    mode="json"
                )
            },
        )
