import type { IdealPortfolioPosition, StockIndexEntry } from '../types/data';

export type UniverseFilter = 'ALL' | 'EX_MEGA' | 'EMERGING';
export type SignalMode = 'rawScore' | 'inflectionScore';
export type SectorCapOption = null | 35 | 25;
export type RebalanceMode = 'full' | 'buyOnly';

export interface DynamicPosition extends IdealPortfolioPosition {
  dynamicWeightPct: number;
  weightVsDefaultPct: number | null;
}

export interface ParsedHoldingsResult {
  holdings: Map<string, number>;
  unparsedLines: string[];
  unknownTickers: string[];
  normalizedAliases: Array<{ from: string; to: string }>;
}

export interface RebalanceRow {
  ticker: string;
  name: string;
  action: 'BUY' | 'ADD' | 'TRIM' | 'EXIT' | 'HOLD';
  currUsd: number;
  currPct: number;
  targetUsd: number;
  targetPct: number;
  deltaUsd: number;
  estSharesDelta: number | null;
}

export interface RebalancePlan {
  mode: RebalanceMode;
  rows: RebalanceRow[];
  totalCurrentUsd: number;
  postDeployUsd: number;
  totalBuyUsd: number;
  totalSellUsd: number;
  cashDeltaUsd: number;
}

// Mirrors DUAL_CLASS_CANONICAL in pipeline/funds_config.py plus punctuation aliases.
export const DUAL_ALIAS_MAP: Record<string, string> = {
  GOOG: 'GOOGL',
  'BRK.A': 'BRK-B',
  'BRK-A': 'BRK-B',
  BRKA: 'BRK-B',
  'BRK.B': 'BRK-B',
  BRKB: 'BRK-B',
  FOX: 'FOXA',
  NWS: 'NWSA',
  ZG: 'Z',
  LBRDA: 'LBRDK',
  FWONA: 'FWONK',
  LSXMA: 'LSXMK',
  LSXMB: 'LSXMK',
  BATRA: 'BATRK',
  PARAA: 'PARA',
  'HEI.A': 'HEI',
  'HEI-A': 'HEI',
  'LEN.B': 'LEN',
  'LEN-B': 'LEN',
  'BF.A': 'BF-B',
  'BF-A': 'BF-B',
  'BF.B': 'BF-B',
  UHAL: 'UHAL-B',
  'UHAL.B': 'UHAL-B',
};

export function canonicalizeInputTicker(raw: string, knownTickers?: Set<string>): string {
  const upper = raw.trim().toUpperCase();
  const mapped = DUAL_ALIAS_MAP[upper];
  if (mapped) {
    // Prefer canonical pipeline symbol; fall back only if dataset uses dot variant
    if (mapped === 'BRK-B' && knownTickers && !knownTickers.has('BRK-B') && knownTickers.has('BRK.B')) {
      return 'BRK.B';
    }
    return mapped;
  }
  return upper;
}

