import { useMemo, useState, type FC } from 'react';
import type { EquityPoint, ReturnWindow } from '../types/data';
import { formatSignedPct, sectorColor } from '../utils/format';

interface MiniSparklineProps {
  points: EquityPoint[];
  positive: boolean;
  width?: number;
  height?: number;
}

export const MiniSparkline: FC<MiniSparklineProps> = ({
  points,
  positive,
  width = 104,
  height = 28,
}) => {
  if (!points || points.length < 2) {
    return <span className="ret-muted mono">—</span>;
  }

  // Show trailing ~1Y by date so weekly and compacted biweekly curves span an identical 1Y window
  const lastDate = points[points.length - 1].date;
  const oneYearCutoff = `${Number(lastDate.slice(0, 4)) - 1}${lastDate.slice(4)}`;
  const trailingYear = points.filter((p) => p.date >= oneYearCutoff);
  const slice = trailingYear.length >= 2 ? trailingYear : points.slice(-60);
  const vals = slice.map((p) => p.nav);
  const minV = Math.min(...vals);
  const maxV = Math.max(...vals);
  const span = maxV - minV || 1;
  const pad = 2;

  const coords = slice.map((p, idx) => {
    const x = pad + (idx / (slice.length - 1)) * (width - 2 * pad);
    const y = pad + (1 - (p.nav - minV) / span) * (height - 2 * pad);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  });

  const stroke = positive ? 'var(--pos-text)' : 'var(--neg-text)';
  return (
    <svg
      width={width}
      height={height}
      viewBox={`0 0 ${width} ${height}`}
      aria-hidden="true"
      style={{ display: 'block' }}
    >
      <polyline
        fill="none"
        stroke={stroke}
        strokeWidth="1.65"
        strokeLinecap="round"
        strokeLinejoin="round"
        points={coords.join(' ')}
      />
    </svg>
  );
};

export interface CurveSeriesInput {
  id: string;
  label: string;
  color: string;
  dashed?: boolean;
  points: EquityPoint[];
}

interface EquityCurveChartProps {
  series: CurveSeriesInput[];
  height?: number;
  defaultWindow?: ReturnWindow;
}

function windowCutoffDate(latestDate: string, win: ReturnWindow): string {
  const dt = new Date(`${latestDate}T00:00:00Z`);
  if (win === 'YTD') {
    return `${dt.getUTCFullYear() - 1}-12-31`;
  }
  const monthsMap: Record<Exclude<ReturnWindow, 'YTD'>, number> = {
    TTM: 12,
    '18M': 18,
    '2Y': 24,
    '3Y': 36,
    '5Y': 60,
  };
  const m = monthsMap[win];
  dt.setUTCMonth(dt.getUTCMonth() - m);
  return dt.toISOString().slice(0, 10);
}

