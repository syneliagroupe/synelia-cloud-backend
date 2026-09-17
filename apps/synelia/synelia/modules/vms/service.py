from __future__ import annotations

import asyncio

from sqlalchemy import select
from synelia_contract import modeles as m
from synelia_db.modeles import Organisation, Ressource, Travail, Utilisateur
from synelia_kernel import erreurs
from synelia_kernel.dates import maintenant
from synelia_kernel.ids import nouvel_id
from synelia_openstack import fournisseur
from synelia_openstack.compute import ComputeOpenStack, ComputeSimule

from synelia.audit import journaliser
from synelia.demo import peupleur
from synelia.depot import Depot
from synelia.deps.contexte import Contexte
from synelia.travaux import Executeur, demarrer_travail, executeur

depot = Depot("vm", m.Vm, champs_recherche=("nom", "os"))
instantane_depot = Depot(
    "vm_instantane", m.InstantaneVm, libelle="Instantané de machine", champ_nom="nom"
)


def amont() -> ComputeSimule:
    return fournisseur(ComputeSimule, ComputeOpenStack)


def ip_privee(vm: m.Vm) -> str:
    return f"10.{hash(vm.nom) % 250}.0.{hash(vm.os) % 250 + 2}"


def ip_publique(vm: m.Vm) -> str:
    return f"196.202.{hash(vm.nom) % 250}.{hash(vm.os) % 250 + 2}"


async def serveur_id(ctx: Contexte, vm_id: str, travail: Travail | None = None) -> str:
    """Identifiant Nova du serveur : dans les secrets de la VM (posé à la création), sinon le contexte du travail."""
    if travail and travail.contexte.get("serveur_id"):
        return str(travail.contexte["serveur_id"])
    try:
        sec = await depot.secrets(ctx, vm_id)
    except Exception:  # noqa: BLE001
        sec = {}
    return str(sec.get("serveur_id") or vm_id)


async def _groupes_securite_neutron(ctx: Contexte, groupes_locaux: list[str] | None) -> list[str]:
    """`groupesSecurite` (contrat `VmLotCreation`/`VmCreation`) donne des id locaux (dépôt
    `groupe_securite`) : Nova attend l'id Neutron réel, posé dans les secrets du groupe par
    `creer_groupe_amont` (`reseau/service.py`), pas l'id local. Un groupe introuvable ou sans
    secret réel (encore en cours de provisioning, ou id périmé) est ignoré plutôt que de faire
    échouer toute la création de VM pour une sélection annexe."""
    if not groupes_locaux:
        return []
    from synelia.modules.reseau.service import depot_groupe

    ids: list[str] = []
    for gid in groupes_locaux:
        try:
            secrets = await depot_groupe.secrets(ctx, gid)
        except Exception:  # noqa: BLE001, S112 — groupe introuvable/périmé, ignoré pas fatal
            continue
        neutron_id = secrets.get("groupe_id")
        if neutron_id:
            ids.append(str(neutron_id))
    return ids


def _diagnostics_vers_valeurs(
    avant: dict, apres: dict, delta_s: float, vcpu: int
) -> dict[str, float]:
    """CPU et réseau sont des compteurs cumulés côté hyperviseur (temps CPU en ns depuis le
    démarrage, octets depuis le démarrage de l'interface) : un seul relevé `diagnostics` ne
    donne qu'un total, jamais un pourcentage ni un débit — d'où les deux relevés espacés de
    `delta_s` passés par l'appelant. La mémoire, elle, est déjà une jauge instantanée
    (`memory-actual`/`memory-unused`), lue sur le second relevé seul."""

    def _somme(d: dict, suffixe: str, prefixe: str = "") -> float:
        return sum(v for k, v in d.items() if k.endswith(suffixe) and k.startswith(prefixe))

    cpu_pct = 0.0
    if delta_s > 0 and vcpu > 0:
        delta_ns = _somme(apres, "_time", "cpu") - _somme(avant, "_time", "cpu")
        cpu_pct = max(0.0, min(100.0, delta_ns / (delta_s * 1e9 * vcpu) * 100))

    reseau_mo_s = 0.0
    if delta_s > 0:
        delta_rx = _somme(apres, "_rx") - _somme(avant, "_rx")
        reseau_mo_s = max(0.0, delta_rx / delta_s / 1_000_000)

    actuelle = float(apres.get("memory-actual") or 0)
    libre = float(apres.get("memory-unused") or apres.get("memory-available") or 0)
    ram_pct = max(0.0, min(100.0, (1 - libre / actuelle) * 100)) if actuelle else 0.0

    return {"cpu": cpu_pct, "ram": ram_pct, "reseau_entrant": reseau_mo_s}


