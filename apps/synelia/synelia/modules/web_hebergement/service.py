from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy import select
from synelia_contract import modeles as m
from synelia_db.modeles import Travail
from synelia_kernel import erreurs
from synelia_kernel.dates import maintenant
from synelia_kernel.ids import jeton_opaque, nouvel_id, slug_court
from synelia_openstack import fournisseur
from synelia_openstack.compute import ComputeOpenStack, ComputeSimule
from synelia_openstack.identite import IdentiteOpenStack, IdentiteSimule
from synelia_openstack.network import NetworkOpenStack, NetworkSimule
from synelia_openstack.ssh import SshReel, SshSimule

from synelia.audit import journaliser
from synelia.depot import Depot
from synelia.deps.contexte import Contexte
from synelia.travaux import Executeur, demarrer_travail, executeur

depot = Depot(
    "web_hebergement",
    m.Hebergement,
    libelle="Hébergement",
    champ_statut="statut",
    champ_nom="domaineProvisoire",
)
depot_sites = Depot(
    "web_site", m.SiteWeb, libelle="Site Web", champ_statut="statut", champ_nom="hote"
)
depot_bases = Depot("web_serveur_bases", m.ServeurBases, libelle="Serveur de bases")
depot_comptes = Depot(
    "web_compte_fichiers", m.CompteFichiers, libelle="Compte fichiers", champ_statut="statut"
)
depot_taches = Depot(
    "web_tache", m.TachePlanifieeWeb, libelle="Tâche planifiée", champ_statut="statut"
)
depot_domaines = Depot("web_domaine", m.Domaine, libelle="Domaine", champ_nom="nom")

VERSIONS_PHP = ["8.1", "8.2", "8.3", "8.4"]

METRIQUES = [
    ("cpu", "%", "Utilisation CPU"),
    ("ram", "%", "Utilisation RAM"),
    ("stockage", "%", "Stockage"),
    ("requetes", "req/min", "Requêtes HTTP"),
    ("trafique", "Mo/s", "Trafic réseau"),
    ("bases", "nb", "Bases de données"),
]


def metriques(fenetre: str) -> dict[str, Any]:
    from datetime import timedelta

    from synelia_kernel.dates import iso

    nb_points = {"24h": 24, "7j": 7, "30j": 30}[fenetre]
    pas = {"24h": timedelta(hours=1), "7j": timedelta(days=1), "30j": timedelta(days=1)}
    origine = maintenant() - pas[fenetre] * (nb_points - 1)
    series = [
        m.Serie(
            metrique=metrique,
            unite=unite,
            fenetre=fenetre,  # type: ignore[arg-type]
            points=[
                m.PointSerie(ts=origine + pas[fenetre] * i, valeur=0.0) for i in range(nb_points)
            ],
        )
        for metrique, unite, _libelle in METRIQUES
    ]
    return {
        "tuiles": [],
        "series": [s for s in series],
        "liens": m.LiensSortie(
            centreon=f"https://monitoring.synelia.cloud/{iso(maintenant())}",
        ),
    }


def traduire_cron(expression: str) -> str:
    parts = expression.split()
    if len(parts) == 5:
        minutes, heures, _jours_mois, _mois, _jours_semaine = parts
        if minutes == "0" and heures.isdigit():
            return f"Tous les jours à {int(heures):02d}:00."
        if minutes == "*/5":
            return "Toutes les 5 minutes."
    return f"Planification : {expression}."


def amont() -> ComputeSimule:
    return fournisseur(ComputeSimule, ComputeOpenStack)


def amont_network() -> NetworkSimule:
    """Neutron/Octavia — gestion du load balancer partagé de la zone VPS (pools, membres,
    règles L7 par hébergement)."""
    return fournisseur(NetworkSimule, NetworkOpenStack)


def amont_ssh() -> SshSimule:
    """SSH vers une VM d'hébergement déjà en service — installation d'une application
    supplémentaire après coup (`router_sites`), pas à la création de la VM elle-même."""
    return fournisseur(SshSimule, SshReel)


def amont_identite() -> IdentiteSimule:
    """Keystone/Neutron admin — IP flottante **dédiée à l'accès SSH backend** d'une VM
    d'hébergement. Le réseau privé de la zone VPS (`vps-zone-net`, 10.90.0.0/16) n'est
    routable que depuis l'intérieur du lab OpenStack : le backend (hors du lab) ne peut
    joindre une VM que par une IP flottante. Le trafic HTTP public, lui, ne passe jamais par
    cette IP : uniquement par le load balancer partagé (`amont_network()`), c'est pour ça que
    `web_hebergement` n'avait jusqu'ici jamais eu besoin d'IP flottante par VM."""
    return fournisseur(IdentiteSimule, IdentiteOpenStack)


NOM_KEYPAIR_ZONE = "synelia-hebergement"


async def assurer_cle_ssh_zone(ctx: Contexte) -> dict[str, str]:
    """Clé SSH unique de la zone VPS, générée une fois puis réutilisée par toutes les VM
    d'hébergement : la clé privée est stockée dans les secrets de l'Espace Cloud de la zone
    (comme `lb_id`, `reseau_id`…), la clé publique est enregistrée comme keypair Nova
    (`assurer_keypair`, idempotent) et injectée dans le cloud-init de chaque nouvelle VM
    (`construire_cloud_init`). Les VM déjà créées avant ce câblage ne l'ont pas : le
    cloud-init ne s'exécute qu'au premier démarrage, on ne peut pas les retrofit."""
    from synelia_kernel.config import reglages

    from synelia.modules.espaces.service import depot_plateforme as depot_espaces

    zone = await zone_vps_secrets(ctx)
    if zone.get("ssh_prive") and zone.get("ssh_publique"):
        # Clé déjà générée : on réenregistre quand même le keypair Nova (idempotent, un
        # `find_keypair` avant tout `create_keypair`) plutôt que de faire confiance à sa
        # seule présence dans les secrets — un keypair Nova peut disparaître (recréation du
        # projet, erreur d'appel initiale) sans que les secrets ne bougent.
        nom = zone.get("ssh_cle_nom") or NOM_KEYPAIR_ZONE
        # `amont().assurer_keypair`/`amont_ssh().generer_cle` (openstacksdk/SSH, synchrones)
        # sont déchargés via `asyncio.to_thread` : même garde que `vms.service`, sans quoi un
        # appel amont lent gèlerait la boucle asyncio — donc l'API entière, tous tenants
        # confondus.
        await asyncio.to_thread(amont().assurer_keypair, nom, zone["ssh_publique"], identifiants=zone)
        return {"ssh_prive": zone["ssh_prive"], "ssh_publique": zone["ssh_publique"], "ssh_cle_nom": nom}
    r = reglages()
    if not r.vps_zone_espace_id:
        return {}
    cle = await asyncio.to_thread(amont_ssh().generer_cle)
    await asyncio.to_thread(
        amont().assurer_keypair, NOM_KEYPAIR_ZONE, cle["publique"], identifiants=zone
    )
    secrets = {
        "ssh_cle_nom": NOM_KEYPAIR_ZONE,
        "ssh_prive": cle["prive"],
        "ssh_publique": cle["publique"],
    }
    await depot_espaces.definir_secrets(ctx, r.vps_zone_espace_id, secrets)
    return secrets


