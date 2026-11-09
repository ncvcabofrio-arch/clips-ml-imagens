#!/usr/bin/env python3
"""Monitor de passagens LATAM com milhas.

Uso:
  python monitor.py                 # roda uma varredura completa das rotas do config.yaml
  python monitor.py --teste GIG LIS 2027-03-05 [Economy]   # uma busca só, imprime tudo
  python monitor.py --testar-telegram
"""
from __future__ import annotations

import argparse
import os
import logging
import random
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import requests
import yaml

from latam import Bloqueado, LatamBuscador, PrecisaLogin, Voo

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
        CREATE TABLE IF NOT EXISTS verificados (origem TEXT, destino TEXT, ida TEXT, volta TEXT, ts TEXT);
        CREATE TABLE IF NOT EXISTS ida_volta (
            ts TEXT, origem TEXT, destino TEXT, ida TEXT, volta TEXT,
            voo_ida TEXT, tarifa_ida TEXT, milhas_ida REAL, taxas_ida REAL,
            voo_volta TEXT, tarifa_volta TEXT, milhas_volta REAL, taxas_volta REAL
        );
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


PAUSA = RAIZ / "data" / "pausa_ate.txt"


def pausar_por_bloqueio(cfg: dict):
    ate = datetime.now() + timedelta(hours=float(cfg.get("pausa_bloqueio_horas", 3)))
    PAUSA.write_text(ate.isoformat(timespec="seconds"))


def em_pausa() -> datetime | None:
    try:
        ate = datetime.fromisoformat(PAUSA.read_text().strip())
    except Exception:  # noqa: BLE001
        return None
    return ate if ate > datetime.now() else None


def salvar_rt(con, vi: Voo, vv: Voo):
    con.execute("INSERT INTO ida_volta VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (datetime.now().isoformat(timespec="seconds"), vi.origem, vi.destino, vi.data, vv.data,
                 vi.voo, vi.tarifa, vi.milhas, vi.taxas, vv.voo, vv.tarifa, vv.milhas, vv.taxas))
    con.commit()


def melhor_salvo(con, de: str, para: str, dia: str, cabine: str, max_paradas):
    """Menor preço só-ida salvo no banco para um dia (coleta mais recente)."""
    linhas = con.execute(
        "SELECT ts, voo, tarifa, paradas, milhas, taxas FROM observacoes "
        "WHERE origem=? AND destino=? AND data=? AND moeda='LOYALTY_POINTS'", (de, para, dia)).fetchall()
    if not linhas:
        return None
    ult = max(r[0] for r in linhas)
    best = None
    for ts, voo, tarifa, paradas, milhas, taxas in linhas:
        if ts != ult:
            continue
        exe = "BUSINESS" in tarifa.upper() or "PREMIUM" in tarifa.upper()
        if (cabine == "economica" and exe) or (cabine == "executiva" and not exe):
            continue
        if max_paradas is not None and paradas > int(max_paradas):
            continue
        if best is None or milhas < best[0]:
            best = (milhas, taxas or 0, voo, tarifa)
    return best


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


def _data(x) -> date:
    return x if isinstance(x, date) else date.fromisoformat(str(x))


def _filtrar(voos: list[Voo], cabine: str, max_paradas) -> list[Voo]:
    ok = []
    for v in voos:
        if cabine in ("economica", "executiva") and v.cabine != cabine:
            continue
        if max_paradas is not None and v.paradas > int(max_paradas):
            continue
        ok.append(v)
    return ok


def _mil(x: float) -> str:
    return f"{x:,.0f}".replace(",", ".")


def _cabine_url(cabine: str) -> str:
    return "Business" if cabine == "executiva" else "Economy"


