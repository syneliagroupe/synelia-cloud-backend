#!/usr/bin/env bash
# Déploie une application sur le cluster PaaS (chart `stakater/application`) depuis :
#   - une image publique (Docker Hub, ghcr, quay…)           → --image
#   - un dépôt Git **public**, construit par nixpacks puis
#     poussé dans le registre local Zot                       → --git
#
# C'est le déclencheur « CI/CD » côté plateforme : un job (GitHub Action, webhook,
# tâche Temporal) n'a qu'à appeler ce script avec l'URL du dépôt.
#
# Usage :
#   ./paas-deploy-app.sh --name demo-web --image nginx:alpine --port 80
#   ./paas-deploy-app.sh --name demo-api --git https://github.com/user/repo --branch main --port 8080
#
# Options :
#   --name        nom de l'application (release Helm)                     [requis]
#   --namespace   namespace cible                                  [demo-apps]
#   --image       image publique à déployer (mutuellement exclusif avec --git)
#   --git         URL d'un dépôt Git public
#   --branch      branche à construire                                    [main]
#   --port        port du conteneur                                       [8080]
#   --replicas    nombre de réplicas                                      [2]
#   --env         K=V (répétable)
#   --registry    registre Zot (défaut : VIP du Service `zot` découvert)
#   --tag         tag d'image (défaut : horodatage)
#   --build-only  construit/pousse sans déployer
set -euo pipefail

NAME=""; NS="${NS:-demo-apps}"; IMAGE=""; GIT=""; BRANCH="main"
PORT=8080; REPLICAS=2; REGISTRY=""; TAG=""; BUILD_ONLY=0
ENVS=()

while [ $# -gt 0 ]; do
  case "$1" in
    --name) NAME="$2"; shift 2;;
    --namespace) NS="$2"; shift 2;;
    --image) IMAGE="$2"; shift 2;;
    --git) GIT="$2"; shift 2;;
    --branch) BRANCH="$2"; shift 2;;
    --port) PORT="$2"; shift 2;;
    --replicas) REPLICAS="$2"; shift 2;;
    --env) ENVS+=("$2"); shift 2;;
    --registry) REGISTRY="$2"; shift 2;;
    --tag) TAG="$2"; shift 2;;
    --build-only) BUILD_ONLY=1; shift;;
    *) echo "option inconnue: $1" >&2; exit 2;;
  esac
done

[ -n "$NAME" ] || { echo "--name requis" >&2; exit 2; }
[ -n "$IMAGE" ] || [ -n "$GIT" ] || { echo "--image ou --git requis" >&2; exit 2; }
[ -z "$IMAGE" ] || [ -z "$GIT" ] || { echo "--image et --git mutuellement exclusifs" >&2; exit 2; }
need() { command -v "$1" >/dev/null || { echo "'$1' introuvable" >&2; exit 1; }; }
need kubectl; need helm

if [ -z "$REGISTRY" ]; then
  VIP=$(kubectl -n registry get svc zot -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null || true)
  [ -n "$VIP" ] || { echo "registre Zot introuvable (Service registry/zot sans VIP)" >&2; exit 1; }
  REGISTRY="${VIP}:5000"
fi

TARGET_IMAGE="$IMAGE"
if [ -n "$GIT" ]; then
  need git; need nixpacks; need crane; need docker
  TAG="${TAG:-$(date +%Y%m%d-%H%M%S)}"
  TARGET_IMAGE="${REGISTRY}/${NAME}:${TAG}"
  SRC="/tmp/paas-build-${NAME}-$$"
  echo "== clone $GIT ($BRANCH)"
  git clone --depth 1 --branch "$BRANCH" "$GIT" "$SRC"
  echo "== nixpacks build → $TARGET_IMAGE"
  nixpacks build "$SRC" --name "$TARGET_IMAGE"
  # nixpacks produit une image dans le démon Docker local ; on la transfère au
  # registre en clair via `crane` (pas besoin de configurer Docker en insecure).
  echo "== push → Zot"
  docker save "$TARGET_IMAGE" -o /tmp/paas-image-$$.tar
  crane push /tmp/paas-image-$$.tar "$TARGET_IMAGE" --insecure
  rm -f /tmp/paas-image-$$.tar
  rm -rf "$SRC"
fi

if [ "$BUILD_ONLY" = "1" ]; then
  echo "build/push seulement : $TARGET_IMAGE"
  exit 0
fi

echo "== helm upgrade --install $NAME ($TARGET_IMAGE)"
kubectl create ns "$NS" 2>/dev/null || true
# Découpage repository/tag : le registre contient un `:` (port) — `${x%%:*}` coupe
# au premier et casse l'image (constaté : `192.168.20.128:20260918-123539`). On ne
# coupe que si la partie après le dernier `/` porte un tag.
LAST_SEG="${TARGET_IMAGE##*/}"
if [ "$LAST_SEG" != "${LAST_SEG%%:*}" ]; then
  IMG_REPO="${TARGET_IMAGE%:*}"; IMG_TAG="${TARGET_IMAGE##*:}"
else
  IMG_REPO="$TARGET_IMAGE"; IMG_TAG="latest"
fi
VALUES=$(mktemp)
{
  echo "applicationName: $NAME"
  echo "deployment:"
  echo "  enabled: true"
  echo "  image:"
  echo "    repository: $IMG_REPO"
  echo "    tag: $IMG_TAG"
  echo "    pullPolicy: Always"
  echo "  replicas: $REPLICAS"
  # Les défauts du chart (runAsNonRoot + readOnlyRootFilesystem) cassent la
  # plupart des images (nginx, nixpacks) : on les désactive.
  echo "  containerSecurityContext:"
  echo "    runAsNonRoot: false"
  echo "    readOnlyRootFilesystem: false"
  echo "  ports:"
  echo "    - containerPort: $PORT"
  echo "      name: http"
  echo "      protocol: TCP"
  if [ ${#ENVS[@]} -gt 0 ]; then
    echo "  env:"
    for e in "${ENVS[@]}"; do
      echo "    ${e%%=*}:"
      echo "      value: \"${e#*=}\""
    done
  fi
  echo "service:"
  echo "  enabled: true"
  echo "  type: ClusterIP"
  echo "  ports:"
  echo "    - port: $PORT"
  echo "      name: http"
  echo "      protocol: TCP"
  echo "      targetPort: $PORT"
} > "$VALUES"

helm upgrade --install "$NAME" stakater/application -n "$NS" -f "$VALUES" --timeout 5m
rm -f "$VALUES"

echo "== attente du déploiement"
kubectl -n "$NS" rollout status deploy/"$NAME" --timeout=3m
kubectl -n "$NS" get deploy,svc,pods -l app.kubernetes.io/name="$NAME" -o wide
echo "OK: $NAME ($TARGET_IMAGE)"
