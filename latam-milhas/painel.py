"""Gera data/painel.html: acompanhamento visual das buscas de ida e volta.

Lê só o que o robô já salvou (data/milhas.db e data/debug), sem acessar a LATAM.
Uso:  .venv/bin/python monitor.py --painel   (e depois: open data/painel.html)
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path

RAIZ = Path(__file__).parent
SAIDA = RAIZ / "data" / "painel.html"


def _dados(cfg: dict) -> dict:
    from monitor import _data, abrir_db, calcular_combos

    vg = (cfg.get("viagens") or [{}])[0]
    o, d = vg["origem"].upper(), vg["destino"].upper()
    evitar = [(_data(a), _data(b)) for a, b in (cfg.get("evitar_periodos") or [])]
    pax = int(cfg.get("passageiros") or 1)
    milhas = float(cfg.get("milhas_disponiveis") or 0)
    ida_de, ida_ate = _data(vg["ida_de"]), _data(vg["ida_ate"])

    def fora(x: date, y: date) -> bool:
        return not any(not (y < a or x > b) for a, b in evitar)

    con = abrir_db()
    linhas = con.execute(
        "SELECT ts, ida, volta, voo_ida, tarifa_ida, milhas_ida, taxas_ida, voo_volta, tarifa_volta, "
        "milhas_volta, taxas_volta FROM ida_volta WHERE origem=? AND destino=? ORDER BY ts", (o, d)).fetchall()

    # 1) preço mais recente de cada par de datas (busca ida e volta)
    ultimo: dict[tuple, tuple] = {}
    # 2) histórico: por dia de coleta, o último preço de cada par naquele dia
    por_dia: dict[str, dict[tuple, float]] = {}
    for r in linhas:
        x, y = date.fromisoformat(r[1]), date.fromisoformat(r[2])
        if not (ida_de <= x <= ida_ate) or not fora(x, y):
            continue
        ultimo[(r[1], r[2])] = r
        por_dia.setdefault(r[0][:10], {})[(r[1], r[2])] = r[5] + r[9]

    por_data = []
    for (ida, volta), r in sorted(ultimo.items()):
        por_data.append({
            "ida": ida, "volta": volta, "total": r[5] + r[9], "ida_pts": r[5], "volta_pts": r[9],
            "taxas": round((r[6] or 0) + (r[10] or 0), 2), "voo_ida": r[3], "voo_volta": r[7],
            "paradas_ida": r[3].count("+"), "paradas_volta": r[7].count("+"), "coletado": r[0][:16],
        })

    dias = sorted(por_dia)
    historico = []
    for dia in dias:
        precos = por_dia[dia]
        par, val = min(precos.items(), key=lambda kv: kv[1])
        historico.append({"dia": dia, "melhor": val, "par": f"{par[0]}|{par[1]}", "n": len(precos)})

    # pares acompanhados: os 3 mais baratos agora
    top_pares = [f"{p['ida']}|{p['volta']}" for p in sorted(por_data, key=lambda p: p["total"])[:3]]
    pares_hist = {k: [] for k in top_pares}
    for dia in dias:
        for k in top_pares:
            ida, volta = k.split("|")
            v = por_dia[dia].get((ida, volta))
            if v is not None:
                pares_hist[k].append({"dia": dia, "total": v})

    # melhores combinações (9/10/11 dias, durações, 1 parada) a partir das respostas salvas
    opcoes = {"barato": [], "meio": [], "rapido": [], "uma_parada": []}
    try:
        r = calcular_combos(cfg)

        def item(c, chave):
            vi, vv = c[chave]
            return {
                "ida": c["x"].isoformat(), "volta": c["y"].isoformat(), "dias": c["n"],
                "total": vi.milhas + vv.milhas, "taxas": round((vi.taxas or 0) + (vv.taxas or 0), 2),
                "ida_voo": vi.voo, "ida_hora": vi.partida[11:16], "ida_dur": vi.duracao_min, "ida_par": vi.paradas,
                "volta_voo": vv.voo, "volta_hora": vv.partida[11:16], "volta_dur": vv.duracao_min,
                "volta_par": vv.paradas,
            }

        cs = r["combos"]

        def tot(c, k):
            return c[k][0].milhas + c[k][1].milhas

        def tmp(c, k):
            return c[k][0].duracao_min + c[k][1].duracao_min

        opcoes["barato"] = [item(c, "barato") for c in sorted(cs, key=lambda c: (tot(c, "barato"), tmp(c, "barato")))[:15]]
        opcoes["meio"] = [item(c, "meio") for c in sorted(cs, key=lambda c: (tot(c, "meio"), tmp(c, "meio")))[:15]]
        opcoes["rapido"] = [item(c, "rapido") for c in sorted(cs, key=lambda c: (tmp(c, "rapido"), tot(c, "rapido")))[:15]]
        com1 = [c for c in cs if "uma_parada" in c]
        opcoes["uma_parada"] = [item(c, "uma_parada")
                                for c in sorted(com1, key=lambda c: (tot(c, "uma_parada"), tmp(c, "uma_parada")))[:15]]
    except Exception as e:  # noqa: BLE001
        opcoes["erro"] = str(e)

    return {
        "gerado": datetime.now().strftime("%d/%m/%Y %H:%M"),
        "nome": vg.get("nome") or f"{o} → {d}",
        "pax": pax, "milhas": milhas, "limite_pp": (milhas / pax) if milhas else None,
        "janela": [ida_de.isoformat(), ida_ate.isoformat()],
        "evitar": [[a.isoformat(), b.isoformat()] for a, b in evitar],
        "por_data": por_data, "historico": historico, "pares_hist": pares_hist, "opcoes": opcoes,
    }


def gerar(cfg: dict) -> Path:
    dados = _dados(cfg)
    html = MODELO.replace("__DADOS__", json.dumps(dados, ensure_ascii=False))
    SAIDA.parent.mkdir(parents=True, exist_ok=True)
    SAIDA.write_text(html, encoding="utf-8")
    return SAIDA


MODELO = r"""<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Milhas Orlando</title>
<style>
:root {
  color-scheme: light;
  --page: #f9f9f7; --surface-1: #fcfcfb; --border: #e4e3de; --grid: #ecebe7;
  --text-primary: #0b0b0b; --text-secondary: #52514e; --text-muted: #75746f;
  --series-1: #2a78d6; --series-2: #eb6834; --series-3: #1baf7a;
  --bar: #2a78d6; --bar-over: #b7d3f6; --ref: #d03b3b; --band: rgba(117,116,111,.10);
  --good: #0ca30c; --critical: #d03b3b;
}
@media (prefers-color-scheme: dark) {
  :root:where(:not([data-theme="light"])) {
    color-scheme: dark;
    --page: #0d0d0d; --surface-1: #1a1a19; --border: #2e2e2b; --grid: #262624;
    --text-primary: #f0efec; --text-secondary: #c3c2b7; --text-muted: #9a998f;
    --series-1: #3987e5; --series-2: #d95926; --series-3: #199e70;
    --bar: #3987e5; --bar-over: #184f95; --ref: #e66767; --band: rgba(195,194,183,.10);
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --page: #0d0d0d; --surface-1: #1a1a19; --border: #2e2e2b; --grid: #262624;
  --text-primary: #f0efec; --text-secondary: #c3c2b7; --text-muted: #9a998f;
  --series-1: #3987e5; --series-2: #d95926; --series-3: #199e70;
  --bar: #3987e5; --bar-over: #184f95; --ref: #e66767; --band: rgba(195,194,183,.10);
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--page); color: var(--text-primary);
  font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
main { max-width: 1120px; margin: 0 auto; padding: 24px 16px 48px; }
h1 { font-size: 22px; margin: 0 0 4px; }
h2 { font-size: 16px; margin: 0 0 2px; }
.sub { color: var(--text-secondary); margin: 0 0 20px; }
.note { color: var(--text-muted); font-size: 12px; margin: 4px 0 12px; }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 12px; margin-bottom: 16px; }
.tile, .card { background: var(--surface-1); border: 1px solid var(--border); border-radius: 12px; padding: 14px 16px; }
.card { margin-bottom: 16px; }
.tile .k { color: var(--text-secondary); font-size: 12px; }
.tile .v { font-size: 24px; font-weight: 650; margin: 2px 0; font-variant-numeric: tabular-nums; }
.tile .d { font-size: 12px; color: var(--text-secondary); }
.chip { display: inline-flex; align-items: center; gap: 4px; font-size: 12px; }
.svgwrap { position: relative; width: 100%; }
svg { display: block; width: 100%; height: auto; overflow: visible; }
svg text { fill: var(--text-muted); font-size: 11px; }
.legend { display: flex; flex-wrap: wrap; gap: 14px; font-size: 12px; color: var(--text-secondary); margin: 6px 0 2px; }
.legend i { display: inline-block; width: 12px; height: 3px; border-radius: 2px; margin-right: 6px; vertical-align: middle; }
.tip { position: absolute; pointer-events: none; background: var(--surface-1); border: 1px solid var(--border);
  border-radius: 8px; padding: 8px 10px; font-size: 12px; box-shadow: 0 4px 16px rgba(0,0,0,.12);
  min-width: 180px; display: none; z-index: 5; color: var(--text-primary); }
