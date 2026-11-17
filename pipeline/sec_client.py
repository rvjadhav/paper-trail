"""
SEC EDGAR client for fetching 13F-HR / 13F-HR/A filings
and quarterly EFTS discovery of emerging concentrated tech funds.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import statistics
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from lxml import etree

from funds_config import DISCOVERY_SEED_PAIRS

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


def parse_13f_filing(filing: dict[str, Any]) -> dict[str, Any] | None:
    cik_int = int(filing["cik"])
    acc_nd = filing["accessionNumber"].replace("-", "")
    parsed_cache_path = CACHE_DIR / f"parsed_v1_{filing['cik']}_{acc_nd}.json"
    if parsed_cache_path.exists():
        try:
            cached = json.loads(parsed_cache_path.read_text(encoding="utf-8"))
            if cached.get("empty"):
                return None
            cached["entityName"] = filing["entityName"]
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

    raw_rows: list[dict[str, Any]] = []
    ratios: list[float] = []

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
        parsed_cache_path.write_text('{"empty":true}', encoding="utf-8")
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

    res = {
        "cik": filing["cik"],
        "entityName": filing["entityName"],
        "form": filing["form"],
        "amendmentType": amendment_type,
        "reportDate": filing["reportDate"],
        "filingDate": filing["filingDate"],
        "accessionNumber": filing["accessionNumber"],
        "rows": list(agg.values()),
    }
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


_ENTITY_TOKEN_RE = re.compile(
    r"\b(llc|lp|llp|ltd|inc|corp|co|plc|pte|sa|ag|gmbh|sarl|capital|management|mgmt|"
    r"partners|advisors|advisers|asset|assets|fund|funds|investment|investments|"
    r"global|equity|equities|holdings|office|group|associates|ventures|strategies)\b"
)


def _format_entity_name(cname: str) -> str:
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


def discover_emerging_tech_funds(known_ciks: set[str], max_candidates: int = 1000) -> list[dict[str, Any]]:
    """
    Scan recent SEC EDGAR 13F-HR filings via breadth-first paginated EFTS full-text
    queries across configured DISCOVERY_SEED_PAIRS CUSIP clusters,
    returning concentrated ($200M+ reported value) candidates not yet in the curated list.
    """
    now = datetime.now(timezone.utc)
    today_date = now.date()
    start_dt = (now - timedelta(days=145)).strftime("%Y-%m-%d")
    end_dt = now.strftime("%Y-%m-%d")

    exclude_keywords = (
        "bank", "bancorp", "banco ", "banque", "banca ", "caisse", "unibanco", "trust", "wealth",
        "financial advisor", "financial adviser", "financial planning", "financial services",
        "financial group", "financial counsel", "private wealth", "private bank", "trust company",
        "family wealth", "retirement", "insurance", "reinsurance", "assurance", "pension",
        "provident", "kranot", "university", "endowment", "foundation", "brokerage", "credit union",
        "savings", "clearing", "sovereign", "leasing", "bnp", "jpmorgan", "morgan stanley",
        "goldman", "wells fargo", "ubs", "barclays", "citigroup", "royal bank", "natwest", "mufg",
        "scotiabank", "sumitomo", "mitsui", "mitsubishi", "mizuho", "daiwa", "jefferies financial",
        "state street", "blackrock", "vanguard", "invesco", "schwab", "fidelity",
        "northern trust", "bny", "ameriprise", "raymond james", "edward jones",
        "stifel", "lpl", "merrill", "hsbc", "nomura", "credit suisse", "deutsche",
        "bochk", "pko ", "norges", "temasek", "mubadala", "korea investment", "british columbia",
        "harvard management", "gates foundation", "pacific life", "prudential", "metlife",
        "new york life", "nippon life", "asahi life", "empire life", "life investments",
        "life insurance", "life asset", "cna financial", "allstate", "chubb", "travelers",
        "aflac", "aviva", "aegon", "zurich", "manulife", "allianz", "axa", "tiaa",
        "geode", "dimensional", "nuveen", "macquarie", "amundi", "natixis",
        "legal & general", "schroder", "univest", "citizens & northern", "south plains financial",
    )

    # Build deduplicated EFTS CUSIP pair queries from funds_config.DISCOVERY_SEED_PAIRS
    queries: list[str] = []
    seen_pairs: set[tuple[str, str]] = set()
    for _ta, ca, _tb, cb, _theme in DISCOVERY_SEED_PAIRS:
        ca_u = ca.strip().upper()
        cb_u = cb.strip().upper()
        canon = (min(ca_u, cb_u), max(ca_u, cb_u))
        if not ca_u or not cb_u or ca_u == cb_u or canon in seen_pairs:
            continue
        seen_pairs.add(canon)
        queries.append(f'"{ca_u}" "{cb_u}"')

    discovered_ciks: list[tuple[str, str]] = []
    seen = set(known_ciks)
    raw_cap = max(max_candidates * 4, 4500)
    cluster_hits: dict[str, set[int]] = {}
    first_page_seen: dict[str, int] = {}
    exhausted_queries: set[str] = set()

    for page in range(10):
        offset = page * 100
        for ci, q_raw in enumerate(queries):
            if q_raw in exhausted_queries or len(discovered_ciks) >= raw_cap:
                continue
            q = urllib.parse.quote(q_raw)
            url = (
                f"https://efts.sec.gov/LATEST/search-index?forms=13F-HR&q={q}"
                f"&dateRange=custom&startdt={start_dt}&enddt={end_dt}&from={offset}&size=100"
            )
            q_hash = hashlib.md5(f"{q_raw}_p{page}".encode("utf-8")).hexdigest()[:12]
            try:
                data = fetch_json(url, cache_key=f"efts_{q_hash}_{end_dt}.json", max_age_hours=12)
            except Exception:
                continue

            hits = data.get("hits", {}).get("hits", [])
            if len(hits) < 100:
                exhausted_queries.add(q_raw)

            for hit in hits:
                src = hit.get("_source", {})
                ciks = src.get("ciks", [])
                names = src.get("display_names", [])
                for cik, dname in zip(ciks, names):
                    cik10 = f"{int(cik):010d}"
                    cluster_hits.setdefault(cik10, set()).add(ci)
                    if cik10 in seen:
                        continue
                    clean_name = dname.split("(CIK")[0].strip()
                    low = clean_name.lower()
                    if any(kw in low for kw in exclude_keywords):
                        seen.add(cik10)
                        continue
                    if not _ENTITY_TOKEN_RE.search(low.replace(".", "")):
                        seen.add(cik10)
                        continue
                    seen.add(cik10)
                    first_page_seen[cik10] = page
                    discovered_ciks.append((cik10, clean_name))
                    if len(discovered_ciks) >= raw_cap:
                        break
                if len(discovered_ciks) >= raw_cap:
                    break
        if len(discovered_ciks) >= raw_cap or len(exhausted_queries) == len(queries):
            break

    print(f"[discovery] Collected {len(discovered_ciks)} filtered candidate CIKs from {len(queries)} subsector clusters; pre-screening...", flush=True)

    # Concurrent pre-screen via submissions + latest primary_doc tableEntryTotal / tableValueTotal
    def _prescreen_one(item: tuple[int, str, str]) -> tuple[tuple[float, int], dict[str, Any]] | None:
        order_idx, cik10, cname = item
        try:
            sub = get_submissions(cik10)
            ent_name = sub.get("name") or cname
            ent_low = ent_name.lower()
            if any(kw in ent_low for kw in exclude_keywords):
                return None

            recent = sub.get("filings", {}).get("recent", {})
            forms = recent.get("form", [])
            accs = recent.get("accessionNumber", [])
            docs = recent.get("primaryDocument", [])
            rds = recent.get("reportDate", [])
            fds = recent.get("filingDate", [])

            recent_13f: list[dict[str, Any]] = []
            for form, acc, doc, rd, fd in zip(forms, accs, docs, rds, fds):
                if form in ("13F-HR", "13F-HR/A") and rd:
                    recent_13f.append({
                        "form": form,
                        "accessionNumber": acc,
                        "primaryDocument": doc,
                        "reportDate": rd,
                        "filingDate": fd,
                    })

            if not recent_13f:
                return None

            recent_13f.sort(key=lambda x: (x["reportDate"], x["filingDate"], x["accessionNumber"]))
            latest = recent_13f[-1]
            latest_rd = latest["reportDate"]
            age_days = (today_date - datetime.strptime(latest_rd, "%Y-%m-%d").date()).days
            if age_days > 210:
                return None

            acc_nd = latest["accessionNumber"].replace("-", "")
            cik_int = int(cik10)
            pdoc_base = (latest.get("primaryDocument") or "").split("/")[-1]
            raw = None
            if pdoc_base and pdoc_base.lower().endswith(".xml"):
                try:
                    raw = fetch_bytes(
                        f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_nd}/{pdoc_base}",
                        cache_key=f"xml_{cik10}_{acc_nd}_{pdoc_base}",
                    )
                except Exception:
                    raw = None
            if raw is None:
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
            if not raw:
                return None

            root = _parse_xml_root(raw)
            cnt = int(float(_find_text(root, "tableEntryTotal", "0") or 0))
            val = float(_find_text(root, "tableValueTotal", "0") or 0)
            # Pre-screen for $200M+ reported value and manageable entry count (allow puts/calls + up to ~650 lines)
            if not (1 <= cnt <= 650 and val >= 200_000_000):
                return None

            # Verify >= 4 distinct quarters of 13F history since 2024-01-01 (only after structure check passes)
            distinct_qtrs = {r["reportDate"] for r in recent_13f if r["reportDate"] >= "2024-01-01"}
            if len(distinct_qtrs) < 4:
                recent_fds = [d for d in fds if d]
                oldest_recent = min(recent_fds) if recent_fds else "9999-12-31"
                if oldest_recent > "2024-01-01":
                    filings_full = list_13f_filings(cik10, min_report_date="2024-01-01")
                    distinct_qtrs = {f["reportDate"] for f in filings_full}
                if len(distinct_qtrs) < 4:
                    return None

            # Prioritize multi-cluster tech/AI matches & focused books over single-pair sprawling RIAs when trimming to max_candidates
            ch = len(cluster_hits.get(cik10, ()))
            fp = first_page_seen.get(cik10, 9)
            conc_bonus = 2.0 if cnt <= 100 else (1.0 if cnt <= 200 else 0.0)
            page_bonus = max(0.0, 1.5 - 0.15 * fp)
            rank_score = min(10, ch) * 1.4 + conc_bonus + page_bonus

            fmt_name = _format_entity_name(cname)
            return (-rank_score, order_idx), {
                "cik": cik10,
                "name": fmt_name,
                "manager": "SEC EDGAR Discovered",
                "hq": "United States",
                "styleTag": "Discovered Tech Concentrated",
                "strategySummary": f"Automatically surfaced from SEC EDGAR quarterly 13F scan ({cnt} filing entries, {ch} seed clusters, AI/tech exposure).",
                "source": "EDGAR Discovered",
            }
        except Exception:
            return None

    matched: list[tuple[tuple[float, int], dict[str, Any]]] = []
    work_items = [(i, cik10, cname) for i, (cik10, cname) in enumerate(discovered_ciks)]
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = [ex.submit(_prescreen_one, w) for w in work_items]
        for done_cnt, fut in enumerate(as_completed(futs), 1):
            res = fut.result()
            if res is not None:
                matched.append(res)
            if done_cnt % 250 == 0 or done_cnt == len(work_items):
                print(f"[discovery] Pre-screened {done_cnt}/{len(work_items)} CIKs ({len(matched)} passed $200M+ & structure gate)", flush=True)

    matched.sort(key=lambda x: x[0])
    return [item for _, item in matched[:max_candidates]]

