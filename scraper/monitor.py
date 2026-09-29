#!/usr/bin/env python3
"""
Monitor de preços — Amazon e Mercado Livre.

Uso:
  python scraper/monitor.py              # verifica todos os produtos de products.json
  python scraper/monitor.py --teste URL  # testa uma URL e mostra o que foi extraído

Variáveis de ambiente (todas opcionais):
  TELEGRAM_BOT_TOKEN   token do bot (@BotFather)
  TELEGRAM_CHAT_ID     seu chat id
  ML_ACCESS_TOKEN      token da API do Mercado Livre (se quiser usar a API oficial)
  ALERTA_QUEDA_PCT     avisa quando o preço (sem cupom) fica X% abaixo do preço normal (padrão 10)
  ALERTA_CUPOM_PCT     avisa quando um cupom deixa o preço X% abaixo do preço normal (padrão 20)

"Preço normal" = mediana do preço nos últimos 7 dias (ignora oscilações rápidas).
"""
import copy
import hashlib
import html as htmllib
import json
import os
import statistics
import random
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from bs4 import BeautifulSoup

try:
    from curl_cffi import requests as http  # imita o TLS do Chrome: menos bloqueio
    IMPERSONATE = True
except ImportError:  # pragma: no cover
    import requests as http
    IMPERSONATE = False

ROOT = Path(__file__).resolve().parent.parent
PRODUCTS_FILE = ROOT / "products.json"
HISTORY_FILE = ROOT / "data" / "history.json"
TELEGRAM_STATE = ROOT / "data" / "telegram.json"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
ML_TOKEN = os.getenv("ML_ACCESS_TOKEN", "").strip()
def _pct(nome, padrao):
    try:
        return float(os.getenv(nome) or padrao)
    except ValueError:
        return float(padrao)


QUEDA_PCT = _pct("ALERTA_QUEDA_PCT", 10)
CUPOM_PCT = _pct("ALERTA_CUPOM_PCT", 20)
DIAS_NORMAL = 7        # janela do "preço normal"
MAX_PONTOS = 4000      # pontos de histórico por produto
PONTO_MIN_INTERVALO = timedelta(hours=1)   # sem mudança, grava 1 ponto por hora
SALVAR_MIN_INTERVALO = timedelta(minutes=15)  # sem mudança, ainda salva o horário da verificação (evita rajada de commits)
FALHAS_ALERTA = 12      # 12 falhas seguidas (~6 h rodando a cada 30 min) = avisa no Telegram
BRT = timezone(timedelta(hours=-3))  # horário de Brasília
RESUMO_DIA, RESUMO_HORA = 6, 9      # resumo semanal: domingo (6), a partir das 9h


class ErroColeta(Exception):
    pass


# ----------------------------------------------------------------- utilidades

def agora_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


_SESSAO = None


def sessao():
    """Uma sessão por execução: guarda cookies entre as páginas, como um navegador."""
    global _SESSAO
    if _SESSAO is None:
        _SESSAO = http.Session()
    return _SESSAO


