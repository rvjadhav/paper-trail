"""
Full-universe SEC Form 13F Bulk Data Sets + Post-Bulk Master Index ingestion.

Replaces EFTS/seed-pair biased discovery with deterministic evaluation of 100% of
SEC Form 13F institutional investment managers (~9,500+ CIKs) using:
  1. SEC quarterly Form 13F Data Sets (2021q1 - latest bulk ZIP, TSV tables)
  2. SEC EDGAR full-index master.idx for incremental post-bulk filings (e.g. Sep-Nov)
  3. Single-request complete submission .txt XML extraction for post-bulk filings
"""

from __future__ import annotations

import csv
import io
import json
import re
import time
import urllib.parse
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sec_client import (
    CACHE_DIR as SEC_CACHE_DIR,
    SEC_HEADERS,
    WARRANT_TITLE_RE,
    _find_text,
    _parse_xml_root,
    detect_is_thousands,
    fetch_bytes,
    fetch_json,
    format_entity_name,
    get_submissions,
    parse_13f_filing,
    parse_xml_info_table,
)

BULK_DIR = Path(__file__).resolve().parent / "cache" / "bulk"
BULK_DIR.mkdir(parents=True, exist_ok=True)
ENTITY_META_PATH = SEC_CACHE_DIR / "entity_meta_cache.json"


def _iter_tsv_rows(
    raw_stream: Any,
    wanted_cols: tuple[str, ...],
    filter_col: str | None = None,
    filter_values: set[str] | None = None,
) -> Any:
    """
    High-speed SEC bulk TSV row streamer that avoids per-row dict allocation across
    20M+ rows. Pre-filters by `filter_col` (typically ACCESSION_NUMBER at col 0) in C
    string ops before parsing the remaining fields.
    """
    text_stream = io.TextIOWrapper(raw_stream, encoding="utf-8", errors="replace")
    hdr_line = text_stream.readline()
    if not hdr_line:
        return
    headers = [h.strip() for h in hdr_line.rstrip("\r\n").split("\t")]
    col_idx = {name: i for i, name in enumerate(headers)}
    wanted_indices = [col_idx.get(c, -1) for c in wanted_cols]

    f_idx = col_idx.get(filter_col, -1) if (filter_col and filter_values is not None) else -1
    max_split = max([f_idx] + [i for i in wanted_indices if i >= 0] + [0]) + 1

    for line in text_stream:
        if f_idx == 0 and filter_values is not None:
            head, _, _ = line.partition("\t")
            key_val = head.strip()
            if key_val not in filter_values:
                continue

        if '"' in line:
            parts = next(csv.reader([line.rstrip("\r\n")], delimiter="\t"))
        else:
            parts = line.rstrip("\r\n").split("\t", max_split)

        if f_idx > 0 and filter_values is not None:
            f_val = parts[f_idx].strip() if f_idx < len(parts) else ""
            if f_val not in filter_values:
                continue

        n_parts = len(parts)
        yield tuple(parts[idx].strip() if 0 <= idx < n_parts else "" for idx in wanted_indices)

KNOWN_BULK_TAGS: list[str] = [
    "2021q1", "2021q2", "2021q3", "2021q4",
    "2022q1", "2022q2", "2022q3", "2022q4",
    "2023q1", "2023q2", "2023q3", "2023q4",
    "01jan2024-29feb2024",
    "01mar2024-31may2024",
    "01jun2024-31aug2024",
    "01sep2024-30nov2024",
    "01dec2024-28feb2025",
    "01mar2025-31may2025",
    "01jun2025-31aug2025",
    "01sep2025-30nov2025",
    "01dec2025-28feb2026",
    "01mar2026-31may2026",
    "01jun2026-31aug2026",
]

_MONTH_MAP = {
    "JAN": "01", "FEB": "02", "MAR": "03", "APR": "04",
    "MAY": "05", "JUN": "06", "JUL": "07", "AUG": "08",
    "SEP": "09", "OCT": "10", "NOV": "11", "DEC": "12",
}

_EXCLUDE_INSTITUTION_RE = re.compile(
    r"\b("
    r"banks?|banking|bancorp|bancshares|banco|banque|banca|banken|caisse|unibanco|"
    r"trust|wealth|fiduciary|financial advisor|financial adviser|financial planning|"
    r"financial services|financial counsel|private wealth|private bank|family wealth|"
    r"retirement|insurance|reinsurance|assurance|pensions?|tjanstepension|pensioen|"
    r"superannuation|pensionskasse|omsesidig|provident|kranot|"
    r"emerytalne|powszechne|towarzystwo|funduszy|forsakrings?|livforsakringsbolaget|"
    r"university|endowment|foundation|brokerage|credit union|savings|clearing|"
    r"massachusetts institute of|california institute of technology|caltech|regents of the university|"
    r"sovereign|leasing|capital markets|proprietary markets|proprietary trading|"
    r"market making|market maker|controlco|"
    r"ctc|chicago trading|marathon trading|walleye|group one|simplex|belvedere|peak6|"
    r"wolverine trading|wolverine asset|sumo capital|dash financial|akuna|optiver|"
    r"imc trading|drw|hudson river|tower research|jump trading|virtu|citadel securities|"
    r"jane street|susquehanna|cutler group|hap trading|ronin capital|"
    r"bnp|jpmorgan|morgan stanley|goldman sachs|wells fargo|ubs|barclays|"
    r"citigroup|citibank|royal bank|natwest|mufg|scotiabank|sumitomo|"
    r"mitsui|mitsubishi|mizuho|daiwa|jefferies financial|state street|"
    r"blackrock|vanguard|invesco|schwab|fidelity|northern trust|bny|mellon|"
    r"ameriprise|raymond james|edward jones|stifel|lpl|merrill|hsbc|nomura|"
    r"credit suisse|deutsche|bochk|norges|temasek|mubadala|korea investment|"
    r"british columbia|calstrs|calpers|teachers retirement|cibc|skandinaviska|"
    r"swedbank|commerzbank|rabobank|danske bank|handelsbanken|oversea-chinese banking|"
    r"quilter|rakuten|koch,?\s*inc|"
    r"pacific life|prudential|metlife|new york life|nippon life|asahi life|"
    r"allstate|chubb|travelers|aflac|aviva|aegon|zurich|manulife|allianz|axa|generali|"
    r"tiaa|geode|dimensional|nuveen|macquarie|amundi|natixis|schroder|"
    r"legal & general|univest|t\.?\s*rowe|"
    r"fmr llc|clear street|apex clearing|pershing|interactive brokers|"
    r"profund|proshares|rafferty asset|van\s?eck|krane funds|global x|neos investment|"
    r"assenagon|dnb asset|kbc group|mirae asset|china universal|"
    r"(?:e|gf|harvest) fund management|ossiam|grantham,?\s*mayo|xponance|amova asset|"
    r"loomis sayles|shelton capital|caxton associates|altshuler shaham|cardano risk|"
    r"skandia|avanza fonder|siemens fonds|e\.?\s*ohman|lombard odier|mondiale|"
    r"van lanschot|eurizon|triglav|sava infond|lbp am|nlb skladi|ofi invest|"
    r"capital international\s+(?:ltd|sarl|investors|inc)|ia global asset"
    r")\b",
    re.I,
)


