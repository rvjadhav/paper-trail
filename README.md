# TechWhale 13F

Institutional-grade quarterly SEC Form 13F tracker for **concentrated, technology-focused investment funds**.

## Features

- **Ranking:** Ranks the top 10 concentrated tech/TMT funds across six performance horizons (`YTD`, `TTM`, `18M`, `2Y`, `3Y`, `5Y`) with a toggle between **Quarter-End Attribution** and **45-Day Filing-Lag Actionable** returns. Clicking any fund opens its full strategy overview, multi-year equity curve vs. `QQQ`/`SPY`, risk metrics, quarterly filing snapshots, and complete 13F holdings table.
- **Holdings (Top 30):** Aggregates the top 30 consensus stocks held across all ranked funds across all windows, supporting both **Equal-Weight Average %** and **AUM-Weighted %** views, along with expandable fund-by-fund weight breakdowns and QoQ share deltas.
- **Ideal Portfolio (≤50 Names):** Constructs a risk-capped (8% default ceiling) portfolio of up to 50 names from all ranked funds using multi-window performance rank, conviction weight, consensus ownership, and QoQ accumulation signals—complete with an out-of-sample quarterly backtest and interactive capital allocator.
- **ETF Lookalikes & Correlations:** Ingests official SEC Form `NPORT-P` holdings across major Tech, AI, Semiconductor, Cloud, Growth, and Market ETFs and computes holdings cosine similarity %, weight overlap %, and trailing 1Y daily return correlation % against every tracked fund.

## Architecture & Automated Quarterly Updates

- `pipeline/`: Python 3.11 SEC EDGAR (`13F-HR`, `13F-HR/A`, `NPORT-P`, `EFTS` discovery) + OpenFIGI + Yahoo Finance daily split/dividend-adjusted price pipeline generating `src/data/portfolio_data.json`.
- `.github/workflows/quarterly-13f-update.yml`: Scheduled GitHub Actions workflow running automatically during every quarterly 13F filing season (Feb, May, Aug, Nov) and weekly on Mondays to ingest new filings, update market prices, commit refreshed data, and redeploy to GitHub Pages.

## Local Development

```bash
# 1. Refresh data from SEC EDGAR & Yahoo Finance
pip install -r pipeline/requirements.txt
python3 -u pipeline/build_data.py

# 2. Run frontend locally
npm install
npm run dev
```