def get(url, headers=None, perfil="chrome"):
    h = {
        "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.7,en;q=0.6",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    if not IMPERSONATE:
        h["User-Agent"] = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
    if headers:
        h.update(headers)
    opts = {"headers": h, "timeout": 30, "allow_redirects": True}
    if IMPERSONATE:
        opts["impersonate"] = perfil
    return sessao().get(url, **opts)


def numero(valor):
    """Converte número em formato de máquina ('1299.9', 1299.9)."""
    if valor is None:
        return None
    try:
        v = float(str(valor).strip())
        return v if v > 0 else None
    except ValueError:
        return parse_brl(valor)


def parse_brl(texto):
    """Converte 'R$ 1.234,56' / '1,234.56' / '1234' em float."""
    if texto is None:
        return None
    s = re.sub(r"[^\d,.]", "", str(texto))
    if not s:
        return None
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    elif s.count(".") >= 1:
        partes = s.split(".")
        if len(partes[-1]) == 3:  # '1.234' = milhar
            s = s.replace(".", "")
    try:
        v = float(s)
        return v if v > 0 else None
    except ValueError:
        return None


def detectar_loja(url):
    host = (urlparse(url).hostname or "").lower()
    if "amazon." in host or host in ("amzn.to", "a.co", "amzn.eu"):
        return "amazon"
    if "mercadolivre" in host or "mercadolibre" in host or host.endswith("meli.la"):
        return "mercadolivre"
    return None


def id_produto(p):
    if p.get("id"):
        return p["id"]
    url = p["url"]
    m = re.search(r"/(?:dp|gp/product|gp/aw/d|product)/([A-Z0-9]{10})", url)
    if m:
        return "amz-" + m.group(1)
    m = re.search(r"MLB-?(\d{5,})", url, re.I)
    if m:
        return "ml-MLB" + m.group(1)
    return "p-" + hashlib.md5(url.encode()).hexdigest()[:10]  # estável entre execuções


# ---------------------------------------------------------------------- Cupons

_VALOR = r"([\d.]+(?:,\d{1,2})?)"
RE_MINIMO = re.compile(r"(?:acima de|a partir de|m[ií]nim[oa] de|compras? de|pedidos? de)\s*R\$\s*" + _VALOR, re.I)
RE_TETO = re.compile(r"(?:at[ée]|limite de|m[áa]ximo de)\s*R\$\s*" + _VALOR, re.I)
RE_PCT = re.compile(r"(\d{1,2}(?:[.,]\d+)?)\s*%")
RE_REAIS = re.compile(
    r"R\$\s*" + _VALOR + r"\s*(?:de\s*)?(?:OFF|de desconto|desconto)"
    + r"|cupom\s*(?:de\s*)?(?:desconto\s*(?:de\s*)?)?R\$\s*" + _VALOR
    + r"|economize\s*R\$\s*" + _VALOR, re.I)
RE_NAO_APLICAVEL = re.compile(
    r"ganhe|receba|pr[óo]xima compra|indicar|indique"
    # cupons só para novos clientes / primeira compra (não valem para quem já compra na loja)
    r"|primeir[oa]s?\s+(?:compra|pedido)|\b1[ºªoa]\s*(?:compra|pedido)|novos?\s+clientes?|novos?\s+usu[áa]rios?"
    r"|clientes?\s+novos?|boas[- ]vindas|first\s+(?:order|purchase)|new\s+customers?", re.I)


RE_CODIGO = re.compile(r"\b(?=[A-Z0-9]*\d)(?=[A-Z0-9]*[A-Z])[A-Z0-9]{5,20}\b")


def analisar_cupom(texto, preco, checar_restricao=True):
    """Lê um texto de cupom ('Aplicar cupom de 15%', 'R$ 50 OFF com cupom') e calcula o preço final."""
    t = " ".join((texto or "").split())
    if not preco or not re.search(r"cupo[mn]|coupon", t, re.I):
        return None
    if checar_restricao and RE_NAO_APLICAVEL.search(t):
        return None
    m = RE_MINIMO.search(t)
    if m and (parse_brl(m.group(1)) or 0) > preco:
        return None  # exige compra mínima maior que o preço do produto
    teto = RE_TETO.search(t)
    teto = parse_brl(teto.group(1)) if teto else None
    limpo = RE_MINIMO.sub(" ", RE_TETO.sub(" ", t))

    desconto = None
    m = RE_PCT.search(limpo)
    if m:
        pct = float(m.group(1).replace(",", "."))
        if 0 < pct < 100:
            desconto = preco * pct / 100
            if teto:
                desconto = min(desconto, teto)
    else:
        m = RE_REAIS.search(limpo)
        if m:
            desconto = parse_brl(next(g for g in m.groups() if g))
    if not desconto or desconto >= preco:
        return None
    return {"descricao": t[:140], "desconto": round(desconto, 2),
            "preco_final": round(preco - desconto, 2)}


def _contexto(el, niveis=4, limite=900):
    """Texto dos blocos em volta do elemento (o aviso de restrição costuma ficar ao lado)."""
    textos, p = [], el
    for _ in range(niveis):
        p = p.parent
        if p is None or p.name in ("body", "html", "[document]"):
            break
        t = p.get_text(" ", strip=True)
        if len(t) > limite:
            break
        textos.append(t)
    return " ".join(textos)


def melhor_cupom(soup, preco, seletores):
    """Retorna (melhor cupom válido, lista de preços finais de cupons recusados)."""
    # códigos de cupons restritos (primeira compra etc.) mencionados em qualquer lugar da página
    restritos = set()
    for trecho in soup.find_all(string=RE_NAO_APLICAVEL):
        el = trecho.parent
        for _ in range(4):
            if el is None:
                break
            t = el.get_text(" ", strip=True)
            if len(t) > 900:
                break
            if re.search(r"cupo[mn]|coupon", t, re.I):
                restritos.update(RE_CODIGO.findall(t))
                break
            el = el.parent

    vistos, melhor, recusados = set(), None, set()
    for sel in seletores:
        for el in soup.select(sel):
            txt = el.get_text(" ", strip=True)
            if not txt or len(txt) > 400 or txt in vistos:
                continue
            vistos.add(txt)
            bruto = analisar_cupom(txt, preco, checar_restricao=False)
            if not bruto:
                continue
            restrito = (RE_NAO_APLICAVEL.search(txt + " " + _contexto(el))
                        or any(cod in txt for cod in restritos))
            if restrito:
                recusados.add(bruto["preco_final"])
                continue
            if melhor is None or bruto["desconto"] > melhor["desconto"]:
                melhor = bruto
    return melhor, sorted(recusados)


CUPOM_AMAZON = ['[id*="oupon"]', '[class*="oupon"]', "#promoPriceBlockMessage_feature_div",
                "#vpcButton", '[id^="promoMessage"]']
CUPOM_ML = ['[class*="coupon"]', '[class*="cupom"]', '[id*="coupon"]', ".ui-pdp-promotions-pill-label",
            ".andes-tag"]


# ---------------------------------------------------------------------- Amazon

_AQUECIDOS = set()


def eh_captcha_amazon(html):
    return ("validateCaptcha" in html or "api-services-support@amazon.com" in html
            or "Digite os caracteres que você vê" in html
            or "Type the characters you see" in html)


def amazon(url):
    # links curtos (amzn.to) precisam ser resolvidos antes
    if detectar_loja(url) == "amazon" and "amazon." not in (urlparse(url).hostname or ""):
        url = get(url).url

    m = re.search(r"/(?:dp|gp/product|gp/aw/d|product)/([A-Z0-9]{10})", url)
    host = urlparse(url).hostname or "www.amazon.com.br"
    if "amazon." not in host:
        host = "www.amazon.com.br"

    # 1ª visita da rodada: abre a home para receber os cookies de sessão, como um navegador
    if host not in _AQUECIDOS:
        _AQUECIDOS.add(host)
        try:
            get(f"https://{host}/")
            time.sleep(1.5 + random.random() * 2)
        except Exception:
            pass

    if m:
        asin = m.group(1)
        tentativas = [(f"https://{host}/dp/{asin}", "chrome"),
                      (f"https://{host}/gp/product/{asin}?psc=1", "edge"),
                      (f"https://{host}/gp/aw/d/{asin}", "safari_ios")]  # página mobile, mais leve
    else:
        tentativas = [(url, "chrome"), (url, "edge"), (url, "safari_ios")]

    html, motivos = None, []
    for u, perfil in tentativas:
        try:
            r = get(u, perfil=perfil)
        except Exception as e:
            motivos.append(f"conexão ({type(e).__name__})")
            continue
        if r.status_code == 404:
            raise ErroColeta("Produto não encontrado na Amazon (404). O link pode ter mudado.")
        if r.status_code == 200 and not eh_captcha_amazon(r.text):
            html = r.text
            break
        motivos.append("captcha" if r.status_code == 200 else f"HTTP {r.status_code}")
        time.sleep(4 + random.random() * 5)
    if html is None:
        raise ErroColeta("Amazon bloqueou a leitura (" + ", ".join(motivos) + "). "
                         "Nova tentativa na próxima rodada.")

    soup = BeautifulSoup(html, "html.parser")

    titulo = soup.select_one("#productTitle")
    titulo = titulo.get_text(" ", strip=True) if titulo else None

    img = soup.select_one("#landingImage") or soup.select_one("#imgBlkFront")
    imagem = None
    if img:
        imagem = img.get("data-old-hires") or img.get("src")
        dyn = img.get("data-a-dynamic-image")
        if not imagem and dyn:
            try:
                imagem = next(iter(json.loads(dyn)))
            except Exception:
                pass

    preco = None
    seletores = [
        "#corePriceDisplay_desktop_feature_div .priceToPay .a-offscreen",
        "#corePriceDisplay_desktop_feature_div .a-price .a-offscreen",
        "#corePrice_feature_div .a-price .a-offscreen",
        "#corePrice_desktop .a-price .a-offscreen",
        "#apex_desktop .a-price .a-offscreen",
        "#priceblock_dealprice",
        "#priceblock_ourprice",
        "#price_inside_buybox",
        "#kindle-price",
        "#tp_price_block_total_price_ww .a-offscreen",
        "#apex_offerDisplay_desktop .a-price .a-offscreen",
        "#buybox .a-price .a-offscreen",
        '.a-price[data-a-color="price"] .a-offscreen',
        "#corePrice_mobile_feature_div .a-offscreen",
    ]
    for sel in seletores:
        el = soup.select_one(sel)
        if el and el.get_text(strip=True):
            preco = parse_brl(el.get_text(strip=True))
            if preco:
                break

    if not preco:
        inp = soup.select_one("#twister-plus-price-data-price")
        if inp and inp.get("value"):
            preco = numero(inp["value"])

    if not preco:
        bloco = soup.select_one("#corePriceDisplay_desktop_feature_div, #corePrice_feature_div")
        if bloco:
            inteiro = bloco.select_one(".a-price-whole")
            cent = bloco.select_one(".a-price-fraction")
            if inteiro:
                txt = inteiro.get_text(strip=True).rstrip(",.") + "," + (
                    cent.get_text(strip=True) if cent else "00")
                preco = parse_brl(txt)

    disp = soup.select_one("#availability")
    disp_txt = disp.get_text(" ", strip=True).lower() if disp else ""
    indisponivel = any(k in disp_txt for k in ("indisponível", "não disponível",
                                                "currently unavailable", "unavailable"))

    if not preco and not indisponivel:
        if not titulo:
            raise ErroColeta("Página da Amazon veio sem produto (possível bloqueio).")
        raise ErroColeta("Preço não encontrado na página da Amazon.")

    cupom, recusados = melhor_cupom(soup, preco, CUPOM_AMAZON) if preco else (None, [])
    return {"titulo": titulo, "imagem": imagem, "preco": preco, "cupom": cupom,
            "cupons_recusados": recusados,
            "disponivel": bool(preco) and not indisponivel}


# ------------------------------------------------------------- Mercado Livre

def ml_ids(url):
    """Retorna (tipo, id): tipo 'item' (anúncio) ou 'produto' (catálogo /p/)."""
    q = parse_qs(urlparse(url).query)
    for chave in ("wid", "item_id"):
        if q.get(chave):
            m = re.search(r"MLB-?(\d+)", q[chave][0], re.I)
            if m:
                return "item", "MLB" + m.group(1)
    m = re.search(r"/p/(MLB\d+)", url, re.I)
    if m:
        return "produto", m.group(1).upper()
    m = re.search(r"MLB-?(\d{5,})", url, re.I)
    if m:
        return "item", "MLB" + m.group(1)
    return None, None


def ml_api(url):
    tipo, ident = ml_ids(url)
    if not ident:
        return None
    h = {"Authorization": f"Bearer {ML_TOKEN}", "Accept": "application/json"}
    if tipo == "item":
        r = get(f"https://api.mercadolibre.com/items/{ident}", headers=h)
        if r.status_code != 200:
            return None
        d = r.json()
        fotos = d.get("pictures") or []
        return {"titulo": d.get("title"),
                "imagem": (fotos[0].get("secure_url") if fotos else d.get("thumbnail")),
                "preco": numero(d.get("price")),
                "disponivel": d.get("status") == "active"}
    r = get(f"https://api.mercadolibre.com/products/{ident}", headers=h)
    if r.status_code != 200:
        return None
    d = r.json()
    bb = d.get("buy_box_winner") or {}
    fotos = d.get("pictures") or []
    return {"titulo": d.get("name"),
            "imagem": fotos[0].get("url") if fotos else None,
            "preco": numero(bb.get("price")),
            "disponivel": bool(bb.get("price"))}


def _ofertas_jsonld(obj):
    """Procura 'offers' recursivamente em JSON-LD."""
    if isinstance(obj, list):
        for x in obj:
            yield from _ofertas_jsonld(x)
    elif isinstance(obj, dict):
        if "offers" in obj:
            yield obj
        for k in ("@graph", "mainEntity"):
            if k in obj:
                yield from _ofertas_jsonld(obj[k])


def mercadolivre(url):
    if ML_TOKEN:
        try:
            r = ml_api(url)
            if r and r.get("preco"):
                return r
        except Exception as e:  # cai para leitura da página
            print(f"   API do ML falhou ({e}); lendo a página…")

    r = get(url)
    if r.status_code != 200:
        raise ErroColeta(f"Mercado Livre respondeu HTTP {r.status_code}.")
    html = r.text
    if "account-verification" in r.url or ("/gz/" in r.url and "captcha" in html.lower()):
        raise ErroColeta("Mercado Livre pediu verificação (captcha). Tenta de novo depois.")

    soup = BeautifulSoup(html, "html.parser")
    titulo = imagem = preco = None
    disponivel = True

    for s in soup.find_all("script", type="application/ld+json"):
        try:
            dados = json.loads(s.string or s.get_text() or "{}")
        except Exception:
            continue
        for prod in _ofertas_jsonld(dados):
            ofertas = prod.get("offers")
            if isinstance(ofertas, list):
                ofertas = ofertas[0] if ofertas else {}
            if not isinstance(ofertas, dict):
                continue
            p = numero(ofertas.get("price") or ofertas.get("lowPrice"))
            if p:
                preco = p
                titulo = prod.get("name") or titulo
                im = prod.get("image")
                imagem = (im[0] if isinstance(im, list) and im else im) or imagem
                av = str(ofertas.get("availability", ""))
                if av and "InStock" not in av:
                    disponivel = False
                break
        if preco:
            break

    if not preco:
        meta = soup.select_one('meta[itemprop="price"]')
        if meta and meta.get("content"):
            preco = numero(meta["content"])

    if not preco:
        bloco = soup.select_one(".ui-pdp-price__second-line") or soup.select_one(".ui-pdp-price")
        if bloco:
            fr = bloco.select_one(".andes-money-amount__fraction")
            ct = bloco.select_one(".andes-money-amount__cents")
            if fr:
                preco = parse_brl(fr.get_text(strip=True) + "," + (
                    ct.get_text(strip=True) if ct else "00"))

    if not titulo:
        og = soup.select_one('meta[property="og:title"]')
        h1 = soup.select_one("h1.ui-pdp-title")
        titulo = (h1.get_text(strip=True) if h1 else None) or (og.get("content") if og else None)
    if not imagem:
        og = soup.select_one('meta[property="og:image"]')
        imagem = og.get("content") if og else None

    if "Anúncio pausado" in html or "Publicação pausada" in html or "Anúncio finalizado" in html:
        disponivel = False

    if not preco and disponivel:
        raise ErroColeta("Preço não encontrado na página do Mercado Livre.")

    cupom, recusados = melhor_cupom(soup, preco, CUPOM_ML) if preco else (None, [])
    return {"titulo": titulo, "imagem": imagem, "preco": preco, "cupom": cupom,
            "cupons_recusados": recusados,
            "disponivel": disponivel and bool(preco)}


# ------------------------------------------------------------------ Telegram

def _tg_token():
    return TELEGRAM_TOKEN.removeprefix("bot").strip().strip('"').strip("'")


def _tg_chat():
    return TELEGRAM_CHAT_ID.strip().strip('"')


def telegram(texto):
    """Envia mensagem. Retorna (ok, detalhe)."""
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID):
        return False, "secrets TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID não configurados"
    try:
        r = http.post(f"https://api.telegram.org/bot{_tg_token()}/sendMessage",
                      json={"chat_id": _tg_chat(), "text": texto[:4000],
                            "parse_mode": "HTML", "disable_web_page_preview": True},
                      timeout=20)
        if r.status_code == 200:
            return True, "ok"
        try:
            desc = r.json().get("description", r.text[:200])
        except Exception:
            desc = r.text[:200]
        dicas = {401: "token inválido: confira TELEGRAM_BOT_TOKEN",
                 404: "token inválido: confira TELEGRAM_BOT_TOKEN",
                 400: "chat não encontrado: confira TELEGRAM_CHAT_ID e se você mandou uma mensagem ao bot",
                 403: "o bot foi bloqueado ou você ainda não iniciou conversa com ele"}
        msg = f"Telegram recusou (HTTP {r.status_code}): {desc}. {dicas.get(r.status_code, '')}"
        print("   " + msg)
        return False, msg
    except Exception as e:
        print(f"   Falha ao enviar Telegram: {e}")
        return False, str(e)


