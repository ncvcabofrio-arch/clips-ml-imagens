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
from smiles import CDP_URL, SmilesBuscador  # noqa: E402


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
    ap.add_argument("--espiar", action="store_true",
                    help="só observa: você faz uma busca à mão no Chrome da Smiles e o robô anota as chamadas")
    ap.add_argument("--teste", nargs="+", metavar="ARG", help="ORIGEM DESTINO IDA [VOLTA]")
    ap.add_argument("--sem-chrome-aberto", action="store_true",
                    help="não conectar no Chrome aberto na porta 9222 (abre um navegador próprio)")
    a = ap.parse_args()

    if a.login:
        with SmilesBuscador(headless=False, timeout_seg=120) as b:
            b.login_manual()
        return

    if a.espiar:
        import json as _json
        from playwright.sync_api import sync_playwright
        from smiles import DEBUG_DIR
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        vistos = []

        def on_resp(r):
            try:
                if r.request.resource_type not in ("xhr", "fetch"):
                    return
                u = r.url
                if "smiles" not in u or any(x in u for x in ("member", "plans", "cobranded", "voucher",
                                                             "ldrelay", "hf-contents", "check-env", "liferay")):
                    return
                vistos.append((r.status, r.request.method, u))
                try:
                    dados = r.json()
                except Exception:  # noqa: BLE001
                    return
                if any(k in u.lower() for k in ("search", "flight", "availability", "fare")):
                    nome = "ESPIAO-" + "_".join(u.split("?")[0].rstrip("/").split("/")[-3:])
                    (DEBUG_DIR / f"{nome}.json").write_text(_json.dumps(dados, ensure_ascii=False, indent=1))
                    print(f"\n### capturei {r.request.method} {u[:200]}\n{resumo_estrutura(dados, 2500)}")
            except Exception:  # noqa: BLE001
                pass

        with sync_playwright() as pw:
            try:
                browser = pw.chromium.connect_over_cdp(CDP_URL)
            except Exception as e:  # noqa: BLE001
                print(f">>> Não achei o Chrome da Smiles aberto (porta 9222): {str(e).splitlines()[0]}")
                return
            ctx = browser.contexts[0]
            ctx.on("response", on_resp)
            for pg in ctx.pages:
                pg.on("response", on_resp)
            ctx.on("page", lambda pg: pg.on("response", on_resp))
            print("=" * 70)
            print("ESPIANDO. No Chrome da Smiles, faça uma busca normal à mão:")
            print("  Ida e volta, Rio de Janeiro (GIG) → Orlando (MCO), 22/02/2027 a 04/03/2027, 1 adulto")
            print("Espere os voos aparecerem na tela. Depois volte AQUI e aperte Enter.")
            print("=" * 70)
            input()
            print("\nURL da página de resultados:")
            for pg in ctx.pages:
                if "smiles" in pg.url:
                    print("  ", pg.url[:400])
            print(f"\nChamadas da Smiles anotadas ({len(vistos)}):")
            for st, m, u in vistos[-40:]:
                print(f"  {st} {m} {u[:200]}")
        return

    if a.teste:
        o, d, di = a.teste[:3]
        dv = date.fromisoformat(a.teste[3]) if len(a.teste) > 3 else None
        try:
            with SmilesBuscador(headless=False, cdp=None if a.sem_chrome_aberto else CDP_URL) as b:
                idas, voltas, info = b.buscar_ida_volta(o.upper(), d.upper(), date.fromisoformat(di), dv)
                respostas = b.ultimas_respostas
        except (PrecisaLogin, Bloqueado, RuntimeError) as e:
            print(f"\n>>> {type(e).__name__}: {e}")
            return
        print("\nAPI de busca:", info.get("url", "")[:200] or "(não encontrada)")
        for nome, dados in respostas.items():
            print(f"\n### {nome}\n{resumo_estrutura(dados, 1500)}")
        mostrar("IDAS", idas)
        mostrar("VOLTAS", voltas)
        if not info.get("url"):
            proprias = [t for t in info["trafego"] if ("smiles" in t[2] or "voegol" in t[2])
                        and not any(x in t[2] for x in ("member", "plans", "cobranded", "voxus"))]
            print(f"\nChamadas da Smiles feitas pela página ({len(proprias)}):")
            for st, m, u in proprias[-40:]:
                print(f"  {st} {m} {u[:160]}")
            print("\nVeja o print da tela:  open data/debug-smiles/*semresposta.png")
        return

    ap.print_help()
    sys.exit(1)


if __name__ == "__main__":
    main()
