import type {
  ActionVerdict,
  ActionabilityMatrix,
  AlphaDecayStatus,
  ConsensusHolding,
  EntryZoneStatus,
  FundSummary,
  FundTier,
  IdealPortfolioData,
  IdealPortfolioPosition,
  MarketCapTier,
  ReturnMode,
  SignalScreenerData,
  SignalScreenerEntry,
  StockDetail,
  StockFundamentals,
  StockIndexEntry,
  ThesisPersistenceSummary,
} from '../types/data';
import { resolveAlphaDecayMetrics } from './alphaDecay';

export type ConvictionSourceTag =
  | 'Top Consensus'
  | 'Cluster Buy'
  | 'Heavy Add'
  | 'Smart Exit'
  | 'Contrarian'
  | 'Ideal Portfolio'
  | 'Elite 50 Held';

export interface UnifiedConvictionRow {
  ticker: string;
  name: string;
  sector: string;
  subSector: string;
  priceUsd: number | null;
  stockTtmPct: number | null;
  marketCapUsd: number | null;
  marketCapTier: MarketCapTier | null;
  estQuarterVwap: number | null;
  pctVsQuarterVwap: number | null;
  entryZoneStatus: EntryZoneStatus | null;
  fundamentals: StockFundamentals | null;
  holderCount: number;
  eliteHoldersCount: number;
  totalValueUsd: number;
  avgWeightPct: number;
  maxWeightPct: number;
  equalAggregateWeightPct: number | null;
  aumAggregateWeightPct: number | null;
  idealWeightPct: number | null;
  idealPortfolioRank: number | null;
  newCount: number;
  addedCount: number;
  trimmedCount: number;
  exitedCount: number;
  eliteBuyersCount: number;
  eliteSellersCount: number;
  netFlowUsd: number | null;
  netDeltaUsd: number | null;
  isCoreConsensus: boolean;
  isIndexOnly: boolean;
  hasExactPillars: boolean;
  signalVerdict: string | null;
  sourceTags: ConvictionSourceTag[];
  avgEliteHoldingQuarters: number | null;
  filingToTodayPct: number | null;
  qVwapToFilingPct: number | null;
  consensusFilingDate: string | null;
  filingDatePriceUsd: number | null;
  alphaDecayStatus: AlphaDecayStatus;
  alphaDecayReason: string;
  actionability: ActionabilityMatrix;
  actionabilityScore: number;
  actionVerdict: ActionVerdict;
  topHoldersPreview: Array<{
    cik: string;
    fundName: string;
    fundTier: FundTier;
    meta: string;
    chipTone: string;
    subline: string;
  }>;
  alphaInput: SignalScreenerEntry;
}

type ActionabilityInput =
  | ConsensusHolding
  | SignalScreenerEntry
  | IdealPortfolioPosition
  | StockDetail
  | StockIndexEntry;

function interpKnots(val: number, knots: Array<[number, number]>): number {
  if (val <= knots[0][0]) return knots[0][1];
  if (val >= knots[knots.length - 1][0]) return knots[knots.length - 1][1];
  for (let i = 1; i < knots.length; i += 1) {
    const [x0, y0] = knots[i - 1];
    const [x1, y1] = knots[i];
    if (val <= x1) {
      const span = x1 - x0;
      const t = span > 0 ? (val - x0) / span : 0;
      return y0 + t * (y1 - y0);
    }
  }
  return knots[knots.length - 1][1];
}

function tierDefaultSkill(tier: FundTier | string | undefined): number {
  if (tier === 'Elite 50') return 82;
  if (tier === 'Top 100') return 66;
  if (tier === 'Qualified') return 52;
  return 44;
}

function round1(v: number): number {
  return Math.round(v * 10) / 10;
}

function clamp(v: number, lo: number, hi: number): number {
  return Math.min(hi, Math.max(lo, v));
}

export function buildFundSkillMap(
  funds: FundSummary[] | undefined,
  mode: ReturnMode = 'filing_lag'
): Map<string, number> {
  const map = new Map<string, number>();
  if (!funds) return map;
  for (const f of funds) {
    const sk = f.metrics?.[mode]?.skillScore;
    if (sk != null && Number.isFinite(sk)) {
      map.set(f.cik, sk);
    }
  }
  return map;
}

export interface ComputeActionabilityRawParams {
  newCount?: number;
  addedCount?: number;
  trimmedCount?: number;
  exitedCount?: number;
  eliteBuyersCount?: number;
  eliteSellersCount?: number;
  eliteHoldersCount?: number;
  totalValueUsd?: number;
  netEstFlowUsd?: number | null;
  marketCapUsd?: number | null;
  movers?: Array<{
    cik?: string;
    fundTier?: FundTier | string;
    skillScore?: number | null;
    action?: string;
  }>;
  holders?: Array<{
    cik?: string;
    fundTier?: FundTier | string;
    skillScore?: number | null;
    status?: string;
  }>;
  fundSkillByCik?: Map<string, number>;
  pctVsQuarterVwap?: number | null;
  filingToTodayPct?: number | null;
  alphaDecayStatus?: AlphaDecayStatus | null;
  fundamentals?: StockFundamentals | null;
  avgEliteHoldingQuarters?: number | null;
  thesisPersistence?: ThesisPersistenceSummary | null;
  isCoreConsensus?: boolean;
}

/**
 * Compute Unified 4-Pillar Actionable Conviction Matrix (0-100 across 4 × 25pt pillars).
 * Mirrors `compute_actionability_matrix` in `pipeline/build_data.py`.
 */
