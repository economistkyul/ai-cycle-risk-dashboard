#!/usr/bin/env python3
"""
AI Cycle Risk Dashboard - v2
FRED collector + rule-based risk engine + readable daily dashboard.

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
    "term_premium": "THREEFYTP10",
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
    "term_premium": "10Y Term Premium",
    "fed_upper": "Fed target upper",
    "fed_lower": "Fed target lower",
    "ig_oas": "US IG OAS",
    "hy_oas": "US HY OAS",
}

MEANINGS = {
    "2y": "연준의 향후 정책금리 경로를 민감하게 반영합니다. 빠른 상승은 1999형 긴축 재가격 신호입니다.",
    "2s10s": "2년물과 10년물의 금리차입니다. 빠른 평탄화나 역전은 경기·긴축 스트레스가 커지고 있다는 뜻입니다.",
    "10y": "미국 금융시장의 대표 장기 할인율입니다. 급등하면 성장주와 장기자산의 밸류에이션 부담이 커집니다.",
    "30y": "재정·수급·장기 인플레이션 불확실성에 민감합니다. 1987형 장기금리 충격을 볼 때 핵심 지표입니다.",
    "real10y": "인플레이션 기대를 제외한 실질 할인율입니다. 상승할수록 주식·금·부동산 등 위험자산의 부담이 커집니다.",
    "term_premium": "장기채를 보유하는 위험에 대해 시장이 요구하는 추가 보상입니다. 상승하면 장기금리의 '위험 프리미엄' 성격이 강해집니다.",
    "ig_oas": "우량 회사채의 국채 대비 추가 금리입니다. 확대되면 자본공급자의 위험회피가 커지고 있다는 뜻입니다.",
    "hy_oas": "하이일드 회사채의 신용위험 프리미엄입니다. 급격한 확대는 금융환경 악화와 신용 스트레스를 의미합니다.",
}

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
DOCS_DIR = ROOT / "docs"
LATEST_JSON = DATA_DIR / "latest.json"
HISTORY_CSV = DATA_DIR / "history.csv"

STATUS_RANK = {"안정": 0, "관찰": 1, "주의": 2, "경계": 3}


def fetch_series(series_id, api_key, limit=180):
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
        headers={"User-Agent": "ai-cycle-risk-dashboard/2.0"},
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
            out.append({
                "date": x["date"],
                "value": (x["value"] - short_map[x["date"]]) * 100.0,
            })
    out.sort(key=lambda x: x["date"])
    if not out:
        raise RuntimeError("Could not align Treasury series for curve calculation.")
    return out


def bp(x):
    return None if x is None else x * 100.0


def round_or_none(x, digits=2):
    return None if x is None else round(x, digits)


def days_between(a, b):
    try:
        da = datetime.strptime(a, "%Y-%m-%d").date()
        db = datetime.strptime(b, "%Y-%m-%d").date()
        return (db - da).days
    except Exception:
        return None


def status_obj(status, interpretation):
    return {"status": status, "interpretation": interpretation}


def score_1999(d):
    score = 0
    reasons = []

    two_y_5 = d["changes"]["2y"]["5d_bp"]
    two_y_20 = d["changes"]["2y"]["20d_bp"]
    curve_5 = d["curves"]["2s10s"]["5d_change_bp"]
    curve_20 = d["curves"]["2s10s"]["20d_change_bp"]
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
    elif curve_20 is not None and curve_20 <= -25:
        score += 15
        reasons.append("2s10s 20일 -25bp 이상 평탄화")
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
    tp_5 = d["changes"]["term_premium"]["5d_bp"]
    tp_20 = d["changes"]["term_premium"]["20d_bp"]
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

    if tp_5 is not None and tp_5 >= 10:
        score += 10
        reasons.append("Term premium 5일 +10bp 이상")
    if tp_20 is not None and tp_20 >= 20:
        score += 15
        reasons.append("Term premium 20일 +20bp 이상")

    if (
        speed
        and real_20 is not None and real_20 >= 20
        and tp_20 is not None and tp_20 >= 10
    ):
        score += 15
        reasons.append("롱엔드 급등 + 실질금리 + term premium 동반 상승")

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


def build_metric_statuses(d):
    s = {}

    # 2Y
    ch5 = d["changes"]["2y"]["5d_bp"]
    if ch5 is not None and ch5 >= 50:
        s["2y"] = status_obj("경계", "단기금리가 매우 빠르게 올라 긴축 재가격 압력이 큽니다.")
    elif ch5 is not None and ch5 >= 25:
        s["2y"] = status_obj("주의", "2년물 상승속도가 빨라지고 있습니다. 연준 경로 재가격을 점검해야 합니다.")
    elif ch5 is not None and ch5 >= 15:
        s["2y"] = status_obj("관찰", "2년물이 완만하게 상승 중입니다. 추가 가속 여부를 봅니다.")
    else:
        s["2y"] = status_obj("안정", "2년물에서 급격한 긴축 재가격 신호는 아직 없습니다.")

    # 2s10s
    curve = d["curves"]["2s10s"]["value_bp"]
    c5 = d["curves"]["2s10s"]["5d_change_bp"]
    c20 = d["curves"]["2s10s"]["20d_change_bp"]
    if curve is not None and curve <= 0:
        s["2s10s"] = status_obj("경계", "2s10s가 역전됐습니다. 1999형 긴축 스트레스 조건을 강하게 확인해야 합니다.")
    elif c5 is not None and c5 <= -15:
        s["2s10s"] = status_obj("주의", "최근 5일간 커브가 빠르게 평탄화되고 있습니다.")
    elif (curve is not None and curve <= 25) or (c20 is not None and c20 <= -20):
        s["2s10s"] = status_obj("관찰", "커브가 역전 구간에 가까워지거나 중기 평탄화가 진행 중입니다.")
    else:
        direction = "스티프닝" if (c5 or 0) > 0 else "큰 변화 없음"
        s["2s10s"] = status_obj("안정", f"현재 역전은 아니며 최근 흐름은 {direction}입니다.")

    # 10Y
    y10 = d["latest"]["10y"]["value"]
    y10_20 = d["changes"]["10y"]["20d_bp"]
    if y10 >= 5.30:
        s["10y"] = status_obj("경계", "10년물이 1987형 절대 경계선에 진입했습니다.")
    elif y10 >= 5.10:
        s["10y"] = status_obj("주의", "10년물이 높은 구간에 있어 추가 상승 시 밸류에이션 압력이 커질 수 있습니다.")
    elif y10_20 is not None and y10_20 >= 25:
        s["10y"] = status_obj("관찰", "10년물의 20일 상승속도가 빨라지고 있습니다.")
    else:
        s["10y"] = status_obj("안정", "10년물은 핵심 경계선 아래이며 상승속도도 아직 극단적이지 않습니다.")

    # 30Y
    y30 = d["latest"]["30y"]["value"]
    y30_20 = d["changes"]["30y"]["20d_bp"]
    if y30 >= 5.75:
        s["30y"] = status_obj("경계", "30년물이 1987형 절대 경계선 5.75% 이상입니다.")
    elif y30 >= 5.50 or (y30_20 is not None and y30_20 >= 40):
        s["30y"] = status_obj("주의", "장기물 수준 또는 상승속도가 부담스러운 구간입니다.")
    elif y30 >= 5.25 or (y30_20 is not None and y30_20 >= 25):
        s["30y"] = status_obj("관찰", "30년물이 높은 수준입니다. 5.50~5.75% 접근 여부를 봅니다.")
    else:
        s["30y"] = status_obj("안정", "30년물은 핵심 장기금리 경계선 아래입니다.")

    # Real yield
    real20 = d["changes"]["real10y"]["20d_bp"]
    if real20 is not None and real20 >= 30:
        s["real10y"] = status_obj("경계", "실질금리가 매우 빠르게 올라 위험자산 할인율 충격이 커지고 있습니다.")
    elif real20 is not None and real20 >= 20:
        s["real10y"] = status_obj("주의", "실질 할인율 상승이 뚜렷합니다.")
    elif real20 is not None and real20 >= 10:
        s["real10y"] = status_obj("관찰", "실질금리가 완만하게 상승하고 있습니다.")
    else:
        s["real10y"] = status_obj("안정", "실질금리의 급격한 상승 신호는 아직 없습니다.")

    # Term premium
    tp5 = d["changes"]["term_premium"]["5d_bp"]
    tp20 = d["changes"]["term_premium"]["20d_bp"]
    if (tp5 is not None and tp5 >= 20) or (tp20 is not None and tp20 >= 35):
        s["term_premium"] = status_obj("경계", "장기채 보유 위험에 대한 보상이 빠르게 뛰고 있습니다.")
    elif (tp5 is not None and tp5 >= 10) or (tp20 is not None and tp20 >= 20):
        s["term_premium"] = status_obj("주의", "term premium 상승이 장기금리 상승에 기여하고 있을 가능성이 커졌습니다.")
    elif tp20 is not None and tp20 >= 10:
        s["term_premium"] = status_obj("관찰", "term premium이 완만하게 상승 중입니다.")
    else:
        s["term_premium"] = status_obj("안정", "term premium 급등 신호는 아직 없습니다.")

    # IG
    ig = d["latest"]["ig_oas"]["value"]
    ig20 = d["changes"]["ig_oas"]["20d_bp"]
    if ig >= 130:
        s["ig_oas"] = status_obj("경계", "우량 신용시장까지 스트레스가 강하게 번지고 있습니다.")
    elif ig >= 100:
        s["ig_oas"] = status_obj("주의", "IG OAS가 경고선 100bp 이상입니다.")
    elif ig >= 90 or (ig20 is not None and ig20 >= 10):
        s["ig_oas"] = status_obj("관찰", "신용스프레드가 경고선에 접근하거나 확대되고 있습니다.")
    else:
        s["ig_oas"] = status_obj("안정", "IG 신용스프레드는 현재 경고선 아래입니다.")

    # HY
    hy = d["latest"]["hy_oas"]["value"]
    hy20 = d["changes"]["hy_oas"]["20d_bp"]
    if hy >= 450:
        s["hy_oas"] = status_obj("경계", "하이일드 신용시장의 스트레스가 강한 수준입니다.")
    elif hy >= 350:
        s["hy_oas"] = status_obj("주의", "HY OAS가 경고선 350bp 이상입니다.")
    elif hy >= 325 or (hy20 is not None and hy20 >= 25):
        s["hy_oas"] = status_obj("관찰", "하이일드 스프레드가 경고선에 접근하거나 확대되고 있습니다.")
    else:
        s["hy_oas"] = status_obj("안정", "HY 신용스프레드는 현재 경고선 아래입니다.")

    return s


def daily_risk_impulse(d):
    """
    Directional score for the latest observation versus the prior observation.
    This is deliberately simple: it tells the user whether today's move was
    broadly risk-increasing or risk-reducing, not whether the absolute regime changed.
    """
    score = 0
    reasons_up = []
    reasons_down = []

    def add_if(change, up_threshold, down_threshold, up_text, down_text):
        nonlocal score
        if change is None:
            return
        if change >= up_threshold:
            score += 1
            reasons_up.append(up_text)
        elif change <= down_threshold:
            score -= 1
            reasons_down.append(down_text)

    add_if(d["changes"]["2y"]["1d_bp"], 5, -5, "2Y 상승", "2Y 하락")
    add_if(d["changes"]["10y"]["1d_bp"], 5, -5, "10Y 상승", "10Y 하락")
    add_if(d["changes"]["30y"]["1d_bp"], 5, -5, "30Y 상승", "30Y 하락")
    add_if(d["changes"]["real10y"]["1d_bp"], 5, -5, "실질금리 상승", "실질금리 하락")
    add_if(d["changes"]["term_premium"]["1d_bp"], 5, -5, "term premium 상승", "term premium 하락")
    add_if(d["changes"]["ig_oas"]["1d_bp"], 5, -5, "IG OAS 확대", "IG OAS 축소")
    add_if(d["changes"]["hy_oas"]["1d_bp"], 10, -10, "HY OAS 확대", "HY OAS 축소")

    curve1 = d["curves"]["2s10s"]["1d_change_bp"]
    if curve1 is not None:
        if curve1 <= -5:
            score += 1
            reasons_up.append("2s10s 평탄화")
        elif curve1 >= 5:
            score -= 1
            reasons_down.append("2s10s 스티프닝")

    if score >= 2:
        return {
            "label": "악화",
            "arrow": "↑",
            "text": "전 관측 대비 위험요인이 늘었습니다.",
            "drivers": reasons_up[:3],
        }
    if score <= -2:
        return {
            "label": "완화",
            "arrow": "↓",
            "text": "전 관측 대비 위험요인이 완화됐습니다.",
            "drivers": reasons_down[:3],
        }
    return {
        "label": "유사",
        "arrow": "→",
        "text": "전 관측 대비 큰 방향 변화는 없습니다.",
        "drivers": (reasons_up + reasons_down)[:3],
    }


def summary_lines(d):
    lines = []

    curve = d["curves"]["2s10s"]
    if curve["value_bp"] > 0 and (curve["5d_change_bp"] or 0) >= 0:
        lines.append(f"커브: 2s10s {curve['value_bp']:+.0f}bp, 5D {curve['5d_change_bp']:+.0f}bp → 현재는 역전보다 스티프닝 쪽입니다.")
    elif curve["value_bp"] <= 0:
        lines.append(f"커브: 2s10s {curve['value_bp']:+.0f}bp → 역전 구간으로 1999형 경계를 높여야 합니다.")
    else:
        lines.append(f"커브: 2s10s {curve['value_bp']:+.0f}bp, 5D {curve['5d_change_bp']:+.0f}bp → 평탄화 속도를 점검합니다.")

    y30 = d["latest"]["30y"]["value"]
    y30_20 = d["changes"]["30y"]["20d_bp"]
    lines.append(
        f"롱엔드: 30Y {y30:.2f}%, 20D {y30_20:+.0f}bp → "
        + ("절대 경계선 5.75% 이상입니다." if y30 >= 5.75
           else "5.75% 절대 경계선 아래지만 장기금리 상승속도를 계속 봅니다.")
    )

    ig = d["latest"]["ig_oas"]["value"]
    hy = d["latest"]["hy_oas"]["value"]
    lines.append(
        f"신용: IG {ig:.0f}bp / HY {hy:.0f}bp → "
        + ("신용 경고선이 작동 중입니다." if ig >= 100 or hy >= 350
           else "현재는 IG 100bp / HY 350bp 경고선 아래입니다.")
    )

    return lines


def build_payload(raw):
    latest_values = {}
    for k, rows in raw.items():
        value = latest(rows)["value"]
        if k in ("ig_oas", "hy_oas"):
            value *= 100.0
        latest_values[k] = {
            "series_id": SERIES[k],
            "label": LABELS[k],
            "date": latest(rows)["date"],
            "value": value,
        }

    changes = {}
    for k, rows in raw.items():
        changes[k] = {
            "1d_bp": round_or_none(bp(delta(rows, 1)), 1),
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
            "1d_change_bp": round_or_none(delta(rows, 1), 1),
            "5d_change_bp": round_or_none(delta(rows, 5), 1),
            "10d_change_bp": round_or_none(delta(rows, 10), 1),
            "20d_change_bp": round_or_none(delta(rows, 20), 1),
        }

    ref_date = max(x["date"] for x in latest_values.values())
    for item in latest_values.values():
        item["age_days_vs_latest"] = days_between(item["date"], ref_date)

    payload = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "reference_date": ref_date,
        "latest": latest_values,
        "changes": changes,
        "curves": curves,
        "data_notes": {
            "term_premium": "FRED THREEFYTP10 (Fed Board Kim-Wright model). NY Fed ACM direct feed can replace this later.",
            "move": "N/A - v2.1에서 신뢰 가능한 소스 연동 예정",
            "fedwatch": "N/A - v2.1에서 회의별 확률 연동 예정",
            "oracle_cds": "N/A - 신뢰 가능한 무료 소스 확보 전까지 미추정",
            "market_stage": "N/A - FedWatch 연동 후 단계 판정 추가 예정",
        },
    }

    r1999, reasons1999 = score_1999(payload)
    r1987, reasons1987 = score_1987(payload)
    regime, action = classify_regime(payload, r1987, r1999)

    payload["metric_status"] = build_metric_statuses(payload)
    highest = max(payload["metric_status"].values(), key=lambda x: STATUS_RANK[x["status"]])["status"]

    payload["risk"] = {
        "risk_1999": r1999,
        "risk_1987": r1987,
        "reasons_1999": reasons1999,
        "reasons_1987": reasons1987,
        "regime": regime,
        "action": action,
        "overall_status": highest,
        "closer_to": (
            "1987형" if r1987 > r1999
            else "1999형" if r1999 > r1987
            else "혼합/중립"
        ),
    }

    payload["daily_change"] = daily_risk_impulse(payload)
    payload["summary"] = summary_lines(payload)
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
        "overall_status": payload["risk"]["overall_status"],
        "daily_change": payload["daily_change"]["label"],
        "regime": payload["risk"]["regime"],
        "action": payload["risk"]["action"],
        "risk_1987": payload["risk"]["risk_1987"],
        "risk_1999": payload["risk"]["risk_1999"],
        "2y": payload["latest"]["2y"]["value"],
        "10y": payload["latest"]["10y"]["value"],
        "30y": payload["latest"]["30y"]["value"],
        "real10y": payload["latest"]["real10y"]["value"],
        "term_premium": payload["latest"]["term_premium"]["value"],
        "2s10s_bp": payload["curves"]["2s10s"]["value_bp"],
        "ig_oas": payload["latest"]["ig_oas"]["value"],
        "hy_oas": payload["latest"]["hy_oas"]["value"],
    }

    fieldnames = list(row.keys())
    existing = []
    if HISTORY_CSV.exists():
        with HISTORY_CSV.open("r", encoding="utf-8", newline="") as f:
            existing = list(csv.DictReader(f))

    normalized = []
    replaced = False
    for old in existing:
        if old.get("date") == row["date"]:
            normalized.append({k: str(row.get(k, "")) for k in fieldnames})
            replaced = True
        else:
            normalized.append({k: old.get(k, "") for k in fieldnames})

    if not replaced:
        normalized.append({k: str(row.get(k, "")) for k in fieldnames})

    with HISTORY_CSV.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(normalized)


def fmt_pct(x):
    return f"{x:.2f}%"


def fmt_bp(x, signed=True):
    if x is None:
        return "N/A"
    if signed:
        return f"{x:+.0f}bp"
    return f"{x:.0f}bp"


def html_escape(s):
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def freshness_text(item):
    age = item.get("age_days_vs_latest")
    if age is None or age <= 2:
        return f"데이터 {item['date']}"
    if age <= 7:
        return f"데이터 {item['date']} · {age}일 지연"
    return f"데이터 {item['date']} · 지연 주의({age}일)"


def build_html(payload):
    r = payload["risk"]
    dc = payload["daily_change"]

    status_colors = {
        "안정": ("#166534", "#dcfce7"),
        "관찰": ("#1d4ed8", "#dbeafe"),
        "주의": ("#92400e", "#fef3c7"),
        "경계": ("#991b1b", "#fee2e2"),
    }
    regime_colors = {
        "공격": ("#166534", "#dcfce7"),
        "경계": ("#92400e", "#fef3c7"),
        "방어": ("#991b1b", "#fee2e2"),
    }

    def badge(text, fg, bg):
        return f'<span class="badge" style="color:{fg};background:{bg}">{html_escape(text)}</span>'

    overall_fg, overall_bg = status_colors[r["overall_status"]]
    regime_fg, regime_bg = regime_colors[r["regime"]]

    def metric_card(key, title, value, d1, d5, d20):
        st = payload["metric_status"][key]
        fg, bg = status_colors[st["status"]]
        item = payload["latest"].get(key)
        fresh = freshness_text(item) if item else ""
        return f"""
        <div class="metric-card">
          <div class="metric-head">
            <div>
              <div class="metric-title">{html_escape(title)}</div>
              <div class="fresh">{html_escape(fresh)}</div>
            </div>
            {badge(st["status"], fg, bg)}
          </div>
          <div class="metric-value">{html_escape(value)}</div>
          <div class="changes">
            <span>전 관측 {html_escape(d1)}</span>
            <span>5D {html_escape(d5)}</span>
            <span>20D {html_escape(d20)}</span>
          </div>
          <div class="meaning"><b>의미</b> {html_escape(MEANINGS[key])}</div>
          <div class="interpret"><b>현재 해석</b> {html_escape(st["interpretation"])}</div>
        </div>
        """

    cards = "".join([
        metric_card(
            "2y", "2Y Treasury",
            fmt_pct(payload["latest"]["2y"]["value"]),
            fmt_bp(payload["changes"]["2y"]["1d_bp"]),
            fmt_bp(payload["changes"]["2y"]["5d_bp"]),
            fmt_bp(payload["changes"]["2y"]["20d_bp"]),
        ),
        metric_card(
            "2s10s", "2s10s Curve",
            fmt_bp(payload["curves"]["2s10s"]["value_bp"]),
            fmt_bp(payload["curves"]["2s10s"]["1d_change_bp"]),
            fmt_bp(payload["curves"]["2s10s"]["5d_change_bp"]),
            fmt_bp(payload["curves"]["2s10s"]["20d_change_bp"]),
        ),
        metric_card(
            "10y", "10Y Treasury",
            fmt_pct(payload["latest"]["10y"]["value"]),
            fmt_bp(payload["changes"]["10y"]["1d_bp"]),
            fmt_bp(payload["changes"]["10y"]["5d_bp"]),
            fmt_bp(payload["changes"]["10y"]["20d_bp"]),
        ),
        metric_card(
            "30y", "30Y Treasury",
            fmt_pct(payload["latest"]["30y"]["value"]),
            fmt_bp(payload["changes"]["30y"]["1d_bp"]),
            fmt_bp(payload["changes"]["30y"]["5d_bp"]),
            fmt_bp(payload["changes"]["30y"]["20d_bp"]),
        ),
        metric_card(
            "real10y", "10Y Real Yield",
            fmt_pct(payload["latest"]["real10y"]["value"]),
            fmt_bp(payload["changes"]["real10y"]["1d_bp"]),
            fmt_bp(payload["changes"]["real10y"]["5d_bp"]),
            fmt_bp(payload["changes"]["real10y"]["20d_bp"]),
        ),
        metric_card(
            "term_premium", "10Y Term Premium",
            fmt_pct(payload["latest"]["term_premium"]["value"]),
            fmt_bp(payload["changes"]["term_premium"]["1d_bp"]),
            fmt_bp(payload["changes"]["term_premium"]["5d_bp"]),
            fmt_bp(payload["changes"]["term_premium"]["20d_bp"]),
        ),
        metric_card(
            "ig_oas", "IG OAS",
            fmt_bp(payload["latest"]["ig_oas"]["value"], signed=False),
            fmt_bp(payload["changes"]["ig_oas"]["1d_bp"]),
            fmt_bp(payload["changes"]["ig_oas"]["5d_bp"]),
            fmt_bp(payload["changes"]["ig_oas"]["20d_bp"]),
        ),
        metric_card(
            "hy_oas", "HY OAS",
            fmt_bp(payload["latest"]["hy_oas"]["value"], signed=False),
            fmt_bp(payload["changes"]["hy_oas"]["1d_bp"]),
            fmt_bp(payload["changes"]["hy_oas"]["5d_bp"]),
            fmt_bp(payload["changes"]["hy_oas"]["20d_bp"]),
        ),
    ])

    reasons87 = "".join(f"<li>{html_escape(x)}</li>" for x in r["reasons_1987"]) or "<li>핵심 트리거 없음</li>"
    reasons99 = "".join(f"<li>{html_escape(x)}</li>" for x in r["reasons_1999"]) or "<li>핵심 트리거 없음</li>"
    summary_html = "".join(f"<li>{html_escape(x)}</li>" for x in payload["summary"])
    drivers_html = ", ".join(dc["drivers"]) if dc["drivers"] else "뚜렷한 단일 요인 없음"

    return f"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI Cycle Risk Dashboard</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Apple SD Gothic Neo", sans-serif;
    background: #f5f7fa;
    color: #111827;
  }}
  .wrap {{ max-width: 1100px; margin: 0 auto; padding: 18px; }}
  .hero {{
    background: #111827; color: white; border-radius: 20px; padding: 22px; margin-bottom: 14px;
  }}
  .hero-top {{ display:flex; gap:8px; flex-wrap:wrap; margin-bottom:10px; }}
  .badge {{
    display:inline-block; padding:5px 10px; border-radius:999px; font-size:12px; font-weight:750;
  }}
  h1 {{ font-size:25px; margin:4px 0 8px; }}
  h2 {{ font-size:18px; margin:24px 0 10px; }}
  .hero-line {{ font-size:16px; line-height:1.55; }}
  .hero-small {{ color:#cbd5e1; font-size:13px; margin-top:8px; }}
  .top-grid {{ display:grid; grid-template-columns:1.3fr 1fr 1fr; gap:10px; }}
  .panel, .metric-card {{
    background:white; border:1px solid #e5e7eb; border-radius:16px; padding:16px;
  }}
  .panel-title {{ font-size:13px; color:#6b7280; margin-bottom:7px; }}
  .big {{ font-size:28px; font-weight:800; }}
  .small {{ font-size:13px; color:#4b5563; line-height:1.5; }}
  .summary {{
    background:white; border:1px solid #e5e7eb; border-radius:16px; padding:16px 18px;
  }}
  .summary ul {{ margin:0; padding-left:20px; }}
  .summary li {{ margin:8px 0; line-height:1.45; }}
  .metrics {{ display:grid; grid-template-columns:repeat(2,1fr); gap:10px; }}
  .metric-head {{ display:flex; align-items:flex-start; justify-content:space-between; gap:8px; }}
  .metric-title {{ font-size:14px; font-weight:750; }}
  .fresh {{ font-size:11px; color:#9ca3af; margin-top:3px; }}
  .metric-value {{ font-size:28px; font-weight:800; margin:12px 0 8px; }}
  .changes {{ display:flex; gap:7px; flex-wrap:wrap; margin-bottom:12px; }}
  .changes span {{
    background:#f3f4f6; padding:4px 7px; border-radius:7px; font-size:12px; color:#374151;
  }}
  .meaning, .interpret {{
    font-size:13px; line-height:1.5; padding-top:9px; border-top:1px solid #f1f5f9;
  }}
  .meaning {{ color:#6b7280; }}
  .interpret {{ color:#1f2937; margin-top:7px; }}
  .riskgrid {{ display:grid; grid-template-columns:1fr 1fr; gap:10px; }}
  .score {{ font-size:34px; font-weight:850; }}
  .riskgrid ul {{ margin:8px 0 0 18px; padding:0; }}
  .riskgrid li {{ margin:5px 0; }}
  .legend {{ display:flex; flex-wrap:wrap; gap:8px; }}
  .legend-item {{ background:white; border:1px solid #e5e7eb; border-radius:12px; padding:10px 12px; font-size:12px; }}
  table {{ width:100%; border-collapse:collapse; background:white; border-radius:14px; overflow:hidden; }}
  th, td {{ text-align:left; border-bottom:1px solid #e5e7eb; padding:10px; font-size:13px; }}
  th {{ background:#f9fafb; }}
  .note {{ background:white; border-left:4px solid #9ca3af; padding:12px 14px; margin-top:10px; font-size:12px; color:#4b5563; line-height:1.5; }}
  a {{ color:#2563eb; }}
  @media (max-width:760px) {{
    .top-grid {{ grid-template-columns:1fr; }}
    .metrics {{ grid-template-columns:1fr; }}
    .riskgrid {{ grid-template-columns:1fr; }}
  }}
</style>
</head>
<body>
<div class="wrap">

  <section class="hero">
    <div class="hero-top">
      {badge("오늘 상태 " + r["overall_status"], overall_fg, overall_bg)}
      {badge("운용 레짐 " + r["regime"], regime_fg, regime_bg)}
      <span class="badge" style="color:#111827;background:#e5e7eb">전 관측 {dc["arrow"]} {html_escape(dc["label"])}</span>
    </div>
    <h1>AI Cycle Risk Dashboard</h1>
    <div class="hero-line"><b>{html_escape(r["action"])}</b> · 현재 상대적으로 {html_escape(r["closer_to"])} 위험축을 더 주시합니다.</div>
    <div class="hero-small">최신 데이터 기준일은 지표별로 다를 수 있습니다. 카드마다 데이터 날짜를 표시합니다.</div>
  </section>

  <div class="top-grid">
    <div class="panel">
      <div class="panel-title">전 관측 대비</div>
      <div class="big">{dc["arrow"]} {html_escape(dc["label"])}</div>
      <div class="small">{html_escape(dc["text"])}</div>
      <div class="small" style="margin-top:6px">주요 변화: {html_escape(drivers_html)}</div>
    </div>
    <div class="panel">
      <div class="panel-title">1987형 Long-end Risk</div>
      <div class="big">{r["risk_1987"]}/100</div>
      <div class="small">장기금리 · 실질금리 · term premium · 신용의 조합을 봅니다.</div>
    </div>
    <div class="panel">
      <div class="panel-title">1999형 Front-end Risk</div>
      <div class="big">{r["risk_1999"]}/100</div>
      <div class="small">2Y 급등 · 커브 평탄화/역전 · 신용 악화를 봅니다.</div>
    </div>
  </div>

  <h2>오늘 한눈에</h2>
  <div class="summary"><ul>{summary_html}</ul></div>

  <h2>핵심 지표 — 숫자 + 의미 + 상태</h2>
  <div class="metrics">{cards}</div>

  <h2>상태 읽는 법</h2>
  <div class="legend">
    <div class="legend-item"><b>안정</b> — 핵심 경고 조건 없음</div>
    <div class="legend-item"><b>관찰</b> — 임계치 접근 또는 완만한 악화</div>
    <div class="legend-item"><b>주의</b> — 명확한 초기 경고 신호</div>
    <div class="legend-item"><b>경계</b> — 강한 트리거 또는 핵심 임계치 진입</div>
  </div>

  <h2>역사적 위험 패턴</h2>
  <div class="riskgrid">
    <div class="panel">
      <div class="panel-title">1987형 · Long-end / credibility stress</div>
      <div class="score">{r["risk_1987"]}/100</div>
      <ul>{reasons87}</ul>
    </div>
    <div class="panel">
      <div class="panel-title">1999형 · Front-end / tightening stress</div>
      <div class="score">{r["risk_1999"]}/100</div>
      <ul>{reasons99}</ul>
    </div>
  </div>

  <h2>Yield Curve</h2>
  <table>
    <tr><th>Curve</th><th>현재</th><th>전 관측</th><th>5D</th><th>20D</th></tr>
    <tr><td>2s10s</td><td>{fmt_bp(payload["curves"]["2s10s"]["value_bp"])}</td><td>{fmt_bp(payload["curves"]["2s10s"]["1d_change_bp"])}</td><td>{fmt_bp(payload["curves"]["2s10s"]["5d_change_bp"])}</td><td>{fmt_bp(payload["curves"]["2s10s"]["20d_change_bp"])}</td></tr>
    <tr><td>3m10y</td><td>{fmt_bp(payload["curves"]["3m10y"]["value_bp"])}</td><td>{fmt_bp(payload["curves"]["3m10y"]["1d_change_bp"])}</td><td>{fmt_bp(payload["curves"]["3m10y"]["5d_change_bp"])}</td><td>{fmt_bp(payload["curves"]["3m10y"]["20d_change_bp"])}</td></tr>
    <tr><td>2s30s</td><td>{fmt_bp(payload["curves"]["2s30s"]["value_bp"])}</td><td>{fmt_bp(payload["curves"]["2s30s"]["1d_change_bp"])}</td><td>{fmt_bp(payload["curves"]["2s30s"]["5d_change_bp"])}</td><td>{fmt_bp(payload["curves"]["2s30s"]["20d_change_bp"])}</td></tr>
  </table>

  <div class="note">
    상태 표시는 규칙 기반 위험 신호이며 시장 방향을 예언하는 지표가 아닙니다.
    Term premium은 현재 FRED의 THREEFYTP10(Federal Reserve Board Kim-Wright model)을 사용하며,
    데이터 날짜가 늦으면 카드에 지연 표시가 나타납니다. NY Fed ACM 직접 연동은 다음 단계에서 교체할 수 있습니다.
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
            "overall_status": payload["risk"]["overall_status"],
            "daily_change": payload["daily_change"]["label"],
            "regime": payload["risk"]["regime"],
            "action": payload["risk"]["action"],
            "1987": payload["risk"]["risk_1987"],
            "1999": payload["risk"]["risk_1999"],
            "date": payload["reference_date"],
        },
        ensure_ascii=False,
        indent=2,
    ))


if __name__ == "__main__":
    main()
