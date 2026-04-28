#!/usr/bin/env sh
set -eu

if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
  cat <<'USAGE'
Usage: ./docker/build-and-push.sh [--dockerhub] [--help]

Builds the scraper Docker image. By default it builds locally only (no push).
Use --dockerhub to tag and push to Docker Hub.

Environment variables:
  IMAGE_NAME           scraper (default)
  TAG                  latest (default)
  DOCKERHUB_NAMESPACE  Docker Hub namespace (repo owner)
  PLATFORM             Optional, e.g. linux/amd64
  DOCKERFILE           docker/Dockerfile (default)
  CONTEXT              . (default)
  ENV_FILE             .env (default; loaded if present)
  DOCKERHUB_USERNAME   Optional; if set (or in ENV_FILE) script can auto-login before push
  DOCKERHUB_TOKEN      Optional; Docker Hub personal access token (recommended) for auto-login

Examples:
  ./docker/build-and-push.sh
  ./docker/build-and-push.sh --dockerhub
  TAG=v1 ./docker/build-and-push.sh --dockerhub
USAGE
  exit 0
fi

PUSH_DOCKERHUB=0
if [ "${1:-}" = "--dockerhub" ]; then
  PUSH_DOCKERHUB=1
  shift
fi

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)

IMAGE_NAME="${IMAGE_NAME:-scraper}"
DOCKERHUB_NAMESPACE="${DOCKERHUB_NAMESPACE:-}"
TAG="${TAG:-latest}"
PLATFORM="${PLATFORM:-}"
DOCKERFILE="${DOCKERFILE:-$PROJECT_ROOT/docker/Dockerfile}"
CONTEXT="${CONTEXT:-$PROJECT_ROOT}"
ENV_FILE="${ENV_FILE:-$PROJECT_ROOT/.env}"

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

LOCAL_IMAGE="$IMAGE_NAME:$TAG"
HUB_IMAGE="$DOCKERHUB_NAMESPACE/$IMAGE_NAME:$TAG"

BUILD_ARGS=""
if [ -n "$PLATFORM" ]; then
  BUILD_ARGS="$BUILD_ARGS --platform $PLATFORM"
fi

set -x

docker build $BUILD_ARGS -f "$DOCKERFILE" -t "$LOCAL_IMAGE" "$CONTEXT"

if [ "$PUSH_DOCKERHUB" -eq 1 ]; then
  if [ -z "$DOCKERHUB_NAMESPACE" ]; then
    echo "DOCKERHUB_NAMESPACE is required for --dockerhub (or set DOCKERHUB_USERNAME)." >&2
    exit 2
  fi
  docker tag "$LOCAL_IMAGE" "$HUB_IMAGE"

  if [ -n "$DOCKERHUB_USERNAME" ] && [ -n "$DOCKERHUB_TOKEN" ]; then
    set +x
    printf '%s' "$DOCKERHUB_TOKEN" | docker login -u "$DOCKERHUB_USERNAME" --password-stdin
    set -x
  fi

  docker push "$HUB_IMAGE"
fi
