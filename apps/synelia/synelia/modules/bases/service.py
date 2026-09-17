from __future__ import annotations

import asyncio

from synelia_contract import modeles as m
from synelia_db.modeles import Travail
from synelia_kernel.ids import jeton_opaque
from synelia_openstack import fournisseur
from synelia_openstack.compute import ComputeOpenStack, ComputeSimule

from synelia.depot import Depot
from synelia.deps.contexte import Contexte
from synelia.travaux import Executeur, executeur

depot = Depot("base_managee", m.BaseManagee)

PORTS = {"postgresql": 5432, "mysql": 3306, "mariadb": 3306, "mongodb": 27017, "redis": 6379}


def amont() -> ComputeSimule:
    return fournisseur(ComputeSimule, ComputeOpenStack)


# Le lab ne possède aujourd'hui que deux gabarits Nova réels — taillés pour Kubernetes,
# pas de gabarit dédié aux bases managées. On rattache les paliers les plus légers à
# `k8s.worker` et les autres à `k8s.master`, même convention que web_hebergement.
_GABARIT_NOM_PAR_PALIER = {
    "s1": "k8s.worker",
    "s2": "k8s.worker",
    "m1": "k8s.master",
    "m2": "k8s.master",
    "l1": "k8s.master",
    "xl1": "k8s.master",
}
# Repli en mode simulé (catalogue de démo sans `k8s.*`) : le plus proche du palier.
_GABARIT_SIMULE_PAR_PALIER = {
    "s1": "s1.small",
    "s2": "g1.medium",
    "m1": "g1.medium",
    "m2": "g1.large",
    "l1": "g1.large",
    "xl1": "g1.xlarge",
}


async def gabarit_pour_palier(palier: str) -> str:
    nom = _GABARIT_NOM_PAR_PALIER.get(palier, "k8s.worker")
    # `amont().gabarits()` (openstacksdk, synchrone) est déchargé via `asyncio.to_thread` :
    # même garde que `vms.service`, sans quoi un appel amont lent gèlerait la boucle asyncio
    # — donc l'API entière, tous tenants confondus.
    gabarits = await asyncio.to_thread(amont().gabarits)
    g = next((f for f in gabarits if f["nom"] == nom), None)
    if g:
        return str(g["id"])
    return _GABARIT_SIMULE_PAR_PALIER.get(palier, "g1.medium")


async def image_ubuntu() -> str:
    """Image système de l'instance : Ubuntu 24.04, ou la plus proche disponible."""
    images = await asyncio.to_thread(amont().images)
    img = next((i for i in images if i["id"] == "ubuntu-24.04"), None)
    if img is None:
        img = next((i for i in images if "ubuntu" in i["nom"].lower()), None)
    if img is None:
        img = images[0] if images else None
    return str(img["id"]) if img else "ubuntu-24.04"


# Image Docker + variables d'environnement de chaque moteur. `env_root_pass` fixe le mot de
# passe superutilisateur exigé par l'image officielle ; `env_user`/`env_pass` créent en plus
# un compte applicatif dédié (repris par `.../identifiants`) ; `env_db` pré-crée la base.
_MOTEUR_DOCKER: dict[str, dict[str, str]] = {
    "postgresql": {
        "image": "postgres",
        "env_root_pass": "POSTGRES_PASSWORD",
        "env_user": "POSTGRES_USER",
        "env_db": "POSTGRES_DB",
    },
    "mysql": {
        "image": "mysql",
        "env_root_pass": "MYSQL_ROOT_PASSWORD",
        "env_user": "MYSQL_USER",
        "env_pass": "MYSQL_PASSWORD",
        "env_db": "MYSQL_DATABASE",
    },
    "mariadb": {
        "image": "mariadb",
        "env_root_pass": "MARIADB_ROOT_PASSWORD",
        "env_user": "MARIADB_USER",
        "env_pass": "MARIADB_PASSWORD",
        "env_db": "MARIADB_DATABASE",
    },
    "mongodb": {
        "image": "mongo",
        "env_root_pass": "MONGO_INITDB_ROOT_PASSWORD",
        "env_user": "MONGO_INITDB_ROOT_USERNAME",
        "env_db": "MONGO_INITDB_DATABASE",
    },
    "redis": {"image": "redis"},
}


def utilisateur_pour_moteur(moteur: str) -> str:
    return "default" if moteur == "redis" else f"synelia_{moteur}"


def generer_identifiants(moteur: str) -> dict[str, str]:
    return {"utilisateur": utilisateur_pour_moteur(moteur), "mot_de_passe": jeton_opaque(16)}


def nouveau_mot_de_passe() -> str:
    return jeton_opaque(16)


