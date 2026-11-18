import { useEffect, useMemo, useState, type FC } from 'react';
import {
  AlertTriangle,
  ArrowDown,
  Download,
  ExternalLink,
  Radio,
  Search,
  X,
} from 'lucide-react';
import type {
  BenchmarkSeries,
  FundHolding,
  FundShardDetail,
  FundSummary,
  FundUpdateSummary,
  ReturnMode,
  StockIndexEntry,
} from '../types/data';
import { fetchFundShard } from '../utils/dataLoader';
import {
  downloadCsv,
  formatPct,
  formatShares,
  formatSignedPct,
  formatUsdCompact,
  returnClass,
  tierChipClass,
} from '../utils/format';
import { EquityCurveChart, SectorBreakdownBar } from './SvgCharts';
import { UpdateSummaryContent } from './UpdateSummaryContent';

interface FundDetailDrawerProps {
  fund: FundSummary;
  fundUpdate: FundUpdateSummary | null;
  mode: ReturnMode;
  benchmarks: Record<'QQQ' | 'SPY' | 'XLK', BenchmarkSeries>;
  stocksIndex: Record<string, StockIndexEntry>;
  onClose: () => void;
  onDiveDeeper: (cik: string) => void;
  onSelectStock: (ticker: string) => void;
}

type HoldingFilter = 'ALL' | 'EQUITY' | 'OPTIONS' | 'INCREASED';
type HoldingSort = 'weightPct' | 'valueUsd' | 'qoqSharesChangePct';

