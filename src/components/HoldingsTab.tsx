import { Fragment, useMemo, useState, type FC } from 'react';
import {
  ChevronDown,
  ChevronRight,
  Download,
  Search,
  ShieldCheck,
  Sparkles,
  TrendingDown,
  Zap,
} from 'lucide-react';
import type {
  ActionVerdict,
  ConsensusHolding,
  EntryZoneStatus,
  FundSummary,
  IdealPortfolioData,
  MarketCapTier,
  ReturnMode,
  SignalScreenerData,
  SignalScreenerEntry,
  StockIndexEntry,
  WeightMode,
} from '../types/data';
import { resolveAlphaDecayMetrics } from '../utils/alphaDecay';
import {
  buildConvictionLookupMap,
  buildFundSkillMap,
  buildUnifiedConvictionList,
  resolveActionabilityMatrix,
  type UnifiedConvictionRow,
} from '../utils/convictionMatrix';
import {
  actionVerdictChipClass,
  actionVerdictShortLabel,
  alphaDecayChipClass,
  alphaDecayLabel,
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
  funds?: FundSummary[];
  idealPortfolio?: IdealPortfolioData;
  mode: ReturnMode;
  onModeChange: (m: ReturnMode) => void;
  onSelectFund: (cik: string) => void;
  onSelectStock: (ticker: string) => void;
}

type ScreenerSubTab =
  | 'MATRIX'
  | 'CONSENSUS'
  | 'CLUSTER'
  | 'HEAVY_ADDS'
  | 'EXITS'
  | 'CONTRARIAN';
type CapFilter = 'ALL' | MarketCapTier;
type EntryFilter = 'ALL' | 'VALUE' | 'EXTENDED' | 'CHASING';
type AlphaFilter = 'ALL' | 'LAG_ADVANTAGE' | 'FOMO_TRAP';
type VerdictFilter = 'ALL' | ActionVerdict;
type MatrixSortKey =
  | 'actionScore'
  | 'flowPillar'
  | 'pricePillar'
  | 'xbrlPillar'
  | 'persistPillar'
  | 'netFlow'
  | 'vsVwap'
  | 'holders'
  | 'ttm';
type ConsensusSortKey = 'aggregate' | 'actionScore' | 'holders' | 'totalValue' | 'ttm' | 'vsVwap';
type ScreenerSortKey =
  | 'default'
  | 'actionScore'
  | 'netFlow'
  | 'holders'
  | 'ttm'
  | 'revYoY'
  | 'vsVwap';

const VERDICT_ORDER: ActionVerdict[] = [
  'STRONG ACCUMULATE',
  'ACCUMULATE ON DIP',
  'HOLD',
  'DISTRIBUTION WARNING',
  'EXIT SIGNAL',
];

function miniPillarTone(val: number): string {
  if (val >= 17) return 'pos';
  if (val >= 12) return '';
  if (val >= 8) return 'warn';
  return 'neg';
}

