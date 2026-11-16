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

  // Show trailing ~1Y (approx 52 weekly samples) for crisp sparkline legibility
  const slice = points.slice(-60);
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

  const stroke = positive ? '#047857' : '#b91c1c';
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
        viewBox={`0 0 ${width} ${height}`}
        style={{ width: '100%', height: 'auto', display: 'block', cursor: 'crosshair' }}
        onMouseMove={(e) => {
          const rect = e.currentTarget.getBoundingClientRect();
          const relX = ((e.clientX - rect.left) / rect.width) * width;
          const frac = (relX - padL) / plotW;
          const idx = Math.round(frac * (dates.length - 1));
          setHoverIdx(Math.min(dates.length - 1, Math.max(0, idx)));
        }}
        onMouseLeave={() => setHoverIdx(null)}
      >
        {/* Horizontal gridlines */}
        {yTicks.map((tv, i) => {
          const y = yAt(tv);
          return (
            <g key={i}>
              <line x1={padL} y1={y} x2={width - padR} y2={y} stroke="#e5e5ea" strokeDasharray="3 3" />
              <text x={padL - 8} y={y + 4} textAnchor="end" fontSize="10" fill="#86868b" fontFamily="-apple-system, BlinkMacSystemFont, 'SF Pro Text', 'Inter', sans-serif">
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
            stroke="#c7c7cc"
            strokeWidth="1"
          />
        )}

        {/* X-axis boundary labels */}
        <text x={padL} y={height - 6} fontSize="10" fill="#86868b" fontFamily="-apple-system, BlinkMacSystemFont, 'SF Pro Text', 'Inter', sans-serif">
          {dates[0]}
        </text>
        <text
          x={padL + plotW / 2}
          y={height - 6}
          textAnchor="middle"
          fontSize="10"
          fill="#86868b"
          fontFamily="-apple-system, BlinkMacSystemFont, 'SF Pro Text', 'Inter', sans-serif"
        >
          {dates[Math.floor(dates.length / 2)]}
        </text>
        <text x={width - padR} y={height - 6} textAnchor="end" fontSize="10" fill="#86868b" fontFamily="-apple-system, BlinkMacSystemFont, 'SF Pro Text', 'Inter', sans-serif">
          {dates[dates.length - 1]}
        </text>

        {/* Lines */}
        {rebased.map((r) => {
          const pts: string[] = [];
          r.values.forEach((v, idx) => {
            if (v !== null) {
              pts.push(`${xAt(idx).toFixed(1)},${yAt(v).toFixed(1)}`);
            }
          });
          if (pts.length < 2) return null;
          return (
            <polyline
              key={r.id}
              fill="none"
              stroke={r.color}
              strokeWidth={r.dashed ? '1.75' : '2.4'}
              strokeDasharray={r.dashed ? '5 4' : undefined}
              strokeLinejoin="round"
              strokeLinecap="round"
              points={pts.join(' ')}
            />
          );
        })}

        {/* Hover vertical crosshair */}
        {hoverIdx !== null && (
          <g>
            <line
              x1={xAt(activeIdx)}
              y1={padT}
              x2={xAt(activeIdx)}
              y2={padT + plotH}
              stroke="#1d1d1f"
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
                  stroke="#ffffff"
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
        {sectors.slice(0, 6).map((s) => (
          <span key={s.sector}>
            <span className="sector-dot" style={{ backgroundColor: sectorColor(s.sector) }} />
            <strong>{s.sector}</strong>{' '}
            <span className="mono">{s.weightPct.toFixed(1)}%</span>
            {s.stockCount !== undefined && (
              <span style={{ color: 'var(--text-muted)' }}> ({s.stockCount})</span>
            )}
          </span>
        ))}
      </div>
    </div>
  );
};
