"""
Free public API & price microstructure enrichment layer:
1. Daily historical risk-free rate from ^IRX (13-week T-Bill) with US Treasury FiscalData fallback
2. Quarter-End OHLCV Est. Quarter VWAP, Q-Low, Q-High, and Entry Zone status
3. SEC EDGAR XBRL CompanyFacts fundamentals (Market Cap via Shares Outstanding, Revenue YoY Growth,
   Operating Margin, FCF Margin, R&D Intensity, YoY Share Dilution)
4. Live mid-quarter Schedule 13D / 13G / 13D/A / 13G/A activist & >=5% stake alerts (XML + HTML Item 4)
5. SEC EDGAR Form 4 open-market ('P') insider cluster buy ingestion
"""

from __future__ import annotations

import html as html_lib
import json
import math
import re
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from sec_client import CACHE_DIR as SEC_CACHE_DIR, _parse_xml_root, fetch_bytes, fetch_json, get_submissions
from security_resolver import (
    _US_VALID_TICKER_RE,
    _normalize_legal_name,
    fetch_daily_prices,
    load_json_dict,
    normalize_yf_ticker,
    save_json_dict,
)

FUNDAMENTALS_CACHE_PATH = Path(__file__).resolve().parent / "fundamentals_cache.json"
XBRL_PARSER_VERSION = 2
MIDQUARTER_HDR_CACHE_PATH = SEC_CACHE_DIR / "hdr_13dg_cache.json"
ITEM4_PARSER_VERSION = 2
FORM4_PARSED_CACHE_PATH = SEC_CACHE_DIR / "form4_parsed_v1.json"
FORM4_TICKER_CACHE_PATH = SEC_CACHE_DIR / "form4_ticker_v1.json"
FORM4_PARSER_VERSION = 1
_13DG_FORM_RE = re.compile(r"^(?:SCHEDULE|SC)\s*13([DG])(?:/A)?$", re.I)
_13DG_ITEM4_MNA_RE = re.compile(
    r"\b(strategic\s+alternatives?|sale\s+of\s+(?:the\s+)?(?:issuer|company)|mergers?|business\s+combinations?|"
    r"take[-\s]?private|going[-\s]?private|go[-\s]?private|spin[-\s]?offs?|divest(?:iture)?s?|tender\s+offers?|acquisition\s+proposals?)\b",
    re.I,
)
_13DG_ITEM4_BOARD_RE = re.compile(
    r"\b(board\s+of\s+directors|board\s+seats?|board\s+representation|director\s+nominees?|proxy\s+(?:contests?|solicitations?)|"
    r"nominat(?:e|ed|es|ing|ions?)|governance|slates?\s+of\s+directors|board\s+observers?|declassify)\b",
    re.I,
)
_13DG_ITEM4_CAP_RE = re.compile(
    r"\b(share\s+repurchases?|stock\s+buybacks?|repurchase\s+programs?|special\s+dividends?|dividends?|"
    r"capital\s+allocation|capital\s+structure|recapitalizations?|undervalued|unlock(?:ing)?\s+(?:shareholder\s+)?value)\b",
    re.I,
)
_13DG_ITEM4_OPS_RE = re.compile(
    r"\b(discussions?\s+with\s+(?:management|the\s+board)|operational|cost\s+reductions?|margin\s+improvements?|"
    r"management\s+teams?|chief\s+executives?|ceos?|leadership|strategic\s+plans?)\b",
    re.I,
)
_NON_CSUITE_VP_RE = re.compile(
    r"\b(?:(?:ASSISTANT|ASSOC(?:IATE)?|REGIONAL|DIVISION(?:AL)?)\s+)?(?:VICE\s+PRESIDENT|VP)\b",
    re.I,
)
_CSUITE_TITLE_RE = re.compile(
    r"\b(CEO|CFO|COO|CTO|CRO|CMO|CIO|CHIEF\b|PRESIDENT|PRES\.?|EXECUTIVE\s+CHAIR|CHAIRMAN|FOUNDER|EVP|SVP|"
    r"PRINCIPAL\s+(?:EXECUTIVE|FINANCIAL|ACCOUNTING)\s+OFFICER)\b",
    re.I,
)
_NOT_10B5_RE = re.compile(
    r"\b(?:not|no)\s+(?:made\s+|effected\s+|executed\s+)?(?:pursuant\s+to|under)\s+(?:a\s+)?(?:Rule\s+)?10b5-1\b",
    re.I,
)


def fetch_risk_free_daily_rates(cal_dates: list[str]) -> dict[str, float]:
    """
    Return {YYYY-MM-DD: annualized_yield_pct} aligned over `cal_dates` using Yahoo ^IRX
    (CBOE 13-week Treasury Bill yield), falling back to US Treasury Fiscal Data API
    and forward-filling weekends/holidays.
    """
    if not cal_dates:
        return {}
    raw_irx: dict[str, float] = {}
    try:
        irx = fetch_daily_prices("^IRX", max_age_hours=24.0) or {}
        for dt, val in irx.items():
            if 0.0 <= val <= 15.0:
                raw_irx[dt] = float(val)
    except Exception:
        pass

    # Fallback / supplement from api.fiscaldata.treasury.gov if Yahoo ^IRX unavailable
    monthly_fb: list[tuple[str, float]] = []
    if len(raw_irx) < 60:
        try:
            url = (
                "https://api.fiscaldata.treasury.gov/services/api/fiscal_service/"
                "v2/accounting/od/avg_interest_rates?"
                "filter=security_desc:eq:Treasury%20Bills,record_date:gte:2020-10-01&page[size]=200"
            )
            req = urllib.request.Request(url, headers={"User-Agent": "PaperTrail13FResearch"})
            with urllib.request.urlopen(req, timeout=12) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            for row in payload.get("data") or []:
                rd = str(row.get("record_date") or "")[:10]
                try:
                    rate = float(row.get("avg_interest_rate_amt") or 0.0)
                except (TypeError, ValueError):
                    continue
                if rd and 0.0 <= rate <= 15.0:
                    monthly_fb.append((rd, rate))
            monthly_fb.sort()
        except Exception:
            pass

    out: dict[str, float] = {}
    last_val = 4.0
    # Seed initial pre-calendar yield if known
    first_dt = cal_dates[0]
    earlier = [v for dt, v in raw_irx.items() if dt <= first_dt]
    if earlier:
        last_val = earlier[-1]
    elif monthly_fb:
        fb_earlier = [v for dt, v in monthly_fb if dt <= first_dt]
        if fb_earlier:
            last_val = fb_earlier[-1]

    fb_idx = 0
    for d in cal_dates:
        if d in raw_irx:
            last_val = raw_irx[d]
        elif monthly_fb:
            while fb_idx + 1 < len(monthly_fb) and monthly_fb[fb_idx + 1][0] <= d:
                fb_idx += 1
            if monthly_fb[fb_idx][0] <= d and not raw_irx:
                last_val = monthly_fb[fb_idx][1]
        out[d] = last_val
    return out


