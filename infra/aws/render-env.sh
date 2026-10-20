#!/usr/bin/env bash
# Print a Docker Compose .env built from the SSM parameters under COPILOT_PARAM_PREFIX.
#
#   /support-copilot/prod/DOMAIN = copilot.example.com   ->   DOMAIN='copilot.example.com'
#
# Values are single-quoted so Compose takes them literally (the admin password hash contains $).
set -euo pipefail

prefix="${COPILOT_PARAM_PREFIX:?set COPILOT_PARAM_PREFIX, e.g. /support-copilot/prod}"

aws ssm get-parameters-by-path --path "${prefix}/" --recursive --with-decryption \
  --query 'Parameters[].[Name,Value]' --output text |
  while IFS=$'\t' read -r name value; do
    key="${name##*/}"
    if [[ ! "$key" =~ ^[A-Z][A-Z0-9_]*$ ]]; then
      echo "render-env: skipping parameter with an unusual name: $name" >&2
      continue
    fi
    if [[ "$value" == *"'"* ]]; then
      echo "render-env: $name contains a single quote, which .env cannot hold literally" >&2
      exit 1
    fi
    printf "%s='%s'\n" "$key" "$value"
  done
