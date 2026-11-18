import { useEffect, useMemo, useRef, useState, type FC } from 'react';
import {
  ArrowLeft,
  ArrowRight,
  Download,
  ExternalLink,
  Search,
  Shield,
  TrendingDown,
  TrendingUp,
  X,
} from 'lucide-react';
import type {
  FundUpdateSummary,
  MoveAction,
  PositionMoveItem,
  StockIndexEntry,
} from '../types/data';
import { fetchFundShard } from '../utils/dataLoader';
import {
  compactShares,
  downloadCsv,
  formatPct,
  formatSignedPct,
  formatSignedUsd,
  formatUsdCompact,
  returnClass,
  tierChipClass,
} from '../utils/format';
import { useModalFocusTrap } from '../utils/useModalFocusTrap';

interface UpdateDetailDrawerProps {
  update: FundUpdateSummary;
  stocksIndex: Record<string, StockIndexEntry>;
  onClose: () => void;
  onOpenFundProfile?: (cik: string) => void;
  onSelectStock?: (ticker: string) => void;
  fromFundDrawer?: boolean;
}

type DiffFilter = 'ALL' | 'NEW' | 'ADDED' | 'TRIMMED' | 'EXITED' | 'OPTIONS' | 'VALUATION';
type DiffSort = 'maxValue' | 'valueDelta' | 'bookDelta' | 'sharesDeltaPct';

function actionChipClass(action: MoveAction): string {
  if (action === 'NEW' || action === 'ADDED') return 'pos';
  if (action === 'TRIMMED' || action === 'EXITED') return 'neg';
  return '';
}

function bookBarSegments(eqPct: number, callPct: number, putPct: number) {
  const total = Math.max(0.01, eqPct + callPct + putPct);
  return {
    eq: (eqPct / total) * 100,
    call: (callPct / total) * 100,
    put: (putPct / total) * 100,
  };
}