async def zone_vps_secrets(ctx: Contexte) -> dict[str, Any]:
    """Secrets de la zone VPS partagée : un unique Espace Cloud **plateforme** (`org_id`
    NULL — cf. `espaces.service.depot_plateforme` et `semer_zone_vps`, qui garantit sa
    présence à chaque démarrage sans bootstrap manuel), réseau privé + load balancer Octavia
    public, référencé par `SYNELIA_VPS_ZONE_ESPACE_ID`. Toutes les VM d'hébergement sont
    créées sur ce même réseau et projet OpenStack (jamais celui de l'organisation cliente) —
    seul le load balancer partagé les expose, chacune isolée par son `Host()` Traefik et sa
    policy L7 dédiée. Étant « plateforme » (jamais listable/visible depuis `/espaces` côté
    client, cf. `Depot._org`), cette lecture n'a plus besoin d'un org_id explicite : le dépôt
    « plateforme » lit par id fixe, indépendamment du contexte appelant."""
    from synelia_kernel.config import reglages

    from synelia.modules.espaces.service import depot_plateforme as depot_espaces

    r = reglages()
    if not r.vps_zone_espace_id:
        return {}
    return await depot_espaces.secrets(ctx, r.vps_zone_espace_id)


# Le lab ne possède aujourd'hui que deux gabarits Nova réels — taillés pour Kubernetes,
# pas de petit gabarit dédié à l'hébergement web. On rattache les paliers les plus légers
# à `k8s.worker` et les autres à `k8s.master` ; c'est une limitation connue de capacité,
# pas un choix définitif (`enterprise` obtient donc les mêmes ressources que `business`).
_GABARIT_NOM_PAR_PALIER = {
    "starter": "k8s.worker",
    "pro": "k8s.worker",
    "business": "k8s.master",
    "enterprise": "k8s.master",
}
# Repli en mode simulé (catalogue de démo sans `k8s.*`) : le plus proche du palier.
_GABARIT_SIMULE_PAR_PALIER = {
    "starter": "s1.small",
    "pro": "g1.medium",
    "business": "g1.large",
    "enterprise": "g1.xlarge",
}


def gabarit_pour_palier(palier: str) -> str:
    nom = _GABARIT_NOM_PAR_PALIER.get(palier, "k8s.worker")
    g = next((f for f in amont().gabarits() if f["nom"] == nom), None)
    if g:
        return str(g["id"])
    return _GABARIT_SIMULE_PAR_PALIER.get(palier, "g1.medium")


def image_ubuntu() -> str:
    """Image système du serveur d'hébergement : Ubuntu 24.04, ou la plus proche disponible."""
    images = amont().images()
    img = next((i for i in images if i["id"] == "ubuntu-24.04"), None)
    if img is None:
        img = next((i for i in images if "ubuntu" in i["nom"].lower()), None)
    if img is None:
        img = images[0] if images else None
    return str(img["id"]) if img else "ubuntu-24.04"


_RACINE_DOCKER = "/srv/synelia"

# `containerd` (paquet `docker.io` d'Ubuntu 24.04) régénère parfois son config.toml en
# `version = 4` — via une mise à jour de sécurité automatique après le premier démarrage —
# alors que le binaire installé ne sait lire qu'une config jusqu'à la version 3 : constaté en
# direct sur deux VM d'hébergement réelles, `containerd.service` boucle en échec au boot
# suivant (`expected containerd config version equal to or less than 3, got 4`), et avec lui
# `docker.service` (dépend de son socket). Un `ExecStartPre` corrige la valeur avant chaque
# démarrage de containerd, pas seulement à la création — c'est justement un redémarrage
# ultérieur (reboot du lab), pas la création elle-même, qui déclenche le bug.
DROP_IN_CONTAINERD = (
    "  - path: /etc/systemd/system/containerd.service.d/synelia-fix-version.conf\n"
    "    content: |\n"
    "      [Service]\n"
    "      ExecStartPre=/bin/sh -c \"sed -i 's/^version = 4/version = 3/' "
    "/etc/containerd/config.toml || true\"\n"
)


def indenter(bloc: str, colonnes: int) -> str:
    """Réutilisé par `web_drive.service` (même recette cloud-init) plutôt que dupliqué."""
    prefixe = " " * colonnes
    return "\n".join(f"{prefixe}{ligne}" for ligne in bloc.splitlines())


def construire_cloud_init(domaine: str, version_php: str, cle_publique: str | None = None) -> str:
    """`#cloud-config` : Docker + Traefik (reverse-proxy HTTP sur :80) et un conteneur PHP pour
    `domaine`, routé par son `Host()`. Nextcloud (« Drive ») est ajouté plus tard au même
    `docker-compose.yml`, sur cette même VM, quand `web_drive` est activé pour l'organisation
    (voir `web_drive.service`) — un Traefik par VM d'hébergement, tout le reste en conteneurs
    Docker derrière lui.

    Traefik route via son *provider fichier* (config statique dans `traefik-dynamic/`), pas le
    provider Docker : le Docker Engine récent (>= API 1.44, cf. `docker.io` sur Ubuntu 24.04)
    rejette le client Docker vendorisé par Traefik (bloqué sur l'API 1.24, y compris en v3.5) —
    `Error response from daemon: client version 1.24 is too old`. Bug de compatibilité amont
    réel, constaté en direct (VM de debug jetable + SSH), pas un problème de labels. Le provider
    fichier n'a pas besoin du socket Docker : chaque service se route par son nom de conteneur
    sur le réseau `synelia` (résolution DNS interne de Compose)."""
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
      - {_RACINE_DOCKER}/traefik-dynamic:/etc/traefik/dynamic:ro
    networks:
      - synelia

  site:
    image: php:{version_php}-apache
    restart: unless-stopped
    volumes:
      - {_RACINE_DOCKER}/www:/var/www/html:ro
    networks:
      - synelia

networks:
  synelia:
    name: synelia
"""
    routage = f"""http:
  routers:
    site:
      rule: "Host(`{domaine}`)"
      entryPoints:
        - web
      service: site
  services:
    site:
      loadBalancer:
        servers:
          - url: "http://site:80"
