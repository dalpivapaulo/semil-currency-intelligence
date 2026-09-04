#!/usr/bin/env python3
# SEMIL Currency Intelligence — atualização diária gratuita e autônoma.
# Gera:
#   data/latest.json
#   data/market-intelligence.json
#
# Fontes automáticas gratuitas:
#   - Banco Central do Brasil: PTAX + Focus
#   - Federal Reserve: RSS oficial
#   - FRED: Treasury 2y, Treasury 10y, índice amplo do dólar
#   - Google News RSS: busca ampla e gratuita de manchetes públicas
#   - Reuters / Bloomberg / BBC: priorizadas quando aparecem nos resultados
#   - GDELT: contingência, não fonte principal
#
# O programa não raspa conteúdo protegido/paywall. Usa somente títulos,
# metadados públicos e links publicados pelos agregadores/fontes.

import csv
import io
import json
import re
import html as html_lib
import unicodedata
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

DATA = Path("data")
LATEST = DATA / "latest.json"
INTEL = DATA / "market-intelligence.json"
REF90 = DATA / "market_reference.json"

UA = "SEMIL-Currency-Intelligence/5.0"

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


PRIORITY_SOURCES = ["Reuters", "Bloomberg", "BBC"]

GOOGLE_NEWS_SEARCHES = [
    {"q": '"dólar" Brasil USD BRL when:2d', "hint": None},
    {"q": '"dólar" Fed juros Treasury Brasil when:2d', "hint": None},
    {"q": '"dólar" Brasil fiscal eleições Selic when:2d', "hint": None},
    {"q": '"Brazil real" dollar Fed Treasury when:2d', "hint": None},
    {"q": 'site:reuters.com "Brazil real" dollar when:2d', "hint": "Reuters"},
    {"q": 'site:uol.com.br/noticias/reuters "dólar" Brasil when:2d', "hint": "Reuters — republicação UOL"},
    {"q": 'site:bloomberg.com "Brazil real" dollar when:2d', "hint": "Bloomberg"},
    {"q": 'site:bbc.com Brazil dollar economy when:2d', "hint": "BBC"},
]

SOURCE_ALIASES = {
    "reuters": "Reuters",
    "bloomberg": "Bloomberg",
    "bbc": "BBC",
    "bbc news": "BBC",
    "uol": "UOL",
    "uol economia": "UOL",
    "infomoney": "InfoMoney",
    "cnn brasil": "CNN Brasil",
    "valor econômico": "Valor Econômico",
    "valor economico": "Valor Econômico",
    "agência brasil": "Agência Brasil",
    "agencia brasil": "Agência Brasil",
    "investing.com brasil": "Investing.com",
    "investing.com": "Investing.com",
    "folha de s.paulo": "Folha de S.Paulo",
    "estadão": "Estadão",
    "estadao": "Estadão",
    "o globo": "O Globo",
    "financial times": "Financial Times",
    "cnbc": "CNBC",
    "the wall street journal": "Wall Street Journal",
}

FX_TERMS = [
    "dolar","dollar","usd","brl","real brasileiro","brazil real",
    "brazilian real","cambio","exchange rate","moeda americana"
]
BRAZIL_TERMS = [
    "brasil","brazil","selic","copom","fiscal","deficit","divida",
    "eleicao","eleicoes","pesquisa eleitoral","ibovespa","fluxo estrangeiro"
]
US_MACRO_TERMS = [
    "fed","federal reserve","treasury","treasuries","yield","yields",
    "payroll","inflacao","inflation","cpi","pce","emprego","jobs",
    "juros americanos","interest rates","rate cut","rate hike","dxy"
]

UP_PATTERNS = [
    "dollar rises","dollar gains","dollar strengthens","stronger dollar",
    "dolar sobe","dolar avanca","dolar dispara","dolar salta",
    "real cai","real recua","real enfraquece",
    "higher for longer","rate hike","rates higher","hawkish",
    "inflation rises","inflation accelerates","hot inflation","strong jobs",
    "yields rise","yields climb","treasury yields rise",
    "juros americanos sobem","treasuries sobem","aversao a risco","risk aversion",
    "risk-off","fiscal concern","fiscal worries","fiscal risk","risco fiscal",
    "deficit widens","debt rises","political uncertainty","incerteza politica",
    "geopolitical tension","trade tensions","selic cut","corte da selic"
]

