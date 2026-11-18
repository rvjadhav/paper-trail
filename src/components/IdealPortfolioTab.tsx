import { useEffect, useMemo, useState, type FC } from 'react';
import { AlertTriangle, Calculator, Check, Download, Info, RotateCcw, Save } from 'lucide-react';
import type {
  ActionVerdict,
  BenchmarkSeries,
  DualConvictionSignal,
  Form4InsiderSummary,
  FundSummary,
  IdealPortfolioData,
  IdealPortfolioPosition,
  ReturnMode,
  StockIndexEntry,
} from '../types/data';
import { resolveAlphaDecayMetrics } from '../utils/alphaDecay';
import {
  buildFundSkillMap,
  resolveActionabilityMatrix,
  type UnifiedConvictionRow,
} from '../utils/convictionMatrix';
import {
  actionVerdictChipClass,
  actionVerdictShortLabel,
  alphaDecayChipClass,
  alphaDecayLabel,
  convergenceTierChipClass,
  convergenceTierLabel,
  downloadCsv,
  entryZoneChipClass,
  formatPct,
  formatSignedPct,
  formatSignedUsd,
  formatUsdCompact,
  insiderSignalChipClass,
  insiderSignalLabelText,
  returnClass,
} from '../utils/format';
import {
  buildConstrainedPortfolio,
  computeRebalancePlan,
  parseHoldingsInput,
  type DynamicPosition,
  type RebalanceMode,
  type SectorCapOption,
  type SignalMode,
  type UniverseFilter,
} from '../utils/portfolioMath';
import { EquityCurveChart, SectorBreakdownBar } from './SvgCharts';

interface IdealPortfolioTabProps {
  idealPortfolio: IdealPortfolioData;
  benchmarks: Record<'QQQ' | 'SPY' | 'XLK', BenchmarkSeries>;
  stocksIndex: Record<string, StockIndexEntry>;
  funds?: FundSummary[];
  convictionByTicker?: Record<string, UnifiedConvictionRow>;
  dualConvictionByTicker?: Record<string, DualConvictionSignal>;
  form4Signals?: Record<string, Form4InsiderSummary>;
  latestQuarterLabel?: string;
  mode: ReturnMode;
  onModeChange: (m: ReturnMode) => void;
  onSelectFund: (cik: string) => void;
  onSelectStock: (ticker: string) => void;
}

type IdealEntryFilter = 'ALL' | 'VALUE' | 'EXTENDED';
type IdealVerdictFilter = 'ALL' | ActionVerdict;
type IdealSortKey =
  | 'idealWeightPct'
  | 'actionScore'
  | 'revYoY'
  | 'fcfMargin'
  | 'convictionScore'
  | 'holderCount'
  | 'stockTtmPct';

const VERDICT_ORDER: ActionVerdict[] = [
  'STRONG ACCUMULATE',
  'ACCUMULATE ON DIP',
  'HOLD',
  'DISTRIBUTION WARNING',
  'EXIT SIGNAL',
];

interface SavedBuilderPreset {
  universeFilter: UniverseFilter;
  signalMode: SignalMode;
  maxNames: number;
  capPct: number;
  sectorCapPct: SectorCapOption;
  capitalUsd: number;
}

interface SavedRebalanceState {
  showRebalance: boolean;
  holdingsText: string;
  rebalanceMode: RebalanceMode;
  newCashUsd: number;
  driftTolerancePct: number;
}

const PRESET_STORAGE_KEY = 'paper-trail-builder-preset-v1';
const REBALANCE_STORAGE_KEY = 'paper-trail-rebalance-state-v1';

function loadSavedPreset(): Partial<SavedBuilderPreset> {
  try {
    const raw = window.localStorage.getItem(PRESET_STORAGE_KEY);
    if (!raw) return {};
    return JSON.parse(raw) as Partial<SavedBuilderPreset>;
  } catch {
    return {};
  }
}

function loadSavedRebalanceState(): Partial<SavedRebalanceState> {
  try {
    const raw = window.localStorage.getItem(REBALANCE_STORAGE_KEY);
    if (!raw) return {};
    return JSON.parse(raw) as Partial<SavedRebalanceState>;
  } catch {
    return {};
  }
}