"""
    index_php = (
        "<?php\n"
        f'echo "<h1>{domaine}</h1><p>Synelia Web Hebergement -- PHP " . phpversion() . "</p>";\n'
    )
    # Clé SSH backend (`assurer_cle_ssh_zone`) : injectée pour root — c'est elle qui permet
    # à `router_sites` d'installer une application supplémentaire après coup, sur une VM
    # déjà en service (cloud-init ne s'exécute qu'au premier démarrage, on ne peut pas
    # revenir en arrière sur une VM déjà créée sans cette clé).
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
        f"  - path: {_RACINE_DOCKER}/docker-compose.yml\n"
        "    content: |\n" + indenter(compose, 6) + "\n"
        f"  - path: {_RACINE_DOCKER}/traefik-dynamic/site.yml\n"
        "    content: |\n" + indenter(routage, 6) + "\n"
        f"  - path: {_RACINE_DOCKER}/www/index.php\n"
        "    content: |\n" + indenter(index_php, 6) + "\n"
        f"{DROP_IN_CONTAINERD}"
        "runcmd:\n"
        "  - systemctl daemon-reload\n"
        "  - systemctl enable --now docker\n"
        f"  - [sh, -c, 'cd {_RACINE_DOCKER} && docker compose up -d']\n"
    )


def ip_privee(hebergement_id: str) -> str:
    return f"10.{hash(hebergement_id) % 250}.0.{hash('web') % 250 + 2}"


async def serveur_id(ctx: Contexte, hebergement_id: str, travail: Travail | None = None) -> str:
    """Identifiant Nova du serveur : dans les secrets (posé à la création), sinon le travail."""
    if travail and travail.contexte.get("serveur_id"):
        return str(travail.contexte["serveur_id"])
    try:
        sec = await depot.secrets(ctx, hebergement_id)
    except Exception:  # noqa: BLE001
        sec = {}
    return str(sec.get("serveur_id") or hebergement_id)


async def reconcilier_statut(ctx: Contexte, h: m.Hebergement) -> m.Hebergement:
    """Relit l'existence réelle de la VM côté Nova et rend le statut sincère si elle a disparu
    depuis le dernier relevé, avant de renvoyer la ressource.

    La ligne en base peut survivre à son infra réelle : une VM d'hébergement supprimée hors bande
    (nettoyage manuel du lab, travail tombé en échec sans compensation) laisse l'hébergement
    s'afficher `en_ligne` dans les listes et les tableaux de bord, et c'est seulement au premier
    usage (installation d'un site, activation Drive — cf. la garde `statut_serveur` de
    `ExecuteurSiteInstaller`/`ExecuteurDriveActivate`) que l'écart se voit — vécu en direct sur
    `verif-final.example.com`, resté `en_ligne` des heures après la disparition de sa VM, ~20 s de
    SSH voué à l'échec à la clé. Même motif « reconcile-on-read » que `kubernetes` (cf.
    `docs/GUIDE-MODULE.md`, invariant « Réconcilier à la lecture ») : sur toute lecture d'un
    hébergement en statut contrôlé, vérifie que Nova connaît encore son serveur et persiste
    l'écart.

    Orphelin confirmé (Nova ignore le serveur référencé — purgé ou soft-delete `DELETED` pas
    encore purgé — alors que la ligne a passé la fenêtre de grâce de la création — statuts
    contrôlés seulement) : décision propriétaire, la ligne est **supprimée** par le même chemin
    métier que DELETE /web/hebergements/{id} (exécuteur `hebergement.supprimer`, cf.
    `_traiter_orphelin`), pas seulement marquée `suspendu`. Le
    marquage historique reste `suspendu` — celui que `ExecuteurHebergementCreer.compenser` pose
    déjà quand l'amont a disparu en cours de création, le seul des trois états du contrat
    (`en_ligne|maintenance|suspendu`) qui ne promette ni un service en ligne, ni une reprise
    imminente. `maintenance` en revanche n'est pas réconcilié : entre la création de la ligne et
    la fin du travail `hebergement.creer`, la VM n'existe pas encore côté Nova (le secret
    `serveur_id` non plus) — une relecture dans cette fenêtre croirait à un orphelin ; et c'est le
    travail qui y mettra le statut final. Le statut du serveur imbriqué passe à `maintenance` :
    `Serveur.statut` n'admet pas `suspendu`, et `maintenance` est le seul état qui ne l'appelle
    plus « en ligne »."""
    if h.statut not in STATUTS_A_CONTROLER:
        return h
    try:
        secrets = await depot.secrets(ctx, h.id)
    except Exception:  # noqa: BLE001
        return h
    sid = secrets.get("serveur_id")
    # Sans `serveur_id` (hébergement de démo, antérieur au câblage Nova), la ligne n'a jamais
    # référencé d'infrastructure réelle identifiable : `serveur_id()` retomberait sur l'id
    # applicatif, que Nova ne connaît pas — un contrôle là-dessus marquerait en erreur des
    # lignes qui ne sont pas orphelines. On n'affirme « absente » qu'à propos d'un serveur
    # qu'on sait avoir existé.
    if not sid:
        return h
    statut_amont = await asyncio.to_thread(amont().statut_serveur, sid)
    if statut_amont.upper() in ("ABSENTE", "DELETED"):
        # `ABSENTE` : Nova ignore le serveur (purgé) ; `DELETED` : ligne soft-delete encore
        # visible de Nova (purge asynchrone) — dans les deux cas l'objet n'existe plus
        # réellement (pas d'hyperviseur, pas d'IP) : c'est un orphelin confirmé.
        return await _traiter_orphelin(ctx, h)
    return h


async def mesurer_espace_utilise(ctx: Contexte, h: m.Hebergement) -> m.Hebergement:
    """Mesure réelle de l'espace disque occupé par les sites de cet hébergement, par SSH (`du
    -sb`, même transport que `ExecuteurSiteInstaller` — `amont_ssh()`/`zone_vps_secrets`),
    posée à la lecture (best-effort, même motif reconcile-on-read que `reconcilier_statut` :
    pas besoin de tourner à chaque requête, seulement de ne plus mentir indéfiniment).
    `espaceUtiliseGo` restait sinon figé à `0.0` depuis `construire_hebergement`, quel que soit
    le contenu réel déployé dessus. No-op en simulé (`SshSimule.executer` renvoie une chaîne
    vide) et si aucune IP de gestion SSH n'est disponible (VM antérieure au câblage SSH/IP,
    hébergement pas encore en ligne)."""
    if not isinstance(amont_ssh(), SshReel):
        return h
    zone = await zone_vps_secrets(ctx)
    cle_privee = zone.get("ssh_prive")
    ip = await ip_gestion_hebergement(ctx, h)
    if not cle_privee or not ip:
        return h
    try:
        sortie = await asyncio.to_thread(
            amont_ssh().executer,
            ip,
            cle_privee,
            f"du -sb {_RACINE_DOCKER}/sites 2>/dev/null | awk '{{print $1}}'",
        )
        octets = int((sortie or "0").split()[0])
    except Exception:  # noqa: BLE001
        return h
    go = round(octets / 1_000_000_000, 2)
    if go == h.espaceUtiliseGo:
        return h
    return await depot.modifier(ctx, h.id, {"espaceUtiliseGo": go})


# Statuts contrôlés à la lecture : `en_ligne` (peut avoir dérivé d'un serveur disparu) et
# `suspendu` (lignes déjà marquées — réconciliation précédente ou compensation d'un travail
# tombé — que la décision propriétaire fait maintenant supprimer une fois l'orphelin reconfirmé).
# `maintenance` en est exclu volontairement : c'est le statut de la fenêtre de grâce de la
# création (la VM n'existe pas encore côté Nova).
STATUTS_A_CONTROLER = {"en_ligne", "suspendu"}

ETAPES_SUPPRESSION = [
    {"nom": "Suspension des sites et bases", "dureeS": 8},
    {"nom": "Suppression du serveur (OpenStack)", "dureeS": 25},
    {"nom": "Clore la facturation", "dureeS": 4},
]

# Décision propriétaire (2026-09-07) : ces hébergements sains du lab (serveurs Nova
# `srv-01a073a1`/`srv-01a076fe`, ACTIVE) ne sont jamais la cible d'une suppression automatique —
# une indisponibilité ponctuelle d'amont ne doit jamais pouvoir coûter un service réel. Ces
# lignes restent marquées, jamais détruites.
_HEBERGEMENTS_PROTEGES = ("01a073a1", "01a076fe")


def _suppression_automatique_interdite(h: m.Hebergement) -> bool:
    noms_proteges = {f"srv-{p}" for p in _HEBERGEMENTS_PROTEGES}
    return h.id.startswith(_HEBERGEMENTS_PROTEGES) or h.serveur.nom in noms_proteges


async def _suppression_en_cours(ctx: Contexte, cible_id: str, type_travail: str) -> bool:
    """Un travail de suppression est-il déjà en vol pour cette ressource (DELETE utilisateur,
    réconciliation d'une lecture concurrente) ? Le statut posé avant le lancement couvre les
    lectures séquentielles ; ce contrôle couvre les lectures concurrentes — sans lui, deux GET
    simultanés lanceraient deux `hebergement.supprimer`."""
    q = select(Travail).where(
        Travail.cible_id == cible_id,
        Travail.type == type_travail,
        Travail.statut.in_(("queued", "running")),
    )
    return (await ctx.session.execute(q)).scalars().first() is not None


async def _marquer_suspendu(ctx: Contexte, h: m.Hebergement) -> m.Hebergement:
    """Le marquage sincère historique (`suspendu`, serveur imbriqué `maintenance`), toujours
    utilisé quand la suppression automatique est interdite ou déjà en vol."""
    if h.statut == "suspendu":
        return h
    return await depot.modifier(
        ctx,
        h.id,
        {
            "statut": "suspendu",
            "serveur": h.serveur.model_copy(update={"statut": "maintenance"}).model_dump(
                mode="json"
            ),
        },
    )


async def _traiter_orphelin(ctx: Contexte, h: m.Hebergement) -> m.Hebergement:
    """Orphelin confirmé (Nova ignore le serveur référencé, fenêtre de grâce de la création
    passée) : décision propriétaire, on le **supprime** réellement — même chemin métier que
    DELETE /web/hebergements/{id} (exécuteur `hebergement.supprimer` et sa compensation
    habituelle : IP flottante de gestion, serveur Nova, règle L7/pool/membre du load balancer
    partagé, sites), pas un simple marquage. Garde-fous : hébergement protégé
    (`_HEBERGEMENTS_PROTEGES`), ou suppression déjà en vol → marquage sincère seulement."""
    if _suppression_automatique_interdite(h) or await _suppression_en_cours(
        ctx, h.id, "hebergement.supprimer"
    ):
        return await _marquer_suspendu(ctx, h)
    return await _supprimer_orphelin(ctx, h)


async def _supprimer_orphelin(ctx: Contexte, h: m.Hebergement) -> m.Hebergement:
    """Suppression réelle d'un hébergement orphelin confirmé, par le chemin métier du DELETE :
    la ligne est d'abord marquée `suspendu` (sincère immédiatement — et une lecture concurrente
    ne redéclenchera pas, le travail de suppression étant déjà en vol), puis le travail
    `hebergement.supprimer` détruit ce qu'il reste de l'infra (Nova répond NotFound à un serveur
    déjà disparu — succès, pas échec) et retire la ligne. Si le travail échoue, la ligne reste
    `suspendu` : sincère, requalifiable à la main."""
    marque = await _marquer_suspendu(ctx, h)
    await journaliser(
        ctx,
        action="hebergement.suppression_orpheline",
        cible_type="web_hebergement",
        cible_id=h.id,
        cible=h.domaineProvisoire,
        details={"origine": "reconciliation_orphelin"},
    )
    await demarrer_travail(
        ctx,
        "hebergement.supprimer",
        h.domaineProvisoire,
        cible_type="web_hebergement",
        cible_id=h.id,
        etapes=ETAPES_SUPPRESSION,
        contexte={"origine": "reconciliation_orphelin"},
    )
    return marque


async def hebergement_pour_domaine(ctx: Contexte, domaine: str) -> m.Hebergement | None:
    """Résout le VPS (hébergement) déjà en service pour un domaine — un domaine n'a qu'un
    seul serveur (`domaine`, le nom de domaine réel du client, jamais `domaineProvisoire`
    qui est propre à Synelia) : c'est sur cette même VM que Drive et les Applications
    s'installent, jamais sur une VM séparée."""
    tous = await depot.tous(ctx)
    return next((h for h in tous if h.domaine == domaine), None)


async def ip_gestion_hebergement(ctx: Contexte, hebergement: m.Hebergement) -> str | None:
    """Adresse à laquelle SSH peut joindre la VM d'hébergement : l'IP flottante de gestion
    (`ssh_fip_id`/`ssh_ip`, posée à la création — voir `amont_identite()`), sinon l'IP privée
    en mode simulé (jamais reproché : aucun appel réseau n'y est réellement fait). Une VM créée
    avant ce câblage n'a ni l'une ni l'autre utilisable en réel : `None`."""
    try:
        secrets = await depot.secrets(ctx, hebergement.id)
    except Exception:  # noqa: BLE001
        secrets = {}
    return secrets.get("ssh_ip") or hebergement.serveur.ip or None


def construire_hebergement(ctx: Contexte, corps: m.HebergementCreation) -> m.Hebergement:
    hid = nouvel_id()
    return m.Hebergement(
        id=hid,
        orgId=ctx.org_id,
        domaine=corps.domaine,
        domaineProvisoire=f"h-{hid[:8]}.cloud.dev01.ovh.smile.ci",
        palier=corps.palier,
        serveur=m.Serveur(
            nom=f"srv-{hid[:8]}",
            vcpu={"starter": 1, "pro": 2, "business": 4, "enterprise": 8}.get(corps.palier, 2),
            ramGo={"starter": 2, "pro": 4, "business": 8, "enterprise": 16}.get(corps.palier, 4),
            diskGo={"starter": 40, "pro": 80, "business": 160, "enterprise": 320}.get(
                corps.palier, 80
            ),
            ip="",
            site=corps.site,
            os="Ubuntu 24.04 LTS",
            serveurWeb="Nginx",
            statut="maintenance",
            chargeCpuPct=0.0,
        ),
        php=m.Php(
            versionDefaut=corps.versionPhp or "8.2",
            versionsDisponibles=list(VERSIONS_PHP),
            extensions=[
                m.Extension(nom=e, active=True)
                for e in ["curl", "mbstring", "xml", "mysqli", "gd", "opcache"]
            ],
            limites=m.Limites(memoryLimitMo=256, uploadMaxMo=64, maxExecutionS=30, opcache=True),
        ),
        acces=m.Acces(ftp=True, sftp=True, ftps=False, ssh=False, portSsh=22),
        espaceUtiliseGo=0.0,
        espaceTotalGo=float(
            {"starter": 40, "pro": 80, "business": 160, "enterprise": 320}.get(corps.palier, 80)
        ),
        sauvegarde=m.Sauvegarde(
            frequence="quotidienne",
            heure="02:00",
            retentionJours=14,
            destination="Object Storage — région ABJ",
            immuable=True,
            statut="en_cours",
        ),
        statut="maintenance",
        cree=maintenant(),
    )


def construire_serveur_bases(ctx: Contexte, hebergement_id: str) -> m.ServeurBases:
    return m.ServeurBases(
        id=nouvel_id(),
        hebergementId=hebergement_id,
        serveur=f"db-{hebergement_id[:8]}",
        moteur="mariadb",
        version="MariaDB 10.11",
        actif=True,
        hoteInterne="localhost",
        port=3306,
        bases=[],
        utilisateurs=[],
        quotaMo=1024.0,
        utiliseMo=0.0,
        connexions=m.Connexions1(actives=0, max=20),
        sauvegarde=m.Sauvegarde1(frequence="quotidienne", derniere=maintenant(), retentionJours=7),
        prixMensuel=5000,
    )


def construire_site(
    ctx: Contexte, hebergement_id: str, creation: m.SiteWebCreation, preprod: bool = False
) -> m.SiteWeb:
    return m.SiteWeb(
        id=nouvel_id(),
        hebergementId=hebergement_id,
        hote=creation.hote,
        racine=creation.racine or f"/var/www/{creation.hote}",
        type=creation.type,
        version=creation.version or _version_defaut(creation.type),
        phpVersion=creation.phpVersion or "8.2",
        baseId=None,
        ssl=m.Ssl(
            etat="en_emission" if creation.ssl else "actif",
            emetteur="Let's Encrypt" if creation.ssl else None,
        ),
        espaceMo=0.0,
        visitesMois=0,
        preproduction=m.Preproduction(actif=True, hote=f"preprod.{creation.hote}")
        if preprod
        else None,
        majEnAttente=0,
        securite=m.Securite(waf=False, bruteForce=True, scanMalware=True),
        statut="installation",
    )


def _version_defaut(type_: str) -> str | None:
    return {"wordpress": "6.7", "prestashop": "8.2", "laravel": "11", "php": "8.2"}.get(type_)


# `SiteWeb.type` (contrat) ne distingue que 5 valeurs (`wordpress`, `prestashop`, `php`,
# `statique`, `laravel`) : Ghost et Dolibarr sont posés côté catalogue frontend sous
# `type: 'php'`, comme Joomla l'était avant eux. Plutôt qu'étendre le contrat pour un simple
# discriminant, on retrouve l'application réelle depuis le premier label du nom d'hôte — le
# catalogue de `/app/web/applications` nomme déjà ses hôtes `ghost.<domaine>`, `dolibarr.
# <domaine>`… Une installation « PHP générique » depuis le formulaire libre (hôte quelconque)
# retombe sur `php`, ce qui est le bon défaut.
_APPLICATIONS_PHP = {"ghost", "dolibarr"}