def esc(t):
    return htmllib.escape(str(t or ""), quote=False)


AJUDA = """<b>Monitor de preços</b>

<b>Adicionar:</b> mande o link do produto (Amazon ou Mercado Livre). Pode compartilhar direto do app da loja.
Com preço-alvo: <code>/add LINK 45</code> ou escreva <code>alvo 45</code> junto com o link.
Com grupo: inclua <code>#Livros</code> na mensagem.

<b>Comandos</b>
/lista  mostra seus produtos numerados
/alvo 2 45  muda o alvo do produto 2 (use 0 para tirar)
/grupo 2 Livros  muda o grupo do produto 2
/remover 2  remove o produto 2
/ajuda  mostra esta mensagem

Os comandos são lidos na próxima rodada do robô (até 30 minutos)."""

RE_URL = re.compile(r"https?://[^\s<>\"]+")
RE_NUM = re.compile(r"(?<![\w/.,])(\d+(?:[.,]\d{1,2})?)(?![\w/])")


def extrair_link(texto):
    for u in RE_URL.findall(texto or ""):
        u = u.rstrip(").,;!?'\"")
        if detectar_loja(u):
            return u
    return None


def normalizar_link(url):
    """Resolve links curtos (amzn.to, a.co, mercadolivre.com/sec, meli.la) e limpa o endereço."""
    host = (urlparse(url).hostname or "").lower()
    curto = host in ("amzn.to", "a.co", "amzn.eu") or host.endswith("meli.la") or "/sec/" in url
    if curto:
        try:
            url = get(url).url or url
        except Exception:
            pass
    m = re.search(r"/(?:dp|gp/product|gp/aw/d|product)/([A-Z0-9]{10})", url)
    if detectar_loja(url) == "amazon" and m:
        h = urlparse(url).hostname or "www.amazon.com.br"
        return f"https://{h if 'amazon.' in h else 'www.amazon.com.br'}/dp/{m.group(1)}"
    return url.split("#")[0]