def _quarter_start_for_period_end(period_end: str) -> str:
    try:
        dt = datetime.strptime(period_end, "%Y-%m-%d").date()
        q_start_month = ((dt.month - 1) // 3) * 3 + 1
        return f"{dt.year:04d}-{q_start_month:02d}-01"
    except Exception:
        return period_end


def compute_quarter_vwap_metrics(
    ohlcv_series: dict[str, list[float]] | None,
    period_end: str,
    latest_price: float | None,
) -> dict[str, Any]:
    """
    Compute Est. Quarter VWAP, Quarter Low, Quarter High, premium/discount vs Q-VWAP,
    and institutional Entry Zone classification from split-adjusted [close, low, high, volume].
    """
    empty = {
        "estQuarterVwap": None,
        "quarterLow": None,
        "quarterHigh": None,
        "pctVsQuarterVwap": None,
        "entryZoneStatus": None,
    }
    if not ohlcv_series or not period_end:
        return empty

    q_start = _quarter_start_for_period_end(period_end)
    q_rows = [vals for dt, vals in ohlcv_series.items() if q_start <= dt <= period_end and len(vals) >= 4 and vals[0] > 0]
    if not q_rows:
        return empty

    sum_pv = 0.0
    sum_v = 0.0
    sum_p = 0.0
    lows: list[float] = []
    highs: list[float] = []
    for c, l, h, v in q_rows:
        sum_p += c
        if v > 0:
            sum_pv += c * v
            sum_v += v
        lows.append(l if l > 0 else c)
        highs.append(h if h > 0 else c)

    vwap = (sum_pv / sum_v) if sum_v > 0 else (sum_p / len(q_rows))
    if not math.isfinite(vwap) or vwap <= 0:
        return empty

    q_low = min(lows)
    q_high = max(highs)
    pct_vs = None
    zone = None
    if latest_price and latest_price > 0:
        pct_vs = round(((latest_price / vwap) - 1.0) * 100.0, 1)
        if pct_vs <= -5.0:
            zone = "Discount to Q-Avg"
        elif pct_vs <= 10.0:
            zone = "Near Q-Avg"
        elif pct_vs <= 30.0:
            zone = "Extended"
        else:
            zone = "Chasing (+30%+)"

    return {
        "estQuarterVwap": round(vwap, 2),
        "quarterLow": round(q_low, 2),
        "quarterHigh": round(q_high, 2),
        "pctVsQuarterVwap": pct_vs,
        "entryZoneStatus": zone,
    }


def load_ticker_cik_mappings() -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    """
    Load SEC company_tickers.json mappings:
      - ticker_to_cik: {canonical US ticker: cik10}
      - cik_to_ticker: {cik10: canonical US ticker}
      - name_to_ticker: {normalized legal name: ticker}
    """
    ticker_to_cik: dict[str, str] = {}
    cik_to_ticker: dict[str, str] = {}
    name_to_sec_ciks: dict[str, set[str]] = {}
    try:
        data = fetch_json(
            "https://www.sec.gov/files/company_tickers.json",
            cache_key="company_tickers.json",
            max_age_hours=24.0,
        )
        for item in data.values():
            raw_t = str(item.get("ticker") or "")
            norm_t = normalize_yf_ticker(raw_t)
            if not norm_t:
                continue
            cik10 = f"{int(item['cik_str']):010d}"
            if norm_t not in ticker_to_cik:
                ticker_to_cik[norm_t] = cik10
            if raw_t.upper() not in ticker_to_cik:
                ticker_to_cik[raw_t.upper()] = cik10
            if cik10 not in cik_to_ticker and _US_VALID_TICKER_RE.fullmatch(norm_t):
                cik_to_ticker[cik10] = norm_t
            nn = _normalize_legal_name(str(item.get("title") or ""))
            if len(nn) >= 5 and _US_VALID_TICKER_RE.fullmatch(norm_t):
                name_to_sec_ciks.setdefault(nn, set()).add(norm_t)
    except Exception:
        pass
    name_to_ticker = {k: next(iter(v)) for k, v in name_to_sec_ciks.items() if len(v) == 1}
    return ticker_to_cik, cik_to_ticker, name_to_ticker


def _extract_latest_point_fact(
    facts_root: dict[str, Any],
    concept_list: list[tuple[str, str]],
    unit_keys: tuple[str, ...],
) -> float | None:
    facts_by_ns = facts_root.get("facts") or {}
    best_rank: tuple[str, str, int] = ("", "", -9999)
    best_val: float | None = None
    for prio_idx, (ns, concept) in enumerate(concept_list):
        c_node = (facts_by_ns.get(ns) or {}).get(concept) or {}
        units = c_node.get("units") or {}
        for uk in unit_keys:
            rows = units.get(uk) or []
            for r in rows:
                end = str(r.get("end") or "")
                filed = str(r.get("filed") or "")
                val = r.get("val")
                if not end or val is None:
                    continue
                try:
                    fval = float(val)
                except (TypeError, ValueError):
                    continue
                if fval <= 0:
                    continue
                cand_rank = (end, filed, -prio_idx)
                if cand_rank > best_rank:
                    best_rank = cand_rank
                    best_val = fval
    return best_val


def _expand_unit_keys(units: dict[str, Any], unit_keys: tuple[str, ...]) -> list[str]:
    if unit_keys == ("CURRENCY",):
        keys: list[str] = []
        if "USD" in units:
            keys.append("USD")
        for uk in units.keys():
            if uk != "USD" and re.fullmatch(r"[A-Z]{3}", str(uk)):
                keys.append(str(uk))
        return keys
    return [uk for uk in unit_keys if uk in units]


def _extract_ttm_and_prior_with_unit(
    facts_root: dict[str, Any],
    concept_list: list[tuple[str, str]],
    unit_keys: tuple[str, ...],
) -> tuple[float | None, float | None, str, str]:
    """
    Extract (ttm_current, ttm_prior_1y, as_of_end_date, unit_used) across ALL listed concepts,
    ranking candidates by data recency so tag migrations (e.g. RevenueFromContract... -> Revenues)
    never stall on legacy tags. Decomposes 6M/9M/12M YTD cumulative statements (common in
    Cash Flows) into standalone ~90d quarters when standalone quarterly rows are omitted.
    """
    facts_by_ns = facts_root.get("facts") or {}
    candidates: list[tuple[float, float | None, str, str, int]] = []

    for prio_idx, (ns, concept) in enumerate(concept_list):
        c_node = (facts_by_ns.get(ns) or {}).get(concept) or {}
        units = c_node.get("units") or {}
        for uk in _expand_unit_keys(units, unit_keys):
            rows = units.get(uk) or []
            if not rows:
                continue
            q_by_end: dict[str, tuple[str, float]] = {}
            a_by_end: dict[str, tuple[str, float]] = {}
            ytd_by_span: dict[tuple[str, str], tuple[str, float]] = {}
            for r in rows:
                start_s = str(r.get("start") or "")[:10]
                end_s = str(r.get("end") or "")[:10]
                filed_s = str(r.get("filed") or "")
                val = r.get("val")
                if not start_s or not end_s or val is None:
                    continue
                try:
                    d0 = datetime.strptime(start_s, "%Y-%m-%d").date()
                    d1 = datetime.strptime(end_s, "%Y-%m-%d").date()
                    fval = float(val)
                except Exception:
                    continue
                dur = (d1 - d0).days
                if dur <= 0:
                    continue
                prev_span = ytd_by_span.get((start_s, end_s))
                if prev_span is None or filed_s > prev_span[0]:
                    ytd_by_span[(start_s, end_s)] = (filed_s, fval)

                if 70 <= dur <= 115:
                    prev = q_by_end.get(end_s)
                    if prev is None or filed_s > prev[0]:
                        q_by_end[end_s] = (filed_s, fval)
                elif 330 <= dur <= 400:
                    prev = a_by_end.get(end_s)
                    if prev is None or filed_s > prev[0]:
                        a_by_end[end_s] = (filed_s, fval)

            # Decompose YTD cumulative periods sharing the exact fiscal-year start_s
            by_fy_start: dict[str, list[tuple[str, str, float]]] = {}
            for (st, en), (fd, fv) in ytd_by_span.items():
                by_fy_start.setdefault(st, []).append((en, fd, fv))
            for st, span_list in by_fy_start.items():
                span_list.sort(key=lambda x: x[0])
                for idx_s in range(1, len(span_list)):
                    prev_en, _, prev_fv = span_list[idx_s - 1]
                    cur_en, cur_fd, cur_fv = span_list[idx_s]
                    if cur_en in q_by_end:
                        continue
                    try:
                        d_prev = datetime.strptime(prev_en, "%Y-%m-%d").date()
                        d_cur = datetime.strptime(cur_en, "%Y-%m-%d").date()
                    except Exception:
                        continue
                    step_days = (d_cur - d_prev).days
                    if 70 <= step_days <= 115:
                        q_by_end[cur_en] = (cur_fd, cur_fv - prev_fv)

            q_dates = sorted(q_by_end.keys(), reverse=True)
            distinct_qs: list[tuple[str, float]] = []
            for qd in q_dates:
                if not distinct_qs:
                    distinct_qs.append((qd, q_by_end[qd][1]))
                else:
                    last_dt = datetime.strptime(distinct_qs[-1][0], "%Y-%m-%d").date()
                    cur_dt = datetime.strptime(qd, "%Y-%m-%d").date()
                    if (last_dt - cur_dt).days >= 70:
                        distinct_qs.append((qd, q_by_end[qd][1]))
                if len(distinct_qs) >= 8:
                    break

            a_dates = sorted(a_by_end.keys(), reverse=True)
            distinct_as: list[tuple[str, float]] = []
            for ad in a_dates:
                if not distinct_as:
                    distinct_as.append((ad, a_by_end[ad][1]))
                else:
                    last_dt = datetime.strptime(distinct_as[-1][0], "%Y-%m-%d").date()
                    cur_dt = datetime.strptime(ad, "%Y-%m-%d").date()
                    if (last_dt - cur_dt).days >= 300:
                        distinct_as.append((ad, a_by_end[ad][1]))
                if len(distinct_as) >= 2:
                    break

            if len(distinct_qs) >= 4:
                q0_dt = datetime.strptime(distinct_qs[0][0], "%Y-%m-%d").date()
                q3_dt = datetime.strptime(distinct_qs[3][0], "%Y-%m-%d").date()
                if (q0_dt - q3_dt).days <= 320:
                    ttm_cur = sum(v for _, v in distinct_qs[:4])
                    ttm_prev = None
                    if len(distinct_qs) == 8:
                        q4_dt = datetime.strptime(distinct_qs[4][0], "%Y-%m-%d").date()
                        q7_dt = datetime.strptime(distinct_qs[7][0], "%Y-%m-%d").date()
                        if (q3_dt - q4_dt).days <= 130 and (q4_dt - q7_dt).days <= 320:
                            ttm_prev = sum(v for _, v in distinct_qs[4:8])
                    candidates.append((ttm_cur, ttm_prev, distinct_qs[0][0], uk, prio_idx))
                    continue

            if distinct_as:
                candidates.append((
                    distinct_as[0][1],
                    distinct_as[1][1] if len(distinct_as) >= 2 else None,
                    distinct_as[0][0],
                    uk,
                    prio_idx,
                ))

    if not candidates:
        return None, None, "", ""

    def _cand_key(c: tuple[float, float | None, str, str, int]) -> tuple[int, str, int, int]:
        return (1 if c[3] == "USD" else 0, c[2], 1 if c[1] is not None else 0, -c[4])

    # Among USD candidates (if any exist) vs primary reporting currency, pick newest end date (tie-break by YoY & priority)
    best = max(candidates, key=_cand_key)
    return best[0], best[1], best[2], best[3]


def _extract_ttm_and_prior(
    facts_root: dict[str, Any],
    concept_list: list[tuple[str, str]],
    unit_keys: tuple[str, ...],
) -> tuple[float | None, float | None, str]:
    cur, prev, end, _ = _extract_ttm_and_prior_with_unit(facts_root, concept_list, unit_keys)
    return cur, prev, end


def _extract_annual_only(
    facts_root: dict[str, Any],
    concept_list: list[tuple[str, str]],
    unit_keys: tuple[str, ...],
) -> tuple[float | None, float | None, str]:
    """Extract latest full-year (10-K/20-F, 330-400 days) current, prior, and period end date across ALL concepts."""
    facts_by_ns = facts_root.get("facts") or {}
    candidates: list[tuple[float, float | None, str, str, int]] = []
    for prio_idx, (ns, concept) in enumerate(concept_list):
        c_node = (facts_by_ns.get(ns) or {}).get(concept) or {}
        units = c_node.get("units") or {}
        for uk in _expand_unit_keys(units, unit_keys):
            rows = units.get(uk) or []
            a_by_end: dict[str, tuple[str, float]] = {}
            for r in rows:
                start_s = str(r.get("start") or "")
                end_s = str(r.get("end") or "")
                filed_s = str(r.get("filed") or "")
                val = r.get("val")
                if not start_s or not end_s or val is None:
                    continue
                try:
                    dur = (datetime.strptime(end_s[:10], "%Y-%m-%d").date() - datetime.strptime(start_s[:10], "%Y-%m-%d").date()).days
                    fval = float(val)
                except Exception:
                    continue
                if 330 <= dur <= 400:
                    prev = a_by_end.get(end_s[:10])
                    if prev is None or filed_s > prev[0]:
                        a_by_end[end_s[:10]] = (filed_s, fval)
            a_dates = sorted(a_by_end.keys(), reverse=True)
            distinct_as: list[tuple[str, float]] = []
            for ad in a_dates:
                if not distinct_as or (datetime.strptime(distinct_as[-1][0], "%Y-%m-%d").date() - datetime.strptime(ad, "%Y-%m-%d").date()).days >= 300:
                    distinct_as.append((ad, a_by_end[ad][1]))
                if len(distinct_as) >= 2:
                    break
            if distinct_as:
                candidates.append((
                    distinct_as[0][1],
                    distinct_as[1][1] if len(distinct_as) >= 2 else None,
                    distinct_as[0][0],
                    uk,
                    prio_idx,
                ))
    if not candidates:
        return None, None, ""
    best = max(candidates, key=lambda c: (1 if c[3] == "USD" else 0, c[2], 1 if c[1] is not None else 0, -c[4]))
    return best[0], best[1], best[2]


def _parse_company_xbrl_facts(raw_facts: dict[str, Any]) -> dict[str, Any]:
    rev_concepts = [
        ("us-gaap", "RevenueFromContractWithCustomerExcludingAssessedTax"),
        ("us-gaap", "Revenues"),
        ("us-gaap", "SalesRevenueNet"),
        ("us-gaap", "RevenueFromContractWithCustomerIncludingAssessedTax"),
        ("ifrs-full", "Revenue"),
    ]
    op_concepts = [
        ("us-gaap", "OperatingIncomeLoss"),
        ("ifrs-full", "ProfitLossFromOperatingActivities"),
    ]
    ocf_concepts = [
        ("us-gaap", "NetCashProvidedByUsedInOperatingActivities"),
        ("ifrs-full", "CashFlowsFromUsedInOperatingActivities"),
    ]
    capex_concepts = [
        ("us-gaap", "PaymentsToAcquirePropertyPlantAndEquipment"),
        ("us-gaap", "PaymentsToAcquireProductiveAssets"),
        ("us-gaap", "PaymentsToAcquirePropertyPlantAndEquipmentAndIntangibleAssets"),
        ("ifrs-full", "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities"),
    ]
    rd_concepts = [
        ("us-gaap", "ResearchAndDevelopmentExpense"),
        ("us-gaap", "ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost"),
        ("ifrs-full", "ResearchAndDevelopmentExpense"),
    ]

    shares_out = _extract_latest_point_fact(
        raw_facts,
        [
            ("dei", "EntityCommonStockSharesOutstanding"),
            ("us-gaap", "CommonStockSharesOutstanding"),
            ("us-gaap", "WeightedAverageNumberOfDilutedSharesOutstanding"),
            ("ifrs-full", "NumberOfSharesIssued"),
        ],
        ("shares",),
    )

    rev_cur, rev_prev, rev_end, rev_unit = _extract_ttm_and_prior_with_unit(raw_facts, rev_concepts, ("CURRENCY",))
    ann_unit_keys = (rev_unit,) if rev_unit else ("CURRENCY",)
    rev_ann, rev_ann_prev, rev_ann_end = _extract_annual_only(raw_facts, rev_concepts, ann_unit_keys)
    if rev_prev is None and rev_ann and rev_ann_prev and (not rev_end or rev_end == rev_ann_end):
        rev_cur, rev_prev, rev_end = rev_ann, rev_ann_prev, rev_ann_end

    flow_unit_keys = (rev_unit,) if rev_unit else ("USD",)

    op_cur, _, op_end, _ = _extract_ttm_and_prior_with_unit(raw_facts, op_concepts, flow_unit_keys)
    if op_cur is not None and op_end != rev_end:
        op_ann, _, op_ann_end = _extract_annual_only(raw_facts, op_concepts, flow_unit_keys)
        if op_ann is not None and rev_ann and op_ann_end == rev_ann_end:
            op_cur, op_end = op_ann, op_ann_end

    ocf_cur, _, ocf_end, _ = _extract_ttm_and_prior_with_unit(raw_facts, ocf_concepts, flow_unit_keys)
    capex_cur, _, capex_end, _ = _extract_ttm_and_prior_with_unit(raw_facts, capex_concepts, flow_unit_keys)
    if ocf_cur is not None and ocf_end != rev_end:
        ocf_ann, _, ocf_ann_end = _extract_annual_only(raw_facts, ocf_concepts, flow_unit_keys)
        cap_ann, _, cap_ann_end = _extract_annual_only(raw_facts, capex_concepts, flow_unit_keys)
        if ocf_ann is not None and ocf_ann_end == rev_ann_end:
            ocf_cur, ocf_end = ocf_ann, ocf_ann_end
            if cap_ann is not None and cap_ann_end == ocf_ann_end:
                capex_cur, capex_end = cap_ann, cap_ann_end

    rd_cur, _, rd_end, _ = _extract_ttm_and_prior_with_unit(raw_facts, rd_concepts, flow_unit_keys)
    if rd_cur is not None and rd_end != rev_end:
        rd_ann, _, rd_ann_end = _extract_annual_only(raw_facts, rd_concepts, flow_unit_keys)
        if rd_ann is not None and rev_ann and rd_ann_end == rev_ann_end:
            rd_cur, rd_end = rd_ann, rd_ann_end

    dil_cur, dil_prev, _ = _extract_ttm_and_prior(
        raw_facts,
        [
            ("us-gaap", "WeightedAverageNumberOfDilutedSharesOutstanding"),
            ("ifrs-full", "AdjustedWeightedAverageShares"),
        ],
        ("shares",),
    )

    rev_growth = None
    if rev_cur is not None and rev_prev is not None and rev_prev > 0:
        g = ((rev_cur / rev_prev) - 1.0) * 100.0
        if -95.0 <= g <= 500.0:
            rev_growth = round(g, 1)
    elif rev_ann is not None and rev_ann_prev is not None and rev_ann_prev > 0:
        g = ((rev_ann / rev_ann_prev) - 1.0) * 100.0
        if -95.0 <= g <= 500.0:
            rev_growth = round(g, 1)

    op_margin = None
    op_den = rev_cur if (op_end == rev_end and rev_cur) else (rev_ann if (op_end == rev_ann_end and rev_ann) else None)
    if op_cur is not None and op_den and op_den > 0:
        om = (op_cur / op_den) * 100.0
        if -250.0 <= om <= 95.0:
            op_margin = round(om, 1)

    # Require matched CapEx reporting end-date (never fabricate 0.0 CapEx when missing)
    fcf_margin = None
    fcf_den = rev_cur if (ocf_end == rev_end and rev_cur) else (rev_ann if (ocf_end == rev_ann_end and rev_ann) else None)
    if ocf_cur is not None and fcf_den and fcf_den > 0 and capex_cur is not None and capex_end == ocf_end:
        fm = ((ocf_cur - abs(capex_cur)) / fcf_den) * 100.0
        if -250.0 <= fm <= 95.0:
            fcf_margin = round(fm, 1)

    rd_pct = None
    rd_den = rev_cur if (rd_end == rev_end and rev_cur) else (rev_ann if (rd_end == rev_ann_end and rev_ann) else None)
    if rd_cur is not None and rd_den and rd_den > 0:
        rp = (rd_cur / rd_den) * 100.0
        if 0.0 <= rp <= 150.0:
            rd_pct = round(rp, 1)

    dilution_pct = None
    if dil_cur is not None and dil_prev is not None and dil_prev > 0:
        dp = ((dil_cur / dil_prev) - 1.0) * 100.0
        if -40.0 <= dp <= 100.0:
            dilution_pct = round(dp, 1)

    rev_usd_val = None
    if rev_unit == "USD":
        if rev_cur:
            rev_usd_val = round(rev_cur, 0)
        elif rev_ann:
            rev_usd_val = round(rev_ann, 0)

    return {
        "sharesOutstanding": round(shares_out) if shares_out else None,
        "revenueTtmUsd": rev_usd_val,
        "revenueGrowthYoYPct": rev_growth,
        "operatingMarginPct": op_margin,
        "fcfMarginPct": fcf_margin,
        "rdIntensityPct": rd_pct,
        "shareDilutionYoYPct": dilution_pct,
    }


def fetch_xbrl_fundamentals_batch(
    tickers: list[str],
    ticker_to_cik: dict[str, str],
    max_age_hours: float = 168.0,
) -> dict[str, dict[str, Any]]:
    """
    Fetch & parse SEC EDGAR XBRL CompanyFacts for candidate/signal US stocks.
    Cached persistently in pipeline/fundamentals_cache.json with a 7-day TTL and parser version check.
    """
    cache = load_json_dict(FUNDAMENTALS_CACHE_PATH)
    now_ts = time.time()
    max_age_sec = max_age_hours * 3600.0

    to_fetch: list[tuple[str, str]] = []
    for t in sorted(set(tickers)):
        cik10 = ticker_to_cik.get(t)
        if not cik10:
            continue
        ent = cache.get(t)
        if (
            ent
            and ent.get("_ver") == XBRL_PARSER_VERSION
            and (now_ts - float(ent.get("_ts", 0.0))) <= max_age_sec
        ):
            continue
        to_fetch.append((t, cik10))

    if to_fetch:
        print(f"[xbrl] Fetching SEC EDGAR CompanyFacts for {len(to_fetch)}/{len(set(tickers))} tracked stocks...", flush=True)
        def _fetch_one(pair: tuple[str, str]) -> tuple[str, dict[str, Any] | None]:
            t, cik10 = pair
            try:
                raw = fetch_json(
                    f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik10}.json",
                    cache_key=f"xbrl_{cik10}.json",
                    max_age_hours=max_age_hours,
                )
                parsed = _parse_company_xbrl_facts(raw)
                parsed["cik"] = cik10
                parsed["_ver"] = XBRL_PARSER_VERSION
                parsed["_ts"] = now_ts
                return t, parsed
            except Exception:
                return t, {"_ts": now_ts, "_ver": XBRL_PARSER_VERSION, "cik": cik10}

        with ThreadPoolExecutor(max_workers=4) as ex:
            for idx_done, (t, parsed) in enumerate(ex.map(_fetch_one, to_fetch), 1):
                if parsed is not None:
                    cache[t] = parsed
                if idx_done % 80 == 0 or idx_done == len(to_fetch):
                    save_json_dict(FUNDAMENTALS_CACHE_PATH, cache)
                    print(f"[xbrl] Progress {idx_done}/{len(to_fetch)} facts parsed...", flush=True)

    return {
        t: {k: v for k, v in cache[t].items() if k not in ("_ts", "_ver")}
        for t in tickers
        if t in cache and cache[t]
    }