def application_pour_site(site_type: str, hote: str) -> str:
    if site_type in {"wordpress", "prestashop", "statique"}:
        return site_type
    label = hote.split(".", 1)[0].lower()
    if site_type == "php" and label in _APPLICATIONS_PHP:
        return label
    return "php"


def _slug_site(site_id: str) -> str:
    """Nom court, mais réellement unique, du site sur la VM partagée — voir
    `synelia_kernel.ids.slug_court` pour le bug de collision que ce choix évite (vécu et
    corrigé ici en premier, réutilisé depuis par `projets.service` pour le même besoin sur
    une VM de projet en cible `vm`)."""
    return slug_court(site_id)


def construire_site_stack(
    application: str, hote: str, php_version: str, mot_de_passe: str, site_id: str
) -> tuple[str, str, dict[str, str]]:
    """`docker-compose.yml` + route Traefik + fichiers additionnels pour installer
    `application` sur une VM d'hébergement **déjà en service**, à côté d'autres sites : chaque
    service est nommé par le short id du site (jamais par son rôle générique `app`/`db`) pour
    ne jamais entrer en collision avec un autre site installé sur la même VM, tous rattachés
    au même réseau Docker externe `synelia` créé par le compose principal de la VM
    (`construire_cloud_init`)."""
    sid = _slug_site(site_id)
    app_svc = f"app-{sid}"
    db_svc = f"db-{sid}"
    racine = f"{_RACINE_DOCKER}/sites/{site_id}"
    fichiers: dict[str, str] = {}
    port = 80
    if application == "wordpress":
        services = f"""  {db_svc}:
    image: mariadb:11
    restart: unless-stopped
    environment:
      - MARIADB_ROOT_PASSWORD={mot_de_passe}
      - MARIADB_DATABASE=wordpress
      - MARIADB_USER=wordpress
      - MARIADB_PASSWORD={mot_de_passe}
    volumes:
      - {racine}/db:/var/lib/mysql
    networks:
      - synelia

  {app_svc}:
    image: wordpress:php{php_version}-apache
    restart: unless-stopped
    depends_on:
      - {db_svc}
    environment:
      - WORDPRESS_DB_HOST={db_svc}
      - WORDPRESS_DB_NAME=wordpress
      - WORDPRESS_DB_USER=wordpress
      - WORDPRESS_DB_PASSWORD={mot_de_passe}
    volumes:
      - {racine}/www:/var/www/html
    networks:
      - synelia
"""
    elif application == "prestashop":
        services = f"""  {db_svc}:
    image: mariadb:11
    restart: unless-stopped
    environment:
      - MARIADB_ROOT_PASSWORD={mot_de_passe}
      - MARIADB_DATABASE=prestashop
      - MARIADB_USER=prestashop
      - MARIADB_PASSWORD={mot_de_passe}
    volumes:
      - {racine}/db:/var/lib/mysql
    networks:
      - synelia

  {app_svc}:
    image: prestashop/prestashop:8-apache
    restart: unless-stopped
    depends_on:
      - {db_svc}
    environment:
      - DB_SERVER={db_svc}
      - DB_NAME=prestashop
      - DB_USER=prestashop
      - DB_PASSWD={mot_de_passe}
      - PS_INSTALL_AUTO=1
      - PS_DOMAIN={hote}
      - ADMIN_MAIL=admin@{hote}
      - ADMIN_PASSWD={mot_de_passe}
    volumes:
      - {racine}/www:/var/www/html
    networks:
      - synelia
"""
    elif application == "ghost":
        port = 2368
        services = f"""  {app_svc}:
    image: ghost:5-alpine
    restart: unless-stopped
    environment:
      - url=http://{hote}
      - database__client=sqlite3
      - database__connection__filename=/var/lib/ghost/content/data/ghost.db
      - database__useNullAsDefault=true
    volumes:
      - {racine}/content:/var/lib/ghost/content
    networks:
      - synelia
"""
    elif application == "dolibarr":
        services = f"""  {db_svc}:
    image: mariadb:11
    restart: unless-stopped
    environment:
      - MARIADB_ROOT_PASSWORD={mot_de_passe}
      - MARIADB_DATABASE=dolibarr
      - MARIADB_USER=dolibarr
      - MARIADB_PASSWORD={mot_de_passe}
    volumes:
      - {racine}/db:/var/lib/mysql
    networks:
      - synelia

  {app_svc}:
    image: dolibarr/dolibarr:latest
    restart: unless-stopped
    depends_on:
      - {db_svc}
    environment:
      - DOLI_DB_HOST={db_svc}
      - DOLI_DB_USER=dolibarr
      - DOLI_DB_PASSWORD={mot_de_passe}
      - DOLI_DB_NAME=dolibarr
      - DOLI_URL_ROOT=http://{hote}
      - DOLI_ADMIN_LOGIN=admin
      - DOLI_ADMIN_PASSWORD={mot_de_passe}
      - DOLI_INSTALL_AUTO=1
    volumes:
      # Jamais `/var/www/html` en entier : contrairement à l'image `wordpress`/`prestashop`
      # (dont l'entrypoint re-remplit un volume vide avec le cœur applicatif au premier
      # démarrage), l'image `dolibarr/dolibarr` embarque son code sous `/var/www/html` sans
      # jamais le recopier ailleurs — un bind mount y écrase silencieusement l'installeur
      # (`install/mysql/*.sql` introuvable, vu en direct via `docker logs`). Seuls
      # `documents` (données) et `html/custom` (modules) sont prévus pour être montés,
      # comme documenté par l'image officielle.
      - {racine}/doc:/var/www/documents
      - {racine}/custom:/var/www/html/custom
    networks:
      - synelia
"""
    elif application == "nextcloud":
        services = f"""  {db_svc}:
    image: mariadb:11
    restart: unless-stopped
    environment:
      - MARIADB_ROOT_PASSWORD={mot_de_passe}
      - MARIADB_DATABASE=nextcloud
      - MARIADB_USER=nextcloud
      - MARIADB_PASSWORD={mot_de_passe}
    volumes:
      - {racine}/db:/var/lib/mysql
    networks:
      - synelia

  {app_svc}:
    image: nextcloud:apache
    restart: unless-stopped
    depends_on:
      - {db_svc}
    environment:
      - MYSQL_HOST={db_svc}
      - MYSQL_DATABASE=nextcloud
      - MYSQL_USER=nextcloud
      - MYSQL_PASSWORD={mot_de_passe}
      - NEXTCLOUD_ADMIN_USER=admin
      - NEXTCLOUD_ADMIN_PASSWORD={mot_de_passe}
      - NEXTCLOUD_TRUSTED_DOMAINS={hote}
      - OVERWRITEPROTOCOL=http
    volumes:
      - {racine}/www:/var/www/html
    networks:
      - synelia
"""
    elif application == "statique":
        fichiers[f"{racine}/www/index.html"] = (
            f"<!doctype html><html><head><title>{hote}</title></head><body>"
            f"<h1>{hote}</h1><p>Synelia Web Cloud — site statique.</p></body></html>\n"
        )
        services = f"""  {app_svc}:
    image: nginx:alpine
    restart: unless-stopped
    volumes:
      - {racine}/www:/usr/share/nginx/html:ro
    networks:
      - synelia
"""
    else:  # `php` générique et `laravel` : même patron que le site par défaut de la VM
        fichiers[f"{racine}/www/index.php"] = (
            "<?php\n"
            f'echo "<h1>{hote}</h1><p>Synelia Web Cloud -- PHP " . phpversion() . "</p>";\n'
        )
        services = f"""  {app_svc}:
    image: php:{php_version}-apache
    restart: unless-stopped
    volumes:
      - {racine}/www:/var/www/html:ro
    networks:
      - synelia
"""
    compose = f"services:\n{services}\nnetworks:\n  synelia:\n    external: true\n    name: synelia\n"
    routage = f"""http:
  routers:
    {app_svc}:
      rule: "Host(`{hote}`)"
      entryPoints:
        - web
      service: {app_svc}
  services:
    {app_svc}:
      loadBalancer:
        servers:
          - url: "http://{app_svc}:{port}"
"""
    return compose, routage, fichiers


