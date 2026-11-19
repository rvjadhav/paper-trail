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
import hashlib
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
    fetch_form4_insider_signals,
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
SKILL_SHRINKAGE_DAYS = 504.0
DEFAULT_LAG_TX_COST_BPS = 10.0
IDEAL_MAX_SUBSECTOR_CAP_PCT = 30.0


def fund_shard_id(cik: str) -> str:
    return f"{int(cik) % 16:x}"


def stock_shard_id(ticker: str) -> int:
    return zlib.crc32(ticker.encode("utf-8")) % 8


def _is_valid_dossier_ticker(
    raw_t: str,
    cusip: str,
    cusip_meta: dict[str, dict[str, Any]],
    aligned_prices: dict[str, tuple[Any, int, int]],
) -> bool:
    """Reject unresolved CUSIP placeholders, failed-resolution dust, and unpriced bogus symbols."""
    if not raw_t or raw_t.startswith("CUSIP:"):
        return False
    meta_c = cusip_meta.get(cusip, {})
    if meta_c.get("isResolved") is False:
        return False
    canon_t = DUAL_CLASS_CANONICAL.get(raw_t, raw_t)
    if canon_t in aligned_prices:
        return True
    # Unpriced securities must match valid US equity symbol shape (1-5 alpha chars + optional -A/-B class)
    parts = canon_t.split("-")
    if len(parts) > 2:
        return False
    base = parts[0]
    if not base.isalpha() or not (1 <= len(base) <= 5) or base != base.upper():
        return False
    if len(parts) == 2 and (not parts[1].isalpha() or len(parts[1]) != 1 or parts[1] != parts[1].upper()):
        return False
    return True


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
        # Strictly causal terminal handling after last_idx (zero peek at dataset end n-1):
        # for sub-$1 distressed securities, hold 3 trading days for routine quote lag/halts,
        # then decay linearly toward 0 over the next 12 trading days; hold buyouts >=$1 flat.
        end_val = float(arr[last_idx])
        if end_val < 1.0:
            for step_k, i in enumerate(range(last_idx + 1, n), 1):
                if step_k <= 3:
                    arr[i] = end_val
                else:
                    decay = max(0.0, 1.0 - (step_k - 3) / 12.0)
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


def find_idx_strictly_after(cal_dates: list[str], target_date: str) -> int | None:
    """Return index of the first trading day strictly after target_date (T+1 execution)."""
    pos = bisect.bisect_right(cal_dates, target_date)
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
    tx_cost_bps: float = 0.0,
) -> np.ndarray:
    """
    Simulate buy-and-hold within each rebalance segment [(start_idx, weights_dict), ...].
    - Enforces min_priced_coverage (default 70%); leaves NaN breaks on under-covered segments
    - Renormalizes priced position weights to sum to 1.0 so unpriced equities are NOT
      modeled as a 0%-volatility cash buffer
    - When tx_cost_bps > 0, deducts round-trip market impact/spread (2 * one_way_turnover *
      tx_cost_bps / 10_000) against prior drifted ending weights at each rebalance transition.
    Returns array of length n_dates with NaN before first valid segment and Base=100.
    """
    nav = np.full(n_dates, np.nan, dtype=np.float64)
    if not segments:
        return nav

    running_nav = 100.0
    started = False
    prev_end_weights: dict[str, float] | None = None
    for seg_i, (s_idx, weights) in enumerate(segments):
        e_idx = segments[seg_i + 1][0] if seg_i + 1 < len(segments) else (n_dates - 1)
        if s_idx >= n_dates or e_idx <= s_idx:
            if started and s_idx < n_dates and math.isnan(nav[s_idx]):
                nav[s_idx] = running_nav
            continue

        priced_items: list[tuple[str, np.ndarray, float, float]] = []
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
            priced_items.append((ticker, arr, float(w), float(arr[base_idx])))
            priced_weight += float(w)

        if priced_weight < min_priced_coverage or not priced_items:
            if started:
                # Reset running_nav for any subsequent recovered run while leaving NaN gap
                running_nav = 100.0
                prev_end_weights = None
            continue

        norm_weights = {t: w / priced_weight for t, _, w, _ in priced_items}
        if started and tx_cost_bps > 0.0 and prev_end_weights is not None:
            all_k = set(prev_end_weights.keys()) | set(norm_weights.keys())
            one_way_to = 0.5 * sum(abs(norm_weights.get(k, 0.0) - prev_end_weights.get(k, 0.0)) for k in all_k)
            cost_frac = min(0.05, max(0.0, 2.0 * one_way_to * (tx_cost_bps / 10_000.0)))
            running_nav *= 1.0 - cost_frac

        started = True
        span = np.zeros(e_idx - s_idx + 1, dtype=np.float64)
        end_contribs: dict[str, float] = {}
        for ticker, arr, w, base_px in priced_items:
            norm_w = w / priced_weight
            sub = arr[s_idx : e_idx + 1]
            rel = np.where(np.isnan(sub), 1.0, sub / base_px)
            span += norm_w * rel
            end_contribs[ticker] = norm_w * float(rel[-1])

        nav[s_idx : e_idx + 1] = running_nav * span
        running_nav = float(nav[e_idx])
        end_span = float(span[-1])
        if end_span > 0:
            prev_end_weights = {t: c / end_span for t, c in end_contribs.items()}
        else:
            prev_end_weights = None

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


def waterfill_sector_and_stock_capped_weights(
    raw_scores: dict[str, float],
    subsector_by_ticker: dict[str, str] | None = None,
    stock_cap: float = 0.08,
    subsector_cap: float = 0.30,
) -> dict[str, float]:
    """
    Iteratively water-fill proportional weights subject to BOTH:
      - individual stock cap: w[t] <= stock_cap (default 8%)
      - sub-sector cap: sum(w[t] for t in sub_sector) <= subsector_cap (default 30%)
    Falls back cleanly to single-stock waterfill if too few distinct sub-sectors exist
    for the sub-sector cap to reach 100% total weight.
    """
    pos = {k: float(v) for k, v in raw_scores.items() if v > 0}
    if not pos:
        return {}
    if len(pos) * stock_cap < 0.9999:
        stock_cap = 1.0 / len(pos) + 1e-6
    if not subsector_by_ticker or subsector_cap <= 0 or subsector_cap >= 0.9999:
        return waterfill_capped_weights(pos, cap=stock_cap)

    sec_of = {k: subsector_by_ticker.get(k) or "Other" for k in pos}
    by_sec_all: dict[str, list[str]] = {}
    for k, s in sec_of.items():
        by_sec_all.setdefault(s, []).append(k)

    max_feasible = sum(min(subsector_cap, len(ks) * stock_cap) for ks in by_sec_all.values())
    if max_feasible < 0.9999:
        lo, hi = subsector_cap, 1.0
        for _ in range(25):
            mid = 0.5 * (lo + hi)
            if sum(min(mid, len(ks) * stock_cap) for ks in by_sec_all.values()) >= 1.0001:
                hi = mid
            else:
                lo = mid
        subsector_cap = hi

    fixed: dict[str, float] = {}
    free = dict(pos)
    rem_budget = 1.0
    sec_used: dict[str, float] = {}

    for _ in range(len(pos) + len(by_sec_all) + 8):
        if not free or rem_budget <= 1e-12:
            break
        tot_sc = sum(free.values())
        if tot_sc <= 0:
            break
        tent = {k: (sc / tot_sc) * rem_budget for k, sc in free.items()}

        free_by_sec: dict[str, list[str]] = {}
        for k in free:
            free_by_sec.setdefault(sec_of[k], []).append(k)

        over_secs = [
            s
            for s, ks in free_by_sec.items()
            if sec_used.get(s, 0.0) + sum(tent[k] for k in ks) > subsector_cap + 1e-9
            and len(ks) * stock_cap + sec_used.get(s, 0.0) >= subsector_cap - 1e-9
        ]
        if over_secs:
            for s in over_secs:
                ks = free_by_sec[s]
                sec_rem = max(0.0, subsector_cap - sec_used.get(s, 0.0))
                if sec_rem <= 1e-12:
                    for k in ks:
                        fixed[k] = 0.0
                        del free[k]
                    continue
                inner_cap = min(1.0, stock_cap / sec_rem)
                inner_w = waterfill_capped_weights({k: free[k] for k in ks}, cap=inner_cap)
                for k in ks:
                    fixed[k] = inner_w.get(k, 0.0) * sec_rem
                    del free[k]
                sec_used[s] = sec_used.get(s, 0.0) + sec_rem
                rem_budget -= sec_rem
            continue

        over_stocks = [k for k, wt in tent.items() if wt > stock_cap + 1e-9]
        if over_stocks:
            for k in over_stocks:
                fixed[k] = stock_cap
                sec_used[sec_of[k]] = sec_used.get(sec_of[k], 0.0) + stock_cap
                rem_budget -= stock_cap
                del free[k]
            continue

        for k, wt in tent.items():
            fixed[k] = wt
        free.clear()
        break

    return {k: v for k, v in fixed.items() if v > 1e-12}


def _select_top_names_with_subsector_breadth(
    sorted_items: list[tuple[str, float]],
    subsector_by_ticker: dict[str, str] | None,
    max_names: int = 40,
    subsector_cap: float = 0.30,
    excluded_tickers: set[str] | None = None,
) -> dict[str, float]:
    """
    Pick up to max_names highest-scoring stocks while preventing one crowded sub-sector
    from starving diversified slots when a sub-sector cap is active.
    """
    excl = excluded_tickers or set()
    eligible = [(t, sc) for t, sc in sorted_items if t not in excl and sc > 0]
    if not eligible:
        return {}
    if not subsector_by_ticker or len(eligible) <= max_names:
        return dict(eligible[:max_names])

    distinct_secs = {subsector_by_ticker.get(t, "Other") for t, _ in eligible}
    if len(distinct_secs) < 4:
        return dict(eligible[:max_names])

    max_per_sub = max(8, int(math.ceil(max_names * subsector_cap * 1.15)))
    selected: list[tuple[str, float]] = []
    deferred: list[tuple[str, float]] = []
    sec_counts: dict[str, int] = {}

    for t, sc in eligible:
        if len(selected) >= max_names:
            break
        ss = subsector_by_ticker.get(t, "Other")
        if sec_counts.get(ss, 0) < max_per_sub:
            selected.append((t, sc))
            sec_counts[ss] = sec_counts.get(ss, 0) + 1
        else:
            deferred.append((t, sc))

    if len(selected) < max_names and deferred:
        selected.extend(deferred[: max_names - len(selected)])
    return dict(selected)


def _compute_pit_vol_mult(
    pdata: tuple[Any, int, int] | None,
    ref_idx: int,
    target_vol: float = 0.32,
) -> float:
    """
    Strictly point-in-time inverse-sqrt trailing 63-day realized volatility multiplier.
    Damps high-beta cyclical risk budgets (e.g., 75%-vol names -> 0.65x) while boosting
    steady quality compounders (e.g., 22%-vol names -> 1.21x), bounded to [0.65, 1.30].
    """
    if pdata is None or ref_idx <= 0:
        return 1.0
    arr, first_idx, last_idx = pdata
    end_i = min(ref_idx, last_idx)
    start_i = max(first_idx, end_i - 63)
    if end_i - start_i < 20:
        return 1.0
    sub = arr[start_i : end_i + 1]
    if np.any(np.isnan(sub)) or np.any(sub <= 0):
        return 1.0
    rets = np.diff(sub) / sub[:-1]
    if len(rets) < 20:
        return 1.0
    ann_vol = float(np.std(rets, ddof=1)) * math.sqrt(252.0)
    if not math.isfinite(ann_vol) or ann_vol <= 1e-4:
        return 1.0
    raw_mult = (target_vol / max(0.15, ann_vol)) ** 0.50
    return max(0.65, min(1.30, raw_mult))