_DELTA_DIAGNOSTICS_S = 0.6


async def diagnostics_instantanes(ctx: Contexte, vm: m.Vm) -> dict[str, float] | None:
    """CPU/RAM/réseau instantanés d'une VM active, dérivés de deux relevés Nova
    `.../diagnostics` (données réelles de l'hyperviseur) espacés de `_DELTA_DIAGNOSTICS_S`.
    `None` en simulation ou si Nova ne répond pas (VM en train de basculer d'état, par
    exemple) : l'appelant retombe alors sur des séries vides plutôt qu'une valeur inventée —
    même politique que le reste du module (`journaux`, `console`)."""
    sid = await serveur_id(ctx, vm.id)
    avant = await asyncio.to_thread(amont().diagnostics, sid)
    if avant is None:
        return None
    await asyncio.sleep(_DELTA_DIAGNOSTICS_S)
    apres = await asyncio.to_thread(amont().diagnostics, sid)
    if apres is None:
        return None
    return _diagnostics_vers_valeurs(avant, apres, _DELTA_DIAGNOSTICS_S, vm.vcpu)


# États contrôlés à la lecture : une VM dans l'un de ces statuts **doit** avoir un serveur Nova
# derrière elle, qui peut avoir disparu depuis le dernier relevé — elle vaut la peine d'être
# vérifiée en direct (cf. `reconcilier_statut`). `creating` en est exclu volontairement : entre la
# création de la ligne et l'étape 1 du travail, le serveur Nova n'existe pas encore (le secret
# `serveur_id` non plus) — une relecture dans cette fenêtre croirait à un orphelin. `migrating`
# est porté par son travail. `error` y figure : c'est le statut des lignes déjà marquées
# (réconciliation précédente, compensation d'un travail tombé) — on les reconfirme à chaque
# lecture, un serveur Nova de nouveau connu (`ERROR`) n'étant jamais supprimé, un orphelin
# confirmé l'étant (décision propriétaire, cf. `_traiter_orphelin`).
STATUTS_STABLES = {"running", "stopped"}
STATUTS_A_CONTROLER = STATUTS_STABLES | {"error"}


def _mapper_statut_nova(statut_amont: str) -> str | None:
    """Statut sincère à écrire quand l'amont Nova contredit la ligne, `None` sinon (on ne touche
    alors pas la ressource).

    Le contrôle est volontairement restreint aux états **cassés** — `absente` (Nova ne connaît
    plus le serveur : supprimé hors bande, ex. nettoyage manuel du lab ; la VM et ses disques
    n'existent plus, `error` est le seul statut sincère du contrat, `stopped` ferait croire à une
    machine simplement arrêtée, redémarrable) et `ERROR` — pas à une synchronisation complète :
    les transitions vivantes (ACTIVE/SHUTOFF/BUILDING…) sont déjà portées par les propres travaux
    de l'application à chaque mutation (`vm.create`, `vm.power.*`, `vm.resize`), et l'amont
    simulé, lui, ne retient aucun état (son `action("arret")` est un no-op, `statut_serveur`
    répond toujours `ACTIVE`) — une relecture qui y traduirait `ACTIVE` en `running` annulerait
    l'arrêt que le travail vient de poser (cassé au premier essai dans la suite de tests).
    `SHUTOFF` en particulier n'est **pas** un orphelin : les invités du lab s'éteignent la nuit
    et sont redémarrés."""
    s = statut_amont.upper()
    if s in ("ABSENTE", "ERROR"):
        return "error"
    return None