SERVICES_PARTAGES = [
    m.ServicePartage(
        id=str(i),
        hebergementId="",
        slug=slug,
        nom=nom,
        solution=solution,
        hote=hote,
        usage=m.Usage1(libelle=libelle, utilise=utilise, total=total, unite=unite),
        version=version,
        sante="ok",
        urlOuverture=f"https://{hote}/",
        actif=True,
    )
    for i, (slug, nom, solution, hote, libelle, utilise, total, unite, version) in enumerate(
        [
            (
                "messagerie",
                "Messagerie",
                "Stalwart",
                "mail.synelia.cloud",
                "Boîtes",
                0,
                10,
                "boîtes",
                "2.5",
            ),
            (
                "drive",
                "Drive",
                "Nextcloud",
                "drive.synelia.cloud",
                "Stockage",
                0,
                100,
                "Go",
                "29.0",
            ),
            (
                "statistiques",
                "Statistiques de visite",
                "Matomo",
                "stats.synelia.cloud",
                "Sites suivis",
                0,
                3,
                "sites",
                "5.2",
            ),
        ]
    )
]


@executeur("hebergement.creer")
class ExecuteurHebergementCreer(Executeur):
    compensable = True

    async def etape(self, ctx: Contexte, travail: Travail, index: int, nom: str) -> str | None:
        if index == 1:
            h = await depot.obtenir(ctx, travail.cible_id or "")
            zone = await zone_vps_secrets(ctx)
            cle = await assurer_cle_ssh_zone(ctx)
            # `image_ubuntu`/`gabarit_pour_palier` (openstacksdk, synchrones — même fonctions
            # utilisées telles quelles par `projets.service._assurer_vm_projet`, signature
            # inchangée ici pour ne pas les casser) et `amont().creer_serveur` sont déchargés
            # via `asyncio.to_thread` : même garde que `vms.service`, sans quoi un appel amont
            # lent gèlerait la boucle asyncio — donc l'API entière, tous tenants confondus.
            image_id = await asyncio.to_thread(image_ubuntu)
            gabarit_id = await asyncio.to_thread(gabarit_pour_palier, h.palier)
            srv = await asyncio.to_thread(
                amont().creer_serveur,
                nom=h.serveur.nom,
                image_id=image_id,
                gabarit_id=gabarit_id,
                reseau_id=zone.get("reseau_id"),
                identifiants=zone,
                org_id=ctx.org_id_ou_none,
                espace_id=None,
                cle_ssh=cle.get("ssh_cle_nom"),
                cloud_init=construire_cloud_init(
                    h.domaineProvisoire, h.php.versionDefaut, cle.get("ssh_publique")
                ),
            )
            c = dict(travail.contexte)
            c["serveur_id"] = srv["id"]
            await depot.definir_secrets(ctx, h.id, {"serveur_id": srv["id"]})
            c["ip_privee"] = srv.get("ip_privee") or ip_privee(h.id)
            travail.contexte = c
            # IP flottante dédiée à l'accès SSH backend (jamais au trafic HTTP public, qui ne
            # passe que par le load balancer partagé) : sans elle, `router_sites` ne peut pas
            # joindre cette VM après coup pour y installer une application supplémentaire —
            # le réseau privé de la zone VPS n'est routable que depuis l'intérieur du lab.
            fip = await asyncio.to_thread(amont_identite().creer_ip_flottante, zone.get("projet_id"))
            ip_gestion = await asyncio.to_thread(
                amont_identite().associer_ip_flottante, fip.get("id"), srv["id"]
            )
            await asyncio.to_thread(amont_network().assurer_regle_ssh, srv["id"])
            c["ssh_fip_id"] = fip.get("id")
            c["ssh_ip"] = ip_gestion or fip.get("adresse")
            travail.contexte = c
            await depot.definir_secrets(
                ctx, h.id, {"ssh_fip_id": fip.get("id") or "", "ssh_ip": c["ssh_ip"] or ""}
            )
            return f"Serveur amont {srv['id']} créé"
        if index == 2:
            # Routage L7 sur le load balancer partagé de la zone VPS (un pool + une policy
            # `Host()` par hébergement) au lieu d'une IP flottante dédiée : toutes les VM
            # d'hébergement partagent le même réseau et le même point d'entrée public.
            hid = travail.cible_id or ""
            h = await depot.obtenir(ctx, hid)
            c = dict(travail.contexte)
            zone = await zone_vps_secrets(ctx)
            lb_id = zone.get("lb_id")
            n = amont_network()
            pool = await asyncio.to_thread(n.creer_pool, loadbalancer_id=lb_id, nom=f"pool-{hid[:8]}")
            await depot.definir_secrets(ctx, hid, {"lb_pool_id": pool["id"]})
            c["lb_pool_id"] = pool["id"]
            # `travail.contexte` est réassigné après chaque effet de bord (pas seulement à la
            # fin) : si l'étape échoue en cours de route, `compenser` doit voir exactement ce
            # qui a déjà été créé côté amont pour pouvoir le défaire.
            travail.contexte = c
            membre = await asyncio.to_thread(
                n.ajouter_membre,
                pool_id=pool["id"],
                adresse=c["ip_privee"],
                port=80,
                subnet_id=zone.get("sous_reseau_id"),
                loadbalancer_id=lb_id,
            )
            await depot.definir_secrets(ctx, hid, {"lb_membre_id": membre["id"]})
            c["lb_membre_id"] = membre["id"]
            travail.contexte = c
            regle = await asyncio.to_thread(
                n.ajouter_regle_hote,
                listener_id=zone.get("lb_listener_id"),
                loadbalancer_id=lb_id,
                pool_id=pool["id"],
                hote=h.domaineProvisoire,
            )
            await depot.definir_secrets(ctx, hid, {"lb_policy_id": regle["policy_id"]})
            c["lb_policy_id"] = regle["policy_id"]
            travail.contexte = c
            return f"Domaine {h.domaineProvisoire} routé sur le load balancer partagé"
        return None

    async def terminer(self, ctx: Contexte, travail: Travail) -> None:
        h = await depot.obtenir(ctx, travail.cible_id or "")
        ip = travail.contexte.get("ip_privee") or ip_privee(h.id)
        serveur = h.serveur.model_copy(update={"ip": ip, "statut": "en_ligne", "chargeCpuPct": 12.0})
        await depot.modifier(ctx, h.id, {"serveur": serveur.model_dump(mode="json")})
        base = await depot_bases.creer(
            ctx, construire_serveur_bases(ctx, travail.cible_id or ""), parent_id=travail.cible_id
        )
        travail.contexte = {**travail.contexte, "serveur_bases_id": base.id}
        await depot.definir_statut(ctx, travail.cible_id or "", "en_ligne")

    async def compenser(self, ctx: Contexte, travail: Travail, index_echoue: int) -> None:
        sid = await serveur_id(ctx, travail.cible_id or "", travail)
        if sid and sid != (travail.cible_id or ""):
            await asyncio.to_thread(amont().supprimer_serveur, sid)
        fip_id = travail.contexte.get("ssh_fip_id")
        if fip_id:
            await asyncio.to_thread(amont_identite().supprimer_ip_flottante, fip_id)
        n = amont_network()
        lb_id = (await zone_vps_secrets(ctx)).get("lb_id")
        policy_id = travail.contexte.get("lb_policy_id")
        if policy_id:
            await asyncio.to_thread(n.supprimer_regle_hote, policy_id, loadbalancer_id=lb_id)
        pool_id = travail.contexte.get("lb_pool_id")
        membre_id = travail.contexte.get("lb_membre_id")
        if pool_id and membre_id:
            await asyncio.to_thread(
                n.supprimer_membre, pool_id, membre_id, loadbalancer_id=lb_id
            )
        if pool_id:
            await asyncio.to_thread(n.supprimer_pool, pool_id, loadbalancer_id=lb_id)
        await depot.definir_statut(ctx, travail.cible_id or "", "suspendu")


