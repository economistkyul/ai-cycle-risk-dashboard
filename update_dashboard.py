#!/usr/bin/env python3
"""
AI Cycle Risk Dashboard - v1
FRED-only collector + rule-based risk engine.

Reads:
    FRED_API_KEY from environment

Writes:
    data/latest.json
    data/history.csv
    docs/index.html

No third-party Python packages are required.
"""

import csv
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

FRED_API = "https://api.stlouisfed.org/fred/series/observations"

SERIES = {
    "3m": "DGS3MO",
    "2y": "DGS2",
    "10y": "DGS10",
    "30y": "DGS30",
    "real10y": "DFII10",
    "breakeven10y": "T10YIE",
    "fed_upper": "DFEDTARU",
    "fed_lower": "DFEDTARL",
    "ig_oas": "BAMLC0A0CM",
    "hy_oas": "BAMLH0A0HYM2",
}

LABELS = {
    "3m": "3M Treasury",
    "2y": "2Y Treasury",
    "10y": "10Y Treasury",
    "30y": "30Y Treasury",
    "real10y": "10Y TIPS real yield",
    "breakeven10y": "10Y breakeven inflation",
    "fed_upper": "Fed target upper",
    "fed_lower": "Fed target lower",
    "ig_oas": "US IG OAS",
    "hy_oas": "US HY OAS",
}

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
DOCS_DIR = ROOT / "docs"
LATEST_JSON = DATA_DIR / "latest.json"
HISTORY_CSV = DATA_DIR / "history.csv"


def fetch_series(series_id, api_key, limit=160):
    params = {
        "series_id": series_id,
        "api_key": api_key,
        "file_type": "json",
        "sort_order": "desc",
        "limit": limit,
    }
    url = FRED_API + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "ai-cycle-risk-dashboard/1.0"},
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))

    rows = []
    for obs in payload.get("observations", []):
        value = obs.get("value")
        if value in (None, ".", ""):
            continue
        try:
            rows.append({"date": obs["date"], "value": float(value)})
        except (ValueError, KeyError):
            continue

    rows.sort(key=lambda x: x["date"])
    if not rows:
        raise RuntimeError(f"No usable observations returned for {series_id}")
    return rows


def latest(rows):
    return rows[-1]


def delta(rows, periods):
    if len(rows) <= periods:
        return None
    return rows[-1]["value"] - rows[-1 - periods]["value"]


def aligned_spread(long_rows, short_rows):
    short_map = {x["date"]: x["value"] for x in short_rows}
    out = []
    for x in long_rows:
        if x["date"] in short_map:
            out.append(
                {
                    "date": x["date"],
                    "value": (x["value"] - short_map[x["date"]]) * 100.0,
                }
            )
    out.sort(key=lambda x: x["date"])
    return out


def bp(x):
    return None if x is None else x * 100.0


def round_or_none(x, digits=2):
    return None if x is None else round(x, digits)


def score_1999(d):
    score = 0
    reasons = []

    two_y_5 = d["changes"]["2y"]["5d_bp"]
    two_y_20 = d["changes"]["2y"]["20d_bp"]
    curve_5 = d["curves"]["2s10s"]["5d_change_bp"]
    curve_now = d["curves"]["2s10s"]["value_bp"]
    ig = d["latest"]["ig_oas"]["value"]
    hy = d["latest"]["hy_oas"]["value"]

    if two_y_5 is not None and two_y_5 >= 25:
        score += 25
        reasons.append("2Y 5일 +25bp 이상")
    if two_y_20 is not None and two_y_20 >= 40:
        score += 20
        reasons.append("2Y 20일 +40bp 이상")
    if curve_5 is not None and curve_5 <= -15:
        score += 25
        reasons.append("2s10s 5일 -15bp 이상 평탄화")
    if curve_now is not None and curve_now <= 0:
        score += 25
        reasons.append("2s10s 역전")
    elif curve_now is not None and curve_now <= 25:
        score += 15
        reasons.append("2s10s +25bp 이내 접근")
    if ig >= 100:
        score += 10
        reasons.append("IG OAS 100bp 이상")
    if hy >= 350:
        score += 10
        reasons.append("HY OAS 350bp 이상")

    return min(score, 100), reasons