async def reconcilier_statut(ctx: Contexte, vm: m.Vm) -> m.Vm:
    """Relit l'existence et l'état réel du serveur côté Nova et rend la ligne sincère si le
    serveur a disparu ou est mort depuis le dernier relevé, avant de la renvoyer.

    La ligne en base peut survivre à son infra réelle : une VM Nova supprimée hors bande (nettoyage
    manuel du lab, travail tombé en échec sans compensation) continue de s'afficher `running` dans
    les listes et les tableaux de bord, et c'est seulement au premier usage (SSH, console…) que
    l'écart se voit — cf. l'hébergement `verif-final.example.com`, resté `en_ligne` des heures
    après la disparition de sa VM. Même motif « reconcile-on-read » que `web_hebergement` et
    `kubernetes` (cf. `docs/GUIDE-MODULE.md`, invariant « Réconcilier à la lecture ») : sur toute
    lecture d'une VM en statut contrôlé, relit le statut réel Nova et persiste l'écart s'il est
    cassé (cf. `_mapper_statut_nova`), avant de renvoyer la ressource.

    Orphelin confirmé (Nova ignore le serveur référencé — purgé ou soft-delete `DELETED` pas
    encore purgé — alors que la ligne a passé la fenêtre de grâce de la création — statuts
    contrôlés seulement) : décision propriétaire, la ligne est **supprimée** par le même chemin
    métier que DELETE /vms/{id} (exécuteur `vm.delete`, cf. `_traiter_orphelin`), pas seulement
    marquée `error`."""
    if vm.statut not in STATUTS_A_CONTROLER:
        return vm
    try:
        secrets = await depot.secrets(ctx, vm.id)
    except Exception:  # noqa: BLE001
        return vm
    sid = secrets.get("serveur_id")
    # Sans `serveur_id` (ligne de démo, VM antérieure au câblage Nova), la VM n'a jamais
    # référencé d'infrastructure réelle identifiable : `serveur_id()` retomberait sur l'id
    # applicatif, que Nova ne connaît pas — un contrôle là-dessus marquerait en erreur des
    # lignes qui ne sont pas orphelines. On n'affirme « absente » qu'à propos d'un serveur
    # qu'on sait avoir existé.
    if not sid:
        return vm
    try:
        statut_amont = await asyncio.to_thread(amont().statut_serveur, sid)
    except Exception:  # noqa: BLE001
        # Une panne/latence Keystone-Nova transitoire (`ServiceUnavailable`…) ne doit pas faire
        # échouer toute une liste de VMs pour un incident ponctuel d'amont — même esprit que le
        # `except` juste au-dessus sur `depot.secrets` : on renvoie la ligne telle quelle plutôt
        # que de laisser l'exception remonter nue (elle traverserait alors `CORSMiddleware` sans
        # en-têtes CORS, un client web la verrait comme un blocage CORS plutôt qu'un vrai 5xx).
        return vm
    if statut_amont.upper() in ("ABSENTE", "DELETED"):
        # `ABSENTE` : Nova ignore le serveur (purgé) ; `DELETED` : ligne soft-delete encore
        # visible de Nova (purge asynchrone) — dans les deux cas l'objet n'existe plus
        # réellement (pas d'hyperviseur, pas d'IP) : c'est un orphelin confirmé.
        return await _traiter_orphelin(ctx, vm)
    nouveau = _mapper_statut_nova(statut_amont)
    if nouveau and nouveau != vm.statut:
        return await depot.definir_statut(ctx, vm.id, nouveau)
    return vm


ETAPES_SUPPRESSION = [
    {"nom": "Arrêter la machine", "dureeS": 18},
    {"nom": "Supprimer les disques", "dureeS": 12},
    {"nom": "Libérer les adresses IP", "dureeS": 6},
]