export function computeActionabilityMatrix(
  params: ComputeActionabilityRawParams
): ActionabilityMatrix {
  const totalBuyers = Math.max(0, Math.trunc((params.newCount ?? 0) + (params.addedCount ?? 0)));
  const totalSellers = Math.max(
    0,
    Math.trunc((params.trimmedCount ?? 0) + (params.exitedCount ?? 0))
  );
  const eliteBuyers = params.eliteBuyersCount ?? 0;
  const eliteSellers = params.eliteSellersCount ?? 0;
  const eliteHolders = params.eliteHoldersCount ?? 0;
  const newCount = params.newCount ?? 0;
  const exitedCount = params.exitedCount ?? 0;
  const totalValueUsd = params.totalValueUsd ?? 0;
  const netEstFlowUsd = params.netEstFlowUsd ?? null;
  const skillMap = params.fundSkillByCik;

  const buyerSkills: number[] = [];
  const sellerSkills: number[] = [];
  const seenMoveCiks = new Set<string>();

  for (const mv of params.movers ?? []) {
    const cik = mv.cik ?? '';
    if (cik) seenMoveCiks.add(cik);
    let sk = mv.skillScore ?? null;
    if (sk == null && cik && skillMap) sk = skillMap.get(cik) ?? null;
    if (sk == null) sk = tierDefaultSkill(mv.fundTier);
    if (mv.action === 'NEW' || mv.action === 'ADDED') buyerSkills.push(sk);
    else if (mv.action === 'TRIMMED' || mv.action === 'EXITED') sellerSkills.push(sk);
  }

  for (const h of params.holders ?? []) {
    const cik = h.cik ?? '';
    if (cik && seenMoveCiks.has(cik)) continue;
    let sk = h.skillScore ?? null;
    if (sk == null && cik && skillMap) sk = skillMap.get(cik) ?? null;
    if (sk == null) sk = tierDefaultSkill(h.fundTier);
    if (h.status === 'NEW' || h.status === 'ADDED') buyerSkills.push(sk);
    else if (h.status === 'TRIMMED') sellerSkills.push(sk);
  }

  const avgBSkill =
    buyerSkills.length > 0
      ? buyerSkills.reduce((a, b) => a + b, 0) / buyerSkills.length
      : eliteBuyers > 0
        ? 82
        : 52;
  const avgSSkill =
    sellerSkills.length > 0
      ? sellerSkills.reduce((a, b) => a + b, 0) / sellerSkills.length
      : eliteSellers > 0
        ? 82
        : 52;
  const avgActiveSkill =
    (avgBSkill * totalBuyers + avgSSkill * totalSellers) / Math.max(1, totalBuyers + totalSellers);

  const buyW = totalBuyers * (0.5 + clamp(avgBSkill, 0, 100) / 100);
  const sellW = totalSellers * (0.5 + clamp(avgSSkill, 0, 100) / 100);
  const totW = buyW + sellW;

  const partScore =
    totW > 0
      ? 6.5 + 6.5 * ((buyW - sellW) / totW) * Math.min(1, totW / 4.5)
      : 6.5;

  const marketCapUsd = params.marketCapUsd ?? null;
  let flowImpulse: number;
  if (netEstFlowUsd != null && Number.isFinite(netEstFlowUsd)) {
    const skillMult = clamp(avgActiveSkill / 55, 0.75, 1.5);
    const effFlow = netEstFlowUsd * skillMult;
    const absTanh = Math.tanh(effFlow / 120_000_000);
    const hasRel = totalValueUsd > 0;
    const relTanh = hasRel
      ? Math.tanh(effFlow / (0.35 * Math.max(totalValueUsd, 25_000_000)))
      : absTanh;
    let flowSignal: number;
    if (marketCapUsd != null && marketCapUsd > 0) {
      const mcapScale = Math.max(marketCapUsd * 0.005, 15_000_000);
      const mcapTanh = Math.tanh(effFlow / mcapScale);
      flowSignal = hasRel
        ? 0.45 * mcapTanh + 0.35 * relTanh + 0.2 * absTanh
        : 0.65 * mcapTanh + 0.35 * absTanh;
    } else if (hasRel) {
      flowSignal = 0.65 * absTanh + 0.35 * relTanh;
    } else {
      flowSignal = absTanh;
    }
    const flowSub = 6.0 + 6.0 * flowSignal;
    flowImpulse = clamp(partScore + flowSub, 0, 25);
  } else {
    flowImpulse = clamp(12.5 + (partScore - 6.5) * (8.5 / 6.5), 0, 25);
  }

  // 2. Price Discipline (0-25): humped curve peaks near institutional Q-VWAP (-10%..-5%)
  // and damps both parabolic chases (>+15%) and severe falling-knife breakdowns (<-20%)
  const pctVsVwap = params.pctVsQuarterVwap ?? null;
  let priceDiscipline =
    pctVsVwap == null
      ? 12.5
      : interpKnots(pctVsVwap, [
          [-45, 7],
          [-30, 12],
          [-20, 18],
          [-10, 24],
          [-5, 25],
          [0, 22],
          [5, 18.5],
          [10, 15],
          [15, 11.5],
          [20, 8.5],
          [30, 5],
          [45, 2.5],
        ]);
  if (params.filingToTodayPct != null && params.filingToTodayPct > 20) {
    priceDiscipline = Math.max(0, priceDiscipline - 1.5);
  }
  if (params.alphaDecayStatus === 'FOMO_TRAP') {
    priceDiscipline = Math.min(5, Math.max(0, priceDiscipline - 2));
  }

  // 3. XBRL Quality Gate (0-25)
  const fund = params.fundamentals ?? null;
  let xbrlQuality: number;
  if (!fund) {
    xbrlQuality = 12.5;
  } else {
    const revYoy = fund.revenueGrowthYoYPct;
    const fcfM = fund.fcfMarginPct;
    const opM = fund.operatingMarginPct;
    const dilYoy = fund.shareDilutionYoYPct;

    const revPts =
      revYoy != null
        ? interpKnots(revYoy, [
            [-20, 0],
            [-10, 2.5],
            [0, 5],
            [10, 7],
            [20, 8.8],
            [40, 11],
          ])
        : 5.5;

    const rawMgn = fcfM != null ? fcfM : opM;
    let mgnPts: number;
    if (rawMgn != null) {
      mgnPts = interpKnots(rawMgn, [
        [-25, 0],
        [-10, 2],
        [0, 4.5],
        [10, 6.4],
        [20, 8.2],
        [35, 10],
      ]);
      if (fcfM == null && mgnPts > 5) {
        mgnPts = 5 + 0.85 * (mgnPts - 5);
      }
    } else {
      mgnPts = 5.0;
    }

    const dilAdj =
      dilYoy != null
        ? interpKnots(dilYoy, [
            [-3, 4],
            [0, 2.5],
            [2, 1],
            [4, -1],
            [8, -4.5],
            [12, -7],
          ])
        : 2.0;

    xbrlQuality = clamp(revPts + mgnPts + dilAdj, 0, 25);
  }

  // 4. Thesis Persistence (0-25)
  const tp = params.thesisPersistence ?? null;
  const hasMultiQ = tp != null && (tp.quartersObserved ?? 0) > 0;
  let streakDelta: number;
  if (hasMultiQ && tp) {
    const cb = tp.consecutiveNetBuyQuarters ?? 0;
    const cs = tp.consecutiveNetSellQuarters ?? 0;
    const nb4 = tp.netBuyQuartersLast4 ?? 0;
    const ns4 = tp.netSellQuartersLast4 ?? 0;
    if (cb >= 4) streakDelta = 9.0;
    else if (cb === 3) streakDelta = 7.5;
    else if (cb === 2) streakDelta = 5.5;
    else if (cb === 1) streakDelta = 2.5 - (ns4 >= 2 ? 1.0 : 0.0);
    else if (cs >= 4) streakDelta = -10.5;
    else if (cs === 3) streakDelta = -8.5;
    else if (cs === 2) streakDelta = -6.5;
    else if (cs === 1) streakDelta = -3.5 - (exitedCount >= 2 ? 1.5 : 0.0);
    else streakDelta = clamp((nb4 - ns4) * 1.5, -4, 4);
  } else {
    const totM = totalBuyers + totalSellers;
    const breadth = totM > 0 ? ((totalBuyers - totalSellers) / totM) * Math.min(1, totM / 4) : 0;
    streakDelta = breadth * 7.5;
    if (exitedCount >= 2 && totalSellers > totalBuyers) streakDelta -= 2.0;
    else if (newCount >= 2 && totalBuyers > totalSellers) streakDelta += 1.5;
    if (params.isCoreConsensus && totalBuyers >= totalSellers) streakDelta += 1.5;
  }

  const avgHold = params.avgEliteHoldingQuarters ?? null;
  let holdDelta = 0;
  if (avgHold != null && avgHold > 0) {
    if (avgHold >= 8) holdDelta = 3.5;
    else if (avgHold >= 4) holdDelta = 2.5;
    else if (avgHold >= 2) holdDelta = 1.0;
    else if (totalSellers > totalBuyers) holdDelta = -1.5;
  } else if (eliteHolders >= 5 && totalBuyers >= totalSellers) {
    holdDelta = 2.0;
  } else if (eliteHolders >= 2 && totalBuyers >= totalSellers) {
    holdDelta = 1.0;
  }

  const persistenceScore = clamp(12.5 + streakDelta + holdDelta, 0, 25);

  const flowR = round1(flowImpulse);
  const priceR = round1(priceDiscipline);
  const xbrlR = round1(xbrlQuality);
  const persR = round1(persistenceScore);
  const totalScore = round1(clamp(flowR + priceR + xbrlR + persR, 0, 100));

  let verdict: ActionVerdict;
  if (totalScore >= 72) verdict = 'STRONG ACCUMULATE';
  else if (totalScore >= 60) verdict = 'ACCUMULATE ON DIP';
  else if (totalScore >= 50) verdict = 'HOLD';
  else if (totalScore >= 35) verdict = 'DISTRIBUTION WARNING';
  else verdict = 'EXIT SIGNAL';

  const hasNegOrFlatFlow = netEstFlowUsd == null || netEstFlowUsd <= 0;
  const hasNegFlow = netEstFlowUsd == null || netEstFlowUsd < 0;
  const hasDistributionEvidence =
    totalSellers > totalBuyers ||
    (netEstFlowUsd != null && netEstFlowUsd < 0) ||
    exitedCount >= 2;
  const hasMultiQSell = (tp?.consecutiveNetSellQuarters ?? 0) >= 2;
  const activePool = Math.max(1, totalBuyers + totalSellers);
  const baseHolders = Math.max(activePool, eliteHolders, (params.holders ?? []).length);
  const exitRatio = exitedCount / Math.max(1, baseHolders + exitedCount);
  const sellDominance = totalSellers / activePool;
  const flowOutRatio =
    netEstFlowUsd != null && netEstFlowUsd < 0 && totalValueUsd > 0
      ? -netEstFlowUsd / totalValueUsd
      : 0;
  const severeExitSignal =
    (exitedCount >= 2 &&
      totalSellers >= totalBuyers + 2 &&
      sellDominance >= 0.65 &&
      (exitRatio >= 0.15 || flowOutRatio >= 0.15) &&
      hasNegOrFlatFlow &&
      totalScore < 58) ||
    (flowR <= 6.0 &&
      persR <= 6.5 &&
      totalSellers >= totalBuyers + 3 &&
      (exitedCount >= 1 || hasMultiQSell) &&
      hasNegFlow);

  let summary: string;
  if (severeExitSignal || (totalScore < 35 && hasDistributionEvidence)) {
    verdict = 'EXIT SIGNAL';
    summary = `Multi-fund institutional distribution (+${totalBuyers} buy vs -${totalSellers} sell, ${exitedCount} exits); capital preservation favors exit.`;
  } else if (
    params.alphaDecayStatus === 'FOMO_TRAP' ||
    (flowR <= 9.5 && totalSellers > totalBuyers && hasNegFlow) ||
    (persR <= 8.5 && totalSellers > totalBuyers) ||
    (totalScore < 50 && hasDistributionEvidence)
  ) {
    verdict = 'DISTRIBUTION WARNING';
    if (params.alphaDecayStatus === 'FOMO_TRAP') {
      summary = `Trading extended above Q-VWAP while smart money distributes (Flow ${flowR}/25, Price ${priceR}/25); avoid chasing strength.`;
    } else {
      summary = `Smart-money outflows & trimming (+${totalBuyers}/-${totalSellers}) outweigh fundamentals (${totalScore}/100); trim risk or wait for stabilization.`;
    }
  } else if (totalScore < 50) {
    verdict = 'HOLD';
    if (priceR < 8 && xbrlR < 9) {
      summary = `Institutional selling is limited (+${totalBuyers}/-${totalSellers}), but extended Q-VWAP pricing (${priceR}/25) & XBRL gate (${xbrlR}/25) hold rating at ${totalScore}/100.`;
    } else if (priceR < 8) {
      summary = `Institutions remain supportive (+${totalBuyers}/-${totalSellers}), but extended Q-VWAP pricing (${priceR}/25) caps reward-to-risk at Hold.`;
    } else if (xbrlR < 9) {
      summary = `Flows are constructive (+${totalBuyers}/-${totalSellers}), but XBRL profitability/dilution gate (${xbrlR}/25) caps conviction at Hold.`;
    } else {
      summary = `Mixed conviction profile (${totalScore}/100: Flow ${flowR}, Price ${priceR}, XBRL ${xbrlR}, Persist ${persR}); hold pending stronger alignment.`;
    }
  } else if (verdict === 'STRONG ACCUMULATE') {
    if (priceR < 12) {
      verdict = 'ACCUMULATE ON DIP';
      summary = `Strong Elite flow (${flowR}/25) & quality (${xbrlR}/25), but price is extended vs. Est. Q-VWAP (Price ${priceR}/25) — stage buys on pullbacks.`;
    } else if (xbrlR < 8) {
      verdict = 'HOLD';
      summary = `Strong institutional buying offset by weak XBRL fundamentals / SBC dilution (XBRL Gate ${xbrlR}/25); monitor execution.`;
    } else if (xbrlR < 10.5 || flowR < 14) {
      verdict = 'ACCUMULATE ON DIP';
      summary = `Constructive multi-pillar profile (${totalScore}/100); prefer staged entries near institutional Q-VWAP.`;
    } else {
      summary = `Aligned 4-pillar conviction (${totalScore}/100): positive skill-weighted inflows (${flowR}/25), disciplined entry (${priceR}/25), XBRL quality (${xbrlR}/25) & persistence (${persR}/25).`;
    }
  } else if (verdict === 'ACCUMULATE ON DIP') {
    if (xbrlR < 7.5) {
      verdict = 'HOLD';
      summary = `Institutional interest present, but XBRL profitability/dilution gate (${xbrlR}/25) warrants patience at Hold.`;
    } else if (priceR < 12) {
      summary = `High smart-money conviction capped by extended price vs. Q-VWAP (${priceR}/25); target pullbacks toward institutional cost basis.`;
    } else {
      summary = `Constructive accumulation setup (${totalScore}/100) near favorable Q-VWAP cost basis (${priceR}/25).`;
    }
  } else {
    summary = `Balanced signal across pillars (${totalScore}/100: Flow ${flowR}, Price ${priceR}, XBRL ${xbrlR}, Persist ${persR}); maintain current sizing.`;
  }

  return {
    score: totalScore,
    verdict,
    pillars: {
      flowImpulse: flowR,
      priceDiscipline: priceR,
      xbrlQuality: xbrlR,
      thesisPersistence: persR,
    },
    summary,
    thesisPersistence: tp,
  };
}

