import { Fragment, useMemo, useState, type FC } from 'react';
import { ChevronDown, ChevronRight, Download, Search, Sparkles, TrendingDown, Zap } from 'lucide-react';
import type {
  ConsensusHolding,
  EntryZoneStatus,
  MarketCapTier,
  ReturnMode,
  SignalScreenerData,
  SignalScreenerEntry,
  StockIndexEntry,
  WeightMode,
} from '../types/data';
import {
  downloadCsv,
  entryZoneChipClass,
  formatPct,
  formatSignedPct,
  formatSignedUsd,
  formatUsdCompact,
  returnClass,
  tierChipClass,
} from '../utils/format';

interface HoldingsTabProps {
  topHoldings: Record<ReturnMode, ConsensusHolding[]>;
  signalScreener: SignalScreenerData;
  stocksIndex: Record<string, StockIndexEntry>;
  mode: ReturnMode;
  onModeChange: (m: ReturnMode) => void;
  onSelectFund: (cik: string) => void;
  onSelectStock: (ticker: string) => void;
}

type ScreenerSubTab = 'CONSENSUS' | 'CLUSTER' | 'HEAVY_ADDS' | 'EXITS' | 'CONTRARIAN';
type CapFilter = 'ALL' | MarketCapTier;
type EntryFilter = 'ALL' | 'VALUE' | 'EXTENDED' | 'CHASING';
type ConsensusSortKey = 'aggregate' | 'holders' | 'totalValue' | 'ttm';
type ScreenerSortKey = 'default' | 'netFlow' | 'holders' | 'ttm' | 'revYoY';

function compareNullableNumbers(a: number | null | undefined, b: number | null | undefined, asc: boolean): number {
  const an = a == null || !Number.isFinite(a) ? null : a;
  const bn = b == null || !Number.isFinite(b) ? null : b;
  if (an === null && bn === null) return 0;
  if (an === null) return 1;
  if (bn === null) return -1;
  return asc ? an - bn : bn - an;
}

function matchesEntryFilter(
  zone: EntryZoneStatus | null | undefined,
  filter: EntryFilter
): boolean {
  if (filter === 'ALL') return true;
  if (!zone) return false;
  if (filter === 'VALUE') return zone === 'Discount to Q-Avg' || zone === 'Near Q-Avg';
  if (filter === 'EXTENDED') return zone === 'Extended';
  return zone === 'Chasing (+30%+)';
}

function matchesCapFilter(
  capTier: MarketCapTier | null | undefined,
  filter: CapFilter
): boolean {
  if (filter === 'ALL') return true;
  return capTier === filter;
}

