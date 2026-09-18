#!/usr/bin/env bash
# toppswatch installer — run as root inside a fresh Debian 12 LXC.
#   ./setup.sh
# Prompts for your Telegram credentials, verifies they work, installs the
# systemd service. No file editing required.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SVC_USER="toppswatch"

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
die() { printf '\n\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run as root"

say "Installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip curl ca-certificates >/dev/null
timedatectl set-timezone Europe/Gibraltar 2>/dev/null || true

say "Creating Python environment"
cd "$APP_DIR"
python3 -m venv .venv
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt
mkdir -p data

# ---------------------------------------------------------------- credentials
if [ -f "$APP_DIR/.env" ]; then
  say "Existing .env found — reusing it"
  # shellcheck disable=SC1091
  set -a; . "$APP_DIR/.env"; set +a
else
  say "Telegram setup"
  echo "Paste the token BotFather gave you (looks like 8123456789:AAH...)"
  read -rp "Bot token: " TELEGRAM_BOT_TOKEN
  [ -n "$TELEGRAM_BOT_TOKEN" ] || die "no token entered"

  echo
  echo "Now make sure you've sent your bot a message (any text) from your phone."
  read -rp "Press Enter once you've done that..." _

  echo "Looking up your chat ID..."
  CHAT_JSON="$(curl -fsS "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getUpdates" || true)"
  TELEGRAM_CHAT_ID="$(printf '%s' "$CHAT_JSON" | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit()
for update in data.get("result", []):
    msg = update.get("message") or update.get("channel_post") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    if chat_id:
        print(chat_id)
        break
' || true)"

  if [ -n "$TELEGRAM_CHAT_ID" ]; then
    echo "Found chat ID: $TELEGRAM_CHAT_ID"
  else
    echo "Couldn't detect it automatically (did you message the bot?)."
    read -rp "Chat ID (enter manually): " TELEGRAM_CHAT_ID
    [ -n "$TELEGRAM_CHAT_ID" ] || die "no chat ID"
  fi

  cat > "$APP_DIR/.env" <<EOF
TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN}
TELEGRAM_CHAT_ID=${TELEGRAM_CHAT_ID}
EOF
  chmod 600 "$APP_DIR/.env"
fi

# -------------------------------------------------------------------- config
if [ ! -f "$APP_DIR/config.yaml" ]; then
  say "Writing config.yaml"
  cat > "$APP_DIR/config.yaml" <<'EOF'
site:
  base_url: https://es.topps.com
  collections:
    - /collections/all-products
    - /collections
  sitemaps:
    - /sitemap.xml

watch:
  patterns: [chrome]
  exclude_patterns: [sleeve, top ?loader, toploader, album, binder]
  handles: []
  auto_discover: true
  discover_every_minutes: 30

polling:
  interval_seconds: 60
  jitter_percent: 20
  concurrency: 3
  rate_limit_cooldown_seconds: 900

alerts:
  min_alert_interval_seconds: 1800
  alert_on_first_sight: false
  notify_on_new_product: true

state_file: /opt/toppswatch/data/state.json
log_level: INFO

notifications:
  telegram:
    enabled: true
    bot_token: ${TELEGRAM_BOT_TOKEN}
    chat_id: ${TELEGRAM_CHAT_ID}
EOF
fi

# ---------------------------------------------------------------- smoke tests
say "Sending a test message to Telegram"
set -a; . "$APP_DIR/.env"; set +a
if ! .venv/bin/python -m toppswatch.main --config config.yaml test-notify; then
  die "Telegram test failed — check the token and chat ID in $APP_DIR/.env"
fi

say "Checking what's live on Topps España right now"
.venv/bin/python -m toppswatch.main --config config.yaml discover || true

echo
read -rp "Run a full detection check now (recommended, ~1 min)? [Y/n] " RUNCHK
if [[ ! "${RUNCHK:-Y}" =~ ^[Nn] ]]; then
  .venv/bin/python -m toppswatch.main --config config.yaml once --debug || true
  echo
  echo "Compare those in_stock verdicts against the real product pages."
  echo "Anything showing 'via none' means the parser needs a tweak."
fi

# ------------------------------------------------------------------- service
say "Installing systemd service"
id -u "$SVC_USER" >/dev/null 2>&1 || useradd -r -s /usr/sbin/nologin "$SVC_USER"
chown -R "$SVC_USER:$SVC_USER" "$APP_DIR"

sed "s|/opt/toppswatch|$APP_DIR|g" "$APP_DIR/toppswatch.service" \
  > /etc/systemd/system/toppswatch.service
systemctl daemon-reload
systemctl enable --now toppswatch

sleep 3
systemctl --no-pager --lines=15 status toppswatch || true

cat <<EOF

$(printf '\033[1;32m')Done.$(printf '\033[0m')

  Live logs     journalctl -u toppswatch -f
  Stock summary sudo -u $SVC_USER $APP_DIR/.venv/bin/python -m toppswatch.main --config $APP_DIR/config.yaml status
  Restart       systemctl restart toppswatch

First run records a silent baseline, so your first alert comes on the next
genuine restock.
EOF