def score_1987(d):
    score = 0
    reasons = []

    y10 = d["latest"]["10y"]["value"]
    y30 = d["latest"]["30y"]["value"]
    y10_20 = d["changes"]["10y"]["20d_bp"]
    y30_20 = d["changes"]["30y"]["20d_bp"]
    real_20 = d["changes"]["real10y"]["20d_bp"]
    ig_20 = d["changes"]["ig_oas"]["20d_bp"]
    hy_20 = d["changes"]["hy_oas"]["20d_bp"]

    if y30 >= 5.75:
        score += 35
        reasons.append("30Y 5.75% 이상")
    elif y30 >= 5.50:
        score += 20
        reasons.append("30Y 5.50% 이상")

    if y10 >= 5.30:
        score += 30
        reasons.append("10Y 5.30% 이상")
    elif y10 >= 5.10:
        score += 15
        reasons.append("10Y 5.10% 이상")

    speed = False
    if y30_20 is not None and y30_20 >= 40:
        score += 20
        speed = True
        reasons.append("30Y 20일 +40bp 이상")
    if y10_20 is not None and y10_20 >= 35:
        score += 20
        speed = True
        reasons.append("10Y 20일 +35bp 이상")

    if speed and real_20 is not None and real_20 >= 20:
        score += 15
        reasons.append("롱엔드 급등 + 실질금리 상승")

    if ig_20 is not None and hy_20 is not None and ig_20 > 5 and hy_20 > 20:
        score += 15
        reasons.append("IG/HY OAS 동시 확대")

    return min(score, 100), reasons


def classify_regime(d, r1987, r1999):
    ig = d["latest"]["ig_oas"]["value"]
    hy = d["latest"]["hy_oas"]["value"]
    curve_now = d["curves"]["2s10s"]["value_bp"]
    two_y_5 = d["changes"]["2y"]["5d_bp"]

    credit_warning = ig >= 100 or hy >= 350
    credit_severe = ig >= 130 or hy >= 450
    front_strong = (
        (two_y_5 is not None and two_y_5 >= 50)
        or (curve_now is not None and curve_now <= 0)
        or r1999 >= 70
    )
    long_strong = r1987 >= 70

    if credit_severe or (credit_warning and (front_strong or long_strong)):
        return "방어", "방어 전환"

    if r1987 >= 40 or r1999 >= 40 or credit_warning:
        return "경계", "리스크 축소 준비"

    return "공격", "상승 추세 추종"


def build_payload(raw):
    latest_values = {
        k: {
            "series_id": SERIES[k],
            "label": LABELS[k],
            "date": latest(rows)["date"],
            "value": latest(rows)["value"],
        }
        for k, rows in raw.items()
    }

    changes = {}
    for k, rows in raw.items():
        changes[k] = {
            "5d_bp": round_or_none(bp(delta(rows, 5)), 1),
            "10d_bp": round_or_none(bp(delta(rows, 10)), 1),
            "20d_bp": round_or_none(bp(delta(rows, 20)), 1),
        }

    curve_rows = {
        "2s10s": aligned_spread(raw["10y"], raw["2y"]),
        "3m10y": aligned_spread(raw["10y"], raw["3m"]),
        "2s30s": aligned_spread(raw["30y"], raw["2y"]),
    }

    curves = {}
    for name, rows in curve_rows.items():
        curves[name] = {
            "date": latest(rows)["date"],
            "value_bp": round_or_none(latest(rows)["value"], 1),
            "5d_change_bp": round_or_none(delta(rows, 5), 1),
            "10d_change_bp": round_or_none(delta(rows, 10), 1),
            "20d_change_bp": round_or_none(delta(rows, 20), 1),
        }

    payload = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "latest": latest_values,
        "changes": changes,
        "curves": curves,
        "data_notes": {
            "term_premium": "N/A - v2에서 NY Fed ACM 연동 예정",
            "move": "N/A - v2에서 신뢰 가능한 소스 연동 예정",
            "fedwatch": "N/A - v2에서 회의별 확률 연동 예정",
            "oracle_cds": "N/A - 신뢰 가능한 무료 소스 확보 전까지 미추정",
            "market_stage": "N/A - FedWatch 연동 후 단계 판정 추가 예정",
        },
    }

    r1999, reasons1999 = score_1999(payload)
    r1987, reasons1987 = score_1987(payload)
    regime, action = classify_regime(payload, r1987, r1999)

    payload["risk"] = {
        "risk_1999": r1999,
        "risk_1987": r1987,
        "reasons_1999": reasons1999,
        "reasons_1987": reasons1987,
        "regime": regime,
        "action": action,
        "closer_to": (
            "1987형" if r1987 > r1999
            else "1999형" if r1999 > r1987
            else "혼합/중립"
        ),
    }
    return payload


