import { describe, expect, it } from 'vitest';
import type { IdealPortfolioPosition, StockIndexEntry } from '../types/data';
import {
  buildConstrainedPortfolio,
  computeRebalancePlan,
  parseHoldingsInput,
  resolveCandidateScore,
  type DynamicPosition,
} from '../utils/portfolioMath';

function makePosition(
  ticker: string,
  subSector: string,
  weightPct: number,
  rawScore: number,
  priceUsd = 100
): IdealPortfolioPosition {
  return {
    rank: 1,
    ticker,
    cusip: `CUSIP-${ticker}`,
    name: `${ticker} Corp`,
    sector: 'Technology',
    subSector,
    industry: subSector,
    isTechTmt: true,
    rawScore,
    inflectionScore: rawScore,
    idealWeightPct: weightPct,
    convictionScore: 80,
    holderCount: 12,
    netNew: 2,
    netAdd: 3,
    netTrim: 0,
    avgFundWeightPct: 4.5,
    maxFundWeightPct: 9.0,
    topHolderName: 'Tiger Global',
    rationaleTag: 'Core Consensus',
    topBackers: [],
    stockTtmPct: 24.5,
    priceUsd,
    marketCapUsd: 500_000_000_000,
    marketCapTier: 'Mega >$200B',
  };
}

function makeDynamic(
  ticker: string,
  subSector: string,
  weightPct: number,
  priceUsd = 100
): DynamicPosition {
  return {
    ...makePosition(ticker, subSector, weightPct, weightPct * 10, priceUsd),
    dynamicWeightPct: weightPct,
    weightVsDefaultPct: 0,
  };
}