export const EquityCurveChart: FC<EquityCurveChartProps> = ({
  series,
  height = 260,
  defaultWindow = '3Y',
}) => {
  const [win, setWin] = useState<ReturnWindow>(defaultWindow);
  const [hoverIdx, setHoverIdx] = useState<number | null>(null);

  const primary = useMemo(() => series[0]?.points ?? [], [series]);
  const latestDate = primary.length > 0 ? primary[primary.length - 1].date : '2026-11-16';
  const cutoff = useMemo(() => windowCutoffDate(latestDate, win), [latestDate, win]);

  // Align all series to common dates >= cutoff (bounded by primary's earliest date)
  const chartData = useMemo(() => {
    if (primary.length < 2) return null;
    const primaryStart = primary[0].date;
    const effCutoff = cutoff > primaryStart ? cutoff : primaryStart;

    const primaryFiltered = primary.filter((p) => p.date >= effCutoff);
    if (primaryFiltered.length < 2) return null;

    const dates = primaryFiltered.map((p) => p.date);
    const rebased = series.map((s) => {
      const pts = s.points;
      let ptr = 0;
      let baseVal: number | null = null;
      const pctSeries: Array<number | null> = [];

      for (const d of dates) {
        while (ptr + 1 < pts.length && pts[ptr + 1].date <= d) {
          ptr++;
        }
        const curPt = pts[ptr];
        const v = curPt && curPt.date <= d ? curPt.nav : undefined;
        if (v !== undefined) {
          if (baseVal === null) baseVal = v;
        }
        if (baseVal !== null && v !== undefined && baseVal > 0) {
          pctSeries.push(((v / baseVal) - 1) * 100);
        } else {
          pctSeries.push(null);
        }
      }
      return {
        ...s,
        values: pctSeries,
      };
    });

    const allVals: number[] = [];
    for (const r of rebased) {
      for (const v of r.values) {
        if (v !== null) allVals.push(v);
      }
    }
    if (allVals.length === 0) return null;
    const minY = Math.min(0, ...allVals);
    const maxY = Math.max(0, ...allVals);
    const padY = Math.max(2, (maxY - minY) * 0.08);

    return {
      dates,
      rebased,
      minY: minY - padY,
      maxY: maxY + padY,
    };
  }, [series, primary, cutoff]);

  const width = 760;
  const padL = 54;
  const padR = 18;
  const padT = 18;
  const padB = 28;
  const plotW = width - padL - padR;
  const plotH = height - padT - padB;

  if (!chartData) {
    return (
      <div style={{ padding: '1rem', color: 'var(--text-muted)', fontSize: '0.85rem' }}>
        Insufficient overlapping price history for this window.
      </div>
    );
  }

  const { dates, rebased, minY, maxY } = chartData;
  const ySpan = maxY - minY || 1;
  const xAt = (i: number) => padL + (i / Math.max(1, dates.length - 1)) * plotW;
  const yAt = (v: number) => padT + (1 - (v - minY) / ySpan) * plotH;

  const yTicks = [minY, (minY + maxY) / 2, maxY];
  const activeIdx = hoverIdx !== null ? Math.min(dates.length - 1, Math.max(0, hoverIdx)) : dates.length - 1;

  const handlePointerPosition = (svgEl: SVGSVGElement, clientX: number) => {
    const rect = svgEl.getBoundingClientRect();
    if (rect.width <= 0) return;
    const relX = ((clientX - rect.left) / rect.width) * width;
    const frac = (relX - padL) / plotW;
    const idx = Math.round(frac * (dates.length - 1));
    setHoverIdx(Math.min(dates.length - 1, Math.max(0, idx)));
  };

  const activeSeriesSummary = rebased
    .map((r) => `${r.label} ${formatSignedPct(r.values[activeIdx], 1)}`)
    .join(', ');

  return (
    <div className="chart-box">
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '0.6rem', flexWrap: 'wrap', gap: '0.5rem' }}>
        <div className="segmented-group" role="group" aria-label="Chart timeframe">
          {(['YTD', 'TTM', '18M', '2Y', '3Y', '5Y'] as ReturnWindow[]).map((w) => (
            <button
              key={w}
              type="button"
              className={`seg-btn ${win === w ? 'active' : ''}`}
              onClick={() => {
                setWin(w);
                setHoverIdx(null);
              }}
            >
              {w}
            </button>
          ))}
        </div>

        <div className="mono" style={{ fontSize: '0.78rem', color: 'var(--text-secondary)', display: 'flex', gap: '0.9rem', flexWrap: 'wrap' }}>
          <span style={{ color: 'var(--text-muted)' }}>{dates[activeIdx]}</span>
          {rebased.map((r) => {
            const val = r.values[activeIdx];
            return (
              <span key={r.id} style={{ color: r.color, fontWeight: 600 }}>
                {r.label}: {formatSignedPct(val, 1)}
              </span>
            );
          })}
        </div>
      </div>

      <svg
        className="chart-svg"
        viewBox={`0 0 ${width} ${height}`}
        tabIndex={0}
        role="img"
        aria-label={`Cumulative return chart as of ${dates[activeIdx]}: ${activeSeriesSummary}. Use Left and Right arrow keys to scrub dates.`}
        style={{ width: '100%', height: 'auto', display: 'block', cursor: 'crosshair', touchAction: 'none' }}
        onPointerDown={(e) => {
          try {
            e.currentTarget.setPointerCapture?.(e.pointerId);
          } catch {
            // Ignore environments without pointer capture
          }
          handlePointerPosition(e.currentTarget, e.clientX);
        }}
        onPointerMove={(e) => handlePointerPosition(e.currentTarget, e.clientX)}
        onPointerUp={(e) => {
          try {
            if (e.currentTarget.hasPointerCapture?.(e.pointerId)) {
              e.currentTarget.releasePointerCapture(e.pointerId);
            }
          } catch {
            // Ignore
          }
          if (e.pointerType !== 'mouse') setHoverIdx(null);
        }}
        onPointerCancel={() => setHoverIdx(null)}
        onPointerLeave={(e) => {
          if (!e.pointerType || e.pointerType === 'mouse') setHoverIdx(null);
        }}
        onMouseMove={(e) => handlePointerPosition(e.currentTarget, e.clientX)}
        onMouseLeave={() => setHoverIdx(null)}
        onKeyDown={(e) => {
          if (dates.length === 0) return;
          if (e.key === 'ArrowLeft') {
            e.preventDefault();
            setHoverIdx((prev) => Math.max(0, (prev ?? dates.length - 1) - 1));
          } else if (e.key === 'ArrowRight') {
            e.preventDefault();
            setHoverIdx((prev) =>
              prev === null ? dates.length - 1 : Math.min(dates.length - 1, prev + 1)
            );
          } else if (e.key === 'Home') {
            e.preventDefault();
            setHoverIdx(0);
          } else if (e.key === 'End') {
            e.preventDefault();
            setHoverIdx(dates.length - 1);
          } else if (e.key === 'Escape') {
            e.preventDefault();
            setHoverIdx(null);
          }
        }}
      >
        {/* Horizontal gridlines */}
        {yTicks.map((tv, i) => {
          const y = yAt(tv);
          return (
            <g key={i}>
              <line x1={padL} y1={y} x2={width - padR} y2={y} stroke="var(--chart-grid)" strokeDasharray="3 3" />
              <text x={padL - 8} y={y + 4} textAnchor="end" fontSize="10" fill="var(--text-muted)" fontFamily="-apple-system, BlinkMacSystemFont, 'SF Pro Text', 'Inter', sans-serif">
                {tv >= 0 ? `+${tv.toFixed(0)}%` : `${tv.toFixed(0)}%`}
              </text>
            </g>
          );
        })}

        {/* Zero baseline */}
        {minY < 0 && maxY > 0 && (
          <line
            x1={padL}
            y1={yAt(0)}
            x2={width - padR}
            y2={yAt(0)}
            stroke="var(--chart-baseline)"
            strokeWidth="1"
          />
        )}

        {/* X-axis boundary labels */}
        <text x={padL} y={height - 6} fontSize="10" fill="var(--text-muted)" fontFamily="-apple-system, BlinkMacSystemFont, 'SF Pro Text', 'Inter', sans-serif">
          {dates[0]}
        </text>
        <text
          x={padL + plotW / 2}
          y={height - 6}
          textAnchor="middle"
          fontSize="10"
          fill="var(--text-muted)"
          fontFamily="-apple-system, BlinkMacSystemFont, 'SF Pro Text', 'Inter', sans-serif"
        >
          {dates[Math.floor(dates.length / 2)]}
        </text>
        <text x={width - padR} y={height - 6} textAnchor="end" fontSize="10" fill="var(--text-muted)" fontFamily="-apple-system, BlinkMacSystemFont, 'SF Pro Text', 'Inter', sans-serif">
          {dates[dates.length - 1]}
        </text>

        {/* Contiguous path segments broken across null gaps */}
        {rebased.map((r) => {
          const pathCommands: string[] = [];
          let inSegment = false;
          let segmentPoints = 0;
          r.values.forEach((v, idx) => {
            if (v === null) {
              inSegment = false;
              return;
            }
            segmentPoints += 1;
            const cmd = inSegment ? 'L' : 'M';
            pathCommands.push(`${cmd}${xAt(idx).toFixed(1)},${yAt(v).toFixed(1)}`);
            inSegment = true;
          });
          if (segmentPoints < 2) return null;
          return (
            <path
              key={r.id}
              data-series-id={r.id}
              fill="none"
              stroke={r.color}
              strokeWidth={r.dashed ? '1.75' : '2.4'}
              strokeDasharray={r.dashed ? '5 4' : undefined}
              strokeLinejoin="round"
              strokeLinecap="round"
              d={pathCommands.join(' ')}
            />
          );
        })}

        {/* Hover / Keyboard vertical crosshair */}
        {hoverIdx !== null && (
          <g data-testid="chart-crosshair">
            <line
              x1={xAt(activeIdx)}
              y1={padT}
              x2={xAt(activeIdx)}
              y2={padT + plotH}
              stroke="var(--text-primary)"
              strokeWidth="1"
              strokeDasharray="3 3"
            />
            {rebased.map((r) => {
              const v = r.values[activeIdx];
              if (v === null) return null;
              return (
                <circle
                  key={r.id}
                  cx={xAt(activeIdx)}
                  cy={yAt(v)}
                  r="3.8"
                  fill={r.color}
                  stroke="var(--bg-surface)"
                  strokeWidth="1.5"
                />
              );
            })}
          </g>
        )}
      </svg>

      <div className="chart-legend">
        {rebased.map((r) => (
          <span key={r.id} className="legend-item">
            <span
              className="legend-swatch"
              style={{
                background: r.dashed ? 'transparent' : r.color,
                borderTop: r.dashed ? `2px dashed ${r.color}` : 'none',
              }}
            />
            {r.label}
          </span>
        ))}
      </div>
    </div>
  );
};