def parse_bulk_date(val: str) -> str:
    s = (val or "").strip().upper()
    if not s:
        return ""
    if len(s) == 10 and s[4] == "-" and s[7] == "-":
        return s
    parts = s.split("-")
    if len(parts) == 3 and parts[1] in _MONTH_MAP:
        try:
            return f"{int(parts[2]):04d}-{_MONTH_MAP[parts[1]]}-{int(parts[0]):02d}"
        except ValueError:
            return ""
    return ""


def _is_2021_plus_bulk_zip(fname: str) -> bool:
    m_q = re.match(r"^(20\d{2})q[1-4]_form13f\.zip$", fname, flags=re.I)
    if m_q:
        return int(m_q.group(1)) >= 2021
    yrs = [int(y) for y in re.findall(r"(20\d{2})", fname)]
    return bool(yrs) and max(yrs) >= 2021


def _discover_bulk_zip_urls() -> list[tuple[str, str]]:
    """
    Build ordered list of (filename, url) for all SEC Form 13F Bulk Data Sets (2021-present),
    combining verified historical tags with live discovery from SEC's datasets page
    (live SEC HTML links always take precedence over static path defaults).
    """
    by_name: dict[str, str] = {}
    for tag in KNOWN_BULK_TAGS:
        fname = f"{tag}_form13f.zip"
        by_name[fname] = f"https://www.sec.gov/files/structureddata/data/form-13f-data-sets/{fname}"

    try:
        html = fetch_bytes(
            "https://www.sec.gov/data-research/sec-markets-data/form-13f-data-sets",
            cache_key="sec_13f_datasets_index.html",
            max_age_hours=12.0,
        ).decode("utf-8", errors="replace")
        for m in re.finditer(r'href="([^"]*?([^/"]+form13f\.zip))"', html, flags=re.I):
            href, fname = m.group(1), m.group(2)
            if not _is_2021_plus_bulk_zip(fname):
                continue
            url = href if href.startswith("http") else f"https://www.sec.gov{href if href.startswith('/') else '/' + href}"
            # Always let live SEC HTML links overwrite fallback structureddata URLs
            by_name[fname] = url
    except Exception:
        pass

    # Preserve chronological ordering for known tags + append any newer SEC ZIPs sorted cleanly
    ordered: list[tuple[str, str]] = []
    seen: set[str] = set()
    for tag in KNOWN_BULK_TAGS:
        fname = f"{tag}_form13f.zip"
        if fname in by_name and fname not in seen:
            ordered.append((fname, by_name[fname]))
            seen.add(fname)
    for fname in sorted(k for k in by_name if k not in seen):
        ordered.append((fname, by_name[fname]))
    return ordered


def sync_bulk_zips() -> list[Path]:
    """Ensure all SEC Form 13F Bulk Data Set ZIPs exist locally and are valid archives."""
    urls = _discover_bulk_zip_urls()
    paths: list[Path] = []
    downloaded = 0
    for fname, url in urls:
        zpath = BULK_DIR / fname
        if zpath.exists() and zpath.stat().st_size > 1_000_000:
            paths.append(zpath)
            continue
        tmp = BULK_DIR / f"{fname}.tmp"
        cand_urls = [url]
        alt_url = f"https://www.sec.gov/files/datastandardsinnovation/data/form-13f-data-sets/{fname}"
        if url != alt_url:
            cand_urls.append(alt_url)

        fetched_ok = False
        last_err: Exception | None = None
        for cand_url in cand_urls:
            try:
                req = urllib.request.Request(cand_url, headers=SEC_HEADERS)
                with urllib.request.urlopen(req, timeout=120) as resp:
                    raw = resp.read()
                if len(raw) < 100_000:
                    continue
                tmp.write_bytes(raw)
                with zipfile.ZipFile(tmp) as zf:
                    zf.namelist()
                tmp.replace(zpath)
                paths.append(zpath)
                downloaded += 1
                fetched_ok = True
                print(f"[sec-bulk] Downloaded {fname} ({len(raw) / 1e6:.1f} MB)", flush=True)
                break
            except Exception as e:
                last_err = e
                if tmp.exists():
                    tmp.unlink(missing_ok=True)
        if not fetched_ok and last_err is not None:
            print(f"[sec-bulk] Warning: unable to fetch {fname}: {last_err}", flush=True)
    print(f"[sec-bulk] Ready: {len(paths)} SEC Form 13F Bulk Data Set ZIPs ({downloaded} newly downloaded).", flush=True)
    return paths


def load_operating_company_ciks(exempt_ciks: set[str]) -> dict[str, str]:
    """
    Load public operating-company CIKs from SEC company_tickers.json so corporate
    treasuries (e.g. NVDA, GOOGL, INTC, CSCO, JNJ) are excluded unless explicitly curated.
    """
    try:
        data = fetch_json(
            "https://www.sec.gov/files/company_tickers.json",
            cache_key="company_tickers.json",
            max_age_hours=24.0,
        )
        out: dict[str, str] = {}
        for item in data.values():
            cik10 = f"{int(item['cik_str']):010d}"
            if cik10 not in exempt_ciks:
                out[cik10] = str(item.get("ticker", ""))
        return out
    except Exception:
        return {}


def filter_operating_entities(ciks: list[str], curated_ciks: set[str]) -> set[str]:
    """
    Verify submissions metadata for non-curated survivor CIKs to exclude private/foreign
    operating companies (entityType == 'operating' outside financial SIC 62xx/67xx).
    Cached persistently so subsequent runs add zero HTTP overhead.
    """
    targets = [c for c in ciks if c not in curated_ciks]
    if not targets:
        return set()

    meta_cache: dict[str, dict[str, str]] = {}
    if ENTITY_META_PATH.exists():
        try:
            meta_cache = json.loads(ENTITY_META_PATH.read_text(encoding="utf-8"))
        except Exception:
            meta_cache = {}

    missing = [c for c in targets if c not in meta_cache]
    if missing:
        print(f"[universe] Verifying SEC entityType/SIC for {len(missing)} non-curated survivors...", flush=True)
        def _fetch_one(cik10: str) -> tuple[str, dict[str, str] | None]:
            try:
                sub = get_submissions(cik10)
                return cik10, {
                    "entityType": str(sub.get("entityType") or ""),
                    "sic": str(sub.get("sic") or ""),
                }
            except Exception:
                return cik10, None

        with ThreadPoolExecutor(max_workers=8) as ex:
            for cik10, info in ex.map(_fetch_one, missing):
                if info is not None:
                    meta_cache[cik10] = info
        try:
            ENTITY_META_PATH.write_text(json.dumps(meta_cache, separators=(",", ":")), encoding="utf-8")
        except Exception:
            pass

    excluded: set[str] = set()
    for c in targets:
        info = meta_cache.get(c)
        if not info:
            continue
        et = info.get("entityType", "").lower()
        sic = info.get("sic", "")
        if et == "operating" and not sic.startswith(("62", "67")):
            excluded.add(c)
    return excluded