export const IdealPortfolioTab: FC<IdealPortfolioTabProps> = ({
  idealPortfolio,
  benchmarks,
  stocksIndex,
  funds,
  convictionByTicker,
  dualConvictionByTicker,
  form4Signals,
  latestQuarterLabel,
  mode,
  onModeChange,
  onSelectFund,
  onSelectStock,
}) => {
  const initialPreset = useMemo(() => loadSavedPreset(), []);
  const initialRebalance = useMemo(() => loadSavedRebalanceState(), []);
  const defaultMaxNames = Math.min(40, idealPortfolio.positions.length || 40);

  const [universeFilter, setUniverseFilter] = useState<UniverseFilter>(
    initialPreset.universeFilter ?? 'ALL'
  );
  const [signalMode, setSignalMode] = useState<SignalMode>(
    initialPreset.signalMode ?? 'rawScore'
  );
  const [maxNames, setMaxNames] = useState<number>(initialPreset.maxNames ?? defaultMaxNames);
  const [capPct, setCapPct] = useState<number>(initialPreset.capPct ?? 8);
  const [sectorCapPct, setSectorCapPct] = useState<SectorCapOption>(
    initialPreset.sectorCapPct ?? null
  );
  const [capitalUsd, setCapitalUsd] = useState<number>(initialPreset.capitalUsd ?? 100000);
  const [savedToast, setSavedToast] = useState(false);

  const [showRebalance, setShowRebalance] = useState<boolean>(
    initialRebalance.showRebalance ?? false
  );
  const [holdingsText, setHoldingsText] = useState<string>(
    initialRebalance.holdingsText ?? ''
  );
  const [rebalanceMode, setRebalanceMode] = useState<RebalanceMode>(
    initialRebalance.rebalanceMode ?? 'full'
  );
  const [newCashUsd, setNewCashUsd] = useState<number>(
    initialRebalance.newCashUsd ?? 10000
  );
  const [driftTolerancePct, setDriftTolerancePct] = useState<number>(
    initialRebalance.driftTolerancePct ?? 0.5
  );

  const [entryFilter, setEntryFilter] = useState<IdealEntryFilter>('ALL');
  const [verdictFilter, setVerdictFilter] = useState<IdealVerdictFilter>('ALL');
  const [sortBy, setSortBy] = useState<IdealSortKey>('idealWeightPct');
  const [sortAsc, setSortAsc] = useState(false);

  useEffect(() => {
    try {
      const payload: SavedRebalanceState = {
        showRebalance,
        holdingsText,
        rebalanceMode,
        newCashUsd,
        driftTolerancePct,
      };
      window.localStorage.setItem(REBALANCE_STORAGE_KEY, JSON.stringify(payload));
    } catch {
      // Ignore storage quota errors
    }
  }, [showRebalance, holdingsText, rebalanceMode, newCashUsd, driftTolerancePct]);

  const fundSkillByCik = useMemo(() => buildFundSkillMap(funds, mode), [funds, mode]);

  const getPositionActionability = (p: IdealPortfolioPosition) =>
    convictionByTicker?.[p.ticker]?.actionability ??
    resolveActionabilityMatrix(p, {
      fundSkillByCik,
      latestQuarterLabel,
      isCoreConsensus: p.rationaleTag === 'Core Consensus',
    });

  const renderConvergenceBadges = (ticker: string) => {
    const upper = ticker.toUpperCase();
    const conv = dualConvictionByTicker?.[upper] ?? dualConvictionByTicker?.[ticker];
    const f4 = form4Signals?.[upper] ?? form4Signals?.[ticker];
    const hasBuy = Boolean(f4 && f4.openMarketBuyCount > 0);
    if (!conv && !hasBuy) return null;
    return (
      <div style={{ display: 'flex', gap: '0.24rem', flexWrap: 'wrap', marginTop: '0.2rem' }}>
        {conv && (
          <span
            className={`chip ${convergenceTierChipClass(conv.tier)}`}
            style={{ fontSize: '0.62rem' }}
            title={conv.headline}
          >
            {convergenceTierLabel(conv.tier)}
          </span>
        )}
        {f4 && hasBuy && (
          <span
            className={`chip ${insiderSignalChipClass(f4.insiderSignalLabel)}`}
            style={{ fontSize: '0.62rem' }}
            title={`Form 4 open-market ('P') buys: ${f4.openMarketBuyCount} trades by ${f4.distinctBuyersCount} insiders`}
          >
            {insiderSignalLabelText(f4.insiderSignalLabel)} · {formatUsdCompact(f4.totalBuyValueUsd)}
          </span>
        )}
      </div>
    );
  };

  const defaultWeightMap = useMemo(
    () => new Map(idealPortfolio.positions.map((p) => [p.ticker, p.idealWeightPct])),
    [idealPortfolio.positions]
  );

  const isDefault =
    universeFilter === 'ALL' &&
    signalMode === 'rawScore' &&
    maxNames === idealPortfolio.positions.length &&
    capPct === 8 &&
    sectorCapPct === null;

  const { activePositions, constraintNote } = useMemo(() => {
    if (isDefault) {
      return {
        activePositions: idealPortfolio.positions.map((p) => ({
          ...p,
          dynamicWeightPct: p.idealWeightPct,
          weightVsDefaultPct: 0,
        })),
        constraintNote: null,
      };
    }
    const pool =
      idealPortfolio.candidatePool && idealPortfolio.candidatePool.length > 0
        ? idealPortfolio.candidatePool
        : idealPortfolio.positions;
    const res = buildConstrainedPortfolio(
      pool,
      universeFilter,
      signalMode,
      maxNames,
      capPct,
      sectorCapPct,
      defaultWeightMap
    );
    return { activePositions: res.positions, constraintNote: res.constraintNote };
  }, [
    isDefault,
    idealPortfolio.positions,
    idealPortfolio.candidatePool,
    universeFilter,
    signalMode,
    maxNames,
    capPct,
    sectorCapPct,
    defaultWeightMap,
  ]);

  const activePositionByTicker = useMemo(
    () => new Map(activePositions.map((p) => [p.ticker, p])),
    [activePositions]
  );

  const verdictCounts = useMemo(() => {
    const counts: Record<ActionVerdict, number> = {
      'STRONG ACCUMULATE': 0,
      'ACCUMULATE ON DIP': 0,
      HOLD: 0,
      'DISTRIBUTION WARNING': 0,
      'EXIT SIGNAL': 0,
    };
    for (const p of activePositions) {
      const v = (
        convictionByTicker?.[p.ticker]?.actionability ??
        resolveActionabilityMatrix(p, {
          fundSkillByCik,
          latestQuarterLabel,
          isCoreConsensus: p.rationaleTag === 'Core Consensus',
        })
      ).verdict;
      counts[v] += 1;
    }
    return counts;
  }, [activePositions, convictionByTicker, fundSkillByCik, latestQuarterLabel]);

  const dynamicSectors = useMemo(() => {
    const map = new Map<string, { sector: string; weightPct: number; stockCount: number }>();
    for (const p of activePositions) {
      const key = p.subSector || p.sector;
      const cur = map.get(key) ?? { sector: key, weightPct: 0, stockCount: 0 };
      cur.weightPct += p.dynamicWeightPct;
      cur.stockCount += 1;
      map.set(key, cur);
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

  const knownTickers = useMemo(() => {
    const s = new Set(Object.keys(stocksIndex));
    (idealPortfolio.candidatePool ?? idealPortfolio.positions).forEach((p) => s.add(p.ticker));
    idealPortfolio.positions.forEach((p) => s.add(p.ticker));
    return s;
  }, [stocksIndex, idealPortfolio.candidatePool, idealPortfolio.positions]);

  const parsedHoldings = useMemo(
    () => parseHoldingsInput(holdingsText, capitalUsd, knownTickers),
    [holdingsText, capitalUsd, knownTickers]
  );

  const rebalancePlan = useMemo(
    () =>
      computeRebalancePlan({
        holdingsMap: parsedHoldings.holdings,
        activePositions,
        stocksIndex,
        capitalUsd,
        mode: rebalanceMode,
        newCashUsd,
        driftTolerancePct,
      }),
    [
      parsedHoldings.holdings,
      activePositions,
      stocksIndex,
      capitalUsd,
      rebalanceMode,
      newCashUsd,
      driftTolerancePct,
    ]
  );

  const getTickerActionabilityForRebalance = (ticker: string) => {
    const conv = convictionByTicker?.[ticker]?.actionability;
    if (conv) return conv;
    const pos = activePositionByTicker.get(ticker);
    if (pos) {
      return resolveActionabilityMatrix(pos, {
        fundSkillByCik,
        latestQuarterLabel,
        isCoreConsensus: pos.rationaleTag === 'Core Consensus',
      });
    }
    const idxEntry = stocksIndex[ticker];
    if (idxEntry) {
      return resolveActionabilityMatrix(idxEntry, {
        fundSkillByCik,
        latestQuarterLabel,
      });
    }
    return null;
  };

  const displayedPositions = useMemo<DynamicPosition[]>(() => {
    const filtered = activePositions.filter((p) => {
      if (entryFilter === 'VALUE') {
        if (p.entryZoneStatus !== 'Discount to Q-Avg' && p.entryZoneStatus !== 'Near Q-Avg') {
          return false;
        }
      } else if (entryFilter === 'EXTENDED') {
        if (p.entryZoneStatus !== 'Extended' && p.entryZoneStatus !== 'Chasing (+30%+)') {
          return false;
        }
      }
      if (verdictFilter !== 'ALL') {
        const v = (
          convictionByTicker?.[p.ticker]?.actionability ??
          resolveActionabilityMatrix(p, {
            fundSkillByCik,
            latestQuarterLabel,
            isCoreConsensus: p.rationaleTag === 'Core Consensus',
          })
        ).verdict;
        if (v !== verdictFilter) return false;
      }
      return true;
    });
    if (sortBy === 'idealWeightPct' && !sortAsc) return filtered;
    const cmpNullable = (a: number | null | undefined, b: number | null | undefined): number => {
      const an = a == null || !Number.isFinite(a) ? null : a;
      const bn = b == null || !Number.isFinite(b) ? null : b;
      if (an === null && bn === null) return 0;
      if (an === null) return 1;
      if (bn === null) return -1;
      return sortAsc ? an - bn : bn - an;
    };
    return [...filtered].sort((a, b) => {
      if (sortBy === 'actionScore') {
        const sa = (
          convictionByTicker?.[a.ticker]?.actionability ??
          resolveActionabilityMatrix(a, {
            fundSkillByCik,
            latestQuarterLabel,
            isCoreConsensus: a.rationaleTag === 'Core Consensus',
          })
        ).score;
        const sb = (
          convictionByTicker?.[b.ticker]?.actionability ??
          resolveActionabilityMatrix(b, {
            fundSkillByCik,
            latestQuarterLabel,
            isCoreConsensus: b.rationaleTag === 'Core Consensus',
          })
        ).score;
        return cmpNullable(sa, sb);
      }
      if (sortBy === 'revYoY') {
        return cmpNullable(a.fundamentals?.revenueGrowthYoYPct, b.fundamentals?.revenueGrowthYoYPct);
      }
      if (sortBy === 'fcfMargin') {
        return cmpNullable(a.fundamentals?.fcfMarginPct, b.fundamentals?.fcfMarginPct);
      }
      if (sortBy === 'convictionScore') {
        return cmpNullable(a.convictionScore, b.convictionScore);
      }
      if (sortBy === 'holderCount') {
        return cmpNullable(a.holderCount, b.holderCount);
      }
      if (sortBy === 'stockTtmPct') {
        return cmpNullable(a.stockTtmPct, b.stockTtmPct);
      }
      return cmpNullable(a.dynamicWeightPct, b.dynamicWeightPct);
    });
  }, [
    activePositions,
    entryFilter,
    verdictFilter,
    convictionByTicker,
    fundSkillByCik,
    latestQuarterLabel,
    sortBy,
    sortAsc,
  ]);

  const toggleSort = (col: IdealSortKey) => {
    if (sortBy === col) {
      setSortAsc((v) => !v);
    } else {
      setSortBy(col);
      setSortAsc(false);
    }
  };

  const handleSavePreset = () => {
    try {
      const payload: SavedBuilderPreset = {
        universeFilter,
        signalMode,
        maxNames,
        capPct,
        sectorCapPct,
        capitalUsd,
      };
      window.localStorage.setItem(PRESET_STORAGE_KEY, JSON.stringify(payload));
      setSavedToast(true);
      window.setTimeout(() => setSavedToast(false), 1800);
    } catch {
      // Ignore storage quota errors
    }
  };

  const handleResetPreset = () => {
    setUniverseFilter('ALL');
    setSignalMode('rawScore');
    setMaxNames(defaultMaxNames);
    setCapPct(8);
    setSectorCapPct(null);
    setCapitalUsd(100000);
    try {
      window.localStorage.removeItem(PRESET_STORAGE_KEY);
    } catch {
      // Ignore
    }
  };

  const handleLoadSampleHoldings = () => {
    const sampleTop = idealPortfolio.positions.slice(0, 4);
    const lines = [
      `${sampleTop[0]?.ticker ?? 'NVDA'} ${Math.round(capitalUsd * 0.22)}`,
      `${sampleTop[1]?.ticker ?? 'MSFT'} ${Math.round(capitalUsd * 0.16)}`,
      `${sampleTop[2]?.ticker ?? 'META'} 12%`,
      `INTC 8000`,
    ];
    setHoldingsText(lines.join('\n'));
  };

  const handleExport = () => {
    downloadCsv(
      `paper-trail-ideal-portfolio-${universeFilter.toLowerCase()}-${signalMode}-${displayedPositions.length}names.csv`,
      [
        'Rank',
        'Ticker',
        'Company',
        'Action Verdict',
        'Actionability Score (/100)',
        'Sub-Sector',
        'Market Cap Tier',
        'Ideal Weight (%)',
        'Delta vs Default (pts)',
        'Allocation (USD)',
        'Est. Q-VWAP (USD)',
        'vs Q-VWAP (%)',
        'Entry Zone',
        'Alpha-Decay Status',
        'Filing->Today (%)',
        'Avg Elite Hold (Q)',
        'XBRL Rev YoY (%)',
        'XBRL Op Margin (%)',
        'XBRL FCF Margin (%)',
        'Conviction Score',
        'Ranked Holders',
        'Avg Fund Weight (%)',
        'Signal Tag',
        'Convergence Signal',
        'Form 4 Insider Signal',
        'Top Backers',
      ],
      displayedPositions.map((p) => {
        const ad = resolveAlphaDecayMetrics(p, { latestQuarterLabel });
        const act = getPositionActionability(p);
        const upper = p.ticker.toUpperCase();
        const conv = dualConvictionByTicker?.[upper] ?? dualConvictionByTicker?.[p.ticker];
        const f4 = form4Signals?.[upper] ?? form4Signals?.[p.ticker];
        return [
          p.rank,
          p.ticker,
          p.name,
          act.verdict,
          act.score,
          p.subSector || p.sector,
          p.marketCapTier ?? '',
          p.dynamicWeightPct,
          p.weightVsDefaultPct ?? '',
          Math.round((capitalUsd * p.dynamicWeightPct) / 100),
          p.estQuarterVwap ?? '',
          p.pctVsQuarterVwap ?? '',
          p.entryZoneStatus ?? '',
          alphaDecayLabel(ad.status),
          ad.filingToTodayPct ?? '',
          ad.avgEliteHoldingQuarters ?? '',
          p.fundamentals?.revenueGrowthYoYPct ?? '',
          p.fundamentals?.operatingMarginPct ?? '',
          p.fundamentals?.fcfMarginPct ?? '',
          p.convictionScore,
          p.holderCount,
          p.avgFundWeightPct,
          p.rationaleTag,
          conv ? convergenceTierLabel(conv.tier) : '',
          f4 && f4.openMarketBuyCount > 0
            ? `${insiderSignalLabelText(f4.insiderSignalLabel)} (${formatUsdCompact(f4.totalBuyValueUsd)})`
            : '',
          p.topBackers.map((b) => `${b.fundName} (${b.weightPct.toFixed(1)}%)`).join('; '),
        ];
      })
    );
  };

  const handleExportRebalance = () => {
    if (!rebalancePlan) return;
    downloadCsv(
      `paper-trail-rebalance-${rebalanceMode}-trades-${activePositions.length}names.csv`,
      [
        'Mode',
        'Action',
        'Ticker',
        'Company',
        'Action Verdict',
        'Actionability Score (/100)',
        'Current (USD)',
        'Current (%)',
        'Target (USD)',
        'Target (%)',
        'Trade Delta (USD)',
        'Est. Shares',
      ],
      rebalancePlan.rows.map((r) => {
        const act = getTickerActionabilityForRebalance(r.ticker);
        return [
          rebalanceMode === 'buyOnly' ? 'Buy-Only' : 'Full',
          r.action,
          r.ticker,
          r.name,
          act?.verdict ?? '',
          act?.score ?? '',
          Math.round(r.currUsd),
          r.currPct.toFixed(2),
          Math.round(r.targetUsd),
          r.targetPct.toFixed(2),
          Math.round(r.deltaUsd),
          r.estSharesDelta ?? '',
        ];
      })
    );
  };

  return (
    <section aria-label="Interactive Ideal Portfolio Builder">
      <div className="section-toolbar">
        <div className="section-title-group">
          <h2>Interactive Portfolio Builder ({activePositions.length} Names)</h2>
          <p>{idealPortfolio.methodologySummary}</p>
        </div>

        <div className="controls-row">
          <button
            type="button"
            id="toggle-rebalance-calc"
            className={`action-btn ${showRebalance ? 'primary' : ''}`}
            onClick={() => setShowRebalance((v) => !v)}
          >
            <Calculator size={14} /> {showRebalance ? 'Hide Rebalance Calc' : 'Rebalance Calculator'}
          </button>
          <button type="button" className="action-btn" onClick={handleSavePreset}>
            {savedToast ? <Check size={14} color="var(--pos-text)" /> : <Save size={14} />}
            {savedToast ? 'Saved Preset' : 'Save Preset'}
          </button>
          {!isDefault && (
            <button type="button" className="action-btn" onClick={handleResetPreset}>
              <RotateCcw size={14} /> Reset Default
            </button>
          )}
        </div>
      </div>

      {/* Builder Controls Grid */}
      <div className="panel" style={{ padding: '1rem 1.25rem', marginBottom: '1.25rem' }}>
        <div className="builder-controls-grid">
          <div className="builder-ctrl-group">
            <span className="builder-ctrl-label">Market-Cap Universe</span>
            <div className="segmented-group" role="group" aria-label="Market cap universe">
              {(
                [
                  { id: 'ALL', label: 'All-Cap Tech' },
                  { id: 'EX_MEGA', label: 'Ex-Mega <$200B' },
                  { id: 'EMERGING', label: 'Emerging <$30B' },
                ] as const
              ).map((u) => (
                <button
                  key={u.id}
                  type="button"
                  id={`builder-univ-${u.id.toLowerCase()}`}
                  className={`seg-btn ${universeFilter === u.id ? 'active' : ''}`}
                  onClick={() => setUniverseFilter(u.id)}
                >
                  {u.label}
                </button>
              ))}
            </div>
          </div>

          <div className="builder-ctrl-group">
            <span className="builder-ctrl-label">Signal Emphasis</span>
            <div className="segmented-group" role="group" aria-label="Signal emphasis">
              <button
                type="button"
                id="builder-sig-core"
                className={`seg-btn ${signalMode === 'rawScore' ? 'active' : ''}`}
                onClick={() => setSignalMode('rawScore')}
                title="Emphasizes steady multi-year Elite-50 core conviction"
              >
                Core Compounder
              </button>
              <button
                type="button"
                id="builder-sig-inflection"
                className={`seg-btn ${signalMode === 'inflectionScore' ? 'active' : ''}`}
                onClick={() => setSignalMode('inflectionScore')}
                title="Tilts toward fresh QoQ initiations and heavy accumulation"
              >
                Fresh Inflection
              </button>
            </div>
          </div>

          <div className="builder-ctrl-group">
            <span className="builder-ctrl-label">Portfolio Size</span>
            <div className="segmented-group" role="group" aria-label="Portfolio size">
              {[20, 30, 40].map((n) => (
                <button
                  key={n}
                  type="button"
                  id={`builder-size-${n}`}
                  className={`seg-btn ${maxNames === n ? 'active' : ''}`}
                  onClick={() => setMaxNames(n)}
                >
                  {n}
                </button>
              ))}
            </div>
          </div>

          <div className="builder-ctrl-group">
            <span className="builder-ctrl-label">Single-Stock Cap</span>
            <div className="segmented-group" role="group" aria-label="Single position cap">
              {[5, 8, 10].map((c) => (
                <button
                  key={c}
                  type="button"
                  id={`builder-cap-${c}`}
                  className={`seg-btn ${capPct === c ? 'active' : ''}`}
                  onClick={() => setCapPct(c)}
                >
                  {c}%
                </button>
              ))}
            </div>
          </div>

          <div className="builder-ctrl-group">
            <span className="builder-ctrl-label">Sub-Sector Cap</span>
            <div className="segmented-group" role="group" aria-label="Sub-sector cap">
              {(
                [
                  { val: null, label: 'No Cap' },
                  { val: 35, label: '35%' },
                  { val: 25, label: '25%' },
                ] as const
              ).map((sc) => (
                <button
                  key={String(sc.val)}
                  type="button"
                  id={`builder-seccap-${String(sc.val ?? 'none')}`}
                  className={`seg-btn ${sectorCapPct === sc.val ? 'active' : ''}`}
                  onClick={() => setSectorCapPct(sc.val)}
                >
                  {sc.label}
                </button>
              ))}
            </div>
          </div>
        </div>

        {constraintNote && (
          <div style={{ marginTop: '0.7rem', fontSize: '0.78rem', color: 'var(--warn-text)', fontWeight: 500 }}>
            {constraintNote}
          </div>
        )}
      </div>

      {/* Collapsible Rebalance Trade Diff Calculator */}
      {showRebalance && (
        <div className="panel rebalance-panel" style={{ marginBottom: '1.35rem' }}>
          <div className="panel-header">
            <div>
              <h3 className="panel-title" style={{ display: 'flex', alignItems: 'center', gap: '0.45rem' }}>
                <Calculator size={16} color="var(--accent-primary)" /> Portfolio Rebalance Trade Calculator
              </h3>
              <p className="panel-subtitle">
                {rebalanceMode === 'buyOnly'
                  ? `Tax-aware Cash Flow Buy-Only mode: deploys $${Math.max(0, newCashUsd).toLocaleString()} fresh cash into your most underweight target positions without generating sells.`
                  : `Paste current positions in USD or % (e.g. NVDA 20000 or MSFT 15%) to generate exact BUY / ADD / TRIM / EXIT orders against your ${activePositions.length}-name target model ($${capitalUsd.toLocaleString()} capital).`}
              </p>
            </div>
            <div className="controls-row">
              <button type="button" className="action-btn" onClick={handleLoadSampleHoldings}>
                Load Sample Portfolio
              </button>
              {rebalancePlan && (
                <button type="button" className="action-btn" onClick={handleExportRebalance}>
                  <Download size={14} /> Export Trades CSV
                </button>
              )}
            </div>
          </div>

          <div style={{ padding: '1rem 1.25rem', display: 'grid', gridTemplateColumns: 'minmax(260px, 345px) 1fr', gap: '1.15rem', alignItems: 'start' }}>
            <div>
              <div className="rebalance-mode-grid">
                <div>
                  <span className="builder-ctrl-label" style={{ display: 'block', marginBottom: '0.28rem' }}>
                    Rebalance Mode
                  </span>
                  <div className="segmented-group" role="group" aria-label="Rebalance mode">
                    <button
                      type="button"
                      id="rebalance-mode-full"
                      className={`seg-btn ${rebalanceMode === 'full' ? 'active' : ''}`}
                      onClick={() => setRebalanceMode('full')}
                    >
                      Full Rebalance
                    </button>
                    <button
                      type="button"
                      id="rebalance-mode-buyonly"
                      className={`seg-btn ${rebalanceMode === 'buyOnly' ? 'active' : ''}`}
                      onClick={() => setRebalanceMode('buyOnly')}
                    >
                      Buy-Only Cash Flow
                    </button>
                  </div>
                </div>

                {rebalanceMode === 'buyOnly' && (
                  <div>
                    <label
                      htmlFor="rebalance-new-cash"
                      className="builder-ctrl-label"
                      style={{ display: 'block', marginBottom: '0.28rem' }}
                    >
                      New Cash ($)
                    </label>
                    <input
                      id="rebalance-new-cash"
                      type="number"
                      min={0}
                      step={500}
                      value={newCashUsd}
                      onChange={(e) => setNewCashUsd(Math.max(0, Number(e.target.value) || 0))}
                      className="search-input mono"
                      style={{ width: '120px', minWidth: '100px', padding: '0.3rem 0.5rem' }}
                    />
                  </div>
                )}

                <div>
                  <span className="builder-ctrl-label" style={{ display: 'block', marginBottom: '0.28rem' }}>
                    Drift Tolerance
                  </span>
                  <div className="segmented-group" role="group" aria-label="Drift tolerance percentage">
                    {[0.25, 0.5, 1.0, 2.0].map((tol) => (
                      <button
                        key={tol}
                        type="button"
                        className={`seg-btn ${driftTolerancePct === tol ? 'active' : ''}`}
                        onClick={() => setDriftTolerancePct(tol)}
                      >
                        {tol.toFixed(tol === 0.25 ? 2 : 1)}%
                      </button>
                    ))}
                  </div>
                </div>
              </div>

              <label htmlFor="rebalance-input" style={{ display: 'block', fontSize: '0.76rem', fontWeight: 600, color: 'var(--text-secondary)', marginBottom: '0.35rem' }}>
                Your Current Holdings (one per line or comma-separated)
              </label>
              <textarea
                id="rebalance-input"
                rows={6}
                className="rebalance-textarea mono"
                placeholder={`NVDA 22000\nMSFT 16000\nMETA 12%\nINTC 8000`}
                value={holdingsText}
                onChange={(e) => setHoldingsText(e.target.value)}
              />

              {(parsedHoldings.normalizedAliases.length > 0 ||
                parsedHoldings.unknownTickers.length > 0 ||
                parsedHoldings.unparsedLines.length > 0) && (
                <div className="rebalance-warn-box" role="status">
                  {parsedHoldings.normalizedAliases.length > 0 && (
                    <div>
                      <strong>Share-class normalized:</strong>{' '}
                      {parsedHoldings.normalizedAliases.map((a) => `${a.from}→${a.to}`).join(', ')}
                    </div>
                  )}
                  {parsedHoldings.unknownTickers.length > 0 && (
                    <div>
                      <strong>Not in tracked 13F tech index:</strong>{' '}
                      {parsedHoldings.unknownTickers.join(', ')}{' '}
                      {rebalanceMode === 'full' ? '(flagged as EXIT)' : '(kept untouched)'}
                    </div>
                  )}
                  {parsedHoldings.unparsedLines.length > 0 && (
                    <div>
                      <strong>Ignored unparsed lines:</strong>{' '}
                      {parsedHoldings.unparsedLines.join(' | ')} (expected format: <code>NVDA 20000</code> or <code>MSFT 15%</code>)
                    </div>
                  )}
                </div>
              )}

              {rebalancePlan && (
                <div className="mono" style={{ fontSize: '0.75rem', color: 'var(--text-secondary)', marginTop: '0.6rem', display: 'flex', flexDirection: 'column', gap: '0.2rem' }}>
                  <span>Current Invested: <strong>{formatUsdCompact(rebalancePlan.totalCurrentUsd)}</strong></span>
                  {rebalanceMode === 'buyOnly' && (
                    <span>
                      Post-Deploy Portfolio: <strong>{formatUsdCompact(rebalancePlan.postDeployUsd)}</strong>
                    </span>
                  )}
                  <span className="ret-pos">
                    Total Buys / Adds: <strong>{formatSignedUsd(rebalancePlan.totalBuyUsd)}</strong>
                  </span>
                  {rebalanceMode === 'buyOnly' ? (
                    <span className="ret-muted">
                      Total Sells / Trims: <strong>$0 (Buy-Only)</strong>
                    </span>
                  ) : (
                    <span className="ret-neg">
                      Total Sells / Trims: <strong>{formatSignedUsd(-rebalancePlan.totalSellUsd)}</strong>
                    </span>
                  )}
                  <span>
                    {rebalanceMode === 'buyOnly' ? 'Fresh Cash Deployed:' : 'Net Cash Needed:'}{' '}
                    <strong>{formatSignedUsd(rebalancePlan.cashDeltaUsd)}</strong>
                  </span>
                </div>
              )}
            </div>

            <div>
              {rebalancePlan ? (
                <div className="table-scroll" style={{ maxHeight: '360px', overflowY: 'auto' }}>
                  <table className="data-table compact">
                    <thead>
                      <tr>
                        <th>Action</th>
                        <th>Stock</th>
                        <th>Action Verdict</th>
                        <th className="num">Current ($)</th>
                        <th className="num">Target ($)</th>
                        <th className="num">Trade Delta ($)</th>
                        <th className="num">Est. Shares</th>
                      </tr>
                    </thead>
                    <tbody>
                      {rebalancePlan.rows.map((r) => {
                        const rowAct = getTickerActionabilityForRebalance(r.ticker);
                        const isBuyCaution =
                          (r.action === 'BUY' || r.action === 'ADD') &&
                          rowAct != null &&
                          (rowAct.verdict === 'DISTRIBUTION WARNING' ||
                            rowAct.verdict === 'EXIT SIGNAL');
                        return (
                          <tr key={`reb-${r.ticker}`}>
                            <td>
                              <span
                                className={`chip ${
                                  r.action === 'BUY' || r.action === 'ADD'
                                    ? 'pos'
                                    : r.action === 'TRIM' || r.action === 'EXIT'
                                      ? 'neg'
                                      : ''
                                }`}
                              >
                                {r.action}
                              </span>
                            </td>
                            <td>
                              {stocksIndex[r.ticker] ? (
                                <button
                                  type="button"
                                  className="ticker-badge ticker-link-btn"
                                  onClick={() => onSelectStock(r.ticker)}
                                >
                                  {r.ticker}
                                </button>
                              ) : (
                                <span className="ticker-badge">{r.ticker}</span>
                              )}
                            </td>
                            <td style={{ minWidth: '160px' }}>
                              {rowAct ? (
                                <div>
                                  <div style={{ display: 'flex', alignItems: 'center', gap: '0.28rem', flexWrap: 'wrap' }}>
                                    <span
                                      className={`chip ${actionVerdictChipClass(rowAct.verdict)}`}
                                      title={rowAct.summary}
                                    >
                                      {rowAct.verdict}
                                    </span>
                                    <span className="mono" style={{ fontSize: '0.72rem', fontWeight: 700 }}>
                                      {rowAct.score.toFixed(0)}/100
                                    </span>
                                  </div>
                                  {isBuyCaution && (
                                    <div
                                      className="mono"
                                      style={{
                                        display: 'inline-flex',
                                        alignItems: 'center',
                                        gap: '0.25rem',
                                        marginTop: '0.18rem',
                                        fontSize: '0.67rem',
                                        fontWeight: 600,
                                        color: 'var(--neg-text)',
                                      }}
                                      title={rowAct.summary}
                                    >
                                      <AlertTriangle size={11} /> Caution: distributing signal
                                    </div>
                                  )}
                                </div>
                              ) : (
                                <span className="ret-muted">—</span>
                              )}
                            </td>
                            <td className="num mono">
                              {formatUsdCompact(r.currUsd)} ({r.currPct.toFixed(1)}%)
                            </td>
                            <td className="num mono">
                              {formatUsdCompact(r.targetUsd)} ({r.targetPct.toFixed(1)}%)
                            </td>
                            <td className={`num mono ${returnClass(r.deltaUsd)}`}>
                              {formatSignedUsd(r.deltaUsd)}
                            </td>
                            <td className={`num mono ${returnClass(r.estSharesDelta)}`}>
                              {r.estSharesDelta != null
                                ? `${r.estSharesDelta > 0 ? '+' : ''}${r.estSharesDelta.toLocaleString()} sh`
                                : '—'}
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              ) : (
                <div style={{ padding: '2rem 1rem', textAlign: 'center', color: 'var(--text-muted)', fontSize: '0.84rem' }}>
                  Enter tickers and amounts on the left or click <strong>Load Sample Portfolio</strong> to preview exact rebalance orders.
                </div>
              )}
            </div>
          </div>
        </div>
      )}

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
          <div className="metric-label">Backtest 3Y / Risk Ratios</div>
          <div className={`metric-value ${returnClass(bt.returns['3Y'])}`}>
            {formatSignedPct(bt.returns['3Y'])}
          </div>
          <div className="metric-sub mono">
            CAGR: {formatSignedPct(bt.cagrPct)} · Sortino: {bt.sortinoRatio ?? '—'} · IR: {bt.infoRatioVsQqq ?? '—'} · DD: {formatSignedPct(bt.maxDrawdownPct)}
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
          <div className="metric-sub">Calculates exact dollar &amp; share allocation</div>
        </div>
      </div>

      {/* Backtest Curve + Sub-Sector Exposure */}
      <div className="split-grid">
        <div className="panel" style={{ marginBottom: 0 }}>
          <div className="panel-header">
            <div>
              <h3 className="panel-title">Out-of-Sample Quarterly Rebalance Backtest</h3>
              <p className="panel-subtitle">
                Survivorship-free baseline simulation (Default 40-stock 8%-capped Elite-50 model vs. QQQ &amp; SPY)
              </p>
              <div style={{ display: 'flex', alignItems: 'flex-start', gap: '0.35rem', marginTop: '0.3rem', fontSize: '0.73rem', color: 'var(--text-muted)' }}>
                <Info size={13} style={{ flexShrink: 0, marginTop: '2px' }} />
                <span>
                  Curve tracks the canonical 40-stock 8%-capped Elite-50 baseline; builder filters customize today&apos;s live allocation weights.
                </span>
              </div>
            </div>
            <div className="segmented-group">
              <button
                type="button"
                className={`seg-btn ${mode === 'filing_lag' ? 'active' : ''}`}
                onClick={() => onModeChange('filing_lag')}
              >
                45-Day Lag
              </button>
              <button
                type="button"
                className={`seg-btn ${mode === 'quarter_end' ? 'active' : ''}`}
                onClick={() => onModeChange('quarter_end')}
              >
                Quarter-End
              </button>
            </div>
          </div>
          <div style={{ padding: '1rem 1.2rem' }}>
            <EquityCurveChart
              defaultWindow="3Y"
              height={230}
              series={[
                {
                  id: 'ideal-lag',
                  label: 'Ideal Portfolio (45d Lag)',
                  color: 'var(--chart-teal)',
                  points: idealPortfolio.backtest.filing_lag.curve,
                },
                {
                  id: 'ideal-qe',
                  label: 'Ideal Portfolio (QE)',
                  color: 'var(--accent-primary)',
                  dashed: true,
                  points: idealPortfolio.backtest.quarter_end.curve,
                },
                {
                  id: 'qqq',
                  label: 'QQQ',
                  color: 'var(--chart-indigo)',
                  dashed: true,
                  points: benchmarks.QQQ.curve,
                },
                {
                  id: 'spy',
                  label: 'SPY',
                  color: 'var(--chart-gray)',
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
              <h3 className="panel-title">Sub-Sector Allocation &amp; Rules</h3>
              <p className="panel-subtitle">Weights sum strictly to 100.00%</p>
            </div>
          </div>
          <div style={{ padding: '1rem 1.2rem' }}>
            <SectorBreakdownBar sectors={dynamicSectors} />
            <div style={{ marginTop: '1.15rem', borderTop: '1px solid var(--border-subtle)', paddingTop: '0.9rem' }}>
              <div style={{ fontSize: '0.78rem', fontWeight: 600, color: 'var(--text-primary)', marginBottom: '0.5rem' }}>
                Active Construction Rules
              </div>
              <ul style={{ margin: 0, paddingLeft: '1.1rem', fontSize: '0.8rem', color: 'var(--text-secondary)', lineHeight: 1.6 }}>
                <li><strong>Source Cohort:</strong> Elite {idealPortfolio.rankedFundsCount} Skill-Ranked Concentrated Tech Managers.</li>
                <li><strong>Scoring Lens:</strong> {signalMode === 'rawScore' ? 'Core Compounder (multi-horizon conviction)' : 'Fresh Inflection (+QoQ initiations/adds)'}.</li>
                <li><strong>Universe:</strong> {universeFilter === 'ALL' ? 'All-Cap Tech & TMT' : universeFilter === 'EX_MEGA' ? 'Excluding Mega-Caps >$200B' : 'Emerging Tech <$30B'}.</li>
                <li><strong>Risk Caps:</strong> {capPct}% max single stock{sectorCapPct ? ` · ${sectorCapPct}% max sub-sector` : ' · No sub-sector cap'}.</li>
              </ul>
            </div>
          </div>
        </div>
      </div>

      {/* Ideal Portfolio Holdings Table */}
      <div className="panel">
        <div className="panel-header">
          <div>
            <h3 className="panel-title">
              Ideal Portfolio Allocation ({displayedPositions.length} of {activePositions.length} Positions · 100.00%)
            </h3>
            <p className="panel-subtitle">
              Click any stock ticker for full Q-VWAP &amp; XBRL Stock Dossier, or click any backing fund to inspect its 13F book
            </p>
          </div>
          <div className="controls-row" style={{ flexWrap: 'wrap' }}>
            <div className="segmented-group" role="group" aria-label="Filter by Action Verdict">
              <button
                type="button"
                className={`seg-btn ${verdictFilter === 'ALL' ? 'active' : ''}`}
                onClick={() => setVerdictFilter('ALL')}
              >
                All Verdicts
              </button>
              {VERDICT_ORDER.filter((vd) => verdictCounts[vd] > 0).map((vd) => (
                <button
                  key={vd}
                  type="button"
                  className={`seg-btn ${verdictFilter === vd ? 'active' : ''}`}
                  onClick={() => setVerdictFilter(vd)}
                >
                  {actionVerdictShortLabel(vd)} ({verdictCounts[vd]})
                </button>
              ))}
            </div>
            <div className="segmented-group" role="group" aria-label="Entry zone filter">
              {(
                [
                  { id: 'ALL', label: 'All Zones' },
                  { id: 'VALUE', label: 'Discount / Near Q-Avg' },
                  { id: 'EXTENDED', label: 'Extended' },
                ] as const
              ).map((z) => (
                <button
                  key={z.id}
                  type="button"
                  className={`seg-btn ${entryFilter === z.id ? 'active' : ''}`}
                  onClick={() => setEntryFilter(z.id)}
                >
                  {z.label}
                </button>
              ))}
            </div>
            <button type="button" className="action-btn" onClick={handleExport}>
              <Download size={14} /> Export Ideal Portfolio CSV
            </button>
          </div>
        </div>

        <div className="table-scroll">
          <table className="data-table">
            <thead>
              <tr>
                <th>#</th>
                <th>Stock</th>
                <th className="sortable" onClick={() => toggleSort('actionScore')}>
                  Action Verdict {sortBy === 'actionScore' ? (sortAsc ? '▲' : '▼') : ''}
                </th>
                <th>Sub-Sector</th>
                <th className="num sortable" onClick={() => toggleSort('idealWeightPct')}>
                  Ideal Weight {sortBy === 'idealWeightPct' ? (sortAsc ? '▲' : '▼') : ''}
                </th>
                <th className="num">Target ($)</th>
                <th>Entry Zone (Q-VWAP)</th>
                <th className="num sortable" onClick={() => toggleSort('revYoY')}>
                  XBRL Rev / OpM {sortBy === 'revYoY' ? (sortAsc ? '▲' : '▼') : ''}
                </th>
                <th className="num sortable" onClick={() => toggleSort('fcfMargin')}>
                  FCF Mgn {sortBy === 'fcfMargin' ? (sortAsc ? '▲' : '▼') : ''}
                </th>
                <th className="num sortable" onClick={() => toggleSort('convictionScore')}>
                  Conviction {sortBy === 'convictionScore' ? (sortAsc ? '▲' : '▼') : ''}
                </th>
                <th>Signal Driver</th>
                <th className="num sortable" onClick={() => toggleSort('holderCount')}>
                  Holders {sortBy === 'holderCount' ? (sortAsc ? '▲' : '▼') : ''}
                </th>
                <th className="num sortable" onClick={() => toggleSort('stockTtmPct')}>
                  TTM {sortBy === 'stockTtmPct' ? (sortAsc ? '▲' : '▼') : ''}
                </th>
                <th>Top Backing Funds</th>
              </tr>
            </thead>
            <tbody>
              {displayedPositions.map((p) => {
                const targetUsd = (capitalUsd * p.dynamicWeightPct) / 100;
                const estShares =
                  p.priceUsd && p.priceUsd > 0 ? Math.floor(targetUsd / p.priceUsd) : null;
                const ad = resolveAlphaDecayMetrics(p, { latestQuarterLabel });
                const act = getPositionActionability(p);
                return (
                  <tr key={p.ticker}>
                    <td>
                      <span className={`rank-badge ${p.rank <= 3 ? 'top3' : ''}`}>#{p.rank}</span>
                    </td>
                    <td style={{ minWidth: '185px' }}>
                      <div style={{ display: 'flex', alignItems: 'center', gap: '0.45rem' }}>
                        {stocksIndex[p.ticker] ? (
                          <button
                            type="button"
                            className="ticker-badge ticker-link-btn"
                            onClick={() => onSelectStock(p.ticker)}
                            title={`Open ${p.ticker} Stock Dossier`}
                          >
                            {p.ticker}
                          </button>
                        ) : (
                          <span className="ticker-badge">{p.ticker}</span>
                        )}
                        <span style={{ fontWeight: 600 }}>{p.name}</span>
                      </div>
                      <div className="mono" style={{ fontSize: '0.7rem', color: 'var(--text-muted)', marginTop: '0.12rem' }}>
                        {p.priceUsd ? `$${p.priceUsd.toFixed(2)}` : '—'}
                        {p.marketCapTier ? ` · ${p.marketCapTier}` : ''} · Avg wt {p.avgFundWeightPct.toFixed(1)}%
                      </div>
                      {renderConvergenceBadges(p.ticker)}
                    </td>
                    <td style={{ minWidth: '162px' }}>
                      <div style={{ display: 'flex', alignItems: 'center', gap: '0.32rem', flexWrap: 'wrap' }}>
                        <span
                          className={`chip ${actionVerdictChipClass(act.verdict)}`}
                          title={act.summary}
                        >
                          {act.verdict}
                        </span>
                        <span className="mono" style={{ fontWeight: 700, fontSize: '0.78rem' }}>
                          {act.score.toFixed(0)}/100
                        </span>
                      </div>
                      <div className="mono" style={{ fontSize: '0.67rem', color: 'var(--text-muted)', marginTop: '0.14rem' }}>
                        F:{act.pillars.flowImpulse.toFixed(0)} · P:{act.pillars.priceDiscipline.toFixed(0)} · Q:{act.pillars.xbrlQuality.toFixed(0)} · T:{act.pillars.thesisPersistence.toFixed(0)}
                      </div>
                    </td>
                    <td>
                      <span className="chip">{p.subSector || p.sector}</span>
                    </td>
                    <td className="num">
                      <div className="weight-cell">
                        <span className="mono" style={{ fontWeight: 700, color: 'var(--accent-primary)', fontSize: '0.92rem' }}>
                          {formatPct(p.dynamicWeightPct, 2)}
                        </span>
                        <div className="bar-track" style={{ width: '72px' }}>
                          <div
                            className="bar-fill pos"
                            style={{ width: `${Math.min(100, (p.dynamicWeightPct / capPct) * 100)}%` }}
                          />
                        </div>
                        {!isDefault && p.weightVsDefaultPct != null && Math.abs(p.weightVsDefaultPct) >= 0.05 && (
                          <span
                            className={`mono ${returnClass(p.weightVsDefaultPct)}`}
                            style={{ fontSize: '0.67rem' }}
                          >
                            {p.weightVsDefaultPct > 0 ? '+' : ''}
                            {p.weightVsDefaultPct.toFixed(1)} vs base
                          </span>
                        )}
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
                    <td style={{ minWidth: '170px' }}>
                      {p.entryZoneStatus || ad.status !== 'NEUTRAL' ? (
                        <div>
                          <div style={{ display: 'flex', gap: '0.28rem', flexWrap: 'wrap' }}>
                            {p.entryZoneStatus && (
                              <span className={`chip ${entryZoneChipClass(p.entryZoneStatus)}`}>
                                {p.entryZoneStatus}
                              </span>
                            )}
                            {ad.status !== 'NEUTRAL' && (
                              <span
                                className={`chip ${alphaDecayChipClass(ad.status)}`}
                                title={ad.reason}
                              >
                                {alphaDecayLabel(ad.status)}
                              </span>
                            )}
                          </div>
                          <div className="mono" style={{ fontSize: '0.68rem', color: 'var(--text-muted)', marginTop: '0.12rem' }}>
                            {p.pctVsQuarterVwap != null ? `${formatSignedPct(p.pctVsQuarterVwap)} vs ` : ''}
                            {p.estQuarterVwap != null ? `$${p.estQuarterVwap.toFixed(2)}` : ''}
                          </div>
                          {(ad.filingToTodayPct != null || ad.avgEliteHoldingQuarters != null) && (
                            <div className="mono" style={{ fontSize: '0.66rem', color: 'var(--text-muted)' }}>
                              {ad.filingToTodayPct != null ? `Fil→Now ${formatSignedPct(ad.filingToTodayPct)}` : ''}
                              {ad.filingToTodayPct != null && ad.avgEliteHoldingQuarters != null ? ' · ' : ''}
                              {ad.avgEliteHoldingQuarters != null ? `Hold ${ad.avgEliteHoldingQuarters.toFixed(1)}Q` : ''}
                            </div>
                          )}
                        </div>
                      ) : (
                        <span className="ret-muted">—</span>
                      )}
                    </td>
                    <td className="num mono" style={{ fontSize: '0.78rem' }}>
                      {p.fundamentals &&
                      (p.fundamentals.revenueGrowthYoYPct != null ||
                        p.fundamentals.operatingMarginPct != null) ? (
                        <div>
                          <div className={returnClass(p.fundamentals.revenueGrowthYoYPct)}>
                            {p.fundamentals.revenueGrowthYoYPct != null
                              ? `${formatSignedPct(p.fundamentals.revenueGrowthYoYPct)} rev`
                              : '—'}
                          </div>
                          <div style={{ fontSize: '0.68rem', color: 'var(--text-muted)' }}>
                            {p.fundamentals.operatingMarginPct != null
                              ? `${p.fundamentals.operatingMarginPct.toFixed(1)}% OpM`
                              : ''}
                          </div>
                        </div>
                      ) : (
                        <span className="ret-muted">—</span>
                      )}
                    </td>
                    <td className={`num mono ${returnClass(p.fundamentals?.fcfMarginPct)}`} style={{ fontSize: '0.79rem' }}>
                      {p.fundamentals?.fcfMarginPct != null
                        ? `${p.fundamentals.fcfMarginPct.toFixed(1)}%`
                        : '—'}
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
                      <div><strong>{p.holderCount}</strong> Elite</div>
                      {(p.netNew > 0 || p.netAdd > 0) && (
                        <div style={{ fontSize: '0.68rem', color: 'var(--pos-text)' }}>
                          +{p.netNew}N / +{p.netAdd}A
                        </div>
                      )}
                    </td>
                    <td className={`num mono ${returnClass(p.stockTtmPct)}`}>
                      {formatSignedPct(p.stockTtmPct, 1)}
                    </td>
                    <td style={{ minWidth: '250px' }}>
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
              {displayedPositions.length === 0 && (
                <tr>
                  <td colSpan={14} style={{ textAlign: 'center', padding: '2rem', color: 'var(--text-muted)' }}>
                    {activePositions.length === 0
                      ? 'No stocks match this market-cap filter. Switch Universe back to All-Cap Tech or Ex-Mega.'
                      : 'No positions match active Entry Zone or Action Verdict filters.'}
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
