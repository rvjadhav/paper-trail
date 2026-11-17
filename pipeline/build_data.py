"""
End-to-end Paper Trail 13F data pipeline:
1. Fetches 2021-present SEC EDGAR 13F-HR / 13F-HR/A filings for curated + discovered funds
2. Resolves CUSIPs -> tickers + GICS sectors + daily adjusted-close prices
3. Qualifies genuinely Tech/TMT-focused concentrated portfolios
4. Reconstructs daily buy-and-hold equity curves for both Quarter-End and 45-Day Lag modes
5. Computes rankings across YTD, TTM, 18M, 2Y, 3Y, 5Y windows
6. Builds Top 30 Consensus Holdings, Ideal Portfolio (<= 50 names), and QoQ Fund Updates Digest
7. Writes src/data/portfolio_data.json for static frontend & GitHub Pages deployment
"""

from __future__ import annotations

import json
import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np

from funds_config import CURATED_FUNDS, NON_STOCK_ETFS
from sec_client import (
    build_clean_quarterly_filings,
    discover_emerging_tech_funds,
    list_13f_filings,
    parse_13f_filing,
)
from security_resolver import (
    CUSIP_CACHE_PATH,
    MANUAL_CUSIP_OVERRIDES,
    TICKER_META_PATH,
    fetch_all_prices,
    is_tech_tmt,
    load_json_dict,
    resolve_cusips,
)

ROOT_DIR = Path(__file__).resolve().parent.parent
OUTPUT_JSON = ROOT_DIR / "src" / "data" / "portfolio_data.json"

WINDOWS = ["YTD", "TTM", "18M", "2Y", "3Y", "5Y"]
MODES = ["quarter_end", "filing_lag"]


def quarter_label(report_date: str) -> str:
    y, m, _ = report_date.split("-")
    q = (int(m) - 1) // 3 + 1
    return f"{y} Q{q}"


def clean_num(val: float | None, decimals: int = 2) -> float | None:
    if val is None or not math.isfinite(val):
        return None
    return round(float(val), decimals)


def build_aligned_price_matrix(
    cal_dates: list[str],
    raw_prices: dict[str, dict[str, float]],
) -> dict[str, tuple[np.ndarray, int, int]]:
    """
    For each ticker, align daily adjusted prices onto `cal_dates` with forward-fill
    between first_idx and last_idx.
    Returns ticker -> (price_array, first_valid_idx, last_valid_idx).
    """
    n = len(cal_dates)
    aligned: dict[str, tuple[np.ndarray, int, int]] = {}
    for ticker, pdict in raw_prices.items():
        if not pdict:
            continue
        arr = np.full(n, np.nan, dtype=np.float64)
        first_idx = n
        last_idx = -1
        for i, d in enumerate(cal_dates):
            if d in pdict:
                arr[i] = pdict[d]
                if i < first_idx:
                    first_idx = i
                if i > last_idx:
                    last_idx = i
        if last_idx < 0 or first_idx >= n:
            continue
        # Forward fill gaps between first_idx and last_idx
        last_val = arr[first_idx]
        for i in range(first_idx + 1, last_idx + 1):
            if math.isnan(arr[i]):
                arr[i] = last_val
            else:
                last_val = arr[i]
        # Freeze at last known price after delisting/end
        for i in range(last_idx + 1, n):
            arr[i] = last_val
        aligned[ticker] = (arr, first_idx, last_idx)
    return aligned


def find_idx_on_or_after(cal_dates: list[str], target_date: str) -> int | None:
    import bisect
    pos = bisect.bisect_left(cal_dates, target_date)
    if pos < len(cal_dates):
        return pos
    return None


def find_idx_on_or_before(cal_dates: list[str], target_date: str) -> int | None:
    import bisect
    pos = bisect.bisect_right(cal_dates, target_date) - 1
    if pos >= 0:
        return pos
    return None


def simulate_nav_series(
    segments: list[tuple[int, dict[str, float]]],
    aligned_prices: dict[str, tuple[np.ndarray, int, int]],
    n_dates: int,
) -> np.ndarray:
    """
    Simulate buy-and-hold within each rebalance segment [(start_idx, weights_dict), ...].
    Returns array of length n_dates with NaN before first start_idx and Base=100 from first start_idx.
    """
    nav = np.full(n_dates, np.nan, dtype=np.float64)
    if not segments:
        return nav

    running_nav = 100.0
    for seg_i, (s_idx, weights) in enumerate(segments):
        e_idx = segments[seg_i + 1][0] if seg_i + 1 < len(segments) else (n_dates - 1)
        if s_idx >= n_dates or e_idx <= s_idx:
            if s_idx < n_dates and math.isnan(nav[s_idx]):
                nav[s_idx] = running_nav
            continue

        # Decompose weights into priced vs unpriced (cash proxy)
        priced_items: list[tuple[np.ndarray, float, float]] = []
        cash_weight = 0.0
        for ticker, w in weights.items():
            if w <= 0:
                continue
            pdata = aligned_prices.get(ticker)
            if pdata is None:
                cash_weight += w
                continue
            arr, first_idx, _ = pdata
            # Allow up to 5 trading days if stock IPO'd right around quarter boundary
            base_idx = s_idx if s_idx >= first_idx else (first_idx if first_idx - s_idx <= 5 else -1)
            if base_idx < 0 or math.isnan(arr[base_idx]) or arr[base_idx] <= 0:
                cash_weight += w
                continue
            priced_items.append((arr, w, float(arr[base_idx])))

        # Vectorized daily segment valuation
        span = np.full(e_idx - s_idx + 1, cash_weight, dtype=np.float64)
        for arr, w, base_px in priced_items:
            sub = arr[s_idx : e_idx + 1]
            # Handle brief pre-IPO days inside the 5-day tolerance window
            rel = np.where(np.isnan(sub), 1.0, sub / base_px)
            span += w * rel

        nav[s_idx : e_idx + 1] = running_nav * span
        running_nav = float(nav[e_idx])

    return nav


def compute_window_start_indices(cal_dates: list[str]) -> dict[str, int]:
    as_of_str = cal_dates[-1]
    as_of_dt = datetime.strptime(as_of_str, "%Y-%m-%d")

    def _months_ago(months: int) -> str:
        y = as_of_dt.year
        m = as_of_dt.month - months
        while m <= 0:
            m += 12
            y -= 1
        # Clamp day to valid day of month
        import calendar
        max_d = calendar.monthrange(y, m)[1]
        d = min(as_of_dt.day, max_d)
        return f"{y:04d}-{m:02d}-{d:02d}"

    targets = {
        "YTD": f"{as_of_dt.year - 1}-12-31",
        "TTM": _months_ago(12),
        "18M": _months_ago(18),
        "2Y": _months_ago(24),
        "3Y": _months_ago(36),
        "5Y": _months_ago(60),
    }

    out: dict[str, int] = {}
    for w, dt_str in targets.items():
        idx = find_idx_on_or_before(cal_dates, dt_str)
        out[w] = idx if idx is not None else 0
    return out


def compute_returns_and_metrics(
    nav: np.ndarray,
    qqq_nav: np.ndarray,
    win_indices: dict[str, int],
) -> tuple[dict[str, float | None], dict[str, float | None]]:
    valid_mask = ~np.isnan(nav)
    valid_indices = np.where(valid_mask)[0]
    if len(valid_indices) < 20:
        return {w: None for w in WINDOWS}, {
            "cagrPct": None,
            "maxDrawdownPct": None,
            "sharpeProxy": None,
            "betaVsQqq": None,
            "alphaVsQqqPct": None,
            "annualizedVolPct": None,
        }

    first_valid = int(valid_indices[0])
    last_valid = int(valid_indices[-1])
    end_nav = float(nav[last_valid])

    win_rets: dict[str, float | None] = {}
    for w in WINDOWS:
        s_idx = win_indices[w]
        # Require full window coverage (tolerance of 5 trading days)
        if first_valid <= s_idx + 5:
            ref_idx = max(s_idx, first_valid)
            start_val = float(nav[ref_idx])
            if start_val > 0:
                win_rets[w] = clean_num(((end_nav / start_val) - 1.0) * 100.0, 2)
            else:
                win_rets[w] = None
        else:
            win_rets[w] = None

    # Trailing 3Y (or full available) risk/alpha stats
    stat_start = max(first_valid, win_indices["3Y"])
    sub_nav = nav[stat_start : last_valid + 1]
    sub_qqq = qqq_nav[stat_start : last_valid + 1]

    days = len(sub_nav)
    years = max(days / 252.0, 0.25)
    total_ret = (sub_nav[-1] / sub_nav[0]) - 1.0
    cagr = ((1.0 + total_ret) ** (1.0 / years) - 1.0) * 100.0 if (1.0 + total_ret) > 0 else None

    daily_r = np.diff(sub_nav) / sub_nav[:-1]
    qqq_r = np.diff(sub_qqq) / sub_qqq[:-1]
    ann_vol = float(np.std(daily_r, ddof=1) * math.sqrt(252.0) * 100.0) if len(daily_r) > 5 else None

    # Max drawdown
    running_max = np.maximum.accumulate(sub_nav)
    dd = (sub_nav / running_max) - 1.0
    max_dd = float(np.min(dd) * 100.0)

    sharpe = None
    if cagr is not None and ann_vol and ann_vol > 0:
        sharpe = clean_num((cagr - 4.0) / ann_vol, 2)

    beta = None
    alpha = None
    if len(daily_r) > 20:
        var_q = float(np.var(qqq_r, ddof=1))
        if var_q > 0:
            cov_fq = float(np.cov(daily_r, qqq_r, ddof=1)[0, 1])
            beta = clean_num(cov_fq / var_q, 2)
        qqq_total = (sub_qqq[-1] / sub_qqq[0]) - 1.0
        qqq_cagr = ((1.0 + qqq_total) ** (1.0 / years) - 1.0) * 100.0
        if cagr is not None:
            alpha = clean_num(cagr - qqq_cagr, 2)

    metrics = {
        "cagrPct": clean_num(cagr, 2),
        "maxDrawdownPct": clean_num(max_dd, 2),
        "sharpeProxy": sharpe,
        "betaVsQqq": beta,
        "alphaVsQqqPct": alpha,
        "annualizedVolPct": clean_num(ann_vol, 2),
    }
    return win_rets, metrics


def downsample_curve(cal_dates: list[str], nav: np.ndarray, step: int = 5) -> list[dict[str, Any]]:
    valid = np.where(~np.isnan(nav))[0]
    if len(valid) == 0:
        return []
    first_i = int(valid[0])
    last_i = int(valid[-1])
    base = float(nav[first_i])
    pts: list[dict[str, Any]] = []
    recent_cutoff = last_i - 325
    coarse_step = max(step, step * 3)
    for i in range(first_i, last_i + 1):
        stride = step if i >= recent_cutoff else coarse_step
        if i == first_i or i == last_i or (i % stride == 0):
            pts.append({"date": cal_dates[i], "nav": round(float(nav[i]) / base * 100.0, 2)})
    return pts


def extract_equity_weights(
    filing: dict[str, Any],
    cusip_meta: dict[str, dict[str, Any]],
) -> tuple[dict[str, float], float, float, int, float, float]:
    """
    Return (ticker_weights_dict, equity_aum_usd, total_reported_usd,
            equity_pos_count, top10_weight_pct, tech_tmt_pct)
    """
    total_reported = sum(r["valueUsd"] for r in filing["rows"])
    eq_rows = [r for r in filing["rows"] if r["secType"] == "EQUITY" and r["valueUsd"] > 0]
    eq_aum = sum(r["valueUsd"] for r in eq_rows)
    if eq_aum <= 0:
        return {}, 0.0, total_reported, 0, 0.0, 0.0

    by_ticker: dict[str, float] = {}
    tech_val = 0.0
    for r in eq_rows:
        meta = cusip_meta.get(r["cusip"], {})
        ticker = meta.get("ticker") or f"CUSIP:{r['cusip']}"
        by_ticker[ticker] = by_ticker.get(ticker, 0.0) + r["valueUsd"]
        if meta.get("isTechTmt"):
            tech_val += r["valueUsd"]

    weights = {t: v / eq_aum for t, v in by_ticker.items()}
    sorted_w = sorted(weights.values(), reverse=True)
    top10_pct = sum(sorted_w[:10]) * 100.0
    tech_pct = (tech_val / eq_aum) * 100.0
    return weights, eq_aum, total_reported, len(by_ticker), top10_pct, tech_pct


def waterfill_capped_weights(raw_scores: dict[str, float], cap: float = 0.08) -> dict[str, float]:
    """
    Normalize positive scores into portfolio weights summing to 1.0
    with no position exceeding `cap`.
    """
    pos = {k: v for k, v in raw_scores.items() if v > 0}
    if not pos:
        return {}
    if len(pos) * cap < 0.9999:
        cap = 1.0 / len(pos) + 1e-6

    fixed: dict[str, float] = {}
    remaining = dict(pos)
    rem_budget = 1.0

    while remaining:
        total_score = sum(remaining.values())
        if total_score <= 0:
            break
        over = [k for k, sc in remaining.items() if (sc / total_score) * rem_budget > cap + 1e-9]
        if not over:
            for k, sc in remaining.items():
                fixed[k] = (sc / total_score) * rem_budget
            break
        for k in over:
            fixed[k] = cap
            rem_budget -= cap
            del remaining[k]
    return fixed