def fetch_post_bulk_master_idx(
    max_bulk_filing_date: str,
    today_date: date,
) -> dict[str, list[dict[str, str]]]:
    """
    Download SEC EDGAR master.idx for all calendar quarters overlapping
    (max_bulk_filing_date, today_date] and collect every 13F-HR / 13F-HR/A filing
    filed strictly after max_bulk_filing_date.
    Returns {cik10: [{cik, companyName, form, filingDate, accessionNumber, txtFilename}, ...]}.
    """
    if not max_bulk_filing_date:
        max_bulk_filing_date = "2026-08-31"

    start_dt = datetime.strptime(max_bulk_filing_date, "%Y-%m-%d").date()
    start_q = (start_dt.month - 1) // 3 + 1
    cur_q = (today_date.month - 1) // 3 + 1

    quarters: list[tuple[int, int]] = []
    y, q = start_dt.year, start_q
    while (y, q) <= (today_date.year, cur_q):
        quarters.append((y, q))
        q += 1
        if q > 4:
            q = 1
            y += 1

    by_cik: dict[str, list[dict[str, str]]] = {}
    total_entries = 0
    for yr, qtr in quarters:
        is_current_q = (yr, qtr) == (today_date.year, cur_q)
        max_age = 8.0 if is_current_q else 24.0 * 90.0
        url = f"https://www.sec.gov/Archives/edgar/full-index/{yr}/QTR{qtr}/master.idx"
        try:
            raw = fetch_bytes(url, cache_key=f"master_{yr}_QTR{qtr}.idx", max_age_hours=max_age)
        except Exception as e:
            print(f"[sec-master] Warning: unable to load {yr} QTR{qtr} master.idx: {e}", flush=True)
            continue

        text = raw.decode("utf-8", errors="replace")
        in_body = False
        for line in text.splitlines():
            if not in_body:
                if line.startswith("-----"):
                    in_body = True
                continue
            parts = line.split("|")
            if len(parts) != 5:
                continue
            cik_raw, cname, form, fdate, fname = parts
            if form not in ("13F-HR", "13F-HR/A") or fdate <= max_bulk_filing_date:
                continue
            try:
                cik10 = f"{int(cik_raw):010d}"
            except ValueError:
                continue
            acc_no_ext = fname.rsplit("/", 1)[-1].replace(".txt", "")
            by_cik.setdefault(cik10, []).append({
                "cik": cik10,
                "entityName": cname.strip(),
                "form": form,
                "filingDate": fdate.strip(),
                "accessionNumber": acc_no_ext,
                "txtFilename": fname.strip(),
            })
            total_entries += 1

    print(
        f"[sec-master] Indexed {total_entries} post-bulk 13F filings (> {max_bulk_filing_date}) "
        f"across {len(by_cik)} CIKs from {len(quarters)} master.idx quarter(s).",
        flush=True,
    )
    return by_cik


def fetch_post_bulk_efts(
    max_bulk_filing_date: str,
    today_date: date,
    lookback_days: int = 5,
) -> dict[str, list[dict[str, str]]]:
    """
    Query SEC EDGAR EFTS full-text search index over the most recent `lookback_days`
    so same-day / intra-day 13F-HR and 13F-HR/A filings appear immediately before
    the overnight master.idx batch index is published.
    """
    cutoff = max_bulk_filing_date or "2026-08-31"
    start_dt = max(
        (today_date - timedelta(days=lookback_days)).strftime("%Y-%m-%d"),
        cutoff,
    )
    end_dt = today_date.strftime("%Y-%m-%d")
    if end_dt < start_dt:
        return {}

    by_cik: dict[str, list[dict[str, str]]] = {}
    seen_acc: set[tuple[str, str]] = set()
    page_size = 100
    offset = 0
    max_hits = 4000
    total_reported = 0

    try:
        while offset < max_hits:
            qs = urllib.parse.urlencode({
                "forms": "13F-HR,13F-HR/A",
                "dateRange": "custom",
                "startdt": start_dt,
                "enddt": end_dt,
                "from": str(offset),
                "size": str(page_size),
            })
            url = f"https://efts.sec.gov/LATEST/search-index?{qs}"
            raw = fetch_bytes(url, cache_key=None)
            payload = json.loads(raw.decode("utf-8", errors="replace"))
            hits_block = payload.get("hits") or {}
            if offset == 0:
                total_reported = int((hits_block.get("total") or {}).get("value") or 0)
            hits = hits_block.get("hits") or []
            if not hits:
                break
            for h in hits:
                src = h.get("_source") or {}
                ciks = src.get("ciks") or []
                adsh = str(src.get("adsh") or "").strip()
                fdate = str(src.get("file_date") or "").strip()
                form = str(src.get("form") or "").strip()
                if not ciks or not adsh or not fdate or form not in ("13F-HR", "13F-HR/A"):
                    continue
                if fdate <= cutoff:
                    continue
                try:
                    cik_int = int(ciks[0])
                    cik10 = f"{cik_int:010d}"
                except Exception:
                    continue
                key = (cik10, adsh)
                if key in seen_acc:
                    continue
                seen_acc.add(key)
                disp = ""
                disp_list = src.get("display_names") or []
                if disp_list:
                    disp = re.sub(r"\s*\(CIK\s+[\d]+\)\s*$", "", str(disp_list[0]), flags=re.I).strip()
                by_cik.setdefault(cik10, []).append({
                    "cik": cik10,
                    "entityName": disp or f"CIK {cik10}",
                    "form": form,
                    "filingDate": fdate,
                    "reportDate": str(src.get("period_ending") or "")[:10],
                    "accessionNumber": adsh,
                    "txtFilename": f"edgar/data/{cik_int}/{adsh}.txt",
                })
            offset += len(hits)
            if len(hits) < page_size or offset >= total_reported:
                break
    except Exception as e:
        print(f"[sec-efts] Warning: EFTS incremental check skipped ({e})", flush=True)
        return {}

    total_added = sum(len(v) for v in by_cik.values())
    if total_added:
        print(
            f"[sec-efts] Indexed {total_added} recent 13F filings ({start_dt}..{end_dt}) across {len(by_cik)} CIKs.",
            flush=True,
        )
    return by_cik


def _zip_tsv_names(zf: zipfile.ZipFile) -> dict[str, str]:
    return {n.rsplit("/", 1)[-1].upper(): n for n in zf.namelist()}