.tip b { font-variant-numeric: tabular-nums; }
.tabs { display: flex; flex-wrap: wrap; gap: 6px; margin: 8px 0 10px; }
.tabs button { border: 1px solid var(--border); background: var(--surface-1); color: var(--text-secondary);
  border-radius: 999px; padding: 5px 12px; font: inherit; font-size: 13px; cursor: pointer; }
.tabs button[aria-selected="true"] { background: var(--text-primary); color: var(--surface-1); border-color: var(--text-primary); }
.tablewrap { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-size: 13px; font-variant-numeric: tabular-nums; }
th, td { text-align: left; padding: 7px 8px; border-bottom: 1px solid var(--grid); white-space: nowrap; }
th { color: var(--text-secondary); font-weight: 600; font-size: 12px; }
td.num, th.num { text-align: right; }
.ok { color: var(--good); } .no { color: var(--critical); }
details summary { cursor: pointer; color: var(--text-secondary); font-size: 13px; margin-top: 8px; }
</style>
</head>
<body>
<main>
  <h1 id="titulo">Milhas</h1>
  <p class="sub" id="sub"></p>

  <div class="tiles" id="tiles"></div>

  <section class="card">
    <h2>Preço por data de ida (ida e volta, 10 dias)</h2>
    <p class="note">Total por pessoa na busca mais recente de cada data. Barras claras passam do limite para <span id="paxn"></span> pessoas. Passe o mouse para ver os voos.</p>
    <div class="legend" id="lg1"></div>
    <div class="svgwrap" id="w1"><svg id="c1" role="img" aria-label="Preço por data de ida"></svg><div class="tip" id="t1"></div></div>
    <details><summary>Ver como tabela</summary><div class="tablewrap" id="tab1"></div></details>
  </section>

  <section class="card">
    <h2>Evolução dos preços ao longo dos dias</h2>
    <p class="note">Cada ponto é um dia de varredura. "Melhor do dia" é a data mais barata encontrada naquele dia.</p>
    <div class="legend" id="lg2"></div>
    <div class="svgwrap" id="w2"><svg id="c2" role="img" aria-label="Evolução dos preços"></svg><div class="tip" id="t2"></div></div>
  </section>

  <section class="card">
    <h2>Melhores combinações (9, 10 ou 11 dias)</h2>
    <p class="note">Montadas com as respostas salvas da última varredura. Preços por pessoa (1 adulto); confirme no site com <span class="paxn2"></span> passageiros antes de emitir. 9 e 11 dias juntam a ida de uma busca com a volta de outra.</p>
    <div class="tabs" role="tablist" id="tabs"></div>
    <div class="tablewrap" id="tab3"></div>
  </section>
