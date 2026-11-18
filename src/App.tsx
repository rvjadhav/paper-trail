import { useCallback, useEffect, useMemo, useState, type MouseEvent } from 'react';
import { Activity, Calendar, Database, Info, Layers, PieChart, Trophy, X } from 'lucide-react';
import type { PaperTrailSummaryDataset, ReturnMode, ReturnWindow } from './types/data';
import { fetchSummary } from './utils/dataLoader';
import { formatUsdCompact } from './utils/format';
import { RankingTab } from './components/RankingTab';
import { HoldingsTab } from './components/HoldingsTab';
import { IdealPortfolioTab } from './components/IdealPortfolioTab';
import { UpdatesTab } from './components/UpdatesTab';
import { FundDetailDrawer } from './components/FundDetailDrawer';
import { UpdateDetailDrawer } from './components/UpdateDetailDrawer';
import { StockDetailDrawer } from './components/StockDetailDrawer';

type PrimaryTab = 'ranking' | 'holdings' | 'ideal' | 'updates';

function parseHashState(): { tab: PrimaryTab; cik: string | null; stock: string | null } {
  const h = window.location.hash.replace(/^#/, '');
  const [tabPart, subPart] = h.split('/');
  const validTabs: PrimaryTab[] = ['ranking', 'holdings', 'ideal', 'updates'];
  if (tabPart === 'stock' && subPart && /^[A-Za-z0-9.-]{1,12}$/.test(subPart)) {
    return { tab: 'ranking', cik: null, stock: subPart.toUpperCase() };
  }
  const tab = validTabs.includes(tabPart as PrimaryTab) ? (tabPart as PrimaryTab) : 'ranking';
  const cik = subPart && /^\d{10}$/.test(subPart) ? subPart : null;
  const stock =
    !cik && subPart && /^[A-Za-z0-9.-]{1,12}$/.test(subPart) ? subPart.toUpperCase() : null;
  return { tab, cik, stock };
}

export function App() {
  const [dataset, setDataset] = useState<PaperTrailSummaryDataset | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const initial = useMemo(() => parseHashState(), []);
  const [activeTab, setActiveTabState] = useState<PrimaryTab>(initial.tab);
  const [windowSize, setWindowSize] = useState<ReturnWindow>('TTM');
  const [mode, setMode] = useState<ReturnMode>('filing_lag');
  const [selectedFundCik, setSelectedFundCikState] = useState<string | null>(initial.cik);
  const [selectedUpdateCik, setSelectedUpdateCik] = useState<string | null>(null);
  const [selectedStockTicker, setSelectedStockTickerState] = useState<string | null>(initial.stock);
  const [updateReturnToFund, setUpdateReturnToFund] = useState(false);
  const [showMethodology, setShowMethodology] = useState(false);
  const [homeResetKey, setHomeResetKey] = useState(0);

  useEffect(() => {
    let cancelled = false;
    fetchSummary()
      .then((data) => {
        if (!cancelled) setDataset(data);
      })
      .catch((err) => {
        if (!cancelled) setLoadError(err instanceof Error ? err.message : 'Failed to load summary dataset');
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const syncHash = useCallback((tab: PrimaryTab, cik: string | null, stock: string | null) => {
    const nextHash = stock && !cik ? `#stock/${stock}` : cik ? `#${tab}/${cik}` : `#${tab}`;
    if (window.location.hash !== nextHash) {
      window.history.replaceState(null, '', nextHash);
    }
  }, []);

  const setActiveTab = useCallback(
    (tab: PrimaryTab) => {
      setActiveTabState(tab);
      setSelectedUpdateCik(null);
      setSelectedStockTickerState(null);
      setUpdateReturnToFund(false);
      syncHash(tab, selectedFundCik, null);
    },
    [selectedFundCik, syncHash]
  );

  const setSelectedFundCik = useCallback(
    (cik: string | null) => {
      setSelectedUpdateCik(null);
      setSelectedStockTickerState(null);
      setUpdateReturnToFund(false);
      setSelectedFundCikState(cik);
      syncHash(activeTab, cik, null);
    },
    [activeTab, syncHash]
  );

  const setSelectedStockTicker = useCallback(
    (ticker: string | null) => {
      const norm = ticker ? ticker.toUpperCase() : null;
      setSelectedStockTickerState(norm);
      if (!selectedFundCik && !selectedUpdateCik) {
        syncHash(activeTab, null, norm);
      }
    },
    [activeTab, selectedFundCik, selectedUpdateCik, syncHash]
  );

  const handleSelectStock = useCallback(
    (ticker: string) => {
      setSelectedStockTicker(ticker);
    },
    [setSelectedStockTicker]
  );

  const handleDiveDeeperFromFund = useCallback(
    (cik: string) => {
      setSelectedFundCikState(null);
      setSelectedStockTickerState(null);
      syncHash(activeTab, null, null);
      setUpdateReturnToFund(true);
      setSelectedUpdateCik(cik);
    },
    [activeTab, syncHash]
  );

  const handleDiveDeeperFromUpdates = useCallback((cik: string) => {
    setUpdateReturnToFund(false);
    setSelectedStockTickerState(null);
    setSelectedUpdateCik(cik);
  }, []);

  const goHome = useCallback((e?: MouseEvent<HTMLAnchorElement>) => {
    if (e && (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button !== 0)) {
      return;
    }
    e?.preventDefault();
    setSelectedFundCikState(null);
    setSelectedUpdateCik(null);
    setSelectedStockTickerState(null);
    setUpdateReturnToFund(false);
    setShowMethodology(false);
    setActiveTabState('ranking');
    setWindowSize('TTM');
    setMode('filing_lag');
    setHomeResetKey((k) => k + 1);
    if (window.location.hash) {
      window.history.replaceState(null, '', `${window.location.pathname}${window.location.search}`);
    }
    window.scrollTo({ top: 0, behavior: 'smooth' });
  }, []);

  useEffect(() => {
    const onHashChange = () => {
      const parsed = parseHashState();
      setActiveTabState(parsed.tab);
      setSelectedFundCikState(parsed.cik);
      setSelectedStockTickerState(parsed.stock);
      setSelectedUpdateCik(null);
      setUpdateReturnToFund(false);
    };
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        if (selectedStockTicker) {
          setSelectedStockTicker(null);
        } else if (selectedUpdateCik) {
          setSelectedUpdateCik(null);
          setUpdateReturnToFund(false);
        } else if (selectedFundCik) {
          setSelectedFundCik(null);
        } else if (showMethodology) {
          setShowMethodology(false);
        }
      }
    };
    window.addEventListener('hashchange', onHashChange);
    window.addEventListener('keydown', onKeyDown);
    return () => {
      window.removeEventListener('hashchange', onHashChange);
      window.removeEventListener('keydown', onKeyDown);
    };
  }, [
    selectedStockTicker,
    selectedFundCik,
    selectedUpdateCik,
    showMethodology,
    setSelectedStockTicker,
    setSelectedFundCik,
  ]);

  const selectedFund = useMemo(
    () => dataset?.funds.find((f) => f.cik === selectedFundCik) ?? null,
    [dataset, selectedFundCik]
  );

  const selectedFundUpdate = useMemo(
    () =>
      selectedFundCik && dataset
        ? dataset.fundUpdates.find((u) => u.cik === selectedFundCik) ?? null
        : null,
    [dataset, selectedFundCik]
  );

  const selectedUpdate = useMemo(
    () =>
      selectedUpdateCik && dataset
        ? dataset.fundUpdates.find((u) => u.cik === selectedUpdateCik) ?? null
        : null,
    [dataset, selectedUpdateCik]
  );

  const searchableTickersByCik = useMemo(
    () => new Map(dataset?.funds.map((f) => [f.cik, f.searchableTickers]) ?? []),
    [dataset]
  );

  const totalTrackedAum = useMemo(
    () => (dataset ? dataset.funds.reduce((s, f) => s + f.equityAumUsd, 0) : 0),
    [dataset]
  );

  const stocksIndexedCount = useMemo(
    () => (dataset ? Object.keys(dataset.stocksIndex).length : 0),
    [dataset]
  );

  const generatedFormatted = useMemo(() => {
    if (!dataset) return 'Loading...';
    try {
      return new Date(dataset.metadata.generatedAt).toLocaleDateString('en-US', {
        month: 'short',
        day: 'numeric',
        year: 'numeric',
      });
    } catch {
      return dataset.metadata.asOfDate;
    }
  }, [dataset]);

  if (loadError) {
    return (
      <div className="app-shell">
        <main className="main-content" style={{ padding: '3rem 1.25rem', textAlign: 'center' }}>
          <h2>Unable to load 13F portfolio dataset</h2>
          <p style={{ color: 'var(--text-secondary)' }}>{loadError}</p>
        </main>
      </div>
    );
  }

  if (!dataset) {
    return (
      <div className="app-shell">
        <header className="site-header">
          <div className="header-inner">
            <div className="header-top">
              <div className="brand-block">
                <h1>
                  <span className="brand-home-link">Paper Trail</span>
                </h1>
                <p className="brand-subtitle">
                  Loading SEC Form 13F filings &amp; multi-year performance intelligence...
                </p>
              </div>
            </div>
          </div>
        </header>
        <main className="main-content" style={{ padding: '2.5rem 1.25rem', color: 'var(--text-secondary)' }}>
          <div className="panel" style={{ padding: '2rem', textAlign: 'center' }}>
            Loading institutional 13F intelligence summary...
          </div>
        </main>
      </div>
    );
  }

  const funnel = dataset.metadata.universeFunnel;

  return (
    <div className="app-shell">
      {/* Live SEC EDGAR Pipeline Status Bar */}
      <div className="status-strip">
        <div className="status-strip-inner">
          <div className="status-items">
            <span className="status-item">
              <span className="status-dot" aria-hidden="true" />
              <span>SEC EDGAR Sync:</span>
              <span className="status-strong">{generatedFormatted}</span>
            </span>
            <span className="status-item">
              <span>Latest Quarter:</span>
              <span className="status-strong">{dataset.metadata.latestQuarterCovered}</span>
            </span>
            <span className="status-item">
              <span>Market Prices As Of:</span>
              <span className="status-strong">{dataset.metadata.asOfDate}</span>
            </span>
            <span className="status-item">
              <span>13W T-Bill (^IRX):</span>
              <span className="status-strong">{dataset.metadata.riskFreeLatestPct.toFixed(2)}%</span>
            </span>
            <span className="status-item">
              <Calendar size={12} />
              <span>Next 13F Season:</span>
              <span className="status-strong">{dataset.metadata.next13fSeasonLabel}</span>
            </span>
          </div>

          <button
            type="button"
            className="status-link-btn"
            onClick={() => setShowMethodology(true)}
          >
            <Info size={12} /> Methodology &amp; Universe Funnel
            {funnel && (
              <span className="funnel-pill" title="Full-universe SEC Form 13F Bulk Data Sets screening funnel">
                <Database size={10} />
                {funnel.totalCiksScanned.toLocaleString()} CIKs → {funnel.elite50Count} Elite / {funnel.qualifiedFundsCount} Funds
              </span>
            )}
          </button>
        </div>
      </div>

      {/* Editorial Masthead & Tab Navigation */}
      <header className="site-header">
        <div className="header-inner">
          <div className="header-top">
            <div className="brand-block">
              <h1>
                <a
                  id="brand-home-link"
                  href="./"
                  className="brand-home-link"
                  onClick={goHome}
                  aria-label="Paper Trail home"
                >
                  Paper Trail
                </a>
              </h1>
              <p className="brand-subtitle">
                Following the SEC Form 13F &amp; 13D/13G paper trail across elite technology managers to build high-conviction portfolios.
              </p>
            </div>

            <div className="header-kpis">
              <div className="header-kpi">
                <div className="header-kpi-label">Elite 50 / Eligible</div>
                <div className="header-kpi-val">
                  {dataset.metadata.elite50Count} / {dataset.metadata.leaderboardEligibleCount}
                </div>
              </div>
              <div className="header-kpi">
                <div className="header-kpi-label">Tracked 13F Capital</div>
                <div className="header-kpi-val">{formatUsdCompact(totalTrackedAum)}</div>
              </div>
              <div className="header-kpi">
                <div className="header-kpi-label">Stocks Indexed</div>
                <div className="header-kpi-val">{stocksIndexedCount.toLocaleString()}</div>
              </div>
              <div className="header-kpi">
                <div className="header-kpi-label">Filings Analyzed</div>
                <div className="header-kpi-val">{dataset.metadata.total13fFilingsProcessed.toLocaleString()}</div>
              </div>
            </div>
          </div>

          <nav className="primary-nav" aria-label="Primary sections">
            <button
              type="button"
              id="tab-ranking"
              className={`nav-tab ${activeTab === 'ranking' ? 'active' : ''}`}
              onClick={() => setActiveTab('ranking')}
            >
              <Trophy size={16} />
              <span>Ranking</span>
              <span className="nav-count">{dataset.metadata.elite50Count} Elite / 100</span>
            </button>

            <button
              type="button"
              id="tab-holdings"
              className={`nav-tab ${activeTab === 'holdings' ? 'active' : ''}`}
              onClick={() => setActiveTab('holdings')}
            >
              <Layers size={16} />
              <span>Holdings</span>
              <span className="nav-count">Screener</span>
            </button>

            <button
              type="button"
              id="tab-ideal"
              className={`nav-tab ${activeTab === 'ideal' ? 'active' : ''}`}
              onClick={() => setActiveTab('ideal')}
            >
              <PieChart size={16} />
              <span>Ideal Portfolio</span>
              <span className="nav-count">{dataset.idealPortfolio.positionCount}</span>
            </button>

            <button
              type="button"
              id="tab-updates"
              className={`nav-tab ${activeTab === 'updates' ? 'active' : ''}`}
              onClick={() => setActiveTab('updates')}
            >
              <Activity size={16} />
              <span>Updates</span>
              <span className="nav-count">{dataset.fundUpdates.length}</span>
            </button>
          </nav>
        </div>
      </header>

      {/* Active Tab Content */}
      <main className="main-content">
        {activeTab === 'ranking' && (
          <RankingTab
            key={homeResetKey}
            funds={dataset.funds}
            rankings={dataset.rankings}
            benchmarks={dataset.benchmarks}
            stocksIndex={dataset.stocksIndex}
            windowSize={windowSize}
            onWindowChange={setWindowSize}
            mode={mode}
            onModeChange={setMode}
            onSelectFund={setSelectedFundCik}
            onSelectStock={handleSelectStock}
          />
        )}

        {activeTab === 'holdings' && (
          <HoldingsTab
            topHoldings={dataset.topHoldings}
            signalScreener={dataset.signalScreener}
            stocksIndex={dataset.stocksIndex}
            mode={mode}
            onModeChange={setMode}
            onSelectFund={setSelectedFundCik}
            onSelectStock={handleSelectStock}
          />
        )}

        {activeTab === 'ideal' && (
          <IdealPortfolioTab
            idealPortfolio={dataset.idealPortfolio}
            benchmarks={dataset.benchmarks}
            stocksIndex={dataset.stocksIndex}
            mode={mode}
            onModeChange={setMode}
            onSelectFund={setSelectedFundCik}
            onSelectStock={handleSelectStock}
          />
        )}

        {activeTab === 'updates' && (
          <UpdatesTab
            updates={dataset.fundUpdates}
            overview={dataset.updatesOverview}
            midQuarterAlerts={dataset.midQuarterAlerts}
            stocksIndex={dataset.stocksIndex}
            searchableTickersByCik={searchableTickersByCik}
            onSelectFund={setSelectedFundCik}
            onDiveDeeper={handleDiveDeeperFromUpdates}
            onSelectStock={handleSelectStock}
          />
        )}
      </main>

      {/* Footer */}
      <footer className="site-footer">
        <div className="footer-inner">
          <div>
            <strong>Paper Trail</strong> · Automated SEC EDGAR Form 13F-HR / 13D / 13G &amp; XBRL analytics pipeline.
          </div>
          <div>
            13F filings reflect long U.S. equity, ADR, and disclosed option positions filed with the SEC and exclude cash, shorts, and private stakes unless reported. Not investment advice.
          </div>
        </div>
      </footer>

      {/* Fund Detail Drawer */}
      {selectedFund && (
        <FundDetailDrawer
          key={selectedFund.cik}
          fund={selectedFund}
          fundUpdate={selectedFundUpdate}
          mode={mode}
          benchmarks={dataset.benchmarks}
          stocksIndex={dataset.stocksIndex}
          onClose={() => setSelectedFundCik(null)}
          onDiveDeeper={handleDiveDeeperFromFund}
          onSelectStock={handleSelectStock}
        />
      )}

      {/* Dive Deeper Filing Diff Drawer */}
      {selectedUpdate && (
        <UpdateDetailDrawer
          key={selectedUpdate.cik}
          update={selectedUpdate}
          stocksIndex={dataset.stocksIndex}
          fromFundDrawer={updateReturnToFund}
          onClose={() => {
            setSelectedUpdateCik(null);
            setUpdateReturnToFund(false);
          }}
          onOpenFundProfile={(cik) => {
            setSelectedFundCik(cik);
          }}
          onSelectStock={handleSelectStock}
        />
      )}

      {/* Stock Dossier Drawer (stacks above Fund/Update drawers when opened inside them) */}
      {selectedStockTicker && (
        <StockDetailDrawer
          key={selectedStockTicker}
          ticker={selectedStockTicker}
          stocksIndex={dataset.stocksIndex}
          latestQuarterLabel={dataset.updatesOverview.latestQuarterLabel}
          onClose={() => setSelectedStockTicker(null)}
          onSelectFund={(cik) => {
            setSelectedStockTickerState(null);
            setSelectedFundCik(cik);
          }}
          onJumpToIdeal={() => {
            setSelectedStockTickerState(null);
            setActiveTab('ideal');
          }}
        />
      )}

      {/* Methodology & Automated Schedule Modal */}
      {showMethodology && (
        <div
          className="drawer-backdrop"
          role="dialog"
          aria-modal="true"
          aria-labelledby="meth-title"
          onClick={(e) => {
            if (e.target === e.currentTarget) setShowMethodology(false);
          }}
        >
          <div className="drawer-panel" style={{ width: 'min(760px, 100vw)' }}>
            <div className="drawer-header">
              <div>
                <h2 id="meth-title" style={{ margin: 0, fontSize: '1.25rem' }}>
                  Data Methodology &amp; Automated SEC Ingestion
                </h2>
                <p style={{ margin: '0.25rem 0 0', fontSize: '0.82rem', color: 'var(--text-secondary)' }}>
                  How Paper Trail screens 100% of 13F managers, scores skill, enriches stocks, and avoids look-ahead &amp; survivorship bias
                </p>
              </div>
              <button type="button" className="close-btn" onClick={() => setShowMethodology(false)} aria-label="Close methodology">
                <X size={18} />
              </button>
            </div>
            <div className="drawer-body" style={{ fontSize: '0.86rem', color: 'var(--text-secondary)', lineHeight: 1.65 }}>
              <h3 style={{ color: 'var(--text-primary)', marginTop: 0 }}>1. Full-Universe SEC 13F Screening &amp; Anti-SPV Separation</h3>
              <p>
                Every run scans <strong>100% of SEC Form 13F institutional managers</strong> ({funnel ? funnel.totalCiksScanned.toLocaleString() : '11,400+'} CIKs) from quarterly SEC Form 13F Bulk Data Sets plus incremental post-bulk <code>master.idx</code> submissions—and cross-checks non-curated candidates against SEC submissions metadata (<code>entityType</code>/SIC) to drop operating companies, banks, insurers, pensions, brokerages, and market makers. Single-stock VC/founder unlock books (Effective Positions &lt;4.0 or top holding &gt;40% 4Q avg / &gt;45% latest) remain browsable under <em>Excluded SPVs</em> but are kept out of Top-100 and Elite-50 cohorts.
              </p>

              <h3 style={{ color: 'var(--text-primary)' }}>2. Composite Skill Score &amp; Elite-50 Cohort</h3>
              <p>
                Rather than chasing raw 1-year luck, managers are ranked on a <strong>Composite Skill Score (0–100)</strong> evaluated under the actionable <strong>45-Day Filing-Lag</strong> lens: <strong>40% Information Ratio vs. QQQ</strong> + <strong>30% Quarterly Batting Average vs. QQQ</strong> + <strong>30% Sortino Ratio</strong> measured against live trailing 13-week Treasury Bill yields (<code>^IRX</code>). The top 50 managers form the <strong>Elite 50</strong> cohort.
              </p>

              <h3 style={{ color: 'var(--text-primary)' }}>3. Zero-Look-Ahead Filing Lag, Amendments &amp; Pitfalls Eliminated</h3>
              <ul>
                <li><strong>45-Day Lag Actionable (Default):</strong> Rebalances only on actual SEC filing dates—and respects <code>13F-HR/A</code> confidential-treatment amendments from the exact amendment filing date, discarding stale restatements.</li>
                <li><strong>Broad ETF Stripping &amp; Dual-Class Normalization:</strong> Broad index/sector/treasury ETFs (<code>SPY</code>, <code>QQQ</code>, <code>BIL</code>, <code>SHV</code>, etc.) are stripped from single-stock selection weights, and dual share classes (<code>GOOG</code>→<code>GOOGL</code>, etc.) are unified before concentration &amp; consensus scoring.</li>
                <li><strong>Survivorship-Free Backtests:</strong> The Ideal Portfolio quarterly simulation includes historical qualified tech funds even if they later closed or drifted below 35% Tech, re-ranking managers at each historical date using only past prices.</li>
              </ul>

              <h3 style={{ color: 'var(--text-primary)' }}>4. Public APIs &amp; Stock Dossier Signals</h3>
              <p>
                Every indexed equity ({stocksIndexedCount.toLocaleString()} stocks across sharded JSON payloads) integrates <strong>Est. Quarter VWAP &amp; Entry Zones</strong> from split-adjusted daily OHLCV volumes, <strong>SEC EDGAR XBRL CompanyFacts</strong> (TTM Revenue, YoY Growth, Operating &amp; FCF Margins, R&amp;D Intensity, Share Dilution, Market Cap Tiers), and <strong>Live Schedule 13D / 13G Filings</strong> across Elite-50 managers for mid-quarter &ge;5% activist &amp; passive stake alerts.
              </p>

              <h3 style={{ color: 'var(--text-primary)' }}>5. Optimal Filing-Season Cron Schedule</h3>
              <p>
                GitHub Actions runs at <strong>23:15 UTC &amp; 05:15 UTC</strong> daily during peak 45-day deadline days (Feb/May/Aug/Nov 11–19) right after SEC EDGAR closing dumps, at 05:15 UTC on season checkpoints (days 1, 5, 8, 10, 21, 25, 28), and every Saturday at 06:15 UTC for weekly amendments, 13D/13G alerts, and XBRL/price refreshes.
              </p>

              {funnel && (
                <div className="universe-funnel-panel">
                  <div className="universe-funnel-banner">
                    <div>
                      <div style={{ fontWeight: 700, fontSize: '0.84rem', color: 'var(--text-primary)', display: 'flex', alignItems: 'center', gap: '0.4rem' }}>
                        <Database size={15} color="#0071e3" />
                        {funnel.totalCiksScanned.toLocaleString()} SEC 13F CIKs Scanned → {funnel.elite50Count} Elite 50 / {funnel.qualifiedFundsCount} Qualified Funds
                      </div>
                      <div style={{ fontSize: '0.75rem', color: 'var(--text-secondary)', marginTop: '0.15rem' }}>
                        {funnel.bulkDatasetsCount} SEC Bulk ZIPs (through {funnel.bulkMaxFilingDate}) + {funnel.postBulkFilingsCount.toLocaleString()} post-bulk .txt filings · {funnel.curatedQualifiedCount} Curated Core + {funnel.discoveredQualifiedCount} EDGAR Discovered
                      </div>
                    </div>
                  </div>

                  <div className="table-scroll">
                    <table className="data-table compact" aria-label="Full-universe screening funnel">
                      <thead>
                        <tr>
                          <th>Pipeline Stage</th>
                          <th>Filter Criteria</th>
                          <th className="num">Remaining</th>
                        </tr>
                      </thead>
                      <tbody>
                        <tr>
                          <td><strong>1. Total 13F Universe</strong></td>
                          <td>Unique CIKs filing Form 13F-HR / 13F-HR/A (2021–present)</td>
                          <td className="num mono">{funnel.totalCiksScanned.toLocaleString()}</td>
                        </tr>
                        <tr>
                          <td><strong>2. Active Recent Filers</strong></td>
                          <td>Filed within past 210 days</td>
                          <td className="num mono">{funnel.activeRecentCount.toLocaleString()}</td>
                        </tr>
                        <tr>
                          <td><strong>3. Focused Managers</strong></td>
                          <td>Exclude {funnel.excludedInstitutionsCount.toLocaleString()} operating corps, banks, insurers, pensions &amp; wirehouses</td>
                          <td className="num mono">{funnel.nonExcludedManagerCount.toLocaleString()}</td>
                        </tr>
                        <tr>
                          <td><strong>4. Multi-Quarter History</strong></td>
                          <td>&ge;4 recent quarters since 2024 (&ge;2 for Curated Core)</td>
                          <td className="num mono">{funnel.fourPlusQuartersCount.toLocaleString()}</td>
                        </tr>
                        <tr>
                          <td><strong>5. Institutional Scale</strong></td>
                          <td>Reported AUM &ge;$200M &amp; US Equity AUM &ge;$100M</td>
                          <td className="num mono">{funnel.sizeAndEquityPassCount.toLocaleString()}</td>
                        </tr>
                        <tr>
                          <td><strong>6. Concentration Screen</strong></td>
                          <td>&ge;5 pos, Eff-N &ge; 1.4, &amp; &le;130 pos (or &le;265 w/ Top 10 &ge; 48%) pre-screen</td>
                          <td className="num mono">{funnel.concentrationPassCount.toLocaleString()}</td>
                        </tr>
                        <tr>
                          <td><strong>7. Tech/TMT Focus Floor</strong></td>
                          <td>&ge;35% Tech/TMT equity weight &amp; SEC entity verification</td>
                          <td className="num mono">{funnel.techFloorPassCount.toLocaleString()}</td>
                        </tr>
                        <tr>
                          <td><strong>8. Rolling Strict Audit</strong></td>
                          <td>Rolling 4Q median Tech/TMT &ge;50% (40% Curated), &ge;75% persistence, &le;120 pos</td>
                          <td className="num mono">{funnel.strictQualificationCount.toLocaleString()}</td>
                        </tr>
                        <tr>
                          <td><strong>9. Priced History Verified</strong></td>
                          <td>&ge;$100M &amp; &ge;70% single-stock book actively priced (&gt;15 trading days)</td>
                          <td className="num mono">{funnel.pricedHistoryPassCount.toLocaleString()}</td>
                        </tr>
                        <tr>
                          <td><strong>10. Leaderboard Eligible / Elite 50</strong></td>
                          <td>Exclude ultra-concentrated SPVs (Eff-N &lt; 4 or Top 1 &gt; 40%/45%) → Elite 50 by Skill</td>
                          <td className="num mono">{funnel.leaderboardEligibleCount} / {funnel.elite50Count}</td>
                        </tr>
                        <tr>
                          <td><strong>11. Ranked Top-100 Union</strong></td>
                          <td>Unique funds reaching Top 100 in any window (QE / 45-Day Lag)</td>
                          <td className="num mono">{funnel.rankedUnionQeCount} / {funnel.rankedUnionLagCount}</td>
                        </tr>
                        <tr>
                          <td><strong>12. Live UI Shards &amp; Signals</strong></td>
                          <td>Updates · Ideal Portfolio · Indexed Stocks · 13D/13G Alerts</td>
                          <td className="num mono">
                            {funnel.updatesCardsCount} · {funnel.idealPortfolioCount} · {funnel.stocksIndexedCount.toLocaleString()} · {funnel.midQuarterAlertsCount}
                          </td>
                        </tr>
                      </tbody>
                    </table>
                  </div>
                </div>
              )}

              <div style={{ marginTop: '1rem', padding: '0.8rem', background: 'var(--bg-surface)', border: '1px solid var(--border-subtle)', borderRadius: '8px' }}>
                <strong>Data Sources:</strong>
                <ul style={{ margin: '0.35rem 0 0', paddingLeft: '1.1rem' }}>
                  {dataset.metadata.dataSources.map((src) => (
                    <li key={src}>{src}</li>
                  ))}
                </ul>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

export default App;