def _build_bulk_period_snapshots(
    acc_metas: list[tuple[str, str, str, str, str, str, str]],
    rows_by_acc: dict[str, list[dict[str, Any]]],
    ratios_by_acc: dict[str, tuple[list[float], list[float]]],
) -> dict[str, Any] | None:
    """
    Merge multiple 13F-HR / 13F-HR/A filings for the same (cik, reportDate) into
    one unit-normalized snapshot, identically to build_clean_quarterly_filings,
    preserving form, amendmentType, disclosedDate, and chronological amendmentEvents.
    acc_metas items: (filingDate, accessionNumber, form, amendmentType, entityName, reportDate, cik10)
    """
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    meta: dict[str, Any] | None = None
    amendment_events: list[dict[str, Any]] = []

    for fdate, acc, form, amend_type, entity_name, report_date, cik10 in sorted(
        acc_metas, key=lambda x: (x[0], x[1])
    ):
        raw_rows = rows_by_acc.get(acc)
        if not raw_rows:
            continue
        eq_r, fb_r = ratios_by_acc.get(acc, ([], []))
        is_thousands = detect_is_thousands(fdate, eq_r, fb_r)
        mult = 1000.0 if is_thousands else 1.0

        # Deduplicate multi-manager lines inside this single submission first
        sub_agg: dict[tuple[str, str], dict[str, Any]] = {}
        for r in raw_rows:
            key = (r["cusip"], r["secType"])
            val_usd = r["rawValue"] * mult
            if key not in sub_agg:
                sub_agg[key] = {
                    "cusip": r["cusip"],
                    "issuer": r["issuer"],
                    "title": r["title"],
                    "secType": r["secType"],
                    "valueUsd": val_usd,
                    "shares": r["shares"],
                    "disclosedDate": fdate,
                }
            else:
                sub_agg[key]["valueUsd"] += val_usd
                sub_agg[key]["shares"] += r["shares"]

        if form == "13F-HR" or amend_type == "RESTATEMENT" or meta is None:
            merged = {k: dict(v) for k, v in sub_agg.items()}
            meta = {
                "cik": cik10,
                "entityName": entity_name,
                "form": form,
                "amendmentType": amend_type,
                "reportDate": report_date,
                "filingDate": fdate,
                "originalFilingDate": fdate,
                "latestAmendmentDate": fdate,
                "accessionNumber": acc,
            }
            amendment_events = [{
                "filingDate": fdate,
                "form": form,
                "amendmentType": amend_type,
                "isReset": True,
                "rows": [dict(v) for v in sub_agg.values()],
            }]
        else:
            meta["latestAmendmentDate"] = max(meta.get("latestAmendmentDate", fdate), fdate)
            meta["amendmentType"] = amend_type or "NEW HOLDINGS"
            inc_rows: list[dict[str, Any]] = []
            for k, row in sub_agg.items():
                inc_rows.append(dict(row))
                if k not in merged:
                    merged[k] = dict(row)
                else:
                    merged[k]["valueUsd"] += row["valueUsd"]
                    merged[k]["shares"] += row["shares"]
            amendment_events.append({
                "filingDate": fdate,
                "form": form,
                "amendmentType": amend_type or "NEW HOLDINGS",
                "isReset": False,
                "rows": inc_rows,
            })

    if not meta or not merged:
        return None
    meta["rows"] = sorted(merged.values(), key=lambda x: x["valueUsd"], reverse=True)
    meta["amendmentEvents"] = amendment_events
    return meta


