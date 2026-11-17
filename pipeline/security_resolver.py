"""
CUSIP-to-Ticker resolution (OpenFIGI + Yahoo Finance fallback), sector/industry
enrichment, and historical daily adjusted-close price fetching.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from funds_config import TECH_TMT_EXTRA_TICKERS

CACHE_ROOT = Path(__file__).resolve().parent / "cache"
PRICE_CACHE_DIR = CACHE_ROOT / "prices"
PRICE_CACHE_DIR.mkdir(parents=True, exist_ok=True)

CUSIP_CACHE_PATH = Path(__file__).resolve().parent / "cusip_cache.json"
TICKER_META_PATH = Path(__file__).resolve().parent / "ticker_meta_cache.json"

YF_HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}
FIGI_HEADERS = {"Content-Type": "application/json", "User-Agent": "PaperTrail13FResearch"}

# Authoritative manual overrides for known tricky CUSIPs / share classes
MANUAL_CUSIP_OVERRIDES: dict[str, dict[str, str]] = {
    "02079K305": {"ticker": "GOOGL", "name": "Alphabet Inc. (Class A)"},
    "02079K107": {"ticker": "GOOG", "name": "Alphabet Inc. (Class C)"},
    "084670702": {"ticker": "BRK-B", "name": "Berkshire Hathaway Inc."},
    "084670108": {"ticker": "BRK-A", "name": "Berkshire Hathaway Inc."},
    "88160R101": {"ticker": "TSLA", "name": "Tesla, Inc."},
    "67066G104": {"ticker": "NVDA", "name": "NVIDIA Corporation"},
    "594918104": {"ticker": "MSFT", "name": "Microsoft Corporation"},
    "037833100": {"ticker": "AAPL", "name": "Apple Inc."},
    "30303M102": {"ticker": "META", "name": "Meta Platforms, Inc."},
    "023135106": {"ticker": "AMZN", "name": "Amazon.com, Inc."},
    "11135F101": {"ticker": "AVGO", "name": "Broadcom Inc."},
    "874039100": {"ticker": "TSM", "name": "Taiwan Semiconductor Manufacturing"},
    "G87572163": {"ticker": "SPOT", "name": "Spotify Technology S.A."},
    "N53745100": {"ticker": "LYB", "name": "LyondellBasell Industries"},
    "N07059210": {"ticker": "ASML", "name": "ASML Holding N.V."},
    "G66721104": {"ticker": "NU", "name": "Nu Holdings Ltd."},
    "82509L107": {"ticker": "SHOP", "name": "Shopify Inc."},
    "81762P102": {"ticker": "NOW", "name": "ServiceNow, Inc."},
    "22788C105": {"ticker": "CRWD", "name": "CrowdStrike Holdings, Inc."},
    "69608A108": {"ticker": "PLTR", "name": "Palantir Technologies Inc."},
    "03831W108": {"ticker": "APP", "name": "AppLovin Corporation"},
    "00091G104": {
        "ticker": "ACVA",
        "name": "ACV Auctions Inc.",
        "sector": "Consumer Cyclical",
        "industry": "Auto & Truck Dealerships",
    },
    "N3167Y103": {
        "ticker": "RACE",
        "name": "Ferrari N.V.",
        "sector": "Consumer Cyclical",
        "industry": "Auto Manufacturers",
    },
    "H2927K103": {
        "ticker": "AMRZ",
        "name": "Amrize Ltd",
        "sector": "Basic Materials",
        "industry": "Building Materials",
    },
    "M3760D101": {
        "ticker": "ESLT",
        "name": "Elbit Systems Ltd.",
        "sector": "Industrials",
        "industry": "Aerospace & Defense",
    },
    "M6240T109": {
        "ticker": "KMDA",
        "name": "Kamada Ltd.",
        "sector": "Healthcare",
        "industry": "Drug Manufacturers—Specialty & Generic",
    },
    "08862E109": {
        "ticker": "BYND",
        "name": "Beyond Meat, Inc.",
        "sector": "Consumer Defensive",
        "industry": "Packaged Foods",
    },
    "94724R108": {
        "ticker": "WEAV",
        "name": "Weave Communications, Inc.",
        "sector": "Technology",
        "industry": "Software—Application",
    },
    "679369108": {
        "ticker": "OLPX",
        "name": "Olaplex Holdings, Inc.",
        "sector": "Consumer Defensive",
        "industry": "Household & Personal Products",
    },
}


def load_json_dict(path: Path) -> dict[str, Any]:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_json_dict(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


def normalize_yf_ticker(ticker: str) -> str:
    if not ticker:
        return ""
    t = ticker.strip().upper()
    # Yahoo uses '-' for class shares like BRK-B instead of BRK.B or BRK/B
    t = t.replace(".", "-").replace("/", "-")
    return t


def is_tech_tmt(ticker: str, sector: str, industry: str) -> bool:
    s = (sector or "").lower()
    ind = (industry or "").lower()
    t = (ticker or "").upper().replace("-", ".")
    if s == "technology":
        return True
    if t in TECH_TMT_EXTRA_TICKERS:
        return True
    if any(k in ind for k in (
        "software", "semiconductor", "internet", "cloud", "cyber",
        "information technology", "electronic gaming", "fintech",
        "data processing", "computer", "communications equipment",
        "interactive media", "electronic components",
    )):
        return True
    return False


def _openfigi_batch(jobs: list[dict[str, str]]) -> list[dict[str, Any]]:
    for attempt in range(4):
        req = urllib.request.Request(
            "https://api.openfigi.com/v3/mapping",
            data=json.dumps(jobs).encode("utf-8"),
            headers=FIGI_HEADERS,
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(15.0 + attempt * 5.0)
                continue
            if e.code >= 500 and attempt < 3:
                time.sleep(2.0 * (attempt + 1))
                continue
            return [{} for _ in jobs]
        except Exception:
            if attempt < 3:
                time.sleep(2.0 * (attempt + 1))
                continue
            return [{} for _ in jobs]
    return [{} for _ in jobs]


def _pick_best_figi(candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not candidates:
        return None
    us_codes = {"US", "UN", "UQ", "UW", "UA", "UP", "UR"}
    preferred_types = ("Common Stock", "Depositary Receipt", "ETP", "REIT", "Preference")
    us_matches = [c for c in candidates if c.get("exchCode") in us_codes and c.get("ticker")]
    pool = us_matches or [c for c in candidates if c.get("ticker")]
    for st in preferred_types:
        for c in pool:
            if c.get("securityType2") == st or c.get("securityType") == st:
                return c
    return pool[0] if pool else None


US_YAHOO_EXCHANGES = {"NMS", "NYQ", "NGM", "NCM", "ASE", "PCX", "BTS", "NAS", "NYS"}


def _yahoo_search(query: str, strict_us_cusip: bool = False) -> dict[str, Any] | None:
    q = urllib.parse.quote(query)
    url = f"https://query1.finance.yahoo.com/v1/finance/search?q={q}&quotesCount=6&newsCount=0"
    for attempt in range(3):
        req = urllib.request.Request(url, headers=YF_HEADERS)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                quotes = json.loads(resp.read().decode("utf-8")).get("quotes", [])
            if not quotes:
                return None
            q_up = query.strip().upper()
            all_equities = [x for x in quotes if x.get("quoteType") in ("EQUITY", "ETF")]
            if strict_us_cusip:
                us_strict = [
                    x for x in all_equities
                    if x.get("exchange") in US_YAHOO_EXCHANGES
                    and "." not in x.get("symbol", "")
                    and re.fullmatch(r"[A-Z]{1,5}(?:-[A-Z])?", (x.get("symbol") or "").upper())
                ]
                if not us_strict:
                    return None
                best = us_strict[0]
                return {
                    "ticker": best.get("symbol", "").upper(),
                    "name": best.get("longname") or best.get("shortname") or "",
                    "sector": best.get("sector") or "",
                    "industry": best.get("industry") or "",
                }

            # General search: prefer exact US equity/ETF without dot suffix
            us_equities = [x for x in all_equities if "." not in x.get("symbol", "")]
            exact = [x for x in us_equities if x.get("symbol", "").upper() == q_up]
            pool = exact or us_equities or all_equities
            if not pool:
                return None
            best = pool[0]
            return {
                "ticker": best.get("symbol", "").upper(),
                "name": best.get("longname") or best.get("shortname") or "",
                "sector": best.get("sector") or "",
                "industry": best.get("industry") or "",
            }
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(2.0 * (attempt + 1))
                continue
            return None
        except Exception:
            if attempt < 2:
                time.sleep(0.6)
                continue
            return None
    return None


def resolve_cusips(cusip_to_issuer: dict[str, str]) -> dict[str, dict[str, Any]]:
    """
    Resolve all CUSIPs to ticker, canonical name, sector, industry, isTechTmt.
    Uses persistent cusip_cache.json + fast US Yahoo search + OpenFIGI + issuer fallback.
    """
    cache = load_json_dict(CUSIP_CACHE_PATH)
    ticker_meta = load_json_dict(TICKER_META_PATH)

    # Apply manual overrides
    for c, ov in MANUAL_CUSIP_OVERRIDES.items():
        entry = cache.get(c, {})
        changed_ticker = entry.get("ticker") != ov["ticker"]
        entry["ticker"] = ov["ticker"]
        entry["name"] = ov["name"]
        entry.pop("unpriced", None)
        entry.pop("displaySymbol", None)
        if ov.get("sector"):
            entry["sector"] = ov["sector"]
        elif changed_ticker:
            entry["sector"] = ""
        if ov.get("industry"):
            entry["industry"] = ov["industry"]
        elif changed_ticker:
            entry["industry"] = ""
        cache[c] = entry
        if ov.get("sector"):
            ticker_meta[ov["ticker"]] = {
                "ticker": ov["ticker"],
                "name": ov["name"],
                "sector": ov["sector"],
                "industry": ov.get("industry", ""),
                "metaChecked": True,
            }

    missing_ticker = [c for c in cusip_to_issuer if c not in cache or not cache[c].get("ticker")]
    print(f"[resolver] Total CUSIPs: {len(cusip_to_issuer)}, missing ticker resolution: {len(missing_ticker)}", flush=True)

    # Step 1a: Fast parallel strict-US Yahoo CUSIP lookup (resolves ~85-90% of US equities w/ sector metadata in 1 call)
    if missing_ticker:
        figi_candidates: list[str] = []
        if len(missing_ticker) > 25:
            def _fast_us_lookup(c: str) -> tuple[str, dict[str, Any] | None]:
                return c, _yahoo_search(c, strict_us_cusip=True)

            resolved_fast = 0
            with ThreadPoolExecutor(max_workers=8) as ex:
                futs = [ex.submit(_fast_us_lookup, c) for c in missing_ticker]
                for idx_done, fut in enumerate(as_completed(futs), 1):
                    c, hit = fut.result()
                    if hit and hit.get("ticker"):
                        t_norm = normalize_yf_ticker(hit["ticker"])
                        sec_val = hit.get("sector") or ("Technology" if t_norm in TECH_TMT_EXTRA_TICKERS else "Other")
                        cache[c] = {
                            "ticker": t_norm,
                            "name": hit.get("name") or cusip_to_issuer.get(c, c),
                            "sector": sec_val,
                            "industry": hit.get("industry") or "",
                        }
                        if t_norm not in ticker_meta or (hit.get("sector") and not ticker_meta[t_norm].get("sector")):
                            ticker_meta[t_norm] = {
                                "ticker": t_norm,
                                "name": cache[c]["name"],
                                "sector": sec_val,
                                "industry": hit.get("industry", ""),
                                "metaChecked": True,
                            }
                        resolved_fast += 1
                    else:
                        figi_candidates.append(c)
                    if idx_done % 500 == 0:
                        save_json_dict(CUSIP_CACHE_PATH, cache)
                        save_json_dict(TICKER_META_PATH, ticker_meta)
                        print(f"[resolver] Fast US lookup progress {idx_done}/{len(missing_ticker)} ({resolved_fast} hits)...", flush=True)
            save_json_dict(CUSIP_CACHE_PATH, cache)
            save_json_dict(TICKER_META_PATH, ticker_meta)
            print(f"[resolver] Fast US lookup resolved {resolved_fast}/{len(missing_ticker)} CUSIPs; {len(figi_candidates)} queued for OpenFIGI", flush=True)
        else:
            figi_candidates = list(missing_ticker)

        # Step 1b: Authoritative OpenFIGI US mapping in batches of 10 for foreign ADRs / Cayman CINS / edge CUSIPs
        still_missing: list[str] = []
        total_batches = (len(figi_candidates) + 9) // 10
        for i in range(0, len(figi_candidates), 10):
            t0 = time.monotonic()
            batch_num = (i // 10) + 1
            batch = figi_candidates[i : i + 10]
            jobs = [{"idType": "ID_CUSIP", "idValue": c, "exchCode": "US"} for c in batch]
            results = _openfigi_batch(jobs)
            for c, res in zip(batch, results):
                best = _pick_best_figi(res.get("data", []))
                if best and best.get("ticker"):
                    ticker = normalize_yf_ticker(best["ticker"])
                    cache[c] = {
                        "ticker": ticker,
                        "name": best.get("name") or cusip_to_issuer.get(c, c),
                        "sector": cache.get(c, {}).get("sector", ""),
                        "industry": cache.get(c, {}).get("industry", ""),
                    }
                else:
                    still_missing.append(c)
            # OpenFIGI unauthenticated cap is 25 req/min (~2.4s/req); 2.55s cycle avoids 429 penalties
            if batch_num % 10 == 0 or batch_num == total_batches:
                save_json_dict(CUSIP_CACHE_PATH, cache)
                print(f"[resolver] OpenFIGI batch {batch_num}/{total_batches} saved ({len(cache)} cached)", flush=True)
            if i + 10 < len(figi_candidates):
                elapsed = time.monotonic() - t0
                if elapsed < 2.55:
                    time.sleep(2.55 - elapsed)
        save_json_dict(CUSIP_CACHE_PATH, cache)

        # Step 2: Yahoo issuer-name fallback for CUSIPs not resolved by OpenFIGI US
        if still_missing:
            print(f"[resolver] Falling back to Yahoo issuer search for {len(still_missing)} CUSIPs...", flush=True)
            def _resolve_one(c: str) -> tuple[str, dict[str, Any]]:
                raw_name = cusip_to_issuer.get(c, "")
                cleaned = re.sub(r"\b(INC|CORP|LTD|PLC|LP|LLC|NV|SA|AG|SE|HLDG|HLDGS|HOLDINGS|CLASS [A-C]|CL [A-C]|COM|ORD)\b\.?", "", raw_name, flags=re.I).strip()
                hit = _yahoo_search(cleaned) if len(cleaned) >= 3 else None
                if hit and hit.get("ticker"):
                    t_norm = normalize_yf_ticker(hit["ticker"])
                    sec_val = hit.get("sector") or ("Technology" if t_norm in TECH_TMT_EXTRA_TICKERS else "Other")
                    return c, {
                        "ticker": t_norm,
                        "name": hit.get("name") or raw_name or c,
                        "sector": sec_val,
                        "industry": hit.get("industry") or "",
                    }
                # Unresolvable (e.g. delisted / private / foreign OTC without US ticker)
                fallback_sym = re.sub(r"[^A-Z0-9]", "", raw_name.upper())[:6] or c[:6]
                return c, {
                    "ticker": f"CUSIP:{c}",
                    "displaySymbol": fallback_sym,
                    "name": raw_name or c,
                    "sector": "Other",
                    "industry": "",
                    "unpriced": True,
                }

            with ThreadPoolExecutor(max_workers=8) as ex:
                futs = [ex.submit(_resolve_one, c) for c in still_missing]
                for idx_done, fut in enumerate(as_completed(futs), 1):
                    c, entry = fut.result()
                    cache[c] = entry
                    if idx_done % 250 == 0:
                        save_json_dict(CUSIP_CACHE_PATH, cache)
                        print(f"[resolver] Issuer fallback progress {idx_done}/{len(still_missing)}...", flush=True)

    # Step 3: Enrich sector/industry/name via Yahoo search for resolved tickers not yet metadata-checked
    need_meta_tickers = sorted({
        cache[c]["ticker"]
        for c in cusip_to_issuer
        if c in cache
        and cache[c].get("ticker")
        and not cache[c]["ticker"].startswith("CUSIP:")
        and (
            cache[c]["ticker"] not in ticker_meta
            or (not cache[c].get("sector") and not ticker_meta[cache[c]["ticker"]].get("metaChecked"))
        )
    })

    if need_meta_tickers:
        print(f"[resolver] Enriching sector/industry metadata for {len(need_meta_tickers)} tickers...", flush=True)
        def _fetch_meta(t: str) -> tuple[str, dict[str, Any] | None]:
            if t in ticker_meta and (ticker_meta[t].get("sector") or ticker_meta[t].get("metaChecked")):
                return t, ticker_meta[t]
            hit = _yahoo_search(t)
            if hit:
                hit["metaChecked"] = True
                if not hit.get("sector"):
                    hit["sector"] = "Technology" if t in TECH_TMT_EXTRA_TICKERS else "Other"
                return t, hit
            return t, {
                "ticker": t,
                "name": "",
                "sector": "Technology" if t in TECH_TMT_EXTRA_TICKERS else "Other",
                "industry": "",
                "metaChecked": True,
            }

        with ThreadPoolExecutor(max_workers=8) as ex:
            futs = [ex.submit(_fetch_meta, t) for t in need_meta_tickers]
            for idx_done, fut in enumerate(as_completed(futs), 1):
                t, meta = fut.result()
                if meta:
                    ticker_meta[t] = meta
                if idx_done % 250 == 0:
                    save_json_dict(TICKER_META_PATH, ticker_meta)
                    print(f"[resolver] Metadata enrich progress {idx_done}/{len(need_meta_tickers)}...", flush=True)

    # Sync enriched metadata back into cache
    for c in cusip_to_issuer:
        entry = cache.get(c)
        if not entry:
            continue
        t = entry.get("ticker", "")
        meta = ticker_meta.get(t)
        if meta:
            if meta.get("sector"):
                entry["sector"] = meta["sector"]
            if meta.get("industry"):
                entry["industry"] = meta["industry"]
            if c not in MANUAL_CUSIP_OVERRIDES and meta.get("name") and len(meta["name"]) > 2:
                entry["name"] = meta["name"]
        if not entry.get("sector"):
            entry["sector"] = "Technology" if t in TECH_TMT_EXTRA_TICKERS else "Other"
        entry["isTechTmt"] = is_tech_tmt(t, entry.get("sector", ""), entry.get("industry", ""))

    save_json_dict(CUSIP_CACHE_PATH, cache)
    save_json_dict(TICKER_META_PATH, ticker_meta)
    return cache


def fetch_daily_prices(ticker: str, start_ts: int = 1609459200, max_age_hours: float = 8.0) -> dict[str, float] | None:
    """
    Fetch daily split- and dividend-adjusted close prices (YYYY-MM-DD -> adjclose)
    from Yahoo Finance v8 chart API.
    """
    if not ticker or ticker.startswith("CUSIP:"):
        return None
    safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", ticker)
    cpath = PRICE_CACHE_DIR / f"{safe_name}.json"
    if cpath.exists():
        age_hours = (time.time() - cpath.stat().st_mtime) / 3600.0
        if age_hours <= max_age_hours:
            data = json.loads(cpath.read_text(encoding="utf-8"))
            return data if data else None

    end_ts = int(time.time()) + 86400
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(ticker)}?period1={start_ts}&period2={end_ts}&interval=1d"
    for attempt in range(3):
        req = urllib.request.Request(url, headers=YF_HEADERS)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            result = (payload.get("chart", {}).get("result") or [None])[0]
            if not result:
                cpath.write_text("{}", encoding="utf-8")
                return None
            timestamps = result.get("timestamp") or []
            ind = result.get("indicators", {})
            adj_list = (ind.get("adjclose") or [{}])[0].get("adjclose")
            close_list = (ind.get("quote") or [{}])[0].get("close")
            prices_raw = adj_list if adj_list else close_list
            if not timestamps or not prices_raw:
                cpath.write_text("{}", encoding="utf-8")
                return None
            out: dict[str, float] = {}
            from datetime import datetime, timezone
            for ts, px in zip(timestamps, prices_raw):
                if px is not None and px > 0:
                    dt = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
                    out[dt] = float(px)
            cpath.write_text(json.dumps(out), encoding="utf-8")
            return out if out else None
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(2.0 * (attempt + 1))
                continue
            if e.code in (400, 404):
                cpath.write_text("{}", encoding="utf-8")
                return None
            time.sleep(1.0)
        except Exception:
            if attempt < 2:
                time.sleep(0.8)
                continue
            return None
    return None


def fetch_all_prices(tickers: list[str]) -> dict[str, dict[str, float]]:
    clean_tickers = sorted({t for t in tickers if t and not t.startswith("CUSIP:")})
    print(f"[prices] Fetching daily adjusted prices for {len(clean_tickers)} tickers...", flush=True)
    prices: dict[str, dict[str, float]] = {}
    with ThreadPoolExecutor(max_workers=12) as ex:
        fut_map = {ex.submit(fetch_daily_prices, t): t for t in clean_tickers}
        for idx_done, fut in enumerate(as_completed(fut_map), 1):
            t = fut_map[fut]
            try:
                series = fut.result()
                if series:
                    prices[t] = series
            except Exception:
                pass
            if idx_done % 400 == 0 or idx_done == len(clean_tickers):
                print(f"[prices] Progress {idx_done}/{len(clean_tickers)} ({len(prices)} valid)...", flush=True)
    print(f"[prices] Loaded valid price series for {len(prices)}/{len(clean_tickers)} tickers.", flush=True)
    return prices