function compareNullableNumbers(
  a: number | null | undefined,
  b: number | null | undefined,
  asc: boolean
): number {
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
  funds,
  idealPortfolio,
  mode,
  onModeChange,
  onSelectFund,
  onSelectStock,
}) => {
  const [subTab, setSubTab] = useState<ScreenerSubTab>('MATRIX');
  const [weightMode, setWeightMode] = useState<WeightMode>('equal');
  const [matrixSortBy, setMatrixSortBy] = useState<MatrixSortKey>('actionScore');
  const [matrixSortAsc, setMatrixSortAsc] = useState(false);
  const [sortBy, setSortBy] = useState<ConsensusSortKey>('aggregate');
  const [sortAsc, setSortAsc] = useState(false);
  const [screenerSortBy, setScreenerSortBy] = useState<ScreenerSortKey>('default');
  const [screenerSortAsc, setScreenerSortAsc] = useState(false);
  const [capFilter, setCapFilter] = useState<CapFilter>('ALL');
  const [entryFilter, setEntryFilter] = useState<EntryFilter>('ALL');
  const [alphaFilter, setAlphaFilter] = useState<AlphaFilter>('ALL');
  const [verdictFilter, setVerdictFilter] = useState<VerdictFilter>('ALL');
  const [search, setSearch] = useState('');
  const [expandedTicker, setExpandedTicker] = useState<string | null>(null);

  const quarterLabel = signalScreener.quarterLabel;
  const consensusList = useMemo(() => topHoldings[mode] ?? [], [topHoldings, mode]);
  const skillMap = useMemo(() => buildFundSkillMap(funds, mode), [funds, mode]);

  const unifiedList = useMemo<UnifiedConvictionRow[]>(
    () =>
      buildUnifiedConvictionList({
        topHoldings,
        signalScreener,
        idealPortfolio,
        stocksIndex,
        funds,
        mode,
      }),
    [topHoldings, signalScreener, idealPortfolio, stocksIndex, funds, mode]
  );

  const unifiedByTicker = useMemo(
    () => buildConvictionLookupMap(unifiedList),
    [unifiedList]
  );

  const getConsensusActionability = (item: ConsensusHolding) =>
    unifiedByTicker[item.ticker]?.actionability ??
    resolveActionabilityMatrix(item, {
      fundSkillByCik: skillMap,
      latestQuarterLabel: quarterLabel,
      isCoreConsensus: true,
    });

  const getScreenerActionability = (item: SignalScreenerEntry) =>
    unifiedByTicker[item.ticker]?.actionability ??
    resolveActionabilityMatrix(item, {
      fundSkillByCik: skillMap,
      latestQuarterLabel: quarterLabel,
      isCoreConsensus: item.eliteHoldersCount >= 5,
    });

  const rawScreenerPool = useMemo<SignalScreenerEntry[]>(() => {
    return subTab === 'CLUSTER'
      ? signalScreener.clusterInitiations
      : subTab === 'HEAVY_ADDS'
        ? signalScreener.heavyAdds
        : subTab === 'EXITS'
          ? signalScreener.smartMoneyExits
          : subTab === 'CONTRARIAN'
            ? signalScreener.contrarianBets
            : [];
  }, [subTab, signalScreener]);

  const alphaCounts = useMemo(() => {
    let lag = 0;
    let fomo = 0;
    if (subTab === 'MATRIX') {
      for (const item of unifiedList) {
        if (item.alphaDecayStatus === 'LAG_ADVANTAGE') lag += 1;
        else if (item.alphaDecayStatus === 'FOMO_TRAP') fomo += 1;
      }
      return { lag, fomo };
    }
    const source = subTab === 'CONSENSUS' ? consensusList : rawScreenerPool;
    for (const item of source) {
      const merged = unifiedByTicker[item.ticker];
      const st = merged
        ? merged.alphaDecayStatus
        : resolveAlphaDecayMetrics(item, { latestQuarterLabel: quarterLabel }).status;
      if (st === 'LAG_ADVANTAGE') lag += 1;
      else if (st === 'FOMO_TRAP') fomo += 1;
    }
    return { lag, fomo };
  }, [subTab, unifiedList, consensusList, rawScreenerPool, unifiedByTicker, quarterLabel]);

  const verdictCounts = useMemo(() => {
    const counts: Record<ActionVerdict, number> = {
      'STRONG ACCUMULATE': 0,
      'ACCUMULATE ON DIP': 0,
      HOLD: 0,
      'DISTRIBUTION WARNING': 0,
      'EXIT SIGNAL': 0,
    };
    if (subTab === 'MATRIX') {
      for (const item of unifiedList) counts[item.actionVerdict] += 1;
    } else if (subTab === 'CONSENSUS') {
      for (const item of consensusList) {
        const v = (
          unifiedByTicker[item.ticker]?.actionability ??
          resolveActionabilityMatrix(item, {
            fundSkillByCik: skillMap,
            latestQuarterLabel: quarterLabel,
            isCoreConsensus: true,
          })
        ).verdict;
        counts[v] += 1;
      }
    } else {
      for (const item of rawScreenerPool) {
        const v = (
          unifiedByTicker[item.ticker]?.actionability ??
          resolveActionabilityMatrix(item, {
            fundSkillByCik: skillMap,
            latestQuarterLabel: quarterLabel,
            isCoreConsensus: item.eliteHoldersCount >= 5,
          })
        ).verdict;
        counts[v] += 1;
      }
    }
    return counts;
  }, [subTab, unifiedList, consensusList, rawScreenerPool, unifiedByTicker, skillMap, quarterLabel]);

  const filteredMatrix = useMemo<UnifiedConvictionRow[]>(() => {
    const q = search.trim().toLowerCase();
    return unifiedList
      .filter((item) => {
        if (!matchesCapFilter(item.marketCapTier, capFilter)) return false;
        if (!matchesEntryFilter(item.entryZoneStatus, entryFilter)) return false;
        if (alphaFilter !== 'ALL' && item.alphaDecayStatus !== alphaFilter) return false;
        if (verdictFilter !== 'ALL' && item.actionVerdict !== verdictFilter) return false;
        if (!q) return true;
        return (
          item.ticker.toLowerCase().includes(q) ||
          item.name.toLowerCase().includes(q) ||
          item.sector.toLowerCase().includes(q) ||
          item.subSector.toLowerCase().includes(q) ||
          item.topHoldersPreview.some((h) => h.fundName.toLowerCase().includes(q))
        );
      })
      .sort((a, b) => {
        if (matrixSortBy === 'flowPillar') {
          return compareNullableNumbers(
            a.actionability.pillars.flowImpulse,
            b.actionability.pillars.flowImpulse,
            matrixSortAsc
          );
        }
        if (matrixSortBy === 'pricePillar') {
          return compareNullableNumbers(
            a.actionability.pillars.priceDiscipline,
            b.actionability.pillars.priceDiscipline,
            matrixSortAsc
          );
        }
        if (matrixSortBy === 'xbrlPillar') {
          return compareNullableNumbers(
            a.actionability.pillars.xbrlQuality,
            b.actionability.pillars.xbrlQuality,
            matrixSortAsc
          );
        }
        if (matrixSortBy === 'persistPillar') {
          return compareNullableNumbers(
            a.actionability.pillars.thesisPersistence,
            b.actionability.pillars.thesisPersistence,
            matrixSortAsc
          );
        }
        if (matrixSortBy === 'netFlow') {
          return compareNullableNumbers(a.netFlowUsd, b.netFlowUsd, matrixSortAsc);
        }
        if (matrixSortBy === 'vsVwap') {
          return compareNullableNumbers(a.pctVsQuarterVwap, b.pctVsQuarterVwap, matrixSortAsc);
        }
        if (matrixSortBy === 'holders') {
          return compareNullableNumbers(a.holderCount, b.holderCount, matrixSortAsc);
        }
        if (matrixSortBy === 'ttm') {
          return compareNullableNumbers(a.stockTtmPct, b.stockTtmPct, matrixSortAsc);
        }
        const diff = compareNullableNumbers(a.actionabilityScore, b.actionabilityScore, matrixSortAsc);
        return diff !== 0 ? diff : b.eliteHoldersCount - a.eliteHoldersCount;
      });
  }, [
    unifiedList,
    capFilter,
    entryFilter,
    alphaFilter,
    verdictFilter,
    search,
    matrixSortBy,
    matrixSortAsc,
  ]);

  const filteredConsensus = useMemo(() => {
    const q = search.trim().toLowerCase();
    return [...consensusList]
      .filter((item) => {
        if (!matchesCapFilter(item.marketCapTier, capFilter)) return false;
        if (!matchesEntryFilter(item.entryZoneStatus, entryFilter)) return false;
        const merged = unifiedByTicker[item.ticker];
        if (alphaFilter !== 'ALL') {
          const st = merged
            ? merged.alphaDecayStatus
            : resolveAlphaDecayMetrics(item, { latestQuarterLabel: quarterLabel }).status;
          if (st !== alphaFilter) return false;
        }
        if (verdictFilter !== 'ALL') {
          const act =
            merged?.actionability ??
            resolveActionabilityMatrix(item, {
              fundSkillByCik: skillMap,
              latestQuarterLabel: quarterLabel,
              isCoreConsensus: true,
            });
          if (act.verdict !== verdictFilter) return false;
        }
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
        if (sortBy === 'actionScore') {
          const sa = (
            unifiedByTicker[a.ticker]?.actionability ??
            resolveActionabilityMatrix(a, {
              fundSkillByCik: skillMap,
              latestQuarterLabel: quarterLabel,
              isCoreConsensus: true,
            })
          ).score;
          const sb = (
            unifiedByTicker[b.ticker]?.actionability ??
            resolveActionabilityMatrix(b, {
              fundSkillByCik: skillMap,
              latestQuarterLabel: quarterLabel,
              isCoreConsensus: true,
            })
          ).score;
          return compareNullableNumbers(sa, sb, sortAsc);
        }
        if (sortBy === 'holders') return compareNullableNumbers(a.holderCount, b.holderCount, sortAsc);
        if (sortBy === 'totalValue') return compareNullableNumbers(a.totalValueUsd, b.totalValueUsd, sortAsc);
        if (sortBy === 'ttm') return compareNullableNumbers(a.stockTtmPct, b.stockTtmPct, sortAsc);
        if (sortBy === 'vsVwap') return compareNullableNumbers(a.pctVsQuarterVwap, b.pctVsQuarterVwap, sortAsc);
        const wa = weightMode === 'equal' ? a.equalAggregateWeightPct : a.aumAggregateWeightPct;
        const wb = weightMode === 'equal' ? b.equalAggregateWeightPct : b.aumAggregateWeightPct;
        return compareNullableNumbers(wa, wb, sortAsc);
      });
  }, [
    consensusList,
    capFilter,
    entryFilter,
    alphaFilter,
    verdictFilter,
    unifiedByTicker,
    skillMap,
    quarterLabel,
    search,
    sortBy,
    sortAsc,
    weightMode,
  ]);

  const activeScreenerList = useMemo<SignalScreenerEntry[]>(() => {
    const q = search.trim().toLowerCase();
    const filtered = rawScreenerPool.filter((item) => {
      if (!matchesCapFilter(item.marketCapTier, capFilter)) return false;
      if (!matchesEntryFilter(item.entryZoneStatus, entryFilter)) return false;
      const merged = unifiedByTicker[item.ticker];
      if (alphaFilter !== 'ALL') {
        const st = merged
          ? merged.alphaDecayStatus
          : resolveAlphaDecayMetrics(item, { latestQuarterLabel: quarterLabel }).status;
        if (st !== alphaFilter) return false;
      }
      if (verdictFilter !== 'ALL') {
        const act =
          merged?.actionability ??
          resolveActionabilityMatrix(item, {
            fundSkillByCik: skillMap,
            latestQuarterLabel: quarterLabel,
            isCoreConsensus: item.eliteHoldersCount >= 5,
          });
        if (act.verdict !== verdictFilter) return false;
      }
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
      if (screenerSortBy === 'actionScore') {
        const sa = (
          unifiedByTicker[a.ticker]?.actionability ??
          resolveActionabilityMatrix(a, {
            fundSkillByCik: skillMap,
            latestQuarterLabel: quarterLabel,
            isCoreConsensus: a.eliteHoldersCount >= 5,
          })
        ).score;
        const sb = (
          unifiedByTicker[b.ticker]?.actionability ??
          resolveActionabilityMatrix(b, {
            fundSkillByCik: skillMap,
            latestQuarterLabel: quarterLabel,
            isCoreConsensus: b.eliteHoldersCount >= 5,
          })
        ).score;
        return compareNullableNumbers(sa, sb, screenerSortAsc);
      }
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
      if (screenerSortBy === 'vsVwap') {
        return compareNullableNumbers(a.pctVsQuarterVwap, b.pctVsQuarterVwap, screenerSortAsc);
      }
      return compareNullableNumbers(
        a.fundamentals?.revenueGrowthYoYPct,
        b.fundamentals?.revenueGrowthYoYPct,
        screenerSortAsc
      );
    });
  }, [
    rawScreenerPool,
    capFilter,
    entryFilter,
    alphaFilter,
    verdictFilter,
    unifiedByTicker,
    skillMap,
    quarterLabel,
    search,
    screenerSortBy,
    screenerSortAsc,
  ]);

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
  const matrixVerdictTotals = useMemo(() => {
    let strong = 0;
    let dip = 0;
    let warnExit = 0;
    for (const r of unifiedList) {
      if (r.actionVerdict === 'STRONG ACCUMULATE') strong += 1;
      else if (r.actionVerdict === 'ACCUMULATE ON DIP') dip += 1;
      else if (r.actionVerdict === 'DISTRIBUTION WARNING' || r.actionVerdict === 'EXIT SIGNAL') {
        warnExit += 1;
      }
    }
    return { strong, dip, warnExit, topStock: unifiedList[0] ?? null };
  }, [unifiedList]);

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
    if (subTab === 'MATRIX') {
      downloadCsv(
        `paper-trail-conviction-matrix-${mode}.csv`,
        [
          'Rank',
          'Ticker',
          'Company',
          'Sub-Sector',
          'Market Cap Tier',
          'Action Verdict',
          'Actionability Score (/100)',
          'Pillar 1 Flow Impulse (/25)',
          'Pillar 2 Price Discipline (/25)',
          'Pillar 3 XBRL Quality (/25)',
          'Pillar 4 Thesis Persistence (/25)',
          'Signals / Sources',
          'Price (USD)',
          'Est Q-VWAP (USD)',
          'vs Q-VWAP (%)',
          'Entry Zone',
          'Alpha-Decay Status',
          'Filing->Today (%)',
          'Avg Elite Hold (Q)',
          'XBRL Rev YoY (%)',
          'XBRL FCF Margin (%)',
          'XBRL Op Margin (%)',
          'Share Dilution YoY (%)',
          'Holders',
          'Elite 50 Holders',
          'New Buys',
          'Added',
          'Trimmed',
          'Exited',
          'Net Flow (USD)',
          'TTM Return (%)',
        ],
        filteredMatrix.map((item, idx) => [
          idx + 1,
          item.ticker,
          item.name,
          item.subSector || item.sector,
          item.marketCapTier ?? '',
          item.actionVerdict,
          item.actionabilityScore,
          item.actionability.pillars.flowImpulse,
          item.actionability.pillars.priceDiscipline,
          item.actionability.pillars.xbrlQuality,
          item.actionability.pillars.thesisPersistence,
          item.sourceTags.join(' | '),
          item.priceUsd ?? '',
          item.estQuarterVwap ?? '',
          item.pctVsQuarterVwap ?? '',
          item.entryZoneStatus ?? '',
          alphaDecayLabel(item.alphaDecayStatus),
          item.filingToTodayPct ?? '',
          item.avgEliteHoldingQuarters ?? '',
          item.fundamentals?.revenueGrowthYoYPct ?? '',
          item.fundamentals?.fcfMarginPct ?? '',
          item.fundamentals?.operatingMarginPct ?? '',
          item.fundamentals?.shareDilutionYoYPct ?? '',
          item.holderCount,
          item.eliteHoldersCount,
          item.newCount,
          item.addedCount,
          item.trimmedCount,
          item.exitedCount,
          item.netFlowUsd ?? '',
          item.stockTtmPct ?? '',
        ])
      );
      return;
    }

    if (subTab === 'CONSENSUS') {
      downloadCsv(
        `paper-trail-top30-consensus-${mode}-${weightMode}.csv`,
        [
          'Rank',
          'Ticker',
          'Company',
          'Action Verdict',
          'Actionability Score (/100)',
          'Sub-Sector',
          'Market Cap Tier',
          'Price (USD)',
          'Est Q-VWAP (USD)',
          'vs Q-VWAP (%)',
          'Entry Zone',
          'Alpha-Decay Status',
          'SEC Filing Date',
          'Filing->Today (%)',
          'Avg Elite Hold (Q)',
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
        filteredConsensus.map((item, idx) => {
          const ad = resolveAlphaDecayMetrics(item, { latestQuarterLabel: quarterLabel });
          const act = getConsensusActionability(item);
          return [
            idx + 1,
            item.ticker,
            item.name,
            act.verdict,
            act.score,
            item.subSector || item.sector,
            item.marketCapTier ?? '',
            item.priceUsd ?? '',
            item.estQuarterVwap ?? '',
            item.pctVsQuarterVwap ?? '',
            item.entryZoneStatus ?? '',
            alphaDecayLabel(ad.status),
            ad.consensusFilingDate ?? '',
            ad.filingToTodayPct ?? '',
            ad.avgEliteHoldingQuarters ?? '',
            item.fundamentals?.revenueGrowthYoYPct ?? '',
            item.fundamentals?.operatingMarginPct ?? '',
            item.holderCount,
            item.totalRankedFundsPool,
            item.equalAggregateWeightPct,
            item.aumAggregateWeightPct,
            item.totalValueUsd,
            item.stockTtmPct ?? '',
            item.holders.map((h) => `${h.fundName} (${h.weightPct.toFixed(1)}%)`).join('; '),
          ];
        })
      );
      return;
    }

    downloadCsv(
      `paper-trail-screener-${subTab.toLowerCase()}.csv`,
      [
        'Rank',
        'Ticker',
        'Company',
        'Action Verdict',
        'Actionability Score (/100)',
        'Sub-Sector',
        'Market Cap Tier',
        'Price (USD)',
        'Est Q-VWAP (USD)',
        'vs Q-VWAP (%)',
        'Entry Zone',
        'Alpha-Decay Status',
        'SEC Filing Date',
        'Filing->Today (%)',
        'Avg Elite Hold (Q)',
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
      activeScreenerList.map((item, idx) => {
        const ad = resolveAlphaDecayMetrics(item, { latestQuarterLabel: quarterLabel });
        const act = getScreenerActionability(item);
        return [
          idx + 1,
          item.ticker,
          item.name,
          act.verdict,
          act.score,
          item.subSector || item.sector,
          item.marketCapTier ?? '',
          item.priceUsd ?? '',
          item.estQuarterVwap ?? '',
          item.pctVsQuarterVwap ?? '',
          item.entryZoneStatus ?? '',
          alphaDecayLabel(ad.status),
          ad.consensusFilingDate ?? '',
          ad.filingToTodayPct ?? '',
          ad.avgEliteHoldingQuarters ?? '',
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
        ];
      })
    );
  };

  const screenerDescription =
    subTab === 'MATRIX'
      ? `Unified 4-pillar Conviction Matrix scoring ${unifiedList.length} tracked stocks (0–100) across Flow Impulse, Q-VWAP Price Discipline, XBRL Quality & Thesis Persistence.`
      : subTab === 'CONSENSUS'
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
          <h2>13F Conviction Matrix &amp; Signal Screener</h2>
          <p>{screenerDescription}</p>
        </div>

        <div className="controls-row">
          <div className="segmented-group" role="group" aria-label="Screener views">
            {(
              [
                { id: 'MATRIX', label: `Conviction Matrix (${unifiedList.length})` },
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
          <div className="metric-label">Conviction Verdicts ({unifiedList.length} Stocks)</div>
          <div className="metric-value mono" style={{ fontSize: '1.15rem', gap: '0.42rem', flexWrap: 'wrap' }}>
            <span className="chip pos">{matrixVerdictTotals.strong} Strong</span>
            <span className="chip tech">{matrixVerdictTotals.dip} Buy Dip</span>
            <span className="chip neg">{matrixVerdictTotals.warnExit} Trim/Exit</span>
          </div>
          <div className="metric-sub">
            {matrixVerdictTotals.topStock
              ? `Top rated: ${matrixVerdictTotals.topStock.ticker} (${matrixVerdictTotals.topStock.actionabilityScore.toFixed(1)}/100)`
              : `Top 30 agg: ${formatPct(totalAggWeight, 1)} (${formatUsdCompact(totalTop30Usd)})`}
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
            <div className="segmented-group" role="group" aria-label="Filter by Action Verdict">
              <button
                type="button"
                id="verdict-filter-all"
                className={`seg-btn ${verdictFilter === 'ALL' ? 'active' : ''}`}
                onClick={() => setVerdictFilter('ALL')}
              >
                All Verdicts
              </button>
              {VERDICT_ORDER.map((vd) => (
                <button
                  key={vd}
                  type="button"
                  id={`verdict-filter-${vd.toLowerCase().replace(/\s+/g, '-')}`}
                  className={`seg-btn ${verdictFilter === vd ? 'active' : ''}`}
                  onClick={() => setVerdictFilter(vd)}
                >
                  {actionVerdictShortLabel(vd)} ({verdictCounts[vd]})
                </button>
              ))}
            </div>

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

            <div className="segmented-group" role="group" aria-label="Filter by Post-13F Alpha-Decay status">
              {(
                [
                  { id: 'ALL', label: 'All Alpha Signals' },
                  { id: 'LAG_ADVANTAGE', label: `Lag Advantage (${alphaCounts.lag})` },
                  { id: 'FOMO_TRAP', label: `FOMO / Late Crowded (${alphaCounts.fomo})` },
                ] as const
              ).map((af) => (
                <button
                  key={af.id}
                  type="button"
                  id={`alpha-filter-${af.id.toLowerCase()}`}
                  className={`seg-btn ${alphaFilter === af.id ? 'active' : ''}`}
                  onClick={() => setAlphaFilter(af.id)}
                >
                  {af.label}
                </button>
              ))}
            </div>
          </div>

          <div className="controls-row">
            {subTab === 'MATRIX' ? (
              <div className="segmented-group" role="group" aria-label="Sort conviction matrix">
                {(
                  [
                    { id: 'actionScore', label: 'Action Score' },
                    { id: 'flowPillar', label: 'Flow' },
                    { id: 'pricePillar', label: 'Price' },
                    { id: 'xbrlPillar', label: 'XBRL' },
                    { id: 'persistPillar', label: 'Persist' },
                    { id: 'netFlow', label: 'Net Flow $' },
                    { id: 'vsVwap', label: 'vs VWAP' },
                    { id: 'holders', label: 'Holders' },
                  ] as const
                ).map((s) => (
                  <button
                    key={s.id}
                    type="button"
                    className={`seg-btn ${matrixSortBy === s.id ? 'active' : ''}`}
                    onClick={() => {
                      if (matrixSortBy === s.id) setMatrixSortAsc((a) => !a);
                      else {
                        setMatrixSortBy(s.id);
                        setMatrixSortAsc(s.id === 'vsVwap');
                      }
                    }}
                  >
                    {s.label}
                    {matrixSortBy === s.id ? (matrixSortAsc ? ' ▲' : ' ▼') : ''}
                  </button>
                ))}
              </div>
            ) : subTab === 'CONSENSUS' ? (
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
                      { id: 'actionScore', label: 'Action Score' },
                      { id: 'holders', label: 'Holders' },
                      { id: 'totalValue', label: 'Total Held' },
                      { id: 'vsVwap', label: 'vs VWAP' },
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
                          setSortAsc(s.id === 'vsVwap');
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
                    { id: 'actionScore', label: 'Action Score' },
                    { id: 'netFlow', label: 'Net Flow' },
                    { id: 'revYoY', label: 'Rev YoY' },
                    { id: 'vsVwap', label: 'vs VWAP' },
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
                        setScreenerSortAsc(s.id === 'vsVwap');
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

        {/* CONVICTION MATRIX TABLE (UNIFIED 4-PILLAR ACTIONABILITY) */}
        {subTab === 'MATRIX' ? (
          <div className="table-scroll">
            <table className="data-table">
              <thead>
                <tr>
                  <th></th>
                  <th>Rank</th>
                  <th>Stock</th>
                  <th
                    className={`sortable ${matrixSortBy === 'actionScore' ? 'active-sort' : ''}`}
                    onClick={() => {
                      if (matrixSortBy === 'actionScore') setMatrixSortAsc((a) => !a);
                      else {
                        setMatrixSortBy('actionScore');
                        setMatrixSortAsc(false);
                      }
                    }}
                    title="Unified 4-Pillar Actionability Score (0–100) & Verdict"
                  >
                    Action Verdict {matrixSortBy === 'actionScore' ? (matrixSortAsc ? '▲' : '▼') : ''}
                  </th>
                  <th title="4 Pillars (0–25 each): Flow Impulse, Q-VWAP Price Discipline, XBRL Quality Gate, Thesis Persistence">
                    4 Pillars (/25)
                  </th>
                  <th
                    className={`num sortable ${matrixSortBy === 'netFlow' ? 'active-sort' : ''}`}
                    onClick={() => {
                      if (matrixSortBy === 'netFlow') setMatrixSortAsc((a) => !a);
                      else {
                        setMatrixSortBy('netFlow');
                        setMatrixSortAsc(false);
                      }
                    }}
                  >
                    QoQ Flow {matrixSortBy === 'netFlow' ? (matrixSortAsc ? '▲' : '▼') : ''}
                  </th>
                  <th
                    className={`sortable ${matrixSortBy === 'vsVwap' ? 'active-sort' : ''}`}
                    onClick={() => {
                      if (matrixSortBy === 'vsVwap') setMatrixSortAsc((a) => !a);
                      else {
                        setMatrixSortBy('vsVwap');
                        setMatrixSortAsc(true);
                      }
                    }}
                  >
                    Entry Zone (Q-VWAP) {matrixSortBy === 'vsVwap' ? (matrixSortAsc ? '▲' : '▼') : ''}
                  </th>
                  <th className="num">XBRL Gate</th>
                  <th
                    className={`num sortable ${matrixSortBy === 'holders' ? 'active-sort' : ''}`}
                    onClick={() => {
                      if (matrixSortBy === 'holders') setMatrixSortAsc((a) => !a);
                      else {
                        setMatrixSortBy('holders');
                        setMatrixSortAsc(false);
                      }
                    }}
                  >
                    Holders / TTM {matrixSortBy === 'holders' ? (matrixSortAsc ? '▲' : '▼') : ''}
                  </th>
                  <th>Key Fund Movers &amp; Backers</th>
                </tr>
              </thead>
              <tbody>
                {filteredMatrix.map((item, idx) => {
                  const isOpen = expandedTicker === item.ticker;
                  const act = item.actionability;
                  const p = act.pillars;
                  return (
                    <Fragment key={item.ticker}>
                      <tr>
                        <td style={{ width: '30px', paddingRight: 0 }}>
                          <button
                            type="button"
                            onClick={() =>
                              setExpandedTicker(isOpen ? null : item.ticker)
                            }
                            aria-label={`Expand ${item.ticker} conviction pillars`}
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
                        <td style={{ minWidth: '195px' }}>
                          <div style={{ display: 'flex', alignItems: 'center', gap: '0.45rem', flexWrap: 'wrap' }}>
                            {renderTickerButton(item.ticker)}
                            <span style={{ fontWeight: 600 }}>{item.name}</span>
                          </div>
                          <div className="mono" style={{ fontSize: '0.7rem', color: 'var(--text-muted)', marginTop: '0.15rem', display: 'flex', gap: '0.32rem', flexWrap: 'wrap' }}>
                            <span>{item.priceUsd ? `$${item.priceUsd.toFixed(2)}` : '—'}</span>
                            <span>·</span>
                            <span>{item.subSector || item.sector}</span>
                            {item.marketCapTier && <span>· {item.marketCapTier}</span>}
                            {item.idealPortfolioRank != null && (
                              <span className="chip pos" style={{ fontSize: '0.62rem' }}>
                                Ideal #{item.idealPortfolioRank}
                              </span>
                            )}
                          </div>
                          {item.sourceTags.length > 0 && (
                            <div style={{ display: 'flex', gap: '0.24rem', flexWrap: 'wrap', marginTop: '0.22rem' }}>
                              {item.sourceTags.slice(0, 3).map((tag) => (
                                <span
                                  key={tag}
                                  className={`chip ${
                                    tag === 'Smart Exit'
                                      ? 'neg'
                                      : tag === 'Cluster Buy' || tag === 'Ideal Portfolio'
                                        ? 'pos'
                                        : tag === 'Heavy Add' || tag === 'Top Consensus'
                                          ? 'tech'
                                          : ''
                                  }`}
                                  style={{ fontSize: '0.62rem' }}
                                >
                                  {tag}
                                </span>
                              ))}
                            </div>
                          )}
                        </td>
                        <td style={{ minWidth: '165px' }}>
                          <div style={{ display: 'flex', alignItems: 'center', gap: '0.35rem', flexWrap: 'wrap' }}>
                            <span className={`chip ${actionVerdictChipClass(item.actionVerdict)}`}>
                              {item.actionVerdict}
                            </span>
                          </div>
                          <div className="mono" style={{ fontSize: '0.82rem', fontWeight: 700, marginTop: '0.18rem' }}>
                            {item.actionabilityScore.toFixed(1)}
                            <span style={{ fontSize: '0.7rem', fontWeight: 500, color: 'var(--text-muted)' }}>
                              /100
                            </span>
                          </div>
                        </td>
                        <td style={{ minWidth: '185px' }}>
                          <div className="pillar-mini-strip mono">
                            <span
                              className={`pillar-mini-chip ${miniPillarTone(p.flowImpulse)}`}
                              title={`Smart-Money Flow Impulse: ${p.flowImpulse.toFixed(1)}/25`}
                            >
                              F {p.flowImpulse.toFixed(0)}
                            </span>
                            <span
                              className={`pillar-mini-chip ${miniPillarTone(p.priceDiscipline)}`}
                              title={`Price Discipline (Q-VWAP): ${p.priceDiscipline.toFixed(1)}/25`}
                            >
                              P {p.priceDiscipline.toFixed(0)}
                            </span>
                            <span
                              className={`pillar-mini-chip ${miniPillarTone(p.xbrlQuality)}`}
                              title={`XBRL Quality Gate: ${p.xbrlQuality.toFixed(1)}/25`}
                            >
                              Q {p.xbrlQuality.toFixed(0)}
                            </span>
                            <span
                              className={`pillar-mini-chip ${miniPillarTone(p.thesisPersistence)}`}
                              title={`Thesis Persistence: ${p.thesisPersistence.toFixed(1)}/25`}
                            >
                              T {p.thesisPersistence.toFixed(0)}
                            </span>
                          </div>
                          {act.thesisPersistence && act.thesisPersistence.quartersObserved > 0 && (
                            <div className="mono" style={{ fontSize: '0.67rem', color: 'var(--text-muted)', marginTop: '0.18rem' }}>
                              {act.thesisPersistence.consecutiveNetBuyQuarters >= 2
                                ? `${act.thesisPersistence.consecutiveNetBuyQuarters}Q net buy streak`
                                : act.thesisPersistence.consecutiveNetSellQuarters >= 2
                                  ? `${act.thesisPersistence.consecutiveNetSellQuarters}Q net sell streak`
                                  : act.thesisPersistence.summary}
                            </div>
                          )}
                        </td>
                        <td className="num" style={{ minWidth: '138px' }}>
                          <div
                            className={`mono ${returnClass(item.netFlowUsd)}`}
                            style={{ fontWeight: 700, fontSize: '0.84rem' }}
                          >
                            {item.netFlowUsd != null ? formatSignedUsd(item.netFlowUsd) : '—'}
                          </div>
                          <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '0.24rem', flexWrap: 'wrap', marginTop: '0.16rem' }}>
                            {item.newCount > 0 && (
                              <span className="chip pos" style={{ fontSize: '0.62rem' }}>+{item.newCount}N</span>
                            )}
                            {item.addedCount > 0 && (
                              <span className="chip tech" style={{ fontSize: '0.62rem' }}>+{item.addedCount}A</span>
                            )}
                            {(item.trimmedCount > 0 || item.exitedCount > 0) && (
                              <span className="chip neg" style={{ fontSize: '0.62rem' }}>
                                -{item.trimmedCount}T{item.exitedCount > 0 ? `/${item.exitedCount}X` : ''}
                              </span>
                            )}
                          </div>
                        </td>
                        <td style={{ minWidth: '170px' }}>
                          {item.entryZoneStatus || item.alphaDecayStatus !== 'NEUTRAL' ? (
                            <div>
                              <div style={{ display: 'flex', gap: '0.26rem', flexWrap: 'wrap' }}>
                                {item.entryZoneStatus && (
                                  <span className={`chip ${entryZoneChipClass(item.entryZoneStatus)}`}>
                                    {item.entryZoneStatus}
                                  </span>
                                )}
                                {item.alphaDecayStatus !== 'NEUTRAL' && (
                                  <span
                                    className={`chip ${alphaDecayChipClass(item.alphaDecayStatus)}`}
                                    title={item.alphaDecayReason}
                                  >
                                    {alphaDecayLabel(item.alphaDecayStatus)}
                                  </span>
                                )}
                              </div>
                              <div className="mono" style={{ fontSize: '0.68rem', color: 'var(--text-muted)', marginTop: '0.15rem' }}>
                                {item.pctVsQuarterVwap != null ? `${formatSignedPct(item.pctVsQuarterVwap)} vs ` : ''}
                                {item.estQuarterVwap != null ? `$${item.estQuarterVwap.toFixed(2)}` : ''}
                              </div>
                              {(item.filingToTodayPct != null || item.avgEliteHoldingQuarters != null) && (
                                <div className="mono" style={{ fontSize: '0.66rem', color: 'var(--text-muted)' }}>
                                  {item.filingToTodayPct != null ? `Fil→Now ${formatSignedPct(item.filingToTodayPct)}` : ''}
                                  {item.filingToTodayPct != null && item.avgEliteHoldingQuarters != null ? ' · ' : ''}
                                  {item.avgEliteHoldingQuarters != null ? `Hold ${item.avgEliteHoldingQuarters.toFixed(1)}Q` : ''}
                                </div>
                              )}
                            </div>
                          ) : (
                            <span className="ret-muted">—</span>
                          )}
                        </td>
                        <td className="num mono" style={{ fontSize: '0.76rem', minWidth: '122px' }}>
                          {item.fundamentals ? (
                            <div>
                              <div className={returnClass(item.fundamentals.revenueGrowthYoYPct)}>
                                {item.fundamentals.revenueGrowthYoYPct != null
                                  ? `${formatSignedPct(item.fundamentals.revenueGrowthYoYPct)} rev`
                                  : 'Rev —'}
                              </div>
                              <div style={{ fontSize: '0.68rem', color: 'var(--text-muted)' }}>
                                {item.fundamentals.fcfMarginPct != null
                                  ? `${item.fundamentals.fcfMarginPct.toFixed(0)}% FCF`
                                  : item.fundamentals.operatingMarginPct != null
                                    ? `${item.fundamentals.operatingMarginPct.toFixed(0)}% OpM`
                                    : 'Mgn —'}
                                {item.fundamentals.shareDilutionYoYPct != null
                                  ? ` · Dil ${formatSignedPct(item.fundamentals.shareDilutionYoYPct, 1)}`
                                  : ''}
                              </div>
                            </div>
                          ) : (
                            <span className="ret-muted">Neutral (12.5)</span>
                          )}
                        </td>
                        <td className="num mono" style={{ minWidth: '115px' }}>
                          <div>
                            <strong>{item.holderCount}</strong> ({item.eliteHoldersCount} Elite)
                          </div>
                          <div className={returnClass(item.stockTtmPct)} style={{ fontSize: '0.7rem' }}>
                            TTM {formatSignedPct(item.stockTtmPct, 1)}
                          </div>
                        </td>
                        <td style={{ minWidth: '255px' }}>
                          <div style={{ display: 'flex', flexWrap: 'wrap', gap: '0.26rem' }}>
                            {item.topHoldersPreview.slice(0, 3).map((h, hi) => (
                              <button
                                key={`${h.cik}-${hi}`}
                                type="button"
                                className={`chip clickable ${h.chipTone}`}
                                onClick={() => onSelectFund(h.cik)}
                                title={`Open ${h.fundName} (${h.fundTier})`}
                              >
                                <span>{h.fundName}</span>
                                <strong className="mono">{h.meta}</strong>
                              </button>
                            ))}
                          </div>
                        </td>
                      </tr>

                      {isOpen && (
                        <tr className="subrow">
                          <td colSpan={10}>
                            <div style={{ marginBottom: '0.65rem', display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: '0.5rem' }}>
                              <div style={{ display: 'flex', alignItems: 'center', gap: '0.45rem', flexWrap: 'wrap' }}>
                                <ShieldCheck size={15} color="var(--accent-primary)" />
                                <span style={{ fontSize: '0.82rem', fontWeight: 700 }}>
                                  {item.ticker} ({item.name}) — {item.actionVerdict} ({item.actionabilityScore.toFixed(1)}/100)
                                </span>
                              </div>
                              <button
                                type="button"
                                className="action-btn"
                                style={{ padding: '0.28rem 0.7rem', fontSize: '0.74rem' }}
                                onClick={() => onSelectStock(item.ticker)}
                              >
                                Open Complete {item.ticker} Stock Dossier
                              </button>
                            </div>

                            <div className="pillar-grid" style={{ marginBottom: '0.65rem' }}>
                              <div className="pillar-card">
                                <div className="pillar-head">
                                  <span>1. Flow Impulse</span>
                                  <strong className="mono">{p.flowImpulse.toFixed(1)}/25</strong>
                                </div>
                                <div className="pillar-bar-track">
                                  <div
                                    className={`pillar-bar-fill ${miniPillarTone(p.flowImpulse)}`}
                                    style={{ width: `${Math.min(100, (p.flowImpulse / 25) * 100)}%` }}
                                  />
                                </div>
                              </div>
                              <div className="pillar-card">
                                <div className="pillar-head">
                                  <span>2. Price Discipline</span>
                                  <strong className="mono">{p.priceDiscipline.toFixed(1)}/25</strong>
                                </div>
                                <div className="pillar-bar-track">
                                  <div
                                    className={`pillar-bar-fill ${miniPillarTone(p.priceDiscipline)}`}
                                    style={{ width: `${Math.min(100, (p.priceDiscipline / 25) * 100)}%` }}
                                  />
                                </div>
                              </div>
                              <div className="pillar-card">
                                <div className="pillar-head">
                                  <span>3. XBRL Quality</span>
                                  <strong className="mono">{p.xbrlQuality.toFixed(1)}/25</strong>
                                </div>
                                <div className="pillar-bar-track">
                                  <div
                                    className={`pillar-bar-fill ${miniPillarTone(p.xbrlQuality)}`}
                                    style={{ width: `${Math.min(100, (p.xbrlQuality / 25) * 100)}%` }}
                                  />
                                </div>
                              </div>
                              <div className="pillar-card">
                                <div className="pillar-head">
                                  <span>4. Persistence</span>
                                  <strong className="mono">{p.thesisPersistence.toFixed(1)}/25</strong>
                                </div>
                                <div className="pillar-bar-track">
                                  <div
                                    className={`pillar-bar-fill ${miniPillarTone(p.thesisPersistence)}`}
                                    style={{ width: `${Math.min(100, (p.thesisPersistence / 25) * 100)}%` }}
                                  />
                                </div>
                              </div>
                            </div>

                            <div
                              className={`alpha-decay-banner ${actionVerdictChipClass(item.actionVerdict)}`}
                              style={{ marginTop: 0, marginBottom: item.topHoldersPreview.length > 0 ? '0.65rem' : 0 }}
                            >
                              <div>
                                <strong style={{ marginRight: '0.4rem' }}>{item.actionVerdict}:</strong>
                                <span>{act.summary}</span>
                              </div>
                            </div>

                            {item.topHoldersPreview.length > 0 && (
                              <div className="holder-grid">
                                {item.topHoldersPreview.map((h, hi) => (
                                  <div key={`mx-h-${h.cik}-${hi}`} className="holder-card">
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
                                        {h.subline}
                                      </div>
                                    </div>
                                    <div style={{ textAlign: 'right' }}>
                                      <span className={`chip ${h.chipTone}`}>{h.meta}</span>
                                    </div>
                                  </div>
                                ))}
                              </div>
                            )}
                          </td>
                        </tr>
                      )}
                    </Fragment>
                  );
                })}
                {filteredMatrix.length === 0 && (
                  <tr>
                    <td colSpan={10} style={{ textAlign: 'center', padding: '2rem', color: 'var(--text-muted)' }}>
                      No stocks match your active Conviction Matrix filters. Try clearing Verdict, Cap Tier, Entry Zone, or Alpha-Decay filters.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        ) : subTab === 'CONSENSUS' ? (
          <div className="table-scroll">
            <table className="data-table">
              <thead>
                <tr>
                  <th></th>
                  <th>Rank</th>
                  <th>Stock</th>
                  <th
                    className={`sortable ${sortBy === 'actionScore' ? 'active-sort' : ''}`}
                    onClick={() => {
                      if (sortBy === 'actionScore') setSortAsc((a) => !a);
                      else {
                        setSortBy('actionScore');
                        setSortAsc(false);
                      }
                    }}
                  >
                    Action Verdict {sortBy === 'actionScore' ? (sortAsc ? '▲' : '▼') : ''}
                  </th>
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
                  const ad = resolveAlphaDecayMetrics(item, { latestQuarterLabel: quarterLabel });
                  const act = getConsensusActionability(item);

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
                        <td style={{ minWidth: '162px' }}>
                          <div style={{ display: 'flex', alignItems: 'center', gap: '0.32rem', flexWrap: 'wrap' }}>
                            <span className={`chip ${actionVerdictChipClass(act.verdict)}`}>
                              {act.verdict}
                            </span>
                            <span className="mono" style={{ fontWeight: 700, fontSize: '0.79rem' }}>
                              {act.score.toFixed(0)}/100
                            </span>
                          </div>
                          <div className="mono" style={{ fontSize: '0.67rem', color: 'var(--text-muted)', marginTop: '0.14rem' }}>
                            F:{act.pillars.flowImpulse.toFixed(0)} · P:{act.pillars.priceDiscipline.toFixed(0)} · Q:{act.pillars.xbrlQuality.toFixed(0)} · T:{act.pillars.thesisPersistence.toFixed(0)}
                          </div>
                        </td>
                        <td>
                          <span className="chip">{item.subSector || item.sector}</span>
                        </td>
                        <td style={{ minWidth: '175px' }}>
                          {item.entryZoneStatus || ad.status !== 'NEUTRAL' ? (
                            <div>
                              <div style={{ display: 'flex', gap: '0.28rem', flexWrap: 'wrap' }}>
                                {item.entryZoneStatus && (
                                  <span className={`chip ${entryZoneChipClass(item.entryZoneStatus)}`}>
                                    {item.entryZoneStatus}
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
                              <div className="mono" style={{ fontSize: '0.69rem', color: 'var(--text-muted)', marginTop: '0.15rem' }}>
                                {item.pctVsQuarterVwap != null ? `${formatSignedPct(item.pctVsQuarterVwap)} vs ` : ''}
                                {item.estQuarterVwap != null ? `$${item.estQuarterVwap.toFixed(2)}` : ''}
                              </div>
                              {(ad.filingToTodayPct != null || ad.avgEliteHoldingQuarters != null) && (
                                <div className="mono" style={{ fontSize: '0.67rem', color: 'var(--text-muted)' }}>
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
                          <td colSpan={12}>
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
                    <td colSpan={12} style={{ textAlign: 'center', padding: '2rem', color: 'var(--text-muted)' }}>
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
                  <th
                    className={`sortable ${screenerSortBy === 'actionScore' ? 'active-sort' : ''}`}
                    onClick={() => {
                      if (screenerSortBy === 'actionScore') setScreenerSortAsc((a) => !a);
                      else {
                        setScreenerSortBy('actionScore');
                        setScreenerSortAsc(false);
                      }
                    }}
                  >
                    Action Verdict {screenerSortBy === 'actionScore' ? (screenerSortAsc ? '▲' : '▼') : ''}
                  </th>
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

                  const ad = resolveAlphaDecayMetrics(item, { latestQuarterLabel: quarterLabel });
                  const act = getScreenerActionability(item);

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
                        <td style={{ minWidth: '162px' }}>
                          <div style={{ display: 'flex', alignItems: 'center', gap: '0.32rem', flexWrap: 'wrap' }}>
                            <span className={`chip ${actionVerdictChipClass(act.verdict)}`}>
                              {act.verdict}
                            </span>
                            <span className="mono" style={{ fontWeight: 700, fontSize: '0.79rem' }}>
                              {act.score.toFixed(0)}/100
                            </span>
                          </div>
                          <div className="mono" style={{ fontSize: '0.67rem', color: 'var(--text-muted)', marginTop: '0.14rem' }}>
                            F:{act.pillars.flowImpulse.toFixed(0)} · P:{act.pillars.priceDiscipline.toFixed(0)} · Q:{act.pillars.xbrlQuality.toFixed(0)} · T:{act.pillars.thesisPersistence.toFixed(0)}
                          </div>
                        </td>
                        <td>
                          <span className="chip">{item.subSector || item.sector}</span>
                        </td>
                        <td style={{ minWidth: '175px' }}>
                          {item.entryZoneStatus || ad.status !== 'NEUTRAL' ? (
                            <div>
                              <div style={{ display: 'flex', gap: '0.28rem', flexWrap: 'wrap' }}>
                                {item.entryZoneStatus && (
                                  <span className={`chip ${entryZoneChipClass(item.entryZoneStatus)}`}>
                                    {item.entryZoneStatus}
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
                              <div className="mono" style={{ fontSize: '0.69rem', color: 'var(--text-muted)', marginTop: '0.15rem' }}>
                                {item.pctVsQuarterVwap != null ? `${formatSignedPct(item.pctVsQuarterVwap)} vs ` : ''}
                                {item.estQuarterVwap != null ? `$${item.estQuarterVwap.toFixed(2)}` : ''}
                              </div>
                              {(ad.filingToTodayPct != null || ad.avgEliteHoldingQuarters != null) && (
                                <div className="mono" style={{ fontSize: '0.67rem', color: 'var(--text-muted)' }}>
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
                          <td colSpan={12}>
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
                    <td colSpan={12} style={{ textAlign: 'center', padding: '2rem', color: 'var(--text-muted)' }}>
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