export function resolveActionabilityMatrix(
  input: ActionabilityInput,
  options?: {
    fundSkillByCik?: Map<string, number>;
    latestQuarterLabel?: string;
    isCoreConsensus?: boolean;
    fallbackFlowUsd?: number | null;
    fallbackBuyers?: { newCount: number; addedCount: number; trimmedCount: number; exitedCount: number } | null;
    fallbackFundamentals?: StockFundamentals | null;
    fallbackActionability?: ActionabilityMatrix | null;
  }
): ActionabilityMatrix {
  if ('actionability' in input && input.actionability) {
    return input.actionability;
  }
  if (options?.fallbackActionability) {
    return options.fallbackActionability;
  }

  const ad = resolveAlphaDecayMetrics(input, { latestQuarterLabel: options?.latestQuarterLabel });
  const fundamentals =
    'fundamentals' in input ? (input.fundamentals ?? options?.fallbackFundamentals ?? null) : (options?.fallbackFundamentals ?? null);
  const totalValueUsd = 'totalValueUsd' in input ? input.totalValueUsd : 0;

  let newCount = 0;
  let addedCount = 0;
  let trimmedCount = 0;
  let exitedCount = 0;
  let eliteHoldersCount = 'eliteHoldersCount' in input ? input.eliteHoldersCount : 0;
  let netEstFlowUsd: number | null = options?.fallbackFlowUsd ?? null;
  let movers: ComputeActionabilityRawParams['movers'] = [];
  let holders: ComputeActionabilityRawParams['holders'] = [];

  if ('recentMoves' in input) {
    newCount = input.newCount;
    addedCount = input.addedCount;
    trimmedCount = input.trimmedCount;
    exitedCount = input.exitedCount;
    movers = input.recentMoves;
    holders = input.holders;
    if (input.netEstFlowUsd != null) {
      netEstFlowUsd = input.netEstFlowUsd;
    } else if (options?.fallbackFlowUsd == null && input.recentMoves.some((m) => m.estFlowUsd != null)) {
      netEstFlowUsd = input.recentMoves.reduce((s, m) => s + (m.estFlowUsd ?? 0), 0);
    }
  } else if ('movers' in input) {
    newCount = input.newCount;
    addedCount = input.addedCount;
    trimmedCount = input.trimmedCount;
    exitedCount = input.exitedCount;
    movers = input.movers;
    holders = input.topBackers;
    netEstFlowUsd = input.netEstFlowUsd ?? input.netDeltaUsd ?? options?.fallbackFlowUsd ?? null;
  } else if ('idealWeightPct' in input) {
    newCount = input.netNew;
    addedCount = input.netAdd;
    trimmedCount = input.netTrim;
    exitedCount = input.netExit ?? 0;
    eliteHoldersCount = input.holderCount;
    holders = input.topBackers.map((b) => ({
      cik: b.cik,
      fundTier: 'Elite 50',
      status: b.status,
    }));
  } else if ('holders' in input) {
    holders = input.holders;
    const hNew = input.holders.filter((h) => h.status === 'NEW').length;
    const hAdd = input.holders.filter((h) => h.status === 'ADDED').length;
    const hTrim = input.holders.filter((h) => h.status === 'TRIMMED').length;
    newCount = hNew;
    addedCount = Math.max(hAdd, Math.max(0, input.netBuyers - hNew));
    trimmedCount = Math.max(hTrim, input.netTrimmers);
    exitedCount = options?.fallbackBuyers?.exitedCount ?? 0;
  } else if ('signalVerdict' in input) {
    newCount = options?.fallbackBuyers?.newCount ?? (ad.totalBuyersCount > 0 ? Math.min(2, ad.totalBuyersCount) : 0);
    addedCount = options?.fallbackBuyers?.addedCount ?? Math.max(0, ad.totalBuyersCount - newCount);
    trimmedCount = options?.fallbackBuyers?.trimmedCount ?? ad.totalSellersCount;
    exitedCount = options?.fallbackBuyers?.exitedCount ?? 0;
  }

  let tpSummary: ThesisPersistenceSummary | null = null;
  if (
    'consecutiveNetBuyQuarters' in input &&
    (input.consecutiveNetBuyQuarters != null || input.consecutiveNetSellQuarters != null)
  ) {
    const cb = input.consecutiveNetBuyQuarters ?? 0;
    const cs = input.consecutiveNetSellQuarters ?? 0;
    tpSummary = {
      consecutiveNetBuyQuarters: cb,
      consecutiveNetSellQuarters: cs,
      netBuyQuartersLast4: cb,
      netSellQuartersLast4: cs,
      quartersObserved: Math.max(cb, cs),
      direction: cb > 0 ? 'ACCUMULATION' : cs > 0 ? 'DISTRIBUTION' : 'MIXED',
      summary:
        cb >= 2
          ? `${cb}Q consecutive net buying`
          : cs >= 2
            ? `${cs}Q consecutive net distribution`
            : 'Single-quarter flow signal',
    };
  }

  const marketCapUsd = 'marketCapUsd' in input ? (input.marketCapUsd ?? null) : null;

  return computeActionabilityMatrix({
    newCount,
    addedCount,
    trimmedCount,
    exitedCount,
    eliteBuyersCount: ad.eliteBuyersCount,
    eliteSellersCount: ad.eliteSellersCount,
    eliteHoldersCount,
    totalValueUsd,
    netEstFlowUsd,
    marketCapUsd,
    movers,
    holders,
    fundSkillByCik: options?.fundSkillByCik,
    pctVsQuarterVwap: ad.pctVsQuarterVwap,
    filingToTodayPct: ad.filingToTodayPct,
    alphaDecayStatus: ad.status,
    fundamentals,
    avgEliteHoldingQuarters: ad.avgEliteHoldingQuarters,
    thesisPersistence: tpSummary,
    isCoreConsensus: options?.isCoreConsensus || eliteHoldersCount >= 5,
  });
}

