#!/usr/bin/env bash
# Instalação na VPS (Ubuntu/Debian). Rode dentro da pasta do projeto:  bash instalar.sh
set -euo pipefail
cd "$(dirname "$0")"

sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip xvfb

python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
# Baixa o Chromium e as bibliotecas de sistema que ele precisa
sudo .venv/bin/python -m playwright install-deps chromium
.venv/bin/python -m playwright install chromium

mkdir -p data
[ -f config.yaml ] || cp config.example.yaml config.yaml
chmod +x rodar.sh

echo
echo "Pronto. Próximos passos:"
echo "  1) edite config.yaml (rotas, Telegram)"
echo "  2) teste uma busca:   ./rodar.sh --teste GIG LIS 2027-03-05"
echo "  3) agende no cron (veja README.md)"