def compute_ideal_portfolio_weights(
    funds_subset: list[dict[str, Any]],
    mode: str,
    cusip_meta: dict[str, dict[str, Any]],
    max_names: int = 40,
    cap_pct: float = 8.0,
) -> tuple[dict[str, float], dict[str, dict[str, Any]]]:
    """
    Score stocks across ranked funds by multi-window performance rank, conviction weight,
    multi-fund consensus, and QoQ net accumulation.
    Returns (weights_0_to_1, stock_scoring_details).
    """
    if not funds_subset:
        return {}, {}

    # Compute percentile rank per window for each fund
    win_weights = {"TTM": 0.25, "3Y": 0.25, "2Y": 0.20, "18M": 0.15, "YTD": 0.10, "5Y": 0.05}
    fund_scores: dict[str, float] = {}

    win_ranks: dict[str, dict[str, float]] = {}
    for w in WINDOWS:
        eligible = [(f["cik"], f["returns"][mode][w]) for f in funds_subset if f["returns"][mode][w] is not None]
        eligible.sort(key=lambda x: x[1], reverse=True)
        n = len(eligible)
        win_ranks[w] = {}
        for rank_i, (cik, _) in enumerate(eligible):
            # Score from 1.0 (top) down to 0.25 (tail)
            win_ranks[w][cik] = 1.0 - 0.75 * (rank_i / max(1, n - 1)) if n > 1 else 1.0

    for f in funds_subset:
        cik = f["cik"]
        num = 0.0
        den = 0.0
        for w, ww in win_weights.items():
            if cik in win_ranks[w]:
                num += ww * win_ranks[w][cik]
                den += ww
        fund_scores[cik] = (num / den) if den > 0 else 0.5

    # Aggregate stock signals across the funds
    stock_agg: dict[str, dict[str, Any]] = {}
    for f in funds_subset:
        cik = f["cik"]
        fscore = fund_scores[cik]
        for h in f["holdings"]:
            if h["securityType"] != "EQUITY" or h["weightPct"] <= 0:
                continue
            t = h["ticker"]
            if t.startswith("CUSIP:") or t in NON_STOCK_ETFS:
                continue
            if t not in stock_agg:
                stock_agg[t] = {
                    "ticker": t,
                    "cusip": h["cusip"],
                    "name": h["name"],
                    "sector": h["sector"],
                    "industry": h["industry"],
                    "convSum": 0.0,
                    "weights": [],
                    "backers": [],
                    "netBuy": 0,
                    "netTrim": 0,
                }
            rec = stock_agg[t]
            w_dec = h["weightPct"] / 100.0
            rec["convSum"] += fscore * math.sqrt(w_dec)
            rec["weights"].append(h["weightPct"])
            rec["backers"].append({
                "cik": cik,
                "fundName": f["name"],
                "weightPct": h["weightPct"],
                "status": h["status"],
                "fscore": fscore,
            })
            if h["status"] in ("NEW", "ADDED"):
                rec["netBuy"] += 1
            elif h["status"] == "TRIMMED":
                rec["netTrim"] += 1

    # Filter out single-fund sub-1.5% noise unless backed by >=2 funds or >=4% conviction by a top fund
    qualified: dict[str, float] = {}
    for t, rec in stock_agg.items():
        cnt = len(rec["backers"])
        max_w = max(rec["weights"])
        if cnt < 2 and max_w < 4.5:
            continue
        consensus_mult = 1.0 + 0.18 * math.log(max(1, cnt))
        mom_delta = (rec["netBuy"] - rec["netTrim"]) / max(1, cnt)
        mom_mult = 1.0 + 0.08 * max(-0.5, min(1.0, mom_delta))
        score = rec["convSum"] * consensus_mult * mom_mult
        rec["rawScore"] = score
        qualified[t] = score

    top_items = sorted(qualified.items(), key=lambda x: x[1], reverse=True)[:max_names]
    top_scores = {t: s for t, s in top_items}
    capped = waterfill_capped_weights(top_scores, cap=cap_pct / 100.0)
    return capped, stock_agg


def _fmt_usd_brief(val: float) -> str:
    av = abs(val)
    if av >= 1e9:
        return f"${av / 1e9:.2f}B"
    if av >= 1e6:
        return f"${av / 1e6:.1f}M"
    if av >= 1e3:
        return f"${av / 1e3:.0f}K"
    return f"${av:.0f}"


def _fmt_shares_brief(sh: float) -> str:
    ash = abs(sh)
    if ash >= 1e6:
        return f"{ash / 1e6:.2f}M"
    if ash >= 1e3:
        return f"{ash / 1e3:.0f}k"
    return f"{ash:.0f}"


def _fmt_period_short(date_str: str) -> str:
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").strftime("%b %d")
    except Exception:
        return date_str


def _short_q_label(period_end: str) -> str:
    _, m, _ = period_end.split("-")
    q = (int(m) - 1) // 3 + 1
    return f"Q{q}"


def compute_deadline_status(period_end: str, filing_date: str) -> dict[str, Any]:
    """
    Compute SEC statutory 13F-HR deadline (period_end + 45 calendar days,
    rolled forward to Monday if Day 45 falls on Saturday or Sunday).
    """
    pe_dt = datetime.strptime(period_end, "%Y-%m-%d").date()
    dl_dt = pe_dt + timedelta(days=45)
    if dl_dt.weekday() == 5:  # Saturday -> Monday
        dl_dt += timedelta(days=2)
    elif dl_dt.weekday() == 6:  # Sunday -> Monday
        dl_dt += timedelta(days=1)

    fd_dt = datetime.strptime(filing_date, "%Y-%m-%d").date()
    days_before = (dl_dt - fd_dt).days
    dl_str = dl_dt.strftime("%Y-%m-%d")

    if days_before == 0:
        badge = "Deadline Day"
        phrase = "on the deadline day"
    elif days_before > 0:
        badge = "Early Filer"
        phrase = f"{days_before}d before the deadline" if days_before <= 14 else f"on {_fmt_period_short(filing_date)}"
    else:
        badge = "Amended / Post-Deadline"
        phrase = f"on {filing_date} (post-deadline / amended)"

    return {
        "statutoryDeadline": dl_str,
        "daysBeforeDeadline": days_before,
        "deadlineBadge": badge,
        "deadlinePhrase": phrase,
    }


def aggregate_filing_positions(
    filing: dict[str, Any],
    cusip_meta: dict[str, dict[str, Any]],
) -> tuple[dict[tuple[str, str], dict[str, Any]], float, float]:
    """
    Aggregate filing rows by (rawTicker, secType).
    Returns (agg_dict, total_reported_usd, equity_aum_usd).
    """
    agg: dict[tuple[str, str], dict[str, Any]] = {}
    total_reported = 0.0
    eq_aum = 0.0
    for r in filing["rows"]:
        val = float(r["valueUsd"])
        sh = float(r["shares"])
        if val <= 0:
            continue
        total_reported += val
        if r["secType"] == "EQUITY":
            eq_aum += val
        meta = cusip_meta.get(r["cusip"], {})
        raw_ticker = meta.get("ticker") or f"CUSIP:{r['cusip']}"
        disp_ticker = meta.get("displaySymbol") or raw_ticker
        key = (raw_ticker, r["secType"])
        if key not in agg:
            agg[key] = {
                "cusip": r["cusip"],
                "ticker": disp_ticker if raw_ticker.startswith("CUSIP:") else raw_ticker,
                "rawTicker": raw_ticker,
                "name": meta.get("name") or r["issuer"],
                "sector": meta.get("sector") or "Other",
                "industry": meta.get("industry") or "",
                "isTechTmt": bool(meta.get("isTechTmt")),
                "securityType": r["secType"],
                "shares": sh,
                "valueUsd": val,
            }
        else:
            agg[key]["shares"] += sh
            agg[key]["valueUsd"] += val
    return agg, total_reported, eq_aum