@executeur("hebergement.supprimer")
class ExecuteurHebergementSupprimer(Executeur):
    async def terminer(self, ctx: Contexte, travail: Travail) -> None:
        try:
            secrets_avant = await depot.secrets(ctx, travail.cible_id or "")
        except Exception:  # noqa: BLE001
            secrets_avant = {}
        fip_id = secrets_avant.get("ssh_fip_id")
        if fip_id:
            await asyncio.to_thread(amont_identite().supprimer_ip_flottante, fip_id)
        sid = await serveur_id(ctx, travail.cible_id or "", travail)
        if sid and sid != (travail.cible_id or ""):
            await asyncio.to_thread(amont().supprimer_serveur, sid)
        secrets = await depot.secrets(ctx, travail.cible_id or "")
        n = amont_network()
        lb_id = (await zone_vps_secrets(ctx)).get("lb_id")
        policy_id = secrets.get("lb_policy_id")
        if policy_id:
            await asyncio.to_thread(n.supprimer_regle_hote, policy_id, loadbalancer_id=lb_id)
        # Avant de supprimer le pool : chaque application installée dessus (`site.installer`)
        # porte sa propre règle L7 sur ce même pool — Octavia refuse de le supprimer tant
        # qu'une policy le référence encore (vécu en direct, `Pool ... is in use by L7
        # policy ...`). `depot_enfants` les retire toutes avant qu'on tente `supprimer_pool`.
        await depot_enfants(ctx, travail.cible_id or "")
        pool_id = secrets.get("lb_pool_id")
        membre_id = secrets.get("lb_membre_id")
        if pool_id and membre_id:
            await asyncio.to_thread(
                n.supprimer_membre, pool_id, membre_id, loadbalancer_id=lb_id
            )
        if pool_id:
            await asyncio.to_thread(n.supprimer_pool, pool_id, loadbalancer_id=lb_id)
        await depot.supprimer(ctx, travail.cible_id or "", logique=True)