def _suppression_automatique_interdite(vm: m.Vm) -> bool:
    """Garde-fou de la décision propriétaire : l'espace partagé `vps-zone` (plateforme, org NULL
    — réseau, load balancer et VM PaaS de la plateforme) n'est jamais la cible d'une suppression
    automatique. Une indisponibilité ponctuelle d'amont ne doit jamais pouvoir coûter une
    infrastructure de plateforme : ces lignes restent marquées, jamais détruites."""
    from synelia.modules.espaces.service import ESPACE_ZONE_VPS_ID

    return vm.espaceId == ESPACE_ZONE_VPS_ID


async def _suppression_en_cours(ctx: Contexte, cible_id: str, type_travail: str) -> bool:
    """Un travail de suppression est-il déjà en vol pour cette ressource (DELETE utilisateur,
    réconciliation d'une lecture concurrente) ? Le statut posé avant le lancement (commit au
    moment de `demarrer_travail`) couvre les lectures séquentielles ; ce contrôle couvre les
    lectures concurrentes — sans lui, deux GET simultanés lanceraient deux `vm.delete`."""
    q = select(Travail).where(
        Travail.cible_id == cible_id,
        Travail.type == type_travail,
        Travail.statut.in_(("queued", "running")),
    )
    return (await ctx.session.execute(q)).scalars().first() is not None


async def _traiter_orphelin(ctx: Contexte, vm: m.Vm) -> m.Vm:
    """Orphelin confirmé (Nova ignore le serveur référencé, fenêtre de grâce de la création
    passée) : décision propriétaire, on le **supprime** réellement — même chemin métier que
    DELETE /vms/{id} (exécuteur `vm.delete` et sa compensation habituelle), pas un simple
    marquage. Garde-fous : ligne de l'espace partagé `vps-zone`, ou suppression déjà en vol
    (DELETE utilisateur, lecture concurrente) → on se borne au marquage sincère `error`."""
    if _suppression_automatique_interdite(vm) or await _suppression_en_cours(
        ctx, vm.id, "vm.delete"
    ):
        return vm if vm.statut == "error" else await depot.definir_statut(ctx, vm.id, "error")
    return await _supprimer_orphelin(ctx, vm)


async def _supprimer_orphelin(ctx: Contexte, vm: m.Vm) -> m.Vm:
    """Suppression réelle d'une VM orpheline confirmée, par le chemin métier du DELETE : la
    ligne est d'abord marquée `error` (sincère immédiatement — et une lecture concurrente ne
    redéclenchera pas, le travail de suppression étant déjà en vol), puis le travail `vm.delete`
    détruit le serveur amont restant s'il en reste un (Nova répond NotFound à un serveur déjà
    disparu — succès, pas échec) et retire la ligne. Si le travail échoue, la ligne reste
    `error` : sincère, requalifiable à la main."""
    marque = await depot.definir_statut(ctx, vm.id, "error")
    await journaliser(
        ctx,
        action="vm.suppression_orpheline",
        cible_type="vm",
        cible_id=vm.id,
        cible=vm.nom,
        details={"origine": "reconciliation_orphelin"},
    )
    await demarrer_travail(
        ctx,
        "vm.delete",
        vm.nom,
        cible_type="vm",
        cible_id=vm.id,
        etapes=ETAPES_SUPPRESSION,
        contexte={"origine": "reconciliation_orphelin"},
    )
    return marque