</main>

<script>
const D = __DADOS__;
const fmt = n => n == null ? "—" : Math.round(n).toLocaleString("pt-BR");
const br = iso => iso.slice(8,10) + "/" + iso.slice(5,7);
const hh = m => m ? Math.floor(m/60) + "h" + String(m%60).padStart(2,"0") : "?";
const css = v => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const NS = "http://www.w3.org/2000/svg";
const el = (tag, attrs={}, parent) => { const e = document.createElementNS(NS, tag);
  for (const k in attrs) e.setAttribute(k, attrs[k]); if (parent) parent.appendChild(e); return e; };
const limite = D.limite_pp;

document.getElementById("titulo").textContent = D.nome;
document.getElementById("sub").textContent =
  `Atualizado em ${D.gerado} · idas de ${br(D.janela[0])} a ${br(D.janela[1])}` +
  (D.evitar.length ? ` · fora do Carnaval (${D.evitar.map(p => br(p[0])+"–"+br(p[1])).join(", ")})` : "") +
  ` · ${D.pax} passageiros, ${fmt(D.milhas)} milhas`;
document.getElementById("paxn").textContent = D.pax;
document.querySelectorAll(".paxn2").forEach(e => e.textContent = D.pax);

// ---------- tiles ----------
(function tiles() {
  const t = document.getElementById("tiles");
  const add = (k, v, d) => { const div = document.createElement("div"); div.className = "tile";
    div.innerHTML = `<div class="k">${k}</div><div class="v">${v}</div><div class="d">${d||""}</div>`; t.appendChild(div); };
  const h = D.historico;
  const hoje = h[h.length-1], ontem = h[h.length-2];
  if (hoje) {
    let delta = "";
    if (ontem) { const df = hoje.melhor - ontem.melhor;
      delta = df === 0 ? "igual ao dia anterior" : (df > 0 ? "▲ " : "▼ ") + fmt(Math.abs(df)) + " vs. dia anterior"; }
    const [i, v] = hoje.par.split("|");
    add("Melhor ida e volta (10 dias) por pessoa", fmt(hoje.melhor), `${br(i)} → ${br(v)} · ${delta}`);
    if (limite) { const tot = hoje.melhor * D.pax, ok = tot <= D.milhas;
      add(`Total para ${D.pax} pessoas`, fmt(tot), `<span class="chip ${ok?"ok":"no"}">${ok?"✓ cabe":"✗ não cabe"}</span> nas suas ${fmt(D.milhas)} milhas`); }
  }
  const u = (D.opcoes.uma_parada||[])[0];
  if (u) add("Mais barata com até 1 parada", fmt(u.total),
    `${br(u.ida)} → ${br(u.volta)} (${u.dias}d) · ${hh(u.ida_dur)} + ${hh(u.volta_dur)} de voo`);
  add("Varreduras registradas", h.length, hoje ? `última com ${hoje.n} datas buscadas` : "");
})();