@executeur("hebergement.redemarrer")
class ExecuteurHebergementRedemarrer(Executeur):
    async def etape(self, ctx: Contexte, travail: Travail, index: int, nom: str) -> str | None:
        if index == 1:
            sid = await serveur_id(ctx, travail.cible_id or "", travail)
            await asyncio.to_thread(amont().action, sid, "redemarrage")
        return None

    async def terminer(self, ctx: Contexte, travail: Travail) -> None:
        await depot.definir_statut(ctx, travail.cible_id or "", "en_ligne")


@executeur("site.installer")
class ExecuteurSiteInstaller(Executeur):
    """Installe une application Docker supplémentaire sur une VM d'hébergement **déjà en
    service**, par SSH (`amont_ssh()`, clé de la zone VPS posée par `assurer_cle_ssh_zone`
    à la création de la VM) : écrit un `docker-compose.yml` dédié sous
    `_RACINE_DOCKER/sites/<siteId>/`, une route Traefik de plus (le provider fichier la
    reprend seul, sans redémarrage — `--providers.file.watch=true`), démarre les conteneurs,
    puis ajoute une règle L7 de plus sur le pool **déjà existant** de l'hébergement (même VM,
    même membre : inutile d'en recréer un, contrairement à `web_hebergement`/`web_drive` qui
    provisionnent chacun leur propre VM)."""

    compensable = True

    async def etape(self, ctx: Contexte, travail: Travail, index: int, nom: str) -> str | None:
        site = await depot_sites.obtenir(ctx, travail.cible_id or "")
        if index == 0:
            hebergement = await depot.obtenir(ctx, site.hebergementId)
            zone = await zone_vps_secrets(ctx)
            cle_privee = zone.get("ssh_prive")
            ip = await ip_gestion_hebergement(ctx, hebergement)
            # `SshSimule` est un no-op qui n'a besoin d'aucun identifiant réel : cette exigence
            # ne s'applique qu'au vrai SSH, sinon le mode simulé (aucune zone VPS n'existe dans
            # les tests) échoue systématiquement sur une contrainte purement réelle.
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
            application = application_pour_site(site.type, site.hote)
            mdp = jeton_opaque(16)
            compose, routage, fichiers = construire_site_stack(
                application, site.hote, site.phpVersion, mdp, site.id
            )
            # SSH réel (`SshReel.executer`/`.ecrire_fichier`) : appels bloquants déchargés via
            # `asyncio.to_thread`, sans quoi l'installation Docker Compose sur la VM cible (par
            # nature lente) gèlerait la boucle asyncio — donc l'API entière, tous tenants
            # confondus.
            ssh = amont_ssh()
            racine = f"{_RACINE_DOCKER}/sites/{site.id}"
            await asyncio.to_thread(
                ssh.ecrire_fichier, ip, cle_privee, f"{racine}/docker-compose.yml", compose
            )
            for chemin, contenu in fichiers.items():
                await asyncio.to_thread(ssh.ecrire_fichier, ip, cle_privee, chemin, contenu)
            await asyncio.to_thread(
                ssh.ecrire_fichier,
                ip,
                cle_privee,
                f"{_RACINE_DOCKER}/traefik-dynamic/site-{site.id}.yml",
                routage,
            )
            # MariaDB (le cas échéant) est créée par ce même `docker compose up -d`, avec
            # l'application : pas d'étape séparée à distinguer côté SSH.
            await asyncio.to_thread(
                ssh.executer, ip, cle_privee, f"cd {racine} && docker compose up -d"
            )
            await depot_sites.definir_secrets(
                ctx, site.id, {"application": application, "mot_de_passe": mdp}
            )
            c = dict(travail.contexte)
            c["application"] = application
            travail.contexte = c
            return f"{application} installé sur {hebergement.domaineProvisoire}"
        if index == 2:
            hebergement = await depot.obtenir(ctx, site.hebergementId)
            heb_secrets = await depot.secrets(ctx, hebergement.id)
            zone = await zone_vps_secrets(ctx)
            regle = await asyncio.to_thread(
                amont_network().ajouter_regle_hote,
                listener_id=zone.get("lb_listener_id"),
                loadbalancer_id=zone.get("lb_id"),
                pool_id=heb_secrets.get("lb_pool_id"),
                hote=site.hote,
            )
            await depot_sites.definir_secrets(ctx, site.id, {"lb_policy_id": regle["policy_id"]})
            c = dict(travail.contexte)
            c["lb_policy_id"] = regle["policy_id"]
            travail.contexte = c
            return f"Domaine {site.hote} routé sur le load balancer partagé"
        return None

    async def terminer(self, ctx: Contexte, travail: Travail) -> None:
        await depot_sites.definir_statut(ctx, travail.cible_id or "", "en_ligne")

    async def compenser(self, ctx: Contexte, travail: Travail, index_echoue: int) -> None:
        site = await depot_sites.obtenir(ctx, travail.cible_id or "")
        try:
            secrets = await depot_sites.secrets(ctx, site.id)
        except Exception:  # noqa: BLE001
            secrets = {}
        zone = await zone_vps_secrets(ctx)
        policy_id = travail.contexte.get("lb_policy_id") or secrets.get("lb_policy_id")
        if policy_id:
            await asyncio.to_thread(
                amont_network().supprimer_regle_hote, policy_id, loadbalancer_id=zone.get("lb_id")
            )
        hebergement = await depot.trouver(ctx, site.hebergementId)
        cle_privee = zone.get("ssh_prive")
        ip = hebergement and await ip_gestion_hebergement(ctx, hebergement)
        if hebergement and cle_privee and ip:
            racine = f"{_RACINE_DOCKER}/sites/{site.id}"
            await asyncio.to_thread(
                amont_ssh().executer,
                ip,
                cle_privee,
                f"cd {racine} && docker compose down -v; rm -rf {racine} "
                f"{_RACINE_DOCKER}/traefik-dynamic/site-{site.id}.yml",
            )
        await depot_sites.definir_statut(ctx, site.id, "suspendu")


