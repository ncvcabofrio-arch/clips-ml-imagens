"""Busca de passagens com milhas na Smiles (GOL e parceiras).

Mesma estratégia do latam.py: um Chromium de verdade abre a página de resultados
da Smiles e o robô captura o JSON que o próprio site recebe da API de busca.
Usa perfil de navegador e pasta de debug próprios, para rodar em paralelo com a LATAM.
"""
from __future__ import annotations

import json
import logging
import random
import time
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

from playwright.sync_api import sync_playwright

from latam import Voo, Bloqueado, PrecisaLogin, _achar, resumo_estrutura  # noqa: F401

log = logging.getLogger("smiles")

BASE = "https://www.smiles.com.br"
RAIZ = Path(__file__).parent
DEBUG_DIR = RAIZ / "data" / "debug-smiles"
PROFILE_DIR = RAIZ / "data" / "browser-profile-smiles-chrome"
# trechos de URL que identificam a API de busca de voos da Smiles
API_HINTS = ("flightsearch", "airlines/search", "flight-search", "flights/search")
# respostas que interessam para diagnóstico (nada de dados pessoais da conta)
DIAG_HINTS = ("search", "flight", "availability", "fare", "segment")
IGNORAR = ("voucher", "member", "plans", "cobranded", "cap-rate", "easy-travel", "users", "goals")


def _ms(d: date) -> int:
    """Smiles usa a data como timestamp em ms: meia-noite de Brasília (03:00 UTC)."""
    return int(datetime(d.year, d.month, d.day, 3, tzinfo=timezone.utc).timestamp() * 1000)


def url_busca(origem: str, destino: str, ida: date, volta: date | None, adultos: int = 1) -> str:
    q = {
        "adults": adultos,
        "cabin": "ALL",
        "children": 0,
        "departureDate": _ms(ida),
        "infants": 0,
        "isElegible": "false",
        "isFlexibleDateChecked": "false",
        "searchType": "g3",
        "segments": 1,
        "tripType": 1 if volta else 2,  # 1 = ida e volta, 2 = só ida
        "originAirport": "RIO" if origem in ("GIG", "SDU", "RIO") else origem,
        "originCity": "",
        "originCountry": "",
        "originAirportIsAny": "true" if origem in ("GIG", "SDU", "RIO") else "false",
        "destinationAirport": destino,
        "destinCity": "",
        "destinCountry": "",
        "destinAirportIsAny": "false",
        "novo-resultado-voos": "true",
    }
    if volta:
        q["returnDate"] = _ms(volta)
    # mesma ordem de parâmetros que o site usa
    ordem = ["adults", "cabin", "children", "departureDate", "infants", "isElegible", "isFlexibleDateChecked",
             "returnDate", "searchType", "segments", "tripType", "originAirport", "originCity", "originCountry",
             "originAirportIsAny", "destinationAirport", "destinCity", "destinCountry", "destinAirportIsAny",
             "novo-resultado-voos"]
    return f"{BASE}/mfe/emissao-passagem/?{urlencode([(k, q[k]) for k in ordem if k in q])}"


# ---------------------------------------------------------------- parser ---

def _minutos(dur) -> int | None:
    if isinstance(dur, (int, float)):
        return int(dur)
    if isinstance(dur, dict):
        h, m = dur.get("hours"), dur.get("minutes")
        if h is not None or m is not None:
            return int(h or 0) * 60 + int(m or 0)
    if isinstance(dur, str) and ":" in dur:
        try:
            h, m = dur.split(":")[:2]
            return int(h) * 60 + int(m)
        except ValueError:
            return None
    return None


def _codigo_voo(f: dict) -> str:
    pernas = f.get("legList") or f.get("legs") or []
    cods = []
    for p in pernas if isinstance(pernas, list) else []:
        num = p.get("flightNumber") or _achar(p, ("flightNumber", "number"))
        cia = p.get("marketingAirline") or p.get("operationAirline") or p.get("airline") or {}
        cia = cia.get("code") if isinstance(cia, dict) else cia
        if num:
            cods.append(f"{cia or ''}{num}")
    if cods:
        return "+".join(cods)
    num = f.get("flightNumber") or _achar(f, ("flightNumber",))
    cia = f.get("airline") or {}
    cia = cia.get("code") if isinstance(cia, dict) else cia
    return f"{cia or ''}{num or '?'}"


