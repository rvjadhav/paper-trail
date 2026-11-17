import { useCallback, useEffect, useMemo, useState, type MouseEvent } from 'react';
import { Activity, AlertTriangle, Calendar, Check, Copy, Info, Layers, PieChart, Trophy, X } from 'lucide-react';
import datasetUrl from './data/portfolio_data.json?url';
import type { PaperTrailDataset, ReturnMode, ReturnWindow } from './types/data';
import { formatPct, formatSignedPct, formatUsdCompact, returnClass } from './utils/format';
import { RankingTab } from './components/RankingTab';
import { HoldingsTab } from './components/HoldingsTab';
import { IdealPortfolioTab } from './components/IdealPortfolioTab';
import { UpdatesTab } from './components/UpdatesTab';
import { FundDetailDrawer } from './components/FundDetailDrawer';

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
  const [showMethodology, setShowMethodology] = useState(false);
  const [copiedSeeds, setCopiedSeeds] = useState(false);
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
      syncHash(tab, selectedFundCik);
    },
    [selectedFundCik, syncHash]
  );

  const setSelectedFundCik = useCallback(
    (cik: string | null) => {
      setSelectedFundCikState(cik);
      syncHash(activeTab, cik);
    },
    [activeTab, syncHash]
  );

  const goHome = useCallback((e?: MouseEvent<HTMLAnchorElement>) => {
    if (e && (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button !== 0)) {
      return;
    }
    e?.preventDefault();
    setSelectedFundCikState(null);
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
    };
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        if (selectedFundCik) setSelectedFundCik(null);
        else if (showMethodology) setShowMethodology(false);
      }
    };
    window.addEventListener('hashchange', onHashChange);
    window.addEventListener('keydown', onKeyDown);
    return () => {
      window.removeEventListener('hashchange', onHashChange);
      window.removeEventListener('keydown', onKeyDown);
    };
  }, [selectedFundCik, showMethodology, setSelectedFundCik]);

  const selectedFund = useMemo(
    () => dataset?.funds.find((f) => f.cik === selectedFundCik) ?? null,
    [dataset, selectedFundCik]
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

  const watchlist = dataset.metadata.discoveryWatchlist;
  const flaggedSeeds = watchlist?.emergingSeedCandidates ?? [];
  const urgentSeedCount = watchlist?.urgentCount ?? 0;

  const copySeedTuples = () => {
    const snippet = flaggedSeeds.map((c) => `    ${c.copyTuple}`).join('\n');
    if (snippet && navigator.clipboard?.writeText) {
      navigator.clipboard.writeText(snippet).then(() => {
        setCopiedSeeds(true);
        window.setTimeout(() => setCopiedSeeds(false), 2000);
      }).catch(() => {});
    }
  };

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
            <Info size={12} /> Methodology &amp; Quarterly Sync
            {flaggedSeeds.length > 0 && (
              <span className="seed-radar-pill" title="Emerging unseeded Tech/TMT stocks flagged for DISCOVERY_SEED_PAIRS review">
                <AlertTriangle size={10} />
                {urgentSeedCount > 0 ? `${urgentSeedCount} Seed Alert${urgentSeedCount > 1 ? 's' : ''}` : `${flaggedSeeds.length} Seed Watch`}
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
              <span className="nav-count">Top 75</span>
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
          fund={selectedFund}
          mode={mode}
          benchmarks={dataset.benchmarks}
          onClose={() => setSelectedFundCik(null)}
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
              <h3 style={{ color: 'var(--text-primary)', marginTop: 0 }}>1. Tech-Focused Concentrated Screening</h3>
              <p>
                Every quarter, our pipeline combines 100 curated AI/TMT &amp; crossover funds with an automated SEC EDGAR full-text scan across {watchlist?.seedPairCount ?? 28} configured sub-sector CUSIP seed pairs (up to 1,000 candidates with $200M+ reported value). Qualified funds maintain ≥20% Tech/TMT equity exposure, ≥$100M in 13F equity AUM, and concentrated sizing (Top 10 ≥35% or ≤75 equity positions).
              </p>

              <h3 style={{ color: 'var(--text-primary)' }}>2. Performance Reconstruction (Quarter-End vs. 45-Day Lag)</h3>
              <p>
                Because SEC Form 13F discloses quarter-end U.S. long equities (with a ~45-day filing lag) and omits cash and short books, Paper Trail reconstructs true buy-and-hold equity returns using split/dividend-adjusted daily prices across two transparent lenses:
              </p>
              <ul>
                <li><strong>Quarter-End Attribution:</strong> Rebalances into quarter-end equity weights at each quarter close to measure manager stock-selection skill.</li>
                <li><strong>45-Day Filing-Lag Actionable:</strong> Rebalances only on the actual SEC filing date when the 13F became public.</li>
              </ul>

              <h3 style={{ color: 'var(--text-primary)' }}>3. Ideal Portfolio &amp; Quarterly Updates</h3>
              <p>
                The Ideal Portfolio selects up to 50 names (default 40) from the union of Top-75 ranked funds across all six windows, scoring each stock by manager rank × √(portfolio weight) × consensus multiplier, water-filled under an 8% position cap. The Updates tab diffs consecutive Form 13F-HR quarters across common shares, calls, and puts to highlight initiations, heavy adds, exits, put-hedge shifts, common-to-call rotations, and valuation markdowns on flat share counts.
              </p>

              <h3 style={{ color: 'var(--text-primary)' }}>4. Automated Quarterly Updates</h3>
              <p>
                GitHub Actions runs automatically during every 13F filing season (February, May, August, November — plus weekly refresh checks) to pull new 13F-HR / 13F-HR/A filings, update market prices, rebuild the static site, and redeploy to GitHub Pages.
              </p>

              <h3 style={{ color: 'var(--text-primary)' }}>5. Discovery Seed Radar (Hardcoded Seed Drift Monitor)</h3>
              <p style={{ marginBottom: '0.45rem' }}>
                SEC EDGAR EFTS queries depend on seeded CUSIP pairs in <code>DISCOVERY_SEED_PAIRS</code> (<code>pipeline/funds_config.py</code>). Every pipeline run audits all unseeded public Tech/TMT stocks held across Top-75 funds and automatically raises an alert here and in GitHub Actions logs when newly emerging &ldquo;must-hold&rdquo; stocks show surging institutional adoption.
              </p>

              {watchlist && (
                <div className="seed-radar-panel">
                  <div className={`seed-radar-banner ${flaggedSeeds.length === 0 ? 'calm' : ''}`}>
                    <div>
                      <div style={{ fontWeight: 700, fontSize: '0.84rem', color: 'var(--text-primary)', display: 'flex', alignItems: 'center', gap: '0.4rem' }}>
                        <AlertTriangle size={15} color={flaggedSeeds.length > 0 ? '#b25000' : '#00823b'} />
                        {flaggedSeeds.length > 0
                          ? `${flaggedSeeds.length} Unseeded Breakout Stocks Flagged (${urgentSeedCount} Urgent)`
                          : 'Seed List Aligned — No Urgent Unseeded Breakouts'}
                      </div>
                      <div style={{ fontSize: '0.75rem', color: 'var(--text-secondary)', marginTop: '0.15rem' }}>
                        Monitoring {watchlist.seedPairCount} EFTS seed pairs across {watchlist.activeSeedTickers.length} tickers
                      </div>
                    </div>

                    {flaggedSeeds.length > 0 && (
                      <button type="button" className="seed-copy-btn" onClick={copySeedTuples}>
                        {copiedSeeds ? <Check size={13} /> : <Copy size={13} />}
                        {copiedSeeds ? 'Copied tuples!' : 'Copy DISCOVERY_SEED_PAIRS'}
                      </button>
                    )}
                  </div>

                  <div style={{ padding: '0.7rem 0.95rem 0.35rem' }}>
                    <div style={{ fontSize: '0.72rem', fontWeight: 600, textTransform: 'uppercase', letterSpacing: '0.04em', color: 'var(--text-muted)' }}>
                      Active Seed Tickers ({watchlist.activeSeedTickers.length})
                    </div>
                    <div className="seed-radar-tickers">
                      {watchlist.activeSeedTickers.map((t) => (
                        <span key={t} className="seed-mini-chip">{t}</span>
                      ))}
                    </div>
                  </div>

                  {flaggedSeeds.length > 0 && (
                    <div className="table-scroll">
                      <table className="data-table compact" aria-label="Emerging unseeded stocks">
                        <thead>
                          <tr>
                            <th>Stock</th>
                            <th>CUSIP</th>
                            <th className="num">Top-75 / All</th>
                            <th className="num">New + Surge</th>
                            <th className="num">TTM</th>
                            <th>Suggested Pair</th>
                          </tr>
                        </thead>
                        <tbody>
                          {flaggedSeeds.map((c) => (
                            <tr key={c.ticker}>
                              <td>
                                <div style={{ display: 'flex', alignItems: 'center', gap: '0.4rem', flexWrap: 'wrap' }}>
                                  <span className="ticker-badge">{c.ticker}</span>
                                  <span className={`chip ${c.alertTier === 'URGENT' ? 'warn' : ''}`} style={{ fontSize: '0.67rem' }}>
                                    {c.alertTier}
                                  </span>
                                </div>
                                <div style={{ fontSize: '0.72rem', color: 'var(--text-muted)', marginTop: '0.15rem' }}>
                                  {c.name}
                                  {c.idealRank ? ` · Ideal #${c.idealRank}` : ''}
                                  {c.consensusRank ? ` · Top-30 #${c.consensusRank}` : ''}
                                </div>
                              </td>
                              <td className="mono" style={{ fontSize: '0.75rem' }}>{c.cusip}</td>
                              <td className="num mono">{c.holderCount} / {c.allQualifiedHolders}</td>
                              <td className="num mono">
                                {c.newInitiationsCount}+{c.heavyAddsCount} ({formatPct(c.surgePct, 0)})
                              </td>
                              <td className={`num mono ${returnClass(c.stockTtmPct)}`}>
                                {formatSignedPct(c.stockTtmPct, 0)}
                              </td>
                              <td className="mono" style={{ fontSize: '0.76rem', color: 'var(--accent-primary)', fontWeight: 600 }}>
                                {c.suggestedPair}
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  )}

                  {watchlist.staleSeeds.length > 0 && (
                    <div style={{ padding: '0.65rem 0.95rem', borderTop: '1px solid var(--border-hairline)', fontSize: '0.76rem', color: 'var(--warn-text)' }}>
                      <strong>Low-coverage seeds:</strong>{' '}
                      {watchlist.staleSeeds.map((s) => `${s.ticker} (${s.rankedHolders}/${s.allQualifiedHolders})`).join(', ')}
                    </div>
                  )}
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