describe('portfolioMath', () => {
  it('normalizes dual-class aliases and reports unparsed/unknown warnings', () => {
    const known = new Set(['GOOGL', 'BRK-B', 'FOXA', 'NWSA', 'NVDA']);
    const parsed = parseHoldingsInput(
      [
        '# comment ignored',
        'GOOG 20000',
        'BRKB 15%',
        'FOX 5000, NWS 3000',
        'ZZZZ 4000',
        'NOT_A_VALID_LINE',
      ].join('\n'),
      100_000,
      known
    );

    expect(parsed.holdings.get('GOOGL')).toBe(20_000);
    expect(parsed.holdings.get('BRK-B')).toBe(15_000);
    expect(parsed.holdings.get('FOXA')).toBe(5_000);
    expect(parsed.holdings.get('NWSA')).toBe(3_000);
    expect(parsed.holdings.get('ZZZZ')).toBe(4_000);
    expect(parsed.normalizedAliases).toEqual([
      { from: 'GOOG', to: 'GOOGL' },
      { from: 'BRKB', to: 'BRK-B' },
      { from: 'FOX', to: 'FOXA' },
      { from: 'NWS', to: 'NWSA' },
    ]);
    expect(parsed.unknownTickers).toEqual(['ZZZZ']);
    expect(parsed.unparsedLines).toEqual(['NOT_A_VALID_LINE']);
  });

  it('deploys only fresh cash with zero sells/trims in buyOnly mode', () => {
    const activePositions: DynamicPosition[] = [
      makeDynamic('NVDA', 'Semiconductors', 40, 100),
      makeDynamic('MSFT', 'Enterprise Software', 35, 200),
      makeDynamic('META', 'Platforms & Interactive', 25, 250),
    ];
    const holdingsMap = new Map<string, number>([
      ['NVDA', 50_000], // overweight vs 40%
      ['MSFT', 20_000], // underweight vs 35%
      ['INTC', 10_000], // outside target model — must NOT generate EXIT/TRIM in buyOnly
    ]);
    const stocksIndex: Record<string, StockIndexEntry> = {};

    const plan = computeRebalancePlan({
      holdingsMap,
      activePositions,
      stocksIndex,
      capitalUsd: 100_000,
      mode: 'buyOnly',
      newCashUsd: 10_000,
      driftTolerancePct: 0.5,
    });

    expect(plan).not.toBeNull();
    expect(plan!.totalSellUsd).toBe(0);
    expect(plan!.postDeployUsd).toBe(90_000);
    expect(Math.round(plan!.totalBuyUsd)).toBe(10_000);
    expect(plan!.rows.every((r) => r.deltaUsd >= 0)).toBe(true);
    expect(plan!.rows.some((r) => r.action === 'TRIM' || r.action === 'EXIT')).toBe(false);

    const nvdaRow = plan!.rows.find((r) => r.ticker === 'NVDA');
    const intcRow = plan!.rows.find((r) => r.ticker === 'INTC');
    const metaRow = plan!.rows.find((r) => r.ticker === 'META');
    expect(nvdaRow?.action).toBe('HOLD');
    expect(intcRow?.action).toBe('HOLD');
    expect(metaRow?.action).toBe('BUY');
    expect(metaRow!.deltaUsd).toBeGreaterThan(0);
  });

  it('applies driftTolerancePct in full rebalance mode to suppress small churn trades', () => {
    const activePositions: DynamicPosition[] = [
      makeDynamic('NVDA', 'Semiconductors', 50, 100),
      makeDynamic('MSFT', 'Enterprise Software', 50, 100),
    ];
    // Target is $50,000 each; holding $49,700 (0.3% drift) & $50,300 (0.3% drift)
    const holdingsMap = new Map<string, number>([
      ['NVDA', 49_700],
      ['MSFT', 50_300],
    ]);

    const tolerantPlan = computeRebalancePlan({
      holdingsMap,
      activePositions,
      stocksIndex: {},
      capitalUsd: 100_000,
      mode: 'full',
      newCashUsd: 0,
      driftTolerancePct: 0.5,
    });
    expect(tolerantPlan!.rows.every((r) => r.action === 'HOLD')).toBe(true);
    expect(tolerantPlan!.totalBuyUsd).toBe(0);
    expect(tolerantPlan!.totalSellUsd).toBe(0);

    const tightPlan = computeRebalancePlan({
      holdingsMap,
      activePositions,
      stocksIndex: {},
      capitalUsd: 100_000,
      mode: 'full',
      newCashUsd: 0,
      driftTolerancePct: 0.25,
    });
    expect(tightPlan!.rows.find((r) => r.ticker === 'NVDA')?.action).toBe('ADD');
    expect(tightPlan!.rows.find((r) => r.ticker === 'MSFT')?.action).toBe('TRIM');
  });

  it('builds constrained weights summing strictly to 100.00% under stock & sector caps', () => {
    const pool: IdealPortfolioPosition[] = Array.from({ length: 25 }, (_, i) =>
      makePosition(
        `STK${i}`,
        i < 12 ? 'Semiconductors' : i < 20 ? 'Enterprise Software' : 'Platforms & Interactive',
        4,
        100 - i * 3
      )
    );
    const defaultMap = new Map(pool.map((p) => [p.ticker, p.idealWeightPct]));
    const { positions } = buildConstrainedPortfolio(pool, 'ALL', 'rawScore', 20, 8, 35, defaultMap);

    expect(positions).toHaveLength(20);
    const totalWeight = positions.reduce((s, p) => s + p.dynamicWeightPct, 0);
    expect(Math.round(totalWeight * 100) / 100).toBe(100);
    expect(Math.max(...positions.map((p) => p.dynamicWeightPct))).toBeLessThanOrEqual(8.01);
  });

  it('enforces 30% sub-sector cap smoothly via waterfill without prematurely dropping #4/#5 names', () => {
    const subSectors = [
      'Semiconductors',
      'Enterprise Software',
      'Platforms & Interactive',
      'Cloud Infrastructure',
      'Cybersecurity',
    ];
    const pool: IdealPortfolioPosition[] = [];
    // 6 strong Semiconductors names + 5 names in each of 4 other sub-sectors
    for (let i = 0; i < 6; i++) {
      pool.push(makePosition(`SEMI${i}`, 'Semiconductors', 5, 120 - i * 4));
    }
    for (let s = 1; s < subSectors.length; s++) {
      for (let i = 0; i < 5; i++) {
        pool.push(makePosition(`S${s}_${i}`, subSectors[s], 4, 80 - s * 4 - i * 2));
      }
    }
    const defaultMap = new Map(pool.map((p) => [p.ticker, p.idealWeightPct]));
    const { positions } = buildConstrainedPortfolio(pool, 'ALL', 'rawScore', 20, 8, 30, defaultMap);

    expect(positions).toHaveLength(20);
    const semiPositions = positions.filter((p) => p.subSector === 'Semiconductors');
    // Should retain more than floor(30/8)=3 Semiconductors names while capping total sub-sector weight at 30%
    expect(semiPositions.length).toBeGreaterThanOrEqual(5);
    const semiWeight = semiPositions.reduce((s, p) => s + p.dynamicWeightPct, 0);
    expect(semiWeight).toBeLessThanOrEqual(30.05);
    const totalWeight = positions.reduce((s, p) => s + p.dynamicWeightPct, 0);
    expect(Math.round(totalWeight * 100) / 100).toBe(100);
  });

  it('switches ranking/weights for High-Upside Growth, applies legacy fallback guard, and excludes EXIT SIGNAL stocks', () => {
    const eliteGrowth: IdealPortfolioPosition = {
      ...makePosition('PLTR', 'Enterprise Software', 4, 70),
      highUpsideScore: 135,
      highUpsideInflectionScore: 155,
      qualityOverrideActive: true,
      fundamentalQualityScore: 92,
      downsideVolPct: 54,
    };
    const sleepyDef: IdealPortfolioPosition = {
      ...makePosition('DEFV', 'Financial Services', 6, 95),
      isTechTmt: false,
      highUpsideScore: 55,
      highUpsideInflectionScore: 55,
    };
    const exitStock: IdealPortfolioPosition = {
      ...makePosition('EXIT', 'Semiconductors', 5, 200),
      highUpsideScore: 220,
      actionVerdict: 'EXIT SIGNAL',
    };
    const peerSubSectors = ['Cloud Infrastructure', 'Cybersecurity', 'Semiconductors', 'Consumer Internet'];
    const peers = Array.from({ length: 14 }, (_, i) =>
      makePosition(
        `P${i}`,
        peerSubSectors[i % peerSubSectors.length],
        4,
        65 - i
      )
    );
    const pool = [eliteGrowth, sleepyDef, exitStock, ...peers];
    const defaultMap = new Map(pool.map((p) => [p.ticker, p.idealWeightPct]));

    const bal = buildConstrainedPortfolio(pool, 'ALL', 'rawScore', 15, 15, 35, defaultMap, 'balanced');
    const hu = buildConstrainedPortfolio(pool, 'ALL', 'rawScore', 15, 15, 35, defaultMap, 'high_upside');

    expect(bal.positions.some((p) => p.ticker === 'EXIT')).toBe(false);
    expect(hu.positions.some((p) => p.ticker === 'EXIT')).toBe(false);

    const balPltr = bal.positions.find((p) => p.ticker === 'PLTR')!.dynamicWeightPct;
    const balDefv = bal.positions.find((p) => p.ticker === 'DEFV')!.dynamicWeightPct;
    const huPltr = hu.positions.find((p) => p.ticker === 'PLTR')!.dynamicWeightPct;
    const huDefv = hu.positions.find((p) => p.ticker === 'DEFV')!.dynamicWeightPct;
    expect(balDefv).toBeGreaterThan(balPltr);
    expect(huPltr).toBeGreaterThan(huDefv);

    // Legacy snapshot fallback (no highUpsideScore): boosts profitable high-growth tech, damps deep cash burner
    const legacyElite: IdealPortfolioPosition = {
      ...makePosition('LEG1', 'Enterprise Software', 5, 80),
      fundamentals: {
        revenueTtmUsd: 5e9,
        revenueGrowthYoYPct: 65,
        operatingMarginPct: 35,
        fcfMarginPct: 40,
        rdIntensityPct: 14,
        shareDilutionYoYPct: 0.5,
      },
    };
    const legacyBurn: IdealPortfolioPosition = {
      ...makePosition('BURN', 'Cloud Infrastructure', 5, 80),
      fundamentals: {
        revenueTtmUsd: 1e9,
        revenueGrowthYoYPct: 400,
        operatingMarginPct: -110,
        fcfMarginPct: -45,
        rdIntensityPct: 30,
        shareDilutionYoYPct: 18,
      },
    };
    expect(resolveCandidateScore(legacyElite, 'rawScore', 'high_upside')).toBeGreaterThan(
      resolveCandidateScore(legacyElite, 'rawScore', 'balanced')
    );
    expect(resolveCandidateScore(legacyBurn, 'rawScore', 'high_upside')).toBeLessThan(
      resolveCandidateScore(legacyBurn, 'rawScore', 'balanced')
    );
  });
});
