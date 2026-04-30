#!/usr/bin/env sh
set -eu

if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
  cat <<'USAGE'
Usage: ./docker/deploy.sh [--dockerhub] [loop|once] [scraper args...]

Runs the scraper container.
  - Default is local deploy (uses local image; builds if missing).
  - With --dockerhub, pulls from Docker Hub before running.

Positional arguments:
  loop|once   Defaults to loop

Additional arguments:
  Any extra arguments are passed through to `python scraper.py ...` inside the container.
  Example: ./docker/deploy.sh loop --all

Environment variables:
  IMAGE_NAME            scraper (default)
  TAG                   latest (default)
  DOCKERHUB_NAMESPACE   Docker Hub namespace (repo owner)
  CONTAINER_NAME        scraper (default)
  ENV_FILE              .env (default)
  RESTART_POLICY        unless-stopped (default)
  DOCKERFILE            docker/Dockerfile (default, for local builds)
  CONTEXT               . (default, for local builds)
  DOCKERHUB_USERNAME     Optional; if set (or in ENV_FILE) script can auto-login before pull
  DOCKERHUB_TOKEN        Optional; only needed for private images or if you want explicit auth

Examples:
  ./docker/deploy.sh
  ./docker/deploy.sh once
  ./docker/deploy.sh loop --all
  ./docker/deploy.sh --dockerhub
USAGE
  exit 0
fi

PULL_DOCKERHUB=0
if [ "${1:-}" = "--dockerhub" ]; then
  PULL_DOCKERHUB=1
  shift
fi

MODE="${1:-loop}"  # loop|once
shift || true
EXTRA_ARGS="$@"

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)

IMAGE_NAME="${IMAGE_NAME:-scraper}"
DOCKERHUB_NAMESPACE="${DOCKERHUB_NAMESPACE:-}"
TAG="${TAG:-latest}"
CONTAINER_NAME="${CONTAINER_NAME:-scraper}"
ENV_FILE="${ENV_FILE:-$PROJECT_ROOT/.env}"
RESTART_POLICY="${RESTART_POLICY:-unless-stopped}"
DOCKERFILE="${DOCKERFILE:-$PROJECT_ROOT/docker/Dockerfile}"
CONTEXT="${CONTEXT:-$PROJECT_ROOT}"
STATE_VOLUME="${STATE_VOLUME:-scraper_state}"
STATE_PATH_IN_CONTAINER="${STATE_PATH_IN_CONTAINER:-/data/state.json}"
if [ -f "$ENV_FILE" ]; then
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
      ''|'#'*)
        continue
        ;;
      export\ *)
        line=${line#export }
        ;;
    esac
    case "$line" in
      *=*)
        key=${line%%=*}
        val=${line#*=}
        case "$key" in
          *[!A-Za-z0-9_]*|'' )
            continue
            ;;
        esac
        export "$key=$val"
        ;;
    esac
  done < "$ENV_FILE"
fi

DOCKERHUB_USERNAME="${DOCKERHUB_USERNAME:-}"
DOCKERHUB_TOKEN="${DOCKERHUB_TOKEN:-}"

if [ -z "$DOCKERHUB_NAMESPACE" ] && [ -n "$DOCKERHUB_USERNAME" ]; then
  DOCKERHUB_NAMESPACE="$DOCKERHUB_USERNAME"
fi

if [ "$PULL_DOCKERHUB" -eq 1 ] && [ -z "$DOCKERHUB_NAMESPACE" ]; then
  echo "DOCKERHUB_NAMESPACE is required for --dockerhub (or set DOCKERHUB_USERNAME)." >&2
  exit 2
fi

LOCAL_IMAGE="$IMAGE_NAME:$TAG"
HUB_IMAGE="$DOCKERHUB_NAMESPACE/$IMAGE_NAME:$TAG"

if [ "$PULL_DOCKERHUB" -eq 1 ]; then
  FULL_IMAGE="$HUB_IMAGE"
else
  FULL_IMAGE="$LOCAL_IMAGE"
fi

set -x

if [ "$PULL_DOCKERHUB" -eq 1 ]; then
  if [ -n "$DOCKERHUB_USERNAME" ] && [ -n "$DOCKERHUB_TOKEN" ]; then
    set +x
    printf '%s' "$DOCKERHUB_TOKEN" | docker login -u "$DOCKERHUB_USERNAME" --password-stdin
    set -x
  fi
  docker pull "$HUB_IMAGE"
else
  if ! docker image inspect "$LOCAL_IMAGE" >/dev/null 2>&1; then
    docker build -f "$DOCKERFILE" -t "$LOCAL_IMAGE" "$CONTEXT"
  fi
fi

if docker ps -a --format '{{.Names}}' | grep -qx "$CONTAINER_NAME"; then
  docker rm -f "$CONTAINER_NAME"
fi

ENV_FILE_ARG=""
if [ -f "$ENV_FILE" ]; then
  ENV_FILE_ARG="--env-file"
fi

docker run -d \
  --name "$CONTAINER_NAME" \
  --restart "$RESTART_POLICY" \
  $ENV_FILE_ARG ${ENV_FILE_ARG:+"$ENV_FILE"} \
  -e "STATE_FILE=$STATE_PATH_IN_CONTAINER" \
  -v "$STATE_VOLUME:/data" \
  "$FULL_IMAGE" \
  python scraper.py "$MODE" $EXTRA_ARGS