def discover_universe_via_bulk(
    curated_funds: list[dict[str, Any]],
    bulk_paths: list[Path],
    today_date: date,
    min_history_report_date: str = "2020-12-31",
) -> tuple[
    list[dict[str, Any]],
    dict[str, dict[str, Any]],
    dict[str, Any],
    dict[str, list[dict[str, str]]],
]:
    """
    Scan ALL SEC 13F Bulk Data Sets + post-bulk master.idx to evaluate the full
    universe (~9,500+ CIKs) deterministically without seed pairs or EFTS caps.

    Returns:
      - all_candidates: list of candidate dicts (Curated Core + all Concentration-Pass non-curated)
      - stage1_results: cik -> Stage 1 latest bulk summary + eqRows/eqAum/repAum
      - funnel_counts: step-by-step full universe funnel metrics
      - post_bulk_by_cik: post-bulk master.idx entries per CIK
    """
    t0 = time.monotonic()
    curated_by_cik = {f["cik"]: dict(f, source="Curated Core") for f in curated_funds}
    curated_ciks = set(curated_by_cik.keys())

    # Pass A: Read SUBMISSION.TSV + COVERPAGE.TSV across ALL bulk ZIPs (metadata-only, ultra-fast)
    all_ciks_seen: set[str] = set()
    # cik -> {reportDate: [(fdate, acc, form, amendType, entityName, zip_idx)]}
    cik_periods: dict[str, dict[str, list[tuple[str, str, str, str, str, int]]]] = {}
    cik_latest_name: dict[str, str] = {}
    cik_latest_rd: dict[str, str] = {}
    max_bulk_filing_date = ""

    for z_idx, zpath in enumerate(bulk_paths):
        with zipfile.ZipFile(zpath) as zf:
            names = _zip_tsv_names(zf)
            if "SUBMISSION.TSV" not in names:
                continue
            cov_names: dict[str, tuple[str, str]] = {}
            if "COVERPAGE.TSV" in names:
                with zf.open(names["COVERPAGE.TSV"]) as cf:
                    for acc, mgr_nm, amend_t in _iter_tsv_rows(
                        cf, ("ACCESSION_NUMBER", "FILINGMANAGER_NAME", "AMENDMENTTYPE")
                    ):
                        if acc:
                            cov_names[acc] = (mgr_nm, amend_t.upper())

            with zf.open(names["SUBMISSION.TSV"]) as sf:
                for acc, fd_raw, form, cik_raw, rd_raw in _iter_tsv_rows(
                    sf, ("ACCESSION_NUMBER", "FILING_DATE", "SUBMISSIONTYPE", "CIK", "PERIODOFREPORT")
                ):
                    if form not in ("13F-HR", "13F-HR/A"):
                        continue
                    rd = parse_bulk_date(rd_raw)
                    fd = parse_bulk_date(fd_raw)
                    if not cik_raw or not acc or not rd or not fd:
                        continue
                    try:
                        cik10 = f"{int(cik_raw):010d}"
                    except ValueError:
                        continue
                    if fd > max_bulk_filing_date:
                        max_bulk_filing_date = fd
                    if rd < min_history_report_date:
                        continue
                    all_ciks_seen.add(cik10)
                    mgr_name, amend_type = cov_names.get(acc, ("", ""))
                    cik_periods.setdefault(cik10, {}).setdefault(rd, []).append(
                        (fd, acc, form, amend_type, mgr_name, z_idx)
                    )
                    if mgr_name and rd >= cik_latest_rd.get(cik10, ""):
                        cik_latest_rd[cik10] = rd
                        cik_latest_name[cik10] = mgr_name

    # Merge incremental post-bulk master.idx + same-day EFTS filings (> max_bulk_filing_date)
    post_bulk_by_cik = fetch_post_bulk_master_idx(max_bulk_filing_date, today_date)
    efts_by_cik = fetch_post_bulk_efts(max_bulk_filing_date, today_date, lookback_days=5)
    for cik10, efts_list in efts_by_cik.items():
        existing = post_bulk_by_cik.setdefault(cik10, [])
        existing_accs = {x["accessionNumber"] for x in existing}
        for item in efts_list:
            if item["accessionNumber"] not in existing_accs:
                existing.append(item)
                existing_accs.add(item["accessionNumber"])

    for cik10, pb_list in post_bulk_by_cik.items():
        all_ciks_seen.add(cik10)
        if cik10 not in cik_latest_name and pb_list:
            cik_latest_name[cik10] = pb_list[-1]["entityName"]

    op_ciks = load_operating_company_ciks(curated_ciks)

    # Identify active candidates, exclude operating corps / banks / insurers / pensions, check >= 4Q
    active_ciks: list[str] = []
    excluded_inst_count = 0
    history_pass_ciks: list[str] = []

    for cik10 in sorted(all_ciks_seen):
        is_curated = cik10 in curated_ciks
        periods = cik_periods.get(cik10, {})
        has_post_bulk = cik10 in post_bulk_by_cik
        latest_rd = max(periods.keys()) if periods else ""
        if not latest_rd and not has_post_bulk:
            continue
        if not has_post_bulk:
            age_days = (today_date - datetime.strptime(latest_rd, "%Y-%m-%d").date()).days
            if age_days > 210:
                continue
        active_ciks.append(cik10)

        # Operating-company / non-focused-institution filter (Curated exempt)
        if not is_curated:
            cname = cik_latest_name.get(cik10, "")
            if cik10 in op_ciks or (cname and _EXCLUDE_INSTITUTION_RE.search(cname)):
                excluded_inst_count += 1
                continue

        # Distinct quarters since 2024-01-01 (credit post-bulk quarter if new filing arrived after bulk ZIP)
        recent_qtrs = sum(1 for rd in periods if rd >= "2024-01-01")
        effective_qtrs = recent_qtrs + (1 if has_post_bulk else 0)
        if is_curated:
            if effective_qtrs >= 2:
                history_pass_ciks.append(cik10)
        else:
            if effective_qtrs >= 4 and periods:
                history_pass_ciks.append(cik10)

    # Pass B: Read latest bulk reportDate INFOTABLE rows for history_pass_ciks (only recent relevant ZIPs)
    zip_to_accs: dict[int, set[str]] = {}
    cik_target_rd: dict[str, str] = {}
    acc_to_cik_meta: dict[str, tuple[str, str, str, str, str, str, str]] = {}

    for cik10 in history_pass_ciks:
        periods = cik_periods.get(cik10, {})
        if not periods:
            continue
        # Inspect latest populated bulk period (and fallback prev period if needed)
        target_rds = sorted(periods.keys(), reverse=True)[:2]
        cik_target_rd[cik10] = target_rds[0]
        for rd in target_rds:
            for fd, acc, form, amend_type, mgr_name, z_idx in periods[rd]:
                zip_to_accs.setdefault(z_idx, set()).add(acc)
                acc_to_cik_meta[acc] = (fd, acc, form, amend_type, mgr_name, rd, cik10)

    rows_by_acc: dict[str, list[dict[str, Any]]] = {}
    ratios_by_acc: dict[str, tuple[list[float], list[float]]] = {}

    for z_idx in sorted(zip_to_accs.keys()):
        target_accs = zip_to_accs[z_idx]
        zpath = bulk_paths[z_idx]
        with zipfile.ZipFile(zpath) as zf:
            names = _zip_tsv_names(zf)
            if "INFOTABLE.TSV" not in names:
                continue
            with zf.open(names["INFOTABLE.TSV"]) as inf_f:
                for acc, cusip_raw, val_raw, sh_raw, title, issuer, sh_type_raw, pc_raw in _iter_tsv_rows(
                    inf_f,
                    ("ACCESSION_NUMBER", "CUSIP", "VALUE", "SSHPRNAMT", "TITLEOFCLASS", "NAMEOFISSUER", "SSHPRNAMTTYPE", "PUTCALL"),
                    filter_col="ACCESSION_NUMBER",
                    filter_values=target_accs,
                ):
                    cusip = cusip_raw.upper().zfill(9)[:9]
                    if not cusip:
                        continue
                    try:
                        val = float(val_raw.replace(",", "")) if val_raw else 0.0
                    except ValueError:
                        val = 0.0
                    if val <= 0:
                        continue
                    try:
                        shares = float(sh_raw.replace(",", "")) if sh_raw else 0.0
                    except ValueError:
                        shares = 0.0
                    sh_type = (sh_type_raw or "SH").upper()
                    put_call = pc_raw.upper()

                    sec_type = "EQUITY"
                    if put_call in ("PUT", "CALL"):
                        sec_type = put_call
                    elif sh_type == "PRN":
                        sec_type = "NOTE"

                    if sec_type == "EQUITY" and shares > 0:
                        rat = val / shares
                        eq_r, fb_r = ratios_by_acc.setdefault(acc, ([], []))
                        fb_r.append(rat)
                        if not WARRANT_TITLE_RE.search(title):
                            eq_r.append(rat)

                    rows_by_acc.setdefault(acc, []).append({
                        "cusip": cusip,
                        "issuer": issuer,
                        "title": title,
                        "rawValue": val,
                        "shares": shares,
                        "secType": sec_type,
                    })

    # Reconstruct latest bulk filing per candidate CIK & apply deterministic Size + Concentration gates
    size_pass_count = 0
    conc_pass_count = 0
    all_candidates: list[dict[str, Any]] = []
    stage1_results: dict[str, dict[str, Any]] = {}

    for cik10 in history_pass_ciks:
        is_curated = cik10 in curated_ciks
        periods = cik_periods.get(cik10, {})
        latest_filing: dict[str, Any] | None = None
        for rd in sorted(periods.keys(), reverse=True)[:2]:
            metas = [
                acc_to_cik_meta[acc]
                for (_, acc, _, _, _, _) in periods[rd]
                if acc in acc_to_cik_meta
            ]
            latest_filing = _build_bulk_period_snapshots(metas, rows_by_acc, ratios_by_acc)
            if latest_filing:
                break

        # Allow Curated funds that have only post-bulk filings (e.g. brand-new Q3 filers)
        if not latest_filing and is_curated and cik10 in post_bulk_by_cik:
            pb_metas = post_bulk_by_cik[cik10]
            all_candidates.append(curated_by_cik[cik10])
            stage1_results[cik10] = {
                "cand": curated_by_cik[cik10],
                "latest": {"reportDate": pb_metas[-1]["filingDate"], "rows": [], "entityName": curated_by_cik[cik10]["name"]},
                "eqRows": [],
                "eqAum": 0.0,
                "repAum": 0.0,
                "qualOk": True,
                "updateOnlyOk": False,
                "postBulkOnly": True,
            }
            continue

        if not latest_filing:
            continue

        rep_aum = sum(r["valueUsd"] for r in latest_filing["rows"])
        eq_rows = [r for r in latest_filing["rows"] if r["secType"] == "EQUITY" and r["valueUsd"] > 0]
        eq_aum = sum(r["valueUsd"] for r in eq_rows)
        opt_rows = [r for r in latest_filing["rows"] if r["secType"] in ("PUT", "CALL") and r["valueUsd"] > 0]
        opt_aum = sum(r["valueUsd"] for r in opt_rows)

        if is_curated:
            cand = curated_by_cik[cik10]
            if rep_aum >= 170_000_000:
                size_pass_count += 1
                conc_pass_count += 1
            all_candidates.append(cand)
            stage1_results[cik10] = {
                "cand": cand,
                "latest": latest_filing,
                "eqRows": eq_rows,
                "eqAum": eq_aum,
                "repAum": rep_aum,
                "qualOk": rep_aum >= 170_000_000 and eq_aum >= 85_000_000,
                "updateOnlyOk": rep_aum >= 170_000_000,
            }
            continue

        # Structural options market-maker exclusion (>50% options AUM or >300 option lines)
        if (rep_aum > 0 and opt_aum / rep_aum > 0.50) or len(opt_rows) > 300:
            excluded_inst_count += 1
            continue

        # Slight 15% Q2->Q3 boundary buffer ($170M rep / $85M eq) at Stage 1 pre-screen; strict $200M/$100M enforced on final latest Q in Stage 3
        if not (rep_aum >= 170_000_000 and eq_aum >= 85_000_000):
            continue
        size_pass_count += 1

        raw_by_c: dict[str, float] = {}
        for r in eq_rows:
            raw_by_c[r["cusip"]] = raw_by_c.get(r["cusip"], 0.0) + r["valueUsd"]
        raw_pos = len(raw_by_c)
        sorted_vals = sorted(raw_by_c.values(), reverse=True)
        raw_top10 = (sum(sorted_vals[:10]) / eq_aum * 100.0) if eq_aum > 0 else 0.0
        hhi = sum((v / eq_aum) ** 2 for v in raw_by_c.values()) if eq_aum > 0 else 0.0
        raw_eff_n = (1.0 / hhi) if hhi > 0 else float(raw_pos)
        sig_pos = sum(1 for v in raw_by_c.values() if eq_aum > 0 and (v / eq_aum) >= 0.0025)

        # Exclude <5 stock VC lockups / single-stock SPVs AND bloated multi-hundred-stock generalists
        min_pos_ok = raw_pos >= 5 and raw_eff_n >= 1.4
        conc_ok = raw_pos <= 280 and (
            raw_pos <= 130
            or (sig_pos <= 98 and raw_eff_n <= 58.0 and raw_top10 >= 33.0)
            or raw_top10 >= 48.0
        )
        if not (min_pos_ok and conc_ok):
            continue
        conc_pass_count += 1

        raw_name = latest_filing.get("entityName") or cik_latest_name.get(cik10) or f"Fund {cik10}"
        fmt_name = format_entity_name(raw_name)
        cand = {
            "cik": cik10,
            "name": fmt_name,
            "manager": "SEC EDGAR Discovered",
            "hq": "United States",
            "styleTag": "Discovered Tech Concentrated",
            "strategySummary": (
                f"Identified from full-universe SEC Form 13F Bulk Data Set screening "
                f"({raw_pos} equity positions, top-10 concentration {raw_top10:.0f}%, Tech/TMT-focused)."
            ),
            "source": "EDGAR Discovered",
        }
        all_candidates.append(cand)
        stage1_results[cik10] = {
            "cand": cand,
            "latest": latest_filing,
            "eqRows": eq_rows,
            "eqAum": eq_aum,
            "repAum": rep_aum,
            "qualOk": True,
            "updateOnlyOk": False,
        }

    # Ensure Curated come first in all_candidates
    all_candidates.sort(key=lambda c: (0 if c.get("source") == "Curated Core" else 1, c["name"]))

    elapsed = time.monotonic() - t0
    print(
        f"[universe] Complete in {elapsed:.1f}s: {len(all_ciks_seen)} SEC 13F CIKs -> "
        f"{len(active_ciks)} active -> {len(history_pass_ciks)} manager/history pass -> "
        f"{size_pass_count} scale pass -> {conc_pass_count} concentrated candidates (no artificial cap).",
        flush=True,
    )

    funnel_counts = {
        "method": "Full-Universe SEC Form 13F Bulk Data Sets + Post-Bulk Master Index + EFTS",
        "totalCiksScanned": len(all_ciks_seen),
        "activeRecentCount": len(active_ciks),
        "excludedInstitutionsCount": excluded_inst_count,
        "nonExcludedManagerCount": len(active_ciks) - excluded_inst_count,
        "fourPlusQuartersCount": len(history_pass_ciks),
        "sizeAndEquityPassCount": size_pass_count,
        "concentrationPassCount": conc_pass_count,
        "bulkDatasetsCount": len(bulk_paths),
        "bulkMaxFilingDate": max_bulk_filing_date,
    }
    return all_candidates, stage1_results, funnel_counts, post_bulk_by_cik