def varrer_viagem(b: LatamBuscador, con, cfg: dict, vg: dict, pausa) -> int:
    """Ida e volta: busca cada dia de ida e de volta e combina os pares."""
    nome = vg.get("nome") or f"{vg['origem']}-{vg['destino']}"
    o, d = vg["origem"].upper(), vg["destino"].upper()
    cabine = str(vg.get("cabine", "economica")).lower()
    duracoes = [int(n) for n in (vg.get("dias_de_viagem") or [10])]
    ini, fim = max(_data(vg["ida_de"]), date.today() + timedelta(days=1)), _data(vg["ida_ate"])
    idas = [ini + timedelta(days=i) for i in range((fim - ini).days + 1)]
    voltas = sorted({x + timedelta(days=n) for x in idas for n in duracoes})
    adultos = int(cfg.get("adultos", 1))
    log.info("[%s] %d datas de ida, %s dias de viagem", nome, len(idas), "/".join(map(str, duracoes)))

    pares = []
    filtro = lambda vs: _filtrar(vs, cabine, vg.get("max_paradas"))  # noqa: E731
    if str(vg.get("modo", "ida_e_volta")) == "ida_e_volta":
        # Uma busca de IDA E VOLTA por par de datas (como no site), em fatias:
        # cada rodada busca só os pares verificados há mais tempo, até o limite.
        revalidar = float(cfg.get("revalidar_horas", 20))
        limite_buscas = int(cfg.get("max_buscas_por_rodada", 10))
        evitar = [(_data(a), _data(b)) for a, b in (cfg.get("evitar_periodos") or [])]
        todos = [(x, x + timedelta(days=n), n) for x in idas for n in duracoes
                 if not any(not (x + timedelta(days=n) < a or x > b) for a, b in evitar)]
        if evitar:
            log.info("[%s] pulando viagens que encostam em %s", nome,
                     ", ".join(f"{_br(a.isoformat())}–{_br(b.isoformat())}" for a, b in evitar))
        ult: dict = {}
        for tabela in ("verificados", "ida_volta"):
            for r in con.execute(f"SELECT ida, volta, MAX(ts) FROM {tabela} WHERE origem=? AND destino=? "
                                 "GROUP BY ida, volta", (o, d)):
                ult[(r[0], r[1])] = max(ult.get((r[0], r[1]), ""), r[2])
        agora = datetime.now()
        pendentes = [p for p in todos
                     if (p[0].isoformat(), p[1].isoformat()) not in ult
                     or agora - datetime.fromisoformat(ult[(p[0].isoformat(), p[1].isoformat())])
                     > timedelta(hours=revalidar)]
        pendentes.sort(key=lambda p: ult.get((p[0].isoformat(), p[1].isoformat()), ""))
        log.info("[%s] %d de %d pares precisam de busca; nesta rodada: até %d",
                 nome, len(pendentes), len(todos), limite_buscas)
        max_freios = int(cfg.get("max_bloqueios_seguidos", 3))
        espera_freio = cfg.get("espera_apos_bloqueio_min", [3.5, 4.5])
        freios, erros = 0, 0
        parar = False
        pausa_longa = cfg.get("pausa_longa_min", [25, 35])
        max_pausas_longas = int(cfg.get("max_pausas_longas", 6))
        pausas_longas = 0
        fila = list(pendentes[:limite_buscas])
        parar = False
        while fila and not parar:
            x, y, n = fila[0]
            pausa()
            try:
                lista_i, lista_v, info = b.buscar_ida_volta(o, d, x, y, _cabine_url(cabine), adultos, filtro=filtro)
                freios, erros = 0, 0
            except Bloqueado as e:
                freios += 1
                if freios >= max_freios:
                    if pausas_longas < max_pausas_longas:
                        pausas_longas += 1
                        minutos = random.uniform(*pausa_longa)
                        log.warning("LATAM bloqueou %d vezes seguidas. Pausa longa de %.0f min e continuo sozinho "
                                    "(%d/%d). Pode deixar rodando.", freios, minutos, pausas_longas, max_pausas_longas)
                        time.sleep(minutos * 60)
                        freios = 0
                        continue  # tenta a mesma data de novo
                    log.error("BLOQUEADO %d vezes seguidas (%s), já com %d pausas longas — pausando por %s h",
                              freios, e, pausas_longas, cfg.get("pausa_bloqueio_horas", 3))
                    pausar_por_bloqueio(cfg)
                    telegram(cfg, f"⚠️ LATAM bloqueou {freios}x seguidas. Pausando {cfg.get('pausa_bloqueio_horas', 3)} h.")
                    break
                minutos = random.uniform(*espera_freio)
                log.warning("Freio da LATAM (403) em %s → %s. Esperando %.0f min e tentando de novo (%d/%d)",
                            x, y, minutos, freios, max_freios)
                time.sleep(minutos * 60)
                continue  # tenta a mesma data
            except PrecisaLogin:
                raise
            except Exception as e:  # noqa: BLE001  (rede, página que não carregou, etc.)
                msg = str(e)
                if any(t in msg for t in ("Connection closed", "Target page, context or browser has been closed",
                                          "Browser has been closed", "has been closed")):
                    raise  # navegador morreu: sobe para reabrir e continuar
                erros += 1
                log.error("Erro inesperado em %s → %s: %s: %s", x, y, type(e).__name__,
                          str(e).splitlines()[0][:200] if str(e) else "")
                fila.pop(0)
                if erros >= 3:
                    raise RuntimeError("3 erros seguidos")  # reabre o navegador e continua
                continue
            fila.pop(0)
            if not info.get("url_idas"):
                log.warning("  %s → %s: a página não trouxe os voos; fica para a próxima rodada", x, y)
                continue
            if info.get("metodo") == "api" and not lista_v:
                log.warning("  %s → %s: o clique na ida falhou e o plano B não trouxe voltas; "
                            "fica para a próxima rodada", x, y)
                continue
            con.execute("INSERT INTO verificados VALUES (?,?,?,?,?)",
                        (o, d, x.isoformat(), y.isoformat(), datetime.now().isoformat(timespec="seconds")))
            con.commit()
            vi = info.get("ida_escolhida")
            bons_v = filtro(lista_v)
            if vi and bons_v:
                vv = min(bons_v, key=lambda v: v.milhas)
                salvar_rt(con, vi, vv)
                log.info("  %s → %s: ida %s + volta %s = %s pts", x, y, _mil(vi.milhas), _mil(vv.milhas),
                         _mil(vi.milhas + vv.milhas))
            else:
                log.info("  %s → %s: nada disponível com os filtros", x, y)
        # Combinações: usa o preço mais recente salvo de cada par (desta e de rodadas anteriores)
        validade = timedelta(hours=float(cfg.get("validade_preco_horas", 72)))
        for x, y, n in todos:
            r = con.execute(
                "SELECT ts, voo_ida, tarifa_ida, milhas_ida, taxas_ida, voo_volta, tarifa_volta, milhas_volta, "
                "taxas_volta FROM ida_volta WHERE origem=? AND destino=? AND ida=? AND volta=? "
                "ORDER BY ts DESC LIMIT 1", (o, d, x.isoformat(), y.isoformat())).fetchone()
            if not r or agora - datetime.fromisoformat(r[0]) > validade:
                continue
            vi = Voo(o, d, x.isoformat(), r[1], "", "", 0, None, r[2], r[3], "LOYALTY_POINTS", r[4])
            vv = Voo(d, o, y.isoformat(), r[5], "", "", 0, None, r[6], r[7], "LOYALTY_POINTS", r[8])
            pares.append((r[3] + r[7], (r[4] or 0) + (r[8] or 0), vi, vv, n))
    else:
        # Modo antigo: cada trecho como só ida, depois combina
        melhor_ida: dict[date, Voo] = {}
        melhor_volta: dict[date, Voo] = {}
        for sentido, datas, de, para, alvo in (("ida", idas, o, d, melhor_ida),
                                               ("volta", voltas, d, o, melhor_volta)):
            for dia in datas:
                pausa()
                voos = b.buscar(de, para, dia, _cabine_url(cabine), adultos)
                salvar(con, voos)
                bons = filtro(voos)
                if bons:
                    alvo[dia] = min(bons, key=lambda v: v.milhas)
                    log.info("  %s %s: menor %s pts (%s, %s)", sentido, dia, _mil(alvo[dia].milhas),
                             alvo[dia].voo, alvo[dia].tarifa)
                else:
                    log.info("  %s %s: nada disponível com os filtros", sentido, dia)
        for x in idas:
            for n in duracoes:
                y = x + timedelta(days=n)
                if x in melhor_ida and y in melhor_volta:
                    vi, vv = melhor_ida[x], melhor_volta[y]
                    pares.append((vi.milhas + vv.milhas, (vi.taxas or 0) + (vv.taxas or 0), vi, vv, n))
    pares.sort(key=lambda p: p[0])

    log.info("[%s] %d combinações ida+volta encontradas", nome, len(pares))
    for tot, tx, vi, vv, n in pares[:10]:
        log.info("   %s → %s (%dd): %s pts + R$ %.2f", vi.data, vv.data, n, f"{tot:,.0f}", tx)

    limite = float(vg.get("max_milhas_total") or 0)
    alertas = 0
    for tot, tx, vi, vv, n in pares:
        if limite and tot > limite:
            break
        chave = f"{nome}|{vi.data}|{vv.data}"
        row = con.execute("SELECT milhas FROM alertas WHERE chave=?", (chave,)).fetchone()
        if row is not None and not (cfg.get("realertar_se_baixar", True) and tot < row[0]):
            continue
        telegram(cfg, _msg_par(nome, tot, tx, vi, vv, n, cabine))
        con.execute("INSERT OR REPLACE INTO alertas VALUES (?,?,?)",
                    (chave, tot, datetime.now().isoformat(timespec="seconds")))
        con.commit()
        alertas += 1

    if pares and cfg.get("resumo_telegram", True):
        linhas = [f"📊 <b>{nome}</b> — 5 melhores ida+volta agora:"]
        for tot, tx, vi, vv, n in pares[:5]:
            linhas.append(f"• {_br(vi.data)} → {_br(vv.data)}: <b>{_mil(tot)}</b> pts + R$ {tx:.0f}")
        telegram(cfg, "\n".join(linhas))
    return alertas