export function buildConstrainedPortfolio(
  candidatePool: IdealPortfolioPosition[],
  universeFilter: UniverseFilter,
  signalMode: SignalMode,
  maxNames: number,
  capPct: number,
  sectorCapPct: SectorCapOption,
  defaultWeightMap: Map<string, number>
): { positions: DynamicPosition[]; constraintNote: string | null } {
  const filtered = candidatePool.filter((p) => {
    if (universeFilter === 'EX_MEGA') {
      return p.marketCapTier === 'Large $30B-$200B' || p.marketCapTier === 'Emerging <$30B';
    }
    if (universeFilter === 'EMERGING') {
      return p.marketCapTier === 'Emerging <$30B';
    }
    return true;
  });

  const sorted = [...filtered].sort((a, b) => {
    const sa = signalMode === 'inflectionScore' ? b.inflectionScore : b.rawScore;
    const sb = signalMode === 'inflectionScore' ? a.inflectionScore : a.rawScore;
    return sa - sb;
  });

  if (sorted.length === 0) {
    return {
      positions: [],
      constraintNote: 'No stocks in the Top-80 candidate pool match this market-cap filter.',
    };
  }

  const targetN = Math.min(maxNames, sorted.length);
  const effStockCap =
    capPct * targetN >= 100 - 1e-6
      ? capPct
      : Math.ceil((100 / targetN + 0.05) * 10) / 10;

  let sliced: IdealPortfolioPosition[];
  if (sectorCapPct !== null) {
    const primary: IdealPortfolioPosition[] = [];
    const deferred: IdealPortfolioPosition[] = [];
    const secCounts = new Map<string, number>();

    for (const p of sorted) {
      const key = p.subSector || p.sector;
      const cnt = secCounts.get(key) ?? 0;
      if (cnt * effStockCap < sectorCapPct - 1e-6 && primary.length < targetN) {
        primary.push(p);
        secCounts.set(key, cnt + 1);
      } else {
        deferred.push(p);
      }
    }
    sliced = [...primary, ...deferred].slice(0, targetN);
  } else {
    sliced = sorted.slice(0, targetN);
  }

  const notes: string[] = [];
  if (sliced.length < maxNames) {
    notes.push(`Universe filter matched ${sliced.length} eligible stocks (target ${maxNames}).`);
  }
  if (effStockCap > capPct + 1e-6) {
    notes.push(
      `With ${sliced.length} positions, single-stock cap lifted from ${capPct}% to ${effStockCap.toFixed(1)}% to invest 100%.`
    );
  }

  const secCounts = new Map<string, number>();
  for (const p of sliced) {
    const key = p.subSector || p.sector;
    secCounts.set(key, (secCounts.get(key) ?? 0) + 1);
  }

  let effSectorCap: number | null = sectorCapPct;
  if (effSectorCap !== null) {
    const maxFeasible = Array.from(secCounts.values()).reduce(
      (sum, cnt) => sum + Math.min(effSectorCap!, cnt * effStockCap),
      0
    );
    if (maxFeasible < 100 - 1e-6) {
      let relaxed = effSectorCap;
      while (relaxed < 100) {
        relaxed += 1;
        const capSum = Array.from(secCounts.values()).reduce(
          (sum, cnt) => sum + Math.min(relaxed, cnt * effStockCap),
          0
        );
        if (capSum >= 100 - 1e-6) break;
      }
      effSectorCap = relaxed;
      notes.push(
        `Available sub-sector breadth required lifting the sub-sector cap from ${sectorCapPct}% to ${effSectorCap}% to invest 100% within ${effStockCap.toFixed(1)}% single-stock caps.`
      );
    }
  }

  const rawScores = sliced.map((p) =>
    Math.max(0.01, signalMode === 'inflectionScore' ? p.inflectionScore : p.rawScore)
  );
  const sumRaw = rawScores.reduce((a, b) => a + b, 0) || 1;
  const weights = rawScores.map((s) => (s / sumRaw) * 100);

  for (let iter = 0; iter < 80; iter++) {
    for (let i = 0; i < weights.length; i++) {
      if (weights[i] > effStockCap) weights[i] = effStockCap;
    }

    if (effSectorCap !== null) {
      const secSums = new Map<string, number>();
      sliced.forEach((p, idx) => {
        const key = p.subSector || p.sector;
        secSums.set(key, (secSums.get(key) ?? 0) + weights[idx]);
      });
      sliced.forEach((p, idx) => {
        const key = p.subSector || p.sector;
        const tot = secSums.get(key) ?? 0;
        if (tot > effSectorCap + 1e-6) {
          weights[idx] *= effSectorCap / tot;
        }
      });
    }

    const currentSum = weights.reduce((a, b) => a + b, 0);
    const deficit = 100 - currentSum;
    if (Math.abs(deficit) < 1e-5) break;

    if (deficit > 0) {
      const secSums = new Map<string, number>();
      if (effSectorCap !== null) {
        sliced.forEach((p, idx) => {
          const key = p.subSector || p.sector;
          secSums.set(key, (secSums.get(key) ?? 0) + weights[idx]);
        });
      }
      const eligibleIndices: number[] = [];
      let eligScoreSum = 0;
      for (let i = 0; i < sliced.length; i++) {
        if (weights[i] >= effStockCap - 1e-6) continue;
        if (effSectorCap !== null) {
          const sk = sliced[i].subSector || sliced[i].sector;
          if ((secSums.get(sk) ?? 0) >= effSectorCap - 1e-6) continue;
        }
        eligibleIndices.push(i);
        eligScoreSum += rawScores[i];
      }
      if (eligibleIndices.length === 0 || eligScoreSum <= 0) break;

      for (const idx of eligibleIndices) {
        const sk = sliced[idx].subSector || sliced[idx].sector;
        const sectorRoom =
          effSectorCap !== null ? Math.max(0, effSectorCap - (secSums.get(sk) ?? 0)) : Infinity;
        const stockRoom = Math.max(0, effStockCap - weights[idx]);
        const add = Math.min(stockRoom, sectorRoom, (rawScores[idx] / eligScoreSum) * deficit);
        weights[idx] += add;
        if (effSectorCap !== null) {
          secSums.set(sk, (secSums.get(sk) ?? 0) + add);
        }
      }
    }
  }

  const indexed = sliced
    .map((pos, idx) => ({
      pos,
      exact: Math.min(effStockCap, weights[idx]),
      w: Math.min(effStockCap, Math.round(weights[idx] * 100) / 100),
      sec: pos.subSector || pos.sector,
    }))
    .sort((a, b) => b.exact - a.exact || b.pos.rawScore - a.pos.rawScore);

  const secRoundedSums = new Map<string, number>();
  for (const item of indexed) {
    secRoundedSums.set(item.sec, (secRoundedSums.get(item.sec) ?? 0) + item.w);
  }

  let remBps = Math.round((100 - indexed.reduce((s, x) => s + x.w, 0)) * 100);
  let safety = 0;
  while (remBps !== 0 && safety < 500) {
    safety += 1;
    const step = remBps > 0 ? 0.01 : -0.01;
    let moved = false;
    const order = [...indexed].sort((a, b) =>
      step > 0 ? (b.exact - b.w) - (a.exact - a.w) : (a.exact - a.w) - (b.exact - b.w)
    );
    for (const item of order) {
      if (step > 0) {
        const curSec = secRoundedSums.get(item.sec) ?? 0;
        const canAddSec = effSectorCap === null || curSec + 0.01 <= effSectorCap + 1e-9;
        if (item.w + 0.01 <= effStockCap + 1e-9 && canAddSec) {
          item.w = Math.round((item.w + 0.01) * 100) / 100;
          secRoundedSums.set(item.sec, curSec + 0.01);
          remBps -= 1;
          moved = true;
          break;
        }
      } else if (item.w - 0.01 >= 0.01 - 1e-9) {
        item.w = Math.round((item.w - 0.01) * 100) / 100;
        secRoundedSums.set(item.sec, (secRoundedSums.get(item.sec) ?? 0) - 0.01);
        remBps += 1;
        moved = true;
        break;
      }
    }
    if (!moved) break;
  }

  indexed.sort((a, b) => b.w - a.w || b.pos.rawScore - a.pos.rawScore);

  const out: DynamicPosition[] = indexed.map(({ pos, w }, idx) => {
    const defW = defaultWeightMap.get(pos.ticker);
    return {
      ...pos,
      rank: idx + 1,
      dynamicWeightPct: w,
      weightVsDefaultPct: defW !== undefined ? Math.round((w - defW) * 100) / 100 : null,
    };
  });

  return { positions: out, constraintNote: notes.length > 0 ? notes.join(' ') : null };
}