def _clean_text_excerpt(raw_txt: str, max_len: int = 340) -> str:
    """Clean and bound narrative SEC filing text (such as Schedule 13D Item 4)."""
    if not raw_txt:
        return ""
    txt = html_lib.unescape(str(raw_txt))
    txt = re.sub(r"<[^>]+>", " ", txt)
    txt = re.sub(r"\s+", " ", txt).strip()
    txt = re.sub(
        r"^(?:ITEM\s*4\.?\s*(?:PURPOSE\s+OF\s+(?:THE\s+)?TRANSACTION\.?\s*)?[-–—:\s]*)+",
        "",
        txt,
        flags=re.I,
    ).strip()
    if len(txt) <= max_len:
        return txt
    cut = txt[:max_len].rsplit(" ", 1)[0].rstrip(" ,;:-")
    return f"{cut}..." if cut else txt[:max_len]


def _classify_item4_intent(item4_text: str, is_activist: bool) -> str:
    """Categorize Schedule 13D Item 4 activist purpose or 13G passive filing."""
    if not is_activist:
        return "Passive (13G)"
    txt = item4_text or ""
    if _13DG_ITEM4_MNA_RE.search(txt):
        return "Strategic Review / M&A"
    if _13DG_ITEM4_BOARD_RE.search(txt):
        return "Board / Governance"
    if _13DG_ITEM4_CAP_RE.search(txt):
        return "Capital Return / Value"
    if _13DG_ITEM4_OPS_RE.search(txt):
        return "Management Engagement"
    return "Active 13D Intent" if txt.strip() else "Active 13D"


