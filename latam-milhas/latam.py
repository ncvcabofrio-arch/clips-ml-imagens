"""Busca de passagens com milhas (LATAM Pass) usando o próprio site da LATAM.

Estratégia: abre a página de resultados com redemption=true num Chromium real
e captura a resposta JSON da API interna (/bff/air-offers/.../offers/search)
que o próprio site chama. Assim não precisamos montar os headers/cookies do
anti-bot na mão.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, asdict
from datetime import date
from pathlib import Path
from urllib.parse import urlencode

from playwright.sync_api import sync_playwright, BrowserContext, Page

log = logging.getLogger("latam")

BASE = "https://www.latamairlines.com"
SEARCH_API_HINT = "/offers/search"
DEBUG_DIR = Path(__file__).parent / "data" / "debug"
PROFILE_DIR = Path(__file__).parent / "data" / "browser-profile"


@dataclass
class Voo:
    origem: str
    destino: str
    data: str
    voo: str
    partida: str
    chegada: str
    paradas: int
    duracao_min: int | None
    tarifa: str
    milhas: float
    moeda: str
    taxas: float | None

    def chave(self) -> str:
        return f"{self.origem}-{self.destino}-{self.data}-{self.voo}-{self.tarifa}"

    def to_dict(self):
        return asdict(self)


class Bloqueado(Exception):
    """O anti-bot (Akamai) negou acesso."""


def url_busca(origem: str, destino: str, dia: date, cabine: str, adultos: int) -> str:
    q = {
        "origin": origem,
        "outbound": f"{dia.isoformat()}T12:00:00.000Z",
        "destination": destino,
        "inbound": "null",
        "adt": adultos,
        "chd": 0,
        "inf": 0,
        "trip": "OW",
        "cabin": cabine,
        "redemption": "true",
        "sort": "RECOMMENDED",
    }
    return f"{BASE}/br/pt/oferta-voos?{urlencode(q)}"


# ---------------------------------------------------------------- parser ---

def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _preco(obj) -> tuple[float | None, str, float | None]:
    """Extrai (valor, moeda, taxas) de um bloco de preço, tolerando formatos."""
    if not isinstance(obj, dict):
        return None, "", None
    price = obj.get("price") or obj.get("lowestPrice") or obj
    if isinstance(price, dict):
        valor = _num(price.get("amount") or price.get("value"))
        moeda = str(price.get("currency") or price.get("currencyCode") or "")
    else:
        valor, moeda = _num(price), ""
    taxas = None
    for k in ("taxes", "tax", "taxAmount", "fees"):
        t = obj.get(k) or (price.get(k) if isinstance(price, dict) else None)
        if isinstance(t, dict):
            taxas = _num(t.get("amount"))
        elif t is not None:
            taxas = _num(t)
        if taxas is not None:
            break
    return valor, moeda, taxas


def parse_ofertas(payload: dict, origem: str, destino: str, dia: date) -> list[Voo]:
    voos: list[Voo] = []
    itens = payload.get("content") or payload.get("data") or []
    if isinstance(itens, dict):
        itens = itens.get("content") or itens.get("offers") or []
    for item in itens:
        if not isinstance(item, dict):
            continue
        s = item.get("summary") or item
        o = s.get("origin") or {}
        d = s.get("destination") or {}
        codigo = s.get("flightCode") or ""
        if not codigo and item.get("itinerary"):
            codigo = "+".join(
                str(seg.get("flightCode") or seg.get("flightNumber") or "")
                for seg in item["itinerary"] if isinstance(seg, dict)
            )
        paradas = s.get("stopQuantity")
        if paradas is None and isinstance(item.get("itinerary"), list):
            paradas = max(len(item["itinerary"]) - 1, 0)
        base = dict(
            origem=o.get("iataCode") or origem,
            destino=d.get("iataCode") or destino,
            data=dia.isoformat(),
            voo=codigo or "?",
            partida=str(o.get("departure") or o.get("departureTime") or ""),
            chegada=str(d.get("arrival") or d.get("arrivalTime") or ""),
            paradas=int(paradas or 0),
            duracao_min=int(s["duration"]) if isinstance(s.get("duration"), (int, float)) else None,
        )
        brands = s.get("brands") or item.get("brands") or []
        achou = False
        for b in brands:
            if not isinstance(b, dict):
                continue
            valor, moeda, taxas = _preco(b)
            if valor is None:
                continue
            nome = b.get("brandText") or b.get("name") or b.get("id") or ""
            if isinstance(nome, dict):
                nome = nome.get("label") or nome.get("text") or ""
            voos.append(Voo(**base, tarifa=str(nome), milhas=valor, moeda=moeda, taxas=taxas))
            achou = True
        if not achou:
            valor, moeda, taxas = _preco(s.get("lowestPrice") and {"price": s["lowestPrice"]} or s)
            if valor is not None:
                voos.append(Voo(**base, tarifa="menor", milhas=valor, moeda=moeda, taxas=taxas))
    return voos


# --------------------------------------------------------------- browser ---

class LatamBuscador:
    def __init__(self, headless: bool = False, proxy: str = "", timeout_seg: int = 60):
        self.headless = headless
        self.proxy = proxy
        self.timeout_ms = timeout_seg * 1000
        self._pw = None
        self.ctx: BrowserContext | None = None
        self.page: Page | None = None

    def __enter__(self):
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        self._pw = sync_playwright().start()
        kw = dict(
            user_data_dir=str(PROFILE_DIR),  # perfil persistente = cookies do Akamai sobrevivem
            headless=self.headless,
            locale="pt-BR",
            timezone_id="America/Sao_Paulo",
            viewport={"width": 1366, "height": 850},
            args=["--disable-blink-features=AutomationControlled"],
        )
        if self.proxy:
            kw["proxy"] = _proxy_cfg(self.proxy)
        self.ctx = self._pw.chromium.launch_persistent_context(**kw)
        self.ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        self._aquecer()
        return self

    def __exit__(self, *a):
        try:
            self.ctx and self.ctx.close()
        finally:
            self._pw and self._pw.stop()

    def _aquecer(self):
        """Abre a home primeiro, como um humano, para ganhar cookies."""
        resp = self.page.goto(f"{BASE}/br/pt", timeout=self.timeout_ms, wait_until="domcontentloaded")
        if resp and resp.status == 403:
            self._debug("home-403")
            raise Bloqueado("Home respondeu 403 (Akamai). IP provavelmente bloqueado.")
        self.page.wait_for_timeout(4000)
        _aceitar_cookies(self.page)
        self.page.mouse.move(400, 300)
        self.page.mouse.wheel(0, 600)
        self.page.wait_for_timeout(2500)

    def buscar(self, origem: str, destino: str, dia: date, cabine: str = "Economy", adultos: int = 1) -> list[Voo]:
        url = url_busca(origem, destino, dia, cabine, adultos)
        tag = f"{origem}-{destino}-{dia.isoformat()}-{cabine}"
        log.info("Buscando %s", tag)
        capturado: dict = {}

        def on_resp(r):
            if SEARCH_API_HINT in r.url and r.request.method in ("GET", "POST"):
                capturado["status"] = r.status
                try:
                    capturado["json"] = r.json()
                except Exception as e:  # noqa: BLE001
                    capturado["erro"] = str(e)

        self.page.on("response", on_resp)
        try:
            resp = self.page.goto(url, timeout=self.timeout_ms, wait_until="domcontentloaded")
            if resp and resp.status == 403:
                self._debug(f"{tag}-403")
                raise Bloqueado(f"Página de busca respondeu 403 ({tag})")
            fim = time.time() + self.timeout_ms / 1000
            while "json" not in capturado and "erro" not in capturado and time.time() < fim:
                self.page.wait_for_timeout(500)
        finally:
            self.page.remove_listener("response", on_resp)

        if capturado.get("status") in (401, 403, 429):
            self._debug(f"{tag}-api{capturado['status']}")
            raise Bloqueado(f"API de busca respondeu {capturado['status']} ({tag})")
        if "json" not in capturado:
            self._debug(f"{tag}-semresposta")
            log.warning("Não capturei a resposta da API para %s (veja data/debug)", tag)
            return []

        payload = capturado["json"]
        voos = parse_ofertas(payload, origem, destino, dia)
        if not voos:
            # Pode ser dia sem disponibilidade OU formato que mudou: guarda o JSON para análise.
            (DEBUG_DIR / f"{tag}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=1))
            log.info("Nenhum voo extraído para %s (JSON salvo em data/debug)", tag)
        return voos

    def _debug(self, nome: str):
        try:
            self.page.screenshot(path=str(DEBUG_DIR / f"{nome}.png"), full_page=True)
            (DEBUG_DIR / f"{nome}.html").write_text(self.page.content())
        except Exception:  # noqa: BLE001
            pass


def _aceitar_cookies(page: Page):
    for sel in ("#cookies-politics-button", "button:has-text('Aceitar')", "button:has-text('Aceito')"):
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=1000):
                btn.click()
                return
        except Exception:  # noqa: BLE001
            continue


def _proxy_cfg(url: str) -> dict:
    from urllib.parse import urlparse

    u = urlparse(url)
    cfg = {"server": f"{u.scheme}://{u.hostname}:{u.port}"}
    if u.username:
        cfg["username"] = u.username
        cfg["password"] = u.password or ""
    return cfg
