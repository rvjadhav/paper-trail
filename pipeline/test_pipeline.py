"""
Deterministic offline unit tests covering all critical safeguards in the Paper Trail 13F pipeline.
Run with: /usr/bin/python3 -u pipeline/test_pipeline.py
"""

from __future__ import annotations

import json
import math
import tempfile
import time
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
import validate_dataset


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
            "2026-06-30",
            "2026-08-14", "2026-08-17",
            "2026-09-15", "2026-09-16",
            "2026-09-30",
            "2026-10-15", "2026-10-16",
            "2026-11-01", "2026-11-02", "2026-11-10",
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
        # Timeline segments execute at T+1 (next trading day strictly after SEC filingDate):
        # 2026-08-14 filing -> idx 2 (2026-08-17) 100% NVDA (zero look-ahead on MSFT confidential position!)
        # 2026-09-15 filing -> idx 4 (2026-09-16) 50% NVDA / 50% MSFT (after confidential amendment filed)
        # 2026-10-15 filing -> idx 7 (2026-10-16) 100% META
        # 2026-11-01 stale Q1 restatement is ignored -> META stays at 100%, AAPL NOT injected!
        self.assertEqual(len(lag_segs), 3)
        self.assertEqual(lag_segs[0], (2, {"NVDA": 1.0}))
        self.assertEqual(lag_segs[1][0], 4)
        self.assertAlmostEqual(lag_segs[1][1]["NVDA"], 0.5, places=4)
        self.assertAlmostEqual(lag_segs[1][1]["MSFT"], 0.5, places=4)
        self.assertEqual(lag_segs[2], (7, {"META": 1.0}))


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
        # Q3-2021 filing_lag (2021-11-15) executes at T+1 (strictly after filing deadline) and must NOT be skipped
        expected_q3_idx = build_data.find_idx_strictly_after(cal_dates, "2021-11-15")
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


