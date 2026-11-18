import { useEffect, useMemo, useRef, useState, type FC } from 'react';
import {
  Activity,
  ArrowRight,
  ArrowUpRight,
  BarChart3,
  Building2,
  Clock,
  ExternalLink,
  Flame,
  Layers,
  PieChart,
  Radio,
  ShieldCheck,
  X,
} from 'lucide-react';
import type {
  DualConvictionSignal,
  Form4InsiderSummary,
  FundSummary,
  ReturnMode,
  StockDetail,
  StockIndexEntry,
} from '../types/data';
import { resolveAlphaDecayMetrics } from '../utils/alphaDecay';
import {
  buildFundSkillMap,
  resolveActionabilityMatrix,
  type UnifiedConvictionRow,
} from '../utils/convictionMatrix';
import { fetchStockDetail } from '../utils/dataLoader';
import {
  actionVerdictChipClass,
  alphaDecayChipClass,
  alphaDecayLabel,
  compactShares,
  convergenceTierChipClass,
  convergenceTierLabel,
  entryZoneChipClass,
  formatPct,
  formatSignedPct,
  formatSignedUsd,
  formatUsdCompact,
  insiderSignalChipClass,
  insiderSignalLabelText,
  item4CategoryChipClass,
  returnClass,
  stakeStatusChipClass,
  tierChipClass,
} from '../utils/format';
import { useModalFocusTrap } from '../utils/useModalFocusTrap';

interface StockDetailDrawerProps {
  ticker: string;
  stocksIndex: Record<string, StockIndexEntry>;
  latestQuarterLabel: string;
  funds?: FundSummary[];
  mode?: ReturnMode;
  convictionByTicker?: Record<string, UnifiedConvictionRow>;
  form4Signals?: Record<string, Form4InsiderSummary>;
  dualConvictionByTicker?: Record<string, DualConvictionSignal>;
  onClose: () => void;
  onSelectFund: (cik: string) => void;
  onJumpToIdeal?: () => void;
}

function pillarTone(val: number): string {
  if (val >= 17) return 'pos';
  if (val >= 12) return '';
  if (val >= 8) return 'warn';
  return 'neg';
}