def _normalize_sec_date_str(raw_dt: str) -> str:
    txt = re.sub(r"\s+", " ", (raw_dt or "").strip())
    if not txt:
        return ""
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", txt):
        return txt
    m_mdy = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", txt)
    if m_mdy:
        return f"{m_mdy.group(3)}-{int(m_mdy.group(1)):02d}-{int(m_mdy.group(2)):02d}"
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%b %d %Y"):
        try:
            return datetime.strptime(txt, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return ""


def _parse_13dg_primary_xml(raw_xml: bytes, is_activist: bool = False) -> dict[str, Any]:
    """
    Parse SEC Schedule 13D/13G XML filing to extract ownership % (classPercent),
    beneficial shares owned, subject CUSIP, event date, and Item 4 activist intent.
    """
    out: dict[str, Any] = {
        "percentOfClass": None,
        "sharesOwned": None,
        "subjectCusip": "",
        "eventDate": "",
        "item4Intent": "",
        "item4Category": "Passive (13G)" if not is_activist else "Active 13D",
    }
    try:
        root = _parse_xml_root(raw_xml)
    except Exception:
        return out
    if root is None:
        return out

    pct_vals: list[float] = []
    sh_vals: list[float] = []
    purpose_candidates: list[str] = []
    item4_wrapper_candidates: list[str] = []
    detected_activist = is_activist

    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        lname = el.tag.rsplit("}", 1)[-1]
        lname_low = lname.lower()
        txt = el.text.strip() if el.text else ""

        if lname_low in ("submissiontype", "formtype") and "13D" in txt.upper():
            detected_activist = True

        if lname_low in ("transactionpurpose", "purposeoftransaction", "item4purpose", "item4text"):
            full_t = " ".join(s.strip() for s in el.itertext() if s and s.strip())
            cleaned_t = _clean_text_excerpt(full_t, max_len=340)
            if len(cleaned_t) >= 8:
                purpose_candidates.append(cleaned_t)
            continue

        if lname_low == "item4":
            full_t = " ".join(s.strip() for s in el.itertext() if s and s.strip())
            cleaned_t = _clean_text_excerpt(full_t, max_len=340)
            if len(cleaned_t) >= 8:
                item4_wrapper_candidates.append(cleaned_t)
            continue

        if not txt:
            continue

        if lname in ("classPercent", "percentOfClass", "aggregatePercentOfClass", "percentOwned"):
            cleaned = re.sub(r"[^0-9.-]", "", txt)
            try:
                pv = float(cleaned)
                if 0.0 <= pv <= 100.0:
                    pct_vals.append(round(pv, 2))
            except ValueError:
                pass
        elif lname in (
            "reportingPersonBeneficiallyOwnedAggregateNumberOfShares",
            "aggregateAmountOwned",
            "aggregateNumberOfShares",
            "aggregateAmountBeneficiallyOwnedByEachReportingPerson",
            "sharesOwned",
        ):
            cleaned = re.sub(r"[^0-9.]", "", txt.replace(",", ""))
            try:
                sv = float(cleaned)
                if sv >= 0:
                    sh_vals.append(round(sv))
            except ValueError:
                pass
        elif lname in ("issuerCusipNumber", "cusipNo", "cusipNumber") and not out["subjectCusip"]:
            c_clean = re.sub(r"[^A-Za-z0-9]", "", txt).upper()[:9]
            if len(c_clean) >= 6:
                out["subjectCusip"] = c_clean.zfill(9)
        elif lname in ("eventDateRequiresFilingThisStatement", "dateOfEvent", "eventDate") and not out["eventDate"]:
            nd = _normalize_sec_date_str(txt)
            if nd:
                out["eventDate"] = nd

    if pct_vals:
        out["percentOfClass"] = max(pct_vals)
    if sh_vals:
        out["sharesOwned"] = max(sh_vals)

    best_item4 = (
        max(purpose_candidates, key=len)
        if purpose_candidates
        else (max(item4_wrapper_candidates, key=len) if item4_wrapper_candidates else "")
    )
    out["item4Intent"] = best_item4
    out["item4Category"] = _classify_item4_intent(best_item4, detected_activist)
    return out


def _parse_13dg_html(raw_html: bytes, is_activist: bool = False) -> dict[str, Any]:
    """
    Parse HTML/text Schedule 13D / 13G primary documents as fallback for cover-page
    share counts, ownership %, CUSIP, event date, and Schedule 13D Item 4 purpose text.
    """
    out: dict[str, Any] = {
        "percentOfClass": None,
        "sharesOwned": None,
        "subjectCusip": "",
        "eventDate": "",
        "item4Intent": "",
        "item4Category": "Passive (13G)" if not is_activist else "Active 13D",
    }
    try:
        raw_txt = raw_html.decode("utf-8", errors="replace")
    except Exception:
        return out
    if not raw_txt:
        return out

    stripped = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw_txt, flags=re.S | re.I)
    stripped = re.sub(r"<(?:/?(?:p|div|tr|li|h[1-6]|section)|br\s*/?)[^>]*>", "\n", stripped, flags=re.I)
    stripped = re.sub(r"<[^>]+>", " ", stripped)
    plain = html_lib.unescape(stripped)
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in plain.splitlines()]
    text_body = "\n".join(ln for ln in lines if ln)
    if not text_body:
        return out

    # Cover-page CUSIP
    m_cusip = re.search(r"\bCUSIP\s*(?:No\.?|Number|#)?[:\s]*([A-Z0-9]{6,9})\b", text_body, flags=re.I)
    if m_cusip:
        c_raw = re.sub(r"[^A-Za-z0-9]", "", m_cusip.group(1)).upper()[:9]
        if len(c_raw) >= 6 and any(ch.isdigit() for ch in c_raw):
            out["subjectCusip"] = c_raw.zfill(9)

    # Event Date
    m_ev = re.search(
        r"Date\s+of\s+Event[\s\S]{0,80}?"
        r"((?:January|February|March|April|May|June|July|August|September|October|November|December|"
        r"Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z.]*\s+\d{1,2},?\s+\d{4}|\d{1,2}/\d{1,2}/\d{4}|\d{4}-\d{2}-\d{2})",
        text_body[:8000],
        flags=re.I,
    )
    if m_ev:
        nd = _normalize_sec_date_str(m_ev.group(1).replace(",", ", ").replace("  ", " ").replace(".", ""))
        if not nd:
            nd = _normalize_sec_date_str(m_ev.group(1))
        if nd:
            out["eventDate"] = nd

    # Shares & ownership % from cover page rows (9)/(11)
    sh_matches = re.findall(
        r"AGGREGATE\s+AMOUNT\s+BENEFICIALLY\s+OWNED\s+BY\s+EACH\s+REPORTING\s+PERSON"
        r"[^0-9A-Za-z\n]{0,80}(?:[A-Z\s]{0,20}\n)?([0-9][0-9,]{2,14})",
        text_body[:35000],
        flags=re.I,
    )
    sh_vals = []
    for raw_sh in sh_matches:
        try:
            sv = float(raw_sh.replace(",", ""))
            if sv >= 0:
                sh_vals.append(round(sv))
        except ValueError:
            pass
    if sh_vals:
        out["sharesOwned"] = max(sh_vals)

    pct_matches = re.findall(
        r"PERCENT\s+OF\s+CLASS\s+REPRESENTED\s+BY\s+AMOUNT\s+IN\s+ROW\s*(?:\(\s*(?:9|11)\s*\)|9|11)"
        r"[^0-9%\n]{0,80}(\d{1,2}(?:\.\d{1,2})?)\s*%",
        text_body[:35000],
        flags=re.I,
    )
    pct_vals = []
    for raw_pct in pct_matches:
        try:
            pv = float(raw_pct)
            if 0.0 <= pv <= 100.0:
                pct_vals.append(round(pv, 2))
        except ValueError:
            pass
    if pct_vals:
        out["percentOfClass"] = max(pct_vals)

    if is_activist:
        item4_sections: list[str] = []
        end_re = re.compile(r"(?:^|\n)\s*(?:Item\s*5\.?\b|SIGNATURE(?:S)?\b)", flags=re.I)
        for m4 in re.finditer(
            r"(?:^|\n)\s*Item\s*4\.?\s*(?:[-–—:\s]*Purpose\s+of\s+(?:the\s+)?Transaction\.?)?",
            text_body,
            flags=re.I,
        ):
            start_pos = m4.end()
            m_end = end_re.search(text_body, start_pos)
            end_pos = m_end.start() if m_end else min(len(text_body), start_pos + 3200)
            chunk = text_body[start_pos:end_pos].strip()
            # Skip short Table-of-Contents lines such as "... 6"
            if len(chunk) >= 45:
                item4_sections.append(chunk)
        if item4_sections:
            # Prefer substantive narrative block
            best_raw = max(item4_sections, key=lambda s: len(s[:2000]))
            excerpt = _clean_text_excerpt(best_raw, max_len=340)
            out["item4Intent"] = excerpt
            out["item4Category"] = _classify_item4_intent(excerpt, True)

    return out


