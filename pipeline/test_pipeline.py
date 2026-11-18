"""
Deterministic offline unit tests covering all critical safeguards in the Paper Trail 13F pipeline.
Run with: /usr/bin/python3 -u pipeline/test_pipeline.py
"""

from __future__ import annotations

import json
import math
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

import numpy as np

import build_data
import enrichment
import funds_config
import sec_bulk
import sec_client
import security_resolver


class TestSecIngestionAndLookAhead(unittest.TestCase):
    def test_discover_bulk_zip_urls_live_html_precedence(self) -> None:
        fake_html = """
        <html><body>
          <a href="/files/datastandardsinnovation/data/form-13f-data-sets/01jun2026-31aug2026_form13f.zip">Latest</a>
          <a href="https://www.sec.gov/files/structureddata/data/form-13f-data-sets/01sep2026-30nov2026_form13f.zip">NewQ</a>
          <a href="/files/structureddata/data/form-13f-data-sets/2019q4_form13f.zip">OldPre2021</a>
        </body></html>
        """.encode("utf-8")
        with mock.patch.object(sec_bulk, "fetch_bytes", return_value=fake_html):
            urls = dict(sec_bulk._discover_bulk_zip_urls())

        self.assertEqual(
            urls["01jun2026-31aug2026_form13f.zip"],
            "https://www.sec.gov/files/datastandardsinnovation/data/form-13f-data-sets/01jun2026-31aug2026_form13f.zip",
        )
        self.assertIn("01sep2026-30nov2026_form13f.zip", urls)
        self.assertNotIn("2019q4_form13f.zip", urls)

    def test_fetch_post_bulk_efts_dedup_and_pagination(self) -> None:
        page0 = json.dumps({
            "hits": {
                "total": {"value": 2},
                "hits": [
                    {
                        "_source": {
                            "ciks": ["0001234567"],
                            "adsh": "0001234567-26-000010",
                            "file_date": "2026-11-16",
                            "period_ending": "2026-09-30",
                            "form": "13F-HR",
                            "display_names": ["ALPHA TECH CAPITAL (CIK 0001234567)"],
                        }
                    },
                    {
                        "_source": {
                            "ciks": ["1234567"],
                            "adsh": "0001234567-26-000010",  # duplicate across same CIK/adsh
                            "file_date": "2026-11-16",
                            "period_ending": "2026-09-30",
                            "form": "13F-HR",
                            "display_names": ["ALPHA TECH CAPITAL (CIK 0001234567)"],
                        }
                    },
                ],
            }
        }).encode("utf-8")
        with mock.patch.object(sec_bulk, "fetch_bytes", return_value=page0):
            res = sec_bulk.fetch_post_bulk_efts("2026-08-31", date(2026, 11, 17), lookback_days=5)

        self.assertIn("0001234567", res)
        self.assertEqual(len(res["0001234567"]), 1)
        self.assertEqual(res["0001234567"][0]["entityName"], "ALPHA TECH CAPITAL")
        self.assertEqual(res["0001234567"][0]["txtFilename"], "edgar/data/1234567/0001234567-26-000010.txt")

    def test_amendment_events_zero_lookahead_and_stale_backfiling_guard(self) -> None:
        # Build snapshot via _build_bulk_period_snapshots with initial 13F-HR + confidential NEW HOLDINGS 13F-HR/A
        acc1 = "0001000001-26-000001"
        acc2 = "0001000001-26-000002"
        q2_metas = [
            ("2026-08-14", acc1, "13F-HR", "", "TEST FUND LP", "2026-06-30", "0001000001"),
            ("2026-09-15", acc2, "13F-HR/A", "NEW HOLDINGS", "TEST FUND LP", "2026-06-30", "0001000001"),
        ]
        rows_by_acc = {
            acc1: [{"cusip": "67066G104", "issuer": "NVIDIA", "title": "COM", "rawValue": 100_000_000.0, "shares": 1_000_000.0, "secType": "EQUITY"}],
            acc2: [{"cusip": "594918104", "issuer": "MICROSOFT", "title": "COM", "rawValue": 100_000_000.0, "shares": 250_000.0, "secType": "EQUITY"}],
        }
        ratios = {acc1: ([100.0], [100.0]), acc2: ([400.0], [400.0])}
        q2_snap = sec_bulk._build_bulk_period_snapshots(q2_metas, rows_by_acc, ratios)
        self.assertIsNotNone(q2_snap)
        assert q2_snap is not None
        self.assertEqual(len(q2_snap["amendmentEvents"]), 2)
        self.assertEqual(q2_snap["originalFilingDate"], "2026-08-14")
        self.assertEqual(q2_snap["latestAmendmentDate"], "2026-09-15")
        self.assertEqual(len(q2_snap["rows"]), 2)

        # Q3 filed on 2026-10-15, plus a late back-dated Q1 RESTATEMENT filed on 2026-11-01
        q1_stale_restatement = {
            "cik": "0001000001",
            "entityName": "TEST FUND LP",
            "form": "13F-HR/A",
            "amendmentType": "RESTATEMENT",
            "reportDate": "2026-03-31",
            "filingDate": "2026-11-01",
            "accessionNumber": "0001000001-26-000099",
            "rows": [{"cusip": "037833100", "issuer": "AAPL", "title": "COM", "secType": "EQUITY", "valueUsd": 100_000_000.0, "shares": 500_000.0}],
            "amendmentEvents": [{
                "filingDate": "2026-11-01",
                "form": "13F-HR/A",
                "amendmentType": "RESTATEMENT",
                "isReset": True,
                "rows": [{"cusip": "037833100", "issuer": "AAPL", "title": "COM", "secType": "EQUITY", "valueUsd": 100_000_000.0, "shares": 500_000.0}],
            }],
        }
        q3_snap = {
            "cik": "0001000001",
            "entityName": "TEST FUND LP",
            "form": "13F-HR",
            "amendmentType": "",
            "reportDate": "2026-09-30",
            "filingDate": "2026-10-15",
            "accessionNumber": "0001000001-26-000050",
            "rows": [{"cusip": "30303M102", "issuer": "META", "title": "COM", "secType": "EQUITY", "valueUsd": 120_000_000.0, "shares": 200_000.0}],
            "amendmentEvents": [{
                "filingDate": "2026-10-15",
                "form": "13F-HR",
                "amendmentType": "",
                "isReset": True,
                "rows": [{"cusip": "30303M102", "issuer": "META", "title": "COM", "secType": "EQUITY", "valueUsd": 120_000_000.0, "shares": 200_000.0}],
            }],
        }

        cal_dates = [
            "2026-06-30", "2026-08-14", "2026-09-15",
            "2026-09-30", "2026-10-15", "2026-11-01", "2026-11-10",
        ]
        cusip_meta = {
            "67066G104": {"ticker": "NVDA", "isTechTmt": True},
            "594918104": {"ticker": "MSFT", "isTechTmt": True},
            "037833100": {"ticker": "AAPL", "isTechTmt": True},
            "30303M102": {"ticker": "META", "isTechTmt": True},
        }
        _, lag_segs, _, _, _, _ = build_data.build_fund_timelines(
            [q1_stale_restatement, q2_snap, q3_snap], cusip_meta, cal_dates, len(cal_dates)
        )
        # Timeline segments:
        # 2026-08-14 -> 100% NVDA (zero look-ahead on MSFT confidential position!)
        # 2026-09-15 -> 50% NVDA / 50% MSFT (after confidential amendment filed)
        # 2026-10-15 -> 100% META
        # 2026-11-01 stale Q1 restatement is ignored -> META stays at 100%, AAPL NOT injected!
        self.assertEqual(len(lag_segs), 3)
        self.assertEqual(lag_segs[0], (1, {"NVDA": 1.0}))
        self.assertEqual(lag_segs[1][0], 2)
        self.assertAlmostEqual(lag_segs[1][1]["NVDA"], 0.5, places=4)
        self.assertAlmostEqual(lag_segs[1][1]["MSFT"], 0.5, places=4)
        self.assertEqual(lag_segs[2], (4, {"META": 1.0}))