def build_fund_update_entry(
    cand: dict[str, Any],
    filings: list[dict[str, Any]],
    cusip_meta: dict[str, dict[str, Any]],
    in_rankings: bool,
    ranked_windows: list[str],
) -> dict[str, Any] | None:
    if len(filings) < 2:
        return None
    prev_fl = filings[-2]
    curr_fl = filings[-1]

    curr_agg, curr_rep, curr_eq_aum = aggregate_filing_positions(curr_fl, cusip_meta)
    prev_agg, prev_rep, prev_eq_aum = aggregate_filing_positions(prev_fl, cusip_meta)
    if curr_rep <= 0 or prev_rep <= 0:
        return None

    cik = cand["cik"]
    acc = curr_fl["accessionNumber"]
    acc_nd = acc.replace("-", "")
    filing_url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc_nd}/"
    dl_info = compute_deadline_status(curr_fl["reportDate"], curr_fl["filingDate"])

    q_label = quarter_label(curr_fl["reportDate"])
    prev_q_label = quarter_label(prev_fl["reportDate"])
    prev_q_short = _short_q_label(prev_fl["reportDate"])

    all_keys = sorted(
        set(curr_agg.keys()) | set(prev_agg.keys()),
        key=lambda k: max(
            curr_agg[k]["valueUsd"] if k in curr_agg else 0.0,
            prev_agg[k]["valueUsd"] if k in prev_agg else 0.0,
        ),
        reverse=True,
    )

    all_moves: list[dict[str, Any]] = []
    valuation_shifts: list[dict[str, Any]] = []
    put_legs: list[dict[str, Any]] = []
    call_legs: list[dict[str, Any]] = []

    curr_eq_book = 0.0
    prev_eq_book = 0.0
    curr_call_usd = 0.0
    prev_call_usd = 0.0
    curr_put_usd = 0.0
    prev_put_usd = 0.0

    new_cnt = added_cnt = trimmed_cnt = exited_cnt = unchanged_cnt = 0
    gross_activity = 0.0

    for key in all_keys:
        c_item = curr_agg.get(key)
        p_item = prev_agg.get(key)
        base = c_item or p_item
        assert base is not None

        sec_type = base["securityType"]
        prev_sh = float(p_item["shares"]) if p_item else 0.0
        curr_sh = float(c_item["shares"]) if c_item else 0.0
        sh_delta = curr_sh - prev_sh

        prev_val = float(p_item["valueUsd"]) if p_item else 0.0
        curr_val = float(c_item["valueUsd"]) if c_item else 0.0
        val_delta = curr_val - prev_val

        prev_book_pct = (prev_val / prev_rep * 100.0) if (p_item and prev_rep > 0) else 0.0
        curr_book_pct = (curr_val / curr_rep * 100.0) if (c_item and curr_rep > 0) else 0.0
        book_delta_pct = curr_book_pct - prev_book_pct

        if sec_type == "EQUITY":
            curr_eq_book += curr_val
            prev_eq_book += prev_val
        elif sec_type == "CALL":
            curr_call_usd += curr_val
            prev_call_usd += prev_val
        elif sec_type == "PUT":
            curr_put_usd += curr_val
            prev_put_usd += prev_val

        sh_delta_pct: float | None = None
        if p_item is None or prev_sh <= 0:
            action = "NEW"
            new_cnt += 1
        elif c_item is None or curr_sh <= 0:
            action = "EXITED"
            sh_delta_pct = -100.0
            exited_cnt += 1
        else:
            diff_pct = ((curr_sh / prev_sh) - 1.0) * 100.0
            sh_delta_pct = clean_num(diff_pct, 1)
            if diff_pct > 2.0:
                action = "ADDED"
                added_cnt += 1
            elif diff_pct < -2.0:
                action = "TRIMMED"
                trimmed_cnt += 1
            else:
                action = "UNCHANGED"
                unchanged_cnt += 1

        if action != "UNCHANGED":
            gross_activity += abs(val_delta)

        # Detect valuation markdowns / markups on essentially flat share counts
        val_shift_type = None
        tag_note = None
        if (
            p_item is not None
            and c_item is not None
            and prev_sh > 0
            and curr_sh > 0
            and sh_delta_pct is not None
            and abs(sh_delta_pct) <= 2.5
        ):
            sig_weight_shift = abs(book_delta_pct) >= 1.2
            sig_val_shift = prev_val >= 25_000_000 and (abs(val_delta) / prev_val >= 0.15) and abs(book_delta_pct) >= 0.5
            if sig_weight_shift or sig_val_shift:
                if book_delta_pct <= -0.5 or val_delta < 0:
                    val_shift_type = "MARKDOWN"
                    tag_note = (
                        f"Markdown on flat shares ({prev_book_pct:.1f}% → {curr_book_pct:.1f}%), not a sale"
                    )
                else:
                    val_shift_type = "MARKUP"
                    tag_note = f"Markup on flat shares ({prev_book_pct:.1f}% → {curr_book_pct:.1f}%)"

        move_obj = {
            "cusip": base["cusip"],
            "ticker": base["ticker"],
            "name": base["name"],
            "sector": base["sector"],
            "industry": base["industry"],
            "securityType": sec_type,
            "action": action,
            "prevShares": round(prev_sh),
            "currShares": round(curr_sh),
            "sharesDelta": round(sh_delta),
            "sharesDeltaPct": sh_delta_pct,
            "prevValueUsd": round(prev_val, 2),
            "currValueUsd": round(curr_val, 2),
            "valueDeltaUsd": round(val_delta, 2),
            "prevBookPct": round(prev_book_pct, 2),
            "currBookPct": round(curr_book_pct, 2),
            "bookDeltaPct": round(book_delta_pct, 2),
            "valuationShiftType": val_shift_type,
            "tagNote": tag_note,
        }
        all_moves.append(move_obj)
        if val_shift_type is not None:
            valuation_shifts.append(move_obj)

        if sec_type == "PUT":
            put_legs.append({
                "ticker": base["ticker"],
                "name": base["name"],
                "securityType": "PUT",
                "action": action,
                "prevShares": round(prev_sh),
                "currShares": round(curr_sh),
                "prevValueUsd": round(prev_val, 2),
                "currValueUsd": round(curr_val, 2),
                "prevBookPct": round(prev_book_pct, 2),
                "currBookPct": round(curr_book_pct, 2),
                "sharesDeltaPct": sh_delta_pct,
            })
        elif sec_type == "CALL":
            call_legs.append({
                "ticker": base["ticker"],
                "name": base["name"],
                "securityType": "CALL",
                "action": action,
                "prevShares": round(prev_sh),
                "currShares": round(curr_sh),
                "prevValueUsd": round(prev_val, 2),
                "currValueUsd": round(curr_val, 2),
                "prevBookPct": round(prev_book_pct, 2),
                "currBookPct": round(curr_book_pct, 2),
                "sharesDeltaPct": sh_delta_pct,
            })

    put_legs.sort(key=lambda x: max(x["currValueUsd"], x["prevValueUsd"]), reverse=True)
    call_legs.sort(key=lambda x: max(x["currValueUsd"], x["prevValueUsd"]), reverse=True)
    valuation_shifts.sort(key=lambda x: abs(x["bookDeltaPct"]), reverse=True)

    # Cross-instrument rotations & multi-leg issuer exposure signals
    by_issuer: dict[str, dict[str, dict[str, float | str]]] = {}
    for mv in all_moves:
        t = mv["ticker"]
        if t not in by_issuer:
            by_issuer[t] = {"meta": {"ticker": t, "name": mv["name"]}}
        by_issuer[t][mv["securityType"]] = {
            "prevVal": float(mv["prevValueUsd"]),
            "currVal": float(mv["currValueUsd"]),
            "prevBook": float(mv["prevBookPct"]),
            "currBook": float(mv["currBookPct"]),
        }

    instrument_shifts: list[dict[str, Any]] = []
    for t, legs in by_issuer.items():
        nm = str(legs["meta"]["name"])
        eq = legs.get("EQUITY", {"prevVal": 0.0, "currVal": 0.0, "prevBook": 0.0, "currBook": 0.0})
        ca = legs.get("CALL", {"prevVal": 0.0, "currVal": 0.0, "prevBook": 0.0, "currBook": 0.0})
        pu = legs.get("PUT", {"prevVal": 0.0, "currVal": 0.0, "prevBook": 0.0, "currBook": 0.0})

        p_eq, c_eq = float(eq["prevVal"]), float(eq["currVal"])
        p_ca, c_ca = float(ca["prevVal"]), float(ca["currVal"])
        p_pu, c_pu = float(pu["prevVal"]), float(pu["currVal"])
        min_sig = max(10_000_000.0, 0.004 * max(curr_rep, prev_rep))

        # Common <-> Call rotations
        if p_eq >= min_sig and c_eq == 0.0 and c_ca >= min_sig and c_ca > p_ca * 1.4:
            instrument_shifts.append({
                "ticker": t,
                "name": nm,
                "shiftType": "COMMON_TO_CALL",
                "label": f"{t}: Shifted from common to calls",
                "detail": f"Exited {_fmt_usd_brief(p_eq)} common; holds {_fmt_usd_brief(c_ca)} calls ({float(ca['currBook']):.1f}% of book)",
                "prevTotalUsd": round(p_eq + p_ca, 2),
                "currTotalUsd": round(c_ca, 2),
                "currBookPct": round(float(ca["currBook"]), 2),
            })
        elif p_ca >= min_sig and c_ca == 0.0 and c_eq > p_eq * 1.25 and c_eq >= min_sig:
            instrument_shifts.append({
                "ticker": t,
                "name": nm,
                "shiftType": "CALL_TO_COMMON",
                "label": f"{t}: Shifted from calls to common",
                "detail": f"Exited {_fmt_usd_brief(p_ca)} calls; common at {_fmt_usd_brief(c_eq)} ({float(eq['currBook']):.1f}% of book)",
                "prevTotalUsd": round(p_eq + p_ca, 2),
                "currTotalUsd": round(c_eq, 2),
                "currBookPct": round(float(eq["currBook"]), 2),
            })
        elif (p_ca + c_ca > 0.0) and (p_eq + c_eq > 0.0):
            prev_comb = p_eq + p_ca
            curr_comb = c_eq + c_ca
            comb_book = float(eq["currBook"]) + float(ca["currBook"])
            if prev_comb >= min_sig and curr_comb >= max(min_sig, prev_comb * 1.45):
                ratio = curr_comb / prev_comb
                mult_txt = "~2×" if 1.75 <= ratio <= 2.3 else f"+{(ratio - 1.0) * 100:.0f}%"
                parts = []
                if p_ca > 0 or c_ca > 0:
                    parts.append(f"calls {_fmt_usd_brief(p_ca)} → {_fmt_usd_brief(c_ca)}")
                if c_eq > 0:
                    parts.append(f"common {_fmt_usd_brief(c_eq)}")
                instrument_shifts.append({
                    "ticker": t,
                    "name": nm,
                    "shiftType": "MULTI_LEG_SURGE",
                    "label": f"{t}: Total exposure {mult_txt}",
                    "detail": f"Combined {_fmt_usd_brief(prev_comb)} → {_fmt_usd_brief(curr_comb)} ({' + '.join(parts)})",
                    "prevTotalUsd": round(prev_comb, 2),
                    "currTotalUsd": round(curr_comb, 2),
                    "currBookPct": round(comb_book, 2),
                })
            elif prev_comb >= max(min_sig * 2, 25_000_000.0) and curr_comb <= prev_comb * 0.55:
                comb_book = float(eq["currBook"]) + float(ca["currBook"])
                instrument_shifts.append({
                    "ticker": t,
                    "name": nm,
                    "shiftType": "MULTI_LEG_CUT",
                    "label": f"{t}: Total exposure cut {(1 - curr_comb / prev_comb) * 100:.0f}%",
                    "detail": f"Combined {_fmt_usd_brief(prev_comb)} → {_fmt_usd_brief(curr_comb)} (common + calls)",
                    "prevTotalUsd": round(prev_comb, 2),
                    "currTotalUsd": round(curr_comb, 2),
                    "currBookPct": round(comb_book, 2),
                })
        elif p_ca >= min_sig and c_ca == 0.0:
            instrument_shifts.append({
                "ticker": t,
                "name": nm,
                "shiftType": "CALL_EXIT",
                "label": f"{t} calls exited",
                "detail": f"Closed {_fmt_usd_brief(p_ca)} {prev_q_short} call position",
                "prevTotalUsd": round(p_ca, 2),
                "currTotalUsd": 0.0,
                "currBookPct": 0.0,
            })

        if c_pu >= max(20_000_000.0, 0.005 * curr_rep):
            if p_pu <= 0:
                instrument_shifts.append({
                    "ticker": t,
                    "name": nm,
                    "shiftType": "NEW_HEDGE",
                    "label": f"{t} puts: New {_fmt_usd_brief(c_pu)} hedge ({float(pu['currBook']):.1f}%)",
                    "detail": f"Initiated {_fmt_shares_brief(float(next((m['currShares'] for m in all_moves if m['ticker'] == t and m['securityType'] == 'PUT'), 0)))} put contracts",
                    "prevTotalUsd": 0.0,
                    "currTotalUsd": round(c_pu, 2),
                    "currBookPct": round(float(pu["currBook"]), 2),
                })
            elif c_pu > p_pu * 1.25 or float(pu["currBook"]) - float(pu["prevBook"]) >= 1.0:
                instrument_shifts.append({
                    "ticker": t,
                    "name": nm,
                    "shiftType": "HEDGE_SURGE",
                    "label": f"{t} puts: {_fmt_usd_brief(p_pu)} → {_fmt_usd_brief(c_pu)}",
                    "detail": f"{float(pu['prevBook']):.1f}% → {float(pu['currBook']):.1f}% of book",
                    "prevTotalUsd": round(p_pu, 2),
                    "currTotalUsd": round(c_pu, 2),
                    "currBookPct": round(float(pu["currBook"]), 2),
                })

    instrument_shifts.sort(key=lambda x: max(x["currTotalUsd"], x["prevTotalUsd"]), reverse=True)

    # Categorized top lists (prioritize Equities for standard equity buckets, fallback includes options clearly labeled)
    top_new = sorted(
        [m for m in all_moves if m["action"] == "NEW"],
        key=lambda m: (m["securityType"] == "EQUITY", m["currValueUsd"]),
        reverse=True,
    )[:8]
    top_added = sorted(
        [m for m in all_moves if m["action"] == "ADDED"],
        key=lambda m: (
            m["securityType"] == "EQUITY",
            max(m["valueDeltaUsd"], 0.0) * (1.35 if (m["sharesDeltaPct"] or 0) >= 100 else 1.0),
        ),
        reverse=True,
    )[:8]
    top_trimmed = sorted(
        [m for m in all_moves if m["action"] == "TRIMMED"],
        key=lambda m: abs(m["valueDeltaUsd"]),
        reverse=True,
    )[:8]
    top_exited = sorted(
        [m for m in all_moves if m["action"] == "EXITED"],
        key=lambda m: m["prevValueUsd"],
        reverse=True,
    )[:8]

    curr_eq_book_pct = (curr_eq_book / curr_rep * 100.0) if curr_rep > 0 else 0.0
    prev_eq_book_pct = (prev_eq_book / prev_rep * 100.0) if prev_rep > 0 else 0.0
    curr_call_book_pct = (curr_call_usd / curr_rep * 100.0) if curr_rep > 0 else 0.0
    prev_call_book_pct = (prev_call_usd / prev_rep * 100.0) if prev_rep > 0 else 0.0
    curr_put_book_pct = (curr_put_usd / curr_rep * 100.0) if curr_rep > 0 else 0.0
    prev_put_book_pct = (prev_put_usd / prev_rep * 100.0) if prev_rep > 0 else 0.0
    put_book_delta_pct = curr_put_book_pct - prev_put_book_pct

    total_val_delta = curr_rep - prev_rep
    total_val_delta_pct = ((curr_rep / prev_rep) - 1.0) * 100.0 if prev_rep > 0 else None
    activity_ratio = (gross_activity / max(curr_rep, prev_rep) * 100.0) if max(curr_rep, prev_rep) > 0 else 0.0
    has_sig_hedge = curr_put_book_pct >= 1.0 or prev_put_book_pct >= 1.0 or curr_put_usd >= 20_000_000.0

    # Synthesize grounded, scannable Apple-editorial narrative sections
    fund_short = cand["name"]
    dir_word = "up" if total_val_delta >= 0 else "down"
    delta_pct_str = f"{total_val_delta_pct:+.1f}%" if total_val_delta_pct is not None else ""
    narrative_lead = (
        f"{fund_short} filed {dl_info['deadlinePhrase']} — {q_label} 13F "
        f"(ended {_fmt_period_short(curr_fl['reportDate'])}, accession {acc}), "
        f"{_fmt_usd_brief(curr_rep)} across {len(curr_agg)} positions, "
        f"{dir_word} from {_fmt_usd_brief(prev_rep)} / {len(prev_agg)} in {prev_q_short}."
    )

    if has_sig_hedge and abs(put_book_delta_pct) >= 3.0:
        narrative_headline = (
            f"Book {delta_pct_str} to {_fmt_usd_brief(curr_rep)}; put hedges shifted "
            f"{prev_put_book_pct:.1f}% → {curr_put_book_pct:.1f}%"
        )
    elif top_new and top_new[0]["currBookPct"] >= 3.0:
        m0 = top_new[0]
        kind = f" {m0['securityType'].lower()}s" if m0["securityType"] != "EQUITY" else ""
        narrative_headline = (
            f"Book {delta_pct_str} to {_fmt_usd_brief(curr_rep)}; led by new {m0['ticker']}{kind} ({m0['currBookPct']:.1f}%)"
        )
    elif valuation_shifts:
        vs0 = valuation_shifts[0]
        narrative_headline = (
            f"Book {delta_pct_str} to {_fmt_usd_brief(curr_rep)}; {vs0['ticker']} "
            f"{'marked down' if vs0['valuationShiftType'] == 'MARKDOWN' else 'marked up'} "
            f"{vs0['prevBookPct']:.1f}% → {vs0['currBookPct']:.1f}%"
        )
    else:
        narrative_headline = (
            f"Book {delta_pct_str} to {_fmt_usd_brief(curr_rep)} across {len(curr_agg)} positions "
            f"({new_cnt} new, {exited_cnt} exited)"
        )

    focus_notes: list[str] = []
    # Highlight up to 1 top Exit, 1-2 Heavy Adds, 1 Trim, 1 New
    if top_exited:
        ex0 = top_exited[0]
        focus_notes.append(
            f"{ex0['ticker']} ({ex0['securityType'].lower()}) — exited entirely. "
            f"Prior {_fmt_shares_brief(ex0['prevShares'])} sh / {_fmt_usd_brief(ex0['prevValueUsd'])} ({ex0['prevBookPct']:.1f}%) is gone from {q_label[-2:]}."
        )
    added_eq = [m for m in top_added if m["securityType"] == "EQUITY"]
    for ad in added_eq[:2]:
        pct_s = f"+{ad['sharesDeltaPct']:,.0f}%" if (ad["sharesDeltaPct"] or 0) >= 100 else f"+{ad['sharesDeltaPct']:.1f}%"
        focus_notes.append(
            f"{ad['ticker']} — added heavily: {_fmt_shares_brief(ad['prevShares'])} → {_fmt_shares_brief(ad['currShares'])} sh ({pct_s}), "
            f"{_fmt_usd_brief(ad['prevValueUsd'])} → {_fmt_usd_brief(ad['currValueUsd'])} ({ad['currBookPct']:.2f}% of book)."
        )
    if top_trimmed:
        tr0 = top_trimmed[0]
        focus_notes.append(
            f"{tr0['ticker']} — trimmed {abs(tr0['sharesDeltaPct'] or 0):.1f}%: "
            f"{_fmt_shares_brief(tr0['prevShares'])} → {_fmt_shares_brief(tr0['currShares'])} sh, "
            f"{_fmt_usd_brief(tr0['prevValueUsd'])} → {_fmt_usd_brief(tr0['currValueUsd'])}."
        )
    new_eq = [m for m in top_new if m["securityType"] == "EQUITY"]
    if new_eq:
        nw0 = new_eq[0]
        focus_notes.append(
            f"{nw0['ticker']} — new initiation: {_fmt_shares_brief(nw0['currShares'])} sh / "
            f"{_fmt_usd_brief(nw0['currValueUsd'])} ({nw0['currBookPct']:.2f}% of book)."
        )

    hedge_parts: list[str] = []
    if has_sig_hedge and put_legs:
        leg_strs = []
        for pl in put_legs[:2]:
            if pl["action"] == "NEW":
                leg_strs.append(f"new {pl['ticker']} puts of {_fmt_usd_brief(pl['currValueUsd'])} ({pl['currBookPct']:.1f}%)")
            elif pl["action"] == "EXITED":
                leg_strs.append(f"exited {pl['ticker']} puts ({_fmt_usd_brief(pl['prevValueUsd'])})")
            else:
                leg_strs.append(
                    f"{pl['ticker']} puts {_fmt_usd_brief(pl['prevValueUsd'])} → {_fmt_usd_brief(pl['currValueUsd'])} "
                    f"({pl['prevBookPct']:.1f}% → {pl['currBookPct']:.1f}%)"
                )
        hedge_parts.append(
            f"Hedging & Puts: {'; '.join(leg_strs)}. Combined puts sit at {curr_put_book_pct:.1f}% of the book "
            f"(vs {prev_put_book_pct:.1f}% in {prev_q_short})."
        )
    if valuation_shifts:
        vs = valuation_shifts[0]
        kind_w = "markdown, not a sale" if vs["valuationShiftType"] == "MARKDOWN" else "markup, not a buy"
        hedge_parts.append(
            f"{vs['ticker']} shifted {vs['prevBookPct']:.1f}% → {vs['currBookPct']:.1f}% on flat shares "
            f"({_fmt_shares_brief(vs['currShares'])} sh) — a {kind_w}."
        )
    narrative_hedge = " ".join(hedge_parts) if hedge_parts else None

    other_bits: list[str] = []
    non_hedge_shifts = [s for s in instrument_shifts if s["shiftType"] not in ("NEW_HEDGE", "HEDGE_SURGE")]
    for sh_sig in non_hedge_shifts[:3]:
        other_bits.append(sh_sig["label"])
    extra_new = [m for m in top_new if m["securityType"] != "EQUITY" or m not in new_eq[:1]][:4]
    if extra_new:
        lbls = [
            f"{m['ticker']}{' ' + m['securityType'].lower() + 's' if m['securityType'] != 'EQUITY' else ''} ({_fmt_usd_brief(m['currValueUsd'])})"
            for m in extra_new
        ]
        other_bits.append(f"New names: {', '.join(lbls)}")
    narrative_other = "; ".join(other_bits) + "." if other_bits else None

    return {
        "cik": cik,
        "fundName": fund_short,
        "legalName": curr_fl["entityName"],
        "manager": cand["manager"],
        "styleTag": cand["styleTag"],
        "source": cand.get("source", "Curated Core"),
        "inRankings": in_rankings,
        "isRankedTop75AnyWindow": bool(ranked_windows),
        "rankedWindows": ranked_windows,
        "quarterLabel": q_label,
        "periodEnd": curr_fl["reportDate"],
        "prevQuarterLabel": prev_q_label,
        "prevPeriodEnd": prev_fl["reportDate"],
        "filingDate": curr_fl["filingDate"],
        "prevFilingDate": prev_fl["filingDate"],
        "statutoryDeadline": dl_info["statutoryDeadline"],
        "daysBeforeDeadline": dl_info["daysBeforeDeadline"],
        "deadlineBadge": dl_info["deadlineBadge"],
        "accessionNumber": acc,
        "filingUrl": filing_url,
        "edgarUrl": f"https://www.sec.gov/edgar/browse/?CIK={int(cik):010d}",
        "currTotalReportedUsd": round(curr_rep, 2),
        "prevTotalReportedUsd": round(prev_rep, 2),
        "totalValueDeltaUsd": round(total_val_delta, 2),
        "totalValueDeltaPct": clean_num(total_val_delta_pct, 1),
        "currEquityAumUsd": round(curr_eq_aum, 2),
        "prevEquityAumUsd": round(prev_eq_aum, 2),
        "currPositionCount": len(curr_agg),
        "prevPositionCount": len(prev_agg),
        "currEquityBookPct": round(curr_eq_book_pct, 1),
        "prevEquityBookPct": round(prev_eq_book_pct, 1),
        "currCallUsd": round(curr_call_usd, 2),
        "prevCallUsd": round(prev_call_usd, 2),
        "currCallBookPct": round(curr_call_book_pct, 1),
        "prevCallBookPct": round(prev_call_book_pct, 1),
        "currPutUsd": round(curr_put_usd, 2),
        "prevPutUsd": round(prev_put_usd, 2),
        "currPutBookPct": round(curr_put_book_pct, 1),
        "prevPutBookPct": round(prev_put_book_pct, 1),
        "putBookDeltaPct": round(put_book_delta_pct, 1),
        "hasSignificantHedge": has_sig_hedge,
        "newCount": new_cnt,
        "addedCount": added_cnt,
        "trimmedCount": trimmed_cnt,
        "exitedCount": exited_cnt,
        "unchangedCount": unchanged_cnt,
        "grossActivityUsd": round(gross_activity, 2),
        "activityRatioPct": round(activity_ratio, 1),
        "narrativeHeadline": narrative_headline,
        "narrativeLead": narrative_lead,
        "narrativeFocusNotes": focus_notes,
        "narrativeHedgeAndStructure": narrative_hedge,
        "narrativeOtherMoves": narrative_other,
        "putLegs": put_legs,
        "callLegs": call_legs,
        "instrumentShifts": instrument_shifts,
        "valuationShifts": valuation_shifts,
        "topNew": top_new,
        "topAdded": top_added,
        "topTrimmed": top_trimmed,
        "topExited": top_exited,
        "allMoves": all_moves,
    }


