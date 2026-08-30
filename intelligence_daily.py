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
#
# Reuters/Bloomberg/BBC ficam como fontes monitoradas, sem raspagem automática,
# até existir feed/API autorizado para uso empresarial.

import csv
import io
import json
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

DATA = Path("data")
LATEST = DATA / "latest.json"
INTEL = DATA / "market-intelligence.json"
REF90 = DATA / "market_reference.json"

UA = "SEMIL-Currency-Intelligence/1.0"

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

def get_bytes(url, timeout=45):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()

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
# 3) Motor determinístico de inteligência
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

    up = [s["detail"] for s in signals if s["score"] > 0][:3]
    down = [s["detail"] for s in signals if s["score"] < 0][:3]

    summary = [f"Score técnico consolidado: {score:+d}."]
    if up:
        summary.append("Pressões de alta: " + " | ".join(up) + ".")
    if down:
        summary.append("Pressões de baixa: " + " | ".join(down) + ".")
    if not up and not down:
        summary.append("Os sinais monitorados estão próximos do equilíbrio.")
    summary.append("A leitura é determinística e não utiliza API de IA paga.")

    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "method": "Motor determinístico SEMIL — custo zero",
        "score10": score,
        "outlook10": {
            "bias": bias,
            "confidence": confidence,
            "summary": " ".join(summary),
            "sources": list(dict.fromkeys(sources))
        },
        "sources_consulted": [
            {"name": x} for x in list(dict.fromkeys(sources))
        ],
        "sources_monitored_not_ingested": [
            {
                "name": "Reuters",
                "reason": "Link de aprofundamento; ingestão automática depende de feed/API autorizado."
            },
            {
                "name": "Bloomberg",
                "reason": "Link de aprofundamento; ingestão automática depende de feed/API autorizado."
            },
            {
                "name": "BBC",
                "reason": "Link de aprofundamento; ingestão empresarial automatizada pode depender de autorização."
            }
        ],
        "signals": signals,
        "news": news[:6]
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