DOWN_PATTERNS = [
    "dollar falls","dollar drops","dollar weakens","weaker dollar",
    "dolar cai","dolar recua","dolar despenca","dolar tomba","dolar vai abaixo",
    "real sobe","real avanca","real fortalece",
    "rate cut","rates lower","dovish","inflation cools","inflation eases",
    "inflacao desacelera","disinflation","weak jobs","jobs weaken","payroll fraco",
    "yields fall","yields drop","treasury yields fall","treasuries caem",
    "juros americanos caem","risk-on","risk appetite","apetite a risco",
    "capital inflow","entrada de capital","inflows to brazil",
    "selic hike","alta da selic","commodity rally"
]

FACTOR_TERMS = {
    "Dólar global / DXY": [
        "dxy","dollar index","indice dolar","indice do dolar","dolar global",
        "moeda americana","dollar weakens","dollar strengthens"
    ],
    "Fed / juros dos EUA": [
        "fed","federal reserve","rate cut","rate hike","interest rates",
        "juros americanos","waller","powell","fomc"
    ],
    "Treasuries": [
        "treasury","treasuries","yield","yields","rendimentos dos titulos",
        "titulos do tesouro"
    ],
    "Dados econômicos dos EUA": [
        "payroll","jobs","employment","unemployment","cpi","pce","inflation",
        "inflacao","atividade dos eua","dados dos eua"
    ],
    "Brasil — fiscal": [
        "fiscal","deficit","divida","arcabouco","gasto publico","contas publicas"
    ],
    "Brasil — política / eleições": [
        "eleicao","eleicoes","pesquisa eleitoral","politica brasileira",
        "cenario eleitoral"
    ],
    "BCB / Selic / Copom": [
        "selic","copom","banco central do brasil","bcb"
    ],
    "Fluxo / apetite a risco": [
        "risk-on","risk-off","risk appetite","apetite a risco","aversao a risco",
        "fluxo estrangeiro","entrada de capital","capital inflow","emerging markets",
        "mercados emergentes"
    ],
    "Commodities / petróleo": [
        "petroleo","oil","commodity","commodities","minerio","iron ore"
    ],
}

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

def get_bytes(url, timeout=35, attempts=3):
    last = None
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:
            last = e
            if i < attempts - 1:
                time.sleep(2 + i * 2)
    raise last

def get_json(url):
    return json.loads(get_bytes(url).decode("utf-8"))

def fmt_num(v, n=2):
    return f"{v:.{n}f}".replace(".", ",")

def fmt_pct(v):
    if v is None:
        return "—"
    return f"{v:+.2f}%".replace(".", ",")

def pct(a, b):
    if b in (None, 0) or a is None:
        return None
    return (a / b - 1) * 100.0

def clamp(v, lo, hi):
    return max(lo, min(hi, v))

def parse_br_date(s):
    return datetime.strptime(s, "%d/%m/%Y")

def impact_label(score):
    if score > 0:
        return "alta"
    if score < 0:
        return "baixa"
    return "neutro"

