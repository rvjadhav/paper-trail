import type {
  AlphaDecayStatus,
  ConsensusHolding,
  FundTier,
  IdealPortfolioPosition,
  MoveAction,
  SignalScreenerEntry,
  StockDetail,
  StockIndexEntry,
} from '../types/data';

export interface ResolvedAlphaDecayMetrics {
  consensusFilingDate: string | null;
  estQuarterVwap: number | null;
  filingDatePriceUsd: number | null;
  todayPriceUsd: number | null;
  qVwapToFilingPct: number | null;
  filingToTodayPct: number | null;
  pctVsQuarterVwap: number | null;
  avgEliteHoldingQuarters: number | null;
  eliteBuyersCount: number;
  eliteSellersCount: number;
  totalBuyersCount: number;
  totalSellersCount: number;
  status: AlphaDecayStatus;
  reason: string;
  holdingHorizonLabel: string;
}

type AlphaDecayInput =
  | ConsensusHolding
  | SignalScreenerEntry
  | IdealPortfolioPosition
  | StockDetail
  | StockIndexEntry;

export function inferStatutoryDeadlineFromQuarterLabel(quarterLabel?: string): string | null {
  if (!quarterLabel) return null;
  const m = quarterLabel.match(/^(\d{4})\s+Q([1-4])$/i);
  if (!m) return null;
  const year = Number(m[1]);
  const q = Number(m[2]);
  const periodEndMonth = q * 3;
  const periodEndDay = periodEndMonth === 3 || periodEndMonth === 12 ? 31 : 30;
  const dt = new Date(Date.UTC(year, periodEndMonth - 1, periodEndDay));
  dt.setUTCDate(dt.getUTCDate() + 45);
  const dow = dt.getUTCDay();
  if (dow === 6) dt.setUTCDate(dt.getUTCDate() + 2);
  else if (dow === 0) dt.setUTCDate(dt.getUTCDate() + 1);
  return dt.toISOString().slice(0, 10);
}

export function formatHoldingPeriodLabel(avgQuarters: number | null | undefined): string {
  if (avgQuarters == null || !Number.isFinite(avgQuarters) || avgQuarters <= 0) {
    return '—';
  }
  if (avgQuarters >= 8) return `${avgQuarters.toFixed(1)}Q · Core Compounder`;
  if (avgQuarters >= 4) return `${avgQuarters.toFixed(1)}Q · Multi-Quarter`;
  if (avgQuarters >= 2) return `${avgQuarters.toFixed(1)}Q · Emerging`;
  return `${avgQuarters.toFixed(1)}Q · Fresh Entry`;
}

