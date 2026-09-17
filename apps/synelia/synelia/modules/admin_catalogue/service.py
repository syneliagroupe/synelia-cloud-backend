"""Catalogue plateforme réel : amorçage des Offres de démarrage.

Indépendant du jeu de données de démo (`SYNELIA_SEED_DEMO`, voir `synelia.demo`) : ce
catalogue doit exister sur un environnement réel (dev01) même quand la démo est coupée.
Appelé à chaque démarrage depuis `synelia.amorcage.amorcer()` — idempotent (une Offre déjà
présente n'est jamais réécrite, un opérateur a pu la modifier depuis `/admin/catalogue`).

Chaque Offre correspond à une ressource réellement provisionnable cette session :
- `espace_cloud` → quota d'un `EspaceCloud` (vCPU/RAM/stockage réels, VM OpenStack).
- `web` → palier d'un `Hebergement` (`web_hebergement.service`, VM Nova dédiée).
- `stack` → palier d'une `BaseManagee` (`bases.service`, VM Nova + moteur Docker)."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from synelia_db.modeles import Ressource

CATALOGUE_REEL: list[dict[str, Any]] = [
    {
        "id": "offre-espace-starter",
        "code": "espace-starter",
        "nom": "Espace Starter",
        "categorie": "espace_cloud",
        "specs": "2 vCPU · 8 Go · 100 Go",
        "caracteristiques": ["IPv4 publique", "Sauvegarde quotidienne"],
        "prix": 20000,
        "statut": "publiee",
        "souscriptionsActives": 0,
        "sla": "99.9",
    },
    {
        "id": "offre-espace-pro",
        "code": "espace-pro",
        "nom": "Espace Pro",
        "categorie": "espace_cloud",
        "specs": "4 vCPU · 16 Go · 500 Go",
        "caracteristiques": ["IPv4 publique", "Sauvegarde quotidienne", "Load balancer inclus"],
        "prix": 45000,
        "populaire": True,
        "statut": "publiee",
        "souscriptionsActives": 0,
        "sla": "99.9",
    },
    {
        "id": "offre-espace-business",
        "code": "espace-business",
        "nom": "Espace Business",
        "categorie": "espace_cloud",
        "specs": "8 vCPU · 32 Go · 1 To",
        "caracteristiques": ["IPv4 publique", "Sauvegarde horaire", "Load balancer inclus"],
        "prix": 90000,
        "statut": "publiee",
        "souscriptionsActives": 0,
        "sla": "99.95",
    },
    {
        "id": "offre-web-starter",
        "code": "web-starter",
        "nom": "Hébergement Starter",
        "categorie": "web",
        "specs": "1 vCPU · 2 Go · 40 Go — PHP, MariaDB",
        "caracteristiques": ["FTP/SFTP", "Sauvegarde quotidienne"],
        "prix": 8000,
        "statut": "publiee",
        "souscriptionsActives": 0,
    },
    {
        "id": "offre-web-pro",
        "code": "web-pro",
        "nom": "Hébergement Pro",
        "categorie": "web",
        "specs": "2 vCPU · 4 Go · 80 Go — PHP, MariaDB",
        "caracteristiques": ["FTP/SFTP", "Sauvegarde quotidienne", "SSL gratuit"],
        "prix": 18000,
        "populaire": True,
        "statut": "publiee",
        "souscriptionsActives": 0,
    },
    {
        "id": "offre-web-business",
        "code": "web-business",
        "nom": "Hébergement Business",
        "categorie": "web",
        "specs": "4 vCPU · 8 Go · 160 Go — PHP, MariaDB",
        "caracteristiques": ["FTP/SFTP", "Sauvegarde horaire", "SSL gratuit"],
        "prix": 35000,
        "statut": "publiee",
        "souscriptionsActives": 0,
    },
    {
        "id": "offre-base-s1",
        "code": "base-s1",
        "nom": "Base managée S",
        "categorie": "stack",
        "specs": "PostgreSQL/MySQL/MariaDB/MongoDB/Redis — palier s1",
        "caracteristiques": ["Sauvegarde quotidienne"],
        "prix": 15000,
        "statut": "publiee",
        "souscriptionsActives": 0,
    },
    {
        "id": "offre-base-m1",
        "code": "base-m1",
        "nom": "Base managée M",
        "categorie": "stack",
        "specs": "PostgreSQL/MySQL/MariaDB/MongoDB/Redis — palier m1",
        "caracteristiques": ["Sauvegarde quotidienne", "Réplication"],
        "prix": 35000,
        "populaire": True,
        "statut": "publiee",
        "souscriptionsActives": 0,
    },
    {
        "id": "offre-base-l1",
        "code": "base-l1",
        "nom": "Base managée L",
        "categorie": "stack",
        "specs": "PostgreSQL/MySQL/MariaDB/MongoDB/Redis — palier l1",
        "caracteristiques": ["Sauvegarde quotidienne", "Réplication", "PITR"],
        "prix": 70000,
        "statut": "publiee",
        "souscriptionsActives": 0,
    },
]


async def semer_catalogue_reel(session: AsyncSession) -> None:
    for o in CATALOGUE_REEL:
        if await session.get(Ressource, o["id"]) is not None:
            continue
        session.add(
            Ressource(
                id=o["id"],
                org_id=None,
                type="offre",
                nom=o["code"],
                statut=o["statut"],
                donnees=o,
            )
        )
    await session.flush()
