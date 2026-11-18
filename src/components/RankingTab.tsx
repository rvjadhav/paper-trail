import { useMemo, useState, type FC } from 'react';
import { AlertCircle, ArrowUpRight, Award, Download, Search } from 'lucide-react';
import type {
  BenchmarkSeries,
  FundSummary,
  RankedWindowEntry,
  ReturnMode,
  ReturnWindow,
  StockIndexEntry,
} from '../types/data';
import { resolveAlphaDecayMetrics } from '../utils/alphaDecay';
import type { UnifiedConvictionRow } from '../utils/convictionMatrix';
import {
  actionVerdictChipClass,
  alphaDecayChipClass,
  alphaDecayLabel,
  downloadCsv,
  entryZoneChipClass,
  formatPct,
  formatSignedPct,
  formatUsdCompact,
  RETURN_WINDOWS,
  returnClass,
  tierChipClass,
} from '../utils/format';

interface RankingTabProps {
  funds: FundSummary[];
  rankings: Record<ReturnMode, Record<ReturnWindow, RankedWindowEntry[]>>;
  benchmarks: Record<'QQQ' | 'SPY' | 'XLK', BenchmarkSeries>;
  stocksIndex: Record<string, StockIndexEntry>;
  convictionByTicker?: Record<string, UnifiedConvictionRow>;
  latestQuarterCovered?: string;
  windowSize: ReturnWindow;
  onWindowChange: (w: ReturnWindow) => void;
  mode: ReturnMode;
  onModeChange: (m: ReturnMode) => void;
  onSelectFund: (cik: string) => void;
  onSelectStock: (ticker: string) => void;
}

type TierFilter = 'ELITE50' | 'TOP100' | 'ALL' | 'EXCLUDED';
type SortMetric =
  | 'skillScore'
  | 'equityAumUsd'
  | 'return'
  | 'sortinoRatio'
  | 'infoRatioVsQqq'
  | 'battingAvgPct';
type HorizonFilter = 'ALL' | '8Q' | '12Q' | '20Q';

function getSortMetricValue(
  fund: FundSummary,
  metric: SortMetric,
  mode: ReturnMode,
  windowSize: ReturnWindow
): number | null {
  if (metric === 'equityAumUsd') return Number.isFinite(fund.equityAumUsd) ? fund.equityAumUsd : null;
  if (metric === 'return') return fund.returns[mode][windowSize];
  return fund.metrics[mode][metric];
}

