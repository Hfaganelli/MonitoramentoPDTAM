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
SALVAR_MIN_INTERVALO = timedelta(minutes=30)  # sem mudança, salva o arquivo a cada 30 min


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


# ---------------------------------------------------------------------- Cupons

_VALOR = r"([\d.]+(?:,\d{1,2})?)"
RE_MINIMO = re.compile(r"(?:acima de|a partir de|m[ií]nim[oa] de|compras? de|pedidos? de)\s*R\$\s*" + _VALOR, re.I)
RE_TETO = re.compile(r"(?:at[ée]|limite de|m[áa]ximo de)\s*R\$\s*" + _VALOR, re.I)
RE_PCT = re.compile(r"(\d{1,2}(?:[.,]\d+)?)\s*%")
RE_REAIS = re.compile(
    r"R\$\s*" + _VALOR + r"\s*(?:de\s*)?(?:OFF|de desconto|desconto)"
    + r"|cupom\s*(?:de\s*)?(?:desconto\s*(?:de\s*)?)?R\$\s*" + _VALOR
    + r"|economize\s*R\$\s*" + _VALOR, re.I)
RE_NAO_APLICAVEL = re.compile(r"ganhe|receba|pr[óo]xima compra|indicar|indique|primeira compra no app", re.I)


def analisar_cupom(texto, preco):
    """Lê um texto de cupom ('Aplicar cupom de 15%', 'R$ 50 OFF com cupom') e calcula o preço final."""
    t = " ".join((texto or "").split())
    if not preco or not re.search(r"cupo[mn]|coupon", t, re.I) or RE_NAO_APLICAVEL.search(t):
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


def melhor_cupom(soup, preco, seletores):
    vistos, melhor = set(), None
    for sel in seletores:
        for el in soup.select(sel):
            txt = el.get_text(" ", strip=True)
            if not txt or len(txt) > 400 or txt in vistos:
                continue
            vistos.add(txt)
            c = analisar_cupom(txt, preco)
            if c and (melhor is None or c["desconto"] > melhor["desconto"]):
                melhor = c
    return melhor


CUPOM_AMAZON = ['[id*="oupon"]', '[class*="oupon"]', "#promoPriceBlockMessage_feature_div",
                "#vpcButton", '[id^="promoMessage"]']
CUPOM_ML = ['[class*="coupon"]', '[class*="cupom"]', '[id*="coupon"]', ".ui-pdp-promotions-pill-label",
            ".andes-tag"]


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

    cupom = melhor_cupom(soup, preco, CUPOM_AMAZON) if preco else None
    return {"titulo": titulo, "imagem": imagem, "preco": preco, "cupom": cupom,
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

    cupom = melhor_cupom(soup, preco, CUPOM_ML) if preco else None
    return {"titulo": titulo, "imagem": imagem, "preco": preco, "cupom": cupom,
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


def _sem_horarios(h):
    h = copy.deepcopy(h)
    h.pop("atualizado_em", None)
    for r in h.get("produtos", {}).values():
        for k in ("ultima_verificacao", "ultima_falha"):
            r.pop(k, None)
    return h


def main():
    produtos = carregar(PRODUCTS_FILE, [])
    hist = carregar(HISTORY_FILE, {"atualizado_em": None, "produtos": {}})
    hist.setdefault("produtos", {})
    original = copy.deepcopy(hist)
    agora = datetime.now(timezone.utc)

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
            time.sleep(2 + random.random() * 3)
            continue

        ok += 1
        preco = dados.get("preco")
        cupom = dados.get("cupom")
        alvo = numero(p.get("preco_alvo"))
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
        if not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID):
            print("Configure os secrets TELEGRAM_BOT_TOKEN e TELEGRAM_CHAT_ID.")
            sys.exit(1)
        telegram("✅ Monitor de preços conectado. Os alertas vão chegar aqui.")
        print("Mensagem de teste enviada.")
    elif len(sys.argv) >= 3 and sys.argv[1] == "--teste":
        try:
            loja, d = coletar(sys.argv[2])
            print(json.dumps({"loja": loja, **d}, ensure_ascii=False, indent=2))
        except Exception as e:
            print(f"Erro: {e}")
            sys.exit(1)
    else:
        main()