interface SectorBarProps {
  sectors: Array<{ sector: string; weightPct: number; stockCount?: number }>;
}

export const SectorBreakdownBar: FC<SectorBarProps> = ({ sectors }) => {
  const total = sectors.reduce((acc, s) => acc + s.weightPct, 0) || 100;
  const topSectors = sectors.slice(0, 6);
  const restSectors = sectors.slice(6);
  const otherWeight = restSectors.reduce((acc, s) => acc + s.weightPct, 0);
  const otherStocks = restSectors.reduce((acc, s) => acc + (s.stockCount ?? 0), 0);
  const hasStockCounts = restSectors.some((s) => s.stockCount !== undefined);

  return (
    <div>
      <div className="sector-stack" role="img" aria-label="Sector weight breakdown">
        {sectors.map((s) => (
          <div
            key={s.sector}
            title={`${s.sector}: ${s.weightPct.toFixed(1)}%`}
            style={{
              width: `${(s.weightPct / total) * 100}%`,
              backgroundColor: sectorColor(s.sector),
            }}
          />
        ))}
      </div>
      <div className="sector-legend">
        {topSectors.map((s) => (
          <span key={s.sector}>
            <span className="sector-dot" style={{ backgroundColor: sectorColor(s.sector) }} />
            <strong>{s.sector}</strong>{' '}
            <span className="mono">{s.weightPct.toFixed(1)}%</span>
            {s.stockCount !== undefined && (
              <span style={{ color: 'var(--text-muted)' }}> ({s.stockCount})</span>
            )}
          </span>
        ))}
        {restSectors.length > 0 && (
          <span key="other-bucket-summary" title={restSectors.map((r) => `${r.sector}: ${r.weightPct.toFixed(1)}%`).join(', ')}>
            <span className="sector-dot" style={{ backgroundColor: sectorColor('Other') }} />
            <strong>Other ({restSectors.length})</strong>{' '}
            <span className="mono">{otherWeight.toFixed(1)}%</span>
            {hasStockCounts && (
              <span style={{ color: 'var(--text-muted)' }}> ({otherStocks})</span>
            )}
          </span>
        )}
      </div>
    </div>
  );
};