export const RankingTab: FC<RankingTabProps> = ({
  funds,
  rankings,
  benchmarks,
  stocksIndex,
  convictionByTicker,
  latestQuarterCovered,
  windowSize,
  onWindowChange,
  mode,
  onModeChange,
  onSelectFund,
  onSelectStock,
}) => {
  const [tierFilter, setTierFilter] = useState<TierFilter>('ELITE50');
  const [sortMetric, setSortMetric] = useState<SortMetric>('skillScore');
  const [sortAsc, setSortAsc] = useState(false);
  const [horizonFilter, setHorizonFilter] = useState<HorizonFilter>('ALL');
  const [freshOnly, setFreshOnly] = useState(false);
  const [search, setSearch] = useState('');

  const isFundStale = (f: FundSummary): boolean =>
    Boolean(f.isStaleQuarter ?? (latestQuarterCovered && f.latestQuarterLabel !== latestQuarterCovered));

  const staleCount = useMemo(
    () => funds.filter((f) => isFundStale(f)).length,
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [funds, latestQuarterCovered]
  );

  const eliteCount = useMemo(
    () => funds.filter((f) => f.isLeaderboardEligible && f.fundTier === 'Elite 50').length,
    [funds]
  );
  const eligibleCount = useMemo(
    () => funds.filter((f) => f.isLeaderboardEligible).length,
    [funds]
  );
  const excludedCount = useMemo(
    () => funds.filter((f) => !f.isLeaderboardEligible).length,
    [funds]
  );

  const cohortRows = useMemo(() => {
    const q = search.trim().toLowerCase();

    const matchesHorizonAndFreshness = (f: FundSummary) => {
      if (freshOnly && isFundStale(f)) return false;
      if (horizonFilter === '8Q' && f.quartersTracked < 8) return false;
      if (horizonFilter === '12Q' && f.quartersTracked < 12) return false;
      if (horizonFilter === '20Q' && f.quartersTracked < 20) return false;
      return true;
    };

    const matchesSearch = (f: FundSummary) => {
      if (!q) return true;
      const mgr = f.manager && f.manager !== 'SEC EDGAR Discovered' ? f.manager.toLowerCase() : '';
      if (q.length === 1) {
        return (
          f.name.toLowerCase().includes(q) ||
          mgr.includes(q) ||
          f.searchableTickers.some((t) => t.toLowerCase() === q)
        );
      }
      return (
        f.name.toLowerCase().includes(q) ||
        mgr.includes(q) ||
        f.styleTag.toLowerCase().includes(q) ||
        f.topHoldingsPreview.some((h) => h.ticker.toLowerCase().includes(q)) ||
        f.searchableTickers.some((t) => t.toLowerCase() === q)
      );
    };

    const comparator = (a: FundSummary, b: FundSummary) => {
      const av = getSortMetricValue(a, sortMetric, mode, windowSize);
      const bv = getSortMetricValue(b, sortMetric, mode, windowSize);
      if (av === null && bv === null) {
        return (b.metrics[mode].skillScore ?? -1) - (a.metrics[mode].skillScore ?? -1);
      }
      if (av === null) return 1;
      if (bv === null) return -1;
      if (bv !== av) return sortAsc ? av - bv : bv - av;
      const ask = a.metrics[mode].skillScore ?? -1;
      const bsk = b.metrics[mode].skillScore ?? -1;
      if (bsk !== ask) return bsk - ask;
      return (b.returns[mode][windowSize] ?? -999) - (a.returns[mode][windowSize] ?? -999);
    };

    let basePool: FundSummary[];
    if (tierFilter === 'EXCLUDED') {
      basePool = funds.filter((f) => !f.isLeaderboardEligible && matchesHorizonAndFreshness(f)).sort(comparator);
    } else {
      const eligible = funds.filter((f) => f.isLeaderboardEligible && matchesHorizonAndFreshness(f));
      if (tierFilter === 'ELITE50') {
        basePool = eligible.filter((f) => f.fundTier === 'Elite 50').sort(comparator);
      } else if (tierFilter === 'TOP100') {
        const sortedEligible = [...eligible].sort(comparator);
        basePool = sortedEligible.slice(0, 100);
      } else {
        basePool = [...eligible].sort(comparator);
      }
    }

    return basePool
      .map((f, idx) => ({
        fund: f,
        displayRank: tierFilter === 'EXCLUDED' ? null : idx + 1,
      }))
      .filter(({ fund }) => matchesSearch(fund));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [funds, tierFilter, sortMetric, sortAsc, horizonFilter, freshOnly, latestQuarterCovered, mode, windowSize, search]);

  const matchingStocks = useMemo(() => {
    const raw = search.trim();
    if (!raw) return [];
    const ql = raw.toLowerCase();
    const qu = raw.toUpperCase();
    const all = Object.values(stocksIndex);

    const hits = all.filter((s) => {
      const tl = s.ticker.toLowerCase();
      if (ql.length === 1) return tl === ql;
      return tl.includes(ql) || s.name.toLowerCase().includes(ql);
    });

    hits.sort((a, b) => {
      const aExact = a.ticker === qu ? 1 : 0;
      const bExact = b.ticker === qu ? 1 : 0;
      if (aExact !== bExact) return bExact - aExact;
      const aPref = a.ticker.startsWith(qu) ? 1 : 0;
      const bPref = b.ticker.startsWith(qu) ? 1 : 0;
      if (aPref !== bPref) return bPref - aPref;
      if (b.eliteHoldersCount !== a.eliteHoldersCount) return b.eliteHoldersCount - a.eliteHoldersCount;
      return b.holderCount - a.holderCount;
    });

    return hits.slice(0, 6);
  }, [search, stocksIndex]);

  const skillLeader = useMemo(() => {
    const eligible = funds.filter((f) => f.isLeaderboardEligible);
    return [...eligible].sort(
      (a, b) => (b.metrics[mode].skillScore ?? -1) - (a.metrics[mode].skillScore ?? -1)
    )[0];
  }, [funds, mode]);

  const cohortStats = useMemo(() => {
    const withWin = cohortRows
      .map((r) => r.fund.returns[mode][windowSize])
      .filter((v): v is number => v !== null);
    const avgReturn = withWin.length > 0 ? withWin.reduce((s, v) => s + v, 0) / withWin.length : null;
    const totalAum = cohortRows.reduce((s, r) => s + r.fund.equityAumUsd, 0);
    return { avgReturn, totalAum, count: cohortRows.length };
  }, [cohortRows, mode, windowSize]);

  const returnTop100Count = rankings[mode][windowSize]?.length ?? 100;
  const qqqRet = benchmarks.QQQ.returns[windowSize];
  const spyRet = benchmarks.SPY.returns[windowSize];
  const xlkRet = benchmarks.XLK.returns[windowSize];

  const handleSortMetricClick = (metric: SortMetric) => {
    if (sortMetric === metric) {
      setSortAsc((v) => !v);
    } else {
      setSortMetric(metric);
      setSortAsc(false);
    }
  };

  const handleWindowClick = (w: ReturnWindow) => {
    if (windowSize === w && sortMetric === 'return') {
      setSortAsc((v) => !v);
    } else {
      onWindowChange(w);
      setSortMetric('return');
      setSortAsc(false);
    }
  };

  const handleExport = () => {
    downloadCsv(
      `paper-trail-ranking-${tierFilter.toLowerCase()}-${sortMetric}-${windowSize}-${mode}.csv`,
      [
        'Display Rank',
        'Fund Tier',
        'Skill Rank',
        'Skill Score',
        'Fund Name',
        'Manager',
        '13F Equity AUM (USD)',
        'Positions',
        'Eff. Positions',
        'Top 10 %',
        'Tech/TMT %',
        'ETF Stripped %',
        'Sortino',
        'IR vs QQQ',
        'Batting Avg %',
        'Avg Turnover %',
        'YTD %',
        'TTM %',
        '18M %',
        '2Y %',
        '3Y %',
        '5Y %',
        'Latest Period',
        'Stale Quarter',
        'Exclusion Reason',
      ],
      cohortRows.map(({ fund, displayRank }) => [
        displayRank ?? 'Excluded',
        fund.fundTier,
        fund.metrics[mode].skillRank ?? '',
        fund.metrics[mode].skillScore ?? '',
        fund.name,
        fund.manager,
        fund.equityAumUsd,
        fund.equityPositionCount,
        fund.effectivePositions,
        fund.top10WeightPct,
        fund.techTmtPct,
        fund.etfSharePct,
        fund.metrics[mode].sortinoRatio ?? '',
        fund.metrics[mode].infoRatioVsQqq ?? '',
        fund.metrics[mode].battingAvgPct ?? '',
        fund.metrics[mode].avgTurnoverPct ?? '',
        fund.returns[mode].YTD ?? '',
        fund.returns[mode].TTM ?? '',
        fund.returns[mode]['18M'] ?? '',
        fund.returns[mode]['2Y'] ?? '',
        fund.returns[mode]['3Y'] ?? '',
        fund.returns[mode]['5Y'] ?? '',
        fund.latestPeriod,
        isFundStale(fund) ? 'Stale' : '',
        fund.exclusionReason ?? '',
      ])
    );
  };

  const tierTitle =
    tierFilter === 'ELITE50'
      ? `Elite ${eliteCount} Skill-Ranked Concentrated Tech Funds`
      : tierFilter === 'TOP100'
        ? `Top ${Math.min(100, eligibleCount)} Cohort (${
            sortMetric === 'return'
              ? `${windowSize} Return · ${returnTop100Count} pool`
              : sortMetric === 'equityAumUsd'
                ? 'Total Fund Size Sort'
                : 'Risk-Adjusted Sort'
          })`
        : tierFilter === 'ALL'
          ? `All ${eligibleCount} Leaderboard-Eligible Specialist Funds`
          : `Excluded Single-Stock / Ultra-Concentrated Books (${excludedCount})`;

  const sortArrow = sortAsc ? '▲' : '▼';

  return (
    <section aria-label="Fund Rankings">
      <div className="section-toolbar">
        <div className="section-title-group">
          <h2>{tierTitle}</h2>
          <p>
            Scored across multi-year risk-adjusted skill (40% IR vs QQQ, 30% Batting Avg, 30% Sortino). Click any fund or stock ticker to inspect complete holdings and dossiers.
          </p>
        </div>

        <div className="controls-row">
          <div className="segmented-group" role="group" aria-label="Performance window">
            {RETURN_WINDOWS.map((w) => (
              <button
                key={w.id}
                type="button"
                id={`window-btn-${w.id}`}
                className={`seg-btn ${windowSize === w.id ? 'active' : ''}`}
                onClick={() => handleWindowClick(w.id)}
                title={w.fullLabel}
              >
                {w.label}
              </button>
            ))}
          </div>

          <div className="segmented-group" role="group" aria-label="Return methodology">
            <button
              type="button"
              id="mode-quarter-end"
              className={`seg-btn ${mode === 'quarter_end' ? 'active' : ''}`}
              onClick={() => onModeChange('quarter_end')}
              title="Measures quarterly buy-and-hold attribution from quarter-end weights"
            >
              Quarter-End
            </button>
            <button
              type="button"
              id="mode-filing-lag"
              className={`seg-btn ${mode === 'filing_lag' ? 'active' : ''}`}
              onClick={() => onModeChange('filing_lag')}
              title="Simulates trading only on the public SEC 13F filing date (45-day lag)"
            >
              45-Day Lag
            </button>
          </div>
        </div>
      </div>

      {/* Benchmark & Cohort KPI Strip */}
      <div className="metrics-strip">
        <div className="metric-card">
          <div className="metric-label">#1 by Skill Score ({mode === 'quarter_end' ? 'QE' : '45d Lag'})</div>
          <div className="metric-value">
            <span className="mono">
              {skillLeader?.metrics[mode].skillScore != null
                ? `${skillLeader.metrics[mode].skillScore.toFixed(1)}/100`
                : '—'}
            </span>
            {skillLeader?.returns[mode][windowSize] != null && (
              <span className={`mono ${returnClass(skillLeader.returns[mode][windowSize])}`} style={{ fontSize: '0.94rem' }}>
                {formatSignedPct(skillLeader.returns[mode][windowSize])}
              </span>
            )}
          </div>
          <div className="metric-sub">
            {skillLeader ? (
              <button
                type="button"
                className="fund-link-btn"
                style={{ fontSize: '0.78rem' }}
                onClick={() => onSelectFund(skillLeader.cik)}
              >
                {skillLeader.name} <ArrowUpRight size={12} />
              </button>
            ) : '—'}
          </div>
        </div>

        <div className="metric-card">
          <div className="metric-label">Cohort Mean ({windowSize})</div>
          <div className="metric-value">
            <span className={returnClass(cohortStats.avgReturn)}>{formatSignedPct(cohortStats.avgReturn)}</span>
          </div>
          <div className="metric-sub">
            vs. QQQ ({formatSignedPct(qqqRet)}) · Alpha{' '}
            <span className={`mono ${returnClass(cohortStats.avgReturn !== null && qqqRet !== null ? cohortStats.avgReturn - qqqRet : null)}`}>
              {cohortStats.avgReturn !== null && qqqRet !== null ? formatSignedPct(cohortStats.avgReturn - qqqRet) : '—'}
            </span>
          </div>
        </div>

        <div className="metric-card">
          <div className="metric-label">Cohort Combined 13F AUM</div>
          <div className="metric-value">{formatUsdCompact(cohortStats.totalAum)}</div>
          <div className="metric-sub">
            Across {cohortStats.count} shown · {eliteCount} Elite 50 / {eligibleCount} Eligible
          </div>
        </div>

        <div className="metric-card">
          <div className="metric-label">Benchmarks ({windowSize})</div>
          <div className="metric-value" style={{ fontSize: '1rem', gap: '0.8rem', flexWrap: 'wrap' }}>
            <span>QQQ <strong className={returnClass(qqqRet)}>{formatSignedPct(qqqRet)}</strong></span>
            <span>XLK <strong className={returnClass(xlkRet)}>{formatSignedPct(xlkRet)}</strong></span>
            <span>SPY <strong className={returnClass(spyRet)}>{formatSignedPct(spyRet)}</strong></span>
          </div>
          <div className="metric-sub">
            QQQ Sortino: <span className="mono">{benchmarks.QQQ.sortinoRatio ?? '—'}</span> · CAGR:{' '}
            <span className="mono">{formatSignedPct(benchmarks.QQQ.cagrPct)}</span>
          </div>
        </div>
      </div>

      {/* Matching Stock Dossiers Strip when Search matches stocks */}
      {matchingStocks.length > 0 && (
        <div className="matching-stocks-strip" aria-label="Matching Stock Dossiers">
          <div className="matching-stocks-head">
            <span>Matching Stocks — Open Dossier</span>
            <span className="mono" style={{ color: 'var(--text-muted)', fontSize: '0.72rem' }}>
              Click to view Q-VWAP cost basis, XBRL fundamentals &amp; all 13F holders
            </span>
          </div>
          <div className="matching-stocks-cards">
            {matchingStocks.map((st) => {
              const ad = resolveAlphaDecayMetrics(st, { latestQuarterLabel: latestQuarterCovered });
              const conv = convictionByTicker?.[st.ticker];
              const actVerdict = conv?.actionVerdict ?? st.actionVerdict ?? null;
              const actScore = conv?.actionabilityScore ?? st.actionabilityScore ?? null;
              return (
                <button
                  key={st.ticker}
                  type="button"
                  className="matching-stock-card"
                  onClick={() => onSelectStock(st.ticker)}
                >
                  <div style={{ display: 'flex', alignItems: 'center', gap: '0.45rem', flexWrap: 'wrap' }}>
                    <span className="ticker-badge">{st.ticker}</span>
                    <strong style={{ fontSize: '0.82rem' }}>{st.name}</strong>
                    {actVerdict && (
                      <span
                        className={`chip ${actionVerdictChipClass(actVerdict)}`}
                        style={{ fontSize: '0.64rem' }}
                      >
                        {actVerdict}
                        {actScore != null ? ` ${actScore.toFixed(0)}/100` : ''}
                      </span>
                    )}
                  </div>
                  <div className="mono" style={{ display: 'flex', alignItems: 'center', gap: '0.45rem', fontSize: '0.73rem', color: 'var(--text-secondary)', marginTop: '0.25rem', flexWrap: 'wrap' }}>
                    <span>{st.priceUsd ? `$${st.priceUsd.toFixed(2)}` : '—'}</span>
                    <span>·</span>
                    <span>{st.holderCount} funds ({st.eliteHoldersCount} Elite)</span>
                    {st.entryZoneStatus && (
                      <span className={`chip ${entryZoneChipClass(st.entryZoneStatus)}`} style={{ fontSize: '0.66rem' }}>
                        {st.entryZoneStatus}
                      </span>
                    )}
                    {(ad.status === 'FOMO_TRAP' || ad.status === 'LAG_ADVANTAGE') && (
                      <span className={`chip ${alphaDecayChipClass(ad.status)}`} style={{ fontSize: '0.66rem' }}>
                        {alphaDecayLabel(ad.status)}
                      </span>
                    )}
                  </div>
                </button>
              );
            })}
          </div>
        </div>
      )}

      {/* Excluded cohort disclaimer banner */}
      {tierFilter === 'EXCLUDED' && (
        <div className="insight-callout hedge" style={{ marginBottom: '1.15rem' }}>
          <div className="insight-callout-label">
            <AlertCircle size={14} /> Separated Single-Stock / Ultra-Concentrated Filers ({excludedCount})
          </div>
          <p>
            These filers hold valid SEC 13F equity books (&ge;35% Tech/TMT) but are separated from Top-100 and Elite-50 rankings because their Effective Positions are &lt;4.0 or a single stock exceeds 40% (4Q avg) / 45% (latest). Separating SPVs prevents one-stock VC lockups from distorting skill cohorts.
          </p>
        </div>
      )}

      {/* Main Ranking Table */}
      <div className="panel">
        <div className="panel-header">
          <div className="controls-row" style={{ flexWrap: 'wrap' }}>
            <div className="segmented-group" role="group" aria-label="Fund cohort tier filter">
              {(
                [
                  { id: 'ELITE50', label: `Elite ${eliteCount}` },
                  { id: 'TOP100', label: 'Top 100' },
                  { id: 'ALL', label: `All Eligible (${eligibleCount})` },
                  { id: 'EXCLUDED', label: `Excluded SPVs (${excludedCount})` },
                ] as const
              ).map((t) => (
                <button
                  key={t.id}
                  type="button"
                  id={`tier-btn-${t.id.toLowerCase()}`}
                  className={`seg-btn ${tierFilter === t.id ? 'active' : ''}`}
                  onClick={() => setTierFilter(t.id)}
                >
                  {t.label}
                </button>
              ))}
            </div>

            <div className="segmented-group" role="group" aria-label="Track record horizon filter">
              {(
                [
                  { id: 'ALL', label: 'All Hist' },
                  { id: '8Q', label: '8Q+' },
                  { id: '12Q', label: '12Q+' },
                  { id: '20Q', label: '20Q+' },
                ] as const
              ).map((h) => (
                <button
                  key={h.id}
                  type="button"
                  id={`horizon-btn-${h.id.toLowerCase()}`}
                  className={`seg-btn ${horizonFilter === h.id ? 'active' : ''}`}
                  onClick={() => setHorizonFilter(h.id)}
                  title={`Filter funds with at least ${h.id === 'ALL' ? '4+' : h.id.replace('Q', '')} quarters tracked`}
                >
                  {h.label}
                </button>
              ))}
            </div>

            {staleCount > 0 && latestQuarterCovered && (
              <button
                type="button"
                id="ranking-fresh-toggle"
                className={`action-btn ${freshOnly ? 'primary' : ''}`}
                style={{ padding: '0.36rem 0.78rem', fontSize: '0.78rem' }}
                onClick={() => setFreshOnly((v) => !v)}
                title={`Hide ${staleCount} fund(s) whose latest filing lags ${latestQuarterCovered}`}
              >
                {freshOnly ? `Showing ${latestQuarterCovered} Only` : `Hide Stale (${staleCount})`}
              </button>
            )}

            <div className="segmented-group" role="group" aria-label="Sort ranking metric">
              {(
                [
                  { id: 'skillScore', label: 'Skill Score' },
                  { id: 'return', label: `${windowSize} Return` },
                  { id: 'sortinoRatio', label: 'Sortino' },
                  { id: 'infoRatioVsQqq', label: 'IR vs QQQ' },
                  { id: 'battingAvgPct', label: 'Bat %' },
                  { id: 'equityAumUsd', label: 'Fund Size' },
                ] as const
              ).map((m) => (
                <button
                  key={m.id}
                  type="button"
                  className={`seg-btn ${sortMetric === m.id ? 'active' : ''}`}
                  onClick={() => handleSortMetricClick(m.id)}
                >
                  {m.label}
                </button>
              ))}
              <button
                type="button"
                id="ranking-sort-dir-btn"
                className="seg-btn mono"
                onClick={() => setSortAsc((v) => !v)}
                aria-label={`Toggle sort direction (currently ${sortAsc ? 'ascending' : 'descending'})`}
                title={`Sort order: ${sortAsc ? 'Ascending (lowest first)' : 'Descending (highest first)'}`}
              >
                {sortArrow}
              </button>
            </div>
          </div>

          <div className="controls-row">
            <div className="search-input-wrap">
              <Search size={14} />
              <input
                id="ranking-search-input"
                type="text"
                className="search-input"
                placeholder="Search fund, manager, or stock..."
                value={search}
                onChange={(e) => setSearch(e.target.value)}
              />
            </div>
            <button type="button" className="action-btn" onClick={handleExport}>
              <Download size={14} /> Export CSV
            </button>
          </div>
        </div>

        <div className="table-scroll">
          <table className="data-table">
            <thead>
              <tr>
                <th>#</th>
                <th>Fund / Strategy</th>
                <th
                  className={`num sortable ${sortMetric === 'equityAumUsd' ? 'active-col' : ''}`}
                  onClick={() => handleSortMetricClick('equityAumUsd')}
                  title="Rank by 13F Equity AUM (Total Fund Size in USD)"
                >
                  Total Fund Size {sortMetric === 'equityAumUsd' ? sortArrow : ''}
                </th>
                <th>Top Holdings</th>
                <th className="num">Tech %</th>
                <th
                  className={`num sortable ${sortMetric === 'skillScore' ? 'active-col' : ''}`}
                  onClick={() => handleSortMetricClick('skillScore')}
                  title="Composite Skill Score (0-100): 40% IR vs QQQ + 30% Batting Avg + 30% Sortino"
                >
                  Skill {sortMetric === 'skillScore' ? sortArrow : ''}
                </th>
                <th
                  className={`num sortable ${sortMetric === 'sortinoRatio' ? 'active-col' : ''}`}
                  onClick={() => handleSortMetricClick('sortinoRatio')}
                  title="Sortino Ratio vs trailing 13W T-Bill yield (^IRX)"
                >
                  Sortino {sortMetric === 'sortinoRatio' ? sortArrow : ''}
                </th>
                <th
                  className={`num sortable ${sortMetric === 'infoRatioVsQqq' ? 'active-col' : ''}`}
                  onClick={() => handleSortMetricClick('infoRatioVsQqq')}
                  title="Information Ratio vs QQQ"
                >
                  IR {sortMetric === 'infoRatioVsQqq' ? sortArrow : ''}
                </th>
                <th
                  className={`num sortable ${sortMetric === 'battingAvgPct' ? 'active-col' : ''}`}
                  onClick={() => handleSortMetricClick('battingAvgPct')}
                  title="% of quarters outperforming QQQ"
                >
                  Bat % {sortMetric === 'battingAvgPct' ? sortArrow : ''}
                </th>
                <th className="num" title="Average quarterly portfolio turnover %">
                  Turn %
                </th>
                {RETURN_WINDOWS.map((w) => (
                  <th
                    key={w.id}
                    className={`num sortable ${windowSize === w.id ? 'active-col' : ''}`}
                    onClick={() => handleWindowClick(w.id)}
                    title={`Rank by ${w.fullLabel}`}
                  >
                    {w.label} {sortMetric === 'return' && windowSize === w.id ? sortArrow : ''}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {cohortRows.map(({ fund, displayRank }) => {
                const rets = fund.returns[mode];
                const met = fund.metrics[mode];
                const stale = isFundStale(fund);
                return (
                  <tr key={fund.cik}>
                    <td>
                      {displayRank !== null ? (
                        <span className={`rank-badge ${displayRank <= 3 ? 'top3' : ''}`}>
                          #{displayRank}
                        </span>
                      ) : (
                        <span className="rank-badge">Excl</span>
                      )}
                    </td>

                    <td style={{ minWidth: '225px' }}>
                      <button
                        type="button"
                        className="fund-link-btn"
                        onClick={() => onSelectFund(fund.cik)}
                      >
                        {fund.name}
                        <ArrowUpRight size={14} style={{ color: 'var(--accent-primary)' }} />
                      </button>
                      <div className="fund-meta-line">
                        <span className={`chip ${tierChipClass(fund.fundTier)}`}>
                          {fund.fundTier === 'Elite 50' && <Award size={10} />}
                          {fund.fundTier}
                        </span>
                        {fund.manager && fund.manager !== 'SEC EDGAR Discovered' && (
                          <>
                            <span>{fund.manager}</span>
                            <span>·</span>
                          </>
                        )}
                        <span>{fund.latestQuarterLabel}</span>
                        {stale && (
                          <span
                            className="chip warn"
                            title={
                              latestQuarterCovered
                                ? `Latest filing (${fund.latestQuarterLabel}) lags dataset quarter (${latestQuarterCovered})`
                                : `Stale filing quarter (${fund.latestQuarterLabel})`
                            }
                          >
                            Stale
                          </span>
                        )}
                      </div>
                      {!fund.isLeaderboardEligible && fund.exclusionReason && (
                        <div style={{ fontSize: '0.71rem', color: 'var(--warn-text)', marginTop: '0.2rem' }}>
                          {fund.exclusionReason}
                        </div>
                      )}
                    </td>

                    <td className={`num ${sortMetric === 'equityAumUsd' ? 'active-col' : ''}`}>
                      <div className="mono" style={{ fontWeight: 600 }}>
                        {formatUsdCompact(fund.equityAumUsd)}
                      </div>
                      <div style={{ fontSize: '0.71rem', color: 'var(--text-muted)', fontWeight: 400 }}>
                        {fund.equityPositionCount} stocks · EffN {fund.effectivePositions} · {fund.quartersTracked}Q
                        {fund.etfSharePct > 0 ? ` · ${fund.etfSharePct.toFixed(0)}% ETF stripped` : ''}
                      </div>
                    </td>

                    <td style={{ minWidth: '205px' }}>
                      <div style={{ display: 'flex', gap: '0.28rem', flexWrap: 'wrap' }}>
                        {fund.topHoldingsPreview.slice(0, 3).map((h) =>
                          stocksIndex[h.ticker] ? (
                            <button
                              key={h.ticker}
                              type="button"
                              className="chip clickable"
                              onClick={() => onSelectStock(h.ticker)}
                              title={`Open ${h.ticker} (${h.name}) Stock Dossier`}
                            >
                              <strong className="mono">{h.ticker}</strong>{' '}
                              <span className="mono">{h.weightPct.toFixed(1)}%</span>
                            </button>
                          ) : (
                            <span key={h.ticker} className="chip" title={h.name}>
                              <strong className="mono">{h.ticker}</strong>{' '}
                              <span className="mono">{h.weightPct.toFixed(1)}%</span>
                            </span>
                          )
                        )}
                      </div>
                    </td>

                    <td className="num mono">
                      <span className="chip tech">{formatPct(fund.techTmtPct, 0)}</span>
                    </td>

                    <td className={`num mono ${sortMetric === 'skillScore' ? 'active-col' : ''}`}>
                      {met.skillScore !== null ? (
                        <div>
                          <span className={`chip ${met.skillScore >= 70 ? 'elite' : met.skillScore >= 55 ? 'tech' : ''}`}>
                            <strong>{met.skillScore.toFixed(0)}</strong>
                          </span>
                          {met.skillRank !== null && (
                            <div style={{ fontSize: '0.67rem', color: 'var(--text-muted)', marginTop: '0.1rem' }}>
                              #{met.skillRank}
                            </div>
                          )}
                        </div>
                      ) : (
                        '—'
                      )}
                    </td>

                    <td className={`num mono ${sortMetric === 'sortinoRatio' ? 'active-col' : ''}`}>
                      {met.sortinoRatio !== null ? met.sortinoRatio.toFixed(2) : '—'}
                    </td>

                    <td
                      className={`num mono ${returnClass(met.infoRatioVsQqq)} ${
                        sortMetric === 'infoRatioVsQqq' ? 'active-col' : ''
                      }`}
                    >
                      {met.infoRatioVsQqq !== null ? met.infoRatioVsQqq.toFixed(2) : '—'}
                    </td>

                    <td className={`num mono ${sortMetric === 'battingAvgPct' ? 'active-col' : ''}`}>
                      {met.battingAvgPct !== null ? `${met.battingAvgPct.toFixed(0)}%` : '—'}
                    </td>

                    <td className="num mono" style={{ color: 'var(--text-secondary)' }}>
                      {met.avgTurnoverPct !== null ? `${met.avgTurnoverPct.toFixed(0)}%` : '—'}
                    </td>

                    {RETURN_WINDOWS.map((w) => {
                      const val = rets[w.id];
                      const active = windowSize === w.id;
                      return (
                        <td
                          key={w.id}
                          className={`num mono ${returnClass(val)} ${active ? 'active-col' : ''}`}
                        >
                          {formatSignedPct(val, 1)}
                        </td>
                      );
                    })}
                  </tr>
                );
              })}
              {cohortRows.length === 0 && (
                <tr>
                  <td colSpan={16} style={{ textAlign: 'center', padding: '2rem', color: 'var(--text-muted)' }}>
                    No funds match &ldquo;{search}&rdquo; in this cohort. Try switching to All Eligible or clearing search.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </section>
  );
};