@executeur("vm.create")
class ExecuteurVmCreate(Executeur):
    compensable = True

    async def etape(self, ctx: Contexte, travail: Travail, index: int, nom: str) -> str | None:
        if index == 1:
            vm = await depot.obtenir(ctx, travail.cible_id or "")
            entre = travail.entree or {}
            from synelia.modules.espaces.service import depot as depot_espaces

            secrets_espace = await depot_espaces.secrets(ctx, vm.espaceId)
            groupes_neutron = await _groupes_securite_neutron(ctx, entre.get("groupesSecurite"))
            # `creer_serveur` (comme les autres appels `amont()` de ce fichier) est un appel
            # openstacksdk synchrone/bloquant : exécuté tel quel dans la coroutine, il bloquerait
            # toute la boucle asyncio — donc toute l'API, pour tous les tenants — jusqu'à sa fin
            # (constaté en direct via `vm.resize`, qui pouvait ainsi geler l'API entière plusieurs
            # minutes). On le décharge donc systématiquement dans un thread.
            srv = await asyncio.to_thread(
                amont().creer_serveur,
                nom=vm.nom,
                image_id=entre.get("imageId"),
                # `vm.flavor` (posé par `_specs()` à la création) est le gabarit résolu, y compris
                # quand la requête ne donnait que vcpu/ramGo/diskGo : `entre.get("gabarit")` serait
                # resté vide dans ce cas et Nova aurait reçu un `flavorRef` nul.
                gabarit_id=vm.flavor,
                reseau_id=entre.get("reseauId") or secrets_espace.get("reseau_id"),
                identifiants=secrets_espace,
                org_id=ctx.org_id_ou_none,
                espace_id=vm.espaceId,
                cle_ssh=entre.get("cleSsh"),
                cloud_init=entre.get("cloudInit"),
                groupes_securite=groupes_neutron,
            )
            c = dict(travail.contexte)
            c["serveur_id"] = srv["id"]
            await depot.definir_secrets(ctx, vm.id, {"serveur_id": srv["id"]})
            c["ip_privee"] = srv.get("ip_privee") or ip_privee(vm)
            travail.contexte = c
            return f"Serveur amont {srv['id']} créé"
        return None

    async def terminer(self, ctx: Contexte, travail: Travail) -> None:
        vm = await depot.obtenir(ctx, travail.cible_id or "")
        entre = travail.entree or {}
        ips = [m.Ip(adresse=travail.contexte.get("ip_privee") or ip_privee(vm), type="privee")]
        if entre.get("ipPubliqueDemandee"):
            ips.append(m.Ip(adresse=ip_publique(vm), type="publique"))
        await depot.modifier(
            ctx, travail.cible_id or "", {"statut": "running", "ips": [i.model_dump() for i in ips]}
        )

    async def compenser(self, ctx: Contexte, travail: Travail, index_echoue: int) -> None:
        sid = await serveur_id(ctx, travail.cible_id or "", travail)
        if sid and sid != (travail.cible_id or ""):
            await asyncio.to_thread(amont().supprimer_serveur, sid)
        await depot.definir_statut(ctx, travail.cible_id or "", "error")


