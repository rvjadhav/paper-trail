"""
Free public API & price microstructure enrichment layer:
1. Daily historical risk-free rate from ^IRX (13-week T-Bill) with US Treasury FiscalData fallback
2. Quarter-End OHLCV Est. Quarter VWAP, Q-Low, Q-High, and Entry Zone status
3. SEC EDGAR XBRL CompanyFacts fundamentals (Market Cap via Shares Outstanding, Revenue YoY Growth,
   Operating Margin, FCF Margin, R&D Intensity, YoY Share Dilution)
4. Live mid-quarter Schedule 13D / 13G / 13D/A / 13G/A activist & >=5% stake alerts for Elite 50 funds
"""

from __future__ import annotations

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
_13DG_FORM_RE = re.compile(r"^(?:SCHEDULE|SC)\s*13([DG])(?:/A)?$", re.I)


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


def _parse_13dg_primary_xml(raw_xml: bytes) -> dict[str, Any]:
    """
    Parse SEC Schedule 13D/13G XML filing to extract ownership % (classPercent),
    beneficial shares owned, subject CUSIP, and event date.
    """
    out: dict[str, Any] = {
        "percentOfClass": None,
        "sharesOwned": None,
        "subjectCusip": "",
        "eventDate": "",
    }
    try:
        root = _parse_xml_root(raw_xml)
    except Exception:
        return out
    if root is None:
        return out

    pct_vals: list[float] = []
    sh_vals: list[float] = []
    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        lname = el.tag.rsplit("}", 1)[-1]
        txt = el.text.strip() if el.text else ""
        if not txt:
            continue
        if lname in ("classPercent", "percentOfClass"):
            cleaned = re.sub(r"[^0-9.-]", "", txt)
            try:
                pv = float(cleaned)
                if 0.0 <= pv <= 100.0:
                    pct_vals.append(round(pv, 2))
            except ValueError:
                pass
        elif lname in ("reportingPersonBeneficiallyOwnedAggregateNumberOfShares", "aggregateAmountOwned"):
            cleaned = re.sub(r"[^0-9.]", "", txt.replace(",", ""))
            try:
                sv = float(cleaned)
                if sv >= 0:
                    sh_vals.append(round(sv))
            except ValueError:
                pass
        elif lname in ("issuerCusipNumber", "cusipNo") and not out["subjectCusip"]:
            c_clean = re.sub(r"[^A-Za-z0-9]", "", txt).upper()[:9]
            if len(c_clean) >= 6:
                out["subjectCusip"] = c_clean.zfill(9)
        elif lname in ("eventDateRequiresFilingThisStatement", "dateOfEvent") and not out["eventDate"]:
            m_mdy = re.fullmatch(r"(\d{2})/(\d{2})/(\d{4})", txt)
            if m_mdy:
                out["eventDate"] = f"{m_mdy.group(3)}-{m_mdy.group(1)}-{m_mdy.group(2)}"
            elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", txt):
                out["eventDate"] = txt

    if pct_vals:
        out["percentOfClass"] = max(pct_vals)
    if sh_vals:
        out["sharesOwned"] = max(sh_vals)
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
    Extracts subject company from .hdr.sgml AND beneficial ownership % / shares / CUSIP from primary_doc.xml.
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
    ]
    if need_fetch:
        print(f"[alerts-13dg] Resolving subject companies & ownership stakes for {len(need_fetch)} mid-quarter Schedule 13D/13G filings...", flush=True)
        def _fetch_hdr_and_xml(item: dict[str, Any]) -> tuple[str, dict[str, Any]]:
            cik10 = item["filerCik"]
            acc = item["accessionNumber"]
            acc_nd = acc.replace("-", "")
            existing = dict(hdr_cache.get(acc) or {})
            info: dict[str, Any] = {
                "subjectName": existing.get("subjectName", ""),
                "subjectCik": existing.get("subjectCik", ""),
                "percentOfClass": existing.get("percentOfClass"),
                "sharesOwned": existing.get("sharesOwned"),
                "subjectCusip": existing.get("subjectCusip", ""),
                "eventDate": existing.get("eventDate", ""),
                "stakeChecked": True,
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

            pdoc = str(item.get("primaryDoc") or "").split("/")[-1].strip()
            if pdoc.lower().endswith(".xml"):
                xml_url = f"https://www.sec.gov/Archives/edgar/data/{int(cik10)}/{acc_nd}/{pdoc}"
                try:
                    raw_xml = fetch_bytes(xml_url, cache_key=f"xml13dg_{cik10}_{acc_nd}.xml", max_age_hours=24 * 365)
                    parsed_xml = _parse_13dg_primary_xml(raw_xml)
                    if parsed_xml["percentOfClass"] is not None:
                        info["percentOfClass"] = parsed_xml["percentOfClass"]
                    if parsed_xml["sharesOwned"] is not None:
                        info["sharesOwned"] = parsed_xml["sharesOwned"]
                    if parsed_xml["subjectCusip"]:
                        info["subjectCusip"] = parsed_xml["subjectCusip"]
                    if parsed_xml["eventDate"]:
                        info["eventDate"] = parsed_xml["eventDate"]
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
            "edgarUrl": f"https://www.sec.gov/Archives/edgar/data/{int(it['filerCik'])}/{acc_nd}/",
        })

    alerts.sort(key=lambda a: (a["filingDate"], a["isActivist"], a["fundName"]), reverse=True)
    return alerts