class TestCusipResolverHygiene(unittest.TestCase):
    def test_umbrella_poison_purge_synthetic_options_and_inmemory_dust(self) -> None:
        self.assertEqual(security_resolver._build_yahoo_fallback_query("BLACKROCK ETF TRUST", "COM"), "")
        self.assertEqual(security_resolver._build_yahoo_fallback_query("TIDAL TRUST II", "YIELDMAX NVDA"), "TIDAL YIELDMAX NVDA")

        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            cusip_cache_p = td_path / "cusip_cache.json"
            ticker_meta_p = td_path / "ticker_meta_cache.json"
            sec_tickers_p = td_path / "company_tickers.json"

            # Seed poisoned GARP entry, unattempted legacy CUSIP: entry, and clean NVDA entry
            cusip_cache_p.write_text(json.dumps({
                "111111111": {"ticker": "GARP", "name": "BlackRock ETF Trust", "sector": ""},
                "222222222": {"ticker": "CUSIP:222222222", "name": "Unattempted Old"},
                "67066G104": {"ticker": "NVDA", "name": "NVIDIA Corporation", "sector": "Technology"},
            }), encoding="utf-8")
            ticker_meta_p.write_text(json.dumps({
                "NVDA": {"ticker": "NVDA", "name": "NVIDIA Corporation", "sector": "Technology", "industry": "Semiconductors", "metaChecked": True},
            }), encoding="utf-8")
            sec_tickers_p.write_text(json.dumps({}), encoding="utf-8")

            # Generate 405 unresolvable dust CUSIPs + 1 synthetic option CUSIP 67066G904
            inputs = {
                "67066G904": {"issuer": "NVIDIA CORP", "title": "CALL"},
                **{f"99900{i:04d}": {"issuer": f"TINY PRIVATE {i}", "title": "COM"} for i in range(405)},
            }
            prio = {"67066G904": 1e9, **{f"99900{i:04d}": float(500 - i) for i in range(405)}}

            with (
                mock.patch.object(security_resolver, "CUSIP_CACHE_PATH", cusip_cache_p),
                mock.patch.object(security_resolver, "TICKER_META_PATH", ticker_meta_p),
                mock.patch.object(security_resolver, "SEC_COMPANY_TICKERS_PATH", sec_tickers_p),
                mock.patch.object(security_resolver, "_openfigi_batch", side_effect=lambda jobs: [{} for _ in jobs]),
                mock.patch.object(security_resolver, "_yahoo_search", return_value=None),
            ):
                out_map = security_resolver.resolve_cusips(inputs, cusip_priority=prio, max_openfigi_batches=1)

            persisted = json.loads(cusip_cache_p.read_text(encoding="utf-8"))
            # Poisoned GARP & unattempted 222222222 must be purged from persisted cache
            self.assertNotIn("111111111", persisted)
            self.assertNotIn("222222222", persisted)
            # Synthetic option CUSIP 67066G904 resolved cleanly to NVDA
            self.assertEqual(persisted["67066G904"]["ticker"], "NVDA")
            # Tail dust placeholders returned in out_map but NOT written to cusip_cache.json
            dust_count_on_disk = sum(1 for k in persisted if k.startswith("99900") and not persisted[k].get("resolvedFail"))
            self.assertEqual(dust_count_on_disk, 0)
            self.assertIn("999000404", out_map)
            self.assertTrue(out_map["999000404"]["unpriced"])


class TestPortfolioMathAndPriceSanitization(unittest.TestCase):
    def test_extract_weights_etf_strip_and_dual_class(self) -> None:
        rows = [
            {"cusip": "AAA", "secType": "EQUITY", "valueUsd": 50.0, "shares": 10},
            {"cusip": "BBB", "secType": "EQUITY", "valueUsd": 30.0, "shares": 10},
            {"cusip": "CCC", "secType": "EQUITY", "valueUsd": 20.0, "shares": 10},  # SGOV treasury ETF
            {"cusip": "DDD", "secType": "PUT", "valueUsd": 100.0, "shares": 10},
        ]
        meta = {
            "AAA": {"ticker": "GOOGL", "isTechTmt": True},
            "BBB": {"ticker": "GOOG", "isTechTmt": True},   # merges into GOOGL
            "CCC": {"ticker": "SGOV", "isTechTmt": False},  # stripped from single-stock AUM
            "DDD": {"ticker": "QQQ", "isTechTmt": False},
        }
        w, single_eq, total_eq, rep, pos_cnt, sig_cnt, top10, tech, max1, etf_pct = build_data.extract_weights_from_rows(rows, meta)
        self.assertAlmostEqual(single_eq, 80.0)
        self.assertAlmostEqual(total_eq, 100.0)
        self.assertAlmostEqual(rep, 200.0)
        self.assertEqual(pos_cnt, 1)
        self.assertAlmostEqual(etf_pct, 20.0)
        self.assertAlmostEqual(w["GOOGL"], 1.0)
        self.assertAlmostEqual(tech, 100.0)

    def test_split_repair_and_sub_dollar_distress_decay(self) -> None:
        cal = [f"2026-01-{d:02d}" for d in range(1, 31)] + [f"2026-02-{d:02d}" for d in range(1, 20)]
        # Unrecorded 10:1 forward split on day 15 (100 -> 10), then flat
        raw_split = {d: (100.0 if i < 15 else 10.0) for i, d in enumerate(cal)}
        # Delisted penny stub ending at $0.40 on day 10 -> decays smoothly to 0 over 20d
        raw_penny = {d: 0.40 for d in cal[:10]}
        aligned = build_data.build_aligned_price_matrix(cal, {"SPLT": raw_split, "DEAD": raw_penny})

        splt_arr = aligned["SPLT"][0]
        self.assertAlmostEqual(float(splt_arr[0]), 10.0, places=3)
        self.assertAlmostEqual(float(splt_arr[-1]), 10.0, places=3)

        dead_arr = aligned["DEAD"][0]
        self.assertGreater(float(dead_arr[9]), 0.39)
        self.assertLess(float(dead_arr[19]), 0.25)
        self.assertAlmostEqual(float(dead_arr[-1]), 0.0, places=4)

    def test_nav_coverage_gate_weight_renorm_and_nan_recovery(self) -> None:
        n_dates = 60
        # Asset A has realistic up/down fluctuations totaling ~+50% over 60 days; QQQ has smaller moves
        steps = np.array([1.0 + (0.025 if i % 2 == 0 else -0.011) for i in range(n_dates - 1)])
        arr_a = np.concatenate([[100.0], 100.0 * np.cumprod(steps)])
        q_steps = np.array([1.0 + (0.012 if i % 2 == 0 else -0.008) for i in range(n_dates - 1)])
        arr_q = np.concatenate([[100.0], 100.0 * np.cumprod(q_steps)])
        aligned = {"A": (arr_a, 0, n_dates - 1)}
        # Seg 0 (day 0): 50% A + 50% B -> <70% covered -> NaN break!
        # Seg 1 (day 20): 80% A + 20% B -> >=70% covered -> renormalized 100% A!
        nav = build_data.simulate_nav_series(
            [(0, {"A": 0.5, "B": 0.5}), (20, {"A": 0.8, "B": 0.2})],
            aligned,
            n_dates,
            min_priced_coverage=0.70,
        )
        self.assertTrue(math.isnan(float(nav[0])))
        self.assertTrue(math.isnan(float(nav[19])))
        self.assertAlmostEqual(float(nav[20]), 100.0, places=4)
        # Because A's weight was renormalized (not cash-diluted), nav[-1] tracks A's exact ratio
        expected = 100.0 * (float(arr_a[-1]) / float(arr_a[20]))
        self.assertAlmostEqual(float(nav[-1]), expected, places=4)

        win_idx = {"YTD": 0, "TTM": 22, "18M": 0, "2Y": 0, "3Y": 0, "5Y": 0}
        rets, metrics = build_data.compute_returns_and_metrics(nav, arr_q, win_idx)
        # YTD requires contig_start <= win_idx["YTD"] + 5 -> None since contig_start == 20
        self.assertIsNone(rets["YTD"])
        # TTM (start=22 >= contig_start=20) is valid
        self.assertIsNotNone(rets["TTM"])
        self.assertIsNotNone(metrics["sortinoRatio"])
        self.assertIsNotNone(metrics["infoRatioVsQqq"])
        self.assertIsNotNone(metrics["sharpeProxy"])
        self.assertIsNotNone(metrics["betaVsQqq"])


