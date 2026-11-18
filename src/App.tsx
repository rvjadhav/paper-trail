import { useCallback, useEffect, useMemo, useState, type MouseEvent } from 'react';
import { Activity, Calendar, Database, Info, Layers, PieChart, Trophy, X } from 'lucide-react';
import datasetUrl from './data/portfolio_data.json?url';
import type { PaperTrailDataset, ReturnMode, ReturnWindow } from './types/data';
import { formatUsdCompact } from './utils/format';
import { RankingTab } from './components/RankingTab';
import { HoldingsTab } from './components/HoldingsTab';
import { IdealPortfolioTab } from './components/IdealPortfolioTab';
import { UpdatesTab } from './components/UpdatesTab';
import { FundDetailDrawer } from './components/FundDetailDrawer';
import { UpdateDetailDrawer } from './components/UpdateDetailDrawer';

type PrimaryTab = 'ranking' | 'holdings' | 'ideal' | 'updates';

function parseHashState(): { tab: PrimaryTab; cik: string | null } {
  const h = window.location.hash.replace(/^#/, '');
  const [tabPart, fundPart] = h.split('/');
  const validTabs: PrimaryTab[] = ['ranking', 'holdings', 'ideal', 'updates'];
  const tab = validTabs.includes(tabPart as PrimaryTab) ? (tabPart as PrimaryTab) : 'ranking';
  const cik = fundPart && /^\d{10}$/.test(fundPart) ? fundPart : null;
  return { tab, cik };
}

export function App() {
  const [dataset, setDataset] = useState<PaperTrailDataset | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const initial = useMemo(() => parseHashState(), []);
  const [activeTab, setActiveTabState] = useState<PrimaryTab>(initial.tab);
  const [windowSize, setWindowSize] = useState<ReturnWindow>('TTM');
  const [mode, setMode] = useState<ReturnMode>('quarter_end');
  const [selectedFundCik, setSelectedFundCikState] = useState<string | null>(initial.cik);
  const [selectedUpdateCik, setSelectedUpdateCik] = useState<string | null>(null);
  const [updateReturnToFund, setUpdateReturnToFund] = useState(false);
  const [showMethodology, setShowMethodology] = useState(false);
  const [homeResetKey, setHomeResetKey] = useState(0);

  useEffect(() => {
    let cancelled = false;
    fetch(datasetUrl)
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return res.json() as Promise<PaperTrailDataset>;
      })
      .then((data) => {
        if (!cancelled) setDataset(data);
      })
      .catch((err) => {
        if (!cancelled) setLoadError(err instanceof Error ? err.message : 'Failed to load dataset');
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const syncHash = useCallback((tab: PrimaryTab, cik: string | null) => {
    const nextHash = cik ? `#${tab}/${cik}` : `#${tab}`;
    if (window.location.hash !== nextHash) {
      window.history.replaceState(null, '', nextHash);
    }
  }, []);

  const setActiveTab = useCallback(
    (tab: PrimaryTab) => {
      setActiveTabState(tab);
      setSelectedUpdateCik(null);
      setUpdateReturnToFund(false);
      syncHash(tab, selectedFundCik);
    },
    [selectedFundCik, syncHash]
  );

  const setSelectedFundCik = useCallback(
    (cik: string | null) => {
      setSelectedUpdateCik(null);
      setUpdateReturnToFund(false);
      setSelectedFundCikState(cik);
      syncHash(activeTab, cik);
    },
    [activeTab, syncHash]
  );

  const handleDiveDeeperFromFund = useCallback(
    (cik: string) => {
      setSelectedFundCikState(null);
      syncHash(activeTab, null);
      setUpdateReturnToFund(true);
      setSelectedUpdateCik(cik);
    },
    [activeTab, syncHash]
  );

  const handleDiveDeeperFromUpdates = useCallback((cik: string) => {
    setUpdateReturnToFund(false);
    setSelectedUpdateCik(cik);
  }, []);

  const goHome = useCallback((e?: MouseEvent<HTMLAnchorElement>) => {
    if (e && (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button !== 0)) {
      return;
    }
    e?.preventDefault();
    setSelectedFundCikState(null);
    setSelectedUpdateCik(null);
    setUpdateReturnToFund(false);
    setShowMethodology(false);
    setActiveTabState('ranking');
    setWindowSize('TTM');
    setMode('quarter_end');
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
      setSelectedUpdateCik(null);
      setUpdateReturnToFund(false);
    };
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        if (selectedUpdateCik) {
          setSelectedUpdateCik(null);
          setUpdateReturnToFund(false);
        } else if (selectedFundCik) setSelectedFundCik(null);
        else if (showMethodology) setShowMethodology(false);
      }
    };
    window.addEventListener('hashchange', onHashChange);
    window.addEventListener('keydown', onKeyDown);
    return () => {
      window.removeEventListener('hashchange', onHashChange);
      window.removeEventListener('keydown', onKeyDown);
    };
  }, [selectedFundCik, selectedUpdateCik, showMethodology, setSelectedFundCik]);

  const selectedFund = useMemo(
    () => dataset?.funds.find((f) => f.cik === selectedFundCik) ?? null,
    [dataset, selectedFundCik]
  );

  const selectedFundUpdate = useMemo(
    () => (selectedFundCik && dataset ? dataset.fundUpdates.find((u) => u.cik === selectedFundCik) ?? null : null),
    [dataset, selectedFundCik]
  );

  const selectedUpdate = useMemo(
    () => (selectedUpdateCik && dataset ? dataset.fundUpdates.find((u) => u.cik === selectedUpdateCik) ?? null : null),
    [dataset, selectedUpdateCik]
  );

  const totalTrackedAum = useMemo(
    () => (dataset ? dataset.funds.reduce((s, f) => s + f.equityAumUsd, 0) : 0),
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
            Loading institutional 13F intelligence...
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
                {funnel.totalCiksScanned.toLocaleString()} CIKs → {funnel.qualifiedFundsCount} Funds
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
                Following the SEC Form 13F paper trail across concentrated technology &amp; TMT funds to gain conviction and insight.
              </p>
            </div>

            <div className="header-kpis">
              <div className="header-kpi">
                <div className="header-kpi-label">Qualified Funds</div>
                <div className="header-kpi-val">{dataset.metadata.qualifiedFundsCount}</div>
              </div>
              <div className="header-kpi">
                <div className="header-kpi-label">Tracked 13F Capital</div>
                <div className="header-kpi-val">{formatUsdCompact(totalTrackedAum)}</div>
              </div>
              <div className="header-kpi">
                <div className="header-kpi-label">Filings Analyzed</div>
                <div className="header-kpi-val">{dataset.metadata.total13fFilingsProcessed}</div>
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
              <span className="nav-count">Top 100</span>
            </button>

            <button
              type="button"
              id="tab-holdings"
              className={`nav-tab ${activeTab === 'holdings' ? 'active' : ''}`}
              onClick={() => setActiveTab('holdings')}
            >
              <Layers size={16} />
              <span>Holdings</span>
              <span className="nav-count">Top 30</span>
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
            windowSize={windowSize}
            onWindowChange={setWindowSize}
            mode={mode}
            onModeChange={setMode}
            onSelectFund={setSelectedFundCik}
          />
        )}

        {activeTab === 'holdings' && (
          <HoldingsTab
            topHoldings={dataset.topHoldings}
            mode={mode}
            onModeChange={setMode}
            onSelectFund={setSelectedFundCik}
          />
        )}

        {activeTab === 'ideal' && (
          <IdealPortfolioTab
            idealPortfolio={dataset.idealPortfolio}
            benchmarks={dataset.benchmarks}
            mode={mode}
            onModeChange={setMode}
            onSelectFund={setSelectedFundCik}
          />
        )}

        {activeTab === 'updates' && (
          <UpdatesTab
            updates={dataset.fundUpdates}
            overview={dataset.updatesOverview}
            onSelectFund={setSelectedFundCik}
            onDiveDeeper={handleDiveDeeperFromUpdates}
          />
        )}
      </main>

      {/* Footer */}
      <footer className="site-footer">
        <div className="footer-inner">
          <div>
            <strong>Paper Trail</strong> · Automated quarterly SEC EDGAR Form 13F-HR analytics pipeline.
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
          onClose={() => setSelectedFundCik(null)}
          onDiveDeeper={handleDiveDeeperFromFund}
        />
      )}

      {/* Dive Deeper Filing Diff Drawer */}
      {selectedUpdate && (
        <UpdateDetailDrawer
          key={selectedUpdate.cik}
          update={selectedUpdate}
          fromFundDrawer={updateReturnToFund}
          onClose={() => {
            setSelectedUpdateCik(null);
            setUpdateReturnToFund(false);
          }}
          onOpenFundProfile={(cik) => {
            setSelectedFundCik(cik);
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
          <div className="drawer-panel" style={{ width: 'min(740px, 100vw)' }}>
            <div className="drawer-header">
              <div>
                <h2 id="meth-title" style={{ margin: 0, fontSize: '1.25rem' }}>
                  Data Methodology &amp; Quarterly Automation
                </h2>
                <p style={{ margin: '0.25rem 0 0', fontSize: '0.82rem', color: 'var(--text-secondary)' }}>
                  How Paper Trail ingests, qualifies, and scores 13F filings every quarter
                </p>
              </div>
              <button type="button" className="close-btn" onClick={() => setShowMethodology(false)} aria-label="Close methodology">
                <X size={18} />
              </button>
            </div>
            <div className="drawer-body" style={{ fontSize: '0.86rem', color: 'var(--text-secondary)', lineHeight: 1.65 }}>
              <h3 style={{ color: 'var(--text-primary)', marginTop: 0 }}>1. Full-Universe SEC 13F Screening (Zero Seed Bias)</h3>
              <p>
                Every quarter, our pipeline scans <strong>100% of SEC Form 13F institutional managers</strong> ({funnel ? funnel.totalCiksScanned.toLocaleString() : '11,400+'} CIKs) directly from quarterly SEC Form 13F Bulk Data Sets plus incremental post-bulk <code>master.idx</code> submissions—with zero hardcoded CUSIP seed pairs or EFTS caps. Operating companies, commercial banks, insurers, brokerages, endowments, prop trading desks, and single-stock VC unlock SPVs are filtered out deterministically. Qualified funds must pass strict thresholds on their latest quarter: ≥4 quarters of recent filings (since 2024), ≥$200M total reported value &amp; ≥$100M US equity AUM, verified post-price active equity history (≥$100M &amp; ≥50% equity weight), concentration (≥5 equity positions, effective N ≥ 1.5 for discovered managers, and ≤120 positions or ≤250 w/ Top 10 ≥ 55%), and <strong>≥35% Tech/TMT allocation</strong> (≥30% for Curated Core crossover funds).
              </p>

              <h3 style={{ color: 'var(--text-primary)' }}>2. Performance Reconstruction (Quarter-End vs. 45-Day Lag)</h3>
              <p>
                Because SEC Form 13F discloses quarter-end U.S. long equities (with a ~45-day filing lag) and omits cash and short books, Paper Trail normalizes dual share classes (e.g., GOOG/GOOGL) and reconstructs true buy-and-hold equity returns using split/dividend-adjusted daily prices across two independent lenses:
              </p>
              <ul>
                <li><strong>Quarter-End Attribution:</strong> Rebalances into quarter-end equity weights at each quarter close to measure manager stock-selection skill.</li>
                <li><strong>45-Day Filing-Lag Actionable:</strong> Rebalances only on the actual SEC filing date when each 13F became public.</li>
              </ul>

              <h3 style={{ color: 'var(--text-primary)' }}>3. Ideal Portfolio &amp; Quarterly Updates</h3>
              <p>
                The Ideal Portfolio selects up to 50 active US-listed names (default 40) from the union of Top-100 ranked funds across all six windows, scoring each stock by manager percentile rank<sup>1.5</sup> × √(portfolio weight) × Tech/TMT specialist relevance × consensus multiplier × QoQ accumulation factor, water-filled under an 8% cap and backtested independently for Quarter-End and 45-Day Lag execution. The Updates tab diffs consecutive Form 13F-HR quarters across common shares, calls, and puts to highlight initiations, heavy adds, exits, put-hedge shifts, common-to-call rotations, and valuation markdowns on flat share counts.
              </p>

              <h3 style={{ color: 'var(--text-primary)' }}>4. Automated Quarterly Updates</h3>
              <p>
                GitHub Actions runs automatically during every 13F filing season (February, May, August, November — plus weekly refresh checks) to sync SEC Form 13F Bulk Data Sets and incremental <code>master.idx</code> filings, update market prices, rebuild the static site, and redeploy to GitHub Pages.
              </p>

              <h3 style={{ color: 'var(--text-primary)' }}>5. Live Universe Screening Funnel</h3>
              <p style={{ marginBottom: '0.45rem' }}>
                Deterministic audit trail tracing every SEC Form 13F CIK evaluated from raw EDGAR filings down to the Top-100 leaderboards, Ideal Portfolio, and Quarterly Updates feed.
              </p>

              {funnel && (
                <div className="universe-funnel-panel">
                  <div className="universe-funnel-banner">
                    <div>
                      <div style={{ fontWeight: 700, fontSize: '0.84rem', color: 'var(--text-primary)', display: 'flex', alignItems: 'center', gap: '0.4rem' }}>
                        <Database size={15} color="#0071e3" />
                        {funnel.totalCiksScanned.toLocaleString()} SEC 13F CIKs Scanned → {funnel.qualifiedFundsCount} Qualified Specialist Funds
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
                          <td>≥4 recent quarters since 2024 (≥2 for Curated Core)</td>
                          <td className="num mono">{funnel.fourPlusQuartersCount.toLocaleString()}</td>
                        </tr>
                        <tr>
                          <td><strong>5. Institutional Scale</strong></td>
                          <td>Reported AUM ≥$200M &amp; US Equity AUM ≥$100M (15% buffer before Q3 merge)</td>
                          <td className="num mono">{funnel.sizeAndEquityPassCount.toLocaleString()}</td>
                        </tr>
                        <tr>
                          <td><strong>6. Concentration &amp; Anti-SPV</strong></td>
                          <td>≥5 pos, Eff-N ≥ 1.4, and ≤130 pos (or ≤265 w/ Top 10 ≥ 48%) pre-screen</td>
                          <td className="num mono">{funnel.concentrationPassCount.toLocaleString()}</td>
                        </tr>
                        <tr>
                          <td><strong>7. Tech/TMT Focus Floor</strong></td>
                          <td>≥35% Tech/TMT equity weight (Curated Core exempt into full audit)</td>
                          <td className="num mono">{funnel.techFloorPassCount.toLocaleString()}</td>
                        </tr>
                        <tr>
                          <td><strong>8. Latest Qtr Strict Audit</strong></td>
                          <td>Latest filing ≥35% Tech (≥30% Curated), ≤120 pos (or ≤250 w/ Top 10 ≥ 55%), ≥$200M/$100M, ≥4Q</td>
                          <td className="num mono">{funnel.strictQualificationCount.toLocaleString()}</td>
                        </tr>
                        <tr>
                          <td><strong>9. Priced History Verified</strong></td>
                          <td>≥$100M &amp; ≥50% of latest equity book actively priced (&gt;15 trading days)</td>
                          <td className="num mono">{funnel.pricedHistoryPassCount.toLocaleString()}</td>
                        </tr>
                        <tr>
                          <td><strong>10. Ranked Top-100 Union</strong></td>
                          <td>Unique funds reaching Top 100 in any window (QE / 45-Day Lag)</td>
                          <td className="num mono">{funnel.rankedUnionQeCount} / {funnel.rankedUnionLagCount}</td>
                        </tr>
                        <tr>
                          <td><strong>11. Live UI Output</strong></td>
                          <td>Updates Feed cards · Ideal Portfolio active holdings</td>
                          <td className="num mono">{funnel.updatesCardsCount} · {funnel.idealPortfolioCount}</td>
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