def save_json(payload):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LATEST_JSON.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def save_history(payload):
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    row = {
        "date": payload["latest"]["10y"]["date"],
        "regime": payload["risk"]["regime"],
        "action": payload["risk"]["action"],
        "risk_1987": payload["risk"]["risk_1987"],
        "risk_1999": payload["risk"]["risk_1999"],
        "2y": payload["latest"]["2y"]["value"],
        "10y": payload["latest"]["10y"]["value"],
        "30y": payload["latest"]["30y"]["value"],
        "real10y": payload["latest"]["real10y"]["value"],
        "2s10s_bp": payload["curves"]["2s10s"]["value_bp"],
        "ig_oas": payload["latest"]["ig_oas"]["value"],
        "hy_oas": payload["latest"]["hy_oas"]["value"],
    }

    fieldnames = list(row.keys())
    existing = []
    if HISTORY_CSV.exists():
        with HISTORY_CSV.open("r", encoding="utf-8", newline="") as f:
            existing = list(csv.DictReader(f))

    replaced = False
    for i, old in enumerate(existing):
        if old.get("date") == row["date"]:
            existing[i] = {k: str(v) for k, v in row.items()}
            replaced = True
            break
    if not replaced:
        existing.append({k: str(v) for k, v in row.items()})

    with HISTORY_CSV.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(existing)


def fmt_pct(x):
    return f"{x:.2f}%"


def fmt_bp(x):
    return f"{x:+.0f}bp" if x is not None else "N/A"