def _nome(p, hist):
    reg = hist.get("produtos", {}).get(id_produto(p), {})
    return p.get("nome") or reg.get("nome") or reg.get("titulo_loja") or p["url"][:60]


def texto_lista(produtos, hist):
    if not produtos:
        return "Sua lista está vazia. Mande o link de um produto para começar."
    linhas = ["📋 <b>Seus produtos</b>"]
    for n, p in enumerate(produtos, 1):
        reg = hist.get("produtos", {}).get(id_produto(p), {})
        partes = [f"{n}. {esc(_nome(p, hist)[:60])}"]
        if reg.get("atual"):
            partes.append(brl(reg["atual"]))
        if p.get("preco_alvo"):
            partes.append(f"alvo {brl(float(p['preco_alvo']))}")
        if p.get("grupo"):
            partes.append("#" + esc(p["grupo"]))
        linhas.append(" · ".join(partes))
    linhas.append("\n/alvo N valor · /grupo N nome · /remover N")
    return "\n".join(linhas)


def comando(texto, produtos, hist):
    """Interpreta uma mensagem. Retorna (resposta, lista_mudou, id_novo)."""
    t = (texto or "").strip()
    low = t.lower()
    cmd = low.split()[0].split("@")[0] if low.startswith("/") else ""

    if cmd in ("/start", "/ajuda", "/help"):
        return AJUDA, False, None
    if cmd == "/lista":
        return texto_lista(produtos, hist), False, None

    if cmd in ("/remover", "/alvo", "/grupo"):
        partes = t.split(maxsplit=2)
        if len(partes) < 2 or not partes[1].isdigit() or not 1 <= int(partes[1]) <= len(produtos):
            return f"Use {cmd} N (o número vem de /lista).", False, None
        p = produtos[int(partes[1]) - 1]
        nome = esc(_nome(p, hist)[:60])
        if cmd == "/remover":
            produtos.remove(p)
            return f"🗑️ Removido: {nome}", True, None
        if cmd == "/alvo":
            v = parse_brl(partes[2]) if len(partes) > 2 else None
            if v:
                p["preco_alvo"] = v
                return f"🎯 Alvo de {nome}: {brl(v)}", True, None
            p.pop("preco_alvo", None)
            return f"Alvo removido de {nome}.", True, None
        g = partes[2].strip().lstrip("#") if len(partes) > 2 else ""
        if g and g != "0":
            p["grupo"] = g[:1].upper() + g[1:40]
            return f"🗂️ {nome} agora está em {esc(p['grupo'])}.", True, None
        p.pop("grupo", None)
        return f"Grupo removido de {nome}.", True, None

    link = extrair_link(t)
    if not link:
        return "Não entendi. Mande o link de um produto da Amazon ou do Mercado Livre, ou /ajuda.", False, None

    alvo = None
    resto = t.replace(link, " ")
    if cmd == "/add":
        nums = RE_NUM.findall(resto[4:])
        alvo = parse_brl(nums[0]) if nums else None
    else:
        m = re.search(r"\balvo\D{0,6}(\d+(?:[.,]\d{1,2})?)", resto, re.I)
        alvo = parse_brl(m.group(1)) if m else None
    tags = re.findall(r"#([0-9A-Za-zÀ-ÿ_-]+)", resto)
    grupo = (tags[0][:1].upper() + tags[0][1:40]) if tags else None

    url = normalizar_link(link)
    novo = {"url": url, "adicionado_em": agora_iso()}
    novo["id"] = id_produto(novo)
    existente = next((x for x in produtos if id_produto(x) == novo["id"]), None)
    if existente:
        extras = []
        if alvo:
            existente["preco_alvo"] = alvo
            extras.append(f"alvo {brl(alvo)}")
        if grupo:
            existente["grupo"] = grupo
            extras.append(f"grupo {esc(grupo)}")
        msg = f"Esse produto já está na lista: {esc(_nome(existente, hist)[:60])}."
        if extras:
            msg += " Atualizei " + " e ".join(extras) + "."
        return msg, bool(extras), None
    if alvo:
        novo["preco_alvo"] = alvo
    if grupo:
        novo["grupo"] = grupo
    produtos.append(novo)
    return None, True, novo["id"]  # a confirmação sai depois da primeira leitura


