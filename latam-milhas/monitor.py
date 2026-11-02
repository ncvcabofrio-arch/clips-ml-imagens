#!/usr/bin/env python3
"""Monitor de passagens LATAM com milhas.

Uso:
  python monitor.py                 # roda uma varredura completa das rotas do config.yaml
  python monitor.py --teste GIG LIS 2027-03-05 [Economy]   # uma busca só, imprime tudo
  python monitor.py --testar-telegram
"""
from __future__ import annotations

import argparse
import logging
import random
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import requests
import yaml

from latam import Bloqueado, LatamBuscador, Voo

RAIZ = Path(__file__).parent
DB = RAIZ / "data" / "milhas.db"
LOCK = RAIZ / "data" / "monitor.lock"
(RAIZ / "data").mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler(RAIZ / "data" / "monitor.log")],
)
log = logging.getLogger("monitor")


def carregar_config() -> dict:
    p = RAIZ / "config.yaml"
    if not p.exists():
        sys.exit("Crie o config.yaml:  cp config.example.yaml config.yaml")
    return yaml.safe_load(p.read_text())


# ------------------------------------------------------------------- db ---

def abrir_db() -> sqlite3.Connection:
    DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB)
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS observacoes (
            ts TEXT, origem TEXT, destino TEXT, data TEXT, voo TEXT, tarifa TEXT,
            partida TEXT, chegada TEXT, paradas INT, milhas REAL, moeda TEXT, taxas REAL
        );
        CREATE TABLE IF NOT EXISTS alertas (chave TEXT PRIMARY KEY, milhas REAL, ts TEXT);
        """
    )
    return con


def salvar(con, voos: list[Voo]):
    ts = datetime.now().isoformat(timespec="seconds")
    con.executemany(
        "INSERT INTO observacoes VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [(ts, v.origem, v.destino, v.data, v.voo, v.tarifa, v.partida, v.chegada,
          v.paradas, v.milhas, v.moeda, v.taxas) for v in voos],
    )
    con.commit()


def deve_alertar(con, v: Voo, realertar_se_baixar: bool) -> bool:
    row = con.execute("SELECT milhas FROM alertas WHERE chave=?", (v.chave(),)).fetchone()
    if row is None:
        return True
    return realertar_se_baixar and v.milhas < row[0]


def marcar_alertado(con, v: Voo):
    con.execute("INSERT OR REPLACE INTO alertas VALUES (?,?,?)",
                (v.chave(), v.milhas, datetime.now().isoformat(timespec="seconds")))
    con.commit()


# ------------------------------------------------------------- telegram ---

def telegram(cfg: dict, texto: str):
    tg = cfg.get("telegram") or {}
    if not tg.get("token") or not tg.get("chat_id"):
        log.info("[sem telegram configurado] %s", texto)
        return
    r = requests.post(
        f"https://api.telegram.org/bot{tg['token']}/sendMessage",
        json={"chat_id": tg["chat_id"], "text": texto, "parse_mode": "HTML",
              "disable_web_page_preview": True},
        timeout=20,
    )
    if not r.ok:
        log.error("Falha no Telegram: %s %s", r.status_code, r.text[:200])


def formatar(v: Voo, rota: dict) -> str:
    from latam import url_busca

    hora = v.partida[11:16] if len(v.partida) >= 16 else v.partida
    taxas = f" + taxas {v.taxas:.2f}" if v.taxas else ""
    link = url_busca(v.origem, v.destino, date.fromisoformat(v.data), rota.get("cabine", "Economy"),
                     1)
    return (
        f"✈️ <b>{v.origem} → {v.destino}</b> {v.data} {hora}\n"
        f"{v.voo} · {'direto' if v.paradas == 0 else f'{v.paradas} parada(s)'} · {v.tarifa}\n"
        f"<b>{f'{v.milhas:,.0f}'.replace(',', '.')} milhas</b>{taxas}\n"
        f"<a href=\"{link}\">abrir na LATAM</a>"
    )


# ----------------------------------------------------------------- main ---

def dias(rota: dict):
    ini = rota["data_inicio"]
    fim = rota["data_fim"]
    ini = ini if isinstance(ini, date) else date.fromisoformat(str(ini))
    fim = fim if isinstance(fim, date) else date.fromisoformat(str(fim))
    d = max(ini, date.today())
    while d <= fim:
        yield d
        d += timedelta(days=1)


def varredura(cfg: dict):
    con = abrir_db()
    espera = cfg.get("espera_entre_buscas_seg", [25, 60])
    adultos = int(cfg.get("adultos", 1))
    total, alertas = 0, 0
    with LatamBuscador(headless=bool(cfg.get("headless", False)), proxy=cfg.get("proxy") or "",
                       timeout_seg=int(cfg.get("timeout_busca_seg", 60))) as b:
        primeira = True
        for rota in cfg["rotas"]:
            for dia in dias(rota):
                if not primeira:
                    time.sleep(random.uniform(*espera))
                primeira = False
                voos = b.buscar(rota["origem"], rota["destino"], dia, rota.get("cabine", "Economy"), adultos)
                total += len(voos)
                salvar(con, voos)
                if voos:
                    menor = min(voos, key=lambda v: v.milhas)
                    log.info("  %d tarifas, menor: %.0f %s (%s)", len(voos), menor.milhas, menor.moeda, menor.voo)
                for v in voos:
                    if v.milhas > rota["max_milhas"]:
                        continue
                    if "max_paradas" in rota and v.paradas > rota["max_paradas"]:
                        continue
                    if deve_alertar(con, v, cfg.get("realertar_se_baixar", True)):
                        telegram(cfg, formatar(v, rota))
                        marcar_alertado(con, v)
                        alertas += 1
    log.info("Fim: %d tarifas lidas, %d alertas enviados", total, alertas)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--teste", nargs="+", metavar="ARG", help="ORIGEM DESTINO AAAA-MM-DD [CABINE]")
    ap.add_argument("--testar-telegram", action="store_true")
    a = ap.parse_args()
    (RAIZ / "data").mkdir(exist_ok=True)
    cfg = carregar_config()

    if a.testar_telegram:
        telegram(cfg, "✅ Monitor LATAM milhas conectado!")
        return

    if a.teste:
        o, d, dt = a.teste[:3]
        cab = a.teste[3] if len(a.teste) > 3 else "Economy"
        with LatamBuscador(headless=bool(cfg.get("headless", False)), proxy=cfg.get("proxy") or "") as b:
            voos = b.buscar(o.upper(), d.upper(), date.fromisoformat(dt), cab, int(cfg.get("adultos", 1)))
        for v in sorted(voos, key=lambda v: v.milhas):
            print(f"{v.voo:<14} {v.partida[11:16]:>5} {v.paradas}p {v.tarifa:<14} {v.milhas:>10,.0f} {v.moeda} taxas={v.taxas}")
        print(f"{len(voos)} tarifas")
        return

    # Evita duas execuções ao mesmo tempo (cron sobreposto)
    try:
        fd = LOCK.open("x")
    except FileExistsError:
        if time.time() - LOCK.stat().st_mtime < 6 * 3600:
            log.warning("Já existe uma varredura rodando (%s). Saindo.", LOCK)
            return
        LOCK.unlink()
        fd = LOCK.open("x")
    try:
        varredura(cfg)
    except Bloqueado as e:
        log.error("BLOQUEADO: %s", e)
        telegram(cfg, f"⚠️ Monitor LATAM bloqueado pelo anti-bot: {e}\nVeja data/debug na VPS.")
        sys.exit(2)
    finally:
        fd.close()
        LOCK.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