function extractActivityCounts(input: AlphaDecayInput): {
  newCount: number;
  addedCount: number;
  trimmedCount: number;
  exitedCount: number;
  eliteBuyersCount: number;
  eliteSellersCount: number;
  eliteHoldersCount: number;
  flowUsd: number | null;
} {
  let newCount = 0;
  let addedCount = 0;
  let trimmedCount = 0;
  let exitedCount = 0;
  let eliteBuyersCount = input.eliteBuyersCount ?? 0;
  let eliteSellersCount = input.eliteSellersCount ?? 0;
  let eliteHoldersCount = 'eliteHoldersCount' in input ? input.eliteHoldersCount : 0;
  const flowUsd =
    'netEstFlowUsd' in input && input.netEstFlowUsd != null
      ? input.netEstFlowUsd
      : 'netDeltaUsd' in input && input.netDeltaUsd != null
        ? input.netDeltaUsd
        : null;

  if ('recentMoves' in input) {
    newCount = input.newCount;
    addedCount = input.addedCount;
    trimmedCount = input.trimmedCount;
    exitedCount = input.exitedCount;
    if (input.eliteBuyersCount == null || input.eliteSellersCount == null) {
      let eb = 0;
      let es = 0;
      for (const mv of input.recentMoves) {
        if (mv.fundTier !== 'Elite 50') continue;
        if (mv.action === 'NEW' || mv.action === 'ADDED') eb += 1;
        else if (mv.action === 'TRIMMED' || mv.action === 'EXITED') es += 1;
      }
      eliteBuyersCount = eb;
      eliteSellersCount = es;
    }
  } else if ('movers' in input) {
    newCount = input.newCount;
    addedCount = input.addedCount;
    trimmedCount = input.trimmedCount;
    exitedCount = input.exitedCount;
    if (input.eliteBuyersCount == null || input.eliteSellersCount == null) {
      let eb = 0;
      let es = 0;
      for (const mv of input.movers) {
        if (mv.fundTier !== 'Elite 50') continue;
        if (mv.action === 'NEW' || mv.action === 'ADDED') eb += 1;
        else if (mv.action === 'TRIMMED' || mv.action === 'EXITED') es += 1;
      }
      eliteBuyersCount = Math.max(eb, input.eliteNewCount);
      eliteSellersCount = es;
    }
  } else if ('idealWeightPct' in input) {
    // All IdealPortfolioPosition positions & counts come strictly from Elite-50 pool
    newCount = input.netNew;
    addedCount = input.netAdd;
    trimmedCount = input.netTrim;
    exitedCount = 0;
    eliteHoldersCount = input.holderCount;
    if (input.eliteBuyersCount == null) eliteBuyersCount = input.netNew + input.netAdd;
    if (input.eliteSellersCount == null) eliteSellersCount = input.netTrim;
  } else if ('holders' in input) {
    newCount = input.netBuyers;
    addedCount = 0;
    trimmedCount = input.netTrimmers;
    exitedCount = 0;
    let eh = 0;
    let eb = 0;
    let es = 0;
    for (const h of input.holders) {
      if (h.fundTier !== 'Elite 50') continue;
      eh += 1;
      if (h.status === 'NEW' || h.status === 'ADDED') eb += 1;
      else if (h.status === 'TRIMMED') es += 1;
    }
    eliteHoldersCount = eh;
    if (input.eliteBuyersCount == null) eliteBuyersCount = eb;
    if (input.eliteSellersCount == null) eliteSellersCount = es;
  } else if ('signalVerdict' in input) {
    // StockIndexEntry summary fallback when only index row is available
    const sv = input.signalVerdict || '';
    if (sv.includes('Cluster')) newCount = 2;
    else if (sv.includes('Elite Consensus') && input.eliteHoldersCount >= 2) addedCount = 2;
    else if (sv.includes('Exit') || sv.includes('Trimming')) trimmedCount = 2;
    if (input.eliteBuyersCount == null && (newCount > 0 || addedCount > 0)) {
      eliteBuyersCount = Math.min(2, input.eliteHoldersCount);
    }
    if (input.eliteSellersCount == null && trimmedCount > 0) {
      eliteSellersCount = input.eliteHoldersCount > 0 ? 1 : 0;
    }
  }

  return {
    newCount,
    addedCount,
    trimmedCount,
    exitedCount,
    eliteBuyersCount,
    eliteSellersCount,
    eliteHoldersCount,
    flowUsd,
  };
}

