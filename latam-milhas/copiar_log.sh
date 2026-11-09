#!/usr/bin/env bash
# Copia o log da última rodada (andamento + resumo) para colar no chat
cd "$(dirname "$0")"
LOG=data/ultima_rodada.log
[ -f "$LOG" ] || { echo "Ainda não há log. Rode ./varrer.sh primeiro."; exit 1; }
{ grep -v -E "^  [0-9]{2}/[0-9]{2} →" "$LOG" | head -150
  echo "--- melhores (início) ---"
  grep -E "^  [0-9]{2}/[0-9]{2} →|^===" "$LOG" | head -40
} | pbcopy
echo "Log copiado! Cole no chat com Cmd+V."
