"""
End-to-end Paper Trail 13F data pipeline:
1. Scans 100% of SEC Form 13F managers via quarterly Bulk Data Sets + post-bulk master.idx & EFTS .txt submissions
2. Resolves CUSIPs -> US tickers + GICS sectors + daily split/dividend-adjusted close prices & OHLCV
3. Qualifies genuinely Tech/TMT-focused concentrated funds (excluding operating corps, options dealers, SPVs)
4. Reconstructs point-in-time daily buy-and-hold equity curves for Quarter-End and zero-look-ahead 45-Day Lag modes
5. Computes ^IRX-adjusted Sharpe, Sortino, Information Ratio vs QQQ, Batting Average, Turnover, & Composite Skill Score
6. Enriches stocks with Quarter VWAP Entry Zones, SEC XBRL Fundamentals, and Elite-50 mid-quarter 13D/13G alerts
7. Emits sharded public/data/{summary.json, funds_{0..f}.json, stocks_{0..7}.json} for fast lazy-loaded SPA delivery
"""

from __future__ import annotations

import bisect
import json
import math
import statistics
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np

from enrichment import (
    compute_quarter_vwap_metrics,
    fetch_midquarter_alerts,
    fetch_risk_free_daily_rates,
    fetch_xbrl_fundamentals_batch,
    load_ticker_cik_mappings,
)
from funds_config import CURATED_FUNDS, DUAL_CLASS_CANONICAL, NON_STOCK_ETFS, is_non_stock_etf
from sec_bulk import (
    discover_universe_via_bulk,
    filter_operating_entities,
    ingest_target_histories,
    sync_bulk_zips,
)
from security_resolver import (
    CUSIP_CACHE_PATH,
    MANUAL_CUSIP_OVERRIDES,
    TICKER_META_PATH,
    _normalize_legal_name,
    fetch_all_prices,
    fetch_nasdaq_market_caps,
    is_tech_tmt,
    load_cached_ohlcv,
    load_cached_splits,
    load_json_dict,
    resolve_cusips,
    split_factor_between,
)

ROOT_DIR = Path(__file__).resolve().parent.parent
PUBLIC_DATA_DIR = ROOT_DIR / "public" / "data"
LEGACY_MONOLITH_JSON = ROOT_DIR / "src" / "data" / "portfolio_data.json"

WINDOWS = ["YTD", "TTM", "18M", "2Y", "3Y", "5Y"]
MODES = ["quarter_end", "filing_lag"]


def fund_shard_id(cik: str) -> str:
    return f"{int(cik) % 16:x}"


def stock_shard_id(ticker: str) -> int:
    return zlib.crc32(ticker.encode("utf-8")) % 8


def quarter_label(report_date: str) -> str:
    y, m, _ = report_date.split("-")
    q = (int(m) - 1) // 3 + 1
    return f"{y} Q{q}"


def clean_num(val: float | None, decimals: int = 2) -> float | None:
    if val is None or not math.isfinite(val):
        return None
    return round(float(val), decimals)


def _build_excluded_etf_tickers(cusip_meta: dict[str, dict[str, Any]]) -> set[str]:
    out: set[str] = set(NON_STOCK_ETFS)
    for m in cusip_meta.values():
        tk = str(m.get("ticker") or "")
        if not tk or tk.startswith("CUSIP:"):
            continue
        is_etf = bool(
            m.get("isNonStockEtf")
            or is_non_stock_etf(tk, m.get("name", ""), m.get("sector", ""), m.get("industry", ""))
        )
        if is_etf:
            m["isNonStockEtf"] = True
            m["isTechTmt"] = False
            out.add(tk)
            out.add(DUAL_CLASS_CANONICAL.get(tk, tk))
    return out


def _is_excluded_etf_entry(
    canon_ticker: str,
    meta: dict[str, Any] | None = None,
    excluded_etfs: set[str] | frozenset[str] = NON_STOCK_ETFS,
) -> bool:
    if canon_ticker in excluded_etfs:
        return True
    if meta:
        if meta.get("isNonStockEtf"):
            return True
        return is_non_stock_etf(
            canon_ticker,
            meta.get("name", ""),
            meta.get("sector", ""),
            meta.get("industry", ""),
        )
    return False