def _br(iso: str) -> str:
    return f"{iso[8:10]}/{iso[5:7]}"


def _msg_par(nome, tot, tx, vi: Voo, vv: Voo, n, cabine) -> str:
    from latam import url_busca

    def linha(v: Voo, rot):
        p = f"{v.voo.count('+')} parada(s)" if "+" in v.voo else "direto"
        return (f"{rot} {_br(v.data)} · {v.voo} · {p} · {v.tarifa} · "
                f"{_mil(v.milhas)} pts + R$ {(v.taxas or 0):.0f}")
    li = url_busca(vi.origem, vi.destino, date.fromisoformat(vi.data), _cabine_url(cabine), 1)
    lv = url_busca(vv.origem, vv.destino, date.fromisoformat(vv.data), _cabine_url(cabine), 1)
    return (f"✈️ <b>{nome}</b> — {n} dias\n"
            f"<b>Total: {_mil(tot)} pts + R$ {tx:.0f}</b>\n"
            f"{linha(vi, '🛫')}\n{linha(vv, '🛬')}\n"
            f"<a href=\"{li}\">ver ida</a> · <a href=\"{lv}\">ver volta</a>")


def relatorio(cfg: dict, top: int = 15):
    """Mostra o ranking com os preços já salvos no banco (sem acessar a LATAM)."""
    con = abrir_db()
    for vg in cfg.get("viagens") or []:
        nome = vg.get("nome") or f"{vg['origem']}-{vg['destino']}"
        o, d = vg["origem"].upper(), vg["destino"].upper()
        linhas = con.execute(
            "SELECT ida, volta, voo_ida, tarifa_ida, milhas_ida, taxas_ida, voo_volta, tarifa_volta, milhas_volta, "
            "taxas_volta, MAX(ts) FROM ida_volta WHERE origem=? AND destino=? GROUP BY ida, volta", (o, d)).fetchall()
        feitos = con.execute("SELECT COUNT(DISTINCT ida||volta) FROM verificados WHERE origem=? AND destino=?",
                             (o, d)).fetchone()[0]
        linhas.sort(key=lambda r: r[4] + r[8])
        print(f"\n=== {nome} — busca IDA E VOLTA: {len(linhas)} pares com preço "
              f"({feitos} pares já buscados) ===")
        print(f"  {'ida → volta':<13} {'total':>9}   {'ida':>9} {'volta':>9}   taxas   voos (ida | volta)        coletado")
        for r in linhas[:top]:
            print(f"  {_br(r[0])} → {_br(r[1])} {_mil(r[4] + r[8]):>9}   {_mil(r[4]):>9} {_mil(r[8]):>9}   "
                  f"R$ {(r[5] or 0) + (r[9] or 0):>4.0f}   {r[2]} | {r[6]}   {r[10][5:16].replace('T', ' ')}")
    print("\n(abaixo: combinação de trechos SÓ IDA, se houver)")
    for vg in cfg.get("viagens") or []:
        nome = vg.get("nome") or f"{vg['origem']}-{vg['destino']}"
        o, d = vg["origem"].upper(), vg["destino"].upper()
        cabine = str(vg.get("cabine", "economica")).lower()
        mp = vg.get("max_paradas")
        duracoes = [int(n) for n in (vg.get("dias_de_viagem") or [10])]

        def melhor_por_dia(de, para):
            q = ("SELECT data, voo, tarifa, partida, paradas, milhas, taxas, ts FROM observacoes "
                 "WHERE origem=? AND destino=? AND moeda='LOYALTY_POINTS'")
            ult: dict[str, str] = {}
            linhas = con.execute(q, (de, para)).fetchall()
            for r in linhas:  # só a coleta mais recente de cada dia
                ult[r[0]] = max(ult.get(r[0], ""), r[7])
            best: dict[str, tuple] = {}
            for data_, voo, tarifa, partida, paradas, milhas, taxas, ts in linhas:
                if ts != ult[data_]:
                    continue
                exe = "BUSINESS" in tarifa.upper() or "PREMIUM" in tarifa.upper()
                if cabine == "economica" and exe or cabine == "executiva" and not exe:
                    continue
                if mp is not None and paradas > int(mp):
                    continue
                if data_ not in best or milhas < best[data_][0]:
                    best[data_] = (milhas, taxas or 0, voo, tarifa, partida)
            return best

        idas, voltas = melhor_por_dia(o, d), melhor_por_dia(d, o)
        pares = []
        for di, (mi, ti, vi, fi, pi) in idas.items():
            for n in duracoes:
                dv = (date.fromisoformat(di) + timedelta(days=n)).isoformat()
                if dv in voltas:
                    mv, tv, vv, fv, pv = voltas[dv]
                    pares.append((mi + mv, ti + tv, di, dv, n, f"{vi} {pi[11:16]}", f"{vv} {pv[11:16]}"))
        pares.sort()
        print(f"\n=== {nome}: {len(idas)} dias de ida e {len(voltas)} de volta no banco; "
              f"{len(pares)} combinações ({'/'.join(map(str, duracoes))} dias) ===")
        for tot, tx, di, dv, n, a, b in pares[:top]:
            print(f"  {_br(di)} → {_br(dv)} ({n:>2}d)  {_mil(tot):>9} pts + R$ {tx:>6.0f}   ida {a} | volta {b}")