// ---------- chart 1: bars por data de ida ----------
function chart1() {
  const svg = document.getElementById("c1"), tip = document.getElementById("t1"), wrap = document.getElementById("w1");
  svg.innerHTML = "";
  const data = D.por_data;
  const W = Math.max(wrap.clientWidth, 320), H = 280, m = {l: 64, r: 12, t: 16, b: 40};
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  if (!data.length) { el("text", {x: 10, y: 30}, svg).textContent = "Sem dados ainda."; return; }
  const d0 = new Date(D.janela[0]+"T12:00"), d1 = new Date(D.janela[1]+"T12:00");
  const nDays = Math.round((d1-d0)/864e5) + 1;
  const iw = W - m.l - m.r, ih = H - m.t - m.b, bw = iw / nDays;
  const xOf = iso => m.l + Math.round((new Date(iso+"T12:00") - d0)/864e5) * bw;
  const vmax = Math.max(...data.map(d => d.total), limite || 0) * 1.08;
  const y = v => m.t + ih - v / vmax * ih;
  // grid
  const step = vmax > 300000 ? 100000 : 50000;
  for (let v = 0; v <= vmax; v += step) {
    el("line", {x1: m.l, x2: W-m.r, y1: y(v), y2: y(v), stroke: css("--grid")}, svg);
    el("text", {x: m.l-8, y: y(v)+4, "text-anchor": "end"}, svg).textContent = v ? (v/1000)+" mil" : "0";
  }
  // carnaval band
  D.evitar.forEach(([a, b]) => {
    const xa = xOf(a), xb = xOf(b) + bw;
    if (xb < m.l || xa > W-m.r) return;
    el("rect", {x: Math.max(xa, m.l), y: m.t, width: Math.min(xb, W-m.r) - Math.max(xa, m.l), height: ih, fill: css("--band")}, svg);
    el("text", {x: (Math.max(xa,m.l)+Math.min(xb,W-m.r))/2, y: m.t+12, "text-anchor": "middle"}, svg).textContent = "Carnaval";
  });
  // bars
  const gap = 2, best = Math.min(...data.map(d => d.total));
  data.forEach(d => {
    const x = xOf(d.ida) + gap/2, w = Math.max(bw - gap, 2), y0 = y(d.total), h = m.t + ih - y0;
    const over = limite && d.total > limite;
    const r = Math.min(4, w/2);
    el("path", {d: `M${x},${m.t+ih} V${y0+r} Q${x},${y0} ${x+r},${y0} H${x+w-r} Q${x+w},${y0} ${x+w},${y0+r} V${m.t+ih} Z`,
      fill: over ? css("--bar-over") : css("--bar")}, svg);
    if (d.total === best) el("text", {x: x + w/2, y: y0 - 6, "text-anchor": "middle", style: "fill:var(--text-primary);font-weight:600"}, svg).textContent = fmt(d.total);
    const hit = el("rect", {x: xOf(d.ida), y: m.t, width: bw, height: ih, fill: "transparent"}, svg);
    hit.addEventListener("mousemove", ev => {
      tip.style.display = "block";
      tip.innerHTML = `<div><b>${br(d.ida)} → ${br(d.volta)}</b></div>
        <div>Total: <b>${fmt(d.total)}</b> /pessoa · ${D.pax}x = <b>${fmt(d.total*D.pax)}</b> ${limite ? (d.total<=limite?'<span class="ok">✓</span>':'<span class="no">✗</span>') : ""}</div>
        <div>Ida ${fmt(d.ida_pts)} · ${d.voo_ida} (${d.paradas_ida}p)</div>
        <div>Volta ${fmt(d.volta_pts)} · ${d.voo_volta} (${d.paradas_volta}p)</div>
        <div style="color:var(--text-muted)">Taxas R$ ${d.taxas.toFixed(2)} · coletado ${d.coletado.replace("T"," ")}</div>`;
      const bx = wrap.getBoundingClientRect(); let lx = ev.clientX - bx.left + 12;
      if (lx + 240 > bx.width) lx = ev.clientX - bx.left - 252;
      tip.style.left = lx + "px"; tip.style.top = (ev.clientY - bx.top - 10) + "px";
    });
    hit.addEventListener("mouseleave", () => tip.style.display = "none");
  });
  // limite
  if (limite) {
    el("line", {x1: m.l, x2: W-m.r, y1: y(limite), y2: y(limite), stroke: css("--ref"), "stroke-width": 1.5, "stroke-dasharray": "5 4"}, svg);
  }
  document.getElementById("lg1").innerHTML =
    `<span><i style="background:var(--bar)"></i>cabe no limite</span><span><i style="background:var(--bar-over)"></i>passa do limite</span>` +
    (limite ? `<span><i style="background:var(--ref);height:2px"></i>limite p/ ${D.pax} pessoas: ${fmt(limite)} por pessoa</span>` : "");
  // x labels: 1º e 15 de cada mês
  for (let i = 0; i < nDays; i++) {
    const dt = new Date(d0.getTime() + i*864e5), dd = dt.getDate();
    if (dd === 1 || dd === 15) {
      const iso = dt.toISOString().slice(0,10);
      el("text", {x: m.l + i*bw + bw/2, y: H - m.b + 16, "text-anchor": "middle"}, svg).textContent = br(iso);
    }
  }
  el("line", {x1: m.l, x2: W-m.r, y1: m.t+ih, y2: m.t+ih, stroke: css("--border")}, svg);
  // tabela
  document.getElementById("tab1").innerHTML = "<table><thead><tr><th>Ida → volta</th><th class=num>Total/pessoa</th><th class=num>Ida</th><th class=num>Volta</th><th>Voos</th><th class=num>Taxas</th></tr></thead><tbody>" +
    data.map(d => `<tr><td>${br(d.ida)} → ${br(d.volta)}</td><td class=num>${fmt(d.total)}</td><td class=num>${fmt(d.ida_pts)}</td><td class=num>${fmt(d.volta_pts)}</td><td>${d.voo_ida} | ${d.voo_volta}</td><td class=num>R$ ${d.taxas.toFixed(2)}</td></tr>`).join("") + "</tbody></table>";
}

