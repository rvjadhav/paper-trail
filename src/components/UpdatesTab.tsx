import { useEffect, useMemo, useState, type FC } from 'react';
import {
  ArrowRight,
  ArrowUpRight,
  ExternalLink,
  Layers,
  Search,
  Shield,
  Sparkles,
  TrendingDown,
  TrendingUp,
  X,
} from 'lucide-react';
import type {
  FundUpdateEntry,
  PositionMoveItem,
  UpdatesOverview,
} from '../types/data';
import {
  formatSignedPct,
  formatUsdCompact,
} from '../utils/format';
import { UpdateDetailDrawer } from './UpdateDetailDrawer';

interface UpdatesTabProps {
  updates: FundUpdateEntry[];
  overview: UpdatesOverview;
  onSelectFund: (cik: string) => void;
}

type UpdateFilter = 'ALL' | 'HEDGED' | 'OPTIONS_SHIFTS' | 'TOP100';
type UpdateSort = 'latest' | 'activity' | 'hedge' | 'aum';

function compactShares(sh: number): string {
  const abs = Math.abs(sh);
  if (abs >= 1e6) return `${(sh / 1e6).toFixed(2)}M`;
  if (abs >= 1e3) return `${(sh / 1e3).toFixed(0)}k`;
  return sh.toLocaleString('en-US', { maximumFractionDigits: 0 });
}

function renderMoveDelta(m: PositionMoveItem): string {
  if (m.action === 'NEW') return `${m.currBookPct.toFixed(1)}%`;
  if (m.action === 'EXITED') return `${formatUsdCompact(m.prevValueUsd)}`;
  if (m.sharesDeltaPct !== null) {
    const p = m.sharesDeltaPct;
    if (Math.abs(p) >= 100) return `${p > 0 ? '+' : ''}${Math.round(p).toLocaleString('en-US')}%`;
    return `${p > 0 ? '+' : ''}${p.toFixed(1)}%`;
  }
  return `${m.currBookPct.toFixed(1)}%`;
}