export const StockDetailDrawer: FC<StockDetailDrawerProps> = ({
  ticker,
  stocksIndex,
  latestQuarterLabel,
  funds,
  mode = 'filing_lag',
  convictionByTicker,
  form4Signals,
  dualConvictionByTicker,
  onClose,
  onSelectFund,
  onJumpToIdeal,
}) => {
  const panelRef = useRef<HTMLDivElement>(null);
  useModalFocusTrap(panelRef, true);

  const [holderFilter, setHolderFilter] = useState<'ALL' | 'ELITE'>('ALL');
  const [retryCount, setRetryCount] = useState(0);
  const requestKey = `${ticker.toUpperCase()}:${retryCount}`;
  const [loadState, setLoadState] = useState<{
    key: string;
    detail: StockDetail | null;
    error: string | null;
  } | null>(null);

  const preview = stocksIndex[ticker.toUpperCase()] ?? stocksIndex[ticker] ?? null;

  useEffect(() => {
    let cancelled = false;
    fetchStockDetail(ticker, stocksIndex)
      .then((res) => {
        if (!cancelled) {
          setLoadState({ key: requestKey, detail: res, error: null });
        }
      })
      .catch((err) => {
        if (!cancelled) {
          setLoadState({
            key: requestKey,
            detail: null,
            error: err instanceof Error ? err.message : 'Unable to load stock dossier',
          });
        }
      });
    return () => {
      cancelled = true;
    };
  }, [requestKey, ticker, stocksIndex]);

  const loading = loadState?.key !== requestKey;
  const error = loadState?.key === requestKey ? loadState.error : null;
  const detail = loadState?.key === requestKey ? loadState.detail : null;

  const filteredHolders = useMemo(() => {
    if (!detail) return [];
    if (holderFilter === 'ELITE') {
      return detail.holders.filter((h) => h.fundTier === 'Elite 50');
    }
    return detail.holders;
  }, [detail, holderFilter]);

  const fundSkillByCik = useMemo(() => buildFundSkillMap(funds, mode), [funds, mode]);
  const fallbackUnified =
    convictionByTicker?.[ticker.toUpperCase()] ?? convictionByTicker?.[ticker] ?? null;

  const alphaDecay = useMemo(() => {
    if (detail) return resolveAlphaDecayMetrics(detail, { latestQuarterLabel });
    if (preview) return resolveAlphaDecayMetrics(preview, { latestQuarterLabel });
    return null;
  }, [detail, preview, latestQuarterLabel]);

  const actionability = useMemo(() => {
    if (detail) {
      const hasFlow = detail.netEstFlowUsd != null;
      return resolveActionabilityMatrix(detail, {
        fundSkillByCik,
        latestQuarterLabel,
        isCoreConsensus:
          detail.signalVerdict.includes('Core Consensus') || detail.eliteHoldersCount >= 5,
        fallbackFlowUsd: hasFlow ? detail.netEstFlowUsd : (fallbackUnified?.netFlowUsd ?? null),
      });
    }
    if (fallbackUnified) return fallbackUnified.actionability;
    if (preview) return resolveActionabilityMatrix(preview, { latestQuarterLabel });
    return null;
  }, [detail, fallbackUnified, preview, fundSkillByCik, latestQuarterLabel]);

  const normTicker = ticker.toUpperCase();
  const insiderActivity = useMemo(
    () => detail?.insiderActivity ?? form4Signals?.[normTicker] ?? form4Signals?.[ticker] ?? null,
    [detail?.insiderActivity, form4Signals, normTicker, ticker]
  );

  const convergenceSignal = useMemo(
    () =>
      detail?.convergenceSignal ??
      dualConvictionByTicker?.[normTicker] ??
      dualConvictionByTicker?.[ticker] ??
      null,
    [detail?.convergenceSignal, dualConvictionByTicker, normTicker, ticker]
  );

  const buyVsVwapPct = useMemo(() => {
    if (!insiderActivity) return null;
    if (insiderActivity.buyPriceVsQuarterVwapPct != null) {
      return insiderActivity.buyPriceVsQuarterVwapPct;
    }
    if (
      insiderActivity.avgBuyPriceUsd != null &&
      detail?.estQuarterVwap != null &&
      detail.estQuarterVwap > 0
    ) {
      return (
        Math.round(((insiderActivity.avgBuyPriceUsd / detail.estQuarterVwap) - 1) * 1000) / 10
      );
    }
    return null;
  }, [insiderActivity, detail]);

  const vwapGeometry = useMemo(() => {
    if (!detail) return null;
    const { quarterLow, quarterHigh, estQuarterVwap, priceUsd } = detail;
    if (
      quarterLow == null ||
      quarterHigh == null ||
      estQuarterVwap == null ||
      quarterHigh <= 0
    ) {
      return null;
    }
    const vals = [quarterLow, quarterHigh, estQuarterVwap];
    if (priceUsd != null && priceUsd > 0) vals.push(priceUsd);
    const minVal = Math.min(...vals);
    const maxVal = Math.max(...vals);
    const pad = Math.max(0.01, (maxVal - minVal) * 0.12);
    const lo = Math.max(0, minVal - pad);
    const hi = maxVal + pad;
    const span = Math.max(0.01, hi - lo);
    const pctPos = (v: number) => Math.max(2, Math.min(98, ((v - lo) / span) * 100));
    return {
      qStartPct: pctPos(quarterLow),
      qEndPct: pctPos(quarterHigh),
      vwapPct: pctPos(estQuarterVwap),
      pricePct: priceUsd != null && priceUsd > 0 ? pctPos(priceUsd) : null,
    };
  }, [detail]);

  const verdict = detail?.signalVerdict ?? preview?.signalVerdict ?? 'Tracked Equity';
  const entryZone = detail?.entryZoneStatus ?? preview?.entryZoneStatus ?? null;
  const pctVsVwap = detail?.pctVsQuarterVwap ?? preview?.pctVsQuarterVwap ?? null;
  const displayPrice = detail?.priceUsd ?? preview?.priceUsd ?? null;
  const displayName = detail?.name ?? preview?.name ?? ticker;
  const displaySector = detail?.sector ?? preview?.sector ?? 'Other';
  const displaySubSector = detail?.subSector ?? preview?.subSector ?? displaySector;
  const displayCapTier = detail?.marketCapTier ?? preview?.marketCapTier ?? null;
  const displayHolderCnt = detail?.holderCount ?? preview?.holderCount ?? 0;
  const displayEliteCnt = detail?.eliteHoldersCount ?? preview?.eliteHoldersCount ?? 0;

  return (
    <div
      className="drawer-backdrop stock-drawer-backdrop"
      role="dialog"
      aria-modal="true"
      aria-labelledby="stock-drawer-title"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div ref={panelRef} tabIndex={-1} className="drawer-panel">
        {/* Header */}
        <div className="drawer-header">
          <div>
            <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', flexWrap: 'wrap' }}>
              <span className="ticker-badge stock-hero-ticker">{ticker}</span>
              <h2 id="stock-drawer-title" style={{ margin: 0, fontSize: '1.3rem', fontWeight: 700 }}>
                {displayName}
              </h2>
              <span className="chip tech">{displaySubSector}</span>
              {displayCapTier && <span className="chip">{displayCapTier}</span>}
              {actionability && (
                <span
                  className={`chip ${actionVerdictChipClass(actionability.verdict)}`}
                  title={`Unified 4-Pillar Actionability Score: ${actionability.score.toFixed(1)}/100`}
                >
                  {actionability.verdict} · {actionability.score.toFixed(0)}/100
                </span>
              )}
              <span
                className={`chip ${
                  verdict.includes('Cluster') || verdict.includes('Elite Consensus')
                    ? 'pos'
                    : verdict.includes('Exit') || verdict.includes('Trimming')
                      ? 'neg'
                      : 'tech'
                }`}
              >
                {verdict}
              </span>
              {alphaDecay && (alphaDecay.status === 'FOMO_TRAP' || alphaDecay.status === 'LAG_ADVANTAGE') && (
                <span className={`chip ${alphaDecayChipClass(alphaDecay.status)}`}>
                  {alphaDecayLabel(alphaDecay.status)}
                </span>
              )}
              {convergenceSignal && (
                <span
                  className={`chip ${convergenceTierChipClass(convergenceSignal.tier)}`}
                  title={convergenceSignal.headline}
                >
                  <Flame size={11} />
                  {convergenceTierLabel(convergenceSignal.tier)} · {convergenceSignal.convergenceScore.toFixed(0)}
                </span>
              )}
              {detail?.idealPortfolioRank && (
                <button
                  type="button"
                  className="chip clickable pos"
                  onClick={() => {
                    onClose();
                    onJumpToIdeal?.();
                  }}
                  title="Included in the Ideal Paper Trail Portfolio"
                >
                  <PieChart size={11} />
                  Ideal Portfolio #{detail.idealPortfolioRank}
                  {detail.idealWeightPct ? ` (${detail.idealWeightPct.toFixed(1)}%)` : ''}
                </button>
              )}
            </div>

            <div className="fund-meta-line" style={{ marginTop: '0.35rem', fontSize: '0.82rem' }}>
              <span>Sector: <strong>{displaySector}</strong></span>
              {detail?.industry && (
                <>
                  <span>·</span>
                  <span>{detail.industry}</span>
                </>
              )}
              {detail?.cusip && (
                <>
                  <span>·</span>
                  <span className="mono">CUSIP {detail.cusip}</span>
                </>
              )}
              <span>·</span>
              <a
                href={`https://finance.yahoo.com/quote/${encodeURIComponent(ticker)}`}
                target="_blank"
                rel="noreferrer"
                className="inline-ext-link"
              >
                Yahoo Finance <ExternalLink size={11} />
              </a>
              <a
                href={`https://www.sec.gov/edgar/browse/?search=${encodeURIComponent(ticker)}`}
                target="_blank"
                rel="noreferrer"
                className="inline-ext-link"
              >
                SEC EDGAR <ExternalLink size={11} />
              </a>
            </div>
          </div>

          <button type="button" className="close-btn" onClick={onClose} aria-label="Close stock dossier">
            <X size={18} />
          </button>
        </div>

        {/* Body */}
        <div className="drawer-body">
          {/* Top KPIs */}
          <div className="metrics-strip">
            <div className="metric-card">
              <div className="metric-label">Current Price &amp; TTM</div>
              <div className="metric-value">
                <span className="mono">{displayPrice ? `$${displayPrice.toFixed(2)}` : '—'}</span>
                {detail?.stockTtmPct != null && (
                  <span className={`mono ${returnClass(detail.stockTtmPct)}`} style={{ fontSize: '0.92rem' }}>
                    {formatSignedPct(detail.stockTtmPct)}
                  </span>
                )}
              </div>
              <div className="metric-sub mono">
                YTD: <span className={returnClass(detail?.stockYtdPct)}>{formatSignedPct(detail?.stockYtdPct)}</span>
              </div>
            </div>

            <div className="metric-card">
              <div className="metric-label">Institutional Ownership</div>
              <div className="metric-value">
                <span className="mono">{displayHolderCnt}</span>
                <span style={{ fontSize: '0.86rem', color: 'var(--text-secondary)', fontWeight: 500 }}>
                  tracked funds
                </span>
              </div>
              <div className="metric-sub mono">
                <strong>{displayEliteCnt}</strong> Elite 50 · Total Held{' '}
                {detail ? formatUsdCompact(detail.totalValueUsd) : '—'}
              </div>
            </div>

            <div className="metric-card">
              <div className="metric-label">Conviction Sizing</div>
              <div className="metric-value mono">
                {detail ? formatPct(detail.avgWeightPct, 2) : '—'}
              </div>
              <div className="metric-sub mono">
                Avg holder wt · Max wt {detail ? formatPct(detail.maxWeightPct, 1) : '—'}
              </div>
            </div>

            <div className="metric-card">
              <div className="metric-label">QoQ Flow Impulse</div>
              <div className="metric-value mono" style={{ fontSize: '1.15rem', gap: '0.4rem', flexWrap: 'wrap' }}>
                {detail ? (
                  <>
                    <span className="chip pos">{detail.newCount} New</span>
                    <span className="chip tech">{detail.addedCount} Add</span>
                    <span className="chip neg">{detail.trimmedCount + detail.exitedCount} Cut</span>
                  </>
                ) : (
                  'Loading...'
                )}
              </div>
              <div className="metric-sub">
                {(detail?.netEstFlowUsd ?? fallbackUnified?.netFlowUsd) != null ? (
                  <>
                    Net est. flow:{' '}
                    <strong className={`mono ${returnClass(detail?.netEstFlowUsd ?? fallbackUnified?.netFlowUsd)}`}>
                      {formatSignedUsd(detail?.netEstFlowUsd ?? fallbackUnified?.netFlowUsd)}
                    </strong>
                    {' · '}
                  </>
                ) : null}
                Across {latestQuarterLabel} updates
              </div>
            </div>
          </div>

          {error && (
            <div className="panel" style={{ padding: '1rem 1.25rem', borderColor: 'var(--neg-text)' }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: '1rem' }}>
                <span className="ret-neg">Error loading stock shard: {error}</span>
                <button type="button" className="action-btn" onClick={() => setRetryCount((c) => c + 1)}>
                  Retry
                </button>
              </div>
            </div>
          )}

          {loading && !detail ? (
            <div className="panel" style={{ padding: '2rem', textAlign: 'center', color: 'var(--text-muted)' }}>
              Loading full SEC 13F ownership, Quarter-VWAP microstructure &amp; XBRL fundamentals for {ticker}...
            </div>
          ) : (
            detail && (
              <>
                {/* Unified Actionable Buy / Trim / Exit Conviction Matrix */}
                {actionability && (
                  <div
                    className={`panel ${
                      actionVerdictChipClass(actionability.verdict)
                        ? `conviction-panel-${actionVerdictChipClass(actionability.verdict)}`
                        : ''
                    }`}
                    style={{ marginBottom: '1.15rem' }}
                    aria-label="Unified Actionable Conviction Matrix"
                  >
                    <div className="panel-header">
                      <div>
                        <h3 className="panel-title" style={{ display: 'flex', alignItems: 'center', gap: '0.45rem' }}>
                          <ShieldCheck size={16} color="var(--accent-primary)" /> Unified Actionable Buy / Trim / Exit Conviction Matrix
                        </h3>
                        <p className="panel-subtitle">
                          Synthesizes 4 independent pillars (25 pts each → 0–100 score) into a single institutional action verdict
                        </p>
                      </div>
                      <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', flexWrap: 'wrap' }}>
                        <div className={`conviction-score-pill mono ${actionVerdictChipClass(actionability.verdict)}`}>
                          <span className="conviction-score-num">{actionability.score.toFixed(1)}</span>
                          <span className="conviction-score-denom">/100</span>
                        </div>
                        <span className={`chip ${actionVerdictChipClass(actionability.verdict)}`}>
                          {actionability.verdict}
                        </span>
                      </div>
                    </div>

                    <div style={{ padding: '1rem 1.25rem' }}>
                      <div className="pillar-grid">
                        <div className="pillar-card">
                          <div className="pillar-head">
                            <span>1. Smart-Money Flow Impulse</span>
                            <strong className="mono">{actionability.pillars.flowImpulse.toFixed(1)}/25</strong>
                          </div>
                          <div className="pillar-bar-track">
                            <div
                              className={`pillar-bar-fill ${pillarTone(actionability.pillars.flowImpulse)}`}
                              style={{ width: `${Math.min(100, (actionability.pillars.flowImpulse / 25) * 100)}%` }}
                            />
                          </div>
                          <div className="pillar-sub mono">
                            +{detail.newCount + detail.addedCount} buy / -{detail.trimmedCount + detail.exitedCount} cut
                            {(detail.netEstFlowUsd ?? fallbackUnified?.netFlowUsd) != null
                              ? ` · ${formatSignedUsd(detail.netEstFlowUsd ?? fallbackUnified?.netFlowUsd)}`
                              : ''}
                          </div>
                        </div>

                        <div className="pillar-card">
                          <div className="pillar-head">
                            <span>2. Price Discipline (Q-VWAP)</span>
                            <strong className="mono">{actionability.pillars.priceDiscipline.toFixed(1)}/25</strong>
                          </div>
                          <div className="pillar-bar-track">
                            <div
                              className={`pillar-bar-fill ${pillarTone(actionability.pillars.priceDiscipline)}`}
                              style={{ width: `${Math.min(100, (actionability.pillars.priceDiscipline / 25) * 100)}%` }}
                            />
                          </div>
                          <div className="pillar-sub mono">
                            {pctVsVwap != null ? `${formatSignedPct(pctVsVwap)} vs Est. Q-VWAP` : 'Q-VWAP pending'}
                            {alphaDecay?.filingToTodayPct != null
                              ? ` · Fil→Now ${formatSignedPct(alphaDecay.filingToTodayPct)}`
                              : ''}
                          </div>
                        </div>

                        <div className="pillar-card">
                          <div className="pillar-head">
                            <span>3. XBRL Quality Gate</span>
                            <strong className="mono">{actionability.pillars.xbrlQuality.toFixed(1)}/25</strong>
                          </div>
                          <div className="pillar-bar-track">
                            <div
                              className={`pillar-bar-fill ${pillarTone(actionability.pillars.xbrlQuality)}`}
                              style={{ width: `${Math.min(100, (actionability.pillars.xbrlQuality / 25) * 100)}%` }}
                            />
                          </div>
                          <div className="pillar-sub mono">
                            {detail.fundamentals
                              ? `Rev ${formatSignedPct(detail.fundamentals.revenueGrowthYoYPct, 0)} · ${
                                  detail.fundamentals.fcfMarginPct != null
                                    ? `FCF ${detail.fundamentals.fcfMarginPct.toFixed(0)}%`
                                    : detail.fundamentals.operatingMarginPct != null
                                      ? `OpM ${detail.fundamentals.operatingMarginPct.toFixed(0)}%`
                                      : 'Mgn N/A'
                                } · Dil ${formatSignedPct(detail.fundamentals.shareDilutionYoYPct, 1)}`
                              : 'XBRL unavailable (12.5 neutral)'}
                          </div>
                        </div>

                        <div className="pillar-card">
                          <div className="pillar-head">
                            <span>4. Thesis Persistence</span>
                            <strong className="mono">{actionability.pillars.thesisPersistence.toFixed(1)}/25</strong>
                          </div>
                          <div className="pillar-bar-track">
                            <div
                              className={`pillar-bar-fill ${pillarTone(actionability.pillars.thesisPersistence)}`}
                              style={{ width: `${Math.min(100, (actionability.pillars.thesisPersistence / 25) * 100)}%` }}
                            />
                          </div>
                          <div className="pillar-sub mono">
                            {actionability.thesisPersistence
                              ? actionability.thesisPersistence.summary
                              : alphaDecay?.avgEliteHoldingQuarters != null
                                ? `Avg Elite hold ${alphaDecay.avgEliteHoldingQuarters.toFixed(1)}Q`
                                : `${displayEliteCnt} Elite holders · +${detail.newCount}N/-${detail.exitedCount}X`}
                          </div>
                        </div>
                      </div>

                      <div
                        className={`alpha-decay-banner ${actionVerdictChipClass(actionability.verdict)}`}
                        style={{ marginTop: '0.78rem' }}
                      >
                        <div>
                          <strong style={{ marginRight: '0.45rem' }}>{actionability.verdict}:</strong>
                          <span>{actionability.summary}</span>
                        </div>
                      </div>
                    </div>
                  </div>
                )}

                {/* Post-13F Lag & Alpha-Decay Engine */}
                {alphaDecay && (
                  <div className="panel" style={{ marginBottom: '1.15rem' }} aria-label="Post-13F Lag and Alpha-Decay Engine">
                    <div className="panel-header">
                      <div>
                        <h3 className="panel-title" style={{ display: 'flex', alignItems: 'center', gap: '0.45rem' }}>
                          <Clock size={16} color="var(--accent-primary)" /> Is the Move Already Over? — Post-13F Lag &amp; Alpha-Decay
                        </h3>
                        <p className="panel-subtitle">
                          Price trajectory across Est. {latestQuarterLabel} Q-VWAP → SEC 13F Filing Date → Today &amp; Elite-50 Holding Tenure
                        </p>
                      </div>
                      <div style={{ display: 'flex', alignItems: 'center', gap: '0.4rem', flexWrap: 'wrap' }}>
                        <span
                          className="chip mono"
                          title="Mean consecutive quarters currently held across Elite-50 managers (resets on exits)"
                        >
                          Avg Smart-Money Hold:{' '}
                          {alphaDecay.avgEliteHoldingQuarters != null
                            ? alphaDecay.holdingHorizonLabel
                            : 'Pending 13F Sync'}
                        </span>
                        <span className={`chip ${alphaDecayChipClass(alphaDecay.status)}`}>
                          {alphaDecayLabel(alphaDecay.status)}
                        </span>
                      </div>
                    </div>

                    <div style={{ padding: '1rem 1.25rem' }}>
                      <div className="lag-timeline">
                        <div className="lag-node">
                          <span className="lag-node-label">1. Est. Q-VWAP</span>
                          <span className="lag-node-value mono">
                            {alphaDecay.estQuarterVwap != null ? `$${alphaDecay.estQuarterVwap.toFixed(2)}` : '—'}
                          </span>
                          <span className="lag-node-sub mono">{latestQuarterLabel} Inst. Avg</span>
                        </div>

                        <div className="lag-arrow-bridge" aria-label="Quarter VWAP to SEC filing date return">
                          <ArrowRight size={14} />
                          <span
                            className={`chip mono ${
                              alphaDecay.qVwapToFilingPct != null
                                ? alphaDecay.qVwapToFilingPct > 15
                                  ? 'warn'
                                  : alphaDecay.qVwapToFilingPct <= 0
                                    ? 'pos'
                                    : ''
                                : ''
                            }`}
                          >
                            {alphaDecay.qVwapToFilingPct != null
                              ? formatSignedPct(alphaDecay.qVwapToFilingPct)
                              : '—'}
                          </span>
                        </div>

                        <div className="lag-node">
                          <span className="lag-node-label">2. SEC Filing Date</span>
                          <span className="lag-node-value mono">
                            {alphaDecay.filingDatePriceUsd != null
                              ? `$${alphaDecay.filingDatePriceUsd.toFixed(2)}`
                              : '—'}
                          </span>
                          <span className="lag-node-sub mono">
                            {alphaDecay.consensusFilingDate ?? 'Statutory Deadline'}
                          </span>
                        </div>

                        <div className="lag-arrow-bridge" aria-label="SEC filing date to today return">
                          <ArrowRight size={14} />
                          <span
                            className={`chip mono ${
                              alphaDecay.filingToTodayPct != null
                                ? alphaDecay.filingToTodayPct > 15
                                  ? 'warn'
                                  : alphaDecay.filingToTodayPct <= 0
                                    ? 'pos'
                                    : ''
                                : ''
                            }`}
                          >
                            {alphaDecay.filingToTodayPct != null
                              ? formatSignedPct(alphaDecay.filingToTodayPct)
                              : '—'}
                          </span>
                        </div>

                        <div
                          className={`lag-node ${
                            alphaDecay.status === 'LAG_ADVANTAGE'
                              ? 'highlight-pos'
                              : alphaDecay.status === 'FOMO_TRAP'
                                ? 'highlight-neg'
                                : ''
                          }`}
                        >
                          <span className="lag-node-label">3. Today</span>
                          <span className="lag-node-value mono">
                            {alphaDecay.todayPriceUsd != null ? `$${alphaDecay.todayPriceUsd.toFixed(2)}` : '—'}
                          </span>
                          <span
                            className={`lag-node-sub mono ${returnClass(
                              alphaDecay.pctVsQuarterVwap != null ? -alphaDecay.pctVsQuarterVwap : null
                            )}`}
                          >
                            {alphaDecay.pctVsQuarterVwap != null
                              ? `${formatSignedPct(alphaDecay.pctVsQuarterVwap)} vs Q-VWAP`
                              : 'Total lag vs Q-VWAP —'}
                          </span>
                        </div>
                      </div>

                      <div className="lag-meta-row mono">
                        <span>
                          Elite-50 Flow: <strong className="ret-pos">+{alphaDecay.eliteBuyersCount} Buy</strong> /{' '}
                          <strong className="ret-neg">-{alphaDecay.eliteSellersCount} Sell</strong>
                        </span>
                        <span>·</span>
                        <span>
                          All Ranked Funds: <strong>+{alphaDecay.totalBuyersCount} Buy</strong> /{' '}
                          <strong>-{alphaDecay.totalSellersCount} Trim &amp; Exit</strong>
                        </span>
                        {alphaDecay.filingDatePriceUsd == null && (
                          <>
                            <span>·</span>
                            <span style={{ color: 'var(--text-muted)' }}>
                              Historical filing-date close &amp; multi-quarter tenure populate on next EDGAR data build.
                            </span>
                          </>
                        )}
                      </div>

                      <div
                        className={`alpha-decay-banner ${
                          alphaDecay.status === 'LAG_ADVANTAGE'
                            ? 'pos'
                            : alphaDecay.status === 'FOMO_TRAP'
                              ? 'neg'
                              : alphaDecay.status === 'POST_FILING_EXTENDED'
                                ? 'warn'
                                : ''
                        }`}
                      >
                        <div>
                          <strong style={{ marginRight: '0.45rem' }}>
                            {alphaDecay.status === 'FOMO_TRAP'
                              ? 'FOMO / Late Crowded Trap:'
                              : alphaDecay.status === 'LAG_ADVANTAGE'
                                ? 'Lag Advantage Entry:'
                                : alphaDecay.status === 'POST_FILING_EXTENDED'
                                  ? 'Post-Filing Extended:'
                                  : 'Balanced Lag Profile:'}
                          </strong>
                          <span>{alphaDecay.reason}</span>
                        </div>
                      </div>
                    </div>
                  </div>
                )}

                {/* SEC Form 4 Open-Market Insider Activity & Dual-Conviction */}
                <div
                  className="panel"
                  style={{ marginBottom: '1.15rem' }}
                  aria-label="SEC Form 4 Open-Market Insider Activity and Dual-Conviction"
                >
                  <div className="panel-header">
                    <div>
                      <h3 className="panel-title" style={{ display: 'flex', alignItems: 'center', gap: '0.45rem' }}>
                        <Flame size={16} color="var(--pos-text)" /> SEC Form 4 Insider Purchases &amp; Dual-Conviction
                      </h3>
                      <p className="panel-subtitle">
                        Open-market Form 4 purchases (&apos;P&apos;, 90d lookback, excluding M/A/F/G awards &amp; exercises) vs. Q-VWAP &amp; Elite 13F co-buying
                      </p>
                    </div>
                    <div style={{ display: 'flex', alignItems: 'center', gap: '0.4rem', flexWrap: 'wrap' }}>
                      {insiderActivity && insiderActivity.openMarketBuyCount > 0 && (
                        <span className={`chip ${insiderSignalChipClass(insiderActivity.insiderSignalLabel)}`}>
                          {insiderSignalLabelText(insiderActivity.insiderSignalLabel)}
                        </span>
                      )}
                      {convergenceSignal && (
                        <span
                          className={`chip mono ${convergenceTierChipClass(convergenceSignal.tier)}`}
                          style={{ fontWeight: 700 }}
                        >
                          Conv Score {convergenceSignal.convergenceScore.toFixed(0)}/100
                        </span>
                      )}
                    </div>
                  </div>

                  <div style={{ padding: '1rem 1.25rem' }}>
                    {convergenceSignal && (
                      <div
                        className={`alpha-decay-banner ${convergenceTierChipClass(convergenceSignal.tier)}`}
                        style={{ marginTop: 0, marginBottom: '0.9rem' }}
                      >
                        <div style={{ width: '100%' }}>
                          <div>
                            <strong style={{ marginRight: '0.45rem' }}>
                              {convergenceTierLabel(convergenceSignal.tier)}:
                            </strong>
                            <span>{convergenceSignal.headline}</span>
                          </div>
                          {convergenceSignal.reasons.length > 0 && (
                            <div className="convergence-reasons" style={{ marginTop: '0.5rem' }}>
                              {convergenceSignal.reasons.map((r, ri) => (
                                <span key={`sd-conv-r-${ri}`} className="convergence-reason-pill">
                                  {r}
                                </span>
                              ))}
                            </div>
                          )}
                          {convergenceSignal.activistIntent && (
                            <div className="item4-intent-box" style={{ marginTop: '0.55rem' }}>
                              <strong>13D Item 4 Intent:</strong> &ldquo;{convergenceSignal.activistIntent}&rdquo;
                            </div>
                          )}
                        </div>
                      </div>
                    )}

                    {insiderActivity && insiderActivity.openMarketBuyCount > 0 ? (
                      <>
                        <div className="insider-kpi-grid" style={{ marginBottom: '0.85rem' }}>
                          <div className="insider-kpi-cell">
                            <span className="xbrl-lbl">Open-Market Buys (90d)</span>
                            <strong className="mono ret-pos">
                              {insiderActivity.openMarketBuyCount} buy{insiderActivity.openMarketBuyCount === 1 ? '' : 's'} ·{' '}
                              {formatUsdCompact(insiderActivity.totalBuyValueUsd)}
                            </strong>
                            <span className="mono" style={{ fontSize: '0.7rem', color: 'var(--text-muted)' }}>
                              {insiderActivity.totalBuyShares.toLocaleString()} shares
                            </span>
                          </div>

                          <div className="insider-kpi-cell">
                            <span className="xbrl-lbl">Distinct / C-Suite Buyers</span>
                            <strong className="mono">
                              {insiderActivity.distinctBuyersCount} buyer{insiderActivity.distinctBuyersCount === 1 ? '' : 's'}
                            </strong>
                            <span className="mono" style={{ fontSize: '0.7rem', color: 'var(--text-muted)' }}>
                              {insiderActivity.cSuiteBuyersCount} C-Suite · {insiderActivity.directorBuyersCount} Director
                            </span>
                          </div>

                          <div className="insider-kpi-cell">
                            <span className="xbrl-lbl">Avg Buy Price vs Q-VWAP</span>
                            <strong className="mono">
                              {insiderActivity.avgBuyPriceUsd != null
                                ? `$${insiderActivity.avgBuyPriceUsd.toFixed(2)}`
                                : '—'}
                            </strong>
                            <span
                              className={`mono ${returnClass(buyVsVwapPct != null ? -buyVsVwapPct : null)}`}
                              style={{ fontSize: '0.7rem' }}
                            >
                              {buyVsVwapPct != null
                                ? `${formatSignedPct(buyVsVwapPct, 1)} vs Q-VWAP`
                                : 'Q-VWAP pending'}
                            </span>
                          </div>

                          <div className="insider-kpi-cell">
                            <span className="xbrl-lbl">Plan &amp; Discretionary Sells</span>
                            <strong className="mono">
                              {insiderActivity.non10b5Count} Discretionary buy{insiderActivity.non10b5Count === 1 ? '' : 's'}
                            </strong>
                            <span
                              className={`mono ${insiderActivity.discretionarySellValueUsd > 0 ? 'ret-warn' : ''}`}
                              style={{ fontSize: '0.7rem', color: insiderActivity.discretionarySellValueUsd > 0 ? 'var(--warn-text)' : 'var(--text-muted)' }}
                            >
                              {insiderActivity.discretionarySellValueUsd > 0
                                ? `Disc. sells ${formatUsdCompact(insiderActivity.discretionarySellValueUsd)}`
                                : 'No offsetting disc. sells'}
                            </span>
                          </div>
                        </div>

                        {insiderActivity.recentTransactions.length > 0 && (
                          <div className="table-scroll" style={{ maxHeight: '260px', overflowY: 'auto' }}>
                            <table className="data-table compact" aria-label="Recent Form 4 open-market insider purchases">
                              <thead>
                                <tr>
                                  <th>Tx Date</th>
                                  <th>Insider &amp; Role</th>
                                  <th className="num">Shares</th>
                                  <th className="num">Price</th>
                                  <th className="num">Value</th>
                                  <th>Plan</th>
                                  <th>SEC Form 4</th>
                                </tr>
                              </thead>
                              <tbody>
                                {insiderActivity.recentTransactions.map((tx, idx) => (
                                  <tr key={`${tx.accessionNumber}-${idx}`}>
                                    <td className="mono">{tx.transactionDate || tx.filingDate}</td>
                                    <td>
                                      <div style={{ display: 'flex', alignItems: 'center', gap: '0.35rem', flexWrap: 'wrap' }}>
                                        <span style={{ fontWeight: 600 }}>{tx.insiderName}</span>
                                        {tx.isCsuite && (
                                          <span className="chip pos" style={{ fontSize: '0.64rem' }}>C-Suite</span>
                                        )}
                                      </div>
                                      <div style={{ fontSize: '0.7rem', color: 'var(--text-muted)' }}>
                                        {tx.insiderRole}
                                      </div>
                                    </td>
                                    <td className="num mono">{tx.shares.toLocaleString()}</td>
                                    <td className="num mono">${tx.pricePerShare.toFixed(2)}</td>
                                    <td className="num mono" style={{ fontWeight: 700 }}>
                                      {formatUsdCompact(tx.valueUsd)}
                                    </td>
                                    <td>
                                      <span className={`chip ${tx.is10b5Plan ? '' : 'pos'}`} style={{ fontSize: '0.66rem' }}>
                                        {tx.is10b5Plan ? '10b5-1' : 'Discretionary'}
                                      </span>
                                    </td>
                                    <td>
                                      <a
                                        href={tx.edgarUrl}
                                        target="_blank"
                                        rel="noreferrer"
                                        className="inline-ext-link mono"
                                        style={{ fontSize: '0.75rem' }}
                                      >
                                        Form 4 <ExternalLink size={11} />
                                      </a>
                                    </td>
                                  </tr>
                                ))}
                              </tbody>
                            </table>
                          </div>
                        )}
                      </>
                    ) : (
                      <div style={{ fontSize: '0.82rem', color: 'var(--text-muted)', lineHeight: 1.55 }}>
                        {insiderActivity && insiderActivity.discretionarySellValueUsd > 0 ? (
                          <>
                            No open-market Form 4 (&apos;P&apos;) purchases recorded for {detail.ticker} over the past{' '}
                            {insiderActivity.lookbackDays} days;{' '}
                            <strong className="mono" style={{ color: 'var(--warn-text)' }}>
                              {formatUsdCompact(insiderActivity.discretionarySellValueUsd)}
                            </strong>{' '}
                            in discretionary open-market sales detected (option exercises &apos;M&apos;, RSU grants &apos;A&apos;, tax withholdings &apos;F&apos;, and gifts &apos;G&apos; excluded).
                          </>
                        ) : insiderActivity ? (
                          <>
                            No open-market Form 4 (&apos;P&apos;) insider purchases detected for {detail.ticker} in the{' '}
                            {insiderActivity.lookbackDays}-day window (non-open-market codes M/A/F/G excluded).
                          </>
                        ) : (
                          <>
                            SEC EDGAR Form 4 open-market (&apos;P&apos;) insider purchases (excluding M/A/F/G option exercises and awards) sync automatically on priority Ideal Portfolio, Screener &amp; Consensus tickers during EDGAR data builds.
                          </>
                        )}
                      </div>
                    )}
                  </div>
                </div>

                {/* Two-Column Microstructure + XBRL Fundamentals */}
                <div className="split-grid" style={{ gridTemplateColumns: '1.1fr 1fr' }}>
                  {/* Est Quarter VWAP & Entry Zone */}
                  <div className="panel" style={{ marginBottom: 0 }}>
                    <div className="panel-header">
                      <div>
                        <h3 className="panel-title" style={{ display: 'flex', alignItems: 'center', gap: '0.45rem' }}>
                          <Activity size={16} color="var(--accent-primary)" /> Institutional Quarter Cost-Basis
                        </h3>
                        <p className="panel-subtitle">
                          {latestQuarterLabel} Est. Quarter VWAP &amp; High/Low vs. current price
                        </p>
                      </div>
                      {entryZone && (
                        <span className={`chip ${entryZoneChipClass(entryZone)}`}>
                          {entryZone} {pctVsVwap != null ? `(${formatSignedPct(pctVsVwap)})` : ''}
                        </span>
                      )}
                    </div>
                    <div style={{ padding: '1.05rem 1.25rem' }}>
                      {detail.estQuarterVwap != null ? (
                        <>
                          <div className="vwap-kpis mono">
                            <div>
                              <span className="vwap-k-lbl">Q-Low</span>
                              <strong>${detail.quarterLow?.toFixed(2) ?? '—'}</strong>
                            </div>
                            <div>
                              <span className="vwap-k-lbl">Est. Q-VWAP</span>
                              <strong style={{ color: 'var(--accent-primary)' }}>
                                ${detail.estQuarterVwap.toFixed(2)}
                              </strong>
                            </div>
                            <div>
                              <span className="vwap-k-lbl">Q-High</span>
                              <strong>${detail.quarterHigh?.toFixed(2) ?? '—'}</strong>
                            </div>
                            <div>
                              <span className="vwap-k-lbl">Now vs Q-Avg</span>
                              <strong className={returnClass(pctVsVwap != null ? -pctVsVwap : null)}>
                                {pctVsVwap != null ? formatSignedPct(pctVsVwap) : '—'}
                              </strong>
                            </div>
                          </div>

                          {vwapGeometry && (
                            <div className="vwap-range-wrap" aria-label="Quarter VWAP price range">
                              <div className="vwap-track">
                                <div
                                  className="vwap-qband"
                                  style={{
                                    left: `${vwapGeometry.qStartPct}%`,
                                    width: `${Math.max(2, vwapGeometry.qEndPct - vwapGeometry.qStartPct)}%`,
                                  }}
                                  title={`${latestQuarterLabel} Low-High Range`}
                                />
                                <div
                                  className="vwap-marker"
                                  style={{ left: `${vwapGeometry.vwapPct}%` }}
                                  title={`Est. Q-VWAP: $${detail.estQuarterVwap.toFixed(2)}`}
                                />
                                {vwapGeometry.pricePct !== null && detail.priceUsd && (
                                  <div
                                    className={`vwap-now-pin ${entryZoneChipClass(entryZone)}`}
                                    style={{ left: `${vwapGeometry.pricePct}%` }}
                                    title={`Current Price: $${detail.priceUsd.toFixed(2)}`}
                                  />
                                )}
                              </div>
                              <div className="vwap-legend">
                                <span><i className="vwap-dot band" /> Q-Low..High Window</span>
                                <span><i className="vwap-dot avg" /> Est. Q-VWAP</span>
                                <span><i className="vwap-dot now" /> Current Price</span>
                              </div>
                            </div>
                          )}

                          <p className="vwap-caption">
                            Computed from split-adjusted daily OHLCV volume-weighted closes during the latest 13F filing quarter. Helps separate disciplined accumulation near/below institutional cost basis from chasing 30%+ breakouts after the 45-day filing lag.
                          </p>
                        </>
                      ) : (
                        <div style={{ color: 'var(--text-muted)', fontSize: '0.84rem' }}>
                          Quarter OHLCV microstructure data not available for {detail.ticker}.
                        </div>
                      )}
                    </div>
                  </div>

                  {/* SEC EDGAR XBRL Fundamentals */}
                  <div className="panel" style={{ marginBottom: 0 }}>
                    <div className="panel-header">
                      <div>
                        <h3 className="panel-title" style={{ display: 'flex', alignItems: 'center', gap: '0.45rem' }}>
                          <BarChart3 size={16} color="var(--accent-primary)" /> SEC XBRL Fundamentals
                        </h3>
                        <p className="panel-subtitle">Direct from SEC EDGAR CompanyFacts API</p>
                      </div>
                      {detail.marketCapUsd != null && (
                        <span className="chip mono">
                          MCap {formatUsdCompact(detail.marketCapUsd)}
                        </span>
                      )}
                    </div>
                    <div style={{ padding: '1.05rem 1.25rem' }}>
                      {detail.fundamentals ? (
                        <div className="xbrl-grid">
                          <div className="xbrl-cell">
                            <span className="xbrl-lbl">TTM Revenue</span>
                            <strong className="mono">{formatUsdCompact(detail.fundamentals.revenueTtmUsd)}</strong>
                          </div>
                          <div className="xbrl-cell">
                            <span className="xbrl-lbl">Rev Growth (YoY)</span>
                            <strong className={`mono ${returnClass(detail.fundamentals.revenueGrowthYoYPct)}`}>
                              {formatSignedPct(detail.fundamentals.revenueGrowthYoYPct)}
                            </strong>
                          </div>
                          <div className="xbrl-cell">
                            <span className="xbrl-lbl">Operating Margin</span>
                            <strong className={`mono ${returnClass(detail.fundamentals.operatingMarginPct)}`}>
                              {detail.fundamentals.operatingMarginPct != null
                                ? formatPct(detail.fundamentals.operatingMarginPct, 1)
                                : '—'}
                            </strong>
                          </div>
                          <div className="xbrl-cell">
                            <span className="xbrl-lbl">FCF Margin</span>
                            <strong className={`mono ${returnClass(detail.fundamentals.fcfMarginPct)}`}>
                              {detail.fundamentals.fcfMarginPct != null
                                ? formatPct(detail.fundamentals.fcfMarginPct, 1)
                                : '—'}
                            </strong>
                          </div>
                          <div className="xbrl-cell">
                            <span className="xbrl-lbl">R&amp;D Intensity</span>
                            <strong className="mono">
                              {detail.fundamentals.rdIntensityPct != null
                                ? formatPct(detail.fundamentals.rdIntensityPct, 1)
                                : '—'}
                            </strong>
                          </div>
                          <div className="xbrl-cell">
                            <span className="xbrl-lbl">Share Dilution YoY</span>
                            <strong
                              className={`mono ${
                                detail.fundamentals.shareDilutionYoYPct == null
                                  ? ''
                                  : detail.fundamentals.shareDilutionYoYPct <= 0
                                    ? 'ret-pos'
                                    : detail.fundamentals.shareDilutionYoYPct > 3
                                      ? 'ret-neg'
                                      : ''
                              }`}
                            >
                              {detail.fundamentals.shareDilutionYoYPct != null
                                ? formatSignedPct(detail.fundamentals.shareDilutionYoYPct)
                                : '—'}
                            </strong>
                          </div>
                        </div>
                      ) : (
                        <div style={{ color: 'var(--text-muted)', fontSize: '0.84rem', lineHeight: 1.55 }}>
                          SEC EDGAR US-GAAP / IFRS XBRL fundamentals are parsed for prioritized core/signal US issuers. Foreign private issuers or newly added single-fund tickers may omit structured CompanyFacts tags.
                        </div>
                      )}
                    </div>
                  </div>
                </div>

                {/* Mid-Quarter Schedule 13D / 13G Alerts if any */}
                {detail.midQuarterAlerts.length > 0 && (
                  <div className="panel" style={{ marginTop: '1.15rem' }}>
                    <div className="panel-header">
                      <div>
                        <h3 className="panel-title" style={{ display: 'flex', alignItems: 'center', gap: '0.45rem' }}>
                          <Radio size={15} color="var(--warn-text)" /> Live Mid-Quarter Schedule 13D / 13G Filings
                        </h3>
                        <p className="panel-subtitle">
                          Elite-50 ≥5% beneficial ownership stakes filed ahead of quarterly 13F
                        </p>
                      </div>
                    </div>
                    <div className="alerts-strip-list" style={{ padding: '0.85rem 1.25rem' }}>
                      {detail.midQuarterAlerts.map((al) => (
                        <div
                          key={al.id}
                          className="alert-row-card"
                          style={{ flexDirection: 'column', alignItems: 'stretch', gap: '0.45rem' }}
                        >
                          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '0.75rem', flexWrap: 'wrap' }}>
                            <div style={{ display: 'flex', alignItems: 'center', gap: '0.45rem', flexWrap: 'wrap' }}>
                              <span className={`chip ${al.isActivist ? 'neg' : 'warn'}`}>{al.form}</span>
                              {al.stakeStatus && (
                                <span className={`chip ${stakeStatusChipClass(al.stakeStatus)}`}>
                                  {al.stakeStatus}
                                </span>
                              )}
                              {al.isActivist && al.item4Category && (
                                <span className={`chip ${item4CategoryChipClass(al.item4Category)}`}>
                                  {al.item4Category}
                                </span>
                              )}
                              {al.percentOfClass != null && (
                                <span className="chip mono">{al.percentOfClass.toFixed(1)}%</span>
                              )}
                              {al.sharesOwned != null && al.sharesOwned > 0 && (
                                <span className="mono" style={{ fontSize: '0.74rem', color: 'var(--text-muted)' }}>
                                  {compactShares(al.sharesOwned)} sh
                                </span>
                              )}
                              <button
                                type="button"
                                className="fund-link-btn"
                                style={{ fontSize: '0.86rem' }}
                                onClick={() => onSelectFund(al.filerCik)}
                              >
                                {al.fundName} <ArrowUpRight size={13} />
                              </button>
                            </div>
                            <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem' }}>
                              <span className="mono" style={{ fontSize: '0.78rem', color: 'var(--text-secondary)' }}>
                                {al.eventDate ? `Event ${al.eventDate} · ` : ''}Filed {al.filingDate}
                              </span>
                              <a
                                href={al.filingDocUrl || al.edgarUrl}
                                target="_blank"
                                rel="noreferrer"
                                className="inline-ext-link mono"
                                style={{ fontSize: '0.76rem' }}
                              >
                                {al.accessionNumber} <ExternalLink size={11} />
                              </a>
                            </div>
                          </div>
                          {al.item4Intent && (
                            <div className="item4-intent-box">
                              <strong>Item 4 Intent:</strong> &ldquo;{al.item4Intent}&rdquo;
                            </div>
                          )}
                        </div>
                      ))}
                    </div>
                  </div>
                )}

                {/* Institutional Owners Table */}
                <div className="panel" style={{ marginTop: '1.15rem' }}>
                  <div className="panel-header">
                    <div>
                      <h3 className="panel-title" style={{ display: 'flex', alignItems: 'center', gap: '0.45rem' }}>
                        <Building2 size={16} color="var(--accent-primary)" /> Institutional Holders ({filteredHolders.length} of {detail.holders.length})
                      </h3>
                      <p className="panel-subtitle">
                        Click any manager to open their full 13F portfolio, risk profile &amp; equity curve
                      </p>
                    </div>
                    <div className="segmented-group">
                      <button
                        type="button"
                        className={`seg-btn ${holderFilter === 'ALL' ? 'active' : ''}`}
                        onClick={() => setHolderFilter('ALL')}
                      >
                        All ({detail.holders.length})
                      </button>
                      <button
                        type="button"
                        className={`seg-btn ${holderFilter === 'ELITE' ? 'active' : ''}`}
                        onClick={() => setHolderFilter('ELITE')}
                      >
                        Elite 50 ({detail.eliteHoldersCount})
                      </button>
                    </div>
                  </div>

                  <div className="table-scroll" style={{ maxHeight: '360px', overflowY: 'auto' }}>
                    <table className="data-table compact">
                      <thead>
                        <tr>
                          <th>Fund</th>
                          <th>Tier</th>
                          <th className="num">Skill</th>
                          <th className="num">Eq. Weight</th>
                          <th className="num">Value</th>
                          <th className="num">QoQ Shares</th>
                          <th className="num">Weight Δ</th>
                          <th>Status</th>
                        </tr>
                      </thead>
                      <tbody>
                        {filteredHolders.map((h) => (
                          <tr key={h.cik}>
                            <td>
                              <button
                                type="button"
                                className="fund-link-btn"
                                style={{ fontSize: '0.84rem' }}
                                onClick={() => onSelectFund(h.cik)}
                              >
                                {h.fundName} <ArrowUpRight size={13} color="var(--accent-primary)" />
                              </button>
                            </td>
                            <td>
                              <span className={`chip ${tierChipClass(h.fundTier)}`}>{h.fundTier}</span>
                            </td>
                            <td className="num mono">
                              {h.skillScore != null ? h.skillScore.toFixed(0) : '—'}
                            </td>
                            <td className="num mono" style={{ fontWeight: 700 }}>
                              {h.weightPct.toFixed(2)}%
                            </td>
                            <td className="num mono">{formatUsdCompact(h.valueUsd)}</td>
                            <td className={`num mono ${returnClass(h.status === 'NEW' ? 1 : h.qoqSharesChangePct)}`}>
                              {h.status === 'NEW' ? 'NEW' : formatSignedPct(h.qoqSharesChangePct, 1)}
                            </td>
                            <td className={`num mono ${returnClass(h.qoqWeightDeltaPct)}`}>
                              {h.qoqWeightDeltaPct != null ? `${h.qoqWeightDeltaPct > 0 ? '+' : ''}${h.qoqWeightDeltaPct.toFixed(2)}` : '—'}
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
                        {filteredHolders.length === 0 && (
                          <tr>
                            <td colSpan={8} style={{ textAlign: 'center', padding: '1.5rem', color: 'var(--text-muted)' }}>
                              No {holderFilter === 'ELITE' ? 'Elite 50 ' : ''}current holders recorded for {detail.ticker}.
                            </td>
                          </tr>
                        )}
                      </tbody>
                    </table>
                  </div>
                </div>

                {/* Recent QoQ Moves Table */}
                {detail.recentMoves.length > 0 && (
                  <div className="panel" style={{ marginTop: '1.15rem' }}>
                    <div className="panel-header">
                      <div>
                        <h3 className="panel-title" style={{ display: 'flex', alignItems: 'center', gap: '0.45rem' }}>
                          <Layers size={16} color="var(--accent-primary)" /> Latest QoQ Trading Activity ({detail.recentMoves.length})
                        </h3>
                        <p className="panel-subtitle">
                          All ±5%+ share-count changes &amp; exits across tracked 13F updates
                        </p>
                      </div>
                    </div>

                    <div className="table-scroll" style={{ maxHeight: '320px', overflowY: 'auto' }}>
                      <table className="data-table compact">
                        <thead>
                          <tr>
                            <th>Fund</th>
                            <th>Tier</th>
                            <th>Quarter</th>
                            <th>Action</th>
                            <th className="num">Shs Δ %</th>
                            <th className="num">Value Δ</th>
                            <th className="num">Book % Shift</th>
                          </tr>
                        </thead>
                        <tbody>
                          {detail.recentMoves.map((mv, idx) => (
                            <tr key={`${mv.cik}-${idx}`}>
                              <td>
                                <button
                                  type="button"
                                  className="fund-link-btn"
                                  style={{ fontSize: '0.84rem' }}
                                  onClick={() => onSelectFund(mv.cik)}
                                >
                                  {mv.fundName} <ArrowUpRight size={13} color="var(--accent-primary)" />
                                </button>
                              </td>
                              <td>
                                <span className={`chip ${tierChipClass(mv.fundTier)}`}>{mv.fundTier}</span>
                              </td>
                              <td className="mono">{mv.quarterLabel}</td>
                              <td>
                                <span
                                  className={`chip ${
                                    mv.action === 'NEW' || mv.action === 'ADDED'
                                      ? 'pos'
                                      : 'neg'
                                  }`}
                                >
                                  {mv.action}
                                </span>
                              </td>
                              <td className={`num mono ${returnClass(mv.action === 'NEW' ? 1 : mv.sharesDeltaPct)}`}>
                                {mv.action === 'NEW'
                                  ? 'NEW'
                                  : mv.action === 'EXITED'
                                    ? 'EXITED'
                                    : formatSignedPct(mv.sharesDeltaPct, 1)}
                              </td>
                              <td className={`num mono ${returnClass(mv.valueDeltaUsd)}`}>
                                <div>
                                  {mv.valueDeltaUsd > 0 ? '+' : mv.valueDeltaUsd < 0 ? '-' : ''}
                                  {formatUsdCompact(Math.abs(mv.valueDeltaUsd))}
                                </div>
                                {mv.estFlowUsd != null && mv.estFlowUsd !== 0 && (
                                  <div className={returnClass(mv.estFlowUsd)} style={{ fontSize: '0.68rem' }}>
                                    Flow {formatSignedUsd(mv.estFlowUsd)}
                                  </div>
                                )}
                              </td>
                              <td className="num mono">
                                {mv.prevBookPct.toFixed(1)}% → <strong>{mv.currBookPct.toFixed(1)}%</strong>
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  </div>
                )}
              </>
            )
          )}
        </div>
      </div>
    </div>
  );
};