// ---------- chart 2: evolução ----------
function chart2() {
  const svg = document.getElementById("c2"), tip = document.getElementById("t2"), wrap = document.getElementById("w2");
  svg.innerHTML = "";
  const series = [{nome: "Melhor do dia (qualquer data)", cor: "--series-1",
                   pts: D.historico.map(h => ({dia: h.dia, v: h.melhor, extra: h.par.split("|").map(br).join(" → ")}))}];
  const cores = ["--series-2", "--series-3"];
  Object.entries(D.pares_hist).slice(0, 2).forEach(([k, pts], i) => {
    const [a, b] = k.split("|");
    series.push({nome: `${br(a)} → ${br(b)}`, cor: cores[i], pts: pts.map(p => ({dia: p.dia, v: p.total}))});
  });
  const lg = document.getElementById("lg2");
  lg.innerHTML = series.map(s => `<span><i style="background:var(${s.cor})"></i>${s.nome}</span>`).join("");
  const dias = D.historico.map(h => h.dia);
  const W = Math.max(wrap.clientWidth, 320), H = 240, m = {l: 64, r: 90, t: 14, b: 32};
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  if (!dias.length) { el("text", {x: 10, y: 30}, svg).textContent = "Sem dados ainda."; return; }
  const all = series.flatMap(s => s.pts.map(p => p.v)).concat(limite ? [limite] : []);
  let lo = Math.min(...all), hi = Math.max(...all); const pad = Math.max((hi-lo)*0.15, 4000); lo -= pad; hi += pad;
  const iw = W-m.l-m.r, ih = H-m.t-m.b;
  const x = dia => m.l + (dias.length === 1 ? iw/2 : dias.indexOf(dia) / (dias.length-1) * iw);
  const y = v => m.t + ih - (v-lo)/(hi-lo)*ih;
  const nt = 4;
  for (let i = 0; i <= nt; i++) { const v = lo + (hi-lo)*i/nt;
    el("line", {x1: m.l, x2: W-m.r, y1: y(v), y2: y(v), stroke: css("--grid")}, svg);
    el("text", {x: m.l-8, y: y(v)+4, "text-anchor": "end"}, svg).textContent = fmt(v/1000) + " mil"; }
  dias.forEach(d => el("text", {x: x(d), y: H-m.b+16, "text-anchor": "middle"}, svg).textContent = br(d));
  if (limite && limite >= lo && limite <= hi) {
    el("line", {x1: m.l, x2: W-m.r, y1: y(limite), y2: y(limite), stroke: css("--ref"), "stroke-width": 1.5, "stroke-dasharray": "5 4"}, svg);
  }
  series.forEach(s => {
    if (!s.pts.length) return;
    el("path", {d: s.pts.map((p, i) => (i ? "L" : "M") + x(p.dia) + "," + y(p.v)).join(" "),
      fill: "none", stroke: css(s.cor), "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round"}, svg);
    s.pts.forEach(p => el("circle", {cx: x(p.dia), cy: y(p.v), r: 4, fill: css(s.cor), stroke: css("--surface-1"), "stroke-width": 2}, svg));
    const last = s.pts[s.pts.length-1];
    el("text", {x: x(last.dia) + 8, y: y(last.v) + 4, style: "fill:var(--text-secondary)"}, svg).textContent = fmt(last.v);
  });
  const cross = el("line", {y1: m.t, y2: m.t+ih, stroke: css("--text-muted"), "stroke-width": 1, opacity: 0}, svg);
  const hit = el("rect", {x: m.l-10, y: m.t, width: iw+20, height: ih, fill: "transparent"}, svg);
  hit.addEventListener("mousemove", ev => {
    const bx = svg.getBoundingClientRect(), px = (ev.clientX - bx.left) * W / bx.width;
    let best = dias[0]; dias.forEach(d => { if (Math.abs(x(d)-px) < Math.abs(x(best)-px)) best = d; });
    cross.setAttribute("x1", x(best)); cross.setAttribute("x2", x(best)); cross.setAttribute("opacity", .5);
    const linhas = series.map(s => { const p = s.pts.find(q => q.dia === best);
      return p ? `<div><i style="display:inline-block;width:10px;height:3px;background:var(${s.cor});margin-right:6px;vertical-align:middle"></i>${s.nome}: <b>${fmt(p.v)}</b>${p.extra ? " ("+p.extra+")" : ""}</div>` : ""; }).join("");
    tip.style.display = "block"; tip.innerHTML = `<div><b>Varredura de ${br(best)}</b></div>${linhas}`;
    const wb = wrap.getBoundingClientRect(); let lx = ev.clientX - wb.left + 12;
    if (lx + 260 > wb.width) lx = ev.clientX - wb.left - 272;
    tip.style.left = lx + "px"; tip.style.top = (ev.clientY - wb.top - 10) + "px";
  });
  hit.addEventListener("mouseleave", () => { tip.style.display = "none"; cross.setAttribute("opacity", 0); });
}

