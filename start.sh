#!/usr/bin/env bash
# Launch Productive Time Sync
cd "$(dirname "$0")"
source .venv/bin/activate
exec python app.py
