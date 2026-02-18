#!/usr/bin/env bash
# KitchenIQ - Home Assistant Add-on startup script
set -e

echo "[KitchenIQ] Starting up..."

# HA Supervisor writes add-on options to /data/options.json
# Python reads them at startup via load_ha_options()
cd /app
exec python3 app.py
