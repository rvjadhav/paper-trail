import { useMemo, useState, type FC } from 'react';
import { Download } from 'lucide-react';
import type {
  BenchmarkSeries,
  IdealPortfolioData,
  IdealPortfolioPosition,
  ReturnMode,
} from '../types/data';
import {
  downloadCsv,
  formatPct,
  formatSignedPct,
  formatUsdCompact,
  returnClass,
} from '../utils/format';
import { EquityCurveChart, SectorBreakdownBar } from './SvgCharts';

interface IdealPortfolioTabProps {
  idealPortfolio: IdealPortfolioData;
  benchmarks: Record<'QQQ' | 'SPY' | 'XLK', BenchmarkSeries>;
  mode: ReturnMode;
  onModeChange: (m: ReturnMode) => void;
  onSelectFund: (cik: string) => void;
}

function waterfillSlice(
  positions: IdealPortfolioPosition[],
  maxNames: number,
  capPct: number
): Array<IdealPortfolioPosition & { dynamicWeightPct: number }> {
  const sliced = positions.slice(0, maxNames);
  if (sliced.length === 0) return [];
  const cap = Math.max(capPct, 100 / sliced.length + 0.01);

  const scores = new Map<string, number>();
  for (const p of sliced) scores.set(p.ticker, Math.max(0.01, p.convictionScore));

  const fixed = new Map<string, number>();
  const rem = new Map(scores);
  let budget = 100.0;

  while (rem.size > 0) {
    const sumS = Array.from(rem.values()).reduce((a, b) => a + b, 0);
    if (sumS <= 0) break;
    const over: string[] = [];
    for (const [k, sc] of rem.entries()) {
      if ((sc / sumS) * budget > cap + 1e-6) over.push(k);
    }
    if (over.length === 0) {
      for (const [k, sc] of rem.entries()) {
        fixed.set(k, (sc / sumS) * budget);
      }
      break;
    }
    for (const k of over) {
      fixed.set(k, cap);
      budget -= cap;
      rem.delete(k);
    }
  }

  const out = sliced.map((p) => ({
    ...p,
    dynamicWeightPct: Math.round((fixed.get(p.ticker) ?? 0) * 100) / 100,
  }));
  // Ensure sum is exactly 100.00%
  const sum = out.reduce((a, b) => a + b.dynamicWeightPct, 0);
  if (out.length > 0) {
    out[0].dynamicWeightPct = Math.round((out[0].dynamicWeightPct + (100 - sum)) * 100) / 100;
  }
  return out;
}