def build_updates_overview(fund_updates: list[dict[str, Any]], total_eligible: int) -> dict[str, Any]:
    if not fund_updates:
        return {
            "latestQuarterLabel": "2026 Q3",
            "latestQuarterPeriodEnd": "2026-09-30",
            "totalUpdateCards": 0,
            "latestQuarterFilersCount": 0,
            "totalEligibleFundsCount": total_eligible,
            "deadlineDayFilersCount": 0,
            "fundsWithPutHedgesCount": 0,
            "totalPutNotionalUsd": 0.0,
            "totalCallNotionalUsd": 0.0,
            "totalLatestBookUsd": 0.0,
            "consensusNewInitiations": [],
            "consensusHeavyAdds": [],
            "consensusExitsAndTrims": [],
            "consensusOptionMoves": [],
        }

    latest_q = max(u["quarterLabel"] for u in fund_updates)
    latest_pe = max(u["periodEnd"] for u in fund_updates if u["quarterLabel"] == latest_q)
    latest_pool = [u for u in fund_updates if u["quarterLabel"] == latest_q]

    deadline_cnt = sum(1 for u in latest_pool if u["deadlineBadge"] == "Deadline Day")
    hedge_cnt = sum(1 for u in latest_pool if u["hasSignificantHedge"])
    tot_puts = sum(u["currPutUsd"] for u in latest_pool)
    tot_calls = sum(u["currCallUsd"] for u in latest_pool)
    tot_book = sum(u["currTotalReportedUsd"] for u in latest_pool)

    def _agg_consensus(
        actions: tuple[str, ...],
        sec_types: tuple[str, ...],
        min_funds: int = 2,
        limit: int = 10,
        sort_by_abs_delta: bool = False,
    ) -> list[dict[str, Any]]:
        buckets: dict[str, dict[str, Any]] = {}
        for u in latest_pool:
            for mv in u["allMoves"]:
                if mv["action"] not in actions or mv["securityType"] not in sec_types:
                    continue
                t = mv["ticker"]
                if t.startswith("CUSIP:"):
                    continue
                if t not in buckets:
                    buckets[t] = {
                        "ticker": t,
                        "name": mv["name"],
                        "sector": mv["sector"],
                        "seenCiks": set(),
                        "totalCurrValueUsd": 0.0,
                        "totalDeltaUsd": 0.0,
                        "funds": [],
                    }
                b = buckets[t]
                b["totalCurrValueUsd"] += mv["currValueUsd"]
                b["totalDeltaUsd"] += mv["valueDeltaUsd"]
                b["seenCiks"].add(u["cik"])
                b["funds"].append({
                    "cik": u["cik"],
                    "fundName": u["fundName"],
                    "action": mv["action"],
                    "securityType": mv["securityType"],
                    "currBookPct": mv["currBookPct"],
                    "prevBookPct": mv["prevBookPct"],
                    "valueDeltaUsd": mv["valueDeltaUsd"],
                    "sharesDeltaPct": mv["sharesDeltaPct"],
                })
        out: list[dict[str, Any]] = []
        for b in buckets.values():
            fcnt = len(b.pop("seenCiks"))
            if fcnt < min_funds:
                continue
            b["fundCount"] = fcnt
            b["totalCurrValueUsd"] = round(b["totalCurrValueUsd"], 2)
            b["totalDeltaUsd"] = round(b["totalDeltaUsd"], 2)
            b["funds"].sort(key=lambda x: abs(x["valueDeltaUsd"]), reverse=True)
            out.append(b)
        out.sort(
            key=lambda x: (
                x["fundCount"],
                abs(x["totalDeltaUsd"]) if sort_by_abs_delta else max(x["totalCurrValueUsd"], abs(x["totalDeltaUsd"])),
            ),
            reverse=True,
        )
        return out[:limit]

    return {
        "latestQuarterLabel": latest_q,
        "latestQuarterPeriodEnd": latest_pe,
        "totalUpdateCards": len(fund_updates),
        "latestQuarterFilersCount": len(latest_pool),
        "totalEligibleFundsCount": total_eligible,
        "deadlineDayFilersCount": deadline_cnt,
        "fundsWithPutHedgesCount": hedge_cnt,
        "totalPutNotionalUsd": round(tot_puts, 2),
        "totalCallNotionalUsd": round(tot_calls, 2),
        "totalLatestBookUsd": round(tot_book, 2),
        "consensusNewInitiations": _agg_consensus(("NEW",), ("EQUITY",), min_funds=2, limit=12),
        "consensusHeavyAdds": _agg_consensus(("ADDED",), ("EQUITY",), min_funds=2, limit=12, sort_by_abs_delta=True),
        "consensusExitsAndTrims": _agg_consensus(("EXITED", "TRIMMED"), ("EQUITY",), min_funds=2, limit=12, sort_by_abs_delta=True),
        "consensusOptionMoves": _agg_consensus(("NEW", "ADDED", "TRIMMED", "EXITED"), ("CALL", "PUT"), min_funds=1, limit=12, sort_by_abs_delta=True),
    }