def processar_telegram(produtos, hist, estado):
    """Lê as mensagens novas enviadas ao bot. Retorna (lista_mudou, ids_novos)."""
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID):
        return False, []
    try:
        r = http.post(f"https://api.telegram.org/bot{_tg_token()}/getUpdates",
                      json={"offset": estado.get("offset", 0), "timeout": 0,
                            "allowed_updates": ["message"]}, timeout=30)
        res = r.json()
    except Exception as e:
        print(f"Telegram: não consegui ler mensagens ({e})")
        return False, []
    if not res.get("ok"):
        print(f"Telegram: {res.get('description')}")
        return False, []

    mudou, novos = False, []
    for up in res.get("result", []):
        estado["offset"] = up["update_id"] + 1
        msg = up.get("message") or {}
        if str((msg.get("chat") or {}).get("id")) != _tg_chat():
            continue  # só obedece a você
        texto = msg.get("text") or msg.get("caption") or ""
        if not texto.strip():
            continue
        print(f"Telegram: comando recebido: {texto[:60]!r}")
        resposta, m, novo = comando(texto, produtos, hist)
        mudou = mudou or m
        if novo:
            novos.append(novo)
        if resposta:
            telegram(resposta)
    return mudou, novos


def brl(v):
    return "R$ " + f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


