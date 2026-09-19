"""Bibliothèque de modèles applicatifs : catalogue statique `modele_applicatif` scellé par slug."""

from __future__ import annotations

from synelia_contract import modeles as m
from synelia_kernel import argent

from synelia.depot import Depot
from synelia.deps.contexte import Contexte
from synelia.modules.facturation.metrologie import PRIX

depot = Depot(
    "modele_applicatif",
    m.ModeleApplicatif,
    libelle="Modèle applicatif",
    plateforme=True,
    champ_nom="nom",
    champ_statut="statut",
    champs_recherche=("nom", "solution", "phrase"),
)

HEURES_MOIS = 730
JOURS_MOIS = 30

SEMENCES: list[m.ModeleApplicatif] = [
    m.ModeleApplicatif(
        slug="wordpress",
        nom="WordPress",
        solution="WordPress",
        categorie="web",
        phrase="Site vitrine ou blog : le CMS le plus répandu au monde.",
        description="WordPress est déployé avec PHP-FPM et un serveur Nginx. La base MariaDB est provisionnée dans le même projet.",
        logoInitiales="WP",
        logoTeinte="#21759b",
        version="6.7.1",
        chart="wordpress",
        ressources=m.Ressources(cpu=1, ramMo=2048, diskGo=20),
        dependances=[m.Dependance(nom="MariaDB", type="base", detail="Base de données du CMS")],
        variables=[],
        volumes=[m.VolumeInline(chemin="/var/www/html", tailleGo=20, role="contenu")],
        ports=[m.Port1(conteneur=8080, protocole="http", role="http")],
        sousDomaine="site",
        configuration="wordpress",
        sauvegardeParDefaut=m.SauvegardeParDefaut(
            frequence="quotidienne", retentionJours=7, inclut=["contenu", "base"]
        ),
        prixIndicatif=0,
        certifie=True,
        populaire=True,
        horsPerimetre="Les extensions premium et les thèmes payants ne sont pas gérés.",
    ),
    m.ModeleApplicatif(
        slug="nextjs",
        nom="Next.js",
        solution="Next.js",
        categorie="developpement",
        phrase="Framework React pour applications web et rendu serveur.",
        description="Next.js est déployé en mode Node.js avec un build de production et un démarrage du serveur SSR.",
        logoInitiales="N",
        logoTeinte="#000000",
        version="15.1.3",
        chart="nextjs",
        ressources=m.Ressources(cpu=1, ramMo=1024, diskGo=10),
        dependances=[],
        variables=[],
        ports=[m.Port1(conteneur=3000, protocole="http", role="http")],
        sousDomaine="app",
        configuration="nextjs",
        sauvegardeParDefaut=m.SauvegardeParDefaut(
            frequence="quotidienne", retentionJours=7, inclut=["stockage"]
        ),
        prixIndicatif=0,
        certifie=False,
        populaire=True,
        horsPerimetre="Les fonctions serverless externe et le rendu statique externalisé ne sont pas repris.",
    ),
    m.ModeleApplicatif(
        slug="django",
        nom="Django",
        solution="Django",
        categorie="developpement",
        phrase="Framework web Python robuste avec admin intégrée.",
        description="Django est servi par Gunicorn derrière Nginx. PostgreSQL est provisionné dans le projet.",
        logoInitiales="Dj",
        logoTeinte="#092e20",
        version="5.1.4",
        chart="django",
        ressources=m.Ressources(cpu=1, ramMo=1024, diskGo=10),
        dependances=[
            m.Dependance(nom="PostgreSQL", type="base", detail="Base de données application")
        ],
        variables=[],
        volumes=[],
        ports=[m.Port1(conteneur=8000, protocole="http", role="http")],
        sousDomaine="api",
        configuration="django",
        sauvegardeParDefaut=m.SauvegardeParDefaut(
            frequence="quotidienne", retentionJours=7, inclut=["base"]
        ),
        prixIndicatif=0,
        certifie=False,
        populaire=False,
        horsPerimetre="Les tâches asynchrones Celery et les files Redis ne sont pas activées par défaut.",
    ),
    m.ModeleApplicatif(
        slug="laravel",
        nom="Laravel",
        solution="Laravel",
        categorie="developpement",
        phrase="Framework PHP élégant pour applications web et API.",
        description="Laravel tourne avec PHP-FPM et Nginx ; MySQL est provisionné dans le même projet.",
        logoInitiales="L",
        logoTeinte="#ff2d20",
        version="11.31.0",
        chart="laravel",
        ressources=m.Ressources(cpu=1, ramMo=1024, diskGo=10),
        dependances=[m.Dependance(nom="MySQL", type="base", detail="Base de données application")],
        variables=[],
        volumes=[],
        ports=[m.Port1(conteneur=9000, protocole="http", role="http")],
        sousDomaine="web",
        configuration="laravel",
        sauvegardeParDefaut=m.SauvegardeParDefaut(
            frequence="quotidienne", retentionJours=7, inclut=["base"]
        ),
        prixIndicatif=0,
        certifie=False,
        populaire=False,
        horsPerimetre="Queue asynchrone et cache Redis non gérés par défaut.",
    ),
    m.ModeleApplicatif(
        slug="odoo",
        nom="Odoo",
        solution="Odoo Community",
        categorie="metier",
        phrase="ERP et CRM : gestion commerciale, comptabilité et e-commerce.",
        description="Odoo Community est déployé en mode multi-databases, avec PostgreSQL et volume de données.",
        logoInitiales="O",
        logoTeinte="#714b67",
        version="17.0",
        chart="odoo",
        ressources=m.Ressources(cpu=2, ramMo=4096, diskGo=40),
        dependances=[
            m.Dependance(nom="PostgreSQL", type="base", detail="Base de données de l'ERP")
        ],
        variables=[],
        volumes=[m.VolumeInline(chemin="/var/lib/odoo", tailleGo=40, role="donnees")],
        ports=[m.Port1(conteneur=8069, protocole="http", role="http")],
        sousDomaine="erp",
        configuration="odoo",
        sauvegardeParDefaut=m.SauvegardeParDefaut(
            frequence="quotidienne", retentionJours=14, inclut=["base", "donnees"]
        ),
        prixIndicatif=0,
        certifie=True,
        populaire=True,
        horsPerimetre="Les modules payants et les éditions Entreprise restent hors du portail.",
    ),
    m.ModeleApplicatif(
        slug="n8n",
        nom="n8n",
        solution="n8n",
        categorie="automatisation",
        phrase="Workflow d'automatisation low-code avec plus de 400 intégrations.",
        description="n8n est déployé avec un volume pour les workflows et le chiffrement des identifiants.",
        logoInitiales="n",
        logoTeinte="#ea4b71",
        version="1.63.0",
        chart="n8n",
        ressources=m.Ressources(cpu=1, ramMo=1024, diskGo=10),
        dependances=[],
        variables=[],
        volumes=[m.VolumeInline(chemin="/home/node/.n8n", tailleGo=10, role="workflows")],
        ports=[m.Port1(conteneur=5678, protocole="http", role="http")],
        sousDomaine="automation",
        configuration="n8n",
        sauvegardeParDefaut=m.SauvegardeParDefaut(
            frequence="quotidienne", retentionJours=7, inclut=["workflows"]
        ),
        prixIndicatif=0,
        certifie=False,
        populaire=False,
        horsPerimetre="Les nœuds premium et les exécutions externes longue durée ne sont pas repris.",
    ),
    m.ModeleApplicatif(
        slug="metabase",
        nom="Metabase",
        solution="Metabase",
        categorie="donnees",
        phrase="Business intelligence : exploration des données et tableaux de bord.",
        description="Metabase se connecte aux bases du projet. Persistance dans une base interne PostgreSQL.",
        logoInitiales="Mb",
        logoTeinte="#509ee3",
        version="v0.52.2",
        chart="metabase",
        ressources=m.Ressources(cpu=1, ramMo=2048, diskGo=20),
        dependances=[m.Dependance(nom="PostgreSQL", type="base", detail="Métadonnées Metabase")],
        variables=[],
        volumes=[],
        ports=[m.Port1(conteneur=3000, protocole="http", role="http")],
        sousDomaine="bi",
        configuration="metabase",
        sauvegardeParDefaut=m.SauvegardeParDefaut(
            frequence="quotidienne", retentionJours=7, inclut=["base"]
        ),
        prixIndicatif=0,
        certifie=False,
        populaire=False,
        horsPerimetre="Les alertes par e-mail et les intégrations d'authentification externe restent hors périmètre.",
    ),
    m.ModeleApplicatif(
        slug="gitlab",
        nom="GitLab",
        solution="GitLab Community Edition",
        categorie="developpement",
        phrase="Référentiel Git, CI/CD et registre de conteneurs.",
        description="GitLab CE est déployé avec PostgreSQL, Redis et les volumes des dépôts.",
        logoInitiales="GL",
        logoTeinte="#fc6d26",
        version="17.7.0",
        chart="gitlab",
        ressources=m.Ressources(cpu=2, ramMo=4096, diskGo=50),
        dependances=[
            m.Dependance(nom="PostgreSQL", type="base", detail="Base de données principale"),
            m.Dependance(nom="Redis", type="cache", detail="Cache et files internes"),
        ],
        variables=[],
        volumes=[m.VolumeInline(chemin="/var/opt/gitlab", tailleGo=50, role="donnees")],
        ports=[m.Port1(conteneur=8080, protocole="http", role="http")],
        sousDomaine="git",
        configuration="gitlab",
        sauvegardeParDefaut=m.SauvegardeParDefaut(
            frequence="quotidienne", retentionJours=14, inclut=["depots", "base"]
        ),
        prixIndicatif=0,
        certifie=True,
        populaire=False,
        horsPerimetre="Les runners externes et la haute disponibilité multi-nœuds ne sont pas gérés.",
    ),
]

