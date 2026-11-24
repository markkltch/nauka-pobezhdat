#!/usr/bin/env bash
# Pull the latest main and restart. Run on the server: /opt/nauka/deploy/update.sh
set -euo pipefail
cd /opt/nauka
git pull --ff-only
docker compose up -d --build
docker compose ps