class TestForm4And13DConvergence(unittest.TestCase):
    def test_parse_13dg_primary_xml_extracts_stakes_and_item4_intent(self) -> None:
        xml_bytes = b"""<?xml version="1.0" encoding="UTF-8"?>
        <edgarSubmission xmlns="http://www.sec.gov/edgar/schedule13D">
          <formData>
            <submissionType>SCHEDULE 13D</submissionType>
            <issuerInfo>
              <issuerCusipNumber>67066G104</issuerCusipNumber>
            </issuerInfo>
            <eventDateRequiresFilingThisStatement>2026-11-04</eventDateRequiresFilingThisStatement>
            <reportingPersonsInfo>
              <reportingPersonInfo>
                <reportingPersonBeneficiallyOwnedAggregateNumberOfShares>2,450,000</reportingPersonBeneficiallyOwnedAggregateNumberOfShares>
                <classPercent>7.4</classPercent>
              </reportingPersonInfo>
            </reportingPersonsInfo>
            <item4>
              <transactionPurpose>
                Item 4. Purpose of Transaction - Reporting person intends to engage management regarding strategic alternatives, potential sale process, and board representation to unlock shareholder value.
              </transactionPurpose>
            </item4>
          </formData>
        </edgarSubmission>
        """
        parsed = enrichment._parse_13dg_primary_xml(xml_bytes, is_activist=True)
        self.assertEqual(parsed["percentOfClass"], 7.4)
        self.assertEqual(parsed["sharesOwned"], 2450000)
        self.assertEqual(parsed["subjectCusip"], "67066G104")
        self.assertEqual(parsed["eventDate"], "2026-11-04")
        self.assertEqual(parsed["item4Category"], "Strategic Review / M&A")
        self.assertIn("strategic alternatives", parsed["item4Intent"].lower())
        self.assertFalse(parsed["item4Intent"].upper().startswith("ITEM 4"))

    def test_parse_13dg_html_skips_toc_and_extracts_cover_and_item4(self) -> None:
        html_bytes = """
        <html><body>
          <div>CUSIP No. 037833100</div>
          <div>Date of Event Which Requires Filing of this Statement: November 2, 2026</div>
          <table>
            <tr><td>(9) AGGREGATE AMOUNT BENEFICIALLY OWNED BY EACH REPORTING PERSON</td><td>1,825,000</td></tr>
            <tr><td>(11) PERCENT OF CLASS REPRESENTED BY AMOUNT IN ROW (9)</td><td>6.8%</td></tr>
          </table>
          <p>Table of Contents: Item 4. Purpose of Transaction ... 6</p>
          <p>Item 4. Purpose of Transaction.</p>
          <p>The Reporting Persons plan to seek board seats and advocate for improved corporate governance and nomination of two independent directors ahead of the annual meeting.</p>
          <p>Item 5. Interest in Securities of the Issuer.</p>
        </body></html>
        """.encode("utf-8")
        parsed = enrichment._parse_13dg_html(html_bytes, is_activist=True)
        self.assertEqual(parsed["subjectCusip"], "037833100")
        self.assertEqual(parsed["eventDate"], "2026-11-02")
        self.assertEqual(parsed["sharesOwned"], 1825000)
        self.assertEqual(parsed["percentOfClass"], 6.8)
        self.assertEqual(parsed["item4Category"], "Board / Governance")
        self.assertIn("board seats", parsed["item4Intent"].lower())
        self.assertNotIn("Table of Contents", parsed["item4Intent"])

    def test_form4_xml_parsing_and_cluster_dedup_rules(self) -> None:
        self.assertTrue(enrichment._is_csuite_title("Chief Executive Officer"))
        self.assertTrue(enrichment._is_csuite_title("Senior Vice President & CFO"))
        self.assertFalse(enrichment._is_csuite_title("Vice President, Sales"))
        self.assertTrue(enrichment._footnote_confirms_10b5("Transaction executed pursuant to a Rule 10b5-1 trading plan."))
        self.assertFalse(enrichment._footnote_confirms_10b5("Purchase not made pursuant to Rule 10b5-1 trading plan."))

        ceo_xml = b"""<?xml version="1.0"?>
        <ownershipDocument>
          <documentType>4</documentType>
          <aff10b5One>0</aff10b5One>
          <issuer><issuerCik>0001045810</issuerCik><issuerTradingSymbol>NVDA</issuerTradingSymbol></issuer>
          <reportingOwner>
            <reportingOwnerId><rptOwnerCik>0009000001</rptOwnerCik><rptOwnerName>DOE JANE</rptOwnerName></reportingOwnerId>
            <reportingOwnerRelationship><isOfficer>1</isOfficer><officerTitle>Chief Executive Officer</officerTitle></reportingOwnerRelationship>
          </reportingOwner>
          <nonDerivativeTable>
            <nonDerivativeTransaction>
              <transactionDate><value>2026-11-01</value></transactionDate>
              <transactionCoding><transactionCode>P</transactionCode></transactionCoding>
              <transactionAmounts>
                <transactionShares><value>5000</value></transactionShares>
                <transactionPricePerShare><value>100.00</value></transactionPricePerShare>
                <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
              </transactionAmounts>
              <footnoteId id="F1"/>
            </nonDerivativeTransaction>
            <nonDerivativeTransaction>
              <transactionDate><value>2026-11-02</value></transactionDate>
              <transactionCoding><transactionCode>P</transactionCode></transactionCoding>
              <transactionAmounts>
                <transactionShares><value>3000</value></transactionShares>
                <transactionPricePerShare><value>102.00</value></transactionPricePerShare>
                <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
              </transactionAmounts>
            </nonDerivativeTransaction>
            <nonDerivativeTransaction>
              <transactionDate><value>2026-11-02</value></transactionDate>
              <transactionCoding><transactionCode>M</transactionCode></transactionCoding>
              <transactionAmounts>
                <transactionShares><value>25000</value></transactionShares>
                <transactionPricePerShare><value>15.00</value></transactionPricePerShare>
                <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
              </transactionAmounts>
            </nonDerivativeTransaction>
            <nonDerivativeTransaction>
              <transactionDate><value>2026-11-02</value></transactionDate>
              <transactionCoding><transactionCode>F</transactionCode></transactionCoding>
              <transactionAmounts>
                <transactionShares><value>9000</value></transactionShares>
                <transactionPricePerShare><value>102.00</value></transactionPricePerShare>
                <transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode>
              </transactionAmounts>
            </nonDerivativeTransaction>
          </nonDerivativeTable>
          <footnotes>
            <footnote id="F1">Open market purchase not made pursuant to Rule 10b5-1 plan.</footnote>
          </footnotes>
        </ownershipDocument>
        """
        ceo_amend_xml = ceo_xml.replace(b"<documentType>4</documentType>", b"<documentType>4/A</documentType>")
        cfo_xml = b"""<?xml version="1.0"?>
        <ownershipDocument>
          <documentType>4</documentType>
          <aff10b5One>1</aff10b5One>
          <issuer><issuerCik>0001045810</issuerCik><issuerTradingSymbol>NVDA</issuerTradingSymbol></issuer>
          <reportingOwner>
            <reportingOwnerId><rptOwnerCik>0009000002</rptOwnerCik><rptOwnerName>SMITH JOHN</rptOwnerName></reportingOwnerId>
            <reportingOwnerRelationship><isOfficer>1</isOfficer><officerTitle>Chief Financial Officer</officerTitle></reportingOwnerRelationship>
          </reportingOwner>
          <nonDerivativeTable>
            <nonDerivativeTransaction>
              <transactionDate><value>2026-11-05</value></transactionDate>
              <transactionCoding><transactionCode>P</transactionCode></transactionCoding>
              <transactionAmounts>
                <transactionShares><value>2000</value></transactionShares>
                <transactionPricePerShare><value>98.00</value></transactionPricePerShare>
                <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
              </transactionAmounts>
            </nonDerivativeTransaction>
          </nonDerivativeTable>
        </ownershipDocument>
        """

        d1 = enrichment._parse_form4_xml(ceo_xml, "0001045810", "0001-26-0001", "2026-11-02", "doc1.xml", "4")
        d1_amend = enrichment._parse_form4_xml(ceo_amend_xml, "0001045810", "0001-26-0002", "2026-11-03", "doc1a.xml", "4/A")
        d2 = enrichment._parse_form4_xml(cfo_xml, "0001045810", "0001-26-0003", "2026-11-06", "doc2.xml", "4")
        self.assertIsNotNone(d1)
        assert d1 is not None and d1_amend is not None and d2 is not None
        # Only 'P' rows kept; 'M' and 'F' excluded; negation 10b5 footnote stays Discretionary
        self.assertEqual(len(d1["buys"]), 2)
        self.assertFalse(d1["buys"][0]["is10b5Plan"])
        self.assertTrue(d2["buys"][0]["is10b5Plan"])

        summary = enrichment.summarize_form4_transactions_for_ticker("NVDA", "0001045810", [d1, d1_amend, d2])
        # 4/A deduplicates CEO's original 2 lots, preserving CFO's 1 lot -> 3 buys total
        self.assertEqual(summary["openMarketBuyCount"], 3)
        self.assertEqual(summary["distinctBuyersCount"], 2)
        self.assertEqual(summary["cSuiteBuyersCount"], 2)
        self.assertEqual(summary["insiderSignalLabel"], "CLUSTER_BUY")
        self.assertTrue(summary["clusterDetected"])
        self.assertEqual(summary["non10b5Count"], 2)

        # Heavy offsetting discretionary sell overrides small single buy
        heavy_sell_xml = b"""<?xml version="1.0"?>
        <ownershipDocument>
          <documentType>4</documentType>
          <aff10b5One>0</aff10b5One>
          <reportingOwner>
            <reportingOwnerId><rptOwnerCik>0009000003</rptOwnerCik><rptOwnerName>SELLER ALEX</rptOwnerName></reportingOwnerId>
            <reportingOwnerRelationship><isDirector>1</isDirector></reportingOwnerRelationship>
          </reportingOwner>
          <nonDerivativeTable>
            <nonDerivativeTransaction>
              <transactionDate><value>2026-11-04</value></transactionDate>
              <transactionCoding><transactionCode>P</transactionCode></transactionCoding>
              <transactionAmounts>
                <transactionShares><value>1000</value></transactionShares>
                <transactionPricePerShare><value>50.00</value></transactionPricePerShare>
                <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
              </transactionAmounts>
            </nonDerivativeTransaction>
            <nonDerivativeTransaction>
              <transactionDate><value>2026-11-05</value></transactionDate>
              <transactionCoding><transactionCode>S</transactionCode></transactionCoding>
              <transactionAmounts>
                <transactionShares><value>10000</value></transactionShares>
                <transactionPricePerShare><value>52.00</value></transactionPricePerShare>
                <transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode>
              </transactionAmounts>
            </nonDerivativeTransaction>
          </nonDerivativeTable>
        </ownershipDocument>
        """
        ds = enrichment._parse_form4_xml(heavy_sell_xml, "0001045810", "0001-26-0004", "2026-11-05", "ds.xml", "4")
        assert ds is not None
        sell_sum = enrichment.summarize_form4_transactions_for_ticker("TEST", "0001045810", [ds])
        self.assertEqual(sell_sum["insiderSignalLabel"], "MIXED_SELL_HEAVY")
        self.assertFalse(sell_sum["clusterDetected"])
        self.assertGreater(sell_sum["discretionarySellValueUsd"], 500000.0)

    def test_compute_dual_conviction_signal_tiers(self) -> None:
        cluster_f4 = {
            "openMarketBuyCount": 3,
            "totalBuyValueUsd": 1_250_000.0,
            "distinctBuyersCount": 2,
            "cSuiteBuyersCount": 2,
            "non10b5Count": 2,
            "insiderSignalLabel": "CLUSTER_BUY",
            "clusterDetected": True,
            "latestBuyDate": "2026-11-10",
            "recentTransactions": [
                {"insiderName": "Jane Doe", "insiderRole": "CEO"},
                {"insiderName": "John Smith", "insiderRole": "CFO"},
            ],
        }

        prime = build_data.compute_dual_conviction_signal(
            ticker="CLD",
            name="Cloud Dip Corp",
            form4_summary=cluster_f4,
            pct_vs_quarter_vwap=-4.5,
            actionability_score=84.0,
            action_verdict="STRONG ACCUMULATE",
            new_count=2,
            added_count=2,
            elite_buyers_count=3,
            elite_holders_count=5,
            net_est_flow_usd=180_000_000.0,
        )
        self.assertIsNotNone(prime)
        assert prime is not None
        self.assertEqual(prime["tier"], "DUAL_CONVICTION_PRIME")
        self.assertGreater(prime["convergenceScore"], 65.0)
        self.assertTrue(prime["nearQuarterVwap"])

        extended = build_data.compute_dual_conviction_signal(
            ticker="CLD",
            name="Cloud Dip Corp",
            form4_summary=cluster_f4,
            pct_vs_quarter_vwap=19.0,
            actionability_score=65.0,
            action_verdict="ACCUMULATE ON DIP",
            new_count=2,
            added_count=1,
            elite_buyers_count=2,
            elite_holders_count=4,
            net_est_flow_usd=90_000_000.0,
        )
        assert extended is not None
        self.assertEqual(extended["tier"], "DUAL_CONVICTION_EXTENDED")

        act_conv = build_data.compute_dual_conviction_signal(
            ticker="ACTV",
            name="Activist Target Inc",
            alerts=[
                {
                    "isActivist": True,
                    "fundName": "Starboard Value",
                    "percentOfClass": 8.2,
                    "stakeStatus": "Active (≥5%)",
                    "item4Category": "Strategic Review / M&A",
                    "item4Intent": "Seeking strategic alternatives and board refresh.",
                    "filingDate": "2026-11-12",
                }
            ],
            elite_holders_count=3,
            new_count=1,
            added_count=1,
            elite_buyers_count=2,
            net_est_flow_usd=60_000_000.0,
        )
        assert act_conv is not None
        self.assertEqual(act_conv["tier"], "ACTIVIST_13F_CONVERGENCE")
        self.assertEqual(act_conv["activistCategory"], "Strategic Review / M&A")

        standalone_13d = build_data.compute_dual_conviction_signal(
            ticker="SOLO",
            name="Solo Catalyst Co",
            alerts=[
                {
                    "isActivist": True,
                    "fundName": "Activist Partners",
                    "percentOfClass": 5.5,
                    "stakeStatus": "Active (≥5%)",
                    "item4Category": "Board / Governance",
                    "filingDate": "2026-11-12",
                }
            ],
            elite_holders_count=0,
        )
        assert standalone_13d is not None
        self.assertEqual(standalone_13d["tier"], "ACTIVIST_13D_CATALYST")

        watch = build_data.compute_dual_conviction_signal(
            ticker="WTCH",
            name="Watch Corp",
            form4_summary=cluster_f4,
            new_count=0,
            added_count=0,
        )
        assert watch is not None
        self.assertEqual(watch["tier"], "INSIDER_CLUSTER_WATCH")

        suppressed = build_data.compute_dual_conviction_signal(
            ticker="SELL",
            name="Sell Heavy Co",
            form4_summary={"openMarketBuyCount": 1, "insiderSignalLabel": "MIXED_SELL_HEAVY"},
            new_count=1,
            added_count=1,
            elite_buyers_count=1,
        )
        self.assertIsNone(suppressed)

    def test_parse_13g_xml_rejects_item4_row11_text_and_cleans_stale_cache(self) -> None:
        exit_13g_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
        <edgarSubmission xmlns="http://www.sec.gov/edgar/schedule13g">
          <formData>
            <submissionType>SCHEDULE 13G/A</submissionType>
            <issuerInfo>
              <issuerCik>0001819394</issuerCik>
              <issuerCusipNumber>00091G104</issuerCusipNumber>
            </issuerInfo>
            <coverPageHeaderReportingPersonDetails>
              <reportingPersonBeneficiallyOwnedAggregateNumberOfShares>0</reportingPersonBeneficiallyOwnedAggregateNumberOfShares>
              <classPercent>0.0</classPercent>
            </coverPageHeaderReportingPersonDetails>
            <item4>
              <ownership>
                <amountBeneficiallyOwned>See Row 9 of cover page for each Reporting Person.</amountBeneficiallyOwned>
                <classPercent>See Row 11 of cover page for each Reporting Person.</classPercent>
              </ownership>
            </item4>
          </formData>
        </edgarSubmission>
        """
        parsed_exit = enrichment._parse_13dg_primary_xml(exit_13g_xml, is_activist=False)
        self.assertEqual(parsed_exit["percentOfClass"], 0.0)
        self.assertEqual(parsed_exit["sharesOwned"], 0)
        self.assertEqual(parsed_exit["subjectCik"], "0001819394")
        self.assertEqual(parsed_exit["item4Intent"], "")
        self.assertEqual(parsed_exit["item4Category"], "Passive (13G)")

        active_13g_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
        <edgarSubmission xmlns="http://www.sec.gov/edgar/schedule13g">
          <formData>
            <submissionType>SCHEDULE 13G/A</submissionType>
            <issuerInfo>
              <issuerCik>0002021528</issuerCik>
              <issuerCusipNumber>15678U102</issuerCusipNumber>
            </issuerInfo>
            <coverPageHeaderReportingPersonDetails>
              <reportingPersonBeneficiallyOwnedAggregateNumberOfShares>9,972,083*</reportingPersonBeneficiallyOwnedAggregateNumberOfShares>
              <classPercent>8.4</classPercent>
            </coverPageHeaderReportingPersonDetails>
            <item4>
              <ownership>
                <amountBeneficiallyOwned>9,972,083*</amountBeneficiallyOwned>
                <classPercent>See Row 11 of cover page for each Reporting Person.</classPercent>
              </ownership>
            </item4>
          </formData>
        </edgarSubmission>
        """
        parsed_active = enrichment._parse_13dg_primary_xml(active_13g_xml, is_activist=False)
        self.assertEqual(parsed_active["percentOfClass"], 8.4)
        self.assertEqual(parsed_active["sharesOwned"], 9972083)
        self.assertEqual(parsed_active["item4Intent"], "")
        self.assertEqual(parsed_active["item4Category"], "Passive (13G)")

        with tempfile.TemporaryDirectory() as td:
            hdr_path = Path(td) / "hdr_cache.json"
            hdr_path.write_text(json.dumps({
                "0001777813-26-000012": {
                    "subjectName": "ACV Auctions Inc.",
                    "subjectCik": "0001819394",
                    "percentOfClass": 11.0,
                    "sharesOwned": 0,
                    "item4Intent": "See Row 11 of cover page for each Reporting Person.",
                    "item4Category": "Active 13D",
                    "stakeChecked": True,
                    "_item4Ver": 2,
                }
            }))
            sub_payload = {
                "name": "Coatue Management",
                "filings": {
                    "recent": {
                        "form": ["SCHEDULE 13G/A"],
                        "accessionNumber": ["0001777813-26-000012"],
                        "filingDate": ["2026-11-10"],
                        "primaryDocument": ["xslSCHEDULE_13G_X01/primary_doc.xml"],
                    }
                },
            }
            with (
                mock.patch.object(enrichment, "MIDQUARTER_HDR_CACHE_PATH", hdr_path),
                mock.patch.object(enrichment, "get_submissions", return_value=sub_payload),
                mock.patch.object(enrichment, "fetch_bytes", return_value=exit_13g_xml),
            ):
                alerts = enrichment.fetch_midquarter_alerts(
                    elite_ciks=["0001777813"],
                    fund_names_by_cik={"0001777813": "Coatue Management"},
                    cik_to_ticker_map={"0001819394": "ACVA"},
                    name_to_ticker_map={},
                    today_date=date(2026, 11, 18),
                    since_days=95,
                )
            self.assertEqual(len(alerts), 1)
            self.assertEqual(alerts[0]["ticker"], "ACVA")
            self.assertEqual(alerts[0]["percentOfClass"], 0.0)
            self.assertEqual(alerts[0]["stakeStatus"], "Exited (0%)")
            self.assertIsNone(alerts[0]["item4Intent"])
            self.assertEqual(alerts[0]["item4Category"], "Passive (13G)")

    def test_form4_cross_row_10b5_footnote_isolation_and_dollar_price(self) -> None:
        mixed_xml = b"""<?xml version="1.0"?>
        <ownershipDocument>
          <documentType>4</documentType>
          <aff10b5One>0</aff10b5One>
          <issuer><issuerCik>50863</issuerCik><issuerTradingSymbol>INTC</issuerTradingSymbol></issuer>
          <reportingOwner>
            <reportingOwnerId><rptOwnerCik>0009000010</rptOwnerCik><rptOwnerName>TAN LIP BU</rptOwnerName></reportingOwnerId>
            <reportingOwnerRelationship><isOfficer>1</isOfficer><officerTitle>Chief Executive Officer</officerTitle></reportingOwnerRelationship>
          </reportingOwner>
          <nonDerivativeTable>
            <nonDerivativeTransaction>
              <transactionDate><value>2026-11-02</value></transactionDate>
              <transactionCoding><transactionCode>S</transactionCode></transactionCoding>
              <transactionAmounts>
                <transactionShares><value>15,000</value></transactionShares>
                <transactionPricePerShare><value>$110.50</value></transactionPricePerShare>
                <transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode>
              </transactionAmounts>
              <footnoteId id="F2"/>
            </nonDerivativeTransaction>
            <nonDerivativeTransaction>
              <transactionDate><value>2026-11-05</value></transactionDate>
              <transactionCoding><transactionCode>P</transactionCode></transactionCoding>
              <transactionAmounts>
                <transactionShares><value>4,000</value></transactionShares>
                <transactionPricePerShare><value>$45.50</value></transactionPricePerShare>
                <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
              </transactionAmounts>
              <footnoteId id="F1"/>
            </nonDerivativeTransaction>
          </nonDerivativeTable>
          <footnotes>
            <footnote id="F1">Open-market purchase executed by reporting person.</footnote>
            <footnote id="F2">Automatic sale effected pursuant to a Rule 10b5-1 trading plan.</footnote>
          </footnotes>
        </ownershipDocument>
        """
        parsed = enrichment._parse_form4_xml(
            mixed_xml,
            cik10="0000050863",
            acc="0000050863-26-000195",
            filing_date="2026-11-06",
            pdoc="form4.xml",
            form_type="4",
        )
        assert parsed is not None
        self.assertEqual(parsed["issuerCik"], "0000050863")
        self.assertEqual(len(parsed["buys"]), 1)
        self.assertFalse(parsed["buys"][0]["is10b5Plan"])
        self.assertEqual(parsed["buys"][0]["pricePerShare"], 45.5)
        self.assertEqual(parsed["buys"][0]["valueUsd"], 182000.0)
        self.assertEqual(parsed["scheduledSellValueUsd"], 1657500.0)
        self.assertEqual(parsed["discretionarySellValueUsd"], 0.0)

    def test_fetch_form4_insider_signals_efts_prefilter_offset_sells_and_fallback(self) -> None:
        intc_buy_xml = b"""<?xml version="1.0"?>
        <ownershipDocument>
          <documentType>4</documentType>
          <aff10b5One>0</aff10b5One>
          <issuer><issuerCik>0000050863</issuerCik><issuerTradingSymbol>INTC</issuerTradingSymbol></issuer>
          <reportingOwner>
            <reportingOwnerId><rptOwnerCik>0009000011</rptOwnerCik><rptOwnerName>BUYER ONE</rptOwnerName></reportingOwnerId>
            <reportingOwnerRelationship><isDirector>1</isDirector></reportingOwnerRelationship>
          </reportingOwner>
          <nonDerivativeTable>
            <nonDerivativeTransaction>
              <transactionDate><value>2026-11-05</value></transactionDate>
              <transactionCoding><transactionCode>P</transactionCode></transactionCoding>
              <transactionAmounts>
                <transactionShares><value>1000</value></transactionShares>
                <transactionPricePerShare><value>50.00</value></transactionPricePerShare>
                <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
              </transactionAmounts>
            </nonDerivativeTransaction>
          </nonDerivativeTable>
        </ownershipDocument>
        """
        intc_disc_sell_xml = b"""<?xml version="1.0"?>
        <ownershipDocument>
          <documentType>4</documentType>
          <aff10b5One>0</aff10b5One>
          <issuer><issuerCik>0000050863</issuerCik><issuerTradingSymbol>INTC</issuerTradingSymbol></issuer>
          <reportingOwner>
            <reportingOwnerId><rptOwnerCik>0009000012</rptOwnerCik><rptOwnerName>SELLER TWO</rptOwnerName></reportingOwnerId>
            <reportingOwnerRelationship><isOfficer>1</isOfficer><officerTitle>Chief Financial Officer</officerTitle></reportingOwnerRelationship>
          </reportingOwner>
          <nonDerivativeTable>
            <nonDerivativeTransaction>
              <transactionDate><value>2026-11-08</value></transactionDate>
              <transactionCoding><transactionCode>S</transactionCode></transactionCoding>
              <transactionAmounts>
                <transactionShares><value>20000</value></transactionShares>
                <transactionPricePerShare><value>45.00</value></transactionPricePerShare>
                <transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode>
              </transactionAmounts>
            </nonDerivativeTransaction>
          </nonDerivativeTable>
        </ownershipDocument>
        """
        other_issuer_xml = b"""<?xml version="1.0"?>
        <ownershipDocument>
          <documentType>4</documentType>
          <aff10b5One>0</aff10b5One>
          <issuer><issuerCik>0001780312</issuerCik><issuerTradingSymbol>ASTS</issuerTradingSymbol></issuer>
          <reportingOwner>
            <reportingOwnerId><rptOwnerCik>0001652044</rptOwnerCik><rptOwnerName>ALPHABET INC</rptOwnerName></reportingOwnerId>
            <reportingOwnerRelationship><isTenPercentOwner>1</isTenPercentOwner></reportingOwnerRelationship>
          </reportingOwner>
          <nonDerivativeTable>
            <nonDerivativeTransaction>
              <transactionDate><value>2026-11-04</value></transactionDate>
              <transactionCoding><transactionCode>P</transactionCode></transactionCoding>
              <transactionAmounts>
                <transactionShares><value>50000</value></transactionShares>
                <transactionPricePerShare><value>25.00</value></transactionPricePerShare>
                <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
              </transactionAmounts>
            </nonDerivativeTransaction>
          </nonDerivativeTable>
        </ownershipDocument>
        """

        msft_sell1_xml = b"""<?xml version="1.0"?>
        <ownershipDocument>
          <documentType>4</documentType>
          <aff10b5One>0</aff10b5One>
          <issuer><issuerCik>0000789019</issuerCik><issuerTradingSymbol>MSFT</issuerTradingSymbol></issuer>
          <reportingOwner>
            <reportingOwnerId><rptOwnerCik>0009000021</rptOwnerCik><rptOwnerName>MSFT CEO</rptOwnerName></reportingOwnerId>
            <reportingOwnerRelationship><isOfficer>1</isOfficer><officerTitle>Chief Executive Officer</officerTitle></reportingOwnerRelationship>
          </reportingOwner>
          <nonDerivativeTable>
            <nonDerivativeTransaction>
              <transactionDate><value>2026-11-07</value></transactionDate>
              <transactionCoding><transactionCode>S</transactionCode></transactionCoding>
              <transactionAmounts>
                <transactionShares><value>1500</value></transactionShares>
                <transactionPricePerShare><value>400.00</value></transactionPricePerShare>
                <transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode>
              </transactionAmounts>
            </nonDerivativeTransaction>
          </nonDerivativeTable>
        </ownershipDocument>
        """
        msft_sell2_xml = b"""<?xml version="1.0"?>
        <ownershipDocument>
          <documentType>4</documentType>
          <aff10b5One>0</aff10b5One>
          <issuer><issuerCik>0000789019</issuerCik><issuerTradingSymbol>MSFT</issuerTradingSymbol></issuer>
          <reportingOwner>
            <reportingOwnerId><rptOwnerCik>0009000022</rptOwnerCik><rptOwnerName>MSFT CFO</rptOwnerName></reportingOwnerId>
            <reportingOwnerRelationship><isOfficer>1</isOfficer><officerTitle>Chief Financial Officer</officerTitle></reportingOwnerRelationship>
          </reportingOwner>
          <nonDerivativeTable>
            <nonDerivativeTransaction>
              <transactionDate><value>2026-11-08</value></transactionDate>
              <transactionCoding><transactionCode>S</transactionCode></transactionCoding>
              <transactionAmounts>
                <transactionShares><value>1250</value></transactionShares>
                <transactionPricePerShare><value>400.00</value></transactionPricePerShare>
                <transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode>
              </transactionAmounts>
            </nonDerivativeTransaction>
          </nonDerivativeTable>
        </ownershipDocument>
        """

        efts_p_response = {
            "hits": {
                "total": {"value": 2, "relation": "eq"},
                "hits": [
                    {
                        "_id": "0000050863-26-000101:buy4.xml",
                        "_source": {
                            "adsh": "0000050863-26-000101",
                            "file_date": "2026-11-06",
                            "form": "4",
                            "ciks": ["0009000011", "0000050863"],
                        },
                    },
                    {
                        "_id": "0001652044-26-000202:asts4.xml",
                        "_source": {
                            "adsh": "0001652044-26-000202",
                            "file_date": "2026-11-05",
                            "form": "4",
                            "ciks": ["0001652044", "0001780312"],
                        },
                    },
                ],
            }
        }
        efts_s_response = {
            "hits": {
                "total": {"value": 3, "relation": "eq"},
                "hits": [
                    {
                        "_id": "0000050863-26-000102:sell4.xml",
                        "_source": {
                            "adsh": "0000050863-26-000102",
                            "file_date": "2026-11-09",
                            "form": "4",
                            "ciks": ["0009000012", "0000050863"],
                        },
                    },
                    {
                        "_id": "0000789019-26-000301:msft_sell1.xml",
                        "_source": {
                            "adsh": "0000789019-26-000301",
                            "file_date": "2026-11-07",
                            "form": "4",
                            "ciks": ["0009000021", "0000789019"],
                        },
                    },
                    {
                        "_id": "0000789019-26-000302:msft_sell2.xml",
                        "_source": {
                            "adsh": "0000789019-26-000302",
                            "file_date": "2026-11-08",
                            "form": "4",
                            "ciks": ["0009000022", "0000789019"],
                        },
                    },
                ],
            }
        }

        efts_calls: list[str] = []
        sub_calls: list[str] = []

        def _fake_fetch_json(url: str, cache_key: object = None, max_age_hours: float = 0) -> dict[str, object]:
            efts_calls.append(url)
            if "q=S" in url:
                # Pass 2 scans both p_buy_ciks (INTC) AND top priority CIKs (MSFT, GOOG) for pure discretionary sell clusters
                self.assertIn("0000050863", url)
                self.assertIn("0000789019", url)
                self.assertIn("0001652044", url)
                return efts_s_response
            return efts_p_response

        def _fake_get_submissions(cik10: str) -> dict[str, object]:
            sub_calls.append(cik10)
            return {
                "filings": {
                    "recent": {
                        "form": ["4", "4"],
                        "accessionNumber": ["0000050863-26-000102", "0000050863-26-000101"],
                        "filingDate": ["2026-11-09", "2026-11-06"],
                        "primaryDocument": ["xslF345X05/sell4.xml", "xslF345X05/buy4.xml"],
                    }
                }
            }

        def _fake_fetch_bytes(url: str, cache_key: object = None, max_age_hours: float = 0) -> bytes:
            if "buy4.xml" in url:
                return intc_buy_xml
            if "sell4.xml" in url:
                return intc_disc_sell_xml
            if "asts4.xml" in url:
                return other_issuer_xml
            if "msft_sell1.xml" in url:
                return msft_sell1_xml
            if "msft_sell2.xml" in url:
                return msft_sell2_xml
            raise AssertionError(f"Unexpected fetch_bytes URL {url}")

        cik_map = {
            "INTC": "0000050863",
            "MSFT": "0000789019",
            "GOOG": "0001652044",
        }

        with tempfile.TemporaryDirectory() as td:
            parsed_path = Path(td) / "f4_parsed.json"
            ticker_path = Path(td) / "f4_ticker.json"
            with (
                mock.patch.object(enrichment, "FORM4_PARSED_CACHE_PATH", parsed_path),
                mock.patch.object(enrichment, "FORM4_TICKER_CACHE_PATH", ticker_path),
                mock.patch.object(enrichment, "fetch_json", side_effect=_fake_fetch_json),
                mock.patch.object(enrichment, "fetch_bytes", side_effect=_fake_fetch_bytes),
                mock.patch.object(enrichment, "get_submissions", side_effect=_fake_get_submissions),
            ):
                res = enrichment.fetch_form4_insider_signals(
                    ["INTC", "MSFT", "GOOG"],
                    cik_map,
                    today_date=date(2026, 11, 18),
                )

        # Happy path completes solely via EFTS q=P + q=S with zero get_submissions calls
        self.assertEqual(sub_calls, [])
        self.assertEqual(len(efts_calls), 2)
        self.assertIn("INTC", res)
        self.assertEqual(res["INTC"]["openMarketBuyCount"], 1)
        self.assertEqual(res["INTC"]["discretionarySellValueUsd"], 900000.0)
        self.assertEqual(res["INTC"]["insiderSignalLabel"], "MIXED_SELL_HEAVY")
        # MSFT has pure discretionary C-suite sell cluster (0 buys, 2 officers selling $1.1M)
        self.assertIn("MSFT", res)
        self.assertEqual(res["MSFT"]["openMarketBuyCount"], 0)
        self.assertEqual(res["MSFT"]["insiderSignalLabel"], "CLUSTER_SELL")
        self.assertTrue(res["MSFT"]["sellClusterDetected"])
        self.assertEqual(res["MSFT"]["distinctSellersCount"], 2)
        self.assertEqual(res["MSFT"]["discretionarySellValueUsd"], 1100000.0)
        # GOOG only bought ASTS (different issuer) and had 0 own sells -> absent
        self.assertNotIn("GOOG", res)

        # Verify automatic get_submissions fallback if Pass 1 (q=P) fails OR Pass 2 (q=S) fails
        with tempfile.TemporaryDirectory() as td2:
            parsed_path2 = Path(td2) / "f4_parsed.json"
            ticker_path2 = Path(td2) / "f4_ticker.json"
            with (
                mock.patch.object(enrichment, "FORM4_PARSED_CACHE_PATH", parsed_path2),
                mock.patch.object(enrichment, "FORM4_TICKER_CACHE_PATH", ticker_path2),
                mock.patch.object(enrichment, "fetch_json", side_effect=RuntimeError("EFTS 503")),
                mock.patch.object(enrichment, "fetch_bytes", side_effect=_fake_fetch_bytes),
                mock.patch.object(enrichment, "get_submissions", side_effect=_fake_get_submissions),
            ):
                fb_res = enrichment.fetch_form4_insider_signals(
                    ["INTC"],
                    {"INTC": "0000050863"},
                    today_date=date(2026, 11, 18),
                )
        self.assertEqual(sub_calls, ["0000050863"])
        self.assertIn("INTC", fb_res)
        self.assertEqual(fb_res["INTC"]["openMarketBuyCount"], 1)
        self.assertEqual(fb_res["INTC"]["discretionarySellValueUsd"], 900000.0)
        self.assertEqual(fb_res["INTC"]["insiderSignalLabel"], "MIXED_SELL_HEAVY")