export const IdealPortfolioTab: FC<IdealPortfolioTabProps> = ({
  idealPortfolio,
  benchmarks,
  mode,
  onModeChange,
  onSelectFund,
}) => {
  const totalAvail = idealPortfolio.positions.length;
  const [maxNames, setMaxNames] = useState<number>(Math.min(40, totalAvail));
  const [capPct, setCapPct] = useState<number>(8);
  const [capitalUsd, setCapitalUsd] = useState<number>(100000);

  const isDefault = maxNames === totalAvail && capPct === 8;

  const activePositions = useMemo(() => {
    if (isDefault) {
      return idealPortfolio.positions.map((p) => ({
        ...p,
        dynamicWeightPct: p.idealWeightPct,
      }));
    }
    return waterfillSlice(idealPortfolio.positions, maxNames, capPct);
  }, [idealPortfolio.positions, maxNames, capPct, isDefault]);

  const dynamicSectors = useMemo(() => {
    const map = new Map<string, { sector: string; weightPct: number; stockCount: number }>();
    for (const p of activePositions) {
      const cur = map.get(p.sector) ?? { sector: p.sector, weightPct: 0, stockCount: 0 };
      cur.weightPct += p.dynamicWeightPct;
      cur.stockCount += 1;
      map.set(p.sector, cur);
    }
    return Array.from(map.values()).sort((a, b) => b.weightPct - a.weightPct);
  }, [activePositions]);

  const top10Weight = useMemo(
    () => activePositions.slice(0, 10).reduce((s, p) => s + p.dynamicWeightPct, 0),
    [activePositions]
  );

  const effN = useMemo(() => {
    const hhi = activePositions.reduce((s, p) => s + (p.dynamicWeightPct / 100) ** 2, 0);
    return hhi > 0 ? 1 / hhi : activePositions.length;
  }, [activePositions]);

  const bt = idealPortfolio.backtest[mode];

  const handleExport = () => {
    downloadCsv(
      `paper-trail-ideal-portfolio-${activePositions.length}names.csv`,
      [
        'Rank',
        'Ticker',
        'Company',
        'Sector',
        'Ideal Weight (%)',
        'Allocation (USD)',
        'Conviction Score',
        'Ranked Holders',
        'Avg Fund Weight (%)',
        'Signal Tag',
        'Top Backers',
      ],
      activePositions.map((p, idx) => [
        idx + 1,
        p.ticker,
        p.name,
        p.sector,
        p.dynamicWeightPct,
        Math.round((capitalUsd * p.dynamicWeightPct) / 100),
        p.convictionScore,
        p.holderCount,
        p.avgFundWeightPct,
        p.rationaleTag,
        p.topBackers.map((b) => `${b.fundName} (${b.weightPct.toFixed(1)}%)`).join('; '),
      ])
    );
  };

  return (
    <section aria-label="Ideal Portfolio">
      <div className="section-toolbar">
        <div className="section-title-group">
          <h2>Ideal Paper Trail Portfolio ({activePositions.length} Names ≤ 50)</h2>
          <p>{idealPortfolio.methodologySummary}</p>
        </div>

        <div className="controls-row">
          <div className="segmented-group" role="group" aria-label="Max portfolio names">
            {Array.from(
              new Set(
                [25, 35, 40, 50]
                  .filter((n) => n <= Math.max(40, totalAvail))
                  .map((n) => Math.min(n, totalAvail))
              )
            ).map((eff) => (
              <button
                key={eff}
                type="button"
                className={`seg-btn ${maxNames === eff ? 'active' : ''}`}
                onClick={() => setMaxNames(eff)}
              >
                {eff} Names
              </button>
            ))}
          </div>

          <div className="segmented-group" role="group" aria-label="Single position cap">
            {[5, 8, 10].map((c) => (
              <button
                key={c}
                type="button"
                className={`seg-btn ${capPct === c ? 'active' : ''}`}
                onClick={() => setCapPct(c)}
              >
                {c}% Cap
              </button>
            ))}
          </div>
        </div>
      </div>

      {/* KPI Strip */}
      <div className="metrics-strip">
        <div className="metric-card">
          <div className="metric-label">Portfolio Breadth</div>
          <div className="metric-value">{activePositions.length} Stocks</div>
          <div className="metric-sub">
            Effective N: {effN.toFixed(1)} · Top 10: {top10Weight.toFixed(1)}%
          </div>
        </div>
        <div className="metric-card">
          <div className="metric-label">Backtest TTM ({mode === 'quarter_end' ? 'QE' : '45d Lag'})</div>
          <div className={`metric-value ${returnClass(bt.returns.TTM)}`}>
            {formatSignedPct(bt.returns.TTM)}
          </div>
          <div className="metric-sub">
            vs. QQQ {formatSignedPct(benchmarks.QQQ.returns.TTM)} · SPY {formatSignedPct(benchmarks.SPY.returns.TTM)}
          </div>
        </div>
        <div className="metric-card">
          <div className="metric-label">Backtest 3Y / CAGR</div>
          <div className={`metric-value ${returnClass(bt.returns['3Y'])}`}>
            {formatSignedPct(bt.returns['3Y'])}
          </div>
          <div className="metric-sub">
            CAGR: {formatSignedPct(bt.cagrPct)} · Sharpe: {bt.sharpeProxy ?? '—'} · Max DD: {formatSignedPct(bt.maxDrawdownPct)}
          </div>
        </div>
        <div className="metric-card">
          <div className="metric-label">Portfolio Capital Planner</div>
          <div style={{ marginTop: '0.3rem', display: 'flex', alignItems: 'center', gap: '0.4rem' }}>
            <span className="mono" style={{ fontWeight: 600 }}>$</span>
            <input
              type="number"
              min={1000}
              step={10000}
              value={capitalUsd}
              onChange={(e) => setCapitalUsd(Math.max(1000, Number(e.target.value) || 100000))}
              className="search-input mono"
              style={{ minWidth: '120px', padding: '0.3rem 0.55rem' }}
              aria-label="Portfolio capital amount in USD"
            />
          </div>
          <div className="metric-sub">Calculates exact dollar allocation</div>
        </div>
      </div>

      {/* Backtest Curve + Sector Exposure */}
      <div className="split-grid">
        <div className="panel" style={{ marginBottom: 0 }}>
          <div className="panel-header">
            <div>
              <h3 className="panel-title">Out-of-Sample Quarterly Rebalance Backtest</h3>
              <p className="panel-subtitle">
                Rolling historical simulation of the 40-stock 8%-capped strategy vs. QQQ &amp; SPY
              </p>
            </div>
            <div className="segmented-group">
              <button
                type="button"
                className={`seg-btn ${mode === 'quarter_end' ? 'active' : ''}`}
                onClick={() => onModeChange('quarter_end')}
              >
                Quarter-End
              </button>
              <button
                type="button"
                className={`seg-btn ${mode === 'filing_lag' ? 'active' : ''}`}
                onClick={() => onModeChange('filing_lag')}
              >
                45-Day Lag
              </button>
            </div>
          </div>
          <div style={{ padding: '1rem 1.2rem' }}>
            <EquityCurveChart
              defaultWindow="3Y"
              height={230}
              series={[
                {
                  id: 'ideal-qe',
                  label: 'Ideal Portfolio (QE)',
                  color: '#0071e3',
                  points: idealPortfolio.backtest.quarter_end.curve,
                },
                {
                  id: 'ideal-lag',
                  label: 'Ideal Portfolio (45d Lag)',
                  color: '#00a388',
                  dashed: true,
                  points: idealPortfolio.backtest.filing_lag.curve,
                },
                {
                  id: 'qqq',
                  label: 'QQQ',
                  color: '#5856d6',
                  dashed: true,
                  points: benchmarks.QQQ.curve,
                },
                {
                  id: 'spy',
                  label: 'SPY',
                  color: '#8e8e93',
                  dashed: true,
                  points: benchmarks.SPY.curve,
                },
              ]}
            />
          </div>
        </div>

        <div className="panel" style={{ marginBottom: 0 }}>
          <div className="panel-header">
            <div>
              <h3 className="panel-title">Sector Allocation &amp; Signal Mix</h3>
              <p className="panel-subtitle">Weights sum strictly to 100.00%</p>
            </div>
          </div>
          <div style={{ padding: '1rem 1.2rem' }}>
            <SectorBreakdownBar sectors={dynamicSectors} />
            <div style={{ marginTop: '1.15rem', borderTop: '1px solid var(--border-subtle)', paddingTop: '0.9rem' }}>
              <div style={{ fontSize: '0.78rem', fontWeight: 600, color: 'var(--text-primary)', marginBottom: '0.5rem' }}>
                Construction Rules
              </div>
              <ul style={{ margin: 0, paddingLeft: '1.1rem', fontSize: '0.8rem', color: 'var(--text-secondary)', lineHeight: 1.6 }}>
                <li><strong>Universe:</strong> Holdings from all {idealPortfolio.rankedFundsCount} ranked Top-100 tech funds.</li>
                <li><strong>Conviction Score:</strong> Weighted sum of manager multi-window rank × √(position weight).</li>
                <li><strong>Consensus Multiplier:</strong> Rewards stocks independently owned across ≥2 top funds.</li>
                <li><strong>Risk Cap:</strong> Single-stock ceiling of <strong>{capPct}%</strong> via water-filling normalization.</li>
              </ul>
            </div>
          </div>
        </div>
      </div>

      {/* Ideal Portfolio Holdings Table */}
      <div className="panel">
        <div className="panel-header">
          <div>
            <h3 className="panel-title">Ideal Portfolio Allocation ({activePositions.length} Positions · 100.00%)</h3>
            <p className="panel-subtitle">Click any backing fund to inspect their full 13F portfolio</p>
          </div>
          <button type="button" className="action-btn" onClick={handleExport}>
            <Download size={14} /> Export Ideal Portfolio CSV
          </button>
        </div>

        <div className="table-scroll">
          <table className="data-table">
            <thead>
              <tr>
                <th>#</th>
                <th>Stock</th>
                <th>Sector</th>
                <th className="num">Ideal Weight</th>
                <th className="num">Target ($)</th>
                <th className="num">Conviction</th>
                <th>Signal Driver</th>
                <th className="num">Holders</th>
                <th className="num">TTM</th>
                <th>Top Backing Funds</th>
              </tr>
            </thead>
            <tbody>
              {activePositions.map((p, idx) => {
                const targetUsd = (capitalUsd * p.dynamicWeightPct) / 100;
                const estShares = p.priceUsd && p.priceUsd > 0 ? Math.floor(targetUsd / p.priceUsd) : null;
                return (
                  <tr key={p.ticker}>
                    <td>
                      <span className={`rank-badge ${idx < 3 ? 'top3' : ''}`}>#{idx + 1}</span>
                    </td>
                    <td style={{ minWidth: '185px' }}>
                      <div style={{ display: 'flex', alignItems: 'center', gap: '0.45rem' }}>
                        <span className="ticker-badge">{p.ticker}</span>
                        <span style={{ fontWeight: 600 }}>{p.name}</span>
                      </div>
                      <div className="mono" style={{ fontSize: '0.7rem', color: 'var(--text-muted)', marginTop: '0.12rem' }}>
                        {p.priceUsd ? `$${p.priceUsd.toFixed(2)}` : '—'} · Avg fund wt {p.avgFundWeightPct.toFixed(1)}%
                      </div>
                    </td>
                    <td>
                      <span className="chip">{p.sector}</span>
                    </td>
                    <td className="num">
                      <div className="weight-cell">
                        <span className="mono" style={{ fontWeight: 700, color: 'var(--accent-primary)', fontSize: '0.92rem' }}>
                          {formatPct(p.dynamicWeightPct, 2)}
                        </span>
                        <div className="bar-track" style={{ width: '72px' }}>
                          <div className="bar-fill pos" style={{ width: `${Math.min(100, (p.dynamicWeightPct / capPct) * 100)}%` }} />
                        </div>
                      </div>
                    </td>
                    <td className="num mono">
                      <div style={{ fontWeight: 600 }}>{formatUsdCompact(targetUsd)}</div>
                      {estShares !== null && (
                        <div style={{ fontSize: '0.69rem', color: 'var(--text-muted)' }}>
                          ~{estShares.toLocaleString()} sh
                        </div>
                      )}
                    </td>
                    <td className="num mono">
                      <strong>{p.convictionScore.toFixed(0)}</strong>
                      <span style={{ color: 'var(--text-muted)' }}>/100</span>
                    </td>
                    <td>
                      <span
                        className={`chip ${
                          p.rationaleTag === 'Top-Decile Alpha'
                            ? 'pos'
                            : p.rationaleTag === 'Active Accumulation'
                              ? 'tech'
                              : ''
                        }`}
                      >
                        {p.rationaleTag}
                      </span>
                    </td>
                    <td className="num mono">
                      <strong>{p.holderCount}</strong> funds
                    </td>
                    <td className={`num mono ${returnClass(p.stockTtmPct)}`}>
                      {formatSignedPct(p.stockTtmPct, 1)}
                    </td>
                    <td style={{ minWidth: '260px' }}>
                      <div style={{ display: 'flex', flexWrap: 'wrap', gap: '0.28rem' }}>
                        {p.topBackers.slice(0, 3).map((b) => (
                          <button
                            key={b.cik}
                            type="button"
                            className="chip clickable"
                            onClick={() => onSelectFund(b.cik)}
                          >
                            <span>{b.fundName}</span>
                            <strong className="mono">{b.weightPct.toFixed(1)}%</strong>
                          </button>
                        ))}
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>
    </section>
  );
};
