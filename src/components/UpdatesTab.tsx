import { useEffect, useMemo, useState, type FC } from 'react';
import {
  ArrowUpRight,
  ExternalLink,
  Radio,
  Search,
  Sparkles,
  X,
} from 'lucide-react';
import type {
  FundUpdateSummary,
  MidQuarterAlert,
  PositionMoveItem,
  StockDetail,
  StockDetailMove,
  StockIndexEntry,
  UpdatesOverview,
} from '../types/data';
import { fetchStockDetail } from '../utils/dataLoader';
import {
  compactShares,
  formatSignedPct,
  formatSignedUsd,
  formatUsdCompact,
  returnClass,
  stakeStatusChipClass,
  tierChipClass,
} from '../utils/format';
import { UpdateSummaryContent } from './UpdateSummaryContent';

interface UpdatesTabProps {
  updates: FundUpdateSummary[];
  overview: UpdatesOverview;
  midQuarterAlerts: MidQuarterAlert[];
  stocksIndex: Record<string, StockIndexEntry>;
  searchableTickersByCik: Map<string, string[]>;
  onSelectFund: (cik: string) => void;
  onDiveDeeper: (cik: string) => void;
  onSelectStock: (ticker: string) => void;
}

type UpdateFilter = 'ALL' | 'ELITE50' | 'HEDGED' | 'OPTIONS_SHIFTS' | 'TOP100';
type UpdateSort = 'latest' | 'activity' | 'hedge' | 'aum';

interface SpotlightMatchItem {
  key: string;
  ticker: string;
  securityType: string;
  action: string;
  sharesLabel: string | null;
  sharesDeltaPct: number | null;
  valueLabel: string;
  tagNote: string | null;
}

function collectSummaryMoves(u: FundUpdateSummary): PositionMoveItem[] {
  return [
    ...u.topNew,
    ...u.topAdded,
    ...u.topTrimmed,
    ...u.topExited,
    ...u.valuationShifts,
  ];
}

