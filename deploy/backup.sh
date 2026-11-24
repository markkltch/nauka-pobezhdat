#!/usr/bin/env bash
# Nightly dump of the database; keeps 14 days. Restore: see README.
set -euo pipefail
cd /opt/nauka
mkdir -p backups
docker compose exec -T db pg_dump -U nauka -Fc nauka > "backups/nauka-$(date +%F).dump"
find backups -name 'nauka-*.dump' -mtime +14 -delete