@executeur("vm.compose")
class ExecuteurVmCompose(Executeur):
    """Déploie plusieurs serveurs en une passe (`/vms/lot`, écran de composition) — un vrai
    serveur Nova par machine du plan (`amont().creer_serveur`, gabarit déjà résolu par le
    routeur dans `entree["gabarits"]`), pas une simple insertion en base : la même classe de
    bug (« faux succès ») que `vm.create`/`vm.resize` avant leurs fixes respectifs."""

    compensable = True

    async def etape(self, ctx: Contexte, travail: Travail, index: int, nom: str) -> str | None:
        if index == 0:
            entre = travail.entree or {}
            espace_id = entre.get("espaceId")
            from synelia.modules.espaces.service import depot as depot_espaces

            secrets_espace = await depot_espaces.secrets(ctx, espace_id) if espace_id else {}
            gabarits = entre.get("gabarits") or {}
            groupes_neutron = await _groupes_securite_neutron(ctx, entre.get("groupesSecurite"))
            serveurs = []
            for mac in entre.get("machines") or []:
                quantite = mac.get("quantite") or 1
                for i in range(quantite):
                    machine_nom = mac["nom"] if quantite == 1 else f"{mac['nom']}{i + 1}"
                    srv = await asyncio.to_thread(
                        amont().creer_serveur,
                        nom=machine_nom,
                        image_id=mac["imageId"],
                        gabarit_id=gabarits.get(mac["nom"]),
                        reseau_id=entre.get("reseauId") or secrets_espace.get("reseau_id"),
                        identifiants=secrets_espace,
                        org_id=ctx.org_id_ou_none,
                        espace_id=espace_id,
                        cle_ssh=entre.get("cleSsh"),
                        cloud_init=entre.get("cloudInit"),
                        groupes_securite=groupes_neutron,
                    )
                    serveurs.append(
                        {
                            "nom": machine_nom,
                            "serveur_id": srv["id"],
                            "image_id": mac["imageId"],
                            "vcpu": mac["vcpu"],
                            "ramGo": mac["ramGo"],
                            "diskGo": mac["diskGo"],
                            "nics": mac.get("nics") or 1,
                            "ip_privee": srv.get("ip_privee") or ip_privee_mac(machine_nom, mac["imageId"]),
                            "backupPlanId": mac.get("backupPlanId"),
                        }
                    )
            c = dict(travail.contexte)
            c["serveurs"] = serveurs
            travail.contexte = c
            return f"{len(serveurs)} serveurs amont créés"
        return None

    async def terminer(self, ctx: Contexte, travail: Travail) -> None:
        entre = travail.entree or {}
        espace_id = entre.get("espaceId")
        site = entre.get("site") or "ABJ"
        for srv in travail.contexte.get("serveurs") or []:
            vm = m.Vm(
                id=nouvel_id(),
                espaceId=espace_id,
                nom=srv["nom"],
                os=srv["image_id"],
                vcpu=srv["vcpu"],
                ramGo=srv["ramGo"],
                diskGo=srv["diskGo"],
                ips=[m.Ip(adresse=srv["ip_privee"], type="privee")],
                statut="running",
                hardware=m.MateielVirtuel(
                    scsiControllers=1, nics=srv["nics"], usb=False, secureBoot=False
                ),
                site=site,
                backupPlanId=srv.get("backupPlanId"),
            )
            await depot.creer(ctx, vm, parent_id=espace_id)
            await depot.definir_secrets(ctx, vm.id, {"serveur_id": srv["serveur_id"]})

    async def compenser(self, ctx: Contexte, travail: Travail, index_echoue: int) -> None:
        for srv in travail.contexte.get("serveurs") or []:
            try:
                await asyncio.to_thread(amont().supprimer_serveur, srv["serveur_id"])
            except Exception:  # noqa: BLE001, S110 — best effort, une machine du lot ne bloque pas les autres
                pass


def ip_privee_mac(nom: str, image_id: str) -> str:
    return f"10.{hash(nom) % 250}.0.{hash(image_id) % 250 + 2}"


@executeur("vm.power.start")
@executeur("vm.power.stop")
@executeur("vm.power.reboot")
class ExecuteurVmPower(Executeur):
    _ACTION = {
        "vm.power.start": "demarrage",
        "vm.power.stop": "arret",
        "vm.power.reboot": "redemarrage",
    }
    _STATUT = {
        "vm.power.stop": "stopped",
        "vm.power.start": "running",
        "vm.power.reboot": "running",
    }

    async def etape(self, ctx: Contexte, travail: Travail, index: int, nom: str) -> str | None:
        if index == 0:
            vm = await depot.obtenir(ctx, travail.cible_id or "")
            sid = await serveur_id(ctx, vm.id, travail)
            await asyncio.to_thread(amont().action, sid, self._ACTION[travail.type])
        return None

    async def terminer(self, ctx: Contexte, travail: Travail) -> None:
        await depot.definir_statut(ctx, travail.cible_id or "", self._STATUT[travail.type])


