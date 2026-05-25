#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.env.deploy"

if [[ ! -f "$ENV_FILE" ]]; then
    echo "Error: .env.deploy not found at $ENV_FILE"
    echo "Copy .env.deploy.example to .env.deploy and fill in the values."
    exit 1
fi

source "$ENV_FILE"

REMOTE="$REMOTE_USER@$REMOTE_HOST"

echo "==> Building image: $IMAGE_NAME:latest"
docker build -t "$IMAGE_NAME:latest" "$SCRIPT_DIR/app"

echo "==> Transferring image to $REMOTE_HOST (this may take a minute)"
docker save "$IMAGE_NAME:latest" | gzip | ssh "$REMOTE" docker load

echo "==> Pushing compose file"
scp "$SCRIPT_DIR/docker-compose.prod.yml" "$REMOTE:$REMOTE_APP_DIR/docker-compose.prod.yml"

echo "==> Restarting service"
ssh "$REMOTE" "cd '$REMOTE_APP_DIR' && docker compose -f docker-compose.prod.yml up -d"

echo "==> Done. Verifying..."
sleep 5
ssh "$REMOTE" "docker compose -f '$REMOTE_APP_DIR/docker-compose.prod.yml' ps"
