import type { FC } from 'react';
import {
  ArrowRight,
  ArrowUpRight,
  Layers,
  Shield,
  TrendingDown,
  TrendingUp,
} from 'lucide-react';
import type { FundUpdateSummary, PositionMoveItem, StockIndexEntry } from '../types/data';
import { formatUsdCompact } from '../utils/format';

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

interface UpdateSummaryContentProps {
  update: FundUpdateSummary;
  stocksIndex?: Record<string, StockIndexEntry>;
  onDiveDeeper: (cik: string) => void;
  onOpenFundProfile?: (cik: string) => void;
  onSelectStock?: (ticker: string) => void;
}

export const UpdateSummaryContent: FC<UpdateSummaryContentProps> = ({
  update: u,
  stocksIndex,
  onDiveDeeper,
  onOpenFundProfile,
  onSelectStock,
}) => {
  const hasDerivativeBook =
    u.currCallBookPct > 0 || u.currPutBookPct > 0 || u.prevCallBookPct > 0 || u.prevPutBookPct > 0;
  const totalMix = Math.max(0.01, u.currEquityBookPct + u.currCallBookPct + u.currPutBookPct);

  const renderMoveTicker = (m: PositionMoveItem) => {
    const canOpen = Boolean(onSelectStock && stocksIndex?.[m.ticker]);
    const label = `${m.ticker}${m.securityType !== 'EQUITY' ? ` ${m.securityType}` : ''}`;
    if (canOpen) {
      return (
        <button
          type="button"
          className="ticker-link-inline mono mp-tick"
          onClick={() => onSelectStock?.(m.ticker)}
          title={`Open ${m.ticker} Stock Dossier`}
        >
          {label}
        </button>
      );
    }
    return <span className="mono mp-tick">{label}</span>;
  };

  return (
    <>
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
                    {onSelectStock && stocksIndex?.[vs.ticker] ? (
                      <button
                        type="button"
                        className="ticker-link-inline mono"
                        onClick={() => onSelectStock(vs.ticker)}
                      >
                        <strong>{vs.ticker}</strong>
                      </button>
                    ) : (
                      <strong>{vs.ticker}</strong>
                    )}
                    : {vs.tagNote}
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
                    {onSelectStock && stocksIndex?.[sig.ticker] ? (
                      <button
                        type="button"
                        className="ticker-link-inline"
                        onClick={() => onSelectStock(sig.ticker)}
                      >
                        <strong>{sig.label}</strong>
                      </button>
                    ) : (
                      <strong>{sig.label}</strong>
                    )}{' '}
                    — {sig.detail}
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
                {renderMoveTicker(m)}
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
                {renderMoveTicker(m)}
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
                {renderMoveTicker(m)}
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
                {renderMoveTicker(m)}
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
          {onOpenFundProfile && (
            <button
              type="button"
              className="action-btn"
              onClick={() => onOpenFundProfile(u.cik)}
            >
              Fund Performance <ArrowUpRight size={13} />
            </button>
          )}
          <button
            type="button"
            className="action-btn primary dive-deeper-btn"
            onClick={() => onDiveDeeper(u.cik)}
          >
            Dive Deeper <ArrowRight size={14} />
          </button>
        </div>
      </div>
    </>
  );
};
