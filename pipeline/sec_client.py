"""
SEC EDGAR client for fetching 13F-HR / 13F-HR/A filings
and quarterly EFTS discovery of emerging concentrated tech funds.
"""

from __future__ import annotations

import json
import os
import statistics
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from lxml import etree

_DEFAULT_SEC_UA = "PaperTrail13FResearch contact@papertrail13f.dev"
_env_sec_ua = os.environ.get("SEC_USER_AGENT", "").strip()
if not _env_sec_ua or "noreply.github.com" in _env_sec_ua.lower():
    _env_sec_ua = _DEFAULT_SEC_UA

SEC_HEADERS = {
    "User-Agent": _env_sec_ua,
    "Accept-Encoding": "gzip, deflate",
}

CACHE_DIR = Path(__file__).resolve().parent / "cache" / "sec"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

_rate_lock = threading.Lock()
_last_req_ts = 0.0


def _throttle(min_interval: float = 0.13) -> None:
    global _last_req_ts
    with _rate_lock:
        now = time.monotonic()
        wait = (_last_req_ts + min_interval) - now
        if wait > 0:
            time.sleep(wait)
        _last_req_ts = time.monotonic()


def fetch_bytes(url: str, cache_key: str | None = None, max_age_hours: float = 24 * 365) -> bytes:
    if cache_key:
        cpath = CACHE_DIR / cache_key
        if cpath.exists():
            age_hours = (time.time() - cpath.stat().st_mtime) / 3600.0
            if age_hours <= max_age_hours:
                return cpath.read_bytes()

    for attempt in range(4):
        _throttle(0.12)
        req = urllib.request.Request(url, headers=SEC_HEADERS)
        try:
            with urllib.request.urlopen(req, timeout=25) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    import gzip
                    raw = gzip.decompress(raw)
            if cache_key:
                cpath = CACHE_DIR / cache_key
                cpath.parent.mkdir(parents=True, exist_ok=True)
                cpath.write_bytes(raw)
            return raw
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < 3:
                time.sleep(1.2 * (attempt + 1))
                continue
            if e.code == 403 and attempt < 2:
                time.sleep(2.5 * (attempt + 1))
                continue
            raise RuntimeError(f"HTTP {e.code} {e.reason} for {url}") from e
        except Exception:
            if attempt < 3:
                time.sleep(1.0 * (attempt + 1))
                continue
            raise
    raise RuntimeError(f"Failed to fetch {url}")


def fetch_json(url: str, cache_key: str | None = None, max_age_hours: float = 24 * 365) -> Any:
    return json.loads(fetch_bytes(url, cache_key=cache_key, max_age_hours=max_age_hours).decode("utf-8"))


def get_submissions(cik: str) -> dict[str, Any]:
    cik10 = f"{int(cik):010d}"
    # Refresh submissions every hour so deadline-day 13F filings are picked up promptly
    return fetch_json(
        f"https://data.sec.gov/submissions/CIK{cik10}.json",
        cache_key=f"sub_{cik10}.json",
        max_age_hours=1.0,
    )


def list_13f_filings(cik: str, min_report_date: str = "2020-12-31") -> list[dict[str, Any]]:
    sub = get_submissions(cik)
    entity_name = sub.get("name", "")
    recent = sub.get("filings", {}).get("recent", {})

    rows: list[dict[str, Any]] = []

    def _collect(block: dict[str, list[Any]]) -> None:
        forms = block.get("form", [])
        accs = block.get("accessionNumber", [])
        docs = block.get("primaryDocument", [])
        rds = block.get("reportDate", [])
        fds = block.get("filingDate", [])
        for form, acc, doc, rd, fd in zip(forms, accs, docs, rds, fds):
            if form in ("13F-HR", "13F-HR/A") and rd and rd >= min_report_date:
                rows.append({
                    "cik": f"{int(cik):010d}",
                    "entityName": entity_name,
                    "form": form,
                    "accessionNumber": acc,
                    "primaryDocument": doc,
                    "reportDate": rd,
                    "filingDate": fd,
                })

    _collect(recent)

    # Also check older archive pagination files if recent doesn't go back far enough
    oldest_recent = min(recent.get("reportDate", ["9999-12-31"]) or ["9999-12-31"])
    if oldest_recent > min_report_date:
        for fmeta in sub.get("filings", {}).get("files", []):
            if fmeta.get("filingTo", "9999-12-31") >= min_report_date:
                fname = fmeta["name"]
                extra = fetch_json(
                    f"https://data.sec.gov/submissions/{fname}",
                    cache_key=f"sub_extra_{fname}",
                    max_age_hours=24 * 30,
                )
                _collect(extra)

    rows.sort(key=lambda x: (x["reportDate"], x["filingDate"], x["accessionNumber"]))
    return rows


