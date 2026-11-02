#!/usr/bin/env bash
# Baixa o monitor para ~/latam-milhas, instala e roda um teste, tudo em segundo plano.
# O progresso fica em ~/instalacao.log  (veja com: tail -20 ~/instalacao.log)
set -euo pipefail
BASE="https://raw.githubusercontent.com/ncvcabofrio-arch/clips-ml-imagens/claude/latam-scraping-vps-58nmif/latam-milhas"
mkdir -p ~/latam-milhas && cd ~/latam-milhas
for f in README.md config.example.yaml latam.py monitor.py requirements.txt instalar.sh rodar.sh .gitignore; do
  curl -fsSL "$BASE/$f" -o "$f"
done
chmod +x instalar.sh rodar.sh
nohup bash -c 'cd ~/latam-milhas && bash instalar.sh && echo "=== TESTE ===" && ./rodar.sh --teste GIG LIS 2027-03-05; echo "=== FIM ==="' > ~/instalacao.log 2>&1 &
echo
echo "Instalando em segundo plano (uns 5 min). Para ver o andamento digite:"
echo "   tail -20 ~/instalacao.log"
