# Docker

This folder contains helper scripts for building and running the scraper as a Docker container.

## Build (local)

Builds `scraper:latest` into your local Docker engine (Docker Desktop, etc.).

```sh
./docker/build-and-push.sh
```

## Push to Docker Hub (public image)

The Docker Hub image name is:

`<dockerhub-namespace>/<IMAGE_NAME>:<TAG>`.

Pushing requires Docker Hub authentication. You can either:

- Login once manually (`docker login`), or
- Put credentials in your `.env` (preferred for automation). See `.env.example` for the required variables.

```sh
./docker/build-and-push.sh --dockerhub

# Optional variables:
IMAGE_NAME=scraper TAG=latest DOCKERHUB_NAMESPACE=<dockerhub-namespace>
```

## Deploy (local)

Uses the local image (builds it if missing) and runs the container.

The container stores its `state.<provider-key>.json` in a named Docker volume (default: `scraper_state`) so it persists across redeploys.

```sh
./docker/deploy.sh

# to run only one scrape cycle:
./docker/deploy.sh once
```

## Deploy (from Docker Hub)

Pulls `<dockerhub-namespace>/<IMAGE_NAME>:<TAG>` from Docker Hub and runs the container.

```sh
./docker/deploy.sh --dockerhub

# start all providers (recommended):
./docker/deploy.sh --dockerhub loop --all
```

For a public image, pulling does not require a token. If the repo is private (or you want explicit auth), set `DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN` in `.env`.

## Runtime configuration

The container reads configuration from an env file (default: `.env` at repo root):

- `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`
- `EMAIL_TO`
- `CHECK_INTERVAL`

To use a different env file:

```sh
ENV_FILE=.env ./docker/deploy.sh
```

## Help

Both scripts support `--help`:

```sh
./docker/build-and-push.sh --help
./docker/deploy.sh --help
```
