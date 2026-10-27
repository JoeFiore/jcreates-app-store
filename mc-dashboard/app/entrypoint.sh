#!/bin/sh
set -e
# The server folder lives on the app's data volume, so worlds, jars, mods
# and backups survive app updates.
mkdir -p /data/server
# First-run hint: if there's no server jar yet, leave a note in the folder
# (the dashboard's file manager is jailed here, so uploads land here too).
if [ ! -f /data/server/server.jar ] && [ ! -f /data/server/README.txt ]; then
  cp /app/server-readme.txt /data/server/README.txt
fi
# Docker build: backend.docker_app strips every /mods route (no ModLink).
exec uvicorn backend.docker_app:app --host 0.0.0.0 --port "${PORT:-8000}"