export function parseHoldingsInput(
  rawText: string,
  capitalUsd: number,
  knownTickers?: Set<string>
): ParsedHoldingsResult {
  const holdings = new Map<string, number>();
  const unparsedLines: string[] = [];
  const unknownSet = new Set<string>();
  const aliasSeen = new Map<string, string>();

  if (!rawText.trim()) {
    return { holdings, unparsedLines, unknownTickers: [], normalizedAliases: [] };
  }

  const chunks = rawText
    .split(/\n|;|,(?=\s*[A-Za-z])/g)
    .map((s) => s.trim())
    .filter((s) => Boolean(s) && !s.startsWith('#') && !s.startsWith('//'));

  for (const chunk of chunks) {
    const m = chunk.match(/^([A-Za-z][A-Za-z0-9.-]{0,9})[\s:=]+(\$?[\d,]+(?:\.\d+)?%?)$/);
    if (!m) {
      unparsedLines.push(chunk);
      continue;
    }
    const orig = m[1].toUpperCase();
    const sym = canonicalizeInputTicker(orig, knownTickers);
    const rawVal = m[2];
    const isPct = rawVal.endsWith('%');
    const num = Number(rawVal.replace(/[$,%]/g, ''));
    if (!Number.isFinite(num) || num < 0) {
      unparsedLines.push(chunk);
      continue;
    }
    if (orig !== sym) {
      aliasSeen.set(orig, sym);
    }
    if (knownTickers && !knownTickers.has(sym)) {
      unknownSet.add(sym);
    }
    const usd = isPct ? (num / 100) * capitalUsd : num;
    holdings.set(sym, (holdings.get(sym) ?? 0) + usd);
  }

  return {
    holdings,
    unparsedLines,
    unknownTickers: Array.from(unknownSet),
    normalizedAliases: Array.from(aliasSeen.entries()).map(([from, to]) => ({ from, to })),
  };
}