SEMENCE_PAR_SLUG = {s.slug: s for s in SEMENCES}


async def _semer(ctx: Contexte) -> None:
    if await depot.compter(ctx) > 0:
        return
    for modele in SEMENCES:
        await depot.creer(ctx, modele, id_=modele.slug)


async def obtenir(ctx: Contexte, slug: str) -> m.ModeleApplicatif:
    await _semer(ctx)
    if slug not in SEMENCE_PAR_SLUG:
        modele = await depot.trouver(ctx, slug)
        if modele is None:
            raise __import__("synelia_kernel.erreurs", fromlist=["introuvable"]).introuvable(
                "Modèle applicatif", slug
            )
        return modele
    return SEMENCE_PAR_SLUG[slug]


def _ht(cpu: float, ram_go: float, disk_go: float, sieges: int) -> tuple[list[m.Ligne], int, float]:
    lignes: list[m.Ligne] = []
    cpu_mensuel = int(cpu * PRIX["vcpu_heure"] * HEURES_MOIS)
    lignes.append(
        m.Ligne(
            libelle="Processeur",
            quantite=cpu,
            unite="vCPU",
            prixUnitaire=int(PRIX["vcpu_heure"] * HEURES_MOIS),
            total=cpu_mensuel,
        )
    )
    ram_mensuel = int(ram_go * PRIX["ram_go_heure"] * HEURES_MOIS)
    lignes.append(
        m.Ligne(
            libelle="Mémoire vive",
            quantite=round(ram_go, 1),
            unite="Go",
            prixUnitaire=int(PRIX["ram_go_heure"] * HEURES_MOIS),
            total=ram_mensuel,
        )
    )
    disk_mensuel = int(disk_go * PRIX["stockage_to_jour"] * JOURS_MOIS)
    lignes.append(
        m.Ligne(
            libelle="Stockage",
            quantite=round(disk_go, 1),
            unite="Go",
            prixUnitaire=int(PRIX["stockage_to_jour"] * JOURS_MOIS),
            total=disk_mensuel,
        )
    )
    total_ht = cpu_mensuel + ram_mensuel + disk_mensuel
    total_horaire = cpu * PRIX["vcpu_heure"] + ram_go * PRIX["ram_go_heure"]
    if sieges and sieges > 0:
        siege_prix = 7500
        siege_total = siege_prix * sieges
        lignes.append(
            m.Ligne(
                libelle="Sièges",
                quantite=sieges,
                unite="sièges",
                prixUnitaire=siege_prix,
                total=siege_total,
            )
        )
        total_ht += siege_total
    return lignes, total_ht, total_horaire


def estimer(
    modele: m.ModeleApplicatif, ressources: m.Ressources3 | None, sieges: int | None
) -> m.EstimationCout:
    res = ressources or m.Ressources3()
    cpu = res.cpu if res.cpu is not None else modele.ressources.cpu
    ram_go = (res.ramMo if res.ramMo is not None else modele.ressources.ramMo) / 1024
    disk_go = res.diskGo if res.diskGo is not None else modele.ressources.diskGo
    lignes, total_ht, total_horaire = _ht(cpu, ram_go, disk_go, sieges)
    tva_montant = argent.tva(total_ht)
    total_mensuel = total_ht + tva_montant
    return m.EstimationCout(
        lignes=lignes,
        totalMensuel=total_mensuel,
        totalHoraire=round(total_horaire, 2),
        devise="XOF",
        engagement="aucun",
        remisePct=0,
        avertissements=["Hors egress réseau et licences tierces."],
    )
