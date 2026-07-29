#!/usr/bin/env bash
# bootstrap_ec2.sh — initialize Ubuntu 22.04 EC2 (ap-south-1) for F&O deploy.
# Run as ubuntu (script will sudo where needed).

set -euo pipefail

APP_DIR="/home/ubuntu/app"
VENV_DIR="${APP_DIR}/venv"

log() { printf '[bootstrap] %s\n' "$*"; }

if [[ "$(id -un)" != "ubuntu" ]]; then
  log "WARN: expected user ubuntu (got $(id -un)); continuing"
fi

log "System update"
sudo apt-get update -y
sudo DEBIAN_FRONTEND=noninteractive apt-get upgrade -y

log "Core dependencies"
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
  python3 \
  python3-pip \
  python3-venv \
  git \
  nodejs \
  npm \
  curl \
  build-essential

log "PM2 (global)"
sudo npm install -g pm2

log "App directory ${APP_DIR}"
sudo mkdir -p "${APP_DIR}"
sudo chown -R ubuntu:ubuntu "${APP_DIR}"

log "Python venv ${VENV_DIR}"
if [[ ! -d "${VENV_DIR}" ]]; then
  python3 -m venv "${VENV_DIR}"
fi
# shellcheck disable=SC1091
source "${VENV_DIR}/bin/activate"
python -m pip install --upgrade pip

log "Done"
log "  app:  ${APP_DIR}"
log "  venv: ${VENV_DIR}"
log "  pm2:  $(command -v pm2) ($(pm2 -v 2>/dev/null || echo unknown))"
log "Next: clone repo into ${APP_DIR}, copy .env / secrets, then pm2 start fno-worker"
