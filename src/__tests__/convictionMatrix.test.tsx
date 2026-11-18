import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { HoldingsTab } from '../components/HoldingsTab';
import { IdealPortfolioTab } from '../components/IdealPortfolioTab';
import { StockDetailDrawer } from '../components/StockDetailDrawer';
import type {
  BenchmarkSeries,
  ConsensusHolding,
  FundSummary,
  IdealPortfolioData,
  IdealPortfolioPosition,
  SignalScreenerData,
  SignalScreenerEntry,
  StockDetail,
  StockFundamentals,
  StockIndexEntry,
} from '../types/data';
import {
  buildUnifiedConvictionList,
  computeActionabilityMatrix,
  resolveActionabilityMatrix,
} from '../utils/convictionMatrix';
import * as dataLoader from '../utils/dataLoader';

function makeFundamentals(overrides: Partial<StockFundamentals> = {}): StockFundamentals {
  return {
    revenueTtmUsd: 25_000_000_000,
    revenueGrowthYoYPct: 26,
    operatingMarginPct: 24,
    fcfMarginPct: 22,
    rdIntensityPct: 14,
    shareDilutionYoYPct: -1.2,
    ...overrides,
  };
}

function makeStockIndexEntry(overrides: Partial<StockIndexEntry> = {}): StockIndexEntry {
  return {
    ticker: 'TEST',
    name: 'Test Corp',
    sector: 'Technology',
    subSector: 'Enterprise Software',
    shard: 0,
    priceUsd: 100,
    marketCapTier: 'Large $30B-$200B',
    holderCount: 10,
    eliteHoldersCount: 6,
    signalVerdict: 'Tracked Equity',
    entryZoneStatus: 'Near Q-Avg',
    pctVsQuarterVwap: 0,
    ...overrides,
  };
}

function makeScreenerEntry(overrides: Partial<SignalScreenerEntry> = {}): SignalScreenerEntry {
  return {
    ticker: 'TEST',
    name: 'Test Corp',
    sector: 'Technology',
    subSector: 'Enterprise Software',
    isTechTmt: true,
    priceUsd: 100,
    stockTtmPct: 22,
    holderCount: 12,
    eliteHoldersCount: 7,
    totalValueUsd: 600_000_000,
    avgWeightPct: 2.8,
    maxWeightPct: 5.4,
    newCount: 0,
    eliteNewCount: 0,
    addedCount: 0,
    trimmedCount: 0,
    exitedCount: 0,
    netDeltaUsd: 0,
    netEstFlowUsd: 0,
    movers: [],
    topBackers: [],
    estQuarterVwap: 100,
    pctVsQuarterVwap: 0,
    entryZoneStatus: 'Near Q-Avg',
    ...overrides,
  };
}

function makeBenchmark(ticker: string, name: string): BenchmarkSeries {
  const emptyWin = { YTD: 14, TTM: 16, '18M': 18, '2Y': 20, '3Y': 24, '5Y': 35 };
  return {
    ticker,
    name,
    returns: emptyWin,
    cagrPct: 16,
    maxDrawdownPct: -11,
    sharpeProxy: 1.2,
    sortinoRatio: 1.8,
    curve: [
      { date: '2025-01-15', nav: 100 },
      { date: '2026-01-15', nav: 116 },
    ],
  };
}

function makeIdealPosition(overrides: Partial<IdealPortfolioPosition> = {}): IdealPortfolioPosition {
  return {
    rank: 1,
    ticker: 'ALPH',
    cusip: '000000010',
    name: 'Alpha Chips Inc',
    sector: 'Technology',
    subSector: 'Semiconductors',
    industry: 'Semiconductors',
    isTechTmt: true,
    rawScore: 90,
    inflectionScore: 88,
    idealWeightPct: 50,
    convictionScore: 85,
    holderCount: 10,
    netNew: 3,
    netAdd: 3,
    netTrim: 0,
    avgFundWeightPct: 4.5,
    maxFundWeightPct: 8.0,
    topHolderName: 'Tiger Global',
    rationaleTag: 'Core Consensus',
    topBackers: [
      { cik: '0001', fundName: 'Alpha Tech', weightPct: 5.5, status: 'NEW' },
    ],
    stockTtmPct: 28,
    priceUsd: 92,
    estQuarterVwap: 100,
    pctVsQuarterVwap: -8,
    entryZoneStatus: 'Discount to Q-Avg',
    fundamentals: makeFundamentals(),
    avgEliteHoldingQuarters: 6.2,
    marketCapUsd: 120_000_000_000,
    marketCapTier: 'Large $30B-$200B',
    ...overrides,
  };
}

function makeConsensusHolding(overrides: Partial<ConsensusHolding> = {}): ConsensusHolding {
  return {
    rank: 1,
    ticker: 'ALPH',
    cusip: '000000010',
    name: 'Alpha Chips Inc',
    sector: 'Technology',
    industry: 'Semiconductors',
    subSector: 'Semiconductors',
    isTechTmt: true,
    priceUsd: 92,
    stockYtdPct: 18,
    stockTtmPct: 28,
    holderCount: 14,
    totalRankedFundsPool: 50,
    equalAggregateWeightPct: 4.6,
    avgHolderWeightPct: 3.4,
    aumAggregateWeightPct: 4.8,
    totalValueUsd: 900_000_000,
    netBuyers: 5,
    netTrimmers: 0,
    holders: [
      {
        cik: '0001',
        fundName: 'Alpha Tech',
        fundTier: 'Elite 50',
        weightPct: 5.2,
        valueUsd: 350_000_000,
        status: 'NEW',
        qoqSharesChangePct: null,
        rankedWindows: ['TTM'],
      },
    ],
    summaryText: 'Core Elite accumulation',
    marketCapTier: 'Large $30B-$200B',
    estQuarterVwap: 100,
    pctVsQuarterVwap: -8,
    entryZoneStatus: 'Discount to Q-Avg',
    fundamentals: makeFundamentals(),
    avgEliteHoldingQuarters: 6.5,
    ...overrides,
  };
}