export function computeRebalancePlan({
  holdingsMap,
  activePositions,
  stocksIndex,
  capitalUsd,
  mode,
  newCashUsd,
  driftTolerancePct,
}: {
  holdingsMap: Map<string, number>;
  activePositions: DynamicPosition[];
  stocksIndex: Record<string, StockIndexEntry>;
  capitalUsd: number;
  mode: RebalanceMode;
  newCashUsd: number;
  driftTolerancePct: number;
}): RebalancePlan | null {
  if (holdingsMap.size === 0) return null;

  const targetMap = new Map<string, DynamicPosition>(
    activePositions.map((p) => [p.ticker, p])
  );
  const allTickers = new Set<string>([
    ...Array.from(holdingsMap.keys()),
    ...Array.from(targetMap.keys()),
  ]);

  const totalCurrentUsd = Array.from(holdingsMap.values()).reduce((a, b) => a + b, 0);
  const safeCash = Math.max(0, Number.isFinite(newCashUsd) ? newCashUsd : 0);

  let rows: RebalanceRow[];

  if (mode === 'buyOnly') {
    const postDeployUsd =
      totalCurrentUsd > 0 ? totalCurrentUsd + safeCash : Math.max(capitalUsd, safeCash);
    const cashBudget = totalCurrentUsd > 0 ? safeCash : postDeployUsd;

    // Identify target stocks underweight relative to ideal post-deploy target
    const underweightByTicker = new Map<string, number>();
    let totalUnderweight = 0;

    for (const p of activePositions) {
      const currUsd = holdingsMap.get(p.ticker) ?? 0;
      const idealUsd = (postDeployUsd * p.dynamicWeightPct) / 100;
      const idealPct = p.dynamicWeightPct;
      const currPct = totalCurrentUsd > 0 ? (currUsd / postDeployUsd) * 100 : 0;
      const deficit = Math.max(0, idealUsd - currUsd);
      // Respect drift tolerance when already held, while always funding brand-new underweights if > 0
      const withinTolerance =
        currUsd > 0 && idealPct - currPct < driftTolerancePct;
      const eligibleDeficit = withinTolerance ? 0 : deficit;
      if (eligibleDeficit > 0) {
        underweightByTicker.set(p.ticker, eligibleDeficit);
        totalUnderweight += eligibleDeficit;
      }
    }

    // Fallback: if tolerance filtered everything out while cash exists, allow any underweight deficit
    if (totalUnderweight <= 0 && cashBudget > 0) {
      for (const p of activePositions) {
        const currUsd = holdingsMap.get(p.ticker) ?? 0;
        const idealUsd = (postDeployUsd * p.dynamicWeightPct) / 100;
        const deficit = Math.max(0, idealUsd - currUsd);
        if (deficit > 0) {
          underweightByTicker.set(p.ticker, deficit);
          totalUnderweight += deficit;
        }
      }
    }

    rows = Array.from(allTickers).map((ticker) => {
      const currUsd = holdingsMap.get(ticker) ?? 0;
      const targetPos = targetMap.get(ticker);
      const targetWeight = targetPos?.dynamicWeightPct ?? 0;
      const idealTargetUsd = (postDeployUsd * targetWeight) / 100;
      const deficit = underweightByTicker.get(ticker) ?? 0;
      const buyUsd =
        deficit > 0 && totalUnderweight > 0
          ? (deficit / totalUnderweight) * cashBudget
          : 0;
      const roundedDelta = buyUsd >= 1 ? buyUsd : 0;
      const priceUsd = targetPos?.priceUsd ?? stocksIndex[ticker]?.priceUsd ?? null;
      const estSharesDelta =
        priceUsd && priceUsd > 0 ? Math.round(roundedDelta / priceUsd) : null;

      let action: RebalanceRow['action'];
      if (roundedDelta <= 0) {
        action = 'HOLD';
      } else if (currUsd <= 0) {
        action = 'BUY';
      } else {
        action = 'ADD';
      }

      return {
        ticker,
        name: targetPos?.name ?? stocksIndex[ticker]?.name ?? ticker,
        action,
        currUsd,
        currPct: postDeployUsd > 0 ? (currUsd / postDeployUsd) * 100 : 0,
        targetUsd: idealTargetUsd,
        targetPct: targetWeight,
        deltaUsd: roundedDelta,
        estSharesDelta,
      };
    });

    const actionPriority: Record<string, number> = {
      BUY: 0,
      ADD: 1,
      HOLD: 2,
      TRIM: 3,
      EXIT: 4,
    };
    rows.sort(
      (a, b) =>
        actionPriority[a.action] - actionPriority[b.action] ||
        b.deltaUsd - a.deltaUsd ||
        b.targetPct - a.targetPct
    );

    const totalBuyUsd = rows.reduce((s, r) => s + Math.max(0, r.deltaUsd), 0);
    return {
      mode,
      rows,
      totalCurrentUsd,
      postDeployUsd,
      totalBuyUsd,
      totalSellUsd: 0,
      cashDeltaUsd: cashBudget,
    };
  }

  // Full Rebalance against Target Capital
  rows = Array.from(allTickers).map((ticker) => {
    const currUsd = holdingsMap.get(ticker) ?? 0;
    const targetPos = targetMap.get(ticker);
    const targetWeight = targetPos?.dynamicWeightPct ?? 0;
    const targetUsd = (capitalUsd * targetWeight) / 100;
    const currPct = capitalUsd > 0 ? (currUsd / capitalUsd) * 100 : 0;
    const rawDeltaUsd = targetUsd - currUsd;
    const absDriftPct = Math.abs(targetWeight - currPct);
    const minDollarThreshold = Math.max(50, capitalUsd * 0.0025);
    const priceUsd = targetPos?.priceUsd ?? stocksIndex[ticker]?.priceUsd ?? null;

    let action: RebalanceRow['action'];
    let deltaUsd = rawDeltaUsd;
    if (currUsd <= 0 && targetUsd > 0) {
      action = 'BUY';
    } else if (targetUsd <= 0 && currUsd > 0) {
      action = 'EXIT';
    } else if (absDriftPct < driftTolerancePct || Math.abs(rawDeltaUsd) < minDollarThreshold) {
      action = 'HOLD';
      deltaUsd = 0;
    } else if (rawDeltaUsd > 0) {
      action = 'ADD';
    } else {
      action = 'TRIM';
    }

    const estSharesDelta =
      action === 'HOLD'
        ? 0
        : priceUsd && priceUsd > 0
          ? Math.round(deltaUsd / priceUsd)
          : null;

    return {
      ticker,
      name: targetPos?.name ?? stocksIndex[ticker]?.name ?? ticker,
      action,
      currUsd,
      currPct,
      targetUsd,
      targetPct: targetWeight,
      deltaUsd,
      estSharesDelta,
    };
  });

  const actionPriority: Record<string, number> = {
    BUY: 0,
    EXIT: 1,
    ADD: 2,
    TRIM: 3,
    HOLD: 4,
  };
  rows.sort(
    (a, b) =>
      actionPriority[a.action] - actionPriority[b.action] ||
      Math.abs(b.deltaUsd) - Math.abs(a.deltaUsd)
  );

  const totalBuyUsd = rows.filter((r) => r.deltaUsd > 0).reduce((s, r) => s + r.deltaUsd, 0);
  const totalSellUsd = rows.filter((r) => r.deltaUsd < 0).reduce((s, r) => s + Math.abs(r.deltaUsd), 0);
  return {
    mode,
    rows,
    totalCurrentUsd,
    postDeployUsd: capitalUsd,
    totalBuyUsd,
    totalSellUsd,
    cashDeltaUsd: capitalUsd - totalCurrentUsd,
  };
}