# ---------------------------------------------------------------------- main

def coletar(url):
    loja = detectar_loja(url)
    if loja == "amazon":
        return loja, amazon(url)
    if loja == "mercadolivre":
        return loja, mercadolivre(url)
    raise ErroColeta("Loja não suportada (use links da Amazon ou do Mercado Livre).")


def carregar(caminho, padrao):
    try:
        return json.loads(caminho.read_text(encoding="utf-8"))
    except Exception:
        return padrao


def _dt(iso):
    try:
        return datetime.fromisoformat(iso)
    except Exception:
        return None


def preco_normal(historico, agora, dias=DIAS_NORMAL):
    """Mediana do preço nos últimos N dias, amostrada de hora em hora (ponderada pelo tempo)."""
    pts = [(_dt(x["t"]), x["p"]) for x in historico if x.get("p") and _dt(x.get("t"))]
    if not pts:
        return None
    inicio = max(agora - timedelta(days=dias), pts[0][0])
    amostras, i, atual = [], 0, pts[0][1]
    t = inicio
    while t <= agora:
        while i < len(pts) and pts[i][0] <= t:
            atual = pts[i][1]
            i += 1
        amostras.append(atual)
        t += timedelta(hours=1)
    if len(amostras) < 2:
        return pts[-1][1]
    return round(statistics.median(amostras), 2)


def limpar_cupons_invalidos(hist):
    """Remove do histórico cupons gravados antes do filtro atual (ex.: 'primeira compra')."""
    for reg in hist.get("produtos", {}).values():
        c = reg.get("cupom")
        if c and RE_NAO_APLICAVEL.search(c.get("descricao", "")):
            for ponto in reg.get("historico", []):
                if ponto.get("c") == c.get("preco_final"):
                    ponto.pop("c", None)
            reg["cupom"] = None
            reg["alerta_cupom"] = None


def preco_em(historico, t):
    """Preço vigente no instante t (último ponto até t)."""
    val = None
    for x in historico:
        dt = _dt(x.get("t", ""))
        if dt and dt <= t:
            val = x.get("p")
        elif dt and dt > t:
            break
    return val if val is not None else (historico[0]["p"] if historico else None)


def texto_resumo(produtos, hist, agora):
    caiu, subiu, melhor, cupom, parado, igual = [], [], [], [], [], 0
    vistos = set()
    for p in produtos:
        pid = id_produto(p)
        if pid in vistos:
            continue
        vistos.add(pid)
        reg = hist.get("produtos", {}).get(pid, {})
        nome = esc(_nome(p, hist)[:50])
        h = reg.get("historico") or []
        atual = reg.get("atual")
        if (reg.get("falhas_seguidas") or 0) >= FALHAS_ALERTA or not atual:
            parado.append(f"• {nome}")
            continue
        antes = preco_em(h, agora - timedelta(days=7))
        if antes and atual < antes * 0.995:
            caiu.append((atual / antes - 1, f"• {nome}: {brl(antes)} → <b>{brl(atual)}</b> ({(atual/antes-1)*100:.0f}%)"))
        elif antes and atual > antes * 1.005:
            subiu.append((atual / antes - 1, f"• {nome}: {brl(antes)} → {brl(atual)} (+{(atual/antes-1)*100:.0f}%)"))
        else:
            igual += 1
        if len(h) > 3 and atual <= min(x["p"] for x in h) < max(x["p"] for x in h):
            melhor.append(f"• {nome}: {brl(atual)}")
        if reg.get("cupom"):
            cupom.append(f"• {nome}: {brl(reg['cupom']['preco_final'])} com cupom")
    partes = [f"📊 <b>Resumo da semana</b> ({agora.astimezone(BRT):%d/%m})"]
    if caiu:
        partes.append("\n⬇️ <b>Caíram</b>\n" + "\n".join(x for _, x in sorted(caiu)))
    if subiu:
        partes.append("\n⬆️ <b>Subiram</b>\n" + "\n".join(x for _, x in sorted(subiu, reverse=True)))
    if melhor:
        partes.append("\n🏆 <b>No menor preço já visto</b>\n" + "\n".join(melhor))
    if cupom:
        partes.append("\n🎟️ <b>Com cupom agora</b>\n" + "\n".join(cupom))
    if parado:
        partes.append("\n⚠️ <b>Sem leitura</b>\n" + "\n".join(parado))
    if igual:
        partes.append(f"\n{igual} produto(s) sem mudança na semana.")
    if len(partes) == 1:
        partes.append("\nNenhum produto na lista ainda.")
    return "\n".join(partes)