def fetch_midquarter_alerts(
    elite_ciks: list[str],
    fund_names_by_cik: dict[str, str],
    cik_to_ticker_map: dict[str, str],
    name_to_ticker_map: dict[str, str],
    today_date: date,
    since_days: int = 95,
    cusip_to_ticker_map: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """
    Scan recent SEC submissions for Elite-50 funds for live mid-quarter Schedule 13D / 13G
    filings reported within 5–10 days vs 45-day 13F lag.
    Extracts subject company from .hdr.sgml AND beneficial ownership % / shares / CUSIP /
    Item 4 activist intent from structured XML (with index.json & HTML fallback).
    """
    if not elite_ciks:
        return []

    cutoff_str = (today_date - timedelta(days=since_days)).strftime("%Y-%m-%d")
    hdr_cache = load_json_dict(MIDQUARTER_HDR_CACHE_PATH)
    hdr_dirty = False

    candidate_filings: list[dict[str, Any]] = []
    for cik10 in elite_ciks:
        try:
            sub = get_submissions(cik10)
        except Exception:
            continue
        recent = (sub.get("filings") or {}).get("recent") or {}
        forms = recent.get("form") or []
        accs = recent.get("accessionNumber") or []
        fds = recent.get("filingDate") or []
        pdocs = recent.get("primaryDocument") or [""] * len(forms)
        for form, acc, fdate, pdoc in zip(forms, accs, fds, pdocs):
            if not form or not acc or not fdate or fdate < cutoff_str:
                continue
            m = _13DG_FORM_RE.match(str(form).strip())
            if not m:
                continue
            candidate_filings.append({
                "filerCik": cik10,
                "fundName": fund_names_by_cik.get(cik10) or sub.get("name") or cik10,
                "form": str(form).strip(),
                "isActivist": m.group(1).upper() == "D",
                "filingDate": str(fdate).strip(),
                "accessionNumber": str(acc).strip(),
                "primaryDoc": str(pdoc or "").strip(),
            })

    if not candidate_filings:
        return []

    need_fetch = [
        it for it in candidate_filings
        if it["accessionNumber"] not in hdr_cache
        or (it.get("primaryDoc") and not (hdr_cache.get(it["accessionNumber"]) or {}).get("stakeChecked"))
        or (hdr_cache.get(it["accessionNumber"]) or {}).get("_item4Ver") != ITEM4_PARSER_VERSION
    ]
    if need_fetch:
        print(
            f"[alerts-13dg] Resolving subject companies, stakes & Item 4 activist intent for "
            f"{len(need_fetch)} mid-quarter Schedule 13D/13G filings...",
            flush=True,
        )

        def _fetch_hdr_and_xml(item: dict[str, Any]) -> tuple[str, dict[str, Any]]:
            cik10 = item["filerCik"]
            acc = item["accessionNumber"]
            acc_nd = acc.replace("-", "")
            existing = dict(hdr_cache.get(acc) or {})
            is_act = bool(item.get("isActivist"))
            info: dict[str, Any] = {
                "subjectName": existing.get("subjectName", ""),
                "subjectCik": existing.get("subjectCik", ""),
                "percentOfClass": existing.get("percentOfClass"),
                "sharesOwned": existing.get("sharesOwned"),
                "subjectCusip": existing.get("subjectCusip", ""),
                "eventDate": existing.get("eventDate", ""),
                "item4Intent": existing.get("item4Intent", ""),
                "item4Category": existing.get("item4Category", "Passive (13G)" if not is_act else "Active 13D"),
                "primaryDoc": item.get("primaryDoc") or existing.get("primaryDoc", ""),
                "stakeChecked": True,
                "_item4Ver": ITEM4_PARSER_VERSION,
            }
            if not info["subjectCik"] and not info["subjectName"]:
                url = f"https://www.sec.gov/Archives/edgar/data/{int(cik10)}/{acc_nd}/{acc}.hdr.sgml"
                try:
                    raw = fetch_bytes(url, cache_key=f"hdr_{cik10}_{acc_nd}.sgml", max_age_hours=24 * 365).decode("utf-8", errors="replace")
                    m_sub = re.search(
                        r"<SUBJECT-COMPANY>.*?<CONFORMED-NAME>([^\r\n<]+).*?<CIK>(\d+)",
                        raw,
                        flags=re.S | re.I,
                    )
                    if m_sub:
                        info["subjectName"] = m_sub.group(1).strip()
                        info["subjectCik"] = f"{int(m_sub.group(2)):010d}"
                except Exception:
                    pass

            # Strip any SEC XSLT path prefix (e.g. xslSCHEDULE_13D_X01/primary_doc.xml -> primary_doc.xml)
            pdoc_raw = str(item.get("primaryDoc") or "").strip()
            pdoc = pdoc_raw.split("/")[-1].strip()
            xml_file: str | None = pdoc if pdoc.lower().endswith(".xml") else None

            if xml_file is None:
                # Inspect index.json safely to find structured primary_doc.xml alongside .htm filings
                try:
                    idx_url = f"https://www.sec.gov/Archives/edgar/data/{int(cik10)}/{acc_nd}/index.json"
                    idx_json = json.loads(
                        fetch_bytes(idx_url, cache_key=f"idx13dg_{cik10}_{acc_nd}.json", max_age_hours=24 * 365).decode("utf-8", errors="replace")
                    )
                    items_list = (idx_json.get("directory") or {}).get("item") or []
                    for f_obj in items_list:
                        fn = str(f_obj.get("name") or "")
                        fn_l = fn.lower()
                        if (
                            fn_l.endswith(".xml")
                            and not fn_l.startswith("xsl")
                            and fn_l != "filingsummary.xml"
                            and not re.fullmatch(r"r\d+\.xml", fn_l)
                        ):
                            xml_file = fn
                            break
                except Exception:
                    pass

            if xml_file:
                xml_url = f"https://www.sec.gov/Archives/edgar/data/{int(cik10)}/{acc_nd}/{xml_file}"
                try:
                    raw_xml = fetch_bytes(xml_url, cache_key=f"xml13dg_{cik10}_{acc_nd}.xml", max_age_hours=24 * 365)
                    parsed_xml = _parse_13dg_primary_xml(raw_xml, is_activist=is_act)
                    if parsed_xml["percentOfClass"] is not None:
                        info["percentOfClass"] = parsed_xml["percentOfClass"]
                    if parsed_xml["sharesOwned"] is not None:
                        info["sharesOwned"] = parsed_xml["sharesOwned"]
                    if parsed_xml["subjectCusip"]:
                        info["subjectCusip"] = parsed_xml["subjectCusip"]
                    if parsed_xml["eventDate"]:
                        info["eventDate"] = parsed_xml["eventDate"]
                    if parsed_xml["item4Intent"]:
                        info["item4Intent"] = parsed_xml["item4Intent"]
                    if parsed_xml["item4Category"]:
                        info["item4Category"] = parsed_xml["item4Category"]
                except Exception:
                    pass

            # HTML fallback when primary document is .htm/.html and either ownership or Item 4 is missing
            if pdoc.lower().endswith((".htm", ".html")) and (
                info["percentOfClass"] is None
                or info["sharesOwned"] is None
                or (is_act and not info["item4Intent"])
            ):
                html_url = f"https://www.sec.gov/Archives/edgar/data/{int(cik10)}/{acc_nd}/{pdoc}"
                try:
                    raw_html = fetch_bytes(html_url, cache_key=f"doc13dg_{cik10}_{acc_nd}.htm", max_age_hours=24 * 365)
                    parsed_h = _parse_13dg_html(raw_html, is_activist=is_act)
                    if info["percentOfClass"] is None and parsed_h["percentOfClass"] is not None:
                        info["percentOfClass"] = parsed_h["percentOfClass"]
                    if info["sharesOwned"] is None and parsed_h["sharesOwned"] is not None:
                        info["sharesOwned"] = parsed_h["sharesOwned"]
                    if not info["subjectCusip"] and parsed_h["subjectCusip"]:
                        info["subjectCusip"] = parsed_h["subjectCusip"]
                    if not info["eventDate"] and parsed_h["eventDate"]:
                        info["eventDate"] = parsed_h["eventDate"]
                    if is_act and not info["item4Intent"] and parsed_h["item4Intent"]:
                        info["item4Intent"] = parsed_h["item4Intent"]
                        info["item4Category"] = parsed_h["item4Category"]
                except Exception:
                    pass

            return acc, info

        with ThreadPoolExecutor(max_workers=6) as ex:
            for acc, info in ex.map(_fetch_hdr_and_xml, need_fetch):
                hdr_cache[acc] = info
                hdr_dirty = True

    if hdr_dirty:
        try:
            MIDQUARTER_HDR_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            save_json_dict(MIDQUARTER_HDR_CACHE_PATH, hdr_cache)
        except Exception:
            pass

    sub_ticker_cache: dict[str, str] = {}
    alerts: list[dict[str, Any]] = []
    for it in candidate_filings:
        acc = it["accessionNumber"]
        hinfo = hdr_cache.get(acc, {})
        sub_cik = str(hinfo.get("subjectCik") or "")
        sub_name = str(hinfo.get("subjectName") or "")
        sub_cusip = str(hinfo.get("subjectCusip") or "")
        pct_cls = hinfo.get("percentOfClass")
        sh_owned = hinfo.get("sharesOwned")
        ev_date = str(hinfo.get("eventDate") or "")
        item4_intent = str(hinfo.get("item4Intent") or "")
        item4_cat = str(
            hinfo.get("item4Category")
            or ("Passive (13G)" if not it["isActivist"] else "Active 13D")
        )
        raw_pdoc = str(it.get("primaryDoc") or hinfo.get("primaryDoc") or "").strip()

        ticker = (
            cik_to_ticker_map.get(sub_cik)
            or (cusip_to_ticker_map.get(sub_cusip) if (cusip_to_ticker_map and sub_cusip) else "")
            or name_to_ticker_map.get(_normalize_legal_name(sub_name))
            or ""
        )
        if not ticker and sub_cik:
            if sub_cik not in sub_ticker_cache:
                fallback_t = ""
                try:
                    sub_json = get_submissions(sub_cik)
                    for raw_t in sub_json.get("tickers") or []:
                        nt = normalize_yf_ticker(str(raw_t or ""))
                        if nt and _US_VALID_TICKER_RE.fullmatch(nt):
                            fallback_t = nt
                            break
                except Exception:
                    pass
                sub_ticker_cache[sub_cik] = fallback_t
            ticker = sub_ticker_cache[sub_cik]
        if not ticker and not sub_name:
            continue

        if pct_cls is not None:
            if float(pct_cls) <= 0.05:
                stake_status = "Exited (0%)"
            elif float(pct_cls) < 5.0:
                stake_status = "Reduced (<5%)"
            else:
                stake_status = "Active (≥5%)"
        else:
            stake_status = "Filed"

        acc_nd = acc.replace("-", "")
        base_url = f"https://www.sec.gov/Archives/edgar/data/{int(it['filerCik'])}/{acc_nd}/"
        alerts.append({
            "id": f"{it['filerCik']}_{acc}",
            "filerCik": it["filerCik"],
            "fundName": it["fundName"],
            "form": it["form"],
            "isActivist": it["isActivist"],
            "filingDate": it["filingDate"],
            "accessionNumber": acc,
            "subjectCik": sub_cik,
            "ticker": ticker,
            "issuerName": sub_name,
            "percentOfClass": pct_cls,
            "sharesOwned": sh_owned,
            "stakeStatus": stake_status,
            "eventDate": ev_date or None,
            "item4Category": item4_cat,
            "item4Intent": item4_intent or None,
            "filingDocUrl": f"{base_url}{raw_pdoc.lstrip('/')}" if raw_pdoc else None,
            "edgarUrl": base_url,
        })

    alerts.sort(key=lambda a: (a["filingDate"], a["isActivist"], a["fundName"]), reverse=True)
    return alerts


def _xml_flag_true(val: str | None) -> bool:
    return (val or "").strip().lower() in ("1", "true", "yes", "y")


def _format_insider_name(raw_name: str) -> str:
    txt = re.sub(r"\s+", " ", (raw_name or "").strip())
    if not txt:
        return "Insider"
    if "," in txt:
        parts = [p.strip() for p in txt.split(",") if p.strip()]
        if len(parts) >= 2:
            txt = f"{parts[1]} {parts[0]}" + (f" {' '.join(parts[2:])}" if len(parts) > 2 else "")
    if txt.isupper() or txt.islower():
        words = txt.split()
        out_w = []
        for w in words:
            uw = w.upper().rstrip(".")
            if uw in ("II", "III", "IV", "MD", "PHD", "LP", "LLC"):
                out_w.append(uw)
            elif uw in ("JR", "SR"):
                out_w.append(f"{uw.title()}.")
            else:
                out_w.append(w.title())
        return " ".join(out_w)
    return txt


def _is_csuite_title(title: str) -> bool:
    if not title:
        return False
    # Strip plain/assistant Vice President before checking PRESIDENT unless Senior/Executive VP
    cleaned = re.sub(
        r"\b(?:SENIOR|EXEC(?:UTIVE)?)\s+(?:VICE\s+PRESIDENT|VP)\b",
        "SVP",
        title,
        flags=re.I,
    )
    cleaned = _NON_CSUITE_VP_RE.sub("", cleaned)
    return bool(_CSUITE_TITLE_RE.search(cleaned))


def _footnote_confirms_10b5(fn_text: str) -> bool:
    if not fn_text or "10b5-1" not in fn_text.lower():
        return False
    return not bool(_NOT_10B5_RE.search(fn_text))


def _parse_form4_xml(
    raw_xml: bytes,
    cik10: str,
    acc: str,
    filing_date: str,
    pdoc: str = "",
    form_type: str = "4",
) -> dict[str, Any] | None:
    """
    Parse SEC Form 4 / 4/A ownershipDocument XML.
    Strictly retains open-market purchases (`transactionCode == 'P'` with `acquiredDisposedCode != 'D'`)
    and separately tracks open-market sales (`'S'`) to guard against sell-heavy false positives.
    Excludes `'M'` (option exercises), `'A'` (grants), `'F'` (tax withholding), `'G'` (gifts), etc.
    """
    try:
        root = _parse_xml_root(raw_xml)
    except Exception:
        return None
    if root is None:
        return None

    def _local_text(el: Any, tag: str) -> str:
        found = el.xpath(f"./*[local-name()='{tag}']")
        if found and found[0].text:
            return found[0].text.strip()
        return ""

    def _deep_text(el: Any, path_tags: tuple[str, ...]) -> str:
        cur = el
        for tg in path_tags:
            nxt = cur.xpath(f"./*[local-name()='{tg}']")
            if not nxt:
                return ""
            cur = nxt[0]
        if cur.text and cur.text.strip():
            return cur.text.strip()
        v = cur.xpath("./*[local-name()='value']")
        if v and v[0].text:
            return v[0].text.strip()
        return ""

    doc_type = (_local_text(root, "documentType") or form_type or "4").strip().upper()
    if not doc_type.startswith("4"):
        return None

    is_amendment = doc_type.endswith("/A") or form_type.strip().upper().endswith("/A")
    doc_10b5 = _xml_flag_true(_local_text(root, "aff10b5One"))

    issuer_el_l = root.xpath("./*[local-name()='issuer']")
    issuer_cik = _local_text(issuer_el_l[0], "issuerCik") if issuer_el_l else ""
    issuer_sym = _local_text(issuer_el_l[0], "issuerTradingSymbol") if issuer_el_l else ""

    footnotes: dict[str, str] = {}
    for fn in root.xpath(".//*[local-name()='footnote']"):
        fid = (fn.attrib.get("id") or "").strip()
        ftxt = " ".join(s.strip() for s in fn.itertext() if s and s.strip())
        if fid and ftxt:
            footnotes[fid] = ftxt

    any_fn_10b5 = any(_footnote_confirms_10b5(ftxt) for ftxt in footnotes.values())

    # Parse reportingOwner(s) prioritizing Officer/C-Suite > Director > 10% Owner
    owners: list[dict[str, Any]] = []
    for ro in root.xpath("./*[local-name()='reportingOwner']"):
        rid_l = ro.xpath("./*[local-name()='reportingOwnerId']")
        rrel_l = ro.xpath("./*[local-name()='reportingOwnerRelationship']")
        r_cik = _local_text(rid_l[0], "rptOwnerCik") if rid_l else ""
        r_name_raw = _local_text(rid_l[0], "rptOwnerName") if rid_l else ""
        is_dir = _xml_flag_true(_local_text(rrel_l[0], "isDirector")) if rrel_l else False
        is_off = _xml_flag_true(_local_text(rrel_l[0], "isOfficer")) if rrel_l else False
        is_ten = _xml_flag_true(_local_text(rrel_l[0], "isTenPercentOwner")) if rrel_l else False
        off_title = _local_text(rrel_l[0], "officerTitle") if rrel_l else ""
        is_cs = is_off and _is_csuite_title(off_title)

        if is_cs and off_title:
            role_lbl = off_title.strip()
        elif is_off:
            role_lbl = off_title.strip() or "Officer"
        elif is_dir:
            role_lbl = "Director"
        elif is_ten:
            role_lbl = "10% Owner"
        else:
            other_t = _local_text(rrel_l[0], "otherText") if rrel_l else ""
            role_lbl = other_t.strip() or "Insider"

        owners.append({
            "cik": f"{int(r_cik):010d}" if r_cik.isdigit() else "",
            "name": _format_insider_name(r_name_raw),
            "role": role_lbl[:60],
            "isOfficer": is_off,
            "isDirector": is_dir,
            "isTenPercentOwner": is_ten,
            "isCsuite": is_cs,
        })

    if owners:
        owners.sort(key=lambda o: (o["isCsuite"], o["isOfficer"], o["isDirector"], o["isTenPercentOwner"]), reverse=True)
        primary_owner = owners[0]
        any_officer = any(o["isOfficer"] for o in owners)
        any_director = any(o["isDirector"] for o in owners)
        any_ten = any(o["isTenPercentOwner"] for o in owners)
        any_csuite = any(o["isCsuite"] for o in owners)
        rep_key = primary_owner["cik"] or primary_owner["name"].upper()
        rep_name = primary_owner["name"]
        rep_role = primary_owner["role"]
    else:
        any_officer = False
        any_director = False
        any_ten = False
        any_csuite = False
        rep_key = "UNKNOWN"
        rep_name = "Insider"
        rep_role = "Insider"

    acc_nd = acc.replace("-", "")
    clean_pdoc = (pdoc or "").strip().lstrip("/")
    edgar_url = (
        f"https://www.sec.gov/Archives/edgar/data/{int(cik10)}/{acc_nd}/{clean_pdoc}"
        if clean_pdoc
        else f"https://www.sec.gov/Archives/edgar/data/{int(cik10)}/{acc_nd}/"
    )

    buys: list[dict[str, Any]] = []
    disc_sell_usd = 0.0
    sched_sell_usd = 0.0

    for tx in root.xpath(".//*[local-name()='nonDerivativeTable']/*[local-name()='nonDerivativeTransaction']"):
        code = _deep_text(tx, ("transactionCoding", "transactionCode")).upper()
        if code not in ("P", "S"):
            continue
        tx_date = _deep_text(tx, ("transactionDate", "value")) or _deep_text(tx, ("transactionDate",)) or filing_date
        acq_disp = _deep_text(tx, ("transactionAmounts", "transactionAcquiredDisposedCode")).upper()
        sh_str = _deep_text(tx, ("transactionAmounts", "transactionShares"))
        px_str = _deep_text(tx, ("transactionAmounts", "transactionPricePerShare"))
        try:
            sh_val = float(sh_str.replace(",", ""))
            px_val = float(px_str.replace(",", ""))
        except ValueError:
            continue
        if not (math.isfinite(sh_val) and math.isfinite(px_val) and sh_val > 0 and px_val > 0):
            continue

        tx_fn_ids = [
            (fn_ref.attrib.get("id") or "").strip()
            for fn_ref in tx.xpath(".//*[local-name()='footnoteId']")
        ]
        tx_fn_10b5 = any(_footnote_confirms_10b5(footnotes.get(fid, "")) for fid in tx_fn_ids)
        is_10b5 = bool(doc_10b5 or tx_fn_10b5 or any_fn_10b5)
        val_usd = round(sh_val * px_val, 2)

        if code == "P":
            if acq_disp == "D":
                continue
            buys.append({
                "accessionNumber": acc,
                "filingDate": filing_date,
                "transactionDate": tx_date[:10],
                "reporterKey": rep_key,
                "insiderName": rep_name,
                "insiderRole": rep_role,
                "isOfficer": any_officer,
                "isDirector": any_director,
                "isTenPercentOwner": any_ten,
                "isCsuite": any_csuite,
                "isAmendment": is_amendment,
                "shares": round(sh_val, 2),
                "pricePerShare": round(px_val, 4),
                "valueUsd": val_usd,
                "is10b5Plan": is_10b5,
                "edgarUrl": edgar_url,
            })
        elif code == "S":
            if acq_disp == "A":
                continue
            if is_10b5:
                sched_sell_usd += val_usd
            else:
                disc_sell_usd += val_usd

    return {
        "accessionNumber": acc,
        "form": doc_type,
        "isAmendment": is_amendment,
        "filingDate": filing_date,
        "issuerCik": issuer_cik,
        "issuerSymbol": issuer_sym.upper(),
        "reporterKey": rep_key,
        "reporterName": rep_name,
        "reporterRole": rep_role,
        "isOfficer": any_officer,
        "isDirector": any_director,
        "isTenPercentOwner": any_ten,
        "isCsuite": any_csuite,
        "buys": buys,
        "discretionarySellValueUsd": round(disc_sell_usd, 2),
        "scheduledSellValueUsd": round(sched_sell_usd, 2),
        "edgarUrl": edgar_url,
    }


def summarize_form4_transactions_for_ticker(
    ticker: str,
    issuer_cik: str,
    parsed_docs: list[dict[str, Any]],
    lookback_days: int = 90,
) -> dict[str, Any]:
    """
    Aggregate parsed Form 4 / 4/A filings into a clean insider cluster summary for a ticker.
    Deduplicates `4` filings superseded by matching `4/A` amendments while preserving separate lots
    inside a single Form 4 filing.
    """
    ordered_docs = sorted(
        parsed_docs,
        key=lambda d: (d.get("filingDate", ""), 1 if d.get("isAmendment") else 0, d.get("accessionNumber", "")),
    )

    # Deduplicate buys by (reporterKey, txDate, round(shares, 1), round(price, 2), per_filing_ordinal)
    dedup_buys: dict[tuple[str, str, float, float, int], dict[str, Any]] = {}
    disc_sell_by_doc: dict[str, float] = {}
    sched_sell_by_doc: dict[str, float] = {}

    for doc in ordered_docs:
        acc = str(doc.get("accessionNumber") or "")
        disc_sell_by_doc[acc] = float(doc.get("discretionarySellValueUsd") or 0.0)
        sched_sell_by_doc[acc] = float(doc.get("scheduledSellValueUsd") or 0.0)
        in_doc_counts: dict[tuple[str, str, float, float], int] = {}
        for tx in doc.get("buys") or []:
            rkey = str(tx.get("reporterKey") or doc.get("reporterKey") or "UNKNOWN")
            txd = str(tx.get("transactionDate") or doc.get("filingDate") or "")
            sh_r = round(float(tx.get("shares") or 0.0), 1)
            px_r = round(float(tx.get("pricePerShare") or 0.0), 2)
            base_k = (rkey, txd, sh_r, px_r)
            ord_idx = in_doc_counts.get(base_k, 0)
            in_doc_counts[base_k] = ord_idx + 1
            full_k = (*base_k, ord_idx)
            prev_tx = dedup_buys.get(full_k)
            if prev_tx is None or tx.get("isAmendment") or doc.get("isAmendment"):
                dedup_buys[full_k] = tx

    buys = list(dedup_buys.values())
    disc_sell_usd = round(sum(disc_sell_by_doc.values()), 2)
    sched_sell_usd = round(sum(sched_sell_by_doc.values()), 2)
    total_sell_usd = round(disc_sell_usd + sched_sell_usd, 2)

    distinct_buyers: set[str] = set()
    officer_buyers: set[str] = set()
    csuite_buyers: set[str] = set()
    director_buyers: set[str] = set()
    tenpct_buyers: set[str] = set()
    total_buy_val = 0.0
    total_buy_sh = 0.0
    non10b5_count = 0
    non10b5_val = 0.0
    latest_buy_date: str | None = None
    latest_filing_date: str | None = None

    for tx in buys:
        rk = str(tx.get("reporterKey") or tx.get("insiderName") or "UNKNOWN")
        distinct_buyers.add(rk)
        if tx.get("isOfficer"):
            officer_buyers.add(rk)
        if tx.get("isCsuite"):
            csuite_buyers.add(rk)
        if tx.get("isDirector"):
            director_buyers.add(rk)
        if tx.get("isTenPercentOwner"):
            tenpct_buyers.add(rk)
        val = float(tx.get("valueUsd") or 0.0)
        sh = float(tx.get("shares") or 0.0)
        total_buy_val += val
        total_buy_sh += sh
        if not tx.get("is10b5Plan"):
            non10b5_count += 1
            non10b5_val += val
        td = tx.get("transactionDate")
        fd = tx.get("filingDate")
        if td and (latest_buy_date is None or td > latest_buy_date):
            latest_buy_date = td
        if fd and (latest_filing_date is None or fd > latest_filing_date):
            latest_filing_date = fd

    open_mkt_cnt = len(buys)
    d_cnt = len(distinct_buyers)
    o_cnt = len(officer_buyers)
    c_cnt = len(csuite_buyers)
    dir_cnt = len(director_buyers)
    ten_cnt = len(tenpct_buyers)

    avg_px = round(total_buy_val / total_buy_sh, 2) if total_buy_sh > 0 else None

    if open_mkt_cnt == 0:
        sig_label = "NONE"
    elif disc_sell_usd > total_buy_val * 3.0 and total_buy_val < 500_000.0:
        sig_label = "MIXED_SELL_HEAVY"
    elif d_cnt >= 2 and (o_cnt >= 1 or total_buy_val >= 100_000.0):
        sig_label = "CLUSTER_BUY"
    elif c_cnt >= 1 and total_buy_val >= 100_000.0:
        sig_label = "C_SUITE_BUY"
    elif dir_cnt >= 1 and total_buy_val >= 50_000.0:
        sig_label = "DIRECTOR_BUY"
    else:
        sig_label = "SINGLE_BUY"

    cluster_detected = bool(
        open_mkt_cnt > 0
        and sig_label != "MIXED_SELL_HEAVY"
        and (
            (d_cnt >= 2 and (o_cnt >= 1 or total_buy_val >= 100_000.0))
            or (c_cnt >= 1 and total_buy_val >= 250_000.0)
        )
    )

    buys_sorted = sorted(
        buys,
        key=lambda x: (x.get("transactionDate", ""), float(x.get("valueUsd") or 0.0)),
        reverse=True,
    )
    clean_recent = [
        {
            "accessionNumber": b["accessionNumber"],
            "filingDate": b["filingDate"],
            "transactionDate": b["transactionDate"],
            "insiderName": b["insiderName"],
            "insiderRole": b["insiderRole"],
            "isOfficer": bool(b.get("isOfficer")),
            "isDirector": bool(b.get("isDirector")),
            "isCsuite": bool(b.get("isCsuite")),
            "isTenPercentOwner": bool(b.get("isTenPercentOwner")),
            "shares": round(float(b["shares"]), 0),
            "pricePerShare": round(float(b["pricePerShare"]), 2),
            "valueUsd": round(float(b["valueUsd"]), 2),
            "is10b5Plan": bool(b.get("is10b5Plan")),
            "edgarUrl": b["edgarUrl"],
        }
        for b in buys_sorted[:12]
    ]

    return {
        "ticker": ticker,
        "issuerCik": issuer_cik,
        "lookbackDays": lookback_days,
        "openMarketBuyCount": open_mkt_cnt,
        "distinctBuyersCount": d_cnt,
        "officerBuyersCount": o_cnt,
        "cSuiteBuyersCount": c_cnt,
        "directorBuyersCount": dir_cnt,
        "tenPctOwnerBuyersCount": ten_cnt,
        "totalBuyValueUsd": round(total_buy_val, 2),
        "totalBuyShares": round(total_buy_sh, 0),
        "avgBuyPriceUsd": avg_px,
        "buyPriceVsQuarterVwapPct": None,
        "latestBuyDate": latest_buy_date,
        "latestFilingDate": latest_filing_date,
        "non10b5Count": non10b5_count,
        "non10b5BuyValueUsd": round(non10b5_val, 2),
        "discretionarySellValueUsd": disc_sell_usd,
        "totalSellValueUsd": total_sell_usd,
        "insiderSignalLabel": sig_label,
        "clusterDetected": cluster_detected,
        "recentTransactions": clean_recent,
    }


def fetch_form4_insider_signals(
    tickers: list[str],
    ticker_to_cik: dict[str, str],
    today_date: date,
    lookback_days: int = 90,
    max_age_hours: float = 24.0,
    max_filings_per_ticker: int = 30,
) -> dict[str, dict[str, Any]]:
    """
    Fetch and parse recent SEC EDGAR Form 4 / 4/A open-market ('P') insider cluster buys
    across prioritized Ideal Portfolio, Signal Screener, Consensus, and 13D/13G tickers.
    Caches parsed Form 4 accessions in `pipeline/cache/sec/` across runs so repeated executions
    only fetch newly filed Form 4s.
    """
    if not tickers:
        return {}

    cutoff_str = (today_date - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    now_ts = time.time()
    ticker_cache = load_json_dict(FORM4_TICKER_CACHE_PATH)
    parsed_cache = load_json_dict(FORM4_PARSED_CACHE_PATH)
    parsed_dirty = False
    ticker_dirty = False

    target_pairs: list[tuple[str, str]] = []
    out: dict[str, dict[str, Any]] = {}

    for t in tickers:
        cik10 = ticker_to_cik.get(t)
        if not cik10:
            continue
        cached = ticker_cache.get(t)
        if (
            isinstance(cached, dict)
            and cached.get("_ver") == FORM4_PARSER_VERSION
            and cached.get("_cutoff") == cutoff_str
            and (now_ts - float(cached.get("_ts", 0))) / 3600.0 <= max_age_hours
        ):
            payload = {k: v for k, v in cached.items() if not k.startswith("_")}
            if payload.get("openMarketBuyCount", 0) > 0 or payload.get("discretionarySellValueUsd", 0) > 0:
                out[t] = payload
            continue
        target_pairs.append((t, cik10))

    if target_pairs:
        print(
            f"[form4] Scanning SEC Form 4 open-market ('P') insider filings across {len(target_pairs)} priority tickers...",
            flush=True,
        )

        def _process_ticker(pair: tuple[str, str]) -> tuple[str, dict[str, Any] | None, list[tuple[str, dict[str, Any]]]]:
            t, cik10 = pair
            new_parsed: list[tuple[str, dict[str, Any]]] = []
            try:
                sub = get_submissions(cik10)
            except Exception:
                return t, None, new_parsed

            recent = (sub.get("filings") or {}).get("recent") or {}
            forms = recent.get("form") or []
            accs = recent.get("accessionNumber") or []
            fds = recent.get("filingDate") or []
            pdocs = recent.get("primaryDocument") or [""] * len(forms)

            cand_docs: list[tuple[str, str, str, str]] = []
            for form, acc, fdate, pdoc in zip(forms, accs, fds, pdocs):
                f_str = str(form or "").strip().upper()
                if f_str not in ("4", "4/A") or not acc or not fdate:
                    continue
                if str(fdate).strip() < cutoff_str:
                    continue
                cand_docs.append((f_str, str(acc).strip(), str(fdate).strip(), str(pdoc or "").strip()))
                if len(cand_docs) >= max_filings_per_ticker:
                    break

            parsed_docs: list[dict[str, Any]] = []
            for f_str, acc, fdate, pdoc in cand_docs:
                cached_doc = parsed_cache.get(acc)
                if isinstance(cached_doc, dict) and cached_doc.get("_ver") == FORM4_PARSER_VERSION:
                    parsed_docs.append(cached_doc)
                    continue
                raw_xml_name = pdoc.split("/")[-1].strip()
                if not raw_xml_name.lower().endswith(".xml"):
                    continue
                acc_nd = acc.replace("-", "")
                xml_url = f"https://www.sec.gov/Archives/edgar/data/{int(cik10)}/{acc_nd}/{raw_xml_name}"
                try:
                    raw_xml = fetch_bytes(xml_url, cache_key=None, max_age_hours=24 * 365)
                    parsed = _parse_form4_xml(
                        raw_xml,
                        cik10=cik10,
                        acc=acc,
                        filing_date=fdate,
                        pdoc=pdoc,
                        form_type=f_str,
                    )
                    if parsed is not None:
                        parsed["_ver"] = FORM4_PARSER_VERSION
                        parsed_docs.append(parsed)
                        new_parsed.append((acc, parsed))
                except Exception:
                    continue

            summary = summarize_form4_transactions_for_ticker(
                t,
                issuer_cik=cik10,
                parsed_docs=parsed_docs,
                lookback_days=lookback_days,
            )
            return t, summary, new_parsed

        with ThreadPoolExecutor(max_workers=6) as ex:
            for idx_done, (t, summary, new_parsed) in enumerate(ex.map(_process_ticker, target_pairs), 1):
                for acc_k, pdoc_v in new_parsed:
                    parsed_cache[acc_k] = pdoc_v
                    parsed_dirty = True
                if summary is not None:
                    cached_entry = dict(summary)
                    cached_entry["_ver"] = FORM4_PARSER_VERSION
                    cached_entry["_cutoff"] = cutoff_str
                    cached_entry["_ts"] = now_ts
                    ticker_cache[t] = cached_entry
                    ticker_dirty = True
                    if summary["openMarketBuyCount"] > 0 or summary["discretionarySellValueUsd"] > 0:
                        out[t] = summary
                if idx_done % 25 == 0 or idx_done == len(target_pairs):
                    if parsed_dirty:
                        FORM4_PARSED_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
                        save_json_dict(FORM4_PARSED_CACHE_PATH, parsed_cache)
                        parsed_dirty = False
                    if ticker_dirty:
                        FORM4_TICKER_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
                        save_json_dict(FORM4_TICKER_CACHE_PATH, ticker_cache)
                        ticker_dirty = False
                    print(f"[form4] Progress {idx_done}/{len(target_pairs)} tickers scanned ({len(out)} active)...", flush=True)

    return out