// ---------- tabela de combinações ----------
(function tabela() {
  const abas = [["barato","Mais baratas"],["uma_parada","Até 1 parada"],["meio","Meio-termo"],["rapido","Mais rápidas"]];
  const tabs = document.getElementById("tabs"), out = document.getElementById("tab3");
  const show = k => {
    tabs.querySelectorAll("button").forEach(b => b.setAttribute("aria-selected", b.dataset.k === k));
    const rows = D.opcoes[k] || [];
    if (!rows.length) { out.innerHTML = `<p class="note">${D.opcoes.erro ? "Erro: "+D.opcoes.erro : "Sem dados."}</p>`; return; }
    out.innerHTML = "<table><thead><tr><th>Ida → volta</th><th class=num>Dias</th><th class=num>Por pessoa</th><th class=num>Para "+D.pax+"</th><th></th><th class=num>Taxas ("+D.pax+")</th><th>Ida</th><th>Volta</th></tr></thead><tbody>" +
      rows.map(r => { const tot = r.total*D.pax, ok = !D.milhas || tot <= D.milhas;
        return `<tr><td>${br(r.ida)} → ${br(r.volta)}</td><td class=num>${r.dias}</td><td class=num><b>${fmt(r.total)}</b></td><td class=num>${fmt(tot)}</td>
        <td class="${ok?"ok":"no"}">${ok?"✓ cabe":"✗"}</td><td class=num>R$ ${fmt(r.taxas*D.pax)}</td>
        <td>${r.ida_hora} · ${hh(r.ida_dur)} · ${r.ida_par}p · ${r.ida_voo}</td><td>${r.volta_hora} · ${hh(r.volta_dur)} · ${r.volta_par}p · ${r.volta_voo}</td></tr>`; }).join("") + "</tbody></table>";
  };
  abas.forEach(([k, nome]) => { const b = document.createElement("button"); b.textContent = nome; b.dataset.k = k;
    b.setAttribute("role","tab"); b.onclick = () => show(k); tabs.appendChild(b); });
  show("barato");
})();

const draw = () => { chart1(); chart2(); };
draw(); window.addEventListener("resize", draw);
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", draw);
</script>
</body>
</html>
"""
