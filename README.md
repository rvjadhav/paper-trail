# Paper Trail

Institutional-grade SEC Form `13F-HR`, `13F-HR/A`, `13D`/`13G`, and XBRL intelligence platform for **concentrated, technology-focused investment managers** — engineered to turn delayed public filings into disciplined buy/sell signals and custom tech portfolios.

## Key Features

- **Skill-Ranked Cohort & Anti-SPV Tiering (`Ranking`):**
  - Scans **100% of SEC Form 13F institutional managers** (~11,400+ CIKs) directly from SEC Bulk Data Sets + post-bulk `master.idx` submissions.
  - Separates ultra-concentrated single-stock SPVs (`Excluded SPVs`) from diversified managers, ranking eligible funds by **Composite Skill Score (0–100)**: `40% Information Ratio vs. QQQ` + `30% Batting Average` + `30% Sortino Ratio` (using trailing 13-week T-Bill yields via `^IRX`).
  - Defaults to the **45-Day Filing-Lag** lens (respecting `13F-HR/A` confidential-treatment amendments without look-ahead bias) with a toggle to Quarter-End attribution, plus **Elite 50**, **Top 100**, **All Eligible**, and **Excluded SPVs** cohort views.
- **13F Signal Screener & Top Consensus (`Holdings`):**
  - Five dedicated screens: **Top 30 Consensus**, **Cluster Initiations**, **High-Conviction Adds**, **Smart-Money Exits**, and **Contrarian Specialist Bets**.
  - Filter across **Market-Cap Tiers** (`Mega >$200B`, `Large $30B-$200B`, `Emerging <$30B`) and **Quarter-VWAP Entry Zones** (`Discount / Near Q-Avg`, `Extended`, `Chasing +30%+`).
- **Stock Dossier Drawer (Click any Stock Anywhere):**
  - **Est. Quarter VWAP Cost-Basis Gauge:** Compares current market price against the prior filing quarter's volume-weighted close, low, and high to distinguish discounted entries from chased breakouts.
  - **SEC EDGAR XBRL Fundamentals:** TTM Revenue, YoY Revenue Growth, Operating Margin, FCF Margin, R&D Intensity, Share Dilution, and Market Cap Tier directly from SEC `companyfacts`.
  - **Complete Institutional Holder & Flow Ledger:** Filter holders between All vs. Elite 50, inspect recent QoQ moves (including full exits), and jump to mid-quarter `13D`/`13G` stakes.
- **Interactive Portfolio Builder & Rebalance Calculator (`Ideal Portfolio`):**
  - Dynamically constructs a 100%-invested portfolio from the Elite-50 Top-80 candidate pool with user-selectable **Market-Cap Universe**, **Core Compounder vs. Fresh Inflection** signal emphasis, **Portfolio Size** (`20 / 30 / 40`), **Single-Stock Cap** (`5% / 8% / 10%`), and **Sub-Sector Cap** (`No Cap / 35% / 25%`), persisted via localStorage presets.
  - Built-in **Portfolio Rebalance Calculator** converts your pasted holdings (`$` or `%`) into exact `BUY / ADD / TRIM / EXIT / HOLD` share trades and CSV export.
  - Survivorship-free out-of-sample quarterly backtest vs. `QQQ` and `SPY`.
- **Quarterly Updates & Live 13D/13G Alerts (`Updates`):**
  - Tracks live mid-quarter **Schedule 13D / 13G** activist and ≥5% filings across Elite-50 managers alongside full `13F-HR` & `13F-HR/A` quarter-over-quarter diffs, option hedges, common-to-call rotations, and flat-share valuation markdowns.

## Sharded Data Architecture & Automated Cron

- `pipeline/`: Python SEC EDGAR pipeline ingesting Bulk Form 13F ZIPs, post-bulk `master.idx` `.txt` submissions, `entityType`/SIC metadata, XBRL CompanyFacts, Schedule 13D/13G submissions, OpenFIGI security mappings, and Yahoo Finance adjusted OHLCV + `^IRX` yields.
- `public/data/`: Emits lazy-loaded static JSON files to keep initial page load fast:
  - `summary.json` (lightweight metadata, fund/update summaries, signal screener, ideal portfolio, and searchable stock index)
  - `funds_{0..f}.json` (16 hex shards holding full fund equity curves, complete 13F tables, quarterly snapshots, and full QoQ diffs)
  - `stocks_{0..7}.json` (8 CRC shards holding per-stock OHLCV cost-basis metrics, XBRL fundamentals, holders, and flow history)
- `.github/workflows/quarterly-13f-update.yml`:
  - **Peak 45-Day Deadlines (Feb/May/Aug/Nov 11–19):** Runs at `23:15 UTC` (post SEC 5:30 PM ET close) and `05:15 UTC` (catch-up).
  - **Early / Late Season Checkpoints:** Runs at `05:15 UTC` on days `1, 5, 8, 10, 21, 25, 28` of filing months.
  - **Weekly Full Refresh:** Runs every Saturday at `06:15 UTC` for amendments, mid-quarter `13D`/`13G` alerts, XBRL updates, and market prices.

## Local Development

```bash
# 1. Refresh data from SEC EDGAR, OpenFIGI & Yahoo Finance
pip install -r pipeline/requirements.txt
python3 -u pipeline/build_data.py

# 2. Lint, typecheck & run frontend locally
npm install
npm run lint
npm run build
npm run dev
```
