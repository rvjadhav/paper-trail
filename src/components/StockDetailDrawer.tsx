import { useEffect, useMemo, useState, type FC } from 'react';
import {
  Activity,
  ArrowUpRight,
  BarChart3,
  Building2,
  ExternalLink,
  Layers,
  PieChart,
  Radio,
  X,
} from 'lucide-react';
import type {
  StockDetail,
  StockIndexEntry,
} from '../types/data';
import { fetchStockDetail } from '../utils/dataLoader';
import {
  entryZoneChipClass,
  formatPct,
  formatSignedPct,
  formatUsdCompact,
  returnClass,
  tierChipClass,
} from '../utils/format';

interface StockDetailDrawerProps {
  ticker: string;
  stocksIndex: Record<string, StockIndexEntry>;
  latestQuarterLabel: string;
  onClose: () => void;
  onSelectFund: (cik: string) => void;
  onJumpToIdeal?: () => void;
}

export const StockDetailDrawer: FC<StockDetailDrawerProps> = ({
  ticker,
  stocksIndex,
  latestQuarterLabel,
  onClose,
  onSelectFund,
  onJumpToIdeal,
}) => {
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
      <div className="drawer-panel">
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
              <div className="metric-sub">Across {latestQuarterLabel} updates</div>
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
                {/* Two-Column Microstructure + XBRL Fundamentals */}
                <div className="split-grid" style={{ gridTemplateColumns: '1.1fr 1fr' }}>
                  {/* Est Quarter VWAP & Entry Zone */}
                  <div className="panel" style={{ marginBottom: 0 }}>
                    <div className="panel-header">
                      <div>
                        <h3 className="panel-title" style={{ display: 'flex', alignItems: 'center', gap: '0.45rem' }}>
                          <Activity size={16} color="#0071e3" /> Institutional Quarter Cost-Basis
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
                          <BarChart3 size={16} color="#0071e3" /> SEC XBRL Fundamentals
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
                          <Radio size={15} color="#b25000" /> Live Mid-Quarter Schedule 13D / 13G Filings
                        </h3>
                        <p className="panel-subtitle">
                          Elite-50 ≥5% beneficial ownership stakes filed ahead of quarterly 13F
                        </p>
                      </div>
                    </div>
                    <div className="alerts-strip-list" style={{ padding: '0.85rem 1.25rem' }}>
                      {detail.midQuarterAlerts.map((al) => (
                        <div key={al.id} className="alert-row-card">
                          <div style={{ display: 'flex', alignItems: 'center', gap: '0.45rem', flexWrap: 'wrap' }}>
                            <span className={`chip ${al.isActivist ? 'neg' : 'warn'}`}>{al.form}</span>
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

                {/* Institutional Owners Table */}
                <div className="panel" style={{ marginTop: '1.15rem' }}>
                  <div className="panel-header">
                    <div>
                      <h3 className="panel-title" style={{ display: 'flex', alignItems: 'center', gap: '0.45rem' }}>
                        <Building2 size={16} color="#0071e3" /> Institutional Holders ({filteredHolders.length} of {detail.holders.length})
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
                          <Layers size={16} color="#0071e3" /> Latest QoQ Trading Activity ({detail.recentMoves.length})
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
                                {mv.valueDeltaUsd > 0 ? '+' : mv.valueDeltaUsd < 0 ? '-' : ''}
                                {formatUsdCompact(Math.abs(mv.valueDeltaUsd))}
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