class TestP0AuditAndIdealUpgrades(unittest.TestCase):
    def test_xbrl_share_count_yoy_no_quarter_summing_and_split_guard(self) -> None:
        # 4 standalone quarters per year: diluted share counts must be compared like-for-like (NOT summed!)
        facts_steady = {
            "facts": {
                "us-gaap": {
                    "WeightedAverageNumberOfDilutedSharesOutstanding": {
                        "units": {
                            "shares": [
                                {"start": "2024-01-01", "end": "2024-03-31", "filed": "2024-05-01", "val": 100_000_000},
                                {"start": "2024-04-01", "end": "2024-06-30", "filed": "2024-08-01", "val": 100_000_000},
                                {"start": "2024-07-01", "end": "2024-09-30", "filed": "2024-11-01", "val": 100_000_000},
                                {"start": "2024-10-01", "end": "2024-12-31", "filed": "2025-02-01", "val": 100_000_000},
                                {"start": "2025-01-01", "end": "2025-03-31", "filed": "2025-05-01", "val": 102_000_000},
                                {"start": "2025-04-01", "end": "2025-06-30", "filed": "2025-08-01", "val": 102_000_000},
                                {"start": "2025-07-01", "end": "2025-09-30", "filed": "2025-11-01", "val": 102_000_000},
                                {"start": "2025-10-01", "end": "2025-12-31", "filed": "2026-02-01", "val": 102_000_000},
                            ]
                        }
                    }
                }
            }
        }
        yoy = enrichment._extract_share_count_yoy(
            facts_steady,
            [("us-gaap", "WeightedAverageNumberOfDilutedSharesOutstanding")],
        )
        self.assertIsNotNone(yoy)
        self.assertAlmostEqual(float(yoy), 2.0, places=2)

        # Unadjusted 4:1 split jump must be caught and suppressed as None
        self.assertTrue(enrichment._is_likely_unadjusted_split_ratio(4.0))
        self.assertTrue(enrichment._is_likely_unadjusted_split_ratio(0.25))
        self.assertFalse(enrichment._is_likely_unadjusted_split_ratio(1.03))
        facts_split = {
            "facts": {
                "us-gaap": {
                    "WeightedAverageNumberOfDilutedSharesOutstanding": {
                        "units": {
                            "shares": [
                                {"start": "2024-10-01", "end": "2024-12-31", "filed": "2025-02-01", "val": 100_000_000},
                                {"start": "2025-10-01", "end": "2025-12-31", "filed": "2026-02-01", "val": 400_000_000},
                            ]
                        }
                    }
                }
            }
        }
        self.assertIsNone(
            enrichment._extract_share_count_yoy(
                facts_split,
                [("us-gaap", "WeightedAverageNumberOfDilutedSharesOutstanding")],
            )
        )

    def test_form4_espp_materiality_adr_normalization_and_entity_dedup(self) -> None:
        # 1. ESPP / DRIP footnote open-market Code P must be ignored
        espp_xml = b"""<?xml version="1.0"?>
        <ownershipDocument>
          <documentType>4</documentType>
          <issuer><issuerCik>0001046179</issuerCik><issuerTradingSymbol>TSM</issuerTradingSymbol></issuer>
          <reportingOwner>
            <reportingOwnerId><rptOwnerCik>0009000101</rptOwnerCik><rptOwnerName>ESPP OFFICER</rptOwnerName></reportingOwnerId>
            <reportingOwnerRelationship><isOfficer>1</isOfficer><officerTitle>VP Engineering</officerTitle></reportingOwnerRelationship>
          </reportingOwner>
          <nonDerivativeTable>
            <nonDerivativeTransaction>
              <securityTitle><value>Common Stock</value></securityTitle>
              <transactionDate><value>2026-10-15</value></transactionDate>
              <transactionCoding><transactionCode>P</transactionCode></transactionCoding>
              <transactionAmounts>
                <transactionShares><value>500</value><footnoteId id="F1"/></transactionShares>
                <transactionPricePerShare><value>350.00</value></transactionPricePerShare>
                <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
              </transactionAmounts>
            </nonDerivativeTransaction>
          </nonDerivativeTable>
          <footnotes>
            <footnote id="F1">Shares acquired under the Issuer Employee Stock Purchase Plan (ESPP).</footnote>
          </footnotes>
        </ownershipDocument>
        """
        parsed_espp = enrichment._parse_form4_xml(espp_xml, "0001046179", "acc-espp", "2026-10-16")
        self.assertIsNotNone(parsed_espp)
        self.assertEqual(parsed_espp["buys"], [])

        # 2. Micro-buy (<$25K) excluded from material signal; 5:1 ADR ordinary-share price normalized
        adr_xml = b"""<?xml version="1.0"?>
        <ownershipDocument>
          <documentType>4</documentType>
          <issuer><issuerCik>0001046179</issuerCik><issuerTradingSymbol>TSM</issuerTradingSymbol></issuer>
          <reportingOwner>
            <reportingOwnerId><rptOwnerCik>0009000102</rptOwnerCik><rptOwnerName>TSM DIRECTOR</rptOwnerName></reportingOwnerId>
            <reportingOwnerRelationship><isDirector>1</isDirector></reportingOwnerRelationship>
          </reportingOwner>
          <nonDerivativeTable>
            <nonDerivativeTransaction>
              <securityTitle><value>Common Shares</value></securityTitle>
              <transactionDate><value>2026-10-20</value></transactionDate>
              <transactionCoding><transactionCode>P</transactionCode></transactionCoding>
              <transactionAmounts>
                <transactionShares><value>500</value></transactionShares>
                <transactionPricePerShare><value>70.50</value></transactionPricePerShare>
                <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
              </transactionAmounts>
            </nonDerivativeTransaction>
          </nonDerivativeTable>
        </ownershipDocument>
        """
        parsed_adr = enrichment._parse_form4_xml(adr_xml, "0001046179", "acc-adr", "2026-10-21")
        self.assertIsNotNone(parsed_adr)
        # 500 ordinary shares @ $70.50 = $35,250 -> normalized to 100 ADS @ $352.50 against $363 ref price
        summ_adr = enrichment.summarize_form4_transactions_for_ticker(
            "TSM", "0001046179", [parsed_adr], ref_price_usd=363.0
        )
        self.assertEqual(summ_adr["openMarketBuyCount"], 1)
        self.assertAlmostEqual(summ_adr["avgBuyPriceUsd"], 352.5, places=1)
        self.assertAlmostEqual(summ_adr["totalBuyValueUsd"], 35250.0, places=1)
        self.assertFalse(summ_adr["priceUnitMismatch"])

        # Micro-buy below $25K materiality floor (e.g., $3,600) yields NONE
        micro_doc = dict(parsed_adr)
        micro_doc["buys"] = [
            {
                **parsed_adr["buys"][0],
                "shares": 10.0,
                "pricePerShare": 360.0,
                "valueUsd": 3600.0,
            }
        ]
        summ_micro = enrichment.summarize_form4_transactions_for_ticker(
            "TSM", "0001046179", [micro_doc], ref_price_usd=363.0
        )
        self.assertEqual(summ_micro["insiderSignalLabel"], "NONE")
        self.assertEqual(summ_micro["distinctBuyersCount"], 0)

        # 3. Affiliated 10%-owner PE/VC entities collapse into 1 buyer AND cannot trigger CLUSTER_BUY
        pe_xml_1 = b"""<?xml version="1.0"?>
        <ownershipDocument>
          <documentType>4</documentType>
          <issuer><issuerCik>0001555555</issuerCik><issuerTradingSymbol>PECO</issuerTradingSymbol></issuer>
          <reportingOwner>
            <reportingOwnerId><rptOwnerCik>0001110001</rptOwnerCik><rptOwnerName>SILVER LAKE PARTNERS V LP</rptOwnerName></reportingOwnerId>
            <reportingOwnerRelationship><isTenPercentOwner>1</isTenPercentOwner></reportingOwnerRelationship>
          </reportingOwner>
          <nonDerivativeTable>
            <nonDerivativeTransaction>
              <securityTitle><value>Common Stock</value></securityTitle>
              <transactionDate><value>2026-10-09</value></transactionDate>
              <transactionCoding><transactionCode>P</transactionCode></transactionCoding>
              <transactionAmounts>
                <transactionShares><value>10000</value></transactionShares>
                <transactionPricePerShare><value>50.00</value></transactionPricePerShare>
                <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
              </transactionAmounts>
            </nonDerivativeTransaction>
          </nonDerivativeTable>
        </ownershipDocument>
        """
        pe_xml_2 = b"""<?xml version="1.0"?>
        <ownershipDocument>
          <documentType>4</documentType>
          <issuer><issuerCik>0001555555</issuerCik><issuerTradingSymbol>PECO</issuerTradingSymbol></issuer>
          <reportingOwner>
            <reportingOwnerId><rptOwnerCik>0002220002</rptOwnerCik><rptOwnerName>SILVER LAKE PARTNERS VI DE LP</rptOwnerName></reportingOwnerId>
            <reportingOwnerRelationship><isTenPercentOwner>1</isTenPercentOwner></reportingOwnerRelationship>
          </reportingOwner>
          <nonDerivativeTable>
            <nonDerivativeTransaction>
              <securityTitle><value>Common Stock</value></securityTitle>
              <transactionDate><value>2026-10-10</value></transactionDate>
              <transactionCoding><transactionCode>P</transactionCode></transactionCoding>
              <transactionAmounts>
                <transactionShares><value>12000</value></transactionShares>
                <transactionPricePerShare><value>50.00</value></transactionPricePerShare>
                <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
              </transactionAmounts>
            </nonDerivativeTransaction>
          </nonDerivativeTable>
        </ownershipDocument>
        """
        pe_docs = [
            enrichment._parse_form4_xml(pe_xml_1, "0001555555", "acc-sl1", "2026-10-10"),
            enrichment._parse_form4_xml(pe_xml_2, "0001555555", "acc-sl2", "2026-10-11"),
        ]
        summ_pe = enrichment.summarize_form4_transactions_for_ticker(
            "PECO", "0001555555", [d for d in pe_docs if d is not None], ref_price_usd=50.0
        )
        self.assertEqual(summ_pe["distinctBuyersCount"], 1)
        self.assertFalse(summ_pe["clusterDetected"])
        self.assertEqual(summ_pe["insiderSignalLabel"], "SINGLE_BUY")

    def test_actionability_humped_price_mcap_flow_and_proportion_exit(self) -> None:
        # 1. Humped Price Discipline: modest pullback (-6%) beats falling knife (-35%) and chased (+25%)
        base_kw = dict(
            new_count=2, added_count=3, trimmed_count=1, exited_count=0,
            elite_buyers_count=5, elite_sellers_count=1, elite_holders_count=10,
            total_value_usd=500_000_000.0, net_est_flow_usd=100_000_000.0,
            market_cap_usd=20_000_000_000.0,
        )
        m_sweet = build_data.compute_actionability_matrix(**base_kw, pct_vs_quarter_vwap=-6.0)
        m_knife = build_data.compute_actionability_matrix(**base_kw, pct_vs_quarter_vwap=-35.0)
        m_chased = build_data.compute_actionability_matrix(**base_kw, pct_vs_quarter_vwap=25.0)
        self.assertGreater(m_sweet["pillars"]["priceDiscipline"], m_knife["pillars"]["priceDiscipline"])
        self.assertGreater(m_sweet["pillars"]["priceDiscipline"], m_chased["pillars"]["priceDiscipline"])

        # 2. Market-cap normalized Flow Impulse: $100M inflow on $5B stock > $100M inflow on $3T mega-cap
        m_sm = build_data.compute_actionability_matrix(
            new_count=2, added_count=2, trimmed_count=0, exited_count=0,
            elite_buyers_count=4, elite_sellers_count=0, elite_holders_count=6,
            total_value_usd=300_000_000.0, net_est_flow_usd=100_000_000.0,
            market_cap_usd=5_000_000_000.0,
        )
        m_mega = build_data.compute_actionability_matrix(
            new_count=2, added_count=2, trimmed_count=0, exited_count=0,
            elite_buyers_count=4, elite_sellers_count=0, elite_holders_count=6,
            total_value_usd=300_000_000.0, net_est_flow_usd=100_000_000.0,
            market_cap_usd=3_000_000_000_000.0,
        )
        self.assertGreater(m_sm["pillars"]["flowImpulse"], m_mega["pillars"]["flowImpulse"])

        # 3. Proportion-aware EXIT SIGNAL: mega-cap with 20 holders, 2 exits, 10 buyers must NOT falsely EXIT SIGNAL
        m_megacap_normal = build_data.compute_actionability_matrix(
            new_count=4, added_count=6, trimmed_count=5, exited_count=2,
            elite_buyers_count=10, elite_sellers_count=7, elite_holders_count=20,
            total_value_usd=5_000_000_000.0, net_est_flow_usd=200_000_000.0,
            pct_vs_quarter_vwap=-4.0, market_cap_usd=2_500_000_000_000.0,
        )
        self.assertNotEqual(m_megacap_normal["verdict"], "EXIT SIGNAL")

        # True institutional distribution (85%+ sell dominance, 3/10 exiting, heavy outflow) MUST trigger EXIT SIGNAL
        m_liquidation = build_data.compute_actionability_matrix(
            new_count=0, added_count=1, trimmed_count=3, exited_count=3,
            elite_buyers_count=1, elite_sellers_count=6, elite_holders_count=8,
            total_value_usd=400_000_000.0, net_est_flow_usd=-250_000_000.0,
            pct_vs_quarter_vwap=-15.0, market_cap_usd=15_000_000_000.0,
        )
        self.assertEqual(m_liquidation["verdict"], "EXIT SIGNAL")

    def test_dual_conviction_bounded_vwap_net_flow_and_sell_suppression(self) -> None:
        strong_f4 = {
            "openMarketBuyCount": 2,
            "totalBuyValueUsd": 500_000.0,
            "distinctBuyersCount": 2,
            "cSuiteBuyersCount": 1,
            "clusterDetected": True,
            "insiderSignalLabel": "CLUSTER_BUY",
        }
        # Deep drawdown (-32%) must NOT qualify as nearQuarterVwap or DUAL_CONVICTION_PRIME
        res_knife = build_data.compute_dual_conviction_signal(
            ticker="TTMI", name="TTM Tech",
            form4_summary=strong_f4,
            pct_vs_quarter_vwap=-32.0,
            actionability_score=62.0, action_verdict="HOLD / MONITOR",
            new_count=2, added_count=1, trimmed_count=1, exited_count=0,
            elite_buyers_count=3, elite_sellers_count=1, elite_holders_count=8,
            net_est_flow_usd=50_000_000.0,
        )
        self.assertIsNotNone(res_knife)
        self.assertFalse(res_knife["nearQuarterVwap"])
        self.assertNotEqual(res_knife["tier"], "DUAL_CONVICTION_PRIME")

        # Negative net_est_flow_usd blocks has13FAccumulation even with new + added >= 2
        res_neg_flow = build_data.compute_dual_conviction_signal(
            ticker="TTMI", name="TTM Tech",
            form4_summary=strong_f4,
            pct_vs_quarter_vwap=-5.0,
            actionability_score=65.0, action_verdict="HOLD / MONITOR",
            new_count=2, added_count=2, trimmed_count=1, exited_count=0,
            elite_buyers_count=4, elite_sellers_count=1, elite_holders_count=8,
            net_est_flow_usd=-133_000_000.0,
        )
        self.assertIsNotNone(res_neg_flow)
        self.assertFalse(res_neg_flow["has13FAccumulation"])

        # Pure CLUSTER_SELL Form 4 must be suppressed from buy convergence feed
        sell_f4 = {
            "openMarketBuyCount": 0,
            "totalBuyValueUsd": 0.0,
            "distinctBuyersCount": 0,
            "cSuiteBuyersCount": 0,
            "clusterDetected": False,
            "discretionarySellValueUsd": 2_000_000.0,
            "insiderSignalLabel": "CLUSTER_SELL",
        }
        res_sell = build_data.compute_dual_conviction_signal(
            ticker="SELLME", name="Sell Corp",
            form4_summary=sell_f4,
            pct_vs_quarter_vwap=-2.0,
            new_count=2, added_count=2, elite_buyers_count=4, elite_holders_count=8,
            net_est_flow_usd=50_000_000.0,
        )
        self.assertIsNone(res_sell)

    def test_ideal_portfolio_subsector_cap_exit_signal_exclusion_and_tx_cost(self) -> None:
        # 1. Dual waterfill across 4 sub-sectors: 6 Semis + 5 Software + 4 Cloud + 4 Cyber -> sum==1.0, stock<=0.08, Semis<=0.30
        raw_scores = {
            **{f"SEMI{i}": 10.0 - 0.3 * i for i in range(6)},
            **{f"SOFT{i}": 4.0 - 0.2 * i for i in range(5)},
            **{f"CLD{i}": 3.5 - 0.2 * i for i in range(4)},
            **{f"CYB{i}": 3.2 - 0.2 * i for i in range(4)},
        }
        sub_map = {
            **{f"SEMI{i}": "Semiconductors" for i in range(6)},
            **{f"SOFT{i}": "Software—Application" for i in range(5)},
            **{f"CLD{i}": "Cloud & Infrastructure" for i in range(4)},
            **{f"CYB{i}": "Cybersecurity" for i in range(4)},
        }
        w_cap = build_data.waterfill_sector_and_stock_capped_weights(
            raw_scores, sub_map, stock_cap=0.08, subsector_cap=0.30
        )
        self.assertAlmostEqual(sum(w_cap.values()), 1.0, places=5)
        self.assertLessEqual(max(w_cap.values()), 0.0801)
        semi_sum = sum(w for t, w in w_cap.items() if sub_map[t] == "Semiconductors")
        self.assertLessEqual(semi_sum, 0.3001)

        # 2. Actionability EXIT SIGNAL exclusion + vol/exit damping in compute_ideal_portfolio_weights
        n_dates = 80
        cal_dates = [(date(2026, 1, 2) + timedelta(days=i)).isoformat() for i in range(n_dates)]
        rng = np.random.default_rng(7)
        # CALM: steady low-vol uptrend; WILD: huge vol swings; EXITME: tagged EXIT SIGNAL
        px_calm = 100.0 * np.cumprod(1.0 + np.full(n_dates, 0.001))
        px_wild = 100.0 * np.cumprod(1.0 + rng.normal(0.001, 0.055, size=n_dates))
        px_exit = 100.0 * np.cumprod(1.0 + np.full(n_dates, 0.001))
        px_other = 100.0 * np.cumprod(1.0 + np.full(n_dates, 0.001))
        aligned = {
            "CALM": (px_calm, 0, n_dates - 1),
            "WILD": (px_wild, 0, n_dates - 1),
            "EXITME": (px_exit, 0, n_dates - 1),
            "OTHR1": (px_other, 0, n_dates - 1),
            "OTHR2": (px_other, 0, n_dates - 1),
            "OTHR3": (px_other, 0, n_dates - 1),
        }
        cusip_meta = {
            "C_CALM": {"ticker": "CALM", "name": "Calm Corp", "sector": "Technology", "industry": "Software—Application", "isTechTmt": True},
            "C_WILD": {"ticker": "WILD", "name": "Wild Corp", "sector": "Technology", "industry": "Software—Application", "isTechTmt": True},
            "C_EXIT": {"ticker": "EXITME", "name": "Exit Corp", "sector": "Technology", "industry": "Semiconductors", "isTechTmt": True},
            "C_O1": {"ticker": "OTHR1", "name": "Other 1", "sector": "Technology", "industry": "Semiconductors", "isTechTmt": True},
            "C_O2": {"ticker": "OTHR2", "name": "Other 2", "sector": "Technology", "industry": "Information Technology Services", "isTechTmt": True},
            "C_O3": {"ticker": "OTHR3", "name": "Other 3", "sector": "Technology", "industry": "Software—Infrastructure", "isTechTmt": True},
        }
        funds_subset = [
            {
                "cik": f"000100000{k}",
                "name": f"Alpha Tech Fund {k}",
                "metrics": {"lag": {"skillScore": 75.0}},
                "latestQuarter": "2026-03-31",
                "latestEquityAum": 1_000_000_000.0,
                "holdings": [
                    {"securityType": "EQUITY", "cusip": "C_CALM", "ticker": "CALM", "name": "Calm Corp", "sector": "Technology", "industry": "Software—Application", "weightPct": 18.0, "valueUsd": 180_000_000.0, "status": "ADDED", "isTechTmt": True},
                    {"securityType": "EQUITY", "cusip": "C_WILD", "ticker": "WILD", "name": "Wild Corp", "sector": "Technology", "industry": "Software—Application", "weightPct": 18.0, "valueUsd": 180_000_000.0, "status": "TRIMMED", "isTechTmt": True},
                    {"securityType": "EQUITY", "cusip": "C_EXIT", "ticker": "EXITME", "name": "Exit Corp", "sector": "Technology", "industry": "Semiconductors", "weightPct": 24.0, "valueUsd": 240_000_000.0, "status": "HELD", "isTechTmt": True},
                    {"securityType": "EQUITY", "cusip": "C_O1", "ticker": "OTHR1", "name": "Other 1", "sector": "Technology", "industry": "Semiconductors", "weightPct": 14.0, "valueUsd": 140_000_000.0, "status": "HELD", "isTechTmt": True},
                    {"securityType": "EQUITY", "cusip": "C_O2", "ticker": "OTHR2", "name": "Other 2", "sector": "Technology", "industry": "Information Technology Services", "weightPct": 13.0, "valueUsd": 130_000_000.0, "status": "HELD", "isTechTmt": True},
                    {"securityType": "EQUITY", "cusip": "C_O3", "ticker": "OTHR3", "name": "Other 3", "sector": "Technology", "industry": "Software—Infrastructure", "weightPct": 13.0, "valueUsd": 130_000_000.0, "status": "HELD", "isTechTmt": True},
                ],
            }
            for k in range(3)
        ]
        navs = {f["cik"]: np.linspace(100.0, 125.0, n_dates) for f in funds_subset}
        win_indices = {"TTM": 0, "3Y": 0, "5Y": 0}
        weights, _, _ = build_data.compute_ideal_portfolio_weights(
            funds_subset, "lag", cusip_meta, navs, cal_dates, win_indices,
            max_names=10, cap_pct=35.0, aligned_prices=aligned, n_dates=n_dates,
            exited_by_ticker={"WILD": 2},
            actionability_by_ticker={"EXITME": {"verdict": "EXIT SIGNAL", "score": 28.0}},
            subsector_cap_pct=45.0,
        )
        self.assertNotIn("EXITME", weights)
        self.assertGreater(weights["CALM"], weights["WILD"])

        # 3. simulate_nav_series transaction-cost slippage deduction on rebalance
        flat_px = np.full(10, 100.0, dtype=np.float64)
        sim_prices = {"AAA": (flat_px, 0, 9), "BBB": (flat_px, 0, 9)}
        segs = [(0, {"AAA": 1.0}), (5, {"BBB": 1.0})]  # 100% one-way turnover => 2x = 2.0 total traded
        nav_zero = build_data.simulate_nav_series(segs, sim_prices, 10, tx_cost_bps=0.0)
        nav_slip = build_data.simulate_nav_series(segs, sim_prices, 10, tx_cost_bps=10.0)
        self.assertAlmostEqual(float(nav_zero[-1]), 100.0, places=4)
        self.assertAlmostEqual(float(nav_slip[-1]), 100.0 * (1.0 - 2.0 * 0.001), places=4)

    def test_security_resolver_14d_fail_ttl_and_dossier_ticker_quarantine(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            cusip_cache_p = td_path / "cusip_cache.json"
            ticker_meta_p = td_path / "ticker_meta_cache.json"
            sec_tickers_p = td_path / "company_tickers.json"

            now_ts = time.time()
            cusip_cache_p.write_text(json.dumps({
                "111111119": {
                    "ticker": "CUSIP:111111119",
                    "name": "Expired Fail",
                    "resolvedFail": True,
                    "isResolved": False,
                    "failTs": now_ts - (20.0 * 86400.0),
                },
                "222222229": {
                    "ticker": "CUSIP:222222229",
                    "name": "Fresh Fail",
                    "resolvedFail": True,
                    "isResolved": False,
                    "failTs": now_ts - (2.0 * 86400.0),
                },
            }), encoding="utf-8")
            ticker_meta_p.write_text(json.dumps({}), encoding="utf-8")
            sec_tickers_p.write_text(json.dumps({}), encoding="utf-8")

            openfigi_seen: list[str] = []

            def _fake_openfigi(jobs: list[dict[str, str]]) -> list[dict[str, object]]:
                for j in jobs:
                    openfigi_seen.append(str(j.get("idValue") or ""))
                return [{} for _ in jobs]

            inputs = {
                "111111119": {"issuer": "EXPIRED CO", "title": "COM"},
                "222222229": {"issuer": "FRESH CO", "title": "COM"},
            }
            with (
                mock.patch.object(security_resolver, "CUSIP_CACHE_PATH", cusip_cache_p),
                mock.patch.object(security_resolver, "TICKER_META_PATH", ticker_meta_p),
                mock.patch.object(security_resolver, "SEC_COMPANY_TICKERS_PATH", sec_tickers_p),
                mock.patch.object(security_resolver, "_openfigi_batch", side_effect=_fake_openfigi),
                mock.patch.object(security_resolver, "_yahoo_search", return_value=None),
            ):
                security_resolver.resolve_cusips(inputs, cusip_priority={"111111119": 1e9, "222222229": 1e9})

            # Expired fail (>14d) MUST be retried; fresh fail (<14d) MUST remain cached without OpenFIGI call
            self.assertIn("111111119", openfigi_seen)
            self.assertNotIn("222222229", openfigi_seen)

        # Dossier ticker quarantine
        aligned_stub = {"NVDA": (np.array([100.0]), 0, 0)}
        cm = {
            "C_BOGUS": {"ticker": "A001053020", "isResolved": False},
            "C_NVDA": {"ticker": "NVDA", "isResolved": True},
            "C_BRKB": {"ticker": "BRK-B", "isResolved": True},
        }
        self.assertFalse(build_data._is_valid_dossier_ticker("A001053020", "C_BOGUS", cm, aligned_stub))
        self.assertFalse(build_data._is_valid_dossier_ticker("CUSIP:123456789", "C_BOGUS", cm, aligned_stub))
        self.assertFalse(build_data._is_valid_dossier_ticker("098765", "C_BOGUS", cm, aligned_stub))
        self.assertTrue(build_data._is_valid_dossier_ticker("NVDA", "C_NVDA", cm, aligned_stub))
        self.assertTrue(build_data._is_valid_dossier_ticker("BRK-B", "C_BRKB", cm, aligned_stub))

    def test_emit_data_artifacts_and_strict_validator_circuit_breaker(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td)
            # Build minimal valid summary meeting all strict validator thresholds
            funds = [{"cik": f"000100{i:04d}", "name": f"Fund {i}", "elite50": True} for i in range(30)]
            updates = [{"cik": f"000100{i:04d}", "fundName": f"Fund {i}", "latestQuarter": "2026-09-30"} for i in range(20)]
            sub_sectors = ["Semiconductors", "Software—Application", "Cloud & Infrastructure", "Cybersecurity"]
            ideal_positions = [
                {
                    "ticker": f"STK{i}",
                    "idealWeightPct": 5.0,
                    "subSector": sub_sectors[i % 4],
                    "actionVerdict": "STRONG BUY",
                }
                for i in range(20)
            ]
            # Pad stocksIndex to 110 clean entries including AAPL, MSFT, NVDA anchors + make summary > 150KB
            stocks_index: dict[str, dict[str, object]] = {
                "AAPL": {"ticker": "AAPL", "name": "Apple Inc.", "priceUsd": 225.0, "marketCapUsd": 3.4e12},
                "MSFT": {"ticker": "MSFT", "name": "Microsoft Corp", "priceUsd": 420.0, "marketCapUsd": 3.1e12},
                "NVDA": {"ticker": "NVDA", "name": "NVIDIA Corp", "priceUsd": 140.0, "marketCapUsd": 3.4e12},
            }
            for i in range(110):
                tk = f"T{chr(65 + (i // 26))}{chr(65 + (i % 26))}X"
                stocks_index[tk] = {
                    "ticker": tk,
                    "name": f"Tech Corp {i} " + ("x" * 1600),
                    "priceUsd": 50.0 + i,
                    "marketCapUsd": 10e9,
                }
            summary_payload = {
                "metadata": {
                    "latestQuarterCovered": "2026-09-30",
                    "asOfDate": "2026-11-18",
                    "qualifiedFundsCount": 30,
                    "leaderboardEligibleCount": 30,
                    "elite50Count": 30,
                    "total13fFilingsProcessed": 500,
                    "universeFunnel": {
                        "totalCiksScanned": 9000,
                        "qualifiedFundsCount": 30,
                    },
                },
                "funds": funds,
                "fundUpdates": updates,
                "idealPortfolio": {"positions": ideal_positions},
                "stocksIndex": stocks_index,
                "signalScreener": {"stocks": [], "summary": {}},
                "midQuarterAlerts": [],
            }
            fund_shards = {f"{i:x}": {"funds": {}, "updates": {}} for i in range(16)}
            stock_shards: dict[int, dict[str, dict[str, object]]] = {i: {} for i in range(8)}
            for anchor, px, mc in (("AAPL", 225.0, 3.4e12), ("MSFT", 420.0, 3.1e12), ("NVDA", 140.0, 3.4e12)):
                stock_shards[0][anchor] = {
                    "ticker": anchor,
                    "priceUsd": px,
                    "marketCapUsd": mc,
                    "fundamentals": {"shareDilutionYoYPct": -2.1},
                }

            manifest, _ = build_data.emit_data_artifacts(
                out_dir,
                summary_payload=summary_payload,
                fund_shards=fund_shards,
                stock_shards=stock_shards,
            )
            for req in ("bootstrap.json", "screener.json", "updates_feed.json", "stocks_search.json", "summary.json", "manifest.json"):
                self.assertTrue((out_dir / req).exists(), f"Missing {req}")
            self.assertIn("bootstrap.json", manifest["files"])

            errors, warnings, stats = validate_dataset.validate_dataset(out_dir, strict=True)
            self.assertEqual(errors, [], f"Unexpected strict errors: {errors}")
            self.assertEqual(stats["idealExitSignalWeightPct"], 0.0)
            self.assertLessEqual(stats["idealTopSubSectorWeightPct"], 30.0)

            # Legitimate +55% dilution on a $75B issuer warns without failing strict deploy
            stock_shards[1]["UPCE"] = {
                "ticker": "UPCE",
                "priceUsd": 120.0,
                "marketCapUsd": 75e9,
                "fundamentals": {"shareDilutionYoYPct": 55.0},
            }
            build_data.emit_data_artifacts(
                out_dir,
                summary_payload=summary_payload,
                fund_shards=fund_shards,
                stock_shards=stock_shards,
            )
            pos_errors, pos_warnings, _ = validate_dataset.validate_dataset(out_dir, strict=True)
            self.assertEqual(pos_errors, [], f"Legitimate +55% dilution unexpectedly failed strict: {pos_errors}")
            self.assertTrue(any("UPCE(+55.0%)" in w for w in pos_warnings))

            # Tampering with bootstrap.json must trigger SHA-256 manifest mismatch failure
            (out_dir / "bootstrap.json").write_text("{}", encoding="utf-8")
            bad_errors, _, _ = validate_dataset.validate_dataset(out_dir, strict=True)
            self.assertTrue(any("Hash mismatch for bootstrap.json" in e for e in bad_errors))

            # Injecting a bogus unpriced ticker, 10% EXIT SIGNAL, and systemic -27% mega-cap swings must fail strict validator
            summary_payload["stocksIndex"]["A001053020"] = {"ticker": "A001053020", "priceUsd": None}
            summary_payload["idealPortfolio"]["positions"][0]["actionVerdict"] = "EXIT SIGNAL"
            summary_payload["idealPortfolio"]["positions"][1]["actionVerdict"] = "EXIT SIGNAL"
            stock_shards[2]["MEGA1"] = {
                "ticker": "MEGA1",
                "priceUsd": 200.0,
                "marketCapUsd": 140e9,
                "fundamentals": {"shareDilutionYoYPct": -27.5},
            }
            stock_shards[2]["MEGA2"] = {
                "ticker": "MEGA2",
                "priceUsd": 300.0,
                "marketCapUsd": 220e9,
                "fundamentals": {"shareDilutionYoYPct": -26.0},
            }
            build_data.emit_data_artifacts(
                out_dir,
                summary_payload=summary_payload,
                fund_shards=fund_shards,
                stock_shards=stock_shards,
            )
            bad_errors2, _, _ = validate_dataset.validate_dataset(out_dir, strict=True)
            self.assertTrue(any("unresolved/bogus unpriced ticker" in e for e in bad_errors2))
            self.assertTrue(any("EXIT SIGNAL" in e for e in bad_errors2))
            self.assertTrue(any("Corrupt/systemic YoY share-count swing" in e for e in bad_errors2))

    def test_asymmetric_downside_vol_fundamental_quality_override_and_risk_styles(self) -> None:
        n_days = 260
        # 1. Pure upside breakout (big green surges, almost no down days): never penalized by downside semi-deviation
        up_surge = [100.0]
        for i in range(1, n_days):
            r = 0.045 if (i % 5 == 0) else 0.002 if (i % 2 == 0) else -0.001
            up_surge.append(up_surge[-1] * (1.0 + r))
        pdata_surge = (np.array(up_surge, dtype=np.float64), 0, n_days - 1)
        m_surge = build_data._compute_pit_downside_vol_metrics(pdata_surge, n_days - 1)
        assert m_surge is not None
        self.assertLess(m_surge[0], 0.15)  # tiny downside vol
        self.assertGreater(m_surge[1], 0.30)  # big upside vol
        self.assertGreaterEqual(build_data._compute_pit_vol_mult(pdata_surge, n_days - 1, quality_override=0.0, risk_style="balanced"), 1.0)

        # 2. High-beta violent stock (symmetric +/-4.5% swings): elite compounder is shielded while weak stock is cut to ~0.65x
        high_beta = [100.0]
        for i in range(1, n_days):
            r = 0.045 if (i % 2 == 0) else -0.042
            high_beta.append(high_beta[-1] * (1.0 + r))
        pdata_hb = (np.array(high_beta, dtype=np.float64), 0, n_days - 1)
        vm_weak_bal = build_data._compute_pit_vol_mult(pdata_hb, n_days - 1, quality_override=0.0, risk_style="balanced")
        vm_elite_bal = build_data._compute_pit_vol_mult(pdata_hb, n_days - 1, quality_override=0.95, risk_style="balanced")
        vm_elite_hu = build_data._compute_pit_vol_mult(pdata_hb, n_days - 1, quality_override=0.95, risk_style="high_upside")
        self.assertLessEqual(vm_weak_bal, 0.72)
        self.assertGreaterEqual(vm_elite_bal, 0.93)
        self.assertGreater(vm_elite_hu, 1.0)
        self.assertLessEqual(vm_elite_hu, 1.15)

        # 3. Low-vol sleepy defensive gets boost in Balanced (>1.05) but NO boost in High-Upside (<=1.0)
        calm = [100.0 * (1.0004 ** i) * (1.003 if i % 2 == 0 else 0.997) for i in range(n_days)]
        pdata_calm = (np.array(calm, dtype=np.float64), 0, n_days - 1)
        self.assertGreater(build_data._compute_pit_vol_mult(pdata_calm, n_days - 1, quality_override=0.2, risk_style="balanced"), 1.10)
        self.assertLessEqual(build_data._compute_pit_vol_mult(pdata_calm, n_days - 1, quality_override=0.2, risk_style="high_upside"), 1.0)

        # 4. XBRL Fundamental Quality Profile + Rule-of-40 deep-burn anti-gaming guard + hyperscaler CapEx tolerance
        elite_prof = build_data._compute_fundamental_quality_profile(
            {"revenueGrowthYoYPct": 65.0, "operatingMarginPct": 44.0, "fcfMarginPct": 50.0, "shareDilutionYoYPct": -1.2},
            {"verdict": "STRONG ACCUMULATE"},
            buy_impulse=0.35,
        )
        self.assertGreaterEqual(elite_prof["qualityScore"], 85.0)
        self.assertGreaterEqual(elite_prof["qualityOverride"], 0.95)
        self.assertGreaterEqual(elite_prof["highUpsideMult"], 1.35)

        burn_prof = build_data._compute_fundamental_quality_profile(
            {"revenueGrowthYoYPct": 479.0, "operatingMarginPct": -115.5, "fcfMarginPct": -40.0, "shareDilutionYoYPct": 18.0},
            {"verdict": "HOLD"},
            buy_impulse=0.20,
        )
        self.assertEqual(burn_prof["qualityOverride"], 0.0)
        self.assertEqual(burn_prof["balancedFundamentalMult"], 0.90)
        self.assertEqual(burn_prof["highUpsideMult"], 0.78)

        hyperscaler_prof = build_data._compute_fundamental_quality_profile(
            {"revenueGrowthYoYPct": 15.0, "operatingMarginPct": 16.0, "fcfMarginPct": -5.5, "shareDilutionYoYPct": 0.4},
            {"verdict": "HOLD"},
            buy_impulse=0.05,
        )
        self.assertGreater(hyperscaler_prof["qualityScore"], 40.0)
        self.assertGreater(hyperscaler_prof["balancedFundamentalMult"], 0.95)

        dist_prof = build_data._compute_fundamental_quality_profile(
            {"revenueGrowthYoYPct": 65.0, "operatingMarginPct": 44.0, "fcfMarginPct": 50.0, "shareDilutionYoYPct": -1.2},
            {"verdict": "EXIT SIGNAL"},
            buy_impulse=-0.40,
        )
        self.assertEqual(dist_prof["qualityOverride"], 0.0)

        # 5. 200-DMA broken-trend guard: cuts weak stock, bypassed for fortress compounder held through market drawdown
        broken = [200.0] * 210 + [140.0] * 50
        pdata_broken = (np.array(broken, dtype=np.float64), 0, n_days - 1)
        rm_weak = build_data._compute_pit_price_regime_mult(pdata_broken, n_days - 1, None, buy_impulse=0.0, quality_override=0.0, risk_style="balanced")
        rm_fortress = build_data._compute_pit_price_regime_mult(pdata_broken, n_days - 1, None, buy_impulse=0.0, quality_override=0.85, risk_style="balanced")
        self.assertLess(rm_weak, 0.90)
        self.assertEqual(rm_fortress, 1.0)

        # 6. compute_ideal_portfolio_weights dual-style scoring & EXIT SIGNAL 0% enforcement in both modes
        cal_dates = [f"2025-{(i // 22) + 1:02d}-{(i % 22) + 1:02d}" for i in range(n_days)]
        win_indices = {"TTM": 5, "3Y": 0, "5Y": 0}
        cusip_meta = {
            "C_ELIT": {"ticker": "ELIT", "name": "Elite AI Corp", "sector": "Technology", "industry": "Software—Infrastructure", "isTechTmt": True},
            "C_DEFV": {"ticker": "DEFV", "name": "Sleepy Bank", "sector": "Financial Services", "industry": "Banks—Diversified", "isTechTmt": False},
            "C_EXIT": {"ticker": "EXIT", "name": "Exiting Corp", "sector": "Technology", "industry": "Semiconductors", "isTechTmt": True},
        }
        funds = [
            {
                "cik": f"000000000{k}",
                "name": f"Elite Fund {k}",
                "metrics": {"filing_lag": {"skillScore": 85.0}},
                "holdings": [
                    {"cusip": "C_ELIT", "ticker": "ELIT", "name": "Elite AI Corp", "sector": "Technology", "industry": "Software—Infrastructure", "isTechTmt": True, "securityType": "EQUITY", "weightPct": 6.0, "valueUsd": 60e6, "status": "ADDED"},
                    {"cusip": "C_DEFV", "ticker": "DEFV", "name": "Sleepy Bank", "sector": "Financial Services", "industry": "Banks—Diversified", "isTechTmt": False, "securityType": "EQUITY", "weightPct": 6.0, "valueUsd": 60e6, "status": "UNCHANGED"},
                    {"cusip": "C_EXIT", "ticker": "EXIT", "name": "Exiting Corp", "sector": "Technology", "industry": "Semiconductors", "isTechTmt": True, "securityType": "EQUITY", "weightPct": 7.0, "valueUsd": 70e6, "status": "TRIMMED"},
                ],
            }
            for k in (1, 2, 3)
        ]
        navs = {f["cik"]: np.linspace(1.0, 1.6, n_days) for f in funds}
        aligned = {"ELIT": pdata_hb, "DEFV": pdata_calm, "EXIT": pdata_calm}
        fundamentals = {
            "ELIT": {"revenueGrowthYoYPct": 72.0, "operatingMarginPct": 40.0, "fcfMarginPct": 46.0, "shareDilutionYoYPct": 0.5},
            "DEFV": {"revenueGrowthYoYPct": 3.0, "operatingMarginPct": 22.0, "fcfMarginPct": 18.0, "shareDilutionYoYPct": 0.0},
            "EXIT": {"revenueGrowthYoYPct": 10.0, "operatingMarginPct": 15.0, "fcfMarginPct": 12.0, "shareDilutionYoYPct": 1.0},
        }
        actionability = {
            "ELIT": {"verdict": "STRONG ACCUMULATE"},
            "DEFV": {"verdict": "HOLD"},
            "EXIT": {"verdict": "EXIT SIGNAL"},
        }
        w_bal, agg_bal, _ = build_data.compute_ideal_portfolio_weights(
            funds,
            mode="filing_lag",
            cusip_meta=cusip_meta,
            fund_navs_mode=navs,
            cal_dates=cal_dates,
            win_indices=win_indices,
            max_names=10,
            cap_pct=80.0,
            aligned_prices=aligned,
            n_dates=n_days,
            actionability_by_ticker=actionability,
            subsector_cap_pct=80.0,
            fundamentals_by_ticker=fundamentals,
            risk_style="balanced",
        )
        w_hu, agg_hu, _ = build_data.compute_ideal_portfolio_weights(
            funds,
            mode="filing_lag",
            cusip_meta=cusip_meta,
            fund_navs_mode=navs,
            cal_dates=cal_dates,
            win_indices=win_indices,
            max_names=10,
            cap_pct=80.0,
            aligned_prices=aligned,
            n_dates=n_days,
            actionability_by_ticker=actionability,
            subsector_cap_pct=80.0,
            fundamentals_by_ticker=fundamentals,
            risk_style="high_upside",
        )
        self.assertEqual(w_bal.get("EXIT", 0.0), 0.0)
        self.assertEqual(w_hu.get("EXIT", 0.0), 0.0)
        self.assertTrue(agg_bal["ELIT"]["qualityOverrideActive"])
        self.assertGreater(agg_bal["ELIT"]["highUpsideScore"], agg_bal["ELIT"]["rawScore"])
        self.assertGreater(w_hu["ELIT"] - w_hu["DEFV"], w_bal["ELIT"] - w_bal["DEFV"])
        self.assertEqual(agg_hu["ELIT"]["fundamentalQualityScore"], agg_bal["ELIT"]["fundamentalQualityScore"])

    def test_run_ideal_portfolio_backtest_high_upside_pit_style(self) -> None:
        cal_dates = [
            (date(2021, 1, 4) + timedelta(days=i)).strftime("%Y-%m-%d")
            for i in range(600)
            if (date(2021, 1, 4) + timedelta(days=i)).weekday() < 5
        ]
        n = len(cal_dates)
        # TECHU: strong upside convexity; DEFSL: flat low-vol non-tech; plus 14 peers so weights stay below 8% cap
        tech_px = [100.0]
        def_px = [100.0]
        for i in range(1, n):
            tech_px.append(tech_px[-1] * (1.032 if i % 4 == 0 else 0.996))
            def_px.append(def_px[-1] * (1.002 if i % 2 == 0 else 0.999))
        aligned: dict[str, tuple[np.ndarray, int, int]] = {
            "TECHU": (np.array(tech_px, dtype=np.float64), 0, n - 1),
            "DEFSL": (np.array(def_px, dtype=np.float64), 0, n - 1),
        }
        ticker_is_tech: dict[str, bool] = {"TECHU": True, "DEFSL": False}
        subsectors = ["Semiconductors", "Enterprise Software", "Cloud & Infrastructure", "Cybersecurity", "Consumer Internet"]
        ticker_sub_sector: dict[str, str] = {"TECHU": "Enterprise Software", "DEFSL": "Financial Services"}
        base_w: dict[str, float] = {"TECHU": 0.06, "DEFSL": 0.08}
        base_sh: dict[str, float] = {"TECHU": 100.0, "DEFSL": 100.0}
        curr_sh: dict[str, float] = {"TECHU": 135.0, "DEFSL": 100.0}
        for j in range(14):
            tk = f"PEER{j:02d}"
            aligned[tk] = (np.linspace(50.0 + j, 80.0 + j, n), 0, n - 1)
            ticker_is_tech[tk] = (j % 3 != 0)
            ticker_sub_sector[tk] = subsectors[j % len(subsectors)] if ticker_is_tech[tk] else "Healthcare"
            base_w[tk] = 0.06
            base_sh[tk] = 100.0
            curr_sh[tk] = 100.0

        qqq_nav = np.linspace(1.0, 1.25, n)
        ciks = [f"000100000{i}" for i in range(6)]
        snaps_by_cik: dict[str, list[dict[str, object]]] = {}
        navs_by_cik: dict[str, np.ndarray] = {}
        for idx_c, cik in enumerate(ciks):
            navs_by_cik[cik] = np.linspace(1.0, 1.35 + idx_c * 0.05, n)
            snaps_by_cik[cik] = [
                {
                    "reportDate": "2021-06-30",
                    "effDate": "2021-06-30",
                    "eqAum": 500_000_000.0,
                    "techPct": 75.0,
                    "posCount": len(base_w),
                    "effN": 12.0,
                    "maxSingle": 10.0,
                    "weights": dict(base_w),
                    "shares": dict(base_sh),
                },
                {
                    "reportDate": "2021-09-30",
                    "effDate": "2021-09-30",
                    "eqAum": 550_000_000.0,
                    "techPct": 75.0,
                    "posCount": len(base_w),
                    "effN": 12.0,
                    "maxSingle": 10.0,
                    "weights": dict(base_w),
                    "shares": dict(curr_sh),
                },
            ]
        segs_bal = build_data.run_ideal_portfolio_backtest(
            bt_mode="quarter_end",
            all_quarters=["2021-06-30", "2021-09-30"],
            all_bt_ciks=ciks,
            snaps_by_cik=snaps_by_cik,
            navs_by_cik=navs_by_cik,
            cal_dates=cal_dates,
            as_of_date=cal_dates[-1],
            aligned_prices=aligned,
            ticker_is_tech=ticker_is_tech,
            curated_bt_ciks=set(),
            qqq_nav=qqq_nav,
            ticker_sub_sector=ticker_sub_sector,
            subsector_cap=0.50,
            risk_style="balanced",
        )
        segs_hu = build_data.run_ideal_portfolio_backtest(
            bt_mode="quarter_end",
            all_quarters=["2021-06-30", "2021-09-30"],
            all_bt_ciks=ciks,
            snaps_by_cik=snaps_by_cik,
            navs_by_cik=navs_by_cik,
            cal_dates=cal_dates,
            as_of_date=cal_dates[-1],
            aligned_prices=aligned,
            ticker_is_tech=ticker_is_tech,
            curated_bt_ciks=set(),
            qqq_nav=qqq_nav,
            ticker_sub_sector=ticker_sub_sector,
            subsector_cap=0.50,
            risk_style="high_upside",
        )
        self.assertEqual(len(segs_bal), 1)
        self.assertEqual(len(segs_hu), 1)
        w_bal = segs_bal[0][1]
        w_hu = segs_hu[0][1]
        self.assertAlmostEqual(sum(w_hu.values()), 1.0, places=4)
        self.assertGreater(w_hu["TECHU"] / w_hu["DEFSL"], w_bal["TECHU"] / w_bal["DEFSL"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