def _compute_pit_price_regime_mult(
    pdata: tuple[Any, int, int] | None,
    ref_idx: int,
    qd_idx: int | None,
    buy_impulse: float,
) -> float:
    """
    Strictly point-in-time entry-discipline & trend-breakdown guard:
      1. Anti-chasing lag haircut: if price surged >35% between quarter-end and filing-lag
         execution, damp allocation down to 0.78x.
      2. Broken-trend guard: if price sits >10% below trailing 200-DMA AND Elite managers are
         not aggressively net-buying the dip (buy_impulse <= 0.15), damp down to 0.75x.
    """
    if pdata is None or ref_idx <= 0:
        return 1.0
    arr, first_idx, last_idx = pdata
    end_i = min(ref_idx, last_idx)
    if end_i < first_idx or math.isnan(arr[end_i]) or arr[end_i] <= 0:
        return 1.0

    mult = 1.0
    cur_px = float(arr[end_i])

    # 1. Filing-lag parabolic run-up anti-chasing haircut
    if qd_idx is not None and end_i > qd_idx >= first_idx:
        q_px = float(arr[qd_idx])
        if not math.isnan(q_px) and q_px > 0:
            runup = (cur_px / q_px) - 1.0
            if runup > 0.35:
                mult *= max(0.78, 1.0 - (runup - 0.35) * 0.63)

    # 2. 200-DMA structural breakdown guard (only when not aggressively bought on dip)
    if end_i - first_idx >= 180 and buy_impulse <= 0.15:
        sma_start = max(first_idx, end_i - 199)
        win = arr[sma_start : end_i + 1]
        if not np.any(np.isnan(win)):
            sma_200 = float(np.mean(win))
            if sma_200 > 0:
                dist_200 = (cur_px / sma_200) - 1.0
                if dist_200 < -0.10:
                    mult *= max(0.75, 1.0 + (dist_200 + 0.10))

    return mult


