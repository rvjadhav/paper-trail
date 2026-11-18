"""
SEC EDGAR client for fetching and parsing 13F-HR / 13F-HR/A filings.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
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

WARRANT_TITLE_RE = re.compile(
    r"(\*W|WARRANT|WT\b|RIGHT|RTS\b|NOTE|DBCV|CONV|DEBT|BOND|PREF|PFD|UNIT)",
    re.I,
)


def detect_is_thousands(
    filing_date: str,
    eq_ratios: list[float],
    fallback_ratios: list[float] | None = None,
) -> bool:
    """
    Determine whether SEC Form 13F INFOTABLE value rows are reported in $1,000s vs $1.
    Uses plain common/ADR equity ratios (excluding options, PRN debt, and warrants/rights/units)
    so SPAC-warrant holders (e.g. $0.20 warrants) or convertible-note filers are not misclassified.
    """
    is_thousands = filing_date < "2023-01-03"
    rs = sorted(r for r in eq_ratios if r > 0)
    if rs:
        n = len(rs)
        med = rs[n // 2]
        if filing_date < "2023-01-03":
            if n >= 3 and med >= 3.0:
                is_thousands = False
            else:
                is_thousands = True
        else:
            p75 = rs[int(n * 0.75)] if n >= 4 else rs[-1]
            p90 = rs[min(n - 1, int(n * 0.90))]
            if n >= 5:
                is_thousands = med < 0.75 and p75 < 1.50 and p90 < 3.0
            else:
                is_thousands = med < 0.60 and rs[-1] < 1.20
    elif fallback_ratios and filing_date >= "2023-01-03":
        rs_fb = sorted(r for r in fallback_ratios if r > 0)
        if rs_fb and len(rs_fb) >= 5:
            n_fb = len(rs_fb)
            if rs_fb[n_fb // 2] < 0.15 and rs_fb[int(n_fb * 0.75)] < 0.35 and rs_fb[int(n_fb * 0.90)] < 0.80:
                is_thousands = True
    return is_thousands


_rate_lock = threading.Lock()
_last_req_ts = 0.0


def _throttle(min_interval: float = 0.105) -> None:
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
        _throttle(0.105)
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
    # Refresh submissions twice daily so filings stay fresh across multi-stage runs
    return fetch_json(
        f"https://data.sec.gov/submissions/CIK{cik10}.json",
        cache_key=f"sub_{cik10}.json",
        max_age_hours=12.0,
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
    recent_fds = [d for d in recent.get("filingDate", []) if d]
    oldest_recent = min(recent_fds) if recent_fds else "9999-12-31"
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


def parse_xml_info_table(
    info_doc: etree._Element,
    filing: dict[str, Any],
    amendment_type: str = "",
) -> dict[str, Any] | None:
    raw_rows: list[dict[str, Any]] = []
    eq_ratios: list[float] = []
    fallback_ratios: list[float] = []

    for row_el in info_doc:
        if not isinstance(row_el.tag, str) or row_el.tag.rsplit("}", 1)[-1] != "infoTable":
            continue
        issuer = ""
        title = ""
        cusip = ""
        val_str = "0"
        sh_str = "0"
        sh_type = "SH"
        put_call = ""
        for sub in row_el.iter():
            if not isinstance(sub.tag, str):
                continue
            lname = sub.tag.rsplit("}", 1)[-1]
            txt = sub.text.strip() if sub.text else ""
            if not txt:
                continue
            if lname == "nameOfIssuer" and not issuer:
                issuer = txt
            elif lname == "titleOfClass" and not title:
                title = txt
            elif lname == "cusip" and not cusip:
                cusip = txt.upper().zfill(9)[:9]
            elif lname == "value":
                val_str = txt
            elif lname == "sshPrnamt":
                sh_str = txt
            elif lname == "sshPrnamtType":
                sh_type = txt.upper()
            elif lname == "putCall":
                put_call = txt.upper()

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
            ratio = val / shares
            fallback_ratios.append(ratio)
            if not WARRANT_TITLE_RE.search(title):
                eq_ratios.append(ratio)

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

    is_thousands = detect_is_thousands(filing["filingDate"], eq_ratios, fallback_ratios)
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
        "entityName": filing.get("entityName", ""),
        "form": filing["form"],
        "amendmentType": amendment_type,
        "reportDate": filing["reportDate"],
        "filingDate": filing["filingDate"],
        "accessionNumber": filing["accessionNumber"],
        "rows": list(agg.values()),
    }


def parse_13f_filing(filing: dict[str, Any]) -> dict[str, Any] | None:
    cik_int = int(filing["cik"])
    acc_nd = filing["accessionNumber"].replace("-", "")
    parsed_cache_path = CACHE_DIR / f"parsed_v2_{filing['cik']}_{acc_nd}.json"
    if parsed_cache_path.exists():
        try:
            cached = json.loads(parsed_cache_path.read_text(encoding="utf-8"))
            if cached.get("empty"):
                return None
            cached["entityName"] = filing.get("entityName", "") or cached.get("entityName", "")
            return cached
        except Exception:
            pass

    idx_url = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_nd}/index.json"
    idx = fetch_json(idx_url, cache_key=f"idx_{filing['cik']}_{acc_nd}.json")
    items = [it["name"] for it in idx.get("directory", {}).get("item", [])]

    xml_files = [fn for fn in items if fn.lower().endswith(".xml") and not fn.lower().startswith("xsl")]
    if not xml_files:
        parsed_cache_path.write_text('{"empty":true}', encoding="utf-8")
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
        local_root = root.tag.rsplit("}", 1)[-1].lower()
        if local_root == "edgarsubmission":
            primary_doc = root
        elif local_root == "informationtable":
            info_doc = root
            if filing["form"] == "13F-HR":
                break

    if info_doc is None:
        parsed_cache_path.write_text('{"empty":true}', encoding="utf-8")
        return None

    amendment_type = ""
    if primary_doc is not None:
        amendment_type = _find_text(primary_doc, "amendmentType", "").upper()

    res = parse_xml_info_table(info_doc, filing, amendment_type=amendment_type)
    if res is None:
        parsed_cache_path.write_text('{"empty":true}', encoding="utf-8")
        return None

    try:
        parsed_cache_path.write_text(json.dumps(res, separators=(",", ":")), encoding="utf-8")
    except Exception:
        pass
    return res


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
        meta: dict[str, Any] | None = None
        amendment_events: list[dict[str, Any]] = []
        for parsed in group:
            fdate = parsed["filingDate"]
            ftype = parsed["form"]
            atype = parsed["amendmentType"]
            if ftype == "13F-HR" or atype == "RESTATEMENT" or meta is None:
                merged = {}
                for r in parsed["rows"]:
                    row_copy = dict(r)
                    row_copy["disclosedDate"] = fdate
                    merged[(r["cusip"], r["secType"])] = row_copy
                meta = {
                    "cik": parsed["cik"],
                    "entityName": parsed["entityName"],
                    "form": ftype,
                    "amendmentType": atype,
                    "reportDate": parsed["reportDate"],
                    "filingDate": fdate,
                    "originalFilingDate": fdate,
                    "latestAmendmentDate": fdate,
                    "accessionNumber": parsed["accessionNumber"],
                }
                amendment_events = [{
                    "filingDate": fdate,
                    "form": ftype,
                    "amendmentType": atype,
                    "isReset": True,
                    "rows": [dict(r, disclosedDate=fdate) for r in parsed["rows"]],
                }]
            else:
                # NEW HOLDINGS confidential-treatment amendment adds chronologically on its own filingDate
                meta["latestAmendmentDate"] = max(meta.get("latestAmendmentDate", fdate), fdate)
                meta["amendmentType"] = atype or "NEW HOLDINGS"
                inc_rows: list[dict[str, Any]] = []
                for r in parsed["rows"]:
                    k = (r["cusip"], r["secType"])
                    row_copy = dict(r)
                    row_copy["disclosedDate"] = fdate
                    inc_rows.append(row_copy)
                    if k not in merged:
                        merged[k] = dict(row_copy)
                    else:
                        merged[k]["valueUsd"] += r["valueUsd"]
                        merged[k]["shares"] += r["shares"]
                amendment_events.append({
                    "filingDate": fdate,
                    "form": ftype,
                    "amendmentType": atype or "NEW HOLDINGS",
                    "isReset": False,
                    "rows": inc_rows,
                })
        if meta and merged:
            meta["rows"] = sorted(merged.values(), key=lambda x: x["valueUsd"], reverse=True)
            meta["amendmentEvents"] = amendment_events
            cleaned.append(meta)
    return cleaned


def format_entity_name(cname: str) -> str:
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
    return re.sub(r"\s+,", ",", " ".join(fmt_words)).strip(" ,")
