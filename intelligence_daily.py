#!/usr/bin/env python3
# SEMIL Currency Intelligence — motor determinístico sem API paga.
# Fontes automáticas gratuitas: data/latest.json (BCB/PTAX + Focus),
# Federal Reserve RSS e FRED (Treasuries + índice amplo do dólar).
# Reuters/Bloomberg/BBC permanecem apenas como fontes monitoradas até existir
# um feed/API cujo uso automatizado seja autorizado para a SEMIL.

import csv
import io
import json
import math
import re
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

DATA_DIR = Path("data")
LATEST = DATA_DIR / "latest.json"
OUTPUT = DATA_DIR / "market-intelligence.json"
MANUAL_REF90 = DATA_DIR / "market_reference.json"

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

def http_get(url, timeout=40):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()

def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))

def pct(a, b):
    if b in (None, 0) or a is None:
        return None
    return (a / b - 1.0) * 100.0

def clamp(v, lo, hi):
    return max(lo, min(hi, v))

def parse_br_date(s):
    return datetime.strptime(s, "%d/%m/%Y")

def fmt_pct(v):
    if v is None:
        return "—"
    sign = "+" if v >= 0 else ""
    return f"{sign}{v:.2f}%".replace(".", ",")

def fmt_num(v, n=2):
    return f"{v:.{n}f}".replace(".", ",")

def fred_csv(series_id):
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
    raw = http_get(url).decode("utf-8", errors="replace")
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
    raw = http_get(url)
    root = ET.fromstring(raw)
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
                dt = parsedate_to_datetime(pub)
                pub_br = dt.strftime("%d/%m/%Y")
            except Exception:
                pass
        items.append({
            "title": title,
            "url": link,
            "summary": desc[:500],
            "published": pub_br,
        })
    return items

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

def fed_item_score(item):
    txt = (item.get("title","") + " " + item.get("summary","")).lower()
    up = sum(1 for k in HAWKISH if k in txt)
    down = sum(1 for k in DOVISH if k in txt)
    if up > down:
        return 1
    if down > up:
        return -1
    return 0

def impact_label(score):
    if score > 0:
        return "alta"
    if score < 0:
        return "baixa"
    return "neutral"