export const FundDetailDrawer: FC<FundDetailDrawerProps> = ({
  fund,
  fundUpdate,
  mode,
  benchmarks,
  stocksIndex,
  onClose,
  onDiveDeeper,
  onSelectStock,
}) => {
  const [search, setSearch] = useState('');
  const [filter, setFilter] = useState<HoldingFilter>('ALL');
  const [sortBy, setSortBy] = useState<HoldingSort>('weightPct');
  const [sortAsc, setSortAsc] = useState(false);

  const [retrySeq, setRetrySeq] = useState(0);
  const requestKey = `${fund.shard}:${fund.cik}:${retrySeq}`;
  const [shardState, setShardState] = useState<{
    key: string;
    detail: FundShardDetail | null;
    error: string | null;
  } | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetchFundShard(fund.shard)
      .then((payload) => {
        if (!cancelled) {
          setShardState({
            key: requestKey,
            detail: payload.funds[fund.cik] ?? null,
            error: null,
          });
        }
      })
      .catch((err) => {
        if (!cancelled) {
          setShardState({
            key: requestKey,
            detail: null,
            error: err instanceof Error ? err.message : 'Unable to load fund shard',
          });
        }
      });
    return () => {
      cancelled = true;
    };
  }, [requestKey, fund.cik, fund.shard]);

  const shardLoading = shardState?.key !== requestKey;
  const shardError = shardState?.key === requestKey ? shardState.error : null;
  const shardDetail = shardState?.key === requestKey ? shardState.detail : null;

  const activeMetrics = fund.metrics[mode];
  const activeReturns = fund.returns[mode];
  const holdings = useMemo(() => shardDetail?.holdings ?? [], [shardDetail]);
  const quarterlyHistory = shardDetail?.quarterlyHistory ?? [];
  const midQuarterAlerts = shardDetail?.midQuarterAlerts ?? [];

  const scrollToUpdates = () => {
    document.getElementById('fund-drawer-updates')?.scrollIntoView({
      behavior: 'smooth',
      block: 'start',
    });
  };

  const filteredHoldings = useMemo(() => {
    const q = search.trim().toLowerCase();
    return holdings
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
  }, [holdings, search, filter, sortBy, sortAsc]);

  const toggleSort = (col: HoldingSort) => {
    if (sortBy === col) {
      setSortAsc(!sortAsc);
    } else {
      setSortBy(col);
      setSortAsc(false);
    }
  };

  const handleExport = () => {
    if (!holdings.length) return;
    downloadCsv(
      `${fund.name.toLowerCase().replace(/[^a-z0-9]+/g, '-')}-${fund.latestPeriod}-13f.csv`,
      ['Ticker', 'Company', 'CUSIP', 'Type', 'Sector', 'Shares', 'Value (USD)', 'Equity Weight (%)', 'QoQ Shares Change (%)', 'Status'],
      holdings.map((h: FundHolding) => [
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
              <span className={`chip ${tierChipClass(fund.fundTier)}`}>{fund.fundTier}</span>
              <span className="chip tech">{fund.styleTag}</span>
              {fund.source === 'EDGAR Discovered' && (
                <span className="chip discovered">EDGAR Discovered</span>
              )}
              {fundUpdate && (
                <button
                  type="button"
                  id="jump-to-fund-updates"
                  className="chip clickable"
                  onClick={scrollToUpdates}
                  title={`Jump to ${fundUpdate.prevQuarterLabel} → ${fundUpdate.quarterLabel} changes`}
                >
                  {fundUpdate.quarterLabel} Updates <ArrowDown size={11} />
                </button>
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
          {!fund.isLeaderboardEligible && fund.exclusionReason && (
            <div className="insight-callout hedge" style={{ marginBottom: '1rem' }}>
              <div className="insight-callout-label">
                <AlertTriangle size={14} /> Excluded from Top-100 Ranking Cohort
              </div>
              <p>
                {fund.exclusionReason}. Retained in Explorer for transparency and 13F research.
              </p>
            </div>
          )}

          {/* Fund Overview KPIs */}
          <div className="metrics-strip">
            <div className="metric-card">
              <div className="metric-label">13F Single-Stock Eq. AUM</div>
              <div className="metric-value">{formatUsdCompact(fund.equityAumUsd)}</div>
              <div className="metric-sub">
                Total reported: {formatUsdCompact(fund.totalReportedUsd)}
                {fund.etfSharePct > 0 ? ` (${fund.etfSharePct.toFixed(1)}% ETFs stripped)` : ''}
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
              <div className="metric-label">Skill &amp; Tech Share</div>
              <div className="metric-value">
                <span className="mono">{activeMetrics.skillScore != null ? `${activeMetrics.skillScore.toFixed(0)}/100` : '—'}</span>
                {activeMetrics.skillRank != null && (
                  <span className="chip elite" style={{ fontSize: '0.72rem' }}>
                    Skill #{activeMetrics.skillRank}
                  </span>
                )}
              </div>
              <div className="metric-sub">
                Tech/TMT {formatPct(fund.techTmtPct, 1)} · {fund.quartersTracked} quarters tracked
              </div>
            </div>
          </div>

          {/* Historic Returns Strip */}
          <div className="panel">
            <div className="panel-header">
              <div>
                <h3 className="panel-title">Past Performance, Skill &amp; Risk Profile</h3>
                <p className="panel-subtitle">
                  13F-reconstructed long equity returns ({mode === 'quarter_end' ? 'Quarter-End Attribution' : '45-Day Filing-Lag Actionable'}) vs. QQQ &amp; SPY
                </p>
              </div>
              <div className="controls-row mono" style={{ fontSize: '0.78rem', gap: '0.65rem' }}>
                <span>CAGR: <strong>{formatSignedPct(activeMetrics.cagrPct)}</strong></span>
                <span>Sharpe: <strong>{activeMetrics.sharpeProxy ?? '—'}</strong></span>
                <span>Sortino: <strong>{activeMetrics.sortinoRatio ?? '—'}</strong></span>
                <span>IR vs QQQ: <strong>{activeMetrics.infoRatioVsQqq ?? '—'}</strong></span>
                <span>Bat%: <strong>{activeMetrics.battingAvgPct != null ? `${activeMetrics.battingAvgPct.toFixed(0)}%` : '—'}</strong></span>
                <span>Turn: <strong>{activeMetrics.avgTurnoverPct != null ? `${activeMetrics.avgTurnoverPct.toFixed(0)}%` : '—'}</strong></span>
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

              {shardError ? (
                <div style={{ padding: '1rem', color: 'var(--neg-text)', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                  <span>Failed to load historical equity curve: {shardError}</span>
                  <button type="button" className="action-btn" onClick={() => setRetrySeq((r) => r + 1)}>
                    Retry
                  </button>
                </div>
              ) : shardLoading || !shardDetail ? (
                <div style={{ padding: '1.75rem', textAlign: 'center', color: 'var(--text-muted)', fontSize: '0.84rem' }}>
                  Loading multi-year equity curve from shard <code>funds_{fund.shard}.json</code>...
                </div>
              ) : (
                <EquityCurveChart
                  defaultWindow="3Y"
                  series={[
                    {
                      id: 'qe',
                      label: `${fund.name} (Quarter-End)`,
                      color: 'var(--accent-primary)',
                      points: shardDetail.equityCurve.quarter_end,
                    },
                    {
                      id: 'lag',
                      label: '45-Day Lag Copycat',
                      color: 'var(--chart-teal)',
                      dashed: true,
                      points: shardDetail.equityCurve.filing_lag,
                    },
                    {
                      id: 'qqq',
                      label: 'QQQ',
                      color: 'var(--chart-indigo)',
                      dashed: true,
                      points: benchmarks.QQQ.curve,
                    },
                    {
                      id: 'spy',
                      label: 'SPY',
                      color: 'var(--chart-gray)',
                      dashed: true,
                      points: benchmarks.SPY.curve,
                    },
                  ]}
                />
              )}
            </div>
          </div>

          {/* Mid-Quarter Schedule 13D/13G Alerts if present */}
          {midQuarterAlerts.length > 0 && (
            <div className="panel">
              <div className="panel-header">
                <div>
                  <h3 className="panel-title" style={{ display: 'flex', alignItems: 'center', gap: '0.45rem' }}>
                    <Radio size={15} color="var(--warn-text)" /> Mid-Quarter Schedule 13D / 13G Filings ({midQuarterAlerts.length})
                  </h3>
                  <p className="panel-subtitle">Real-time ≥5% stakes filed between quarterly 13F reports</p>
                </div>
              </div>
              <div className="alerts-strip-list" style={{ padding: '0.85rem 1.25rem' }}>
                {midQuarterAlerts.map((al) => (
                  <div key={al.id} className="alert-row-card">
                    <div style={{ display: 'flex', alignItems: 'center', gap: '0.45rem', flexWrap: 'wrap' }}>
                      <span className={`chip ${al.isActivist ? 'neg' : 'warn'}`}>{al.form}</span>
                      {al.ticker && stocksIndex[al.ticker] ? (
                        <button
                          type="button"
                          className="ticker-badge ticker-link-btn"
                          onClick={() => onSelectStock(al.ticker)}
                        >
                          {al.ticker}
                        </button>
                      ) : al.ticker ? (
                        <span className="ticker-badge">{al.ticker}</span>
                      ) : null}
                      <span style={{ fontWeight: 600, fontSize: '0.86rem' }}>{al.issuerName}</span>
                    </div>
                    <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem' }}>
                      <span className="mono" style={{ fontSize: '0.78rem', color: 'var(--text-secondary)' }}>
                        Filed {al.filingDate}
                      </span>
                      <a
                        href={al.edgarUrl}
                        target="_blank"
                        rel="noreferrer"
                        className="inline-ext-link mono"
                        style={{ fontSize: '0.76rem' }}
                      >
                        {al.accessionNumber} <ExternalLink size={11} />
                      </a>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}

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
                <h3 className="panel-title">
                  Total 13F Holdings ({shardLoading ? `${fund.totalPositionCount} positions...` : `${filteredHoldings.length} of ${holdings.length}`})
                </h3>
                <p className="panel-subtitle">Complete disclosed positions from {fund.latestQuarterLabel} Form 13F-HR (click any ticker for full Stock Dossier)</p>
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
                <button type="button" className="action-btn" onClick={handleExport} disabled={!holdings.length}>
                  <Download size={14} /> CSV
                </button>
              </div>
            </div>

            {shardLoading && !shardDetail ? (
              <div style={{ padding: '2rem', textAlign: 'center', color: 'var(--text-muted)', fontSize: '0.84rem' }}>
                Loading complete {fund.totalPositionCount}-position 13F table...
              </div>
            ) : (
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
                    {filteredHoldings.map((h, i) => {
                      const canOpenStock = Boolean(stocksIndex[h.ticker]);
                      return (
                        <tr key={`${h.cusip}-${h.securityType}`}>
                          <td className="mono" style={{ color: 'var(--text-muted)' }}>{i + 1}</td>
                          <td>
                            <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
                              {canOpenStock ? (
                                <button
                                  type="button"
                                  className="ticker-badge ticker-link-btn"
                                  onClick={() => onSelectStock(h.ticker)}
                                  title={`Open ${h.ticker} Stock Dossier`}
                                >
                                  {h.ticker}
                                </button>
                              ) : (
                                <span className="ticker-badge">{h.ticker}</span>
                              )}
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
                      );
                    })}
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
            )}
          </div>

          {/* Quarterly Filing Return History */}
          <div className="panel">
            <div className="panel-header">
              <h3 className="panel-title">Quarterly Filing Snapshots ({quarterlyHistory.length || fund.quartersTracked})</h3>
            </div>
            {shardLoading && !shardDetail ? (
              <div style={{ padding: '1.5rem', textAlign: 'center', color: 'var(--text-muted)', fontSize: '0.84rem' }}>
                Loading quarterly snapshots...
              </div>
            ) : (
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
                    {quarterlyHistory.map((q) => (
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
            )}
          </div>

          {/* Latest Quarter Portfolio Updates */}
          <div className="panel" id="fund-drawer-updates">
            {fundUpdate ? (
              <>
                <div className="panel-header">
                  <div>
                    <h3
                      className="panel-title"
                      style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', flexWrap: 'wrap' }}
                    >
                      <span>
                        Updates ({fundUpdate.prevQuarterLabel} → {fundUpdate.quarterLabel})
                      </span>
                      <span
                        className={`chip ${
                          fundUpdate.deadlineBadge === 'Deadline Day'
                            ? 'pos'
                            : fundUpdate.deadlineBadge === 'Early Filer'
                              ? ''
                              : 'warn'
                        }`}
                      >
                        {fundUpdate.deadlineBadge}
                      </span>
                    </h3>
                    <div className="fund-meta-line" style={{ marginTop: '0.25rem' }}>
                      <span className="mono">Filed {fundUpdate.filingDate}</span>
                      <span>·</span>
                      <a
                        href={fundUpdate.filingUrl}
                        target="_blank"
                        rel="noreferrer"
                        className="inline-ext-link mono"
                      >
                        Accession {fundUpdate.accessionNumber} <ExternalLink size={11} />
                      </a>
                    </div>
                  </div>

                  <div className="update-card-book">
                    <div className="update-book-main">
                      <span className="update-book-val mono">
                        {formatUsdCompact(fundUpdate.currTotalReportedUsd)}
                      </span>
                      {fundUpdate.totalValueDeltaPct !== null && (
                        <span
                          className={`chip ${fundUpdate.totalValueDeltaPct >= 0 ? 'pos' : 'neg'} mono`}
                        >
                          {formatSignedPct(fundUpdate.totalValueDeltaPct, 1)}
                        </span>
                      )}
                    </div>
                    <div className="update-book-sub mono">
                      {fundUpdate.currPositionCount} positions (from{' '}
                      {formatUsdCompact(fundUpdate.prevTotalReportedUsd)} / {fundUpdate.prevPositionCount})
                    </div>
                  </div>
                </div>

                <UpdateSummaryContent
                  update={fundUpdate}
                  stocksIndex={stocksIndex}
                  onDiveDeeper={onDiveDeeper}
                  onSelectStock={onSelectStock}
                />
              </>
            ) : (
              <div style={{ padding: '1.25rem', color: 'var(--text-muted)', fontSize: '0.84rem' }}>
                No consecutive quarter-over-quarter 13F comparison available for {fund.name} yet.
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
};
