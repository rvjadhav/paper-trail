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

from sec_client import CACHE_DIR as SEC_CACHE_DIR, fetch_bytes, fetch_json, get_submissions
from security_resolver import (
    _US_VALID_TICKER_RE,
    _normalize_legal_name,
    fetch_daily_prices,
    load_json_dict,
    normalize_yf_ticker,
    save_json_dict,
)

FUNDAMENTALS_CACHE_PATH = Path(__file__).resolve().parent / "fundamentals_cache.json"
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


def _extract_latest_point_fact(facts_root: dict[str, Any], concept_list: list[tuple[str, str]], unit_keys: tuple[str, ...]) -> float | None:
    facts_by_ns = facts_root.get("facts") or {}
    best_end = ""
    best_filed = ""
    best_val: float | None = None
    for ns, concept in concept_list:
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
                if (end, filed) > (best_end, best_filed):
                    best_end = end
                    best_filed = filed
                    best_val = fval
        if best_val is not None:
            return best_val
    return best_val


def _extract_ttm_and_prior(
    facts_root: dict[str, Any],
    concept_list: list[tuple[str, str]],
    unit_keys: tuple[str, ...],
) -> tuple[float | None, float | None, str]:
    """
    Extract (ttm_current, ttm_prior_1y, as_of_end_date) for a flow metric cleanly using either:
      1) Sum of 4 latest contiguous ~90d quarterly periods vs prior 4 quarterly periods
      2) Latest annual 10-K/20-F full-year period vs prior annual full-year period
    """
    facts_by_ns = facts_root.get("facts") or {}
    for ns, concept in concept_list:
        c_node = (facts_by_ns.get(ns) or {}).get(concept) or {}
        units = c_node.get("units") or {}
        for uk in unit_keys:
            rows = units.get(uk) or []
            if not rows:
                continue
            q_by_end: dict[str, tuple[str, float]] = {}
            a_by_end: dict[str, tuple[str, float]] = {}
            for r in rows:
                start_s = str(r.get("start") or "")
                end_s = str(r.get("end") or "")
                filed_s = str(r.get("filed") or "")
                val = r.get("val")
                if not start_s or not end_s or val is None:
                    continue
                try:
                    d0 = datetime.strptime(start_s[:10], "%Y-%m-%d").date()
                    d1 = datetime.strptime(end_s[:10], "%Y-%m-%d").date()
                    fval = float(val)
                except Exception:
                    continue
                dur = (d1 - d0).days
                if 70 <= dur <= 115:
                    prev = q_by_end.get(end_s[:10])
                    if prev is None or filed_s > prev[0]:
                        q_by_end[end_s[:10]] = (filed_s, fval)
                elif 330 <= dur <= 400:
                    prev = a_by_end.get(end_s[:10])
                    if prev is None or filed_s > prev[0]:
                        a_by_end[end_s[:10]] = (filed_s, fval)

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
                    if ttm_prev is None and len(distinct_as) >= 2:
                        return ttm_cur, None, distinct_qs[0][0]
                    return ttm_cur, ttm_prev, distinct_qs[0][0]

            if distinct_as:
                return distinct_as[0][1], (distinct_as[1][1] if len(distinct_as) >= 2 else None), distinct_as[0][0]
    return None, None, ""


def _extract_annual_only(
    facts_root: dict[str, Any],
    concept_list: list[tuple[str, str]],
    unit_keys: tuple[str, ...],
) -> tuple[float | None, float | None, str]:
    """Extract latest full-year (10-K/20-F, 330-400 days) current, prior, and period end date."""
    facts_by_ns = facts_root.get("facts") or {}
    for ns, concept in concept_list:
        c_node = (facts_by_ns.get(ns) or {}).get(concept) or {}
        units = c_node.get("units") or {}
        for uk in unit_keys:
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
                return distinct_as[0][1], (distinct_as[1][1] if len(distinct_as) >= 2 else None), distinct_as[0][0]
    return None, None, ""