function makeFundSummary(overrides: Partial<FundSummary> = {}): FundSummary {
  return {
    cik: '0001',
    shard: '1',
    name: 'Alpha Tech',
    legalName: 'Alpha Tech LP',
    manager: 'Jane Doe',
    hq: 'SF',
    styleTag: 'Tech',
    strategySummary: 'Tech growth',
    source: 'Curated Core',
    fundTier: 'Elite 50',
    isLeaderboardEligible: true,
    exclusionReason: null,
    latestPeriod: '2026-09-30',
    latestQuarterLabel: '2026 Q3',
    latestFilingDate: '2026-11-16',
    equityAumUsd: 2_000_000_000,
    totalReportedUsd: 2_000_000_000,
    equityPositionCount: 15,
    totalPositionCount: 15,
    top10WeightPct: 75,
    effectivePositions: 9,
    techTmtPct: 92,
    etfSharePct: 0,
    quartersTracked: 12,
    returns: {
      quarter_end: { YTD: 20, TTM: 25, '18M': 30, '2Y': 35, '3Y': 45, '5Y': 70 },
      filing_lag: { YTD: 21, TTM: 26, '18M': 31, '2Y': 36, '3Y': 47, '5Y': 72 },
    },
    metrics: {
      quarter_end: {
        cagrPct: 20,
        maxDrawdownPct: -10,
        sharpeProxy: 1.5,
        sortinoRatio: 2.2,
        infoRatioVsQqq: 0.9,
        betaVsQqq: 1.1,
        alphaVsQqqPct: 5,
        annualizedVolPct: 18,
        battingAvgPct: 70,
        avgTurnoverPct: 12,
        skillScore: 92,
        skillRank: 1,
      },
      filing_lag: {
        cagrPct: 21,
        maxDrawdownPct: -10,
        sharpeProxy: 1.6,
        sortinoRatio: 2.3,
        infoRatioVsQqq: 1.0,
        betaVsQqq: 1.1,
        alphaVsQqqPct: 6,
        annualizedVolPct: 18,
        battingAvgPct: 72,
        avgTurnoverPct: 12,
        skillScore: 93,
        skillRank: 1,
      },
    },
    topHoldingsPreview: [],
    sectorBreakdown: [],
    searchableTickers: ['ALPH'],
    edgarUrl: 'https://www.sec.gov',
    ...overrides,
  };
}