class TestSignalsAndEnrichment(unittest.TestCase):
    def _mock_filing(self, rd: str, fd: str, holdings: list[tuple[str, float, float]]) -> dict:
        return {
            "cik": "0001000001",
            "entityName": "ALPHA TECH LP",
            "form": "13F-HR",
            "amendmentType": "",
            "reportDate": rd,
            "filingDate": fd,
            "originalFilingDate": fd,
            "latestAmendmentDate": fd,
            "accessionNumber": f"0001000001-26-{fd[-2:]}",
            "rows": [
                {
                    "cusip": c,
                    "issuer": c,
                    "title": "COM",
                    "secType": "EQUITY",
                    "shares": sh,
                    "valueUsd": val,
                }
                for c, sh, val in holdings
            ],
        }

    def test_five_percent_threshold_and_signal_screener_buckets(self) -> None:
        cusip_meta = {
            "C_NVDA": {"ticker": "NVDA", "name": "NVIDIA", "sector": "Technology", "industry": "Semiconductors", "isTechTmt": True},
            "C_CRWD": {"ticker": "CRWD", "name": "CrowdStrike", "sector": "Technology", "industry": "Software—Infrastructure", "isTechTmt": True},
            "C_SNAP": {"ticker": "SNAP", "name": "Snap Inc.", "sector": "Communication Services", "industry": "Internet Content & Information", "isTechTmt": True},
        }
        cand1 = {"cik": "0001000001", "name": "Alpha Tech", "manager": "M1", "styleTag": "TMT", "source": "Curated Core"}
        cand2 = {"cik": "0001000002", "name": "Beta Growth", "manager": "M2", "styleTag": "TMT", "source": "Curated Core"}

        # Fund 1: +4% NVDA (UNCHANGED!), NEW CRWD, EXITED SNAP
        prev1 = self._mock_filing("2026-06-30", "2026-08-14", [("C_NVDA", 1000, 100e6), ("C_SNAP", 1000, 40e6)])
        curr1 = self._mock_filing("2026-09-30", "2026-11-14", [("C_NVDA", 1040, 104e6), ("C_CRWD", 500, 50e6)])
        u1 = build_data.build_fund_update_entry(cand1, [prev1, curr1], cusip_meta, True, ["TTM"])
        assert u1 is not None
        by_t1 = {m["ticker"]: m["action"] for m in u1["allMoves"]}
        self.assertEqual(by_t1["NVDA"], "UNCHANGED")  # +4% < 5% noise band
        self.assertEqual(by_t1["CRWD"], "NEW")
        self.assertEqual(by_t1["SNAP"], "EXITED")

        # Fund 2: NEW CRWD, EXITED SNAP, ADDED +6% NVDA (>=5%)
        prev2 = self._mock_filing("2026-06-30", "2026-08-14", [("C_NVDA", 1000, 100e6), ("C_SNAP", 1000, 40e6)])
        curr2 = self._mock_filing("2026-09-30", "2026-11-14", [("C_NVDA", 1060, 106e6), ("C_CRWD", 500, 50e6)])
        u2 = build_data.build_fund_update_entry(cand2, [prev2, curr2], cusip_meta, True, ["TTM"])
        assert u2 is not None
        u1["fundTier"] = "Elite 50"
        u2["fundTier"] = "Elite 50"

        f1 = {
            "cik": "0001000001", "name": "Alpha Tech", "fundTier": "Elite 50", "isLeaderboardEligible": True,
            "holdings": [{"cusip": "C_NVDA", "ticker": "NVDA", "name": "NVIDIA", "sector": "Technology", "industry": "Semiconductors", "isTechTmt": True, "securityType": "EQUITY", "weightPct": 67.5, "valueUsd": 104e6, "status": "UNCHANGED"},
                         {"cusip": "C_CRWD", "ticker": "CRWD", "name": "CrowdStrike", "sector": "Technology", "industry": "Software—Infrastructure", "isTechTmt": True, "securityType": "EQUITY", "weightPct": 32.5, "valueUsd": 50e6, "status": "NEW"}],
        }
        f2 = {
            "cik": "0001000002", "name": "Beta Growth", "fundTier": "Elite 50", "isLeaderboardEligible": True,
            "holdings": [{"cusip": "C_NVDA", "ticker": "NVDA", "name": "NVIDIA", "sector": "Technology", "industry": "Semiconductors", "isTechTmt": True, "securityType": "EQUITY", "weightPct": 67.9, "valueUsd": 106e6, "status": "ADDED"},
                         {"cusip": "C_CRWD", "ticker": "CRWD", "name": "CrowdStrike", "sector": "Technology", "industry": "Software—Infrastructure", "isTechTmt": True, "securityType": "EQUITY", "weightPct": 32.1, "valueUsd": 50e6, "status": "NEW"}],
        }
        scr = build_data.build_signal_screener([f1, f2], [u1, u2], cusip_meta, {}, {"TTM": 0})
        self.assertEqual([x["ticker"] for x in scr["clusterInitiations"]], ["CRWD"])
        self.assertEqual([x["ticker"] for x in scr["smartMoneyExits"]], ["SNAP"])
        self.assertTrue(any(x["ticker"] == "CRWD" for x in scr["contrarianBets"]))

    def test_q_vwap_and_xbrl_parser_and_shards(self) -> None:
        ohlcv = {
            "2026-07-15": [100.0, 95.0, 105.0, 1_000_000.0],
            "2026-08-15": [110.0, 102.0, 112.0, 2_000_000.0],
        }
        # VWAP = (100*1 + 110*2) / 3 = 106.67
        vw = enrichment.compute_quarter_vwap_metrics(ohlcv, "2026-09-30", 98.0)
        self.assertAlmostEqual(vw["estQuarterVwap"], 106.67, places=2)
        self.assertEqual(vw["quarterLow"], 95.0)
        self.assertEqual(vw["quarterHigh"], 112.0)
        self.assertEqual(vw["entryZoneStatus"], "Discount to Q-Avg")

        # XBRL parser falls back cleanly to aligned full-year 10-K figures when 90d OCF missing
        fake_facts = {
            "facts": {
                "dei": {"EntityCommonStockSharesOutstanding": {"units": {"shares": [{"end": "2026-09-30", "filed": "2026-10-20", "val": 1_000_000}]}}},
                "us-gaap": {
                    "Revenues": {"units": {"USD": [
                        {"start": "2025-01-01", "end": "2025-12-31", "filed": "2026-02-20", "val": 100.0},
                        {"start": "2024-01-01", "end": "2024-12-31", "filed": "2025-02-20", "val": 80.0},
                    ]}},
                    "NetCashProvidedByUsedInOperatingActivities": {"units": {"USD": [
                        {"start": "2025-01-01", "end": "2025-12-31", "filed": "2026-02-20", "val": 40.0},
                    ]}},
                    "PaymentsToAcquirePropertyPlantAndEquipment": {"units": {"USD": [
                        {"start": "2025-01-01", "end": "2025-12-31", "filed": "2026-02-20", "val": 10.0},
                    ]}},
                },
            }
        }
        xb = enrichment._parse_company_xbrl_facts(fake_facts)
        self.assertEqual(xb["sharesOutstanding"], 1_000_000)
        self.assertAlmostEqual(xb["revenueGrowthYoYPct"], 25.0)
        self.assertAlmostEqual(xb["fcfMarginPct"], 30.0)

        self.assertEqual(build_data.fund_shard_id("0001067983"), f"{1067983 % 16:x}")
        self.assertIn(build_data.stock_shard_id("NVDA"), range(8))

    def test_institution_exclusions_subsector_and_midquarter_fallback(self) -> None:
        # Broad mutual complexes, banks, clearing brokers & foreign pensions excluded
        for bad in (
            "FMR LLC",
            "Swedbank AB",
            "Clear Street Group Inc.",
            "Altshuler Shaham Ltd.",
            "Generali Powszechne Towarzystwo Emerytalne",
            "VAN ECK Associates CORP",
            "Capital International Ltd. /Ca/",
            "E. Ohman J:Or Asset Management AB",
        ):
            self.assertIsNotNone(sec_bulk._EXCLUDE_INSTITUTION_RE.search(bad), f"Expected exclusion: {bad}")

        # Boutique tech funds & L1 Capital International must NOT be false-positive excluded
        for keep in (
            "Fenghe Fund Management Pte. Ltd.",
            "Optimus Prime Fund Management CO., Ltd.",
            "L1 Capital International Pty Ltd.",
            "Dock Street Asset Management Inc.",
        ):
            self.assertIsNone(sec_bulk._EXCLUDE_INSTITUTION_RE.search(keep), f"Unexpected false exclusion: {keep}")

        # Sub-sector classification correctness
        self.assertEqual(build_data.classify_sub_sector("Consumer Cyclical", "Auto & Truck Dealerships", True), "Consumer & E-Commerce")
        self.assertEqual(build_data.classify_sub_sector("Consumer Cyclical", "Auto Manufacturers", True), "Consumer & E-Commerce")
        self.assertEqual(build_data.classify_sub_sector("Industrials", "Electrical Equipment & Parts", True), "AI Power & Grid")
        self.assertEqual(build_data.classify_sub_sector("Financial Services", "Banks—Regional", True), "Fintech & Payments")
        self.assertEqual(build_data.classify_sub_sector("Communication Services", "Advertising Agencies", True), "Platforms & Interactive")

        # Mid-quarter Schedule 13G/A ticker resolution via override name lookup + get_submissions fallback
        fake_filer_sub = {
            "filings": {
                "recent": {
                    "form": ["SCHEDULE 13G/A", "SCHEDULE 13G"],
                    "accessionNumber": ["0001000001-26-000011", "0001000001-26-000012"],
                    "filingDate": ["2026-11-16", "2026-11-15"],
                }
            }
        }
        fake_sub_issuer = {"tickers": ["NEWT"]}
        with tempfile.TemporaryDirectory() as td:
            hdr_cache_p = Path(td) / "hdr_cache.json"
            hdr_cache_p.write_text(
                json.dumps({
                    "0001000001-26-000011": {"subjectName": "ACV Auctions Inc.", "subjectCik": "0001637873"},
                    "0001000001-26-000012": {"subjectName": "New Tech Corp", "subjectCik": "0001999999"},
                }),
                encoding="utf-8",
            )
            ov = security_resolver.MANUAL_CUSIP_OVERRIDES["00091G104"]
            name_map = {security_resolver._normalize_legal_name(ov["name"]): ov["ticker"]}

            def _mock_sub(cik: str) -> dict:
                if cik == "0001000001":
                    return fake_filer_sub
                if cik == "0001999999":
                    return fake_sub_issuer
                return {}

            with (
                mock.patch.object(enrichment, "MIDQUARTER_HDR_CACHE_PATH", hdr_cache_p),
                mock.patch.object(enrichment, "get_submissions", side_effect=_mock_sub),
            ):
                alerts = enrichment.fetch_midquarter_alerts(
                    elite_ciks=["0001000001"],
                    fund_names_by_cik={"0001000001": "Atreides Management"},
                    cik_to_ticker_map={},
                    name_to_ticker_map=name_map,
                    today_date=date(2026, 11, 17),
                    since_days=30,
                )
        by_acc = {a["accessionNumber"]: a for a in alerts}
        self.assertEqual(by_acc["0001000001-26-000011"]["ticker"], "ACVA")
        self.assertEqual(by_acc["0001000001-26-000012"]["ticker"], "NEWT")

    def test_dynamic_non_stock_etf_filter_and_screener_exclusion(self) -> None:
        # Static & dynamically matched ETFs / mutual funds must be excluded
        for tk, nm, sec, ind in (
            ("DRAM", "", "", ""),
            ("IEI", "", "", ""),
            ("VO", "", "", ""),
            ("LUXE", "Tema Luxury ETF", "Consumer Cyclical", ""),
            ("ETHE", "Grayscale Ethereum Trust ETF", "Other", ""),
            ("XYZQ", "Roundhill Memory ETF", "Other", ""),
            ("AGTHX", "The Growth Fund of America", "Other", ""),
            ("SWVXX", "Schwab Value Advantage Money Fund", "", ""),
        ):
            self.assertTrue(
                funds_config.is_non_stock_etf(tk, nm, sec, ind),
                f"Expected ETF/fund exclusion for {tk} ({nm})",
            )

        # Operating companies with valid GICS sectors & industries must NEVER be false-excluded
        for tk, nm, sec, ind in (
            ("AMPY", "Amplify Energy Corp.", "Energy", "Oil & Gas E&P"),
            ("IVZ", "Invesco Ltd.", "Financial Services", "Asset Management"),
            ("IVR", "Invesco Mortgage Capital Inc.", "Real Estate", "REIT—Mortgage"),
            ("WT", "WisdomTree, Inc.", "Financial Services", "Asset Management"),
            ("STRIX", "Strix Electronics Inc.", "Technology", "Electronic Components"),
        ):
            self.assertFalse(
                funds_config.is_non_stock_etf(tk, nm, sec, ind),
                f"Unexpected ETF false-positive for operating company {tk} ({nm})",
            )

        # extract_weights_from_rows & build_signal_screener strip dynamically flagged ETFs
        cusip_meta = {
            "C_STRIX": {"ticker": "STRIX", "name": "Strix Electronics Inc.", "sector": "Technology", "industry": "Electronic Components", "isTechTmt": True},
            "C_RUND": {"ticker": "XYZQ", "name": "Roundhill Memory ETF", "sector": "Other", "industry": "", "isTechTmt": False, "isNonStockEtf": True},
        }
        excluded = build_data._build_excluded_etf_tickers(cusip_meta)
        self.assertIn("XYZQ", excluded)
        self.assertNotIn("STRIX", excluded)

        rows = [
            {"cusip": "C_STRIX", "secType": "EQUITY", "valueUsd": 60.0, "shares": 100},
            {"cusip": "C_RUND", "secType": "EQUITY", "valueUsd": 40.0, "shares": 100},
        ]
        w, single_eq, total_eq, _, pos_cnt, _, _, _, _, etf_pct = build_data.extract_weights_from_rows(
            rows, cusip_meta, excluded_etfs=excluded
        )
        self.assertEqual(list(w.keys()), ["STRIX"])
        self.assertAlmostEqual(single_eq, 60.0)
        self.assertAlmostEqual(total_eq, 100.0)
        self.assertEqual(pos_cnt, 1)
        self.assertAlmostEqual(etf_pct, 40.0)

        prev1_ok = self._mock_filing("2026-06-30", "2026-08-14", [("C_AAPL_PREV", 1, 1e6)])
        curr1 = self._mock_filing("2026-09-30", "2026-11-14", [("C_STRIX", 100, 60e6), ("C_RUND", 100, 40e6)])
        c1 = {"cik": "0001000001", "name": "Alpha Tech", "manager": "M1", "styleTag": "TMT"}
        c2 = {"cik": "0001000002", "name": "Beta Tech", "manager": "M2", "styleTag": "TMT"}
        u1 = build_data.build_fund_update_entry(c1, [prev1_ok, curr1], cusip_meta, True, ["TTM"])
        u2 = build_data.build_fund_update_entry(c2, [prev1_ok, curr1], cusip_meta, True, ["TTM"])
        assert u1 is not None and u2 is not None
        f1 = {
            "cik": "0001000001", "name": "Alpha Tech", "fundTier": "Elite 50", "isLeaderboardEligible": True,
            "holdings": [
                {"cusip": "C_STRIX", "ticker": "STRIX", "name": "Strix Electronics Inc.", "sector": "Technology", "industry": "Electronic Components", "isTechTmt": True, "securityType": "EQUITY", "weightPct": 100.0, "valueUsd": 60e6, "status": "NEW"},
                {"cusip": "C_RUND", "ticker": "XYZQ", "name": "Roundhill Memory ETF", "sector": "Other", "industry": "", "isTechTmt": False, "securityType": "EQUITY", "weightPct": 0.0, "valueUsd": 40e6, "status": "NEW"},
            ],
        }
        f2 = {**f1, "cik": "0001000002", "name": "Beta Tech"}
        scr = build_data.build_signal_screener([f1, f2], [u1, u2], cusip_meta, {}, {"TTM": 0})
        all_scr_tickers = {x["ticker"] for k in ("clusterInitiations", "heavyAdds", "smartMoneyExits", "contrarianBets") for x in scr[k]}
        self.assertIn("STRIX", all_scr_tickers)
        self.assertNotIn("XYZQ", all_scr_tickers)

    def test_ideal_backtest_year1_filing_lag_alignment(self) -> None:
        # Build ~300 daily trading dates starting 2021-01-04
        cal_dates: list[str] = []
        d_cur = date(2021, 1, 4)
        while len(cal_dates) < 305:
            if d_cur.weekday() < 5:
                cal_dates.append(d_cur.strftime("%Y-%m-%d"))
            d_cur += timedelta(days=1)

        n_dates = len(cal_dates)
        tickers = ["NVDA", "MSFT", "AAPL", "CRWD", "META"]
        aligned_prices = {}
        for idx_t, tk in enumerate(tickers):
            arr = np.array([100.0 + 0.1 * i + idx_t for i in range(n_dates)], dtype=np.float64)
            aligned_prices[tk] = (arr, 0, n_dates - 1)

        w_eq = {tk: 0.2 for tk in tickers}
        sh_eq = {tk: 1000.0 for tk in tickers}

        # 6 PIT funds: 5 debuting around index 30 (2021-02-16 Q4-2020 45-day lag), 1 early filer at index 0
        ciks = [f"000100000{i}" for i in range(1, 7)]
        snaps_by_cik: dict[str, list[dict]] = {}
        navs_by_cik: dict[str, np.ndarray] = {}
        for idx_c, cik in enumerate(ciks):
            snaps_by_cik[cik] = [
                {
                    "effDate": "2021-08-14",
                    "reportDate": "2021-06-30",
                    "weights": w_eq,
                    "shares": sh_eq,
                    "eqAum": 500_000_000.0,
                    "techPct": 100.0,
                    "effN": 5.0,
                    "maxSingle": 20.0,
                    "posCount": 5,
                },
                {
                    "effDate": "2021-11-12",
                    "reportDate": "2021-09-30",
                    "weights": w_eq,
                    "shares": sh_eq,
                    "eqAum": 550_000_000.0,
                    "techPct": 100.0,
                    "effN": 5.0,
                    "maxSingle": 20.0,
                    "posCount": 5,
                },
            ]
            nav = np.full(n_dates, np.nan, dtype=np.float64)
            start_pos = 0 if idx_c == 0 else 30
            nav[start_pos:] = np.array([100.0 * (1.0 + 0.001 * (j - start_pos) * (idx_c + 1)) for j in range(start_pos, n_dates)])
            navs_by_cik[cik] = nav

        segs = build_data.run_ideal_portfolio_backtest(
            bt_mode="filing_lag",
            all_quarters=["2021-09-30", "2021-12-31"],
            all_bt_ciks=ciks,
            snaps_by_cik=snaps_by_cik,
            navs_by_cik=navs_by_cik,
            cal_dates=cal_dates,
            as_of_date=cal_dates[-1],
            aligned_prices=aligned_prices,
            ticker_is_tech={tk: True for tk in tickers},
            curated_bt_ciks=set(),
        )
        # Q3-2021 filing_lag (2021-11-15) must NOT be skipped due to s_h=0 NaN gap
        expected_q3_idx = build_data.find_idx_on_or_after(cal_dates, "2021-11-15")
        self.assertIsNotNone(expected_q3_idx)
        self.assertGreaterEqual(len(segs), 1)
        self.assertEqual(segs[0][0], expected_q3_idx)
        bt_nav = build_data.simulate_nav_series(segs, aligned_prices, n_dates)
        self.assertFalse(math.isnan(float(bt_nav[expected_q3_idx])))
        self.assertFalse(math.isnan(float(bt_nav[-1])))


