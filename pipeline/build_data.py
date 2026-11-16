"""
End-to-end TechWhale 13F data pipeline:
1. Fetches 2021-present SEC EDGAR 13F-HR / 13F-HR/A filings for curated + discovered funds
2. Resolves CUSIPs -> tickers + GICS sectors + daily adjusted-close prices
3. Qualifies genuinely Tech/TMT-focused concentrated portfolios
4. Reconstructs daily buy-and-hold equity curves for both Quarter-End and 45-Day Lag modes
5. Computes rankings across YTD, TTM, 18M, 2Y, 3Y, 5Y windows
6. Builds Top 30 Consensus Holdings, Ideal Portfolio (<= 50 names), and NPORT-P ETF Correlations
7. Writes src/data/portfolio_data.json for static frontend & GitHub Pages deployment
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np

from funds_config import CURATED_FUNDS, NON_STOCK_ETFS, TARGET_ETFS
from sec_client import (
    build_clean_quarterly_filings,
    discover_emerging_tech_funds,
    fetch_etf_nport_holdings,
    list_13f_filings,
)
from security_resolver import (
    fetch_all_prices,
    normalize_yf_ticker,
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
    for i in range(first_i, last_i + 1):
        if i == first_i or i == last_i or (i % step == 0):
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


def main() -> None:
    print("=== TechWhale 13F Pipeline Starting ===")
    known_ciks = {f["cik"] for f in CURATED_FUNDS}
    print(f"[universe] Curated funds: {len(CURATED_FUNDS)}. Scanning EDGAR for emerging tech funds...")
    discovered = discover_emerging_tech_funds(known_ciks, max_candidates=10)
    print(f"[universe] Discovered {len(discovered)} candidate funds via SEC EDGAR EFTS.")

    all_candidates: list[dict[str, Any]] = []
    for f in CURATED_FUNDS:
        item = dict(f)
        item["source"] = "Curated Core"
        all_candidates.append(item)
    all_candidates.extend(discovered)

    # Step 1: Fetch 13F filings (2020-12-31 onward for full 5Y coverage)
    raw_fund_filings: dict[str, list[dict[str, Any]]] = {}
    all_cusips: dict[str, str] = {}

    today_date = datetime.now(timezone.utc).date()
    for cand in all_candidates:
        cik = cand["cik"]
        try:
            raw_list = list_13f_filings(cik, min_report_date="2020-12-31")
            filings = build_clean_quarterly_filings(cik, min_report_date="2020-12-31")
            if not filings:
                print(f"  [skip] {cand['name']} ({cik}): no valid 13F holdings found")
                continue
            latest_rd = filings[-1]["reportDate"]
            latest_raw_rd = raw_list[-1]["reportDate"] if raw_list else latest_rd
            if latest_raw_rd > latest_rd and (
                datetime.strptime(latest_raw_rd, "%Y-%m-%d").date() - datetime.strptime(latest_rd, "%Y-%m-%d").date()
            ).days > 100:
                print(f"  [skip] {cand['name']} ({cik}): latest filing {latest_raw_rd} reported $0/no holdings (last populated {latest_rd})")
                continue
            age_days = (today_date - datetime.strptime(latest_rd, "%Y-%m-%d").date()).days
            if age_days > 210:
                print(f"  [skip] {cand['name']} ({cik}): stale latest period {latest_rd} ({age_days}d old)")
                continue
            raw_fund_filings[cik] = filings
            for idx_fl, fl in enumerate(filings):
                is_latest = idx_fl == len(filings) - 1
                for row in fl["rows"]:
                    if row["secType"] == "EQUITY" or is_latest:
                        if row["cusip"] not in all_cusips:
                            all_cusips[row["cusip"]] = row["issuer"]
            print(f"  [ok]   {cand['name']:28s} ({cik}): {len(filings):2d} quarters, latest={latest_rd} (filed {filings[-1]['filingDate']})")
        except Exception as e:
            print(f"  [err]  {cand['name']} ({cik}): {e}")

    # Step 2: Fetch ETF NPORT-P holdings from SEC EDGAR
    etf_raw_list: list[dict[str, Any]] = []
    for etf_cfg in TARGET_ETFS:
        try:
            res = fetch_etf_nport_holdings(etf_cfg["ticker"], etf_cfg["cik"], etf_cfg["seriesId"])
            if res:
                res["name"] = etf_cfg["name"]
                res["category"] = etf_cfg["category"]
                etf_raw_list.append(res)
                for h in res["holdings"]:
                    if h["cusip"] not in all_cusips:
                        all_cusips[h["cusip"]] = h["name"]
                print(f"  [etf]  {etf_cfg['ticker']:6s} NPORT-P rd={res['reportDate']}, holdings={len(res['holdings'])}")
            else:
                print(f"  [etf-skip] {etf_cfg['ticker']:6s}: NPORT-P not matched")
        except Exception as e:
            print(f"  [etf-err]  {etf_cfg['ticker']:6s}: {e}")

    # Step 3: Resolve all CUSIPs -> Tickers, Sectors, Industries
    cusip_meta = resolve_cusips(all_cusips)

    # Step 4: Evaluate Tech-Focused & Concentrated qualification on each fund's latest filing
    qualified_cands: list[dict[str, Any]] = []
    needed_tickers: set[str] = {"QQQ", "SPY", "XLK"}
    for etf in etf_raw_list:
        needed_tickers.add(etf["ticker"])

    print("\n=== Fund Qualification Audit (Tech/TMT >= 45% & Concentrated) ===")
    for cand in all_candidates:
        cik = cand["cik"]
        if cik not in raw_fund_filings:
            continue
        filings = raw_fund_filings[cik]
        latest = filings[-1]
        weights, eq_aum, rep_aum, pos_cnt, top10_pct, tech_pct = extract_equity_weights(latest, cusip_meta)

        is_tech = tech_pct >= 45.0
        is_conc = (top10_pct >= 45.0 or pos_cnt <= 75) and pos_cnt >= 7
        is_size = eq_aum >= 100_000_000
        # Require at least 4 quarters of filings so TTM works
        has_hist = len(filings) >= 4

        status_str = "PASS" if (is_tech and is_conc and is_size and has_hist) else "FILTERED"
        print(
            f"  {status_str:8s} | {cand['name'][:26]:26s} | AUM=${eq_aum/1e9:6.2f}B | "
            f"pos={pos_cnt:3d} | top10={top10_pct:5.1f}% | techTMT={tech_pct:5.1f}% | qtrs={len(filings):2d}"
        )
        if status_str == "PASS":
            qualified_cands.append(cand)
            for fl in filings:
                for row in fl["rows"]:
                    if row["secType"] == "EQUITY":
                        t = cusip_meta.get(row["cusip"], {}).get("ticker", "")
                        if t and not t.startswith("CUSIP:"):
                            needed_tickers.add(t)

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
        fund_equity_weights[cik] = lat_weights

        # Previous quarter shares/weights for QoQ change
        prev_eq_shares: dict[str, float] = {}
        prev_eq_weights: dict[str, float] = {}
        if prev:
            p_weights, _, _, _, _, _ = extract_equity_weights(prev, cusip_meta)
            prev_eq_weights = p_weights
            for r in prev["rows"]:
                if r["secType"] == "EQUITY":
                    t = cusip_meta.get(r["cusip"], {}).get("ticker") or f"CUSIP:{r['cusip']}"
                    prev_eq_shares[t] = prev_eq_shares.get(t, 0.0) + r["shares"]

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
            if is_eq and prev:
                psh = prev_eq_shares.get(rt, 0.0)
                pw = prev_eq_weights.get(rt, 0.0) * 100.0
                if psh <= 0:
                    status = "NEW"
                else:
                    diff_pct = ((item["shares"] / psh) - 1.0) * 100.0
                    qoq_sh_pct = clean_num(diff_pct, 1)
                    if diff_pct > 2.0:
                        status = "ADDED"
                    elif diff_pct < -2.0:
                        status = "TRIMMED"
                qoq_w_delta = clean_num(w_eq - pw, 2)

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

    # Step 7: Build Top 10 Rankings per mode & window
    rankings: dict[str, dict[str, list[dict[str, Any]]]] = {"quarter_end": {}, "filing_lag": {}}
    ranked_union_ciks: dict[str, list[str]] = {"quarter_end": [], "filing_lag": []}
    fund_top10_windows: dict[str, dict[str, list[str]]] = {"quarter_end": {}, "filing_lag": {}}

    for mode in MODES:
        union_set: list[str] = []
        for w in WINDOWS:
            eligible = [
                (f["cik"], f["returns"][mode][w])
                for f in funds_out
                if f["returns"][mode][w] is not None
            ]
            eligible.sort(key=lambda x: x[1], reverse=True)
            top10 = eligible[:10]
            rankings[mode][w] = [
                {"rank": idx + 1, "cik": cik, "returnPct": round(ret, 2)}
                for idx, (cik, ret) in enumerate(top10)
            ]
            for cik, _ in top10:
                if cik not in union_set:
                    union_set.append(cik)
                fund_top10_windows[mode].setdefault(cik, []).append(w)
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
                    "top10Windows": fund_top10_windows[mode].get(cik, []),
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

        if len(backers_sorted) >= 8:
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
        top_slice = scored_funds[:12]

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
            f"Constructed from all holdings of the {len(ideal_pool_funds)} funds ranked Top 10 across at least one window "
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

    # Step 10: Compute ETF Holdings Similarity & Return Correlations
    etfs_out: list[dict[str, Any]] = []
    corr_start = max(0, n_dates - 253)

    def _cosine_and_overlap(w_a: dict[str, float], w_b: dict[str, float]) -> tuple[float, float, list[dict[str, Any]]]:
        norm_a = math.sqrt(sum(v * v for v in w_a.values()))
        norm_b = math.sqrt(sum(v * v for v in w_b.values()))
        if norm_a <= 0 or norm_b <= 0:
            return 0.0, 0.0, []
        common = set(w_a.keys()) & set(w_b.keys())
        dot = sum(w_a[t] * w_b[t] for t in common)
        overlap = sum(min(w_a[t], w_b[t]) for t in common)
        shared = sorted(
            [
                {
                    "ticker": t,
                    "etfWeightPct": round(w_a[t] * 100.0, 2),
                    "fundWeightPct": round(w_b[t] * 100.0, 2),
                }
                for t in common
            ],
            key=lambda x: min(x["etfWeightPct"], x["fundWeightPct"]),
            reverse=True,
        )[:6]
        return (dot / (norm_a * norm_b)) * 100.0, overlap * 100.0, shared

    def _daily_return_corr(nav_a: np.ndarray, nav_b: np.ndarray) -> float | None:
        sub_a = nav_a[corr_start:]
        sub_b = nav_b[corr_start:]
        valid = (~np.isnan(sub_a)) & (~np.isnan(sub_b))
        if int(np.sum(valid)) < 60:
            return None
        sa = sub_a[valid]
        sb = sub_b[valid]
        ra = np.diff(sa) / sa[:-1]
        rb = np.diff(sb) / sb[:-1]
        if len(ra) < 30 or np.std(ra) == 0 or np.std(rb) == 0:
            return None
        r = float(np.corrcoef(ra, rb)[0, 1])
        return clean_num(r * 100.0, 1)

    for etf in etf_raw_list:
        etf_sym = etf["ticker"]
        # Build normalized ticker weight dict for ETF
        etf_w: dict[str, float] = {}
        etf_holdings_named: list[dict[str, Any]] = []
        for h in etf["holdings"]:
            meta = cusip_meta.get(h["cusip"], {})
            t = meta.get("ticker") or h["cusip"]
            if t.startswith("CUSIP:"):
                continue
            w = h["weightPct"] / 100.0
            etf_w[t] = etf_w.get(t, 0.0) + w
            etf_holdings_named.append({
                "ticker": t,
                "name": meta.get("name") or h["name"],
                "weightPct": round(h["weightPct"], 2),
            })

        total_ew = sum(etf_w.values())
        if total_ew <= 0:
            continue
        etf_w = {t: v / total_ew for t, v in etf_w.items()}
        etf_holdings_named.sort(key=lambda x: x["weightPct"], reverse=True)

        etf_nav = None
        if etf_sym in aligned_prices:
            arr, _, _ = aligned_prices[etf_sym]
            etf_nav = arr / arr[0] * 100.0 if not math.isnan(arr[0]) else arr
            etf_rets, _ = compute_returns_and_metrics(etf_nav, qqq_nav, win_indices)
        else:
            etf_rets = {w: None for w in WINDOWS}

        matches: list[dict[str, Any]] = []
        for f in funds_out:
            cik = f["cik"]
            f_w = fund_equity_weights[cik]
            cos_pct, ov_pct, shared = _cosine_and_overlap(etf_w, f_w)
            ret_corr = _daily_return_corr(etf_nav, fund_navs["quarter_end"][cik]) if etf_nav is not None else None
            blended = 0.65 * cos_pct + 0.35 * max(0.0, ret_corr or 0.0)
            matches.append({
                "cik": cik,
                "fundName": f["name"],
                "cosineSimilarityPct": round(cos_pct, 1),
                "weightOverlapPct": round(ov_pct, 1),
                "returnCorrelationPct": ret_corr,
                "blendedScorePct": round(blended, 1),
                "sharedTopStocks": shared,
            })

        matches.sort(key=lambda x: (x["cosineSimilarityPct"], x["returnCorrelationPct"] or 0.0), reverse=True)
        if not matches:
            continue

        ideal_cos, ideal_ov, _ = _cosine_and_overlap(etf_w, ideal_weights)
        ideal_corr = _daily_return_corr(etf_nav, ideal_nav_qe) if etf_nav is not None else None

        etfs_out.append({
            "ticker": etf_sym,
            "name": etf["name"],
            "category": etf["category"],
            "reportDate": etf["reportDate"],
            "holdingsCount": len(etf_holdings_named),
            "top10WeightPct": round(sum(x["weightPct"] for x in etf_holdings_named[:10]), 1),
            "returns": etf_rets,
            "topHoldings": etf_holdings_named[:10],
            "bestMatch": matches[0],
            "fundMatches": matches,
            "idealPortfolioMatch": {
                "cosineSimilarityPct": round(ideal_cos, 1),
                "weightOverlapPct": round(ideal_ov, 1),
                "returnCorrelationPct": ideal_corr,
            },
        })

    # Sort ETFs by how closely their best match mirrors one of our top tech funds
    etfs_out.sort(key=lambda x: x["bestMatch"]["cosineSimilarityPct"], reverse=True)

    # Sort funds by TTM performance by default
    funds_out.sort(key=lambda f: f["returns"]["quarter_end"]["TTM"] or -999.0, reverse=True)

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
            "rankedFundsUnionCount": len(ranked_union_ciks["quarter_end"]),
            "total13fFilingsProcessed": total_filings_cnt,
            "etfsProcessedCount": len(etfs_out),
            "next13fSeasonLabel": next_season,
            "dataSources": [
                "SEC EDGAR Form 13F-HR / 13F-HR/A XML Filings",
                "SEC EDGAR Form NPORT-P ETF Holdings",
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
        "etfs": etfs_out,
    }

    OUTPUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_JSON.write_text(json.dumps(dataset, allow_nan=False), encoding="utf-8")
    size_kb = OUTPUT_JSON.stat().st_size / 1024.0
    print(f"\n=== Pipeline Complete ===")
    print(f"Wrote {OUTPUT_JSON} ({size_kb:.1f} KB)")
    print(f"Qualified Funds: {len(funds_out)} | Ranked Union: {len(ranked_union_ciks['quarter_end'])} | ETFs: {len(etfs_out)}")
    print(f"Ideal Portfolio: {len(ideal_positions)} names | TTM QE Return: {ideal_rets_qe['TTM']}% | QQQ TTM: {benchmarks['QQQ']['returns']['TTM']}%")


if __name__ == "__main__":
    main()