def _tarifas(f: dict) -> list[tuple[str, float, float | None]]:
    """[(tipo, milhas, dinheiro)] — tolera nomes diferentes de campos."""
    out = []
    for fare in f.get("fareList") or f.get("fares") or []:
        if not isinstance(fare, dict):
            continue
        milhas = fare.get("miles") or fare.get("milesAmount") or _achar(fare, ("miles",))
        try:
            milhas = float(milhas)
        except (TypeError, ValueError):
            continue
        if milhas <= 0:
            continue
        dinheiro = fare.get("money") or fare.get("airlineFareAmount")
        try:
            dinheiro = float(dinheiro) if dinheiro is not None else None
        except (TypeError, ValueError):
            dinheiro = None
        out.append((str(fare.get("type") or fare.get("fareType") or "?"), milhas, dinheiro))
    return out


def parse_smiles(payload, origem: str, destino: str, ida: date, volta: date | None):
    """Retorna (voos_ida, voos_volta). Cada voo vira um Voo por tipo de tarifa."""
    segs = []
    if isinstance(payload, dict):
        segs = payload.get("requestedFlightSegmentList") or payload.get("segments") or []
    idas, voltas = [], []
    for i, seg in enumerate(segs if isinstance(segs, list) else []):
        tipo = str(seg.get("type") or "")
        e_volta = ("2" in tipo) or (i == 1)
        dia, de, para = (volta, destino, origem) if e_volta else (ida, origem, destino)
        if dia is None:
            continue
        for f in seg.get("flightList") or seg.get("flights") or []:
            if not isinstance(f, dict):
                continue
            dep = f.get("departure") or {}
            arr = f.get("arrival") or {}
            cab = str(f.get("cabin") or "")
            paradas = f.get("stops")
            if paradas is None:
                pernas = f.get("legList") or []
                paradas = max(len(pernas) - 1, 0) if isinstance(pernas, list) else 0
            assentos = f.get("availableSeats")
            for tipo_tar, milhas, dinheiro in _tarifas(f):
                v = Voo(
                    origem=(dep.get("airport") or {}).get("code") or de if isinstance(dep, dict) else de,
                    destino=(arr.get("airport") or {}).get("code") or para if isinstance(arr, dict) else para,
                    data=dia.isoformat(),
                    voo=_codigo_voo(f),
                    partida=str(dep.get("date") or "") if isinstance(dep, dict) else "",
                    chegada=str(arr.get("date") or "") if isinstance(arr, dict) else "",
                    paradas=int(paradas or 0),
                    duracao_min=_minutos(f.get("durationNumber") or f.get("duration")),
                    tarifa=tipo_tar,
                    milhas=milhas,
                    moeda="SMILES",
                    taxas=None,
                    cabine="executiva" if cab.upper() in ("BUSINESS", "EXECUTIVA", "PREMIUM_ECONOMY_BUSINESS")
                    else "economica",
                    assentos=int(assentos) if str(assentos or "").isdigit() else None,
                    offer_id=str(f.get("uid") or ""),
                )
                (voltas if e_volta else idas).append(v)
    return idas, voltas


# --------------------------------------------------------------- browser ---

CDP_URL = "http://localhost:9222"