def _parse_company_xbrl_facts(raw_facts: dict[str, Any]) -> dict[str, Any]:
    rev_concepts = [
        ("us-gaap", "RevenueFromContractWithCustomerExcludingAssessedTax"),
        ("us-gaap", "Revenues"),
        ("us-gaap", "SalesRevenueNet"),
        ("us-gaap", "RevenueFromContractWithCustomerIncludingAssessedTax"),
        ("ifrs-full", "Revenue"),
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

    rev_cur, rev_prev, rev_end = _extract_ttm_and_prior(raw_facts, rev_concepts, ("USD",))
    rev_ann, rev_ann_prev, rev_ann_end = _extract_annual_only(raw_facts, rev_concepts, ("USD",))
    if rev_prev is None and rev_ann and rev_ann_prev and (not rev_end or rev_end == rev_ann_end):
        rev_cur, rev_prev, rev_end = rev_ann, rev_ann_prev, rev_ann_end

    op_cur, _, op_end = _extract_ttm_and_prior(
        raw_facts,
        [("us-gaap", "OperatingIncomeLoss"), ("ifrs-full", "ProfitLossFromOperatingActivities")],
        ("USD",),
    )
    if op_cur is not None and op_end != rev_end:
        op_ann, _, op_ann_end = _extract_annual_only(
            raw_facts,
            [("us-gaap", "OperatingIncomeLoss"), ("ifrs-full", "ProfitLossFromOperatingActivities")],
            ("USD",),
        )
        if op_ann is not None and rev_ann and op_ann_end == rev_ann_end:
            op_cur, op_end = op_ann, op_ann_end

    # Use matched full-year OCF & CapEx if standalone 90d OCF quarters aren't available
    ocf_cur, _, ocf_end = _extract_ttm_and_prior(
        raw_facts,
        [("us-gaap", "NetCashProvidedByUsedInOperatingActivities"), ("ifrs-full", "CashFlowsFromUsedInOperatingActivities")],
        ("USD",),
    )
    capex_cur, _, capex_end = _extract_ttm_and_prior(
        raw_facts,
        [
            ("us-gaap", "PaymentsToAcquirePropertyPlantAndEquipment"),
            ("us-gaap", "PaymentsToAcquirePropertyPlantAndEquipmentAndIntangibleAssets"),
            ("ifrs-full", "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities"),
        ],
        ("USD",),
    )
    if ocf_cur is not None and ocf_end != rev_end:
        ocf_ann, _, ocf_ann_end = _extract_annual_only(
            raw_facts,
            [("us-gaap", "NetCashProvidedByUsedInOperatingActivities"), ("ifrs-full", "CashFlowsFromUsedInOperatingActivities")],
            ("USD",),
        )
        cap_ann, _, cap_ann_end = _extract_annual_only(
            raw_facts,
            [
                ("us-gaap", "PaymentsToAcquirePropertyPlantAndEquipment"),
                ("us-gaap", "PaymentsToAcquirePropertyPlantAndEquipmentAndIntangibleAssets"),
                ("ifrs-full", "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities"),
            ],
            ("USD",),
        )
        if ocf_ann is not None and ocf_ann_end == rev_ann_end:
            ocf_cur, ocf_end = ocf_ann, ocf_ann_end
            if cap_ann is not None and cap_ann_end == ocf_ann_end:
                capex_cur, capex_end = cap_ann, cap_ann_end

    rd_cur, _, rd_end = _extract_ttm_and_prior(
        raw_facts,
        [
            ("us-gaap", "ResearchAndDevelopmentExpense"),
            ("us-gaap", "ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost"),
            ("ifrs-full", "ResearchAndDevelopmentExpense"),
        ],
        ("USD",),
    )

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

    fcf_margin = None
    fcf_den = rev_cur if (ocf_end == rev_end and rev_cur) else (rev_ann if (ocf_end == rev_ann_end and rev_ann) else None)
    if ocf_cur is not None and fcf_den and fcf_den > 0:
        cap_matched = abs(capex_cur) if (capex_cur is not None and capex_end == ocf_end) else 0.0
        fm = ((ocf_cur - cap_matched) / fcf_den) * 100.0
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

    return {
        "sharesOutstanding": round(shares_out) if shares_out else None,
        "revenueTtmUsd": round(rev_cur, 0) if rev_cur else (round(rev_ann, 0) if rev_ann else None),
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
    Cached persistently in pipeline/fundamentals_cache.json with a 7-day TTL.
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
        if ent and (now_ts - float(ent.get("_ts", 0.0))) <= max_age_sec:
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
                parsed["_ts"] = now_ts
                return t, parsed
            except Exception:
                return t, {"_ts": now_ts, "cik": cik10}

        with ThreadPoolExecutor(max_workers=4) as ex:
            for idx_done, (t, parsed) in enumerate(ex.map(_fetch_one, to_fetch), 1):
                if parsed is not None:
                    cache[t] = parsed
                if idx_done % 80 == 0 or idx_done == len(to_fetch):
                    save_json_dict(FUNDAMENTALS_CACHE_PATH, cache)
                    print(f"[xbrl] Progress {idx_done}/{len(to_fetch)} facts parsed...", flush=True)

    return {
        t: {k: v for k, v in cache[t].items() if k != "_ts"}
        for t in tickers
        if t in cache and cache[t]
    }


def fetch_midquarter_alerts(
    elite_ciks: list[str],
    fund_names_by_cik: dict[str, str],
    cik_to_ticker_map: dict[str, str],
    name_to_ticker_map: dict[str, str],
    today_date: date,
    since_days: int = 95,
) -> list[dict[str, Any]]:
    """
    Scan recent SEC submissions for Elite-50 funds for live mid-quarter Schedule 13D / 13G
    filings (>= 5% beneficial ownership stakes reported within 5–10 days vs 45-day 13F lag).
    Resolves subject company issuer/CUSIP -> ticker via compact ~2KB .hdr.sgml headers.
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
        for form, acc, fdate in zip(forms, accs, fds):
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
            })

    if not candidate_filings:
        return []

    need_hdr = [it for it in candidate_filings if it["accessionNumber"] not in hdr_cache]
    if need_hdr:
        print(f"[alerts-13dg] Resolving subject companies for {len(need_hdr)} mid-quarter Schedule 13D/13G filings...", flush=True)
        def _fetch_hdr(item: dict[str, Any]) -> tuple[str, dict[str, str]]:
            cik10 = item["filerCik"]
            acc = item["accessionNumber"]
            acc_nd = acc.replace("-", "")
            url = f"https://www.sec.gov/Archives/edgar/data/{int(cik10)}/{acc_nd}/{acc}.hdr.sgml"
            try:
                raw = fetch_bytes(url, cache_key=f"hdr_{cik10}_{acc_nd}.sgml", max_age_hours=24 * 365).decode("utf-8", errors="replace")
                m_sub = re.search(
                    r"<SUBJECT-COMPANY>.*?<CONFORMED-NAME>([^\r\n<]+).*?<CIK>(\d+)",
                    raw,
                    flags=re.S | re.I,
                )
                if m_sub:
                    return acc, {
                        "subjectName": m_sub.group(1).strip(),
                        "subjectCik": f"{int(m_sub.group(2)):010d}",
                    }
            except Exception:
                pass
            return acc, {"subjectName": "", "subjectCik": ""}

        with ThreadPoolExecutor(max_workers=6) as ex:
            for acc, info in ex.map(_fetch_hdr, need_hdr):
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
        sub_cik = hinfo.get("subjectCik", "")
        sub_name = hinfo.get("subjectName", "")
        ticker = cik_to_ticker_map.get(sub_cik) or name_to_ticker_map.get(_normalize_legal_name(sub_name)) or ""
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
            "edgarUrl": f"https://www.sec.gov/Archives/edgar/data/{int(it['filerCik'])}/{acc_nd}/",
        })

    alerts.sort(key=lambda a: (a["filingDate"], a["isActivist"], a["fundName"]), reverse=True)
    return alerts
