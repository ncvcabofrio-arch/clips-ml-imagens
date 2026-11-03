#!/usr/bin/env python3
"""Robô da Smiles (roda em paralelo com o da LATAM).

Uso:
  .venv/bin/python monitor_smiles.py --login
  .venv/bin/python monitor_smiles.py --teste GIG MCO 2027-02-22 2027-03-04
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

RAIZ = Path(__file__).parent
(RAIZ / "data").mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler(RAIZ / "data" / "smiles.log")],
)

from latam import Bloqueado, PrecisaLogin, resumo_estrutura  # noqa: E402
from smiles import SmilesBuscador  # noqa: E402


def _mil(x: float) -> str:
    return f"{x:,.0f}".replace(",", ".")


def _h(m) -> str:
    return f"{m // 60}h{m % 60:02d}" if m else "?"


def mostrar(titulo, voos, n=10):
    print(f"\n== {titulo} ({len(voos)} tarifas) ==")
    for v in sorted(voos, key=lambda v: v.milhas)[:n]:
        print(f"  {v.voo:<22} {v.partida[11:16]:>5} {v.paradas}p {_h(v.duracao_min):>6} {v.tarifa:<16} "
              f"{_mil(v.milhas):>9} milhas  assentos={v.assentos}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--login", action="store_true")
    ap.add_argument("--teste", nargs="+", metavar="ARG", help="ORIGEM DESTINO IDA [VOLTA]")
    a = ap.parse_args()

    if a.login:
        with SmilesBuscador(headless=False, timeout_seg=120) as b:
            b.login_manual()
        return

    if a.teste:
        o, d, di = a.teste[:3]
        dv = date.fromisoformat(a.teste[3]) if len(a.teste) > 3 else None
        try:
            with SmilesBuscador(headless=False) as b:
                idas, voltas, info = b.buscar_ida_volta(o.upper(), d.upper(), date.fromisoformat(di), dv)
                respostas = b.ultimas_respostas
        except (PrecisaLogin, Bloqueado) as e:
            print(f"\n>>> {type(e).__name__}: {e}")
            return
        print("\nAPI de busca:", info.get("url", "")[:200] or "(não encontrada)")
        for nome, dados in respostas.items():
            print(f"\n### {nome}\n{resumo_estrutura(dados, 1500)}")
        mostrar("IDAS", idas)
        mostrar("VOLTAS", voltas)
        if not info.get("url"):
            print("\nChamadas feitas pela página (últimas 25):")
            for st, m, u in info["trafego"][-25:]:
                print(f"  {st} {m} {u[:160]}")
        return

    ap.print_help()
    sys.exit(1)


if __name__ == "__main__":
    main()