def extract_bulk_histories(
    target_ciks: set[str],
    bulk_paths: list[Path],
    min_report_date: str = "2020-12-31",
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, tuple[str, str]]]:
    """
    Extract complete historical 13F-HR & 13F-HR/A holdings (2021-present) offline
    for `target_ciks` across all bulk ZIPs, returning:
      - histories: {cik10: [merged_quarter_snapshot, ...]}
      - latest_names: {cik10: entityName}
    """
    t0 = time.monotonic()
    print(f"[sec-bulk] Extracting full 2021-present histories for {len(target_ciks)} funds across {len(bulk_paths)} ZIPs...", flush=True)

    acc_meta_map: dict[str, tuple[str, str, str, str, str, str, str]] = {}
    latest_names: dict[str, str] = {}
    rows_by_acc: dict[str, list[dict[str, Any]]] = {}
    ratios_by_acc: dict[str, tuple[list[float], list[float]]] = {}

    for zpath in bulk_paths:
        with zipfile.ZipFile(zpath) as zf:
            names = _zip_tsv_names(zf)
            if "SUBMISSION.TSV" not in names or "INFOTABLE.TSV" not in names:
                continue

            sub_hits: list[tuple[str, str, str, str, str]] = []
            zip_target_accs: set[str] = set()
            with zf.open(names["SUBMISSION.TSV"]) as sf:
                for acc, fd_raw, form, cik_raw, rd_raw in _iter_tsv_rows(
                    sf, ("ACCESSION_NUMBER", "FILING_DATE", "SUBMISSIONTYPE", "CIK", "PERIODOFREPORT")
                ):
                    if form not in ("13F-HR", "13F-HR/A") or not cik_raw or not acc:
                        continue
                    try:
                        cik10 = f"{int(cik_raw):010d}"
                    except ValueError:
                        continue
                    if cik10 not in target_ciks:
                        continue
                    rd = parse_bulk_date(rd_raw)
                    fd = parse_bulk_date(fd_raw)
                    if not rd or not fd or rd < min_report_date:
                        continue
                    zip_target_accs.add(acc)
                    sub_hits.append((fd, acc, form, rd, cik10))

            if not zip_target_accs:
                continue

            cov_names: dict[str, tuple[str, str]] = {}
            if "COVERPAGE.TSV" in names:
                with zf.open(names["COVERPAGE.TSV"]) as cf:
                    for acc, mgr_nm, amend_t in _iter_tsv_rows(
                        cf,
                        ("ACCESSION_NUMBER", "FILINGMANAGER_NAME", "AMENDMENTTYPE"),
                        filter_col="ACCESSION_NUMBER",
                        filter_values=zip_target_accs,
                    ):
                        cov_names[acc] = (mgr_nm, amend_t.upper())

            for fd, acc, form, rd, cik10 in sub_hits:
                mgr_name, amend_type = cov_names.get(acc, ("", ""))
                if mgr_name:
                    latest_names[cik10] = mgr_name
                acc_meta_map[acc] = (fd, acc, form, amend_type, mgr_name, rd, cik10)

            with zf.open(names["INFOTABLE.TSV"]) as inf_f:
                for acc, cusip_raw, val_raw, sh_raw, title, issuer, sh_type_raw, pc_raw in _iter_tsv_rows(
                    inf_f,
                    ("ACCESSION_NUMBER", "CUSIP", "VALUE", "SSHPRNAMT", "TITLEOFCLASS", "NAMEOFISSUER", "SSHPRNAMTTYPE", "PUTCALL"),
                    filter_col="ACCESSION_NUMBER",
                    filter_values=zip_target_accs,
                ):
                    cusip = cusip_raw.upper().zfill(9)[:9]
                    if not cusip:
                        continue
                    try:
                        val = float(val_raw.replace(",", "")) if val_raw else 0.0
                    except ValueError:
                        val = 0.0
                    if val <= 0:
                        continue
                    try:
                        shares = float(sh_raw.replace(",", "")) if sh_raw else 0.0
                    except ValueError:
                        shares = 0.0
                    sh_type = (sh_type_raw or "SH").upper()
                    put_call = pc_raw.upper()

                    sec_type = "EQUITY"
                    if put_call in ("PUT", "CALL"):
                        sec_type = put_call
                    elif sh_type == "PRN":
                        sec_type = "NOTE"

                    if sec_type == "EQUITY" and shares > 0:
                        rat = val / shares
                        eq_r, fb_r = ratios_by_acc.setdefault(acc, ([], []))
                        fb_r.append(rat)
                        if not WARRANT_TITLE_RE.search(title):
                            eq_r.append(rat)

                    rows_by_acc.setdefault(acc, []).append({
                        "cusip": cusip,
                        "issuer": issuer,
                        "title": title,
                        "rawValue": val,
                        "shares": shares,
                        "secType": sec_type,
                    })

    # Reconstruct merged period dicts per (cik, reportDate)
    by_cik_period: dict[str, dict[str, list[tuple[str, str, str, str, str, str, str]]]] = {}
    for meta in acc_meta_map.values():
        cik10 = meta[6]
        rd = meta[5]
        by_cik_period.setdefault(cik10, {}).setdefault(rd, []).append(meta)

    histories: dict[str, list[dict[str, Any]]] = {}
    total_filings = 0
    for cik10 in target_ciks:
        p_map = by_cik_period.get(cik10, {})
        snaps: list[dict[str, Any]] = []
        for rd in sorted(p_map.keys()):
            snap = _build_bulk_period_snapshots(p_map[rd], rows_by_acc, ratios_by_acc)
            if snap:
                snaps.append(snap)
        if snaps:
            histories[cik10] = snaps
            total_filings += len(snaps)

    elapsed = time.monotonic() - t0
    print(f"[sec-bulk] Extracted {total_filings} historical quarterly snapshots across {len(histories)}/{len(target_ciks)} CIKs offline in {elapsed:.1f}s.", flush=True)
    return histories, latest_names