@executeur("vm.resize")
class ExecuteurVmResize(Executeur):
    async def etape(self, ctx: Contexte, travail: Travail, index: int, nom: str) -> str | None:
        if index == 1:
            entre = travail.entree or {}
            vm = await depot.obtenir(ctx, travail.cible_id or "")
            gabarit = next(
                (
                    g
                    for g in await asyncio.to_thread(amont().gabarits)
                    if g["vcpu"] == entre.get("vcpu")
                    and g["ramGo"] == entre.get("ramGo")
                    and g["diskGo"] == entre.get("diskGo")
                ),
                None,
            )
            if gabarit:
                # Sans cet appel, le redimensionnement ne touchait que la fiche DB : la VM Nova
                # gardait son ancien gabarit (constaté en direct — `openstack server show`
                # inchangé après un `POST .../redimensionnement` pourtant rendu « done »).
                # `redimensionner` attend Nova jusqu'à `VERIFY_RESIZE` (jusqu'à 600 s, appel
                # openstacksdk synchrone) : sans `to_thread`, un redimensionnement lent gèle toute
                # la boucle asyncio — donc l'API entière, tous tenants confondus — pendant toute
                # l'attente (constaté en direct : plus aucune requête, même publique, ne répondait
                # pendant l'appel).
                sid = await serveur_id(ctx, vm.id, travail)
                await asyncio.to_thread(amont().redimensionner, sid, gabarit["id"])
                return f"Redimensionné vers le gabarit {gabarit['id']}"
            # Le routeur valide le triplet contre le catalogue avant de démarrer le travail ;
            # si on arrive ici avec un triplet sans gabarit (entrée directe du travail, ou
            # catalogue modifié entre-temps), échouer franchement plutôt que sauter l'appel
            # amont et rendre `done` avec la seule fiche DB mise à jour.
            raise erreurs.validation(
                "Aucun gabarit du catalogue ne correspond à ce vcpu/ramGo/diskGo.",
                champs={"gabarit": "Indiquez un gabarit existant du catalogue."},
            )
        return None

    async def terminer(self, ctx: Contexte, travail: Travail) -> None:
        entre = travail.entree or {}
        patch = {k: entre[k] for k in ("vcpu", "ramGo", "diskGo") if entre.get(k) is not None}
        if patch:
            await depot.modifier(ctx, travail.cible_id or "", patch)


@executeur("vm.migrate")
class ExecuteurVmMigrate(Executeur):
    async def terminer(self, ctx: Contexte, travail: Travail) -> None:
        await depot.definir_statut(ctx, travail.cible_id or "", "running")


@executeur("vm.snapshot")
class ExecuteurVmSnapshot(Executeur):
    """`amont().instantane()` crée un vrai instantané Glance et renvoie son id réel — jusqu'ici
    jeté au sol : `InstantaneVm` (contrat) n'a pas de champ pour un id amont, donc rien ne
    permettait à `vm.restore` de savoir quelle image restaurer (cf. mémoire
    `vm-snapshot-restore-fake-success-bug`). Persisté ici en secret (`image_id`), même motif
    que `serveur_id` sur la VM elle-même et `image_{p.id}` sur `web_backup`."""

    async def terminer(self, ctx: Contexte, travail: Travail) -> None:
        vm = await depot.obtenir(ctx, travail.cible_id or "")
        entre = travail.entree or {}
        nom = entre.get("nom") or "snapshot"
        sid = await serveur_id(ctx, vm.id, travail)
        image_id = await asyncio.to_thread(amont().instantane, sid, nom)
        inst = m.InstantaneVm(
            id=nouvel_id(),
            vmId=vm.id,
            nom=nom,
            cree=maintenant(),
            tailleGo=1.0,
            avecMemoire=bool(entre.get("avecMemoire")),
            description=entre.get("description"),
        )
        await instantane_depot.creer(ctx, inst, parent_id=vm.id, secrets={"image_id": image_id})


@executeur("vm.hardware")
class ExecuteurVmHardware(Executeur):
    async def terminer(self, ctx: Contexte, travail: Travail) -> None:
        await depot.definir_statut(ctx, travail.cible_id or "", "running")