@executeur("site.supprimer")
class ExecuteurSiteSupprimer(Executeur):
    async def terminer(self, ctx: Contexte, travail: Travail) -> None:
        site = await depot_sites.obtenir(ctx, travail.cible_id or "")
        try:
            secrets = await depot_sites.secrets(ctx, site.id)
        except Exception:  # noqa: BLE001
            secrets = {}
        zone = await zone_vps_secrets(ctx)
        policy_id = secrets.get("lb_policy_id")
        if policy_id:
            await asyncio.to_thread(
                amont_network().supprimer_regle_hote, policy_id, loadbalancer_id=zone.get("lb_id")
            )
        hebergement = await depot.trouver(ctx, site.hebergementId)
        cle_privee = zone.get("ssh_prive")
        ip = hebergement and await ip_gestion_hebergement(ctx, hebergement)
        if hebergement and cle_privee and ip:
            racine = f"{_RACINE_DOCKER}/sites/{site.id}"
            await asyncio.to_thread(
                amont_ssh().executer,
                ip,
                cle_privee,
                f"cd {racine} && docker compose down -v; rm -rf {racine} "
                f"{_RACINE_DOCKER}/traefik-dynamic/site-{site.id}.yml",
            )
        await depot_sites.supprimer(ctx, site.id, logique=True)


@executeur("site.analyse_securite")
class ExecuteurSiteAnalyse(Executeur):
    async def terminer(self, ctx: Contexte, travail: Travail) -> None:
        s = await depot_sites.obtenir(ctx, travail.cible_id or "")
        await depot_sites.remplacer(
            ctx,
            s.id,
            s.model_copy(
                update={"securite": m.Securite(waf=True, bruteForce=True, scanMalware=True)}
            ),
        )


@executeur("site.preproduction")
class ExecuteurSitePreproduction(Executeur):
    async def terminer(self, ctx: Contexte, travail: Travail) -> None:
        s = await depot_sites.obtenir(ctx, travail.cible_id or "")
        await depot_sites.remplacer(
            ctx,
            s.id,
            s.model_copy(
                update={
                    "preproduction": m.Preproduction(
                        actif=True, hote=f"preprod.{s.hote}", derniereSync=maintenant()
                    ),
                    "statut": "en_ligne",
                }
            ),
        )


@executeur("site.mise_en_production")
class ExecuteurSiteMiseEnProduction(Executeur):
    async def terminer(self, ctx: Contexte, travail: Travail) -> None:
        s = await depot_sites.obtenir(ctx, travail.cible_id or "")
        await depot_sites.remplacer(
            ctx, s.id, s.model_copy(update={"preproduction": None, "statut": "en_ligne"})
        )


@executeur("site.mise_a_jour")
class ExecuteurSiteMiseAJour(Executeur):
    async def terminer(self, ctx: Contexte, travail: Travail) -> None:
        s = await depot_sites.obtenir(ctx, travail.cible_id or "")
        await depot_sites.remplacer(ctx, s.id, s.model_copy(update={"majEnAttente": 0}))


@executeur("base.export")
class ExecuteurBaseExport(Executeur):
    async def terminer(self, ctx: Contexte, travail: Travail) -> None:
        return None


@executeur("base.import")
class ExecuteurBaseImport(Executeur):
    async def terminer(self, ctx: Contexte, travail: Travail) -> None:
        return None


@executeur("tache.execution")
class ExecuteurTacheExecution(Executeur):
    async def terminer(self, ctx: Contexte, travail: Travail) -> None:
        t = await depot_taches.obtenir(ctx, travail.cible_id or "")
        await depot_taches.remplacer(
            ctx,
            t.id,
            t.model_copy(update={"derniereExecution": maintenant(), "statut": "ok", "dureeS": 3}),
        )


async def depot_enfants(ctx: Contexte, hebergement_id: str) -> None:
    await depot_comptes.supprimer_enfants(ctx, hebergement_id)
    await depot_taches.supprimer_enfants(ctx, hebergement_id)
    # Chaque application (`site.installer`) posée sur cet hébergement porte sa propre règle
    # L7 sur le pool partagé de la VM — Octavia refuse de supprimer un pool encore référencé
    # par une policy (`Pool ... is in use by L7 policy ...`, vécu en direct). Les retirer
    # d'abord est ce qui débloque la suppression du pool par `ExecuteurHebergementSupprimer`
    # juste après. La VM elle-même étant sur le point d'être détruite, inutile d'y faire un
    # `docker compose down` en plus : seule la policy compte ici.
    zone = await zone_vps_secrets(ctx)
    for s in await depot_sites.tous(ctx, filtre=lambda x: x.hebergementId == hebergement_id):
        try:
            secrets = await depot_sites.secrets(ctx, s.id)
        except Exception:  # noqa: BLE001
            secrets = {}
        policy_id = secrets.get("lb_policy_id")
        if policy_id:
            await asyncio.to_thread(
                amont_network().supprimer_regle_hote, policy_id, loadbalancer_id=zone.get("lb_id")
            )
        await depot_sites.supprimer(ctx, s.id, logique=True)
    # Drive n'est pas un enfant au sens du dépôt (pas de `hebergementId`, résolu par domaine) :
    # import tardif pour éviter le cycle (web_drive importe déjà web_hebergement). Même raison
    # de le faire ici — sa policy L7 vit sur ce même pool.
    from synelia.modules.web_drive.service import depot as depot_drive

    h = await depot.trouver(ctx, hebergement_id)
    if h and h.domaine:
        for d in await depot_drive.tous(ctx, filtre=lambda x: x.domaine == h.domaine):
            try:
                secrets = await depot_drive.secrets(ctx, d.id)
            except Exception:  # noqa: BLE001
                secrets = {}
            policy_id = secrets.get("lb_policy_id")
            if policy_id:
                await asyncio.to_thread(
                    amont_network().supprimer_regle_hote,
                    policy_id,
                    loadbalancer_id=zone.get("lb_id"),
                )
            await depot_drive.supprimer(ctx, d.id, logique=True)
