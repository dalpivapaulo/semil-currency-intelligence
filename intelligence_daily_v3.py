#!/usr/bin/env python3
"""
SEMIL Currency Intelligence — Protocolo de Análise Cambial Consolidada v1.0

Gera:
  data/latest-v3.json
  data/market-intelligence-v3.json

Princípio de integridade:
- dado observado nunca é substituído por estimativa silenciosa;
- projeção numérica de ~90 dias exige referência futura validada e fresca;
- o ICD mede confiabilidade da BASE DE EVIDÊNCIAS, não probabilidade de acerto.
"""

import csv
import io
import json
import math
import re
import statistics
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

DATA = Path("data")
LATEST = DATA / "latest-v3.json"
INTEL = DATA / "market-intelligence-v3.json"
REF90 = DATA / "market_reference.json"

UA = "SEMIL-Currency-Intelligence/1.0"
CURRENT_RATE = 5.00

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
    "DFF": "Fed Funds efetiva",
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


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def pct(a, b):
    if b in (None, 0) or a is None:
        return None
    return (a / b - 1) * 100.0


def fmt_num(v, n=2):
    if v is None:
        return "—"
    return f"{v:.{n}f}".replace(".", ",")


def fmt_pct(v):
    if v is None:
        return "—"
    return f"{v:+.2f}%".replace(".", ",")


def parse_br_date(s):
    return datetime.strptime(s, "%d/%m/%Y")


def parse_any_date(s):
    if not s:
        return None
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(str(s)[:19], fmt)
        except Exception:
            pass
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None


def business_age(date_str, now=None):
    d = parse_any_date(date_str)
    if not d:
        return 999
    now = (now or datetime.now()).replace(tzinfo=None)
    d = d.replace(hour=0, minute=0, second=0, microsecond=0)
    now = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if d >= now:
        return 0
    count = 0
    while d < now:
        d += timedelta(days=1)
        if d.weekday() < 5:
            count += 1
    return count


def calendar_age(date_str, now=None):
    d = parse_any_date(date_str)
    if not d:
        return 999
    now = (now or datetime.now()).replace(tzinfo=None)
    return max(0, (now.date() - d.date()).days)


def freshness_score(age, full=1, acceptable=3, stale=7):
    if age <= full:
        return 100
    if age <= acceptable:
        return 80
    if age <= stale:
        return 45
    return 0


def score_to_bias(score):
    if score is None:
        return "NÃO CONCLUSIVO"
    if score <= -1.20:
        return "FORTEMENTE BAIXISTA"
    if score < -0.40:
        return "BAIXISTA"
    if score <= 0.39:
        return "NEUTRO"
    if score < 1.20:
        return "ALTISTA"
    return "FORTEMENTE ALTISTA"


def icd_class(score):
    if score >= 85:
        return "MUITO ALTO"
    if score >= 70:
        return "ALTO"
    if score >= 55:
        return "MODERADO"
    if score >= 40:
        return "BAIXO"
    return "INSUFICIENTE"


def signal_from_change(value, mild, strong):
    if value is None:
        return None
    if value >= strong:
        return 2.0
    if value >= mild:
        return 1.0
    if value <= -strong:
        return -2.0
    if value <= -mild:
        return -1.0
    return 0.0


def weighted_score(blocks, weights):
    available = [(k, blocks.get(k)) for k in weights if blocks.get(k) is not None]
    denom = sum(weights[k] for k, _ in available)
    if denom <= 0:
        return None
    return clamp(sum(v * weights[k] for k, v in available) / denom, -2.0, 2.0)


def recommendation(reference, current=CURRENT_RATE):
    if reference is None:
        return {"rate": current, "action": "REAVALIAR", "lo": None, "hi": None}
    lo = reference - 0.15
    hi = reference - 0.10
    rate = math.floor((hi + 1e-9) / 0.05) * 0.05
    if rate < lo:
        rate = math.ceil(lo / 0.05) * 0.05
    rate = round(rate + 1e-9, 2)
    # Histerese mínima de R$ 0,05.
    if abs(rate - current) < 0.05 - 1e-9:
        rate = current
    action = "ELEVAR" if rate > current else "REDUZIR" if rate < current else "MANTER"
    return {"rate": rate, "action": action, "lo": lo, "hi": hi}