def _parse_xml_root(xml_bytes: bytes) -> etree._Element:
    parser = etree.XMLParser(recover=True, huge_tree=True)
    return etree.fromstring(xml_bytes, parser=parser)


def _find_text(el: etree._Element, local_name: str, default: str = "") -> str:
    found = el.xpath(f".//*[local-name()='{local_name}']")
    if found and found[0].text:
        return found[0].text.strip()
    return default


def parse_13f_filing(filing: dict[str, Any]) -> dict[str, Any] | None:
    cik_int = int(filing["cik"])
    acc_nd = filing["accessionNumber"].replace("-", "")
    idx_url = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_nd}/index.json"
    idx = fetch_json(idx_url, cache_key=f"idx_{filing['cik']}_{acc_nd}.json")
    items = [it["name"] for it in idx.get("directory", {}).get("item", [])]

    xml_files = [fn for fn in items if fn.lower().endswith(".xml") and not fn.lower().startswith("xsl")]
    if not xml_files:
        return None

    primary_doc = None
    info_doc = None
    primary_base = (filing.get("primaryDocument") or "").split("/")[-1].lower()
    if filing["form"] == "13F-HR" and primary_base:
        candidate_xmls = [fn for fn in xml_files if fn.lower() != primary_base] or xml_files
    else:
        candidate_xmls = xml_files

    for fn in candidate_xmls:
        raw = fetch_bytes(
            f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_nd}/{fn}",
            cache_key=f"xml_{filing['cik']}_{acc_nd}_{fn}",
        )
        root = _parse_xml_root(raw)
        local_root = root.tag.split("}")[-1].lower()
        if local_root == "edgarsubmission":
            primary_doc = root
        elif local_root == "informationtable":
            info_doc = root
            if filing["form"] == "13F-HR":
                break

    if info_doc is None:
        return None

    amendment_type = ""
    if primary_doc is not None:
        amendment_type = _find_text(primary_doc, "amendmentType", "").upper()

    raw_rows: list[dict[str, Any]] = []
    ratios: list[float] = []

    for row_el in info_doc.xpath("./*[local-name()='infoTable']"):
        issuer = _find_text(row_el, "nameOfIssuer")
        title = _find_text(row_el, "titleOfClass")
        cusip = _find_text(row_el, "cusip").upper().zfill(9)[:9]
        val_str = _find_text(row_el, "value", "0")
        sh_str = _find_text(row_el, "sshPrnamt", "0")
        sh_type = _find_text(row_el, "sshPrnamtType", "SH").upper()
        put_call = _find_text(row_el, "putCall", "").upper()

        try:
            val = float(val_str.replace(",", ""))
        except ValueError:
            val = 0.0
        try:
            shares = float(sh_str.replace(",", ""))
        except ValueError:
            shares = 0.0

        if not cusip or val <= 0:
            continue

        sec_type = "EQUITY"
        if put_call in ("PUT", "CALL"):
            sec_type = put_call
        elif sh_type == "PRN":
            sec_type = "NOTE"

        if sec_type == "EQUITY" and shares > 0:
            ratios.append(val / shares)

        raw_rows.append({
            "cusip": cusip,
            "issuer": issuer,
            "title": title,
            "rawValue": val,
            "shares": shares,
            "secType": sec_type,
        })

    if not raw_rows:
        return None

    # SEC switched 13F value units from $1,000s to $1 on Jan 3, 2023.
    # Verify via both filingDate and median(value / shares) for plain equity rows.
    is_thousands = filing["filingDate"] < "2023-01-03"
    if ratios:
        med = statistics.median(ratios)
        if med < 2.5:
            is_thousands = True
        elif med >= 3.0 and filing["filingDate"] >= "2023-01-03":
            is_thousands = False

    mult = 1000.0 if is_thousands else 1.0

    # Aggregate multiple sub-manager lines with identical (cusip, secType)
    agg: dict[tuple[str, str], dict[str, Any]] = {}
    for r in raw_rows:
        key = (r["cusip"], r["secType"])
        val_usd = r["rawValue"] * mult
        if key not in agg:
            agg[key] = {
                "cusip": r["cusip"],
                "issuer": r["issuer"],
                "title": r["title"],
                "secType": r["secType"],
                "valueUsd": val_usd,
                "shares": r["shares"],
            }
        else:
            agg[key]["valueUsd"] += val_usd
            agg[key]["shares"] += r["shares"]

    return {
        "cik": filing["cik"],
        "entityName": filing["entityName"],
        "form": filing["form"],
        "amendmentType": amendment_type,
        "reportDate": filing["reportDate"],
        "filingDate": filing["filingDate"],
        "accessionNumber": filing["accessionNumber"],
        "rows": list(agg.values()),
    }


