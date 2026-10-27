#!/bin/bash
# First install: generate the dashboard login password and keep it in the
# app's data dir so it survives updates. Read it any time from the Umbrel
# terminal with:
#   cat ~/umbrel/app-data/mc-dashboard/dashboard-password.txt
set -e
PASSWORD_FILE="${APP_DATA_DIR}/dashboard-password.txt"
if [ ! -f "$PASSWORD_FILE" ]; then
  mkdir -p "${APP_DATA_DIR}"
  tr -dc 'A-Za-z0-9' < /dev/urandom | head -c 16 > "$PASSWORD_FILE"
fi
export DASHBOARD_PASSWORD="$(cat "$PASSWORD_FILE")"