def _merge_period_filings(period_filings: list[dict[str, Any]]) -> dict[str, Any] | None:
    parsed_group: list[dict[str, Any]] = []
    for f in sorted(period_filings, key=lambda x: (x["filingDate"], x["accessionNumber"])):
        parsed = parse_13f_filing(f)
        if parsed:
            parsed_group.append(parsed)
    if not parsed_group:
        return None
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    meta: dict[str, Any] | None = None
    for parsed in parsed_group:
        if parsed["form"] == "13F-HR" or parsed["amendmentType"] == "RESTATEMENT" or meta is None:
            merged = {(r["cusip"], r["secType"]): dict(r) for r in parsed["rows"]}
            meta = {
                "cik": parsed["cik"],
                "entityName": parsed["entityName"],
                "reportDate": parsed["reportDate"],
                "filingDate": parsed["filingDate"],
                "accessionNumber": parsed["accessionNumber"],
            }
        else:
            for r in parsed["rows"]:
                k = (r["cusip"], r["secType"])
                if k not in merged:
                    merged[k] = dict(r)
                else:
                    merged[k]["valueUsd"] += r["valueUsd"]
                    merged[k]["shares"] += r["shares"]
    if meta and merged:
        meta["rows"] = sorted(merged.values(), key=lambda x: x["valueUsd"], reverse=True)
        return meta
    return None