describe('convictionMatrix 4-pillar scoring engine', () => {
  it('assigns STRONG ACCUMULATE vs ACCUMULATE ON DIP vs HOLD vs DISTRIBUTION WARNING vs EXIT SIGNAL accurately', () => {
    const strong = computeActionabilityMatrix({
      newCount: 3,
      addedCount: 4,
      trimmedCount: 0,
      exitedCount: 0,
      eliteBuyersCount: 6,
      eliteSellersCount: 0,
      eliteHoldersCount: 10,
      totalValueUsd: 500_000_000,
      netEstFlowUsd: 260_000_000,
      pctVsQuarterVwap: -9,
      filingToTodayPct: -2,
      alphaDecayStatus: 'LAG_ADVANTAGE',
      fundamentals: makeFundamentals({
        revenueGrowthYoYPct: 34,
        fcfMarginPct: 28,
        shareDilutionYoYPct: -1.8,
      }),
      avgEliteHoldingQuarters: 7.4,
      thesisPersistence: {
        consecutiveNetBuyQuarters: 3,
        consecutiveNetSellQuarters: 0,
        netBuyQuartersLast4: 3,
        netSellQuartersLast4: 0,
        quartersObserved: 4,
        direction: 'ACCUMULATION',
        summary: '3Q consecutive net buying',
      },
      isCoreConsensus: true,
    });

    expect(strong.verdict).toBe('STRONG ACCUMULATE');
    expect(strong.score).toBeGreaterThanOrEqual(75);
    expect(strong.pillars.flowImpulse).toBeGreaterThanOrEqual(20);
    expect(strong.pillars.priceDiscipline).toBeGreaterThanOrEqual(22);
    expect(strong.pillars.xbrlQuality).toBeGreaterThanOrEqual(20);
    expect(strong.pillars.thesisPersistence).toBeGreaterThanOrEqual(20);

    const onDip = computeActionabilityMatrix({
      newCount: 3,
      addedCount: 4,
      trimmedCount: 0,
      exitedCount: 0,
      eliteBuyersCount: 6,
      eliteSellersCount: 0,
      eliteHoldersCount: 9,
      totalValueUsd: 500_000_000,
      netEstFlowUsd: 240_000_000,
      pctVsQuarterVwap: 20, // Price discipline ~7.0 < 12 -> gates STRONG ACCUMULATE to ACCUMULATE ON DIP
      filingToTodayPct: 6,
      alphaDecayStatus: 'NEUTRAL',
      fundamentals: makeFundamentals(),
      avgEliteHoldingQuarters: 6.2,
      thesisPersistence: {
        consecutiveNetBuyQuarters: 3,
        consecutiveNetSellQuarters: 0,
        netBuyQuartersLast4: 3,
        netSellQuartersLast4: 0,
        quartersObserved: 4,
        direction: 'ACCUMULATION',
        summary: '3Q consecutive net buying',
      },
      isCoreConsensus: true,
    });

    expect(onDip.verdict).toBe('ACCUMULATE ON DIP');
    expect(onDip.pillars.priceDiscipline).toBeLessThan(12);

    const dilutedGate = computeActionabilityMatrix({
      newCount: 2,
      addedCount: 3,
      trimmedCount: 0,
      exitedCount: 0,
      eliteBuyersCount: 4,
      eliteSellersCount: 0,
      eliteHoldersCount: 6,
      totalValueUsd: 300_000_000,
      netEstFlowUsd: 160_000_000,
      pctVsQuarterVwap: -6,
      alphaDecayStatus: 'LAG_ADVANTAGE',
      fundamentals: makeFundamentals({
        revenueGrowthYoYPct: 6,
        fcfMarginPct: -8,
        operatingMarginPct: -6,
        shareDilutionYoYPct: 12, // severe SBC dilution drags XBRL quality < 8
      }),
      avgEliteHoldingQuarters: 5.0,
      thesisPersistence: {
        consecutiveNetBuyQuarters: 3,
        consecutiveNetSellQuarters: 0,
        netBuyQuartersLast4: 3,
        netSellQuartersLast4: 0,
        quartersObserved: 4,
        direction: 'ACCUMULATION',
        summary: '3Q consecutive net buying',
      },
    });

    expect(dilutedGate.pillars.xbrlQuality).toBeLessThan(8);
    expect(dilutedGate.verdict).toBe('HOLD');

    const neutralXbrl = computeActionabilityMatrix({
      newCount: 1,
      addedCount: 1,
      pctVsQuarterVwap: 0,
      fundamentals: null,
    });
    expect(neutralXbrl.pillars.xbrlQuality).toBe(12.5);

    const distTrap = computeActionabilityMatrix({
      newCount: 0,
      addedCount: 1,
      trimmedCount: 2,
      exitedCount: 1,
      eliteBuyersCount: 1,
      eliteSellersCount: 3,
      eliteHoldersCount: 4,
      totalValueUsd: 350_000_000,
      netEstFlowUsd: -45_000_000,
      pctVsQuarterVwap: 28,
      filingToTodayPct: 12,
      alphaDecayStatus: 'FOMO_TRAP',
      fundamentals: makeFundamentals({ revenueGrowthYoYPct: 24, fcfMarginPct: 22, shareDilutionYoYPct: 0.5 }),
      avgEliteHoldingQuarters: 3.1,
    });

    expect(distTrap.verdict).toBe('DISTRIBUTION WARNING');
    expect(distTrap.pillars.priceDiscipline).toBeLessThanOrEqual(5);

    const exitSignal = computeActionabilityMatrix({
      newCount: 0,
      addedCount: 0,
      trimmedCount: 3,
      exitedCount: 3,
      eliteBuyersCount: 0,
      eliteSellersCount: 5,
      eliteHoldersCount: 2,
      totalValueUsd: 400_000_000,
      netEstFlowUsd: -250_000_000,
      pctVsQuarterVwap: 18,
      fundamentals: makeFundamentals({ revenueGrowthYoYPct: -4, fcfMarginPct: 2, shareDilutionYoYPct: 5 }),
      avgEliteHoldingQuarters: 1.4,
      thesisPersistence: {
        consecutiveNetBuyQuarters: 0,
        consecutiveNetSellQuarters: 3,
        netBuyQuartersLast4: 0,
        netSellQuartersLast4: 3,
        quartersObserved: 4,
        direction: 'DISTRIBUTION',
        summary: '3Q consecutive net distribution',
      },
    });

    expect(exitSignal.verdict).toBe('EXIT SIGNAL');
    expect(exitSignal.score).toBeLessThan(35);
  });

  it('merges cross-silo tickers cleanly into UnifiedConvictionList without duplicates', () => {
    const alphConsensus = makeConsensusHolding({ ticker: 'ALPH', name: 'Alpha Chips Inc' });
    const alphCluster = makeScreenerEntry({
      ticker: 'ALPH',
      name: 'Alpha Chips Inc',
      newCount: 3,
      eliteNewCount: 2,
      addedCount: 3,
      netEstFlowUsd: 210_000_000,
      pctVsQuarterVwap: -8,
      entryZoneStatus: 'Discount to Q-Avg',
      fundamentals: makeFundamentals(),
      avgEliteHoldingQuarters: 6.5,
      movers: [
        {
          cik: '0001',
          fundName: 'Alpha Tech',
          fundTier: 'Elite 50',
          action: 'NEW',
          currBookPct: 5.2,
          prevBookPct: 0,
          valueDeltaUsd: 120_000_000,
          estFlowUsd: 115_000_000,
          sharesDeltaPct: null,
        },
      ],
    });
    const fomoExit = makeScreenerEntry({
      ticker: 'OVRP',
      name: 'Overpriced Software',
      newCount: 0,
      addedCount: 0,
      trimmedCount: 3,
      exitedCount: 3,
      eliteHoldersCount: 3,
      eliteBuyersCount: 0,
      eliteSellersCount: 5,
      netEstFlowUsd: -195_000_000,
      pctVsQuarterVwap: 29,
      entryZoneStatus: 'Chasing (+30%+)',
      alphaDecayStatus: 'FOMO_TRAP',
      fundamentals: makeFundamentals({ revenueGrowthYoYPct: 1, fcfMarginPct: 2, shareDilutionYoYPct: 7 }),
      avgEliteHoldingQuarters: 1.5,
      movers: [
        {
          cik: '0002',
          fundName: 'Exit Fund',
          fundTier: 'Elite 50',
          action: 'EXITED',
          currBookPct: 0,
          prevBookPct: 3.1,
          valueDeltaUsd: -95_000_000,
          estFlowUsd: -90_000_000,
          sharesDeltaPct: -100,
        },
      ],
    });

    const idealPos = makeIdealPosition({ ticker: 'ALPH', rank: 1, idealWeightPct: 100 });
    const poolCand = makeIdealPosition({
      ticker: 'CONT',
      rank: 15,
      name: 'Contrarian Security',
      idealWeightPct: 0,
      holderCount: 4,
      netNew: 1,
      netAdd: 1,
      netTrim: 1,
      pctVsQuarterVwap: 6,
      entryZoneStatus: 'Near Q-Avg',
      avgEliteHoldingQuarters: 2.2,
      fundamentals: makeFundamentals({ revenueGrowthYoYPct: 10, fcfMarginPct: 9, shareDilutionYoYPct: 1.5 }),
    });

    const rows = buildUnifiedConvictionList({
      topHoldings: { filing_lag: [alphConsensus], quarter_end: [alphConsensus] },
      signalScreener: {
        quarterLabel: '2026 Q3',
        clusterInitiations: [alphCluster],
        heavyAdds: [],
        smartMoneyExits: [fomoExit],
        contrarianBets: [],
      },
      idealPortfolio: {
        positionCount: 1,
        maxPositionCapPct: 8,
        rankedFundsCount: 50,
        effectivePositions: 1,
        top10WeightPct: 100,
        methodologySummary: 'Mock ideal portfolio',
        sectorBreakdown: [],
        positions: [idealPos],
        candidatePool: [idealPos, poolCand],
        backtest: {
          quarter_end: {
            returns: { YTD: 15, TTM: 20, '18M': 24, '2Y': 28, '3Y': 35, '5Y': 50 },
            cagrPct: 20,
            maxDrawdownPct: -10,
            sharpeProxy: 1.5,
            sortinoRatio: 2.1,
            infoRatioVsQqq: 0.9,
            curve: [],
          },
          filing_lag: {
            returns: { YTD: 16, TTM: 21, '18M': 25, '2Y': 30, '3Y': 38, '5Y': 55 },
            cagrPct: 21,
            maxDrawdownPct: -10,
            sharpeProxy: 1.6,
            sortinoRatio: 2.2,
            infoRatioVsQqq: 1.0,
            curve: [],
          },
        },
      },
      stocksIndex: {
        ALPH: makeStockIndexEntry({ ticker: 'ALPH', consecutiveNetBuyQuarters: 3 }),
        OVRP: makeStockIndexEntry({ ticker: 'OVRP', consecutiveNetSellQuarters: 2 }),
        CONT: makeStockIndexEntry({ ticker: 'CONT' }),
      },
      funds: [
        {
          cik: '0001',
          shard: '1',
          name: 'Alpha Tech',
          legalName: 'Alpha Tech LP',
          manager: 'Jane Doe',
          hq: 'SF',
          styleTag: 'Tech',
          strategySummary: 'Tech growth',
          source: 'Curated Core',
          fundTier: 'Elite 50',
          isLeaderboardEligible: true,
          exclusionReason: null,
          latestPeriod: '2026-09-30',
          latestQuarterLabel: '2026 Q3',
          latestFilingDate: '2026-11-16',
          equityAumUsd: 2_000_000_000,
          totalReportedUsd: 2_000_000_000,
          equityPositionCount: 15,
          totalPositionCount: 15,
          top10WeightPct: 75,
          effectivePositions: 9,
          techTmtPct: 92,
          etfSharePct: 0,
          quartersTracked: 12,
          returns: {
            quarter_end: { YTD: 20, TTM: 25, '18M': 30, '2Y': 35, '3Y': 45, '5Y': 70 },
            filing_lag: { YTD: 21, TTM: 26, '18M': 31, '2Y': 36, '3Y': 47, '5Y': 72 },
          },
          metrics: {
            quarter_end: {
              cagrPct: 20,
              maxDrawdownPct: -10,
              sharpeProxy: 1.5,
              sortinoRatio: 2.2,
              infoRatioVsQqq: 0.9,
              betaVsQqq: 1.1,
              alphaVsQqqPct: 5,
              annualizedVolPct: 18,
              battingAvgPct: 70,
              avgTurnoverPct: 12,
              skillScore: 92,
              skillRank: 1,
            },
            filing_lag: {
              cagrPct: 21,
              maxDrawdownPct: -10,
              sharpeProxy: 1.6,
              sortinoRatio: 2.3,
              infoRatioVsQqq: 1.0,
              betaVsQqq: 1.1,
              alphaVsQqqPct: 6,
              annualizedVolPct: 18,
              battingAvgPct: 72,
              avgTurnoverPct: 12,
              skillScore: 93,
              skillRank: 1,
            },
          },
          topHoldingsPreview: [],
          sectorBreakdown: [],
          searchableTickers: ['ALPH'],
          edgarUrl: 'https://www.sec.gov',
        } satisfies FundSummary,
      ],
      mode: 'filing_lag',
    });

    expect(rows.map((r) => r.ticker)).toEqual(['ALPH', 'CONT', 'OVRP']);
    const alphRow = rows[0];
    expect(alphRow.sourceTags).toEqual(['Top Consensus', 'Cluster Buy', 'Ideal Portfolio']);
    expect(alphRow.actionVerdict).toBe('STRONG ACCUMULATE');
    expect(rows[2].actionVerdict).toBe('EXIT SIGNAL');

    const resolvedFromIndex = resolveActionabilityMatrix(
      makeStockIndexEntry({ ticker: 'ALPH', pctVsQuarterVwap: -8, consecutiveNetBuyQuarters: 3 }),
      {
        latestQuarterLabel: '2026 Q3',
        fallbackBuyers: { newCount: 3, addedCount: 2, trimmedCount: 0, exitedCount: 0 },
        fallbackFlowUsd: 180_000_000,
        fallbackFundamentals: makeFundamentals(),
      }
    );
    expect(resolvedFromIndex.verdict).toBe('STRONG ACCUMULATE');
  });

  it('expands buildUnifiedConvictionList to all stocksIndex equities held by Elite 50 when includeAllEliteHeld=true while preserving curated rows and backend verdicts', () => {
    const curatedConsensus = makeConsensusHolding({ ticker: 'ALPH', name: 'Alpha Chips Inc' });
    const signalScreener: SignalScreenerData = {
      quarterLabel: '2026 Q3',
      clusterInitiations: [],
      heavyAdds: [],
      smartMoneyExits: [],
      contrarianBets: [],
    };
    const stocksIndex: Record<string, StockIndexEntry> = {
      ALPH: makeStockIndexEntry({ ticker: 'ALPH', name: 'Alpha Chips Inc', holderCount: 14, eliteHoldersCount: 10 }),
      EXIN: makeStockIndexEntry({
        ticker: 'EXIN',
        name: 'Extra Index Equity',
        subSector: 'MedTech Devices',
        priceUsd: 64.5,
        holderCount: 11,
        eliteHoldersCount: 3,
        eliteBuyersCount: 1,
        eliteSellersCount: 4,
        signalVerdict: 'Trimming / Distribution',
        entryZoneStatus: 'Near Q-Avg',
        pctVsQuarterVwap: 1.5,
        filingToTodayPct: 4.2,
        avgEliteHoldingQuarters: 3.5,
        alphaDecayStatus: 'LAG_ADVANTAGE',
        actionabilityScore: 54.0,
        actionVerdict: 'DISTRIBUTION WARNING',
        consecutiveNetSellQuarters: 2,
      }),
      NOEL: makeStockIndexEntry({
        ticker: 'NOEL',
        name: 'No Elite Holders Corp',
        holderCount: 8,
        eliteHoldersCount: 0,
      }),
      ZERO: makeStockIndexEntry({
        ticker: 'ZERO',
        name: 'Zero Total Holders Corp',
        holderCount: 0,
        eliteHoldersCount: 2,
      }),
    };

    const funds: FundSummary[] = [
      makeFundSummary({
        cik: '0002',
        name: 'Beta Top 100',
        fundTier: 'Top 100',
        searchableTickers: ['EXIN'],
        metrics: {
          ...makeFundSummary().metrics,
          filing_lag: { ...makeFundSummary().metrics.filing_lag, skillScore: 71 },
        },
      }),
      makeFundSummary({
        cik: '0001',
        name: 'Alpha Elite',
        fundTier: 'Elite 50',
        searchableTickers: ['ALPH', 'EXIN'],
        topHoldingsPreview: [{ ticker: 'EXIN', name: 'Extra Index Equity', weightPct: 3.8 }],
      }),
    ];

    const curatedOnly = buildUnifiedConvictionList({
      topHoldings: { filing_lag: [curatedConsensus], quarter_end: [curatedConsensus] },
      signalScreener,
      stocksIndex,
      funds,
      mode: 'filing_lag',
    });
    expect(curatedOnly.map((r) => r.ticker)).toEqual(['ALPH']);
    expect(curatedOnly[0].hasExactPillars).toBe(true);
    expect(curatedOnly[0].isIndexOnly).toBe(false);

    const allHeld = buildUnifiedConvictionList({
      topHoldings: { filing_lag: [curatedConsensus], quarter_end: [curatedConsensus] },
      signalScreener,
      stocksIndex,
      funds,
      mode: 'filing_lag',
      includeAllEliteHeld: true,
    });

    expect(allHeld.map((r) => r.ticker)).toEqual(['ALPH', 'EXIN']);

    const alph = allHeld.find((r) => r.ticker === 'ALPH')!;
    expect(alph.sourceTags).toEqual(['Top Consensus']);
    expect(alph.hasExactPillars).toBe(true);
    expect(alph.isIndexOnly).toBe(false);

    const exin = allHeld.find((r) => r.ticker === 'EXIN')!;
    expect(exin.isIndexOnly).toBe(true);
    expect(exin.hasExactPillars).toBe(false);
    expect(exin.sourceTags).toEqual(['Elite 50 Held']);
    expect(exin.signalVerdict).toBe('Trimming / Distribution');
    expect(exin.actionabilityScore).toBe(54.0);
    expect(exin.actionVerdict).toBe('DISTRIBUTION WARNING');
    expect(exin.eliteBuyersCount).toBe(1);
    expect(exin.eliteSellersCount).toBe(4);
    expect(exin.alphaDecayStatus).toBe('LAG_ADVANTAGE');
    expect(exin.alphaDecayReason).not.toMatch(/0 cluster/i);
    // Elite 50 fund sorted before Top 100 fund with weight % preserved from topHoldingsPreview
    expect(exin.topHoldersPreview[0]).toMatchObject({
      cik: '0001',
      fundName: 'Alpha Elite',
      fundTier: 'Elite 50',
      meta: '3.8%',
      chipTone: 'elite',
    });
    expect(exin.topHoldersPreview[1]).toMatchObject({
      cik: '0002',
      fundName: 'Beta Top 100',
      fundTier: 'Top 100',
      meta: 'Skill 71',
    });
  });
});