def compute_orthogonal_fund_scores(
    funds_subset: list[dict[str, Any]],
    fund_navs_mode: dict[str, np.ndarray],
    cal_dates: list[str],
    win_indices: dict[str, int],
) -> dict[str, float]:
    """
    Score funds across 3 NON-OVERLAPPING orthogonal return horizons (0-12M: 35%,
    12-36M: 40%, 36-60M: 25%) with horizon-coverage Bayesian shrinkage toward
    the 0.625 cohort midpoint so short-history funds cannot leapfrog seasoned
    multi-cycle managers on a single momentum year.
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
        if den > 0:
            raw_o = num / den
            cov = min(1.0, den / 0.75)
            scores[cik] = 0.625 + cov * (raw_o - 0.625)
        else:
            scores[cik] = 0.5
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
    exited_by_ticker: dict[str, int] | None = None,
    actionability_by_ticker: dict[str, dict[str, Any]] | None = None,
    latest_quarter_end: str | None = None,
    subsector_cap_pct: float = IDEAL_MAX_SUBSECTOR_CAP_PCT,
) -> tuple[dict[str, float], dict[str, dict[str, Any]], list[tuple[str, float]]]:
    """
    Score actively traded US stocks across Elite-50 / ranked funds by:
      - 50/50 blended Orthogonal Multi-Horizon Return & Risk-Adjusted Skill Score^1.5
      - sqrt(portfolio weight) × Tech/TMT relevance × multi-fund consensus
      - Full QoQ accumulation impulse INCLUDING complete fund exits (NEW/ADDED/TRIMMED/EXITED)
      - Inverse-sqrt 63d realized volatility sizing + lag anti-chasing & 200-DMA breakdown guard
      - Actionability Matrix alignment (excludes EXIT SIGNAL from Top-40, damps DISTRIBUTION WARNING)
      - Simultaneous single-stock (8%) AND sub-sector (30%) water-fill caps.
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
        fund_scores[cik] = 0.50 * ortho_scores.get(cik, 0.5) + 0.50 * sk_norm

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
                    "netExit": int((exited_by_ticker or {}).get(t, 0)),
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

    ref_idx = (n_dates - 1) if n_dates > 0 else max(0, len(cal_dates) - 1)
    qd_idx = (
        find_idx_on_or_before(cal_dates, latest_quarter_end)
        if (latest_quarter_end and cal_dates)
        else None
    )

    # Require multi-fund backing (>=2) or top high-conviction anchor (>=4.5%)
    qualified: dict[str, float] = {}
    subsector_by_ticker: dict[str, str] = {}
    exit_signal_excluded: set[str] = set()

    for t, rec in stock_agg.items():
        cnt = len(rec["backers"])
        max_w = max(rec["weights"])
        if cnt < 2 and max_w < 4.5:
            continue
        net_exit = int(rec.get("netExit", 0))
        consensus_mult = 1.0 + 0.18 * math.log(max(1, cnt))
        # Continuous QoQ inflection score incorporating NEW, ADDED, TRIMMED, and full EXITED moves
        buy_impulse = (
            1.35 * rec["netNew"] + 1.0 * rec["netAdd"] - 0.85 * rec["netTrim"] - 1.50 * net_exit
        ) / max(1, cnt + net_exit)
        mom_mult = 1.0 + 0.28 * max(-0.85, min(1.0, buy_impulse))
        tech_mult = 1.0 if rec["isTechTmt"] else 0.68

        pdata = aligned_prices.get(t) if aligned_prices is not None else None
        vol_mult = _compute_pit_vol_mult(pdata, ref_idx)
        regime_mult = _compute_pit_price_regime_mult(pdata, ref_idx, qd_idx, buy_impulse)

        act_mult = 1.0
        if actionability_by_ticker is not None:
            act_info = actionability_by_ticker.get(t)
            if act_info:
                verdict = act_info.get("verdict")
                if verdict == "EXIT SIGNAL":
                    act_mult = 0.25
                    exit_signal_excluded.add(t)
                elif verdict == "DISTRIBUTION WARNING":
                    act_mult = 0.65
                elif verdict == "STRONG ACCUMULATE":
                    act_mult = 1.12
                elif verdict == "ACCUMULATE ON DIP":
                    act_mult = 1.04

        score = rec["convSum"] * consensus_mult * mom_mult * tech_mult * vol_mult * regime_mult * act_mult
        rec["rawScore"] = score
        rec["netBuy"] = rec["netNew"] + rec["netAdd"]
        qualified[t] = score
        subsector_by_ticker[t] = classify_sub_sector(rec["sector"] or "Other", rec["industry"] or "", rec["isTechTmt"])

    sorted_items = sorted(qualified.items(), key=lambda x: x[1], reverse=True)
    sub_cap_dec = subsector_cap_pct / 100.0
    top_scores = _select_top_names_with_subsector_breadth(
        sorted_items,
        subsector_by_ticker=subsector_by_ticker,
        max_names=max_names,
        subsector_cap=sub_cap_dec,
        excluded_tickers=exit_signal_excluded,
    )
    capped = waterfill_sector_and_stock_capped_weights(
        top_scores,
        subsector_by_ticker=subsector_by_ticker,
        stock_cap=cap_pct / 100.0,
        subsector_cap=sub_cap_dec,
    )
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
    ticker_sub_sector: dict[str, str] | None = None,
    subsector_cap: float = IDEAL_MAX_SUBSECTOR_CAP_PCT / 100.0,
) -> list[tuple[int, dict[str, float]]]:
    """
    Reconstruct survivorship-free quarterly point-in-time backtest segments for the
    Elite-50 Ideal Portfolio in Quarter-End or zero-look-ahead T+1 45-Day Filing-Lag mode,
    mirroring uniform live fund qualification, rolling 4Q persistence, seasoned >=8Q Elite
    preference, 50/50 Orthogonal + Skill Score, split-adjusted QoQ accumulation/exit impulse,
    PIT volatility sizing, lag anti-chasing / 200-DMA breakdown guard, and 30% sub-sector cap.
    """
    _ = curated_bt_ciks  # Retained for signature compatibility; uniform qualification applied to all CIKs
    segs: list[tuple[int, dict[str, float]]] = []
    # Orthogonal horizons matching live scorer: 0-12M (35%), 12-36M (40%), 36-60M (25%)
    ortho_ranges = [(0, 252, 0.35), (252, 756, 0.40), (756, 1260, 0.25)]

    for qd in all_quarters:
        if qd < min_quarter_date:
            continue
        qd_dt = datetime.strptime(qd, "%Y-%m-%d")
        if bt_mode == "filing_lag":
            deadline_dt = statutory_deadline_date(qd)
            if deadline_dt >= as_of_date:
                continue
            rebal_idx = find_idx_strictly_after(cal_dates, deadline_dt)
            if rebal_idx is None:
                continue
            filing_cutoff_dt = deadline_dt
            rebal_dt = cal_dates[rebal_idx]
        else:
            rebal_dt = qd
            filing_cutoff_dt = qd
            rebal_idx = find_idx_on_or_after(cal_dates, rebal_dt)
            if rebal_idx is None:
                continue

        qd_idx = find_idx_on_or_before(cal_dates, qd)

        active_cands: list[tuple[str, dict[str, Any], dict[str, float], str, list[dict[str, Any]]]] = []
        for cik in all_bt_ciks:
            sn_list = snaps_by_cik.get(cik) or []
            avail = [s for s in sn_list if s["effDate"] <= filing_cutoff_dt]
            if not avail:
                continue
            cur_s = avail[-1]
            # Require filing period within 180d + uniform point-in-time concentration/eligibility
            if (qd_dt - datetime.strptime(cur_s["reportDate"], "%Y-%m-%d")).days > 180:
                continue
            if (
                cur_s["eqAum"] < 100_000_000
                or cur_s["techPct"] < 35.0
                or cur_s["posCount"] < 5
                or cur_s["posCount"] > 250
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
                if med4q_tech < 50.0:
                    continue

            # Prior period snapshot (for split-adjusted +/-5% share-count delta & full exits)
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
                    ei = (
                        find_idx_strictly_after(cal_dates, s_snap["effDate"])
                        if bt_mode == "filing_lag"
                        else find_idx_on_or_after(cal_dates, s_snap["effDate"])
                    )
                    if ei is not None and ei <= rebal_idx:
                        if not eff_indices or ei > eff_indices[-1]:
                            eff_indices.append(ei)
                if not eff_indices or eff_indices[-1] < rebal_idx - 10:
                    eff_indices.append(rebal_idx)
                for ii in range(len(eff_indices) - 1):
                    si_0, si_1 = eff_indices[ii], eff_indices[ii + 1]
                    if (
                        si_1 - si_0 >= 15
                        and si_0 >= c_start
                        and not math.isnan(nav_arr[si_0])
                        and not math.isnan(nav_arr[si_1])
                        and not np.any(np.isnan(nav_arr[si_0 : si_1 + 1]))
                    ):
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
                conf = min(1.0, pit_valid_days[cik] / SKILL_SHRINKAGE_DAYS)
                shrunk = 0.50 + conf * (raw_comp - 0.50)
                pit_skill_norm[cik] = 0.25 + 0.75 * shrunk

        has_1y_cohort = 0 in h_ranks_pit and len(h_ranks_pit[0]) >= 25
        scored_funds: list[tuple[str, float, dict[str, Any], dict[str, float], str, int]] = []
        for cik, cur_s, prev_shares, prev_rd, avail in active_cands:
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
            if den > 0:
                raw_r = num / den
                cov = min(1.0, den / 0.75)
                ortho_pit = 0.25 + 0.75 * (0.50 + cov * (raw_r - 0.50))
            else:
                ortho_pit = 0.5
            if cik in pit_skill_norm:
                sc_pit = 0.50 * ortho_pit + 0.50 * pit_skill_norm[cik]
            else:
                sc_pit = ortho_pit
            distinct_q = len({s["reportDate"] for s in avail})
            scored_funds.append((cik, sc_pit, cur_s, prev_shares, prev_rd, distinct_q))

        seasoned = [x for x in scored_funds if x[5] >= 8]
        cohort_pool = seasoned if len(seasoned) >= 50 else scored_funds
        cohort_pool.sort(key=lambda x: x[1], reverse=True)
        top_cohort = cohort_pool[: min(50, len(cohort_pool))]

        s_scores: dict[str, float] = {}
        s_max_w: dict[str, float] = {}
        s_counts: dict[str, int] = {}
        s_new: dict[str, int] = {}
        s_add: dict[str, int] = {}
        s_trim: dict[str, int] = {}
        s_exit: dict[str, int] = {}

        for _, f_sc, cur_s, prev_shares, prev_rd, _ in top_cohort:
            cur_sh_map = cur_s["shares"]
            cur_rd = cur_s["reportDate"]
            if prev_shares:
                for pt, psh in prev_shares.items():
                    if psh > 0 and cur_sh_map.get(pt, 0.0) <= 0 and not pt.startswith("CUSIP:") and pt not in excluded_etfs:
                        s_exit[pt] = s_exit.get(pt, 0) + 1
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
            ex_cnt = s_exit.get(t, 0)
            cons_m = 1.0 + 0.18 * math.log(max(1, cnt))
            buy_imp = (
                1.35 * s_new.get(t, 0) + 1.0 * s_add.get(t, 0) - 0.85 * s_trim.get(t, 0) - 1.50 * ex_cnt
            ) / max(1, cnt + ex_cnt)
            mom_m = 1.0 + 0.28 * max(-0.85, min(1.0, buy_imp))
            tech_m = 1.0 if ticker_is_tech.get(t, False) else 0.68
            pdata = aligned_prices.get(t)
            vol_m = _compute_pit_vol_mult(pdata, rebal_idx)
            regime_m = _compute_pit_price_regime_mult(
                pdata,
                rebal_idx,
                qd_idx if bt_mode == "filing_lag" else None,
                buy_imp,
            )
            filt_scores[t] = sc * cons_m * mom_m * tech_m * vol_m * regime_m

        sorted_bt = sorted(filt_scores.items(), key=lambda x: x[1], reverse=True)
        top40 = _select_top_names_with_subsector_breadth(
            sorted_bt,
            subsector_by_ticker=ticker_sub_sector,
            max_names=40,
            subsector_cap=subsector_cap,
        )
        bt_w = waterfill_sector_and_stock_capped_weights(
            top40,
            subsector_by_ticker=ticker_sub_sector,
            stock_cap=0.08,
            subsector_cap=subsector_cap,
        )
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
        # Execute on T+1 (next trading day after filingDate) since 13F-HR drops 4:00-5:30 PM ET
        lag_idx = find_idx_strictly_after(cal_dates, ev_fd)
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


def compute_elite_holding_periods(
    elite_ciks: list[str] | set[str],
    raw_fund_filings: dict[str, list[dict[str, Any]]],
    cusip_meta: dict[str, dict[str, Any]],
    excluded_etfs: set[str] | frozenset[str] = NON_STOCK_ETFS,
) -> dict[str, float]:
    """
    Compute average consecutive quarters held among Elite-50 current holders for each stock ticker.
    Resets streak immediately if a fund completely exited the ticker in any intervening quarter.
    """
    streaks_by_ticker: dict[str, list[int]] = {}
    for cik in elite_ciks:
        filings = raw_fund_filings.get(cik) or []
        if not filings:
            continue
        sorted_fls = sorted(filings, key=lambda x: x["reportDate"])
        latest_sh = extract_canon_equity_shares(sorted_fls[-1]["rows"], cusip_meta, excluded_etfs=excluded_etfs)
        curr_held = {t for t, sh in latest_sh.items() if sh > 0}
        if not curr_held:
            continue
        fund_streaks: dict[str, int] = {t: 1 for t in curr_held}
        active = set(curr_held)
        for fl in reversed(sorted_fls[:-1]):
            if not active:
                break
            prev_sh = extract_canon_equity_shares(fl["rows"], cusip_meta, excluded_etfs=excluded_etfs)
            still_held = set()
            for t in active:
                if prev_sh.get(t, 0.0) > 0:
                    fund_streaks[t] += 1
                    still_held.add(t)
            active = still_held

        for t, q_cnt in fund_streaks.items():
            streaks_by_ticker.setdefault(t, []).append(q_cnt)

    return {
        t: round(sum(vals) / len(vals), 1)
        for t, vals in streaks_by_ticker.items()
        if vals
    }


def compute_stock_filing_date_anchors(
    raw_fund_filings: dict[str, list[dict[str, Any]]],
    cusip_meta: dict[str, dict[str, Any]],
    excluded_etfs: set[str] | frozenset[str],
    latest_pe_mode: str,
    elite_ciks: set[str] | frozenset[str] | None = None,
    lb_ciks: set[str] | frozenset[str] | None = None,
) -> dict[str, str]:
    """
    Compute representative SEC public filing disclosure date per ticker by taking the median
    latest-quarter filingDate among Elite-50 holders (falling back to leaderboard-eligible or
    all tracked holders, then the statutory 45-day deadline).
    """
    elite_dates: dict[str, list[str]] = {}
    lb_dates: dict[str, list[str]] = {}
    all_dates: dict[str, list[str]] = {}
    elite_set = elite_ciks or set()
    lb_set = lb_ciks or set()

    for cik, filings in raw_fund_filings.items():
        if not filings:
            continue
        sorted_fls = sorted(filings, key=lambda x: x["reportDate"])
        target_fl = next((fl for fl in reversed(sorted_fls) if fl["reportDate"] == latest_pe_mode), sorted_fls[-1])
        f_date = str(target_fl.get("filingDate") or "")
        if not f_date:
            continue
        held = extract_canon_equity_shares(target_fl["rows"], cusip_meta, excluded_etfs=excluded_etfs)
        for t, sh in held.items():
            if sh <= 0:
                continue
            if cik in elite_set:
                elite_dates.setdefault(t, []).append(f_date)
            if cik in lb_set:
                lb_dates.setdefault(t, []).append(f_date)
            all_dates.setdefault(t, []).append(f_date)

    out: dict[str, str] = {}
    for t in all_dates:
        pool = elite_dates.get(t) or lb_dates.get(t) or all_dates[t]
        pool_sorted = sorted(pool)
        out[t] = pool_sorted[len(pool_sorted) // 2]
    return out


def compute_alpha_decay_metrics(
    ticker: str,
    aligned_prices: dict[str, tuple[np.ndarray, int, int]],
    cal_dates: list[str],
    price_usd: float | None,
    est_quarter_vwap: float | None,
    pct_vs_quarter_vwap: float | None,
    latest_pe_mode: str,
    consensus_filing_date: str | None,
    avg_elite_holding_quarters: float | None,
    new_count: int,
    added_count: int,
    trimmed_count: int,
    exited_count: int,
    elite_buyers_count: int,
    elite_sellers_count: int,
    elite_holders_count: int,
    net_est_flow_usd: float | None,
) -> dict[str, Any]:
    """
    Post-13F Lag & Alpha-Decay Engine ('Is the Move Already Over?'):
      - Computes 3-timestamp price return bridge: Est. Q-VWAP -> SEC Filing Date -> Today
      - Tracks Average Smart-Money Holding Period across Elite-50 holders
      - Flags FOMO / Late Crowded traps (>20% above Q-VWAP while Elite/funds trim into strength)
        vs. Lag Advantage dips (<=Q-VWAP after strong NEW/ADDED buying clusters)
    """
    anchor_date = consensus_filing_date or statutory_deadline_date(latest_pe_mode)
    filing_px: float | None = None
    if ticker in aligned_prices and cal_dates and anchor_date:
        arr, fi, li = aligned_prices[ticker]
        try:
            anchor_dt = datetime.strptime(anchor_date, "%Y-%m-%d").date()
        except Exception:
            anchor_dt = None
        cand_idx = find_idx_on_or_after(cal_dates, anchor_date)
        chosen_idx: int | None = None
        if cand_idx is not None and fi <= cand_idx <= li and anchor_dt is not None:
            cand_dt = datetime.strptime(cal_dates[cand_idx], "%Y-%m-%d").date()
            if abs((cand_dt - anchor_dt).days) <= 7:
                chosen_idx = cand_idx
        if chosen_idx is None:
            bef_idx = find_idx_on_or_before(cal_dates, anchor_date)
            if bef_idx is not None and fi <= bef_idx <= li and anchor_dt is not None:
                bef_dt = datetime.strptime(cal_dates[bef_idx], "%Y-%m-%d").date()
                if abs((anchor_dt - bef_dt).days) <= 7:
                    chosen_idx = bef_idx
        if chosen_idx is not None:
            raw_px = float(arr[chosen_idx])
            if math.isfinite(raw_px) and raw_px > 0:
                filing_px = clean_num(raw_px, 2)

    q_vwap_to_filing: float | None = None
    if filing_px is not None and est_quarter_vwap is not None and est_quarter_vwap > 0:
        q_vwap_to_filing = round(((filing_px / est_quarter_vwap) - 1.0) * 100.0, 1)

    filing_to_today: float | None = None
    if price_usd is not None and price_usd > 0 and filing_px is not None and filing_px > 0:
        filing_to_today = round(((price_usd / filing_px) - 1.0) * 100.0, 1)

    pct_vs = pct_vs_quarter_vwap
    if pct_vs is None and price_usd is not None and price_usd > 0 and est_quarter_vwap is not None and est_quarter_vwap > 0:
        pct_vs = round(((price_usd / est_quarter_vwap) - 1.0) * 100.0, 1)

    total_buyers = new_count + added_count
    total_sellers = trimmed_count + exited_count
    flow = net_est_flow_usd or 0.0

    is_fomo_trap = (
        pct_vs is not None
        and pct_vs > 20.0
        and (
            (
                elite_sellers_count >= 1
                and (
                    elite_sellers_count > elite_buyers_count
                    or (elite_sellers_count == elite_buyers_count and total_sellers >= total_buyers)
                    or flow < 0
                )
            )
            or (total_sellers >= 2 and total_sellers > total_buyers and flow < 0)
        )
    )
    is_lag_advantage = (
        pct_vs is not None
        and pct_vs <= 0.0
        and total_buyers >= 2
        and total_buyers > total_sellers
        and (new_count >= 2 or flow > 0 or elite_buyers_count >= 1 or elite_holders_count >= 1)
    )

    if is_fomo_trap:
        status = "FOMO_TRAP"
        reason = (
            f"Trading {pct_vs:+.1f}% above Est. Q-VWAP while "
            f"{elite_sellers_count} Elite-50 & {total_sellers} total funds trimmed/exited vs. {total_buyers} buys."
        )
    elif is_lag_advantage:
        status = "LAG_ADVANTAGE"
        reason = (
            f"Available at {pct_vs:+.1f}% vs. Est. Q-VWAP after {new_count} NEW & "
            f"{added_count} ADDED cluster buys (vs. {total_sellers} cuts)."
        )
    elif (pct_vs is not None and pct_vs > 20.0) or (filing_to_today is not None and filing_to_today > 15.0):
        status = "POST_FILING_EXTENDED"
        if pct_vs is not None and pct_vs > 20.0:
            reason = f"Trading {pct_vs:+.1f}% above Est. Q-VWAP with net institutional buying (+{total_buyers}/-{total_sellers}); entry chases the move."
        else:
            reason = f"Surged {filing_to_today:+.1f}% since the 13F filing date; verify risk/reward before chasing."
    else:
        status = "NEUTRAL"
        reason = "Trading near institutional cost basis without skewed FOMO distribution or cluster dip dislocation."

    return {
        "consensusFilingDate": anchor_date,
        "filingDatePriceUsd": filing_px,
        "qVwapToFilingPct": q_vwap_to_filing,
        "filingToTodayPct": filing_to_today,
        "avgEliteHoldingQuarters": avg_elite_holding_quarters,
        "eliteBuyersCount": elite_buyers_count,
        "eliteSellersCount": elite_sellers_count,
        "alphaDecayStatus": status,
        "alphaDecayReason": reason,
    }


def _interp_knots(val: float, knots: list[tuple[float, float]]) -> float:
    """Piecewise-linear clamped interpolation across sorted (x, y) knots."""
    if val <= knots[0][0]:
        return knots[0][1]
    if val >= knots[-1][0]:
        return knots[-1][1]
    for i in range(1, len(knots)):
        x0, y0 = knots[i - 1]
        x1, y1 = knots[i]
        if val <= x1:
            span = x1 - x0
            t = (val - x0) / span if span > 0 else 0.0
            return y0 + t * (y1 - y0)
    return knots[-1][1]


def _quarter_sort_key(q_lbl: str) -> tuple[int, int]:
    parts = q_lbl.split()
    try:
        return (int(parts[0]), int(parts[1].lstrip("Qq")))
    except Exception:
        return (0, 0)


def compute_stock_thesis_persistence(
    raw_fund_filings: dict[str, list[dict[str, Any]]],
    cusip_meta: dict[str, dict[str, Any]],
    excluded_etfs: set[str] | frozenset[str] = NON_STOCK_ETFS,
    splits_map: dict[str, list[dict[str, Any]]] | None = None,
    eligible_ciks: set[str] | frozenset[str] | None = None,
    elite_ciks: set[str] | frozenset[str] | None = None,
    latest_pe_mode: str | None = None,
) -> dict[str, dict[str, Any]]:
    """
    Measure multi-quarter split-adjusted institutional accumulation vs. distribution persistence
    across up to the last 4 QoQ transitions (5 quarterly filings per eligible manager).
    """
    target_ciks = set(eligible_ciks) if eligible_ciks else set(raw_fund_filings.keys())
    elite_set = set(elite_ciks) if elite_ciks else set()
    max_q_key = _quarter_sort_key(quarter_label(latest_pe_mode)) if latest_pe_mode else None

    # ticker -> quarter_label -> [buyers, sellers, elite_buyers, elite_sellers]
    per_ticker_q: dict[str, dict[str, list[int]]] = {}

    for cik in target_ciks:
        filings = raw_fund_filings.get(cik) or []
        if len(filings) < 2:
            continue
        by_q: dict[str, dict[str, Any]] = {}
        for fl in sorted(filings, key=lambda x: x["reportDate"]):
            q_lbl = quarter_label(fl["reportDate"])
            if max_q_key and _quarter_sort_key(q_lbl) > max_q_key:
                continue
            by_q[q_lbl] = fl
        ordered_qs = sorted(by_q.keys(), key=_quarter_sort_key)[-6:]
        if len(ordered_qs) < 2:
            continue

        is_elite = cik in elite_set
        q_shares = [
            (
                q_lbl,
                by_q[q_lbl]["reportDate"],
                extract_canon_equity_shares(by_q[q_lbl]["rows"], cusip_meta, excluded_etfs=excluded_etfs),
            )
            for q_lbl in ordered_qs
        ]
        for idx in range(1, len(q_shares)):
            _, prev_rd, prev_sh = q_shares[idx - 1]
            curr_q, curr_rd, curr_sh = q_shares[idx]
            for t in set(prev_sh.keys()) | set(curr_sh.keys()):
                psh_raw = prev_sh.get(t, 0.0)
                csh = curr_sh.get(t, 0.0)
                psh = psh_raw
                if psh_raw > 0 and csh > 0 and splits_map and not t.startswith("CUSIP:"):
                    sp_fac, _ = split_factor_between(t, prev_rd, curr_rd, splits_map)
                    psh = psh_raw * sp_fac
                if psh <= 0 and csh > 0:
                    side = 1
                elif psh > 0 and csh <= 0:
                    side = -1
                else:
                    dp = (csh / psh) - 1.0
                    side = 1 if dp >= 0.05 else (-1 if dp <= -0.05 else 0)
                if side == 0:
                    continue
                t_qs = per_ticker_q.setdefault(t, {})
                cnts = t_qs.get(curr_q)
                if cnts is None:
                    cnts = [0, 0, 0, 0]
                    t_qs[curr_q] = cnts
                if side > 0:
                    cnts[0] += 1
                    if is_elite:
                        cnts[2] += 1
                else:
                    cnts[1] += 1
                    if is_elite:
                        cnts[3] += 1

    out: dict[str, dict[str, Any]] = {}
    for t, q_map in per_ticker_q.items():
        recent_qs = sorted(q_map.keys(), key=_quarter_sort_key)[-4:]
        if not recent_qs:
            continue

        def _net_sign(c: list[int]) -> int:
            b, s, eb, es = c
            if b > s or (b == s and eb > es):
                return 1
            if s > b or (b == s and es > eb):
                return -1
            return 0

        signs = [_net_sign(q_map[q]) for q in recent_qs]
        net_buy_4 = sum(1 for sg in signs if sg > 0)
        net_sell_4 = sum(1 for sg in signs if sg < 0)

        consec_buy = 0
        for sg in reversed(signs):
            if sg > 0:
                consec_buy += 1
            else:
                break

        consec_sell = 0
        for sg in reversed(signs):
            if sg < 0:
                consec_sell += 1
            else:
                break

        lat_b, lat_s, _, _ = q_map[recent_qs[-1]]
        if consec_buy >= 1 or (consec_sell == 0 and net_buy_4 > net_sell_4):
            direction = "ACCUMULATION"
        elif consec_sell >= 1 or (consec_buy == 0 and net_sell_4 > net_buy_4):
            direction = "DISTRIBUTION"
        else:
            direction = "MIXED"

        n_obs = len(recent_qs)
        if consec_buy >= 2:
            summary = f"{consec_buy}Q consecutive net buying (latest: +{lat_b} buy vs -{lat_s} sell)"
        elif consec_sell >= 2:
            summary = f"{consec_sell}Q consecutive net distribution (latest: +{lat_b} buy vs -{lat_s} sell)"
        elif consec_buy == 1:
            summary = f"Fresh net buying in latest Q (+{lat_b} buy vs -{lat_s} sell; {net_buy_4}/{n_obs}Q net buy)"
        elif consec_sell == 1:
            summary = f"Net selling in latest Q (+{lat_b} buy vs -{lat_s} sell; {net_sell_4}/{n_obs}Q net sell)"
        else:
            summary = f"Balanced multi-quarter flow ({net_buy_4} buy / {net_sell_4} sell across last {n_obs}Q)"

        out[t] = {
            "consecutiveNetBuyQuarters": consec_buy,
            "consecutiveNetSellQuarters": consec_sell,
            "netBuyQuartersLast4": net_buy_4,
            "netSellQuartersLast4": net_sell_4,
            "quartersObserved": n_obs,
            "direction": direction,
            "summary": summary,
        }
    return out


def _tier_default_skill(tier: str | None) -> float:
    if tier == "Elite 50":
        return 82.0
    if tier == "Top 100":
        return 66.0
    if tier == "Qualified":
        return 52.0
    return 44.0


def compute_actionability_matrix(
    *,
    new_count: int = 0,
    added_count: int = 0,
    trimmed_count: int = 0,
    exited_count: int = 0,
    elite_buyers_count: int = 0,
    elite_sellers_count: int = 0,
    elite_holders_count: int = 0,
    total_value_usd: float = 0.0,
    net_est_flow_usd: float | None = None,
    movers: list[dict[str, Any]] | None = None,
    holders: list[dict[str, Any]] | None = None,
    fund_skill_by_cik: dict[str, float | None] | None = None,
    pct_vs_quarter_vwap: float | None = None,
    filing_to_today_pct: float | None = None,
    alpha_decay_status: str | None = None,
    fundamentals: dict[str, Any] | None = None,
    avg_elite_holding_quarters: float | None = None,
    thesis_persistence: dict[str, Any] | None = None,
    is_core_consensus: bool = False,
    market_cap_usd: float | None = None,
) -> dict[str, Any]:
    """
    Unified Actionable Buy / Trim / Exit Conviction Matrix:
      Combines 4 independent pillars (each 0-25 -> 0-100 Total Score):
        1. Smart-Money Flow Impulse (split- & price-adjusted net dollar flow normalized by market cap & Elite skill)
        2. Price Discipline (humped distance to Est. Quarter-VWAP rewarding -5%..-10% entries & penalizing chases/falling knives)
        3. XBRL Quality Gate (Rev Growth + FCF Margin less SBC/Share Dilution penalty)
        4. Thesis Persistence (consecutive quarters of net buying vs. multi-fund distribution)
    """
    skill_map = fund_skill_by_cik or {}

    # 1. Smart-Money Flow Impulse (0-25)
    total_buyers = max(0, int(new_count) + int(added_count))
    total_sellers = max(0, int(trimmed_count) + int(exited_count))

    buyer_skills: list[float] = []
    seller_skills: list[float] = []
    seen_move_ciks: set[str] = set()

    for mv in movers or []:
        cik = mv.get("cik", "")
        if cik:
            seen_move_ciks.add(cik)
        sk = mv.get("skillScore")
        if sk is None and cik:
            sk = skill_map.get(cik)
        if sk is None:
            sk = _tier_default_skill(mv.get("fundTier"))
        act = mv.get("action")
        if act in ("NEW", "ADDED"):
            buyer_skills.append(float(sk))
        elif act in ("TRIMMED", "EXITED"):
            seller_skills.append(float(sk))

    for h in holders or []:
        cik = h.get("cik", "")
        if cik and cik in seen_move_ciks:
            continue
        sk = h.get("skillScore")
        if sk is None and cik:
            sk = skill_map.get(cik)
        if sk is None:
            sk = _tier_default_skill(h.get("fundTier"))
        st = h.get("status")
        if st in ("NEW", "ADDED"):
            buyer_skills.append(float(sk))
        elif st == "TRIMMED":
            seller_skills.append(float(sk))

    avg_b_skill = (
        sum(buyer_skills) / len(buyer_skills)
        if buyer_skills
        else (82.0 if elite_buyers_count > 0 else 52.0)
    )
    avg_s_skill = (
        sum(seller_skills) / len(seller_skills)
        if seller_skills
        else (82.0 if elite_sellers_count > 0 else 52.0)
    )
    avg_active_skill = (
        (avg_b_skill * total_buyers + avg_s_skill * total_sellers) / max(1, total_buyers + total_sellers)
    )
    buy_w = total_buyers * (0.5 + min(100.0, max(0.0, avg_b_skill)) / 100.0)
    sell_w = total_sellers * (0.5 + min(100.0, max(0.0, avg_s_skill)) / 100.0)

    tot_w = buy_w + sell_w
    if tot_w > 0:
        part_balance = (buy_w - sell_w) / tot_w
        part_conf = min(1.0, tot_w / 4.5)
        part_score = 6.5 + 6.5 * part_balance * part_conf
    else:
        part_score = 6.5

    if net_est_flow_usd is not None:
        skill_mult = min(1.5, max(0.75, avg_active_skill / 55.0))
        eff_flow = float(net_est_flow_usd) * skill_mult
        abs_tanh = math.tanh(eff_flow / 120_000_000.0)
        has_rel = bool(total_value_usd and total_value_usd > 0)
        rel_tanh = (
            math.tanh(eff_flow / (0.35 * max(float(total_value_usd), 25_000_000.0)))
            if has_rel
            else abs_tanh
        )
        if market_cap_usd is not None and market_cap_usd > 0:
            mcap_scale = max(float(market_cap_usd) * 0.005, 15_000_000.0)
            mcap_tanh = math.tanh(eff_flow / mcap_scale)
            flow_signal = (
                0.45 * mcap_tanh + 0.35 * rel_tanh + 0.20 * abs_tanh
                if has_rel
                else 0.65 * mcap_tanh + 0.35 * abs_tanh
            )
        elif has_rel:
            flow_signal = 0.65 * abs_tanh + 0.35 * rel_tanh
        else:
            flow_signal = abs_tanh
        flow_sub = 6.0 + 6.0 * flow_signal
        flow_impulse = min(25.0, max(0.0, part_score + flow_sub))
    else:
        flow_impulse = min(25.0, max(0.0, 12.5 + (part_score - 6.5) * (8.5 / 6.5)))

    # 2. Price Discipline (0-25): humped curve peaks near institutional Q-VWAP (-10%..-5%)
    # and damps both parabolic chases (>+15%) and severe falling-knife breakdowns (<-20%)
    if pct_vs_quarter_vwap is None:
        price_discipline = 12.5
    else:
        price_discipline = _interp_knots(
            float(pct_vs_quarter_vwap),
            [
                (-45.0, 7.0),
                (-30.0, 12.0),
                (-20.0, 18.0),
                (-10.0, 24.0),
                (-5.0, 25.0),
                (0.0, 22.0),
                (5.0, 18.5),
                (10.0, 15.0),
                (15.0, 11.5),
                (20.0, 8.5),
                (30.0, 5.0),
                (45.0, 2.5),
            ],
        )
    if filing_to_today_pct is not None and filing_to_today_pct > 20.0:
        price_discipline = max(0.0, price_discipline - 1.5)
    if alpha_decay_status == "FOMO_TRAP":
        price_discipline = min(5.0, max(0.0, price_discipline - 2.0))

    # 3. XBRL Quality Gate (0-25): Rev Growth + FCF/Op Margin - SBC/Share Dilution penalty
    if not fundamentals:
        xbrl_quality = 12.5
    else:
        rev_yoy = fundamentals.get("revenueGrowthYoYPct")
        fcf_m = fundamentals.get("fcfMarginPct")
        op_m = fundamentals.get("operatingMarginPct")
        dil_yoy = fundamentals.get("shareDilutionYoYPct")

        rev_pts = (
            _interp_knots(
                float(rev_yoy),
                [(-20.0, 0.0), (-10.0, 2.5), (0.0, 5.0), (10.0, 7.0), (20.0, 8.8), (40.0, 11.0)],
            )
            if rev_yoy is not None
            else 5.5
        )
        raw_mgn = fcf_m if fcf_m is not None else op_m
        if raw_mgn is not None:
            mgn_pts = _interp_knots(
                float(raw_mgn),
                [(-25.0, 0.0), (-10.0, 2.0), (0.0, 4.5), (10.0, 6.4), (20.0, 8.2), (35.0, 10.0)],
            )
            if fcf_m is None and mgn_pts > 5.0:
                mgn_pts = 5.0 + 0.85 * (mgn_pts - 5.0)
        else:
            mgn_pts = 5.0

        dil_adj = (
            _interp_knots(
                float(dil_yoy),
                [(-3.0, 4.0), (0.0, 2.5), (2.0, 1.0), (4.0, -1.0), (8.0, -4.5), (12.0, -7.0)],
            )
            if dil_yoy is not None
            else 2.0
        )
        xbrl_quality = min(25.0, max(0.0, rev_pts + mgn_pts + dil_adj))

    # 4. Thesis Persistence (0-25)
    has_multi_q = thesis_persistence is not None and (thesis_persistence.get("quartersObserved") or 0) > 0
    if has_multi_q and thesis_persistence is not None:
        cb = int(thesis_persistence.get("consecutiveNetBuyQuarters", 0))
        cs = int(thesis_persistence.get("consecutiveNetSellQuarters", 0))
        nb4 = int(thesis_persistence.get("netBuyQuartersLast4", 0))
        ns4 = int(thesis_persistence.get("netSellQuartersLast4", 0))
        if cb >= 4:
            streak_delta = 9.0
        elif cb == 3:
            streak_delta = 7.5
        elif cb == 2:
            streak_delta = 5.5
        elif cb == 1:
            streak_delta = 2.5 - (1.0 if ns4 >= 2 else 0.0)
        elif cs >= 4:
            streak_delta = -10.5
        elif cs == 3:
            streak_delta = -8.5
        elif cs == 2:
            streak_delta = -6.5
        elif cs == 1:
            streak_delta = -3.5 - (1.5 if exited_count >= 2 else 0.0)
        else:
            streak_delta = max(-4.0, min(4.0, (nb4 - ns4) * 1.5))
    else:
        tot_m = total_buyers + total_sellers
        breadth = ((total_buyers - total_sellers) / tot_m) * min(1.0, tot_m / 4.0) if tot_m > 0 else 0.0
        streak_delta = breadth * 7.5
        if exited_count >= 2 and total_sellers > total_buyers:
            streak_delta -= 2.0
        elif new_count >= 2 and total_buyers > total_sellers:
            streak_delta += 1.5
        if is_core_consensus and total_buyers >= total_sellers:
            streak_delta += 1.5

    hold_delta = 0.0
    if avg_elite_holding_quarters is not None and avg_elite_holding_quarters > 0:
        if avg_elite_holding_quarters >= 8.0:
            hold_delta = 3.5
        elif avg_elite_holding_quarters >= 4.0:
            hold_delta = 2.5
        elif avg_elite_holding_quarters >= 2.0:
            hold_delta = 1.0
        elif total_sellers > total_buyers:
            hold_delta = -1.5
    elif elite_holders_count >= 5 and total_buyers >= total_sellers:
        hold_delta = 2.0
    elif elite_holders_count >= 2 and total_buyers >= total_sellers:
        hold_delta = 1.0

    persistence_score = min(25.0, max(0.0, 12.5 + streak_delta + hold_delta))

    flow_r = round(flow_impulse, 1)
    price_r = round(price_discipline, 1)
    xbrl_r = round(xbrl_quality, 1)
    pers_r = round(persistence_score, 1)
    total_score = round(min(100.0, max(0.0, flow_r + price_r + xbrl_r + pers_r)), 1)

    if total_score >= 72.0:
        verdict = "STRONG ACCUMULATE"
    elif total_score >= 60.0:
        verdict = "ACCUMULATE ON DIP"
    elif total_score >= 50.0:
        verdict = "HOLD"
    elif total_score >= 35.0:
        verdict = "DISTRIBUTION WARNING"
    else:
        verdict = "EXIT SIGNAL"

    has_neg_or_flat_flow = net_est_flow_usd is None or net_est_flow_usd <= 0.0
    has_neg_flow = net_est_flow_usd is None or net_est_flow_usd < 0.0
    has_distribution_evidence = (
        (total_sellers > total_buyers)
        or (net_est_flow_usd is not None and net_est_flow_usd < 0.0)
        or (exited_count >= 2)
    )
    has_multi_q_sell = bool(
        thesis_persistence and int(thesis_persistence.get("consecutiveNetSellQuarters", 0)) >= 2
    )
    active_pool = max(1, total_buyers + total_sellers)
    base_holders = max(active_pool, int(elite_holders_count), len(holders or []))
    exit_ratio = exited_count / max(1, base_holders + exited_count)
    sell_dominance = total_sellers / active_pool
    flow_out_ratio = (
        (-float(net_est_flow_usd) / float(total_value_usd))
        if (net_est_flow_usd is not None and net_est_flow_usd < 0.0 and total_value_usd and total_value_usd > 0)
        else 0.0
    )
    severe_exit_signal = (
        (
            exited_count >= 2
            and total_sellers >= total_buyers + 2
            and sell_dominance >= 0.65
            and (exit_ratio >= 0.15 or flow_out_ratio >= 0.15)
            and has_neg_or_flat_flow
            and total_score < 58.0
        )
        or (
            flow_r <= 6.0
            and pers_r <= 6.5
            and total_sellers >= total_buyers + 3
            and (exited_count >= 1 or has_multi_q_sell)
            and has_neg_flow
        )
    )

    if severe_exit_signal or (total_score < 35.0 and has_distribution_evidence):
        verdict = "EXIT SIGNAL"
        summary = (
            f"Multi-fund institutional distribution (+{total_buyers} buy vs -{total_sellers} sell, "
            f"{exited_count} exits); capital preservation favors exit."
        )
    elif (
        alpha_decay_status == "FOMO_TRAP"
        or (flow_r <= 9.5 and total_sellers > total_buyers and has_neg_flow)
        or (pers_r <= 8.5 and total_sellers > total_buyers)
        or (total_score < 50.0 and has_distribution_evidence)
    ):
        verdict = "DISTRIBUTION WARNING"
        if alpha_decay_status == "FOMO_TRAP":
            summary = (
                f"Trading extended above Q-VWAP while smart money distributes "
                f"(Flow {flow_r}/25, Price {price_r}/25); avoid chasing strength."
            )
        else:
            summary = (
                f"Smart-money outflows & trimming (+{total_buyers}/-{total_sellers}) outweigh fundamentals "
                f"({total_score}/100); trim risk or wait for stabilization."
            )
    elif total_score < 50.0:
        verdict = "HOLD"
        if price_r < 8.0 and xbrl_r < 9.0:
            summary = (
                f"Institutional selling is limited (+{total_buyers}/-{total_sellers}), but extended Q-VWAP pricing "
                f"({price_r}/25) & XBRL gate ({xbrl_r}/25) hold rating at {total_score}/100."
            )
        elif price_r < 8.0:
            summary = (
                f"Institutions remain supportive (+{total_buyers}/-{total_sellers}), but extended Q-VWAP pricing "
                f"({price_r}/25) caps reward-to-risk at Hold."
            )
        elif xbrl_r < 9.0:
            summary = (
                f"Flows are constructive (+{total_buyers}/-{total_sellers}), but XBRL profitability/dilution gate "
                f"({xbrl_r}/25) caps conviction at Hold."
            )
        else:
            summary = (
                f"Mixed conviction profile ({total_score}/100: Flow {flow_r}, Price {price_r}, "
                f"XBRL {xbrl_r}, Persist {pers_r}); hold pending stronger alignment."
            )
    elif verdict == "STRONG ACCUMULATE":
        if price_r < 12.0:
            verdict = "ACCUMULATE ON DIP"
            summary = (
                f"Strong Elite flow ({flow_r}/25) & quality ({xbrl_r}/25), but price is extended vs. Est. Q-VWAP "
                f"(Price {price_r}/25) — stage buys on pullbacks."
            )
        elif xbrl_r < 8.0:
            verdict = "HOLD"
            summary = (
                f"Strong institutional buying offset by weak XBRL fundamentals / SBC dilution "
                f"(XBRL Gate {xbrl_r}/25); monitor execution."
            )
        elif xbrl_r < 10.5 or flow_r < 14.0:
            verdict = "ACCUMULATE ON DIP"
            summary = (
                f"Constructive multi-pillar profile ({total_score}/100); prefer staged entries near institutional Q-VWAP."
            )
        else:
            summary = (
                f"Aligned 4-pillar conviction ({total_score}/100): positive skill-weighted inflows ({flow_r}/25), "
                f"disciplined entry ({price_r}/25), XBRL quality ({xbrl_r}/25) & persistence ({pers_r}/25)."
            )
    elif verdict == "ACCUMULATE ON DIP":
        if xbrl_r < 7.5:
            verdict = "HOLD"
            summary = (
                f"Institutional interest present, but XBRL profitability/dilution gate ({xbrl_r}/25) "
                f"warrants patience at Hold."
            )
        elif price_r < 12.0:
            summary = (
                f"High smart-money conviction capped by extended price vs. Q-VWAP ({price_r}/25); "
                f"target pullbacks toward institutional cost basis."
            )
        else:
            summary = (
                f"Constructive accumulation setup ({total_score}/100) near favorable Q-VWAP cost basis ({price_r}/25)."
            )
    else:
        summary = (
            f"Balanced signal across pillars ({total_score}/100: Flow {flow_r}, Price {price_r}, "
            f"XBRL {xbrl_r}, Persist {pers_r}); maintain current sizing."
        )

    return {
        "score": total_score,
        "verdict": verdict,
        "pillars": {
            "flowImpulse": flow_r,
            "priceDiscipline": price_r,
            "xbrlQuality": xbrl_r,
            "thesisPersistence": pers_r,
        },
        "summary": summary,
        "thesisPersistence": thesis_persistence,
    }


_CONVERGENCE_TIER_ORDER = {
    "DUAL_CONVICTION_PRIME": 5,
    "ACTIVIST_13F_CONVERGENCE": 4,
    "DUAL_CONVICTION_EXTENDED": 3,
    "INSIDER_CLUSTER_WATCH": 2,
    "ACTIVIST_13D_CATALYST": 1,
}


def compute_dual_conviction_signal(
    *,
    ticker: str,
    name: str,
    sector: str = "Other",
    sub_sector: str = "Other",
    form4_summary: dict[str, Any] | None = None,
    alerts: list[dict[str, Any]] | None = None,
    price_usd: float | None = None,
    est_quarter_vwap: float | None = None,
    pct_vs_quarter_vwap: float | None = None,
    alpha_decay_status: str | None = None,
    actionability_score: float | None = None,
    action_verdict: str | None = None,
    new_count: int = 0,
    added_count: int = 0,
    trimmed_count: int = 0,
    exited_count: int = 0,
    elite_buyers_count: int = 0,
    elite_sellers_count: int = 0,
    elite_holders_count: int = 0,
    net_est_flow_usd: float | None = None,
) -> dict[str, Any] | None:
    """
    Cross-reference SEC Form 4 open-market ('P') insider buys, Schedule 13D/13G Item 4 activist
    catalysts, Elite 13F institutional accumulation, and Est. Quarter-VWAP valuation proximity
    to surface rare Dual-Conviction Convergence signals.
    """
    f4 = form4_summary or {}
    open_buy_cnt = int(f4.get("openMarketBuyCount") or 0)
    insider_buy_val = float(f4.get("totalBuyValueUsd") or 0.0)
    buyers_cnt = int(f4.get("distinctBuyersCount") or 0)
    csuite_cnt = int(f4.get("cSuiteBuyersCount") or 0)
    non10b5_cnt = int(f4.get("non10b5Count") or 0)
    insider_label = str(f4.get("insiderSignalLabel") or "NONE")
    is_sell_heavy = insider_label in ("MIXED_SELL_HEAVY", "CLUSTER_SELL", "DISCRETIONARY_SELL")
    has_insider_buy = open_buy_cnt > 0 and not is_sell_heavy
    has_form4_cluster = bool(
        f4
        and not is_sell_heavy
        and (
            f4.get("clusterDetected")
            or insider_label == "CLUSTER_BUY"
            or (csuite_cnt >= 1 and insider_buy_val >= 100_000.0)
        )
    )
    latest_insider_date = f4.get("latestBuyDate") or f4.get("latestFilingDate") or None

    top_insiders: list[str] = []
    seen_ins: set[str] = set()
    for tx in f4.get("recentTransactions") or []:
        iname = str(tx.get("insiderName") or "").strip()
        irole = str(tx.get("insiderRole") or "").strip()
        lbl = f"{iname} ({irole})" if (iname and irole) else (iname or irole)
        if lbl and lbl not in seen_ins:
            seen_ins.add(lbl)
            top_insiders.append(lbl)
        if len(top_insiders) >= 3:
            break

    active_alerts = [
        al for al in (alerts or [])
        if not str(al.get("stakeStatus") or "").startswith("Exited")
    ]
    activist_alerts = [al for al in active_alerts if al.get("isActivist")]
    passive_13g_alerts = [
        al for al in active_alerts
        if not al.get("isActivist")
        and (
            al.get("percentOfClass") is None
            or float(al.get("percentOfClass") or 0.0) >= 5.0
            or str(al.get("stakeStatus") or "").startswith("Active")
        )
    ]

    has_13d = len(activist_alerts) > 0
    has_13g = (not has_13d) and len(passive_13g_alerts) > 0

    activist_funds: list[str] = []
    seen_af: set[str] = set()
    for al in activist_alerts:
        fn = str(al.get("fundName") or "").strip()
        if fn and fn not in seen_af:
            seen_af.add(fn)
            activist_funds.append(fn)
        if len(activist_funds) >= 3:
            break

    top_13d = activist_alerts[0] if activist_alerts else None
    activist_cat = (
        str(top_13d.get("item4Category") or "Active 13D")
        if top_13d
        else None
    )
    activist_intent = (str(top_13d.get("item4Intent") or "").strip() or None) if top_13d else None
    activist_date = (str(top_13d.get("filingDate") or "").strip() or None) if top_13d else None

    total_buyers = max(0, int(new_count + added_count))
    total_sellers = max(0, int(trimmed_count + exited_count))
    flow_val = float(net_est_flow_usd or 0.0)
    is_fomo = alpha_decay_status == "FOMO_TRAP"
    is_dist_verdict = action_verdict in ("EXIT SIGNAL", "DISTRIBUTION WARNING")
    flow_positive_or_unknown = (net_est_flow_usd is None) or (flow_val > 0.0)
    has_13f_accum = (not is_fomo) and (not is_dist_verdict) and flow_positive_or_unknown and (
        action_verdict in ("STRONG ACCUMULATE", "ACCUMULATE ON DIP")
        or (total_buyers > total_sellers and (elite_buyers_count >= 1 or flow_val > 0.0 or new_count >= 2))
        or (elite_buyers_count > elite_sellers_count and total_buyers >= total_sellers and flow_val > 0.0)
    )

    pv_num = float(pct_vs_quarter_vwap) if pct_vs_quarter_vwap is not None else None
    near_q_vwap = pv_num is not None and (-20.0 <= pv_num <= 12.0)
    deep_drawdown = pv_num is not None and pv_num < -20.0
    extended_or_fomo = bool(is_fomo or (pv_num is not None and pv_num > 12.0))

    if not (has_form4_cluster or has_13d or (has_insider_buy and (has_13f_accum or has_13g))):
        return None

    # Tier classification
    if has_form4_cluster and (has_13f_accum or has_13d):
        tier = "DUAL_CONVICTION_PRIME" if (near_q_vwap and not is_fomo) else "DUAL_CONVICTION_EXTENDED"
    elif has_13d and (has_13f_accum or elite_holders_count >= 1 or has_insider_buy):
        tier = "ACTIVIST_13F_CONVERGENCE"
    elif has_insider_buy and (has_13f_accum or has_13d or has_13g):
        if extended_or_fomo or deep_drawdown:
            tier = "DUAL_CONVICTION_EXTENDED"
        else:
            tier = "INSIDER_CLUSTER_WATCH"
    elif has_form4_cluster:
        tier = "INSIDER_CLUSTER_WATCH"
    else:
        tier = "ACTIVIST_13D_CATALYST"

    # Sub-scores (sum up to 100)
    s_insider = 0.0
    if has_insider_buy:
        if insider_label == "CLUSTER_BUY":
            s_insider = (
                24.0
                + min(5.0, max(0, buyers_cnt - 2) * 2.5)
                + (3.0 if csuite_cnt >= 1 else 0.0)
                + (3.0 if insider_buy_val >= 1_000_000.0 else (1.5 if insider_buy_val >= 250_000.0 else 0.0))
                + (1.0 if non10b5_cnt > 0 else 0.0)
            )
            s_insider = min(36.0, s_insider)
        elif insider_label == "C_SUITE_BUY":
            s_insider = (
                19.0
                + (6.0 if insider_buy_val >= 1_000_000.0 else (3.0 if insider_buy_val >= 250_000.0 else 1.0))
                + (2.0 if non10b5_cnt > 0 else 0.0)
            )
            s_insider = min(28.0, s_insider)
        elif insider_label == "DIRECTOR_BUY":
            s_insider = min(16.0, 12.0 + (3.0 if insider_buy_val >= 250_000.0 else 1.0))
        else:
            s_insider = min(11.0, 8.0 + (2.0 if insider_buy_val >= 100_000.0 else 0.0))

    s_13d = 0.0
    if has_13d:
        max_pct = max((float(al.get("percentOfClass") or 0.0) for al in activist_alerts), default=0.0)
        cat_bonus = 0.0
        if activist_cat in ("Strategic Review / M&A", "Board / Governance"):
            cat_bonus = 4.0
        elif activist_cat in ("Capital Return / Value", "Management Engagement"):
            cat_bonus = 2.5
        pct_bonus = 3.0 if max_pct >= 9.0 else (1.5 if max_pct >= 5.0 else 0.0)
        s_13d = min(24.0, 17.0 + cat_bonus + pct_bonus)
    elif has_13g:
        s_13d = 7.0

    if actionability_score is not None:
        base_13f = min(18.0, max(0.0, (float(actionability_score) / 100.0) * 18.0))
    else:
        net_b = total_buyers - total_sellers
        base_13f = min(18.0, max(0.0, 8.0 + max(-6.0, min(8.0, net_b * 2.0)) + (2.0 if flow_val > 0 else 0.0)))
    elite_bonus = (
        min(6.0, elite_buyers_count * 2.0 + (2.0 if elite_holders_count >= 5 else 0.0))
        if has_13f_accum
        else min(3.0, elite_holders_count * 0.8)
    )
    s_13f = min(24.0, max(0.0, base_13f + elite_bonus - (8.0 if is_fomo else 0.0)))

    if pv_num is None:
        s_vwap = 7.0
    else:
        pv = pv_num
        if pv < -25.0:
            s_vwap = 8.0
        elif pv < -20.0:
            s_vwap = 11.0
        elif pv <= -5.0:
            s_vwap = 16.0
        elif pv <= 0.0:
            s_vwap = 14.5
        elif pv <= 12.0:
            s_vwap = 14.5 - (pv / 12.0) * 4.0
        elif pv <= 20.0:
            s_vwap = 10.5 - ((pv - 12.0) / 8.0) * 5.0
        else:
            s_vwap = max(1.5, 5.5 - ((pv - 20.0) / 15.0) * 4.0)

    conv_score = round(min(100.0, max(0.0, s_insider + s_13d + s_13f + s_vwap)), 1)

    reasons: list[str] = []
    if has_insider_buy:
        ins_kind = (
            f"{buyers_cnt} insiders"
            if buyers_cnt >= 2
            else (top_insiders[0] if top_insiders else ("C-Suite insider" if csuite_cnt >= 1 else "Insider"))
        )
        reasons.append(f"Form 4 open-market buy: {ins_kind} bought {_fmt_usd_brief(insider_buy_val)}")
    elif is_sell_heavy:
        reasons.append("Form 4 buy activity offset by heavier discretionary insider sales")

    if has_13d:
        pct_top = top_13d.get("percentOfClass") if top_13d else None
        pct_txt = f" ({float(pct_top):.1f}%)" if pct_top is not None else ""
        reasons.append(f"Schedule 13D{pct_txt} by {', '.join(activist_funds) or 'Activist'}: {activist_cat}")
    elif has_13g:
        reasons.append("Active ≥5% Schedule 13G stake alongside tracked filings")

    if has_13f_accum:
        reasons.append(
            f"13F accumulation: +{total_buyers} buyers / -{total_sellers} sellers"
            + (f" ({elite_buyers_count} Elite buyers)" if elite_buyers_count > 0 else "")
        )
    elif elite_holders_count > 0:
        reasons.append(f"Held by {elite_holders_count} Elite-50 13F fund(s)")

    if pv_num is not None:
        reasons.append(f"{pv_num:+.1f}% vs. Est. Q-VWAP")
    else:
        reasons.append("Est. Q-VWAP pricing pending")

    if tier == "DUAL_CONVICTION_PRIME":
        headline = "Rare Dual-Conviction: Form 4 insider buying + Elite 13F accumulation near Q-VWAP."
    elif tier == "ACTIVIST_13F_CONVERGENCE":
        headline = "Activist Schedule 13D catalyst aligned with Elite-50 institutional backing."
    elif tier == "DUAL_CONVICTION_EXTENDED":
        if deep_drawdown:
            headline = "Insider + 13F co-buying in a deep drawdown (>20% below Q-VWAP) — confirm stabilization before sizing."
        elif extended_or_fomo:
            headline = "Insider + 13F co-buying on an extended rally (>12% above Q-VWAP) — monitor pullbacks."
        else:
            headline = "Insider + 13F co-buying detected; verify Q-VWAP entry pricing."
    elif tier == "INSIDER_CLUSTER_WATCH":
        headline = (
            "Insider + 13F co-buying watch (non-cluster insider buy alongside 13F accumulation)."
            if (has_insider_buy and has_13f_accum)
            else "Open-market Form 4 insider cluster/C-suite buy on a 13F-tracked stock."
        )
    else:
        headline = "Live mid-quarter Schedule 13D activist campaign filed ahead of 13F reporting."

    sig_dates = [d for d in (latest_insider_date, activist_date) if d]
    latest_sig_date = max(sig_dates) if sig_dates else None

    return {
        "ticker": ticker,
        "name": name,
        "sector": sector,
        "subSector": sub_sector,
        "tier": tier,
        "convergenceScore": conv_score,
        "hasForm4Cluster": has_form4_cluster,
        "hasInsiderBuy": has_insider_buy,
        "has13DActivist": has_13d,
        "has13GStake": has_13g,
        "has13FAccumulation": has_13f_accum,
        "nearQuarterVwap": near_q_vwap,
        "priceUsd": price_usd,
        "estQuarterVwap": est_quarter_vwap,
        "pctVsQuarterVwap": pct_vs_quarter_vwap,
        "actionabilityScore": actionability_score,
        "actionVerdict": action_verdict,
        "eliteBuyersCount": elite_buyers_count,
        "eliteHoldersCount": elite_holders_count,
        "netEstFlowUsd": round(flow_val, 2) if net_est_flow_usd is not None else None,
        "insiderBuyValueUsd": round(insider_buy_val, 2) if insider_buy_val > 0 else 0.0,
        "insiderBuyersCount": buyers_cnt,
        "cSuiteBuyersCount": csuite_cnt,
        "insiderSignalLabel": insider_label,
        "topInsiders": top_insiders,
        "latestInsiderDate": latest_insider_date,
        "activistFunds": activist_funds,
        "activistCategory": activist_cat,
        "activistIntent": activist_intent,
        "activistFilingDate": activist_date,
        "latestSignalDate": latest_sig_date,
        "reasons": reasons,
        "headline": headline,
    }



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
        nav_lag = simulate_nav_series(lag_segments, aligned_prices, n_dates, tx_cost_bps=DEFAULT_LAG_TX_COST_BPS)
        fund_navs["quarter_end"][cik] = nav_qe
        fund_navs["filing_lag"][cik] = nav_lag

        # Compute quarter-by-quarter returns & Batting Average vs QQQ (independently for QE & 45d-Lag)
        qe_nans = np.where(np.isnan(nav_qe))[0]
        qe_contig_start = int(qe_nans[-1]) + 1 if len(qe_nans) > 0 else 0
        q_beats = 0
        q_total = 0
        for i, qrow in enumerate(quarterly_hist):
            s_i = qrow.pop("qeIdx")
            e_i = find_idx_on_or_after(cal_dates, quarterly_hist[i + 1]["periodEnd"]) if i + 1 < len(quarterly_hist) else (n_dates - 1)
            if e_i is None:
                e_i = n_dates - 1
            if (
                e_i > s_i
                and s_i >= qe_contig_start
                and not math.isnan(nav_qe[s_i])
                and not math.isnan(nav_qe[e_i])
                and nav_qe[s_i] > 0
                and not np.any(np.isnan(nav_qe[s_i : e_i + 1]))
            ):
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

        lag_nans = np.where(np.isnan(nav_lag))[0]
        lag_contig_start = int(lag_nans[-1]) + 1 if len(lag_nans) > 0 else 0
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
                if (
                    prev_b >= lag_contig_start
                    and not math.isnan(nav_lag[prev_b])
                    and not math.isnan(nav_lag[nxt_b])
                    and nav_lag[prev_b] > 0
                    and not np.any(np.isnan(nav_lag[prev_b : nxt_b + 1]))
                ):
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
                conf = min(1.0, valid_3y_by_mode[mode].get(c, 0) / SKILL_SHRINKAGE_DAYS)
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

    # Count full Elite-50 exits per ticker from latest quarterly fund updates
    elite_exited_by_ticker: dict[str, int] = {}
    for u in fund_updates_out:
        if u["cik"] not in elite50_set:
            continue
        seen_ex_u: set[str] = set()
        for mv in u.get("allMoves") or []:
            if mv.get("securityType") != "EQUITY" or mv.get("action") != "EXITED":
                continue
            raw_et = str(mv.get("ticker") or "")
            if not raw_et or raw_et.startswith("CUSIP:"):
                continue
            ct = DUAL_CLASS_CANONICAL.get(raw_et, raw_et)
            if ct not in excluded_etf_tickers and ct not in seen_ex_u:
                seen_ex_u.add(ct)
                elite_exited_by_ticker[ct] = elite_exited_by_ticker.get(ct, 0) + 1

    latest_pe_mode = updates_overview.get("latestQuarterPeriodEnd") or max((f["latestPeriod"] for f in funds_out), default="2026-09-30")

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
        exited_by_ticker=elite_exited_by_ticker,
        latest_quarter_end=latest_pe_mode,
        subsector_cap_pct=IDEAL_MAX_SUBSECTOR_CAP_PCT,
    )

    # Survivorship-free Point-in-Time Quarterly Backtests (QE vs 45-Day Lag)
    ticker_is_tech: dict[str, bool] = {}
    ticker_sub_sector: dict[str, str] = {}
    for m_val in cusip_meta.values():
        tk = m_val.get("ticker", "")
        if tk and not tk.startswith("CUSIP:"):
            ctk = DUAL_CLASS_CANONICAL.get(tk, tk)
            if m_val.get("isTechTmt"):
                ticker_is_tech[ctk] = True
            if ctk not in ticker_sub_sector or m_val.get("industry"):
                ticker_sub_sector[ctk] = classify_sub_sector(
                    m_val.get("sector") or "Other",
                    m_val.get("industry") or "",
                    bool(m_val.get("isTechTmt")),
                )

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
        ticker_sub_sector=ticker_sub_sector,
        subsector_cap=IDEAL_MAX_SUBSECTOR_CAP_PCT / 100.0,
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
        ticker_sub_sector=ticker_sub_sector,
        subsector_cap=IDEAL_MAX_SUBSECTOR_CAP_PCT / 100.0,
    )
    ideal_nav_qe = simulate_nav_series(bt_segments_qe, aligned_prices, n_dates)
    ideal_nav_lag = simulate_nav_series(bt_segments_lag, aligned_prices, n_dates, tx_cost_bps=DEFAULT_LAG_TX_COST_BPS)
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
            if not _is_valid_dossier_ticker(raw_t, h["cusip"], cusip_meta, aligned_prices):
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
                "eliteBuyersCount": 0,
                "eliteSellersCount": 0,
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
        is_elite_u = f_tier == "Elite 50"
        per_u_stock: dict[str, dict[str, Any]] = {}
        for mv in u["allMoves"]:
            if mv["securityType"] != "EQUITY":
                continue
            raw_t = mv["ticker"]
            if not _is_valid_dossier_ticker(raw_t, mv["cusip"], cusip_meta, aligned_prices):
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
                "eliteBuyersCount": 0,
                "eliteSellersCount": 0,
                "newCount": 0,
                "addedCount": 0,
                "trimmedCount": 0,
                "exitedCount": 0,
            })
            rec["netEstFlowUsd"] += s["estFlow"]
            if act == "NEW":
                rec["newCount"] += 1
                if is_elite_u:
                    rec["eliteBuyersCount"] += 1
            elif act == "ADDED":
                rec["addedCount"] += 1
                if is_elite_u:
                    rec["eliteBuyersCount"] += 1
            elif act == "TRIMMED":
                rec["trimmedCount"] += 1
                if is_elite_u:
                    rec["eliteSellersCount"] += 1
            elif act == "EXITED":
                rec["exitedCount"] += 1
                if is_elite_u:
                    rec["eliteSellersCount"] += 1

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

    form4_cik_map = dict(ticker_to_cik)
    for al in midquarter_alerts:
        al_t = DUAL_CLASS_CANONICAL.get(al.get("ticker", ""), al.get("ticker", ""))
        al_scik = str(al.get("subjectCik") or "")
        if al_t and al_scik.isdigit() and len(al_scik) == 10 and al_t not in form4_cik_map:
            form4_cik_map[al_t] = al_scik

    form4_candidates_sorted = sorted(
        [
            t
            for t, r in all_dossier_builders.items()
            if (
                r["eliteHoldersCount"] >= 1
                or r["eliteBuyersCount"] >= 1
                or (r["newCount"] + r["addedCount"]) >= 2
                or len(r["holders"]) >= 2
            )
        ],
        key=lambda t: (
            all_dossier_builders[t]["eliteHoldersCount"] >= 1
            and all_dossier_builders[t]["eliteBuyersCount"] >= 1,
            (all_dossier_builders[t]["newCount"] + all_dossier_builders[t]["addedCount"])
            > (all_dossier_builders[t]["trimmedCount"] + all_dossier_builders[t]["exitedCount"]),
            all_dossier_builders[t]["eliteBuyersCount"],
            (all_dossier_builders[t]["newCount"] + all_dossier_builders[t]["addedCount"])
            - (all_dossier_builders[t]["trimmedCount"] + all_dossier_builders[t]["exitedCount"]),
            all_dossier_builders[t]["eliteHoldersCount"],
            all_dossier_builders[t]["totalValueUsd"],
        ),
        reverse=True,
    )

    form4_priority_set: set[str] = set()
    for t_cand in (xbrl_priority_set | set(alerts_by_ticker.keys()) | set(form4_candidates_sorted[:750])):
        canon_cand = DUAL_CLASS_CANONICAL.get(t_cand, t_cand)
        if (
            canon_cand
            and not canon_cand.startswith("CUSIP:")
            and canon_cand not in excluded_etf_tickers
            and canon_cand in form4_cik_map
        ):
            form4_priority_set.add(canon_cand)

    ref_prices_by_ticker: dict[str, float] = {}
    for tk_p, (arr_p, _, li_p) in aligned_prices.items():
        if li_p >= 0 and not math.isnan(arr_p[li_p]) and arr_p[li_p] > 0:
            ref_prices_by_ticker[tk_p] = float(arr_p[li_p])

    form4_map = fetch_form4_insider_signals(
        sorted(form4_priority_set),
        form4_cik_map,
        today_date=today_date,
        lookback_days=90,
        ref_prices_by_ticker=ref_prices_by_ticker,
    )

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
        if nq_mc and 0.0 < nq_mc <= 50e12 and nq_mc >= total_held * 0.75:
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

    elite_holding_periods = compute_elite_holding_periods(
        elite_ciks=elite50_ciks,
        raw_fund_filings=raw_fund_filings,
        cusip_meta=cusip_meta,
        excluded_etfs=excluded_etf_tickers,
    )
    stock_filing_anchors = compute_stock_filing_date_anchors(
        raw_fund_filings=raw_fund_filings,
        cusip_meta=cusip_meta,
        excluded_etfs=excluded_etf_tickers,
        latest_pe_mode=latest_pe_mode,
        elite_ciks=elite50_set,
        lb_ciks=lb_ciks,
    )
    thesis_persistence_map = compute_stock_thesis_persistence(
        raw_fund_filings=raw_fund_filings,
        cusip_meta=cusip_meta,
        excluded_etfs=excluded_etf_tickers,
        splits_map=splits_map,
        eligible_ciks=lb_ciks,
        elite_ciks=elite50_set,
        latest_pe_mode=latest_pe_mode,
    )
    fund_skill_by_cik = {
        f["cik"]: f["metrics"]["filing_lag"]["skillScore"]
        for f in funds_out
    }

    dual_conviction_signals: list[dict[str, Any]] = []

    for t in all_dossier_tickers:
        rec_b = all_dossier_builders[t]
        enr = enrichment_by_ticker[t]
        enr.update(compute_alpha_decay_metrics(
            ticker=t,
            aligned_prices=aligned_prices,
            cal_dates=cal_dates,
            price_usd=enr["priceUsd"],
            est_quarter_vwap=enr["estQuarterVwap"],
            pct_vs_quarter_vwap=enr["pctVsQuarterVwap"],
            latest_pe_mode=latest_pe_mode,
            consensus_filing_date=stock_filing_anchors.get(t),
            avg_elite_holding_quarters=elite_holding_periods.get(t),
            new_count=rec_b["newCount"],
            added_count=rec_b["addedCount"],
            trimmed_count=rec_b["trimmedCount"],
            exited_count=rec_b["exitedCount"],
            elite_buyers_count=rec_b["eliteBuyersCount"],
            elite_sellers_count=rec_b["eliteSellersCount"],
            elite_holders_count=rec_b["eliteHoldersCount"],
            net_est_flow_usd=rec_b["netEstFlowUsd"],
        ))
        act_m = compute_actionability_matrix(
            new_count=rec_b["newCount"],
            added_count=rec_b["addedCount"],
            trimmed_count=rec_b["trimmedCount"],
            exited_count=rec_b["exitedCount"],
            elite_buyers_count=rec_b["eliteBuyersCount"],
            elite_sellers_count=rec_b["eliteSellersCount"],
            elite_holders_count=rec_b["eliteHoldersCount"],
            total_value_usd=rec_b["totalValueUsd"],
            net_est_flow_usd=rec_b["netEstFlowUsd"],
            movers=rec_b["recentMoves"],
            holders=rec_b["holders"],
            fund_skill_by_cik=fund_skill_by_cik,
            pct_vs_quarter_vwap=enr.get("pctVsQuarterVwap"),
            filing_to_today_pct=enr.get("filingToTodayPct"),
            alpha_decay_status=enr.get("alphaDecayStatus"),
            fundamentals=enr.get("fundamentals"),
            avg_elite_holding_quarters=enr.get("avgEliteHoldingQuarters"),
            thesis_persistence=thesis_persistence_map.get(t),
            market_cap_usd=enr.get("marketCapUsd"),
        )
        enr["actionability"] = act_m
        enr["actionabilityScore"] = act_m["score"]
        enr["actionVerdict"] = act_m["verdict"]

        f4_t = form4_map.get(t)
        if f4_t and f4_t.get("avgBuyPriceUsd") and f4_t["avgBuyPriceUsd"] > 0 and enr.get("estQuarterVwap") and enr["estQuarterVwap"] > 0:
            f4_t["buyPriceVsQuarterVwapPct"] = round(
                ((float(f4_t["avgBuyPriceUsd"]) / float(enr["estQuarterVwap"])) - 1.0) * 100.0,
                1,
            )

        conv_sig = compute_dual_conviction_signal(
            ticker=t,
            name=rec_b["name"],
            sector=rec_b["sector"] or "Other",
            sub_sector=classify_sub_sector(rec_b["sector"] or "Other", rec_b["industry"], rec_b["isTechTmt"]),
            form4_summary=f4_t,
            alerts=alerts_by_ticker.get(t, []),
            price_usd=enr.get("priceUsd"),
            est_quarter_vwap=enr.get("estQuarterVwap"),
            pct_vs_quarter_vwap=enr.get("pctVsQuarterVwap"),
            alpha_decay_status=enr.get("alphaDecayStatus"),
            actionability_score=act_m["score"],
            action_verdict=act_m["verdict"],
            new_count=rec_b["newCount"],
            added_count=rec_b["addedCount"],
            trimmed_count=rec_b["trimmedCount"],
            exited_count=rec_b["exitedCount"],
            elite_buyers_count=rec_b["eliteBuyersCount"],
            elite_sellers_count=rec_b["eliteSellersCount"],
            elite_holders_count=rec_b["eliteHoldersCount"],
            net_est_flow_usd=rec_b["netEstFlowUsd"],
        )
        enr["insiderActivity"] = f4_t
        enr["insiderSignalLabel"] = f4_t.get("insiderSignalLabel") if f4_t else None
        enr["convergenceSignal"] = conv_sig
        enr["convergenceScore"] = conv_sig["convergenceScore"] if conv_sig else None
        enr["convergenceTier"] = conv_sig["tier"] if conv_sig else None
        if conv_sig is not None:
            dual_conviction_signals.append(conv_sig)

    # Also capture standalone 13D / Form 4 convergence candidates outside current 13F dossier keys
    extra_conv_tickers = sorted((set(alerts_by_ticker.keys()) | set(form4_map.keys())) - set(all_dossier_tickers))
    for et in extra_conv_tickers:
        als_et = alerts_by_ticker.get(et, [])
        f4_et = form4_map.get(et)
        px_et = None
        if et in aligned_prices:
            arr_e, _, li_e = aligned_prices[et]
            px_et = clean_num(float(arr_e[li_e]), 2)
        name_et = (als_et[0].get("issuerName") if als_et else None) or et
        conv_et = compute_dual_conviction_signal(
            ticker=et,
            name=name_et,
            form4_summary=f4_et,
            alerts=als_et,
            price_usd=px_et,
        )
        if conv_et is not None:
            dual_conviction_signals.append(conv_et)

    dual_conviction_signals.sort(
        key=lambda c: (
            _CONVERGENCE_TIER_ORDER.get(c["tier"], 0),
            float(c.get("convergenceScore") or 0.0),
            float(c.get("insiderBuyValueUsd") or 0.0),
            str(c.get("latestSignalDate") or ""),
        ),
        reverse=True,
    )

    form4_summary_out: dict[str, dict[str, Any]] = {}
    for tk, f4 in form4_map.items():
        if f4.get("openMarketBuyCount", 0) > 0 or f4.get("discretionarySellValueUsd", 0) > 0:
            trimmed_f4 = dict(f4)
            trimmed_f4["recentTransactions"] = (f4.get("recentTransactions") or [])[:6]
            if "recentSellTransactions" in trimmed_f4:
                trimmed_f4["recentSellTransactions"] = (f4.get("recentSellTransactions") or [])[:6]
            form4_summary_out[tk] = trimmed_f4

    _ALPHA_DECAY_KEYS = (
        "consensusFilingDate",
        "filingDatePriceUsd",
        "qVwapToFilingPct",
        "filingToTodayPct",
        "avgEliteHoldingQuarters",
        "eliteBuyersCount",
        "eliteSellersCount",
        "alphaDecayStatus",
        "alphaDecayReason",
        "actionability",
        "actionabilityScore",
        "actionVerdict",
    )

    # Final Actionability-aligned Ideal Portfolio pass: ensures 0% EXIT SIGNAL weight,
    # damps DISTRIBUTION WARNING, incorporates full EXITED moves, vol-sizing, and 30% sub-sector cap.
    actionability_by_ticker = {
        t_k: enr_v["actionability"]
        for t_k, enr_v in enrichment_by_ticker.items()
        if enr_v.get("actionability")
    }
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
        exited_by_ticker=elite_exited_by_ticker,
        actionability_by_ticker=actionability_by_ticker,
        latest_quarter_end=latest_pe_mode,
        subsector_cap_pct=IDEAL_MAX_SUBSECTOR_CAP_PCT,
    )

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
        net_ex = int(rec.get("netExit", 0))

        # Fresh Inflection score emphasizes net new initiations + share adds over trims & full exits
        buy_impulse = (1.5 * rec["netNew"] + 1.0 * rec["netAdd"] - 1.0 * rec["netTrim"] - 1.5 * net_ex) / max(1, cnt_b + net_ex)
        inflection_score = round(raw_sc * (1.0 + 0.65 * max(-0.75, min(1.5, buy_impulse))), 4)

        if cnt_b >= core_thresh:
            tag = "Core Consensus"
        elif rec["netBuy"] > (rec["netTrim"] + net_ex) and rec["netBuy"] >= max(2, cnt_b // 2):
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
            "netExit": net_ex,
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
            **{ak: enr.get(ak) for ak in _ALPHA_DECAY_KEYS},
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
        "maxSubSectorCapPct": IDEAL_MAX_SUBSECTOR_CAP_PCT,
        "rankedFundsCount": len(ideal_pool_funds),
        "effectivePositions": round(ideal_eff_n, 1),
        "top10WeightPct": round(sum(p["idealWeightPct"] for p in ideal_positions[:10]), 2),
        "methodologySummary": (
            f"Constructed from active US equity holdings across the Elite {len(ideal_pool_funds)} Skill-Ranked managers "
            f"(50/50 Orthogonal Multi-Horizon Return & Risk-Adjusted Skill). Positions are scored by conviction^1.5 × "
            f"√(weight) × full QoQ accumulation/exit impulse × inverse-√(63d vol) sizing × Actionability guard "
            f"(0% EXIT SIGNAL), with 8% single-stock and {int(IDEAL_MAX_SUBSECTOR_CAP_PCT)}% sub-sector caps."
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

    # Enrich Top 30 Consensus Holdings & Signal Screener with Q-VWAP + XBRL + Alpha-Decay + Actionability
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
                for ak in _ALPHA_DECAY_KEYS:
                    h[ak] = enr.get(ak)

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
                for ak in _ALPHA_DECAY_KEYS:
                    item[ak] = enr.get(ak)

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
        tp_t = thesis_persistence_map.get(t)
        f4_t = form4_map.get(t)
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
            "consensusFilingDate": enr.get("consensusFilingDate"),
            "filingDatePriceUsd": enr.get("filingDatePriceUsd"),
            "qVwapToFilingPct": enr.get("qVwapToFilingPct"),
            "filingToTodayPct": enr.get("filingToTodayPct"),
            "avgEliteHoldingQuarters": enr.get("avgEliteHoldingQuarters"),
            "eliteBuyersCount": enr.get("eliteBuyersCount", rec["eliteBuyersCount"]),
            "eliteSellersCount": enr.get("eliteSellersCount", rec["eliteSellersCount"]),
            "alphaDecayStatus": enr.get("alphaDecayStatus"),
            "actionabilityScore": enr.get("actionabilityScore"),
            "actionVerdict": enr.get("actionVerdict"),
            "consecutiveNetBuyQuarters": tp_t["consecutiveNetBuyQuarters"] if tp_t else None,
            "consecutiveNetSellQuarters": tp_t["consecutiveNetSellQuarters"] if tp_t else None,
            "insiderSignalLabel": enr.get("insiderSignalLabel"),
            "insiderBuyValueUsd": f4_t["totalBuyValueUsd"] if f4_t and f4_t["totalBuyValueUsd"] > 0 else None,
            "insiderBuyersCount": f4_t["distinctBuyersCount"] if f4_t and f4_t["distinctBuyersCount"] > 0 else None,
            "convergenceScore": enr.get("convergenceScore"),
            "convergenceTier": enr.get("convergenceTier"),
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
            "insiderActivity": enr.get("insiderActivity"),
            "convergenceSignal": enr.get("convergenceSignal"),
            **{ak: enr.get(ak) for ak in _ALPHA_DECAY_KEYS},
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

    form4_cluster_cnt = sum(1 for f4 in form4_map.values() if f4.get("clusterDetected"))
    updates_overview["dualConvictionCount"] = len(dual_conviction_signals)
    updates_overview["form4ClusterCount"] = form4_cluster_cnt

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
        "form4ClusterBuysCount": form4_cluster_cnt,
        "dualConvictionCount": len(dual_conviction_signals),
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
                "SEC EDGAR Form 4 / 4/A Open-Market ('P') Insider Filings & Schedule 13D/13G Item 4 Submissions",
                "SEC EDGAR XBRL CompanyFacts & SEC Submissions Ownership Metadata",
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
        "dualConvictionSignals": dual_conviction_signals[:40],
        "form4Signals": form4_summary_out,
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

    manifest_info, total_bytes = emit_data_artifacts(
        PUBLIC_DATA_DIR,
        summary_payload=summary_payload,
        fund_shards=fund_shards,
        stock_shards=stock_shards,
        summary_json_preencoded=summary_json,
    )

    if LEGACY_MONOLITH_JSON.exists():
        LEGACY_MONOLITH_JSON.unlink()
        print(f"[cleanup] Removed legacy monolith {LEGACY_MONOLITH_JSON}", flush=True)

    summary_kb = (PUBLIC_DATA_DIR / "summary.json").stat().st_size / 1024.0
    bootstrap_kb = (PUBLIC_DATA_DIR / "bootstrap.json").stat().st_size / 1024.0
    print("\n=== Pipeline Complete ===")
    print(
        f"Wrote bootstrap.json ({bootstrap_kb:.1f} KB), summary.json ({summary_kb:.1f} KB), "
        f"split feeds + manifest.json ({len(manifest_info['files'])} files, total {total_bytes / 1024.0:.1f} KB)"
    )
    print(
        f"Universe Funnel: {funnel_counts['totalCiksScanned']} CIKs -> {funnel_counts['activeRecentCount']} active -> "
        f"{funnel_counts['nonExcludedManagerCount']} focused managers -> {funnel_counts['concentrationPassCount']} concentrated -> "
        f"{funnel_counts['techFloorPassCount']} tech floor -> {funnel_counts['strictQualificationCount']} strict -> "
        f"{funnel_counts['qualifiedFundsCount']} verified ({len(lb_funds)} Leaderboard Eligible, {len(elite50_ciks)} Elite 50)"
    )
    print(
        f"Indexed Stocks: {len(stocks_index)} | Mid-Quarter 13D/13G Alerts: {len(midquarter_alerts)} | "
        f"Form 4 Active: {len(form4_summary_out)} ({form4_cluster_cnt} clusters) | "
        f"Dual-Conviction Signals: {len(dual_conviction_signals)} | Fund Updates: {len(fund_updates_out)}"
    )
    print(
        f"Ideal Portfolio: {len(ideal_positions)} names (+{len(ideal_candidate_pool)} pool) | "
        f"TTM QE Return: {ideal_rets_qe['TTM']}% | TTM 45d-Lag Return: {ideal_rets_lag['TTM']}% | "
        f"QQQ TTM: {benchmarks['QQQ']['returns']['TTM']}%"
    )


def emit_data_artifacts(
    out_dir: Path,
    *,
    summary_payload: dict[str, Any],
    fund_shards: dict[str, dict[str, Any]],
    stock_shards: dict[int, dict[str, dict[str, Any]]],
    summary_json_preencoded: str | None = None,
) -> tuple[dict[str, Any], int]:
    """
    Write backwards-compatible summary.json, tab-split SPA payloads (bootstrap.json,
    screener.json, updates_feed.json, stocks_search.json), 16 fund shards, 8 stock shards,
    and a content-hashed manifest.json (written last) atomically via .tmp files.
    """
    out_dir.mkdir(parents=True, exist_ok=True)

    summary_json = (
        summary_json_preencoded
        if summary_json_preencoded is not None
        else json.dumps(summary_payload, allow_nan=False, separators=(",", ":"))
    )
    bootstrap_payload = {
        k: summary_payload[k]
        for k in (
            "metadata",
            "benchmarks",
            "funds",
            "rankings",
            "rankedUnionCiks",
            "topHoldings",
            "idealPortfolio",
            "updatesOverview",
            "midQuarterAlerts",
            "dualConvictionSignals",
            "form4Signals",
        )
        if k in summary_payload
    }
    screener_payload = {"signalScreener": summary_payload.get("signalScreener", {})}
    updates_feed_payload = {
        "fundUpdates": summary_payload.get("fundUpdates", []),
        "updatesOverview": summary_payload.get("updatesOverview", {}),
    }
    stocks_search_payload = {"stocksIndex": summary_payload.get("stocksIndex", {})}

    staged_files: list[tuple[str, str]] = [
        ("summary.json", summary_json),
        ("bootstrap.json", json.dumps(bootstrap_payload, allow_nan=False, separators=(",", ":"))),
        ("screener.json", json.dumps(screener_payload, allow_nan=False, separators=(",", ":"))),
        ("updates_feed.json", json.dumps(updates_feed_payload, allow_nan=False, separators=(",", ":"))),
        ("stocks_search.json", json.dumps(stocks_search_payload, allow_nan=False, separators=(",", ":"))),
    ]
    for sid, payload in sorted(fund_shards.items()):
        staged_files.append((f"funds_{sid}.json", json.dumps(payload, allow_nan=False, separators=(",", ":"))))
    for sh_id, payload in sorted(stock_shards.items()):
        staged_files.append((f"stocks_{sh_id}.json", json.dumps(payload, allow_nan=False, separators=(",", ":"))))

    file_hashes: dict[str, str] = {}
    total_bytes = 0
    for fname, content in staged_files:
        raw_bytes = content.encode("utf-8")
        file_hashes[fname] = hashlib.sha256(raw_bytes).hexdigest()[:12]
        target_p = out_dir / fname
        tmp_p = target_p.with_suffix(".json.tmp")
        tmp_p.write_bytes(raw_bytes)
        tmp_p.replace(target_p)
        total_bytes += len(raw_bytes)

    meta = summary_payload.get("metadata") or {}
    manifest_payload: dict[str, Any] = {
        "schemaVersion": 2,
        "generatedAt": meta.get("generatedAt", ""),
        "asOfDate": meta.get("asOfDate", ""),
        "files": file_hashes,
    }
    manifest_bytes = json.dumps(manifest_payload, allow_nan=False, separators=(",", ":")).encode("utf-8")
    manifest_p = out_dir / "manifest.json"
    manifest_tmp = manifest_p.with_suffix(".json.tmp")
    manifest_tmp.write_bytes(manifest_bytes)
    manifest_tmp.replace(manifest_p)
    total_bytes += len(manifest_bytes)
    return manifest_payload, total_bytes


if __name__ == "__main__":
    main()