def comparar(cfg: dict, o: str, d: str, inicio: date, n_datas: int, dias_viagem: int):
    """Para N datas: busca IDA E VOLTA e compara com a soma dos trechos SÓ IDA."""
    vg = (cfg.get("viagens") or [{}])[0]
    cabine = str(vg.get("cabine", "economica")).lower()
    mp = vg.get("max_paradas", 1)
    filtro = lambda vs: _filtrar(vs, cabine, mp)  # noqa: E731
    adultos = int(cfg.get("adultos", 1))
    espera = cfg.get("espera_entre_buscas_seg", [20, 45])
    con = abrir_db()
    linhas = []
    primeira = [True]

    def pausa():
        if not primeira[0]:
            time.sleep(random.uniform(*espera))
        primeira[0] = False

    def so_ida(b, de, para, dia):
        r = melhor_salvo(con, de, para, dia.isoformat(), cabine, mp)
        if r is None:
            pausa()
            voos = b.buscar(de, para, dia, _cabine_url(cabine), adultos)
            salvar(con, voos)
            bons = filtro(voos)
            if bons:
                m = min(bons, key=lambda v: v.milhas)
                r = (m.milhas, m.taxas or 0, m.voo, m.tarifa)
        return r

    with LatamBuscador(headless=bool(cfg.get("headless", False)), proxy=cfg.get("proxy") or "",
                       timeout_seg=int(cfg.get("timeout_busca_seg", 60))) as b:
        for i in range(n_datas):
            x = inicio + timedelta(days=i)
            y = x + timedelta(days=dias_viagem)
            pausa()
            _, lv, info = b.buscar_ida_volta(o, d, x, y, _cabine_url(cabine), adultos, filtro=filtro)
            vi = info.get("ida_escolhida")
            bv = filtro(lv)
            rt = None
            if vi and bv:
                vv = min(bv, key=lambda v: v.milhas)
                salvar_rt(con, vi, vv)
                rt = (vi.milhas, vv.milhas)
            ow_i = so_ida(b, o, d, x)
            ow_v = so_ida(b, d, o, y)
            linhas.append((x, y, rt, ow_i, ow_v, info.get("status_voltas")))
            log.info("  %s → %s  RT=%s  ida=%s  volta=%s", x, y, rt, ow_i and ow_i[0], ow_v and ow_v[0])

    def f(v):
        return _mil(v) if v is not None else "—"
    print()
    print(f"{'datas':<13} | {'IDA E VOLTA (busca RT)':^29} | {'SÓ IDA (cada trecho)':^29} | diferença")
    print(f"{'':<13} | {'ida':>9} {'volta':>9} {'total':>9} | {'ida':>9} {'volta':>9} {'total':>9} |")
    for x, y, rt, oi, ov, st in linhas:
        rti, rtv = rt if rt else (None, None)
        rtt = rti + rtv if rt else None
        oii, ovv = (oi[0] if oi else None), (ov[0] if ov else None)
        owt = oii + ovv if oi and ov else None
        dif = f"{_mil(owt - rtt)} a menos no RT" if rtt and owt and owt > rtt else (
            f"{_mil(rtt - owt)} a mais no RT" if rtt and owt and rtt > owt else ("igual" if rtt and owt else "—"))
        print(f"{_br(x.isoformat())} → {_br(y.isoformat())} | {f(rti):>9} {f(rtv):>9} {f(rtt):>9} | "
              f"{f(oii):>9} {f(ovv):>9} {f(owt):>9} | {dif}" + (f"  (status voltas {st})" if not rt else ""))


