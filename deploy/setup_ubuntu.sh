#!/usr/bin/env bash
set -euo pipefail

# Automated setup script for Gemini Telegram Bot on Ubuntu
# Usage:
#   Webhook mode with HTTPS (Recommended with domain):
#     DOMAIN=bot.example.com ./deploy/setup_ubuntu.sh --webhook
#   Polling mode (no domain needed):
#     ./deploy/setup_ubuntu.sh

APP_DIR="/opt/gemini-tel-bot"
SERVICE_USER="${SUDO_USER:-$(whoami)}"
MODE="${1:-polling}"
DOMAIN="${DOMAIN:-bot.example.com}"

echo "==> Installing system dependencies..."
sudo apt-get update -y
sudo apt-get install -y curl git build-essential rsync

if ! command -v uv >/dev/null 2>&1; then
    echo "==> Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
fi

UV_BIN="$(command -v uv || echo "$HOME/.local/bin/uv")"

echo "==> Setting up application directory at ${APP_DIR}..."
sudo mkdir -p "${APP_DIR}"
sudo chown -R "${SERVICE_USER}:${SERVICE_USER}" "${APP_DIR}"

if [ "$(pwd)" != "${APP_DIR}" ]; then
    echo "==> Copying project files to ${APP_DIR}..."
    rsync -av --exclude='.venv' --exclude='__pycache__' --exclude='.git' ./ "${APP_DIR}/"
fi

cd "${APP_DIR}"

echo "==> Creating Python 3.12 virtual environment and syncing dependencies..."
"${UV_BIN}" sync --python 3.12

if [ ! -f "${APP_DIR}/.env" ]; then
    cp "${APP_DIR}/.env.example" "${APP_DIR}/.env"
    echo "WARNING: Created ${APP_DIR}/.env from .env.example. Please edit ${APP_DIR}/.env with your real keys!"
fi
chmod 600 "${APP_DIR}/.env"

if [ "${MODE}" = "--webhook" ] || [ "${MODE}" = "webhook" ]; then
    echo "==> Configuring Webhook Mode for domain: ${DOMAIN}..."

    # Ensure trailing newline in .env before appending
    sed -i -e '$a\' "${APP_DIR}/.env"

    # Ensure BOT_MODE=webhook and WEBHOOK_URL are set in .env
    if grep -q "^BOT_MODE=" "${APP_DIR}/.env"; then
        sed -i "s|^BOT_MODE=.*|BOT_MODE=webhook|" "${APP_DIR}/.env"
    else
        echo "BOT_MODE=webhook" >> "${APP_DIR}/.env"
    fi

    if grep -q "^WEBHOOK_URL=" "${APP_DIR}/.env"; then
        sed -i "s|^WEBHOOK_URL=.*|WEBHOOK_URL=https://${DOMAIN}/api/webhook|" "${APP_DIR}/.env"
    else
        echo "WEBHOOK_URL=https://${DOMAIN}/api/webhook" >> "${APP_DIR}/.env"
    fi

    # Install Caddy for automatic Let's Encrypt HTTPS
    if ! command -v caddy >/dev/null 2>&1; then
        echo "==> Installing Caddy web server for automatic HTTPS..."
        sudo apt-get install -y debian-keyring debian-archive-keyring apt-transport-https
        curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg --yes
        curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
        sudo apt-get update -y
        sudo apt-get install -y caddy
    fi

    echo "==> Configuring Caddy reverse proxy for ${DOMAIN} -> 127.0.0.1:8000..."
    sudo tee /etc/caddy/Caddyfile >/dev/null <<EOF
${DOMAIN} {
    reverse_proxy 127.0.0.1:8000
}
EOF
    sudo systemctl enable caddy
    sudo systemctl reload-or-restart caddy

    # Stop polling service if it was running
    sudo systemctl stop gemini-tel-bot.service 2>/dev/null || true
    sudo systemctl disable gemini-tel-bot.service 2>/dev/null || true

    echo "==> Installing systemd service (gemini-tel-bot-webhook.service)..."
    sudo sed "s|User=ubuntu|User=${SERVICE_USER}|g" "${APP_DIR}/deploy/gemini-tel-bot-webhook.service" | sudo tee /etc/systemd/system/gemini-tel-bot-webhook.service >/dev/null

    sudo systemctl daemon-reload
    sudo systemctl enable gemini-tel-bot-webhook.service
    sudo systemctl restart gemini-tel-bot-webhook.service

    echo "==> Done! Webhook service status:"
    sudo systemctl status gemini-tel-bot-webhook.service --no-pager
else
    echo "==> Installing systemd service in Polling Mode (gemini-tel-bot.service)..."
    sudo systemctl stop gemini-tel-bot-webhook.service 2>/dev/null || true
    sudo systemctl disable gemini-tel-bot-webhook.service 2>/dev/null || true

    sudo sed "s|User=ubuntu|User=${SERVICE_USER}|g" "${APP_DIR}/deploy/gemini-tel-bot.service" | sudo tee /etc/systemd/system/gemini-tel-bot.service >/dev/null

    sudo systemctl daemon-reload
    sudo systemctl enable gemini-tel-bot.service
    sudo systemctl restart gemini-tel-bot.service

    echo "==> Done! Polling service status:"
    sudo systemctl status gemini-tel-bot.service --no-pager
fi
