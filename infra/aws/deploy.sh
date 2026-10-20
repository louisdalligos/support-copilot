#!/usr/bin/env bash
# Deploy one image on the EC2 host. The Deploy workflow runs this as root through SSM
# Run Command, after unpacking the release bundle (compose files, Caddyfile, these scripts)
# into the project directory. To run it by hand in a Session Manager shell:
#
#   sudo bash /opt/support-copilot/infra/aws/deploy.sh <account>.dkr.ecr.<region>.amazonaws.com/support-copilot:<sha>
#
# It rebuilds .env from Parameter Store, pulls the image, restarts what changed (Caddy too when
# its Caddyfile changed), and waits
# until the app reports healthy. On failure it prints the app's recent logs.
set -euo pipefail

image="${1:?usage: deploy.sh <image>}"

# shellcheck source=/dev/null
set -a && . /etc/support-copilot/host.env && set +a
export PATH="/usr/local/bin:$PATH"
cd "$COPILOT_PROJECT_DIR"

umask 077
{
  bash infra/aws/render-env.sh
  printf "COPILOT_IMAGE='%s'\n" "$image"
  printf "COMPOSE_PROJECT_NAME='%s'\n" "$COMPOSE_PROJECT_NAME"
} > .env.new
mv .env.new .env
umask 022

compose=(docker compose -f docker-compose.yml -f docker-compose.prod.yml)
"${compose[@]}" config --quiet
"${compose[@]}" pull --quiet
"${compose[@]}" up -d --no-build --remove-orphans

# Caddy reads its config at start, through a bind mount of one file. Unpacking a release
# replaces that file, so a running Caddy would keep the old config: recreate it when the
# Caddyfile changed since the last deploy.
caddy_hash="$(sha256sum infra/caddy/Caddyfile | cut -d' ' -f1)"
if [ "$caddy_hash" != "$(cat .caddyfile-hash 2>/dev/null || true)" ]; then
  echo "deploy: Caddyfile changed; restarting Caddy"
  "${compose[@]}" up -d --no-build --no-deps --force-recreate caddy
  echo "$caddy_hash" > .caddyfile-hash
fi

app="$("${compose[@]}" ps -q app)"
status=""
for _ in $(seq 1 60); do
  status="$(docker inspect --format '{{.State.Health.Status}}' "$app")"
  [ "$status" = healthy ] && break
  sleep 5
done

"${compose[@]}" ps
if [ "$status" != healthy ]; then
  echo "deploy: app is $status after 5 minutes" >&2
  "${compose[@]}" logs --tail 80 app >&2
  exit 1
fi

echo "$image" > .deployed-image
docker image prune -f >/dev/null
echo "deploy: $image is live"