def main() -> None:
    print("=== Paper Trail 13F Pipeline Starting ===")
    known_ciks = {f["cik"] for f in CURATED_FUNDS}
    print(f"[universe] Curated funds: {len(CURATED_FUNDS)}. Scanning EDGAR for up to 1,000 emerging tech funds...")
    discovered = discover_emerging_tech_funds(known_ciks, max_candidates=1000)
    print(f"[universe] Discovered {len(discovered)} pre-screened candidate funds via SEC EDGAR EFTS.")

    all_candidates: list[dict[str, Any]] = []
    for f in CURATED_FUNDS:
        item = dict(f)
        item["source"] = "Curated Core"
        all_candidates.append(item)
    all_candidates.extend(discovered)

    today_date = datetime.now(timezone.utc).date()

    # Stage 1: Concurrent Latest-Filing Ingestion & Size / Concentration Pre-Screening
    print(f"[stage-1] Inspecting latest 13F filings across {len(all_candidates)} total candidates (8 workers)...", flush=True)
    stage1_results: dict[str, dict[str, Any]] = {}
    fetch_errors: list[tuple[str, str]] = []

    def _stage1_one(cand: dict[str, Any]) -> tuple[str, dict[str, Any] | None, str | None]:
        cik = cand["cik"]
        is_curated = cand.get("source") == "Curated Core"
        try:
            raw_list = list_13f_filings(cik, min_report_date="2020-12-31")
            if not raw_list:
                return cik, None, "no valid 13F holdings found"
            by_period: dict[str, list[dict[str, Any]]] = {}
            for fl in raw_list:
                by_period.setdefault(fl["reportDate"], []).append(fl)
            periods_desc = sorted(by_period.keys(), reverse=True)
            latest_raw_rd = periods_desc[0]

            latest_filing = None
            for rd in periods_desc[:3]:
                latest_filing = _merge_period_filings(by_period[rd])
                if latest_filing:
                    break
            if not latest_filing:
                return cik, None, "no valid 13F holdings found"

            latest_rd = latest_filing["reportDate"]
            if latest_raw_rd > latest_rd and (
                datetime.strptime(latest_raw_rd, "%Y-%m-%d").date() - datetime.strptime(latest_rd, "%Y-%m-%d").date()
            ).days > 100:
                return cik, None, f"latest filing {latest_raw_rd} reported $0/no holdings (last populated {latest_rd})"

            age_days = (today_date - datetime.strptime(latest_rd, "%Y-%m-%d").date()).days
            if age_days > 210:
                return cik, None, f"stale latest period {latest_rd} ({age_days}d old)"

            rep_aum = sum(r["valueUsd"] for r in latest_filing["rows"])
            eq_rows = [r for r in latest_filing["rows"] if r["secType"] == "EQUITY" and r["valueUsd"] > 0]
            eq_aum = sum(r["valueUsd"] for r in eq_rows)
            raw_qtrs = len(periods_desc)

            qual_ok = raw_qtrs >= 4 and rep_aum >= 200_000_000 and eq_aum >= 100_000_000
            update_only_ok = is_curated and raw_qtrs >= 2 and rep_aum >= 200_000_000
            if not (qual_ok or update_only_ok):
                return cik, None, f"below $200M/$100M/qtrs gate (rep=${rep_aum/1e6:.0f}M eq=${eq_aum/1e6:.0f}M qtrs={raw_qtrs})"

            # Safe concentration pre-filter for non-curated funds before CUSIP ticker merging
            if not is_curated and eq_aum > 0:
                raw_by_c: dict[str, float] = {}
                for r in eq_rows:
                    raw_by_c[r["cusip"]] = raw_by_c.get(r["cusip"], 0.0) + r["valueUsd"]
                raw_pos = len(raw_by_c)
                raw_top10 = sum(sorted(raw_by_c.values(), reverse=True)[:10]) / eq_aum * 100.0
                if not (raw_top10 >= 30.0 or raw_pos <= 85):
                    return cik, None, f"unconcentrated (rawPos={raw_pos}, rawTop10={raw_top10:.1f}%)"

            return cik, {
                "cand": cand,
                "latest": latest_filing,
                "eqRows": eq_rows,
                "eqAum": eq_aum,
                "repAum": rep_aum,
                "qualOk": qual_ok,
                "updateOnlyOk": update_only_ok and not qual_ok,
            }, None
        except Exception as e:
            return cik, None, f"ERROR: {e}"

    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = [ex.submit(_stage1_one, c) for c in all_candidates]
        for done_cnt, fut in enumerate(as_completed(futs), 1):
            cik, res, reason = fut.result()
            if res is not None:
                stage1_results[cik] = res
            elif reason and reason.startswith("ERROR:"):
                cand_nm = next((c["name"] for c in all_candidates if c["cik"] == cik), cik)
                fetch_errors.append((f"{cand_nm} ({cik})", reason))
            if done_cnt % 150 == 0 or done_cnt == len(all_candidates):
                print(f"  [stage-1] {done_cnt}/{len(all_candidates)} inspected ({len(stage1_results)} passed size/recency gate)", flush=True)

    if len(stage1_results) < 25:
        sample = "; ".join(f"{k}: {v}" for k, v in fetch_errors[:3])
        raise RuntimeError(
            f"Aborting build_data.py before modifying output: only {len(stage1_results)} candidates passed Stage 1 "
            f"({len(fetch_errors)} fetch errors). Sample errors: {sample}"
        )

    # Stage 1b: Mathematical Tech/TMT >= 20% bound check + Material & Borderline CUSIP resolution
    def _build_cached_lookup() -> tuple[dict[str, Any], dict[str, Any]]:
        cc = load_json_dict(CUSIP_CACHE_PATH)
        tm = load_json_dict(TICKER_META_PATH)
        return cc, tm

    def _cached_tech_state(cusip: str, cc: dict[str, Any], tm: dict[str, Any]) -> bool | None:
        if cusip in MANUAL_CUSIP_OVERRIDES:
            ov = MANUAL_CUSIP_OVERRIDES[cusip]
            return is_tech_tmt(ov["ticker"], ov.get("sector", ""), ov.get("industry", ""))
        entry = cc.get(cusip)
        if not entry or not entry.get("ticker"):
            return None
        t = entry["ticker"]
        if t.startswith("CUSIP:"):
            return False
        tmeta = tm.get(t, {})
        sec = tmeta.get("sector") or entry.get("sector", "")
        ind = tmeta.get("industry") or entry.get("industry", "")
        if is_tech_tmt(t, sec, ind):
            return True
        if sec or tmeta.get("metaChecked"):
            return False
        return None

    cc, tm = _build_cached_lookup()
    pre_prune_ciks: list[str] = []
    material_stage1_cusips: dict[str, str] = {}

    for cand in all_candidates:
        cik = cand["cik"]
        info = stage1_results.get(cik)
        if not info:
            continue
        is_curated = cand.get("source") == "Curated Core"
        eq_aum = info["eqAum"]
        eq_rows = info["eqRows"]

        known_non_tech = 0.0
        if eq_aum > 0:
            for r in eq_rows:
                if _cached_tech_state(r["cusip"], cc, tm) is False:
                    known_non_tech += r["valueUsd"]
        max_possible_tech = (1.0 - known_non_tech / eq_aum) * 100.0 if eq_aum > 0 else 0.0
        if not is_curated and max_possible_tech < 20.0:
            continue

        pre_prune_ciks.append(cik)
        for idx_r, r in enumerate(eq_rows):
            if is_curated or idx_r < 40 or (eq_aum > 0 and r["valueUsd"] / eq_aum >= 0.004):
                c_id = r["cusip"]
                if c_id not in material_stage1_cusips:
                    material_stage1_cusips[c_id] = r["issuer"]

    print(f"[stage-1b] {len(pre_prune_ciks)} candidates cleared mathematical Tech/TMT ceiling; resolving {len(material_stage1_cusips)} latest-filing CUSIPs...", flush=True)
    if material_stage1_cusips:
        resolve_cusips(material_stage1_cusips)

    cc, tm = _build_cached_lookup()
    borderline_cusips: dict[str, str] = {}
    stage1_survivors_raw: list[dict[str, Any]] = []

    for cik in pre_prune_ciks:
        info = stage1_results[cik]
        cand = info["cand"]
        is_curated = cand.get("source") == "Curated Core"
        eq_aum = info["eqAum"]
        eq_rows = info["eqRows"]

        if is_curated:
            stage1_survivors_raw.append(info)
            continue

        known_tech = 0.0
        known_non_tech = 0.0
        for r in eq_rows:
            st = _cached_tech_state(r["cusip"], cc, tm)
            if st is True:
                known_tech += r["valueUsd"]
            elif st is False:
                known_non_tech += r["valueUsd"]
        min_tech = (known_tech / eq_aum) * 100.0 if eq_aum > 0 else 0.0
        max_tech = (1.0 - known_non_tech / eq_aum) * 100.0 if eq_aum > 0 else 0.0
        if max_tech < 20.0:
            continue
        stage1_survivors_raw.append(info)
        if min_tech < 20.0:
            for r in eq_rows:
                c_id = r["cusip"]
                if _cached_tech_state(c_id, cc, tm) is None and c_id not in borderline_cusips:
                    borderline_cusips[c_id] = r["issuer"]

    if borderline_cusips:
        print(f"[stage-1b] Resolving {len(borderline_cusips)} tail equity CUSIPs for borderline Tech/TMT >= 20% funds...", flush=True)
        resolve_cusips(borderline_cusips)

    cc, tm = _build_cached_lookup()
    stage2_targets: list[dict[str, Any]] = []
    for info in stage1_survivors_raw:
        cand = info["cand"]
        is_curated = cand.get("source") == "Curated Core"
        if is_curated:
            stage2_targets.append(cand)
            continue
        eq_aum = info["eqAum"]
        eq_rows = info["eqRows"]
        tech_val = sum(
            r["valueUsd"]
            for r in eq_rows
            if _cached_tech_state(r["cusip"], cc, tm) is True
        )
        exact_tech_pct = (tech_val / eq_aum * 100.0) if eq_aum > 0 else 0.0
        if exact_tech_pct >= 20.0:
            stage2_targets.append(cand)

    # Stage 2: Concurrent Historical 13F Ingestion for Qualified & Curated Candidates
    print(f"\n[stage-2] Fetching full historical 13F filings (2021-present) for {len(stage2_targets)} surviving funds (8 workers)...", flush=True)
    raw_fund_filings: dict[str, list[dict[str, Any]]] = {}
    all_cusips: dict[str, str] = {}

    def _stage2_one(cand: dict[str, Any]) -> tuple[str, list[dict[str, Any]] | None, str | None]:
        cik = cand["cik"]
        try:
            raw_list = list_13f_filings(cik, min_report_date="2020-12-31")
            filings = build_clean_quarterly_filings(cik, min_report_date="2020-12-31")
            if not filings:
                return cik, None, "no valid 13F holdings found"
            latest_rd = filings[-1]["reportDate"]
            latest_raw_rd = raw_list[-1]["reportDate"] if raw_list else latest_rd
            if latest_raw_rd > latest_rd and (
                datetime.strptime(latest_raw_rd, "%Y-%m-%d").date() - datetime.strptime(latest_rd, "%Y-%m-%d").date()
            ).days > 100:
                return cik, None, f"latest filing {latest_raw_rd} reported $0/no holdings (last populated {latest_rd})"
            age_days = (today_date - datetime.strptime(latest_rd, "%Y-%m-%d").date()).days
            if age_days > 210:
                return cik, None, f"stale latest period {latest_rd} ({age_days}d old)"
            return cik, filings, None
        except Exception as e:
            return cik, None, str(e)

    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = [ex.submit(_stage2_one, c) for c in stage2_targets]
        for done_cnt, fut in enumerate(as_completed(futs), 1):
            cik, filings, err_msg = fut.result()
            cand_nm = stage1_results[cik]["cand"]["name"] if cik in stage1_results else cik
            if filings:
                raw_fund_filings[cik] = filings
                for idx_fl, fl in enumerate(filings):
                    is_recent_q = idx_fl >= len(filings) - 2
                    for row in fl["rows"]:
                        if row["secType"] == "EQUITY" or is_recent_q:
                            if row["cusip"] not in all_cusips:
                                all_cusips[row["cusip"]] = row["issuer"]
            elif err_msg:
                fetch_errors.append((f"{cand_nm} ({cik})", err_msg))
            if done_cnt % 50 == 0 or done_cnt == len(stage2_targets):
                print(f"  [stage-2] {done_cnt}/{len(stage2_targets)} funds fetched ({len(raw_fund_filings)} valid histories)", flush=True)

    if len(raw_fund_filings) < 25:
        sample = "; ".join(f"{k}: {v}" for k, v in fetch_errors[:3])
        raise RuntimeError(
            f"Aborting build_data.py before modifying output: only {len(raw_fund_filings)} funds ingested "
            f"({len(fetch_errors)} fetch errors). Sample errors: {sample}"
        )

    # Step 2: Resolve all historical equity + recent option CUSIPs -> Tickers, Sectors, Industries
    cusip_meta = resolve_cusips(all_cusips)

    # Step 3: Exact Tech-Focused & Concentrated qualification audit on each fund's latest filing
    qualified_cands: list[dict[str, Any]] = []
    update_cands: list[dict[str, Any]] = []
    needed_tickers: set[str] = {"QQQ", "SPY", "XLK"}

    print("\n=== Fund Qualification Audit (Tech/TMT >= 20%, $200M+ Reported / $100M+ Equity, Top10 >= 35% or Pos <= 75) ===")
    for cand in stage2_targets:
        cik = cand["cik"]
        if cik not in raw_fund_filings:
            continue
        filings = raw_fund_filings[cik]
        latest = filings[-1]
        weights, eq_aum, rep_aum, pos_cnt, top10_pct, tech_pct = extract_equity_weights(latest, cusip_meta)

        is_tech = tech_pct >= 20.0
        is_conc = top10_pct >= 35.0 or pos_cnt <= 75
        is_size = rep_aum >= 200_000_000 and eq_aum >= 100_000_000
        # Require at least 4 quarters of filings so TTM works in Rankings
        has_hist = len(filings) >= 4

        status_str = "PASS" if (is_tech and is_conc and is_size and has_hist) else "FILTERED"
        if cand.get("source") == "Curated Core" or status_str == "PASS":
            print(
                f"  {status_str:8s} | {cand['name'][:28]:28s} | {cand.get('source', '')[:7]:7s} | "
                f"Eq=${eq_aum/1e9:6.2f}B | Rep=${rep_aum/1e9:6.2f}B | "
                f"pos={pos_cnt:3d} | top10={top10_pct:5.1f}% | techTMT={tech_pct:5.1f}% | qtrs={len(filings):2d}"
            )
        if status_str == "PASS":
            qualified_cands.append(cand)
            update_cands.append(cand)
            for fl in filings:
                for row in fl["rows"]:
                    if row["secType"] == "EQUITY":
                        t = cusip_meta.get(row["cusip"], {}).get("ticker", "")
                        if t and not t.startswith("CUSIP:"):
                            needed_tickers.add(t)
        elif cand.get("source") == "Curated Core" and len(filings) >= 2 and rep_aum >= 200_000_000:
            # Include emerging / option-heavy Curated funds in Quarterly Updates feed
            update_cands.append(cand)

    # Step 5: Fetch historical daily adjusted prices
    raw_prices = fetch_all_prices(sorted(needed_tickers))
    qqq_dict = raw_prices.get("QQQ") or {}
    cal_dates = sorted(qqq_dict.keys())
    if len(cal_dates) < 252:
        raise RuntimeError("Insufficient QQQ trading calendar dates fetched from Yahoo Finance.")

    aligned_prices = build_aligned_price_matrix(cal_dates, raw_prices)
    n_dates = len(cal_dates)
    win_indices = compute_window_start_indices(cal_dates)
    as_of_date = cal_dates[-1]
    print(f"\n[calendar] Trading days: {len(cal_dates)} ({cal_dates[0]} .. {as_of_date})")
    print(f"[windows]  { {w: cal_dates[idx] for w, idx in win_indices.items()} }")

    qqq_nav = aligned_prices["QQQ"][0] / aligned_prices["QQQ"][0][0] * 100.0
    spy_nav = aligned_prices["SPY"][0] / aligned_prices["SPY"][0][0] * 100.0
    xlk_nav = aligned_prices["XLK"][0] / aligned_prices["XLK"][0][0] * 100.0

    benchmarks = {}
    for b_sym, b_name, b_nav in [
        ("QQQ", "Invesco QQQ Trust (Nasdaq-100)", qqq_nav),
        ("SPY", "SPDR S&P 500 ETF Trust", spy_nav),
        ("XLK", "Technology Select Sector SPDR", xlk_nav),
    ]:
        b_rets, b_met = compute_returns_and_metrics(b_nav, qqq_nav, win_indices)
        benchmarks[b_sym] = {
            "ticker": b_sym,
            "name": b_name,
            "returns": b_rets,
            "cagrPct": b_met["cagrPct"],
            "maxDrawdownPct": b_met["maxDrawdownPct"],
            "sharpeProxy": b_met["sharpeProxy"],
            "curve": downsample_curve(cal_dates, b_nav, step=5),
        }

    # Step 6: Simulate every qualified fund across both modes
    funds_out: list[dict[str, Any]] = []
    fund_navs: dict[str, dict[str, np.ndarray]] = {"quarter_end": {}, "filing_lag": {}}
    fund_equity_weights: dict[str, dict[str, float]] = {}

    for cand in qualified_cands:
        cik = cand["cik"]
        filings = raw_fund_filings[cik]
        latest = filings[-1]
        prev = filings[-2] if len(filings) >= 2 else None

        qe_segments: list[tuple[int, dict[str, float]]] = []
        lag_segments: list[tuple[int, dict[str, float]]] = []
        quarterly_hist: list[dict[str, Any]] = []

        for fl in filings:
            weights, eq_aum, rep_aum, pos_cnt, top10_pct, tech_pct = extract_equity_weights(fl, cusip_meta)
            if not weights:
                continue
            qe_idx = find_idx_on_or_after(cal_dates, fl["reportDate"])
            if qe_idx is None:
                qe_idx = n_dates - 1
            if not qe_segments or qe_idx > qe_segments[-1][0]:
                qe_segments.append((qe_idx, weights))
            elif qe_idx == qe_segments[-1][0]:
                qe_segments[-1] = (qe_idx, weights)

            # Filing lag date (actual filingDate, or capped at reportDate + 47 days if late anomaly)
            fd_dt = datetime.strptime(fl["filingDate"], "%Y-%m-%d")
            rd_dt = datetime.strptime(fl["reportDate"], "%Y-%m-%d")
            eff_dt = min(fd_dt, rd_dt + timedelta(days=47)).strftime("%Y-%m-%d")
            lag_idx = find_idx_on_or_after(cal_dates, eff_dt)
            if lag_idx is None:
                lag_idx = n_dates - 1
            if not lag_segments or lag_idx > lag_segments[-1][0]:
                lag_segments.append((lag_idx, weights))
            elif lag_idx == lag_segments[-1][0]:
                lag_segments[-1] = (lag_idx, weights)

            quarterly_hist.append({
                "quarter": quarter_label(fl["reportDate"]),
                "periodEnd": fl["reportDate"],
                "filingDate": fl["filingDate"],
                "equityAumUsd": round(eq_aum, 2),
                "positionCount": pos_cnt,
                "top10WeightPct": round(top10_pct, 2),
                "techTmtPct": round(tech_pct, 2),
                "qeIdx": qe_idx,
            })

        nav_qe = simulate_nav_series(qe_segments, aligned_prices, n_dates)
        nav_lag = simulate_nav_series(lag_segments, aligned_prices, n_dates)
        fund_navs["quarter_end"][cik] = nav_qe
        fund_navs["filing_lag"][cik] = nav_lag

        # Compute quarter-by-quarter returns in quarterlyHistory
        for i, qrow in enumerate(quarterly_hist):
            s_i = qrow.pop("qeIdx")
            e_i = find_idx_on_or_after(cal_dates, quarterly_hist[i + 1]["periodEnd"]) if i + 1 < len(quarterly_hist) else (n_dates - 1)
            if e_i is None:
                e_i = n_dates - 1
            if e_i > s_i and not math.isnan(nav_qe[s_i]) and not math.isnan(nav_qe[e_i]):
                qrow["quarterReturnPct"] = clean_num(((nav_qe[e_i] / nav_qe[s_i]) - 1.0) * 100.0, 2)
                qrow["qqqQuarterReturnPct"] = clean_num(((qqq_nav[e_i] / qqq_nav[s_i]) - 1.0) * 100.0, 2)
            else:
                qrow["quarterReturnPct"] = None
                qrow["qqqQuarterReturnPct"] = None

        rets_qe, met_qe = compute_returns_and_metrics(nav_qe, qqq_nav, win_indices)
        rets_lag, met_lag = compute_returns_and_metrics(nav_lag, qqq_nav, win_indices)

        # Latest holdings table (all rows: Equity + Options + Notes)
        lat_weights, eq_aum, rep_aum, pos_cnt, top10_pct, tech_pct = extract_equity_weights(latest, cusip_meta)

        # Previous quarter aggregated shares/weights across all security types (Equity + Options + Notes)
        prev_agg_rows: dict[tuple[str, str], dict[str, Any]] = {}
        prev_eq_weights: dict[str, float] = {}
        prev_rep_aum = 0.0
        if prev:
            p_weights, _, _, _, _, _ = extract_equity_weights(prev, cusip_meta)
            prev_eq_weights = p_weights
            prev_agg_rows, prev_rep_aum, _ = aggregate_filing_positions(prev, cusip_meta)

        # Aggregate latest rows by (ticker, secType) so multi-bucket rows combine cleanly
        combined_rows: dict[tuple[str, str], dict[str, Any]] = {}
        for r in latest["rows"]:
            meta = cusip_meta.get(r["cusip"], {})
            ticker = meta.get("ticker") or f"CUSIP:{r['cusip']}"
            disp_ticker = meta.get("displaySymbol") or ticker
            key = (ticker, r["secType"])
            if key not in combined_rows:
                combined_rows[key] = {
                    "cusip": r["cusip"],
                    "ticker": disp_ticker if ticker.startswith("CUSIP:") else ticker,
                    "rawTicker": ticker,
                    "name": meta.get("name") or r["issuer"],
                    "sector": meta.get("sector") or "Other",
                    "industry": meta.get("industry") or "",
                    "isTechTmt": bool(meta.get("isTechTmt")),
                    "securityType": r["secType"],
                    "shares": r["shares"],
                    "valueUsd": r["valueUsd"],
                }
            else:
                combined_rows[key]["shares"] += r["shares"]
                combined_rows[key]["valueUsd"] += r["valueUsd"]

        holdings_list: list[dict[str, Any]] = []
        sector_totals: dict[str, float] = {}
        for item in sorted(combined_rows.values(), key=lambda x: x["valueUsd"], reverse=True):
            rt = item.pop("rawTicker")
            is_eq = item["securityType"] == "EQUITY"
            w_eq = (item["valueUsd"] / eq_aum * 100.0) if (is_eq and eq_aum > 0) else 0.0
            w_rep = (item["valueUsd"] / rep_aum * 100.0) if rep_aum > 0 else 0.0

            if is_eq:
                sec = item["sector"] or "Other"
                sector_totals[sec] = sector_totals.get(sec, 0.0) + w_eq

            qoq_sh_pct = None
            qoq_w_delta = None
            status = "UNCHANGED"
            if prev:
                p_row = prev_agg_rows.get((rt, item["securityType"]))
                psh = float(p_row["shares"]) if p_row else 0.0
                if psh <= 0:
                    status = "NEW"
                else:
                    diff_pct = ((item["shares"] / psh) - 1.0) * 100.0
                    qoq_sh_pct = clean_num(diff_pct, 1)
                    if diff_pct > 2.0:
                        status = "ADDED"
                    elif diff_pct < -2.0:
                        status = "TRIMMED"
                if is_eq:
                    pw = prev_eq_weights.get(rt, 0.0) * 100.0
                    qoq_w_delta = clean_num(w_eq - pw, 2)
                else:
                    pw_rep = (float(p_row["valueUsd"]) / prev_rep_aum * 100.0) if (p_row and prev_rep_aum > 0) else 0.0
                    qoq_w_delta = clean_num(w_rep - pw_rep, 2)

            px_last = None
            if rt in aligned_prices:
                arr, _, last_i = aligned_prices[rt]
                px_last = clean_num(float(arr[last_i]), 2)

            holdings_list.append({
                **item,
                "shares": round(item["shares"]),
                "valueUsd": round(item["valueUsd"], 2),
                "weightPct": round(w_eq, 2),
                "reportedWeightPct": round(w_rep, 2),
                "qoqSharesChangePct": qoq_sh_pct,
                "qoqWeightDeltaPct": qoq_w_delta,
                "status": status,
                "pricePerShare": px_last,
            })

        # Effective positions = 1 / sum(w^2)
        hhi = sum(w * w for w in lat_weights.values())
        eff_n = (1.0 / hhi) if hhi > 0 else float(pos_cnt)

        sector_breakdown = [
            {"sector": sec, "weightPct": round(w, 2)}
            for sec, w in sorted(sector_totals.items(), key=lambda x: x[1], reverse=True)
        ]

        top_preview = [
            {"ticker": h["ticker"], "name": h["name"], "weightPct": h["weightPct"]}
            for h in holdings_list
            if h["securityType"] == "EQUITY"
        ][:4]

        funds_out.append({
            "cik": cik,
            "name": cand["name"],
            "legalName": latest["entityName"],
            "manager": cand["manager"],
            "hq": cand["hq"],
            "styleTag": cand["styleTag"],
            "strategySummary": cand["strategySummary"],
            "source": cand.get("source", "Curated Core"),
            "latestPeriod": latest["reportDate"],
            "latestQuarterLabel": quarter_label(latest["reportDate"]),
            "latestFilingDate": latest["filingDate"],
            "equityAumUsd": round(eq_aum, 2),
            "totalReportedUsd": round(rep_aum, 2),
            "equityPositionCount": pos_cnt,
            "totalPositionCount": len(holdings_list),
            "top10WeightPct": round(top10_pct, 2),
            "effectivePositions": round(eff_n, 1),
            "techTmtPct": round(tech_pct, 1),
            "quartersTracked": len(filings),
            "returns": {"quarter_end": rets_qe, "filing_lag": rets_lag},
            "metrics": {"quarter_end": met_qe, "filing_lag": met_lag},
            "topHoldingsPreview": top_preview,
            "sectorBreakdown": sector_breakdown,
            "equityCurve": {
                "quarter_end": downsample_curve(cal_dates, nav_qe, step=5),
                "filing_lag": downsample_curve(cal_dates, nav_lag, step=5),
            },
            "quarterlyHistory": list(reversed(quarterly_hist)),
            "holdings": holdings_list,
            "edgarUrl": f"https://www.sec.gov/edgar/browse/?CIK={int(cik):010d}",
        })

    # Step 7: Build Top 75 Rankings per mode & window
    rankings: dict[str, dict[str, list[dict[str, Any]]]] = {"quarter_end": {}, "filing_lag": {}}
    ranked_union_ciks: dict[str, list[str]] = {"quarter_end": [], "filing_lag": []}
    fund_top75_windows: dict[str, dict[str, list[str]]] = {"quarter_end": {}, "filing_lag": {}}

    for mode in MODES:
        union_set: list[str] = []
        for w in WINDOWS:
            eligible = [
                (f["cik"], f["returns"][mode][w])
                for f in funds_out
                if f["returns"][mode][w] is not None
            ]
            eligible.sort(key=lambda x: x[1], reverse=True)
            top75 = eligible[:75]
            rankings[mode][w] = [
                {"rank": idx + 1, "cik": cik, "returnPct": round(ret, 2)}
                for idx, (cik, ret) in enumerate(top75)
            ]
            for cik, _ in top75:
                if cik not in union_set:
                    union_set.append(cik)
                fund_top75_windows[mode].setdefault(cik, []).append(w)
        ranked_union_ciks[mode] = union_set

    fund_by_cik = {f["cik"]: f for f in funds_out}

    # Step 8: Build Top 30 Consensus Holdings per mode (across ranked union funds)
    top_holdings_out: dict[str, list[dict[str, Any]]] = {}
    for mode in MODES:
        pool_ciks = ranked_union_ciks[mode] or [f["cik"] for f in funds_out]
        pool_funds = [fund_by_cik[c] for c in pool_ciks]
        n_pool = len(pool_funds)
        total_pool_aum = sum(f["equityAumUsd"] for f in pool_funds)

        stock_map: dict[str, dict[str, Any]] = {}
        for f in pool_funds:
            cik = f["cik"]
            for h in f["holdings"]:
                if h["securityType"] != "EQUITY" or h["weightPct"] <= 0:
                    continue
                t = h["ticker"]
                if t.startswith("CUSIP:") or t in NON_STOCK_ETFS:
                    continue
                if t not in stock_map:
                    stock_map[t] = {
                        "ticker": t,
                        "cusip": h["cusip"],
                        "name": h["name"],
                        "sector": h["sector"],
                        "industry": h["industry"],
                        "isTechTmt": h["isTechTmt"],
                        "priceUsd": h["pricePerShare"],
                        "sumWeightPct": 0.0,
                        "sumValueUsd": 0.0,
                        "netBuyers": 0,
                        "netTrimmers": 0,
                        "holders": [],
                    }
                rec = stock_map[t]
                rec["sumWeightPct"] += h["weightPct"]
                rec["sumValueUsd"] += h["valueUsd"]
                if h["status"] in ("NEW", "ADDED"):
                    rec["netBuyers"] += 1
                elif h["status"] == "TRIMMED":
                    rec["netTrimmers"] += 1
                rec["holders"].append({
                    "cik": cik,
                    "fundName": f["name"],
                    "weightPct": h["weightPct"],
                    "valueUsd": h["valueUsd"],
                    "status": h["status"],
                    "qoqSharesChangePct": h["qoqSharesChangePct"],
                    "rankedWindows": fund_top75_windows[mode].get(cik, []),
                })

        entries: list[dict[str, Any]] = []
        for t, rec in stock_map.items():
            holders_sorted = sorted(rec["holders"], key=lambda x: x["weightPct"], reverse=True)
            eq_agg = rec["sumWeightPct"] / max(1, n_pool)
            avg_holder_w = rec["sumWeightPct"] / max(1, len(holders_sorted))
            aum_agg = (rec["sumValueUsd"] / total_pool_aum * 100.0) if total_pool_aum > 0 else 0.0

            # Compute stock YTD and TTM returns
            ytd_ret = None
            ttm_ret = None
            if t in aligned_prices:
                arr, first_i, last_i = aligned_prices[t]
                if first_i <= win_indices["YTD"] + 5:
                    s_i = max(win_indices["YTD"], first_i)
                    ytd_ret = clean_num(((arr[last_i] / arr[s_i]) - 1.0) * 100.0, 2)
                if first_i <= win_indices["TTM"] + 5:
                    s_i = max(win_indices["TTM"], first_i)
                    ttm_ret = clean_num(((arr[last_i] / arr[s_i]) - 1.0) * 100.0, 2)

            top3_txt = ", ".join(f"{h['fundName'].split()[0]} ({h['weightPct']:.1f}%)" for h in holders_sorted[:3])
            more = f" +{len(holders_sorted)-3} more" if len(holders_sorted) > 3 else ""
            summary_txt = f"Held by {len(holders_sorted)}/{n_pool} ranked funds — led by {top3_txt}{more}."

            entries.append({
                "ticker": t,
                "cusip": rec["cusip"],
                "name": rec["name"],
                "sector": rec["sector"],
                "industry": rec["industry"],
                "isTechTmt": rec["isTechTmt"],
                "priceUsd": rec["priceUsd"],
                "stockYtdPct": ytd_ret,
                "stockTtmPct": ttm_ret,
                "holderCount": len(holders_sorted),
                "totalRankedFundsPool": n_pool,
                "equalAggregateWeightPct": round(eq_agg, 2),
                "avgHolderWeightPct": round(avg_holder_w, 2),
                "aumAggregateWeightPct": round(aum_agg, 2),
                "totalValueUsd": round(rec["sumValueUsd"], 2),
                "netBuyers": rec["netBuyers"],
                "netTrimmers": rec["netTrimmers"],
                "holders": holders_sorted,
                "summaryText": summary_txt,
                "_sortScore": 0.6 * eq_agg + 0.4 * aum_agg,
            })

        entries.sort(key=lambda x: x["_sortScore"], reverse=True)
        top30 = entries[:30]
        for idx, item in enumerate(top30):
            item.pop("_sortScore", None)
            item["rank"] = idx + 1
        top_holdings_out[mode] = top30

    # Step 9: Build Ideal Portfolio (<= 50 names, default 40 names, 8% cap) + Historical Backtest
    ideal_pool_ciks = ranked_union_ciks["quarter_end"] or [f["cik"] for f in funds_out]
    ideal_pool_funds = [fund_by_cik[c] for c in ideal_pool_ciks]
    ideal_weights, ideal_stock_agg = compute_ideal_portfolio_weights(
        ideal_pool_funds, mode="quarter_end", cusip_meta=cusip_meta, max_names=40, cap_pct=8.0
    )

    core_thresh = max(12, len(ideal_pool_funds) // 5)
    max_raw_score = max((ideal_stock_agg[t]["rawScore"] for t in ideal_weights), default=1.0)
    ideal_positions: list[dict[str, Any]] = []
    ideal_sector_totals: dict[str, dict[str, Any]] = {}

    for idx, (t, w_dec) in enumerate(sorted(ideal_weights.items(), key=lambda x: x[1], reverse=True)):
        rec = ideal_stock_agg[t]
        w_pct = round(w_dec * 100.0, 2)
        conv_score = round((rec["rawScore"] / max_raw_score) * 100.0, 1) if max_raw_score > 0 else 50.0
        backers_sorted = sorted(rec["backers"], key=lambda x: x["weightPct"], reverse=True)
        avg_fw = sum(rec["weights"]) / len(rec["weights"])
        max_fw = max(rec["weights"])

        if len(backers_sorted) >= core_thresh:
            tag = "Core Consensus"
        elif backers_sorted[0]["fscore"] >= 0.85 and max_fw >= 6.0:
            tag = "Top-Decile Alpha"
        elif rec["netBuy"] >= max(2, len(backers_sorted) // 2):
            tag = "Active Accumulation"
        else:
            tag = "High-Conviction Specialist"

        ttm_ret = None
        px_usd = None
        if t in aligned_prices:
            arr, first_i, last_i = aligned_prices[t]
            px_usd = clean_num(float(arr[last_i]), 2)
            if first_i <= win_indices["TTM"] + 5:
                s_i = max(win_indices["TTM"], first_i)
                ttm_ret = clean_num(((arr[last_i] / arr[s_i]) - 1.0) * 100.0, 2)

        sec = rec["sector"] or "Other"
        if sec not in ideal_sector_totals:
            ideal_sector_totals[sec] = {"sector": sec, "weightPct": 0.0, "stockCount": 0}
        ideal_sector_totals[sec]["weightPct"] += w_pct
        ideal_sector_totals[sec]["stockCount"] += 1

        ideal_positions.append({
            "rank": idx + 1,
            "ticker": t,
            "cusip": rec["cusip"],
            "name": rec["name"],
            "sector": sec,
            "industry": rec["industry"],
            "idealWeightPct": w_pct,
            "convictionScore": conv_score,
            "holderCount": len(backers_sorted),
            "avgFundWeightPct": round(avg_fw, 2),
            "maxFundWeightPct": round(max_fw, 2),
            "topHolderName": backers_sorted[0]["fundName"],
            "rationaleTag": tag,
            "topBackers": [
                {
                    "cik": b["cik"],
                    "fundName": b["fundName"],
                    "weightPct": b["weightPct"],
                    "status": b["status"],
                }
                for b in backers_sorted[:5]
            ],
            "stockTtmPct": ttm_ret,
            "priceUsd": px_usd,
        })

    # Rebalance rounding error on top weight so sum == 100.00%
    if ideal_positions:
        diff = round(100.0 - sum(p["idealWeightPct"] for p in ideal_positions), 2)
        ideal_positions[0]["idealWeightPct"] = round(ideal_positions[0]["idealWeightPct"] + diff, 2)

    # Ideal Portfolio out-of-sample quarterly backtest (rebalanced each quarter from 2021-Q4 onward)
    all_quarters = sorted({fl["reportDate"] for f in funds_out for fl in raw_fund_filings[f["cik"]]})
    bt_segments: dict[str, list[tuple[int, dict[str, float]]]] = {"quarter_end": [], "filing_lag": []}

    for qd in all_quarters:
        if qd < "2021-09-30":
            continue
        q_idx = find_idx_on_or_after(cal_dates, qd)
        if q_idx is None:
            continue
        # Rank funds using blended multi-horizon trailing NAV up to q_idx (no look-ahead)
        horizons = [
            (126, 0.30),
            (252, 0.40),
            (504, 0.30),
        ]
        scored_funds: list[tuple[str, float, dict[str, float]]] = []
        for f in funds_out:
            cik = f["cik"]
            # Find filing at or immediately before qd
            fls = [fl for fl in raw_fund_filings[cik] if fl["reportDate"] <= qd]
            if not fls:
                continue
            fl = fls[-1]
            # Require filing within last 180 days so stale funds don't leak
            if (datetime.strptime(qd, "%Y-%m-%d") - datetime.strptime(fl["reportDate"], "%Y-%m-%d")).days > 180:
                continue
            w_dict, _, _, _, _, _ = extract_equity_weights(fl, cusip_meta)
            nav_arr = fund_navs["quarter_end"][cik]
            score_num = 0.0
            score_den = 0.0
            if not math.isnan(nav_arr[q_idx]):
                for lb_days, hw in horizons:
                    lb_idx = max(0, q_idx - lb_days)
                    if q_idx - lb_idx >= 60 and not math.isnan(nav_arr[lb_idx]) and nav_arr[lb_idx] > 0:
                        ret_h = (nav_arr[q_idx] / nav_arr[lb_idx]) - 1.0
                        # Annualize for cross-horizon comparability
                        ann_h = ((1.0 + max(-0.95, ret_h)) ** (252.0 / (q_idx - lb_idx))) - 1.0
                        score_num += hw * ann_h
                        score_den += hw
            trailing_score = (score_num / score_den) if score_den > 0 else 0.0
            scored_funds.append((cik, trailing_score, w_dict))

        if len(scored_funds) < 5:
            continue
        scored_funds.sort(key=lambda x: x[1], reverse=True)
        top_slice = scored_funds[:max(12, min(25, len(scored_funds) // 3))]

        # Score stocks across top_slice
        s_scores: dict[str, float] = {}
        s_counts: dict[str, int] = {}
        for rank_i, (_, _, w_dict) in enumerate(top_slice):
            f_sc = 1.0 - 0.65 * (rank_i / max(1, len(top_slice) - 1))
            for t, w_val in w_dict.items():
                if t.startswith("CUSIP:") or t in NON_STOCK_ETFS or w_val <= 0:
                    continue
                s_scores[t] = s_scores.get(t, 0.0) + f_sc * math.sqrt(w_val)
                s_counts[t] = s_counts.get(t, 0) + 1

        filt_scores = {
            t: sc * (1.0 + 0.18 * math.log(max(1, s_counts[t])))
            for t, sc in s_scores.items()
            if s_counts[t] >= 2
        }
        top40 = dict(sorted(filt_scores.items(), key=lambda x: x[1], reverse=True)[:40])
        bt_w = waterfill_capped_weights(top40, cap=0.08)
        if not bt_w:
            continue

        if not bt_segments["quarter_end"] or q_idx > bt_segments["quarter_end"][-1][0]:
            bt_segments["quarter_end"].append((q_idx, bt_w))

        lag_dt = (datetime.strptime(qd, "%Y-%m-%d") + timedelta(days=46)).strftime("%Y-%m-%d")
        lag_idx = find_idx_on_or_after(cal_dates, lag_dt)
        if lag_idx is not None and (not bt_segments["filing_lag"] or lag_idx > bt_segments["filing_lag"][-1][0]):
            bt_segments["filing_lag"].append((lag_idx, bt_w))

    ideal_nav_qe = simulate_nav_series(bt_segments["quarter_end"], aligned_prices, n_dates)
    ideal_nav_lag = simulate_nav_series(bt_segments["filing_lag"], aligned_prices, n_dates)
    ideal_rets_qe, ideal_met_qe = compute_returns_and_metrics(ideal_nav_qe, qqq_nav, win_indices)
    ideal_rets_lag, ideal_met_lag = compute_returns_and_metrics(ideal_nav_lag, qqq_nav, win_indices)

    ideal_hhi = sum((p["idealWeightPct"] / 100.0) ** 2 for p in ideal_positions)
    ideal_eff_n = (1.0 / ideal_hhi) if ideal_hhi > 0 else float(len(ideal_positions))

    ideal_portfolio_data = {
        "positionCount": len(ideal_positions),
        "maxPositionCapPct": 8.0,
        "rankedFundsCount": len(ideal_pool_funds),
        "effectivePositions": round(ideal_eff_n, 1),
        "top10WeightPct": round(sum(p["idealWeightPct"] for p in ideal_positions[:10]), 2),
        "methodologySummary": (
            f"Constructed from all holdings of the {len(ideal_pool_funds)} funds ranked Top 75 across at least one window "
            f"(TTM/3Y/2Y/18M/YTD/5Y). Each position is scored by manager multi-window percentile rank × √(portfolio weight) "
            f"× multi-fund consensus multiplier × QoQ accumulation factor, then water-filled with an 8.0% single-stock cap."
        ),
        "sectorBreakdown": [
            {"sector": v["sector"], "weightPct": round(v["weightPct"], 2), "stockCount": v["stockCount"]}
            for v in sorted(ideal_sector_totals.values(), key=lambda x: x["weightPct"], reverse=True)
        ],
        "positions": ideal_positions,
        "backtest": {
            "quarter_end": {
                "returns": ideal_rets_qe,
                "cagrPct": ideal_met_qe["cagrPct"],
                "maxDrawdownPct": ideal_met_qe["maxDrawdownPct"],
                "sharpeProxy": ideal_met_qe["sharpeProxy"],
                "curve": downsample_curve(cal_dates, ideal_nav_qe, step=5),
            },
            "filing_lag": {
                "returns": ideal_rets_lag,
                "cagrPct": ideal_met_lag["cagrPct"],
                "maxDrawdownPct": ideal_met_lag["maxDrawdownPct"],
                "sharpeProxy": ideal_met_lag["sharpeProxy"],
                "curve": downsample_curve(cal_dates, ideal_nav_lag, step=5),
            },
        },
    }

    # Step 10: Build QoQ Fund Updates Digest & Cross-Fund Consensus Signals
    fund_updates_out: list[dict[str, Any]] = []
    seen_update_ciks: set[str] = set()
    ranked_any_ciks = set(ranked_union_ciks["quarter_end"])
    for cand in update_cands:
        cik = cand["cik"]
        if cik in seen_update_ciks or cik not in raw_fund_filings:
            continue
        seen_update_ciks.add(cik)
        filings = raw_fund_filings[cik]
        in_rankings = cik in fund_by_cik
        top75_w = fund_top75_windows["quarter_end"].get(cik, [])
        upd = build_fund_update_entry(
            cand=cand,
            filings=filings,
            cusip_meta=cusip_meta,
            in_rankings=in_rankings,
            ranked_windows=top75_w,
        )
        if upd:
            fund_updates_out.append(upd)

    fund_updates_out.sort(
        key=lambda u: (u["periodEnd"], u["filingDate"], u["grossActivityUsd"]),
        reverse=True,
    )
    updates_overview = build_updates_overview(fund_updates_out, total_eligible=len(update_cands))

    # Sort funds by TTM performance by default
    funds_out.sort(key=lambda f: f["returns"]["quarter_end"]["TTM"] or -999.0, reverse=True)

    # Compact tail rows on unranked EDGAR-discovered funds (Curated Core + all Top-75 Ranked funds retain full depth)
    ranked_union_all_modes = set(ranked_union_ciks["quarter_end"]) | set(ranked_union_ciks["filing_lag"])
    for f in funds_out:
        is_vip = f["source"] == "Curated Core" or f["cik"] in ranked_union_all_modes
        if not is_vip and len(f["holdings"]) > 80:
            opts = [h for h in f["holdings"] if h["securityType"] != "EQUITY"]
            eqs = [h for h in f["holdings"] if h["securityType"] == "EQUITY"][:80]
            seen_k = {(h["cusip"], h["securityType"]) for h in eqs}
            f["holdings"] = sorted(
                eqs + [h for h in opts if (h["cusip"], h["securityType"]) not in seen_k],
                key=lambda x: x["valueUsd"],
                reverse=True,
            )

    for u in fund_updates_out:
        is_vip = u["source"] == "Curated Core" or u["cik"] in ranked_union_all_modes
        if is_vip:
            if len(u["allMoves"]) > 250:
                u["allMoves"] = [
                    m for m in u["allMoves"]
                    if not (
                        m["action"] == "UNCHANGED"
                        and m["securityType"] == "EQUITY"
                        and m["valuationShiftType"] is None
                        and max(m["currBookPct"], m["prevBookPct"]) < 0.15
                    )
                ]
        elif len(u["allMoves"]) > 60:
            keep_keys: set[tuple[str, str]] = set()
            kept: list[dict[str, Any]] = []
            for bucket in (u["topNew"], u["topAdded"], u["topTrimmed"], u["topExited"], u["valuationShifts"]):
                for m in bucket:
                    k = (m["cusip"], m["securityType"])
                    if k not in keep_keys:
                        keep_keys.add(k)
                        kept.append(m)
            for m in u["allMoves"]:
                if m["securityType"] in ("CALL", "PUT") or m["valuationShiftType"] is not None:
                    k = (m["cusip"], m["securityType"])
                    if k not in keep_keys:
                        keep_keys.add(k)
                        kept.append(m)
            for m in u["allMoves"]:
                if len(kept) >= 65 and (m["action"] == "UNCHANGED" or max(m["currBookPct"], m["prevBookPct"]) < 0.3):
                    continue
                k = (m["cusip"], m["securityType"])
                if k not in keep_keys:
                    keep_keys.add(k)
                    kept.append(m)
                if len(kept) >= 90:
                    break
            u["allMoves"] = sorted(kept, key=lambda x: max(x["currValueUsd"], x["prevValueUsd"]), reverse=True)

    latest_q_overall = max(f["latestQuarterLabel"] for f in funds_out) if funds_out else "2026 Q3"
    total_filings_cnt = sum(len(raw_fund_filings[f["cik"]]) for f in funds_out)

    # Compute next 13F filing deadline season label
    now_dt = datetime.now(timezone.utc)
    season_deadlines = [
        (datetime(now_dt.year, 2, 14, tzinfo=timezone.utc), f"Q4 {now_dt.year - 1} (Mid-Feb {now_dt.year})"),
        (datetime(now_dt.year, 5, 15, tzinfo=timezone.utc), f"Q1 {now_dt.year} (Mid-May {now_dt.year})"),
        (datetime(now_dt.year, 8, 14, tzinfo=timezone.utc), f"Q2 {now_dt.year} (Mid-Aug {now_dt.year})"),
        (datetime(now_dt.year, 11, 14, tzinfo=timezone.utc), f"Q3 {now_dt.year} (Mid-Nov {now_dt.year})"),
        (datetime(now_dt.year + 1, 2, 14, tzinfo=timezone.utc), f"Q4 {now_dt.year} (Mid-Feb {now_dt.year + 1})"),
    ]
    next_season = next(lbl for dt, lbl in season_deadlines if dt >= now_dt - timedelta(days=5))

    dataset = {
        "metadata": {
            "generatedAt": now_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "asOfDate": as_of_date,
            "latestQuarterCovered": latest_q_overall,
            "fundsEvaluatedCount": len(all_candidates),
            "qualifiedFundsCount": len(funds_out),
            "rankedFundsUnionCount": len(ranked_any_ciks),
            "total13fFilingsProcessed": total_filings_cnt,
            "next13fSeasonLabel": next_season,
            "dataSources": [
                "SEC EDGAR Form 13F-HR / 13F-HR/A XML Filings",
                "OpenFIGI CUSIP Security Master",
                "Yahoo Finance Daily Split & Dividend Adjusted Closes",
            ],
        },
        "benchmarks": benchmarks,
        "funds": funds_out,
        "rankings": rankings,
        "rankedUnionCiks": ranked_union_ciks,
        "topHoldings": top_holdings_out,
        "idealPortfolio": ideal_portfolio_data,
        "fundUpdates": fund_updates_out,
        "updatesOverview": updates_overview,
    }

    if (
        len(funds_out) < 25
        or len(ranked_union_ciks["quarter_end"]) < 25
        or total_filings_cnt < 100
        or len(fund_updates_out) < 20
        or len(ideal_positions) < 15
    ):
        raise RuntimeError(
            f"Refusing to overwrite {OUTPUT_JSON} with incomplete output: "
            f"funds={len(funds_out)}, ranked={len(ranked_union_ciks['quarter_end'])}, "
            f"filings={total_filings_cnt}, updates={len(fund_updates_out)}, ideal={len(ideal_positions)}"
        )

    serialized = json.dumps(dataset, allow_nan=False, separators=(",", ":"))
    if len(serialized.encode("utf-8")) < 500_000:
        raise RuntimeError(
            f"Refusing to overwrite {OUTPUT_JSON}: serialized payload abnormally small ({len(serialized)} bytes)"
        )

    OUTPUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = OUTPUT_JSON.with_suffix(".json.tmp")
    tmp_path.write_text(serialized, encoding="utf-8")
    tmp_path.replace(OUTPUT_JSON)
    size_kb = OUTPUT_JSON.stat().st_size / 1024.0
    print(f"\n=== Pipeline Complete ===")
    print(f"Wrote {OUTPUT_JSON} ({size_kb:.1f} KB)")
    print(f"Qualified Funds: {len(funds_out)} | Ranked Union: {len(ranked_union_ciks['quarter_end'])} | Fund Updates: {len(fund_updates_out)}")
    print(f"Ideal Portfolio: {len(ideal_positions)} names | TTM QE Return: {ideal_rets_qe['TTM']}% | QQQ TTM: {benchmarks['QQQ']['returns']['TTM']}%")


if __name__ == "__main__":
    main()
