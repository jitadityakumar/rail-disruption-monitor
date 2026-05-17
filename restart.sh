#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if docker compose ps --status running --services 2>/dev/null | grep -q '^app$'; then
  echo "Container is running — rebuilding and restarting..."
  docker compose up -d --build
else
  echo "Container is not running — starting..."
  docker compose up -d
fi