def construire_cloud_init(moteur: str, version: str, port: int, mot_de_passe: str, nom_base: str) -> str:
    """`#cloud-config` minimal : Docker + le moteur demandé, exposé sur toutes les interfaces
    de la VM. La VM ne reçoit volontairement aucune IP flottante (cf. `BaseManageeCreation.
    sourcesAutorisees`, un filtrage par CIDR côté réseau privé, pas un accès public) : le port
    n'est donc atteignable que depuis le réseau privé de l'Espace Cloud qui l'héberge."""
    spec = _MOTEUR_DOCKER.get(moteur, _MOTEUR_DOCKER["postgresql"])
    image = f"{spec['image']}:{version}" if version else spec["image"]
    utilisateur = utilisateur_pour_moteur(moteur)
    variables = []
    if spec.get("env_root_pass"):
        variables.append(f"-e {spec['env_root_pass']}={mot_de_passe}")
    if spec.get("env_user"):
        variables.append(f"-e {spec['env_user']}={utilisateur}")
    if spec.get("env_pass"):
        variables.append(f"-e {spec['env_pass']}={mot_de_passe}")
    if spec.get("env_db"):
        variables.append(f"-e {spec['env_db']}={nom_base}")
    commande = f"redis-server --requirepass {mot_de_passe}" if moteur == "redis" else ""
    docker_run = " ".join(
        part
        for part in (
            "docker run -d --restart unless-stopped --name base-managee",
            f"-p {port}:{port}",
            *variables,
            image,
            commande,
        )
        if part
    )
    return (
        "#cloud-config\n"
        "package_update: true\n"
        "packages:\n"
        "  - docker.io\n"
        "runcmd:\n"
        "  - systemctl enable --now docker\n"
        f"  - {docker_run}\n"
    )


def ip_privee(base_id: str) -> str:
    return f"10.{hash(base_id) % 250}.0.{hash('db') % 250 + 2}"


async def serveur_id(ctx: Contexte, base_id: str, travail: Travail | None = None) -> str:
    """Identifiant Nova du serveur : dans les secrets de la base (posé à la création), sinon le travail."""
    if travail and travail.contexte.get("serveur_id"):
        return str(travail.contexte["serveur_id"])
    try:
        sec = await depot.secrets(ctx, base_id)
    except Exception:  # noqa: BLE001
        sec = {}
    return str(sec.get("serveur_id") or base_id)


@executeur("base.create")
class ExecuteurBaseCreate(Executeur):
    compensable = True

    async def etape(self, ctx: Contexte, travail: Travail, index: int, nom: str) -> str | None:
        if index == 0:
            base = await depot.obtenir(ctx, travail.cible_id or "")
            entre = travail.entree or {}
            from synelia.modules.espaces.service import depot as depot_espaces

            secrets_espace = await depot_espaces.secrets(ctx, base.espaceId)
            secrets_base = await depot.secrets(ctx, base.id)
            port = PORTS.get(base.moteur, 5432)
            image_id = await image_ubuntu()
            gabarit_id = entre.get("gabarit") or await gabarit_pour_palier(base.palier)
            srv = await asyncio.to_thread(
                amont().creer_serveur,
                nom=f"db-{base.id[:8]}",
                image_id=image_id,
                gabarit_id=gabarit_id,
                reseau_id=entre.get("reseauId") or secrets_espace.get("reseau_id"),
                identifiants=secrets_espace,
                org_id=ctx.org_id_ou_none,
                espace_id=base.espaceId,
                cloud_init=construire_cloud_init(
                    base.moteur, base.version, port, secrets_base.get("mot_de_passe", ""), base.nom
                ),
            )
            c = dict(travail.contexte)
            c["serveur_id"] = srv["id"]
            await depot.definir_secrets(ctx, base.id, {"serveur_id": srv["id"]})
            c["ip_privee"] = srv.get("ip_privee") or ip_privee(base.id)
            travail.contexte = c
            return f"Serveur amont {srv['id']} créé"
        return None

    async def terminer(self, ctx: Contexte, travail: Travail) -> None:
        ip = travail.contexte.get("ip_privee") or ip_privee(travail.cible_id or "")
        await depot.modifier(ctx, travail.cible_id or "", {"host": ip, "statut": "running"})

    async def compenser(self, ctx: Contexte, travail: Travail, index_echoue: int) -> None:
        sid = await serveur_id(ctx, travail.cible_id or "", travail)
        if sid and sid != (travail.cible_id or ""):
            await asyncio.to_thread(amont().supprimer_serveur, sid)
        await depot.definir_statut(ctx, travail.cible_id or "", "degraded")


@executeur("base.replica")
class ExecuteurBaseReplica(Executeur):
    async def terminer(self, ctx: Contexte, travail: Travail) -> None:
        base = await depot.obtenir(ctx, travail.cible_id or "")
        await depot.remplacer(
            ctx,
            travail.cible_id or "",
            base.model_copy(
                update={"replicas": travail.contexte.get("replicas", base.replicas + 1)}
            ),
        )


@executeur("base.restore")
class ExecuteurBaseRestore(Executeur):
    async def etape(self, ctx: Contexte, travail: Travail, index: int, nom: str) -> str | None:
        if index == 0:
            sid = await serveur_id(ctx, travail.cible_id or "", travail)
            if sid and sid != (travail.cible_id or ""):
                await asyncio.to_thread(amont().action, sid, "redemarrage")
        return None

    async def terminer(self, ctx: Contexte, travail: Travail) -> None:
        await depot.definir_statut(ctx, travail.cible_id or "", "running")


@executeur("base.delete")
class ExecuteurBaseDelete(Executeur):
    async def terminer(self, ctx: Contexte, travail: Travail) -> None:
        sid = await serveur_id(ctx, travail.cible_id or "", travail)
        if sid and sid != (travail.cible_id or ""):
            await asyncio.to_thread(amont().supprimer_serveur, sid)
        await depot.supprimer(ctx, travail.cible_id or "", logique=True)