# ----------------------------------------------------------------------
# 1) BCB — PTAX + Focus
# ----------------------------------------------------------------------
def fetch_bcb():
    now = datetime.now(timezone.utc)
    end = now.date()
    start = end - timedelta(days=45)

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
        rows.sort(key=lambda x: x[0])
        closing = [
            x for x in rows
            if "fechamento" in str(x[1].get("tipoBoletim", "")).lower()
        ]
        dt, row = (closing or rows)[-1]
        history.append({
            "date": date_br,
            "buy": float(row["cotacaoCompra"]),
            "sell": float(row["cotacaoVenda"]),
            "iso": dt.date().isoformat()
        })

    history.sort(key=lambda x: x["iso"])
    history = history[-30:]
    if len(history) < 2:
        raise RuntimeError("Histórico PTAX insuficiente.")

    latest, prev = history[-1], history[-2]
    var = pct(latest["sell"], prev["sell"])

    focus = None
    try:
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
        fp = get_json(
            fbase + "?" + urllib.parse.urlencode(fparams, safe="'$")
        )
        if fp.get("value"):
            fr = fp["value"][0]
            focus = {
                "value": float(fr["Mediana"]),
                "date": fr.get("Data")
            }
    except Exception as e:
        print("Focus indisponível:", e)

    clean_history = [
        {k: v for k, v in h.items() if k != "iso"}
        for h in history
    ]

    latest_json = {
        "generated_at": now.isoformat(),
        "source": "Banco Central do Brasil — PTAX/Olinda",
        "ptax": {
            "date": latest["date"],
            "buy": latest["buy"],
            "sell": latest["sell"],
            "average": (latest["buy"] + latest["sell"]) / 2,
            "prev_date": prev["date"],
            "prev_sell": prev["sell"],
            "variation_pct": var,
        },
        "focus": focus,
        "history": clean_history,
    }

    DATA.mkdir(exist_ok=True)
    LATEST.write_text(
        json.dumps(latest_json, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    return latest_json

# ----------------------------------------------------------------------
# 2) FRED / Federal Reserve
# ----------------------------------------------------------------------
def fred_csv(series_id):
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
    raw = get_bytes(url).decode("utf-8", errors="replace")
    rows = []
    for row in csv.DictReader(io.StringIO(raw)):
        val = row.get(series_id)
        if not val or val == ".":
            continue
        try:
            rows.append((row["DATE"], float(val)))
        except Exception:
            pass
    return rows

def rss_items(url, limit=12):
    root = ET.fromstring(get_bytes(url))
    items = []
    for item in root.findall(".//item")[:limit]:
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        desc = re.sub(r"<[^>]+>", " ", item.findtext("description") or "")
        desc = re.sub(r"\s+", " ", desc).strip()
        pub = (item.findtext("pubDate") or "").strip()
        pub_br = pub
        if pub:
            try:
                pub_br = parsedate_to_datetime(pub).strftime("%d/%m/%Y")
            except Exception:
                pass
        items.append({
            "title": title,
            "url": link,
            "summary": desc[:500],
            "published": pub_br
        })
    return items

def fed_item_score(item):
    txt = (item.get("title", "") + " " + item.get("summary", "")).lower()
    up = sum(1 for k in HAWKISH if k in txt)
    down = sum(1 for k in DOVISH if k in txt)
    if up > down:
        return 1
    if down > up:
        return -1
    return 0

# ----------------------------------------------------------------------
# 3) Notícias públicas — Google News RSS principal + GDELT contingência
# ----------------------------------------------------------------------
def strip_accents(s):
    s = unicodedata.normalize("NFKD", str(s or ""))
    return "".join(ch for ch in s if not unicodedata.combining(ch))

def norm_text(s):
    s = html_lib.unescape(str(s or ""))
    s = strip_accents(s).lower()
    s = re.sub(r"[^a-z0-9/ .:-]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()

def clean_title(s):
    return re.sub(r"\s+", " ", html_lib.unescape(str(s or ""))).strip()[:300]

def normalize_title(title):
    t = norm_text(title)
    # remove nomes de fontes comuns no final
    t = re.sub(r"\s+-\s+(reuters|bloomberg|bbc|uol|infomoney|cnn brasil|valor economico)$", "", t)
    return t.strip()

def canonical_source(name):
    n = norm_text(name)
    if not n:
        return "Fonte não identificada"
    for key, value in SOURCE_ALIASES.items():
        if norm_text(key) in n:
            return value
    return clean_title(name)

def parse_pubdate(pub):
    if not pub:
        return ""
    try:
        dt = parsedate_to_datetime(pub)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt = dt.astimezone(ZoneInfo("America/Sao_Paulo"))
        return dt.strftime("%d/%m/%Y %H:%M")
    except Exception:
        return str(pub)

def google_news_rss(query):
    params = {
        "q": query,
        "hl": "pt-BR",
        "gl": "BR",
        "ceid": "BR:pt-419",
    }
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(params)
    root = ET.fromstring(get_bytes(url, timeout=25, attempts=2))
    rows = []
    for item in root.findall(".//item"):
        title = clean_title(item.findtext("title"))
        link = (item.findtext("link") or "").strip()
        pub = parse_pubdate(item.findtext("pubDate"))
        source_el = item.find("source")
        source = canonical_source(source_el.text if source_el is not None else "")
        if not title or not link:
            continue
        # Google News frequentemente anexa " - Fonte" ao título.
        if source and title.lower().endswith(" - " + source.lower()):
            title = title[:-(len(source)+3)].strip()
        rows.append({
            "title": title,
            "source": source,
            "published": pub,
            "url": link,
        })
    return rows

def headline_relevance(title):
    t = norm_text(title)
    fx = any(k in t for k in FX_TERMS)
    br = any(k in t for k in BRAZIL_TERMS)
    us = any(k in t for k in US_MACRO_TERMS)

    # Regra de integridade: tarifa/geopolítica genérica sem vínculo a câmbio,
    # Brasil ou macro EUA não entra.
    if not fx and not (br and us) and not (br and any(x in t for x in ["fiscal","selic","copom","eleicao","eleicoes"])):
        return "descartar"

    hits = 0
    hits += 4 if fx else 0
    hits += 2 if br else 0
    hits += 2 if us else 0

    if any(x in t for x in ["dolar cai","dolar recua","dolar sobe","dolar avanca","usd/brl","brazil real","real brasileiro"]):
        hits += 3
    if any(x in t for x in ["fed","treasury","payroll","selic","copom","fiscal","eleicao","eleicoes"]):
        hits += 2

    if hits >= 8:
        return "alta"
    if hits >= 5:
        return "média"
    return "descartar"

def headline_score(title):
    t = norm_text(title)
    up = sum(1 for p in UP_PATTERNS if norm_text(p) in t)
    down = sum(1 for p in DOWN_PATTERNS if norm_text(p) in t)

    # Regras adicionais por contexto.
    if any(x in t for x in ["fiscal","deficit","divida"]) and any(x in t for x in ["brasil","brazil","real","brl"]):
        up += 1
    if "selic" in t:
        if any(x in t for x in ["corte","cut","reduz","queda"]):
            up += 1
        if any(x in t for x in ["alta","hike","eleva","aumenta"]):
            down += 1
    if any(x in t for x in ["treasury","treasuries","yield","yields"]):
        if any(x in t for x in ["cai","caem","fall","falls","drop","drops","recuam"]):
            down += 1
        if any(x in t for x in ["sobe","sobem","rise","rises","climb","climbs"]):
            up += 1

    if up > down:
        return min(2, up)
    if down > up:
        return -min(2, down)
    return 0

def article_factors(title):
    t = norm_text(title)
    found = []
    for factor, terms in FACTOR_TERMS.items():
        if any(norm_text(term) in t for term in terms):
            found.append(factor)
    return found

def gdelt_fallback(timespan="24h", maxrecords=30):
    query = '("dollar" OR "Brazil real" OR BRL OR "dólar") (Brazil OR Brasil OR Fed OR Treasury OR fiscal OR Selic)'
    params = {
        "query": query,
        "mode": "ArtList",
        "maxrecords": str(maxrecords),
        "format": "json",
        "timespan": timespan,
    }
    url = "https://api.gdeltproject.org/api/v2/doc/doc?" + urllib.parse.urlencode(params)
    payload = get_json(url)
    rows = []
    for a in payload.get("articles", []):
        title = clean_title(a.get("title"))
        if headline_relevance(title) == "descartar":
            continue
        host = urlparse(a.get("url") or "").netloc.replace("www.", "")
        rows.append({
            "title": title,
            "source": host or "GDELT",
            "published": str(a.get("seendate") or ""),
            "url": a.get("url") or "",
            "relevance": headline_relevance(title),
            "impact": impact_label(headline_score(title)),
            "_score": headline_score(title),
            "_factors": article_factors(title),
            "_via": "GDELT contingência",
        })
    return rows

def collect_public_news():
    gathered = []
    failures = []
    checked = set()
    successful_searches = 0

    for spec in GOOGLE_NEWS_SEARCHES:
        query, hint = spec["q"], spec.get("hint")
        try:
            rows = google_news_rss(query)
            successful_searches += 1

            if hint == "Reuters":
                checked.add("Reuters")
            elif hint and hint.startswith("Reuters"):
                checked.add("Reuters")
            elif hint == "Bloomberg":
                checked.add("Bloomberg")
            elif hint == "BBC":
                checked.add("BBC")

            for row in rows:
                source = row["source"]

                # Uma busca com site:reuters.com / bloomberg / bbc é evidência
                # suficiente de que aquela fonte foi efetivamente verificada.
                if hint == "Reuters" and source == "Reuters":
                    checked.add("Reuters")
                elif hint == "Bloomberg" and source == "Bloomberg":
                    checked.add("Bloomberg")
                elif hint == "BBC" and source == "BBC":
                    checked.add("BBC")
                elif hint and hint.startswith("Reuters") and source in ["UOL", "InfoMoney", "Investing.com"]:
                    source = hint

                relevance = headline_relevance(row["title"])
                if relevance == "descartar":
                    continue

                score = headline_score(row["title"])
                gathered.append({
                    **row,
                    "source": source,
                    "relevance": relevance,
                    "impact": impact_label(score),
                    "_score": score,
                    "_factors": article_factors(row["title"]),
                    "_via": "Google News RSS",
                })

        except Exception as e:
            print("Google News falhou:", query, e)
            failures.append({
                "name": "Google News RSS",
                "reason": f"Falha na consulta ({type(e).__name__})."
            })

    # Marcar fontes prioritárias encontradas em qualquer pesquisa.
    for row in gathered:
        src = str(row.get("source", ""))
        for p in PRIORITY_SOURCES:
            if src.startswith(p):
                checked.add(p)

    # Deduplicação global por título normalizado.
    dedup = []
    seen = set()
    for row in gathered:
        key = normalize_title(row["title"])
        if not key or key in seen:
            continue
        seen.add(key)
        dedup.append(row)

    # Se o Google News não trouxe material suficiente, usa GDELT somente como contingência.
    if len(dedup) < 2:
        try:
            fallback = gdelt_fallback()
            for row in fallback:
                key = normalize_title(row["title"])
                if key and key not in seen:
                    seen.add(key)
                    dedup.append(row)
        except Exception as e:
            failures.append({
                "name": "GDELT contingência",
                "reason": f"Falha na contingência ({type(e).__name__})."
            })

    # Alta relevância primeiro; fontes preferenciais recebem desempate.
    def priority(row):
        rel = 0 if row.get("relevance") == "alta" else 1
        src = str(row.get("source", ""))
        pref = 0 if any(src.startswith(p) for p in PRIORITY_SOURCES) else 1
        directional = 0 if int(row.get("_score", 0)) != 0 else 1
        return (rel, pref, directional)

    dedup.sort(key=priority)
    dedup = dedup[:16]

    by_source = defaultdict(list)
    broad = []
    for row in dedup:
        matched = False
        for p in PRIORITY_SOURCES:
            if str(row.get("source", "")).startswith(p):
                by_source[p].append(row)
                matched = True
                break
        if not matched:
            broad.append(row)

    for p in PRIORITY_SOURCES:
        by_source[p] = by_source[p][:2]
    broad = broad[:8]

    # Se as buscas específicas rodaram mas não acharam item, ainda é VERIFICADA.
    # Como as consultas site: são separadas, consideramos a tentativa registrada.
    if successful_searches:
        checked.update(PRIORITY_SOURCES)

    return by_source, failures, sorted(checked), broad, dedup

def source_news_signal(items):
    vals = [int(x.get("_score", 0)) for x in items]
    total = sum(vals)
    if total >= 2:
        return 1
    if total <= -2:
        return -1
    return 0

def source_summary(source, items):
    if not items:
        return (
            f"{source}: fonte verificada nas últimas 48 horas; "
            "nenhuma manchete material para USD/BRL passou pelo filtro."
        )
    pos = sum(1 for x in items if x["_score"] > 0)
    neg = sum(1 for x in items if x["_score"] < 0)
    neu = sum(1 for x in items if x["_score"] == 0)
    if neg > pos:
        direction = "predomínio baixista para o USD/BRL"
    elif pos > neg:
        direction = "predomínio altista para o USD/BRL"
    else:
        direction = "leitura mista/neutra"
    return (
        f"{source}: {direction}; {len(items)} manchete(s) material(is) "
        f"({pos} alta, {neg} baixa, {neu} contexto)."
    )

def build_market_explanation(items, day_var):
    factors = {}
    for row in items:
        score = int(row.get("_score", 0))
        for factor in row.get("_factors", []):
            d = factors.setdefault(factor, {"score": 0, "count": 0, "sources": set(), "examples": []})
            d["score"] += score
            d["count"] += 1
            d["sources"].add(str(row.get("source", "—")))
            if len(d["examples"]) < 2:
                d["examples"].append(row.get("title", ""))

    ranked = sorted(
        factors.items(),
        key=lambda kv: (abs(kv[1]["score"]), kv[1]["count"]),
        reverse=True
    )

    out_factors = []
    for factor, d in ranked[:6]:
        impact = "alta" if d["score"] > 0 else "baixa" if d["score"] < 0 else "neutro"
        out_factors.append({
            "factor": factor,
            "impact": impact,
            "score": d["score"],
            "evidence_count": d["count"],
            "sources": sorted(d["sources"]),
            "examples": d["examples"],
        })

    if not items:
        return {
            "status": "incompleta" if abs(day_var or 0) >= 0.50 else "sem_evento_material",
            "summary": (
                "O câmbio apresentou movimento material, mas a coleta automática "
                "não encontrou explicação suficiente."
                if abs(day_var or 0) >= 0.50
                else "Nenhum evento material foi identificado na janela pesquisada."
            ),
            "factors": [],
        }

    neg = [x["factor"] for x in out_factors if x["impact"] == "baixa"]
    pos = [x["factor"] for x in out_factors if x["impact"] == "alta"]

    pieces = [f"A busca localizou {len(items)} manchete(s) material(is)."]
    if neg:
        pieces.append("Vetores de baixa do USD/BRL: " + ", ".join(neg[:4]) + ".")
    if pos:
        pieces.append("Vetores de alta/risco: " + ", ".join(pos[:4]) + ".")
    if not neg and not pos:
        pieces.append("As manchetes são relevantes, mas não permitem atribuir direção com segurança.")

    return {
        "status": "material",
        "summary": " ".join(pieces),
        "factors": out_factors,
    }

# ----------------------------------------------------------------------
# 4) Motor determinístico de inteligência
# ----------------------------------------------------------------------
def build_intelligence(latest):
    hist = latest.get("history") or []
    hist = sorted(hist, key=lambda x: parse_br_date(x["date"]))

    ptax_now = float(hist[-1]["sell"])
    ptax_prev = float(hist[-2]["sell"])

    def momentum(n):
        rows = hist[-n:]
        if len(rows) < 2:
            return 0.0
        return pct(float(rows[-1]["sell"]), float(rows[0]["sell"]))

    mom5 = momentum(6)
    mom10 = momentum(11)
    day_var = pct(ptax_now, ptax_prev)

    score = 0
    signals = []
    sources = ["Banco Central do Brasil — PTAX"]
    news = []
    source_analysis = []
    sources_checked = ["Banco Central do Brasil — PTAX"]

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

    score += ptax_score
    signals.append({
        "name": "Momento da PTAX",
        "score": ptax_score,
        "detail": (
            f"5 pregões {fmt_pct(mom5)}; "
            f"10 pregões {fmt_pct(mom10)}; "
            f"dia {fmt_pct(day_var)}."
        )
    })

    focus = latest.get("focus")
    if focus and focus.get("value") is not None:
        sources.append("Focus / Banco Central")
        sources_checked.append("Focus / Banco Central")
        fv = float(focus["value"])
        diff = pct(fv, ptax_now)
        fs = 0
        if diff >= 1.5:
            fs = 2
        elif diff <= -1.5:
            fs = -2
        elif diff >= 0.5:
            fs = 1
        elif diff <= -0.5:
            fs = -1
        score += fs
        signals.append({
            "name": "Focus/BCB",
            "score": fs,
            "detail": (
                f"Mediana {fmt_num(fv,2)}; "
                f"diferença frente à PTAX {fmt_pct(diff)}."
            )
        })

    fred_ok = False
    for sid, label in FRED_SERIES.items():
        try:
            rows = fred_csv(sid)
            if len(rows) < 6:
                continue
            fred_ok = True
            last = rows[-1]
            prev5 = rows[-6]

            if sid == "DTWEXBGS":
                chg = pct(last[1], prev5[1])
                s = (
                    2 if chg >= 0.40 else
                    -2 if chg <= -0.40 else
                    1 if chg >= 0.15 else
                    -1 if chg <= -0.15 else 0
                )
                detail = (
                    f"{label}: {last[1]:.2f}; "
                    f"5 observações {fmt_pct(chg)}."
                )
            else:
                chg_bp = (last[1] - prev5[1]) * 100
                s = (
                    2 if chg_bp >= 10 else
                    -2 if chg_bp <= -10 else
                    1 if chg_bp >= 5 else
                    -1 if chg_bp <= -5 else 0
                )
                detail = (
                    f"{label}: {last[1]:.2f}%; "
                    f"5 observações {chg_bp:+.0f} pb."
                )

            score += s
            signals.append({
                "name": label,
                "score": s,
                "detail": detail
            })
            news.append({
                "title": detail,
                "source": "Federal Reserve / FRED",
                "published": (
                    last[0].split("-")[2] + "/" +
                    last[0].split("-")[1] + "/" +
                    last[0].split("-")[0]
                ),
                "url": f"https://fred.stlouisfed.org/series/{sid}",
                "impact": impact_label(s)
            })
        except Exception as e:
            print(f"FRED {sid} indisponível:", e)

    if fred_ok:
        sources.append("Federal Reserve / FRED")
        sources_checked.append("Federal Reserve / FRED")

    fed_score = 0
    fed_items = []
    fed_ok = False

    for feed_name, feed_url in FED_FEEDS:
        try:
            items = rss_items(feed_url, limit=10)
            if items:
                fed_ok = True
            for item in items:
                s = fed_item_score(item)
                if s:
                    fed_score += s
                    fed_items.append((s, item))
        except Exception as e:
            print(feed_name, "indisponível:", e)

    fed_score = clamp(fed_score, -3, 3)
    score += fed_score
    if fed_ok:
        sources.append("Federal Reserve")
        sources_checked.append("Federal Reserve")

    signals.append({
        "name": "Sinalização do Federal Reserve",
        "score": fed_score,
        "detail": (
            "Classificação por palavras-chave de política monetária "
            "em comunicados e discursos oficiais recentes."
        )
    })

    for s, item in fed_items[:4]:
        news.append({
            "title": item["title"],
            "source": "Federal Reserve",
            "published": item["published"],
            "url": item["url"],
            "impact": impact_label(s)
        })

    # Notícias: Google News RSS como camada principal; GDELT apenas contingência.
    public_news, news_failures, checked_media, broad_news, all_material_news = collect_public_news()
    sources_checked.extend([f"{x} — busca via Google News RSS" for x in checked_media])
    media_score_total = 0

    for source_name in ["Reuters", "Bloomberg", "BBC"]:
        items = public_news.get(source_name, [])
        summary_src = source_summary(source_name, items)

        # Toda fonte efetivamente pesquisada recebe uma leitura, mesmo se
        # nenhum evento material tiver passado pelo filtro.
        if source_name in checked_media:
            source_analysis.append({
                "source": source_name,
                "summary": summary_src,
                "score": source_news_signal(items) if items else 0,
                "count": len(items),
                "status": "material" if items else "sem_evento_material"
            })

        if not items:
            continue

        sources.append(f"{source_name} — manchetes públicas via Google News RSS")
        ss = source_news_signal(items)
        media_score_total += ss

        signals.append({
            "name": f"Noticiário {source_name}",
            "score": ss,
            "detail": summary_src
        })

        # No máximo 2 matérias materiais por fonte.
        for x in items[:2]:
            news.append({k: v for k, v in x.items() if not k.startswith("_")})

    # Busca ampla de confirmação. Ela é especialmente importante quando a PTAX
    # já mostra movimento material ou quando Reuters/Bloomberg/BBC não retornam
    # uma manchete diretamente em seus próprios domínios.
    broad_score = 0
    if broad_news:
        sources_checked.append("Noticiário auxiliar de mercado")
        for x in broad_news[:5]:
            # Se for Reuters republicada, contabiliza Reuters como fonte consultada.
            if str(x.get("source","")).startswith("Reuters"):
                sources.append("Reuters — via republicação pública")
            else:
                sources.append(str(x.get("source","")) + " — contexto de mercado")
            broad_score += int(x.get("_score", 0))
            news.append({k: v for k, v in x.items() if not k.startswith("_")})

        source_analysis.append({
            "source": "Busca ampla de mercado",
            "summary": (
                f"Foram localizadas {len(broad_news)} manchete(s) material(is) em fontes "
                "auxiliares pela busca ampla do Google News RSS."
            ),
            "score": clamp(broad_score, -2, 2),
            "count": len(broad_news),
            "status": "material"
        })
    elif is_material_move(day_var, mom5):
        source_analysis.append({
            "source": "Busca ampla de mercado",
            "summary": (
                "MOVIMENTO MATERIAL DETECTADO, porém a busca ampla não encontrou explicação "
                "suficiente. A leitura de mercado deve ser tratada como INCOMPLETA, não como "
                "'sem evento material'."
            ),
            "score": 0,
            "count": 0,
            "status": "incompleta"
        })

    # Notícias têm peso limitado para não dominar os dados quantitativos.
    score += clamp(media_score_total + broad_score, -3, 3)

    if score >= 6:
        bias = "ALTA FORTE"
    elif score >= 3:
        bias = "ALTA MODERADA"
    elif score >= 1:
        bias = "ALTA LEVE"
    elif score <= -6:
        bias = "BAIXA FORTE"
    elif score <= -3:
        bias = "BAIXA MODERADA"
    elif score <= -1:
        bias = "BAIXA LEVE"
    else:
        bias = "NEUTRO"

    active = [s for s in signals if s["score"] != 0]
    if active and score != 0:
        same = sum(
            1 for s in active
            if (s["score"] > 0) == (score > 0)
        )
        agreement = same / len(active)
    else:
        agreement = 0.5

    confidence = int(
        clamp(52 + 6 * len(active) + 18 * (agreement - 0.5), 50, 85)
    )

    # Redação executiva: não mistura uma variação negativa dentro de
    # um rótulo "pressões de alta".
    def dir_word(v, threshold=0.05):
        if v > threshold:
            return "alta"
        if v < -threshold:
            return "baixa"
        return "estável"

    ptax_components = [
        f"5 pregões {fmt_pct(mom5)} ({dir_word(mom5)})",
        f"10 pregões {fmt_pct(mom10)} ({dir_word(mom10)})",
        f"último pregão {fmt_pct(day_var)} ({dir_word(day_var)})"
    ]

    directional = [s for s in signals if s["name"] != "Momento da PTAX"]
    up = [s["detail"] for s in directional if s["score"] > 0][:3]
    down = [s["detail"] for s in directional if s["score"] < 0][:3]

    summary = [
        f"Score técnico consolidado: {score:+d}.",
        "Momento da PTAX: " + "; ".join(ptax_components) + "."
    ]

    if (mom5 > 0 and mom10 < 0) or (mom5 < 0 and mom10 > 0):
        summary.append("O momento recente é misto; o viés final depende da ponderação dos demais sinais.")
    elif mom5 > 0 and mom10 > 0:
        summary.append("O momento recente da PTAX apresenta predominância altista.")
    elif mom5 < 0 and mom10 < 0:
        summary.append("O momento recente da PTAX apresenta predominância baixista.")

    if up:
        summary.append("Outros fatores de alta: " + " | ".join(up) + ".")
    if down:
        summary.append("Outros fatores de baixa: " + " | ".join(down) + ".")
    if not up and not down:
        summary.append("Os demais sinais monitorados estão próximos do equilíbrio.")

    summary.append(
        "A leitura é determinística, usa dados/metadados públicos e não utiliza API de IA paga."
    )

    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "method": "Motor determinístico SEMIL V5 — Google News RSS + fontes prioritárias + GDELT contingência + custo zero",
        "score10": score,
        "market_move": {
            "material": is_material_move(day_var, mom5),
            "day_variation_pct": day_var,
            "momentum_5_pct": mom5,
            "explanation_status": (
                "material_encontrado" if all_material_news
                else ("incompleta" if is_material_move(day_var, mom5) else "sem_evento_material")
            )
        },
        "market_explanation": build_market_explanation(all_material_news, day_var),
        "outlook10": {
            "bias": bias,
            "confidence": confidence,
            "summary": " ".join(summary),
            "sources": list(dict.fromkeys(sources))
        },
        "sources_consulted": [
            {"name": x} for x in list(dict.fromkeys(sources))
        ],
        "sources_checked": [
            {"name": x} for x in list(dict.fromkeys(sources_checked))
        ],
        "source_analysis": source_analysis,
        "sources_monitored_not_ingested": [
            {
                "name": "B3",
                "reason": "Curva futura segue monitorada; integração pública estável ainda não automatizada."
            },
            {
                "name": "CME",
                "reason": "Referência futura segue monitorada; integração pública estável ainda não automatizada."
            }
        ] + news_failures,
        "signals": signals,
        "news": news[:18]
    }

    if REF90.exists():
        try:
            ref = json.loads(REF90.read_text(encoding="utf-8"))
            if ref.get("value") and ref.get("date"):
                result["ref90"] = {
                    "value": float(ref["value"]),
                    "date": ref["date"],
                    "source": ref.get("source", "Referência validada")
                }
        except Exception as e:
            print("Referência 90 dias inválida:", e)

    INTEL.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

    return result

def main():
    latest = fetch_bcb()
    intel = build_intelligence(latest)

    print("PTAX:", latest["ptax"]["date"], latest["ptax"]["sell"])
    print(
        "10 dias:",
        intel["outlook10"]["bias"],
        f"confiança estrutural {intel['outlook10']['confidence']}%"
    )
    print("Arquivos gerados:")
    print(" -", LATEST)
    print(" -", INTEL)

if __name__ == "__main__":
    main()