def build_aligned_price_matrix(
    cal_dates: list[str],
    raw_prices: dict[str, dict[str, float]],
) -> dict[str, tuple[np.ndarray, int, int]]:
    """
    For each ticker, align daily adjusted prices onto `cal_dates` with forward-fill
    between first_idx and last_idx.
    - Detects unrecorded split steps (>= 2.8x or <= 0.36x) with pre/post stability check
    - Avoids infinitely freezing long-delisted sub-$1 distress stubs (20-day decay toward 0)
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
        # Sanitize unrecorded reverse/forward split discontinuities in historical series
        for i in range(first_idx + 1, last_idx + 1):
            prev_px = arr[i - 1]
            cur_px = arr[i]
            if prev_px <= 0 or cur_px <= 0:
                continue
            r = cur_px / prev_px
            if r >= 2.8 or r <= 0.36:
                prev_win = arr[max(first_idx, i - 10) : i]
                next_win = arr[i : min(last_idx + 1, i + 10)]
                if len(prev_win) >= 4 and len(next_win) >= 4:
                    med_prev = float(np.median(prev_win))
                    med_next = float(np.median(next_win))
                    if med_prev > 0 and med_next > 0:
                        cov_prev = float(np.std(prev_win)) / med_prev
                        cov_next = float(np.std(next_win)) / med_next
                        med_ratio = med_next / med_prev
                        if cov_prev < 0.16 and cov_next < 0.16 and (
                            (r >= 2.8 and med_ratio >= 2.3) or (r <= 0.36 and med_ratio <= 0.43)
                        ):
                            arr[first_idx:i] *= r
                            print(
                                f"  [prices] Sanitized unrecorded split boundary for {ticker} at {cal_dates[i]} "
                                f"({prev_px:.4f} -> {cur_px:.4f}, factor {r:.2f}x).",
                                flush=True,
                            )
        # Terminal handling after last_idx: decay sub-$1 distress delistings toward 0 over 20d; hold buyouts >=$1 flat
        end_val = float(arr[last_idx])
        stale_days = (n - 1) - last_idx
        if stale_days > 20 and end_val < 1.0:
            for step_k, i in enumerate(range(last_idx + 1, n), 1):
                decay = max(0.0, 1.0 - step_k / 20.0)
                arr[i] = end_val * decay
        else:
            for i in range(last_idx + 1, n):
                arr[i] = end_val
        aligned[ticker] = (arr, first_idx, last_idx)
    return aligned


def find_idx_on_or_after(cal_dates: list[str], target_date: str) -> int | None:
    pos = bisect.bisect_left(cal_dates, target_date)
    if pos < len(cal_dates):
        return pos
    return None


def find_idx_on_or_before(cal_dates: list[str], target_date: str) -> int | None:
    pos = bisect.bisect_right(cal_dates, target_date) - 1
    if pos >= 0:
        return pos
    return None


def simulate_nav_series(
    segments: list[tuple[int, dict[str, float]]],
    aligned_prices: dict[str, tuple[np.ndarray, int, int]],
    n_dates: int,
    min_priced_coverage: float = 0.70,
) -> np.ndarray:
    """
    Simulate buy-and-hold within each rebalance segment [(start_idx, weights_dict), ...].
    - Enforces min_priced_coverage (default 70%); leaves NaN breaks on under-covered segments
    - Renormalizes priced position weights to sum to 1.0 so unpriced equities are NOT
      modeled as a 0%-volatility cash buffer
    Returns array of length n_dates with NaN before first valid segment and Base=100.
    """
    nav = np.full(n_dates, np.nan, dtype=np.float64)
    if not segments:
        return nav

    running_nav = 100.0
    started = False
    for seg_i, (s_idx, weights) in enumerate(segments):
        e_idx = segments[seg_i + 1][0] if seg_i + 1 < len(segments) else (n_dates - 1)
        if s_idx >= n_dates or e_idx <= s_idx:
            if started and s_idx < n_dates and math.isnan(nav[s_idx]):
                nav[s_idx] = running_nav
            continue

        priced_items: list[tuple[np.ndarray, float, float]] = []
        priced_weight = 0.0
        for ticker, w in weights.items():
            if w <= 0:
                continue
            pdata = aligned_prices.get(ticker)
            if pdata is None:
                continue
            arr, first_idx, _ = pdata
            # Allow up to 5 trading days if stock IPO'd right around quarter boundary
            base_idx = s_idx if s_idx >= first_idx else (first_idx if first_idx - s_idx <= 5 else -1)
            if base_idx < 0 or math.isnan(arr[base_idx]) or arr[base_idx] <= 0:
                continue
            priced_items.append((arr, float(w), float(arr[base_idx])))
            priced_weight += float(w)

        if priced_weight < min_priced_coverage or not priced_items:
            if started:
                # Reset running_nav for any subsequent recovered run while leaving NaN gap
                running_nav = 100.0
            continue

        started = True
        span = np.zeros(e_idx - s_idx + 1, dtype=np.float64)
        for arr, w, base_px in priced_items:
            norm_w = w / priced_weight
            sub = arr[s_idx : e_idx + 1]
            rel = np.where(np.isnan(sub), 1.0, sub / base_px)
            span += norm_w * rel

        nav[s_idx : e_idx + 1] = running_nav * span
        running_nav = float(nav[e_idx])

    return nav


def compute_window_start_indices(cal_dates: list[str]) -> dict[str, int]:
    as_of_str = cal_dates[-1]
    as_of_dt = datetime.strptime(as_of_str, "%Y-%m-%d")

    def _months_ago(months: int) -> str:
        import calendar
        y = as_of_dt.year
        m = as_of_dt.month - months
        while m <= 0:
            m += 12
            y -= 1
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
    rf_ann_arr: np.ndarray | None = None,
) -> tuple[dict[str, float | None], dict[str, float | None]]:
    valid_mask = ~np.isnan(nav)
    valid_indices = np.where(valid_mask)[0]
    empty_metrics = {
        "cagrPct": None,
        "maxDrawdownPct": None,
        "sharpeProxy": None,
        "sortinoRatio": None,
        "infoRatioVsQqq": None,
        "betaVsQqq": None,
        "alphaVsQqqPct": None,
        "annualizedVolPct": None,
    }
    if len(valid_indices) < 20:
        return {w: None for w in WINDOWS}, empty_metrics

    last_valid = int(valid_indices[-1])
    # Identify trailing contiguous valid run so any earlier NaN gap cannot distort return denominators
    nan_before_last = np.where(np.isnan(nav[: last_valid + 1]))[0]
    contig_start = int(nan_before_last[-1]) + 1 if len(nan_before_last) > 0 else int(valid_indices[0])
    if (last_valid - contig_start) < 19:
        return {w: None for w in WINDOWS}, empty_metrics

    end_nav = float(nav[last_valid])
    win_rets: dict[str, float | None] = {}
    for w in WINDOWS:
        s_idx = win_indices[w]
        if contig_start <= s_idx + 5:
            ref_idx = max(s_idx, contig_start)
            start_val = float(nav[ref_idx])
            if start_val > 0:
                win_rets[w] = clean_num(((end_nav / start_val) - 1.0) * 100.0, 2)
            else:
                win_rets[w] = None
        else:
            win_rets[w] = None

    # Trailing 3Y (or full available contiguous run) risk/alpha stats
    stat_start = max(contig_start, win_indices["3Y"])
    sub_nav = nav[stat_start : last_valid + 1]
    sub_qqq = qqq_nav[stat_start : last_valid + 1]

    days = len(sub_nav)
    years = max(days / 252.0, 0.25)
    total_ret = (sub_nav[-1] / sub_nav[0]) - 1.0
    cagr = ((1.0 + total_ret) ** (1.0 / years) - 1.0) * 100.0 if (1.0 + total_ret) > 0 else None

    daily_r = np.diff(sub_nav) / sub_nav[:-1]
    qqq_r = np.diff(sub_qqq) / sub_qqq[:-1]
    ann_vol = float(np.std(daily_r, ddof=1) * math.sqrt(252.0) * 100.0) if len(daily_r) > 5 else None

    if rf_ann_arr is not None and len(rf_ann_arr) > last_valid:
        rf_sub = np.clip(rf_ann_arr[stat_start + 1 : last_valid + 1], 0.0, 15.0)
        rf_daily = (1.0 + rf_sub / 100.0) ** (1.0 / 252.0) - 1.0
    else:
        rf_daily = np.full(len(daily_r), (1.04) ** (1.0 / 252.0) - 1.0, dtype=np.float64)

    excess_r = daily_r - rf_daily
    ann_excess = float(np.mean(excess_r) * 252.0) if len(excess_r) > 5 else None

    # Max drawdown
    running_max = np.maximum.accumulate(sub_nav)
    dd = (sub_nav / running_max) - 1.0
    max_dd = float(np.min(dd) * 100.0)

    sharpe = None
    if len(excess_r) > 20:
        ex_std = float(np.std(excess_r, ddof=1))
        if ex_std > 1e-8:
            sharpe = clean_num((float(np.mean(excess_r)) / ex_std) * math.sqrt(252.0), 2)

    sortino = None
    if len(excess_r) > 20 and ann_excess is not None:
        down_r = np.minimum(0.0, excess_r)
        down_dev = float(np.sqrt(np.mean(down_r * down_r)) * math.sqrt(252.0))
        if down_dev > 1e-6:
            sortino = clean_num(max(-5.0, min(10.0, ann_excess / down_dev)), 2)

    info_ratio = None
    beta = None
    alpha = None
    if len(daily_r) > 20:
        act_r = daily_r - qqq_r
        act_std = float(np.std(act_r, ddof=1))
        if act_std > 1e-8:
            info_ratio = clean_num((float(np.mean(act_r)) / act_std) * math.sqrt(252.0), 2)
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
        "sortinoRatio": sortino,
        "infoRatioVsQqq": info_ratio,
        "betaVsQqq": beta,
        "alphaVsQqqPct": alpha,
        "annualizedVolPct": clean_num(ann_vol, 2),
    }
    return win_rets, metrics


def downsample_curve(cal_dates: list[str], nav: np.ndarray, step: int = 5) -> list[dict[str, Any]]:
    valid = np.where(~np.isnan(nav))[0]
    if len(valid) == 0:
        return []
    last_i = int(valid[-1])
    nan_before_last = np.where(np.isnan(nav[: last_i + 1]))[0]
    first_i = int(nan_before_last[-1]) + 1 if len(nan_before_last) > 0 else int(valid[0])
    base = float(nav[first_i])
    if base <= 0:
        return []
    pts: list[dict[str, Any]] = []
    recent_cutoff = last_i - 325
    coarse_step = max(step, step * 3)
    for i in range(first_i, last_i + 1):
        stride = step if i >= recent_cutoff else coarse_step
        if i == first_i or i == last_i or (i % stride == 0):
            pts.append({"date": cal_dates[i], "nav": round(float(nav[i]) / base * 100.0, 2)})
    return pts


def extract_weights_from_rows(
    rows: list[dict[str, Any]],
    cusip_meta: dict[str, dict[str, Any]],
    excluded_etfs: set[str] | frozenset[str] = NON_STOCK_ETFS,
) -> tuple[dict[str, float], float, float, float, int, int, float, float, float, float]:
    """
    Strip broad/sector/bond/treasury ETFs cleanly from single-stock weights and normalize
    dual-class share lines (GOOG -> GOOGL).
    Returns:
      (weights, single_stock_aum, total_eq_aum, total_reported_usd,
       single_pos_count, sig_pos_count_25bps, top10_pct, tech_tmt_pct,
       max_single_pct, etf_share_of_eq_pct)
    """
    total_reported = sum(float(r["valueUsd"]) for r in rows if float(r["valueUsd"]) > 0)
    eq_rows = [r for r in rows if r["secType"] == "EQUITY" and float(r["valueUsd"]) > 0]
    total_eq_aum = sum(float(r["valueUsd"]) for r in eq_rows)
    if total_eq_aum <= 0:
        return {}, 0.0, 0.0, total_reported, 0, 0, 0.0, 0.0, 0.0, 0.0

    by_ticker: dict[str, float] = {}
    tech_val = 0.0
    etf_val = 0.0
    for r in eq_rows:
        val = float(r["valueUsd"])
        meta = cusip_meta.get(r["cusip"], {})
        raw_t = meta.get("ticker") or f"CUSIP:{r['cusip']}"
        ticker = DUAL_CLASS_CANONICAL.get(raw_t, raw_t)
        if _is_excluded_etf_entry(ticker, meta, excluded_etfs):
            etf_val += val
            continue
        by_ticker[ticker] = by_ticker.get(ticker, 0.0) + val
        if meta.get("isTechTmt"):
            tech_val += val

    single_stock_aum = sum(by_ticker.values())
    etf_pct = (etf_val / total_eq_aum * 100.0) if total_eq_aum > 0 else 0.0
    if single_stock_aum <= 0:
        return {}, 0.0, total_eq_aum, total_reported, 0, 0, 0.0, 0.0, 0.0, etf_pct

    weights = {t: v / single_stock_aum for t, v in by_ticker.items()}
    sorted_w = sorted(weights.values(), reverse=True)
    top10_pct = sum(sorted_w[:10]) * 100.0
    max_single_pct = sorted_w[0] * 100.0 if sorted_w else 0.0
    sig_pos_cnt = sum(1 for w in weights.values() if w >= 0.0025)
    tech_pct = (tech_val / single_stock_aum) * 100.0
    return (
        weights,
        single_stock_aum,
        total_eq_aum,
        total_reported,
        len(by_ticker),
        sig_pos_cnt,
        top10_pct,
        tech_pct,
        max_single_pct,
        etf_pct,
    )


def extract_equity_weights(
    filing: dict[str, Any],
    cusip_meta: dict[str, dict[str, Any]],
    excluded_etfs: set[str] | frozenset[str] = NON_STOCK_ETFS,
) -> tuple[dict[str, float], float, float, float, int, int, float, float, float, float]:
    return extract_weights_from_rows(filing["rows"], cusip_meta, excluded_etfs=excluded_etfs)


def _pct_ranks_desc(pairs: list[tuple[str, float]]) -> dict[str, float]:
    n_p = len(pairs)
    if n_p <= 1:
        return {k: 1.0 for k, _ in pairs}
    ordered = sorted(pairs, key=lambda x: x[1], reverse=True)
    return {k: (n_p - 1 - idx) / (n_p - 1) for idx, (k, _) in enumerate(ordered)}


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


def compute_orthogonal_fund_scores(
    funds_subset: list[dict[str, Any]],
    fund_navs_mode: dict[str, np.ndarray],
    cal_dates: list[str],
    win_indices: dict[str, int],
) -> dict[str, float]:
    """
    Score funds across 3 NON-OVERLAPPING orthogonal return horizons (0-12M: 35%,
    12-36M: 40%, 36-60M: 25%) blended with Composite Skill Score (or standalone Skill),
    eliminating double-counting of the latest year across TTM/18M/2Y/3Y/5Y.
    """
    last_idx = len(cal_dates) - 1
    idx_1y = win_indices["TTM"]
    idx_3y = win_indices["3Y"]
    idx_5y = win_indices["5Y"]

    horizon_defs = [
        ("h0_12", idx_1y, last_idx, 0.35),
        ("h12_36", idx_3y, idx_1y, 0.40),
        ("h36_60", idx_5y, idx_3y, 0.25),
    ]
    h_ranks: dict[str, dict[str, float]] = {h[0]: {} for h in horizon_defs}

    for hname, s_i, e_i, _ in horizon_defs:
        vals: list[tuple[str, float]] = []
        for f in funds_subset:
            cik = f["cik"]
            arr = fund_navs_mode.get(cik)
            if arr is None or e_i <= s_i + 20 or len(arr) <= e_i:
                continue
            if math.isnan(arr[s_i]) or math.isnan(arr[e_i]) or arr[s_i] <= 0:
                continue
            # Ensure no NaN break between s_i and e_i
            if np.any(np.isnan(arr[s_i : e_i + 1])):
                continue
            ret_h = (float(arr[e_i]) / float(arr[s_i])) - 1.0
            ann_h = ((1.0 + max(-0.95, ret_h)) ** (252.0 / (e_i - s_i))) - 1.0
            vals.append((cik, ann_h))
        vals.sort(key=lambda x: x[1], reverse=True)
        n = len(vals)
        for rank_i, (cik, _) in enumerate(vals):
            h_ranks[hname][cik] = 1.0 - 0.75 * (rank_i / max(1, n - 1)) if n > 1 else 1.0

    scores: dict[str, float] = {}
    for f in funds_subset:
        cik = f["cik"]
        num = 0.0
        den = 0.0
        for hname, _, _, hw in horizon_defs:
            if cik in h_ranks[hname]:
                num += hw * h_ranks[hname][cik]
                den += hw
        scores[cik] = (num / den) if den > 0 else 0.5
    return scores


def compute_ideal_portfolio_weights(
    funds_subset: list[dict[str, Any]],
    mode: str,
    cusip_meta: dict[str, dict[str, Any]],
    fund_navs_mode: dict[str, np.ndarray],
    cal_dates: list[str],
    win_indices: dict[str, int],
    max_names: int = 40,
    cap_pct: float = 8.0,
    aligned_prices: dict[str, tuple[Any, int, int]] | None = None,
    n_dates: int = 0,
    excluded_etfs: set[str] | frozenset[str] | None = None,
) -> tuple[dict[str, float], dict[str, dict[str, Any]], list[tuple[str, float]]]:
    """
    Score actively traded US stocks across Elite-50 / ranked funds by orthogonal
    multi-horizon fund score^1.5, sqrt(portfolio weight), Tech/TMT relevance,
    multi-fund consensus, and continuous QoQ share-delta inflection multiplier.
    Returns (weights_0_to_1, stock_scoring_details, sorted_qualified_items).
    """
    if not funds_subset:
        return {}, {}, []

    etf_set = excluded_etfs if excluded_etfs is not None else _build_excluded_etf_tickers(cusip_meta)
    canon_meta_by_ticker: dict[str, dict[str, Any]] = {}
    canon_cusip_by_ticker: dict[str, str] = {}
    for c_key, m_val in cusip_meta.items():
        tk = m_val.get("ticker", "")
        if tk and not tk.startswith("CUSIP:"):
            if tk not in canon_meta_by_ticker or m_val.get("sector"):
                canon_meta_by_ticker[tk] = m_val
                canon_cusip_by_ticker[tk] = c_key

    ortho_scores = compute_orthogonal_fund_scores(funds_subset, fund_navs_mode, cal_dates, win_indices)
    fund_scores: dict[str, float] = {}
    for f in funds_subset:
        cik = f["cik"]
        sk = f["metrics"][mode].get("skillScore")
        sk_norm = (0.25 + 0.75 * (sk / 100.0)) if sk is not None else 0.5
        fund_scores[cik] = 0.65 * ortho_scores.get(cik, 0.5) + 0.35 * sk_norm

    stock_agg: dict[str, dict[str, Any]] = {}
    for f in funds_subset:
        cik = f["cik"]
        fscore = fund_scores[cik]
        fund_merged: dict[str, dict[str, Any]] = {}
        for h in f["holdings"]:
            if h["securityType"] != "EQUITY" or h["weightPct"] <= 0:
                continue
            raw_t = cusip_meta.get(h["cusip"], {}).get("ticker") or h["ticker"]
            if not raw_t or raw_t.startswith("CUSIP:"):
                continue
            canon_t = DUAL_CLASS_CANONICAL.get(raw_t, raw_t)
            meta_ref = cusip_meta.get(h["cusip"]) or canon_meta_by_ticker.get(canon_t) or h
            if _is_excluded_etf_entry(canon_t, meta_ref, etf_set):
                continue
            if aligned_prices is not None and n_dates > 0:
                pdata = aligned_prices.get(canon_t)
                if pdata is None or pdata[2] < n_dates - 7 or (pdata[2] - pdata[1]) < 20:
                    continue
            slot = fund_merged.get(canon_t)
            if slot is None:
                c_meta = canon_meta_by_ticker.get(canon_t, {})
                fund_merged[canon_t] = {
                    "ticker": canon_t,
                    "cusip": h["cusip"] if raw_t == canon_t else (canon_cusip_by_ticker.get(canon_t) or h["cusip"]),
                    "name": (c_meta.get("name") or h["name"]) if raw_t != canon_t else h["name"],
                    "sector": c_meta.get("sector") or h["sector"] if raw_t != canon_t else h["sector"],
                    "industry": c_meta.get("industry") or h["industry"] if raw_t != canon_t else h["industry"],
                    "isTechTmt": bool(h.get("isTechTmt") or c_meta.get("isTechTmt")),
                    "weightPct": float(h["weightPct"]),
                    "maxLegVal": float(h["valueUsd"]),
                    "status": h["status"],
                }
            else:
                slot["weightPct"] += float(h["weightPct"])
                slot["isTechTmt"] = slot["isTechTmt"] or bool(h.get("isTechTmt"))
                if float(h["valueUsd"]) > slot["maxLegVal"]:
                    slot["maxLegVal"] = float(h["valueUsd"])
                    slot["status"] = h["status"]
                    if raw_t == canon_t:
                        slot["cusip"] = h["cusip"]
                        slot["name"] = h["name"]

        for t, hm in fund_merged.items():
            if t not in stock_agg:
                stock_agg[t] = {
                    "ticker": t,
                    "cusip": hm["cusip"],
                    "name": hm["name"],
                    "sector": hm["sector"],
                    "industry": hm["industry"],
                    "isTechTmt": hm["isTechTmt"],
                    "convSum": 0.0,
                    "weights": [],
                    "backers": [],
                    "netNew": 0,
                    "netAdd": 0,
                    "netTrim": 0,
                }
            rec = stock_agg[t]
            rec["isTechTmt"] = rec["isTechTmt"] or hm["isTechTmt"]
            w_pct = round(hm["weightPct"], 2)
            w_dec = hm["weightPct"] / 100.0
            rec["convSum"] += (fscore ** 1.5) * math.sqrt(w_dec)
            rec["weights"].append(w_pct)
            rec["backers"].append({
                "cik": cik,
                "fundName": f["name"],
                "weightPct": w_pct,
                "status": hm["status"],
                "fscore": fscore,
            })
            if hm["status"] == "NEW":
                rec["netNew"] += 1
            elif hm["status"] == "ADDED":
                rec["netAdd"] += 1
            elif hm["status"] == "TRIMMED":
                rec["netTrim"] += 1

    # Require multi-fund backing (>=2) or top high-conviction anchor (>=4.5%)
    qualified: dict[str, float] = {}
    for t, rec in stock_agg.items():
        cnt = len(rec["backers"])
        max_w = max(rec["weights"])
        if cnt < 2 and max_w < 4.5:
            continue
        consensus_mult = 1.0 + 0.18 * math.log(max(1, cnt))
        # Continuous QoQ inflection score giving extra signal to fresh NEW + ADDED over TRIMMED
        buy_impulse = (1.25 * rec["netNew"] + 1.0 * rec["netAdd"] - 1.0 * rec["netTrim"]) / max(1, cnt)
        mom_mult = 1.0 + 0.14 * max(-0.5, min(1.0, buy_impulse))
        tech_mult = 1.0 if rec["isTechTmt"] else 0.68
        score = rec["convSum"] * consensus_mult * mom_mult * tech_mult
        rec["rawScore"] = score
        rec["netBuy"] = rec["netNew"] + rec["netAdd"]
        qualified[t] = score

    sorted_items = sorted(qualified.items(), key=lambda x: x[1], reverse=True)
    top_scores = dict(sorted_items[:max_names])
    capped = waterfill_capped_weights(top_scores, cap=cap_pct / 100.0)
    return capped, stock_agg, sorted_items


def statutory_deadline_date(period_end: str) -> str:
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
    return dl_dt.strftime("%Y-%m-%d")


def run_ideal_portfolio_backtest(
    bt_mode: str,
    all_quarters: list[str],
    all_bt_ciks: list[str],
    snaps_by_cik: dict[str, list[dict[str, Any]]],
    navs_by_cik: dict[str, np.ndarray],
    cal_dates: list[str],
    as_of_date: str,
    aligned_prices: dict[str, tuple[Any, int, int]],
    ticker_is_tech: dict[str, bool],
    curated_bt_ciks: set[str],
    excluded_etfs: set[str] | frozenset[str] = NON_STOCK_ETFS,
    min_quarter_date: str = "2021-09-30",
    splits_map: dict[str, Any] | None = None,
    rf_ann_arr: np.ndarray | None = None,
    qqq_nav: np.ndarray | None = None,
) -> list[tuple[int, dict[str, float]]]:
    """
    Reconstruct survivorship-free quarterly point-in-time backtest segments for the
    Elite-50 Ideal Portfolio in Quarter-End or zero-look-ahead 45-Day Filing-Lag mode,
    mirroring live fund qualification, rolling 4Q persistence, risk-adjusted PIT Skill Score,
    and split-adjusted QoQ accumulation signals.
    """
    segs: list[tuple[int, dict[str, float]]] = []
    # Orthogonal horizons matching live scorer: 0-12M (35%), 12-36M (40%), 36-60M (25%)
    ortho_ranges = [(0, 252, 0.35), (252, 756, 0.40), (756, 1260, 0.25)]

    for qd in all_quarters:
        if qd < min_quarter_date:
            continue
        qd_dt = datetime.strptime(qd, "%Y-%m-%d")
        if bt_mode == "filing_lag":
            rebal_dt = max(statutory_deadline_date(qd), (qd_dt + timedelta(days=46)).strftime("%Y-%m-%d"))
            if rebal_dt > as_of_date:
                continue
        else:
            rebal_dt = qd

        rebal_idx = find_idx_on_or_after(cal_dates, rebal_dt)
        if rebal_idx is None:
            continue

        active_cands: list[tuple[str, dict[str, Any], dict[str, float], str, list[dict[str, Any]]]] = []
        for cik in all_bt_ciks:
            sn_list = snaps_by_cik.get(cik) or []
            avail = [s for s in sn_list if s["effDate"] <= rebal_dt]
            if not avail:
                continue
            cur_s = avail[-1]
            # Require filing period within 180d + point-in-time concentration/eligibility
            if (qd_dt - datetime.strptime(cur_s["reportDate"], "%Y-%m-%d")).days > 180:
                continue
            is_cur = cik in curated_bt_ciks
            tech_floor = 30.0 if is_cur else 35.0
            if (
                cur_s["eqAum"] < 100_000_000
                or cur_s["techPct"] < tech_floor
                or cur_s["posCount"] < 5
                or (cur_s["posCount"] > 250 and not is_cur)
                or cur_s["effN"] < 4.0
                or cur_s["maxSingle"] > 45.0
            ):
                continue

            recent_snaps = avail[-4:]
            avg4q_max1 = sum(s["maxSingle"] for s in recent_snaps) / len(recent_snaps)
            if avg4q_max1 > 40.0:
                continue
            if len(avail) >= 4:
                med4q_tech = float(statistics.median(s["techPct"] for s in recent_snaps))
                persist_floor = 40.0 if is_cur else 50.0
                if med4q_tech < persist_floor:
                    continue

            # Prior period snapshot (for split-adjusted +/-5% share-count delta)
            prev_same = [s for s in avail if s["reportDate"] < cur_s["reportDate"]]
            prev_shares = prev_same[-1]["shares"] if prev_same else {}
            prev_rd = prev_same[-1]["reportDate"] if prev_same else ""
            active_cands.append((cik, cur_s, prev_shares, prev_rd, avail))

        if len(active_cands) < 5:
            continue

        def _collect_horizon_vals(start_i: int, end_i: int) -> list[tuple[str, float]]:
            out_vals: list[tuple[str, float]] = []
            for cik, _, _, _, _ in active_cands:
                nav_arr = navs_by_cik.get(cik)
                if nav_arr is None or end_i >= len(nav_arr):
                    continue
                if math.isnan(nav_arr[start_i]) or math.isnan(nav_arr[end_i]) or nav_arr[start_i] <= 0:
                    continue
                if np.any(np.isnan(nav_arr[start_i : end_i + 1])):
                    continue
                ret_h = (float(nav_arr[end_i]) / float(nav_arr[start_i])) - 1.0
                ann_h = ((1.0 + max(-0.95, ret_h)) ** (252.0 / (end_i - start_i))) - 1.0
                out_vals.append((cik, ann_h))
            return out_vals

        # Orthogonal horizon percentile ranking up to rebal_idx
        h_ranks_pit: dict[int, dict[str, float]] = {}
        for h_idx, (off_end, off_start, _) in enumerate(ortho_ranges):
            e_h = rebal_idx - off_end
            s_h = max(0, rebal_idx - off_start)
            if e_h <= s_h + 45:
                continue
            vals = _collect_horizon_vals(s_h, e_h)
            if h_idx == 0 and s_h <= 45 and len(vals) < min(20, max(3, int(len(active_cands) * 0.45))):
                cand_starts: dict[str, int] = {}
                for cik, _, _, _, _ in active_cands:
                    nav_arr = navs_by_cik.get(cik)
                    if nav_arr is None or e_h >= len(nav_arr) or math.isnan(nav_arr[e_h]):
                        continue
                    nans_before = np.where(np.isnan(nav_arr[: e_h + 1]))[0]
                    c_start = int(nans_before[-1]) + 1 if len(nans_before) > 0 else 0
                    if e_h - c_start > 45 and not math.isnan(nav_arr[c_start]) and nav_arr[c_start] > 0:
                        cand_starts[cik] = c_start
                if cand_starts:
                    best_s = s_h
                    best_sc = -1.0
                    for cs in sorted(set(cand_starts.values())):
                        if cs <= s_h or e_h - cs <= 45:
                            continue
                        cnt_cov = sum(1 for st in cand_starts.values() if st <= cs)
                        sc_cs = cnt_cov * math.sqrt(e_h - cs)
                        if sc_cs > best_sc:
                            best_sc = sc_cs
                            best_s = cs
                    if best_s > s_h:
                        common_vals = _collect_horizon_vals(best_s, e_h)
                        if len(common_vals) > len(vals):
                            vals = common_vals
            if vals:
                h_ranks_pit[h_idx] = _pct_ranks_desc(vals)

        # Compute PIT trailing risk-adjusted metrics (Sortino, Info Ratio vs QQQ, Batting Avg) up to rebal_idx
        pit_ir_pairs: list[tuple[str, float]] = []
        pit_sort_pairs: list[tuple[str, float]] = []
        pit_bat_pairs: list[tuple[str, float]] = []
        pit_valid_days: dict[str, int] = {}
        if qqq_nav is not None and rebal_idx >= 60 and len(qqq_nav) > rebal_idx:
            min_req_days = 120 if rebal_idx >= 180 else 45
            stat_floor = max(0, rebal_idx - 756)
            for cik, _, _, _, avail in active_cands:
                nav_arr = navs_by_cik.get(cik)
                if nav_arr is None or len(nav_arr) <= rebal_idx or math.isnan(nav_arr[rebal_idx]):
                    continue
                nans_b = np.where(np.isnan(nav_arr[: rebal_idx + 1]))[0]
                c_start = int(nans_b[-1]) + 1 if len(nans_b) > 0 else 0
                s_st = max(c_start, stat_floor)
                n_days_valid = (rebal_idx - s_st) + 1
                if n_days_valid < min_req_days:
                    continue
                sub_n = nav_arr[s_st : rebal_idx + 1]
                sub_q = qqq_nav[s_st : rebal_idx + 1]
                dr = np.diff(sub_n) / sub_n[:-1]
                qr = np.diff(sub_q) / sub_q[:-1]
                if len(dr) < 30:
                    continue
                act_r = dr - qr
                act_std = float(np.std(act_r, ddof=1))
                ir_val = (float(np.mean(act_r)) / act_std) * math.sqrt(252.0) if act_std > 1e-8 else 0.0
                if rf_ann_arr is not None and len(rf_ann_arr) > rebal_idx:
                    rf_sub = np.clip(rf_ann_arr[s_st + 1 : rebal_idx + 1], 0.0, 15.0)
                    rf_d = (1.0 + rf_sub / 100.0) ** (1.0 / 252.0) - 1.0
                else:
                    rf_d = np.full(len(dr), (1.04) ** (1.0 / 252.0) - 1.0, dtype=np.float64)
                ex_r = dr - rf_d
                ann_ex = float(np.mean(ex_r) * 252.0)
                down_dev = float(np.sqrt(np.mean(np.minimum(0.0, ex_r) ** 2)) * math.sqrt(252.0))
                sort_val = max(-5.0, min(10.0, ann_ex / down_dev)) if down_dev > 1e-6 else 0.0

                # Interval batting avg across prior disclosed snapshots up to rebal_idx
                beats = 0
                tot_int = 0
                eff_indices: list[int] = []
                for s_snap in avail:
                    ei = find_idx_on_or_after(cal_dates, s_snap["effDate"])
                    if ei is not None and ei <= rebal_idx:
                        if not eff_indices or ei > eff_indices[-1]:
                            eff_indices.append(ei)
                if not eff_indices or eff_indices[-1] < rebal_idx - 10:
                    eff_indices.append(rebal_idx)
                for ii in range(len(eff_indices) - 1):
                    si_0, si_1 = eff_indices[ii], eff_indices[ii + 1]
                    if si_1 - si_0 >= 15 and si_0 >= c_start and not math.isnan(nav_arr[si_0]) and not math.isnan(nav_arr[si_1]):
                        tot_int += 1
                        if (nav_arr[si_1] / nav_arr[si_0]) > (qqq_nav[si_1] / qqq_nav[si_0]):
                            beats += 1
                bat_val = (beats / tot_int) if tot_int >= 2 else 0.5
                pit_ir_pairs.append((cik, ir_val))
                pit_sort_pairs.append((cik, sort_val))
                pit_bat_pairs.append((cik, bat_val))
                pit_valid_days[cik] = n_days_valid

        pit_skill_norm: dict[str, float] = {}
        if pit_ir_pairs:
            ir_r = _pct_ranks_desc(pit_ir_pairs)
            sort_r = _pct_ranks_desc(pit_sort_pairs)
            bat_r = _pct_ranks_desc(pit_bat_pairs)
            for cik, _ in pit_ir_pairs:
                raw_comp = 0.40 * ir_r[cik] + 0.30 * bat_r[cik] + 0.30 * sort_r[cik]
                conf = min(1.0, pit_valid_days[cik] / 504.0)
                shrunk = 0.50 + conf * (raw_comp - 0.50)
                pit_skill_norm[cik] = 0.25 + 0.75 * shrunk

        has_1y_cohort = 0 in h_ranks_pit and len(h_ranks_pit[0]) >= 25
        scored_funds: list[tuple[str, float, dict[str, Any], dict[str, float], str]] = []
        for cik, cur_s, prev_shares, prev_rd, _ in active_cands:
            if has_1y_cohort and cik not in h_ranks_pit[0]:
                continue
            num = 0.0
            den = 0.0
            for h_idx, (_, _, hw) in enumerate(ortho_ranges):
                hr = h_ranks_pit.get(h_idx)
                if hr and cik in hr:
                    num += hw * hr[cik]
                    den += hw
            if rebal_idx >= 90 and den <= 0:
                continue
            ortho_pit = (0.25 + 0.75 * (num / den)) if den > 0 else 0.5
            if cik in pit_skill_norm:
                sc_pit = 0.65 * ortho_pit + 0.35 * pit_skill_norm[cik]
            else:
                sc_pit = ortho_pit
            scored_funds.append((cik, sc_pit, cur_s, prev_shares, prev_rd))

        scored_funds.sort(key=lambda x: x[1], reverse=True)
        top_cohort = scored_funds[: min(50, len(scored_funds))]

        s_scores: dict[str, float] = {}
        s_max_w: dict[str, float] = {}
        s_counts: dict[str, int] = {}
        s_new: dict[str, int] = {}
        s_add: dict[str, int] = {}
        s_trim: dict[str, int] = {}

        for _, f_sc, cur_s, prev_shares, prev_rd in top_cohort:
            cur_sh_map = cur_s["shares"]
            cur_rd = cur_s["reportDate"]
            for t, w_val in cur_s["weights"].items():
                if t.startswith("CUSIP:") or t in excluded_etfs or w_val <= 0:
                    continue
                pdata = aligned_prices.get(t)
                if pdata is None or pdata[1] > rebal_idx + 5 or pdata[2] <= rebal_idx:
                    continue
                s_scores[t] = s_scores.get(t, 0.0) + (f_sc ** 1.5) * math.sqrt(w_val)
                s_counts[t] = s_counts.get(t, 0) + 1
                s_max_w[t] = max(s_max_w.get(t, 0.0), w_val * 100.0)
                if prev_shares:
                    psh = prev_shares.get(t, 0.0)
                    csh = cur_sh_map.get(t, 0.0)
                    if psh <= 0:
                        s_new[t] = s_new.get(t, 0) + 1
                    else:
                        sp_factor, _ = split_factor_between(t, prev_rd, cur_rd, splits_map)
                        psh_adj = psh * sp_factor
                        if csh >= psh_adj * 1.05:
                            s_add[t] = s_add.get(t, 0) + 1
                        elif csh <= psh_adj * 0.95:
                            s_trim[t] = s_trim.get(t, 0) + 1

        filt_scores: dict[str, float] = {}
        for t, sc in s_scores.items():
            cnt = s_counts[t]
            if cnt < 2 and s_max_w.get(t, 0.0) < 4.5:
                continue
            cons_m = 1.0 + 0.18 * math.log(max(1, cnt))
            buy_imp = (1.25 * s_new.get(t, 0) + 1.0 * s_add.get(t, 0) - 1.0 * s_trim.get(t, 0)) / max(1, cnt)
            mom_m = 1.0 + 0.14 * max(-0.5, min(1.0, buy_imp))
            tech_m = 1.0 if ticker_is_tech.get(t, False) else 0.68
            filt_scores[t] = sc * cons_m * mom_m * tech_m

        top40 = dict(sorted(filt_scores.items(), key=lambda x: x[1], reverse=True)[:40])
        bt_w = waterfill_capped_weights(top40, cap=0.08)
        if not bt_w:
            continue
        if not segs or rebal_idx > segs[-1][0]:
            segs.append((rebal_idx, bt_w))
        elif rebal_idx == segs[-1][0]:
            segs[-1] = (rebal_idx, bt_w)
    return segs



def classify_sub_sector(sector: str, industry: str, is_tech_tmt: bool = False) -> str:
    ind = (industry or "").lower()
    sec = (sector or "Other").strip() or "Other"
    if "semiconductor" in ind:
        return "Semiconductors"
    if "software" in ind or "cloud" in ind:
        return "Enterprise Software"
    if (
        "internet" in ind
        or "interactive media" in ind
        or "electronic gaming" in ind
        or "entertainment" in ind
        or "advertising" in ind
    ):
        return "Platforms & Interactive"
    if (
        "electrical equipment" in ind
        or "power producer" in ind
        or "electric utilit" in ind
        or sec == "Utilities"
    ):
        return "AI Power & Grid"
    if (
        "credit services" in ind
        or "capital markets" in ind
        or "financial data" in ind
        or "fintech" in ind
        or ("bank" in ind and is_tech_tmt)
    ):
        return "Fintech & Payments"
    if (
        "communication equipment" in ind
        or "computer hardware" in ind
        or "electronic components" in ind
        or "consumer electronics" in ind
        or "information technology services" in ind
        or "scientific & technical" in ind
    ):
        return "Hardware & IT Services"
    if sec in ("Consumer Cyclical", "Consumer Discretionary"):
        return "Consumer & E-Commerce"
    if sec == "Communication Services":
        return "Platforms & Interactive"
    if sec == "Technology":
        return "Hardware & IT Services"
    if sec == "Industrials" and is_tech_tmt:
        return "AI Power & Grid"
    return sec


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
    dl_str = statutory_deadline_date(period_end)
    dl_dt = datetime.strptime(dl_str, "%Y-%m-%d").date()
    fd_dt = datetime.strptime(filing_date, "%Y-%m-%d").date()
    days_before = (dl_dt - fd_dt).days

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
    Aggregate filing rows by canonical (canonTicker, secType) so dual-class lines (GOOG+GOOGL) merge cleanly.
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
        canon_ticker = raw_ticker if raw_ticker.startswith("CUSIP:") else DUAL_CLASS_CANONICAL.get(raw_ticker, raw_ticker)
        key = (canon_ticker, r["secType"])
        if key not in agg:
            agg[key] = {
                "cusip": r["cusip"],
                "ticker": disp_ticker if raw_ticker.startswith("CUSIP:") else canon_ticker,
                "rawTicker": canon_ticker,
                "name": meta.get("name") or r["issuer"],
                "sector": meta.get("sector") or "Other",
                "industry": meta.get("industry") or "",
                "isTechTmt": bool(meta.get("isTechTmt")),
                "securityType": r["secType"],
                "shares": sh,
                "valueUsd": val,
                "maxLegVal": val,
            }
        else:
            agg[key]["shares"] += sh
            agg[key]["valueUsd"] += val
            agg[key]["isTechTmt"] = agg[key]["isTechTmt"] or bool(meta.get("isTechTmt"))
            if val > agg[key]["maxLegVal"] or raw_ticker == canon_ticker:
                agg[key]["maxLegVal"] = val
                if raw_ticker == canon_ticker:
                    agg[key]["cusip"] = r["cusip"]
                    agg[key]["name"] = meta.get("name") or r["issuer"]
    for item in agg.values():
        item.pop("maxLegVal", None)
    return agg, total_reported, eq_aum


def build_fund_update_entry(
    cand: dict[str, Any],
    filings: list[dict[str, Any]],
    cusip_meta: dict[str, dict[str, Any]],
    in_rankings: bool,
    ranked_windows: list[str],
    splits_map: dict[str, Any] | None = None,
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
    eff_fd = curr_fl.get("originalFilingDate") or curr_fl["filingDate"]
    dl_info = compute_deadline_status(curr_fl["reportDate"], eff_fd)

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
        prev_sh_raw = float(p_item["shares"]) if p_item else 0.0
        curr_sh = float(c_item["shares"]) if c_item else 0.0
        sp_factor = 1.0
        sp_ratio = ""
        if sec_type == "EQUITY" and splits_map and not base["rawTicker"].startswith("CUSIP:"):
            sp_factor, sp_ratio = split_factor_between(
                base["rawTicker"], prev_fl["reportDate"], curr_fl["reportDate"], splits_map
            )
        prev_sh = prev_sh_raw * sp_factor
        sh_delta = curr_sh - prev_sh

        prev_val = float(p_item["valueUsd"]) if p_item else 0.0
        curr_val = float(c_item["valueUsd"]) if c_item else 0.0
        val_delta = curr_val - prev_val

        est_price = (
            (curr_val / curr_sh)
            if curr_sh > 0
            else ((prev_val / prev_sh) if prev_sh > 0 else 0.0)
        )
        est_flow_usd = sh_delta * est_price

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
        is_dust_scale = False
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
            is_dust_scale = (
                prev_sh < 100
                and prev_val < 50_000
                and curr_val >= 250_000
                and diff_pct >= 1000.0
            )
            # +/- 5.0% threshold prevents share-class/DRIP/rounding noise from triggering Added/Trimmed signals
            if diff_pct >= 5.0:
                action = "ADDED"
                added_cnt += 1
            elif diff_pct <= -5.0:
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
            and abs(sh_delta_pct) < 5.0
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

        if sp_factor != 1.0 and sp_ratio:
            if tag_note:
                tag_note = f"{tag_note} (adjusted for {sp_ratio} split)"
            else:
                tag_note = f"Prior shares adjusted {sp_ratio} for corporate split"
        elif is_dust_scale:
            tag_note = "Scaled from starter stub"

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
            "estFlowUsd": round(est_flow_usd, 2),
            "prevBookPct": round(prev_book_pct, 2),
            "currBookPct": round(curr_book_pct, 2),
            "bookDeltaPct": round(book_delta_pct, 2),
            "valuationShiftType": val_shift_type,
            "tagNote": tag_note,
            "_isDustScale": is_dust_scale,
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

    def _is_mat(m: dict[str, Any]) -> bool:
        return max(m["currValueUsd"], m["prevValueUsd"], abs(m["valueDeltaUsd"])) >= 250_000.0

    # Categorized top lists (prioritize material Equities, cleanly strip internal scratch flag)
    top_new = sorted(
        [m for m in all_moves if m["action"] == "NEW"],
        key=lambda m: (m["securityType"] == "EQUITY", _is_mat(m), m["currValueUsd"]),
        reverse=True,
    )[:8]
    top_added = sorted(
        [m for m in all_moves if m["action"] == "ADDED"],
        key=lambda m: (
            m["securityType"] == "EQUITY",
            _is_mat(m),
            max(m["estFlowUsd"], m["valueDeltaUsd"], 0.0)
            * (1.35 if (not m.get("_isDustScale") and (m["sharesDeltaPct"] or 0) >= 100) else 1.0),
        ),
        reverse=True,
    )[:8]
    top_trimmed = sorted(
        [m for m in all_moves if m["action"] == "TRIMMED"],
        key=lambda m: (_is_mat(m), max(abs(m["estFlowUsd"]), abs(m["valueDeltaUsd"]))),
        reverse=True,
    )[:8]
    top_exited = sorted(
        [m for m in all_moves if m["action"] == "EXITED"],
        key=lambda m: (_is_mat(m), m["prevValueUsd"]),
        reverse=True,
    )[:8]

    for mv in all_moves:
        mv.pop("_isDustScale", None)

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

    # Synthesize grounded, scannable Apple-editorial narrative sections (material positions >= $250K)
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
    mat_exited = [m for m in top_exited if _is_mat(m)]
    if mat_exited:
        ex0 = mat_exited[0]
        focus_notes.append(
            f"{ex0['ticker']} ({ex0['securityType'].lower()}) — exited entirely. "
            f"Prior {_fmt_shares_brief(ex0['prevShares'])} sh / {_fmt_usd_brief(ex0['prevValueUsd'])} ({ex0['prevBookPct']:.1f}%) is gone from {q_label[-2:]}."
        )
    added_eq = [m for m in top_added if m["securityType"] == "EQUITY" and _is_mat(m)]
    for ad in added_eq[:2]:
        dp_val = ad["sharesDeltaPct"] or 0.0
        if dp_val >= 1000.0 or ad.get("tagNote") == "Scaled from starter stub":
            focus_notes.append(
                f"{ad['ticker']} — scaled from starter stub to {_fmt_shares_brief(ad['currShares'])} sh: "
                f"{_fmt_usd_brief(ad['prevValueUsd'])} → {_fmt_usd_brief(ad['currValueUsd'])} ({ad['currBookPct']:.2f}% of book)."
            )
        else:
            pct_s = f"+{dp_val:,.0f}%" if dp_val >= 100 else f"+{dp_val:.1f}%"
            focus_notes.append(
                f"{ad['ticker']} — added heavily: {_fmt_shares_brief(ad['prevShares'])} → {_fmt_shares_brief(ad['currShares'])} sh ({pct_s}), "
                f"{_fmt_usd_brief(ad['prevValueUsd'])} → {_fmt_usd_brief(ad['currValueUsd'])} ({ad['currBookPct']:.2f}% of book)."
            )
    mat_trimmed = [m for m in top_trimmed if _is_mat(m)]
    if mat_trimmed:
        tr0 = mat_trimmed[0]
        focus_notes.append(
            f"{tr0['ticker']} — trimmed {abs(tr0['sharesDeltaPct'] or 0):.1f}%: "
            f"{_fmt_shares_brief(tr0['prevShares'])} → {_fmt_shares_brief(tr0['currShares'])} sh, "
            f"{_fmt_usd_brief(tr0['prevValueUsd'])} → {_fmt_usd_brief(tr0['currValueUsd'])}."
        )
    new_eq = [m for m in top_new if m["securityType"] == "EQUITY" and _is_mat(m)]
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
    extra_new = [m for m in top_new if _is_mat(m) and (m["securityType"] != "EQUITY" or m not in new_eq[:1])][:4]
    if extra_new:
        lbls = [
            f"{m['ticker']}{' ' + m['securityType'].lower() + 's' if m['securityType'] != 'EQUITY' else ''} ({_fmt_usd_brief(m['currValueUsd'])})"
            for m in extra_new
        ]
        other_bits.append(f"New names: {', '.join(lbls)}")
    narrative_other = "; ".join(other_bits) + "." if other_bits else None

    return {
        "cik": cik,
        "shard": fund_shard_id(cik),
        "fundName": fund_short,
        "legalName": curr_fl["entityName"],
        "manager": cand["manager"],
        "styleTag": cand["styleTag"],
        "source": cand.get("source", "Curated Core"),
        "form": curr_fl.get("form", "13F-HR"),
        "amendmentType": curr_fl.get("amendmentType", ""),
        "inRankings": in_rankings,
        "isRankedTop100AnyWindow": bool(ranked_windows),
        "rankedWindows": ranked_windows,
        "quarterLabel": q_label,
        "periodEnd": curr_fl["reportDate"],
        "prevQuarterLabel": prev_q_label,
        "prevPeriodEnd": prev_fl["reportDate"],
        "filingDate": curr_fl["filingDate"],
        "originalFilingDate": curr_fl.get("originalFilingDate") or curr_fl["filingDate"],
        "latestAmendmentDate": curr_fl.get("latestAmendmentDate") or curr_fl["filingDate"],
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
        "putLegs": put_legs[:6],
        "callLegs": call_legs[:6],
        "instrumentShifts": instrument_shifts[:6],
        "valuationShifts": valuation_shifts[:4],
        "topNew": top_new[:4],
        "topAdded": top_added[:4],
        "topTrimmed": top_trimmed[:4],
        "topExited": top_exited[:4],
        "allMoves": all_moves,
    }


def build_updates_overview(
    fund_updates: list[dict[str, Any]],
    total_eligible: int,
    excluded_etfs: set[str] | frozenset[str] = NON_STOCK_ETFS,
) -> dict[str, Any]:
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
        limit: int = 12,
        sort_by_abs_delta: bool = False,
        sign_filter: int = 0,
    ) -> list[dict[str, Any]]:
        buckets: dict[str, dict[str, Any]] = {}
        for u in latest_pool:
            for mv in u["allMoves"]:
                if mv["action"] not in actions or mv["securityType"] not in sec_types:
                    continue
                raw_t = mv["ticker"]
                if raw_t.startswith("CUSIP:"):
                    continue
                t = DUAL_CLASS_CANONICAL.get(raw_t, raw_t)
                if sec_types == ("EQUITY",) and _is_excluded_etf_entry(t, mv, excluded_etfs):
                    continue
                if t not in buckets:
                    buckets[t] = {
                        "ticker": t,
                        "name": mv["name"],
                        "sector": mv["sector"],
                        "seenCiks": set(),
                        "totalCurrValueUsd": 0.0,
                        "totalDeltaUsd": 0.0,
                        "totalEstFlowUsd": 0.0,
                        "funds": [],
                    }
                b = buckets[t]
                b["totalCurrValueUsd"] += mv["currValueUsd"]
                b["totalDeltaUsd"] += mv["valueDeltaUsd"]
                b["totalEstFlowUsd"] += mv.get("estFlowUsd", mv["valueDeltaUsd"])
                b["seenCiks"].add(u["cik"])
                b["funds"].append({
                    "cik": u["cik"],
                    "fundName": u["fundName"],
                    "action": mv["action"],
                    "securityType": mv["securityType"],
                    "currBookPct": mv["currBookPct"],
                    "prevBookPct": mv["prevBookPct"],
                    "valueDeltaUsd": mv["valueDeltaUsd"],
                    "estFlowUsd": mv.get("estFlowUsd", mv["valueDeltaUsd"]),
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
            b["totalEstFlowUsd"] = round(b["totalEstFlowUsd"], 2)
            if sign_filter > 0 and b["totalEstFlowUsd"] <= 0:
                continue
            if sign_filter < 0 and b["totalEstFlowUsd"] >= 0:
                continue
            b["funds"].sort(key=lambda x: abs(x.get("estFlowUsd", x["valueDeltaUsd"])), reverse=True)
            b["funds"] = b["funds"][:6]
            out.append(b)
        out.sort(
            key=lambda x: (
                x["fundCount"],
                abs(x["totalEstFlowUsd"]) if sort_by_abs_delta else max(x["totalCurrValueUsd"], abs(x["totalEstFlowUsd"])),
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
        "consensusNewInitiations": _agg_consensus(("NEW",), ("EQUITY",), min_funds=2, limit=12, sign_filter=1),
        "consensusHeavyAdds": _agg_consensus(("ADDED",), ("EQUITY",), min_funds=2, limit=12, sort_by_abs_delta=True, sign_filter=1),
        "consensusExitsAndTrims": _agg_consensus(("EXITED", "TRIMMED"), ("EQUITY",), min_funds=2, limit=12, sort_by_abs_delta=True, sign_filter=-1),
        "consensusOptionMoves": _agg_consensus(("NEW", "ADDED", "TRIMMED", "EXITED"), ("CALL", "PUT"), min_funds=1, limit=12, sort_by_abs_delta=True),
    }


def extract_canon_equity_shares(
    rows: list[dict[str, Any]],
    cusip_meta: dict[str, dict[str, Any]],
    excluded_etfs: set[str] | frozenset[str] = NON_STOCK_ETFS,
) -> dict[str, float]:
    out: dict[str, float] = {}
    for r in rows:
        if r.get("secType") != "EQUITY" or float(r.get("valueUsd", 0.0)) <= 0:
            continue
        meta = cusip_meta.get(r["cusip"], {})
        raw_t = meta.get("ticker") or ""
        if not raw_t or raw_t.startswith("CUSIP:"):
            continue
        t = DUAL_CLASS_CANONICAL.get(raw_t, raw_t)
        if _is_excluded_etf_entry(t, meta, excluded_etfs):
            continue
        out[t] = out.get(t, 0.0) + float(r.get("shares", 0.0))
    return out


def build_fund_timelines(
    filings: list[dict[str, Any]],
    cusip_meta: dict[str, dict[str, Any]],
    cal_dates: list[str],
    n_dates: int,
    excluded_etfs: set[str] | frozenset[str] | None = None,
) -> tuple[
    list[tuple[int, dict[str, float]]],
    list[tuple[int, dict[str, float]]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    float | None,
]:
    """
    Construct Quarter-End and zero-look-ahead 45-Day Filing-Lag timelines +
    point-in-time backtest snapshots + quarterly history + avg turnover %.
    Strictly respects 13F-HR/A amendment disclosure timestamps (`amendmentEvents`)
    and discards out-of-order stale back-quarter restatements in live timelines.
    """
    etf_set = excluded_etfs if excluded_etfs is not None else _build_excluded_etf_tickers(cusip_meta)
    qe_segments: list[tuple[int, dict[str, float]]] = []
    qe_snapshots: list[dict[str, Any]] = []
    quarterly_hist: list[dict[str, Any]] = []
    weights_seq: list[dict[str, float]] = []

    raw_events: list[tuple[str, str, int, list[dict[str, Any]]]] = []

    for fl in sorted(filings, key=lambda x: x["reportDate"]):
        rd = fl["reportDate"]
        w, eq_aum, _, _, pos_cnt, _, top10_pct, tech_pct, max_1, _ = extract_equity_weights(
            fl, cusip_meta, excluded_etfs=etf_set
        )
        if w:
            hhi = sum(v * v for v in w.values())
            eff_n = (1.0 / hhi) if hhi > 0 else float(pos_cnt)
            qe_idx = find_idx_on_or_after(cal_dates, rd)
            if qe_idx is None:
                qe_idx = n_dates - 1
            if not qe_segments or qe_idx > qe_segments[-1][0]:
                qe_segments.append((qe_idx, w))
            elif qe_idx == qe_segments[-1][0]:
                qe_segments[-1] = (qe_idx, w)

            qe_snapshots.append({
                "effDate": rd,
                "reportDate": rd,
                "weights": w,
                "shares": extract_canon_equity_shares(fl["rows"], cusip_meta, excluded_etfs=etf_set),
                "eqAum": eq_aum,
                "techPct": tech_pct,
                "effN": eff_n,
                "maxSingle": max_1,
                "posCount": pos_cnt,
            })
            quarterly_hist.append({
                "quarter": quarter_label(rd),
                "periodEnd": rd,
                "filingDate": fl["filingDate"],
                "equityAumUsd": round(eq_aum, 2),
                "positionCount": pos_cnt,
                "top10WeightPct": round(top10_pct, 2),
                "techTmtPct": round(tech_pct, 2),
                "qeIdx": qe_idx,
            })
            weights_seq.append(w)

        events = fl.get("amendmentEvents")
        if events:
            for ev in events:
                raw_events.append((
                    ev["filingDate"],
                    rd,
                    0 if ev.get("isReset", True) else 1,
                    ev["rows"],
                ))
        else:
            raw_events.append((fl["filingDate"], rd, 0, fl["rows"]))

    turnovers: list[float] = []
    for i in range(1, len(weights_seq)):
        pw, cw = weights_seq[i - 1], weights_seq[i]
        keys = set(pw.keys()) | set(cw.keys())
        to = 0.5 * sum(abs(cw.get(k, 0.0) - pw.get(k, 0.0)) for k in keys) * 100.0
        turnovers.append(to)
    avg_turnover_pct = clean_num(sum(turnovers) / len(turnovers), 1) if turnovers else None

    # Chronological zero-look-ahead disclosure timeline
    raw_events.sort(key=lambda x: (x[0], x[1], x[2]))
    lag_segments: list[tuple[int, dict[str, float]]] = []
    lag_snapshots: list[dict[str, Any]] = []
    latest_disclosed_rd = ""
    period_rows_so_far: dict[tuple[str, str], dict[str, Any]] = {}

    for ev_fd, fl_rd, is_inc_flag, ev_rows in raw_events:
        # Ignore late back-quarter restatements for periods older than already disclosed newer quarter
        if fl_rd < latest_disclosed_rd:
            continue
        if fl_rd > latest_disclosed_rd or is_inc_flag == 0:
            latest_disclosed_rd = fl_rd
            period_rows_so_far = {(r["cusip"], r["secType"]): dict(r) for r in ev_rows}
        else:
            # Confidential treatment NEW HOLDINGS amendment accumulates forward from ev_fd
            for r in ev_rows:
                k = (r["cusip"], r["secType"])
                if k not in period_rows_so_far:
                    period_rows_so_far[k] = dict(r)
                else:
                    period_rows_so_far[k]["valueUsd"] = float(period_rows_so_far[k]["valueUsd"]) + float(r["valueUsd"])
                    period_rows_so_far[k]["shares"] = float(period_rows_so_far[k]["shares"]) + float(r["shares"])

        rows_now = list(period_rows_so_far.values())
        w_lag, eq_aum, _, _, pos_cnt, _, _, tech_pct, max_1, _ = extract_weights_from_rows(
            rows_now, cusip_meta, excluded_etfs=etf_set
        )
        if not w_lag:
            continue
        hhi = sum(v * v for v in w_lag.values())
        eff_n = (1.0 / hhi) if hhi > 0 else float(pos_cnt)
        lag_idx = find_idx_on_or_after(cal_dates, ev_fd)
        if lag_idx is None:
            lag_idx = n_dates - 1
        if not lag_segments or lag_idx > lag_segments[-1][0]:
            lag_segments.append((lag_idx, w_lag))
        elif lag_idx == lag_segments[-1][0]:
            lag_segments[-1] = (lag_idx, w_lag)

        lag_snapshots.append({
            "effDate": ev_fd,
            "reportDate": fl_rd,
            "weights": w_lag,
            "shares": extract_canon_equity_shares(rows_now, cusip_meta, excluded_etfs=etf_set),
            "eqAum": eq_aum,
            "techPct": tech_pct,
            "effN": eff_n,
            "maxSingle": max_1,
            "posCount": pos_cnt,
        })

    return qe_segments, lag_segments, qe_snapshots, lag_snapshots, quarterly_hist, avg_turnover_pct


def build_signal_screener(
    funds_out: list[dict[str, Any]],
    fund_updates_out: list[dict[str, Any]],
    cusip_meta: dict[str, dict[str, Any]],
    aligned_prices: dict[str, tuple[np.ndarray, int, int]],
    win_indices: dict[str, int],
) -> dict[str, Any]:
    """
    Compute 4 actionable QoQ & positioning signal screens across leaderboard-eligible managers:
      1. Cluster Initiations (>=2 fresh NEW buys)
      2. High-Conviction Adds (strongest net accumulation impulse)
      3. Smart-Money Exits & Cuts (consensus exits/trims)
      4. Contrarian Specialist Bets (2-6 holders with >=3.0% Elite/Qualified weight)
    """
    lb_funds = [f for f in funds_out if f.get("isLeaderboardEligible", True)]
    lb_by_cik = {f["cik"]: f for f in lb_funds}
    elite_ciks = {f["cik"] for f in lb_funds if f.get("fundTier") == "Elite 50"}
    etf_set = _build_excluded_etf_tickers(cusip_meta)

    # Canonical current holdings aggregation across eligible funds
    stock_pos: dict[str, dict[str, Any]] = {}
    for f in lb_funds:
        cik = f["cik"]
        is_elite = cik in elite_ciks
        merged: dict[str, dict[str, Any]] = {}
        for h in f["holdings"]:
            if h["securityType"] != "EQUITY" or h["weightPct"] <= 0:
                continue
            raw_t = cusip_meta.get(h["cusip"], {}).get("ticker") or h["ticker"]
            if not raw_t or raw_t.startswith("CUSIP:"):
                continue
            t = DUAL_CLASS_CANONICAL.get(raw_t, raw_t)
            if _is_excluded_etf_entry(t, h, etf_set):
                continue
            slot = merged.get(t)
            if slot is None:
                merged[t] = {
                    "ticker": t,
                    "name": h["name"],
                    "sector": h["sector"] or "Other",
                    "industry": h["industry"] or "",
                    "isTechTmt": bool(h.get("isTechTmt")),
                    "weightPct": float(h["weightPct"]),
                    "valueUsd": float(h["valueUsd"]),
                    "status": h["status"],
                }
            else:
                slot["weightPct"] += float(h["weightPct"])
                slot["valueUsd"] += float(h["valueUsd"])
                slot["isTechTmt"] = slot["isTechTmt"] or bool(h.get("isTechTmt"))

        for t, hm in merged.items():
            rec = stock_pos.setdefault(t, {
                "ticker": t,
                "name": hm["name"],
                "sector": hm["sector"],
                "industry": hm["industry"],
                "isTechTmt": hm["isTechTmt"],
                "holderCount": 0,
                "eliteHoldersCount": 0,
                "totalValueUsd": 0.0,
                "weights": [],
                "backers": [],
            })
            rec["holderCount"] += 1
            if is_elite:
                rec["eliteHoldersCount"] += 1
            rec["totalValueUsd"] += hm["valueUsd"]
            w_p = round(hm["weightPct"], 2)
            rec["weights"].append(w_p)
            rec["backers"].append({
                "cik": cik,
                "fundName": f["name"],
                "fundTier": f.get("fundTier", "Qualified"),
                "weightPct": w_p,
                "status": hm["status"],
            })

    # Choose active season updates among eligible funds
    lb_updates = [u for u in fund_updates_out if u["cik"] in lb_by_cik]
    q_labels = sorted({u["quarterLabel"] for u in lb_updates}, reverse=True)
    if q_labels:
        top_q_updates = [u for u in lb_updates if u["quarterLabel"] == q_labels[0]]
        season_updates = top_q_updates if len(top_q_updates) >= 15 else [
            u for u in lb_updates if u["quarterLabel"] in set(q_labels[:2])
        ]
    else:
        season_updates = []

    move_agg: dict[str, dict[str, Any]] = {}
    for u in season_updates:
        cik = u["cik"]
        f = lb_by_cik[cik]
        is_elite = cik in elite_ciks
        per_fund: dict[str, dict[str, Any]] = {}
        for mv in u["allMoves"]:
            if mv["securityType"] != "EQUITY":
                continue
            raw_t = mv["ticker"]
            if not raw_t or raw_t.startswith("CUSIP:"):
                continue
            t = DUAL_CLASS_CANONICAL.get(raw_t, raw_t)
            if _is_excluded_etf_entry(t, mv, etf_set):
                continue
            slot = per_fund.setdefault(t, {
                "ticker": t,
                "name": mv["name"],
                "sector": mv["sector"] or "Other",
                "industry": mv["industry"] or "",
                "prevSh": 0.0,
                "currSh": 0.0,
                "prevVal": 0.0,
                "currVal": 0.0,
                "valDelta": 0.0,
                "estFlow": 0.0,
                "currBookPct": 0.0,
                "prevBookPct": 0.0,
            })
            slot["prevSh"] += float(mv["prevShares"])
            slot["currSh"] += float(mv["currShares"])
            slot["prevVal"] += float(mv["prevValueUsd"])
            slot["currVal"] += float(mv["currValueUsd"])
            slot["valDelta"] += float(mv["valueDeltaUsd"])
            slot["estFlow"] += float(mv.get("estFlowUsd", mv["valueDeltaUsd"]))
            slot["currBookPct"] += float(mv["currBookPct"])
            slot["prevBookPct"] += float(mv["prevBookPct"])

        for t, s in per_fund.items():
            if s["prevSh"] <= 0:
                act = "NEW"
                sh_pct = None
            elif s["currSh"] <= 0:
                act = "EXITED"
                sh_pct = -100.0
            else:
                dp = ((s["currSh"] / s["prevSh"]) - 1.0) * 100.0
                sh_pct = clean_num(dp, 1)
                if dp >= 5.0:
                    act = "ADDED"
                elif dp <= -5.0:
                    act = "TRIMMED"
                else:
                    act = "UNCHANGED"
            if act == "UNCHANGED":
                continue

            ma = move_agg.setdefault(t, {
                "ticker": t,
                "name": s["name"],
                "sector": s["sector"],
                "industry": s["industry"],
                "newCount": 0,
                "eliteNewCount": 0,
                "addedCount": 0,
                "trimmedCount": 0,
                "exitedCount": 0,
                "eliteSellCount": 0,
                "netValueDeltaUsd": 0.0,
                "netEstFlowUsd": 0.0,
                "totalNewValueUsd": 0.0,
                "totalSellOutflowUsd": 0.0,
                "movers": [],
            })
            ma["netValueDeltaUsd"] += s["valDelta"]
            ma["netEstFlowUsd"] += s["estFlow"]
            if act == "NEW":
                ma["newCount"] += 1
                ma["totalNewValueUsd"] += s["currVal"]
                if is_elite:
                    ma["eliteNewCount"] += 1
            elif act == "ADDED":
                ma["addedCount"] += 1
            elif act == "TRIMMED":
                ma["trimmedCount"] += 1
                ma["totalSellOutflowUsd"] += max(0.0, -s["estFlow"], -s["valDelta"])
                if is_elite:
                    ma["eliteSellCount"] += 1
            elif act == "EXITED":
                ma["exitedCount"] += 1
                ma["totalSellOutflowUsd"] += s["prevVal"]
                if is_elite:
                    ma["eliteSellCount"] += 1

            ma["movers"].append({
                "cik": cik,
                "fundName": f["name"],
                "fundTier": f.get("fundTier", "Qualified"),
                "action": act,
                "currBookPct": round(s["currBookPct"], 2),
                "prevBookPct": round(s["prevBookPct"], 2),
                "valueDeltaUsd": round(s["valDelta"], 2),
                "estFlowUsd": round(s["estFlow"], 2),
                "sharesDeltaPct": sh_pct,
            })

    def _build_entry(t: str) -> dict[str, Any]:
        sp = stock_pos.get(t, {})
        ma = move_agg.get(t, {})
        name = sp.get("name") or ma.get("name") or t
        sector = sp.get("sector") or ma.get("sector") or "Other"
        industry = sp.get("industry") or ma.get("industry") or ""
        is_tt = bool(sp.get("isTechTmt") or is_tech_tmt(t, sector, industry))
        px_usd = None
        ttm_ret = None
        if t in aligned_prices:
            arr, fi, li = aligned_prices[t]
            px_usd = clean_num(float(arr[li]), 2)
            if fi <= win_indices["TTM"] + 5:
                si = max(win_indices["TTM"], fi)
                ttm_ret = clean_num(((arr[li] / arr[si]) - 1.0) * 100.0, 2)

        weights = sp.get("weights") or []
        movers_sorted = sorted(
            ma.get("movers", []),
            key=lambda m: (m.get("fundTier") == "Elite 50", abs(m.get("estFlowUsd", m["valueDeltaUsd"]))),
            reverse=True,
        )
        backers_sorted = sorted(
            sp.get("backers", []),
            key=lambda b: (b.get("fundTier") == "Elite 50", b["weightPct"]),
            reverse=True,
        )
        return {
            "ticker": t,
            "name": name,
            "sector": sector,
            "subSector": classify_sub_sector(sector, industry, is_tt),
            "isTechTmt": is_tt,
            "priceUsd": px_usd,
            "stockTtmPct": ttm_ret,
            "holderCount": sp.get("holderCount", 0),
            "eliteHoldersCount": sp.get("eliteHoldersCount", 0),
            "totalValueUsd": round(sp.get("totalValueUsd", 0.0), 2),
            "avgWeightPct": round(sum(weights) / len(weights), 2) if weights else 0.0,
            "maxWeightPct": round(max(weights), 2) if weights else 0.0,
            "newCount": ma.get("newCount", 0),
            "eliteNewCount": ma.get("eliteNewCount", 0),
            "addedCount": ma.get("addedCount", 0),
            "trimmedCount": ma.get("trimmedCount", 0),
            "exitedCount": ma.get("exitedCount", 0),
            "netDeltaUsd": round(ma.get("netValueDeltaUsd", 0.0), 2),
            "netEstFlowUsd": round(ma.get("netEstFlowUsd", 0.0), 2),
            "movers": movers_sorted[:6],
            "topBackers": backers_sorted[:6],
        }

    cluster_keys = sorted(
        [t for t, ma in move_agg.items() if ma["newCount"] >= 2],
        key=lambda t: (
            move_agg[t]["eliteNewCount"],
            move_agg[t]["newCount"],
            move_agg[t]["totalNewValueUsd"],
        ),
        reverse=True,
    )[:25]

    heavy_add_keys = sorted(
        [
            t for t, ma in move_agg.items()
            if (ma["newCount"] + ma["addedCount"]) >= 2
            and (ma["newCount"] + ma["addedCount"]) > (ma["trimmedCount"] + ma["exitedCount"])
            and ma["netEstFlowUsd"] > 0
        ],
        key=lambda t: (
            stock_pos.get(t, {}).get("eliteHoldersCount", 0) > 0,
            1.3 * move_agg[t]["newCount"] + move_agg[t]["addedCount"] - move_agg[t]["trimmedCount"] - 1.2 * move_agg[t]["exitedCount"],
            move_agg[t]["netEstFlowUsd"],
        ),
        reverse=True,
    )[:25]

    exit_keys = sorted(
        [
            t for t, ma in move_agg.items()
            if (ma["exitedCount"] + ma["trimmedCount"]) >= 2
            and (ma["exitedCount"] + ma["trimmedCount"]) > (ma["newCount"] + ma["addedCount"])
            and ma["netEstFlowUsd"] < 0
        ],
        key=lambda t: (
            1.5 * move_agg[t]["exitedCount"] + move_agg[t]["trimmedCount"],
            move_agg[t]["eliteSellCount"],
            move_agg[t]["totalSellOutflowUsd"],
        ),
        reverse=True,
    )[:25]

    contrarian_keys = sorted(
        [
            t for t, sp in stock_pos.items()
            if 2 <= sp["holderCount"] <= 6
            and sp["eliteHoldersCount"] >= 1
            and (max(sp["weights"]) if sp["weights"] else 0.0) >= 3.0
        ],
        key=lambda t: (
            stock_pos[t]["eliteHoldersCount"],
            max(stock_pos[t]["weights"]),
            sum(stock_pos[t]["weights"]) / max(1, len(stock_pos[t]["weights"])),
        ),
        reverse=True,
    )[:25]

    return {
        "quarterLabel": q_labels[0] if q_labels else "Latest",
        "clusterInitiations": [_build_entry(t) for t in cluster_keys],
        "heavyAdds": [_build_entry(t) for t in heavy_add_keys],
        "smartMoneyExits": [_build_entry(t) for t in exit_keys],
        "contrarianBets": [_build_entry(t) for t in contrarian_keys],
    }


def main() -> None:
    print("=== Paper Trail 13F Pipeline Starting ===")
    today_date = datetime.now(timezone.utc).date()
    known_ciks = {f["cik"] for f in CURATED_FUNDS}

    # Stage 1: Sync SEC Form 13F Bulk Data Sets (2021-present) & scan 100% of Form 13F CIKs
    bulk_paths = sync_bulk_zips()
    all_candidates, stage1_results, funnel_counts, post_bulk_by_cik = discover_universe_via_bulk(
        CURATED_FUNDS,
        bulk_paths,
        today_date,
        min_history_report_date="2020-12-31",
    )
    hist_liq_ciks: list[str] = funnel_counts.pop("_historicalLiquidatedCiks", [])

    if len(stage1_results) < 25:
        raise RuntimeError(
            f"Aborting build_data.py before modifying output: only {len(stage1_results)} candidates passed Stage 1."
        )

    # Stage 1b: Mathematical Tech/TMT >= 35% bound check + Materiality-prioritized CUSIP resolution
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
        nm = tmeta.get("name") or entry.get("name", "")
        sec = tmeta.get("sector") or entry.get("sector", "")
        ind = tmeta.get("industry") or entry.get("industry", "")
        if is_non_stock_etf(t, nm, sec, ind):
            return False
        if is_tech_tmt(t, sec, ind):
            return True
        if sec or tmeta.get("metaChecked"):
            return False
        return None

    cc, tm = _build_cached_lookup()
    pre_prune_ciks: list[str] = []
    material_stage1_cusips: dict[str, dict[str, str]] = {}
    material_stage1_priority: dict[str, float] = {}

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
        if not is_curated and max_possible_tech < 35.0:
            continue

        pre_prune_ciks.append(cik)
        for idx_r, r in enumerate(eq_rows):
            if is_curated or idx_r < 40 or (eq_aum > 0 and r["valueUsd"] / eq_aum >= 0.004):
                c_id = r["cusip"]
                if c_id not in material_stage1_cusips:
                    material_stage1_cusips[c_id] = {"issuer": r["issuer"], "title": r.get("title", "")}
                material_stage1_priority[c_id] = material_stage1_priority.get(c_id, 0.0) + float(r["valueUsd"])

    print(
        f"[stage-1b] {len(pre_prune_ciks)} candidates cleared mathematical Tech/TMT >=35% ceiling; "
        f"resolving {len(material_stage1_cusips)} latest-filing CUSIPs...",
        flush=True,
    )
    if material_stage1_cusips:
        resolve_cusips(material_stage1_cusips, cusip_priority=material_stage1_priority)

    cc, tm = _build_cached_lookup()
    borderline_cusips: dict[str, dict[str, str]] = {}
    borderline_priority: dict[str, float] = {}
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
        if max_tech < 35.0:
            continue
        stage1_survivors_raw.append(info)
        if min_tech < 35.0:
            for r in eq_rows:
                c_id = r["cusip"]
                if _cached_tech_state(c_id, cc, tm) is None:
                    if c_id not in borderline_cusips:
                        borderline_cusips[c_id] = {"issuer": r["issuer"], "title": r.get("title", "")}
                    borderline_priority[c_id] = borderline_priority.get(c_id, 0.0) + float(r["valueUsd"])

    if borderline_cusips:
        print(
            f"[stage-1b] Resolving {len(borderline_cusips)} tail equity CUSIPs for borderline Tech/TMT >= 35% funds...",
            flush=True,
        )
        resolve_cusips(borderline_cusips, cusip_priority=borderline_priority)

    cc, tm = _build_cached_lookup()
    stage2_targets_raw: list[dict[str, Any]] = []
    for info in stage1_survivors_raw:
        cand = info["cand"]
        is_curated = cand.get("source") == "Curated Core"
        if is_curated:
            stage2_targets_raw.append(cand)
            continue
        eq_aum = info["eqAum"]
        eq_rows = info["eqRows"]
        tech_val = sum(
            r["valueUsd"]
            for r in eq_rows
            if _cached_tech_state(r["cusip"], cc, tm) is True
        )
        exact_tech_pct = (tech_val / eq_aum * 100.0) if eq_aum > 0 else 0.0
        if exact_tech_pct >= 35.0:
            stage2_targets_raw.append(cand)

    # Verify SEC entityType/SIC on non-curated Stage 2 survivors to exclude operating corps
    excluded_ops = filter_operating_entities([c["cik"] for c in stage2_targets_raw], known_ciks)
    if excluded_ops:
        print(f"[universe] Excluded {len(excluded_ops)} operating companies verified via SEC submissions metadata.", flush=True)
        funnel_counts["excludedInstitutionsCount"] += len(excluded_ops)
        funnel_counts["nonExcludedManagerCount"] = max(0, funnel_counts["nonExcludedManagerCount"] - len(excluded_ops))
        funnel_counts["fourPlusQuartersCount"] = max(0, funnel_counts["fourPlusQuartersCount"] - len(excluded_ops))
        funnel_counts["sizeAndEquityPassCount"] = max(0, funnel_counts["sizeAndEquityPassCount"] - len(excluded_ops))
        funnel_counts["concentrationPassCount"] = max(0, funnel_counts["concentrationPassCount"] - len(excluded_ops))
    stage2_targets = [c for c in stage2_targets_raw if c["cik"] not in excluded_ops]
    funnel_counts["techFloorPassCount"] = len(stage2_targets)

    # Include closed/liquidated historical managers (>=4Q) from bulk datasets for survivorship-free PIT backtest
    stage2_ciks_set = {c["cik"] for c in stage2_targets}
    hist_liq_set = {c for c in hist_liq_ciks if c not in stage2_ciks_set and c not in excluded_ops}
    all_stage2_ciks = [c["cik"] for c in stage2_targets] + sorted(hist_liq_set)

    # Stage 2: Offline Bulk Data Set historical extraction + Post-Bulk .txt XML ingestion
    print(
        f"\n[stage-2] Assembling full 2021-present histories for {len(stage2_targets)} surviving funds "
        f"(+{len(hist_liq_set)} historical closed PIT funds)...",
        flush=True,
    )
    raw_fund_filings, pb_stats = ingest_target_histories(
        target_ciks=all_stage2_ciks,
        bulk_paths=bulk_paths,
        post_bulk_by_cik=post_bulk_by_cik,
        today_date=today_date,
        min_report_date="2020-12-31",
        allow_stale_ciks=hist_liq_set,
    )
    funnel_counts["postBulkFilingsCount"] = pb_stats["postBulkFilingsCount"]

    if len(raw_fund_filings) < 25:
        raise RuntimeError(
            f"Aborting build_data.py before modifying output: only {len(raw_fund_filings)} funds assembled in Stage 2."
        )

    from sec_client import WARRANT_TITLE_RE
    all_cusips: dict[str, dict[str, str]] = {}
    all_cusip_priority: dict[str, float] = {}
    for cik_r, filings in raw_fund_filings.items():
        is_live_target = cik_r not in hist_liq_set
        for idx_fl, fl in enumerate(filings):
            is_recent_q = is_live_target and idx_fl >= len(filings) - 2
            rec_mult = 5.0 if is_recent_q else 1.0
            for row in fl["rows"]:
                if row["secType"] == "EQUITY" or is_recent_q:
                    c_id = row["cusip"]
                    title_s = row.get("title", "")
                    if c_id not in all_cusips:
                        all_cusips[c_id] = {"issuer": row["issuer"], "title": title_s}
                    tit_mult = 0.15 if (title_s and WARRANT_TITLE_RE.search(title_s)) else 1.0
                    all_cusip_priority[c_id] = all_cusip_priority.get(c_id, 0.0) + float(row["valueUsd"]) * rec_mult * tit_mult

    # Step 2: Resolve all historical equity + recent option CUSIPs -> Tickers, Sectors, Industries
    cusip_meta = resolve_cusips(
        all_cusips,
        cusip_priority=all_cusip_priority,
        max_openfigi_batches=15,
        max_fast_us=800,
    )
    excluded_etf_tickers = _build_excluded_etf_tickers(cusip_meta)

    # Step 3: Exact Tech-Focused & Concentrated qualification audit + Rolling Tech persistence
    strict_pass_cands: list[dict[str, Any]] = []
    prelim_update_cands: list[dict[str, Any]] = []
    audit_meta_by_cik: dict[str, dict[str, float]] = {}
    needed_tickers: set[str] = {"QQQ", "SPY", "XLK"}
    excluded_mm_stage3 = 0

    print(
        "\n=== Fund Qualification Audit (Rolling Tech >= 35% Disc / >= 30% Curated, $200M+ Rep / $100M+ Single-Stock Eq, "
        "EffN/Breadth Check, Qtrs >= 4) ==="
    )
    for cand in stage2_targets:
        cik = cand["cik"]
        if cik not in raw_fund_filings:
            continue
        filings = raw_fund_filings[cik]
        latest = filings[-1]
        is_curated = cand.get("source") == "Curated Core"

        opt_rows = [r for r in latest["rows"] if r["secType"] in ("PUT", "CALL") and r["valueUsd"] > 0]
        opt_aum = sum(r["valueUsd"] for r in opt_rows)
        rep_check = sum(r["valueUsd"] for r in latest["rows"] if r["valueUsd"] > 0)
        if not is_curated and ((rep_check > 0 and opt_aum / rep_check > 0.50) or len(opt_rows) > 300):
            excluded_mm_stage3 += 1
            continue

        weights, eq_aum, total_eq_aum, rep_aum, pos_cnt, sig_pos_cnt, top10_pct, tech_pct, max_1, etf_pct = extract_equity_weights(
            latest, cusip_meta, excluded_etfs=excluded_etf_tickers
        )

        tech_floor = 30.0 if is_curated else 35.0
        persist_med_floor = 40.0 if is_curated else 50.0
        hhi = sum(w * w for w in weights.values())
        eff_n = (1.0 / hhi) if hhi > 0 else float(pos_cnt)
        min_pos_ok = pos_cnt >= 5 and (is_curated or eff_n >= 1.5)
        is_conc = min_pos_ok and (is_curated or pos_cnt <= 250) and (
            pos_cnt <= 120
            or (sig_pos_cnt <= 95 and eff_n <= 55.0 and top10_pct >= 35.0)
            or (pos_cnt <= 250 and top10_pct >= 55.0)
        )
        is_size = (
            rep_aum >= 200_000_000
            and eq_aum >= 100_000_000
            and (total_eq_aum > 0 and (eq_aum / total_eq_aum) >= 0.50)
        )
        has_hist = len(filings) >= 4

        is_tech = tech_pct >= tech_floor
        avg_4q_max1 = max_1
        med_4q_tech = tech_pct
        if is_tech and is_conc and is_size and has_hist:
            tech_vals: list[float] = []
            max1_vals: list[float] = []
            for hist_fl in filings[-12:]:
                hw, h_eq, _, _, _, _, _, h_tech, h_max1, _ = extract_equity_weights(
                    hist_fl, cusip_meta, excluded_etfs=excluded_etf_tickers
                )
                if hw and h_eq > 0:
                    tech_vals.append(h_tech)
                    max1_vals.append(h_max1)
            if tech_vals:
                med_4q_tech = float(statistics.median(tech_vals[-4:]))
                persist_frac = sum(1 for v in tech_vals if v >= tech_floor) / len(tech_vals)
                avg_4q_max1 = sum(max1_vals[-4:]) / len(max1_vals[-4:])
                is_tech = is_tech and (med_4q_tech >= persist_med_floor) and (persist_frac >= 0.75)

        status_str = "PASS" if (is_tech and is_conc and is_size and has_hist) else "FILTERED"
        if is_curated or status_str == "PASS":
            print(
                f"  {status_str:8s} | {cand['name'][:26]:26s} | {cand.get('source', '')[:7]:7s} | "
                f"Eq=${eq_aum/1e9:5.2f}B | EffN={eff_n:4.1f} | pos={pos_cnt:3d} | "
                f"top10={top10_pct:4.1f}% | tech={tech_pct:4.1f}% (4Qmed {med_4q_tech:4.1f}%) | q={len(filings):2d}"
            )
        if status_str == "PASS":
            strict_pass_cands.append(cand)
            audit_meta_by_cik[cik] = {
                "avg4qMaxSingle": avg_4q_max1,
                "maxSinglePct": max_1,
                "effN": eff_n,
                "etfSharePct": etf_pct,
            }
            for fl in filings:
                for row in fl["rows"]:
                    if row["secType"] == "EQUITY":
                        raw_t = cusip_meta.get(row["cusip"], {}).get("ticker", "")
                        if raw_t and not raw_t.startswith("CUSIP:"):
                            canon_t = DUAL_CLASS_CANONICAL.get(raw_t, raw_t)
                            if canon_t not in excluded_etf_tickers:
                                needed_tickers.add(raw_t)
                                needed_tickers.add(canon_t)
        elif is_curated and len(filings) >= 2 and rep_aum >= 200_000_000:
            prelim_update_cands.append(cand)
            for fl in filings[-2:]:
                for row in fl["rows"]:
                    if row["secType"] == "EQUITY":
                        raw_t = cusip_meta.get(row["cusip"], {}).get("ticker", "")
                        if raw_t and not raw_t.startswith("CUSIP:"):
                            canon_t = DUAL_CLASS_CANONICAL.get(raw_t, raw_t)
                            if canon_t not in excluded_etf_tickers:
                                needed_tickers.add(raw_t)
                                needed_tickers.add(canon_t)

    if excluded_mm_stage3:
        funnel_counts["excludedInstitutionsCount"] += excluded_mm_stage3
        funnel_counts["techFloorPassCount"] = max(0, funnel_counts.get("techFloorPassCount", 0) - excluded_mm_stage3)

    # Identify historical non-surviving Stage 2 & closed/liquidated CIKs that met Tech criteria for survivorship-free backtest
    strict_ciks_set = {c["cik"] for c in strict_pass_cands}
    pit_extra_ciks: list[str] = []
    pit_extra_seen: set[str] = set()
    for cand in stage2_targets:
        cik = cand["cik"]
        if cik in strict_ciks_set or cik not in raw_fund_filings or cik in pit_extra_seen:
            continue
        filings = raw_fund_filings[cik]
        if len(filings) < 4:
            continue
        ever_tech = False
        for fl in filings:
            w_h, eq_h, _, _, pos_h, _, _, tech_h, _, _ = extract_equity_weights(
                fl, cusip_meta, excluded_etfs=excluded_etf_tickers
            )
            if w_h and eq_h >= 100_000_000 and tech_h >= 30.0 and pos_h >= 5:
                ever_tech = True
                break
        if ever_tech:
            pit_extra_seen.add(cik)
            pit_extra_ciks.append(cik)
            for fl in filings:
                for row in fl["rows"]:
                    if row["secType"] == "EQUITY":
                        raw_t = cusip_meta.get(row["cusip"], {}).get("ticker", "")
                        if raw_t and not raw_t.startswith("CUSIP:"):
                            canon_t = DUAL_CLASS_CANONICAL.get(raw_t, raw_t)
                            if canon_t not in excluded_etf_tickers:
                                needed_tickers.add(raw_t)
                                needed_tickers.add(canon_t)

    hist_added_cnt = 0
    for cik_h in sorted(hist_liq_set):
        if cik_h in strict_ciks_set or cik_h in pit_extra_seen or cik_h not in raw_fund_filings:
            continue
        filings = raw_fund_filings[cik_h]
        if len(filings) < 4:
            continue
        qual_q_cnt = 0
        for fl in filings:
            w_h, eq_h, _, _, pos_h, _, _, tech_h, _, _ = extract_equity_weights(
                fl, cusip_meta, excluded_etfs=excluded_etf_tickers
            )
            hhi_h = sum(w * w for w in w_h.values()) if w_h else 0.0
            eff_n_h = (1.0 / hhi_h) if hhi_h > 0 else float(pos_h)
            if w_h and eq_h >= 100_000_000 and tech_h >= 35.0 and pos_h >= 5 and eff_n_h >= 3.5:
                qual_q_cnt += 1
        if qual_q_cnt >= 2:
            pit_extra_seen.add(cik_h)
            pit_extra_ciks.append(cik_h)
            hist_added_cnt += 1
            for fl in filings:
                for row in fl["rows"]:
                    if row["secType"] == "EQUITY":
                        raw_t = cusip_meta.get(row["cusip"], {}).get("ticker", "")
                        if raw_t and not raw_t.startswith("CUSIP:"):
                            canon_t = DUAL_CLASS_CANONICAL.get(raw_t, raw_t)
                            if canon_t not in excluded_etf_tickers:
                                needed_tickers.add(raw_t)
                                needed_tickers.add(canon_t)

    if pit_extra_ciks:
        print(
            f"[pit-backtest] Including {len(pit_extra_ciks)} non-surviving historical funds "
            f"({hist_added_cnt} defunct/liquidated) in survivorship-free PIT universe.",
            flush=True,
        )
    funnel_counts["pitHistoricalBacktestCount"] = len(pit_extra_ciks)
    funnel_counts["strictQualificationCount"] = len(strict_pass_cands)

    # Step 5: Fetch historical daily adjusted prices, splits & OHLCV + ^IRX risk-free yield
    sorted_needed_tickers = sorted(needed_tickers)
    raw_prices = fetch_all_prices(sorted_needed_tickers)
    splits_map = load_cached_splits(sorted_needed_tickers)
    qqq_dict = raw_prices.get("QQQ") or {}
    cal_dates = sorted(qqq_dict.keys())
    if len(cal_dates) < 252:
        raise RuntimeError("Insufficient QQQ trading calendar dates fetched from Yahoo Finance.")

    aligned_prices = build_aligned_price_matrix(cal_dates, raw_prices)
    n_dates = len(cal_dates)
    win_indices = compute_window_start_indices(cal_dates)
    as_of_date = cal_dates[-1]

    rf_map = fetch_risk_free_daily_rates(cal_dates)
    rf_ann_arr = np.array([rf_map.get(d, 4.0) for d in cal_dates], dtype=np.float64)
    print(f"\n[calendar] Trading days: {len(cal_dates)} ({cal_dates[0]} .. {as_of_date}) | Latest ^IRX ~{rf_ann_arr[-1]:.2f}%")
    print(f"[windows]  { {w: cal_dates[idx] for w, idx in win_indices.items()} }")

    # Post-Price Active Equity Coverage Gate (>= $100M and >= 70% of single-stock equity book actively priced >= 15d)
    qualified_cands: list[dict[str, Any]] = []
    update_cands: list[dict[str, Any]] = []
    for cand in strict_pass_cands:
        cik = cand["cik"]
        filings = raw_fund_filings[cik]
        latest = filings[-1]
        is_curated = cand.get("source") == "Curated Core"
        single_eq_rows = []
        for r in latest["rows"]:
            if r["secType"] != "EQUITY" or r["valueUsd"] <= 0:
                continue
            raw_t = cusip_meta.get(r["cusip"], {}).get("ticker") or ""
            canon_t = DUAL_CLASS_CANONICAL.get(raw_t, raw_t)
            if canon_t in excluded_etf_tickers:
                continue
            single_eq_rows.append((r, raw_t, canon_t))

        eq_aum_lat = sum(r["valueUsd"] for r, _, _ in single_eq_rows)
        rep_aum_lat = sum(r["valueUsd"] for r in latest["rows"])
        priced_eq_aum = 0.0
        for r, raw_t, canon_t in single_eq_rows:
            pdata = aligned_prices.get(canon_t) or aligned_prices.get(raw_t)
            if pdata is not None and (pdata[2] - pdata[1]) >= 15:
                priced_eq_aum += r["valueUsd"]
        priced_cov = (priced_eq_aum / eq_aum_lat) if eq_aum_lat > 0 else 0.0
        if priced_eq_aum >= 100_000_000 and priced_cov >= 0.70:
            qualified_cands.append(cand)
            update_cands.append(cand)
        else:
            print(
                f"  [post-price DROP] {cand['name'][:30]:30s} | singleEq=${eq_aum_lat/1e9:.2f}B "
                f"priced=${priced_eq_aum/1e9:.2f}B ({priced_cov*100:.1f}% < 70%)",
                flush=True,
            )
            if is_curated and len(filings) >= 2 and rep_aum_lat >= 200_000_000:
                update_cands.append(cand)

    update_cands.extend(prelim_update_cands)
    funnel_counts["pricedHistoryPassCount"] = len(qualified_cands)

    qqq_nav = aligned_prices["QQQ"][0] / aligned_prices["QQQ"][0][0] * 100.0
    spy_nav = aligned_prices["SPY"][0] / aligned_prices["SPY"][0][0] * 100.0
    xlk_nav = aligned_prices["XLK"][0] / aligned_prices["XLK"][0][0] * 100.0

    benchmarks = {}
    for b_sym, b_name, b_nav in [
        ("QQQ", "Invesco QQQ Trust (Nasdaq-100)", qqq_nav),
        ("SPY", "SPDR S&P 500 ETF Trust", spy_nav),
        ("XLK", "Technology Select Sector SPDR", xlk_nav),
    ]:
        b_rets, b_met = compute_returns_and_metrics(b_nav, qqq_nav, win_indices, rf_ann_arr=rf_ann_arr)
        benchmarks[b_sym] = {
            "ticker": b_sym,
            "name": b_name,
            "returns": b_rets,
            "cagrPct": b_met["cagrPct"],
            "maxDrawdownPct": b_met["maxDrawdownPct"],
            "sharpeProxy": b_met["sharpeProxy"],
            "sortinoRatio": b_met["sortinoRatio"],
            "curve": downsample_curve(cal_dates, b_nav, step=5),
        }

    # Precompute canonical metadata lookup for merged share classes (GOOG -> GOOGL) & latest quarter label
    canon_meta_by_ticker: dict[str, dict[str, Any]] = {}
    canon_cusip_by_ticker: dict[str, str] = {}
    for c_key, m_val in cusip_meta.items():
        tk = m_val.get("ticker", "")
        if tk and not tk.startswith("CUSIP:"):
            if tk not in canon_meta_by_ticker or m_val.get("sector"):
                canon_meta_by_ticker[tk] = m_val
                canon_cusip_by_ticker[tk] = c_key

    latest_q_overall = max(
        (quarter_label(raw_fund_filings[c["cik"]][-1]["reportDate"]) for c in qualified_cands),
        default="2026 Q3",
    )

    # Step 6: Simulate every qualified fund + build zero-look-ahead PIT histories
    funds_out: list[dict[str, Any]] = []
    fund_navs: dict[str, dict[str, np.ndarray]] = {"quarter_end": {}, "filing_lag": {}}
    pit_snapshots: dict[str, dict[str, list[dict[str, Any]]]] = {"quarter_end": {}, "filing_lag": {}}

    for cik_extra in pit_extra_ciks:
        fls_ex = raw_fund_filings[cik_extra]
        qe_seg_ex, lag_seg_ex, qe_sn_ex, lag_sn_ex, _, _ = build_fund_timelines(
            fls_ex, cusip_meta, cal_dates, n_dates, excluded_etfs=excluded_etf_tickers
        )
        pit_snapshots["quarter_end"][cik_extra] = qe_sn_ex
        pit_snapshots["filing_lag"][cik_extra] = lag_sn_ex
        fund_navs["quarter_end"][cik_extra] = simulate_nav_series(qe_seg_ex, aligned_prices, n_dates)
        fund_navs["filing_lag"][cik_extra] = simulate_nav_series(lag_seg_ex, aligned_prices, n_dates)

    for cand in qualified_cands:
        cik = cand["cik"]
        filings = raw_fund_filings[cik]
        latest = filings[-1]
        prev = filings[-2] if len(filings) >= 2 else None

        qe_segments, lag_segments, qe_snaps, lag_snaps, quarterly_hist, avg_turnover_pct = build_fund_timelines(
            filings, cusip_meta, cal_dates, n_dates, excluded_etfs=excluded_etf_tickers
        )
        pit_snapshots["quarter_end"][cik] = qe_snaps
        pit_snapshots["filing_lag"][cik] = lag_snaps

        nav_qe = simulate_nav_series(qe_segments, aligned_prices, n_dates)
        nav_lag = simulate_nav_series(lag_segments, aligned_prices, n_dates)
        fund_navs["quarter_end"][cik] = nav_qe
        fund_navs["filing_lag"][cik] = nav_lag

        # Compute quarter-by-quarter returns & Batting Average vs QQQ (independently for QE & 45d-Lag)
        q_beats = 0
        q_total = 0
        for i, qrow in enumerate(quarterly_hist):
            s_i = qrow.pop("qeIdx")
            e_i = find_idx_on_or_after(cal_dates, quarterly_hist[i + 1]["periodEnd"]) if i + 1 < len(quarterly_hist) else (n_dates - 1)
            if e_i is None:
                e_i = n_dates - 1
            if e_i > s_i and not math.isnan(nav_qe[s_i]) and not math.isnan(nav_qe[e_i]):
                qr = clean_num(((nav_qe[e_i] / nav_qe[s_i]) - 1.0) * 100.0, 2)
                qqr = clean_num(((qqq_nav[e_i] / qqq_nav[s_i]) - 1.0) * 100.0, 2)
                qrow["quarterReturnPct"] = qr
                qrow["qqqQuarterReturnPct"] = qqr
                if qr is not None and qqr is not None:
                    q_total += 1
                    if qr > qqr:
                        q_beats += 1
            else:
                qrow["quarterReturnPct"] = None
                qrow["qqqQuarterReturnPct"] = None

        lag_beats = 0
        lag_total = 0
        lag_boundaries: list[int] = [seg[0] for seg in lag_segments]
        if lag_boundaries:
            if lag_boundaries[-1] < n_dates - 1:
                lag_boundaries.append(n_dates - 1)
            prev_b = lag_boundaries[0]
            for nxt_b in lag_boundaries[1:]:
                if nxt_b - prev_b < 15:
                    continue
                if not math.isnan(nav_lag[prev_b]) and not math.isnan(nav_lag[nxt_b]) and nav_lag[prev_b] > 0:
                    lr = (nav_lag[nxt_b] / nav_lag[prev_b]) - 1.0
                    lqr = (qqq_nav[nxt_b] / qqq_nav[prev_b]) - 1.0
                    lag_total += 1
                    if lr > lqr:
                        lag_beats += 1
                prev_b = nxt_b

        batting_avg_pct = clean_num((q_beats / q_total) * 100.0, 1) if q_total >= 2 else None
        lag_batting_avg_pct = clean_num((lag_beats / lag_total) * 100.0, 1) if lag_total >= 2 else None
        rets_qe, met_qe = compute_returns_and_metrics(nav_qe, qqq_nav, win_indices, rf_ann_arr=rf_ann_arr)
        rets_lag, met_lag = compute_returns_and_metrics(nav_lag, qqq_nav, win_indices, rf_ann_arr=rf_ann_arr)
        met_qe["battingAvgPct"] = batting_avg_pct
        met_qe["avgTurnoverPct"] = avg_turnover_pct
        met_lag["battingAvgPct"] = lag_batting_avg_pct
        met_lag["avgTurnoverPct"] = avg_turnover_pct

        # Latest holdings table (all rows: Equity + Options + Notes, untruncated in shard)
        lat_weights, eq_aum, _, rep_aum, pos_cnt, _, top10_pct, tech_pct, max_1, etf_pct = extract_equity_weights(
            latest, cusip_meta, excluded_etfs=excluded_etf_tickers
        )

        prev_agg_rows: dict[tuple[str, str], dict[str, Any]] = {}
        prev_rep_aum = 0.0
        prev_eq_aum = 0.0
        if prev:
            prev_agg_rows, prev_rep_aum, prev_eq_aum = aggregate_filing_positions(prev, cusip_meta)

        combined_rows: dict[tuple[str, str], dict[str, Any]] = {}
        for r in latest["rows"]:
            meta = cusip_meta.get(r["cusip"], {})
            raw_ticker = meta.get("ticker") or f"CUSIP:{r['cusip']}"
            disp_ticker = meta.get("displaySymbol") or raw_ticker
            canon_ticker = raw_ticker if raw_ticker.startswith("CUSIP:") else DUAL_CLASS_CANONICAL.get(raw_ticker, raw_ticker)
            key = (canon_ticker, r["secType"])
            if key not in combined_rows:
                c_meta = canon_meta_by_ticker.get(canon_ticker, {}) if raw_ticker != canon_ticker else {}
                combined_rows[key] = {
                    "cusip": canon_cusip_by_ticker.get(canon_ticker, r["cusip"]) if raw_ticker != canon_ticker else r["cusip"],
                    "ticker": disp_ticker if raw_ticker.startswith("CUSIP:") else canon_ticker,
                    "rawTicker": canon_ticker,
                    "name": (c_meta.get("name") or meta.get("name") or r["issuer"]) if c_meta else (meta.get("name") or r["issuer"]),
                    "sector": (c_meta.get("sector") or meta.get("sector") or "Other") if c_meta else (meta.get("sector") or "Other"),
                    "industry": (c_meta.get("industry") or meta.get("industry") or "") if c_meta else (meta.get("industry") or ""),
                    "isTechTmt": bool(meta.get("isTechTmt") or c_meta.get("isTechTmt")),
                    "securityType": r["secType"],
                    "shares": float(r["shares"]),
                    "valueUsd": float(r["valueUsd"]),
                    "_maxLegVal": float(r["valueUsd"]),
                }
            else:
                slot = combined_rows[key]
                slot["shares"] += float(r["shares"])
                slot["valueUsd"] += float(r["valueUsd"])
                slot["isTechTmt"] = slot["isTechTmt"] or bool(meta.get("isTechTmt"))
                if float(r["valueUsd"]) > slot["_maxLegVal"] or raw_ticker == canon_ticker:
                    slot["_maxLegVal"] = float(r["valueUsd"])
                    if raw_ticker == canon_ticker:
                        slot["cusip"] = r["cusip"]
                        slot["name"] = meta.get("name") or r["issuer"]

        holdings_list: list[dict[str, Any]] = []
        sector_totals: dict[str, float] = {}
        for item in sorted(combined_rows.values(), key=lambda x: x["valueUsd"], reverse=True):
            rt = item.pop("rawTicker")
            item.pop("_maxLegVal", None)
            is_eq = item["securityType"] == "EQUITY"
            canon_t = DUAL_CLASS_CANONICAL.get(rt, rt)
            is_single_eq = is_eq and canon_t not in excluded_etf_tickers
            w_eq = (item["valueUsd"] / eq_aum * 100.0) if (is_single_eq and eq_aum > 0) else 0.0
            w_rep = (item["valueUsd"] / rep_aum * 100.0) if rep_aum > 0 else 0.0

            if is_single_eq:
                sec = item["sector"] or "Other"
                sector_totals[sec] = sector_totals.get(sec, 0.0) + w_eq

            qoq_sh_pct = None
            qoq_w_delta = None
            est_flow_usd = None
            status = "UNCHANGED"
            if prev:
                p_row = prev_agg_rows.get((rt, item["securityType"]))
                psh_raw = float(p_row["shares"]) if p_row else 0.0
                sp_factor = 1.0
                if is_eq and splits_map and not canon_t.startswith("CUSIP:"):
                    sp_factor, _ = split_factor_between(canon_t, prev["reportDate"], latest["reportDate"], splits_map)
                psh = psh_raw * sp_factor
                if psh <= 0:
                    status = "NEW"
                else:
                    diff_pct = ((item["shares"] / psh) - 1.0) * 100.0
                    qoq_sh_pct = clean_num(diff_pct, 1)
                    if diff_pct >= 5.0:
                        status = "ADDED"
                    elif diff_pct <= -5.0:
                        status = "TRIMMED"
                est_px = (
                    (item["valueUsd"] / item["shares"])
                    if item["shares"] > 0
                    else ((float(p_row["valueUsd"]) / psh) if (p_row and psh > 0) else 0.0)
                )
                est_flow_usd = round((item["shares"] - psh) * est_px, 2)
                if is_single_eq:
                    pw = (float(p_row["valueUsd"]) / prev_eq_aum * 100.0) if (p_row and prev_eq_aum > 0) else 0.0
                    qoq_w_delta = clean_num(w_eq - pw, 2)
                else:
                    pw_rep = (float(p_row["valueUsd"]) / prev_rep_aum * 100.0) if (p_row and prev_rep_aum > 0) else 0.0
                    qoq_w_delta = clean_num(w_rep - pw_rep, 2)

            px_t = rt if rt in aligned_prices else canon_t
            px_last = None
            if px_t in aligned_prices:
                arr, _, last_i = aligned_prices[px_t]
                px_last = clean_num(float(arr[last_i]), 2)

            holdings_list.append({
                **item,
                "shares": round(item["shares"]),
                "valueUsd": round(item["valueUsd"], 2),
                "weightPct": round(w_eq, 2),
                "reportedWeightPct": round(w_rep, 2),
                "qoqSharesChangePct": qoq_sh_pct,
                "qoqWeightDeltaPct": qoq_w_delta,
                "estFlowUsd": est_flow_usd,
                "status": status,
                "pricePerShare": px_last,
            })

        hhi = sum(w * w for w in lat_weights.values())
        eff_n = (1.0 / hhi) if hhi > 0 else float(pos_cnt)
        am = audit_meta_by_cik.get(cik, {})
        avg4q_max1 = am.get("avg4qMaxSingle", max_1)

        # Two-Tier Leaderboard Eligibility: qualified SPVs stay in Explorer, excluded from Rankings/Cohort
        is_lb_eligible = True
        exclusion_reason: str | None = None
        if eff_n < 4.0:
            is_lb_eligible = False
            exclusion_reason = f"Ultra-concentrated book (Eff. Positions {eff_n:.1f} < 4.0 min for Top-100 ranking)"
        elif avg4q_max1 > 40.0:
            is_lb_eligible = False
            exclusion_reason = f"Single-stock dominance (4Q avg top weight {avg4q_max1:.1f}% > 40% cap)"
        elif max_1 > 45.0:
            is_lb_eligible = False
            exclusion_reason = f"Single-stock dominance (latest top weight {max_1:.1f}% > 45% cap)"

        sector_breakdown = [
            {"sector": sec, "weightPct": round(w, 2)}
            for sec, w in sorted(sector_totals.items(), key=lambda x: x[1], reverse=True)
        ]

        top_preview = [
            {"ticker": h["ticker"], "name": h["name"], "weightPct": h["weightPct"]}
            for h in holdings_list
            if h["securityType"] == "EQUITY" and DUAL_CLASS_CANONICAL.get(h["ticker"], h["ticker"]) not in excluded_etf_tickers
        ][:4]

        searchable_tickers: list[str] = []
        seen_st: set[str] = set()
        for h in holdings_list:
            if h["securityType"] != "EQUITY":
                continue
            resolved_tk = cusip_meta.get(h["cusip"], {}).get("ticker") or h["ticker"]
            if resolved_tk.startswith("CUSIP:"):
                continue
            for cand_tk in (h["ticker"], DUAL_CLASS_CANONICAL.get(h["ticker"], h["ticker"])):
                if cand_tk not in excluded_etf_tickers and cand_tk not in seen_st:
                    seen_st.add(cand_tk)
                    searchable_tickers.append(cand_tk)

        lat_q_lbl = quarter_label(latest["reportDate"])
        funds_out.append({
            "cik": cik,
            "shard": fund_shard_id(cik),
            "name": cand["name"],
            "legalName": latest["entityName"],
            "manager": cand["manager"],
            "hq": cand["hq"],
            "styleTag": cand["styleTag"],
            "strategySummary": cand["strategySummary"],
            "source": cand.get("source", "Curated Core"),
            "fundTier": "Qualified" if is_lb_eligible else "Excluded",
            "isLeaderboardEligible": is_lb_eligible,
            "exclusionReason": exclusion_reason,
            "latestPeriod": latest["reportDate"],
            "latestQuarterLabel": lat_q_lbl,
            "latestFilingDate": latest["filingDate"],
            "isStaleQuarter": lat_q_lbl != latest_q_overall,
            "equityAumUsd": round(eq_aum, 2),
            "totalReportedUsd": round(rep_aum, 2),
            "equityPositionCount": pos_cnt,
            "totalPositionCount": len(holdings_list),
            "top10WeightPct": round(top10_pct, 2),
            "effectivePositions": round(eff_n, 1),
            "techTmtPct": round(tech_pct, 1),
            "etfSharePct": round(etf_pct, 1),
            "quartersTracked": len(filings),
            "returns": {"quarter_end": rets_qe, "filing_lag": rets_lag},
            "metrics": {"quarter_end": met_qe, "filing_lag": met_lag},
            "topHoldingsPreview": top_preview,
            "sectorBreakdown": sector_breakdown,
            "searchableTickers": searchable_tickers,
            "equityCurve": {
                "quarter_end": downsample_curve(cal_dates, nav_qe, step=5),
                "filing_lag": downsample_curve(cal_dates, nav_lag, step=5),
            },
            "quarterlyHistory": list(reversed(quarterly_hist)),
            "holdings": holdings_list,
            "edgarUrl": f"https://www.sec.gov/edgar/browse/?CIK={int(cik):010d}",
        })

    # Step 7: Composite Skill Score (0-100) with track-record confidence shrinkage, Elite-50 Tiers & Dual Rankings
    lb_funds = [f for f in funds_out if f["isLeaderboardEligible"]]
    lb_ciks = {f["cik"] for f in lb_funds}
    fund_by_cik = {f["cik"]: f for f in funds_out}

    valid_3y_by_mode: dict[str, dict[str, int]] = {"quarter_end": {}, "filing_lag": {}}
    for mode in MODES:
        for f in lb_funds:
            nav_m = fund_navs[mode].get(f["cik"])
            vdays = 0
            if nav_m is not None:
                vm = np.where(~np.isnan(nav_m))[0]
                if len(vm) >= 20:
                    lv = int(vm[-1])
                    nb = np.where(np.isnan(nav_m[: lv + 1]))[0]
                    cs = int(nb[-1]) + 1 if len(nb) > 0 else int(vm[0])
                    st = max(cs, win_indices["3Y"])
                    vdays = max(0, lv - st + 1)
            valid_3y_by_mode[mode][f["cik"]] = vdays

    skill_ranks_by_mode: dict[str, dict[str, int]] = {"quarter_end": {}, "filing_lag": {}}
    for mode in MODES:
        sk_cands = [
            f for f in lb_funds
            if f["metrics"][mode]["infoRatioVsQqq"] is not None
            and f["metrics"][mode]["sortinoRatio"] is not None
            and f["metrics"][mode]["battingAvgPct"] is not None
            and f["returns"][mode]["TTM"] is not None
        ]
        if sk_cands:
            ir_pr = _pct_ranks_desc([(f["cik"], float(f["metrics"][mode]["infoRatioVsQqq"])) for f in sk_cands])
            bat_pr = _pct_ranks_desc([(f["cik"], float(f["metrics"][mode]["battingAvgPct"])) for f in sk_cands])
            sort_pr = _pct_ranks_desc([(f["cik"], float(f["metrics"][mode]["sortinoRatio"])) for f in sk_cands])
            for f in sk_cands:
                c = f["cik"]
                raw_sc = 0.40 * ir_pr[c] + 0.30 * bat_pr[c] + 0.30 * sort_pr[c]
                conf = min(1.0, valid_3y_by_mode[mode].get(c, 0) / 630.0)
                shrunk_sc = 0.50 + conf * (raw_sc - 0.50)
                sc = round(shrunk_sc * 100.0, 1)
                f["metrics"][mode]["skillScore"] = sc
        for f in funds_out:
            if "skillScore" not in f["metrics"][mode]:
                f["metrics"][mode]["skillScore"] = None
            f["metrics"][mode]["skillRank"] = None

        ranked_by_skill = sorted(
            lb_funds,
            key=lambda x: (
                1 if x["metrics"][mode]["skillScore"] is not None else 0,
                x["metrics"][mode]["skillScore"] if x["metrics"][mode]["skillScore"] is not None else -1.0,
                x["returns"][mode]["TTM"] if x["returns"][mode]["TTM"] is not None else -999.0,
            ),
            reverse=True,
        )
        for idx, f in enumerate(ranked_by_skill):
            sr = idx + 1
            f["metrics"][mode]["skillRank"] = sr
            skill_ranks_by_mode[mode][f["cik"]] = sr

    # Assign FundTier from 45-Day Lag Skill Rank, preferring seasoned >=8Q track records for Elite 50
    scored_lag_funds = [f for f in lb_funds if f["metrics"]["filing_lag"]["skillScore"] is not None]
    eligible_elite_pool = [f for f in scored_lag_funds if f.get("quartersTracked", 0) >= 8]
    elite_source = (
        eligible_elite_pool
        if len(eligible_elite_pool) >= 50
        else (scored_lag_funds if len(scored_lag_funds) >= 50 else lb_funds)
    )
    elite50_ciks = [
        f["cik"]
        for f in sorted(
            elite_source,
            key=lambda x: skill_ranks_by_mode["filing_lag"].get(x["cik"], 9999),
        )
    ][:50]
    elite50_set = set(elite50_ciks)
    scored_lag_count = len(scored_lag_funds)
    for f in lb_funds:
        sr_lag = skill_ranks_by_mode["filing_lag"].get(f["cik"], 9999)
        if f["cik"] in elite50_set:
            f["fundTier"] = "Elite 50"
        elif f["metrics"]["filing_lag"]["skillScore"] is None and scored_lag_count >= 50:
            f["fundTier"] = "Qualified"
        elif sr_lag <= 100:
            f["fundTier"] = "Top 100"
        else:
            f["fundTier"] = "Qualified"

    rankings: dict[str, dict[str, list[dict[str, Any]]]] = {"quarter_end": {}, "filing_lag": {}}
    ranked_union_ciks: dict[str, list[str]] = {"quarter_end": [], "filing_lag": []}
    fund_top100_windows: dict[str, dict[str, list[str]]] = {"quarter_end": {}, "filing_lag": {}}

    for mode in MODES:
        union_set: list[str] = []
        for w in WINDOWS:
            elig_w = [f for f in lb_funds if f["returns"][mode][w] is not None]
            by_ret = sorted(elig_w, key=lambda f: f["returns"][mode][w], reverse=True)
            ret_rank_map = {f["cik"]: idx + 1 for idx, f in enumerate(by_ret)}
            by_sk = sorted(
                elig_w,
                key=lambda f: (
                    f["metrics"][mode]["skillScore"] if f["metrics"][mode]["skillScore"] is not None else -1.0,
                    f["returns"][mode][w],
                ),
                reverse=True,
            )
            sk_rank_map = {f["cik"]: idx + 1 for idx, f in enumerate(by_sk)}

            top100_ret_ciks = [f["cik"] for f in by_ret[:100]]
            top100_sk_ciks = [f["cik"] for f in by_sk[:100]]
            comb_ciks = list(top100_ret_ciks)
            seen_w = set(top100_ret_ciks)
            for c_sk in top100_sk_ciks:
                if c_sk not in seen_w:
                    comb_ciks.append(c_sk)
                    seen_w.add(c_sk)

            rankings[mode][w] = [
                {
                    "rank": ret_rank_map[c],
                    "skillRank": sk_rank_map[c],
                    "cik": c,
                    "returnPct": round(float(fund_by_cik[c]["returns"][mode][w]), 2),
                    "skillScore": fund_by_cik[c]["metrics"][mode]["skillScore"],
                }
                for c in comb_ciks
            ]
            for c in top100_ret_ciks:
                if c not in union_set:
                    union_set.append(c)
                fund_top100_windows[mode].setdefault(c, []).append(w)
            for c in top100_sk_ciks[:50]:
                if c not in union_set:
                    union_set.append(c)
        ranked_union_ciks[mode] = union_set

    # Step 8: Build QoQ Fund Updates Digest early so Signal Screener & Stock Dossiers can index exits/moves
    fund_updates_out: list[dict[str, Any]] = []
    seen_update_ciks: set[str] = set()
    for cand in update_cands:
        cik = cand["cik"]
        if cik in seen_update_ciks or cik not in raw_fund_filings:
            continue
        seen_update_ciks.add(cik)
        filings = raw_fund_filings[cik]
        in_rankings = cik in lb_ciks
        top100_w = fund_top100_windows["filing_lag"].get(cik, [])
        upd = build_fund_update_entry(
            cand=cand,
            filings=filings,
            cusip_meta=cusip_meta,
            in_rankings=in_rankings,
            ranked_windows=top100_w,
            splits_map=splits_map,
        )
        if upd:
            upd["fundTier"] = fund_by_cik[cik]["fundTier"] if cik in fund_by_cik else "Watchlist"
            fund_updates_out.append(upd)

    fund_updates_out.sort(
        key=lambda u: (u["periodEnd"], u["filingDate"], u["grossActivityUsd"]),
        reverse=True,
    )
    updates_overview = build_updates_overview(
        fund_updates_out,
        total_eligible=len(update_cands),
        excluded_etfs=excluded_etf_tickers,
    )

    # Step 9: Build Top 30 Consensus Holdings per mode
    top_holdings_out: dict[str, list[dict[str, Any]]] = {}
    for mode in MODES:
        pool_ciks = ranked_union_ciks[mode] or [f["cik"] for f in lb_funds]
        pool_funds = [fund_by_cik[c] for c in pool_ciks if c in fund_by_cik]
        n_pool = len(pool_funds)
        total_pool_aum = sum(f["equityAumUsd"] for f in pool_funds)

        stock_map: dict[str, dict[str, Any]] = {}
        for f in pool_funds:
            cik = f["cik"]
            fund_merged: dict[str, dict[str, Any]] = {}
            for h in f["holdings"]:
                if h["securityType"] != "EQUITY" or h["weightPct"] <= 0:
                    continue
                raw_t = cusip_meta.get(h["cusip"], {}).get("ticker") or h["ticker"]
                if not raw_t or raw_t.startswith("CUSIP:"):
                    continue
                canon_t = DUAL_CLASS_CANONICAL.get(raw_t, raw_t)
                if canon_t in excluded_etf_tickers:
                    continue
                slot = fund_merged.get(canon_t)
                if slot is None:
                    c_meta = canon_meta_by_ticker.get(canon_t, {})
                    fund_merged[canon_t] = {
                        "ticker": canon_t,
                        "cusip": h["cusip"] if raw_t == canon_t else (canon_cusip_by_ticker.get(canon_t) or h["cusip"]),
                        "name": (c_meta.get("name") or h["name"]) if raw_t != canon_t else h["name"],
                        "sector": c_meta.get("sector") or h["sector"] if raw_t != canon_t else h["sector"],
                        "industry": c_meta.get("industry") or h["industry"] if raw_t != canon_t else h["industry"],
                        "isTechTmt": bool(h.get("isTechTmt") or c_meta.get("isTechTmt")),
                        "priceUsd": h["pricePerShare"],
                        "weightPct": float(h["weightPct"]),
                        "valueUsd": float(h["valueUsd"]),
                        "maxLegVal": float(h["valueUsd"]),
                        "status": h["status"],
                        "qoqSharesChangePct": h["qoqSharesChangePct"],
                    }
                else:
                    slot["weightPct"] += float(h["weightPct"])
                    slot["valueUsd"] += float(h["valueUsd"])
                    slot["isTechTmt"] = slot["isTechTmt"] or bool(h.get("isTechTmt"))
                    if float(h["valueUsd"]) > slot["maxLegVal"]:
                        slot["maxLegVal"] = float(h["valueUsd"])
                        slot["status"] = h["status"]
                        slot["qoqSharesChangePct"] = h["qoqSharesChangePct"]
                        if raw_t == canon_t:
                            slot["cusip"] = h["cusip"]
                            slot["name"] = h["name"]
                    if slot["priceUsd"] is None and h["pricePerShare"] is not None:
                        slot["priceUsd"] = h["pricePerShare"]

            for t, hm in fund_merged.items():
                if t not in stock_map:
                    stock_map[t] = {
                        "ticker": t,
                        "cusip": hm["cusip"],
                        "name": hm["name"],
                        "sector": hm["sector"],
                        "industry": hm["industry"],
                        "isTechTmt": hm["isTechTmt"],
                        "priceUsd": hm["priceUsd"],
                        "sumWeightPct": 0.0,
                        "sumValueUsd": 0.0,
                        "netBuyers": 0,
                        "netTrimmers": 0,
                        "holders": [],
                    }
                rec = stock_map[t]
                rec["isTechTmt"] = rec["isTechTmt"] or hm["isTechTmt"]
                rec["sumWeightPct"] += hm["weightPct"]
                rec["sumValueUsd"] += hm["valueUsd"]
                if hm["status"] in ("NEW", "ADDED"):
                    rec["netBuyers"] += 1
                elif hm["status"] == "TRIMMED":
                    rec["netTrimmers"] += 1
                rec["holders"].append({
                    "cik": cik,
                    "fundName": f["name"],
                    "fundTier": f["fundTier"],
                    "weightPct": round(hm["weightPct"], 2),
                    "valueUsd": round(hm["valueUsd"], 2),
                    "status": hm["status"],
                    "qoqSharesChangePct": hm["qoqSharesChangePct"],
                    "rankedWindows": fund_top100_windows[mode].get(cik, []),
                })

        entries: list[dict[str, Any]] = []
        for t, rec in stock_map.items():
            holders_sorted = sorted(rec["holders"], key=lambda x: x["weightPct"], reverse=True)
            eq_agg = rec["sumWeightPct"] / max(1, n_pool)
            avg_holder_w = rec["sumWeightPct"] / max(1, len(holders_sorted))
            aum_agg = (rec["sumValueUsd"] / total_pool_aum * 100.0) if total_pool_aum > 0 else 0.0

            ytd_ret = None
            ttm_ret = None
            px_usd = rec["priceUsd"]
            if t in aligned_prices:
                arr, first_i, last_i = aligned_prices[t]
                px_usd = clean_num(float(arr[last_i]), 2)
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
                "subSector": classify_sub_sector(rec["sector"], rec["industry"], rec["isTechTmt"]),
                "isTechTmt": rec["isTechTmt"],
                "priceUsd": px_usd,
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

    # Step 10: Build Signal Screener + Elite-50 Ideal Portfolio (40 default + Top-80 candidatePool)
    signal_screener = build_signal_screener(
        funds_out=funds_out,
        fund_updates_out=fund_updates_out,
        cusip_meta=cusip_meta,
        aligned_prices=aligned_prices,
        win_indices=win_indices,
    )

    ideal_pool_funds = [fund_by_cik[c] for c in elite50_ciks if c in fund_by_cik] or lb_funds[:50] or funds_out
    ideal_weights, ideal_stock_agg, ideal_sorted_items = compute_ideal_portfolio_weights(
        ideal_pool_funds,
        mode="filing_lag",
        cusip_meta=cusip_meta,
        fund_navs_mode=fund_navs["filing_lag"],
        cal_dates=cal_dates,
        win_indices=win_indices,
        max_names=40,
        cap_pct=8.0,
        aligned_prices=aligned_prices,
        n_dates=n_dates,
        excluded_etfs=excluded_etf_tickers,
    )

    # Survivorship-free Point-in-Time Quarterly Backtests (QE vs 45-Day Lag)
    ticker_is_tech: dict[str, bool] = {}
    for m_val in cusip_meta.values():
        tk = m_val.get("ticker", "")
        if tk and not tk.startswith("CUSIP:") and m_val.get("isTechTmt"):
            ticker_is_tech[DUAL_CLASS_CANONICAL.get(tk, tk)] = True

    all_bt_ciks = sorted(pit_snapshots["quarter_end"].keys())
    all_quarters = sorted({fl["reportDate"] for c in all_bt_ciks for fl in raw_fund_filings[c]})

    curated_bt_ciks = {f["cik"] for f in CURATED_FUNDS}

    bt_segments_qe = run_ideal_portfolio_backtest(
        bt_mode="quarter_end",
        all_quarters=all_quarters,
        all_bt_ciks=all_bt_ciks,
        snaps_by_cik=pit_snapshots["quarter_end"],
        navs_by_cik=fund_navs["quarter_end"],
        cal_dates=cal_dates,
        as_of_date=as_of_date,
        aligned_prices=aligned_prices,
        ticker_is_tech=ticker_is_tech,
        curated_bt_ciks=curated_bt_ciks,
        excluded_etfs=excluded_etf_tickers,
        splits_map=splits_map,
        rf_ann_arr=rf_ann_arr,
        qqq_nav=qqq_nav,
    )
    bt_segments_lag = run_ideal_portfolio_backtest(
        bt_mode="filing_lag",
        all_quarters=all_quarters,
        all_bt_ciks=all_bt_ciks,
        snaps_by_cik=pit_snapshots["filing_lag"],
        navs_by_cik=fund_navs["filing_lag"],
        cal_dates=cal_dates,
        as_of_date=as_of_date,
        aligned_prices=aligned_prices,
        ticker_is_tech=ticker_is_tech,
        curated_bt_ciks=curated_bt_ciks,
        excluded_etfs=excluded_etf_tickers,
        splits_map=splits_map,
        rf_ann_arr=rf_ann_arr,
        qqq_nav=qqq_nav,
    )
    ideal_nav_qe = simulate_nav_series(bt_segments_qe, aligned_prices, n_dates)
    ideal_nav_lag = simulate_nav_series(bt_segments_lag, aligned_prices, n_dates)
    ideal_rets_qe, ideal_met_qe = compute_returns_and_metrics(ideal_nav_qe, qqq_nav, win_indices, rf_ann_arr=rf_ann_arr)
    ideal_rets_lag, ideal_met_lag = compute_returns_and_metrics(ideal_nav_lag, qqq_nav, win_indices, rf_ann_arr=rf_ann_arr)

    # Step 11: External Public APIs (OHLCV Q-VWAP + SEC XBRL Fundamentals + Nasdaq Market Caps + Elite-50 13D/13G Alerts) & Stock Dossiers
    all_dossier_builders: dict[str, dict[str, Any]] = {}
    for f in funds_out:
        cik = f["cik"]
        is_elite = f["fundTier"] == "Elite 50"
        merged_f: dict[str, dict[str, Any]] = {}
        for h in f["holdings"]:
            if h["securityType"] != "EQUITY" or h["weightPct"] <= 0:
                continue
            raw_t = cusip_meta.get(h["cusip"], {}).get("ticker") or h["ticker"]
            if not raw_t or raw_t.startswith("CUSIP:"):
                continue
            t = DUAL_CLASS_CANONICAL.get(raw_t, raw_t)
            if t in excluded_etf_tickers:
                continue
            slot = merged_f.get(t)
            if slot is None:
                c_meta = canon_meta_by_ticker.get(t, {})
                merged_f[t] = {
                    "ticker": t,
                    "cusip": h["cusip"] if raw_t == t else (canon_cusip_by_ticker.get(t) or h["cusip"]),
                    "name": (c_meta.get("name") or h["name"]) if raw_t != t else h["name"],
                    "sector": c_meta.get("sector") or h["sector"] if raw_t != t else h["sector"],
                    "industry": c_meta.get("industry") or h["industry"] if raw_t != t else h["industry"],
                    "isTechTmt": bool(h.get("isTechTmt") or c_meta.get("isTechTmt")),
                    "weightPct": float(h["weightPct"]),
                    "valueUsd": float(h["valueUsd"]),
                    "status": h["status"],
                    "qoqSharesChangePct": h["qoqSharesChangePct"],
                    "qoqWeightDeltaPct": h["qoqWeightDeltaPct"],
                }
            else:
                slot["weightPct"] += float(h["weightPct"])
                slot["valueUsd"] += float(h["valueUsd"])
                slot["isTechTmt"] = slot["isTechTmt"] or bool(h.get("isTechTmt"))

        for t, hm in merged_f.items():
            rec = all_dossier_builders.setdefault(t, {
                "ticker": t,
                "cusip": hm["cusip"],
                "name": hm["name"],
                "sector": hm["sector"] or "Other",
                "industry": hm["industry"] or "",
                "isTechTmt": hm["isTechTmt"],
                "holders": [],
                "recentMoves": [],
                "totalValueUsd": 0.0,
                "netEstFlowUsd": 0.0,
                "weights": [],
                "eliteHoldersCount": 0,
                "newCount": 0,
                "addedCount": 0,
                "trimmedCount": 0,
                "exitedCount": 0,
            })
            rec["totalValueUsd"] += hm["valueUsd"]
            w_p = round(hm["weightPct"], 2)
            rec["weights"].append(w_p)
            if is_elite:
                rec["eliteHoldersCount"] += 1
            rec["holders"].append({
                "cik": cik,
                "fundName": f["name"],
                "fundTier": f["fundTier"],
                "skillScore": f["metrics"]["filing_lag"]["skillScore"],
                "weightPct": w_p,
                "valueUsd": round(hm["valueUsd"], 2),
                "status": hm["status"],
                "qoqSharesChangePct": hm["qoqSharesChangePct"],
                "qoqWeightDeltaPct": hm["qoqWeightDeltaPct"],
            })

    # Add latest QoQ moves (including EXITED) across fund updates onto stock dossiers
    for u in fund_updates_out:
        cik = u["cik"]
        f_tier = u.get("fundTier", "Watchlist")
        per_u_stock: dict[str, dict[str, Any]] = {}
        for mv in u["allMoves"]:
            if mv["securityType"] != "EQUITY":
                continue
            raw_t = mv["ticker"]
            if not raw_t or raw_t.startswith("CUSIP:"):
                continue
            t = DUAL_CLASS_CANONICAL.get(raw_t, raw_t)
            if t in excluded_etf_tickers:
                continue
            slot = per_u_stock.setdefault(t, {
                "ticker": t,
                "cusip": mv["cusip"],
                "name": mv["name"],
                "sector": mv["sector"] or "Other",
                "industry": mv["industry"] or "",
                "prevSh": 0.0,
                "currSh": 0.0,
                "valDelta": 0.0,
                "estFlow": 0.0,
                "currBookPct": 0.0,
                "prevBookPct": 0.0,
            })
            slot["prevSh"] += float(mv["prevShares"])
            slot["currSh"] += float(mv["currShares"])
            slot["valDelta"] += float(mv["valueDeltaUsd"])
            slot["estFlow"] += float(mv.get("estFlowUsd", mv["valueDeltaUsd"]))
            slot["currBookPct"] += float(mv["currBookPct"])
            slot["prevBookPct"] += float(mv["prevBookPct"])

        for t, s in per_u_stock.items():
            if s["prevSh"] <= 0:
                act, sh_pct = "NEW", None
            elif s["currSh"] <= 0:
                act, sh_pct = "EXITED", -100.0
            else:
                dp = ((s["currSh"] / s["prevSh"]) - 1.0) * 100.0
                sh_pct = clean_num(dp, 1)
                act = "ADDED" if dp >= 5.0 else ("TRIMMED" if dp <= -5.0 else "UNCHANGED")
            if act == "UNCHANGED":
                continue

            rec = all_dossier_builders.setdefault(t, {
                "ticker": t,
                "cusip": s["cusip"],
                "name": s["name"],
                "sector": s["sector"],
                "industry": s["industry"],
                "isTechTmt": is_tech_tmt(t, s["sector"], s["industry"]),
                "holders": [],
                "recentMoves": [],
                "totalValueUsd": 0.0,
                "netEstFlowUsd": 0.0,
                "weights": [],
                "eliteHoldersCount": 0,
                "newCount": 0,
                "addedCount": 0,
                "trimmedCount": 0,
                "exitedCount": 0,
            })
            rec["netEstFlowUsd"] += s["estFlow"]
            if act == "NEW":
                rec["newCount"] += 1
            elif act == "ADDED":
                rec["addedCount"] += 1
            elif act == "TRIMMED":
                rec["trimmedCount"] += 1
            elif act == "EXITED":
                rec["exitedCount"] += 1

            rec["recentMoves"].append({
                "cik": cik,
                "fundName": u["fundName"],
                "fundTier": f_tier,
                "quarterLabel": u["quarterLabel"],
                "action": act,
                "sharesDeltaPct": sh_pct,
                "valueDeltaUsd": round(s["valDelta"], 2),
                "estFlowUsd": round(s["estFlow"], 2),
                "prevBookPct": round(s["prevBookPct"], 2),
                "currBookPct": round(s["currBookPct"], 2),
            })

    # Determine prioritized ticker list for XBRL fundamentals batch fetch
    cand80_tickers = [t for t, _ in ideal_sorted_items[:80]]
    xbrl_priority_set: set[str] = set(cand80_tickers)
    for mode in MODES:
        for h in top_holdings_out[mode]:
            xbrl_priority_set.add(h["ticker"])
    for key in ("clusterInitiations", "heavyAdds", "smartMoneyExits", "contrarianBets"):
        for item in signal_screener[key]:
            xbrl_priority_set.add(item["ticker"])
    multi_or_elite = sorted(
        [t for t, r in all_dossier_builders.items() if len(r["holders"]) >= 2 or r["eliteHoldersCount"] >= 1],
        key=lambda t: all_dossier_builders[t]["totalValueUsd"],
        reverse=True,
    )
    for t in multi_or_elite[:250]:
        xbrl_priority_set.add(t)

    all_dossier_tickers = sorted(all_dossier_builders.keys())
    ohlcv_map = load_cached_ohlcv(all_dossier_tickers)
    nasdaq_mcaps = fetch_nasdaq_market_caps(max_age_hours=24.0)
    ticker_to_cik, cik_to_ticker, name_to_ticker = load_ticker_cik_mappings()
    for ov in MANUAL_CUSIP_OVERRIDES.values():
        tk = DUAL_CLASS_CANONICAL.get(ov.get("ticker", ""), ov.get("ticker", ""))
        nn = _normalize_legal_name(ov.get("name", ""))
        if tk and len(nn) >= 5 and nn not in name_to_ticker:
            name_to_ticker[nn] = tk
    for t_key, rec in all_dossier_builders.items():
        nn = _normalize_legal_name(rec.get("name", ""))
        if t_key and len(nn) >= 5 and nn not in name_to_ticker:
            name_to_ticker[nn] = t_key
    xbrl_map = fetch_xbrl_fundamentals_batch(sorted(xbrl_priority_set), ticker_to_cik, max_age_hours=168.0)

    cusip_to_ticker_map = {
        c_k: DUAL_CLASS_CANONICAL.get(m_v["ticker"], m_v["ticker"])
        for c_k, m_v in cusip_meta.items()
        if m_v.get("ticker") and not str(m_v["ticker"]).startswith("CUSIP:")
    }
    elite_names_by_cik = {f["cik"]: f["name"] for f in funds_out if f["cik"] in elite50_ciks}
    midquarter_alerts = fetch_midquarter_alerts(
        elite_ciks=elite50_ciks,
        fund_names_by_cik=elite_names_by_cik,
        cik_to_ticker_map=cik_to_ticker,
        name_to_ticker_map=name_to_ticker,
        today_date=today_date,
        since_days=95,
        cusip_to_ticker_map=cusip_to_ticker_map,
    )
    alerts_by_ticker: dict[str, list[dict[str, Any]]] = {}
    alerts_by_cik: dict[str, list[dict[str, Any]]] = {}
    for al in midquarter_alerts:
        if al["ticker"]:
            canon_al_t = DUAL_CLASS_CANONICAL.get(al["ticker"], al["ticker"])
            alerts_by_ticker.setdefault(canon_al_t, []).append(al)
        alerts_by_cik.setdefault(al["filerCik"], []).append(al)

    # Latest quarter period end for Quarter-VWAP microstructure calculation
    latest_pe_mode = updates_overview.get("latestQuarterPeriodEnd") or max((f["latestPeriod"] for f in funds_out), default="2026-09-30")

    def _compute_stock_enrichment(t: str) -> dict[str, Any]:
        px_usd = None
        ytd_ret = None
        ttm_ret = None
        if t in aligned_prices:
            arr, fi, li = aligned_prices[t]
            px_usd = clean_num(float(arr[li]), 2)
            if fi <= win_indices["YTD"] + 5:
                si = max(win_indices["YTD"], fi)
                ytd_ret = clean_num(((arr[li] / arr[si]) - 1.0) * 100.0, 2)
            if fi <= win_indices["TTM"] + 5:
                si = max(win_indices["TTM"], fi)
                ttm_ret = clean_num(((arr[li] / arr[si]) - 1.0) * 100.0, 2)

        vwap_info = compute_quarter_vwap_metrics(ohlcv_map.get(t), latest_pe_mode, px_usd)
        xb = xbrl_map.get(t)
        total_held = all_dossier_builders.get(t, {}).get("totalValueUsd", 0.0)
        mkt_cap_usd = None
        mkt_cap_tier = None
        raw_mc: float | None = None
        nq_mc = nasdaq_mcaps.get(t)
        if nq_mc and 0.0 < nq_mc <= 5e12 and nq_mc >= total_held * 0.75:
            raw_mc = float(nq_mc)
        elif xb and xb.get("sharesOutstanding") and px_usd and px_usd > 0:
            cand_mc = px_usd * float(xb["sharesOutstanding"])
            if cand_mc >= total_held * 0.75:
                raw_mc = cand_mc

        if raw_mc is not None and raw_mc > 0:
            mkt_cap_usd = round(raw_mc, 0)
            if raw_mc >= 200e9:
                mkt_cap_tier = "Mega >$200B"
            elif raw_mc >= 30e9:
                mkt_cap_tier = "Large $30B-$200B"
            else:
                mkt_cap_tier = "Emerging <$30B"

        fundamentals = None
        if xb:
            fundamentals = {
                "revenueTtmUsd": xb.get("revenueTtmUsd"),
                "revenueGrowthYoYPct": xb.get("revenueGrowthYoYPct"),
                "operatingMarginPct": xb.get("operatingMarginPct"),
                "fcfMarginPct": xb.get("fcfMarginPct"),
                "rdIntensityPct": xb.get("rdIntensityPct"),
                "shareDilutionYoYPct": xb.get("shareDilutionYoYPct"),
            }
        return {
            "priceUsd": px_usd,
            "stockYtdPct": ytd_ret,
            "stockTtmPct": ttm_ret,
            "marketCapUsd": mkt_cap_usd,
            "marketCapTier": mkt_cap_tier,
            **vwap_info,
            "fundamentals": fundamentals,
        }

    enrichment_by_ticker = {t: _compute_stock_enrichment(t) for t in all_dossier_tickers}

    # Build Ideal Portfolio Top-40 positions AND Top-80 candidatePool (for interactive filters)
    ideal_rank_map = {t: idx + 1 for idx, (t, _) in enumerate(ideal_sorted_items[:80])}
    top80_raw = [ideal_stock_agg[t]["rawScore"] for t, _ in ideal_sorted_items[:80]] if ideal_sorted_items else [1.0]
    max_raw_score = max(top80_raw) if top80_raw else 1.0
    holder_counts_sel = sorted(len(ideal_stock_agg[t]["backers"]) for t in ideal_weights)
    core_q75 = holder_counts_sel[int(0.75 * max(0, len(holder_counts_sel) - 1))] if holder_counts_sel else 12
    core_thresh = max(10, len(ideal_pool_funds) // 6, core_q75)
    top_fscore_by_t = {t: max(b["fscore"] for b in ideal_stock_agg[t]["backers"]) for t in ideal_weights}
    fscore_q60 = (
        sorted(top_fscore_by_t.values())[int(0.60 * max(0, len(top_fscore_by_t) - 1))]
        if top_fscore_by_t
        else 0.82
    )

    ideal_positions: list[dict[str, Any]] = []
    ideal_candidate_pool: list[dict[str, Any]] = []
    ideal_sector_totals: dict[str, dict[str, Any]] = {}

    for idx, (t, raw_sc) in enumerate(ideal_sorted_items[:80]):
        rec = ideal_stock_agg[t]
        w_dec = ideal_weights.get(t, 0.0)
        w_pct = round(w_dec * 100.0, 2)
        conv_score = round((rec["rawScore"] / max_raw_score) * 100.0, 1) if max_raw_score > 0 else 50.0
        backers_sorted = sorted(rec["backers"], key=lambda x: x["weightPct"], reverse=True)
        cnt_b = len(backers_sorted)
        avg_fw = sum(rec["weights"]) / max(1, len(rec["weights"]))
        max_fw = max(rec["weights"]) if rec["weights"] else 0.0
        top_fs = max(b["fscore"] for b in rec["backers"])

        # Fresh Inflection score emphasizes net new initiations + share adds over static consensus
        buy_impulse = (1.5 * rec["netNew"] + 1.0 * rec["netAdd"] - 1.2 * rec["netTrim"]) / max(1, cnt_b)
        inflection_score = round(raw_sc * (1.0 + 0.65 * max(-0.5, min(1.5, buy_impulse))), 4)

        if cnt_b >= core_thresh:
            tag = "Core Consensus"
        elif rec["netBuy"] > rec["netTrim"] and rec["netBuy"] >= max(2, cnt_b // 2):
            tag = "Active Accumulation"
        elif top_fs >= fscore_q60 and max_fw >= 4.5:
            tag = "Top-Decile Alpha"
        else:
            tag = "High-Conviction Specialist"

        sec = rec["sector"] or "Other"
        sub_sec = classify_sub_sector(sec, rec["industry"], rec["isTechTmt"])
        enr = enrichment_by_ticker.get(t) or _compute_stock_enrichment(t)

        cand_obj = {
            "rank": idx + 1,
            "ticker": t,
            "cusip": rec["cusip"],
            "name": rec["name"],
            "sector": sec,
            "subSector": sub_sec,
            "industry": rec["industry"],
            "isTechTmt": rec["isTechTmt"],
            "rawScore": round(raw_sc, 4),
            "inflectionScore": inflection_score,
            "idealWeightPct": w_pct,
            "convictionScore": conv_score,
            "holderCount": cnt_b,
            "netNew": rec["netNew"],
            "netAdd": rec["netAdd"],
            "netTrim": rec["netTrim"],
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
            "stockTtmPct": enr["stockTtmPct"],
            "priceUsd": enr["priceUsd"],
            "marketCapUsd": enr["marketCapUsd"],
            "marketCapTier": enr["marketCapTier"],
            "estQuarterVwap": enr["estQuarterVwap"],
            "pctVsQuarterVwap": enr["pctVsQuarterVwap"],
            "entryZoneStatus": enr["entryZoneStatus"],
            "fundamentals": enr["fundamentals"],
        }
        ideal_candidate_pool.append(cand_obj)
        if t in ideal_weights:
            ideal_positions.append(dict(cand_obj))
            if sec not in ideal_sector_totals:
                ideal_sector_totals[sec] = {"sector": sec, "weightPct": 0.0, "stockCount": 0}
            ideal_sector_totals[sec]["weightPct"] += w_pct
            ideal_sector_totals[sec]["stockCount"] += 1

    ideal_positions.sort(key=lambda x: (-x["idealWeightPct"], x["rank"]))
    for i_p, p in enumerate(ideal_positions):
        p["rank"] = i_p + 1
    if ideal_positions:
        diff = round(100.0 - sum(p["idealWeightPct"] for p in ideal_positions), 2)
        ideal_positions[0]["idealWeightPct"] = round(ideal_positions[0]["idealWeightPct"] + diff, 2)

    ideal_hhi = sum((p["idealWeightPct"] / 100.0) ** 2 for p in ideal_positions)
    ideal_eff_n = (1.0 / ideal_hhi) if ideal_hhi > 0 else float(len(ideal_positions))

    ideal_portfolio_data = {
        "positionCount": len(ideal_positions),
        "maxPositionCapPct": 8.0,
        "rankedFundsCount": len(ideal_pool_funds),
        "effectivePositions": round(ideal_eff_n, 1),
        "top10WeightPct": round(sum(p["idealWeightPct"] for p in ideal_positions[:10]), 2),
        "methodologySummary": (
            f"Constructed from active US equity holdings across the Elite {len(ideal_pool_funds)} Skill-Ranked managers "
            f"(40% Info Ratio vs QQQ, 30% Batting Avg, 30% ^IRX Sortino). Positions are scored by non-overlapping "
            f"multi-horizon conviction^1.5 × √(weight) × Tech/TMT relevance × consensus × ±5% share accumulation."
        ),
        "sectorBreakdown": [
            {"sector": v["sector"], "weightPct": round(v["weightPct"], 2), "stockCount": v["stockCount"]}
            for v in sorted(ideal_sector_totals.values(), key=lambda x: x["weightPct"], reverse=True)
        ],
        "positions": ideal_positions,
        "candidatePool": ideal_candidate_pool,
        "backtest": {
            "quarter_end": {
                "returns": ideal_rets_qe,
                "cagrPct": ideal_met_qe["cagrPct"],
                "maxDrawdownPct": ideal_met_qe["maxDrawdownPct"],
                "sharpeProxy": ideal_met_qe["sharpeProxy"],
                "sortinoRatio": ideal_met_qe["sortinoRatio"],
                "infoRatioVsQqq": ideal_met_qe["infoRatioVsQqq"],
                "curve": downsample_curve(cal_dates, ideal_nav_qe, step=5),
            },
            "filing_lag": {
                "returns": ideal_rets_lag,
                "cagrPct": ideal_met_lag["cagrPct"],
                "maxDrawdownPct": ideal_met_lag["maxDrawdownPct"],
                "sharpeProxy": ideal_met_lag["sharpeProxy"],
                "sortinoRatio": ideal_met_lag["sortinoRatio"],
                "infoRatioVsQqq": ideal_met_lag["infoRatioVsQqq"],
                "curve": downsample_curve(cal_dates, ideal_nav_lag, step=5),
            },
        },
    }

    # Enrich Top 30 Consensus Holdings & Signal Screener with Q-VWAP + XBRL
    for mode in MODES:
        for h in top_holdings_out[mode]:
            enr = enrichment_by_ticker.get(h["ticker"])
            if enr:
                h["marketCapUsd"] = enr["marketCapUsd"]
                h["marketCapTier"] = enr["marketCapTier"]
                h["estQuarterVwap"] = enr["estQuarterVwap"]
                h["pctVsQuarterVwap"] = enr["pctVsQuarterVwap"]
                h["entryZoneStatus"] = enr["entryZoneStatus"]
                h["fundamentals"] = enr["fundamentals"]

    for bkey in ("clusterInitiations", "heavyAdds", "smartMoneyExits", "contrarianBets"):
        for item in signal_screener[bkey]:
            enr = enrichment_by_ticker.get(item["ticker"])
            if enr:
                item["marketCapUsd"] = enr["marketCapUsd"]
                item["marketCapTier"] = enr["marketCapTier"]
                item["estQuarterVwap"] = enr["estQuarterVwap"]
                item["pctVsQuarterVwap"] = enr["pctVsQuarterVwap"]
                item["entryZoneStatus"] = enr["entryZoneStatus"]
                item["fundamentals"] = enr["fundamentals"]

    # Build Stocks Index & 8 Sharded Stock Dossiers (public/data/stocks_{0..7}.json)
    ideal_w_by_t = {p["ticker"]: p["idealWeightPct"] for p in ideal_positions}
    stocks_index: dict[str, dict[str, Any]] = {}
    stock_shards: dict[int, dict[str, dict[str, Any]]] = {i: {} for i in range(8)}

    for t in all_dossier_tickers:
        rec = all_dossier_builders[t]
        holders_s = sorted(
            rec["holders"],
            key=lambda h: (h["fundTier"] == "Elite 50", h["weightPct"]),
            reverse=True,
        )
        moves_s = sorted(rec["recentMoves"], key=lambda m: abs(m["valueDeltaUsd"]), reverse=True)
        h_cnt = len(holders_s)
        el_cnt = rec["eliteHoldersCount"]
        weights_l = rec["weights"]
        avg_w = round(sum(weights_l) / h_cnt, 2) if h_cnt else 0.0
        max_w = round(max(weights_l), 2) if weights_l else 0.0
        net_buyers = rec["newCount"] + rec["addedCount"]
        net_sellers = rec["trimmedCount"] + rec["exitedCount"]

        if rec["newCount"] >= 2 and net_buyers >= net_sellers + 2:
            verdict = "Cluster Accumulation"
        elif rec["exitedCount"] >= 2 and net_sellers > net_buyers + 1:
            verdict = "Consensus Exit / Trim"
        elif el_cnt >= 5 and net_buyers >= net_sellers:
            verdict = "Elite Consensus Core"
        elif 2 <= h_cnt <= 6 and el_cnt >= 1 and max_w >= 3.0:
            verdict = "Contrarian High-Conviction"
        elif net_sellers > net_buyers:
            verdict = "Net Trimming"
        else:
            verdict = "Held Across Funds"

        enr = enrichment_by_ticker.get(t) or _compute_stock_enrichment(t)
        sec = rec["sector"] or "Other"
        sub_sec = classify_sub_sector(sec, rec["industry"], rec["isTechTmt"])
        sh_id = stock_shard_id(t)

        stocks_index[t] = {
            "ticker": t,
            "name": rec["name"],
            "sector": sec,
            "subSector": sub_sec,
            "shard": sh_id,
            "priceUsd": enr["priceUsd"],
            "marketCapTier": enr["marketCapTier"],
            "holderCount": h_cnt,
            "eliteHoldersCount": el_cnt,
            "signalVerdict": verdict,
            "entryZoneStatus": enr["entryZoneStatus"],
            "pctVsQuarterVwap": enr["pctVsQuarterVwap"],
        }

        stock_shards[sh_id][t] = {
            "ticker": t,
            "cusip": rec["cusip"],
            "name": rec["name"],
            "sector": sec,
            "industry": rec["industry"],
            "subSector": sub_sec,
            "isTechTmt": rec["isTechTmt"],
            "priceUsd": enr["priceUsd"],
            "stockYtdPct": enr["stockYtdPct"],
            "stockTtmPct": enr["stockTtmPct"],
            "marketCapUsd": enr["marketCapUsd"],
            "marketCapTier": enr["marketCapTier"],
            "estQuarterVwap": enr["estQuarterVwap"],
            "quarterLow": enr["quarterLow"],
            "quarterHigh": enr["quarterHigh"],
            "pctVsQuarterVwap": enr["pctVsQuarterVwap"],
            "entryZoneStatus": enr["entryZoneStatus"],
            "fundamentals": enr["fundamentals"],
            "signalVerdict": verdict,
            "idealPortfolioRank": ideal_rank_map.get(t),
            "idealWeightPct": ideal_w_by_t.get(t),
            "holderCount": h_cnt,
            "eliteHoldersCount": el_cnt,
            "totalValueUsd": round(rec["totalValueUsd"], 2),
            "netEstFlowUsd": round(rec["netEstFlowUsd"], 2),
            "avgWeightPct": avg_w,
            "maxWeightPct": max_w,
            "newCount": rec["newCount"],
            "addedCount": rec["addedCount"],
            "trimmedCount": rec["trimmedCount"],
            "exitedCount": rec["exitedCount"],
            "holders": holders_s,
            "recentMoves": moves_s,
            "midQuarterAlerts": alerts_by_ticker.get(t, []),
        }

    # Sort funds by default 45-Day Lag Skill Score / TTM performance
    funds_out.sort(
        key=lambda f: (
            1 if f["isLeaderboardEligible"] else 0,
            f["metrics"]["filing_lag"]["skillScore"] if f["metrics"]["filing_lag"]["skillScore"] is not None else -1.0,
            f["returns"]["filing_lag"]["TTM"] if f["returns"]["filing_lag"]["TTM"] is not None else -999.0,
        ),
        reverse=True,
    )

    # Assemble Sharded Payloads cleanly (Untruncated funds/updates in 16 shards; compact summary in summary.json)
    shard_hex_ids = [f"{i:x}" for i in range(16)]
    fund_shards: dict[str, dict[str, Any]] = {sid: {"funds": {}, "updates": {}} for sid in shard_hex_ids}
    funds_summary_out: list[dict[str, Any]] = []
    updates_summary_out: list[dict[str, Any]] = []

    for f in funds_out:
        sid = f["shard"]
        cik = f["cik"]
        fund_shards[sid]["funds"][cik] = {
            "cik": cik,
            "holdings": f["holdings"],
            "equityCurve": f["equityCurve"],
            "quarterlyHistory": f["quarterlyHistory"],
            "midQuarterAlerts": alerts_by_cik.get(cik, []),
        }
        compact_f = {
            k: v for k, v in f.items()
            if k not in ("holdings", "equityCurve", "quarterlyHistory")
        }
        funds_summary_out.append(compact_f)

    _compact_move_keys = ("topNew", "topAdded", "topTrimmed", "topExited", "valuationShifts")
    for u in fund_updates_out:
        sid = u["shard"]
        cik = u["cik"]
        fund_shards[sid]["updates"][cik] = {
            "cik": cik,
            "allMoves": u["allMoves"],
        }
        compact_u = {k: v for k, v in u.items() if k != "allMoves"}
        for mk in _compact_move_keys:
            compact_u[mk] = [
                {ik: iv for ik, iv in item.items() if ik != "industry"}
                for item in (compact_u.get(mk) or [])[:4]
            ]
        updates_summary_out.append(compact_u)

    total_filings_cnt = sum(len(raw_fund_filings[f["cik"]]) for f in funds_out)
    curated_qual_cnt = sum(1 for f in funds_out if f.get("source") == "Curated Core")
    disc_qual_cnt = len(funds_out) - curated_qual_cnt
    ranked_any_ciks = set(ranked_union_ciks["filing_lag"]) | set(ranked_union_ciks["quarter_end"])

    funnel_counts.update({
        "qualifiedFundsCount": len(funds_out),
        "leaderboardEligibleCount": len(lb_funds),
        "elite50Count": len(elite50_ciks),
        "curatedQualifiedCount": curated_qual_cnt,
        "discoveredQualifiedCount": disc_qual_cnt,
        "rankedUnionQeCount": len(ranked_union_ciks["quarter_end"]),
        "rankedUnionLagCount": len(ranked_union_ciks["filing_lag"]),
        "updatesCardsCount": len(fund_updates_out),
        "idealPortfolioCount": len(ideal_positions),
        "stocksIndexedCount": len(stocks_index),
        "midQuarterAlertsCount": len(midquarter_alerts),
    })

    now_dt = datetime.now(timezone.utc)
    season_deadlines = [
        (datetime(now_dt.year, 2, 14, tzinfo=timezone.utc), f"Q4 {now_dt.year - 1} (Mid-Feb {now_dt.year})"),
        (datetime(now_dt.year, 5, 15, tzinfo=timezone.utc), f"Q1 {now_dt.year} (Mid-May {now_dt.year})"),
        (datetime(now_dt.year, 8, 14, tzinfo=timezone.utc), f"Q2 {now_dt.year} (Mid-Aug {now_dt.year})"),
        (datetime(now_dt.year, 11, 14, tzinfo=timezone.utc), f"Q3 {now_dt.year} (Mid-Nov {now_dt.year})"),
        (datetime(now_dt.year + 1, 2, 14, tzinfo=timezone.utc), f"Q4 {now_dt.year} (Mid-Feb {now_dt.year + 1})"),
    ]
    next_season = next(lbl for dt, lbl in season_deadlines if dt >= now_dt - timedelta(days=5))

    summary_payload = {
        "metadata": {
            "generatedAt": now_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "asOfDate": as_of_date,
            "latestQuarterCovered": latest_q_overall,
            "fundsEvaluatedCount": funnel_counts["totalCiksScanned"],
            "qualifiedFundsCount": len(funds_out),
            "leaderboardEligibleCount": len(lb_funds),
            "elite50Count": len(elite50_ciks),
            "rankedFundsUnionCount": len(ranked_any_ciks),
            "total13fFilingsProcessed": total_filings_cnt,
            "next13fSeasonLabel": next_season,
            "riskFreeLatestPct": round(float(rf_ann_arr[-1]), 2),
            "dataSources": [
                "SEC EDGAR Form 13F Bulk Data Sets (2021-Present), EFTS & 13F-HR / 13F-HR/A Filings",
                "SEC EDGAR XBRL CompanyFacts & Schedule 13D/13G Submissions",
                "OpenFIGI CUSIP Security Master",
                "Nasdaq Stock Screener Market Caps (~7,200 US Equities & ADRs)",
                "Yahoo Finance Adjusted OHLCV & ^IRX 13W T-Bill + US Treasury Fiscal Data",
            ],
            "universeFunnel": funnel_counts,
        },
        "benchmarks": benchmarks,
        "funds": funds_summary_out,
        "rankings": rankings,
        "rankedUnionCiks": ranked_union_ciks,
        "topHoldings": top_holdings_out,
        "signalScreener": signal_screener,
        "idealPortfolio": ideal_portfolio_data,
        "fundUpdates": updates_summary_out,
        "updatesOverview": updates_overview,
        "midQuarterAlerts": midquarter_alerts[:40],
        "stocksIndex": stocks_index,
    }

    if (
        len(funds_out) < 25
        or len(ranked_union_ciks["filing_lag"]) < 25
        or total_filings_cnt < 100
        or len(fund_updates_out) < 20
        or len(ideal_positions) < 15
    ):
        raise RuntimeError(
            f"Refusing to overwrite {PUBLIC_DATA_DIR} with incomplete output: "
            f"funds={len(funds_out)}, ranked={len(ranked_union_ciks['filing_lag'])}, "
            f"filings={total_filings_cnt}, updates={len(fund_updates_out)}, ideal={len(ideal_positions)}"
        )

    summary_json = json.dumps(summary_payload, allow_nan=False, separators=(",", ":"))
    if len(summary_json.encode("utf-8")) < 150_000:
        raise RuntimeError(f"Summary payload abnormally small ({len(summary_json)} bytes)")

    PUBLIC_DATA_DIR.mkdir(parents=True, exist_ok=True)
    # Write summary & all 16 fund shards + 8 stock shards atomically via .tmp files
    staged_files: list[tuple[Path, str]] = [(PUBLIC_DATA_DIR / "summary.json", summary_json)]
    for sid, payload in fund_shards.items():
        staged_files.append((PUBLIC_DATA_DIR / f"funds_{sid}.json", json.dumps(payload, allow_nan=False, separators=(",", ":"))))
    for sh_id, payload in stock_shards.items():
        staged_files.append((PUBLIC_DATA_DIR / f"stocks_{sh_id}.json", json.dumps(payload, allow_nan=False, separators=(",", ":"))))

    total_bytes = 0
    for target_p, content in staged_files:
        tmp_p = target_p.with_suffix(".json.tmp")
        tmp_p.write_text(content, encoding="utf-8")
        tmp_p.replace(target_p)
        total_bytes += target_p.stat().st_size

    if LEGACY_MONOLITH_JSON.exists():
        LEGACY_MONOLITH_JSON.unlink()
        print(f"[cleanup] Removed legacy monolith {LEGACY_MONOLITH_JSON}", flush=True)

    summary_kb = (PUBLIC_DATA_DIR / "summary.json").stat().st_size / 1024.0
    print("\n=== Pipeline Complete ===")
    print(f"Wrote {PUBLIC_DATA_DIR / 'summary.json'} ({summary_kb:.1f} KB) + 16 fund shards + 8 stock shards (total {total_bytes / 1024.0:.1f} KB)")
    print(
        f"Universe Funnel: {funnel_counts['totalCiksScanned']} CIKs -> {funnel_counts['activeRecentCount']} active -> "
        f"{funnel_counts['nonExcludedManagerCount']} focused managers -> {funnel_counts['concentrationPassCount']} concentrated -> "
        f"{funnel_counts['techFloorPassCount']} tech floor -> {funnel_counts['strictQualificationCount']} strict -> "
        f"{funnel_counts['qualifiedFundsCount']} verified ({len(lb_funds)} Leaderboard Eligible, {len(elite50_ciks)} Elite 50)"
    )
    print(f"Indexed Stocks: {len(stocks_index)} | Mid-Quarter 13D/13G Alerts: {len(midquarter_alerts)} | Fund Updates: {len(fund_updates_out)}")
    print(
        f"Ideal Portfolio: {len(ideal_positions)} names (+{len(ideal_candidate_pool)} pool) | "
        f"TTM QE Return: {ideal_rets_qe['TTM']}% | TTM 45d-Lag Return: {ideal_rets_lag['TTM']}% | "
        f"QQQ TTM: {benchmarks['QQQ']['returns']['TTM']}%"
    )


if __name__ == "__main__":
    main()

