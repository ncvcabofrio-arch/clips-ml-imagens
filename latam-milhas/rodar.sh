#!/usr/bin/env bash
# Roda o monitor dentro de uma tela virtual (xvfb), para usar o navegador "com tela",
# que passa melhor pelo anti-bot do que o modo headless.
cd "$(dirname "$0")"
exec xvfb-run -a --server-args="-screen 0 1366x850x24" .venv/bin/python monitor.py "$@"