class SmilesBuscador:
    def __init__(self, headless: bool = False, timeout_seg: int = 60, cdp: str | None = None):
        self.cdp = cdp
        self._pagina_propria = False
        self.headless = headless
        self.timeout_ms = timeout_seg * 1000
        self._pw = None
        self.ctx = None
        self.page = None
        self.ultimas_respostas: dict = {}

    def __enter__(self):
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        self._pw = sync_playwright().start()
        if self.cdp:
            # Conecta no Chrome que a pessoa abriu e logou à mão (sem automação no login)
            try:
                browser = self._pw.chromium.connect_over_cdp(self.cdp)
            except Exception as e:  # noqa: BLE001
                raise RuntimeError(
                    "Não achei o Chrome da Smiles aberto. Abra-o primeiro com o comando do passo 1 "
                    f"(porta 9222). Detalhe: {str(e).splitlines()[0][:120]}") from e
            self.ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            self.page = self.ctx.new_page()
            self._pagina_propria = True
            log.info("Smiles: conectado ao seu Chrome (porta 9222)")
            return self
        kw = dict(
            user_data_dir=str(PROFILE_DIR),
            headless=self.headless,
            locale="pt-BR",
            timezone_id="America/Sao_Paulo",
            viewport={"width": 1366, "height": 850},
            args=["--disable-blink-features=AutomationControlled"],
            ignore_default_args=["--enable-automation"],
        )
        try:
            # Google Chrome instalado no computador (mais difícil de ser detectado)
            self.ctx = self._pw.chromium.launch_persistent_context(channel="chrome", **kw)
            log.info("Smiles: usando o Google Chrome instalado")
        except Exception as e:  # noqa: BLE001
            log.warning("Smiles: Google Chrome não encontrado (%s); usando o Chromium do robô",
                        str(e).splitlines()[0][:100])
            self.ctx = self._pw.chromium.launch_persistent_context(**kw)
        self.ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        return self

    def __exit__(self, *a):
        try:
            if self.cdp:
                if self._pagina_propria and self.page:
                    self.page.close()   # fecha só a aba do robô; o seu Chrome continua aberto
            elif self.ctx:
                self.ctx.close()
        finally:
            self._pw and self._pw.stop()

    def login_manual(self):
        self.page.goto(BASE, timeout=self.timeout_ms, wait_until="domcontentloaded")
        print()
        print("=" * 60)
        print("Na janela do navegador da SMILES:")
        print("  1) clique em 'Entrar' no topo do site")
        print("  2) entre com CPF/número Smiles e senha (e código, se pedir)")
        print("  3) quando aparecer seu nome/saldo, volte AQUI e aperte Enter")
        print("=" * 60)
        input()
        print("Sessão Smiles salva. URL atual:", self.page.url)

    def buscar_ida_volta(self, origem: str, destino: str, ida: date, volta: date | None, adultos: int = 1):
        url = url_busca(origem, destino, ida, volta, adultos)
        tag = f"SM-{origem}-{destino}-{ida.isoformat()}-{volta.isoformat() if volta else 'OW'}"
        log.info("Smiles: buscando %s", tag)
        jsons: dict[str, object] = {}
        busca: dict = {}
        trafego: list = []

        def on_resp(r):
            try:
                ct = (r.headers or {}).get("content-type", "")
                if r.request.resource_type not in ("xhr", "fetch", "other") and "json" not in ct:
                    return
                trafego.append((r.status, r.request.method, r.url))
                if "json" not in ct and r.request.resource_type not in ("xhr", "fetch"):
                    return
                if any(h in r.url for h in API_HINTS) and r.status in (401, 403, 406, 429):
                    busca["bloqueio"] = r.status
                    return
                try:
                    dados = r.json()
                except Exception:  # noqa: BLE001
                    return
                url_l = r.url.lower()
                if any(i in url_l for i in IGNORAR):
                    return
                eh_busca = isinstance(dados, dict) and (
                    "requestedFlightSegmentList" in dados or any(h in url_l for h in API_HINTS))
                if eh_busca or any(h in url_l for h in DIAG_HINTS):
                    nome = r.url.split("?")[0].rstrip("/").split("/")[-3:]
                    jsons["_".join(nome)] = dados
                if eh_busca:
                    busca["json"] = dados
                    busca["url"] = r.url
            except Exception:  # noqa: BLE001
                pass

        self.ctx.on("response", on_resp)   # nível do navegador: pega também abas novas e workers
        try:
            resp = self.page.goto(url, timeout=self.timeout_ms, wait_until="domcontentloaded")
            if resp and resp.status in (403, 406):
                self._debug(f"{tag}-{resp.status}")
                raise Bloqueado(f"Smiles respondeu {resp.status} na página ({tag})")
            fim = time.time() + self.timeout_ms / 1000
            while "json" not in busca and "bloqueio" not in busca and time.time() < fim:
                if "/login" in self.page.url or "login.smiles" in self.page.url:
                    raise PrecisaLogin("A Smiles pediu login. Rode:  .venv/bin/python monitor_smiles.py --login")
                self.page.wait_for_timeout(500)
            if "json" not in busca:
                log.info("Smiles: a página não fez a busca de voos em %ds", self.timeout_ms // 1000)
            self.page.wait_for_timeout(random.randint(1500, 2500))
            if "json" not in busca:
                try:
                    texto = " ".join(self.page.inner_text("body", timeout=5000).split())
                    busca["texto"] = texto[:1500]
                except Exception:  # noqa: BLE001
                    pass
        finally:
            self.ctx.remove_listener("response", on_resp)

        self.ultimas_respostas = jsons
        for nome, dados in jsons.items():
            (DEBUG_DIR / f"{tag}-{nome}.json").write_text(json.dumps(dados, ensure_ascii=False, indent=1))
        info = {"url": busca.get("url", ""), "trafego": trafego, "texto": busca.get("texto", ""),
                "pagina": url}
        if "bloqueio" in busca:
            self._debug(f"{tag}-api{busca['bloqueio']}")
            raise Bloqueado(f"API da Smiles respondeu {busca['bloqueio']} ({tag})")
        if "json" not in busca:
            self._debug(f"{tag}-semresposta")
            log.warning("Smiles: não capturei a busca de voos (%s). Veja data/debug-smiles", tag)
            return [], [], info
        idas, voltas = parse_smiles(busca["json"], origem, destino, ida, volta)
        return idas, voltas, info

    def _debug(self, nome: str):
        try:
            self.page.screenshot(path=str(DEBUG_DIR / f"{nome}.png"), full_page=True)
            (DEBUG_DIR / f"{nome}.html").write_text(self.page.content())
        except Exception:  # noqa: BLE001
            pass
