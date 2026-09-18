from __future__ import annotations

import asyncio

from synelia_contract import modeles as m
from synelia_db.modeles import Organisation, Ressource, Travail, Utilisateur
from synelia_kernel import erreurs
from synelia_openstack import fournisseur
from synelia_openstack.identite import IdentiteOpenStack, IdentiteSimule
from synelia_openstack.network import NetworkOpenStack, NetworkSimule

from synelia.demo import peupleur
from synelia.depot import Depot
from synelia.deps.contexte import Contexte
from synelia.travaux import Executeur, executeur

depot_reseau = Depot("reseau", m.Reseau, libelle="Réseau")
depot_ip = Depot(
    "ip_publique", m.IpPublique, libelle="IP publique", champs_recherche=("adresse", "ptr")
)
depot_groupe = Depot("groupe_securite", m.GroupeSecurite, libelle="Groupe de sécurité")
depot_lb = Depot("load_balancer", m.LoadBalancer, libelle="Load balancer")
depot_vpn = Depot(
    "vpn_tunnel", m.TunnelVpn, libelle="Tunnel VPN", champs_recherche=("nom", "passerelleDistante")
)


def amont() -> NetworkSimule:
    return fournisseur(NetworkSimule, NetworkOpenStack)


def amont_identite() -> IdentiteSimule:
    """Réseaux secondaires et IP flottantes vivent dans le projet de l'Espace Cloud parent :
    même amont (Keystone/Neutron scopé projet) que `espaces.service.amont()`."""
    return fournisseur(IdentiteSimule, IdentiteOpenStack)