class TestAuditedFixesP0P1(unittest.TestCase):
    def _mock_filing(self, cik: str, rd: str, fd: str, holdings: list[tuple[str, float, float]]) -> dict:
        return {
            "cik": cik,
            "entityName": f"FUND {cik}",
            "form": "13F-HR",
            "amendmentType": "",
            "reportDate": rd,
            "filingDate": fd,
            "originalFilingDate": fd,
            "latestAmendmentDate": fd,
            "accessionNumber": f"{cik}-26-{rd[-5:].replace('-', '')}",
            "rows": [
                {
                    "cusip": c,
                    "issuer": c,
                    "title": "COM",
                    "secType": "EQUITY",
                    "shares": sh,
                    "valueUsd": val,
                }
                for c, sh, val in holdings
            ],
        }

    def test_split_adjustments_est_flow_and_dust_stub_suppression(self) -> None:
        splits_map = {
            "NVDA": [{"date": "2024-06-10", "factor": 10.0, "ratio": "10:1"}],
            "SMCI": [{"date": "2024-10-01", "factor": 10.0, "ratio": "10:1"}],
            "REV": [{"date": "2024-08-01", "factor": 0.2, "ratio": "1:5"}],
        }
        fac_f, lbl_f = security_resolver.split_factor_between("NVDA", "2024-03-31", "2024-06-30", splits_map)
        self.assertAlmostEqual(fac_f, 10.0)
        self.assertEqual(lbl_f, "10:1")

        # Exact boundary matching: split on prev_end excluded, on curr_end included
        fac_r, lbl_r = security_resolver.split_factor_between("REV", "2024-06-30", "2024-09-30", splits_map)
        self.assertAlmostEqual(fac_r, 0.2)
        self.assertEqual(lbl_r, "1:5")
        fac_out, _ = security_resolver.split_factor_between("SMCI", "2024-06-30", "2024-09-30", splits_map)
        self.assertAlmostEqual(fac_out, 1.0)

        cusip_meta = {
            "C_NVDA": {"ticker": "NVDA", "name": "NVIDIA", "sector": "Technology", "industry": "Semiconductors", "isTechTmt": True},
            "C_REV": {"ticker": "REV", "name": "RevCo", "sector": "Technology", "industry": "Software—Application", "isTechTmt": True},
            "C_STUB": {"ticker": "STUB", "name": "StubCo", "sector": "Technology", "industry": "Software—Infrastructure", "isTechTmt": True},
        }
        cand = {"cik": "0001000001", "name": "Alpha Tech", "manager": "M1", "styleTag": "TMT"}
        # Q1->Q2: NVDA 10:1 split (100k -> 1,000k shares = flat!), REV 1:5 reverse split (500k -> 100k = flat!), STUB 10 sh ($1k) -> 10k sh ($1M) = dust scale
        prev_fl = self._mock_filing("0001000001", "2024-03-31", "2024-05-15", [
            ("C_NVDA", 100_000, 90_000_000.0),
            ("C_REV", 500_000, 10_000_000.0),
            ("C_STUB", 10, 1_000.0),
        ])
        curr_fl = self._mock_filing("0001000001", "2024-08-31", "2024-10-15", [
            ("C_NVDA", 1_000_000, 110_000_000.0),
            ("C_REV", 100_000, 10_000_000.0),
            ("C_STUB", 10_000, 1_000_000.0),
        ])
        u = build_data.build_fund_update_entry(cand, [prev_fl, curr_fl], cusip_meta, True, ["TTM"], splits_map=splits_map)
        assert u is not None
        by_t = {m["ticker"]: m for m in u["allMoves"]}
        self.assertEqual(by_t["NVDA"]["action"], "UNCHANGED")
        self.assertAlmostEqual(by_t["NVDA"]["estFlowUsd"], 0.0, places=2)
        self.assertIn("10:1 split", by_t["NVDA"]["tagNote"] or "")
        self.assertEqual(by_t["REV"]["action"], "UNCHANGED")
        self.assertEqual(by_t["STUB"]["action"], "ADDED")
        self.assertEqual(by_t["STUB"]["tagNote"], "Scaled from starter stub")
        self.assertAlmostEqual(by_t["STUB"]["estFlowUsd"], 999_000.0, places=2)
        notes_joined = " ".join(u["narrativeFocusNotes"])
        self.assertIn("scaled from starter stub", notes_joined)
        self.assertNotIn("99,900%", notes_joined)

    def test_xbrl_v2_tag_migration_ytd_decomp_missing_capex_and_fpi_eur(self) -> None:
        # 1) Tag migration (stale SalesRevenueNet ends 2022, active RevenueFromContract... has 2025/2026) + YTD OCF + missing CapEx
        migrated_facts = {
            "facts": {
                "us-gaap": {
                    "RevenueFromContractWithCustomerExcludingAssessedTax": {"units": {"USD": [
                        {"start": "2025-10-01", "end": "2025-12-31", "filed": "2026-02-01", "val": 120.0},
                        {"start": "2026-01-01", "end": "2026-03-31", "filed": "2026-05-01", "val": 130.0},
                        {"start": "2026-04-01", "end": "2026-06-30", "filed": "2026-08-01", "val": 140.0},
                        {"start": "2026-07-01", "end": "2026-09-30", "filed": "2026-10-28", "val": 150.0},
                        {"start": "2024-10-01", "end": "2024-12-31", "filed": "2025-02-01", "val": 100.0},
                        {"start": "2025-01-01", "end": "2025-03-31", "filed": "2025-05-01", "val": 105.0},
                        {"start": "2025-04-01", "end": "2025-06-30", "filed": "2025-08-01", "val": 110.0},
                        {"start": "2025-07-01", "end": "2025-09-30", "filed": "2025-10-28", "val": 115.0},
                    ]}},
                    "SalesRevenueNet": {"units": {"USD": [
                        {"start": "2021-01-01", "end": "2021-12-31", "filed": "2022-02-01", "val": 50.0},
                        {"start": "2022-01-01", "end": "2022-12-31", "filed": "2023-02-01", "val": 60.0},
                    ]}},
                    # YTD cumulative OCF statements (Q1 90d, 6M 181d, 9M 273d, FY 365d) -> decomposed cleanly to standalone quarters
                    "NetCashProvidedByUsedInOperatingActivities": {"units": {"USD": [
                        {"start": "2025-01-01", "end": "2025-09-30", "filed": "2025-10-28", "val": 75.0},
                        {"start": "2025-01-01", "end": "2025-12-31", "filed": "2026-02-01", "val": 105.0},  # Q4_25 = 30
                        {"start": "2026-01-01", "end": "2026-03-31", "filed": "2026-05-01", "val": 32.0},   # Q1_26 = 32
                        {"start": "2026-01-01", "end": "2026-06-30", "filed": "2026-08-01", "val": 66.0},   # Q2_26 = 34
                        {"start": "2026-01-01", "end": "2026-09-30", "filed": "2026-10-28", "val": 102.0},  # Q3_26 = 36 -> TTM OCF = 132
                    ]}},
                    "OperatingIncomeLoss": {"units": {"USD": [
                        {"start": "2025-10-01", "end": "2025-12-31", "filed": "2026-02-01", "val": 24.0},
                        {"start": "2026-01-01", "end": "2026-03-31", "filed": "2026-05-01", "val": 26.0},
                        {"start": "2026-04-01", "end": "2026-06-30", "filed": "2026-08-01", "val": 28.0},
                        {"start": "2026-07-01", "end": "2026-09-30", "filed": "2026-10-28", "val": 30.0},
                    ]}},
                }
            }
        }
        xb = enrichment._parse_company_xbrl_facts(migrated_facts)
        # Current TTM = 540 vs Prior TTM = 430 -> +25.6% YoY; Operating margin = 108/540 = 20.0%
        self.assertAlmostEqual(xb["revenueTtmUsd"], 540.0)
        self.assertAlmostEqual(xb["revenueGrowthYoYPct"], 25.6, places=1)
        self.assertAlmostEqual(xb["operatingMarginPct"], 20.0, places=1)
        # Missing CapEx must NEVER be fabricated as 0.0 -> fcfMarginPct remains None
        self.assertIsNone(xb["fcfMarginPct"])

        # 2) Foreign private issuer reporting IFRS in EUR -> revenueTtmUsd=None, matched-currency margins computed
        fpi_facts = {
            "facts": {
                "ifrs-full": {
                    "Revenue": {"units": {"EUR": [
                        {"start": "2025-01-01", "end": "2025-12-31", "filed": "2026-03-01", "val": 200.0},
                        {"start": "2024-01-01", "end": "2024-12-31", "filed": "2025-03-01", "val": 160.0},
                    ]}},
                    "ProfitLossFromOperatingActivities": {"units": {"EUR": [
                        {"start": "2025-01-01", "end": "2025-12-31", "filed": "2026-03-01", "val": 60.0},
                    ]}},
                    "CashFlowsFromUsedInOperatingActivities": {"units": {"EUR": [
                        {"start": "2025-01-01", "end": "2025-12-31", "filed": "2026-03-01", "val": 80.0},
                    ]}},
                    "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities": {"units": {"EUR": [
                        {"start": "2025-01-01", "end": "2025-12-31", "filed": "2026-03-01", "val": 20.0},
                    ]}},
                }
            }
        }
        xb_fpi = enrichment._parse_company_xbrl_facts(fpi_facts)
        self.assertIsNone(xb_fpi["revenueTtmUsd"])
        self.assertAlmostEqual(xb_fpi["revenueGrowthYoYPct"], 25.0)
        self.assertAlmostEqual(xb_fpi["operatingMarginPct"], 30.0)
        self.assertAlmostEqual(xb_fpi["fcfMarginPct"], 30.0)

    def test_schedule_13dg_xml_parsing_and_cusip_fallback(self) -> None:
        sample_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
        <edgarSubmission>
          <formData>
            <coverPageHeader>
              <issuerInfo>
                <issuerCusipNumber>67066G104</issuerCusipNumber>
              </issuerInfo>
              <eventDateRequiresFilingThisStatement>11/10/2026</eventDateRequiresFilingThisStatement>
            </coverPageHeader>
            <coverPageHeaderReportingPersonDetails>
              <reportingPersonBeneficiallyOwnedAggregateNumberOfShares>0</reportingPersonBeneficiallyOwnedAggregateNumberOfShares>
              <classPercent>0.0</classPercent>
            </coverPageHeaderReportingPersonDetails>
          </formData>
        </edgarSubmission>
        """
        parsed = enrichment._parse_13dg_primary_xml(sample_xml)
        self.assertEqual(parsed["percentOfClass"], 0.0)
        self.assertEqual(parsed["sharesOwned"], 0)
        self.assertEqual(parsed["subjectCusip"], "67066G104")
        self.assertEqual(parsed["eventDate"], "2026-11-10")

        sample_hdr = b"<SUBMISSION><SUBJECT-COMPANY><CONFORMED-NAME>Unknown Name LLC<CIK>0009998888</SUBJECT-COMPANY>"
        fake_sub = {
            "filings": {
                "recent": {
                    "form": ["SCHEDULE 13G/A"],
                    "accessionNumber": ["0001000001-26-000777"],
                    "filingDate": ["2026-11-15"],
                    "primaryDocument": ["xslSCHEDULE_13G_X01/primary_doc.xml"],
                }
            }
        }
        with tempfile.TemporaryDirectory() as td:
            hdr_cache_p = Path(td) / "hdr_cache.json"
            def _fake_fetch_bytes(url: str, **_kwargs) -> bytes:
                return sample_xml if url.endswith("primary_doc.xml") else sample_hdr

            with (
                mock.patch.object(enrichment, "MIDQUARTER_HDR_CACHE_PATH", hdr_cache_p),
                mock.patch.object(enrichment, "get_submissions", return_value=fake_sub),
                mock.patch.object(enrichment, "fetch_bytes", side_effect=_fake_fetch_bytes),
            ):
                alerts = enrichment.fetch_midquarter_alerts(
                    elite_ciks=["0001000001"],
                    fund_names_by_cik={"0001000001": "Atreides Management"},
                    cik_to_ticker_map={},
                    name_to_ticker_map={},
                    today_date=date(2026, 11, 17),
                    since_days=30,
                    cusip_to_ticker_map={"67066G104": "NVDA"},
                )
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["ticker"], "NVDA")
        self.assertEqual(alerts[0]["stakeStatus"], "Exited (0%)")
        self.assertEqual(alerts[0]["percentOfClass"], 0.0)
        self.assertEqual(alerts[0]["eventDate"], "2026-11-10")

    def test_updates_overview_signed_est_flow_filtering(self) -> None:
        cusip_meta = {
            "C_TRIM": {"ticker": "RKLB", "name": "Rocket Lab", "sector": "Industrials", "industry": "Aerospace & Defense", "isTechTmt": False},
            "C_ADD": {"ticker": "SNOW", "name": "Snowflake", "sector": "Technology", "industry": "Software—Application", "isTechTmt": True},
        }
        # Fund 1 & 2 both trim RKLB (-20% shares) while RKLB rallies +100% -> valueDeltaUsd > 0, estFlowUsd < 0!
        # And both add SNOW (+33% shares) while SNOW dips -40% -> valueDeltaUsd < 0, estFlowUsd > 0!
        updates = []
        for idx in range(1, 3):
            cik = f"000100000{idx}"
            cand = {"cik": cik, "name": f"Fund {idx}", "manager": f"M{idx}", "styleTag": "TMT"}
            prev_fl = self._mock_filing(cik, "2026-06-30", "2026-08-14", [
                ("C_TRIM", 1_000, 10_000_000.0),
                ("C_ADD", 1_000, 20_000_000.0),
            ])
            curr_fl = self._mock_filing(cik, "2026-09-30", "2026-11-16", [
                ("C_TRIM", 800, 16_000_000.0),
                ("C_ADD", 1_333, 16_000_000.0),
            ])
            u = build_data.build_fund_update_entry(cand, [prev_fl, curr_fl], cusip_meta, True, ["TTM"])
            assert u is not None
            updates.append(u)

        ov = build_data.build_updates_overview(updates, total_eligible=2)
        exits_t = {x["ticker"]: x for x in ov["consensusExitsAndTrims"]}
        adds_t = {x["ticker"]: x for x in ov["consensusHeavyAdds"]}
        self.assertIn("RKLB", exits_t)
        self.assertLess(exits_t["RKLB"]["totalEstFlowUsd"], 0.0)
        self.assertIn("SNOW", adds_t)
        self.assertGreater(adds_t["SNOW"]["totalEstFlowUsd"], 0.0)

    def test_weekend_statutory_deadline_and_pit_skill_backtest(self) -> None:
        # 2026-09-30 + 45d = 2026-11-14 (Saturday) -> rolls to Monday 2026-11-16
        self.assertEqual(build_data.statutory_deadline_date("2026-09-30"), "2026-11-16")
        # 2025-09-30 + 45d = 2025-11-14 (Friday) -> stays 2025-11-14
        self.assertEqual(build_data.statutory_deadline_date("2025-09-30"), "2025-11-14")

        cal_dates: list[str] = []
        d_cur = date(2024, 1, 2)
        while len(cal_dates) < 280:
            if d_cur.weekday() < 5:
                cal_dates.append(d_cur.strftime("%Y-%m-%d"))
            d_cur += timedelta(days=1)
        n_dates = len(cal_dates)
        tickers = ["NVDA", "MSFT", "AAPL", "CRWD", "META"]
        aligned_prices = {
            tk: (np.array([100.0 + 0.15 * i + idx for i in range(n_dates)], dtype=np.float64), 0, n_dates - 1)
            for idx, tk in enumerate(tickers)
        }
        qqq_nav = np.array([100.0 + 0.08 * i for i in range(n_dates)], dtype=np.float64)
        rf_ann_arr = np.full(n_dates, 4.5, dtype=np.float64)
        splits_map = {"NVDA": [{"date": "2024-06-10", "factor": 10.0, "ratio": "10:1"}]}

        ciks = [f"000200000{i}" for i in range(1, 7)]
        snaps_by_cik: dict[str, list[dict]] = {}
        navs_by_cik: dict[str, np.ndarray] = {}
        for idx_c, cik in enumerate(ciks):
            snaps_by_cik[cik] = [
                {
                    "effDate": "2024-05-15",
                    "reportDate": "2024-03-31",
                    "weights": {tk: 0.2 for tk in tickers},
                    "shares": {tk: 100.0 for tk in tickers},
                    "eqAum": 500_000_000.0,
                    "techPct": 100.0,
                    "effN": 5.0,
                    "maxSingle": 20.0,
                    "posCount": 5,
                },
                {
                    "effDate": "2024-08-14",
                    "reportDate": "2024-06-30",
                    "weights": {tk: 0.2 for tk in tickers},
                    "shares": {"NVDA": 1000.0, "MSFT": 100.0, "AAPL": 100.0, "CRWD": 100.0, "META": 100.0},
                    "eqAum": 550_000_000.0,
                    "techPct": 100.0,
                    "effN": 5.0,
                    "maxSingle": 20.0,
                    "posCount": 5,
                },
            ]
            navs_by_cik[cik] = np.array([100.0 * (1.0 + 0.0012 * j * (idx_c + 1)) for j in range(n_dates)], dtype=np.float64)

        segs = build_data.run_ideal_portfolio_backtest(
            bt_mode="filing_lag",
            all_quarters=["2024-06-30", "2024-09-30"],
            all_bt_ciks=ciks,
            snaps_by_cik=snaps_by_cik,
            navs_by_cik=navs_by_cik,
            cal_dates=cal_dates,
            as_of_date=cal_dates[-1],
            aligned_prices=aligned_prices,
            ticker_is_tech={tk: True for tk in tickers},
            curated_bt_ciks=set(),
            splits_map=splits_map,
            rf_ann_arr=rf_ann_arr,
            qqq_nav=qqq_nav,
        )
        self.assertGreaterEqual(len(segs), 1)
        self.assertAlmostEqual(sum(segs[0][1].values()), 1.0, places=3)


class TestAlphaDecayAndLagEngine(unittest.TestCase):
    def _mock_filing(self, cik: str, rd: str, fd: str, holdings: list[tuple[str, float, float]]) -> dict:
        return {
            "cik": cik,
            "entityName": f"FUND {cik}",
            "form": "13F-HR",
            "amendmentType": "",
            "reportDate": rd,
            "filingDate": fd,
            "originalFilingDate": fd,
            "latestAmendmentDate": fd,
            "accessionNumber": f"{cik}-26-{rd[-5:].replace('-', '')}",
            "rows": [
                {
                    "cusip": c,
                    "issuer": c,
                    "title": "COM",
                    "secType": "EQUITY",
                    "shares": sh,
                    "valueUsd": val,
                }
                for c, sh, val in holdings
            ],
        }

    def test_elite_holding_periods_exit_reset_and_filing_anchors(self) -> None:
        cusip_meta = {
            "C_NVDA": {"ticker": "NVDA", "name": "NVIDIA", "sector": "Technology", "industry": "Semiconductors", "isTechTmt": True},
            "C_SNOW": {"ticker": "SNOW", "name": "Snowflake", "sector": "Technology", "industry": "Software—Application", "isTechTmt": True},
        }
        # Elite Fund 1 held NVDA across 3 consecutive quarters; exited SNOW in Q2 then re-entered Q3 (streak = 1)
        f1 = [
            self._mock_filing("0003000001", "2026-03-31", "2026-05-14", [("C_NVDA", 100, 10_000_000), ("C_SNOW", 50, 5_000_000)]),
            self._mock_filing("0003000001", "2026-06-30", "2026-08-14", [("C_NVDA", 120, 12_000_000)]),
            self._mock_filing("0003000001", "2026-09-30", "2026-11-12", [("C_NVDA", 140, 14_000_000), ("C_SNOW", 80, 8_000_000)]),
        ]
        # Elite Fund 2 held NVDA only in latest Q3 (streak = 1)
        f2 = [
            self._mock_filing("0003000002", "2026-06-30", "2026-08-14", [("C_SNOW", 40, 4_000_000)]),
            self._mock_filing("0003000002", "2026-09-30", "2026-11-16", [("C_NVDA", 50, 5_000_000)]),
        ]
        # Non-Elite Qualified Fund 3 held SNOW for 3Q (must NOT affect Elite average holding period!)
        f3 = [
            self._mock_filing("0003000003", "2026-03-31", "2026-05-15", [("C_SNOW", 10, 1_000_000)]),
            self._mock_filing("0003000003", "2026-06-30", "2026-08-14", [("C_SNOW", 10, 1_000_000)]),
            self._mock_filing("0003000003", "2026-09-30", "2026-11-10", [("C_SNOW", 10, 1_000_000)]),
        ]
        raw_filings = {"0003000001": f1, "0003000002": f2, "0003000003": f3}
        elite_ciks = {"0003000001", "0003000002"}
        lb_ciks = {"0003000001", "0003000002", "0003000003"}

        periods = build_data.compute_elite_holding_periods(elite_ciks, raw_filings, cusip_meta)
        # NVDA: (3 + 1) / 2 = 2.0Q ; SNOW: Fund 1 streak reset to 1Q after Q2 exit!
        self.assertAlmostEqual(periods["NVDA"], 2.0)
        self.assertAlmostEqual(periods["SNOW"], 1.0)

        anchors = build_data.compute_stock_filing_date_anchors(
            raw_filings,
            cusip_meta,
            build_data.NON_STOCK_ETFS,
            latest_pe_mode="2026-09-30",
            elite_ciks=elite_ciks,
            lb_ciks=lb_ciks,
        )
        # NVDA Elite dates = [2026-11-12, 2026-11-16] -> median upper = 2026-11-16; SNOW Elite date = 2026-11-12
        self.assertIn(anchors["NVDA"], ("2026-11-12", "2026-11-16"))
        self.assertEqual(anchors["SNOW"], "2026-11-12")

    def test_three_timestamp_return_bridge_and_fomo_vs_lag_flags(self) -> None:
        cal_dates = ["2026-11-12", "2026-11-13", "2026-11-16", "2026-11-17"]
        aligned_prices = {
            "FOMO": (np.array([110.0, 111.0, 112.0, 126.0], dtype=np.float64), 0, 3),
            "DIPP": (np.array([92.0, 91.0, 90.0, 81.0], dtype=np.float64), 0, 3),
            "RUNN": (np.array([115.0, 116.0, 118.0, 130.0], dtype=np.float64), 0, 3),
        }

        # Case 1: FOMO Trap -> Est. Q-VWAP = $100, Filing (Nov 16) = $112 (+12.0%), Today = $126 (+12.5%, +26.0% vs VWAP)
        # while Elite funds trimmed into strength (2 Elite sellers vs 1 Elite buyer)
        fomo = build_data.compute_alpha_decay_metrics(
            ticker="FOMO",
            aligned_prices=aligned_prices,
            cal_dates=cal_dates,
            price_usd=126.0,
            est_quarter_vwap=100.0,
            pct_vs_quarter_vwap=26.0,
            latest_pe_mode="2026-09-30",
            consensus_filing_date="2026-11-16",
            avg_elite_holding_quarters=2.5,
            new_count=2,
            added_count=1,
            trimmed_count=3,
            exited_count=1,
            elite_buyers_count=1,
            elite_sellers_count=2,
            elite_holders_count=5,
            net_est_flow_usd=-45_000_000.0,
        )
        self.assertEqual(fomo["alphaDecayStatus"], "FOMO_TRAP")
        self.assertEqual(fomo["filingDatePriceUsd"], 112.0)
        self.assertAlmostEqual(fomo["qVwapToFilingPct"], 12.0)
        self.assertAlmostEqual(fomo["filingToTodayPct"], 12.5)
        self.assertAlmostEqual(fomo["avgEliteHoldingQuarters"], 2.5)

        # Case 2: Lag Advantage -> Est. Q-VWAP = $100, Filing = $90 (-10.0%), Today = $81 (-10.0% post-filing, -19.0% vs VWAP)
        # after 3 NEW + 2 ADDED cluster buys
        dipp = build_data.compute_alpha_decay_metrics(
            ticker="DIPP",
            aligned_prices=aligned_prices,
            cal_dates=cal_dates,
            price_usd=81.0,
            est_quarter_vwap=100.0,
            pct_vs_quarter_vwap=-19.0,
            latest_pe_mode="2026-09-30",
            consensus_filing_date="2026-11-16",
            avg_elite_holding_quarters=4.0,
            new_count=3,
            added_count=2,
            trimmed_count=1,
            exited_count=0,
            elite_buyers_count=3,
            elite_sellers_count=0,
            elite_holders_count=4,
            net_est_flow_usd=120_000_000.0,
        )
        self.assertEqual(dipp["alphaDecayStatus"], "LAG_ADVANTAGE")
        self.assertAlmostEqual(dipp["qVwapToFilingPct"], -10.0)
        self.assertAlmostEqual(dipp["filingToTodayPct"], -10.0)

        # Case 3: Extended (>20% above Q-VWAP) with pure Elite accumulation (no trimming trap)
        runn = build_data.compute_alpha_decay_metrics(
            ticker="RUNN",
            aligned_prices=aligned_prices,
            cal_dates=cal_dates,
            price_usd=130.0,
            est_quarter_vwap=100.0,
            pct_vs_quarter_vwap=30.0,
            latest_pe_mode="2026-09-30",
            consensus_filing_date="2026-11-16",
            avg_elite_holding_quarters=3.2,
            new_count=4,
            added_count=3,
            trimmed_count=0,
            exited_count=0,
            elite_buyers_count=4,
            elite_sellers_count=0,
            elite_holders_count=6,
            net_est_flow_usd=210_000_000.0,
        )
        self.assertEqual(runn["alphaDecayStatus"], "POST_FILING_EXTENDED")
        self.assertAlmostEqual(runn["qVwapToFilingPct"], 18.0)


class TestConvictionMatrixAndPersistence(unittest.TestCase):
    def _mock_filing(self, cik: str, rd: str, fd: str, holdings: list[tuple[str, float, float]]) -> dict:
        return {
            "cik": cik,
            "entityName": f"FUND {cik}",
            "form": "13F-HR",
            "amendmentType": "",
            "reportDate": rd,
            "filingDate": fd,
            "originalFilingDate": fd,
            "latestAmendmentDate": fd,
            "accessionNumber": f"{cik}-{rd}",
            "rows": [
                {
                    "cusip": c,
                    "issuer": c,
                    "title": "COM",
                    "secType": "EQUITY",
                    "shares": sh,
                    "valueUsd": val,
                }
                for c, sh, val in holdings
            ],
        }

    def test_split_adjusted_thesis_persistence_and_streaks(self) -> None:
        cusip_meta = {
            "C_NVDA": {"ticker": "NVDA", "name": "NVIDIA", "sector": "Technology", "industry": "Semiconductors", "isTechTmt": True},
            "C_META": {"ticker": "META", "name": "Meta", "sector": "Communication Services", "industry": "Internet Content", "isTechTmt": True},
            "C_INTC": {"ticker": "INTC", "name": "Intel", "sector": "Technology", "industry": "Semiconductors", "isTechTmt": True},
        }
        splits_map = {
            "NVDA": [{"date": "2026-04-10", "factor": 10.0, "ratio": "10:1"}],
        }
        # Fund 1: NVDA held flat through 10:1 split (100 -> 1,000 -> 1,020); META added 3 consecutive transitions; INTC trimmed & exited
        f1 = [
            self._mock_filing("0004000001", "2025-12-31", "2026-02-14", [("C_NVDA", 100, 10_000_000), ("C_META", 100, 10_000_000), ("C_INTC", 200, 5_000_000)]),
            self._mock_filing("0004000001", "2026-03-31", "2026-05-15", [("C_NVDA", 100, 10_000_000), ("C_META", 125, 12_500_000), ("C_INTC", 150, 3_500_000)]),
            self._mock_filing("0004000001", "2026-06-30", "2026-08-14", [("C_NVDA", 1000, 10_000_000), ("C_META", 160, 16_000_000), ("C_INTC", 80, 1_800_000)]),
            self._mock_filing("0004000001", "2026-09-30", "2026-11-16", [("C_NVDA", 1020, 10_200_000), ("C_META", 200, 20_000_000)]),
        ]
        # Fund 2: also trims INTC in Q2 & Q3; joins META in Q3
        f2 = [
            self._mock_filing("0004000002", "2026-03-31", "2026-05-15", [("C_INTC", 300, 7_000_000)]),
            self._mock_filing("0004000002", "2026-06-30", "2026-08-14", [("C_INTC", 200, 4_500_000)]),
            self._mock_filing("0004000002", "2026-09-30", "2026-11-16", [("C_META", 90, 9_000_000), ("C_INTC", 100, 2_000_000)]),
        ]

        res = build_data.compute_stock_thesis_persistence(
            raw_fund_filings={"0004000001": f1, "0004000002": f2},
            cusip_meta=cusip_meta,
            splits_map=splits_map,
            eligible_ciks={"0004000001", "0004000002"},
            elite_ciks={"0004000001"},
            latest_pe_mode="2026-09-30",
        )

        # NVDA had zero >=5% moves after split-adjustment -> never falsely flagged as buying/selling streak
        self.assertNotIn("NVDA", res)
        self.assertEqual(res["META"]["consecutiveNetBuyQuarters"], 3)
        self.assertEqual(res["META"]["consecutiveNetSellQuarters"], 0)
        self.assertEqual(res["META"]["direction"], "ACCUMULATION")

        self.assertEqual(res["INTC"]["consecutiveNetSellQuarters"], 3)
        self.assertEqual(res["INTC"]["consecutiveNetBuyQuarters"], 0)
        self.assertEqual(res["INTC"]["direction"], "DISTRIBUTION")

    def test_actionability_matrix_all_verdicts_dilution_and_guardrails(self) -> None:
        skill_map = {"0001": 92.0, "0002": 84.0, "0003": 79.0, "0004": 48.0}
        strong_movers = [
            {"cik": "0001", "fundTier": "Elite 50", "action": "NEW"},
            {"cik": "0002", "fundTier": "Elite 50", "action": "NEW"},
            {"cik": "0003", "fundTier": "Elite 50", "action": "ADDED"},
        ]
        good_xbrl = {
            "revenueGrowthYoYPct": 28.0,
            "fcfMarginPct": 24.0,
            "operatingMarginPct": 27.0,
            "shareDilutionYoYPct": -1.2,
        }
        good_pers = {"consecutiveNetBuyQuarters": 3, "consecutiveNetSellQuarters": 0, "netBuyQuartersLast4": 3, "netSellQuartersLast4": 0, "quartersObserved": 4}

        # 1. Aligned STRONG ACCUMULATE
        strong = build_data.compute_actionability_matrix(
            new_count=2,
            added_count=2,
            trimmed_count=0,
            exited_count=0,
            elite_buyers_count=3,
            elite_holders_count=5,
            total_value_usd=450_000_000.0,
            net_est_flow_usd=210_000_000.0,
            movers=strong_movers,
            fund_skill_by_cik=skill_map,
            pct_vs_quarter_vwap=-6.0,
            alpha_decay_status="LAG_ADVANTAGE",
            fundamentals=good_xbrl,
            avg_elite_holding_quarters=5.2,
            thesis_persistence=good_pers,
        )
        self.assertEqual(strong["verdict"], "STRONG ACCUMULATE")
        self.assertGreaterEqual(strong["score"], 80.0)
        self.assertGreater(strong["pillars"]["flowImpulse"], 18.0)
        self.assertGreater(strong["pillars"]["priceDiscipline"], 20.0)
        self.assertGreater(strong["pillars"]["xbrlQuality"], 20.0)
        self.assertGreater(strong["pillars"]["thesisPersistence"], 20.0)

        # 2. Same setup at +28% above Q-VWAP -> Price Discipline guardrail caps STRONG ACCUMULATE to ACCUMULATE ON DIP
        extended = build_data.compute_actionability_matrix(
            new_count=2,
            added_count=2,
            trimmed_count=0,
            exited_count=0,
            elite_buyers_count=3,
            elite_holders_count=5,
            total_value_usd=450_000_000.0,
            net_est_flow_usd=210_000_000.0,
            movers=strong_movers,
            fund_skill_by_cik=skill_map,
            pct_vs_quarter_vwap=28.0,
            alpha_decay_status="POST_FILING_EXTENDED",
            fundamentals=good_xbrl,
            avg_elite_holding_quarters=5.2,
            thesis_persistence=good_pers,
        )
        self.assertEqual(extended["verdict"], "ACCUMULATE ON DIP")
        self.assertLess(extended["pillars"]["priceDiscipline"], 12.0)

        # 3. Heavy SBC Share Dilution (+14% YoY) + negative FCF margin fails XBRL Quality Gate -> HOLD
        dilutive = build_data.compute_actionability_matrix(
            new_count=2,
            added_count=1,
            trimmed_count=0,
            exited_count=0,
            elite_buyers_count=2,
            elite_holders_count=3,
            total_value_usd=200_000_000.0,
            net_est_flow_usd=80_000_000.0,
            movers=strong_movers,
            fund_skill_by_cik=skill_map,
            pct_vs_quarter_vwap=-1.0,
            fundamentals={
                "revenueGrowthYoYPct": 4.0,
                "fcfMarginPct": -12.0,
                "operatingMarginPct": -8.0,
                "shareDilutionYoYPct": 14.0,
            },
            avg_elite_holding_quarters=2.5,
            thesis_persistence={"consecutiveNetBuyQuarters": 2, "consecutiveNetSellQuarters": 0, "netBuyQuartersLast4": 2, "netSellQuartersLast4": 0, "quartersObserved": 2},
        )
        self.assertLess(dilutive["pillars"]["xbrlQuality"], 7.5)
        self.assertEqual(dilutive["verdict"], "HOLD")

        # Missing fundamentals (e.g., foreign issuer) falls back strictly to neutral 12.5 / 25
        missing_xbrl = build_data.compute_actionability_matrix(
            new_count=1,
            added_count=1,
            trimmed_count=1,
            exited_count=0,
            fundamentals=None,
            pct_vs_quarter_vwap=0.0,
        )
        self.assertEqual(missing_xbrl["pillars"]["xbrlQuality"], 12.5)

        # 4. FOMO_TRAP -> forced DISTRIBUTION WARNING regardless of legacy profit margin
        fomo_trap = build_data.compute_actionability_matrix(
            new_count=0,
            added_count=1,
            trimmed_count=3,
            exited_count=1,
            elite_buyers_count=1,
            elite_sellers_count=3,
            total_value_usd=300_000_000.0,
            net_est_flow_usd=-45_000_000.0,
            pct_vs_quarter_vwap=27.0,
            alpha_decay_status="FOMO_TRAP",
            fundamentals=good_xbrl,
        )
        self.assertEqual(fomo_trap["verdict"], "DISTRIBUTION WARNING")
        self.assertLessEqual(fomo_trap["pillars"]["priceDiscipline"], 5.0)

        # 5. Severe exits + negative persistence -> EXIT SIGNAL
        exit_sig = build_data.compute_actionability_matrix(
            new_count=0,
            added_count=1,
            trimmed_count=3,
            exited_count=3,
            elite_buyers_count=0,
            elite_sellers_count=4,
            total_value_usd=180_000_000.0,
            net_est_flow_usd=-140_000_000.0,
            pct_vs_quarter_vwap=15.0,
            fundamentals={"revenueGrowthYoYPct": -8.0, "fcfMarginPct": -2.0, "shareDilutionYoYPct": 4.5},
            thesis_persistence={"consecutiveNetBuyQuarters": 0, "consecutiveNetSellQuarters": 3, "netBuyQuartersLast4": 0, "netSellQuartersLast4": 3, "quartersObserved": 3},
        )
        self.assertEqual(exit_sig["verdict"], "EXIT SIGNAL")
        self.assertLess(exit_sig["score"], 35.0)

        # 6. Score < 50 WITHOUT distribution evidence (net buying + expensive + weak XBRL) must stay HOLD, not DISTRIBUTION WARNING
        no_sell_drag = build_data.compute_actionability_matrix(
            new_count=1,
            added_count=1,
            trimmed_count=0,
            exited_count=0,
            elite_buyers_count=1,
            elite_sellers_count=0,
            total_value_usd=200_000_000.0,
            net_est_flow_usd=20_000_000.0,
            pct_vs_quarter_vwap=42.0,
            fundamentals={"revenueGrowthYoYPct": -12.0, "fcfMarginPct": -15.0, "shareDilutionYoYPct": 10.0},
            thesis_persistence={"consecutiveNetBuyQuarters": 1, "consecutiveNetSellQuarters": 0, "netBuyQuartersLast4": 1, "netSellQuartersLast4": 1, "quartersObserved": 2},
        )
        self.assertLess(no_sell_drag["score"], 50.0)
        self.assertEqual(no_sell_drag["verdict"], "HOLD")
        self.assertNotIn("outflows", no_sell_drag["summary"].lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)