def build_clean_quarterly_filings(cik: str, min_report_date: str = "2020-12-31") -> list[dict[str, Any]]:
    """
    Fetch and merge 13F-HR and 13F-HR/A filings into one authoritative snapshot per reportDate.
    """
    filings = list_13f_filings(cik, min_report_date=min_report_date)
    if not filings:
        return []

    with ThreadPoolExecutor(max_workers=6) as ex:
        parsed_list = list(ex.map(parse_13f_filing, filings))

    by_period: dict[str, list[dict[str, Any]]] = {}
    for parsed in parsed_list:
        if parsed:
            by_period.setdefault(parsed["reportDate"], []).append(parsed)

    cleaned: list[dict[str, Any]] = []
    for period in sorted(by_period.keys()):
        group = sorted(by_period[period], key=lambda x: (x["filingDate"], x["accessionNumber"]))
        merged: dict[tuple[str, str], dict[str, Any]] = {}
        meta = None
        for parsed in group:
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
                # NEW HOLDINGS confidential-treatment amendment adds to original
                for r in parsed["rows"]:
                    k = (r["cusip"], r["secType"])
                    if k not in merged:
                        merged[k] = dict(r)
                    else:
                        merged[k]["valueUsd"] += r["valueUsd"]
                        merged[k]["shares"] += r["shares"]
        if meta and merged:
            meta["rows"] = sorted(merged.values(), key=lambda x: x["valueUsd"], reverse=True)
            cleaned.append(meta)
    return cleaned