export const UpdatesTab: FC<UpdatesTabProps> = ({
  updates,
  overview,
  onSelectFund,
}) => {
  const [filter, setFilter] = useState<UpdateFilter>('ALL');
  const [sortBy, setSortBy] = useState<UpdateSort>('latest');
  const [search, setSearch] = useState('');
  const [selectedTicker, setSelectedTicker] = useState<string | null>(null);
  const [selectedUpdateCik, setSelectedUpdateCik] = useState<string | null>(null);
  const [visibleLimit, setVisibleLimit] = useState<number>(40);

  useEffect(() => {
    if (!selectedUpdateCik) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setSelectedUpdateCik(null);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [selectedUpdateCik]);

  const selectedUpdate = useMemo(
    () => updates.find((u) => u.cik === selectedUpdateCik) ?? null,
    [updates, selectedUpdateCik]
  );

  const counts = useMemo(
    () => ({
      all: updates.length,
      hedged: updates.filter((u) => u.hasSignificantHedge).length,
      shifts: updates.filter(
        (u) => u.instrumentShifts.length > 0 || u.callLegs.length > 0 || u.putLegs.length > 0
      ).length,
      top100: updates.filter((u) => u.isRankedTop100AnyWindow).length,
    }),
    [updates]
  );

  const filteredUpdates = useMemo(() => {
    const q = search.trim().toLowerCase();
    const selUpper = selectedTicker?.toUpperCase() ?? null;
    return updates
      .filter((u) => {
        if (filter === 'HEDGED' && !u.hasSignificantHedge) return false;
        if (
          filter === 'OPTIONS_SHIFTS' &&
          u.instrumentShifts.length === 0 &&
          u.callLegs.length === 0 &&
          u.putLegs.length === 0
        ) {
          return false;
        }
        if (filter === 'TOP100' && !u.isRankedTop100AnyWindow) return false;
        if (selUpper && !u.allMoves.some((m) => m.ticker.toUpperCase() === selUpper)) {
          return false;
        }
        if (!q) return true;
        if (q.length === 1) {
          return (
            u.allMoves.some((m) => m.ticker.toLowerCase() === q) ||
            u.fundName.toLowerCase().includes(q)
          );
        }
        return (
          u.fundName.toLowerCase().includes(q) ||
          u.manager.toLowerCase().includes(q) ||
          u.styleTag.toLowerCase().includes(q) ||
          u.accessionNumber.toLowerCase().includes(q) ||
          u.allMoves.some(
            (m) =>
              m.ticker.toLowerCase().includes(q) ||
              m.name.toLowerCase().includes(q)
          )
        );
      })
      .sort((a, b) => {
        if (sortBy === 'activity') return b.grossActivityUsd - a.grossActivityUsd;
        if (sortBy === 'hedge') return b.currPutBookPct - a.currPutBookPct || b.currPutUsd - a.currPutUsd;
        if (sortBy === 'aum') return b.currTotalReportedUsd - a.currTotalReportedUsd;
        // 'latest'
        return (
          b.periodEnd.localeCompare(a.periodEnd) ||
          b.filingDate.localeCompare(a.filingDate) ||
          b.grossActivityUsd - a.grossActivityUsd
        );
      });
  }, [updates, filter, sortBy, search, selectedTicker]);

  const trimmedQuery = search.trim();
  const isTickerLikeSearch = trimmedQuery.length >= 2 && trimmedQuery.length <= 24;

  const togglePulseTicker = (ticker: string) => {
    setSearch('');
    setSelectedTicker((prev) => (prev === ticker ? null : ticker));
    setVisibleLimit(40);
  };

  const getMatchedTickerMoves = (u: FundUpdateEntry): PositionMoveItem[] => {
    if (selectedTicker) {
      const su = selectedTicker.toUpperCase();
      return u.allMoves.filter((m) => m.ticker.toUpperCase() === su).slice(0, 4);
    }
    if (!isTickerLikeSearch) return [];
    const ql = trimmedQuery.toLowerCase();
    const fundMatchesItself =
      u.fundName.toLowerCase().includes(ql) || u.manager.toLowerCase().includes(ql);
    if (fundMatchesItself) return [];
    return u.allMoves
      .filter((m) => m.ticker.toLowerCase().includes(ql) || m.name.toLowerCase().includes(ql))
      .slice(0, 4);
  };

  return (
    <section aria-label="Quarterly Fund Updates">
      {/* Header Toolbar */}
      <div className="section-toolbar">
        <div className="section-title-group">
          <h2>Quarterly 13F Paper Trail — {overview.latestQuarterLabel} Updates</h2>
          <p>
            Quarter-over-quarter portfolio changes across {overview.totalUpdateCards} tracked funds — capturing initiations, heavy adds, exits, put hedges, common-to-call shifts, and flat-share markdowns.
          </p>
        </div>
      </div>

      {/* Top Bento KPIs */}
      <div className="metrics-strip">
        <div className="metric-card">
          <div className="metric-label">{overview.latestQuarterLabel} Filings Ingested</div>
          <div className="metric-value">
            <span>
              {overview.latestQuarterFilersCount} / {overview.totalEligibleFundsCount}
            </span>
          </div>
          <div className="metric-sub">
            {overview.deadlineDayFilersCount} filed on SEC deadline day
          </div>
        </div>

        <div className="metric-card">
          <div className="metric-label">Aggregate {overview.latestQuarterLabel} Book</div>
          <div className="metric-value">{formatUsdCompact(overview.totalLatestBookUsd)}</div>
          <div className="metric-sub">Reported U.S. equities &amp; disclosed options</div>
        </div>

        <div className="metric-card">
          <div className="metric-label">Disclosed Put Hedges</div>
          <div className="metric-value">{formatUsdCompact(overview.totalPutNotionalUsd)}</div>
          <div className="metric-sub">
            Active protection across {overview.fundsWithPutHedgesCount} filers
          </div>
        </div>

        <div className="metric-card">
          <div className="metric-label">Disclosed Call Exposure</div>
          <div className="metric-value">{formatUsdCompact(overview.totalCallNotionalUsd)}</div>
          <div className="metric-sub">Upside derivative bets in {overview.latestQuarterLabel}</div>
        </div>
      </div>

      {/* Cross-Fund Quarterly Pulse Strip */}
      <div className="panel">
        <div className="panel-header">
          <div>
            <h3 className="panel-title">
              {overview.latestQuarterLabel} Cross-Fund Paper Trail Pulse
            </h3>
            <p className="panel-subtitle">
              Click any stock chip below to filter every fund&apos;s update card matching that ticker.
            </p>
          </div>
          {(selectedTicker || trimmedQuery) && (
            <button
              type="button"
              className="action-btn"
              onClick={() => {
                setSelectedTicker(null);
                setSearch('');
                setVisibleLimit(40);
              }}
            >
              <X size={13} /> Clear &ldquo;{selectedTicker || trimmedQuery}&rdquo;
            </button>
          )}
        </div>

        <div className="pulse-grid">
          <div className="pulse-col">
            <div className="pulse-col-head">
              <span className="chip pos">New Initiations</span>
              <span className="pulse-col-hint">2+ filers</span>
            </div>
            <div className="pulse-chip-list">
              {overview.consensusNewInitiations.slice(0, 6).map((item) => {
                const isActive =
                  selectedTicker === item.ticker ||
                  (!selectedTicker && trimmedQuery.toUpperCase() === item.ticker);
                return (
                  <button
                    key={`new-${item.ticker}`}
                    type="button"
                    className={`pulse-ticker-btn ${isActive ? 'active' : ''}`}
                    onClick={() => togglePulseTicker(item.ticker)}
                    title={`${item.name} — Initiated by ${item.fundCount} funds (${formatUsdCompact(item.totalCurrValueUsd)})`}
                  >
                    <strong className="mono">{item.ticker}</strong>
                    <span className="pulse-meta mono">
                      {item.fundCount}f · {formatUsdCompact(item.totalCurrValueUsd)}
                    </span>
                  </button>
                );
              })}
              {overview.consensusNewInitiations.length === 0 && (
                <span className="pulse-empty">No multi-fund initiations yet</span>
              )}
            </div>
          </div>

          <div className="pulse-col">
            <div className="pulse-col-head">
              <span className="chip tech">Heavy Adds</span>
              <span className="pulse-col-hint">Largest inflows</span>
            </div>
            <div className="pulse-chip-list">
              {overview.consensusHeavyAdds.slice(0, 6).map((item) => {
                const isActive =
                  selectedTicker === item.ticker ||
                  (!selectedTicker && trimmedQuery.toUpperCase() === item.ticker);
                return (
                  <button
                    key={`add-${item.ticker}`}
                    type="button"
                    className={`pulse-ticker-btn ${isActive ? 'active' : ''}`}
                    onClick={() => togglePulseTicker(item.ticker)}
                    title={`${item.name} — Added by ${item.fundCount} funds`}
                  >
                    <strong className="mono">{item.ticker}</strong>
                    <span className="pulse-meta mono">
                      {item.fundCount}f · {formatUsdCompact(Math.abs(item.totalDeltaUsd))}
                    </span>
                  </button>
                );
              })}
            </div>
          </div>

          <div className="pulse-col">
            <div className="pulse-col-head">
              <span className="chip neg">Trims &amp; Exits</span>
              <span className="pulse-col-hint">Largest cuts</span>
            </div>
            <div className="pulse-chip-list">
              {overview.consensusExitsAndTrims.slice(0, 6).map((item) => {
                const isActive =
                  selectedTicker === item.ticker ||
                  (!selectedTicker && trimmedQuery.toUpperCase() === item.ticker);
                return (
                  <button
                    key={`trim-${item.ticker}`}
                    type="button"
                    className={`pulse-ticker-btn ${isActive ? 'active' : ''}`}
                    onClick={() => togglePulseTicker(item.ticker)}
                    title={`${item.name} — Trimmed/Exited by ${item.fundCount} funds`}
                  >
                    <strong className="mono">{item.ticker}</strong>
                    <span className="pulse-meta mono">
                      {item.fundCount}f · -{formatUsdCompact(Math.abs(item.totalDeltaUsd))}
                    </span>
                  </button>
                );
              })}
            </div>
          </div>

          <div className="pulse-col">
            <div className="pulse-col-head">
              <span className="chip warn">Options &amp; Hedges</span>
              <span className="pulse-col-hint">Calls &amp; Puts</span>
            </div>
            <div className="pulse-chip-list">
              {overview.consensusOptionMoves.slice(0, 6).map((item) => {
                const primType = item.funds[0]?.securityType ?? 'CALL';
                const isActive =
                  selectedTicker === item.ticker ||
                  (!selectedTicker && trimmedQuery.toUpperCase() === item.ticker);
                return (
                  <button
                    key={`opt-${item.ticker}`}
                    type="button"
                    className={`pulse-ticker-btn ${isActive ? 'active' : ''}`}
                    onClick={() => togglePulseTicker(item.ticker)}
                    title={`${item.name} derivative activity across ${item.fundCount} fund(s)`}
                  >
                    <strong className="mono">
                      {item.ticker} {primType}
                    </strong>
                    <span className="pulse-meta mono">
                      {formatUsdCompact(item.totalCurrValueUsd || Math.abs(item.totalDeltaUsd))}
                    </span>
                  </button>
                );
              })}
              {overview.consensusOptionMoves.length === 0 && (
                <span className="pulse-empty">No option moves disclosed</span>
              )}
            </div>
          </div>
        </div>
      </div>

      {/* Filter / Sort / Search Controls */}
      <div className="updates-toolbar-card">
        <div className="controls-row" style={{ flexWrap: 'wrap' }}>
          <div className="segmented-group" role="group" aria-label="Filter update cards">
            {(
              [
                { id: 'ALL', label: `All (${counts.all})` },
                { id: 'HEDGED', label: `Put Hedges (${counts.hedged})` },
                { id: 'OPTIONS_SHIFTS', label: `Options / Shifts (${counts.shifts})` },
                { id: 'TOP100', label: `Top 100 Ranked (${counts.top100})` },
              ] as const
            ).map((opt) => (
              <button
                key={opt.id}
                type="button"
                className={`seg-btn ${filter === opt.id ? 'active' : ''}`}
                onClick={() => {
                  setFilter(opt.id);
                  setVisibleLimit(40);
                }}
              >
                {opt.label}
              </button>
            ))}
          </div>

          <div className="segmented-group" role="group" aria-label="Sort update cards">
            {(
              [
                { id: 'latest', label: 'Latest Filed' },
                { id: 'activity', label: 'Activity $' },
                { id: 'hedge', label: 'Put Hedge %' },
                { id: 'aum', label: 'Book Size' },
              ] as const
            ).map((s) => (
              <button
                key={s.id}
                type="button"
                className={`seg-btn ${sortBy === s.id ? 'active' : ''}`}
                onClick={() => setSortBy(s.id)}
              >
                {s.label}
              </button>
            ))}
          </div>
        </div>

        <div className="controls-row" style={{ flexWrap: 'wrap' }}>
          {selectedTicker && (
            <button
              type="button"
              className="action-btn primary"
              onClick={() => {
                setSelectedTicker(null);
                setVisibleLimit(40);
              }}
              title="Clear selected ticker filter"
            >
              <span className="mono">Ticker: {selectedTicker}</span>
              <X size={13} />
            </button>
          )}
          <div className="search-input-wrap updates-search-wrap">
            <Search size={14} />
            <input
              id="updates-search-input"
              type="text"
              className="search-input updates-search-input"
              placeholder="Search fund, manager, or ticker..."
              value={search}
              onChange={(e) => {
                setSelectedTicker(null);
                setSearch(e.target.value);
                setVisibleLimit(40);
              }}
            />
          </div>
        </div>
      </div>

      {/* Fund Update Feed Cards */}
      <div className="updates-feed">
        {filteredUpdates.slice(0, visibleLimit).map((u) => {
          const matchedMoves = getMatchedTickerMoves(u);
          const hasDerivativeBook = u.currCallBookPct > 0 || u.currPutBookPct > 0 || u.prevCallBookPct > 0 || u.prevPutBookPct > 0;
          const totalMix = Math.max(0.01, u.currEquityBookPct + u.currCallBookPct + u.currPutBookPct);

          return (
            <article key={u.cik} className="update-card" data-cik={u.cik}>
              {/* Card Header */}
              <div className="update-card-header">
                <div className="update-card-identity">
                  <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', flexWrap: 'wrap' }}>
                    <button
                      type="button"
                      className="fund-link-btn"
                      style={{ fontSize: '1.08rem' }}
                      onClick={() => setSelectedUpdateCik(u.cik)}
                    >
                      {u.fundName}
                    </button>
                    <span className="chip tech">{u.quarterLabel}</span>
                    <span
                      className={`chip ${
                        u.deadlineBadge === 'Deadline Day'
                          ? 'pos'
                          : u.deadlineBadge === 'Early Filer'
                            ? ''
                            : 'warn'
                      }`}
                    >
                      {u.deadlineBadge}
                    </span>
                    {u.isRankedTop100AnyWindow && (
                      <span className="chip">Top 100 ({u.rankedWindows.slice(0, 2).join('/')})</span>
                    )}
                    {!u.inRankings && (
                      <span
                        className="chip"
                        title="Tracked in Quarterly Updates watchlist (excluded from long-equity Top 100 Ranking screen due to option weighting or Tech/TMT & concentration thresholds)"
                      >
                        Curated Watchlist
                      </span>
                    )}
                  </div>

                  <div className="fund-meta-line" style={{ marginTop: '0.28rem' }}>
                    <span>{u.manager}</span>
                    <span>·</span>
                    <span>{u.styleTag}</span>
                    <span>·</span>
                    <span className="mono">Filed {u.filingDate}</span>
                    <span>·</span>
                    <a
                      href={u.filingUrl}
                      target="_blank"
                      rel="noreferrer"
                      className="inline-ext-link mono"
                    >
                      {u.accessionNumber} <ExternalLink size={11} />
                    </a>
                  </div>
                </div>

                <div className="update-card-book">
                  <div className="update-book-main">
                    <span className="update-book-val mono">
                      {formatUsdCompact(u.currTotalReportedUsd)}
                    </span>
                    {u.totalValueDeltaPct !== null && (
                      <span
                        className={`chip ${u.totalValueDeltaPct >= 0 ? 'pos' : 'neg'} mono`}
                      >
                        {formatSignedPct(u.totalValueDeltaPct, 1)}
                      </span>
                    )}
                  </div>
                  <div className="update-book-sub mono">
                    {u.currPositionCount} positions (from {formatUsdCompact(u.prevTotalReportedUsd)} / {u.prevPositionCount})
                  </div>
                </div>
              </div>

              {/* Search Ticker Match Spotlight (when filtering by specific stock ticker) */}
              {matchedMoves.length > 0 && (
                <div className="ticker-spotlight">
                  <div className="ticker-spotlight-label">
                    <Sparkles size={13} /> Matched Position{matchedMoves.length > 1 ? 's' : ''}
                  </div>
                  <div className="ticker-spotlight-items">
                    {matchedMoves.map((m) => (
                      <div key={`sp-${m.cusip}-${m.securityType}`} className="spotlight-item">
                        <span className="ticker-badge">{m.ticker}</span>
                        {m.securityType !== 'EQUITY' && (
                          <span className={`chip ${m.securityType === 'PUT' ? 'warn' : 'tech'}`}>
                            {m.securityType}
                          </span>
                        )}
                        <span className={`chip ${m.action === 'NEW' || m.action === 'ADDED' ? 'pos' : m.action === 'TRIMMED' || m.action === 'EXITED' ? 'neg' : ''}`}>
                          {m.action}
                        </span>
                        <span className="mono" style={{ fontSize: '0.79rem' }}>
                          {compactShares(m.prevShares)} → {compactShares(m.currShares)} sh
                          {m.sharesDeltaPct !== null && m.action !== 'NEW' && m.action !== 'EXITED'
                            ? ` (${m.sharesDeltaPct > 0 ? '+' : ''}${m.sharesDeltaPct.toFixed(1)}%)`
                            : ''}
                        </span>
                        <span className="mono" style={{ fontSize: '0.79rem', color: 'var(--text-secondary)' }}>
                          {formatUsdCompact(m.prevValueUsd)} → {formatUsdCompact(m.currValueUsd)} ({m.currBookPct.toFixed(2)}% book)
                        </span>
                        {m.tagNote && (
                          <span className="chip warn" style={{ fontSize: '0.7rem' }}>
                            {m.tagNote}
                          </span>
                        )}
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {/* Book Composition Strip when Calls or Puts present */}
              {hasDerivativeBook && (
                <div className="card-mix-strip">
                  <div className="book-mix-bar small">
                    <div className="bm-eq" style={{ width: `${(u.currEquityBookPct / totalMix) * 100}%` }} />
                    <div className="bm-call" style={{ width: `${(u.currCallBookPct / totalMix) * 100}%` }} />
                    <div className="bm-put" style={{ width: `${(u.currPutBookPct / totalMix) * 100}%` }} />
                  </div>
                  <div className="card-mix-labels mono">
                    <span>Common {u.currEquityBookPct.toFixed(1)}%</span>
                    {u.currCallBookPct > 0 && (
                      <span style={{ color: '#008770', fontWeight: 600 }}>
                        Calls {u.currCallBookPct.toFixed(1)}% ({formatUsdCompact(u.currCallUsd)})
                      </span>
                    )}
                    {(u.currPutBookPct > 0 || u.prevPutBookPct > 0) && (
                      <span style={{ color: 'var(--warn-text)', fontWeight: 600 }}>
                        Puts {u.currPutBookPct.toFixed(1)}% ({formatUsdCompact(u.currPutUsd)} · vs {u.prevPutBookPct.toFixed(1)}%)
                      </span>
                    )}
                  </div>
                </div>
              )}

              {/* Editorial Brief & Insight Callouts */}
              <div className="update-narrative-area">
                <p className="update-lead">{u.narrativeLead}</p>

                {u.narrativeFocusNotes.length > 0 && (
                  <ul className="update-focus-list compact">
                    {u.narrativeFocusNotes.slice(0, 3).map((note, idx) => (
                      <li key={idx}>{note}</li>
                    ))}
                  </ul>
                )}

                {(u.hasSignificantHedge || u.valuationShifts.length > 0 || u.instrumentShifts.length > 0) && (
                  <div className="update-signal-pills">
                    {u.hasSignificantHedge && u.narrativeHedgeAndStructure && (
                      <div className="signal-inline-pill hedge">
                        <Shield size={13} />
                        <span>{u.narrativeHedgeAndStructure}</span>
                      </div>
                    )}
                    {!u.hasSignificantHedge &&
                      u.valuationShifts.slice(0, 1).map((vs) => (
                        <div key={`vsp-${vs.ticker}`} className="signal-inline-pill valuation">
                          {vs.valuationShiftType === 'MARKDOWN' ? (
                            <TrendingDown size={13} />
                          ) : (
                            <TrendingUp size={13} />
                          )}
                          <span>
                            <strong>{vs.ticker}:</strong> {vs.tagNote}
                          </span>
                        </div>
                      ))}
                    {u.instrumentShifts
                      .filter((s) => s.shiftType !== 'NEW_HEDGE' && s.shiftType !== 'HEDGE_SURGE')
                      .slice(0, 3)
                      .map((sig) => (
                        <div key={`isp-${sig.shiftType}-${sig.ticker}`} className="signal-inline-pill shift">
                          <Layers size={12} />
                          <span>
                            <strong>{sig.label}</strong> — {sig.detail}
                          </span>
                        </div>
                      ))}
                  </div>
                )}
              </div>

              {/* Four Scannable Move Buckets: New / Added / Trimmed / Exited */}
              <div className="move-buckets-grid">
                <div className="move-bucket">
                  <div className="move-bucket-title">
                    <span className="dot new" /> New ({u.newCount})
                  </div>
                  <div className="move-bucket-items">
                    {u.topNew.slice(0, 4).map((m) => (
                      <div key={`n-${m.cusip}-${m.securityType}`} className="move-pill new">
                        <span className="mono mp-tick">
                          {m.ticker}
                          {m.securityType !== 'EQUITY' ? ` ${m.securityType}` : ''}
                        </span>
                        <span className="mono mp-val">
                          {formatUsdCompact(m.currValueUsd)} ({m.currBookPct.toFixed(1)}%)
                        </span>
                      </div>
                    ))}
                    {u.topNew.length === 0 && <span className="mp-none">No new positions</span>}
                  </div>
                </div>

                <div className="move-bucket">
                  <div className="move-bucket-title">
                    <span className="dot add" /> Added ({u.addedCount})
                  </div>
                  <div className="move-bucket-items">
                    {u.topAdded.slice(0, 4).map((m) => (
                      <div key={`a-${m.cusip}-${m.securityType}`} className="move-pill add">
                        <span className="mono mp-tick">
                          {m.ticker}
                          {m.securityType !== 'EQUITY' ? ` ${m.securityType}` : ''}
                        </span>
                        <span className="mono mp-val ret-pos">
                          {renderMoveDelta(m)} · {formatUsdCompact(m.currValueUsd)}
                        </span>
                      </div>
                    ))}
                    {u.topAdded.length === 0 && <span className="mp-none">No major adds</span>}
                  </div>
                </div>

                <div className="move-bucket">
                  <div className="move-bucket-title">
                    <span className="dot trim" /> Trimmed ({u.trimmedCount})
                  </div>
                  <div className="move-bucket-items">
                    {u.topTrimmed.slice(0, 4).map((m) => (
                      <div key={`t-${m.cusip}-${m.securityType}`} className="move-pill trim">
                        <span className="mono mp-tick">
                          {m.ticker}
                          {m.securityType !== 'EQUITY' ? ` ${m.securityType}` : ''}
                        </span>
                        <span className="mono mp-val ret-neg">
                          {renderMoveDelta(m)} · {formatUsdCompact(m.currValueUsd)}
                        </span>
                      </div>
                    ))}
                    {u.topTrimmed.length === 0 && <span className="mp-none">No trims</span>}
                  </div>
                </div>

                <div className="move-bucket">
                  <div className="move-bucket-title">
                    <span className="dot exit" /> Exited ({u.exitedCount})
                  </div>
                  <div className="move-bucket-items">
                    {u.topExited.slice(0, 4).map((m) => (
                      <div key={`e-${m.cusip}-${m.securityType}`} className="move-pill exit">
                        <span className="mono mp-tick">
                          {m.ticker}
                          {m.securityType !== 'EQUITY' ? ` ${m.securityType}` : ''}
                        </span>
                        <span className="mono mp-val ret-neg">
                          was {formatUsdCompact(m.prevValueUsd)}
                        </span>
                      </div>
                    ))}
                    {u.topExited.length === 0 && <span className="mp-none">No full exits</span>}
                  </div>
                </div>
              </div>

              {/* Card Footer */}
              <div className="update-card-footer">
                <div className="mono" style={{ fontSize: '0.76rem', color: 'var(--text-muted)' }}>
                  Gross quarterly activity: <strong>{formatUsdCompact(u.grossActivityUsd)}</strong> ({u.activityRatioPct.toFixed(1)}% of book)
                </div>

                <div className="controls-row">
                  {u.inRankings && (
                    <button
                      type="button"
                      className="action-btn"
                      onClick={() => onSelectFund(u.cik)}
                    >
                      Fund Performance <ArrowUpRight size={13} />
                    </button>
                  )}
                  <button
                    type="button"
                    className="action-btn primary dive-deeper-btn"
                    onClick={() => setSelectedUpdateCik(u.cik)}
                  >
                    Dive Deeper <ArrowRight size={14} />
                  </button>
                </div>
              </div>
            </article>
          );
        })}

        {filteredUpdates.length > visibleLimit && (
          <div style={{ display: 'flex', justifyContent: 'center', gap: '0.75rem', padding: '0.5rem 0 0.75rem', flexWrap: 'wrap' }}>
            <button
              type="button"
              className="action-btn primary"
              onClick={() => setVisibleLimit((v) => v + 40)}
            >
              Show Next {Math.min(40, filteredUpdates.length - visibleLimit)} Updates ({filteredUpdates.length - visibleLimit} remaining)
            </button>
            <button
              type="button"
              className="action-btn"
              onClick={() => setVisibleLimit(filteredUpdates.length)}
            >
              Show All {filteredUpdates.length}
            </button>
          </div>
        )}

        {filteredUpdates.length === 0 && (
          <div className="panel" style={{ padding: '2.5rem 1.5rem', textAlign: 'center', color: 'var(--text-muted)' }}>
            No filing updates match &ldquo;{selectedTicker || search || filter}&rdquo;. Try clearing filters or search terms.
          </div>
        )}
      </div>

      {/* Dive Deeper Update Sheet */}
      {selectedUpdate && (
        <UpdateDetailDrawer
          update={selectedUpdate}
          onClose={() => setSelectedUpdateCik(null)}
          onOpenFundProfile={(cik) => {
            setSelectedUpdateCik(null);
            onSelectFund(cik);
          }}
        />
      )}
    </section>
  );
};