def _horas(m) -> str:
    return f"{m // 60}h{m % 60:02d}" if m else "?"


def calcular_combos(cfg: dict) -> dict:
    """Monta as melhores combinações com as respostas de IDA E VOLTA já salvas
    (data/debug/RT-*-idas.json e *-voltas.json), sem acessar a LATAM.
    Combina 9/10/11 dias, evita o período configurado (ex.: Carnaval) e mostra
    o mais barato, o mais rápido e um meio-termo para cada par de datas."""
    import glob
    import json as _json
    import re as _re
    from latam import DEBUG_DIR, parse_ofertas

    vg = (cfg.get("viagens") or [{}])[0]
    o, d = vg["origem"].upper(), vg["destino"].upper()
    cabine = str(vg.get("cabine", "economica")).lower()
    mp = vg.get("max_paradas")
    duracoes = [int(n) for n in (cfg.get("melhores_dias") or [9, 10, 11])]
    evitar = [(_data(a), _data(b)) for a, b in (cfg.get("evitar_periodos") or [])]
    milhas_tenho = float(cfg.get("milhas_disponiveis") or 0)
    pax = int(cfg.get("passageiros") or 1)
    folga_h = float(cfg.get("folga_horas_rapido", 2))
    volta_ate = _data(cfg["melhores_volta_ate"]) if cfg.get("melhores_volta_ate") else None
    ida_de = _data(cfg["melhores_ida_de"]) if cfg.get("melhores_ida_de") else None

    def carregar(sufixo, de, para):
        por_dia: dict[date, tuple[float, list]] = {}
        padrao = str(DEBUG_DIR / f"RT-{o}-{d}-*-{sufixo}.json")
        for f in glob.glob(padrao):
            m = _re.search(r"RT-\w+-\w+-(\d{4}-\d{2}-\d{2})-(\d{4}-\d{2}-\d{2})-", f)
            if not m:
                continue
            dia = date.fromisoformat(m.group(1) if sufixo == "idas" else m.group(2))
            mt = os.path.getmtime(f)
            if dia in por_dia and por_dia[dia][0] >= mt:
                continue
            try:
                voos = parse_ofertas(_json.load(open(f)), de, para, dia)
            except Exception:  # noqa: BLE001
                continue
            # descarta voo que mostra menos assentos que o número de passageiros
            voos = [v for v in _filtrar(voos, cabine, mp)
                    if v.duracao_min and (v.assentos is None or v.assentos >= pax)]
            if voos:
                por_dia[dia] = (mt, voos)
        return {k: v[1] for k, v in por_dia.items()}

    idas, voltas = carregar("idas", o, d), carregar("voltas", d, o)

    def escolhas(vs):
        barato = min(vs, key=lambda v: (v.milhas, v.duracao_min))
        rapido = min(vs, key=lambda v: (v.duracao_min, v.milhas))
        perto = [v for v in vs if v.duracao_min <= rapido.duracao_min + folga_h * 60]
        meio = min(perto, key=lambda v: (v.milhas, v.duracao_min))
        ate1 = [v for v in vs if v.paradas <= 1]
        uma = min(ate1, key=lambda v: (v.milhas, v.duracao_min)) if ate1 else None
        return barato, rapido, meio, uma

    combos = []
    for x, li in idas.items():
        for n in duracoes:
            y = x + timedelta(days=n)
            if y not in voltas:
                continue
            if any(not (y < a or x > b) for a, b in evitar):
                continue
            if (volta_ate and y > volta_ate) or (ida_de and x < ida_de):
                continue
            bi, ri, mi, ui = escolhas(li)
            bv, rv, mv, uv = escolhas(voltas[y])
            c = {"x": x, "y": y, "n": n, "barato": (bi, bv), "rapido": (ri, rv), "meio": (mi, mv)}
            if ui and uv:
                c["uma_parada"] = (ui, uv)
            combos.append(c)

    return {"combos": combos, "idas": idas, "voltas": voltas, "pax": pax, "milhas_tenho": milhas_tenho,
            "duracoes": duracoes, "evitar": evitar, "volta_ate": volta_ate, "folga_h": folga_h}



