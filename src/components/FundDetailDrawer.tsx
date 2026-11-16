import { useMemo, useState, type FC } from 'react';
import { Download, ExternalLink, Search, X } from 'lucide-react';
import type { BenchmarkSeries, FundHolding, FundSummary, ReturnMode } from '../types/data';
import {
  downloadCsv,
  formatPct,
  formatShares,
  formatSignedPct,
  formatUsdCompact,
  returnClass,
} from '../utils/format';
import { EquityCurveChart, SectorBreakdownBar } from './SvgCharts';

interface FundDetailDrawerProps {
  fund: FundSummary;
  mode: ReturnMode;
  benchmarks: Record<'QQQ' | 'SPY' | 'XLK', BenchmarkSeries>;
  onClose: () => void;
}

type HoldingFilter = 'ALL' | 'EQUITY' | 'OPTIONS' | 'INCREASED';
type HoldingSort = 'weightPct' | 'valueUsd' | 'qoqSharesChangePct';

export const FundDetailDrawer: FC<FundDetailDrawerProps> = ({
  fund,
  mode,
  benchmarks,
  onClose,
}) => {
  const [search, setSearch] = useState('');
  const [filter, setFilter] = useState<HoldingFilter>('ALL');
  const [sortBy, setSortBy] = useState<HoldingSort>('weightPct');
  const [sortAsc, setSortAsc] = useState(false);

  const activeMetrics = fund.metrics[mode];
  const activeReturns = fund.returns[mode];

  const filteredHoldings = useMemo(() => {
    const q = search.trim().toLowerCase();
    return fund.holdings
      .filter((h) => {
        if (filter === 'EQUITY' && h.securityType !== 'EQUITY') return false;
        if (filter === 'OPTIONS' && h.securityType === 'EQUITY') return false;
        if (filter === 'INCREASED' && h.status !== 'NEW' && h.status !== 'ADDED') return false;
        if (!q) return true;
        return (
          h.ticker.toLowerCase().includes(q) ||
          h.name.toLowerCase().includes(q) ||
          h.sector.toLowerCase().includes(q) ||
          h.cusip.toLowerCase().includes(q)
        );
      })
      .sort((a, b) => {
        const av = (a[sortBy] ?? -9999) as number;
        const bv = (b[sortBy] ?? -9999) as number;
        return sortAsc ? av - bv : bv - av;
      });
  }, [fund.holdings, search, filter, sortBy, sortAsc]);

  const toggleSort = (col: HoldingSort) => {
    if (sortBy === col) {
      setSortAsc(!sortAsc);
    } else {
      setSortBy(col);
      setSortAsc(false);
    }
  };

  const handleExport = () => {
    downloadCsv(
      `${fund.name.toLowerCase().replace(/[^a-z0-9]+/g, '-')}-${fund.latestPeriod}-13f.csv`,
      ['Ticker', 'Company', 'CUSIP', 'Type', 'Sector', 'Shares', 'Value (USD)', 'Equity Weight (%)', 'QoQ Shares Change (%)', 'Status'],
      fund.holdings.map((h: FundHolding) => [
        h.ticker,
        h.name,
        h.cusip,
        h.securityType,
        h.sector,
        h.shares,
        h.valueUsd,
        h.weightPct,
        h.qoqSharesChangePct ?? '',
        h.status,
      ])
    );
  };

  return (
    <div
      className="drawer-backdrop"
      role="dialog"
      aria-modal="true"
      aria-labelledby="fund-drawer-title"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="drawer-panel">
        <div className="drawer-header">
          <div>
            <div style={{ display: 'flex', alignItems: 'center', gap: '0.55rem', flexWrap: 'wrap' }}>
              <h2 id="fund-drawer-title" style={{ margin: 0, fontSize: '1.35rem', fontWeight: 700 }}>
                {fund.name}
              </h2>
              <span className="chip tech">{fund.styleTag}</span>
              {fund.source === 'EDGAR Discovered' && (
                <span className="chip discovered">EDGAR Discovered</span>
              )}
            </div>
            <div style={{ fontSize: '0.82rem', color: 'var(--text-secondary)', marginTop: '0.3rem' }}>
              <strong>{fund.legalName}</strong> · Manager: {fund.manager} · {fund.hq} ·{' '}
              <a
                href={fund.edgarUrl}
                target="_blank"
                rel="noreferrer"
                style={{ color: 'var(--accent-primary)', textDecoration: 'none', fontWeight: 500, display: 'inline-flex', alignItems: 'center', gap: '0.2rem' }}
              >
                SEC CIK {fund.cik} <ExternalLink size={12} />
              </a>
            </div>
            <p style={{ margin: '0.45rem 0 0', fontSize: '0.83rem', color: 'var(--text-secondary)', maxWidth: '720px' }}>
              {fund.strategySummary}
            </p>
          </div>
          <button type="button" className="close-btn" onClick={onClose} aria-label="Close fund details">
            <X size={18} />
          </button>
        </div>

        <div className="drawer-body">
          {/* Fund Overview KPIs */}
          <div className="metrics-strip">
            <div className="metric-card">
              <div className="metric-label">13F Equity AUM</div>
              <div className="metric-value">{formatUsdCompact(fund.equityAumUsd)}</div>
              <div className="metric-sub">
                Total w/ options: {formatUsdCompact(fund.totalReportedUsd)}
              </div>
            </div>
            <div className="metric-card">
              <div className="metric-label">Latest Filing</div>
              <div className="metric-value">{fund.latestQuarterLabel}</div>
              <div className="metric-sub">
                Period {fund.latestPeriod} · Filed {fund.latestFilingDate}
              </div>
            </div>
            <div className="metric-card">
              <div className="metric-label">Concentration</div>
              <div className="metric-value">{formatPct(fund.top10WeightPct, 1)}</div>
              <div className="metric-sub">
                Top 10 weight · {fund.equityPositionCount} stocks ({fund.effectivePositions} eff.)
              </div>
            </div>
            <div className="metric-card">
              <div className="metric-label">Tech / TMT Share</div>
              <div className="metric-value">{formatPct(fund.techTmtPct, 1)}</div>
              <div className="metric-sub">
                {fund.quartersTracked} quarterly 13Fs tracked
              </div>
            </div>
          </div>

          {/* Historic Returns Strip */}
          <div className="panel">
            <div className="panel-header">
              <div>
                <h3 className="panel-title">Past Performance & Risk Profile</h3>
                <p className="panel-subtitle">
                  13F-reconstructed long equity returns ({mode === 'quarter_end' ? 'Quarter-End Attribution' : '45-Day Filing-Lag Actionable'}) vs. QQQ & SPY
                </p>
              </div>
              <div className="controls-row mono" style={{ fontSize: '0.78rem' }}>
                <span>CAGR: <strong>{formatSignedPct(activeMetrics.cagrPct)}</strong></span>
                <span>Sharpe: <strong>{activeMetrics.sharpeProxy ?? '—'}</strong></span>
                <span>Beta: <strong>{activeMetrics.betaVsQqq ?? '—'}</strong></span>
                <span className="ret-neg">Max DD: <strong>{formatSignedPct(activeMetrics.maxDrawdownPct)}</strong></span>
              </div>
            </div>

            <div style={{ padding: '1rem 1.2rem' }}>
              <div
                style={{
                  display: 'grid',
                  gridTemplateColumns: 'repeat(auto-fit, minmax(105px, 1fr))',
                  gap: '0.6rem',
                  marginBottom: '1rem',
                }}
              >
                {(['YTD', 'TTM', '18M', '2Y', '3Y', '5Y'] as const).map((w) => {
                  const val = activeReturns[w];
                  const qqqVal = benchmarks.QQQ.returns[w];
                  const spread = val !== null && qqqVal !== null ? val - qqqVal : null;
                  return (
                    <div
                      key={w}
                      style={{
                        padding: '0.6rem 0.75rem',
                        border: '1px solid var(--border-subtle)',
                        borderRadius: '8px',
                        background: 'var(--bg-surface-subtle)',
                      }}
                    >
                      <div style={{ fontSize: '0.7rem', fontWeight: 600, color: 'var(--text-muted)' }}>{w}</div>
                      <div className={`mono ${returnClass(val)}`} style={{ fontSize: '1.05rem', fontWeight: 700, marginTop: '0.15rem' }}>
                        {formatSignedPct(val)}
                      </div>
                      <div style={{ fontSize: '0.69rem', color: 'var(--text-muted)', marginTop: '0.1rem' }}>
                        vs QQQ: <span className={`mono ${returnClass(spread)}`}>{formatSignedPct(spread)}</span>
                      </div>
                    </div>
                  );
                })}
              </div>

              <EquityCurveChart
                defaultWindow="3Y"
                series={[
                  {
                    id: 'qe',
                    label: `${fund.name} (Quarter-End)`,
                    color: '#1d4ed8',
                    points: fund.equityCurve.quarter_end,
                  },
                  {
                    id: 'lag',
                    label: '45-Day Lag Copycat',
                    color: '#0d9488',
                    dashed: true,
                    points: fund.equityCurve.filing_lag,
                  },
                  {
                    id: 'qqq',
                    label: 'QQQ',
                    color: '#64748b',
                    dashed: true,
                    points: benchmarks.QQQ.curve,
                  },
                  {
                    id: 'spy',
                    label: 'SPY',
                    color: '#94a3b8',
                    dashed: true,
                    points: benchmarks.SPY.curve,
                  },
                ]}
              />
            </div>
          </div>

          {/* Sector Exposure */}
          <div className="panel">
            <div className="panel-header">
              <h3 className="panel-title">Sector Allocation ({fund.latestQuarterLabel})</h3>
            </div>
            <div style={{ padding: '1rem 1.2rem' }}>
              <SectorBreakdownBar sectors={fund.sectorBreakdown} />
            </div>
          </div>

          {/* Total Holdings Table */}
          <div className="panel">
            <div className="panel-header">
              <div>
                <h3 className="panel-title">Total 13F Holdings ({filteredHoldings.length} of {fund.holdings.length})</h3>
                <p className="panel-subtitle">Complete disclosed positions from {fund.latestQuarterLabel} Form 13F-HR</p>
              </div>
              <div className="controls-row">
                <div className="search-input-wrap">
                  <Search size={14} />
                  <input
                    type="text"
                    className="search-input"
                    placeholder="Filter ticker, company, CUSIP..."
                    value={search}
                    onChange={(e) => setSearch(e.target.value)}
                  />
                </div>
                <div className="segmented-group">
                  {([
                    { id: 'ALL', label: 'All' },
                    { id: 'EQUITY', label: 'Equities' },
                    { id: 'INCREASED', label: 'New / Added' },
                    { id: 'OPTIONS', label: 'Options' },
                  ] as const).map((f) => (
                    <button
                      key={f.id}
                      type="button"
                      className={`seg-btn ${filter === f.id ? 'active' : ''}`}
                      onClick={() => setFilter(f.id)}
                    >
                      {f.label}
                    </button>
                  ))}
                </div>
                <button type="button" className="action-btn" onClick={handleExport}>
                  <Download size={14} /> CSV
                </button>
              </div>
            </div>

            <div className="table-scroll" style={{ maxHeight: '440px', overflowY: 'auto' }}>
              <table className="data-table">
                <thead>
                  <tr>
                    <th>#</th>
                    <th>Ticker / Issuer</th>
                    <th>Sector</th>
                    <th>Type</th>
                    <th className="num">Shares</th>
                    <th className="num sortable" onClick={() => toggleSort('valueUsd')}>
                      Value {sortBy === 'valueUsd' ? (sortAsc ? '↑' : '↓') : ''}
                    </th>
                    <th className="num sortable" onClick={() => toggleSort('weightPct')}>
                      Weight {sortBy === 'weightPct' ? (sortAsc ? '↑' : '↓') : ''}
                    </th>
                    <th className="num sortable" onClick={() => toggleSort('qoqSharesChangePct')}>
                      QoQ Shares {sortBy === 'qoqSharesChangePct' ? (sortAsc ? '↑' : '↓') : ''}
                    </th>
                    <th>Action</th>
                  </tr>
                </thead>
                <tbody>
                  {filteredHoldings.map((h, i) => (
                    <tr key={`${h.cusip}-${h.securityType}`}>
                      <td className="mono" style={{ color: 'var(--text-muted)' }}>{i + 1}</td>
                      <td>
                        <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
                          <span className="ticker-badge">{h.ticker}</span>
                          <span style={{ fontWeight: 500 }}>{h.name}</span>
                        </div>
                        <div className="mono" style={{ fontSize: '0.7rem', color: 'var(--text-muted)', marginTop: '0.12rem' }}>
                          CUSIP {h.cusip} {h.pricePerShare ? `· $${h.pricePerShare.toFixed(2)}` : ''}
                        </div>
                      </td>
                      <td>
                        <span className="chip">{h.sector}</span>
                      </td>
                      <td>
                        <span className={`chip ${h.securityType === 'EQUITY' ? '' : 'warn'}`}>
                          {h.securityType}
                        </span>
                      </td>
                      <td className="num mono">{formatShares(h.shares)}</td>
                      <td className="num mono">{formatUsdCompact(h.valueUsd)}</td>
                      <td className="num mono">
                        <div className="weight-cell">
                          <span>{h.securityType === 'EQUITY' ? formatPct(h.weightPct, 2) : `${h.reportedWeightPct.toFixed(2)}%*`}</span>
                          {h.securityType === 'EQUITY' && (
                            <div className="bar-track" style={{ width: '56px' }}>
                              <div className="bar-fill" style={{ width: `${Math.min(100, h.weightPct * 4)}%` }} />
                            </div>
                          )}
                        </div>
                      </td>
                      <td className={`num mono ${returnClass(h.qoqSharesChangePct)}`}>
                        {h.status === 'NEW' ? 'NEW' : formatSignedPct(h.qoqSharesChangePct, 1)}
                      </td>
                      <td>
                        <span
                          className={`chip ${
                            h.status === 'NEW' || h.status === 'ADDED'
                              ? 'pos'
                              : h.status === 'TRIMMED'
                                ? 'neg'
                                : ''
                          }`}
                        >
                          {h.status}
                        </span>
                      </td>
                    </tr>
                  ))}
                  {filteredHoldings.length === 0 && (
                    <tr>
                      <td colSpan={9} style={{ textAlign: 'center', padding: '1.75rem', color: 'var(--text-muted)' }}>
                        No positions match the current filter criteria.
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </div>

          {/* Quarterly Filing Return History */}
          <div className="panel">
            <div className="panel-header">
              <h3 className="panel-title">Quarterly Filing Snapshots ({fund.quarterlyHistory.length})</h3>
            </div>
            <div className="table-scroll" style={{ maxHeight: '280px', overflowY: 'auto' }}>
              <table className="data-table">
                <thead>
                  <tr>
                    <th>Quarter</th>
                    <th>Period End</th>
                    <th>Filed Date</th>
                    <th className="num">Equity AUM</th>
                    <th className="num">Positions</th>
                    <th className="num">Top 10 %</th>
                    <th className="num">Tech/TMT %</th>
                    <th className="num">Est. Quarter Ret</th>
                    <th className="num">QQQ Quarter</th>
                  </tr>
                </thead>
                <tbody>
                  {fund.quarterlyHistory.map((q) => (
                    <tr key={q.periodEnd}>
                      <td style={{ fontWeight: 600 }}>{q.quarter}</td>
                      <td className="mono">{q.periodEnd}</td>
                      <td className="mono">{q.filingDate}</td>
                      <td className="num mono">{formatUsdCompact(q.equityAumUsd)}</td>
                      <td className="num mono">{q.positionCount}</td>
                      <td className="num mono">{formatPct(q.top10WeightPct, 1)}</td>
                      <td className="num mono">{formatPct(q.techTmtPct, 1)}</td>
                      <td className={`num mono ${returnClass(q.quarterReturnPct)}`}>
                        {formatSignedPct(q.quarterReturnPct, 1)}
                      </td>
                      <td className={`num mono ${returnClass(q.qqqQuarterReturnPct)}`}>
                        {formatSignedPct(q.qqqQuarterReturnPct, 1)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
};