async def prochaine_ip(ctx: Contexte, espace_id: str) -> str:
    ips = await depot_ip.tous(ctx, filtre=lambda ip: ip.espaceId == espace_id)
    n = len(ips)
    octet3 = (n // 250) + 1
    octet4 = (n % 250) + 2
    return f"196.201.{octet3}.{octet4}"


async def _projet_id(ctx: Contexte, espace_id: str) -> str | None:
    from synelia.modules.espaces.service import depot as depot_espaces

    secrets_espace = await depot_espaces.secrets(ctx, espace_id)
    return secrets_espace.get("projet_id")


async def creer_reseau_amont(ctx: Contexte, espace_id: str, nom: str, cidr: str) -> dict[str, str]:
    """Crée le réseau/sous-réseau amont (sans routeur : c'est un réseau interne de plus dans un
    projet qui en a déjà un) et renvoie les identifiants à poser en secrets sur la ressource."""
    projet_id = await _projet_id(ctx, espace_id)
    r = await asyncio.to_thread(amont_identite().creer_reseau_secondaire, projet_id, nom, cidr)
    return {"reseau_id": r["reseau_id"], "sous_reseau_id": r.get("sous_reseau_id") or ""}


async def supprimer_reseau_amont(ctx: Contexte, reseau_id_local: str) -> None:
    secrets = await depot_reseau.secrets(ctx, reseau_id_local)
    rid = secrets.get("reseau_id")
    if rid:
        await asyncio.to_thread(amont_identite().supprimer_reseau_secondaire, rid)


async def reserver_ip_amont(ctx: Contexte, espace_id: str) -> dict[str, str]:
    """Alloue une IP flottante amont ; le simulé ne renvoie pas d'adresse plausible-mais-stable
    (pas d'accès à la base), on retombe alors sur l'allocation séquentielle locale."""
    projet_id = await _projet_id(ctx, espace_id)
    fip = await asyncio.to_thread(amont_identite().creer_ip_flottante, projet_id)
    adresse = fip.get("adresse") or await prochaine_ip(ctx, espace_id)
    return {"id": fip["id"], "adresse": adresse}


async def liberer_ip_amont(ctx: Contexte, ip_id_local: str) -> None:
    secrets = await depot_ip.secrets(ctx, ip_id_local)
    fid = secrets.get("ip_flottante_id")
    if fid:
        await asyncio.to_thread(amont_identite().supprimer_ip_flottante, fid)


async def resoudre_cible_attachement_ip(ctx: Contexte, cible_id: str) -> tuple[str, str]:
    """Détermine le type réel d'une cible d'attachement d'IP flottante — le contrat
    (`IpsIpIdAttachementPutRequest.cibleId`) documente « VM, load balancer ou passerelle »
    mais ne porte aucun discriminant explicite : on résout donc `cibleId` en essayant
    chaque type de ressource à son tour. Sans cette résolution, toute cible non-VM (un load
    balancer, pourtant documenté comme cible valide) échouait avec un 404 « Vm ... introuvable »
    trompeur, quelle que soit la cible réelle (constaté en direct)."""
    vm = await Depot("vm", m.Vm).trouver(ctx, cible_id)
    if vm is not None:
        return "vm", vm.nom
    lb = await depot_lb.trouver(ctx, cible_id)
    if lb is not None:
        return "load_balancer", lb.nom
    from synelia.modules.espaces.service import depot as depot_espaces

    espace = await depot_espaces.trouver(ctx, cible_id)
    if espace is not None:
        # La « passerelle » d'un Espace est son routeur Neutron (créé avec sa sortie externe
        # à la création de l'Espace, cf. `IdentiteOpenStack.creer_reseau`) — pas une ressource
        # distincte que le client pourrait référencer autrement que par l'Espace lui-même.
        # Neutron refuse toutefois d'associer une IP flottante au port de sortie externe d'un
        # routeur (constaté en direct sur le lab réel : `openstack floating ip set --port
        # <port-passerelle>` échoue avec « External network ... is not reachable from subnet
        # ... Therefore, cannot associate Port ... with a Floating IP » — la passerelle a déjà
        # sa propre sortie externe et ne peut pas en recevoir une seconde par ce mécanisme).
        raise erreurs.non_porte(
            "La passerelle d'un Espace dispose déjà de sa propre sortie externe : Neutron ne "
            "permet pas d'y associer une IP flottante supplémentaire."
        )
    raise erreurs.introuvable("VM, load balancer ou passerelle", cible_id)


async def associer_ip_amont(
    ctx: Contexte, ip_id_local: str, cible_id: str, cible_type: str
) -> str | None:
    """Associe réellement l'IP flottante amont à sa cible — sans cet appel l'attachement ne
    vivait que côté DB (constaté en direct : `openstack floating ip show` restait sans port
    associé après un `PUT .../attachement` réussi sur une VM ; un load balancer, lui,
    échouait carrément avec un 404 avant même d'atteindre ce point)."""
    secrets = await depot_ip.secrets(ctx, ip_id_local)
    fid = secrets.get("ip_flottante_id")
    if not fid:
        return None
    if cible_type == "load_balancer":
        secrets_lb = await depot_lb.secrets(ctx, cible_id)
        lb_id = secrets_lb.get("octavia_lb_id")
        if not lb_id:
            return None
        return await asyncio.to_thread(amont().associer_ip_flottante_lb_existante, fid, lb_id)
    from synelia.modules.vms.service import serveur_id

    sid = await serveur_id(ctx, cible_id)
    return await asyncio.to_thread(amont_identite().associer_ip_flottante, fid, sid)


async def dissocier_ip_amont(ctx: Contexte, ip_id_local: str) -> None:
    secrets = await depot_ip.secrets(ctx, ip_id_local)
    fid = secrets.get("ip_flottante_id")
    if fid:
        await asyncio.to_thread(amont_identite().dissocier_ip_flottante, fid)


# ── Réconciliation à la lecture ──────────────────────────────────────────
# Même motif que `vms.service.reconcilier_statut` / `kubernetes` / `web_hebergement` :
# les GET détail relisent l'amont réel (Neutron/Octavia, appels synchrones déchargés
# via `asyncio.to_thread`) au lieu de servir la fiche DB figée — une ressource
# supprimée hors bande (nettoyage manuel du lab) répond 404 au lieu de 200 fantôme,
# et les champs dynamiques (VIP du LB, attachement d'une IP) reflètent l'amont.
# En simulation, `details_*` rend `None` (« inconnu », pas « perdu ») : on renvoie
# alors la ligne DB telle quelle, comme avant.


async def reconcilier_reseau(ctx: Contexte, reseau: m.Reseau) -> m.Reseau:
    """404 si Neutron ne connaît plus ce réseau ; sinon la ligne DB (aucun champ
    dynamique exposé au contrat pour un réseau)."""
    if not isinstance(amont_identite(), IdentiteOpenStack):
        return reseau
    secrets = await depot_reseau.secrets(ctx, reseau.id)
    rid = secrets.get("reseau_id")
    if not rid:
        return reseau
    details = await asyncio.to_thread(amont_identite().details_reseau_secondaire, rid)
    if details is None:
        raise erreurs.introuvable("Réseau", reseau.nom)
    return reseau


async def reconcilier_groupe(ctx: Contexte, groupe: m.GroupeSecurite) -> m.GroupeSecurite:
    """404 si Neutron ne connaît plus ce groupe. Les règles restent pilotées par
    l'application (chaque mutation les pose déjà côté Neutron de façon synchrone)."""
    if not isinstance(amont(), NetworkOpenStack):
        return groupe
    secrets = await depot_groupe.secrets(ctx, groupe.id)
    gid = secrets.get("groupe_id")
    if not gid:
        return groupe
    details = await asyncio.to_thread(amont().details_groupe, gid)
    if details is None:
        raise erreurs.introuvable("Groupe de sécurité", groupe.nom)
    return groupe


async def reconcilier_ip(ctx: Contexte, ip: m.IpPublique) -> m.IpPublique:
    """404 si Neutron ne connaît plus cette IP ; sinon, un détachement hors bande
    (port libéré côté Neutron alors que la fiche dit `attachedTo`) est reflété et
    persisté — l'API ne prétend plus une attache qui n'existe plus."""
    if not isinstance(amont_identite(), IdentiteOpenStack):
        return ip
    secrets = await depot_ip.secrets(ctx, ip.id)
    fid = secrets.get("ip_flottante_id")
    if not fid:
        return ip
    details = await asyncio.to_thread(amont_identite().details_ip_flottante, fid)
    if details is None:
        raise erreurs.introuvable("IP publique", ip.adresse)
    if details.get("port_id") is None and ip.attachedTo is not None:
        await depot_ip.modifier(ctx, ip.id, {"attachedTo": None, "attachedLabel": None})
        return await depot_ip.obtenir(ctx, ip.id)
    return ip


async def reconcilier_lb(ctx: Contexte, lb: m.LoadBalancer) -> m.LoadBalancer:
    """404 si Octavia ne connaît plus ce LB ; sinon la VIP réelle est reflétée et
    persistée quand elle a changé (même en `ERROR`/`OFFLINE` — état réel courant
    du lab — c'est l'amont qui fait foi, pas la fiche posée à la création)."""
    if not isinstance(amont(), NetworkOpenStack):
        return lb
    secrets = await depot_lb.secrets(ctx, lb.id)
    oid = secrets.get("octavia_lb_id")
    if not oid:
        return lb
    details = await asyncio.to_thread(amont().details_lb, oid)
    if details is None:
        raise erreurs.introuvable("Load balancer", lb.nom)
    if details.get("vip") and details["vip"] != lb.vip:
        await depot_lb.modifier(ctx, lb.id, {"vip": details["vip"]})
        return await depot_lb.obtenir(ctx, lb.id)
    return lb


def _regle_neutron(regle: m.RegleSecurite) -> dict[str, object]:
    """Traduit une `RegleSecurite` applicative en attributs Neutron. Sans cette traduction (et
    sans qu'aucune règle ne soit jamais posée côté amont, cf. `ajouter_regle_amont`), un groupe
    de sécurité créé par l'API n'avait strictement aucun effet réel : il ne vivait qu'en base,
    n'était jamais attaché à un port Neutron ni doté de la moindre règle (constaté en direct —
    un port bloqué par une règle « deny » restait joignable après création de la règle)."""
    port_min = port_max = None
    protocole = None if regle.protocole == "any" else regle.protocole
    if regle.ports and protocole in ("tcp", "udp"):
        if "-" in regle.ports:
            lo, hi = regle.ports.split("-", 1)
            port_min, port_max = int(lo), int(hi)
        else:
            port_min = port_max = int(regle.ports)
    attrs: dict[str, object] = {
        "direction": "ingress" if regle.direction == "in" else "egress",
        "protocol": protocole,
        "port_range_min": port_min,
        "port_range_max": port_max,
        "ethertype": "IPv4",
    }
    try:
        import ipaddress

        ipaddress.ip_network(regle.cible, strict=False)
        attrs["remote_ip_prefix"] = regle.cible
    except ValueError:
        # Pas un CIDR : `cible` est l'identifiant (local) d'un autre groupe de sécurité —
        # on ne peut le référencer côté amont qu'en résolvant son identifiant Neutron réel.
        attrs["remote_group_id"] = regle.cible
    return attrs


async def creer_groupe_amont(
    ctx: Contexte, espace_id: str, nom: str, description: str | None
) -> str:
    projet_id = await _projet_id(ctx, espace_id)
    return await asyncio.to_thread(amont().creer_groupe, nom, description, projet_id)


async def supprimer_groupe_amont(ctx: Contexte, groupe_id_local: str) -> None:
    secrets = await depot_groupe.secrets(ctx, groupe_id_local)
    gid = secrets.get("groupe_id")
    if gid:
        await asyncio.to_thread(amont().supprimer_groupe, gid)


async def ajouter_regle_amont(ctx: Contexte, groupe_id_local: str, regle: m.RegleSecurite) -> None:
    secrets = await depot_groupe.secrets(ctx, groupe_id_local)
    gid = secrets.get("groupe_id")
    if not gid:
        return
    rid = await asyncio.to_thread(amont().ajouter_regle_securite, gid, **_regle_neutron(regle))
    await depot_groupe.definir_secrets(ctx, groupe_id_local, {f"regle_{regle.id}": rid})


async def supprimer_regle_amont(ctx: Contexte, groupe_id_local: str, regle_id: str) -> None:
    secrets = await depot_groupe.secrets(ctx, groupe_id_local)
    rid = secrets.get(f"regle_{regle_id}")
    if rid:
        await asyncio.to_thread(amont().supprimer_regle_securite, rid)


async def attacher_groupe_amont(ctx: Contexte, groupe_id_local: str, cibles: list[str]) -> None:
    """Reflète l'ensemble des cibles demandées sur le port Neutron de chaque VM concernée :
    attache le groupe aux VM nouvellement listées, le détache de celles retirées."""
    from synelia.modules.vms.service import serveur_id

    secrets = await depot_groupe.secrets(ctx, groupe_id_local)
    gid = secrets.get("groupe_id")
    if not gid:
        return
    anciennes = {c for c in secrets if c.startswith("attache_")}
    anciens_ids = {c.removeprefix("attache_") for c in anciennes}
    nouveaux_ids = set(cibles)
    for retire in anciens_ids - nouveaux_ids:
        sid = await serveur_id(ctx, retire)
        await asyncio.to_thread(amont().detacher_groupe_serveur, gid, sid)
    nouveaux_secrets: dict[str, str] = {}
    for ajoute in nouveaux_ids - anciens_ids:
        sid = await serveur_id(ctx, ajoute)
        await asyncio.to_thread(amont().attacher_groupe_serveur, gid, sid)
        nouveaux_secrets[f"attache_{ajoute}"] = "1"
    if nouveaux_secrets:
        await depot_groupe.definir_secrets(ctx, groupe_id_local, nouveaux_secrets)


async def supprimer_lb_amont(ctx: Contexte, lb_id_local: str) -> None:
    secrets = await depot_lb.secrets(ctx, lb_id_local)
    oid = secrets.get("octavia_lb_id")
    if oid:
        # `cascade=True` (côté NetworkOpenStack.supprimer_load_balancer) fait tomber avec lui
        # listeners, pools, membres et moniteur de santé amont : pas besoin de les défaire un
        # par un ici.
        await asyncio.to_thread(amont().supprimer_load_balancer, oid)
    fip_id = secrets.get("octavia_fip_id")
    if fip_id:
        # L'IP flottante d'un LB `exposure=public` n'est pas défaite par la suppression
        # cascade du load balancer (ressource Neutron indépendante) : sans cet appel elle
        # fuit à chaque suppression (constaté en direct : IP flottante encore allouée au
        # projet, `port_id` à `null`, après suppression du LB public qui la portait).
        await asyncio.to_thread(amont().supprimer_ip_flottante_lb, fip_id)


def _ip_privee(vm: m.Vm) -> str | None:
    return next((ip.adresse for ip in vm.ips if ip.type == "privee"), None)


async def synchroniser_pool_amont(
    ctx: Contexte, lb: m.LoadBalancer, cibles: list[m.Cible2]
) -> list[m.PoolItem]:
    """Reflète la liste de cibles demandée sur le pool Octavia du load balancer (ajoute/retire
    de vrais membres) : sans ça, poser une cible via `PUT /pool` ne fait que ranger une ligne
    en base, le trafic réel ne suit jamais (constaté en testant en direct : un membre "ok" en
    base ne recevait jamais de requête)."""
    secrets = await depot_lb.secrets(ctx, lb.id)
    pool_id = secrets.get("octavia_pool_id")
    octavia_lb_id = secrets.get("octavia_lb_id")
    port = (lb.listeners[0].port if lb.listeners else None) or 80

    anciens_ids = {p.targetId for p in lb.pool}
    nouveaux_ids = {c.targetId for c in cibles}

    nouveaux_secrets: dict[str, str] = {}
    if pool_id:
        for retire in anciens_ids - nouveaux_ids:
            membre_id = secrets.get(f"membre_{retire}")
            if membre_id:
                await asyncio.to_thread(
                    amont().supprimer_membre, pool_id, membre_id, loadbalancer_id=octavia_lb_id
                )
                # Efface la trace du membre défait : sinon une cible retirée puis reposée
                # plus tard serait prise pour "déjà membre" (secret encore présent) et ne
                # recréerait jamais de membre Octavia réel.
                nouveaux_secrets[f"membre_{retire}"] = ""

    items: list[m.PoolItem] = []
    for c in cibles:
        vm = await Depot("vm", m.Vm).trouver(ctx, c.targetId)
        label = vm.nom if vm else c.targetId
        membre_id = secrets.get(f"membre_{c.targetId}")
        if pool_id and not membre_id and vm is not None:
            adresse = _ip_privee(vm)
            if adresse:
                membre = await asyncio.to_thread(
                    amont().ajouter_membre,
                    pool_id=pool_id,
                    adresse=adresse,
                    port=port,
                    loadbalancer_id=octavia_lb_id,
                    poids=c.poids or 1,
                )
                nouveaux_secrets[f"membre_{c.targetId}"] = membre["id"]
                # Le port du membre vit dans le groupe de sécurité `default` du projet, qui
                # n'autorise que le trafic intra-groupe : l'amphore Octavia (autre groupe)
                # y reste bloquée sans cette règle (constaté en direct : membre "ONLINE" côté
                # Octavia, mais `curl` sur la VIP renvoyait 503 tant qu'elle manquait).
                from synelia.modules.vms.service import serveur_id

                sid = await serveur_id(ctx, c.targetId)
                await asyncio.to_thread(amont().assurer_regle_port, sid, port)
        items.append(
            m.PoolItem(
                targetId=c.targetId,
                targetLabel=label,
                poids=c.poids or 1,
                sante="drain" if c.drain else "ok",
            )
        )
    if nouveaux_secrets:
        await depot_lb.definir_secrets(ctx, lb.id, nouveaux_secrets)
    return items


def sante_defaut() -> m.HealthCheck:
    return m.HealthCheck(
        protocole="http", chemin="/health", codeAttendu=200, intervalleS=30, seuilKo=3, seuilOk=2
    )


def metriques_vides() -> m.Metriques:
    return m.Metriques(rps=0, p50=0, p95=0, p99=0, taux4xx=0, taux5xx=0, connexions=0)


@executeur("lb.create")
class ExecuteurLbCreate(Executeur):
    compensable = True

    async def etape(self, ctx: Contexte, travail: Travail, index: int, nom: str) -> str | None:
        if index == 1:
            lb = await depot_lb.obtenir(ctx, travail.cible_id or "")
            entree = travail.entree or {}
            from synelia.modules.espaces.service import depot as depot_espaces

            secrets_espace = await depot_espaces.secrets(ctx, lb.espaceId)
            # Octavia (création d'amphore incluse) : appel openstacksdk synchrone, souvent
            # lent — déchargé via `asyncio.to_thread` pour ne pas geler la boucle asyncio.
            res = await asyncio.to_thread(
                amont().creer_load_balancer,
                projet_id=secrets_espace.get("projet_id"),
                nom=lb.nom,
                reseau_id=secrets_espace.get("reseau_id"),
                layer=lb.layer,
                exposure=lb.exposure,
                listeners=entree.get("listeners"),
            )
            secrets_lb = {"octavia_lb_id": res["id"]}
            if res.get("listener_id"):
                secrets_lb["octavia_listener_id"] = res["listener_id"]
            if res.get("pool_id"):
                secrets_lb["octavia_pool_id"] = res["pool_id"]
            if res.get("fip_id"):
                secrets_lb["octavia_fip_id"] = res["fip_id"]
            await depot_lb.definir_secrets(ctx, lb.id, secrets_lb)
            if res.get("pool_id"):
                # Moniteur de santé par défaut sur le pool par défaut du listener : c'est lui
                # qui permet à Octavia de retirer réellement un membre KO de la rotation
                # (constaté en testant en direct : sans moniteur, le pool continue d'envoyer
                # du trafic à un membre arrêté).
                hc = sante_defaut()
                mon = await asyncio.to_thread(
                    amont().creer_moniteur_sante,
                    pool_id=res["pool_id"],
                    type_=hc.protocole.upper(),
                    delay=hc.intervalleS,
                    timeout=max(1, hc.intervalleS - 1),
                    max_retries=hc.seuilKo,
                    url_path=hc.chemin,
                    expected_codes=str(hc.codeAttendu) if hc.codeAttendu else None,
                    loadbalancer_id=res["id"],
                )
                await depot_lb.definir_secrets(ctx, lb.id, {"octavia_moniteur_id": mon["id"]})
                # Si des cibles ont été fournies à la création, les ajouter au pool maintenant
                # que le pool Octavia existe (sinon elles seraient ignorées).
                cibles_entree = entree.get("cibles")
                if cibles_entree:
                    # Convertir les Cible en Cible2 (ajouter le champ drain par défaut)
                    cibles = [
                        m.Cible2(
                            targetId=c.get("targetId"),
                            poids=c.get("poids"),
                            drain=False,
                        )
                        for c in cibles_entree
                    ]
                    # Actualiser le LB en base avec le pool synchronisé
                    lb = await depot_lb.obtenir(ctx, travail.cible_id or "")
                    pool = await synchroniser_pool_amont(ctx, lb, cibles)
                    await depot_lb.modifier(ctx, lb.id, {"pool": [p.model_dump() for p in pool]})
            c = dict(travail.contexte)
            c["vip"] = res["vip"]
            travail.contexte = c
            return f"Load balancer amont {res['id']} créé ({res['statut']})"
        return None

    async def terminer(self, ctx: Contexte, travail: Travail) -> None:
        lb = await depot_lb.obtenir(ctx, travail.cible_id or "")
        vip = travail.contexte.get("vip") or await asyncio.to_thread(amont().allouer_vip)
        await depot_lb.modifier(ctx, lb.id, {"vip": vip})

    async def compenser(self, ctx: Contexte, travail: Travail, index_echoue: int) -> None:
        await supprimer_lb_amont(ctx, travail.cible_id or "")


@peupleur
async def demo(session, org: Organisation, admin: Utilisateur) -> None:
    espace_id = "espace-demo-abj"
    ressources = [
        m.Reseau(
            id="reseau-demo-prod",
            espaceId=espace_id,
            nom="prod-net",
            cidr="10.50.0.0/16",
            dnsInterne=True,
            workloads=2,
            vlan=101,
        ),
        m.Reseau(
            id="reseau-demo-app",
            espaceId=espace_id,
            nom="app-net",
            cidr="10.51.0.0/16",
            dnsInterne=True,
            workloads=0,
            vlan=102,
        ),
        m.IpPublique(
            id="ip-demo-1",
            espaceId=espace_id,
            adresse="196.201.1.10",
            ptr="api.example.com",
            attachedTo="vm-demo-web",
            attachedLabel="web-01",
            antiDdos=False,
        ),
        m.IpPublique(
            id="ip-demo-2",
            espaceId=espace_id,
            adresse="196.201.1.11",
            ptr="db.example.com",
            attachedTo=None,
            attachedLabel=None,
            antiDdos=True,
        ),
    ]
    for res in ressources:
        session.add(
            Ressource(
                id=res.id,
                org_id=org.id,
                type={"Reseau": "reseau", "IpPublique": "ip_publique"}[type(res).__name__],
                nom=getattr(res, "nom", getattr(res, "adresse", None)),
                donnees=res.model_dump(mode="json"),
            )
        )