def melhores(cfg: dict, top: int = 12):
    """Mostra no terminal as melhores combinações (ver calcular_combos)."""
    r = calcular_combos(cfg)
    combos, idas, voltas = r["combos"], r["idas"], r["voltas"]
    pax, milhas_tenho, duracoes = r["pax"], r["milhas_tenho"], r["duracoes"]
    evitar, volta_ate, folga_h = r["evitar"], r["volta_ate"], r["folga_h"]

    def linha(c, chave):
        vi, vv = c[chave]
        tot = vi.milhas + vv.milhas
        tx = (vi.taxas or 0) + (vv.taxas or 0)
        cabe = ("✅" if tot * pax <= milhas_tenho else "❌") if milhas_tenho else ""
        tot_g = f" ({pax}x = {_mil(tot * pax)} {cabe})" if pax > 1 else ""
        return (f"  {_br(c['x'].isoformat())} → {_br(c['y'].isoformat())} ({c['n']:>2}d) {_mil(tot):>9}/pessoa{tot_g} "
                f"+ R$ {tx * pax:>5.0f} taxas | "
                f"ida {_horas(vi.duracao_min)} {vi.paradas}p {vi.partida[11:16]} {vi.voo} | "
                f"volta {_horas(vv.duracao_min)} {vv.paradas}p {vv.partida[11:16]} {vv.voo}")

    def total(c, chave):
        return c[chave][0].milhas + c[chave][1].milhas

    def tempo(c, chave):
        return c[chave][0].duracao_min + c[chave][1].duracao_min

    if pax > 1:
        print(f"\n{pax} passageiros, {_mil(milhas_tenho)} milhas disponíveis = até {_mil(milhas_tenho / pax)} por pessoa. "
              f"Voos com menos de {pax} assentos visíveis foram descartados.")
    print(f"\nDados: {len(idas)} datas de ida e {len(voltas)} datas de volta salvas; "
          f"{len(combos)} combinações de {'/'.join(map(str, duracoes))} dias"
          + (f", volta até {_br(volta_ate.isoformat())}" if volta_ate else "")
          + (f" fora de {', '.join(f'{_br(a.isoformat())}–{_br(b.isoformat())}' for a, b in evitar)}" if evitar else ""))

    print(f"\n=== 1) MAIS BARATAS (qualquer duração de voo) ===")
    for c in sorted(combos, key=lambda c: (total(c, "barato"), tempo(c, "barato")))[:top]:
        print(linha(c, "barato"))

    print(f"\n=== 2) MEIO-TERMO: voos até {folga_h:.0f}h mais longos que o mais rápido do dia, pelo menor preço ===")
    for c in sorted(combos, key=lambda c: (total(c, "meio"), tempo(c, "meio")))[:top]:
        print(linha(c, "meio"))

    print("\n=== 3) MAIS RÁPIDAS (menor tempo somado de ida + volta) ===")
    for c in sorted(combos, key=lambda c: (tempo(c, "rapido"), total(c, "rapido")))[:top]:
        print(linha(c, "rapido"))
    print("\nObs.: preços buscados para 1 adulto; para 4 pode mudar. Confirme no site com 4 passageiros.")
    print("Obs.: 9 e 11 dias juntam a ida de uma busca com a volta de outra; confirme o preço no site antes de emitir.")


