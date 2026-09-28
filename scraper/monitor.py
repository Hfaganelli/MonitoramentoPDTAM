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
  ALERTA_QUEDA_PCT     avisa quando o preço cai X% desde a última verificação (padrão 10)
"""
import json
import os
import random
import re
import sys
import time
from datetime import datetime, timezone
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

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
ML_TOKEN = os.getenv("ML_ACCESS_TOKEN", "").strip()
try:
    QUEDA_PCT = float(os.getenv("ALERTA_QUEDA_PCT") or 10)
except ValueError:
    QUEDA_PCT = 10.0
MAX_PONTOS = 3000  # pontos de histórico guardados por produto


class ErroColeta(Exception):
    pass


# ----------------------------------------------------------------- utilidades

def agora_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def get(url, headers=None):
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
        opts["impersonate"] = "chrome"
    return http.get(url, **opts)


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
    return "p-" + str(abs(hash(url)) % 10**10)


# ---------------------------------------------------------------------- Amazon

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
    url_limpa = f"https://{host}/dp/{m.group(1)}" if m else url

    html, status = "", None
    for tentativa in range(3):
        r = get(url_limpa)
        html, status = r.text, r.status_code
        if status == 200 and not eh_captcha_amazon(html):
            break
        time.sleep(6 + random.random() * 8)
    else:
        raise ErroColeta(f"Amazon bloqueou a leitura (HTTP {status} / captcha). "
                         "Tenta de novo na próxima rodada.")

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

    return {"titulo": titulo, "imagem": imagem, "preco": preco,
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

    return {"titulo": titulo, "imagem": imagem, "preco": preco,
            "disponivel": disponivel and bool(preco)}


# ------------------------------------------------------------------ Telegram

def telegram(texto):
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID):
        return
    try:
        http.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                  json={"chat_id": TELEGRAM_CHAT_ID, "text": texto,
                        "parse_mode": "HTML", "disable_web_page_preview": False},
                  timeout=20)
    except Exception as e:
        print(f"   Falha ao enviar Telegram: {e}")


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


def main():
    produtos = carregar(PRODUCTS_FILE, [])
    hist = carregar(HISTORY_FILE, {"atualizado_em": None, "produtos": {}})
    hist.setdefault("produtos", {})

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
            reg.update({"url": p["url"], "erro": str(e), "ultima_falha": agora_iso()})
            print(f"   ✗ {e}")
            time.sleep(3 + random.random() * 4)
            continue

        ok += 1
        preco = dados.get("preco")
        anterior = reg.get("atual")
        alvo = numero(p.get("preco_alvo"))

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
        })

        if preco:
            reg["historico"].append({"t": agora_iso(), "p": round(preco, 2)})
            reg["historico"] = reg["historico"][-MAX_PONTOS:]
            reg["atual"] = round(preco, 2)
            precos = [x["p"] for x in reg["historico"]]
            reg["menor"], reg["maior"] = min(precos), max(precos)
            print(f"   ✓ {brl(preco)}" + (f" (antes {brl(anterior)})" if anterior else ""))

            motivos = []
            if alvo:
                if preco <= alvo:
                    ult = reg.get("ultimo_alerta")
                    if ult is None or preco < ult:
                        motivos.append(f"🎯 Atingiu seu preço-alvo de {brl(alvo)}")
                        reg["ultimo_alerta"] = preco
                else:
                    reg["ultimo_alerta"] = None  # volta a avisar quando cair de novo
            if anterior and preco <= anterior * (1 - QUEDA_PCT / 100):
                queda = (1 - preco / anterior) * 100
                motivos.append(f"📉 Caiu {queda:.0f}% (era {brl(anterior)})")
            if len(precos) > 3 and preco < min(precos[:-1]):
                motivos.append("🏆 Menor preço já registrado")

            if motivos:
                telegram(f"<b>{reg['nome']}</b>\n{brl(preco)}\n" + "\n".join(motivos)
                         + f"\n\n{p['url']}")
        else:
            reg["atual"] = None
            print("   – indisponível no momento")

        time.sleep(4 + random.random() * 6)  # educado com as lojas

    hist["atualizado_em"] = agora_iso()
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    HISTORY_FILE.write_text(json.dumps(hist, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nConcluído: {ok} ok, {falhas} com falha.")


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--teste":
        try:
            loja, d = coletar(sys.argv[2])
            print(json.dumps({"loja": loja, **d}, ensure_ascii=False, indent=2))
        except Exception as e:
            print(f"Erro: {e}")
            sys.exit(1)
    else:
        main()
