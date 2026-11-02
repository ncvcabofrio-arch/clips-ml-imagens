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


class PrecisaLogin(Exception):
    """A LATAM redirecionou para a tela de login."""


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
    if isinstance(payload, list):
        itens = payload
    else:
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


def resumo_estrutura(obj, limite: int = 1200) -> str:
    """Mostra o formato de um JSON de forma compacta: chaves e o 1º item de listas."""
    def podar(o, prof=0):
        if prof > 6:
            return "…"
        if isinstance(o, dict):
            return {k: podar(v, prof + 1) for k, v in list(o.items())[:25]}
        if isinstance(o, list):
            return [f"<lista com {len(o)} itens; 1º:>", podar(o[0], prof + 1)] if o else []
        if isinstance(o, str) and len(o) > 40:
            return o[:40] + "…"
        return o
    txt = json.dumps(podar(obj), ensure_ascii=False, separators=(",", ":"))
    return txt[:limite] + ("…" if len(txt) > limite else "")


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
        jsons: dict[str, object] = {}   # todas as respostas JSON da API interna (/bff/)
        trafego: list[tuple[int, str, str]] = []

        def on_resp(r):
            try:
                if r.request.resource_type not in ("xhr", "fetch"):
                    return
                trafego.append((r.status, r.request.method, r.url))
                if "/bff/" not in r.url:
                    return
                nome = r.url.split("?")[0].split("/bff/")[-1].replace("/", "_")
                if r.status in (401, 403, 429) and SEARCH_API_HINT in r.url:
                    capturado["bloqueio"] = r.status
                    return
                try:
                    dados = r.json()
                except Exception:  # noqa: BLE001
                    return
                jsons[nome] = dados
                if SEARCH_API_HINT in r.url:
                    capturado["json"] = dados
                    capturado["url"] = r.url
            except Exception:  # noqa: BLE001
                pass

        self.page.on("response", on_resp)
        try:
            resp = self.page.goto(url, timeout=self.timeout_ms, wait_until="domcontentloaded")
            if resp and resp.status == 403:
                self._debug(f"{tag}-403")
                raise Bloqueado(f"Página de busca respondeu 403 ({tag})")
            fim = time.time() + self.timeout_ms / 1000
            while "json" not in capturado and "bloqueio" not in capturado and time.time() < fim:
                if "auth.latamairlines.com" in self.page.url or "/login" in self.page.url:
                    raise PrecisaLogin("A LATAM pediu login. Rode:  .venv/bin/python monitor.py --login")
                self.page.wait_for_timeout(500)
            self.page.wait_for_timeout(1500)  # deixa chegar respostas paralelas (ex.: calendário)
        finally:
            self.page.remove_listener("response", on_resp)

        self.ultimas_respostas = jsons
        for nome, dados in jsons.items():
            (DEBUG_DIR / f"{tag}-{nome}.json").write_text(json.dumps(dados, ensure_ascii=False, indent=1))

        if "bloqueio" in capturado:
            self._debug(f"{tag}-api{capturado['bloqueio']}")
            raise Bloqueado(f"API de busca respondeu {capturado['bloqueio']} ({tag})")
        if "json" not in capturado:
            self._debug(f"{tag}-semresposta")
            log.warning("Não capturei a busca de voos para %s (respostas salvas em data/debug)", tag)
            self._diagnostico(tag, trafego)
            return []
        log.info("Busca capturada: %s", capturado.get("url", "")[:160])

        voos = parse_ofertas(capturado["json"], origem, destino, dia)
        if not voos:
            log.info("Nenhum voo extraído para %s (JSON salvo em data/debug)", tag)
        return voos

    def login_manual(self):
        """Abre a LATAM e espera a pessoa fazer login à mão (senha + SMS).
        A sessão fica salva no perfil do navegador (data/browser-profile)."""
        self.page.goto(f"{BASE}/br/pt", timeout=self.timeout_ms, wait_until="domcontentloaded")
        print()
        print("=" * 60)
        print("Na janela do navegador que abriu:")
        print("  1) clique em 'Entrar' / 'Fazer login' no topo do site")
        print("  2) entre com seu e-mail/CPF, senha e o código SMS")
        print("  3) quando aparecer seu nome logado, volte AQUI e aperte Enter")
        print("Não tem pressa: a janela fica aberta até você apertar Enter.")
        print("=" * 60)
        input()
        print("Sessão salva. URL atual:", self.page.url)

    def _diagnostico(self, tag: str, trafego: list):
        """Escreve (e imprime) um resumo em texto do que a página carregou."""
        linhas = [f"URL final: {self.page.url}"]
        try:
            linhas.append(f"Título: {self.page.title()}")
            texto = self.page.inner_text("body", timeout=5000)
            texto = " ".join(texto.split())[:700]
            linhas.append(f"Texto da página: {texto}")
        except Exception as e:  # noqa: BLE001
            linhas.append(f"(não li o texto da página: {e})")
        linhas.append(f"Chamadas XHR/fetch ({len(trafego)}):")
        for st, metodo, u in trafego[-40:]:
            linhas.append(f"  {st} {metodo} {u[:170]}")
        rel = "\n".join(linhas)
        (DEBUG_DIR / f"{tag}-diagnostico.txt").write_text(rel)
        print("\n----- DIAGNÓSTICO -----\n" + rel + "\n-----------------------")

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