def varredura(cfg: dict):
    ate = em_pausa()
    if ate:
        log.warning("Em pausa por bloqueio da LATAM até %s. Nada a fazer agora.", ate.strftime("%d/%m %H:%M"))
        return
    con = abrir_db()
    espera = cfg.get("espera_entre_buscas_seg", [60, 120])
    adultos = int(cfg.get("adultos", 1))
    total, alertas = 0, 0
    estado = {"primeira": True}

    def pausa():
        if not estado["primeira"]:
            time.sleep(random.uniform(*espera))
        estado["primeira"] = False

    with LatamBuscador(headless=bool(cfg.get("headless", False)), proxy=cfg.get("proxy") or "",
                       timeout_seg=int(cfg.get("timeout_busca_seg", 60))) as b:
        for vg in cfg.get("viagens") or []:
            alertas += varrer_viagem(b, con, cfg, vg, pausa)
        for rota in cfg.get("rotas") or []:
            for dia in dias(rota):
                pausa()
                voos = b.buscar(rota["origem"], rota["destino"], dia, rota.get("cabine", "Economy"), adultos)
                total += len(voos)
                salvar(con, voos)
                for v in voos:
                    if v.milhas > rota["max_milhas"]:
                        continue
                    if "max_paradas" in rota and v.paradas > rota["max_paradas"]:
                        continue
                    if deve_alertar(con, v, cfg.get("realertar_se_baixar", True)):
                        telegram(cfg, formatar(v, rota))
                        marcar_alertado(con, v)
                        alertas += 1
    log.info("Fim da varredura: %d alertas enviados", alertas)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--teste", nargs="+", metavar="ARG", help="ORIGEM DESTINO AAAA-MM-DD [CABINE]")
    ap.add_argument("--testar-telegram", action="store_true")
    ap.add_argument("--teste-rt", nargs="+", metavar="ARG",
                    help="ORIGEM DESTINO AAAA-MM-DD(ida) AAAA-MM-DD(volta) [CABINE]")
    ap.add_argument("--relatorio", action="store_true",
                    help="mostra as melhores combinações com os preços já coletados (sem acessar a LATAM)")
    ap.add_argument("--comparar", nargs="+", metavar="ARG",
                    help="ORIGEM DESTINO AAAA-MM-DD N_DATAS [DIAS_VIAGEM]: compara ida-e-volta x só ida")
    ap.add_argument("--painel", action="store_true", help="gera data/painel.html com gráficos do histórico")
    ap.add_argument("--volta-ate", metavar="AAAA-MM-DD", help="com --melhores: só voltas até essa data")
    ap.add_argument("--melhores", action="store_true",
                    help="monta as melhores combinações (9/10/11 dias, mais barato/rápido) com os dados já salvos")
    ap.add_argument("--login", action="store_true", help="abre o navegador para você fazer login na LATAM Pass")
    a = ap.parse_args()
    (RAIZ / "data").mkdir(exist_ok=True)
    cfg = carregar_config()

    if a.testar_telegram:
        telegram(cfg, "✅ Monitor LATAM milhas conectado!")
        return

    if a.login:
        with LatamBuscador(headless=False, proxy=cfg.get("proxy") or "", timeout_seg=120) as b:
            b.login_manual()
        return

    if a.comparar:
        o, d, ini, n = a.comparar[:4]
        dv = int(a.comparar[4]) if len(a.comparar) > 4 else 10
        try:
            comparar(cfg, o.upper(), d.upper(), date.fromisoformat(ini), int(n), dv)
        except (PrecisaLogin, Bloqueado) as e:
            print(f"\n>>> {type(e).__name__}: {e}")
        return

    if a.painel:
        from painel import gerar
        caminho = gerar(cfg)
        print(f"Painel gerado: {caminho}\nAbra com:  open {caminho}")
        return

    if a.melhores:
        if a.volta_ate:
            cfg["melhores_volta_ate"] = a.volta_ate
        melhores(cfg)
        return

    if a.relatorio:
        relatorio(cfg)
        return

    if a.teste_rt:
        o, d, di, dv = a.teste_rt[:4]
        cab = a.teste_rt[4] if len(a.teste_rt) > 4 else "Economy"
        filtro = lambda vs: _filtrar(vs, "executiva" if cab == "Business" else "economica", None)  # noqa: E731

        def mostra(titulo, vs):
            print(f"\n== {titulo} ({len(vs)} tarifas) ==")
            for v in sorted(vs, key=lambda v: v.milhas)[:8]:
                print(f"  {v.voo:<16} {v.partida[11:16]:>5} {v.paradas}p {v.cabine:<9} {v.tarifa:<22} "
                      f"{_mil(v.milhas):>9} pts + R$ {(v.taxas or 0):.2f}")
        try:
            with LatamBuscador(headless=bool(cfg.get("headless", False)), proxy=cfg.get("proxy") or "") as b:
                idas, voltas, info = b.buscar_ida_volta(o.upper(), d.upper(), date.fromisoformat(di),
                                                        date.fromisoformat(dv), cab, int(cfg.get("adultos", 1)),
                                                        filtro=filtro)
        except (PrecisaLogin, Bloqueado) as e:
            print(f"\n>>> {type(e).__name__}: {e}")
            return
        print("URL idas :", info.get("url_idas"))
        print("URL voltas:", info.get("url_voltas"), "status", info.get("status_voltas"))
        print("Voltas obtidas por:", info.get("metodo"), "(clique = como uma pessoa no site)")
        mostra("IDAS (busca ida e volta)", idas)
        esc = info.get("ida_escolhida")
        if esc:
            print(f"\nIda escolhida: {esc.voo} {esc.tarifa} {_mil(esc.milhas)} pts")
        mostra("VOLTAS (depois de escolher a ida)", voltas)
        vb = _filtrar(voltas, "executiva" if cab == "Business" else "economica", None)
        if esc and vb:
            m = min(vb, key=lambda v: v.milhas)
            print(f"\nIda {_mil(esc.milhas)} + volta {_mil(m.milhas)} = {_mil(esc.milhas + m.milhas)} pts")
            print("(confira no site se a volta mostra o preço só da volta ou o total)")
        return

    if a.teste:
        o, d, dt = a.teste[:3]
        cab = a.teste[3] if len(a.teste) > 3 else "Economy"
        try:
            with LatamBuscador(headless=bool(cfg.get("headless", False)), proxy=cfg.get("proxy") or "") as b:
                voos = b.buscar(o.upper(), d.upper(), date.fromisoformat(dt), cab, int(cfg.get("adultos", 1)))
        except (PrecisaLogin, Bloqueado) as e:
            print(f"\n>>> {type(e).__name__}: {e}")
            return
        voos = sorted(voos, key=lambda v: v.milhas)
        for v in voos[:20]:
            print(f"{v.voo:<16} {v.partida[11:16]:>5} {v.paradas}p {v.cabine:<9} {v.tarifa:<22} "
                  f"{_mil(v.milhas):>9} pts + R$ {(v.taxas or 0):.2f}  assentos={v.assentos}")
        print(f"{len(voos)} tarifas (sem repetição); mostrando as 20 mais baratas")
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
        tentativas = 0
        while True:
            try:
                varredura(cfg)
                break
            except (PrecisaLogin, Bloqueado, KeyboardInterrupt):
                raise
            except Exception as e:  # noqa: BLE001  (navegador fechou, rede caiu...)
                tentativas += 1
                if tentativas > 3:
                    raise
                log.warning("O navegador do robô caiu (%s: %s). Abrindo de novo em 1 min e continuando "
                            "de onde parou (%d/3).", type(e).__name__, str(e).splitlines()[0][:120] if str(e) else "",
                            tentativas)
                time.sleep(60)
                if float(cfg.get("revalidar_horas", 20)) < 1:
                    cfg["revalidar_horas"] = 12  # não refazer o que já foi buscado nesta rodada
        try:
            from painel import gerar
            log.info("Painel atualizado: %s", gerar(cfg))
        except Exception as e:  # noqa: BLE001
            log.warning("Não consegui gerar o painel: %s", e)
    except PrecisaLogin as e:
        log.error("PRECISA LOGIN: %s", e)
        telegram(cfg, "⚠️ Monitor LATAM: a sessão da LATAM Pass expirou. Faça login de novo (--login).")
        sys.exit(3)
    except Bloqueado as e:
        log.error("BLOQUEADO: %s", e)
        telegram(cfg, f"⚠️ Monitor LATAM bloqueado pelo anti-bot: {e}\nVeja data/debug na VPS.")
        sys.exit(2)
    except KeyboardInterrupt:
        log.warning("Interrompido pelo usuário")
    except Exception as e:  # noqa: BLE001
        log.exception("ERROR inesperado: %s", e)
        sys.exit(1)
    finally:
        fd.close()
        LOCK.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
