#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SEMIL Currency Intelligence — Motor V6

Objetivo:
- Atualizar PTAX e Focus via Banco Central do Brasil.
- Medir vetores quantitativos via FRED/Federal Reserve.
- Fazer descoberta AMPLA e DINÂMICA de notícias pela web usando Google News RSS.
- Não depender de uma lista fixa de sites para funcionar.
- Selecionar apenas fontes com credibilidade suficiente para a síntese.
- Separar:
    1) o que explica o movimento atual;
    2) o que pode determinar o USD/BRL nos próximos dias/semanas.
- Se a evidência for insuficiente, declarar "EVIDÊNCIA INSUFICIENTE";
  não usar "NEUTRO" como substituto para falha de coleta.

Gera:
- data/latest.json
- data/market-intelligence.json

Somente biblioteca padrão do Python. Não exige API paga.
"""

import csv
import html as html_lib
import io
import json
import math
import re
import time
import unicodedata
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo


DATA = Path("data")
LATEST = DATA / "latest.json"
INTEL = DATA / "market-intelligence.json"
REF90 = DATA / "market_reference.json"

UA = "SEMIL-Currency-Intelligence/6.0"
TZ_BR = ZoneInfo("America/Sao_Paulo")


# ----------------------------------------------------------------------
# CONFIGURAÇÃO DE FONTES OFICIAIS
# ----------------------------------------------------------------------

FED_FEEDS = [
    ("Federal Reserve — Política Monetária",
     "https://www.federalreserve.gov/feeds/press_monetary.xml"),
    ("Federal Reserve — Discursos e Depoimentos",
     "https://www.federalreserve.gov/feeds/speeches_and_testimony.xml"),
]

FRED_SERIES = {
    "DGS2": "Treasury 2 anos",
    "DGS10": "Treasury 10 anos",
    "DTWEXBGS": "Índice amplo do dólar",
}

# Isto NÃO é uma lista fixa de sites a consultar.
# É apenas uma tabela de credibilidade aplicada DEPOIS que a busca ampla
# encontra uma fonte. A descoberta é dinâmica.
TRUST_PATTERNS = [
    (1.00, [
        "banco central do brasil", "federal reserve", "u.s. treasury",
        "us treasury", "bureau of labor statistics", "bea", "ibge"
    ]),
    (0.98, ["reuters"]),
    (0.96, ["bloomberg"]),
    (0.94, ["financial times", "wall street journal", "wsj"]),
    (0.92, ["valor economico", "valor econômico"]),
    (0.90, ["cnbc", "bbc"]),
    (0.88, ["estadao", "estadão", "folha de s.paulo", "o globo"]),
    (0.86, ["infomoney", "cnn brasil"]),
    (0.84, ["agencia brasil", "agência brasil"]),
    (0.82, ["uol"]),
    (0.80, ["investing.com"]),
    (0.78, ["exame"]),
]

# Busca ampla por ASSUNTO. Nenhuma query exige um site específico.
SEARCH_THEMES = [
    {
        "id": "movimento_usdbrl",
        "label": "Movimento do USD/BRL",
        "horizon": "hoje",
        "queries": [
            '"dólar" "real" Brasil USD BRL hoje when:2d',
            '"Brazil real" dollar USD BRL today when:2d',
        ],
    },
    {
        "id": "fed_juros",
        "label": "Fed / juros dos EUA",
        "horizon": "10d",
        "queries": [
            'Fed juros dólar corte alta taxas when:3d',
            '"Federal Reserve" dollar rate cut rate hike when:3d',
        ],
    },
    {
        "id": "treasuries_dxy",
        "label": "Dólar global / DXY / Treasuries",
        "horizon": "10d",
        "queries": [
            'DXY Treasury yields dollar emerging markets when:3d',
            'dólar global Treasuries juros americanos moedas emergentes when:3d',
        ],
    },
    {
        "id": "dados_eua",
        "label": "Dados econômicos dos EUA",
        "horizon": "10d",
        "queries": [
            'US inflation CPI PCE payroll jobs dollar Fed when:5d',
            'inflação EUA payroll emprego dólar Fed when:5d',
        ],
    },
    {
        "id": "brasil_monetario",
        "label": "BCB / Selic / Copom",
        "horizon": "90d",
        "queries": [
            'Selic Copom dólar real Brasil juros when:5d',
        ],
    },
    {
        "id": "brasil_fiscal",
        "label": "Brasil — fiscal",
        "horizon": "90d",
        "queries": [
            'Brasil fiscal déficit dívida dólar real mercado when:5d',
        ],
    },
    {
        "id": "brasil_politica",
        "label": "Brasil — política / eleições",
        "horizon": "90d",
        "queries": [
            'eleições Brasil dólar real mercado pesquisa eleitoral economia when:5d',
        ],
    },
    {
        "id": "fluxo_risco",
        "label": "Fluxo / apetite a risco",
        "horizon": "10d",
        "queries": [
            'fluxo estrangeiro Brasil real dólar emergentes risco when:5d',
            'emerging markets capital flows Brazil real dollar risk appetite when:5d',
        ],
    },
    {
        "id": "commodities",
        "label": "Commodities / China / petróleo",
        "horizon": "90d",
        "queries": [
            'China commodities oil iron ore Brazil real dollar when:5d',
            'China petróleo minério dólar real Brasil when:5d',
        ],
    },
    {
        "id": "geopolitica",
        "label": "Geopolítica / comércio",
        "horizon": "90d",
        "queries": [
            'geopolitics tariffs trade war dollar emerging markets when:5d',
        ],
    },
]


# ----------------------------------------------------------------------
# VOCABULÁRIO / CLASSIFICAÇÃO
# ----------------------------------------------------------------------

FX_TERMS = [
    "dolar", "dollar", "usd", "brl", "real brasileiro", "brazil real",
    "brazilian real", "cambio", "exchange rate", "moeda americana"
]

FACTOR_TERMS = {
    "Dólar global / DXY": [
        "dxy", "dollar index", "indice dolar", "indice do dolar",
        "dolar global", "moeda americana", "broad dollar"
    ],
    "Fed / juros dos EUA": [
        "fed", "federal reserve", "fomc", "powell", "waller",
        "rate cut", "rate hike", "interest rate", "interest rates",
        "juros americanos", "corte de juros", "alta de juros"
    ],
    "Treasuries": [
        "treasury", "treasuries", "yield", "yields",
        "titulo do tesouro", "titulos do tesouro"
    ],
    "Dados econômicos dos EUA": [
        "payroll", "jobs", "employment", "unemployment", "cpi", "pce",
        "inflation", "inflacao", "atividade dos eua", "dados dos eua",
        "consumer price", "jobless"
    ],
    "BCB / Selic / Copom": [
        "selic", "copom", "banco central do brasil", "bcb"
    ],
    "Brasil — fiscal": [
        "fiscal", "deficit", "divida", "arcabouco", "gasto publico",
        "contas publicas", "meta fiscal", "resultado primario"
    ],
    "Brasil — política / eleições": [
        "eleicao", "eleicoes", "pesquisa eleitoral", "cenario eleitoral",
        "politica brasileira", "presidencial"
    ],
    "Fluxo / apetite a risco": [
        "risk-on", "risk-off", "risk appetite", "apetite a risco",
        "aversao a risco", "fluxo estrangeiro", "entrada de capital",
        "capital inflow", "capital outflow", "emerging markets",
        "mercados emergentes"
    ],
    "Commodities / petróleo": [
        "petroleo", "oil", "commodity", "commodities", "minerio",
        "iron ore", "china"
    ],
    "Geopolítica / comércio": [
        "geopolit", "tariff", "tarifa", "trade war", "guerra comercial",
        "sanction", "sancao", "sanções", "sanction"
    ],
}

UP_PATTERNS = [
    # USD/BRL para cima
    "dolar sobe", "dolar avanca", "dolar dispara", "dolar salta",
    "dollar rises", "dollar gains", "dollar strengthens", "stronger dollar",
    "real cai", "real recua", "real enfraquece", "brazil real falls",
    # EUA / risco
    "hawkish", "higher for longer", "rate hike", "rates higher",
    "inflation accelerates", "hot inflation", "strong jobs",
    "yields rise", "yields climb", "treasury yields rise",
    "risk-off", "risk aversion", "aversao a risco",
    # Brasil
    "fiscal risk", "risco fiscal", "fiscal concern", "fiscal worries",
    "deficit widens", "debt rises", "incerteza politica",
    "political uncertainty", "selic cut", "corte da selic",
    "capital outflow", "saida de capital"
]

DOWN_PATTERNS = [
    # USD/BRL para baixo
    "dolar cai", "dolar recua", "dolar despenca", "dolar tomba",
    "dolar vai abaixo", "dollar falls", "dollar drops", "dollar weakens",
    "weaker dollar", "real sobe", "real avanca", "real fortalece",
    "brazil real rises",
    # EUA / risco
    "dovish", "rate cut", "rates lower", "inflation cools",
    "inflation eases", "inflacao desacelera", "weak jobs",
    "jobs weaken", "payroll fraco", "yields fall", "yields drop",
    "treasury yields fall", "risk-on", "risk appetite", "apetite a risco",
    # Brasil
    "capital inflow", "entrada de capital", "inflows to brazil",
    "selic hike", "alta da selic", "commodity rally"
]

HAWKISH = [
    "inflation", "price pressures", "elevated", "restrictive",
    "higher rates", "rate increase", "tightening", "upside risks",
    "above target", "persistent inflation"
]

DOVISH = [
    "rate cut", "easing", "lower rates", "downside risks",
    "weaker labor", "weak labor", "unemployment", "disinflation",
    "slowing economy", "economic slowdown"
]


# ----------------------------------------------------------------------
# UTILITÁRIOS
# ----------------------------------------------------------------------

def get_bytes(url, timeout=25, attempts=2):
    last = None
    for i in range(attempts):
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": UA,
                    "Accept": "application/json, application/xml, text/xml, text/csv, */*",
                },
            )
            with urllib.request.urlopen(req, timeout=timeout) as response:
                return response.read()
        except Exception as exc:
            last = exc
            if i < attempts - 1:
                time.sleep(1.2 + i)
    raise last


def get_json(url):
    return json.loads(get_bytes(url).decode("utf-8"))


def clamp(value, low, high):
    return max(low, min(high, value))


def pct(a, b):
    if a is None or b in (None, 0):
        return None
    return (a / b - 1.0) * 100.0


def fmt_pct(value):
    if value is None:
        return "—"
    return f"{value:+.2f}%".replace(".", ",")


def fmt_num(value, decimals=2):
    return f"{value:.{decimals}f}".replace(".", ",")


def parse_br_date(value):
    return datetime.strptime(value, "%d/%m/%Y")


def strip_accents(value):
    txt = unicodedata.normalize("NFKD", str(value or ""))
    return "".join(ch for ch in txt if not unicodedata.combining(ch))


def norm_text(value):
    txt = html_lib.unescape(str(value or ""))
    txt = strip_accents(txt).lower()
    txt = re.sub(r"[^a-z0-9/%+ .,:;()\-]+", " ", txt)
    return re.sub(r"\s+", " ", txt).strip()


def clean_title(value):
    return re.sub(r"\s+", " ", html_lib.unescape(str(value or ""))).strip()[:350]


def parse_pubdate(value):
    if not value:
        return ""
    try:
        dt = parsedate_to_datetime(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(TZ_BR).strftime("%d/%m/%Y %H:%M")
    except Exception:
        return str(value)


def normalize_title(value):
    txt = norm_text(value)
    # Retira nomes comuns de veículo anexados ao fim da manchete.
    txt = re.sub(
        r"\s+-\s+(reuters|bloomberg|bbc|uol|infomoney|cnn brasil|"
        r"valor economico|folha de s paulo|estadao|o globo|cnbc)$",
        "",
        txt,
    )
    return txt.strip()


def impact_label(score):
    if score > 0:
        return "alta"
    if score < 0:
        return "baixa"
    return "neutro"


def source_trust(source):
    txt = norm_text(source)
    for score, patterns in TRUST_PATTERNS:
        if any(norm_text(p) in txt for p in patterns):
            return score
    return 0.58


def factor_names(text):
    txt = norm_text(text)
    found = []
    for factor, terms in FACTOR_TERMS.items():
        if any(norm_text(term) in txt for term in terms):
            found.append(factor)
    return found


def direct_fx_relevance(text):
    txt = norm_text(text)
    return any(term in txt for term in FX_TERMS)


def headline_relevance(title, theme_label=""):
    txt = norm_text(title + " " + theme_label)
    factors = factor_names(txt)
    direct = direct_fx_relevance(txt)

    score = 0
    if direct:
        score += 5
    if factors:
        score += min(5, len(factors) * 2)
    if any(k in txt for k in [
        "hoje", "today", "mercado", "market", "juros", "rates",
        "inflacao", "inflation", "payroll", "fiscal", "selic",
        "copom", "treasury", "dxy", "eleicao", "election"
    ]):
        score += 2

    if score >= 8:
        return "alta", score
    if score >= 5:
        return "média", score
    return "baixa", score


def headline_direction(title):
    txt = norm_text(title)
    up = sum(1 for p in UP_PATTERNS if norm_text(p) in txt)
    down = sum(1 for p in DOWN_PATTERNS if norm_text(p) in txt)

    # Regras específicas por fator.
    if "selic" in txt:
        if any(k in txt for k in ["corte", "cut", "reduz", "queda"]):
            up += 2
        if any(k in txt for k in ["alta", "hike", "eleva", "aumenta"]):
            down += 2

    if any(k in txt for k in ["treasury", "treasuries", "yield", "yields"]):
        if any(k in txt for k in ["cai", "caem", "fall", "falls", "drop", "drops", "recuam"]):
            down += 2
        if any(k in txt for k in ["sobe", "sobem", "rise", "rises", "climb", "climbs"]):
            up += 2

    if any(k in txt for k in ["fiscal", "deficit", "divida"]):
        if any(k in txt for k in ["piora", "risco", "pressiona", "preocup", "widen", "rises"]):
            up += 2
        if any(k in txt for k in ["melhora", "superavit", "superávit", "cumpre", "reduz deficit"]):
            down += 1

    if up > down:
        return min(3, up)
    if down > up:
        return -min(3, down)
    return 0


def is_material_move(day_var, mom5):
    return abs(day_var or 0) >= 0.45 or abs(mom5 or 0) >= 0.80


# ----------------------------------------------------------------------
# 1) BANCO CENTRAL — PTAX + FOCUS
# ----------------------------------------------------------------------

def fetch_bcb():
    DATA.mkdir(parents=True, exist_ok=True)

    now = datetime.now(TZ_BR)
    end = now.date()
    start = end - timedelta(days=60)

    base = (
        "https://olinda.bcb.gov.br/olinda/servico/PTAX/versao/v1/odata/"
        "CotacaoMoedaPeriodo(moeda=@moeda,dataInicial=@dataInicial,"
        "dataFinalCotacao=@dataFinalCotacao)"
    )
    params = {
        "@moeda": "'USD'",
        "@dataInicial": f"'{start.strftime('%m-%d-%Y')}'",
        "@dataFinalCotacao": f"'{end.strftime('%m-%d-%Y')}'",
        "$format": "json",
    }
    url = base + "?" + urllib.parse.urlencode(params, safe="'$@")
    payload = get_json(url)
    values = payload.get("value", [])
    if not values:
        raise RuntimeError("BCB não retornou PTAX.")

    grouped = defaultdict(list)
    for row in values:
        raw = row.get("dataHoraCotacao") or row.get("dataCotacao")
        if not raw:
            continue
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        grouped[dt.strftime("%d/%m/%Y")].append((dt, row))

    history = []
    for date_br, rows in grouped.items():
        rows.sort(key=lambda item: item[0])
        closing = [
            item for item in rows
            if "fechamento" in str(item[1].get("tipoBoletim", "")).lower()
        ]
        dt, row = (closing or rows)[-1]
        history.append({
            "date": date_br,
            "buy": float(row["cotacaoCompra"]),
            "sell": float(row["cotacaoVenda"]),
            "iso": dt.date().isoformat(),
        })

    history.sort(key=lambda item: item["iso"])
    history = history[-45:]
    if len(history) < 2:
        raise RuntimeError("Histórico PTAX insuficiente.")

    latest = history[-1]
    previous = history[-2]

    focus = None
    try:
        # Focus é apoio macro. Não é usado como "cotação exata de 90 dias".
        year = str(end.year)
        fbase = (
            "https://olinda.bcb.gov.br/olinda/servico/Expectativas/versao/v1/"
            "odata/ExpectativasMercadoAnuais"
        )
        fparams = {
            "$filter": f"Indicador eq 'Câmbio' and DataReferencia eq '{year}'",
            "$orderby": "Data desc",
            "$top": "1",
            "$format": "json",
        }
        furl = fbase + "?" + urllib.parse.urlencode(fparams, safe="'$ ")
        fpayload = get_json(furl)
        frows = fpayload.get("value", [])
        if frows:
            row = frows[0]
            value = row.get("Mediana")
            if value is not None:
                focus = {
                    "value": float(value),
                    "date": row.get("Data") or "",
                    "reference": row.get("DataReferencia") or year,
                }
    except Exception as exc:
        print("Focus indisponível:", exc)

    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "Banco Central do Brasil",
        "ptax": {
            "date": latest["date"],
            "buy": latest["buy"],
            "sell": latest["sell"],
            "average": (latest["buy"] + latest["sell"]) / 2,
            "previous_sell": previous["sell"],
            "variation_pct": pct(latest["sell"], previous["sell"]),
        },
        "history": history,
        "focus": focus,
    }

    LATEST.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return result


# ----------------------------------------------------------------------
# 2) FRED / FEDERAL RESERVE
# ----------------------------------------------------------------------

def fred_csv(series_id):
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
    raw = get_bytes(url).decode("utf-8", errors="replace")
    rows = []
    for row in csv.DictReader(io.StringIO(raw)):
        value = row.get(series_id)
        if not value or value == ".":
            continue
        try:
            rows.append((row["DATE"], float(value)))
        except Exception:
            pass
    return rows


def rss_items(url, limit=15):
    root = ET.fromstring(get_bytes(url))
    items = []
    for item in root.findall(".//item")[:limit]:
        title = clean_title(item.findtext("title"))
        link = (item.findtext("link") or "").strip()
        desc = re.sub(r"<[^>]+>", " ", item.findtext("description") or "")
        desc = re.sub(r"\s+", " ", desc).strip()
        pub = parse_pubdate(item.findtext("pubDate"))
        items.append({
            "title": title,
            "url": link,
            "summary": desc[:600],
            "published": pub,
        })
    return items


def fed_item_score(item):
    txt = norm_text(item.get("title", "") + " " + item.get("summary", ""))
    hawk = sum(1 for term in HAWKISH if norm_text(term) in txt)
    dove = sum(1 for term in DOVISH if norm_text(term) in txt)
    if hawk > dove:
        return 1
    if dove > hawk:
        return -1
    return 0


# ----------------------------------------------------------------------
# 3) DESCOBERTA DINÂMICA DE NOTÍCIAS NA WEB
# ----------------------------------------------------------------------

def google_news_rss(query, locale="pt-BR"):
    if locale == "en-US":
        params = {"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"}
    else:
        params = {"q": query, "hl": "pt-BR", "gl": "BR", "ceid": "BR:pt-419"}

    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(params)
    root = ET.fromstring(get_bytes(url, timeout=18, attempts=2))

    rows = []
    for item in root.findall(".//item")[:40]:
        title = clean_title(item.findtext("title"))
        link = (item.findtext("link") or "").strip()
        pub = parse_pubdate(item.findtext("pubDate"))
        source_el = item.find("source")
        source = clean_title(source_el.text if source_el is not None else "")

        if not title or not link or not source:
            continue

        if title.lower().endswith(" - " + source.lower()):
            title = title[:-(len(source) + 3)].strip()

        rows.append({
            "title": title,
            "source": source,
            "published": pub,
            "url": link,
        })
    return rows


def search_one(query, theme):
    locales = ["pt-BR", "en-US"] if theme["id"] in {
        "fed_juros", "treasuries_dxy", "dados_eua", "commodities", "geopolitica"
    } else ["pt-BR"]

    collected = []
    errors = []
    for locale in locales:
        try:
            for row in google_news_rss(query, locale=locale):
                collected.append({
                    **row,
                    "_theme": theme["label"],
                    "_theme_id": theme["id"],
                    "_horizon": theme["horizon"],
                    "_locale": locale,
                    "_via": "Google News RSS",
                })
        except Exception as exc:
            errors.append(f"{locale}: {type(exc).__name__}")
    return collected, errors


def gdelt_fallback(maxrecords=50):
    query = (
        '("dollar" OR "Brazil real" OR BRL OR "dólar") '
        '(Brazil OR Brasil OR Fed OR Treasury OR fiscal OR Selic OR inflation)'
    )
    params = {
        "query": query,
        "mode": "ArtList",
        "maxrecords": str(maxrecords),
        "format": "json",
        "timespan": "48h",
    }
    url = "https://api.gdeltproject.org/api/v2/doc/doc?" + urllib.parse.urlencode(params)
    payload = get_json(url)

    rows = []
    for article in payload.get("articles", []):
        title = clean_title(article.get("title"))
        article_url = article.get("url") or ""
        host = urlparse(article_url).netloc.replace("www.", "")
        if not title:
            continue
        rows.append({
            "title": title,
            "source": host or "GDELT",
            "published": str(article.get("seendate") or ""),
            "url": article_url,
            "_theme": "Busca ampla de contingência",
            "_theme_id": "gdelt",
            "_horizon": "hoje",
            "_locale": "global",
            "_via": "GDELT contingência",
        })
    return rows


def enrich_news_row(row):
    relevance, relevance_score = headline_relevance(
        row["title"], row.get("_theme", "")
    )
    direction = headline_direction(row["title"])
    factors = factor_names(row["title"] + " " + row.get("_theme", ""))
    trust = source_trust(row.get("source", ""))

    # Fonte desconhecida: só entra na síntese se a relevância direta for muito alta.
    direct = direct_fx_relevance(row["title"])
    usable = trust >= 0.78 or (trust >= 0.58 and direct and relevance == "alta")

    # Peso da evidência: credibilidade * relevância * direção.
    rel_weight = 1.00 if relevance == "alta" else 0.72 if relevance == "média" else 0.35
    weighted = direction * trust * rel_weight

    return {
        **row,
        "relevance": relevance,
        "credibility": round(trust, 2),
        "impact": impact_label(direction),
        "horizon": row.get("_horizon", "10d"),
        "factors": factors,
        "_direction": direction,
        "_weighted": weighted,
        "_usable": usable and relevance != "baixa",
        "_relevance_score": relevance_score,
    }


def collect_dynamic_news():
    tasks = []
    for theme in SEARCH_THEMES:
        for query in theme["queries"]:
            tasks.append((query, theme))

    raw = []
    failures = []
    succeeded = 0

    # Busca em paralelo para não transformar o workflow em uma execução de 10+ min.
    with ThreadPoolExecutor(max_workers=5) as pool:
        future_map = {
            pool.submit(search_one, query, theme): (query, theme)
            for query, theme in tasks
        }
        for future in as_completed(future_map):
            query, theme = future_map[future]
            try:
                rows, errors = future.result()
                if rows:
                    succeeded += 1
                raw.extend(rows)
                if errors:
                    failures.append({
                        "name": f"Busca web — {theme['label']}",
                        "reason": "; ".join(errors),
                    })
            except Exception as exc:
                failures.append({
                    "name": f"Busca web — {theme['label']}",
                    "reason": f"Falha ({type(exc).__name__})",
                })

    enriched = [enrich_news_row(row) for row in raw]

    # Deduplicação por título normalizado, preservando a versão de maior credibilidade.
    best = {}
    for row in enriched:
        key = normalize_title(row["title"])
        if not key:
            continue
        current = best.get(key)
        quality = (
            row["credibility"],
            row["_relevance_score"],
            abs(row["_direction"]),
        )
        if current is None:
            best[key] = row
        else:
            current_quality = (
                current["credibility"],
                current["_relevance_score"],
                abs(current["_direction"]),
            )
            if quality > current_quality:
                best[key] = row

    dedup = list(best.values())

    # Se a busca principal falhou ou veio pobre, complementa com GDELT.
    usable_count = sum(1 for row in dedup if row["_usable"])
    used_fallback = False
    if usable_count < 4 or succeeded < max(3, len(tasks) // 3):
        try:
            fallback = [enrich_news_row(row) for row in gdelt_fallback()]
            used_fallback = True
            existing = set(best.keys())
            for row in fallback:
                key = normalize_title(row["title"])
                if key and key not in existing:
                    existing.add(key)
                    dedup.append(row)
        except Exception as exc:
            failures.append({
                "name": "GDELT contingência",
                "reason": f"Falha ({type(exc).__name__})",
            })

    # Ordenação: credibilidade + relevância + direção + atualidade temática.
    def sort_key(row):
        horizon_bonus = {"hoje": 3, "10d": 2, "90d": 1}.get(row["horizon"], 0)
        return (
            1 if row["_usable"] else 0,
            row["credibility"],
            row["_relevance_score"],
            abs(row["_direction"]),
            horizon_bonus,
        )

    dedup.sort(key=sort_key, reverse=True)
    usable = [row for row in dedup if row["_usable"]][:28]

    # Lista de fontes realmente encontradas e aproveitadas.
    sources_used = []
    for row in usable:
        if row["source"] not in sources_used:
            sources_used.append(row["source"])

    public_rows = []
    for row in usable:
        public_rows.append({
            "title": row["title"],
            "source": row["source"],
            "published": row["published"],
            "url": row["url"],
            "relevance": row["relevance"],
            "credibility": row["credibility"],
            "impact": row["impact"],
            "horizon": row["horizon"],
            "theme": row["_theme"],
            "factors": row["factors"],
            "_direction": row["_direction"],
            "_weighted": row["_weighted"],
        })

    return {
        "items": public_rows,
        "sources_used": sources_used,
        "failures": failures,
        "searches_total": len(tasks),
        "searches_succeeded": succeeded,
        "raw_results": len(raw),
        "material_results": len(public_rows),
        "fallback_used": used_fallback,
    }


# ----------------------------------------------------------------------
# 4) SÍNTESE CAUSAL
# ----------------------------------------------------------------------

def quantitative_factor(factor, score, detail, source, date="", horizon="10d"):
    return {
        "factor": factor,
        "score": score,
        "impact": impact_label(score),
        "evidence_count": 1,
        "sources": [source],
        "examples": [detail],
        "date": date,
        "horizon": horizon,
        "type": "quantitativo",
    }


def aggregate_market_factors(news_items, quantitative_factors):
    bucket = {}

    def ensure(name):
        if name not in bucket:
            bucket[name] = {
                "score": 0.0,
                "count": 0,
                "sources": set(),
                "examples": [],
                "horizons": Counter(),
                "quant_count": 0,
            }
        return bucket[name]

    for item in news_items:
        direction = float(item.get("_direction", 0))
        weighted = float(item.get("_weighted", 0))
        for factor in item.get("factors", []):
            slot = ensure(factor)
            slot["score"] += weighted
            slot["count"] += 1
            slot["sources"].add(item.get("source", "—"))
            slot["horizons"][item.get("horizon", "10d")] += 1
            if len(slot["examples"]) < 3:
                slot["examples"].append(item.get("title", ""))

    for factor in quantitative_factors:
        slot = ensure(factor["factor"])
        slot["score"] += float(factor["score"])
        slot["count"] += int(factor.get("evidence_count", 1))
        slot["quant_count"] += 1
        for source in factor.get("sources", []):
            slot["sources"].add(source)
        slot["horizons"][factor.get("horizon", "10d")] += 1
        for example in factor.get("examples", []):
            if len(slot["examples"]) < 3:
                slot["examples"].append(example)

    result = []
    for name, data in bucket.items():
        score = data["score"]
        # Evita classificar ruído muito pequeno como direção.
        if score > 0.35:
            impact = "alta"
        elif score < -0.35:
            impact = "baixa"
        else:
            impact = "neutro"

        horizon = data["horizons"].most_common(1)[0][0] if data["horizons"] else "10d"
        result.append({
            "factor": name,
            "impact": impact,
            "score": round(score, 2),
            "evidence_count": data["count"],
            "quantitative_count": data["quant_count"],
            "sources": sorted(data["sources"]),
            "examples": data["examples"],
            "horizon": horizon,
        })

    result.sort(
        key=lambda row: (
            abs(row["score"]),
            row["evidence_count"],
            row["quantitative_count"],
        ),
        reverse=True,
    )
    return result


def build_market_explanation(news_items, quantitative_factors, day_var, mom5):
    factors = aggregate_market_factors(news_items, quantitative_factors)

    down = [f for f in factors if f["impact"] == "baixa"]
    up = [f for f in factors if f["impact"] == "alta"]

    sources = set()
    for item in news_items:
        sources.add(item.get("source", ""))
    for factor in quantitative_factors:
        sources.update(factor.get("sources", []))
    sources.discard("")

    evidence_count = len(news_items) + len(quantitative_factors)

    if evidence_count < 2:
        status = "evidencia_insuficiente"
        summary = (
            "EVIDÊNCIA INSUFICIENTE: a coleta não reuniu base suficiente para "
            "atribuir uma causa confiável ao movimento do USD/BRL."
        )
    else:
        status = "material"
        pieces = [
            f"A leitura reuniu {evidence_count} evidência(s) em "
            f"{len(sources)} fonte(s) independente(s)."
        ]
        if down:
            pieces.append(
                "Principais vetores de BAIXA do USD/BRL: "
                + ", ".join(f["factor"] for f in down[:4]) + "."
            )
        if up:
            pieces.append(
                "Principais vetores de ALTA/risco: "
                + ", ".join(f["factor"] for f in up[:4]) + "."
            )
        if not down and not up:
            pieces.append(
                "As evidências disponíveis estão equilibradas; neutralidade "
                "foi atribuída por compensação real de forças, não por falha de coleta."
            )
        summary = " ".join(pieces)

    # Explicação do movimento de hoje.
    today_news = [n for n in news_items if n.get("horizon") == "hoje"]
    if today_news or quantitative_factors:
        today_down = [f["factor"] for f in down if f["horizon"] in ("hoje", "10d")]
        today_up = [f["factor"] for f in up if f["horizon"] in ("hoje", "10d")]
        parts = [
            f"USD/BRL no último pregão: {fmt_pct(day_var)}; "
            f"momento em 5 pregões: {fmt_pct(mom5)}."
        ]
        if today_down:
            parts.append("Pressões de baixa: " + ", ".join(today_down[:3]) + ".")
        if today_up:
            parts.append("Pressões de alta: " + ", ".join(today_up[:3]) + ".")
        today_summary = " ".join(parts)
    else:
        today_summary = "Evidência insuficiente para explicar o movimento de hoje."

    # Fatores que podem determinar o futuro.
    future_candidates = [
        f for f in factors
        if f["horizon"] in ("10d", "90d") and f["impact"] != "neutro"
    ][:7]

    return {
        "status": status,
        "summary": summary,
        "today_summary": today_summary,
        "factors": factors[:10],
        "future_drivers": future_candidates,
    }


# ----------------------------------------------------------------------
# 5) MOTOR SEMIL
# ----------------------------------------------------------------------

def build_intelligence(latest):
    history = sorted(
        latest.get("history") or [],
        key=lambda row: parse_br_date(row["date"]),
    )
    if len(history) < 2:
        raise RuntimeError("Histórico PTAX insuficiente.")

    ptax_now = float(history[-1]["sell"])
    ptax_prev = float(history[-2]["sell"])
    day_var = pct(ptax_now, ptax_prev)

    def momentum(periods):
        rows = history[-periods:]
        if len(rows) < 2:
            return 0.0
        return pct(float(rows[-1]["sell"]), float(rows[0]["sell"]))

    mom5 = momentum(6)
    mom10 = momentum(11)

    total_score = 0.0
    signals = []
    quantitative_factors = []
    sources_consulted = ["Banco Central do Brasil — PTAX"]
    sources_checked = ["Banco Central do Brasil — PTAX"]
    monitored = []

    # PTAX — momento.
    ptax_score = 0
    if mom5 >= 0.45:
        ptax_score += 2
    elif mom5 <= -0.45:
        ptax_score -= 2

    if mom10 >= 0.35:
        ptax_score += 2
    elif mom10 <= -0.35:
        ptax_score -= 2

    if day_var >= 0.55:
        ptax_score += 1
    elif day_var <= -0.55:
        ptax_score -= 1

    total_score += ptax_score
    signals.append({
        "name": "Momento da PTAX",
        "score": ptax_score,
        "detail": (
            f"5 pregões {fmt_pct(mom5)}; 10 pregões {fmt_pct(mom10)}; "
            f"último pregão {fmt_pct(day_var)}."
        ),
    })

    # Focus — apenas apoio macro.
    focus = latest.get("focus")
    if focus and focus.get("value") is not None:
        sources_consulted.append("Focus / Banco Central")
        sources_checked.append("Focus / Banco Central")
        fv = float(focus["value"])
        diff = pct(fv, ptax_now)

        fs = 0
        if diff >= 1.5:
            fs = 1
        elif diff <= -1.5:
            fs = -1

        total_score += fs
        signals.append({
            "name": "Focus/BCB",
            "score": fs,
            "detail": (
                f"Mediana anual {fmt_num(fv, 2)}; diferença frente à PTAX "
                f"{fmt_pct(diff)}. Usado apenas como apoio macro."
            ),
        })

    # FRED: dólar amplo + Treasuries.
    fred_success = 0
    for series_id, label in FRED_SERIES.items():
        try:
            rows = fred_csv(series_id)
            if len(rows) < 6:
                continue

            fred_success += 1
            last = rows[-1]
            prev5 = rows[-6]

            if series_id == "DTWEXBGS":
                change = pct(last[1], prev5[1])
                s = (
                    2 if change >= 0.40 else
                    -2 if change <= -0.40 else
                    1 if change >= 0.15 else
                    -1 if change <= -0.15 else 0
                )
                detail = (
                    f"{label}: {last[1]:.2f}; 5 observações {fmt_pct(change)}."
                )
                factor_name = "Dólar global / DXY"
            else:
                change_bp = (last[1] - prev5[1]) * 100.0
                s = (
                    1 if change_bp >= 8 else
                    -1 if change_bp <= -8 else 0
                )
                detail = (
                    f"{label}: {last[1]:.2f}%; variação em 5 observações "
                    f"{change_bp:+.0f} pb."
                )
                factor_name = "Treasuries"

            total_score += s
            signals.append({"name": label, "score": s, "detail": detail})
            quantitative_factors.append(
                quantitative_factor(
                    factor=factor_name,
                    score=s,
                    detail=detail,
                    source="Federal Reserve / FRED",
                    date=last[0],
                    horizon="10d",
                )
            )

        except Exception as exc:
            monitored.append({
                "name": f"FRED — {label}",
                "reason": f"Falha na consulta ({type(exc).__name__}).",
            })

    if fred_success:
        sources_consulted.append("Federal Reserve / FRED")
        sources_checked.append("Federal Reserve / FRED")

    # Fed oficial.
    fed_score = 0
    fed_relevant = []
    fed_feed_ok = 0

    for feed_name, feed_url in FED_FEEDS:
        try:
            items = rss_items(feed_url)
            fed_feed_ok += 1
            for item in items:
                score = fed_item_score(item)
                if score != 0:
                    fed_score += score
                    fed_relevant.append((score, item))
        except Exception as exc:
            monitored.append({
                "name": feed_name,
                "reason": f"Falha na consulta ({type(exc).__name__}).",
            })

    fed_score = int(clamp(fed_score, -2, 2))
    total_score += fed_score

    if fed_feed_ok:
        sources_consulted.append("Federal Reserve")
        sources_checked.append("Federal Reserve")

    signals.append({
        "name": "Sinalização do Federal Reserve",
        "score": fed_score,
        "detail": (
            "Leitura de comunicados e discursos oficiais recentes, classificada "
            "por sinalização hawkish/dovish."
        ),
    })

    if fed_score != 0:
        examples = [item["title"] for _, item in fed_relevant[:3]]
        quantitative_factors.append({
            "factor": "Fed / juros dos EUA",
            "score": fed_score,
            "impact": impact_label(fed_score),
            "evidence_count": max(1, len(examples)),
            "sources": ["Federal Reserve"],
            "examples": examples or ["Sinalização oficial do Federal Reserve."],
            "horizon": "10d",
            "type": "oficial",
        })

    # Busca dinâmica de notícias.
    news_pack = collect_dynamic_news()
    news_items = news_pack["items"]

    # Só fontes REALMENTE encontradas e aproveitadas entram como consultadas.
    for source in news_pack["sources_used"]:
        sources_consulted.append(source)
        sources_checked.append(source)

    monitored.extend(news_pack["failures"])

    # Score do noticiário: limitado para não dominar dados quantitativos.
    news_weighted = sum(float(item.get("_weighted", 0)) for item in news_items)
    news_score = clamp(news_weighted, -3.0, 3.0)
    total_score += news_score

    signals.append({
        "name": "Noticiário dinâmico",
        "score": round(news_score, 2),
        "detail": (
            f"{news_pack['material_results']} notícia(s) material(is), "
            f"{len(news_pack['sources_used'])} fonte(s) aproveitada(s), "
            f"{news_pack['searches_succeeded']}/{news_pack['searches_total']} "
            "busca(s) com retorno."
        ),
    })

    # Síntese causal inclui notícia + quantitativo.
    explanation = build_market_explanation(
        news_items,
        quantitative_factors,
        day_var,
        mom5,
    )

    # Evidência/qualidade da base.
    source_diversity = len(set(news_pack["sources_used"]))
    quantitative_coverage = fred_success + (1 if fed_feed_ok else 0)
    material_news = len(news_items)

    # Peso da direção das evidências.
    directional_signals = [
        float(s["score"]) for s in signals
        if isinstance(s.get("score"), (int, float)) and abs(float(s["score"])) > 0.01
    ]
    sign_target = 1 if total_score > 0 else -1 if total_score < 0 else 0
    if sign_target and directional_signals:
        agreement = sum(
            1 for s in directional_signals if (s > 0) == (sign_target > 0)
        ) / len(directional_signals)
    else:
        agreement = 0.5

    sufficient_evidence = (
        (material_news >= 3 and source_diversity >= 2)
        or quantitative_coverage >= 2
        or (material_news >= 2 and quantitative_coverage >= 1)
    )

    # "NEUTRO" só existe quando a evidência é suficiente e as forças se compensam.
    if not sufficient_evidence:
        bias = "EVIDÊNCIA INSUFICIENTE"
    elif total_score >= 6:
        bias = "ALTA FORTE"
    elif total_score >= 3:
        bias = "ALTA MODERADA"
    elif total_score >= 1:
        bias = "ALTA LEVE"
    elif total_score <= -6:
        bias = "BAIXA FORTE"
    elif total_score <= -3:
        bias = "BAIXA MODERADA"
    elif total_score <= -1:
        bias = "BAIXA LEVE"
    else:
        bias = "NEUTRO — FORÇAS EQUILIBRADAS"

    confidence = int(clamp(
        38
        + min(18, material_news * 2)
        + min(14, source_diversity * 2)
        + min(16, quantitative_coverage * 5)
        + 14 * (agreement - 0.5),
        35,
        88,
    ))
    if not sufficient_evidence:
        confidence = min(confidence, 54)

    # Drivers futuros.
    future_drivers = explanation.get("future_drivers", [])
    future_down = [f for f in future_drivers if f["impact"] == "baixa"]
    future_up = [f for f in future_drivers if f["impact"] == "alta"]

    future_parts = []
    if future_down:
        future_parts.append(
            "Fatores que podem pressionar o USD/BRL para BAIXO: "
            + ", ".join(f["factor"] for f in future_down[:4]) + "."
        )
    if future_up:
        future_parts.append(
            "Fatores que podem pressionar o USD/BRL para CIMA: "
            + ", ".join(f["factor"] for f in future_up[:4]) + "."
        )
    if not future_parts:
        future_parts.append(
            "A base atual ainda não permite identificar catalisadores futuros "
            "com confiança suficiente."
        )

    # Redação executiva.
    summary_parts = [
        f"Score técnico consolidado: {total_score:+.1f}.",
        f"PTAX: último pregão {fmt_pct(day_var)}; "
        f"5 pregões {fmt_pct(mom5)}; 10 pregões {fmt_pct(mom10)}.",
        explanation["today_summary"],
        " ".join(future_parts),
    ]
    if bias == "EVIDÊNCIA INSUFICIENTE":
        summary_parts.append(
            "A classificação não foi convertida artificialmente em NEUTRO: "
            "faltou evidência suficiente."
        )

    source_analysis = []
    counts_by_source = Counter(item["source"] for item in news_items)
    for source, count in counts_by_source.most_common(8):
        rows = [item for item in news_items if item["source"] == source]
        net = sum(float(item.get("_weighted", 0)) for item in rows)
        source_analysis.append({
            "source": source,
            "count": count,
            "score": round(net, 2),
            "status": "material",
            "summary": (
                f"{count} evidência(s) aproveitada(s); "
                f"impacto predominante: "
                f"{'alta do USD/BRL' if net > 0.2 else 'baixa do USD/BRL' if net < -0.2 else 'misto'}."
            ),
        })

    # Remove campos internos antes de salvar JSON público.
    public_news = []
    for item in news_items[:24]:
        public_news.append({
            key: value for key, value in item.items()
            if not key.startswith("_")
        })

    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "method": (
            "Motor determinístico SEMIL V6 — descoberta dinâmica na web + "
            "fontes oficiais + análise causal quantitativa + custo zero"
        ),
        "score10": round(total_score, 2),
        "market_move": {
            "material": is_material_move(day_var, mom5),
            "day_variation_pct": day_var,
            "momentum_5_pct": mom5,
            "momentum_10_pct": mom10,
            "explanation_status": explanation["status"],
        },
        "market_explanation": explanation,
        "outlook10": {
            "bias": bias,
            "confidence": confidence,
            "summary": " ".join(summary_parts),
            "sources": list(dict.fromkeys(sources_consulted)),
        },
        "future_outlook": {
            "horizon": "10 a 90 dias",
            "summary": " ".join(future_parts),
            "drivers": future_drivers,
            "confidence": confidence,
        },
        "collection_status": {
            "web_searches_total": news_pack["searches_total"],
            "web_searches_succeeded": news_pack["searches_succeeded"],
            "raw_news_results": news_pack["raw_results"],
            "material_news_results": news_pack["material_results"],
            "dynamic_sources_used": news_pack["sources_used"],
            "fallback_used": news_pack["fallback_used"],
            "quantitative_coverage": quantitative_coverage,
            "sufficient_evidence": sufficient_evidence,
        },
        "sources_consulted": [
            {"name": name}
            for name in list(dict.fromkeys(sources_consulted))
        ],
        "sources_checked": [
            {"name": name}
            for name in list(dict.fromkeys(sources_checked))
        ],
        "source_analysis": source_analysis,
        "sources_monitored_not_ingested": monitored + [
            {
                "name": "B3",
                "reason": (
                    "Curva futura segue monitorada. Sem integração pública estável "
                    "e auditável, não é usada automaticamente como referência ~90 dias."
                ),
            },
            {
                "name": "CME",
                "reason": (
                    "Referência futura segue monitorada. Sem integração pública estável "
                    "e auditável, não é usada automaticamente como referência ~90 dias."
                ),
            },
        ],
        "signals": signals,
        "news": public_news,
    }

    # Referência ~90d separada e validada.
    if REF90.exists():
        try:
            ref = json.loads(REF90.read_text(encoding="utf-8"))
            if ref.get("value") and ref.get("date"):
                result["ref90"] = {
                    "value": float(ref["value"]),
                    "date": ref["date"],
                    "source": ref.get("source", "Referência validada"),
                }
        except Exception as exc:
            print("Referência 90 dias inválida:", exc)

    INTEL.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return result


def main():
    latest = fetch_bcb()
    intelligence = build_intelligence(latest)

    print("SEMIL Currency Intelligence V6")
    print("PTAX:", latest["ptax"]["date"], latest["ptax"]["sell"])
    print(
        "10 dias:",
        intelligence["outlook10"]["bias"],
        f"confiança {intelligence['outlook10']['confidence']}%",
    )
    print(
        "Notícias materiais:",
        intelligence["collection_status"]["material_news_results"],
    )
    print(
        "Fontes dinâmicas:",
        len(intelligence["collection_status"]["dynamic_sources_used"]),
    )
    print("Arquivos gerados:")
    print(" -", LATEST)
    print(" -", INTEL)


if __name__ == "__main__":
    main()