def _sem_horarios(h):
    h = copy.deepcopy(h)
    h.pop("atualizado_em", None)
    for r in h.get("produtos", {}).values():
        for k in ("ultima_verificacao", "ultima_falha"):
            r.pop(k, None)
    return h


def main(forcar_resumo=False):
    produtos = carregar(PRODUCTS_FILE, [])
    hist = carregar(HISTORY_FILE, {"atualizado_em": None, "produtos": {}})
    hist.setdefault("produtos", {})
    limpar_cupons_invalidos(hist)
    original = copy.deepcopy(hist)
    agora = datetime.now(timezone.utc)
    estado = carregar(TELEGRAM_STATE, {"offset": 0})
    estado_original = copy.deepcopy(estado)

    # 1) comandos enviados ao bot (adicionar, remover, alvo, grupo)
    lista_mudou, novos = processar_telegram(produtos, hist, estado)
    if lista_mudou:
        PRODUCTS_FILE.write_text(json.dumps(produtos, ensure_ascii=False, indent=2) + "\n",
                                 encoding="utf-8")
        print("Lista de produtos atualizada pelo Telegram.")

    ok = falhas = 0
    vistos = set()
    for i, p in enumerate(produtos):
        if not p.get("url") or p.get("pausado"):
            continue
        pid = id_produto(p)
        if pid in vistos:  # produto repetido na lista: verifica só uma vez
            continue
        vistos.add(pid)
        reg = hist["produtos"].setdefault(pid, {"historico": []})
        print(f"[{i + 1}/{len(produtos)}] {p.get('nome') or p['url'][:70]}")

        try:
            loja, dados = coletar(p["url"])
        except Exception as e:
            falhas += 1
            reg.update({"url": p["url"], "erro": str(e), "ultima_falha": agora_iso(),
                        "falhas_seguidas": reg.get("falhas_seguidas", 0) + 1})
            print(f"   ✗ {e}")
            if reg["falhas_seguidas"] >= FALHAS_ALERTA and not reg.get("alerta_parado"):
                horas = reg["falhas_seguidas"] // 2
                telegram(f"⚠️ <b>Não consigo ler o preço</b> de {esc(_nome(p, hist)[:60])} "
                         f"há cerca de {horas} horas.\nMotivo: {esc(str(e)[:150])}\n"
                         "Continuo tentando e aviso quando voltar.\n\n" + p["url"])
                reg["alerta_parado"] = True
            time.sleep(2 + random.random() * 3)
            continue

        ok += 1
        if reg.get("alerta_parado"):
            reg["alerta_parado"] = False
            telegram(f"✅ Voltei a ler {esc(_nome(p, hist)[:60])}"
                     + (f": {brl(dados['preco'])}" if dados.get("preco") else "."))
        preco = dados.get("preco")
        cupom = dados.get("cupom")
        alvo = numero(p.get("preco_alvo"))
        # cupons que a página marca como restritos: some com eles do histórico também
        recusados = set(dados.get("cupons_recusados") or [])
        if recusados:
            for ponto in reg.get("historico", []):
                if ponto.get("c") in recusados:
                    ponto.pop("c", None)
            reg["alerta_cupom"] = None
        normal = preco_normal(reg.get("historico", []), agora)  # calculado ANTES da leitura nova

        reg.update({
            "url": p["url"],
            "loja": loja,
            "nome": p.get("nome") or dados.get("titulo") or reg.get("nome") or "Produto",
            "titulo_loja": dados.get("titulo"),
            "imagem": dados.get("imagem") or reg.get("imagem"),
            "preco_alvo": alvo,
            "disponivel": dados.get("disponivel", True),
            "ultima_verificacao": agora_iso(),
            "erro": None,
            "falhas_seguidas": 0,
            "cupom": cupom,
            "preco_normal": normal,
        })

        if not preco:
            reg["atual"] = None
            print("   – indisponível no momento")
            time.sleep(2 + random.random() * 3)
            continue

        # histórico: grava ponto quando o preço/cupom muda, ou 1 por hora
        h = reg.setdefault("historico", [])
        ponto = {"t": agora_iso(), "p": round(preco, 2)}
        if cupom:
            ponto["c"] = cupom["preco_final"]
        ult = h[-1] if h else None
        if (not ult or ult.get("p") != ponto["p"] or ult.get("c") != ponto.get("c")
                or agora - (_dt(ult["t"]) or agora) >= PONTO_MIN_INTERVALO):
            h.append(ponto)
            reg["historico"] = h[-MAX_PONTOS:]
        reg["atual"] = round(preco, 2)
        precos = [x["p"] for x in reg["historico"]]
        reg["menor"], reg["maior"] = min(precos), max(precos)

        txt = f"   ✓ {brl(preco)}"
        if normal:
            txt += f" (normal {brl(normal)})"
        if cupom:
            txt += f" | com cupom {brl(cupom['preco_final'])}"
        print(txt)

        motivos = []
        # 1) queda sem cupom
        if normal:
            if preco <= normal * (1 - QUEDA_PCT / 100):
                ultimo = reg.get("alerta_queda")
                if ultimo is None or preco <= ultimo * 0.99:  # só repete se cair mais 1%
                    pct = (1 - preco / normal) * 100
                    motivos.append(f"📉 {pct:.0f}% abaixo do preço normal ({brl(normal)}), sem cupom")
                    reg["alerta_queda"] = preco
            else:
                reg["alerta_queda"] = None

        # 2) cupom que derruba o preço
        if normal and cupom and cupom["preco_final"] <= normal * (1 - CUPOM_PCT / 100):
            ultimo = reg.get("alerta_cupom")
            if ultimo is None or cupom["preco_final"] <= ultimo * 0.99:
                pct = (1 - cupom["preco_final"] / normal) * 100
                motivos.append(f"🎟️ Cupom na página: {brl(cupom['preco_final'])} com cupom "
                               f"({pct:.0f}% abaixo do normal de {brl(normal)})\n“{cupom['descricao'][:90]}”")
                reg["alerta_cupom"] = cupom["preco_final"]
        else:
            reg["alerta_cupom"] = None

        # 3) preço-alvo (se você definiu um)
        if alvo:
            final = min(preco, cupom["preco_final"]) if cupom else preco
            if final <= alvo:
                ultimo = reg.get("ultimo_alerta")
                if ultimo is None or final < ultimo:
                    motivos.append(f"🎯 Atingiu seu preço-alvo de {brl(alvo)}")
                    reg["ultimo_alerta"] = final
            else:
                reg["ultimo_alerta"] = None

        if motivos:
            telegram(f"<b>{reg['nome']}</b>\nAgora: {brl(preco)}\n\n" + "\n".join(motivos)
                     + f"\n\n{p['url']}")

        time.sleep(2 + random.random() * 4)  # educado com as lojas

    # 2) confirma no Telegram os produtos adicionados por lá, já com o primeiro preço
    for pid in novos:
        reg = hist["produtos"].get(pid, {})
        p = next((x for x in produtos if id_produto(x) == pid), {"url": ""})
        nome = esc((reg.get("nome") or p.get("url", ""))[:70])
        if reg.get("atual"):
            extra = f"\nPreço agora: <b>{brl(reg['atual'])}</b>"
            if reg.get("cupom"):
                extra += f" ({brl(reg['cupom']['preco_final'])} com cupom)"
        else:
            extra = "\nAinda não consegui ler o preço; tento de novo na próxima rodada."
        if p.get("preco_alvo"):
            extra += f"\nAlvo: {brl(float(p['preco_alvo']))}"
        telegram(f"✅ <b>Adicionado:</b> {nome}{extra}")

    # 3) resumo semanal (domingo de manhã, horário de Brasília)
    local = agora.astimezone(BRT)
    hoje = local.date().isoformat()
    if forcar_resumo or (local.weekday() == RESUMO_DIA and local.hour >= RESUMO_HORA
                         and estado.get("ultimo_resumo") != hoje):
        ok_envio, _ = telegram(texto_resumo(produtos, hist, agora))
        if ok_envio and not forcar_resumo:
            estado["ultimo_resumo"] = hoje

    if estado != estado_original:
        TELEGRAM_STATE.parent.mkdir(parents=True, exist_ok=True)
        TELEGRAM_STATE.write_text(json.dumps(estado), encoding="utf-8")

    # só salva se algo mudou de verdade, ou a cada 30 min (evita commits a cada 10 min à toa)
    ultimo_save = _dt(original.get("atualizado_em") or "")
    mudou = _sem_horarios(hist) != _sem_horarios(original)
    if mudou or not ultimo_save or agora - ultimo_save >= SALVAR_MIN_INTERVALO:
        hist["atualizado_em"] = agora_iso()
        HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        HISTORY_FILE.write_text(json.dumps(hist, ensure_ascii=False, separators=(",", ":")),
                                encoding="utf-8")
        print("\nHistórico salvo.")
    else:
        print("\nNada mudou; histórico não precisa ser salvo agora.")
    print(f"Concluído: {ok} ok, {falhas} com falha.")


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--telegram-teste":
        print(f"Token configurado: {'sim' if TELEGRAM_TOKEN else 'NÃO'} "
              f"({len(TELEGRAM_TOKEN)} caracteres) | Chat ID configurado: "
              f"{'sim' if TELEGRAM_CHAT_ID else 'NÃO'}")
        ok, detalhe = telegram("✅ Monitor de preços conectado. Os alertas vão chegar aqui.")
        if ok:
            print("Mensagem de teste enviada com sucesso.")
        else:
            print(f"ERRO: {detalhe}")
            sys.exit(1)
    elif len(sys.argv) >= 3 and sys.argv[1] == "--teste":
        try:
            loja, d = coletar(sys.argv[2])
            print(json.dumps({"loja": loja, **d}, ensure_ascii=False, indent=2))
        except Exception as e:
            print(f"Erro: {e}")
            sys.exit(1)
    else:
        main(forcar_resumo="--com-resumo" in sys.argv)
