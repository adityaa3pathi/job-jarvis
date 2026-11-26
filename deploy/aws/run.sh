#!/bin/bash
# Start (or restart) the job-jarvis API on the AWS host: secrets from SSM Parameter Store,
# the API on a private Docker network, and Caddy in front for HTTPS. Run as root on the host.
# Usage: run.sh <public-hostname>     e.g. run.sh 54-153-140-241.sslip.io
set -euo pipefail
HOST="${1:?public hostname, e.g. 54-153-140-241.sslip.io}"
REGION="${AWS_REGION:-ap-southeast-2}"
DIR=/opt/jobjarvis
umask 077

# Secrets: every /jobjarvis/* parameter becomes NAME=value in an env file only root can read.
aws ssm get-parameters-by-path --region "$REGION" --path /jobjarvis --with-decryption \
  --query 'Parameters[].[Name,Value]' --output text \
  | while IFS=$'\t' read -r name value; do printf '%s=%s\n' "${name##*/}" "$value"; done > "$DIR/api.env.new"
# Only Caddy reaches the API (no published port), so trusting forwarded headers is safe.
echo 'FORWARDED_ALLOW_IPS=*' >> "$DIR/api.env.new"
mv "$DIR/api.env.new" "$DIR/api.env"

cat > "$DIR/Caddyfile" <<CADDY
$HOST {
	encode gzip
	reverse_proxy api:8000
}
CADDY

docker network inspect jobjarvis >/dev/null 2>&1 || docker network create jobjarvis
docker rm -f jobjarvis-api jobjarvis-caddy >/dev/null 2>&1 || true
# Exactly one API container: background AI tasks live in its memory.
docker run -d --name jobjarvis-api --network jobjarvis --network-alias api \
  --env-file "$DIR/api.env" --restart unless-stopped --memory 1400m \
  jobjarvis-api:latest
docker run -d --name jobjarvis-caddy --network jobjarvis -p 80:80 -p 443:443 \
  -v "$DIR/Caddyfile:/etc/caddy/Caddyfile:ro" -v jobjarvis-caddy-data:/data \
  --restart unless-stopped caddy:2.10
echo "started: https://$HOST"
