# Runbook — Vercel + GitHub (préversions par branche)

## 1. Pousser le dépôt
Depuis dev01 la clé `~/.ssh/synelia_deploy_ed25519.pub` doit être ajoutée au dépôt GitHub
(`Settings → Deploy keys`, **Allow write access**) ou au compte. Puis :
```bash
env -u SSH_AUTH_SOCK git push -u origin main
```

## 2. Option A (recommandée) — intégration Git native
Vercel → *Add New Project* → importer `jvkassi/synelia-cloud-backend` → Framework « Other », racine `/`.
Variables : `SYNELIA_SECRET` (long aléatoire), `SYNELIA_SEED_ADMIN_MOT_DE_PASSE`, `SYNELIA_CORS_ORIGINES`
(`["https://synelia.cloud","http://localhost:3000"]`), et si Postgres : `SYNELIA_DATABASE_URL=postgresql+asyncpg://…`.
Chaque branche/PR obtient une URL de préversion ; `main` déploie la production.

## 3. Option B — GitHub Actions (`.github/workflows/vercel.yml`)
Secrets du dépôt : `VERCEL_TOKEN` (vercel.com/account/tokens), `VERCEL_ORG_ID`, `VERCEL_PROJECT_ID`
(`vercel link` puis `.vercel/project.json`). Le workflow déploie en préversion hors `main`, en production sur `main`.

## 4. Configuration de build — workspace uv
Vercel détecte `uv.lock` et construit avec `uv`. `vercel.json` :
```json
{
  "framework": null,
  "installCommand": "uv sync --no-dev",
  "buildCommand": "",
  "functions": { "api/index.py": { "memory": 1024, "maxDuration": 60 } },
  "rewrites": [{ "source": "/(.*)", "destination": "/api/index" }],
  "env": { "VERCEL": "1", "SYNELIA_ENV": "preview" }
}
```
`.vercelignore` — **critique** : ne jamais exclure `.agents`/`.claude`. Leur exclusion fait
échouer le build avec `ENOENT: lstat '/vercel/path0/.agents/…'` (le builder Vercel/uv les référence).
Contenu retenu :
```
.venv
.git
__pycache__
**/__pycache__
.pytest_cache
.ruff_cache
```
Ne pas exiger de `requirements.txt` : `uv sync --no-dev` installe les travaux (workspace) entiers
(sans le groupe `dev`, ni les extras lourds temporalio/openstack/weasyprint). `api/index.py` ajoute
`apps/synelia` et `packages/*` au `sys.path` → `synelia.creer_app()`.

Déploiement anonyme de test :
```bash
vercel deploy --temporary --yes
```

## 5. Vérifier
```bash
curl -s https://<url>/healthz
curl -s -X POST https://<url>/v1/auth/connexion -H 'content-type: application/json' \
  -d '{"email":"admin@synelia.cloud","motDePasse":"<SYNELIA_SEED_ADMIN_MOT_DE_PASSE>"}'
```
Documentation interactive : `https://<url>/v1/docs`. Contrat servi : `/v1/openapi.json`.

## 6. Brancher le frontend
`NEXT_PUBLIC_API_URL=https://<url>/v1` côté synelia-cloud.

## 6. Backend près du lab : `https://api.synelia.dev01.ovh.smile.ci`

Sur dev01, `docker-compose.dev01.yml` lance Postgres 18 (volume durable) et l'API en mode **OpenStack réel**
(lab kolla via `https://keystone.openstack-lab.dev01.ovh.smile.ci/v3`, application credential `synelia-backend`).
Apache (vhost `api.synelia.dev01.ovh.smile.ci`, certificat Let's Encrypt) proxifie vers `127.0.0.1:4010`.

```bash
sudo docker compose -f docker-compose.dev01.yml --env-file /home/jekas/.config/synelia/backend-dev01.env up -d --build
sudo kill -USR1 "$(cat /run/httpd/httpd.pid)"   # rechargement gracieux d'Apache (le `systemctl reload` échoue sur cet hôte)
```

Répartition : **Vercel** = frontend + backend bac à sable (amont simulé, SQLite éphémère, préversions par branche) ;
**dev01** = backend de référence sur le vrai OpenStack, base durable. Le frontend pointe `NEXT_PUBLIC_API_URL`
sur l'un ou l'autre.

## 7. Frontend du bac à sable : `https://app.synelia.dev01.ovh.smile.ci`

Conteneur Docker (image bun → Node standalone, `Dockerfile` du dépôt frontend) construit depuis le commit poussé de la
branche `branchement-api`, `NEXT_PUBLIC_API_URL=https://api.synelia.dev01.ovh.smile.ci/v1` figé à la construction,
servi sur `127.0.0.1:3010` derrière Apache (vhost + Let's Encrypt). Fichiers : `/var/lib/synelia-cloud/deploy-dev01/`
(copies dans `docs/runbooks/`). Redéployer : `/var/lib/synelia-cloud/deploy-dev01/redeploy-front.sh [ref]`.
