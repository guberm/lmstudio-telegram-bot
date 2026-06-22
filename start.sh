#!/usr/bin/env bash
set -Eeuo pipefail
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$APP_DIR"
mkdir -p data logs
exec /usr/bin/flock -n "${TMPDIR:-/tmp}/lmstudio-telegram-bot.lock" \
  "$APP_DIR/.venv/bin/python" "$APP_DIR/bot.py" \
  >> "$APP_DIR/logs/bot.log" 2>&1