export const HoldingsTab: FC<HoldingsTabProps> = ({
  topHoldings,
  signalScreener,
  stocksIndex,
  mode,
  onModeChange,
  onSelectFund,
  onSelectStock,
}) => {
  const [subTab, setSubTab] = useState<ScreenerSubTab>('CONSENSUS');
  const [weightMode, setWeightMode] = useState<WeightMode>('equal');
  const [sortBy, setSortBy] = useState<ConsensusSortKey>('aggregate');
  const [sortAsc, setSortAsc] = useState(false);
  const [screenerSortBy, setScreenerSortBy] = useState<ScreenerSortKey>('default');
  const [screenerSortAsc, setScreenerSortAsc] = useState(false);
  const [capFilter, setCapFilter] = useState<CapFilter>('ALL');
  const [entryFilter, setEntryFilter] = useState<EntryFilter>('ALL');
  const [search, setSearch] = useState('');
  const [expandedTicker, setExpandedTicker] = useState<string | null>(null);

  const consensusList = useMemo(() => topHoldings[mode] ?? [], [topHoldings, mode]);

  const filteredConsensus = useMemo(() => {
    const q = search.trim().toLowerCase();
    return [...consensusList]
      .filter((item) => {
        if (!matchesCapFilter(item.marketCapTier, capFilter)) return false;
        if (!matchesEntryFilter(item.entryZoneStatus, entryFilter)) return false;
        if (!q) return true;
        return (
          item.ticker.toLowerCase().includes(q) ||
          item.name.toLowerCase().includes(q) ||
          item.sector.toLowerCase().includes(q) ||
          item.subSector.toLowerCase().includes(q) ||
          item.holders.some((h) => h.fundName.toLowerCase().includes(q))
        );
      })
      .sort((a, b) => {
        if (sortBy === 'holders') return compareNullableNumbers(a.holderCount, b.holderCount, sortAsc);
        if (sortBy === 'totalValue') return compareNullableNumbers(a.totalValueUsd, b.totalValueUsd, sortAsc);
        if (sortBy === 'ttm') return compareNullableNumbers(a.stockTtmPct, b.stockTtmPct, sortAsc);
        const wa = weightMode === 'equal' ? a.equalAggregateWeightPct : a.aumAggregateWeightPct;
        const wb = weightMode === 'equal' ? b.equalAggregateWeightPct : b.aumAggregateWeightPct;
        return compareNullableNumbers(wa, wb, sortAsc);
      });
  }, [consensusList, capFilter, entryFilter, search, sortBy, sortAsc, weightMode]);

  const activeScreenerList = useMemo<SignalScreenerEntry[]>(() => {
    const raw =
      subTab === 'CLUSTER'
        ? signalScreener.clusterInitiations
        : subTab === 'HEAVY_ADDS'
          ? signalScreener.heavyAdds
          : subTab === 'EXITS'
            ? signalScreener.smartMoneyExits
            : subTab === 'CONTRARIAN'
              ? signalScreener.contrarianBets
              : [];
    const q = search.trim().toLowerCase();
    const filtered = raw.filter((item) => {
      if (!matchesCapFilter(item.marketCapTier, capFilter)) return false;
      if (!matchesEntryFilter(item.entryZoneStatus, entryFilter)) return false;
      if (!q) return true;
      return (
        item.ticker.toLowerCase().includes(q) ||
        item.name.toLowerCase().includes(q) ||
        item.subSector.toLowerCase().includes(q) ||
        item.sector.toLowerCase().includes(q) ||
        item.movers.some((m) => m.fundName.toLowerCase().includes(q)) ||
        item.topBackers.some((b) => b.fundName.toLowerCase().includes(q))
      );
    });
    if (screenerSortBy === 'default') return filtered;
    return [...filtered].sort((a, b) => {
      if (screenerSortBy === 'netFlow') {
        const fa = a.netEstFlowUsd ?? a.netDeltaUsd;
        const fb = b.netEstFlowUsd ?? b.netDeltaUsd;
        return compareNullableNumbers(fa, fb, screenerSortAsc);
      }
      if (screenerSortBy === 'holders') {
        return compareNullableNumbers(a.holderCount, b.holderCount, screenerSortAsc);
      }
      if (screenerSortBy === 'ttm') {
        return compareNullableNumbers(a.stockTtmPct, b.stockTtmPct, screenerSortAsc);
      }
      return compareNullableNumbers(
        a.fundamentals?.revenueGrowthYoYPct,
        b.fundamentals?.revenueGrowthYoYPct,
        screenerSortAsc
      );
    });
  }, [subTab, signalScreener, capFilter, entryFilter, search, screenerSortBy, screenerSortAsc]);

  const poolSize = consensusList[0]?.totalRankedFundsPool ?? 0;
  const totalTop30Usd = useMemo(
    () => consensusList.reduce((s, x) => s + x.totalValueUsd, 0),
    [consensusList]
  );
  const totalAggWeight = useMemo(
    () =>
      consensusList.reduce(
        (s, x) => s + (weightMode === 'equal' ? x.equalAggregateWeightPct : x.aumAggregateWeightPct),
        0
      ),
    [consensusList, weightMode]
  );
  const topClusterStock = signalScreener.clusterInitiations[0] ?? null;

  const renderTickerButton = (ticker: string) => {
    if (stocksIndex[ticker]) {
      return (
        <button
          type="button"
          className="ticker-badge ticker-link-btn"
          onClick={() => onSelectStock(ticker)}
          title={`Open ${ticker} Stock Dossier`}
        >
          {ticker}
        </button>
      );
    }
    return <span className="ticker-badge">{ticker}</span>;
  };

  const handleExport = () => {
    if (subTab === 'CONSENSUS') {
      downloadCsv(
        `paper-trail-top30-consensus-${mode}-${weightMode}.csv`,
        [
          'Rank',
          'Ticker',
          'Company',
          'Sub-Sector',
          'Market Cap Tier',
          'Price (USD)',
          'Est Q-VWAP (USD)',
          'vs Q-VWAP (%)',
          'Entry Zone',
          'XBRL Rev YoY (%)',
          'XBRL Op Margin (%)',
          'Holders',
          'Pool Size',
          'Equal-Weight Agg (%)',
          'AUM-Weight Agg (%)',
          'Combined Value (USD)',
          'TTM Return (%)',
          'Top Holders',
        ],
        filteredConsensus.map((item, idx) => [
          idx + 1,
          item.ticker,
          item.name,
          item.subSector || item.sector,
          item.marketCapTier ?? '',
          item.priceUsd ?? '',
          item.estQuarterVwap ?? '',
          item.pctVsQuarterVwap ?? '',
          item.entryZoneStatus ?? '',
          item.fundamentals?.revenueGrowthYoYPct ?? '',
          item.fundamentals?.operatingMarginPct ?? '',
          item.holderCount,
          item.totalRankedFundsPool,
          item.equalAggregateWeightPct,
          item.aumAggregateWeightPct,
          item.totalValueUsd,
          item.stockTtmPct ?? '',
          item.holders.map((h) => `${h.fundName} (${h.weightPct.toFixed(1)}%)`).join('; '),
        ])
      );
      return;
    }

    downloadCsv(
      `paper-trail-screener-${subTab.toLowerCase()}.csv`,
      [
        'Rank',
        'Ticker',
        'Company',
        'Sub-Sector',
        'Market Cap Tier',
        'Price (USD)',
        'Est Q-VWAP (USD)',
        'vs Q-VWAP (%)',
        'Entry Zone',
        'XBRL Rev YoY (%)',
        'XBRL Op Margin (%)',
        'Holders',
        'Elite 50 Holders',
        'Max Fund Weight (%)',
        'New Buys',
        'Elite New Buys',
        'Added',
        'Trimmed',
        'Exited',
        'Net Flow (USD)',
        'TTM Return (%)',
      ],
      activeScreenerList.map((item, idx) => [
        idx + 1,
        item.ticker,
        item.name,
        item.subSector || item.sector,
        item.marketCapTier ?? '',
        item.priceUsd ?? '',
        item.estQuarterVwap ?? '',
        item.pctVsQuarterVwap ?? '',
        item.entryZoneStatus ?? '',
        item.fundamentals?.revenueGrowthYoYPct ?? '',
        item.fundamentals?.operatingMarginPct ?? '',
        item.holderCount,
        item.eliteHoldersCount,
        item.maxWeightPct,
        item.newCount,
        item.eliteNewCount,
        item.addedCount,
        item.trimmedCount,
        item.exitedCount,
        item.netEstFlowUsd ?? item.netDeltaUsd,
        item.stockTtmPct ?? '',
      ])
    );
  };

  const screenerDescription =
    subTab === 'CONSENSUS'
      ? `Aggregated across all ${poolSize} ranked funds placing in Top 100 of any window. Click any ticker for Stock Dossier.`
      : subTab === 'CLUSTER'
        ? `Stocks with ≥2 fresh NEW initiations during ${signalScreener.quarterLabel}, prioritized by Elite-50 buying and dollar deployment.`
        : subTab === 'HEAVY_ADDS'
          ? `Stocks with net accumulation impulse across ≥2 buyers where total dollar inflows exceed trims/exits in ${signalScreener.quarterLabel}.`
          : subTab === 'EXITS'
            ? `Distribution warning: stocks where ≥2 tracked funds exited or trimmed ≥5% with net dollar outflows in ${signalScreener.quarterLabel}.`
            : `Under-owned specialist convictions: 2–6 total holders with at least one Elite-50 manager sizing the stock ≥3.0% of their book.`;

  return (
    <section aria-label="13F Signal Screener and Top Holdings">
      <div className="section-toolbar">
        <div className="section-title-group">
          <h2>13F Signal Screener &amp; Consensus Holdings</h2>
          <p>{screenerDescription}</p>
        </div>

        <div className="controls-row">
          <div className="segmented-group" role="group" aria-label="Screener views">
            {(
              [
                { id: 'CONSENSUS', label: `Top 30 Consensus` },
                { id: 'CLUSTER', label: `Cluster Buys (${signalScreener.clusterInitiations.length})` },
                { id: 'HEAVY_ADDS', label: `Heavy Adds (${signalScreener.heavyAdds.length})` },
                { id: 'EXITS', label: `Smart Exits (${signalScreener.smartMoneyExits.length})` },
                { id: 'CONTRARIAN', label: `Contrarian (${signalScreener.contrarianBets.length})` },
              ] as const
            ).map((tab) => (
              <button
                key={tab.id}
                type="button"
                id={`screener-subtab-${tab.id.toLowerCase()}`}
                className={`seg-btn ${subTab === tab.id ? 'active' : ''}`}
                onClick={() => {
                  setSubTab(tab.id);
                  setExpandedTicker(null);
                }}
              >
                {tab.label}
              </button>
            ))}
          </div>
        </div>
      </div>

      {/* KPI Strip */}
      <div className="metrics-strip">
        <div className="metric-card">
          <div className="metric-label">Top 30 Aggregate Weight</div>
          <div className="metric-value">{formatPct(totalAggWeight, 1)}</div>
          <div className="metric-sub">
            {weightMode === 'equal' ? 'Mean allocation across ranked funds' : 'Share of combined ranked AUM'} · {formatUsdCompact(totalTop30Usd)}
          </div>
        </div>
        <div className="metric-card">
          <div className="metric-label">Cluster Initiations ({signalScreener.quarterLabel})</div>
          <div className="metric-value">
            <span>{signalScreener.clusterInitiations.length}</span>
            {topClusterStock && (
              <button
                type="button"
                className="ticker-badge ticker-link-btn"
                onClick={() => onSelectStock(topClusterStock.ticker)}
              >
                {topClusterStock.ticker}
              </button>
            )}
          </div>
          <div className="metric-sub">
            {topClusterStock
              ? `Led by ${topClusterStock.ticker} (${topClusterStock.newCount} new · ${topClusterStock.eliteNewCount} Elite 50)`
              : 'No multi-fund clusters in season'}
          </div>
        </div>
        <div className="metric-card">
          <div className="metric-label">Heavy Adds / Smart Exits</div>
          <div className="metric-value mono">
            <span className="ret-pos">+{signalScreener.heavyAdds.length}</span>
            <span style={{ color: 'var(--text-muted)', fontSize: '1rem' }}>/</span>
            <span className="ret-neg">-{signalScreener.smartMoneyExits.length}</span>
          </div>
          <div className="metric-sub">
            Contrarian specialist ideas: <strong>{signalScreener.contrarianBets.length}</strong>
          </div>
        </div>
        <div className="metric-card">
          <div className="metric-label">Timing Pool Mode</div>
          <div style={{ marginTop: '0.35rem' }}>
            <div className="segmented-group" role="group" aria-label="Ranking mode pool">
              <button
                type="button"
                className={`seg-btn ${mode === 'filing_lag' ? 'active' : ''}`}
                style={{ padding: '0.26rem 0.6rem', fontSize: '0.74rem' }}
                onClick={() => onModeChange('filing_lag')}
              >
                45-Day Lag Pool
              </button>
              <button
                type="button"
                className={`seg-btn ${mode === 'quarter_end' ? 'active' : ''}`}
                style={{ padding: '0.26rem 0.6rem', fontSize: '0.74rem' }}
                onClick={() => onModeChange('quarter_end')}
              >
                Quarter-End Pool
              </button>
            </div>
          </div>
          <div className="metric-sub">Controls Consensus pool ({poolSize} funds)</div>
        </div>
      </div>

      {/* Main Panel */}
      <div className="panel">
        <div className="panel-header">
          <div className="controls-row" style={{ flexWrap: 'wrap' }}>
            <div className="segmented-group" role="group" aria-label="Filter by market cap tier">
              {(
                [
                  { id: 'ALL', label: 'All Caps' },
                  { id: 'Mega >$200B', label: 'Mega >$200B' },
                  { id: 'Large $30B-$200B', label: 'Large $30B–$200B' },
                  { id: 'Emerging <$30B', label: 'Emerging <$30B' },
                ] as const
              ).map((c) => (
                <button
                  key={c.id}
                  type="button"
                  className={`seg-btn ${capFilter === c.id ? 'active' : ''}`}
                  onClick={() => setCapFilter(c.id)}
                >
                  {c.label}
                </button>
              ))}
            </div>

            <div className="segmented-group" role="group" aria-label="Filter by Quarter VWAP entry zone">
              {(
                [
                  { id: 'ALL', label: 'All Entry Zones' },
                  { id: 'VALUE', label: 'Discount / Near Q-Avg' },
                  { id: 'EXTENDED', label: 'Extended' },
                  { id: 'CHASING', label: 'Chasing +30%+' },
                ] as const
              ).map((ez) => (
                <button
                  key={ez.id}
                  type="button"
                  className={`seg-btn ${entryFilter === ez.id ? 'active' : ''}`}
                  onClick={() => setEntryFilter(ez.id)}
                >
                  {ez.label}
                </button>
              ))}
            </div>
          </div>

          <div className="controls-row">
            {subTab === 'CONSENSUS' ? (
              <>
                <div className="segmented-group" role="group" aria-label="Aggregate weighting mode">
                  <button
                    type="button"
                    id="weight-mode-equal"
                    className={`seg-btn ${weightMode === 'equal' ? 'active' : ''}`}
                    onClick={() => setWeightMode('equal')}
                  >
                    Equal-Wt
                  </button>
                  <button
                    type="button"
                    id="weight-mode-aum"
                    className={`seg-btn ${weightMode === 'aum' ? 'active' : ''}`}
                    onClick={() => setWeightMode('aum')}
                  >
                    AUM-Wt
                  </button>
                </div>

                <div className="segmented-group" role="group" aria-label="Sort consensus table">
                  {(
                    [
                      { id: 'aggregate', label: 'Weight' },
                      { id: 'holders', label: 'Holders' },
                      { id: 'totalValue', label: 'Total Held' },
                      { id: 'ttm', label: 'TTM' },
                    ] as const
                  ).map((s) => (
                    <button
                      key={s.id}
                      type="button"
                      className={`seg-btn ${sortBy === s.id ? 'active' : ''}`}
                      onClick={() => {
                        if (sortBy === s.id) setSortAsc((a) => !a);
                        else {
                          setSortBy(s.id);
                          setSortAsc(false);
                        }
                      }}
                    >
                      {s.label}
                      {sortBy === s.id ? (sortAsc ? ' ▲' : ' ▼') : ''}
                    </button>
                  ))}
                </div>
              </>
            ) : (
              <div className="segmented-group" role="group" aria-label="Sort signal screener">
                {(
                  [
                    { id: 'default', label: 'Signal Rank' },
                    { id: 'netFlow', label: 'Net Flow' },
                    { id: 'revYoY', label: 'Rev YoY' },
                    { id: 'holders', label: 'Holders' },
                    { id: 'ttm', label: 'TTM' },
                  ] as const
                ).map((s) => (
                  <button
                    key={s.id}
                    type="button"
                    className={`seg-btn ${screenerSortBy === s.id ? 'active' : ''}`}
                    onClick={() => {
                      if (s.id === 'default') {
                        setScreenerSortBy('default');
                        setScreenerSortAsc(false);
                      } else if (screenerSortBy === s.id) {
                        setScreenerSortAsc((a) => !a);
                      } else {
                        setScreenerSortBy(s.id);
                        setScreenerSortAsc(false);
                      }
                    }}
                  >
                    {s.label}
                    {screenerSortBy === s.id && s.id !== 'default'
                      ? screenerSortAsc
                        ? ' ▲'
                        : ' ▼'
                      : ''}
                  </button>
                ))}
              </div>
            )}

            <div className="search-input-wrap">
              <Search size={14} />
              <input
                type="text"
                className="search-input"
                placeholder="Filter ticker, sub-sector, fund..."
                value={search}
                onChange={(e) => setSearch(e.target.value)}
              />
            </div>

            <button type="button" className="action-btn" onClick={handleExport}>
              <Download size={14} /> Export CSV
            </button>
          </div>
        </div>

        {/* CONSENSUS TABLE */}
        {subTab === 'CONSENSUS' ? (
          <div className="table-scroll">
            <table className="data-table">
              <thead>
                <tr>
                  <th></th>
                  <th>Rank</th>
                  <th>Stock</th>
                  <th>Sub-Sector</th>
                  <th>Entry Zone (vs Q-VWAP)</th>
                  <th className="num">XBRL Rev / OpM</th>
                  <th
                    className={`num sortable ${sortBy === 'aggregate' ? 'active-sort' : ''}`}
                    onClick={() => {
                      if (sortBy === 'aggregate') setSortAsc((a) => !a);
                      else {
                        setSortBy('aggregate');
                        setSortAsc(false);
                      }
                    }}
                  >
                    {weightMode === 'equal' ? 'Agg Wt (Eq)' : 'Agg Wt (AUM)'}{' '}
                    {sortBy === 'aggregate' ? (sortAsc ? '▲' : '▼') : ''}
                  </th>
                  <th
                    className={`num sortable ${sortBy === 'holders' ? 'active-sort' : ''}`}
                    onClick={() => {
                      if (sortBy === 'holders') setSortAsc((a) => !a);
                      else {
                        setSortBy('holders');
                        setSortAsc(false);
                      }
                    }}
                  >
                    Holders {sortBy === 'holders' ? (sortAsc ? '▲' : '▼') : ''}
                  </th>
                  <th
                    className={`num sortable ${sortBy === 'totalValue' ? 'active-sort' : ''}`}
                    onClick={() => {
                      if (sortBy === 'totalValue') setSortAsc((a) => !a);
                      else {
                        setSortBy('totalValue');
                        setSortAsc(false);
                      }
                    }}
                  >
                    Total Held {sortBy === 'totalValue' ? (sortAsc ? '▲' : '▼') : ''}
                  </th>
                  <th
                    className={`num sortable ${sortBy === 'ttm' ? 'active-sort' : ''}`}
                    onClick={() => {
                      if (sortBy === 'ttm') setSortAsc((a) => !a);
                      else {
                        setSortBy('ttm');
                        setSortAsc(false);
                      }
                    }}
                  >
                    Stock TTM {sortBy === 'ttm' ? (sortAsc ? '▲' : '▼') : ''}
                  </th>
                  <th>Backing Funds &amp; Weights</th>
                </tr>
              </thead>
              <tbody>
                {filteredConsensus.map((item, idx) => {
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
                            {renderTickerButton(item.ticker)}
                            <span style={{ fontWeight: 600 }}>{item.name}</span>
                          </div>
                          <div className="mono" style={{ fontSize: '0.71rem', color: 'var(--text-muted)', marginTop: '0.15rem', display: 'flex', gap: '0.35rem', flexWrap: 'wrap' }}>
                            <span>{item.priceUsd ? `$${item.priceUsd.toFixed(2)}` : '—'}</span>
                            <span>·</span>
                            <span>Avg wt {item.avgHolderWeightPct.toFixed(1)}%</span>
                            {item.marketCapTier && <span>· {item.marketCapTier}</span>}
                          </div>
                        </td>
                        <td>
                          <span className="chip">{item.subSector || item.sector}</span>
                        </td>
                        <td style={{ minWidth: '155px' }}>
                          {item.entryZoneStatus ? (
                            <div>
                              <span className={`chip ${entryZoneChipClass(item.entryZoneStatus)}`}>
                                {item.entryZoneStatus}
                              </span>
                              <div className="mono" style={{ fontSize: '0.69rem', color: 'var(--text-muted)', marginTop: '0.15rem' }}>
                                {item.pctVsQuarterVwap != null ? `${formatSignedPct(item.pctVsQuarterVwap)} vs ` : ''}
                                {item.estQuarterVwap != null ? `$${item.estQuarterVwap.toFixed(2)}` : ''}
                              </div>
                            </div>
                          ) : (
                            <span className="ret-muted">—</span>
                          )}
                        </td>
                        <td className="num mono" style={{ fontSize: '0.78rem' }}>
                          {item.fundamentals &&
                          (item.fundamentals.revenueGrowthYoYPct != null ||
                            item.fundamentals.operatingMarginPct != null) ? (
                            <div>
                              <div className={returnClass(item.fundamentals.revenueGrowthYoYPct)}>
                                {item.fundamentals.revenueGrowthYoYPct != null
                                  ? `${formatSignedPct(item.fundamentals.revenueGrowthYoYPct)} rev`
                                  : '—'}
                              </div>
                              <div style={{ fontSize: '0.69rem', color: 'var(--text-muted)' }}>
                                {item.fundamentals.operatingMarginPct != null
                                  ? `${item.fundamentals.operatingMarginPct.toFixed(1)}% OpM`
                                  : ''}
                              </div>
                            </div>
                          ) : (
                            <span className="ret-muted">—</span>
                          )}
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
                        <td style={{ minWidth: '300px' }}>
                          <div style={{ display: 'flex', flexWrap: 'wrap', gap: '0.3rem' }}>
                            {item.holders.slice(0, 4).map((h) => (
                              <button
                                key={h.cik}
                                type="button"
                                className="chip clickable"
                                onClick={() => onSelectFund(h.cik)}
                                title={`Click to open ${h.fundName} (${h.fundTier} · ${formatUsdCompact(h.valueUsd)})`}
                              >
                                <span>{h.fundName}</span>
                                <strong className="mono">{h.weightPct.toFixed(1)}%</strong>
                              </button>
                            ))}
                            {item.holders.length > 4 && (
                              <button
                                type="button"
                                className="chip clickable"
                                onClick={() =>
                                  setExpandedTicker(isOpen ? null : item.ticker)
                                }
                              >
                                +{item.holders.length - 4} more
                              </button>
                            )}
                          </div>
                        </td>
                      </tr>

                      {isOpen && (
                        <tr className="subrow">
                          <td colSpan={11}>
                            <div style={{ marginBottom: '0.55rem', display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: '0.5rem' }}>
                              <span style={{ fontSize: '0.8rem', fontWeight: 600, color: 'var(--text-primary)' }}>
                                All {item.holders.length} Ranked Funds Holding {item.ticker} ({item.name})
                              </span>
                              <button
                                type="button"
                                className="action-btn"
                                style={{ padding: '0.28rem 0.7rem', fontSize: '0.74rem' }}
                                onClick={() => onSelectStock(item.ticker)}
                              >
                                Open Complete {item.ticker} Stock Dossier
                              </button>
                            </div>
                            <div className="holder-grid">
                              {item.holders.map((h) => (
                                <div key={h.cik} className="holder-card">
                                  <div>
                                    <div style={{ display: 'flex', alignItems: 'center', gap: '0.35rem', flexWrap: 'wrap' }}>
                                      <button
                                        type="button"
                                        className="fund-link-btn"
                                        style={{ fontSize: '0.81rem' }}
                                        onClick={() => onSelectFund(h.cik)}
                                      >
                                        {h.fundName}
                                      </button>
                                      <span className={`chip ${tierChipClass(h.fundTier)}`} style={{ fontSize: '0.64rem' }}>
                                        {h.fundTier}
                                      </span>
                                    </div>
                                    <div className="mono" style={{ fontSize: '0.7rem', color: 'var(--text-muted)', marginTop: '0.15rem' }}>
                                      {formatUsdCompact(h.valueUsd)} · Top 100: {h.rankedWindows.join(', ') || 'Pool'}
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
                {filteredConsensus.length === 0 && (
                  <tr>
                    <td colSpan={11} style={{ textAlign: 'center', padding: '2rem', color: 'var(--text-muted)' }}>
                      No top-30 consensus holdings match your active filters.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        ) : (
          /* SIGNAL SCREENER TABLE (CLUSTER / HEAVY_ADDS / EXITS / CONTRARIAN) */
          <div className="table-scroll">
            <table className="data-table">
              <thead>
                <tr>
                  <th></th>
                  <th>#</th>
                  <th>Stock</th>
                  <th>Sub-Sector</th>
                  <th>Entry Zone (Q-VWAP)</th>
                  <th
                    className={`num sortable ${screenerSortBy === 'revYoY' ? 'active-sort' : ''}`}
                    onClick={() => {
                      if (screenerSortBy === 'revYoY') setScreenerSortAsc((a) => !a);
                      else {
                        setScreenerSortBy('revYoY');
                        setScreenerSortAsc(false);
                      }
                    }}
                  >
                    XBRL Rev / OpM {screenerSortBy === 'revYoY' ? (screenerSortAsc ? '▲' : '▼') : ''}
                  </th>
                  <th>QoQ Signal Activity</th>
                  <th
                    className={`num sortable ${screenerSortBy === 'netFlow' ? 'active-sort' : ''}`}
                    onClick={() => {
                      if (screenerSortBy === 'netFlow') setScreenerSortAsc((a) => !a);
                      else {
                        setScreenerSortBy('netFlow');
                        setScreenerSortAsc(false);
                      }
                    }}
                  >
                    Net Flow $ {screenerSortBy === 'netFlow' ? (screenerSortAsc ? '▲' : '▼') : ''}
                  </th>
                  <th
                    className={`num sortable ${screenerSortBy === 'holders' ? 'active-sort' : ''}`}
                    onClick={() => {
                      if (screenerSortBy === 'holders') setScreenerSortAsc((a) => !a);
                      else {
                        setScreenerSortBy('holders');
                        setScreenerSortAsc(false);
                      }
                    }}
                  >
                    Holders / Max Wt {screenerSortBy === 'holders' ? (screenerSortAsc ? '▲' : '▼') : ''}
                  </th>
                  <th
                    className={`num sortable ${screenerSortBy === 'ttm' ? 'active-sort' : ''}`}
                    onClick={() => {
                      if (screenerSortBy === 'ttm') setScreenerSortAsc((a) => !a);
                      else {
                        setScreenerSortBy('ttm');
                        setScreenerSortAsc(false);
                      }
                    }}
                  >
                    Stock TTM {screenerSortBy === 'ttm' ? (screenerSortAsc ? '▲' : '▼') : ''}
                  </th>
                  <th>{subTab === 'CONTRARIAN' ? 'Top Specialist Backers' : 'Key Fund Movers'}</th>
                </tr>
              </thead>
              <tbody>
                {activeScreenerList.map((item, idx) => {
                  const isOpen = expandedTicker === item.ticker;
                  const netFlow = item.netEstFlowUsd ?? item.netDeltaUsd;
                  const badgesSource =
                    subTab === 'CONTRARIAN' && item.topBackers.length > 0
                      ? item.topBackers.map((b) => ({
                          cik: b.cik,
                          fundName: b.fundName,
                          fundTier: b.fundTier,
                          meta: `${b.weightPct.toFixed(1)}%${b.status !== 'UNCHANGED' ? ` · ${b.status}` : ''}`,
                          chipTone:
                            b.status === 'NEW' || b.status === 'ADDED'
                              ? 'pos'
                              : b.status === 'TRIMMED'
                                ? 'neg'
                                : b.fundTier === 'Elite 50'
                                  ? 'elite'
                                  : '',
                        }))
                      : item.movers.map((m) => ({
                          cik: m.cik,
                          fundName: m.fundName,
                          fundTier: m.fundTier,
                          meta:
                            m.action === 'NEW'
                              ? `NEW ${m.currBookPct.toFixed(1)}%`
                              : m.action === 'EXITED'
                                ? `EXIT (${formatUsdCompact(Math.abs(m.estFlowUsd ?? m.valueDeltaUsd))})`
                                : `${m.action} ${m.sharesDeltaPct != null ? formatSignedPct(m.sharesDeltaPct, 0) : ''}`,
                          chipTone:
                            m.action === 'NEW' || m.action === 'ADDED' ? 'pos' : 'neg',
                        }));

                  return (
                    <Fragment key={item.ticker}>
                      <tr>
                        <td style={{ width: '30px', paddingRight: 0 }}>
                          <button
                            type="button"
                            onClick={() =>
                              setExpandedTicker(isOpen ? null : item.ticker)
                            }
                            aria-label={`Expand ${item.ticker} details`}
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
                            {renderTickerButton(item.ticker)}
                            <span style={{ fontWeight: 600 }}>{item.name}</span>
                          </div>
                          <div className="mono" style={{ fontSize: '0.71rem', color: 'var(--text-muted)', marginTop: '0.15rem' }}>
                            {item.priceUsd ? `$${item.priceUsd.toFixed(2)}` : '—'}
                            {item.marketCapTier ? ` · ${item.marketCapTier}` : ''}
                            {item.totalValueUsd > 0 ? ` · ${formatUsdCompact(item.totalValueUsd)} held` : ''}
                          </div>
                        </td>
                        <td>
                          <span className="chip">{item.subSector || item.sector}</span>
                        </td>
                        <td style={{ minWidth: '150px' }}>
                          {item.entryZoneStatus ? (
                            <div>
                              <span className={`chip ${entryZoneChipClass(item.entryZoneStatus)}`}>
                                {item.entryZoneStatus}
                              </span>
                              <div className="mono" style={{ fontSize: '0.69rem', color: 'var(--text-muted)', marginTop: '0.15rem' }}>
                                {item.pctVsQuarterVwap != null ? `${formatSignedPct(item.pctVsQuarterVwap)} vs ` : ''}
                                {item.estQuarterVwap != null ? `$${item.estQuarterVwap.toFixed(2)}` : ''}
                              </div>
                            </div>
                          ) : (
                            <span className="ret-muted">—</span>
                          )}
                        </td>
                        <td className="num mono" style={{ fontSize: '0.78rem' }}>
                          {item.fundamentals &&
                          (item.fundamentals.revenueGrowthYoYPct != null ||
                            item.fundamentals.operatingMarginPct != null) ? (
                            <div>
                              <div className={returnClass(item.fundamentals.revenueGrowthYoYPct)}>
                                {item.fundamentals.revenueGrowthYoYPct != null
                                  ? `${formatSignedPct(item.fundamentals.revenueGrowthYoYPct)} rev`
                                  : '—'}
                              </div>
                              <div style={{ fontSize: '0.69rem', color: 'var(--text-muted)' }}>
                                {item.fundamentals.operatingMarginPct != null
                                  ? `${item.fundamentals.operatingMarginPct.toFixed(1)}% OpM`
                                  : ''}
                              </div>
                            </div>
                          ) : (
                            <span className="ret-muted">—</span>
                          )}
                        </td>
                        <td style={{ minWidth: '165px' }}>
                          <div style={{ display: 'flex', gap: '0.28rem', flexWrap: 'wrap' }}>
                            {item.newCount > 0 && (
                              <span className="chip pos">
                                <Sparkles size={10} /> {item.newCount} New
                                {item.eliteNewCount > 0 ? ` (${item.eliteNewCount} Elite)` : ''}
                              </span>
                            )}
                            {item.addedCount > 0 && (
                              <span className="chip tech">
                                <Zap size={10} /> {item.addedCount} Add
                              </span>
                            )}
                            {(item.trimmedCount > 0 || item.exitedCount > 0) && (
                              <span className="chip neg">
                                <TrendingDown size={10} /> {item.trimmedCount}T / {item.exitedCount}X
                              </span>
                            )}
                          </div>
                        </td>
                        <td
                          className={`num mono ${returnClass(netFlow)}`}
                          title={
                            item.netEstFlowUsd != null
                              ? `Q-VWAP est. flow (reported MV Δ: ${formatSignedUsd(item.netDeltaUsd)})`
                              : undefined
                          }
                        >
                          {formatSignedUsd(netFlow)}
                        </td>
                        <td className="num mono">
                          <div>
                            <strong>{item.holderCount}</strong> ({item.eliteHoldersCount} Elite)
                          </div>
                          <div style={{ fontSize: '0.69rem', color: 'var(--text-muted)' }}>
                            Avg {item.avgWeightPct.toFixed(1)}% · Max {item.maxWeightPct.toFixed(1)}%
                          </div>
                        </td>
                        <td className={`num mono ${returnClass(item.stockTtmPct)}`}>
                          {formatSignedPct(item.stockTtmPct, 1)}
                        </td>
                        <td style={{ minWidth: '285px' }}>
                          <div style={{ display: 'flex', flexWrap: 'wrap', gap: '0.28rem' }}>
                            {badgesSource.slice(0, 3).map((b, bi) => (
                              <button
                                key={`${b.cik}-${bi}`}
                                type="button"
                                className={`chip clickable ${b.chipTone}`}
                                onClick={() => onSelectFund(b.cik)}
                                title={`Open ${b.fundName} (${b.fundTier})`}
                              >
                                <span>{b.fundName}</span>
                                <strong className="mono">{b.meta}</strong>
                              </button>
                            ))}
                          </div>
                        </td>
                      </tr>

                      {isOpen && (
                        <tr className="subrow">
                          <td colSpan={11}>
                            <div style={{ marginBottom: '0.55rem', display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: '0.5rem' }}>
                              <span style={{ fontSize: '0.8rem', fontWeight: 600 }}>
                                {item.ticker} ({item.name}) — {item.movers.length} Key QoQ Moves &amp; {item.topBackers.length} Largest Holders
                              </span>
                              <button
                                type="button"
                                className="action-btn"
                                style={{ padding: '0.28rem 0.7rem', fontSize: '0.74rem' }}
                                onClick={() => onSelectStock(item.ticker)}
                              >
                                Open Complete {item.ticker} Stock Dossier
                              </button>
                            </div>
                            <div className="holder-grid">
                              {item.movers.map((mv, mi) => {
                                const mvFlow = mv.estFlowUsd ?? mv.valueDeltaUsd;
                                return (
                                  <div key={`mv-${mv.cik}-${mi}`} className="holder-card">
                                    <div>
                                      <div style={{ display: 'flex', alignItems: 'center', gap: '0.35rem', flexWrap: 'wrap' }}>
                                        <button
                                          type="button"
                                          className="fund-link-btn"
                                          style={{ fontSize: '0.81rem' }}
                                          onClick={() => onSelectFund(mv.cik)}
                                        >
                                          {mv.fundName}
                                        </button>
                                        <span className={`chip ${tierChipClass(mv.fundTier)}`} style={{ fontSize: '0.64rem' }}>
                                          {mv.fundTier}
                                        </span>
                                      </div>
                                      <div className="mono" style={{ fontSize: '0.7rem', color: 'var(--text-muted)', marginTop: '0.15rem' }}>
                                        Book: {mv.prevBookPct.toFixed(1)}% → {mv.currBookPct.toFixed(1)}%
                                      </div>
                                    </div>
                                    <div style={{ textAlign: 'right' }}>
                                      <span className={`chip ${mv.action === 'NEW' || mv.action === 'ADDED' ? 'pos' : 'neg'}`}>
                                        {mv.action}
                                        {mv.sharesDeltaPct != null && mv.action !== 'NEW' && mv.action !== 'EXITED'
                                          ? ` ${formatSignedPct(mv.sharesDeltaPct, 0)}`
                                          : ''}
                                      </span>
                                      <div
                                        className={`mono ${returnClass(mvFlow)}`}
                                        style={{ fontSize: '0.72rem', marginTop: '0.15rem' }}
                                        title={mv.estFlowUsd != null ? `Q-VWAP est. flow (MV Δ: ${formatSignedUsd(mv.valueDeltaUsd)})` : undefined}
                                      >
                                        {formatSignedUsd(mvFlow)}
                                      </div>
                                    </div>
                                  </div>
                                );
                              })}
                              {item.movers.length === 0 &&
                                item.topBackers.map((bk) => (
                                  <div key={`bk-${bk.cik}`} className="holder-card">
                                    <div>
                                      <button
                                        type="button"
                                        className="fund-link-btn"
                                        style={{ fontSize: '0.81rem' }}
                                        onClick={() => onSelectFund(bk.cik)}
                                      >
                                        {bk.fundName}
                                      </button>
                                      <div>
                                        <span className={`chip ${tierChipClass(bk.fundTier)}`} style={{ fontSize: '0.64rem' }}>
                                          {bk.fundTier}
                                        </span>
                                      </div>
                                    </div>
                                    <div style={{ textAlign: 'right' }}>
                                      <div className="mono" style={{ fontWeight: 700 }}>{bk.weightPct.toFixed(2)}%</div>
                                      <span className="chip">{bk.status}</span>
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
                {activeScreenerList.length === 0 && (
                  <tr>
                    <td colSpan={11} style={{ textAlign: 'center', padding: '2rem', color: 'var(--text-muted)' }}>
                      No screener signals match your active filters. Try clearing Cap Tier or Entry Zone filters.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </section>
  );
};
