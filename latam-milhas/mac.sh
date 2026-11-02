#!/usr/bin/env bash
# Instala e testa o monitor no Mac. Uso (no Terminal do Mac, NÃO dentro do ssh):
#   curl -sL https://raw.githubusercontent.com/ncvcabofrio-arch/clips-ml-imagens/claude/latam-scraping-vps-58nmif/latam-milhas/mac.sh | bash
set -euo pipefail
BASE="https://raw.githubusercontent.com/ncvcabofrio-arch/clips-ml-imagens/claude/latam-scraping-vps-58nmif/latam-milhas"
if ! python3 -c 'import sys; assert sys.version_info >= (3, 9)' 2>/dev/null; then
  echo "Python 3 não encontrado. Se abrir uma janela pedindo para instalar as"
  echo "'ferramentas de linha de comando', clique em Instalar e rode este comando de novo."
  xcode-select --install 2>/dev/null || true
  exit 1
fi
mkdir -p ~/latam-milhas && cd ~/latam-milhas
for f in README.md config.example.yaml latam.py monitor.py requirements.txt .gitignore; do
  curl -fsSL "$BASE/$f?nocache=$(date +%s)" -o "$f"
done
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
# versões novas com pacote pronto para Mac/Python 3.14 (sem compilar nada)
.venv/bin/pip install -q --upgrade --only-binary=greenlet "greenlet>=3.2.4" "playwright>=1.55" "PyYAML>=6.0.2" "requests>=2.32"
.venv/bin/python -m playwright install chromium
mkdir -p data
[ -f config.yaml ] || cp config.example.yaml config.yaml
echo
echo "=== TESTE (vai abrir uma janela do navegador, não mexa nela) ==="
.venv/bin/python monitor.py --teste GIG LIS 2027-03-05 || true
echo "=== FIM ==="
