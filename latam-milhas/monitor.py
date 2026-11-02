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
        # Uma busca de IDA E VOLTA por par de datas (como no site)
        for x in idas:
            for n in duracoes:
                y = x + timedelta(days=n)
                pausa()
                lista_i, lista_v, info = b.buscar_ida_volta(o, d, x, y, _cabine_url(cabine), adultos, filtro=filtro)
                vi = info.get("ida_escolhida")
                bons_v = filtro(lista_v)
                if vi and bons_v:
                    vv = min(bons_v, key=lambda v: v.milhas)
                    salvar_rt(con, vi, vv)
                    pares.append((vi.milhas + vv.milhas, (vi.taxas or 0) + (vv.taxas or 0), vi, vv, n))
                    log.info("  %s → %s: ida %s + volta %s = %s pts", x, y, _mil(vi.milhas), _mil(vv.milhas),
                             _mil(vi.milhas + vv.milhas))
                else:
                    log.info("  %s → %s: nada disponível com os filtros", x, y)
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
        p = "direto" if v.paradas == 0 else f"{v.paradas} parada(s)"
        return (f"{rot} {_br(v.data)} {v.partida[11:16]} · {v.voo} · {p} · {v.tarifa} · "
                f"{_mil(v.milhas)} pts + R$ {(v.taxas or 0):.0f}")
    li = url_busca(vi.origem, vi.destino, date.fromisoformat(vi.data), _cabine_url(cabine), 1)
    lv = url_busca(vv.origem, vv.destino, date.fromisoformat(vv.data), _cabine_url(cabine), 1)
    return (f"✈️ <b>{nome}</b> — {n} dias\n"
            f"<b>Total: {_mil(tot)} pts + R$ {tx:.0f}</b>\n"
            f"{linha(vi, '🛫')}\n{linha(vv, '🛬')}\n"
            f"<a href=\"{li}\">ver ida</a> · <a href=\"{lv}\">ver volta</a>")


def relatorio(cfg: dict, top: int = 15):
    """Combina ida+volta usando os preços já salvos no banco (sem acessar a LATAM)."""
    con = abrir_db()
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


def varredura(cfg: dict):
    con = abrir_db()
    espera = cfg.get("espera_entre_buscas_seg", [20, 45])
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
        varredura(cfg)
    except PrecisaLogin as e:
        log.error("PRECISA LOGIN: %s", e)
        telegram(cfg, "⚠️ Monitor LATAM: a sessão da LATAM Pass expirou. Faça login de novo (--login).")
        sys.exit(3)
    except Bloqueado as e:
        log.error("BLOQUEADO: %s", e)
        telegram(cfg, f"⚠️ Monitor LATAM bloqueado pelo anti-bot: {e}\nVeja data/debug na VPS.")
        sys.exit(2)
    finally:
        fd.close()
        LOCK.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