export function resolveAlphaDecayMetrics(
  input: AlphaDecayInput,
  options?: { latestQuarterLabel?: string }
): ResolvedAlphaDecayMetrics {
  const estQuarterVwap = 'estQuarterVwap' in input ? (input.estQuarterVwap ?? null) : null;
  const todayPriceUsd = input.priceUsd ?? null;
  const filingDatePriceUsd = input.filingDatePriceUsd ?? null;
  const avgEliteHoldingQuarters = input.avgEliteHoldingQuarters ?? null;
  const consensusFilingDate =
    input.consensusFilingDate ?? inferStatutoryDeadlineFromQuarterLabel(options?.latestQuarterLabel);

  let pctVsQuarterVwap = input.pctVsQuarterVwap ?? null;
  if (
    pctVsQuarterVwap == null &&
    todayPriceUsd != null &&
    todayPriceUsd > 0 &&
    estQuarterVwap != null &&
    estQuarterVwap > 0
  ) {
    pctVsQuarterVwap = Math.round(((todayPriceUsd / estQuarterVwap) - 1) * 1000) / 10;
  }

  let qVwapToFilingPct = input.qVwapToFilingPct ?? null;
  if (
    qVwapToFilingPct == null &&
    filingDatePriceUsd != null &&
    filingDatePriceUsd > 0 &&
    estQuarterVwap != null &&
    estQuarterVwap > 0
  ) {
    qVwapToFilingPct = Math.round(((filingDatePriceUsd / estQuarterVwap) - 1) * 1000) / 10;
  }

  let filingToTodayPct = input.filingToTodayPct ?? null;
  if (
    filingToTodayPct == null &&
    todayPriceUsd != null &&
    todayPriceUsd > 0 &&
    filingDatePriceUsd != null &&
    filingDatePriceUsd > 0
  ) {
    filingToTodayPct = Math.round(((todayPriceUsd / filingDatePriceUsd) - 1) * 1000) / 10;
  }

  const counts = extractActivityCounts(input);
  const totalBuyersCount = counts.newCount + counts.addedCount;
  const totalSellersCount = counts.trimmedCount + counts.exitedCount;

  if (input.alphaDecayStatus) {
    const backendReason =
      'alphaDecayReason' in input && input.alphaDecayReason
        ? input.alphaDecayReason
        : input.alphaDecayStatus === 'FOMO_TRAP'
          ? `Trading ${pctVsQuarterVwap != null ? `${pctVsQuarterVwap > 0 ? '+' : ''}${pctVsQuarterVwap.toFixed(1)}%` : '>20%'} above Est. Q-VWAP while smart money trimmed into strength.`
          : input.alphaDecayStatus === 'LAG_ADVANTAGE'
            ? `Available at or below Est. Q-VWAP after ${totalBuyersCount} cluster buy signals.`
            : 'See full Stock Dossier for three-timestamp lag breakdown.';
    return {
      consensusFilingDate,
      estQuarterVwap,
      filingDatePriceUsd,
      todayPriceUsd,
      qVwapToFilingPct,
      filingToTodayPct,
      pctVsQuarterVwap,
      avgEliteHoldingQuarters,
      eliteBuyersCount: counts.eliteBuyersCount,
      eliteSellersCount: counts.eliteSellersCount,
      totalBuyersCount,
      totalSellersCount,
      status: input.alphaDecayStatus,
      reason: backendReason,
      holdingHorizonLabel: formatHoldingPeriodLabel(avgEliteHoldingQuarters),
    };
  }

  const flow = counts.flowUsd;
  const hasNegFlow = flow != null ? flow < 0 : false;
  const hasPosFlow = flow != null ? flow > 0 : false;

  const isFomoTrap =
    pctVsQuarterVwap != null &&
    pctVsQuarterVwap > 20.0 &&
    ((counts.eliteSellersCount >= 1 &&
      (counts.eliteSellersCount > counts.eliteBuyersCount ||
        (counts.eliteSellersCount === counts.eliteBuyersCount &&
          totalSellersCount >= totalBuyersCount) ||
        hasNegFlow)) ||
      (totalSellersCount >= 2 && totalSellersCount > totalBuyersCount && (flow == null || flow < 0)));

  const isLagAdvantage =
    pctVsQuarterVwap != null &&
    pctVsQuarterVwap <= 0.0 &&
    totalBuyersCount >= 2 &&
    totalBuyersCount > totalSellersCount &&
    (counts.newCount >= 2 || hasPosFlow || counts.eliteBuyersCount >= 1 || counts.eliteHoldersCount >= 1);

  let status: AlphaDecayStatus;
  let reason: string;

  if (isFomoTrap) {
    status = 'FOMO_TRAP';
    reason = `Trading +${pctVsQuarterVwap!.toFixed(1)}% above Est. Q-VWAP while ${counts.eliteSellersCount} Elite-50 & ${totalSellersCount} total funds trimmed/exited vs. ${totalBuyersCount} buys.`;
  } else if (isLagAdvantage) {
    status = 'LAG_ADVANTAGE';
    reason = `Available at ${pctVsQuarterVwap!.toFixed(1)}% vs. Est. Q-VWAP after ${counts.newCount} NEW & ${counts.addedCount} ADDED buys (vs. ${totalSellersCount} cuts).`;
  } else if (
    (pctVsQuarterVwap != null && pctVsQuarterVwap > 20.0) ||
    (filingToTodayPct != null && filingToTodayPct > 15.0)
  ) {
    status = 'POST_FILING_EXTENDED';
    reason =
      pctVsQuarterVwap != null && pctVsQuarterVwap > 20.0
        ? `Trading +${pctVsQuarterVwap.toFixed(1)}% above Est. Q-VWAP (+${totalBuyersCount}/-${totalSellersCount} moves); watch post-13F chase risk.`
        : `Surged +${filingToTodayPct!.toFixed(1)}% since the SEC filing date; verify risk/reward before chasing.`;
  } else {
    status = 'NEUTRAL';
    reason = 'Trading near institutional cost basis without skewed FOMO distribution or cluster dip dislocation.';
  }

  // Explicitly keep type referenced cleanly for strict TS builds
  const _tierCheck: FundTier | MoveAction | undefined = undefined;
  void _tierCheck;

  return {
    consensusFilingDate,
    estQuarterVwap,
    filingDatePriceUsd,
    todayPriceUsd,
    qVwapToFilingPct,
    filingToTodayPct,
    pctVsQuarterVwap,
    avgEliteHoldingQuarters,
    eliteBuyersCount: counts.eliteBuyersCount,
    eliteSellersCount: counts.eliteSellersCount,
    totalBuyersCount,
    totalSellersCount,
    status,
    reason,
    holdingHorizonLabel: formatHoldingPeriodLabel(avgEliteHoldingQuarters),
  };
}