@executeur("vm.restore")
class ExecuteurVmRestore(Executeur):
    """Jusqu'ici `terminer()` ne posait que le statut `running` : aucun override d'`etape()`,
    donc simulation par défaut de `Executeur` (docs/GUIDE-MODULE.md — « sans exécuteur [réel],
    le travail réussit en simulation ») — un `POST .../instantanes/{id}` rendait déjà 202
    `done` sans qu'aucun code ne touche le serveur amont (cf. mémoire
    `vm-snapshot-restore-fake-success-bug`). Restaure maintenant réellement depuis l'image
    Glance capturée par `vm.snapshot` (`Compute.restaurer()` = `rebuild_server`, partagé avec
    `web.backup.restore`)."""

    async def etape(self, ctx: Contexte, travail: Travail, index: int, nom: str) -> str | None:
        if index == 1:  # « Restaurer les volumes »
            entre = travail.entree or {}
            vm = await depot.obtenir(ctx, travail.cible_id or "")
            inst = await instantane_depot.obtenir(ctx, entre.get("instantaneId") or "")
            try:
                secrets = await instantane_depot.secrets(ctx, inst.id)
            except Exception:  # noqa: BLE001
                secrets = {}
            image_id = secrets.get("image_id")
            if not image_id:
                # Instantané antérieur à ce fix (créé avant que `image_id` soit persisté, ou
                # démo) : aucune image réelle à restaurer — échec explicite plutôt qu'un `done`
                # qui ne restaurerait rien (même garde-fou que `ExecuteurSauvegardeRestore`,
                # mais ici sans issue silencieuse : une VM n'a pas de granularité qui justifie
                # un no-op honnête, il n'y a qu'un instantané entier ou rien).
                raise erreurs.conflit(
                    "Cet instantané n'a pas d'image capturée : recréez-en un pour pouvoir le "
                    "restaurer.",
                    code="instantane_sans_image",
                )
            sid = await serveur_id(ctx, vm.id, travail)
            await asyncio.to_thread(amont().restaurer, sid, image_id)
            return f"Serveur restauré depuis l'image {image_id}"
        return None

    async def terminer(self, ctx: Contexte, travail: Travail) -> None:
        await depot.definir_statut(ctx, travail.cible_id or "", "running")


@executeur("vm.delete")
class ExecuteurVmDelete(Executeur):
    async def terminer(self, ctx: Contexte, travail: Travail) -> None:
        sid = await serveur_id(ctx, travail.cible_id or "", travail)
        if sid and sid != (travail.cible_id or ""):
            await asyncio.to_thread(amont().supprimer_serveur, sid)
        await depot.supprimer(ctx, travail.cible_id or "", logique=True)


@peupleur
async def demo(session, org: Organisation, admin: Utilisateur) -> None:
    espace_abj = m.EspaceCloud(
        id="espace-demo-abj",
        orgId=org.id,
        code="demo-abj",
        offerId="offre-standard",
        offreNom="Espace Standard",
        site="ABJ",
        cidr="10.20.0.0/16",
        quota=m.Quota(vcpu=8, ramGo=32, stockageTo=1),
        usage=m.Quota(vcpu=0, ramGo=0, stockageTo=0),
        projets=1,
        statut="active",
        createdAt=maintenant(),
        dnsInterne="dns.synelia.cloud",
    )
    session.add(
        Ressource(
            id=espace_abj.id,
            org_id=org.id,
            type="espace",
            nom=espace_abj.code,
            statut=espace_abj.statut,
            donnees=espace_abj.model_dump(mode="json"),
        )
    )
    vms = [
        m.Vm(
            id="vm-demo-web",
            espaceId=espace_abj.id,
            nom="web-01",
            os="ubuntu-24.04",
            vcpu=2,
            ramGo=4,
            diskGo=40,
            ips=[m.Ip(adresse="10.20.1.10", type="privee")],
            statut="running",
            hardware=m.MateielVirtuel(scsiControllers=1, nics=1, usb=False, secureBoot=False),
            site="ABJ",
        ),
        m.Vm(
            id="vm-demo-db",
            espaceId=espace_abj.id,
            nom="db-01",
            os="debian-12",
            vcpu=1,
            ramGo=2,
            diskGo=20,
            ips=[m.Ip(adresse="10.20.1.11", type="privee")],
            statut="running",
            hardware=m.MateielVirtuel(scsiControllers=1, nics=1, usb=False, secureBoot=False),
            site="ABJ",
        ),
    ]
    for vm in vms:
        session.add(
            Ressource(
                id=vm.id,
                org_id=org.id,
                type="vm",
                nom=vm.nom,
                statut=vm.statut,
                parent_id=espace_abj.id,
                donnees=vm.model_dump(mode="json"),
            )
        )