def main():
    if not LATEST.exists():
        raise RuntimeError("data/latest.json não encontrado. Execute update_daily.py primeiro.")

    latest = load_json(LATEST)
    hist = latest.get("history") or []
    if len(hist) < 2:
        raise RuntimeError("Histórico PTAX insuficiente.")

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
    source_names = ["Banco Central do Brasil — PTAX"]
    news = []

    # PTAX
    ptax_score = 0
    if mom5 is not None:
        if mom5 >= 0.45:
            ptax_score += 2
        elif mom5 <= -0.45:
            ptax_score -= 2
    if mom10 is not None:
        if mom10 >= 0.35:
            ptax_score += 2
        elif mom10 <= -0.35:
            ptax_score -= 2
    if day_var is not None:
        if day_var >= 0.55:
            ptax_score += 1
        elif day_var <= -0.55:
            ptax_score -= 1
    score += ptax_score
    signals.append({
        "name": "Momento da PTAX",
        "score": ptax_score,
        "detail": f"5 pregões {fmt_pct(mom5)}; 10 pregões {fmt_pct(mom10)}; dia {fmt_pct(day_var)}."
    })

    # Focus
    focus = latest.get("focus")
    if focus and focus.get("value") is not None:
        source_names.append("Focus / Banco Central")
        fv = float(focus["value"])
        dif = pct(fv, ptax_now)
        fs = 0
        if dif is not None:
            if dif >= 1.5:
                fs = 2
            elif dif <= -1.5:
                fs = -2
            elif dif >= 0.5:
                fs = 1
            elif dif <= -0.5:
                fs = -1
        score += fs
        signals.append({
            "name": "Focus/BCB",
            "score": fs,
            "detail": f"Mediana {fmt_num(fv,2)}; diferença frente à PTAX {fmt_pct(dif)}."
        })

    # FRED: 2y, 10y, broad USD
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
                s = 2 if chg >= 0.40 else -2 if chg <= -0.40 else 1 if chg >= 0.15 else -1 if chg <= -0.15 else 0
                detail = f"{label}: {last[1]:.2f}; 5 observações {fmt_pct(chg)}."
            else:
                chg_bp = (last[1] - prev5[1]) * 100.0
                s = 2 if chg_bp >= 10 else -2 if chg_bp <= -10 else 1 if chg_bp >= 5 else -1 if chg_bp <= -5 else 0
                detail = f"{label}: {last[1]:.2f}%; variação em 5 observações {chg_bp:+.0f} pb."
            score += s
            signals.append({"name": label, "score": s, "detail": detail})
            news.append({
                "title": detail,
                "source": "Federal Reserve / FRED",
                "published": last[0].split("-")[2] + "/" + last[0].split("-")[1] + "/" + last[0].split("-")[0],
                "url": f"https://fred.stlouisfed.org/series/{sid}",
                "impact": impact_label(s),
            })
        except Exception as e:
            print(f"FRED {sid} indisponível: {e}")

    if fred_ok:
        source_names.append("Federal Reserve / FRED")

    # Federal Reserve RSS
    fed_items = []
    fed_score = 0
    fed_source_ok = False
    for feed_name, feed_url in FED_FEEDS:
        try:
            items = rss_items(feed_url, limit=10)
            if items:
                fed_source_ok = True
            for item in items:
                s = fed_item_score(item)
                if s != 0:
                    fed_score += s
                    fed_items.append((s, item, feed_name))
        except Exception as e:
            print(f"{feed_name} indisponível: {e}")

    fed_score = clamp(fed_score, -3, 3)
    score += fed_score
    if fed_source_ok and "Federal Reserve" not in source_names:
        source_names.append("Federal Reserve")

    signals.append({
        "name": "Sinalização do Federal Reserve",
        "score": fed_score,
        "detail": "Leitura por palavras-chave de política monetária em comunicados e discursos oficiais recentes."
    })

    for s, item, feed_name in fed_items[:4]:
        news.append({
            "title": item["title"],
            "source": "Federal Reserve",
            "published": item["published"],
            "url": item["url"],
            "impact": impact_label(s),
        })

    # Classificação 10 dias
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
    if active:
        same_dir = sum(1 for s in active if (s["score"] > 0) == (score > 0))
        agreement = same_dir / len(active) if score != 0 else 0.5
    else:
        agreement = 0.5

    # Confiança é estrutural, não probabilística.
    confidence = int(clamp(52 + 6 * len(active) + 18 * (agreement - 0.5), 50, 85))

    up_factors = [s["detail"] for s in signals if s["score"] > 0][:3]
    down_factors = [s["detail"] for s in signals if s["score"] < 0][:3]

    parts = [f"Score técnico consolidado: {score:+d}."]
    if up_factors:
        parts.append("Pressões de alta: " + " | ".join(up_factors) + ".")
    if down_factors:
        parts.append("Pressões de baixa: " + " | ".join(down_factors) + ".")
    if not up_factors and not down_factors:
        parts.append("Os sinais monitorados estão próximos do equilíbrio.")
    parts.append("A leitura é determinística e não utiliza IA paga.")

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "method": "Motor determinístico SEMIL — custo zero",
        "score10": score,
        "outlook10": {
            "bias": bias,
            "confidence": confidence,
            "summary": " ".join(parts),
            "sources": source_names,
        },
        "sources_consulted": [{"name": x} for x in source_names],
        "sources_monitored_not_ingested": [
            {
                "name": "Reuters",
                "reason": "Mantida como link de aprofundamento; ingestão automática depende de feed/API autorizado."
            },
            {
                "name": "Bloomberg",
                "reason": "Mantida como link de aprofundamento; ingestão automática depende de feed/API autorizado."
            },
            {
                "name": "BBC",
                "reason": "Mantida como link de aprofundamento; uso automatizado empresarial de RSS/metadados pode exigir permissão."
            }
        ],
        "signals": signals,
        "news": news[:6],
    }

    # Referência ~90 dias manual/validada, se existir.
    if MANUAL_REF90.exists():
        try:
            ref = load_json(MANUAL_REF90)
            if ref.get("value") and ref.get("date"):
                out["ref90"] = {
                    "value": float(ref["value"]),
                    "date": ref["date"],
                    "source": ref.get("source", "Referência validada")
                }
        except Exception as e:
            print("market_reference.json inválido:", e)

    DATA_DIR.mkdir(exist_ok=True)
    OUTPUT.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Inteligência gerada:", bias, f"confiança estrutural {confidence}%")
    print("Arquivo:", OUTPUT)

if __name__ == "__main__":
    main()
