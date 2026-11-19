"""
Post-build circuit-breaker validator for Paper Trail 13F sharded datasets.
Runs after pipeline/build_data.py (with --strict) to block publishing corrupted,
truncated, or logically broken snapshots, and emits a GitHub Actions Step Summary.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

ANCHOR_MEGA_CAPS = ("AAPL", "MSFT", "NVDA")


def _is_bogus_unpriced_ticker(ticker: str, entry: dict[str, Any]) -> bool:
    if not ticker or ticker.startswith("CUSIP:"):
        return True
    if entry.get("priceUsd") is not None:
        return False
    parts = ticker.split("-")
    if len(parts) > 2:
        return True
    base = parts[0]
    if not base.isalpha() or not (1 <= len(base) <= 5) or base != base.upper():
        return True
    if len(parts) == 2 and (not parts[1].isalpha() or len(parts[1]) != 1 or parts[1] != parts[1].upper()):
        return True
    return False


def validate_dataset(data_dir: Path, *, strict: bool = True) -> tuple[list[str], list[str], dict[str, Any]]:
    """
    Validate public/data artifacts and return (errors, warnings, summary_stats).
    In strict=True mode (post-pipeline rebuild), all semantic & manifest invariants are hard errors.
    """
    errors: list[str] = []
    warnings: list[str] = []
    stats: dict[str, Any] = {}

    summary_path = data_dir / "summary.json"
    if not summary_path.exists():
        return [f"Missing required {summary_path}"], warnings, stats
    summary_size = summary_path.stat().st_size
    if summary_size < 150_000:
        errors.append(f"summary.json abnormally small ({summary_size} bytes < 150,000)")

    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return [f"Failed to parse summary.json: {exc}"], warnings, stats

    meta = summary.get("metadata") or {}
    uf = meta.get("universeFunnel") or {}
    funds = summary.get("funds") or []
    updates = summary.get("fundUpdates") or []
    ideal = summary.get("idealPortfolio") or {}
    ideal_pos = ideal.get("positions") or []
    stocks_index = summary.get("stocksIndex") or {}

    stats.update({
        "latestQuarterCovered": meta.get("latestQuarterCovered", ""),
        "asOfDate": meta.get("asOfDate", ""),
        "qualifiedFundsCount": int(meta.get("qualifiedFundsCount") or 0),
        "leaderboardEligibleCount": int(meta.get("leaderboardEligibleCount") or 0),
        "elite50Count": int(meta.get("elite50Count") or 0),
        "stocksIndexedCount": len(stocks_index),
        "idealPositionsCount": len(ideal_pos),
        "midQuarterAlertsCount": len(summary.get("midQuarterAlerts") or []),
        "universeFunnel": uf,
    })

    # 1. Core volume & structural invariants
    if stats["qualifiedFundsCount"] < 25 or len(funds) < 25:
        errors.append(f"Too few qualified funds: meta={stats['qualifiedFundsCount']}, list={len(funds)}")
    if stats["elite50Count"] < 25:
        errors.append(f"Too few Elite 50 funds: {stats['elite50Count']}")
    if int(meta.get("total13fFilingsProcessed") or 0) < 100:
        errors.append(f"Too few 13F filings processed: {meta.get('total13fFilingsProcessed')}")
    if len(updates) < 15:
        errors.append(f"Too few fundUpdates entries: {len(updates)}")
    if len(ideal_pos) < 15:
        errors.append(f"Too few idealPortfolio positions: {len(ideal_pos)}")
    if len(stocks_index) < 100:
        errors.append(f"Too few stocksIndex entries: {len(stocks_index)}")
    if not isinstance(summary.get("signalScreener"), dict):
        errors.append("Missing signalScreener object")
    if uf:
        if int(uf.get("totalCiksScanned") or 0) < 5000:
            errors.append(f"Unexpected low totalCiksScanned in universeFunnel: {uf.get('totalCiksScanned')}")
        if uf.get("qualifiedFundsCount") != meta.get("qualifiedFundsCount"):
            errors.append("universeFunnel.qualifiedFundsCount mismatches metadata.qualifiedFundsCount")

    # 2. Verify all 16 fund shards + 8 stock shards exist and parse cleanly
    stock_dossiers_by_ticker: dict[str, dict[str, Any]] = {}
    for i in range(16):
        fp = data_dir / f"funds_{i:x}.json"
        if not fp.exists():
            errors.append(f"Missing fund shard {fp.name}")
            continue
        try:
            obj = json.loads(fp.read_text(encoding="utf-8"))
            if "funds" not in obj or "updates" not in obj:
                errors.append(f"Invalid fund shard schema in {fp.name}")
        except Exception as exc:
            errors.append(f"Corrupt JSON in {fp.name}: {exc}")

    for i in range(8):
        sp = data_dir / f"stocks_{i}.json"
        if not sp.exists():
            errors.append(f"Missing stock shard {sp.name}")
            continue
        try:
            obj = json.loads(sp.read_text(encoding="utf-8"))
            if isinstance(obj, dict):
                stock_dossiers_by_ticker.update(obj)
        except Exception as exc:
            errors.append(f"Corrupt JSON in {sp.name}: {exc}")

    # 3. Verify split SPA payloads and manifest.json hashes
    manifest_path = data_dir / "manifest.json"
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            files_map = manifest.get("files") or {}
            for req_name in ("bootstrap.json", "screener.json", "updates_feed.json", "stocks_search.json", "summary.json"):
                req_p = data_dir / req_name
                if not req_p.exists():
                    errors.append(f"manifest.json present but missing split payload {req_name}")
                    continue
                expected_hash = files_map.get(req_name)
                actual_hash = hashlib.sha256(req_p.read_bytes()).hexdigest()[:12]
                if expected_hash != actual_hash:
                    errors.append(f"Hash mismatch for {req_name}: manifest={expected_hash}, actual={actual_hash}")
        except Exception as exc:
            errors.append(f"Invalid manifest.json: {exc}")
    elif strict:
        errors.append("Missing manifest.json and split SPA payloads in strict validation mode")
    else:
        warnings.append("manifest.json not present (legacy pre-split snapshot)")

    # 4. Anchor mega-cap sanity (prices & market caps > $1T)
    for anchor in ANCHOR_MEGA_CAPS:
        dossier = stock_dossiers_by_ticker.get(anchor)
        idx_row = stocks_index.get(anchor)
        if not idx_row or not dossier:
            errors.append(f"Anchor mega-cap {anchor} missing from stocksIndex/stock shards")
            continue
        px = dossier.get("priceUsd")
        mc = dossier.get("marketCapUsd")
        if px is None or float(px) <= 10.0:
            errors.append(f"Anchor mega-cap {anchor} has invalid priceUsd={px}")
        if mc is None or float(mc) < 1e12:
            msg = f"Anchor mega-cap {anchor} marketCapUsd={mc} (< $1T)"
            if strict:
                errors.append(msg)
            else:
                warnings.append(msg)

    # 5. Unresolved / bogus ticker quarantine check
    bogus_tickers = [t for t, row in stocks_index.items() if _is_bogus_unpriced_ticker(t, row)]
    stats["bogusUnpricedTickersCount"] = len(bogus_tickers)
    if bogus_tickers:
        sample = ", ".join(sorted(bogus_tickers)[:8])
        msg = f"Found {len(bogus_tickers)} unresolved/bogus unpriced ticker(s) in stocksIndex: {sample}"
        if strict:
            errors.append(msg)
        else:
            warnings.append(msg)

    # 6. XBRL mega-cap share dilution sanity check (catch summed-quarter buyback bugs / unadjusted splits without false-failing legitimate +25%..+85% M&A or Up-C dilution)
    severe_dilution: list[str] = []
    warn_dilution: list[str] = []
    neg_megacap_25: list[str] = []
    for t, dos in stock_dossiers_by_ticker.items():
        mc = dos.get("marketCapUsd")
        fund_data = dos.get("fundamentals") or {}
        dil = fund_data.get("shareDilutionYoYPct")
        if mc is None or dil is None:
            continue
        mc_f = float(mc)
        dil_f = float(dil)
        if mc_f >= 50e9:
            label = f"{t}({dil_f:+.1f}%)"
            if dil_f < -30.0 or dil_f > 85.0:
                severe_dilution.append(label)
            elif mc_f >= 500e9 and abs(dil_f) > 25.0:
                severe_dilution.append(label)
            elif abs(dil_f) > 25.0:
                warn_dilution.append(label)
            if mc_f >= 100e9 and dil_f <= -25.0:
                neg_megacap_25.append(label)
    if len(neg_megacap_25) >= 2:
        for lbl in neg_megacap_25:
            if lbl not in severe_dilution:
                severe_dilution.append(lbl)
    stats["suspiciousMegaCapDilutionCount"] = len(severe_dilution) + len(warn_dilution)
    if warn_dilution:
        warnings.append(f"Large >25% YoY share-count swing on >=$50B issuer(s): {', '.join(warn_dilution[:8])}")
    if severe_dilution:
        msg = f"Corrupt/systemic YoY share-count swing on >=$50B issuer(s): {', '.join(severe_dilution[:8])}"
        if strict:
            errors.append(msg)
        else:
            warnings.append(msg)

    # 7. Ideal Portfolio sanity: weights ~100%, 0% EXIT SIGNAL, max sub-sector <= 32%
    total_ideal_w = sum(float(p.get("idealWeightPct") or 0.0) for p in ideal_pos)
    if ideal_pos and not (99.0 <= total_ideal_w <= 101.0):
        errors.append(f"Ideal Portfolio weights sum to {total_ideal_w:.2f}% (expected ~100%)")

    exit_signal_w = sum(
        float(p.get("idealWeightPct") or 0.0)
        for p in ideal_pos
        if p.get("actionVerdict") == "EXIT SIGNAL"
    )
    stats["idealExitSignalWeightPct"] = round(exit_signal_w, 2)
    if exit_signal_w > 5.0:
        msg = f"Ideal Portfolio allocates {exit_signal_w:.2f}% to EXIT SIGNAL stocks (limit <= 5%)"
        if strict:
            errors.append(msg)
        else:
            warnings.append(msg)

    sub_totals: dict[str, float] = {}
    for p in ideal_pos:
        ss = str(p.get("subSector") or p.get("sector") or "Other")
        sub_totals[ss] = sub_totals.get(ss, 0.0) + float(p.get("idealWeightPct") or 0.0)
    if sub_totals:
        top_sub, top_sub_w = max(sub_totals.items(), key=lambda x: x[1])
        stats["idealTopSubSector"] = top_sub
        stats["idealTopSubSectorWeightPct"] = round(top_sub_w, 2)
        if top_sub_w > 32.0:
            msg = f"Ideal Portfolio sub-sector concentration breached: {top_sub} = {top_sub_w:.2f}% (limit <= 32%)"
            if strict:
                errors.append(msg)
            else:
                warnings.append(msg)

    return errors, warnings, stats


def _write_github_step_summary(stats: dict[str, Any], warnings: list[str]) -> None:
    gh_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    uf = stats.get("universeFunnel") or {}
    if not gh_summary or not uf:
        return
    lines = [
        f"## Paper Trail Full-Universe 13F Funnel ({stats.get('latestQuarterCovered', '')})",
        "",
        f"**Method:** {uf.get('method', '')} ({uf.get('bulkDatasetsCount', 0)} bulk ZIPs through `{uf.get('bulkMaxFilingDate', '')}` + {uf.get('postBulkFilingsCount', 0):,} post-bulk filings)",
        "",
        "| Stage | Filter Description | Count |",
        "|---|---|---:|",
        f"| 1. Universe Scanned | All SEC 13F-HR / 13F-HR/A filers (2021–Present) | {uf.get('totalCiksScanned', 0):,} |",
        f"| 2. Active Managers | Filed within 210 days / current filing cycle | {uf.get('activeRecentCount', 0):,} |",
        f"| 3. Manager Filter | Exclude operating corps, mega-banks, insurers, pensions | {uf.get('nonExcludedManagerCount', 0):,} |",
        f"| 4. Filing History | ≥4 quarters since 2024-01-01 | {uf.get('fourPlusQuartersCount', 0):,} |",
        f"| 5. Scale Screen | ≥$200M reported / ≥$100M single-stock equity AUM | {uf.get('sizeAndEquityPassCount', 0):,} |",
        f"| 6. Concentration | 5+ pos, effN ≥ 1.4, & ≤130 pos (or ≤265 w/ Top-10 ≥48%) | {uf.get('concentrationPassCount', 0):,} |",
        f"| 7. Tech/TMT Focus | ≥35% Tech/TMT + SEC entityType/SIC check | {uf.get('techFloorPassCount', 0):,} |",
        f"| 8. Strict Audit | Rolling 4Q median Tech/TMT persistence & breadth | {uf.get('strictQualificationCount', 0):,} |",
        f"| 9. Priced History Qualified | ≥$100M & ≥70% single-stock equity w/ price history | {uf.get('qualifiedFundsCount', 0):,} |",
        f"| 10. Leaderboard Eligible / Elite 50 | Anti-SPV cohort ({uf.get('leaderboardEligibleCount', 0)}) → Elite 50 Skill Cohort ({uf.get('elite50Count', 0)}) | {uf.get('leaderboardEligibleCount', 0)} / {uf.get('elite50Count', 0)} |",
        f"| 11. Top-100 Union | Ranked Top 100 in any window (QE / 45d Lag) | {uf.get('rankedUnionQeCount', 0):,} / {uf.get('rankedUnionLagCount', 0):,} |",
        f"| 12. Sharded UI Output | Ideal Positions / Indexed Stocks / 13D-13G Alerts | {uf.get('idealPortfolioCount', 0)} / {uf.get('stocksIndexedCount', 0):,} / {uf.get('midQuarterAlertsCount', 0)} |",
        "",
        f"**Circuit-Breaker Checks:** Ideal `EXIT SIGNAL` weight = `{stats.get('idealExitSignalWeightPct', 0.0):.2f}%` | Top Sub-Sector = `{stats.get('idealTopSubSector', 'N/A')}` (`{stats.get('idealTopSubSectorWeightPct', 0.0):.2f}%`) | Bogus Unpriced Tickers = `{stats.get('bogusUnpricedTickersCount', 0)}`",
    ]
    if warnings:
        lines.append("")
        lines.append("### Warnings")
        for w in warnings:
            lines.append(f"- {w}")
    with open(gh_summary, "a", encoding="utf-8") as sf:
        sf.write("\n".join(lines) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate Paper Trail sharded dataset integrity.")
    parser.add_argument("--data-dir", type=Path, default=Path("public/data"))
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Enforce all post-rebuild semantic invariants (manifest, 0%% EXIT SIGNAL, sub-sector cap, clean tickers).",
    )
    args = parser.parse_args()

    errors, warnings, stats = validate_dataset(args.data_dir, strict=args.strict)
    _write_github_step_summary(stats, warnings)

    for w in warnings:
        print(f"[validate][WARN] {w}", flush=True)
    if errors:
        for e in errors:
            print(f"[validate][FAIL] {e}", file=sys.stderr, flush=True)
        return 1

    print(
        f"[validate][OK] {stats.get('qualifiedFundsCount', 0)} qualified funds "
        f"({stats.get('elite50Count', 0)} Elite 50), {stats.get('stocksIndexedCount', 0)} indexed stocks, "
        f"Ideal EXIT SIGNAL={stats.get('idealExitSignalWeightPct', 0.0):.2f}%, "
        f"Top Sub-Sector={stats.get('idealTopSubSector', 'N/A')} ({stats.get('idealTopSubSectorWeightPct', 0.0):.2f}%)",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