export function buildUnifiedConvictionList({
  topHoldings,
  signalScreener,
  idealPortfolio,
  stocksIndex,
  funds,
  mode,
  includeAllEliteHeld = false,
}: {
  topHoldings: Record<ReturnMode, ConsensusHolding[]>;
  signalScreener: SignalScreenerData;
  idealPortfolio?: IdealPortfolioData | null;
  stocksIndex: Record<string, StockIndexEntry>;
  funds?: FundSummary[];
  mode: ReturnMode;
  includeAllEliteHeld?: boolean;
}): UnifiedConvictionRow[] {
  const skillMap = buildFundSkillMap(funds, mode);
  const consensusList = topHoldings[mode] ?? [];
  const quarterLabel = signalScreener.quarterLabel;

  interface AccRec {
    ticker: string;
    consensus?: ConsensusHolding;
    screenerEntries: Array<{ tag: ConvictionSourceTag; entry: SignalScreenerEntry }>;
    idealPos?: IdealPortfolioPosition;
    idealCandidate?: IdealPortfolioPosition;
  }

  const byTicker = new Map<string, AccRec>();
  const ensure = (ticker: string): AccRec => {
    let cur = byTicker.get(ticker);
    if (!cur) {
      cur = { ticker, screenerEntries: [] };
      byTicker.set(ticker, cur);
    }
    return cur;
  };

  for (const item of consensusList) {
    ensure(item.ticker).consensus = item;
  }
  for (const entry of signalScreener.clusterInitiations) {
    ensure(entry.ticker).screenerEntries.push({ tag: 'Cluster Buy', entry });
  }
  for (const entry of signalScreener.heavyAdds) {
    ensure(entry.ticker).screenerEntries.push({ tag: 'Heavy Add', entry });
  }
  for (const entry of signalScreener.smartMoneyExits) {
    ensure(entry.ticker).screenerEntries.push({ tag: 'Smart Exit', entry });
  }
  for (const entry of signalScreener.contrarianBets) {
    ensure(entry.ticker).screenerEntries.push({ tag: 'Contrarian', entry });
  }
  for (const pos of idealPortfolio?.positions ?? []) {
    const r = ensure(pos.ticker);
    r.idealPos = pos;
    r.idealCandidate = pos;
  }
  for (const cand of idealPortfolio?.candidatePool ?? []) {
    const r = ensure(cand.ticker);
    if (!r.idealCandidate) r.idealCandidate = cand;
  }

  if (includeAllEliteHeld) {
    for (const [key, entry] of Object.entries(stocksIndex)) {
      const tk = (entry.ticker || key).trim();
      if (!tk) continue;
      if ((entry.eliteHoldersCount ?? 0) < 1 || entry.holderCount <= 0) continue;
      const tkUpper = tk.toUpperCase();
      if (byTicker.has(tk) || byTicker.has(key) || byTicker.has(tkUpper)) continue;
      ensure(tk);
    }
  }

  interface IndexedFundMatch {
    cik: string;
    fundName: string;
    fundTier: FundTier;
    skillScore: number | null;
    weightPct: number | null;
  }
  const indexedHoldersByTicker = new Map<string, IndexedFundMatch[]>();
  if (includeAllEliteHeld && funds && funds.length > 0) {
    const tierOrder = (t: FundTier | string) =>
      t === 'Elite 50' ? 0 : t === 'Top 100' ? 1 : t === 'Qualified' ? 2 : 3;
    const sortedFunds = [...funds].sort((a, b) => {
      const td = tierOrder(a.fundTier) - tierOrder(b.fundTier);
      if (td !== 0) return td;
      return (b.metrics?.[mode]?.skillScore ?? -1) - (a.metrics?.[mode]?.skillScore ?? -1);
    });
    for (const f of sortedFunds) {
      const previewWtByTicker = new Map<string, number>();
      const fundTickers = new Set<string>();
      for (const hp of f.topHoldingsPreview ?? []) {
        if (!hp.ticker) continue;
        const up = hp.ticker.toUpperCase();
        previewWtByTicker.set(up, hp.weightPct);
        fundTickers.add(up);
      }
      for (const tk of f.searchableTickers ?? []) {
        if (tk) fundTickers.add(tk.toUpperCase());
      }
      const sk = f.metrics?.[mode]?.skillScore ?? null;
      for (const tkUp of fundTickers) {
        let arr = indexedHoldersByTicker.get(tkUp);
        if (!arr) {
          arr = [];
          indexedHoldersByTicker.set(tkUp, arr);
        }
        if (arr.length < 8) {
          arr.push({
            cik: f.cik,
            fundName: f.name,
            fundTier: f.fundTier,
            skillScore: sk,
            weightPct: previewWtByTicker.get(tkUp) ?? null,
          });
        }
      }
    }
  }

  const out: UnifiedConvictionRow[] = [];

  for (const [ticker, rec] of byTicker.entries()) {
    const idxEntry = stocksIndex[ticker] ?? stocksIndex[ticker.toUpperCase()];
    const c = rec.consensus;
    const ideal = rec.idealPos ?? rec.idealCandidate;
    // Choose the screener entry with the broadest movers + absolute flow metadata
    const bestScreener =
      rec.screenerEntries.length > 0
        ? [...rec.screenerEntries].sort(
            (a, b) =>
              b.entry.movers.length - a.entry.movers.length ||
              Math.abs(b.entry.netEstFlowUsd ?? b.entry.netDeltaUsd) -
                Math.abs(a.entry.netEstFlowUsd ?? a.entry.netDeltaUsd)
          )[0].entry
        : undefined;
    const isIndexOnly = !c && !bestScreener && !ideal;

    const sourceTags: ConvictionSourceTag[] = [];
    if (c) sourceTags.push('Top Consensus');
    for (const se of rec.screenerEntries) {
      if (!sourceTags.includes(se.tag)) sourceTags.push(se.tag);
    }
    if (rec.idealPos) sourceTags.push('Ideal Portfolio');
    if (isIndexOnly) sourceTags.push('Elite 50 Held');

    const backendActionability =
      bestScreener?.actionability ??
      c?.actionability ??
      ideal?.actionability ??
      null;

    const fundamentals =
      bestScreener?.fundamentals ??
      c?.fundamentals ??
      ideal?.fundamentals ??
      null;

    const estQuarterVwap =
      bestScreener?.estQuarterVwap ??
      c?.estQuarterVwap ??
      ideal?.estQuarterVwap ??
      null;
    const pctVsQuarterVwap =
      bestScreener?.pctVsQuarterVwap ??
      c?.pctVsQuarterVwap ??
      ideal?.pctVsQuarterVwap ??
      idxEntry?.pctVsQuarterVwap ??
      null;
    const entryZoneStatus =
      bestScreener?.entryZoneStatus ??
      c?.entryZoneStatus ??
      ideal?.entryZoneStatus ??
      idxEntry?.entryZoneStatus ??
      null;
    const priceUsd =
      bestScreener?.priceUsd ??
      c?.priceUsd ??
      ideal?.priceUsd ??
      idxEntry?.priceUsd ??
      null;
    const stockTtmPct =
      bestScreener?.stockTtmPct ??
      c?.stockTtmPct ??
      ideal?.stockTtmPct ??
      null;
    const marketCapUsd =
      bestScreener?.marketCapUsd ??
      c?.marketCapUsd ??
      ideal?.marketCapUsd ??
      null;
    const marketCapTier =
      bestScreener?.marketCapTier ??
      c?.marketCapTier ??
      ideal?.marketCapTier ??
      idxEntry?.marketCapTier ??
      null;

    const consensusEliteCount = c
      ? c.holders.filter((h) => h.fundTier === 'Elite 50').length
      : 0;
    const eliteHoldersCount = Math.max(
      bestScreener?.eliteHoldersCount ?? 0,
      consensusEliteCount,
      ideal?.holderCount ?? 0,
      idxEntry?.eliteHoldersCount ?? 0
    );
    const holderCount = Math.max(
      bestScreener?.holderCount ?? 0,
      c?.holderCount ?? 0,
      idxEntry?.holderCount ?? 0,
      eliteHoldersCount
    );
    const totalValueUsd = bestScreener?.totalValueUsd ?? c?.totalValueUsd ?? 0;
    const avgWeightPct =
      bestScreener?.avgWeightPct ?? c?.avgHolderWeightPct ?? ideal?.avgFundWeightPct ?? 0;
    const maxWeightPct = bestScreener?.maxWeightPct ?? ideal?.maxFundWeightPct ?? avgWeightPct;

    let newCount = 0;
    let addedCount = 0;
    let trimmedCount = 0;
    let exitedCount = 0;
    let eliteBuyersCount = 0;
    let eliteSellersCount = 0;

    if (bestScreener) {
      newCount = bestScreener.newCount;
      addedCount = bestScreener.addedCount;
      trimmedCount = bestScreener.trimmedCount;
      exitedCount = bestScreener.exitedCount;

      const seenCiks = new Set<string>();
      for (const mv of bestScreener.movers) {
        seenCiks.add(mv.cik);
        if (mv.fundTier !== 'Elite 50') continue;
        if (mv.action === 'NEW' || mv.action === 'ADDED') eliteBuyersCount += 1;
        else if (mv.action === 'TRIMMED' || mv.action === 'EXITED') eliteSellersCount += 1;
      }
      if (c) {
        for (const h of c.holders) {
          if (seenCiks.has(h.cik) || h.fundTier !== 'Elite 50') continue;
          if (h.status === 'NEW' || h.status === 'ADDED') eliteBuyersCount += 1;
          else if (h.status === 'TRIMMED') eliteSellersCount += 1;
        }
      }
      if (bestScreener.eliteBuyersCount != null) {
        eliteBuyersCount = Math.max(eliteBuyersCount, bestScreener.eliteBuyersCount);
      } else {
        eliteBuyersCount = Math.max(eliteBuyersCount, bestScreener.eliteNewCount);
      }
      if (bestScreener.eliteSellersCount != null) {
        eliteSellersCount = Math.max(eliteSellersCount, bestScreener.eliteSellersCount);
      }
    } else if (c) {
      const hNew = c.holders.filter((h) => h.status === 'NEW').length;
      const hAdd = c.holders.filter((h) => h.status === 'ADDED').length;
      const hTrim = c.holders.filter((h) => h.status === 'TRIMMED').length;
      newCount = Math.max(hNew, ideal?.netNew ?? 0);
      addedCount = Math.max(hAdd, ideal?.netAdd ?? 0, Math.max(0, c.netBuyers - newCount));
      trimmedCount = Math.max(hTrim, ideal?.netTrim ?? 0, c.netTrimmers);
      exitedCount = 0;

      let eb = 0;
      let es = 0;
      for (const h of c.holders) {
        if (h.fundTier !== 'Elite 50') continue;
        if (h.status === 'NEW' || h.status === 'ADDED') eb += 1;
        else if (h.status === 'TRIMMED') es += 1;
      }
      eliteBuyersCount = c.eliteBuyersCount ?? Math.max(eb, ideal ? ideal.netNew + ideal.netAdd : 0);
      eliteSellersCount = c.eliteSellersCount ?? Math.max(es, ideal?.netTrim ?? 0);
    } else if (ideal) {
      newCount = ideal.netNew;
      addedCount = ideal.netAdd;
      trimmedCount = ideal.netTrim;
      exitedCount = 0;
      eliteBuyersCount = ideal.eliteBuyersCount ?? ideal.netNew + ideal.netAdd;
      eliteSellersCount = ideal.eliteSellersCount ?? ideal.netTrim;
    } else if (isIndexOnly && idxEntry) {
      eliteBuyersCount = idxEntry.eliteBuyersCount ?? 0;
      eliteSellersCount = idxEntry.eliteSellersCount ?? 0;
    }

    const netFlowUsd =
      bestScreener != null ? (bestScreener.netEstFlowUsd ?? bestScreener.netDeltaUsd) : null;
    const netDeltaUsd = bestScreener?.netDeltaUsd ?? null;

    // Combine movers (from screener) and holders (from consensus / ideal / screener)
    const combinedMovers: SignalScreenerEntry['movers'] = [...(bestScreener?.movers ?? [])];
    const combinedHolders: SignalScreenerEntry['topBackers'] = [];
    const seenMoverCiks = new Set(combinedMovers.map((m) => m.cik));
    const seenHolderCiks = new Set<string>();

    if (c) {
      for (const h of c.holders) {
        if (!seenHolderCiks.has(h.cik)) {
          seenHolderCiks.add(h.cik);
          combinedHolders.push({
            cik: h.cik,
            fundName: h.fundName,
            fundTier: h.fundTier,
            weightPct: h.weightPct,
            status: h.status,
          });
        }
        if (!seenMoverCiks.has(h.cik) && h.status !== 'UNCHANGED') {
          seenMoverCiks.add(h.cik);
          combinedMovers.push({
            cik: h.cik,
            fundName: h.fundName,
            fundTier: h.fundTier,
            action: h.status,
            currBookPct: h.weightPct,
            prevBookPct: h.weightPct,
            valueDeltaUsd: 0,
            sharesDeltaPct: h.qoqSharesChangePct,
          });
        }
      }
    }
    if (bestScreener) {
      for (const bk of bestScreener.topBackers) {
        if (!seenHolderCiks.has(bk.cik)) {
          seenHolderCiks.add(bk.cik);
          combinedHolders.push(bk);
        }
      }
    }
    if (ideal) {
      for (const bk of ideal.topBackers) {
        if (!seenHolderCiks.has(bk.cik)) {
          seenHolderCiks.add(bk.cik);
          const normStatus =
            bk.status === 'NEW' || bk.status === 'ADDED' || bk.status === 'TRIMMED'
              ? bk.status
              : 'UNCHANGED';
          combinedHolders.push({
            cik: bk.cik,
            fundName: bk.fundName,
            fundTier: 'Elite 50',
            weightPct: bk.weightPct,
            status: normStatus,
          });
        }
      }
    }

    const avgEliteHoldingQuarters =
      bestScreener?.avgEliteHoldingQuarters ??
      c?.avgEliteHoldingQuarters ??
      ideal?.avgEliteHoldingQuarters ??
      idxEntry?.avgEliteHoldingQuarters ??
      null;
    const consensusFilingDate =
      bestScreener?.consensusFilingDate ??
      c?.consensusFilingDate ??
      ideal?.consensusFilingDate ??
      idxEntry?.consensusFilingDate ??
      null;
    const filingDatePriceUsd =
      bestScreener?.filingDatePriceUsd ??
      c?.filingDatePriceUsd ??
      ideal?.filingDatePriceUsd ??
      idxEntry?.filingDatePriceUsd ??
      null;
    const qVwapToFilingPct =
      bestScreener?.qVwapToFilingPct ??
      c?.qVwapToFilingPct ??
      ideal?.qVwapToFilingPct ??
      idxEntry?.qVwapToFilingPct ??
      null;
    const filingToTodayPct =
      bestScreener?.filingToTodayPct ??
      c?.filingToTodayPct ??
      ideal?.filingToTodayPct ??
      idxEntry?.filingToTodayPct ??
      null;

    const alphaInput: SignalScreenerEntry = {
      ticker,
      name: bestScreener?.name ?? c?.name ?? ideal?.name ?? idxEntry?.name ?? ticker,
      sector: bestScreener?.sector ?? c?.sector ?? ideal?.sector ?? idxEntry?.sector ?? 'Other',
      subSector:
        bestScreener?.subSector ?? c?.subSector ?? ideal?.subSector ?? idxEntry?.subSector ?? 'Other',
      isTechTmt: bestScreener?.isTechTmt ?? c?.isTechTmt ?? ideal?.isTechTmt ?? true,
      priceUsd,
      stockTtmPct,
      holderCount,
      eliteHoldersCount,
      totalValueUsd,
      avgWeightPct,
      maxWeightPct,
      newCount,
      eliteNewCount: bestScreener?.eliteNewCount ?? ideal?.netNew ?? 0,
      addedCount,
      trimmedCount,
      exitedCount,
      netDeltaUsd: netDeltaUsd ?? 0,
      netEstFlowUsd: netFlowUsd ?? undefined,
      movers: combinedMovers,
      topBackers: combinedHolders,
      marketCapUsd,
      marketCapTier,
      estQuarterVwap,
      pctVsQuarterVwap,
      entryZoneStatus,
      fundamentals,
      consensusFilingDate,
      filingDatePriceUsd,
      qVwapToFilingPct,
      filingToTodayPct,
      avgEliteHoldingQuarters,
      eliteBuyersCount,
      eliteSellersCount,
      alphaDecayStatus:
        bestScreener?.alphaDecayStatus ??
        c?.alphaDecayStatus ??
        ideal?.alphaDecayStatus ??
        idxEntry?.alphaDecayStatus ??
        null,
      alphaDecayReason:
        bestScreener?.alphaDecayReason ??
        c?.alphaDecayReason ??
        ideal?.alphaDecayReason ??
        null,
      actionability: backendActionability,
    };

    const ad =
      isIndexOnly && idxEntry
        ? resolveAlphaDecayMetrics(idxEntry, { latestQuarterLabel: quarterLabel })
        : resolveAlphaDecayMetrics(alphaInput, { latestQuarterLabel: quarterLabel });

    let tpFallback: ThesisPersistenceSummary | null = null;
    if (
      !backendActionability &&
      idxEntry &&
      (idxEntry.consecutiveNetBuyQuarters != null || idxEntry.consecutiveNetSellQuarters != null)
    ) {
      const cb = idxEntry.consecutiveNetBuyQuarters ?? 0;
      const cs = idxEntry.consecutiveNetSellQuarters ?? 0;
      tpFallback = {
        consecutiveNetBuyQuarters: cb,
        consecutiveNetSellQuarters: cs,
        netBuyQuartersLast4: cb,
        netSellQuartersLast4: cs,
        quartersObserved: Math.max(cb, cs),
        direction: cb > 0 ? 'ACCUMULATION' : cs > 0 ? 'DISTRIBUTION' : 'MIXED',
        summary: cb >= 2 ? `${cb}Q consecutive net buying` : cs >= 2 ? `${cs}Q consecutive net distribution` : 'Single-quarter flow signal',
      };
    }

    const fallbackMatrix = computeActionabilityMatrix({
      newCount: isIndexOnly ? eliteBuyersCount : newCount,
      addedCount,
      trimmedCount: isIndexOnly ? eliteSellersCount : trimmedCount,
      exitedCount,
      eliteBuyersCount,
      eliteSellersCount,
      eliteHoldersCount,
      totalValueUsd,
      netEstFlowUsd: netFlowUsd,
      marketCapUsd,
      movers: combinedMovers,
      holders: combinedHolders,
      fundSkillByCik: skillMap,
      pctVsQuarterVwap: ad.pctVsQuarterVwap,
      filingToTodayPct: ad.filingToTodayPct,
      alphaDecayStatus: ad.status,
      fundamentals,
      avgEliteHoldingQuarters: ad.avgEliteHoldingQuarters,
      thesisPersistence: tpFallback,
      isCoreConsensus: Boolean(c) || eliteHoldersCount >= 5,
    });

    let actionability: ActionabilityMatrix;
    let hasExactPillars: boolean;
    if (backendActionability) {
      actionability = backendActionability;
      hasExactPillars = true;
    } else if (isIndexOnly) {
      hasExactPillars = false;
      const indexScore = idxEntry?.actionabilityScore ?? fallbackMatrix.score;
      const indexVerdict = idxEntry?.actionVerdict ?? fallbackMatrix.verdict;
      const sigVerdict = idxEntry?.signalVerdict ?? 'Tracked Equity';
      const qoqNote =
        eliteBuyersCount > 0 || eliteSellersCount > 0
          ? ` · +${eliteBuyersCount}/-${eliteSellersCount} Elite QoQ`
          : '';
      actionability = {
        ...fallbackMatrix,
        score: indexScore,
        verdict: indexVerdict,
        summary: `${sigVerdict} across ${holderCount} funds (${eliteHoldersCount} Elite 50${qoqNote}). Open Complete Stock Dossier for the full 4-pillar Flow, Price, XBRL & Persistence breakdown.`,
      };
    } else {
      actionability = fallbackMatrix;
      hasExactPillars = true;
    }

    const indexedMatches = isIndexOnly
      ? indexedHoldersByTicker.get(ticker.toUpperCase()) ?? []
      : [];

    const topHoldersPreview: UnifiedConvictionRow['topHoldersPreview'] =
      combinedMovers.length > 0
        ? combinedMovers.slice(0, 6).map((m) => ({
            cik: m.cik,
            fundName: m.fundName,
            fundTier: m.fundTier,
            meta:
              m.action === 'NEW'
                ? `NEW ${m.currBookPct.toFixed(1)}%`
                : m.action === 'EXITED'
                  ? 'EXIT'
                  : `${m.action} ${m.sharesDeltaPct != null ? `${m.sharesDeltaPct > 0 ? '+' : ''}${m.sharesDeltaPct.toFixed(0)}%` : ''}`,
            chipTone: m.action === 'NEW' || m.action === 'ADDED' ? 'pos' : 'neg',
            subline: `Book ${m.prevBookPct.toFixed(1)}% → ${m.currBookPct.toFixed(1)}%`,
          }))
        : combinedHolders.length > 0
          ? combinedHolders.slice(0, 6).map((b) => ({
              cik: b.cik,
              fundName: b.fundName,
              fundTier: b.fundTier,
              meta: `${b.weightPct.toFixed(1)}%`,
              chipTone:
                b.status === 'NEW' || b.status === 'ADDED'
                  ? 'pos'
                  : b.status === 'TRIMMED'
                    ? 'neg'
                    : b.fundTier === 'Elite 50'
                      ? 'elite'
                      : '',
              subline: `Status: ${b.status}`,
            }))
          : indexedMatches.slice(0, 6).map((fh) => ({
              cik: fh.cik,
              fundName: fh.fundName,
              fundTier: fh.fundTier,
              meta:
                fh.weightPct != null
                  ? `${fh.weightPct.toFixed(1)}%`
                  : fh.skillScore != null
                    ? `Skill ${fh.skillScore.toFixed(0)}`
                    : fh.fundTier,
              chipTone: fh.fundTier === 'Elite 50' ? 'elite' : '',
              subline:
                fh.weightPct != null
                  ? `Top holding (${fh.weightPct.toFixed(2)}% weight)`
                  : `Held in Q filing${fh.skillScore != null ? ` · Skill ${fh.skillScore.toFixed(0)}` : ''}`,
            }));

    out.push({
      ticker,
      name: alphaInput.name,
      sector: alphaInput.sector,
      subSector: alphaInput.subSector,
      priceUsd,
      stockTtmPct,
      marketCapUsd,
      marketCapTier,
      estQuarterVwap,
      pctVsQuarterVwap: ad.pctVsQuarterVwap,
      entryZoneStatus,
      fundamentals,
      holderCount,
      eliteHoldersCount,
      totalValueUsd,
      avgWeightPct,
      maxWeightPct,
      equalAggregateWeightPct: c?.equalAggregateWeightPct ?? null,
      aumAggregateWeightPct: c?.aumAggregateWeightPct ?? null,
      idealWeightPct: rec.idealPos?.idealWeightPct ?? null,
      idealPortfolioRank: rec.idealPos?.rank ?? null,
      newCount,
      addedCount,
      trimmedCount,
      exitedCount,
      eliteBuyersCount,
      eliteSellersCount,
      netFlowUsd,
      netDeltaUsd,
      isCoreConsensus: Boolean(c) || eliteHoldersCount >= 5,
      isIndexOnly,
      hasExactPillars,
      signalVerdict: idxEntry?.signalVerdict ?? null,
      sourceTags,
      avgEliteHoldingQuarters: ad.avgEliteHoldingQuarters,
      filingToTodayPct: ad.filingToTodayPct,
      qVwapToFilingPct: ad.qVwapToFilingPct,
      consensusFilingDate: ad.consensusFilingDate,
      filingDatePriceUsd: ad.filingDatePriceUsd,
      alphaDecayStatus: ad.status,
      alphaDecayReason: ad.reason,
      actionability,
      actionabilityScore: actionability.score,
      actionVerdict: actionability.verdict,
      topHoldersPreview,
      alphaInput,
    });
  }

  out.sort(
    (a, b) =>
      b.actionabilityScore - a.actionabilityScore ||
      b.eliteHoldersCount - a.eliteHoldersCount ||
      b.holderCount - a.holderCount ||
      b.totalValueUsd - a.totalValueUsd
  );

  return out;
}

export function buildConvictionLookupMap(
  rows: UnifiedConvictionRow[]
): Record<string, UnifiedConvictionRow> {
  const rec: Record<string, UnifiedConvictionRow> = {};
  for (const r of rows) {
    rec[r.ticker] = r;
  }
  return rec;
}