def _norm_xml_period_date(raw_dt: str) -> str:
    s = (raw_dt or "").strip()
    if not s:
        return ""
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return s
    m = re.fullmatch(r"(\d{2})-(\d{2})-(\d{4})", s)
    if m:
        return f"{m.group(3)}-{m.group(1)}-{m.group(2)}"
    return ""


def _infer_quarter_end(filing_date: str) -> str:
    fd_dt = datetime.strptime(filing_date, "%Y-%m-%d").date()
    q_ends = [
        date(yr, m, d)
        for yr in (fd_dt.year - 1, fd_dt.year)
        for (m, d) in ((3, 31), (6, 30), (9, 30), (12, 31))
        if date(yr, m, d) <= fd_dt
    ]
    return max(q_ends).strftime("%Y-%m-%d") if q_ends else filing_date


def fetch_post_bulk_filing_via_txt(entry: dict[str, str]) -> dict[str, Any] | None:
    """
    Fetch ONE SEC complete submission .txt file per post-bulk filing (eliminating separate
    submissions.json, index.json, primary_doc.xml, and infotable.xml HTTP roundtrips),
    parse embedded <XML> blocks, normalize units via detect_is_thousands, and cache as parsed_v2_.
    Falls back to index.json-based parse_13f_filing if .txt is unavailable or incomplete.
    """
    cik10 = entry["cik"]
    acc = entry["accessionNumber"]
    acc_nd = acc.replace("-", "")
    parsed_cache_path = SEC_CACHE_DIR / f"parsed_v2_{cik10}_{acc_nd}.json"
    if parsed_cache_path.exists():
        try:
            cached = json.loads(parsed_cache_path.read_text(encoding="utf-8"))
            if cached.get("empty"):
                return None
            return cached
        except Exception:
            pass

    def _fallback_via_index() -> dict[str, Any] | None:
        try:
            fb_filing = {
                "cik": cik10,
                "entityName": entry.get("entityName", ""),
                "form": entry["form"],
                "accessionNumber": acc,
                "primaryDocument": "",
                "reportDate": entry.get("reportDate") or _infer_quarter_end(entry["filingDate"]),
                "filingDate": entry["filingDate"],
            }
            return parse_13f_filing(fb_filing)
        except Exception:
            return None

    txt_fname = entry.get("txtFilename")
    if not txt_fname:
        return _fallback_via_index()

    url = f"https://www.sec.gov/Archives/{txt_fname}"
    try:
        raw_txt = fetch_bytes(url, cache_key=f"subtxt_{cik10}_{acc_nd}.txt").decode("utf-8", errors="replace")
    except Exception:
        return _fallback_via_index()

    xml_blocks = re.findall(r"<XML>(.*?)</XML>", raw_txt, flags=re.S | re.I)
    if not xml_blocks:
        fb = _fallback_via_index()
        if fb is not None:
            return fb
        parsed_cache_path.write_text('{"empty":true}', encoding="utf-8")
        return None

    primary_doc = None
    info_doc = None
    for xb in xml_blocks:
        stripped = xb.strip()
        if not stripped:
            continue
        root = _parse_xml_root(stripped.encode("utf-8", errors="replace"))
        if root is None or not isinstance(root.tag, str):
            continue
        local_root = root.tag.rsplit("}", 1)[-1].lower()
        if local_root == "edgarsubmission":
            primary_doc = root
        elif local_root == "informationtable":
            info_doc = root

    if info_doc is None:
        fb = _fallback_via_index()
        if fb is not None:
            return fb
        parsed_cache_path.write_text('{"empty":true}', encoding="utf-8")
        return None

    report_date = ""
    amendment_type = ""
    entity_name = entry.get("entityName", "")
    if primary_doc is not None:
        amendment_type = _find_text(primary_doc, "amendmentType", "").upper()
        for tag_name in ("periodOfReport", "reportCalendarOrQuarter"):
            cand_rd = _norm_xml_period_date(_find_text(primary_doc, tag_name, ""))
            if cand_rd:
                report_date = cand_rd
                break
        mgr_nm = _find_text(primary_doc, "filingManager", "")
        if not mgr_nm:
            for nm_tag in ("name",):
                found = primary_doc.xpath(f".//*[local-name()='filingManager']/*[local-name()='{nm_tag}']")
                if found and found[0].text and found[0].text.strip():
                    entity_name = found[0].text.strip()
                    break

    if not report_date:
        report_date = _infer_quarter_end(entry["filingDate"])

    filing_dict = {
        "cik": cik10,
        "entityName": entity_name,
        "form": entry["form"],
        "reportDate": report_date,
        "filingDate": entry["filingDate"],
        "accessionNumber": acc,
    }
    res = parse_xml_info_table(info_doc, filing_dict, amendment_type=amendment_type)
    if res is None:
        parsed_cache_path.write_text('{"empty":true}', encoding="utf-8")
        return None

    try:
        parsed_cache_path.write_text(json.dumps(res, separators=(",", ":")), encoding="utf-8")
    except Exception:
        pass
    return res