# ----------------------------------------------------------------------
# 1) BCB — PTAX, Focus e Selic
# ----------------------------------------------------------------------
def fetch_focus_annual(indicator, year):
    base = (
        "https://olinda.bcb.gov.br/olinda/servico/Expectativas/versao/v1/"
        "odata/ExpectativasMercadoAnuais"
    )
    params = {
        "$filter": f"Indicador eq '{indicator}' and DataReferencia eq '{year}'",
        "$orderby": "Data desc",
        "$top": "1",
        "$format": "json",
    }
    payload = get_json(base + "?" + urllib.parse.urlencode(params, safe="'$"))
    if not payload.get("value"):
        return None
    row = payload["value"][0]
    return {"value": float(row["Mediana"]), "date": row.get("Data")}


def fetch_selic_target():
    url = "https://api.bcb.gov.br/dados/serie/bcdata.sgs.432/dados/ultimos/1?formato=json"
    payload = get_json(url)
    if not payload:
        return None
    row = payload[-1]
    raw = str(row.get("valor", "")).replace(".", "").replace(",", ".")
    # Alguns retornos já usam ponto decimal. Corrige apenas se houve vírgula no original.
    if "," not in str(row.get("valor", "")):
        raw = str(row.get("valor", ""))
    return {"value": float(raw), "date": row.get("data")}


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
    payload = get_json(base + "?" + urllib.parse.urlencode(params, safe="'$@"))
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
        closing = [x for x in rows if "fechamento" in str(x[1].get("tipoBoletim", "")).lower()]
        dt, row = (closing or rows)[-1]
        history.append({
            "date": date_br,
            "buy": float(row["cotacaoCompra"]),
            "sell": float(row["cotacaoVenda"]),
            "iso": dt.date().isoformat(),
        })

    history.sort(key=lambda x: x["iso"])
    history = history[-30:]
    if len(history) < 2:
        raise RuntimeError("Histórico PTAX insuficiente.")

    latest, prev = history[-1], history[-2]
    focus_fx = None
    focus_selic = None
    selic_target = None

    try:
        focus_fx = fetch_focus_annual("Câmbio", str(end.year))
    except Exception as e:
        print("Focus câmbio indisponível:", e)
    try:
        focus_selic = fetch_focus_annual("Selic", str(end.year))
    except Exception as e:
        print("Focus Selic indisponível:", e)
    try:
        selic_target = fetch_selic_target()
    except Exception as e:
        print("Meta Selic indisponível:", e)

    clean_history = [{k: v for k, v in h.items() if k != "iso"} for h in history]
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
            "variation_pct": pct(latest["sell"], prev["sell"]),
        },
        "focus": focus_fx,                 # compatibilidade com painel existente
        "focus_fx": focus_fx,
        "focus_selic": focus_selic,
        "selic_target": selic_target,
        "history": clean_history,
    }

    DATA.mkdir(exist_ok=True)
    LATEST.write_text(json.dumps(latest_json, ensure_ascii=False, indent=2), encoding="utf-8")
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
        pub_iso = None
        pub_br = pub
        if pub:
            try:
                dt = parsedate_to_datetime(pub)
                pub_iso = dt.date().isoformat()
                pub_br = dt.strftime("%d/%m/%Y")
            except Exception:
                pass
        items.append({
            "title": title,
            "url": link,
            "summary": desc[:500],
            "published": pub_br,
            "published_iso": pub_iso,
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
# 3) Volatilidade e evidências
# ----------------------------------------------------------------------
def hist_volatility_20(hist):
    vals = [float(x["sell"]) for x in hist[-21:] if x.get("sell")]
    if len(vals) < 6:
        return None
    rets = [math.log(vals[i] / vals[i - 1]) for i in range(1, len(vals)) if vals[i - 1] > 0]
    if len(rets) < 5:
        return None
    return statistics.stdev(rets)


def evidence(name, status, score, weight, freshness, quality, detail, updated=None):
    return {
        "name": name,
        "status": status,
        "signal": score,
        "coverage_weight": weight,
        "freshness": freshness,
        "quality": quality,
        "detail": detail,
        "updated": updated,
    }


def convergence_score(signal_map):
    vals = [v for v in signal_map.values() if v is not None]
    if not vals:
        return 0
    mean = sum(vals) / len(vals)
    mad = sum(abs(v - mean) for v in vals) / len(vals)
    return int(round(100 * (1 - min(1.0, mad / 2.0))))


def build_icd(evidences, signal_map):
    total_weight = sum(e["coverage_weight"] for e in evidences)
    valid_weight = sum(e["coverage_weight"] for e in evidences if e["status"] == "VALIDADA")
    coverage = 100 * valid_weight / total_weight if total_weight else 0

    observed = [e for e in evidences if e["status"] != "INDISPONÍVEL"]
    if observed:
        w = sum(e["coverage_weight"] for e in observed)
        actuality = sum(e["freshness"] * e["coverage_weight"] for e in observed) / w
        quality = sum(e["quality"] * e["coverage_weight"] for e in observed) / w
    else:
        actuality = 0
        quality = 0

    convergence = convergence_score(signal_map)
    score = round(0.35 * coverage + 0.25 * actuality + 0.25 * convergence + 0.15 * quality)
    score = int(clamp(score, 0, 100))
    return {
        "score": score,
        "class": icd_class(score),
        "components": {
            "coverage": round(coverage),
            "actuality": round(actuality),
            "convergence": round(convergence),
            "quality": round(quality),
        }
    }


# ----------------------------------------------------------------------
# 4) Motor consolidado SEMIL
# ----------------------------------------------------------------------
def build_intelligence(latest):
    now = datetime.now(timezone.utc)
    hist = sorted(latest.get("history") or [], key=lambda x: parse_br_date(x["date"]))
    if len(hist) < 2:
        raise RuntimeError("Histórico insuficiente para inteligência cambial.")

    ptax_now = float(hist[-1]["sell"])
    ptax_prev = float(hist[-2]["sell"])

    def momentum(rows_count):
        rows = hist[-rows_count:]
        if len(rows) < 2:
            return None
        return pct(float(rows[-1]["sell"]), float(rows[0]["sell"]))

    day_var = pct(ptax_now, ptax_prev)
    mom5 = momentum(6)
    mom10 = momentum(11)

    # --- Bloco SPOT / MOMENTUM
    s_day = signal_from_change(day_var, 0.30, 0.70)
    s_5 = signal_from_change(mom5, 0.50, 1.50)
    s_10 = signal_from_change(mom10, 0.75, 2.00)
    spot_signal = clamp(0.20 * (s_day or 0) + 0.40 * (s_5 or 0) + 0.40 * (s_10 or 0), -2, 2)

    ptax_age = business_age(hist[-1]["date"])
    evidences = [
        evidence(
            "Mercado doméstico / PTAX",
            "VALIDADA" if ptax_age <= 3 else "DESATUALIZADA",
            spot_signal,
            15,
            freshness_score(ptax_age, 1, 3, 5),
            100,
            f"Dia {fmt_pct(day_var)}; 5 pregões {fmt_pct(mom5)}; 10 pregões {fmt_pct(mom10)}.",
            hist[-1]["date"],
        )
    ]

    # --- Focus câmbio
    focus_fx = latest.get("focus_fx") or latest.get("focus")
    focus_signal = None
    if focus_fx and focus_fx.get("value") is not None:
        fv = float(focus_fx["value"])
        fdiff = pct(fv, ptax_now)
        focus_signal = signal_from_change(fdiff, 1.0, 3.0)
        fage = calendar_age(focus_fx.get("date"))
        evidences.append(evidence(
            "Expectativas de mercado",
            "VALIDADA" if fage <= 10 else "DESATUALIZADA",
            focus_signal,
            10,
            freshness_score(fage, 7, 10, 20),
            100,
            f"Câmbio Focus {fmt_num(fv,2)}; distância da PTAX {fmt_pct(fdiff)}.",
            focus_fx.get("date"),
        ))
    else:
        evidences.append(evidence("Expectativas de mercado", "INDISPONÍVEL", None, 10, 0, 100, "Focus câmbio indisponível."))

    # --- FRED: dólar, Treasuries e Fed Funds
    fred = {}
    fred_status = {}
    for sid, label in FRED_SERIES.items():
        try:
            rows = fred_csv(sid)
            if len(rows) >= 6:
                fred[sid] = rows
                fred_status[sid] = "VALIDADA"
            else:
                fred_status[sid] = "INDISPONÍVEL"
        except Exception as e:
            print(f"FRED {sid} indisponível:", e)
            fred_status[sid] = "INDISPONÍVEL"

    dxy_signal = None
    if "DTWEXBGS" in fred:
        rows = fred["DTWEXBGS"]
        chg5 = pct(rows[-1][1], rows[-6][1])
        dxy_signal = signal_from_change(chg5, 0.30, 1.00)
        age = calendar_age(rows[-1][0])
        evidences.append(evidence(
            "Dólar global",
            "VALIDADA" if age <= 5 else "DESATUALIZADA",
            dxy_signal,
            10,
            freshness_score(age, 2, 5, 8),
            100,
            f"Índice amplo do dólar: 5 observações {fmt_pct(chg5)}.",
            rows[-1][0],
        ))
    else:
        evidences.append(evidence("Dólar global", "INDISPONÍVEL", None, 10, 0, 100, "Índice amplo do dólar indisponível."))

    treasury_parts = []
    treasury_details = []
    treasury_dates = []
    for sid, w in (("DGS2", 0.65), ("DGS10", 0.35)):
        if sid in fred:
            rows = fred[sid]
            bp = (rows[-1][1] - rows[-6][1]) * 100
            sig = signal_from_change(bp, 5, 15)
            treasury_parts.append((sig or 0, w))
            treasury_details.append(f"{FRED_SERIES[sid]} {rows[-1][1]:.2f}% ({bp:+.0f} pb/5 obs.)")
            treasury_dates.append(rows[-1][0])
    treasury_signal = None
    if treasury_parts:
        treasury_signal = clamp(sum(s * w for s, w in treasury_parts) / sum(w for _, w in treasury_parts), -2, 2)
        age = min(calendar_age(x) for x in treasury_dates)
        evidences.append(evidence(
            "Juros de mercado nos EUA",
            "VALIDADA" if age <= 5 else "DESATUALIZADA",
            treasury_signal,
            10,
            freshness_score(age, 2, 5, 8),
            100,
            "; ".join(treasury_details) + ".",
            max(treasury_dates),
        ))
    else:
        evidences.append(evidence("Juros de mercado nos EUA", "INDISPONÍVEL", None, 10, 0, 100, "Treasuries indisponíveis."))

    # --- Fed RSS / eventos
    fed_feed_ok = False
    fed_score_raw = 0
    event_items = []
    for feed_name, feed_url in FED_FEEDS:
        try:
            items = rss_items(feed_url, limit=12)
            fed_feed_ok = True
            for item in items:
                age = calendar_age(item.get("published_iso"))
                if age > 14:
                    continue
                s = fed_item_score(item)
                if s:
                    fed_score_raw += s
                    event_items.append((s, item))
        except Exception as e:
            print(feed_name, "indisponível:", e)

    event_signal = clamp(fed_score_raw / 2.0, -2, 2) if fed_feed_ok else None
    evidences.append(evidence(
        "Eventos monetários relevantes",
        "VALIDADA" if fed_feed_ok else "INDISPONÍVEL",
        event_signal,
        10,
        100 if fed_feed_ok else 0,
        100,
        "Comunicados e discursos oficiais recentes do Federal Reserve classificados por direção cambial.",
        now.date().isoformat() if fed_feed_ok else None,
    ))

    # --- Bloco de juros Brasil x EUA
    selic_target = latest.get("selic_target")
    focus_selic = latest.get("focus_selic")
    fed_funds = fred.get("DFF")
    rates_signal = None
    rates_details = []
    rate_parts = []
    if selic_target and selic_target.get("value") is not None:
        selic = float(selic_target["value"])
        rates_details.append(f"Selic alvo {selic:.2f}%")
        if focus_selic and focus_selic.get("value") is not None:
            exp_selic = float(focus_selic["value"])
            delta_bp = (exp_selic - selic) * 100
            # Queda esperada de Selic reduz carry e tende a pressionar USD/BRL para cima.
            sig = -signal_from_change(delta_bp, 50, 150)
            rate_parts.append((sig or 0, 0.55))
            rates_details.append(f"Focus Selic {exp_selic:.2f}% ({delta_bp:+.0f} pb vs atual)")
    if fed_funds:
        dff_now = fed_funds[-1][1]
        dff_bp = (fed_funds[-1][1] - fed_funds[-6][1]) * 100
        sig = signal_from_change(dff_bp, 5, 15)
        rate_parts.append((sig or 0, 0.25))
        rates_details.append(f"Fed Funds efetiva {dff_now:.2f}% ({dff_bp:+.0f} pb/5 obs.)")
        if selic_target and selic_target.get("value") is not None:
            differential = float(selic_target["value"]) - dff_now
            rates_details.append(f"Diferencial nominal Brasil-EUA {differential:.2f} p.p.")
    if fed_feed_ok:
        rate_parts.append((event_signal or 0, 0.20))
    if rate_parts:
        rates_signal = clamp(sum(s * w for s, w in rate_parts) / sum(w for _, w in rate_parts), -2, 2)
        ages = []
        if selic_target:
            ages.append(calendar_age(selic_target.get("date")))
        if fed_funds:
            ages.append(calendar_age(fed_funds[-1][0]))
        age = max(ages) if ages else 0
        evidences.append(evidence(
            "Diferencial de juros Brasil-EUA",
            "VALIDADA" if age <= 10 else "DESATUALIZADA",
            rates_signal,
            15,
            freshness_score(age, 3, 10, 20),
            100,
            "; ".join(rates_details) + ".",
            now.date().isoformat(),
        ))
    else:
        evidences.append(evidence("Diferencial de juros Brasil-EUA", "INDISPONÍVEL", None, 15, 0, 100, "Dados de juros insuficientes."))

    # --- Referência futura ~90d validada (B3/CME ou outra fonte de mercado)
    future_signal = None
    ref90 = None
    if REF90.exists():
        try:
            ref = json.loads(REF90.read_text(encoding="utf-8"))
            if ref.get("value") and ref.get("date"):
                ref90 = {
                    "value": float(ref["value"]),
                    "date": ref["date"],
                    "source": ref.get("source", "Referência futura validada"),
                }
        except Exception as e:
            print("Referência 90 dias inválida:", e)

    if ref90:
        age = business_age(ref90["date"])
        diff_future = pct(ref90["value"], ptax_now)
        future_signal = signal_from_change(diff_future, 1.0, 3.0)
        status = "VALIDADA" if age <= 3 else "DESATUALIZADA"
        evidences.append(evidence(
            "Curva / referência futura",
            status,
            future_signal if status == "VALIDADA" else None,
            20,
            freshness_score(age, 1, 3, 5),
            90,
            f"Referência ~90d {fmt_num(ref90['value'],4)}; distância da PTAX {fmt_pct(diff_future)}.",
            ref90["date"],
        ))
        if status != "VALIDADA":
            future_signal = None
    else:
        evidences.append(evidence("Curva / referência futura", "INDISPONÍVEL", None, 20, 0, 90, "Referência futura validada indisponível."))

    # --- Risco Brasil: não inventar proxy se a fonte não existe.
    brazil_signal = None
    evidences.append(evidence(
        "Risco Brasil / fluxo / fiscal",
        "INDISPONÍVEL",
        None,
        10,
        0,
        0,
        "Camada estruturada de risco Brasil ainda não integrada; não entra no cálculo.",
    ))

    # Externo combina dólar global e Treasuries.
    external_parts = []
    if dxy_signal is not None:
        external_parts.append((dxy_signal, 0.60))
    if treasury_signal is not None:
        external_parts.append((treasury_signal, 0.40))
    external_signal = None
    if external_parts:
        external_signal = clamp(sum(s * w for s, w in external_parts) / sum(w for _, w in external_parts), -2, 2)

    blocks10 = {
        "future": future_signal,
        "spot": spot_signal,
        "external": external_signal,
        "rates": rates_signal,
        "events": event_signal,
        "focus": focus_signal,
    }
    weights10 = {"future": 30, "spot": 20, "external": 20, "rates": 15, "events": 10, "focus": 5}
    score10 = weighted_score(blocks10, weights10)

    blocks90 = {
        "future": future_signal,
        "rates": rates_signal,
        "brazil": brazil_signal,
        "external": external_signal,
        "focus": focus_signal,
        "events": event_signal,
    }
    weights90 = {"future": 25, "rates": 20, "brazil": 20, "external": 15, "focus": 15, "events": 5}
    score90 = weighted_score(blocks90, weights90)

    signal_map = {
        "mercado_domestico": spot_signal,
        "curva_futura": future_signal,
        "dolar_global": dxy_signal,
        "treasuries": treasury_signal,
        "juros": rates_signal,
        "expectativas": focus_signal,
        "eventos": event_signal,
        "risco_brasil": brazil_signal,
    }
    icd = build_icd(evidences, signal_map)

    # --- Projeção técnica ~90d: só existe com referência futura fresca.
    projection90 = {
        "available": False,
        "center": None,
        "min": None,
        "max": None,
        "base_center": None,
        "adjustment_pct": None,
        "reason": "Referência futura validada e fresca indisponível.",
    }
    if future_signal is not None and ref90:
        focus_value = float(focus_fx["value"]) if focus_fx and focus_fx.get("value") is not None else None
        if focus_value is not None:
            base_center = 0.60 * ref90["value"] + 0.25 * focus_value + 0.15 * ptax_now
        else:
            base_center = 0.85 * ref90["value"] + 0.15 * ptax_now
        adj_pct = clamp((score90 or 0) / 2.0 * 1.5, -1.5, 1.5)
        center = base_center * (1 + adj_pct / 100.0)
        sigma_daily = hist_volatility_20(hist)
        if sigma_daily is not None:
            margin_pct = clamp(sigma_daily * math.sqrt(63) * 0.75, 0.01, 0.05)
        else:
            margin_pct = 0.02
        margin = center * margin_pct
        projection90 = {
            "available": True,
            "center": round(center, 4),
            "min": round(center - margin, 4),
            "max": round(center + margin, 4),
            "base_center": round(base_center, 4),
            "adjustment_pct": round(adj_pct, 2),
            "volatility_margin_pct": round(margin_pct * 100, 2),
            "reason": "Faixa técnica construída a partir da referência futura validada, Focus/PTAX e volatilidade histórica.",
        }

    # --- Gatekeepers
    gates = []
    if future_signal is None:
        gates.append("Curva/referência futura indisponível ou desatualizada: projeção quantitativa de 90 dias bloqueada.")
    if icd["score"] < 40:
        gates.append("ICD abaixo de 40: análise não conclusiva e recomendação comercial bloqueada.")

    conclusive = icd["score"] >= 40
    outlook10_status = "REDUZIDA" if future_signal is None else "NORMAL"

    # --- Recomendação SEMIL
    semil = {
        "current_rate": CURRENT_RATE,
        "recommended_rate": CURRENT_RATE,
        "action": "MANTER",
        "classification": "REAVALIAR",
        "policy_band": {"min": None, "max": None},
        "reason": "Aguardando base suficiente para alteração da taxa interna.",
    }
    if conclusive and projection90["available"]:
        rec = recommendation(projection90["center"], CURRENT_RATE)
        # Alteração só com ICD >= 70; abaixo disso mantém e reavalia.
        if icd["score"] >= 70:
            semil.update({
                "recommended_rate": rec["rate"],
                "action": rec["action"],
                "classification": "COMPETITIVA" if rec["rate"] <= projection90["center"] - 0.10 else "NEUTRA",
                "policy_band": {"min": round(rec["lo"], 4), "max": round(rec["hi"], 4)},
                "reason": "Recomendação calculada dentro da política SEMIL de R$ 0,10 a R$ 0,15 abaixo do centro de ~90 dias, com ICD mínimo atendido.",
            })
        else:
            semil.update({
                "classification": "REAVALIAR",
                "policy_band": {"min": round(rec["lo"], 4), "max": round(rec["hi"], 4)},
                "reason": "Existe referência técnica, porém o ICD está abaixo de 70; por histerese de segurança, a taxa atual é mantida.",
            })

    # --- Síntese única
    valid_count = sum(1 for e in evidences if e["status"] == "VALIDADA")
    total_count = len(evidences)
    drivers_up = sorted(
        [e for e in evidences if e.get("signal") is not None and e["signal"] > 0],
        key=lambda e: abs(e["signal"]), reverse=True
    )[:3]
    drivers_down = sorted(
        [e for e in evidences if e.get("signal") is not None and e["signal"] < 0],
        key=lambda e: abs(e["signal"]), reverse=True
    )[:3]

    if not conclusive:
        executive_summary = "A base de evidências está insuficiente para uma conclusão cambial confiável. A taxa interna SEMIL permanece inalterada por trava de segurança."
    else:
        executive_summary = (
            f"A leitura consolidada do USD/BRL indica viés de 10 dias {score_to_bias(score10).lower()} "
            f"e horizonte de ~90 dias {score_to_bias(score90).lower()}. "
            f"O ICD é {icd['score']}/100 ({icd['class'].lower()}), com {valid_count} de {total_count} blocos de evidência validados."
        )
        if projection90["available"]:
            executive_summary += (
                f" A faixa técnica de ~90 dias está entre R$ {fmt_num(projection90['min'],4)} e "
                f"R$ {fmt_num(projection90['max'],4)}, com centro em R$ {fmt_num(projection90['center'],4)}."
            )
        else:
            executive_summary += " A faixa numérica de ~90 dias permanece bloqueada até nova referência futura validada."

    result = {
        "generated_at": now.isoformat(),
        "protocol": "SEMIL — Análise Cambial Consolidada v1.0",
        "method": "Motor determinístico, auditável e sem preenchimento fictício",
        "score10": round(score10, 3) if score10 is not None else None,
        "score90": round(score90, 3) if score90 is not None else None,
        "outlook10": {
            "score": round(score10, 3) if score10 is not None else None,
            "bias": score_to_bias(score10),
            "status": outlook10_status,
        },
        "outlook90": {
            "score": round(score90, 3) if score90 is not None else None,
            "bias": score_to_bias(score90),
            "projection": projection90,
        },
        "icd": icd,
        "semil": semil,
        "conclusive": conclusive,
        "executive_summary": executive_summary,
        "evidence_summary": {
            "valid": valid_count,
            "total": total_count,
            "drivers_up": [{"name": e["name"], "signal": e["signal"], "detail": e["detail"]} for e in drivers_up],
            "drivers_down": [{"name": e["name"], "signal": e["signal"], "detail": e["detail"]} for e in drivers_down],
        },
        "gates": gates,
        "evidences": evidences,
        "ref90": ref90,
        # Mantidos por compatibilidade, mas o painel não deve renderizar fonte por fonte.
        "sources_consulted": [
            {"name": e["name"], "status": e["status"]}
            for e in evidences if e["status"] != "INDISPONÍVEL"
        ],
        "news": [
            {
                "title": item["title"],
                "source": "Federal Reserve",
                "published": item["published"],
                "url": item["url"],
                "impact": "alta" if s > 0 else "baixa" if s < 0 else "neutro",
            }
            for s, item in event_items[:6]
        ],
    }

    DATA.mkdir(exist_ok=True)
    INTEL.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main():
    latest = fetch_bcb()
    intel = build_intelligence(latest)
    print("PTAX:", latest["ptax"]["date"], latest["ptax"]["sell"])
    print("10 dias:", intel["outlook10"]["bias"], f"ICD {intel['icd']['score']}/100")
    print("90 dias:", intel["outlook90"]["bias"])
    print("SEMIL:", intel["semil"]["action"], intel["semil"]["recommended_rate"])
    print("Arquivos gerados:")
    print(" -", LATEST)
    print(" -", INTEL)


if __name__ == "__main__":
    main()
