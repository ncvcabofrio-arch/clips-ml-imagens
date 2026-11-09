#!/usr/bin/env bash
# Roda uma varredura da LATAM mostrando o log ao vivo e salvando em data/ultima_rodada.log
cd "$(dirname "$0")"
LOG=data/ultima_rodada.log
mkdir -p data
echo "=== Rodada iniciada em $(date '+%d/%m %H:%M') ===" | tee "$LOG"
grep -E "ida_de|ida_ate|espera_entre|max_buscas|revalidar" config.yaml | sed 's/ *#.*//' | tee -a "$LOG"
caffeinate -i .venv/bin/python -u monitor.py 2>&1 \
  | grep --line-buffered -v -E "fullstory|google|sst-la|telegram|pipe closed" | tee -a "$LOG"
echo | tee -a "$LOG"
.venv/bin/python -u monitor.py --melhores 2>&1 | tee -a "$LOG"
echo "=== Rodada terminada em $(date '+%d/%m %H:%M') ===" | tee -a "$LOG"
open -a "Google Chrome" data/painel.html 2>/dev/null
echo
echo "Log salvo em $LOG. Para me mandar:  ./copiar_log.sh  e depois Cmd+V no chat."
