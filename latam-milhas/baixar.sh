#!/usr/bin/env bash
# Baixa o monitor para ~/latam-milhas e instala.  Uso na VPS:
#   curl -sL https://raw.githubusercontent.com/ncvcabofrio-arch/clips-ml-imagens/claude/latam-scraping-vps-58nmif/latam-milhas/baixar.sh | bash
set -euo pipefail
BASE="https://raw.githubusercontent.com/ncvcabofrio-arch/clips-ml-imagens/claude/latam-scraping-vps-58nmif/latam-milhas"
mkdir -p ~/latam-milhas && cd ~/latam-milhas
for f in README.md config.example.yaml latam.py monitor.py requirements.txt instalar.sh rodar.sh .gitignore; do
  echo "baixando $f"
  curl -fsSL "$BASE/$f" -o "$f"
done
chmod +x instalar.sh rodar.sh
bash instalar.sh