def ingest_target_histories(
    target_ciks: list[str],
    bulk_paths: list[Path],
    post_bulk_by_cik: dict[str, list[dict[str, str]]],
    today_date: date,
    min_report_date: str = "2020-12-31",
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, int]]:
    """
    Assemble complete 2021-present clean quarterly filing sequences for all surviving Stage 2 targets
    by merging offline Bulk Data Set histories with incremental post-bulk master.idx / EFTS .txt filings,
    preserving true amendment metadata, per-row disclosedDate, and chronological amendmentEvents.
    """
    target_set = set(target_ciks)
    bulk_histories, _ = extract_bulk_histories(target_set, bulk_paths, min_report_date=min_report_date)

    post_tasks: list[dict[str, str]] = []
    for cik10 in target_ciks:
        for item in post_bulk_by_cik.get(cik10, []):
            post_tasks.append(item)

    post_parsed: dict[str, list[dict[str, Any]]] = {}
    if post_tasks:
        print(f"[sec-master] Fetching/parsing {len(post_tasks)} post-bulk 13F .txt submissions across {len(target_ciks)} targets (8 workers)...", flush=True)
        done = 0
        with ThreadPoolExecutor(max_workers=8) as ex:
            futs = [ex.submit(fetch_post_bulk_filing_via_txt, item) for item in post_tasks]
            for fut in as_completed(futs):
                done += 1
                res = fut.result()
                if res:
                    post_parsed.setdefault(res["cik"], []).append(res)
                if done % 250 == 0 or done == len(post_tasks):
                    print(f"  [sec-master] {done}/{len(post_tasks)} post-bulk filings parsed", flush=True)

    # Merge bulk snapshots + post-bulk snapshots per period (respecting 13F-HR/A restatements / additions)
    assembled: dict[str, list[dict[str, Any]]] = {}
    for cik10 in target_ciks:
        by_period: dict[str, list[dict[str, Any]]] = {}
        for snap in bulk_histories.get(cik10, []):
            by_period.setdefault(snap["reportDate"], []).append(snap)
        for parsed in post_parsed.get(cik10, []):
            by_period.setdefault(parsed["reportDate"], []).append(parsed)

        if not by_period:
            continue

        cleaned: list[dict[str, Any]] = []
        for rd in sorted(by_period.keys()):
            items = by_period[rd]
            # Fast-path: single already-normalized snapshot with intact amendmentEvents
            if len(items) == 1 and "amendmentEvents" in items[0]:
                cleaned.append(items[0])
                continue

            # Expand any bulk snapshot into its atomic chronological submission events
            atomic_events: list[dict[str, Any]] = []
            for item in items:
                if "amendmentEvents" in item:
                    for ev in item["amendmentEvents"]:
                        atomic_events.append({
                            "cik": cik10,
                            "entityName": item.get("entityName", ""),
                            "form": ev.get("form", item.get("form", "13F-HR")),
                            "amendmentType": ev.get("amendmentType", item.get("amendmentType", "")),
                            "filingDate": ev["filingDate"],
                            "accessionNumber": item.get("accessionNumber", ""),
                            "rows": ev["rows"],
                        })
                else:
                    atomic_events.append(item)

            group = sorted(atomic_events, key=lambda x: (x["filingDate"], x.get("accessionNumber", "")))
            merged: dict[tuple[str, str], dict[str, Any]] = {}
            meta: dict[str, Any] | None = None
            amendment_events: list[dict[str, Any]] = []

            for item in group:
                fdate = item["filingDate"]
                ftype = item.get("form", "13F-HR")
                atype = item.get("amendmentType", "")
                if ftype == "13F-HR" or atype == "RESTATEMENT" or meta is None:
                    merged = {}
                    ev_rows: list[dict[str, Any]] = []
                    for r in item["rows"]:
                        rc = dict(r)
                        rc["disclosedDate"] = fdate
                        ev_rows.append(dict(rc))
                        merged[(r["cusip"], r["secType"])] = rc
                    meta = {
                        "cik": cik10,
                        "entityName": item.get("entityName", ""),
                        "form": ftype,
                        "amendmentType": atype,
                        "reportDate": rd,
                        "filingDate": fdate,
                        "originalFilingDate": fdate,
                        "latestAmendmentDate": fdate,
                        "accessionNumber": item.get("accessionNumber", ""),
                    }
                    amendment_events = [{
                        "filingDate": fdate,
                        "form": ftype,
                        "amendmentType": atype,
                        "isReset": True,
                        "rows": ev_rows,
                    }]
                else:
                    meta["latestAmendmentDate"] = max(meta.get("latestAmendmentDate", fdate), fdate)
                    meta["amendmentType"] = atype or "NEW HOLDINGS"
                    if not meta.get("accessionNumber"):
                        meta["accessionNumber"] = item.get("accessionNumber", "")
                    inc_rows: list[dict[str, Any]] = []
                    for r in item["rows"]:
                        k = (r["cusip"], r["secType"])
                        rc = dict(r)
                        rc["disclosedDate"] = fdate
                        inc_rows.append(dict(rc))
                        if k not in merged:
                            merged[k] = rc
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

        if not cleaned:
            continue

        latest_rd = cleaned[-1]["reportDate"]
        age_days = (today_date - datetime.strptime(latest_rd, "%Y-%m-%d").date()).days
        if age_days <= 210:
            assembled[cik10] = cleaned

    stats = {
        "postBulkFilingsCount": len(post_tasks),
        "assembledHistoriesCount": len(assembled),
    }
    print(f"[stage-2] Assembled unbroken histories for {len(assembled)}/{len(target_ciks)} targets.", flush=True)
    return assembled, stats