def html_escape(s):
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def build_html(payload):
    r = payload["risk"]
    color = {"공격": "#16a34a", "경계": "#d97706", "방어": "#dc2626"}[r["regime"]]

    def metric(title, value, sub=""):
        return f"""
        <div class="card">
          <div class="muted">{html_escape(title)}</div>
          <div class="value">{html_escape(value)}</div>
          <div class="sub">{html_escape(sub)}</div>
        </div>"""

    cards = "".join([
        metric(
            "2Y Treasury",
            fmt_pct(payload["latest"]["2y"]["value"]),
            f"5D {fmt_bp(payload['changes']['2y']['5d_bp'])} · 20D {fmt_bp(payload['changes']['2y']['20d_bp'])}",
        ),
        metric(
            "2s10s",
            fmt_bp(payload["curves"]["2s10s"]["value_bp"]),
            f"5D {fmt_bp(payload['curves']['2s10s']['5d_change_bp'])} · 20D {fmt_bp(payload['curves']['2s10s']['20d_change_bp'])}",
        ),
        metric(
            "30Y Treasury",
            fmt_pct(payload["latest"]["30y"]["value"]),
            f"20D {fmt_bp(payload['changes']['30y']['20d_bp'])}",
        ),
        metric(
            "10Y Real Yield",
            fmt_pct(payload["latest"]["real10y"]["value"]),
            f"20D {fmt_bp(payload['changes']['real10y']['20d_bp'])}",
        ),
        metric(
            "IG OAS",
            f"{payload['latest']['ig_oas']['value']:.0f}bp",
            f"20D {fmt_bp(payload['changes']['ig_oas']['20d_bp'])}",
        ),
        metric(
            "HY OAS",
            f"{payload['latest']['hy_oas']['value']:.0f}bp",
            f"20D {fmt_bp(payload['changes']['hy_oas']['20d_bp'])}",
        ),
    ])

    reasons87 = "".join(f"<li>{html_escape(x)}</li>" for x in r["reasons_1987"]) or "<li>핵심 트리거 없음</li>"
    reasons99 = "".join(f"<li>{html_escape(x)}</li>" for x in r["reasons_1999"]) or "<li>핵심 트리거 없음</li>"

    updated = payload["latest"]["10y"]["date"]

    return f"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI Cycle Risk Dashboard</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    background: #f6f7f9; color: #111827;
  }}
  .wrap {{ max-width: 1050px; margin: 0 auto; padding: 18px; }}
  .hero {{
    background: #111827; color: white; border-radius: 18px; padding: 22px;
    margin-bottom: 16px;
  }}
  .badge {{
    display:inline-block; padding:6px 12px; border-radius:999px;
    background:{color}; color:white; font-weight:700;
  }}
  .muted {{ color:#6b7280; font-size:13px; }}
  .hero .muted {{ color:#cbd5e1; }}
  h1 {{ font-size:24px; margin:8px 0 6px; }}
  h2 {{ font-size:18px; margin:22px 0 10px; }}
  .grid {{
    display:grid; grid-template-columns:repeat(3,1fr); gap:10px;
  }}
  .card {{
    background:white; border:1px solid #e5e7eb; border-radius:14px; padding:15px;
  }}
  .value {{ font-size:25px; font-weight:750; margin:7px 0 4px; }}
  .sub {{ font-size:13px; color:#4b5563; }}
  .riskgrid {{ display:grid; grid-template-columns:1fr 1fr; gap:10px; }}
  .score {{ font-size:34px; font-weight:800; }}
  ul {{ margin:8px 0 0 18px; padding:0; }}
  li {{ margin:5px 0; }}
  .note {{
    background:#fff; border-left:4px solid #9ca3af; padding:12px 14px;
    margin-top:10px; font-size:13px; color:#4b5563;
  }}
  table {{
    width:100%; border-collapse:collapse; background:white; border-radius:14px; overflow:hidden;
  }}
  th,td {{ text-align:left; border-bottom:1px solid #e5e7eb; padding:10px; font-size:13px; }}
  th {{ background:#f9fafb; }}
  a {{ color:#2563eb; }}
  @media (max-width:720px) {{
    .grid {{ grid-template-columns:1fr 1fr; }}
    .riskgrid {{ grid-template-columns:1fr; }}
  }}
  @media (max-width:430px) {{
    .grid {{ grid-template-columns:1fr; }}
  }}
</style>
</head>
<body>
<div class="wrap">
  <section class="hero">
    <span class="badge">{html_escape(r["regime"])}</span>
    <h1>AI Cycle Risk Dashboard</h1>
    <div>{html_escape(r["action"])} · 현재는 <b>{html_escape(r["closer_to"])}</b> 위험축이 상대적으로 더 강함</div>
    <div class="muted" style="margin-top:8px">FRED 최신 관측 기준일: {updated}</div>
  </section>

  <div class="grid">{cards}</div>

  <h2>역사적 위험 패턴</h2>
  <div class="riskgrid">
    <div class="card">
      <div class="muted">1987형 · Long-end / credibility stress</div>
      <div class="score">{r["risk_1987"]}/100</div>
      <ul>{reasons87}</ul>
    </div>
    <div class="card">
      <div class="muted">1999형 · Front-end / tightening stress</div>
      <div class="score">{r["risk_1999"]}/100</div>
      <ul>{reasons99}</ul>
    </div>
  </div>

  <h2>Yield Curve</h2>
  <table>
    <tr><th>Curve</th><th>현재</th><th>5D 변화</th><th>20D 변화</th></tr>
    <tr><td>2s10s</td><td>{fmt_bp(payload["curves"]["2s10s"]["value_bp"])}</td><td>{fmt_bp(payload["curves"]["2s10s"]["5d_change_bp"])}</td><td>{fmt_bp(payload["curves"]["2s10s"]["20d_change_bp"])}</td></tr>
    <tr><td>3m10y</td><td>{fmt_bp(payload["curves"]["3m10y"]["value_bp"])}</td><td>{fmt_bp(payload["curves"]["3m10y"]["5d_change_bp"])}</td><td>{fmt_bp(payload["curves"]["3m10y"]["20d_change_bp"])}</td></tr>
    <tr><td>2s30s</td><td>{fmt_bp(payload["curves"]["2s30s"]["value_bp"])}</td><td>{fmt_bp(payload["curves"]["2s30s"]["5d_change_bp"])}</td><td>{fmt_bp(payload["curves"]["2s30s"]["20d_change_bp"])}</td></tr>
  </table>

  <h2>v2에서 추가할 데이터</h2>
  <div class="note">
    NY Fed ACM term premium · MOVE · CME FedWatch · Treasury auction/foreign demand ·
    AI/데이터센터 신용 · hyperscaler Capex. 신뢰 가능한 소스가 없으면 추정하지 않고 N/A로 유지합니다.
  </div>

  <h2>데이터 출처</h2>
  <div class="card">
    <div>FRED series:
      <a href="https://fred.stlouisfed.org/series/DGS2">DGS2</a>,
      <a href="https://fred.stlouisfed.org/series/DGS10">DGS10</a>,
      <a href="https://fred.stlouisfed.org/series/DGS30">DGS30</a>,
      <a href="https://fred.stlouisfed.org/series/DFII10">DFII10</a>,
      <a href="https://fred.stlouisfed.org/series/BAMLC0A0CM">IG OAS</a>,
      <a href="https://fred.stlouisfed.org/series/BAMLH0A0HYM2">HY OAS</a>.
    </div>
    <div class="muted" style="margin-top:8px">
      이 대시보드는 투자 판단 보조용 규칙 기반 도구이며 투자 권유가 아닙니다.
    </div>
  </div>
</div>
</body>
</html>"""


def main():
    api_key = os.getenv("FRED_API_KEY", "").strip()
    if not api_key:
        print("ERROR: FRED_API_KEY environment variable is not set.", file=sys.stderr)
        sys.exit(2)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DOCS_DIR.mkdir(parents=True, exist_ok=True)

    raw = {}
    for name, series_id in SERIES.items():
        print(f"Fetching {name}: {series_id}")
        raw[name] = fetch_series(series_id, api_key)

    payload = build_payload(raw)
    save_json(payload)
    save_history(payload)

    html = build_html(payload)
    (DOCS_DIR / "index.html").write_text(html, encoding="utf-8")

    print(json.dumps(
        {
            "regime": payload["risk"]["regime"],
            "action": payload["risk"]["action"],
            "1987": payload["risk"]["risk_1987"],
            "1999": payload["risk"]["risk_1999"],
            "date": payload["latest"]["10y"]["date"],
        },
        ensure_ascii=False,
        indent=2,
    ))


if __name__ == "__main__":
    main()
