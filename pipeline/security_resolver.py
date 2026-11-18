"""
CUSIP-to-Ticker resolution (OpenFIGI + Yahoo Finance fallback), sector/industry
enrichment, and historical daily adjusted-close price fetching.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from funds_config import TECH_TMT_EXTRA_TICKERS, is_non_stock_etf

CACHE_ROOT = Path(__file__).resolve().parent / "cache"
PRICE_CACHE_DIR = CACHE_ROOT / "prices"
PRICE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
SEC_COMPANY_TICKERS_PATH = CACHE_ROOT / "sec" / "company_tickers.json"

CUSIP_CACHE_PATH = Path(__file__).resolve().parent / "cusip_cache.json"
TICKER_META_PATH = Path(__file__).resolve().parent / "ticker_meta_cache.json"

YF_HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}
FIGI_HEADERS = {"Content-Type": "application/json", "User-Agent": "PaperTrail13FResearch"}
_openfigi_key = os.environ.get("OPENFIGI_API_KEY", "").strip()
if _openfigi_key:
    FIGI_HEADERS["X-OPENFIGI-APIKEY"] = _openfigi_key

# Authoritative manual overrides for known tricky CUSIPs / share classes / recently delisted M&A
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
    "74736K101": {
        "ticker": "QRVO",
        "name": "Qorvo, Inc.",
        "sector": "Technology",
        "industry": "Semiconductors",
    },
    "934423104": {
        "ticker": "WBD",
        "name": "Warner Bros. Discovery, Inc.",
        "sector": "Communication Services",
        "industry": "Entertainment",
    },
    "49177J102": {
        "ticker": "KVUE",
        "name": "Kenvue Inc.",
        "sector": "Consumer Defensive",
        "industry": "Household & Personal Products",
    },
    "H50430232": {
        "ticker": "LOGI",
        "name": "Logitech International S.A.",
        "sector": "Technology",
        "industry": "Computer Hardware",
    },
    "G25839104": {
        "ticker": "CCEP",
        "name": "Coca-Cola Europacific Partners",
        "sector": "Consumer Defensive",
        "industry": "Beverages—Non-Alcoholic",
    },
    "053484101": {
        "ticker": "AVB",
        "name": "AvalonBay Communities, Inc.",
        "sector": "Real Estate",
        "industry": "REIT—Residential",
    },
    "530307305": {
        "ticker": "LBRDK",
        "name": "Liberty Broadband Corporation",
        "sector": "Communication Services",
        "industry": "Telecom Services",
    },
}

_MALFORMED_FOREIGN_TICKER_RE = re.compile(r"-[A-Z0-9]{2,}$")
_US_VALID_TICKER_RE = re.compile(r"^[A-Z]{1,5}(?:-[A-Z])?$")

_UMBRELLA_ETF_ISSUER_RE = re.compile(
    r"\b(ETF|ETFS|TRUST|TR\b|ISHARES|INVESCO|VANGUARD|SPDR|DIMENSIONAL|GLOBAL\s+X|"
    r"WISDOMTREE|FIRST\s+TRUST|PROSHARES|VANECK|KRANESHARES|ROUNDHILL|GRANITESHARES|"
    r"DIREXION|TIDAL|EA\s+SERIES|SELECT\s+SECTOR|EXCHANGE\s+TRADED)\b",
    re.I,
)
_GENERIC_TITLE_RE = re.compile(
    r"^(COM|COMMON|CL\s*[A-Z]|CLASS\s*[A-Z]|ORD|ORDINARY|SHS|SHARES|SH\s+BEN\s+INT|"
    r"ADR|ADS|ETF|UNIT|UNITS|NOTE|NOTES|WARRANT|WT|PFD|PREF|TR\s+UNIT|\*W\s+EXP.*)$",
    re.I,
)
# Known multi-CUSIP umbrella-poisoned ETF symbols that must never bind generically to unrelated CUSIPs
_POISON_PRONE_ETF_TICKERS = {"GARP", "XLYM", "KHYB"}

_13F_ABBREV_MAP = {
    "CMNTYS": "COMMUNITIES",
    "CMNTY": "COMMUNITY",
    "PPTYS": "PROPERTIES",
    "PPTY": "PROPERTY",
    "HLDGS": "HOLDINGS",
    "HLDG": "HOLDING",
    "INTL": "INTERNATIONAL",
    "SVCS": "SERVICES",
    "SVC": "SERVICE",
    "SYS": "SYSTEMS",
    "TECH": "TECHNOLOGIES",
    "ENTMT": "ENTERTAINMENT",
    "COS": "COMPANIES",
    "FINL": "FINANCIAL",
    "AMER": "AMERICAN",
    "NATL": "NATIONAL",
    "INDS": "INDUSTRIES",
    "INDL": "INDUSTRIAL",
    "PHARMA": "PHARMACEUTICALS",
    "THERAP": "THERAPEUTICS",
    "COMM": "COMMUNICATIONS",
    "COMMUN": "COMMUNICATIONS",
    "MFG": "MANUFACTURING",
}

_LEGAL_SUFFIX_RE = re.compile(
    r"\b(INCORPORATED|INC|CORPORATION|CORP|LIMITED|LTD|PLC|LP|LLC|LLP|NV|SA|AG|SE|"
    r"HOLDINGS|HOLDING|GROUP|COMPANY|CO|CLASS [A-C]|CL [A-C]|CAP STK|COM|ORD|SHS|NEW|THE)\b\.?",
    re.I,
)


def _normalize_legal_name(name: str) -> str:
    txt = re.sub(r"[^A-Za-z0-9\s]", " ", (name or "").upper())
    tokens = [_13F_ABBREV_MAP.get(tok, tok) for tok in txt.split()]
    expanded = " ".join(tokens)
    stripped = _LEGAL_SUFFIX_RE.sub(" ", expanded)
    return re.sub(r"\s+", " ", stripped).strip()


def _extract_issuer_title(val: str | dict[str, str] | None) -> tuple[str, str]:
    if isinstance(val, dict):
        return str(val.get("issuer") or ""), str(val.get("title") or "")
    return str(val or ""), ""


def _build_yahoo_fallback_query(issuer: str, title: str) -> str:
    """
    Build a safe Yahoo issuer/title query. Prevents umbrella ETF trusts (e.g. BlackRock ETF Trust,
    Tidal Trust II, KraneShares Trust) with generic 'COM'/'SHS' titles from colliding onto a single
    arbitrary ETF ticker like GARP/XLYM/KHYB.
    """
    iss_clean = (issuer or "").strip()
    tit_clean = (title or "").strip()
    if _UMBRELLA_ETF_ISSUER_RE.search(iss_clean):
        if tit_clean and not _GENERIC_TITLE_RE.match(tit_clean):
            # If title explicitly specifies the sub-fund name or ticker, search specifically on title (+ brand)
            brand = re.sub(r"\b(ETF|ETFS|TRUST|TR|SERIES|II|III|IV)\b", " ", iss_clean, flags=re.I)
            brand_clean = re.sub(r"\s+", " ", brand).strip()
            combined = f"{brand_clean} {tit_clean}".strip() if brand_clean else tit_clean
            return combined
        return ""
    return _normalize_legal_name(iss_clean)


def _load_sec_company_name_map() -> dict[str, str]:
    """Build normalized unique company name -> US ticker lookup from SEC company_tickers.json."""
    if not SEC_COMPANY_TICKERS_PATH.exists():
        return {}
    try:
        raw = json.loads(SEC_COMPANY_TICKERS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    by_norm: dict[str, set[str]] = {}
    for entry in raw.values():
        sym = normalize_yf_ticker(str(entry.get("ticker") or ""))
        title = str(entry.get("title") or "")
        if not sym or not _US_VALID_TICKER_RE.fullmatch(sym):
            continue
        norm = _normalize_legal_name(title)
        if len(norm) >= 5:
            by_norm.setdefault(norm, set()).add(sym)
    return {k: next(iter(v)) for k, v in by_norm.items() if len(v) == 1}


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
    # Yahoo uses '-' ONLY for US single-letter share classes like BRK.B -> BRK-B or BRK/B -> BRK-B.
    # Never mangle multi-character foreign exchange suffixes (.TW, .HM, .TI, .SG) into hyphens.
    t = re.sub(r"[./]([A-Z])$", r"-\1", t)
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
    us_matches = [
        c for c in candidates
        if c.get("exchCode") in us_codes
        and c.get("ticker")
        and _US_VALID_TICKER_RE.fullmatch(normalize_yf_ticker(str(c.get("ticker") or "")))
    ]
    if not us_matches:
        return None
    for st in preferred_types:
        for c in us_matches:
            if c.get("securityType2") == st or c.get("securityType") == st:
                return c
    return us_matches[0]


US_YAHOO_EXCHANGES = {"NMS", "NYQ", "NGM", "NCM", "ASE", "PCX", "BTS", "NAS", "NYS"}


def _yahoo_search(query: str, strict_us_cusip: bool = False, us_only: bool = False) -> dict[str, Any] | None:
    q = urllib.parse.quote(query)
    url = f"https://query1.finance.yahoo.com/v1/finance/search?q={q}&quotesCount=6&newsCount=0&region=US&lang=en-US"
    for attempt in range(3):
        req = urllib.request.Request(url, headers=YF_HEADERS)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                quotes = json.loads(resp.read().decode("utf-8")).get("quotes", [])
            if not quotes:
                return None
            q_up = query.strip().upper()
            all_equities = [x for x in quotes if x.get("quoteType") in ("EQUITY", "ETF")]
            us_strict = [
                x for x in all_equities
                if x.get("exchange") in US_YAHOO_EXCHANGES
                and "." not in x.get("symbol", "")
                and _US_VALID_TICKER_RE.fullmatch((x.get("symbol") or "").upper())
            ]
            if strict_us_cusip or us_only:
                if not us_strict:
                    return None
                exact_us = [x for x in us_strict if x.get("symbol", "").upper() == q_up]
                best = exact_us[0] if exact_us else us_strict[0]
                return {
                    "ticker": best.get("symbol", "").upper(),
                    "name": best.get("longname") or best.get("shortname") or "",
                    "sector": best.get("sector") or "",
                    "industry": best.get("industry") or "",
                }

            # General symbol lookup: prefer US exchanges first, fall back to clean non-regional tickers
            exact = [x for x in us_strict if x.get("symbol", "").upper() == q_up]
            pool = exact or us_strict or [x for x in all_equities if "." not in x.get("symbol", "")]
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


def resolve_cusips(
    cusip_to_issuer: dict[str, str | dict[str, str]],
    cusip_priority: dict[str, float] | None = None,
    max_openfigi_batches: int | None = None,
    max_fast_us: int | None = None,
) -> dict[str, dict[str, Any]]:
    """
    Resolve all CUSIPs to US ticker, canonical name, sector, industry, isTechTmt.
    Accepts `cusip_to_issuer` values as either `issuer_str` or `{"issuer": ..., "title": ...}`.
    Cleans umbrella ETF collisions and legacy unattempted CUSIP:* entries; never persists
    unattempted dust tails into cusip_cache.json.
    """
    cache = load_json_dict(CUSIP_CACHE_PATH)
    ticker_meta = load_json_dict(TICKER_META_PATH)
    sec_name_map = _load_sec_company_name_map()

    issuer_by_c: dict[str, str] = {}
    title_by_c: dict[str, str] = {}
    for c, info_val in cusip_to_issuer.items():
        iss, tit = _extract_issuer_title(info_val)
        issuer_by_c[c] = iss
        title_by_c[c] = tit

    # Purge poisoned umbrella ETF collisions & never-attempted legacy CUSIP:* placeholders
    repaired_poison = 0
    pruned_unattempted = 0
    for c in list(cache.keys()):
        if c in MANUAL_CUSIP_OVERRIDES:
            continue
        ent = cache.get(c) or {}
        t_cur = str(ent.get("ticker") or "")
        if t_cur in _POISON_PRONE_ETF_TICKERS:
            cache.pop(c, None)
            repaired_poison += 1
        elif t_cur.startswith("CUSIP:") and not ent.get("resolvedFail"):
            cache.pop(c, None)
            pruned_unattempted += 1
    if repaired_poison or pruned_unattempted:
        print(
            f"[resolver] Cache hygiene: purged {repaired_poison} poisoned umbrella ETF entries, "
            f"{pruned_unattempted} unattempted placeholder entries.",
            flush=True,
        )

    # Apply manual overrides
    for c, ov in MANUAL_CUSIP_OVERRIDES.items():
        entry = cache.get(c, {})
        changed_ticker = entry.get("ticker") != ov["ticker"]
        entry["ticker"] = ov["ticker"]
        entry["name"] = ov["name"]
        entry.pop("unpriced", None)
        entry.pop("resolvedFail", None)
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

    # Invalidate previously mangled foreign-exchange hyphen tickers (-HM, -TI, -SG, -SW, etc.) on active CUSIPs
    repaired_bad = 0
    for c in cusip_to_issuer:
        if c in MANUAL_CUSIP_OVERRIDES:
            continue
        ent = cache.get(c)
        if ent:
            t_cur = ent.get("ticker", "")
            if t_cur and not t_cur.startswith("CUSIP:") and _MALFORMED_FOREIGN_TICKER_RE.search(t_cur):
                cache.pop(c, None)
                repaired_bad += 1
    if repaired_bad:
        print(f"[resolver] Scheduled repair for {repaired_bad} malformed foreign-suffix CUSIP entries.", flush=True)

    # Build unambiguous cached 6-char CUSIP issuer root -> ticker & norm issuer name -> ticker for synthetic option CUSIPs
    prefix6_tickers: dict[str, set[str]] = {}
    norm_issuer_tickers: dict[str, set[str]] = {}
    for c_k, ent_k in cache.items():
        tk_k = str(ent_k.get("ticker") or "")
        nm_k = str(ent_k.get("name") or "")
        if not tk_k or tk_k.startswith("CUSIP:") or is_non_stock_etf(tk_k, nm_k, ent_k.get("sector", ""), ent_k.get("industry", "")):
            continue
        if _UMBRELLA_ETF_ISSUER_RE.search(nm_k):
            continue
        if len(c_k) >= 6:
            prefix6_tickers.setdefault(c_k[:6], set()).add(tk_k)
        nn = _normalize_legal_name(nm_k)
        if len(nn) >= 5:
            norm_issuer_tickers.setdefault(nn, set()).add(tk_k)
    unique_p6 = {p: next(iter(s)) for p, s in prefix6_tickers.items() if len(s) == 1}
    unique_norm_cache = {n: next(iter(s)) for n, s in norm_issuer_tickers.items() if len(s) == 1}

    missing_ticker = [c for c in cusip_to_issuer if c not in cache or not cache[c].get("ticker")]
    if cusip_priority:
        missing_ticker.sort(key=lambda c: cusip_priority.get(c, 0.0), reverse=True)
    print(f"[resolver] Total CUSIPs: {len(cusip_to_issuer)}, missing ticker resolution: {len(missing_ticker)}", flush=True)

    dust_placeholders: dict[str, dict[str, Any]] = {}

    if missing_ticker:
        # Step 1a-0: Instant offline exact SEC normalized company_tickers.json match + synthetic option CUSIP resolution
        after_sec: list[str] = []
        sec_hits = 0
        opt_hits = 0
        for c in missing_ticker:
            iss_str = issuer_by_c.get(c, "")
            is_umbrella = bool(_UMBRELLA_ETF_ISSUER_RE.search(iss_str))
            norm_iss = _normalize_legal_name(iss_str) if not is_umbrella else ""
            sym = sec_name_map.get(norm_iss) if norm_iss else None
            if not sym and not is_umbrella:
                # Try synthetic 90x/95x option or shared-root CUSIP when normalized issuer name matches
                cand_p6 = unique_p6.get(c[:6]) if len(c) >= 6 else None
                cand_nm = unique_norm_cache.get(norm_iss) if norm_iss else None
                if cand_nm and (cand_p6 is None or cand_p6 == cand_nm):
                    sym = cand_nm
                    opt_hits += 1
                elif cand_p6 and len(c) == 9 and c[6:8] in ("90", "95"):
                    sym = cand_p6
                    opt_hits += 1
            if sym:
                cache[c] = {
                    "ticker": sym,
                    "name": iss_str or sym,
                    "sector": ticker_meta.get(sym, {}).get("sector", ""),
                    "industry": ticker_meta.get(sym, {}).get("industry", ""),
                }
                sec_hits += 1
            else:
                after_sec.append(c)
        if sec_hits:
            print(f"[resolver] Offline SEC/synthetic matcher resolved {sec_hits} CUSIPs ({opt_hits} option/root matches).", flush=True)

        # Step 1a: Fast parallel strict-US Yahoo CUSIP lookup (materiality-capped so tail dust does not stall Stage 2)
        if max_fast_us is not None:
            fast_limit = max(0, max_fast_us)
        elif cusip_priority is not None and len(after_sec) > 800:
            fast_limit = 800
        else:
            fast_limit = len(after_sec)

        fast_queue = after_sec[:fast_limit]
        figi_candidates: list[str] = list(after_sec[fast_limit:])
        if figi_candidates:
            print(
                f"[resolver] Capping Fast US lookup to top {len(fast_queue)}/{len(after_sec)} material CUSIPs; "
                f"{len(figi_candidates)} deferred to OpenFIGI/fallback.",
                flush=True,
            )

        if len(fast_queue) > 25:
            def _fast_us_lookup(c: str) -> tuple[str, dict[str, Any] | None]:
                return c, _yahoo_search(c, strict_us_cusip=True)

            resolved_fast = 0
            with ThreadPoolExecutor(max_workers=16) as ex:
                futs = [ex.submit(_fast_us_lookup, c) for c in fast_queue]
                for idx_done, fut in enumerate(as_completed(futs), 1):
                    c, hit = fut.result()
                    if hit and hit.get("ticker"):
                        t_norm = normalize_yf_ticker(hit["ticker"])
                        if t_norm in _POISON_PRONE_ETF_TICKERS and t_norm not in title_by_c.get(c, "").upper():
                            figi_candidates.append(c)
                            continue
                        sec_val = hit.get("sector") or ("Technology" if t_norm in TECH_TMT_EXTRA_TICKERS else "Other")
                        cache[c] = {
                            "ticker": t_norm,
                            "name": hit.get("name") or issuer_by_c.get(c, c),
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
                    if idx_done % 400 == 0:
                        save_json_dict(CUSIP_CACHE_PATH, cache)
                        save_json_dict(TICKER_META_PATH, ticker_meta)
                        print(f"[resolver] Fast US lookup progress {idx_done}/{len(fast_queue)} ({resolved_fast} hits)...", flush=True)
            save_json_dict(CUSIP_CACHE_PATH, cache)
            save_json_dict(TICKER_META_PATH, ticker_meta)
            print(f"[resolver] Fast US lookup resolved {resolved_fast}/{len(fast_queue)} CUSIPs; {len(figi_candidates)} queued for OpenFIGI", flush=True)
        else:
            figi_candidates = list(after_sec)

        if cusip_priority and figi_candidates:
            figi_candidates.sort(key=lambda c: cusip_priority.get(c, 0.0), reverse=True)

        # Step 1b: Authoritative OpenFIGI US mapping (scales cleanly when OPENFIGI_API_KEY is set)
        if _openfigi_key and (max_openfigi_batches is None or max_openfigi_batches < 100):
            eff_figi_batches = 200
        elif max_openfigi_batches is None:
            env_cap = os.environ.get("MAX_OPENFIGI_BATCHES", "40").strip()
            eff_figi_batches = int(env_cap) if env_cap.isdigit() else 40
        else:
            eff_figi_batches = max_openfigi_batches

        figi_limit = eff_figi_batches * 10
        if len(figi_candidates) > figi_limit:
            print(f"[resolver] Capping OpenFIGI to top {figi_limit}/{len(figi_candidates)} most material CUSIPs ({eff_figi_batches} batches); remaining queued for issuer fallback.", flush=True)
            overflow = figi_candidates[figi_limit:]
            figi_candidates = figi_candidates[:figi_limit]
        else:
            overflow = []

        still_missing: list[str] = list(overflow)
        total_batches = (len(figi_candidates) + 9) // 10
        sleep_cycle = 0.30 if _openfigi_key else 2.50
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
                        "name": best.get("name") or issuer_by_c.get(c, c),
                        "sector": cache.get(c, {}).get("sector", ""),
                        "industry": cache.get(c, {}).get("industry", ""),
                    }
                else:
                    still_missing.append(c)
            if batch_num % 10 == 0 or batch_num == total_batches:
                save_json_dict(CUSIP_CACHE_PATH, cache)
                print(f"[resolver] OpenFIGI batch {batch_num}/{total_batches} saved ({len(cache)} cached)", flush=True)
            if i + 10 < len(figi_candidates):
                elapsed = time.monotonic() - t0
                if elapsed < sleep_cycle:
                    time.sleep(sleep_cycle - elapsed)
        save_json_dict(CUSIP_CACHE_PATH, cache)

        # Step 2: US-exchange-only Yahoo issuer-name fallback for top 400 material CUSIPs
        # Unattempted tail is kept ONLY in-memory so cusip_cache.json is never poisoned with false negatives.
        if still_missing:
            if cusip_priority:
                still_missing.sort(key=lambda c: cusip_priority.get(c, 0.0), reverse=True)
            http_fallback = still_missing[:400]
            dust_tail = still_missing[400:]
            if dust_tail:
                print(f"[resolver] Falling back to US Yahoo issuer search for top {len(http_fallback)}/{len(still_missing)} CUSIPs ({len(dust_tail)} tail kept uncached for retry)...", flush=True)
                for c in dust_tail:
                    raw_name = issuer_by_c.get(c, "")
                    fallback_sym = re.sub(r"[^A-Z0-9]", "", raw_name.upper())[:6] or c[:6]
                    dust_placeholders[c] = {
                        "ticker": f"CUSIP:{c}",
                        "displaySymbol": fallback_sym,
                        "name": raw_name or c,
                        "sector": "Other",
                        "industry": "",
                        "isTechTmt": False,
                        "unpriced": True,
                    }
            else:
                print(f"[resolver] Falling back to US Yahoo issuer search for {len(http_fallback)} CUSIPs...", flush=True)

            def _resolve_one(c: str) -> tuple[str, dict[str, Any]]:
                raw_name = issuer_by_c.get(c, "")
                raw_title = title_by_c.get(c, "")
                query = _build_yahoo_fallback_query(raw_name, raw_title)
                hit = _yahoo_search(query, us_only=True) if len(query) >= 3 else None
                if hit and hit.get("ticker"):
                    t_norm = normalize_yf_ticker(hit["ticker"])
                    if t_norm not in _POISON_PRONE_ETF_TICKERS or t_norm in f"{raw_name} {raw_title}".upper():
                        sec_val = hit.get("sector") or ("Technology" if t_norm in TECH_TMT_EXTRA_TICKERS else "Other")
                        return c, {
                            "ticker": t_norm,
                            "name": hit.get("name") or raw_name or c,
                            "sector": sec_val,
                            "industry": hit.get("industry") or "",
                        }
                fallback_sym = re.sub(r"[^A-Z0-9]", "", raw_name.upper())[:6] or c[:6]
                return c, {
                    "ticker": f"CUSIP:{c}",
                    "displaySymbol": fallback_sym,
                    "name": raw_name or c,
                    "sector": "Other",
                    "industry": "",
                    "unpriced": True,
                    "resolvedFail": True,
                }

            with ThreadPoolExecutor(max_workers=12) as ex:
                futs = [ex.submit(_resolve_one, c) for c in http_fallback]
                for idx_done, fut in enumerate(as_completed(futs), 1):
                    c, entry = fut.result()
                    cache[c] = entry
                    if idx_done % 150 == 0:
                        save_json_dict(CUSIP_CACHE_PATH, cache)
                        print(f"[resolver] Issuer fallback progress {idx_done}/{len(http_fallback)}...", flush=True)
            save_json_dict(CUSIP_CACHE_PATH, cache)

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

        with ThreadPoolExecutor(max_workers=12) as ex:
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
        is_etf = is_non_stock_etf(t, entry.get("name", ""), entry.get("sector", ""), entry.get("industry", ""))
        entry["isNonStockEtf"] = is_etf
        entry["isTechTmt"] = False if is_etf else is_tech_tmt(t, entry.get("sector", ""), entry.get("industry", ""))

    save_json_dict(CUSIP_CACHE_PATH, cache)
    save_json_dict(TICKER_META_PATH, ticker_meta)

    if dust_placeholders:
        out_map = dict(cache)
        out_map.update(dust_placeholders)
        return out_map
    return cache


def fetch_daily_prices(ticker: str, start_ts: int = 1609459200, max_age_hours: float = 20.0) -> dict[str, float] | None:
    """
    Fetch daily split- and dividend-adjusted close prices (YYYY-MM-DD -> adjclose)
    from Yahoo Finance v8 chart API.
    Simultaneously saves split-adjusted OHLCV [adjclose, adjlow, adjhigh, volume]
    to cache/prices/ohlcv_<ticker>.json so Quarter VWAP & Entry Zone metrics add zero requests.
    """
    if not ticker or ticker.startswith("CUSIP:"):
        return None
    safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", ticker)
    cpath = PRICE_CACHE_DIR / f"{safe_name}.json"
    opath = PRICE_CACHE_DIR / f"ohlcv_{safe_name}.json"
    if cpath.exists() and opath.exists():
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
                break
            timestamps = result.get("timestamp") or []
            ind = result.get("indicators", {})
            q_block = (ind.get("quote") or [{}])[0]
            adj_list = (ind.get("adjclose") or [{}])[0].get("adjclose")
            close_list = q_block.get("close") or []
            low_list = q_block.get("low") or []
            high_list = q_block.get("high") or []
            vol_list = q_block.get("volume") or []
            prices_raw = adj_list if adj_list else close_list
            if not timestamps or not prices_raw:
                break
            out: dict[str, float] = {}
            ohlcv_out: dict[str, list[float]] = {}
            n_pts = len(timestamps)
            for idx_t in range(n_pts):
                px = prices_raw[idx_t]
                if px is None or px <= 0:
                    continue
                dt = datetime.fromtimestamp(timestamps[idx_t], tz=timezone.utc).strftime("%Y-%m-%d")
                adj_c = float(px)
                out[dt] = adj_c

                raw_c = close_list[idx_t] if idx_t < len(close_list) else None
                raw_l = low_list[idx_t] if idx_t < len(low_list) else None
                raw_h = high_list[idx_t] if idx_t < len(high_list) else None
                raw_v = vol_list[idx_t] if idx_t < len(vol_list) else None
                factor = (adj_c / float(raw_c)) if (raw_c and float(raw_c) > 0) else 1.0
                adj_l = float(raw_l) * factor if (raw_l and float(raw_l) > 0) else adj_c
                adj_h = float(raw_h) * factor if (raw_h and float(raw_h) > 0) else adj_c
                vol = float(raw_v) if (raw_v and float(raw_v) >= 0) else 0.0
                ohlcv_out[dt] = [round(adj_c, 4), round(min(adj_l, adj_c), 4), round(max(adj_h, adj_c), 4), round(vol, 0)]

            if out:
                cpath.write_text(json.dumps(out), encoding="utf-8")
                opath.write_text(json.dumps(ohlcv_out, separators=(",", ":")), encoding="utf-8")
                return out
            break
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(2.0 * (attempt + 1))
                continue
            if e.code in (400, 404):
                break
            time.sleep(1.0)
        except Exception:
            if attempt < 2:
                time.sleep(0.8)
                continue
            break

    cpath.write_text("{}", encoding="utf-8")
    opath.write_text("{}", encoding="utf-8")
    return None


def fetch_all_prices(tickers: list[str]) -> dict[str, dict[str, float]]:
    clean_tickers = sorted({t for t in tickers if t and not t.startswith("CUSIP:")})
    print(f"[prices] Fetching daily adjusted prices & OHLCV for {len(clean_tickers)} tickers...", flush=True)
    prices: dict[str, dict[str, float]] = {}
    with ThreadPoolExecutor(max_workers=16) as ex:
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


def load_cached_ohlcv(tickers: list[str]) -> dict[str, dict[str, list[float]]]:
    """Load split-adjusted OHLCV series ([adjclose, adjlow, adjhigh, volume]) cached alongside prices."""
    out: dict[str, dict[str, list[float]]] = {}
    for t in tickers:
        if not t or t.startswith("CUSIP:"):
            continue
        safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", t)
        opath = PRICE_CACHE_DIR / f"ohlcv_{safe_name}.json"
        if opath.exists():
            try:
                data = json.loads(opath.read_text(encoding="utf-8"))
                if data:
                    out[t] = data
            except Exception:
                pass
    return out