export const UpdatesTab: FC<UpdatesTabProps> = ({
  updates,
  overview,
  midQuarterAlerts,
  stocksIndex,
  searchableTickersByCik,
  onSelectFund,
  onDiveDeeper,
  onSelectStock,
}) => {
  const [filter, setFilter] = useState<UpdateFilter>('ALL');
  const [sortBy, setSortBy] = useState<UpdateSort>('latest');
  const [search, setSearch] = useState('');
  const [debouncedSearch, setDebouncedSearch] = useState('');
  const [selectedTicker, setSelectedTicker] = useState<string | null>(null);
  const [visibleLimit, setVisibleLimit] = useState<number>(40);
  const [loadedStockState, setLoadedStockState] = useState<{
    ticker: string;
    detail: StockDetail | null;
  } | null>(null);

  useEffect(() => {
    const id = window.setTimeout(() => setDebouncedSearch(search), 180);
    return () => window.clearTimeout(id);
  }, [search]);

  const trimmedQuery = search.trim();
  const resolvedExactTicker = useMemo(() => {
    if (selectedTicker) return selectedTicker.toUpperCase();
    const qu = trimmedQuery.toUpperCase();
    if (qu && stocksIndex[qu]) return qu;
    return null;
  }, [selectedTicker, trimmedQuery, stocksIndex]);

  const fetchTargetTicker = useMemo(() => {
    if (selectedTicker) return selectedTicker.toUpperCase();
    const dqu = debouncedSearch.trim().toUpperCase();
    if (dqu && stocksIndex[dqu]) return dqu;
    return null;
  }, [selectedTicker, debouncedSearch, stocksIndex]);

  useEffect(() => {
    if (!fetchTargetTicker || !stocksIndex[fetchTargetTicker]) {
      return;
    }
    const reqTicker = fetchTargetTicker;
    let cancelled = false;
    fetchStockDetail(reqTicker, stocksIndex)
      .then((res) => {
        if (!cancelled) setLoadedStockState({ ticker: reqTicker, detail: res });
      })
      .catch(() => {
        if (!cancelled) setLoadedStockState({ ticker: reqTicker, detail: null });
      });
    return () => {
      cancelled = true;
    };
  }, [fetchTargetTicker, stocksIndex]);

  const activeStockDetail =
    resolvedExactTicker && loadedStockState?.ticker === resolvedExactTicker ? loadedStockState.detail : null;

  const stockDetailMovesByCik = useMemo(() => {
    const map = new Map<string, StockDetailMove[]>();
    if (!activeStockDetail || activeStockDetail.ticker.toUpperCase() !== resolvedExactTicker) {
      return map;
    }
    for (const mv of activeStockDetail.recentMoves) {
      const arr = map.get(mv.cik) ?? [];
      arr.push(mv);
      map.set(mv.cik, arr);
    }
    return map;
  }, [activeStockDetail, resolvedExactTicker]);

  const stockDetailHoldersByCik = useMemo(() => {
    const map = new Map<string, StockDetail['holders'][number]>();
    if (!activeStockDetail || activeStockDetail.ticker.toUpperCase() !== resolvedExactTicker) {
      return map;
    }
    for (const h of activeStockDetail.holders) {
      map.set(h.cik, h);
    }
    return map;
  }, [activeStockDetail, resolvedExactTicker]);

  const counts = useMemo(
    () => ({
      all: updates.length,
      elite: updates.filter((u) => u.fundTier === 'Elite 50').length,
      hedged: updates.filter((u) => u.hasSignificantHedge).length,
      shifts: updates.filter(
        (u) => u.instrumentShifts.length > 0 || u.callLegs.length > 0 || u.putLegs.length > 0
      ).length,
      top100: updates.filter((u) => u.isRankedTop100AnyWindow).length,
    }),
    [updates]
  );

  const filteredUpdates = useMemo(() => {
    const q = trimmedQuery.toLowerCase();
    const selUpper = selectedTicker?.toUpperCase() ?? null;

    return updates
      .filter((u) => {
        if (filter === 'ELITE50' && u.fundTier !== 'Elite 50') return false;
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

        const summaryMoves = collectSummaryMoves(u);
        const heldTickers = searchableTickersByCik.get(u.cik) ?? [];

        if (selUpper) {
          if (stockDetailMovesByCik.has(u.cik) || stockDetailHoldersByCik.has(u.cik)) {
            return true;
          }
          return (
            summaryMoves.some((m) => m.ticker.toUpperCase() === selUpper) ||
            u.instrumentShifts.some((s) => s.ticker.toUpperCase() === selUpper) ||
            u.putLegs.some((p) => p.ticker.toUpperCase() === selUpper) ||
            u.callLegs.some((c) => c.ticker.toUpperCase() === selUpper) ||
            heldTickers.some((t) => t.toUpperCase() === selUpper)
          );
        }

        if (!q) return true;
        if (stockDetailMovesByCik.has(u.cik) || stockDetailHoldersByCik.has(u.cik)) {
          return true;
        }
        if (q.length === 1) {
          return (
            summaryMoves.some((m) => m.ticker.toLowerCase() === q) ||
            heldTickers.some((t) => t.toLowerCase() === q) ||
            u.fundName.toLowerCase().includes(q)
          );
        }
        return (
          u.fundName.toLowerCase().includes(q) ||
          u.manager.toLowerCase().includes(q) ||
          u.styleTag.toLowerCase().includes(q) ||
          u.accessionNumber.toLowerCase().includes(q) ||
          (u.form && u.form.toLowerCase().includes(q)) ||
          heldTickers.some((t) => t.toLowerCase().includes(q)) ||
          summaryMoves.some(
            (m) => m.ticker.toLowerCase().includes(q) || m.name.toLowerCase().includes(q)
          ) ||
          u.putLegs.some(
            (p) => p.ticker.toLowerCase().includes(q) || p.name.toLowerCase().includes(q)
          ) ||
          u.callLegs.some(
            (c) => c.ticker.toLowerCase().includes(q) || c.name.toLowerCase().includes(q)
          )
        );
      })
      .sort((a, b) => {
        if (sortBy === 'activity') return b.grossActivityUsd - a.grossActivityUsd;
        if (sortBy === 'hedge') return b.currPutBookPct - a.currPutBookPct || b.currPutUsd - a.currPutUsd;
        if (sortBy === 'aum') return b.currTotalReportedUsd - a.currTotalReportedUsd;
        return (
          b.periodEnd.localeCompare(a.periodEnd) ||
          b.filingDate.localeCompare(a.filingDate) ||
          b.grossActivityUsd - a.grossActivityUsd
        );
      });
  }, [
    updates,
    filter,
    sortBy,
    trimmedQuery,
    selectedTicker,
    searchableTickersByCik,
    stockDetailMovesByCik,
    stockDetailHoldersByCik,
  ]);

  const isTickerLikeSearch = trimmedQuery.length >= 2 && trimmedQuery.length <= 24;

  const togglePulseTicker = (ticker: string) => {
    setSearch('');
    setSelectedTicker((prev) => (prev === ticker ? null : ticker));
    setVisibleLimit(40);
  };

  const getSpotlightMatches = (u: FundUpdateSummary): SpotlightMatchItem[] => {
    const summaryMoves = collectSummaryMoves(u);
    const dedupe = new Map<string, SpotlightMatchItem>();

    const addFromMove = (m: PositionMoveItem) => {
      const key = `${m.cusip}-${m.securityType}`;
      if (dedupe.has(key)) return;
      dedupe.set(key, {
        key,
        ticker: m.ticker,
        securityType: m.securityType,
        action: m.action,
        sharesLabel:
          m.sharesDeltaPct !== null && m.action !== 'NEW' && m.action !== 'EXITED'
            ? `${compactShares(m.prevShares)} → ${compactShares(m.currShares)} sh (${m.sharesDeltaPct > 0 ? '+' : ''}${m.sharesDeltaPct.toFixed(1)}%)`
            : `${compactShares(m.prevShares)} → ${compactShares(m.currShares)} sh`,
        sharesDeltaPct: m.sharesDeltaPct,
        valueLabel: `${formatUsdCompact(m.prevValueUsd)} → ${formatUsdCompact(m.currValueUsd)} (${m.currBookPct.toFixed(2)}% book)${
          m.estFlowUsd != null ? ` · Est. flow ${formatSignedUsd(m.estFlowUsd)}` : ''
        }`,
        tagNote: m.tagNote,
      });
    };

    if (resolvedExactTicker) {
      for (const m of summaryMoves) {
        if (m.ticker.toUpperCase() === resolvedExactTicker) addFromMove(m);
      }
      if (dedupe.size === 0) {
        for (const dm of stockDetailMovesByCik.get(u.cik) ?? []) {
          const key = `sd-${resolvedExactTicker}-${dm.action}`;
          dedupe.set(key, {
            key,
            ticker: resolvedExactTicker,
            securityType: 'EQUITY',
            action: dm.action,
            sharesLabel:
              dm.action === 'NEW'
                ? 'New initiation'
                : dm.action === 'EXITED'
                  ? 'Full exit'
                  : dm.sharesDeltaPct != null
                    ? `${formatSignedPct(dm.sharesDeltaPct, 1)} shares`
                    : null,
            sharesDeltaPct: dm.sharesDeltaPct,
            valueLabel: `${dm.valueDeltaUsd >= 0 ? '+' : '-'}${formatUsdCompact(Math.abs(dm.valueDeltaUsd))} · ${dm.prevBookPct.toFixed(1)}% → ${dm.currBookPct.toFixed(1)}% book`,
            tagNote: null,
          });
        }
      }
      return Array.from(dedupe.values()).slice(0, 4);
    }

    if (!isTickerLikeSearch) return [];
    const ql = trimmedQuery.toLowerCase();
    const fundMatchesItself =
      u.fundName.toLowerCase().includes(ql) || u.manager.toLowerCase().includes(ql);
    if (fundMatchesItself) return [];

    for (const m of summaryMoves) {
      if (m.ticker.toLowerCase().includes(ql) || m.name.toLowerCase().includes(ql)) {
        addFromMove(m);
      }
    }
    return Array.from(dedupe.values()).slice(0, 4);
  };

  return (
    <section aria-label="Quarterly Fund Updates">
      {/* Header Toolbar */}
      <div className="section-toolbar">
        <div className="section-title-group">
          <h2>Quarterly 13F Paper Trail — {overview.latestQuarterLabel} Updates</h2>
          <p>
            Quarter-over-quarter portfolio changes across {overview.totalUpdateCards} tracked funds — capturing initiations, heavy adds, exits, put hedges, common-to-call shifts, and mid-quarter 13D/13G filings.
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
          <div
            className="metric-label"
            title="SEC Form 13F reports option market fair value, not delta-adjusted notional exposure"
          >
            13F Disclosed Put Fair Value
          </div>
          <div className="metric-value">{formatUsdCompact(overview.totalPutNotionalUsd)}</div>
          <div className="metric-sub">
            Reported option fair value (not notional) · {overview.fundsWithPutHedgesCount} filers
          </div>
        </div>

        <div className="metric-card">
          <div className="metric-label">Mid-Quarter 13D / 13G Alerts</div>
          <div className="metric-value">{midQuarterAlerts.length}</div>
          <div className="metric-sub">
            Elite-50 &ge;5% stakes · {formatUsdCompact(overview.totalCallNotionalUsd)} call fair value in {overview.latestQuarterLabel}
          </div>
        </div>
      </div>

      {/* Live Mid-Quarter Schedule 13D/13G Strip */}
      <div className="panel">
        <div className="panel-header">
          <div>
            <h3 className="panel-title" style={{ display: 'flex', alignItems: 'center', gap: '0.45rem' }}>
              <Radio size={15} color="var(--warn-text)" /> Live Mid-Quarter Schedule 13D / 13G Filings ({midQuarterAlerts.length})
            </h3>
            <p className="panel-subtitle">
              Real-time activist (13D) &amp; passive &ge;5% (13G) beneficial ownership filings by Elite-50 managers between 13F quarters (95-day window)
            </p>
          </div>
        </div>

        {midQuarterAlerts.length > 0 ? (
          <div className="midq-alerts-grid">
            {midQuarterAlerts.slice(0, 12).map((al) => (
              <div key={al.id} className="alert-row-card">
                <div style={{ display: 'flex', alignItems: 'center', gap: '0.45rem', flexWrap: 'wrap' }}>
                  <span className={`chip ${al.isActivist ? 'neg' : 'warn'}`}>{al.form}</span>
                  {al.stakeStatus && (
                    <span className={`chip ${stakeStatusChipClass(al.stakeStatus)}`}>{al.stakeStatus}</span>
                  )}
                  <button
                    type="button"
                    className="fund-link-btn"
                    style={{ fontSize: '0.84rem' }}
                    onClick={() => onSelectFund(al.filerCik)}
                  >
                    {al.fundName} <ArrowUpRight size={12} />
                  </button>
                  {al.ticker && stocksIndex[al.ticker] ? (
                    <button
                      type="button"
                      className="ticker-badge ticker-link-btn"
                      onClick={() => onSelectStock(al.ticker)}
                      title={`Open ${al.ticker} Stock Dossier`}
                    >
                      {al.ticker}
                    </button>
                  ) : al.ticker ? (
                    <span className="ticker-badge">{al.ticker}</span>
                  ) : null}
                  {al.percentOfClass != null && (
                    <span className="chip mono">{al.percentOfClass.toFixed(1)}%</span>
                  )}
                  {al.sharesOwned != null && al.sharesOwned > 0 && (
                    <span className="mono" style={{ fontSize: '0.74rem', color: 'var(--text-muted)' }}>
                      {compactShares(al.sharesOwned)} sh
                    </span>
                  )}
                  <span style={{ fontSize: '0.8rem', color: 'var(--text-secondary)' }}>{al.issuerName}</span>
                </div>
                <div style={{ display: 'flex', alignItems: 'center', gap: '0.65rem' }}>
                  <span className="mono" style={{ fontSize: '0.75rem', color: 'var(--text-muted)' }}>
                    {al.eventDate ? `Event ${al.eventDate} · ` : ''}{al.filingDate}
                  </span>
                  <a
                    href={al.edgarUrl}
                    target="_blank"
                    rel="noreferrer"
                    className="inline-ext-link mono"
                    style={{ fontSize: '0.74rem' }}
                  >
                    EDGAR <ExternalLink size={11} />
                  </a>
                </div>
              </div>
            ))}
          </div>
        ) : (
          <div style={{ padding: '0.9rem 1.25rem', fontSize: '0.82rem', color: 'var(--text-muted)' }}>
            No new Elite-50 Schedule 13D/13G beneficial ownership filings recorded in the past 95 days.
          </div>
        )}
      </div>

      {/* Cross-Fund Quarterly Pulse Strip */}
      <div className="panel">
        <div className="panel-header">
          <div>
            <h3 className="panel-title">
              {overview.latestQuarterLabel} Cross-Fund Paper Trail Pulse
            </h3>
            <p className="panel-subtitle">
              Click any stock pill to filter fund updates for that ticker, or click the arrow icon to open its Stock Dossier.
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
                  <div key={`new-${item.ticker}`} className="pulse-pill-group">
                    <button
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
                    {stocksIndex[item.ticker] && (
                      <button
                        type="button"
                        className="pulse-dossier-btn"
                        aria-label={`Open ${item.ticker} Stock Dossier`}
                        title={`Open ${item.ticker} Stock Dossier`}
                        onClick={() => onSelectStock(item.ticker)}
                      >
                        <ArrowUpRight size={11} />
                      </button>
                    )}
                  </div>
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
                const flow = item.totalEstFlowUsd ?? item.totalDeltaUsd;
                return (
                  <div key={`add-${item.ticker}`} className="pulse-pill-group">
                    <button
                      type="button"
                      className={`pulse-ticker-btn ${isActive ? 'active' : ''}`}
                      onClick={() => togglePulseTicker(item.ticker)}
                      title={`${item.name} — Added by ${item.fundCount} funds (${formatSignedUsd(flow)} est. Q-VWAP net flow)`}
                    >
                      <strong className="mono">{item.ticker}</strong>
                      <span className={`pulse-meta mono ${isActive ? '' : returnClass(flow)}`}>
                        {item.fundCount}f · {formatSignedUsd(flow)}
                      </span>
                    </button>
                    {stocksIndex[item.ticker] && (
                      <button
                        type="button"
                        className="pulse-dossier-btn"
                        aria-label={`Open ${item.ticker} Stock Dossier`}
                        title={`Open ${item.ticker} Stock Dossier`}
                        onClick={() => onSelectStock(item.ticker)}
                      >
                        <ArrowUpRight size={11} />
                      </button>
                    )}
                  </div>
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
                const flow = item.totalEstFlowUsd ?? item.totalDeltaUsd;
                return (
                  <div key={`trim-${item.ticker}`} className="pulse-pill-group">
                    <button
                      type="button"
                      className={`pulse-ticker-btn ${isActive ? 'active' : ''}`}
                      onClick={() => togglePulseTicker(item.ticker)}
                      title={`${item.name} — Trimmed/Exited by ${item.fundCount} funds (${formatSignedUsd(flow)} est. Q-VWAP net flow)`}
                    >
                      <strong className="mono">{item.ticker}</strong>
                      <span className={`pulse-meta mono ${isActive ? '' : returnClass(flow)}`}>
                        {item.fundCount}f · {formatSignedUsd(flow)}
                      </span>
                    </button>
                    {stocksIndex[item.ticker] && (
                      <button
                        type="button"
                        className="pulse-dossier-btn"
                        aria-label={`Open ${item.ticker} Stock Dossier`}
                        title={`Open ${item.ticker} Stock Dossier`}
                        onClick={() => onSelectStock(item.ticker)}
                      >
                        <ArrowUpRight size={11} />
                      </button>
                    )}
                  </div>
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
                  <div key={`opt-${item.ticker}`} className="pulse-pill-group">
                    <button
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
                    {stocksIndex[item.ticker] && (
                      <button
                        type="button"
                        className="pulse-dossier-btn"
                        aria-label={`Open ${item.ticker} Stock Dossier`}
                        title={`Open ${item.ticker} Stock Dossier`}
                        onClick={() => onSelectStock(item.ticker)}
                      >
                        <ArrowUpRight size={11} />
                      </button>
                    )}
                  </div>
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
                { id: 'ELITE50', label: `Elite 50 (${counts.elite})` },
                { id: 'HEDGED', label: `Put Hedges (${counts.hedged})` },
                { id: 'OPTIONS_SHIFTS', label: `Options / Shifts (${counts.shifts})` },
                { id: 'TOP100', label: `Top 100 (${counts.top100})` },
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
          const matchedMoves = getSpotlightMatches(u);

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
                      onClick={() => onDiveDeeper(u.cik)}
                    >
                      {u.fundName}
                    </button>
                    <span className={`chip ${tierChipClass(u.fundTier)}`}>{u.fundTier}</span>
                    <span className="chip tech">{u.quarterLabel}</span>
                    {u.form && u.form !== '13F-HR' && (
                      <span className="chip warn">{u.form}</span>
                    )}
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
                  </div>

                  <div className="fund-meta-line" style={{ marginTop: '0.28rem' }}>
                    {u.manager && u.manager !== 'SEC EDGAR Discovered' && (
                      <>
                        <span>{u.manager}</span>
                        <span>·</span>
                      </>
                    )}
                    <span>{u.styleTag}</span>
                    <span>·</span>
                    <span className="mono">
                      {u.originalFilingDate && u.originalFilingDate !== u.filingDate
                        ? `Orig ${u.originalFilingDate} · Amended ${u.latestAmendmentDate || u.filingDate}`
                        : `Filed ${u.filingDate}`}
                    </span>
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

              {/* Search Ticker Match Spotlight */}
              {matchedMoves.length > 0 && (
                <div className="ticker-spotlight">
                  <div className="ticker-spotlight-label">
                    <Sparkles size={13} /> Matched Position{matchedMoves.length > 1 ? 's' : ''}
                  </div>
                  <div className="ticker-spotlight-items">
                    {matchedMoves.map((m) => (
                      <div key={m.key} className="spotlight-item">
                        {stocksIndex[m.ticker] ? (
                          <button
                            type="button"
                            className="ticker-badge ticker-link-btn"
                            onClick={() => onSelectStock(m.ticker)}
                            title={`Open ${m.ticker} Stock Dossier`}
                          >
                            {m.ticker}
                          </button>
                        ) : (
                          <span className="ticker-badge">{m.ticker}</span>
                        )}
                        {m.securityType !== 'EQUITY' && (
                          <span className={`chip ${m.securityType === 'PUT' ? 'warn' : 'tech'}`}>
                            {m.securityType}
                          </span>
                        )}
                        <span
                          className={`chip ${
                            m.action === 'NEW' || m.action === 'ADDED'
                              ? 'pos'
                              : m.action === 'TRIMMED' || m.action === 'EXITED'
                                ? 'neg'
                                : ''
                          }`}
                        >
                          {m.action}
                        </span>
                        {m.sharesLabel && (
                          <span className="mono" style={{ fontSize: '0.79rem' }}>
                            {m.sharesLabel}
                          </span>
                        )}
                        <span className="mono" style={{ fontSize: '0.79rem', color: 'var(--text-secondary)' }}>
                          {m.valueLabel}
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

              <UpdateSummaryContent
                update={u}
                stocksIndex={stocksIndex}
                onDiveDeeper={onDiveDeeper}
                onOpenFundProfile={onSelectFund}
                onSelectStock={onSelectStock}
              />
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
    </section>
  );
};
