import { useMemo, useState, type FC } from 'react';
import { ArrowUpRight, Download, Search } from 'lucide-react';
import type { BenchmarkSeries, FundSummary, ReturnMode, ReturnWindow } from '../types/data';
import {
  downloadCsv,
  formatPct,
  formatSignedPct,
  formatUsdCompact,
  RETURN_WINDOWS,
  returnClass,
} from '../utils/format';
import { MiniSparkline } from './SvgCharts';

interface RankingTabProps {
  funds: FundSummary[];
  rankings: Record<ReturnMode, Record<ReturnWindow, Array<{ rank: number; cik: string; returnPct: number }>>>;
  benchmarks: Record<'QQQ' | 'SPY' | 'XLK', BenchmarkSeries>;
  windowSize: ReturnWindow;
  onWindowChange: (w: ReturnWindow) => void;
  mode: ReturnMode;
  onModeChange: (m: ReturnMode) => void;
  onSelectFund: (cik: string) => void;
}

export const RankingTab: FC<RankingTabProps> = ({
  funds,
  rankings,
  benchmarks,
  windowSize,
  onWindowChange,
  mode,
  onModeChange,
  onSelectFund,
}) => {
  const [showAll, setShowAll] = useState(false);
  const [search, setSearch] = useState('');

  const fundByCik = useMemo(() => new Map(funds.map((f) => [f.cik, f])), [funds]);

  const rankedRows = useMemo(() => {
    const q = search.trim().toLowerCase();
    const top10Map = new Map(rankings[mode][windowSize].map((r) => [r.cik, r.rank]));

    const eligible = funds
      .filter((f) => f.returns[mode][windowSize] !== null)
      .sort((a, b) => (b.returns[mode][windowSize] ?? -9999) - (a.returns[mode][windowSize] ?? -9999));

    const noWindowFunds = funds.filter((f) => f.returns[mode][windowSize] === null);
    const ordered = showAll ? [...eligible, ...noWindowFunds] : eligible.slice(0, 10);

    return ordered
      .filter((f) => {
        if (!q) return true;
        return (
          f.name.toLowerCase().includes(q) ||
          f.manager.toLowerCase().includes(q) ||
          f.styleTag.toLowerCase().includes(q) ||
          f.topHoldingsPreview.some((h) => h.ticker.toLowerCase().includes(q))
        );
      })
      .map((f, idx) => ({
        fund: f,
        displayRank: top10Map.get(f.cik) ?? (f.returns[mode][windowSize] !== null ? idx + 1 : null),
        isTop10: top10Map.has(f.cik),
      }));
  }, [funds, rankings, mode, windowSize, showAll, search]);

  const top10Ids = rankings[mode][windowSize];
  const leaderFund = top10Ids[0] ? fundByCik.get(top10Ids[0].cik) : undefined;
  const top10AvgReturn = useMemo(() => {
    if (!top10Ids.length) return null;
    return top10Ids.reduce((s, x) => s + x.returnPct, 0) / top10Ids.length;
  }, [top10Ids]);
  const top10CombinedAum = useMemo(() => {
    return top10Ids.reduce((s, x) => s + (fundByCik.get(x.cik)?.equityAumUsd ?? 0), 0);
  }, [top10Ids, fundByCik]);

  const qqqRet = benchmarks.QQQ.returns[windowSize];
  const spyRet = benchmarks.SPY.returns[windowSize];
  const xlkRet = benchmarks.XLK.returns[windowSize];

  const handleExport = () => {
    downloadCsv(
      `techwhale-ranking-${windowSize}-${mode}.csv`,
      ['Rank', 'Fund Name', 'Manager', '13F Equity AUM (USD)', 'Positions', 'Top 10 %', 'Tech/TMT %', 'YTD %', 'TTM %', '18M %', '2Y %', '3Y %', '5Y %', 'Latest Period'],
      rankedRows.map(({ fund, displayRank }) => [
        displayRank ?? 'N/A',
        fund.name,
        fund.manager,
        fund.equityAumUsd,
        fund.equityPositionCount,
        fund.top10WeightPct,
        fund.techTmtPct,
        fund.returns[mode].YTD ?? '',
        fund.returns[mode].TTM ?? '',
        fund.returns[mode]['18M'] ?? '',
        fund.returns[mode]['2Y'] ?? '',
        fund.returns[mode]['3Y'] ?? '',
        fund.returns[mode]['5Y'] ?? '',
        fund.latestPeriod,
      ])
    );
  };

  return (
    <section aria-label="Fund Rankings">
      <div className="section-toolbar">
        <div className="section-title-group">
          <h2>Top {showAll ? funds.length : 10} Concentrated Tech Funds — {windowSize} Performance</h2>
          <p>
            Click any fund name to inspect complete 13F holdings, strategy overview, and multi-year return curves.
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
                onClick={() => onWindowChange(w.id)}
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

      {/* Benchmark & Top 10 KPI Strip */}
      <div className="metrics-strip">
        <div className="metric-card">
          <div className="metric-label">#1 Ranked ({windowSize})</div>
          <div className="metric-value">
            <span className={returnClass(top10Ids[0]?.returnPct)}>
              {formatSignedPct(top10Ids[0]?.returnPct)}
            </span>
          </div>
          <div className="metric-sub">
            {leaderFund ? (
              <button
                type="button"
                className="fund-link-btn"
                style={{ fontSize: '0.78rem' }}
                onClick={() => onSelectFund(leaderFund.cik)}
              >
                {leaderFund.name} <ArrowUpRight size={12} />
              </button>
            ) : '—'}
          </div>
        </div>

        <div className="metric-card">
          <div className="metric-label">Top 10 Mean ({windowSize})</div>
          <div className="metric-value">
            <span className={returnClass(top10AvgReturn)}>{formatSignedPct(top10AvgReturn)}</span>
          </div>
          <div className="metric-sub">
            vs. QQQ ({formatSignedPct(qqqRet)}) · Alpha{' '}
            <span className={`mono ${returnClass(top10AvgReturn !== null && qqqRet !== null ? top10AvgReturn - qqqRet : null)}`}>
              {top10AvgReturn !== null && qqqRet !== null ? formatSignedPct(top10AvgReturn - qqqRet) : '—'}
            </span>
          </div>
        </div>

        <div className="metric-card">
          <div className="metric-label">Top 10 Combined 13F AUM</div>
          <div className="metric-value">{formatUsdCompact(top10CombinedAum)}</div>
          <div className="metric-sub">Disclosed U.S. long equity value</div>
        </div>

        <div className="metric-card">
          <div className="metric-label">Benchmarks ({windowSize})</div>
          <div className="metric-value" style={{ fontSize: '1rem', gap: '0.8rem', flexWrap: 'wrap' }}>
            <span>QQQ <strong className={returnClass(qqqRet)}>{formatSignedPct(qqqRet)}</strong></span>
            <span>XLK <strong className={returnClass(xlkRet)}>{formatSignedPct(xlkRet)}</strong></span>
            <span>SPY <strong className={returnClass(spyRet)}>{formatSignedPct(spyRet)}</strong></span>
          </div>
          <div className="metric-sub">Total return benchmarks</div>
        </div>
      </div>

      {/* Main Ranking Table */}
      <div className="panel">
        <div className="panel-header">
          <div>
            <h3 className="panel-title">
              {showAll ? `All Qualified Tech-Focused Funds (${rankedRows.length})` : `Top 10 Ranked Funds (${windowSize})`}
            </h3>
            <p className="panel-subtitle">
              Filtered for ≥45% Tech/TMT allocation and high portfolio concentration · Ranked by {windowSize} ({mode === 'quarter_end' ? 'Quarter-End Attribution' : '45-Day Lag Actionable'})
            </p>
          </div>
          <div className="controls-row">
            <div className="search-input-wrap">
              <Search size={14} />
              <input
                type="text"
                className="search-input"
                placeholder="Search fund, manager, or stock..."
                value={search}
                onChange={(e) => setSearch(e.target.value)}
              />
            </div>
            <button
              type="button"
              id="toggle-show-all-funds"
              className={`action-btn ${showAll ? 'primary' : ''}`}
              onClick={() => setShowAll(!showAll)}
            >
              {showAll ? 'Show Top 10 Only' : `View All ${funds.length} Funds`}
            </button>
            <button type="button" className="action-btn" onClick={handleExport}>
              <Download size={14} /> Export CSV
            </button>
          </div>
        </div>

        <div className="table-scroll">
          <table className="data-table">
            <thead>
              <tr>
                <th>Rank</th>
                <th>Fund / Strategy</th>
                <th className="num">Total Fund Size</th>
                <th>Top Holdings</th>
                <th className="num">Tech %</th>
                {RETURN_WINDOWS.map((w) => (
                  <th
                    key={w.id}
                    className={`num sortable ${windowSize === w.id ? 'active-col' : ''}`}
                    onClick={() => onWindowChange(w.id)}
                    title={`Rank by ${w.fullLabel}`}
                  >
                    {w.label} {windowSize === w.id ? '▼' : ''}
                  </th>
                ))}
                <th>1Y Curve</th>
              </tr>
            </thead>
            <tbody>
              {rankedRows.map(({ fund, displayRank, isTop10 }) => {
                const rets = fund.returns[mode];
                const curvePts = fund.equityCurve[mode];
                const ttmPos = (rets.TTM ?? rets.YTD ?? 0) >= 0;
                return (
                  <tr key={fund.cik}>
                    <td>
                      {displayRank !== null ? (
                        <span className={`rank-badge ${displayRank <= 3 ? 'top3' : ''}`}>
                          #{displayRank}
                        </span>
                      ) : (
                        <span className="rank-badge">N/A</span>
                      )}
                    </td>

                    <td style={{ minWidth: '210px' }}>
                      <button
                        type="button"
                        className="fund-link-btn"
                        onClick={() => onSelectFund(fund.cik)}
                      >
                        {fund.name}
                        <ArrowUpRight size={14} style={{ color: 'var(--accent-primary)' }} />
                      </button>
                      <div className="fund-meta-line">
                        <span>{fund.manager}</span>
                        <span>·</span>
                        <span>{fund.latestQuarterLabel}</span>
                        {fund.source === 'EDGAR Discovered' && (
                          <span className="chip discovered">Discovered</span>
                        )}
                        {!isTop10 && showAll && displayRank !== null && (
                          <span className="chip">Outside Top 10</span>
                        )}
                      </div>
                    </td>

                    <td className="num">
                      <div className="mono" style={{ fontWeight: 600 }}>
                        {formatUsdCompact(fund.equityAumUsd)}
                      </div>
                      <div style={{ fontSize: '0.72rem', color: 'var(--text-muted)' }}>
                        {fund.equityPositionCount} stocks · Top 10: {fund.top10WeightPct.toFixed(0)}%
                      </div>
                    </td>

                    <td style={{ minWidth: '195px' }}>
                      <div style={{ display: 'flex', gap: '0.3rem', flexWrap: 'wrap' }}>
                        {fund.topHoldingsPreview.slice(0, 3).map((h) => (
                          <span key={h.ticker} className="chip" title={h.name}>
                            <strong className="mono">{h.ticker}</strong>{' '}
                            <span className="mono">{h.weightPct.toFixed(1)}%</span>
                          </span>
                        ))}
                      </div>
                    </td>

                    <td className="num mono">
                      <span className="chip tech">{formatPct(fund.techTmtPct, 0)}</span>
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

                    <td>
                      <MiniSparkline points={curvePts} positive={ttmPos} />
                    </td>
                  </tr>
                );
              })}
              {rankedRows.length === 0 && (
                <tr>
                  <td colSpan={12} style={{ textAlign: 'center', padding: '2rem', color: 'var(--text-muted)' }}>
                    No concentrated tech funds match &ldquo;{search}&rdquo;. Try clearing the search filter.
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