export const UpdateDetailDrawer: FC<UpdateDetailDrawerProps> = ({
  update,
  stocksIndex,
  onClose,
  onOpenFundProfile,
  onSelectStock,
  fromFundDrawer = false,
}) => {
  const panelRef = useRef<HTMLDivElement>(null);
  useModalFocusTrap(panelRef, true);

  const [filter, setFilter] = useState<DiffFilter>('ALL');
  const [sortBy, setSortBy] = useState<DiffSort>('maxValue');
  const [sortAsc, setSortAsc] = useState(false);
  const [search, setSearch] = useState('');

  const [retrySeq, setRetrySeq] = useState(0);
  const requestKey = `${update.shard}:${update.cik}:${retrySeq}`;
  const [shardState, setShardState] = useState<{
    key: string;
    moves: PositionMoveItem[] | null;
    error: string | null;
  } | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetchFundShard(update.shard)
      .then((payload) => {
        if (!cancelled) {
          setShardState({
            key: requestKey,
            moves: payload.updates[update.cik]?.allMoves ?? [],
            error: null,
          });
        }
      })
      .catch((err) => {
        if (!cancelled) {
          setShardState({
            key: requestKey,
            moves: null,
            error: err instanceof Error ? err.message : 'Unable to load update shard',
          });
        }
      });
    return () => {
      cancelled = true;
    };
  }, [requestKey, update.cik, update.shard]);

  const shardLoading = shardState?.key !== requestKey;
  const shardError = shardState?.key === requestKey ? shardState.error : null;
  const allMoves = useMemo(
    () => (shardState?.key === requestKey && shardState.moves ? shardState.moves : []),
    [requestKey, shardState]
  );

  const filteredMoves = useMemo(() => {
    const q = search.trim().toLowerCase();
    return allMoves
      .filter((m) => {
        if (filter === 'NEW' && m.action !== 'NEW') return false;
        if (filter === 'ADDED' && m.action !== 'ADDED') return false;
        if (filter === 'TRIMMED' && m.action !== 'TRIMMED') return false;
        if (filter === 'EXITED' && m.action !== 'EXITED') return false;
        if (filter === 'OPTIONS' && m.securityType !== 'CALL' && m.securityType !== 'PUT') return false;
        if (filter === 'VALUATION' && !m.valuationShiftType) return false;
        if (!q) return true;
        return (
          m.ticker.toLowerCase().includes(q) ||
          m.name.toLowerCase().includes(q) ||
          m.sector.toLowerCase().includes(q) ||
          m.securityType.toLowerCase().includes(q) ||
          (m.tagNote ?? '').toLowerCase().includes(q)
        );
      })
      .sort((a, b) => {
        let av = 0;
        let bv = 0;
        if (sortBy === 'maxValue') {
          av = Math.max(a.currValueUsd, a.prevValueUsd);
          bv = Math.max(b.currValueUsd, b.prevValueUsd);
        } else if (sortBy === 'valueDelta') {
          av = Math.abs(a.valueDeltaUsd);
          bv = Math.abs(b.valueDeltaUsd);
        } else if (sortBy === 'bookDelta') {
          av = Math.abs(a.bookDeltaPct);
          bv = Math.abs(b.bookDeltaPct);
        } else {
          av = a.sharesDeltaPct ?? (a.action === 'NEW' ? 999999 : -999999);
          bv = b.sharesDeltaPct ?? (b.action === 'NEW' ? 999999 : -999999);
        }
        return sortAsc ? av - bv : bv - av;
      });
  }, [allMoves, filter, sortBy, sortAsc, search]);

  const toggleSort = (col: DiffSort) => {
    if (sortBy === col) setSortAsc(!sortAsc);
    else {
      setSortBy(col);
      setSortAsc(false);
    }
  };

  const handleExport = () => {
    if (!allMoves.length) return;
    downloadCsv(
      `paper-trail-${update.fundName.toLowerCase().replace(/[^a-z0-9]+/g, '-')}-${update.periodEnd}-diff.csv`,
      [
        'Ticker',
        'Company',
        'Type',
        'Action',
        `${update.prevQuarterLabel} Shares`,
        `${update.quarterLabel} Shares`,
        'Shares Delta (%)',
        `${update.prevQuarterLabel} Value (USD)`,
        `${update.quarterLabel} Value (USD)`,
        'Value Delta (USD)',
        'Est. Flow (USD)',
        `${update.prevQuarterLabel} Book %`,
        `${update.quarterLabel} Book %`,
        'Book Delta (pts)',
        'Note',
      ],
      allMoves.map((m: PositionMoveItem) => [
        m.ticker,
        m.name,
        m.securityType,
        m.action,
        m.prevShares,
        m.currShares,
        m.sharesDeltaPct ?? '',
        m.prevValueUsd,
        m.currValueUsd,
        m.valueDeltaUsd,
        m.estFlowUsd ?? '',
        m.prevBookPct,
        m.currBookPct,
        m.bookDeltaPct,
        m.tagNote ?? '',
      ])
    );
  };

  const prevMix = bookBarSegments(
    update.prevEquityBookPct,
    update.prevCallBookPct,
    update.prevPutBookPct
  );
  const currMix = bookBarSegments(
    update.currEquityBookPct,
    update.currCallBookPct,
    update.currPutBookPct
  );

  const optionsCount = useMemo(
    () => allMoves.filter((m) => m.securityType === 'CALL' || m.securityType === 'PUT').length,
    [allMoves]
  );

  const valShiftsCount = useMemo(
    () =>
      shardState?.key === requestKey && shardState.moves
        ? allMoves.filter((m) => Boolean(m.valuationShiftType)).length
        : update.valuationShifts.length,
    [allMoves, requestKey, shardState, update.valuationShifts.length]
  );

  const renderTicker = (t: string) => {
    if (onSelectStock && stocksIndex[t]) {
      return (
        <button
          type="button"
          className="ticker-badge ticker-link-btn"
          onClick={() => onSelectStock(t)}
          title={`Open ${t} Stock Dossier`}
        >
          {t}
        </button>
      );
    }
    return <span className="ticker-badge">{t}</span>;
  };

  return (
    <div
      className="drawer-backdrop"
      role="dialog"
      aria-modal="true"
      aria-labelledby="update-drawer-title"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div ref={panelRef} tabIndex={-1} className="drawer-panel">
        {/* Sticky Frosted Header */}
        <div className="drawer-header">
          <div>
            <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', flexWrap: 'wrap' }}>
              <h2 id="update-drawer-title" style={{ margin: 0, fontSize: '1.35rem', fontWeight: 700 }}>
                {update.fundName}
              </h2>
              <span className={`chip ${tierChipClass(update.fundTier)}`}>{update.fundTier}</span>
              <span className="chip tech">{update.quarterLabel} {update.form || '13F-HR'}</span>
              {update.amendmentType && (
                <span className="chip warn">{update.amendmentType}</span>
              )}
              <span
                className={`chip ${
                  update.deadlineBadge === 'Deadline Day'
                    ? 'pos'
                    : update.deadlineBadge === 'Early Filer'
                      ? ''
                      : 'warn'
                }`}
              >
                {update.deadlineBadge}
              </span>
              {!update.inRankings && (
                <span
                  className="chip"
                  title="Tracked in Quarterly Updates watchlist (excluded from long-equity Top 100 Ranking screen due to option weighting or Tech/TMT & concentration thresholds)"
                >
                  Curated Watchlist
                </span>
              )}
            </div>

            <div className="fund-meta-line" style={{ marginTop: '0.35rem', fontSize: '0.82rem' }}>
              {update.manager && update.manager !== 'SEC EDGAR Discovered' && (
                <>
                  <span>
                    <strong>{update.manager}</strong>
                  </span>
                  <span>·</span>
                </>
              )}
              <span>{update.prevQuarterLabel} → {update.quarterLabel}</span>
              <span>·</span>
              <span className="mono">
                {update.originalFilingDate && update.originalFilingDate !== update.filingDate
                  ? `Orig ${update.originalFilingDate} · Amended ${update.latestAmendmentDate || update.filingDate}`
                  : `Filed ${update.filingDate}`}
              </span>
              <span>·</span>
              <a
                href={update.filingUrl}
                target="_blank"
                rel="noreferrer"
                className="inline-ext-link mono"
              >
                Accession {update.accessionNumber} <ExternalLink size={12} />
              </a>
            </div>
          </div>

          <div className="drawer-header-actions">
            {(fromFundDrawer || update.inRankings) && onOpenFundProfile && (
              <button
                type="button"
                className="action-btn back-to-profile-btn"
                onClick={() => onOpenFundProfile(update.cik)}
              >
                {fromFundDrawer ? (
                  <>
                    <ArrowLeft size={13} /> Back to Performance Profile
                  </>
                ) : (
                  <>
                    5Y Performance Profile <ArrowRight size={13} />
                  </>
                )}
              </button>
            )}
            <button
              type="button"
              className="close-btn"
              onClick={onClose}
              aria-label="Close filing update drawer"
            >
              <X size={18} />
            </button>
          </div>
        </div>

        <div className="drawer-body">
          {/* Top Summary Bento Cards */}
          <div className="metrics-strip">
            <div className="metric-card">
              <div className="metric-label">Reported 13F Book</div>
              <div className="metric-value">
                <span>{formatUsdCompact(update.currTotalReportedUsd)}</span>
                {update.totalValueDeltaPct !== null && (
                  <span
                    className={`mono ${returnClass(update.totalValueDeltaPct)}`}
                    style={{ fontSize: '0.88rem' }}
                  >
                    {formatSignedPct(update.totalValueDeltaPct, 1)}
                  </span>
                )}
              </div>
              <div className="metric-sub mono">
                {formatUsdCompact(update.prevTotalReportedUsd)} ({update.prevPositionCount} pos) →{' '}
                {update.currPositionCount} pos
              </div>
            </div>

            <div className="metric-card">
              <div
                className="metric-label"
                title="SEC Form 13F reports option market fair value, not delta-adjusted notional exposure"
              >
                Put Hedges (13F Fair Value %)
              </div>
              <div className="metric-value">
                <span className={update.currPutBookPct > 0 ? 'warn-text' : ''}>
                  {formatPct(update.currPutBookPct, 1)}
                </span>
                {update.putBookDeltaPct !== 0 && (
                  <span
                    className="mono"
                    style={{
                      fontSize: '0.84rem',
                      color: update.putBookDeltaPct > 0 ? 'var(--warn-text)' : 'var(--text-secondary)',
                    }}
                  >
                    {update.putBookDeltaPct > 0 ? '+' : ''}
                    {update.putBookDeltaPct.toFixed(1)} pts
                  </span>
                )}
              </div>
              <div className="metric-sub mono">
                {update.currPutUsd > 0
                  ? `${formatUsdCompact(update.currPutUsd)} fair val (vs ${formatUsdCompact(update.prevPutUsd)} / ${update.prevPutBookPct.toFixed(1)}%)`
                  : 'No disclosed put hedges'}
              </div>
            </div>

            <div className="metric-card">
              <div
                className="metric-label"
                title="SEC Form 13F reports option market fair value, not delta-adjusted notional exposure"
              >
                Call Options (13F Fair Value %)
              </div>
              <div className="metric-value">
                <span>{formatPct(update.currCallBookPct, 1)}</span>
              </div>
              <div className="metric-sub mono">
                {update.currCallUsd > 0
                  ? `${formatUsdCompact(update.currCallUsd)} fair val (vs ${formatUsdCompact(update.prevCallUsd)} / ${update.prevCallBookPct.toFixed(1)}%)`
                  : 'No disclosed call positions'}
              </div>
            </div>

            <div className="metric-card">
              <div className="metric-label">Gross Book Activity</div>
              <div className="metric-value">{formatUsdCompact(update.grossActivityUsd)}</div>
              <div className="metric-sub mono">
                {update.newCount} new · {update.addedCount} add · {update.trimmedCount} trim · {update.exitedCount} exit
              </div>
            </div>
          </div>

          {/* Paper Trail Intelligence Narrative & Structure Signals */}
          <div className="panel">
            <div className="panel-header">
              <div>
                <h3 className="panel-title">Filing Intelligence Digest</h3>
                <p className="panel-subtitle">{update.narrativeHeadline}</p>
              </div>
            </div>
            <div style={{ padding: '1.05rem 1.25rem' }}>
              <p style={{ margin: 0, fontSize: '0.9rem', color: 'var(--text-primary)', lineHeight: 1.6 }}>
                {update.narrativeLead}
              </p>

              {update.narrativeFocusNotes.length > 0 && (
                <ul className="update-focus-list">
                  {update.narrativeFocusNotes.map((note, idx) => (
                    <li key={idx}>{note}</li>
                  ))}
                </ul>
              )}

              {(update.narrativeHedgeAndStructure || update.narrativeOtherMoves) && (
                <div className="update-insight-boxes">
                  {update.narrativeHedgeAndStructure && (
                    <div className="insight-callout hedge">
                      <div className="insight-callout-label">
                        <Shield size={14} /> Hedging, Options &amp; Valuation Shifts
                      </div>
                      <p>{update.narrativeHedgeAndStructure}</p>
                    </div>
                  )}
                  {update.narrativeOtherMoves && (
                    <div className="insight-callout info">
                      <div className="insight-callout-label">
                        <TrendingUp size={14} /> Instrument Rotations &amp; Additional Moves
                      </div>
                      <p>{update.narrativeOtherMoves}</p>
                    </div>
                  )}
                </div>
              )}

              {/* Book Composition Shift Visual Bar */}
              <div className="book-mix-compare">
                <div className="book-mix-row">
                  <div className="book-mix-meta">
                    <span className="book-mix-q">{update.prevQuarterLabel}</span>
                    <span className="mono">{formatUsdCompact(update.prevTotalReportedUsd)}</span>
                  </div>
                  <div className="book-mix-bar" title="Equities / Calls / Puts">
                    <div className="bm-eq" style={{ width: `${prevMix.eq}%` }} />
                    <div className="bm-call" style={{ width: `${prevMix.call}%` }} />
                    <div className="bm-put" style={{ width: `${prevMix.put}%` }} />
                  </div>
                  <div className="book-mix-nums mono">
                    <span>Eq {update.prevEquityBookPct.toFixed(1)}%</span>
                    {update.prevCallBookPct > 0 && <span>Call {update.prevCallBookPct.toFixed(1)}%</span>}
                    {update.prevPutBookPct > 0 && <span>Put {update.prevPutBookPct.toFixed(1)}%</span>}
                  </div>
                </div>

                <div className="book-mix-row">
                  <div className="book-mix-meta">
                    <span className="book-mix-q active">{update.quarterLabel}</span>
                    <span className="mono">{formatUsdCompact(update.currTotalReportedUsd)}</span>
                  </div>
                  <div className="book-mix-bar" title="Equities / Calls / Puts">
                    <div className="bm-eq" style={{ width: `${currMix.eq}%` }} />
                    <div className="bm-call" style={{ width: `${currMix.call}%` }} />
                    <div className="bm-put" style={{ width: `${currMix.put}%` }} />
                  </div>
                  <div className="book-mix-nums mono">
                    <span>Eq {update.currEquityBookPct.toFixed(1)}%</span>
                    {update.currCallBookPct > 0 && <span>Call {update.currCallBookPct.toFixed(1)}%</span>}
                    {update.currPutBookPct > 0 && <span>Put {update.currPutBookPct.toFixed(1)}%</span>}
                  </div>
                </div>

                <div className="sector-legend" style={{ marginTop: '0.55rem' }}>
                  <span>
                    <span className="sector-dot" style={{ background: 'var(--accent-primary)' }} />
                    Common Equity
                  </span>
                  <span>
                    <span className="sector-dot" style={{ background: 'var(--chart-teal)' }} />
                    Call Options
                  </span>
                  <span>
                    <span className="sector-dot" style={{ background: 'var(--chart-amber)' }} />
                    Put Hedges
                  </span>
                </div>
              </div>
            </div>
          </div>

          {/* Structured Instrument Shifts / Valuation Moves Cards */}
          {(update.instrumentShifts.length > 0 || update.valuationShifts.length > 0) && (
            <div className="panel">
              <div className="panel-header">
                <h3 className="panel-title">Detected Paper Trail Signals</h3>
                <span style={{ fontSize: '0.78rem', color: 'var(--text-muted)' }}>
                  Options legs, instrument rotations, and valuation moves on flat share counts
                </span>
              </div>
              <div className="signal-grid" style={{ padding: '1rem 1.25rem' }}>
                {update.valuationShifts.map((vs) => (
                  <div key={`vs-${vs.cusip}-${vs.securityType}`} className="signal-tile valuation">
                    <div className="signal-tile-head">
                      {renderTicker(vs.ticker)}
                      <span className="chip warn">
                        {vs.valuationShiftType === 'MARKDOWN' ? (
                          <TrendingDown size={11} />
                        ) : (
                          <TrendingUp size={11} />
                        )}
                        {vs.valuationShiftType === 'MARKDOWN' ? 'Markdown (Not a Sale)' : 'Markup on Flat Shares'}
                      </span>
                    </div>
                    <div className="signal-tile-title">
                      {vs.prevBookPct.toFixed(1)}% → {vs.currBookPct.toFixed(1)}% of book
                    </div>
                    <div className="signal-tile-sub mono">
                      Shares flat ({compactShares(vs.prevShares)} → {compactShares(vs.currShares)}) ·{' '}
                      {formatUsdCompact(vs.prevValueUsd)} → {formatUsdCompact(vs.currValueUsd)}
                    </div>
                  </div>
                ))}

                {update.instrumentShifts.map((sig) => (
                  <div key={`${sig.shiftType}-${sig.ticker}`} className="signal-tile">
                    <div className="signal-tile-head">
                      {renderTicker(sig.ticker)}
                      <span className="chip tech">{sig.shiftType.replace(/_/g, ' ')}</span>
                    </div>
                    <div className="signal-tile-title">{sig.label}</div>
                    <div className="signal-tile-sub">{sig.detail}</div>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Complete Position-by-Position QoQ Diff Table */}
          <div className="panel">
            <div className="panel-header">
              <div>
                <h3 className="panel-title">
                  Complete Quarter-over-Quarter Diff ({shardLoading ? 'loading...' : `${filteredMoves.length} of ${allMoves.length}`})
                </h3>
                <p className="panel-subtitle">
                  Comparing {update.prevQuarterLabel} ({update.prevPeriodEnd}) vs. {update.quarterLabel} ({update.periodEnd})
                </p>
              </div>
              <div className="controls-row">
                <div className="search-input-wrap">
                  <Search size={14} />
                  <input
                    type="text"
                    className="search-input"
                    placeholder="Filter ticker, company, note..."
                    value={search}
                    onChange={(e) => setSearch(e.target.value)}
                  />
                </div>
                <button type="button" className="action-btn" onClick={handleExport} disabled={!allMoves.length}>
                  <Download size={14} /> Export Diff CSV
                </button>
              </div>
            </div>

            {shardError ? (
              <div style={{ padding: '1.25rem', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                <span className="ret-neg">Error loading filing diff shard: {shardError}</span>
                <button type="button" className="action-btn" onClick={() => setRetrySeq((r) => r + 1)}>
                  Retry
                </button>
              </div>
            ) : shardLoading ? (
              <div style={{ padding: '2rem', textAlign: 'center', color: 'var(--text-muted)', fontSize: '0.84rem' }}>
                Loading complete position-by-position QoQ diff from shard <code>funds_{update.shard}.json</code>...
              </div>
            ) : (
              <>
                <div style={{ padding: '0.75rem 1.25rem', borderBottom: '1px solid var(--border-hairline)', display: 'flex', gap: '0.6rem', flexWrap: 'wrap', alignItems: 'center' }}>
                  <div className="segmented-group" role="group" aria-label="Filter position changes">
                    {(
                      [
                        { id: 'ALL', label: `All (${allMoves.length})` },
                        { id: 'NEW', label: `New (${update.newCount})` },
                        { id: 'ADDED', label: `Added (${update.addedCount})` },
                        { id: 'TRIMMED', label: `Trimmed (${update.trimmedCount})` },
                        { id: 'EXITED', label: `Exited (${update.exitedCount})` },
                        { id: 'OPTIONS', label: `Calls & Puts (${optionsCount})` },
                        ...(valShiftsCount > 0
                          ? [{ id: 'VALUATION' as const, label: `Markdowns/Ups (${valShiftsCount})` }]
                          : []),
                      ] as const
                    ).map((tab) => (
                      <button
                        key={tab.id}
                        type="button"
                        className={`seg-btn ${filter === tab.id ? 'active' : ''}`}
                        onClick={() => setFilter(tab.id)}
                      >
                        {tab.label}
                      </button>
                    ))}
                  </div>
                </div>

                <div className="table-scroll" style={{ maxHeight: '480px', overflowY: 'auto' }}>
                  <table className="data-table compact">
                    <thead>
                      <tr>
                        <th style={{ minWidth: '185px' }}>Security</th>
                        <th>Action</th>
                        <th className="num">
                          Shares ({update.prevQuarterLabel.replace(/^20\d{2}\s*/, '')} →{' '}
                          {update.quarterLabel.replace(/^20\d{2}\s*/, '')})
                        </th>
                        <th className="num sortable" onClick={() => toggleSort('sharesDeltaPct')}>
                          Shs Δ {sortBy === 'sharesDeltaPct' ? (sortAsc ? '↑' : '↓') : ''}
                        </th>
                        <th className="num sortable" onClick={() => toggleSort('maxValue')}>
                          Value ({update.prevQuarterLabel.replace(/^20\d{2}\s*/, '')} →{' '}
                          {update.quarterLabel.replace(/^20\d{2}\s*/, '')}){' '}
                          {sortBy === 'maxValue' ? (sortAsc ? '↑' : '↓') : ''}
                        </th>
                        <th className="num sortable" onClick={() => toggleSort('valueDelta')}>
                          Val Δ {sortBy === 'valueDelta' ? (sortAsc ? '↑' : '↓') : ''}
                        </th>
                        <th className="num sortable" onClick={() => toggleSort('bookDelta')}>
                          Book % Δ {sortBy === 'bookDelta' ? (sortAsc ? '↑' : '↓') : ''}
                        </th>
                      </tr>
                    </thead>
                    <tbody>
                      {filteredMoves.map((m) => (
                        <tr key={`${m.cusip}-${m.securityType}-${m.action}`}>
                          <td style={{ maxWidth: '250px', whiteSpace: 'normal' }}>
                            <div style={{ display: 'flex', alignItems: 'center', gap: '0.4rem', flexWrap: 'wrap' }}>
                              {renderTicker(m.ticker)}
                              {m.securityType !== 'EQUITY' && (
                                <span className={`chip ${m.securityType === 'PUT' ? 'warn' : 'tech'}`}>
                                  {m.securityType}
                                </span>
                              )}
                              <span style={{ fontWeight: 500, lineHeight: 1.3 }}>{m.name}</span>
                            </div>
                            {m.tagNote && (
                              <div style={{ fontSize: '0.73rem', color: 'var(--warn-text)', fontWeight: 500, marginTop: '0.2rem' }}>
                                {m.tagNote}
                              </div>
                            )}
                          </td>

                          <td style={{ whiteSpace: 'nowrap' }}>
                            <span className={`chip ${actionChipClass(m.action)}`}>{m.action}</span>
                          </td>

                          <td className="num mono" style={{ fontSize: '0.77rem', color: 'var(--text-secondary)', whiteSpace: 'nowrap' }}>
                            {m.action === 'NEW'
                              ? `0 → ${compactShares(m.currShares)}`
                              : m.action === 'EXITED'
                                ? `${compactShares(m.prevShares)} → 0`
                                : `${compactShares(m.prevShares)} → ${compactShares(m.currShares)}`}
                          </td>

                          <td className={`num mono ${returnClass(m.action === 'NEW' ? 1 : m.sharesDeltaPct)}`}>
                            {m.action === 'NEW'
                              ? 'NEW'
                              : m.action === 'EXITED'
                                ? 'EXITED'
                                : formatSignedPct(m.sharesDeltaPct, 1)}
                          </td>

                          <td className="num mono" style={{ fontSize: '0.79rem' }}>
                            {m.action === 'NEW'
                              ? `— → ${formatUsdCompact(m.currValueUsd)}`
                              : m.action === 'EXITED'
                                ? `${formatUsdCompact(m.prevValueUsd)} → $0`
                                : `${formatUsdCompact(m.prevValueUsd)} → ${formatUsdCompact(m.currValueUsd)}`}
                          </td>

                          <td className={`num mono ${returnClass(m.valueDeltaUsd)}`}>
                            <div>
                              {m.valueDeltaUsd > 0 ? '+' : m.valueDeltaUsd < 0 ? '-' : ''}
                              {formatUsdCompact(Math.abs(m.valueDeltaUsd))}
                            </div>
                            {m.estFlowUsd != null && m.estFlowUsd !== 0 && (
                              <div className={returnClass(m.estFlowUsd)} style={{ fontSize: '0.68rem' }}>
                                Flow {formatSignedUsd(m.estFlowUsd)}
                              </div>
                            )}
                          </td>

                          <td className="num mono">
                            <div style={{ fontSize: '0.79rem' }}>
                              {m.prevBookPct.toFixed(1)}% → <strong>{m.currBookPct.toFixed(1)}%</strong>
                            </div>
                            <div className={returnClass(m.bookDeltaPct)} style={{ fontSize: '0.72rem' }}>
                              {m.bookDeltaPct > 0 ? '+' : ''}
                              {m.bookDeltaPct.toFixed(2)} pts
                            </div>
                          </td>
                        </tr>
                      ))}
                      {filteredMoves.length === 0 && (
                        <tr>
                          <td colSpan={7} style={{ textAlign: 'center', padding: '1.75rem', color: 'var(--text-muted)' }}>
                            No position changes match the current filter.
                          </td>
                        </tr>
                      )}
                    </tbody>
                  </table>
                </div>
              </>
            )}
          </div>
        </div>
      </div>
    </div>
  );
};
