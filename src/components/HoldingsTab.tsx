import { Fragment, useMemo, useState, type FC } from 'react';
import { ChevronDown, ChevronRight, Download, Search } from 'lucide-react';
import type { ConsensusHolding, ReturnMode, WeightMode } from '../types/data';
import {
  downloadCsv,
  formatPct,
  formatSignedPct,
  formatUsdCompact,
  returnClass,
} from '../utils/format';

interface HoldingsTabProps {
  topHoldings: Record<ReturnMode, ConsensusHolding[]>;
  mode: ReturnMode;
  onModeChange: (m: ReturnMode) => void;
  onSelectFund: (cik: string) => void;
}

type SortKey = 'aggregate' | 'holders' | 'totalValue' | 'ttm';

export const HoldingsTab: FC<HoldingsTabProps> = ({
  topHoldings,
  mode,
  onModeChange,
  onSelectFund,
}) => {
  const [weightMode, setWeightMode] = useState<WeightMode>('equal');
  const [sortBy, setSortBy] = useState<SortKey>('aggregate');
  const [search, setSearch] = useState('');
  const [expandedTicker, setExpandedTicker] = useState<string | null>(null);

  const list = useMemo(() => topHoldings[mode] ?? [], [topHoldings, mode]);

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    return [...list]
      .filter((item) => {
        if (!q) return true;
        return (
          item.ticker.toLowerCase().includes(q) ||
          item.name.toLowerCase().includes(q) ||
          item.sector.toLowerCase().includes(q) ||
          item.holders.some((h) => h.fundName.toLowerCase().includes(q))
        );
      })
      .sort((a, b) => {
        if (sortBy === 'holders') return b.holderCount - a.holderCount;
        if (sortBy === 'totalValue') return b.totalValueUsd - a.totalValueUsd;
        if (sortBy === 'ttm') return (b.stockTtmPct ?? -999) - (a.stockTtmPct ?? -999);
        const wa = weightMode === 'equal' ? a.equalAggregateWeightPct : a.aumAggregateWeightPct;
        const wb = weightMode === 'equal' ? b.equalAggregateWeightPct : b.aumAggregateWeightPct;
        return wb - wa;
      });
  }, [list, search, sortBy, weightMode]);

  const poolSize = list[0]?.totalRankedFundsPool ?? 0;
  const totalTop30Usd = useMemo(() => list.reduce((s, x) => s + x.totalValueUsd, 0), [list]);
  const totalAggWeight = useMemo(
    () =>
      list.reduce(
        (s, x) => s + (weightMode === 'equal' ? x.equalAggregateWeightPct : x.aumAggregateWeightPct),
        0
      ),
    [list, weightMode]
  );
  const topConsensusStock = useMemo(
    () => [...list].sort((a, b) => b.holderCount - a.holderCount)[0],
    [list]
  );

  const handleExport = () => {
    downloadCsv(
      `techwhale-top30-holdings-${mode}-${weightMode}.csv`,
      [
        'Rank',
        'Ticker',
        'Company',
        'Sector',
        'Holders',
        'Pool Size',
        'Equal-Weight Agg (%)',
        'AUM-Weight Agg (%)',
        'Avg Weight Among Holders (%)',
        'Combined Value (USD)',
        'TTM Return (%)',
        'Fund Summary',
      ],
      filtered.map((item, idx) => [
        idx + 1,
        item.ticker,
        item.name,
        item.sector,
        item.holderCount,
        item.totalRankedFundsPool,
        item.equalAggregateWeightPct,
        item.aumAggregateWeightPct,
        item.avgHolderWeightPct,
        item.totalValueUsd,
        item.stockTtmPct ?? '',
        item.holders.map((h) => `${h.fundName} (${h.weightPct.toFixed(1)}%)`).join('; '),
      ])
    );
  };

  return (
    <section aria-label="Top 30 Consensus Holdings">
      <div className="section-toolbar">
        <div className="section-title-group">
          <h2>Top 30 Consensus Holdings Across All Ranked Funds</h2>
          <p>
            Aggregated across all {poolSize} funds that place in the Top 10 of any window (YTD, TTM, 18M, 2Y, 3Y, 5Y). Expand any row for every holder&apos;s exact weight.
          </p>
        </div>

        <div className="controls-row">
          <div className="segmented-group" role="group" aria-label="Aggregate weighting mode">
            <button
              type="button"
              id="weight-mode-equal"
              className={`seg-btn ${weightMode === 'equal' ? 'active' : ''}`}
              onClick={() => setWeightMode('equal')}
              title="Every ranked fund has equal say regardless of AUM size"
            >
              Equal-Weight Avg %
            </button>
            <button
              type="button"
              id="weight-mode-aum"
              className={`seg-btn ${weightMode === 'aum' ? 'active' : ''}`}
              onClick={() => setWeightMode('aum')}
              title="Dollar-weighted share across combined AUM of ranked funds"
            >
              AUM-Weighted %
            </button>
          </div>

          <div className="segmented-group" role="group" aria-label="Ranking mode pool">
            <button
              type="button"
              className={`seg-btn ${mode === 'quarter_end' ? 'active' : ''}`}
              onClick={() => onModeChange('quarter_end')}
            >
              Quarter-End Pool
            </button>
            <button
              type="button"
              className={`seg-btn ${mode === 'filing_lag' ? 'active' : ''}`}
              onClick={() => onModeChange('filing_lag')}
            >
              45-Day Lag Pool
            </button>
          </div>
        </div>
      </div>

      {/* KPI Strip */}
      <div className="metrics-strip">
        <div className="metric-card">
          <div className="metric-label">Top 30 Aggregate Weight</div>
          <div className="metric-value">{formatPct(totalAggWeight, 1)}</div>
          <div className="metric-sub">
            {weightMode === 'equal' ? 'Mean allocation across ranked funds' : 'Share of combined ranked AUM'}
          </div>
        </div>
        <div className="metric-card">
          <div className="metric-label">Combined Capital Deployed</div>
          <div className="metric-value">{formatUsdCompact(totalTop30Usd)}</div>
          <div className="metric-sub">Across {poolSize} Top-10 ranked tech funds</div>
        </div>
        <div className="metric-card">
          <div className="metric-label">Highest Conviction Ownership</div>
          <div className="metric-value">
            {topConsensusStock ? topConsensusStock.ticker : '—'}
          </div>
          <div className="metric-sub">
            {topConsensusStock
              ? `Held by ${topConsensusStock.holderCount} of ${poolSize} ranked funds`
              : '—'}
          </div>
        </div>
        <div className="metric-card">
          <div className="metric-label">Sort View</div>
          <div style={{ marginTop: '0.35rem' }}>
            <div className="segmented-group">
              {(
                [
                  { id: 'aggregate', label: 'Weight' },
                  { id: 'holders', label: 'Holders' },
                  { id: 'ttm', label: 'TTM' },
                ] as const
              ).map((s) => (
                <button
                  key={s.id}
                  type="button"
                  className={`seg-btn ${sortBy === s.id ? 'active' : ''}`}
                  style={{ padding: '0.25rem 0.55rem', fontSize: '0.73rem' }}
                  onClick={() => setSortBy(s.id)}
                >
                  {s.label}
                </button>
              ))}
            </div>
          </div>
        </div>
      </div>

      {/* Top 30 Table */}
      <div className="panel">
        <div className="panel-header">
          <div>
            <h3 className="panel-title">Top 30 Consensus Stocks ({filtered.length})</h3>
            <p className="panel-subtitle">
              Click any fund chip to inspect that fund&apos;s complete 13F portfolio, or expand a stock row to view all holders.
            </p>
          </div>
          <div className="controls-row">
            <div className="search-input-wrap">
              <Search size={14} />
              <input
                type="text"
                className="search-input"
                placeholder="Filter ticker, sector, or fund..."
                value={search}
                onChange={(e) => setSearch(e.target.value)}
              />
            </div>
            <button type="button" className="action-btn" onClick={handleExport}>
              <Download size={14} /> Export CSV
            </button>
          </div>
        </div>

        <div className="table-scroll">
          <table className="data-table">
            <thead>
              <tr>
                <th></th>
                <th>Rank</th>
                <th>Stock</th>
                <th>Sector</th>
                <th className="num">
                  {weightMode === 'equal' ? 'Aggregate Wt (Eq)' : 'Aggregate Wt (AUM)'}
                </th>
                <th className="num">Holders</th>
                <th className="num">Total Held</th>
                <th className="num">Stock TTM</th>
                <th>Which Funds Hold This Stock &amp; Their Respective Weight</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((item, idx) => {
                const isOpen = expandedTicker === item.ticker;
                const primaryW =
                  weightMode === 'equal'
                    ? item.equalAggregateWeightPct
                    : item.aumAggregateWeightPct;
                const altW =
                  weightMode === 'equal'
                    ? item.aumAggregateWeightPct
                    : item.equalAggregateWeightPct;

                return (
                  <Fragment key={item.ticker}>
                    <tr>
                      <td style={{ width: '30px', paddingRight: 0 }}>
                        <button
                          type="button"
                          onClick={() =>
                            setExpandedTicker(isOpen ? null : item.ticker)
                          }
                          aria-label={`Toggle holders for ${item.ticker}`}
                          style={{
                            background: 'transparent',
                            border: 'none',
                            cursor: 'pointer',
                            color: 'var(--text-secondary)',
                            padding: '0.2rem',
                            display: 'inline-flex',
                          }}
                        >
                          {isOpen ? <ChevronDown size={16} /> : <ChevronRight size={16} />}
                        </button>
                      </td>
                      <td>
                        <span className={`rank-badge ${idx < 3 ? 'top3' : ''}`}>
                          #{idx + 1}
                        </span>
                      </td>
                      <td style={{ minWidth: '185px' }}>
                        <div style={{ display: 'flex', alignItems: 'center', gap: '0.45rem' }}>
                          <span className="ticker-badge">{item.ticker}</span>
                          <span style={{ fontWeight: 600 }}>{item.name}</span>
                        </div>
                        <div className="mono" style={{ fontSize: '0.71rem', color: 'var(--text-muted)', marginTop: '0.15rem' }}>
                          {item.priceUsd ? `$${item.priceUsd.toFixed(2)}` : ''} · Avg holder wt: {item.avgHolderWeightPct.toFixed(1)}%
                        </div>
                      </td>
                      <td>
                        <span className="chip">{item.sector}</span>
                      </td>
                      <td className="num">
                        <div className="weight-cell">
                          <span className="mono" style={{ fontWeight: 700, fontSize: '0.92rem' }}>
                            {formatPct(primaryW, 2)}
                          </span>
                          <div className="bar-track" style={{ width: '74px' }}>
                            <div
                              className="bar-fill"
                              style={{ width: `${Math.min(100, primaryW * 10)}%` }}
                            />
                          </div>
                          <span className="mono" style={{ fontSize: '0.68rem', color: 'var(--text-muted)' }}>
                            {weightMode === 'equal' ? 'AUM' : 'Eq'}: {altW.toFixed(2)}%
                          </span>
                        </div>
                      </td>
                      <td className="num mono">
                        <div>
                          <strong>{item.holderCount}</strong> / {item.totalRankedFundsPool}
                        </div>
                        <div style={{ fontSize: '0.7rem', color: 'var(--text-muted)' }}>
                          +{item.netBuyers} buy · -{item.netTrimmers} trim
                        </div>
                      </td>
                      <td className="num mono">{formatUsdCompact(item.totalValueUsd)}</td>
                      <td className={`num mono ${returnClass(item.stockTtmPct)}`}>
                        {formatSignedPct(item.stockTtmPct, 1)}
                      </td>
                      <td style={{ minWidth: '330px' }}>
                        <div style={{ fontSize: '0.77rem', color: 'var(--text-secondary)', marginBottom: '0.35rem' }}>
                          {item.summaryText}
                        </div>
                        <div style={{ display: 'flex', flexWrap: 'wrap', gap: '0.3rem' }}>
                          {item.holders.slice(0, 5).map((h) => (
                            <button
                              key={h.cik}
                              type="button"
                              className="chip clickable"
                              onClick={() => onSelectFund(h.cik)}
                              title={`Click to open ${h.fundName} (${formatUsdCompact(h.valueUsd)})`}
                            >
                              <span>{h.fundName}</span>
                              <strong className="mono">{h.weightPct.toFixed(1)}%</strong>
                            </button>
                          ))}
                          {item.holders.length > 5 && (
                            <button
                              type="button"
                              className="chip clickable"
                              onClick={() =>
                                setExpandedTicker(isOpen ? null : item.ticker)
                              }
                            >
                              +{item.holders.length - 5} more
                            </button>
                          )}
                        </div>
                      </td>
                    </tr>

                    {isOpen && (
                      <tr className="subrow">
                        <td colSpan={9}>
                          <div style={{ marginBottom: '0.55rem', fontSize: '0.8rem', fontWeight: 600, color: 'var(--text-primary)' }}>
                            All {item.holders.length} Ranked Funds Holding {item.ticker} ({item.name})
                          </div>
                          <div className="holder-grid">
                            {item.holders.map((h) => (
                              <div key={h.cik} className="holder-card">
                                <div>
                                  <button
                                    type="button"
                                    className="fund-link-btn"
                                    style={{ fontSize: '0.81rem' }}
                                    onClick={() => onSelectFund(h.cik)}
                                  >
                                    {h.fundName}
                                  </button>
                                  <div className="mono" style={{ fontSize: '0.7rem', color: 'var(--text-muted)', marginTop: '0.15rem' }}>
                                    {formatUsdCompact(h.valueUsd)} · Top 10 in: {h.top10Windows.join(', ') || 'Pool'}
                                  </div>
                                </div>
                                <div style={{ textAlign: 'right' }}>
                                  <div className="mono" style={{ fontWeight: 700, fontSize: '0.88rem' }}>
                                    {h.weightPct.toFixed(2)}%
                                  </div>
                                  <span
                                    className={`chip ${
                                      h.status === 'NEW' || h.status === 'ADDED'
                                        ? 'pos'
                                        : h.status === 'TRIMMED'
                                          ? 'neg'
                                          : ''
                                    }`}
                                    style={{ fontSize: '0.66rem', marginTop: '0.15rem' }}
                                  >
                                    {h.status}
                                    {h.qoqSharesChangePct !== null
                                      ? ` (${formatSignedPct(h.qoqSharesChangePct, 0)})`
                                      : ''}
                                  </span>
                                </div>
                              </div>
                            ))}
                          </div>
                        </td>
                      </tr>
                    )}
                  </Fragment>
                );
              })}
              {filtered.length === 0 && (
                <tr>
                  <td colSpan={9} style={{ textAlign: 'center', padding: '2rem', color: 'var(--text-muted)' }}>
                    No top-30 consensus holdings match &ldquo;{search}&rdquo;.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </section>
  );
};