describe('Conviction Matrix UI integration across Drawer, HoldingsTab, and IdealPortfolioTab', () => {
  it('renders 4-pillar Actionability Matrix card and header verdict chip inside StockDetailDrawer', async () => {
    const detail: StockDetail = {
      ticker: 'ALPH',
      cusip: '000000010',
      name: 'Alpha Chips Inc',
      sector: 'Technology',
      industry: 'Semiconductors',
      subSector: 'Semiconductors',
      isTechTmt: true,
      priceUsd: 92,
      stockYtdPct: 15,
      stockTtmPct: 28,
      marketCapUsd: 120_000_000_000,
      marketCapTier: 'Large $30B-$200B',
      estQuarterVwap: 100,
      quarterLow: 85,
      quarterHigh: 108,
      pctVsQuarterVwap: -8,
      entryZoneStatus: 'Discount to Q-Avg',
      fundamentals: makeFundamentals(),
      signalVerdict: 'Cluster Initiation',
      idealPortfolioRank: 1,
      idealWeightPct: 6.5,
      holderCount: 14,
      eliteHoldersCount: 9,
      totalValueUsd: 950_000_000,
      avgWeightPct: 3.4,
      maxWeightPct: 6.2,
      newCount: 3,
      addedCount: 4,
      trimmedCount: 0,
      exitedCount: 0,
      netEstFlowUsd: 260_000_000,
      holders: [],
      recentMoves: [],
      midQuarterAlerts: [],
      actionability: {
        score: 88.5,
        verdict: 'STRONG ACCUMULATE',
        pillars: {
          flowImpulse: 22.8,
          priceDiscipline: 24.1,
          xbrlQuality: 21.4,
          thesisPersistence: 20.2,
        },
        summary: 'Aligned 4-pillar conviction across inflows, Q-VWAP discount, XBRL FCF & persistence.',
        thesisPersistence: {
          consecutiveNetBuyQuarters: 3,
          consecutiveNetSellQuarters: 0,
          netBuyQuartersLast4: 3,
          netSellQuartersLast4: 0,
          quartersObserved: 4,
          direction: 'ACCUMULATION',
          summary: '3Q consecutive net buying',
        },
      },
      actionabilityScore: 88.5,
      actionVerdict: 'STRONG ACCUMULATE',
    };

    const spy = vi.spyOn(dataLoader, 'fetchStockDetail').mockResolvedValueOnce(detail);

    render(
      <StockDetailDrawer
        ticker="ALPH"
        stocksIndex={{ ALPH: makeStockIndexEntry({ ticker: 'ALPH', name: 'Alpha Chips Inc', priceUsd: 92 }) }}
        latestQuarterLabel="2026 Q3"
        onClose={() => {}}
        onSelectFund={() => {}}
      />
    );

    await waitFor(() => {
      expect(screen.getByText(/Unified Actionable Buy \/ Trim \/ Exit Conviction Matrix/i)).toBeInTheDocument();
    });

    expect(screen.getByText('STRONG ACCUMULATE · 89/100')).toBeInTheDocument();
    expect(screen.getByText('1. Smart-Money Flow Impulse')).toBeInTheDocument();
    expect(screen.getByText('2. Price Discipline (Q-VWAP)')).toBeInTheDocument();
    expect(screen.getByText('3. XBRL Quality Gate')).toBeInTheDocument();
    expect(screen.getByText('4. Thesis Persistence')).toBeInTheDocument();
    expect(screen.getByText('22.8/25')).toBeInTheDocument();
    expect(screen.getByText('24.1/25')).toBeInTheDocument();
    expect(screen.getByText('21.4/25')).toBeInTheDocument();
    expect(screen.getByText('20.2/25')).toBeInTheDocument();
    expect(screen.getByText('3Q consecutive net buying')).toBeInTheDocument();

    spy.mockRestore();
  });

  it('defaults HoldingsTab to Conviction Matrix, filters by Action Verdict, and expands 4-pillar breakdown', () => {
    const strongConsensus = makeConsensusHolding({ ticker: 'ALPH', name: 'Alpha Chips Inc' });
    const exitEntry = makeScreenerEntry({
      ticker: 'OVRP',
      name: 'Overpriced Software',
      newCount: 0,
      addedCount: 0,
      trimmedCount: 3,
      exitedCount: 3,
      eliteHoldersCount: 2,
      eliteBuyersCount: 0,
      eliteSellersCount: 4,
      netEstFlowUsd: -200_000_000,
      pctVsQuarterVwap: 24,
      entryZoneStatus: 'Extended',
      fundamentals: makeFundamentals({ revenueGrowthYoYPct: -3, fcfMarginPct: 1, shareDilutionYoYPct: 6 }),
      avgEliteHoldingQuarters: 1.5,
      movers: [
        {
          cik: '0002',
          fundName: 'Exit Fund',
          fundTier: 'Elite 50',
          action: 'EXITED',
          currBookPct: 0,
          prevBookPct: 3.0,
          valueDeltaUsd: -110_000_000,
          estFlowUsd: -105_000_000,
          sharesDeltaPct: -100,
        },
      ],
    });

    const signalScreener: SignalScreenerData = {
      quarterLabel: '2026 Q3',
      clusterInitiations: [],
      heavyAdds: [],
      smartMoneyExits: [exitEntry],
      contrarianBets: [],
    };

    const onSelectFund = vi.fn();

    render(
      <HoldingsTab
        topHoldings={{ filing_lag: [strongConsensus], quarter_end: [strongConsensus] }}
        signalScreener={signalScreener}
        stocksIndex={{
          ALPH: makeStockIndexEntry({ ticker: 'ALPH', name: 'Alpha Chips Inc', consecutiveNetBuyQuarters: 3 }),
          OVRP: makeStockIndexEntry({ ticker: 'OVRP', name: 'Overpriced Software', consecutiveNetSellQuarters: 3 }),
        }}
        mode="filing_lag"
        onModeChange={() => {}}
        onSelectFund={onSelectFund}
        onSelectStock={() => {}}
      />
    );

    expect(screen.getByRole('button', { name: /Conviction Matrix \(2\)/i })).toHaveClass('active');
    expect(screen.getByText('Alpha Chips Inc')).toBeInTheDocument();
    expect(screen.getByText('Overpriced Software')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /Exit Signal \(1\)/i }));
    expect(screen.queryByText('Alpha Chips Inc')).not.toBeInTheDocument();
    expect(screen.getByText('Overpriced Software')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /Expand OVRP conviction pillars/i }));
    expect(screen.getByText('1. Flow Impulse')).toBeInTheDocument();
    expect(screen.getByText('2. Price Discipline')).toBeInTheDocument();
    expect(screen.getByText('3. XBRL Quality')).toBeInTheDocument();
    expect(screen.getByText('4. Persistence')).toBeInTheDocument();
    expect(screen.getByText(/Open Complete OVRP Stock Dossier/i)).toBeInTheDocument();
  });

  it('shows Action Verdict filters in IdealPortfolioTab and inline distributing-signal caution in Rebalance Calculator', () => {
    window.localStorage.clear();

    const posStrong = makeIdealPosition({
      rank: 1,
      ticker: 'ALPH',
      name: 'Alpha Chips Inc',
      idealWeightPct: 50,
      priceUsd: 92,
      actionability: {
        score: 86,
        verdict: 'STRONG ACCUMULATE',
        pillars: { flowImpulse: 22, priceDiscipline: 22, xbrlQuality: 21, thesisPersistence: 21 },
        summary: 'Aligned 4-pillar conviction',
      },
    });
    const posDist = makeIdealPosition({
      rank: 2,
      ticker: 'DIST',
      name: 'Distributing Cloud Inc',
      idealWeightPct: 50,
      priceUsd: 128,
      pctVsQuarterVwap: 28,
      entryZoneStatus: 'Extended',
      netNew: 0,
      netAdd: 1,
      netTrim: 4,
      actionability: {
        score: 42,
        verdict: 'DISTRIBUTION WARNING',
        pillars: { flowImpulse: 8, priceDiscipline: 4, xbrlQuality: 16, thesisPersistence: 14 },
        summary: 'Trading extended above Q-VWAP while smart money distributes.',
      },
    });

    const idealPortfolio: IdealPortfolioData = {
      positionCount: 2,
      maxPositionCapPct: 50,
      rankedFundsCount: 50,
      effectivePositions: 2,
      top10WeightPct: 100,
      methodologySummary: 'Test ideal portfolio',
      sectorBreakdown: [{ sector: 'Semiconductors', weightPct: 100, stockCount: 2 }],
      positions: [posStrong, posDist],
      candidatePool: [posStrong, posDist],
      backtest: {
        quarter_end: {
          returns: { YTD: 15, TTM: 20, '18M': 24, '2Y': 28, '3Y': 35, '5Y': 50 },
          cagrPct: 20,
          maxDrawdownPct: -10,
          sharpeProxy: 1.5,
          sortinoRatio: 2.0,
          infoRatioVsQqq: 0.9,
          curve: [
            { date: '2025-01-15', nav: 100 },
            { date: '2026-01-15', nav: 120 },
          ],
        },
        filing_lag: {
          returns: { YTD: 16, TTM: 21, '18M': 25, '2Y': 30, '3Y': 38, '5Y': 55 },
          cagrPct: 21,
          maxDrawdownPct: -10,
          sharpeProxy: 1.6,
          sortinoRatio: 2.1,
          infoRatioVsQqq: 1.0,
          curve: [
            { date: '2025-01-15', nav: 100 },
            { date: '2026-01-15', nav: 121 },
          ],
        },
      },
    };

    render(
      <IdealPortfolioTab
        idealPortfolio={idealPortfolio}
        benchmarks={{
          QQQ: makeBenchmark('QQQ', 'Invesco QQQ'),
          SPY: makeBenchmark('SPY', 'SPDR S&P 500'),
          XLK: makeBenchmark('XLK', 'Tech Select SPDR'),
        }}
        stocksIndex={{
          ALPH: makeStockIndexEntry({ ticker: 'ALPH', name: 'Alpha Chips Inc', priceUsd: 92 }),
          DIST: makeStockIndexEntry({ ticker: 'DIST', name: 'Distributing Cloud Inc', priceUsd: 128 }),
        }}
        latestQuarterLabel="2026 Q3"
        mode="filing_lag"
        onModeChange={() => {}}
        onSelectFund={() => {}}
        onSelectStock={() => {}}
      />
    );

    expect(screen.getByText('Alpha Chips Inc')).toBeInTheDocument();
    expect(screen.getByText('Distributing Cloud Inc')).toBeInTheDocument();

    // Verdict filter pills filter the portfolio holdings table cleanly
    fireEvent.click(screen.getByRole('button', { name: /Strong Accumulate \(1\)/i }));
    expect(screen.getByText('Alpha Chips Inc')).toBeInTheDocument();
    expect(screen.queryByText('Distributing Cloud Inc')).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /All Verdicts/i }));
    expect(screen.getByText('Distributing Cloud Inc')).toBeInTheDocument();

    // Open Rebalance Calculator and enter ALPH 50000 (0 DIST held -> BUY order for DIST triggers caution)
    fireEvent.click(screen.getByRole('button', { name: /Rebalance Calculator/i }));
    const textarea = screen.getByLabelText(/Your Current Holdings/i);
    fireEvent.change(textarea, { target: { value: 'ALPH 50000' } });

    expect(screen.getByText(/Caution: distributing signal/i)).toBeInTheDocument();
  });

  it('toggles HoldingsTab between Curated Signals and All Held Stocks from stocksIndex, renders index-only metadata honestly, and opens Dossier', () => {
    const strongConsensus = makeConsensusHolding({ ticker: 'ALPH', name: 'Alpha Chips Inc' });
    const signalScreener: SignalScreenerData = {
      quarterLabel: '2026 Q3',
      clusterInitiations: [],
      heavyAdds: [],
      smartMoneyExits: [],
      contrarianBets: [],
    };
    const stocksIndex: Record<string, StockIndexEntry> = {
      ALPH: makeStockIndexEntry({
        ticker: 'ALPH',
        name: 'Alpha Chips Inc',
        holderCount: 14,
        eliteHoldersCount: 10,
      }),
      EXIN: makeStockIndexEntry({
        ticker: 'EXIN',
        name: 'Extra Index Equity',
        subSector: 'MedTech Devices',
        priceUsd: 64.5,
        holderCount: 9,
        eliteHoldersCount: 3,
        eliteBuyersCount: 2,
        eliteSellersCount: 3,
        signalVerdict: 'Mixed / Holding',
        entryZoneStatus: 'Near Q-Avg',
        pctVsQuarterVwap: -4.2,
        filingToTodayPct: 3.1,
        avgEliteHoldingQuarters: 4.5,
        alphaDecayStatus: 'LAG_ADVANTAGE',
        actionabilityScore: 54.0,
        actionVerdict: 'DISTRIBUTION WARNING',
        consecutiveNetSellQuarters: 2,
      }),
      NOEL: makeStockIndexEntry({
        ticker: 'NOEL',
        name: 'Non Elite Stock Corp',
        holderCount: 5,
        eliteHoldersCount: 0,
      }),
    };
    const funds: FundSummary[] = [
      makeFundSummary({
        cik: '0001',
        name: 'Alpha Elite',
        fundTier: 'Elite 50',
        searchableTickers: ['ALPH', 'EXIN'],
        topHoldingsPreview: [{ ticker: 'EXIN', name: 'Extra Index Equity', weightPct: 4.1 }],
      }),
    ];

    const onSelectFund = vi.fn();
    const onSelectStock = vi.fn();

    render(
      <HoldingsTab
        topHoldings={{ filing_lag: [strongConsensus], quarter_end: [strongConsensus] }}
        signalScreener={signalScreener}
        stocksIndex={stocksIndex}
        funds={funds}
        mode="filing_lag"
        onModeChange={() => {}}
        onSelectFund={onSelectFund}
        onSelectStock={onSelectStock}
      />
    );

    const curatedBtn = screen.getByRole('button', { name: /Curated Signals \(1\)/i });
    const allHeldBtn = screen.getByRole('button', { name: /All Held Stocks \(2\)/i });
    expect(curatedBtn).toHaveClass('active');
    expect(screen.getByRole('button', { name: /Conviction Matrix \(1\)/i })).toHaveClass('active');
    expect(screen.getByText('Alpha Chips Inc')).toBeInTheDocument();
    expect(screen.queryByText('Extra Index Equity')).not.toBeInTheDocument();
    expect(screen.queryByText('Non Elite Stock Corp')).not.toBeInTheDocument();

    fireEvent.click(allHeldBtn);

    expect(allHeldBtn).toHaveClass('active');
    expect(allHeldBtn).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByRole('button', { name: /Conviction Matrix \(2\)/i })).toHaveClass('active');
    expect(screen.getByText(/Conviction Verdicts \(2 Stocks\)/i)).toBeInTheDocument();
    expect(screen.getByText('Extra Index Equity')).toBeInTheDocument();
    expect(screen.queryByText('Non Elite Stock Corp')).not.toBeInTheDocument();
    expect(screen.getByText('Elite 50 Held')).toBeInTheDocument();
    expect(screen.getByText('54.0')).toBeInTheDocument();
    expect(screen.getByText('DISTRIBUTION WARNING')).toBeInTheDocument();
    expect(screen.getByText('+2E buy')).toBeInTheDocument();
    expect(screen.getByText('-3E sell')).toBeInTheDocument();
    expect(screen.getByText('-4.2% vs Q-VWAP')).toBeInTheDocument();
    expect(screen.getByText('Via Dossier')).toBeInTheDocument();

    // Expand EXIN row to check index summary cards and Dossier button
    fireEvent.click(screen.getByRole('button', { name: /Expand EXIN conviction pillars/i }));
    expect(screen.getByText('Index Signal')).toBeInTheDocument();
    expect(screen.getByText('Elite 50 QoQ Activity')).toBeInTheDocument();
    expect(screen.getByText('+2 Buy / -3 Sell')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /Open Complete EXIN Stock Dossier/i }));
    expect(onSelectStock).toHaveBeenCalledWith('EXIN');

    // Search filter works on expanded rows
    const searchInput = screen.getByPlaceholderText(/Filter ticker, sub-sector, fund/i);
    fireEvent.change(searchInput, { target: { value: 'Extra Index' } });
    expect(screen.queryByText('Alpha Chips Inc')).not.toBeInTheDocument();
    expect(screen.getByText('Extra Index Equity')).toBeInTheDocument();
    fireEvent.change(searchInput, { target: { value: '' } });

    // Toggling back returns to Curated Signals view cleanly
    fireEvent.click(curatedBtn);
    expect(curatedBtn).toHaveClass('active');
    expect(screen.getByRole('button', { name: /Conviction Matrix \(1\)/i })).toHaveClass('active');
    expect(screen.queryByText('Extra Index Equity')).not.toBeInTheDocument();
  });

  it('paginates All Held Stocks past 150 rows and resets visible slice on search', () => {
    const stocksIndex: Record<string, StockIndexEntry> = {};
    for (let i = 1; i <= 160; i += 1) {
      const tk = `TK${String(i).padStart(3, '0')}`;
      stocksIndex[tk] = makeStockIndexEntry({
        ticker: tk,
        name: `Sample Stock ${i}`,
        holderCount: 5,
        eliteHoldersCount: 1,
        actionabilityScore: 90 - i * 0.1,
        actionVerdict: 'ACCUMULATE ON DIP',
      });
    }

    render(
      <HoldingsTab
        topHoldings={{ filing_lag: [], quarter_end: [] }}
        signalScreener={{
          quarterLabel: '2026 Q3',
          clusterInitiations: [],
          heavyAdds: [],
          smartMoneyExits: [],
          contrarianBets: [],
        }}
        stocksIndex={stocksIndex}
        mode="filing_lag"
        onModeChange={() => {}}
        onSelectFund={() => {}}
        onSelectStock={() => {}}
      />
    );

    fireEvent.click(screen.getByRole('button', { name: /All Held Stocks \(160\)/i }));
    expect(screen.getByText('Showing 150 of 160 matching stocks')).toBeInTheDocument();
    expect(screen.getByText('Sample Stock 1')).toBeInTheDocument();
    expect(screen.queryByText('Sample Stock 160')).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /Show Next 10/i }));
    expect(screen.getByText('Sample Stock 160')).toBeInTheDocument();
    expect(screen.queryByText(/Showing .* matching stocks/i)).not.toBeInTheDocument();

    const searchInput = screen.getByPlaceholderText(/Filter ticker, sub-sector, fund/i);
    fireEvent.change(searchInput, { target: { value: 'TK160' } });
    expect(screen.getByText('Sample Stock 160')).toBeInTheDocument();

    // Clearing search resets visible slice back to 150
    fireEvent.change(searchInput, { target: { value: '' } });
    expect(screen.getByText('Showing 150 of 160 matching stocks')).toBeInTheDocument();
    expect(screen.queryByText('Sample Stock 160')).not.toBeInTheDocument();
  });
});