def discover_emerging_tech_funds(known_ciks: set[str], max_candidates: int = 12) -> list[dict[str, Any]]:
    """
    Scan recent SEC EDGAR 13F-HR filings for emerging concentrated funds holding
    flagship AI/cloud/semi names that are not yet in the curated list.
    """
    # Search for filings mentioning NVIDIA (67066G104) + Microsoft (594918104) or CrowdStrike (22788C105)
    # in the most recent ~120 days
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    start_dt = (now - timedelta(days=120)).strftime("%Y-%m-%d")
    end_dt = now.strftime("%Y-%m-%d")

    exclude_keywords = (
        "bank", "trust", "wealth", "advisors, llc", "retirement", "insurance",
        "pension", "university", "foundation", "bnp", "jpmorgan", "morgan stanley",
        "goldman", "wells fargo", "ubs", "barclays", "citigroup", "royal bank",
        "state street", "blackrock", "vanguard", "invesco", "schwab", "fidelity",
        "northern trust", "bny", "ameriprise", "raymond james", "edward jones",
        "stifel", "lpl", "merrill", "hsbc", "nomura", "credit suisse", "deutsche",
        "pacific life", "prudential", "metlife", "manulife", "allianz", "axa",
    )

    discovered_ciks: list[tuple[str, str]] = []
    seen = set(known_ciks)

    # Queries pairing high-conviction modern tech stocks (NVDA + CRWD, NVDA + PLTR, MSFT + AVGO)
    queries = [
        '"67066G104" "22788C105"',   # NVDA + CRWD
        '"67066G104" "69608A108"',   # NVDA + PLTR
        '"67066G104" "11135F101"',   # NVDA + AVGO
    ]

    for q_raw in queries:
        q = urllib.parse.quote(q_raw)
        url = (
            f"https://efts.sec.gov/LATEST/search-index?forms=13F-HR&q={q}"
            f"&dateRange=custom&startdt={start_dt}&enddt={end_dt}"
        )
        try:
            data = fetch_json(url, cache_key=f"efts_{abs(hash(q_raw))}_{end_dt}.json", max_age_hours=12)
        except Exception:
            continue

        for hit in data.get("hits", {}).get("hits", []):
            src = hit.get("_source", {})
            ciks = src.get("ciks", [])
            names = src.get("display_names", [])
            for cik, dname in zip(ciks, names):
                cik10 = f"{int(cik):010d}"
                if cik10 in seen:
                    continue
                low = dname.lower()
                if any(kw in low for kw in exclude_keywords):
                    continue
                clean_name = dname.split("(CIK")[0].strip()
                seen.add(cik10)
                discovered_ciks.append((cik10, clean_name))
                if len(discovered_ciks) >= max_candidates * 3:
                    break
            if len(discovered_ciks) >= max_candidates * 3:
                break

    # Quick pre-screen via submissions + latest primary_doc tableEntryTotal / tableValueTotal
    shortlist: list[dict[str, Any]] = []
    for cik10, cname in discovered_ciks:
        try:
            filings = list_13f_filings(cik10, min_report_date="2024-01-01")
            if len(filings) < 4:
                continue
            latest = filings[-1]
            acc_nd = latest["accessionNumber"].replace("-", "")
            cik_int = int(cik10)
            idx = fetch_json(
                f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_nd}/index.json",
                cache_key=f"idx_{cik10}_{acc_nd}.json",
            )
            items = [it["name"] for it in idx.get("directory", {}).get("item", [])]
            pdoc = next((fn for fn in items if fn.lower() == "primary_doc.xml"), None)
            if pdoc:
                raw = fetch_bytes(
                    f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_nd}/{pdoc}",
                    cache_key=f"xml_{cik10}_{acc_nd}_{pdoc}",
                )
                root = _parse_xml_root(raw)
                cnt = int(float(_find_text(root, "tableEntryTotal", "0") or 0))
                val = float(_find_text(root, "tableValueTotal", "0") or 0)
                # Concentrated ($250M+ AUM, 8-85 line items)
                if 8 <= cnt <= 85 and val >= 250_000_000:
                    import re
                    words = cname.split()
                    fmt_words = []
                    for w in words:
                        up = w.upper().rstrip(",.")
                        trail = w[len(w.rstrip(",.")):]
                        if up in ("LLC", "LP", "LLP", "LTD", "INC", "CO", "CORP"):
                            base = "Ltd." if up == "LTD" else "L.P." if up == "LP" else "Inc." if up == "INC" else up
                            fmt_w = base + ("" if base.endswith(".") and trail == "." else trail)
                        elif len(up) <= 3 and w.isupper() and up not in ("THE", "AND", "FOR"):
                            fmt_w = up + trail
                        else:
                            fmt_w = w.title()
                        fmt_words.append(fmt_w)
                    fmt_name = re.sub(r"\s+,", ",", " ".join(fmt_words)).strip(" ,")
                    shortlist.append({
                        "cik": cik10,
                        "name": fmt_name,
                        "manager": "SEC EDGAR Discovered",
                        "hq": "United States",
                        "styleTag": "Discovered Tech Concentrated",
                        "strategySummary": f"Automatically surfaced from SEC EDGAR quarterly 13F scan ({cnt} filing entries, high AI/tech concentration).",
                        "source": "EDGAR Discovered",
                    })
                    if len(shortlist) >= max_candidates:
                        break
        except Exception:
            continue
    return shortlist
