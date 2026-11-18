import type {
  ActionVerdict,
  AlphaDecayStatus,
  ConvergenceTier,
  DualConvictionSignal,
  Form4InsiderSummary,
  InsiderSignalLabel,
  MidQuarterAlert,
  StockIndexEntry,
} from '../types/data';
import type { UnifiedConvictionRow } from './convictionMatrix';

const CONVERGENCE_TIER_ORDER: Record<ConvergenceTier, number> = {
  DUAL_CONVICTION_PRIME: 5,
  ACTIVIST_13F_CONVERGENCE: 4,
  DUAL_CONVICTION_EXTENDED: 3,
  INSIDER_CLUSTER_WATCH: 2,
  ACTIVIST_13D_CATALYST: 1,
};

function clamp(v: number, lo: number, hi: number): number {
  return Math.min(hi, Math.max(lo, v));
}

function round1(v: number): number {
  return Math.round(v * 10) / 10;
}

function fmtUsdBrief(val: number): string {
  const av = Math.abs(val);
  if (av >= 1_000_000_000) return `$${(val / 1_000_000_000).toFixed(2)}B`;
  if (av >= 1_000_000) return `$${(val / 1_000_000).toFixed(1)}M`;
  if (av >= 1_000) return `$${(val / 1_000).toFixed(0)}K`;
  return `$${val.toFixed(0)}`;
}

export interface ComputeDualConvictionParams {
  ticker: string;
  name: string;
  sector?: string;
  subSector?: string;
  form4Summary?: Form4InsiderSummary | null;
  alerts?: MidQuarterAlert[] | null;
  priceUsd?: number | null;
  estQuarterVwap?: number | null;
  pctVsQuarterVwap?: number | null;
  alphaDecayStatus?: AlphaDecayStatus | null;
  actionabilityScore?: number | null;
  actionVerdict?: ActionVerdict | null;
  newCount?: number;
  addedCount?: number;
  trimmedCount?: number;
  exitedCount?: number;
  eliteBuyersCount?: number;
  eliteSellersCount?: number;
  eliteHoldersCount?: number;
  netEstFlowUsd?: number | null;
}

/**
 * Cross-reference SEC Form 4 open-market ('P') insider buys, Schedule 13D/13G Item 4 activist
 * catalysts, Elite 13F institutional accumulation, and Est. Quarter-VWAP valuation proximity.
 * Mirrors `compute_dual_conviction_signal` in `pipeline/build_data.py`.
 */
export function computeDualConvictionSignal(
  params: ComputeDualConvictionParams
): DualConvictionSignal | null {
  const f4 = params.form4Summary ?? null;
  const openBuyCnt = f4?.openMarketBuyCount ?? 0;
  const insiderBuyVal = f4?.totalBuyValueUsd ?? 0;
  const buyersCnt = f4?.distinctBuyersCount ?? 0;
  const csuiteCnt = f4?.cSuiteBuyersCount ?? 0;
  const non10b5Cnt = f4?.non10b5Count ?? 0;
  const insiderLabel: InsiderSignalLabel = f4?.insiderSignalLabel ?? 'NONE';
  const isSellHeavy = insiderLabel === 'MIXED_SELL_HEAVY';
  const hasInsiderBuy = openBuyCnt > 0 && !isSellHeavy;
  const hasForm4Cluster = Boolean(
    f4 &&
      !isSellHeavy &&
      (f4.clusterDetected ||
        insiderLabel === 'CLUSTER_BUY' ||
        (csuiteCnt >= 1 && insiderBuyVal >= 100_000))
  );
  const latestInsiderDate = f4?.latestBuyDate || f4?.latestFilingDate || null;

  const topInsiders: string[] = [];
  const seenIns = new Set<string>();
  for (const tx of f4?.recentTransactions ?? []) {
    const iname = (tx.insiderName || '').trim();
    const irole = (tx.insiderRole || '').trim();
    const lbl = iname && irole ? `${iname} (${irole})` : iname || irole;
    if (lbl && !seenIns.has(lbl)) {
      seenIns.add(lbl);
      topInsiders.push(lbl);
    }
    if (topInsiders.length >= 3) break;
  }

  const activeAlerts = (params.alerts ?? []).filter(
    (al) => !(al.stakeStatus ?? '').startsWith('Exited')
  );
  const activistAlerts = activeAlerts.filter((al) => Boolean(al.isActivist));
  const passive13gAlerts = activeAlerts.filter(
    (al) =>
      !al.isActivist &&
      (al.percentOfClass == null ||
        al.percentOfClass >= 5.0 ||
        (al.stakeStatus ?? '').startsWith('Active'))
  );

  const has13D = activistAlerts.length > 0;
  const has13G = !has13D && passive13gAlerts.length > 0;

  const activistFunds: string[] = [];
  const seenAf = new Set<string>();
  for (const al of activistAlerts) {
    const fn = (al.fundName || '').trim();
    if (fn && !seenAf.has(fn)) {
      seenAf.add(fn);
      activistFunds.push(fn);
    }
    if (activistFunds.length >= 3) break;
  }

  const top13d = activistAlerts[0] ?? null;
  const activistCat = top13d ? top13d.item4Category || 'Active 13D' : null;
  const activistIntent = top13d ? top13d.item4Intent?.trim() || null : null;
  const activistDate = top13d ? top13d.filingDate?.trim() || null : null;

  const newCount = params.newCount ?? 0;
  const addedCount = params.addedCount ?? 0;
  const trimmedCount = params.trimmedCount ?? 0;
  const exitedCount = params.exitedCount ?? 0;
  const eliteBuyersCount = params.eliteBuyersCount ?? 0;
  const eliteSellersCount = params.eliteSellersCount ?? 0;
  const eliteHoldersCount = params.eliteHoldersCount ?? 0;

  const totalBuyers = Math.max(0, Math.trunc(newCount + addedCount));
  const totalSellers = Math.max(0, Math.trunc(trimmedCount + exitedCount));
  const flowVal = params.netEstFlowUsd ?? 0;
  const isFomo = params.alphaDecayStatus === 'FOMO_TRAP';
  const has13fAccum =
    !isFomo &&
    (params.actionVerdict === 'STRONG ACCUMULATE' ||
      params.actionVerdict === 'ACCUMULATE ON DIP' ||
      (totalBuyers > totalSellers &&
        (eliteBuyersCount >= 1 || flowVal > 0 || newCount >= 2)) ||
      (eliteBuyersCount > eliteSellersCount &&
        totalBuyers >= totalSellers &&
        flowVal > 0));

  const pvRaw = params.pctVsQuarterVwap ?? null;
  const nearQuarterVwap = pvRaw != null && pvRaw <= 12.0;
  const extendedOrFomo = isFomo || (pvRaw != null && pvRaw > 12.0);

  if (!(hasForm4Cluster || has13D || (hasInsiderBuy && (has13fAccum || has13G)))) {
    return null;
  }

  let tier: ConvergenceTier;
  if (hasForm4Cluster && (has13fAccum || has13D)) {
    tier = nearQuarterVwap && !isFomo ? 'DUAL_CONVICTION_PRIME' : 'DUAL_CONVICTION_EXTENDED';
  } else if (has13D && (has13fAccum || eliteHoldersCount >= 1 || hasInsiderBuy)) {
    tier = 'ACTIVIST_13F_CONVERGENCE';
  } else if (hasInsiderBuy && (has13fAccum || has13D || has13G)) {
    tier = extendedOrFomo ? 'DUAL_CONVICTION_EXTENDED' : 'INSIDER_CLUSTER_WATCH';
  } else if (hasForm4Cluster) {
    tier = 'INSIDER_CLUSTER_WATCH';
  } else {
    tier = 'ACTIVIST_13D_CATALYST';
  }

  let sInsider = 0;
  if (hasInsiderBuy) {
    if (insiderLabel === 'CLUSTER_BUY') {
      sInsider = Math.min(
        36,
        24 +
          Math.min(5, Math.max(0, buyersCnt - 2) * 2.5) +
          (csuiteCnt >= 1 ? 3 : 0) +
          (insiderBuyVal >= 1_000_000 ? 3 : insiderBuyVal >= 250_000 ? 1.5 : 0) +
          (non10b5Cnt > 0 ? 1 : 0)
      );
    } else if (insiderLabel === 'C_SUITE_BUY') {
      sInsider = Math.min(
        28,
        19 +
          (insiderBuyVal >= 1_000_000 ? 6 : insiderBuyVal >= 250_000 ? 3 : 1) +
          (non10b5Cnt > 0 ? 2 : 0)
      );
    } else if (insiderLabel === 'DIRECTOR_BUY') {
      sInsider = Math.min(16, 12 + (insiderBuyVal >= 250_000 ? 3 : 1));
    } else {
      sInsider = Math.min(11, 8 + (insiderBuyVal >= 100_000 ? 2 : 0));
    }
  }

  let s13d = 0;
  if (has13D) {
    const maxPct = activistAlerts.reduce(
      (mx, al) => Math.max(mx, al.percentOfClass ?? 0),
      0
    );
    let catBonus = 0;
    if (activistCat === 'Strategic Review / M&A' || activistCat === 'Board / Governance') {
      catBonus = 4;
    } else if (
      activistCat === 'Capital Return / Value' ||
      activistCat === 'Management Engagement'
    ) {
      catBonus = 2.5;
    }
    const pctBonus = maxPct >= 9 ? 3 : maxPct >= 5 ? 1.5 : 0;
    s13d = Math.min(24, 17 + catBonus + pctBonus);
  } else if (has13G) {
    s13d = 7;
  }

  let base13f: number;
  if (params.actionabilityScore != null) {
    base13f = clamp((params.actionabilityScore / 100) * 18, 0, 18);
  } else {
    const netB = totalBuyers - totalSellers;
    base13f = clamp(
      8 + clamp(netB * 2, -6, 8) + (flowVal > 0 ? 2 : 0),
      0,
      18
    );
  }
  const eliteBonus = has13fAccum
    ? Math.min(6, eliteBuyersCount * 2 + (eliteHoldersCount >= 5 ? 2 : 0))
    : Math.min(3, eliteHoldersCount * 0.8);
  const s13f = clamp(base13f + eliteBonus - (isFomo ? 8 : 0), 0, 24);

  let sVwap: number;
  if (pvRaw == null) {
    sVwap = 7;
  } else if (pvRaw <= -5) {
    sVwap = 16;
  } else if (pvRaw <= 0) {
    sVwap = 14.5;
  } else if (pvRaw <= 12) {
    sVwap = 14.5 - (pvRaw / 12) * 4;
  } else if (pvRaw <= 20) {
    sVwap = 10.5 - ((pvRaw - 12) / 8) * 5;
  } else {
    sVwap = Math.max(1.5, 5.5 - ((pvRaw - 20) / 15) * 4);
  }

  const convScore = round1(clamp(sInsider + s13d + s13f + sVwap, 0, 100));

  const reasons: string[] = [];
  if (hasInsiderBuy) {
    const insKind =
      buyersCnt >= 2
        ? `${buyersCnt} insiders`
        : topInsiders[0] || (csuiteCnt >= 1 ? 'C-Suite insider' : 'Insider');
    reasons.push(`Form 4 open-market buy: ${insKind} bought ${fmtUsdBrief(insiderBuyVal)}`);
  } else if (isSellHeavy) {
    reasons.push('Form 4 buy activity offset by heavier discretionary insider sales');
  }

  if (has13D) {
    const pctTop = top13d?.percentOfClass ?? null;
    const pctTxt = pctTop != null ? ` (${pctTop.toFixed(1)}%)` : '';
    reasons.push(
      `Schedule 13D${pctTxt} by ${activistFunds.join(', ') || 'Activist'}: ${activistCat}`
    );
  } else if (has13G) {
    reasons.push('Active ≥5% Schedule 13G stake alongside tracked filings');
  }

  if (has13fAccum) {
    reasons.push(
      `13F accumulation: +${totalBuyers} buyers / -${totalSellers} sellers` +
        (eliteBuyersCount > 0 ? ` (${eliteBuyersCount} Elite buyers)` : '')
    );
  } else if (eliteHoldersCount > 0) {
    reasons.push(`Held by ${eliteHoldersCount} Elite-50 13F fund(s)`);
  }

  if (pvRaw != null) {
    reasons.push(`${pvRaw >= 0 ? '+' : ''}${pvRaw.toFixed(1)}% vs. Est. Q-VWAP`);
  } else {
    reasons.push('Est. Q-VWAP pricing pending');
  }

  let headline: string;
  if (tier === 'DUAL_CONVICTION_PRIME') {
    headline = 'Rare Dual-Conviction: Form 4 insider buying + Elite 13F accumulation near Q-VWAP.';
  } else if (tier === 'ACTIVIST_13F_CONVERGENCE') {
    headline = 'Activist Schedule 13D catalyst aligned with Elite-50 institutional backing.';
  } else if (tier === 'DUAL_CONVICTION_EXTENDED') {
    headline = extendedOrFomo
      ? 'Insider + 13F co-buying on an extended rally (>12% above Q-VWAP) — monitor pullbacks.'
      : 'Insider + 13F co-buying detected; verify Q-VWAP entry pricing.';
  } else if (tier === 'INSIDER_CLUSTER_WATCH') {
    headline =
      hasInsiderBuy && has13fAccum
        ? 'Insider + 13F co-buying watch (non-cluster insider buy alongside 13F accumulation).'
        : 'Open-market Form 4 insider cluster/C-suite buy on a 13F-tracked stock.';
  } else {
    headline = 'Live mid-quarter Schedule 13D activist campaign filed ahead of 13F reporting.';
  }

  let latestSignalDate: string | null = null;
  if (latestInsiderDate && activistDate) {
    latestSignalDate = latestInsiderDate >= activistDate ? latestInsiderDate : activistDate;
  } else {
    latestSignalDate = latestInsiderDate || activistDate || null;
  }

  return {
    ticker: params.ticker,
    name: params.name,
    sector: params.sector || 'Other',
    subSector: params.subSector || params.sector || 'Other',
    tier,
    convergenceScore: convScore,
    hasForm4Cluster,
    hasInsiderBuy,
    has13DActivist: has13D,
    has13GStake: has13G,
    has13FAccumulation: has13fAccum,
    nearQuarterVwap,
    priceUsd: params.priceUsd ?? null,
    estQuarterVwap: params.estQuarterVwap ?? null,
    pctVsQuarterVwap: pvRaw,
    actionabilityScore: params.actionabilityScore ?? null,
    actionVerdict: params.actionVerdict ?? null,
    eliteBuyersCount,
    eliteHoldersCount,
    netEstFlowUsd:
      params.netEstFlowUsd != null ? Math.round(params.netEstFlowUsd * 100) / 100 : null,
    insiderBuyValueUsd: insiderBuyVal > 0 ? Math.round(insiderBuyVal * 100) / 100 : 0,
    insiderBuyersCount: buyersCnt,
    cSuiteBuyersCount: csuiteCnt,
    insiderSignalLabel: insiderLabel,
    topInsiders,
    latestInsiderDate,
    activistFunds,
    activistCategory: activistCat,
    activistIntent,
    activistFilingDate: activistDate,
    latestSignalDate,
    reasons,
    headline,
  };
}

export interface BuildConvergenceFeedParams {
  dualConvictionSignals?: DualConvictionSignal[];
  form4Signals?: Record<string, Form4InsiderSummary>;
  midQuarterAlerts?: MidQuarterAlert[];
  stocksIndex?: Record<string, StockIndexEntry>;
  convictionByTicker?: Record<string, UnifiedConvictionRow>;
}

/**
 * Merge backend precomputed `dualConvictionSignals` with live client-side cross-referencing
 * across `form4Signals`, `midQuarterAlerts`, `convictionByTicker`, and `stocksIndex` so
 * Schedule 13D/13G catalysts and Form 4 clusters render consistently before & after data builds.
 */
export function buildConvergenceFeed(
  params: BuildConvergenceFeedParams
): DualConvictionSignal[] {
  const byTicker = new Map<string, DualConvictionSignal>();

  for (const sig of params.dualConvictionSignals ?? []) {
    const tk = sig.ticker.toUpperCase();
    byTicker.set(tk, sig);
  }

  const alertsByTicker = new Map<string, MidQuarterAlert[]>();
  for (const al of params.midQuarterAlerts ?? []) {
    const tk = (al.ticker || '').toUpperCase().trim();
    if (!tk) continue;
    const arr = alertsByTicker.get(tk) ?? [];
    arr.push(al);
    alertsByTicker.set(tk, arr);
  }

  const f4ByTicker = new Map<string, Form4InsiderSummary>();
  for (const [k, v] of Object.entries(params.form4Signals ?? {})) {
    f4ByTicker.set(k.toUpperCase(), v);
  }

  const candidateTickers = new Set<string>([
    ...f4ByTicker.keys(),
    ...alertsByTicker.keys(),
  ]);

  for (const tk of candidateTickers) {
    if (byTicker.has(tk)) continue;
    const f4 = f4ByTicker.get(tk) ?? null;
    const alerts = alertsByTicker.get(tk) ?? [];
    const idxEntry = params.stocksIndex?.[tk] ?? null;
    const unified = params.convictionByTicker?.[tk] ?? null;

    let newCount = unified?.newCount ?? 0;
    let addedCount = unified?.addedCount ?? 0;
    const trimmedCount = unified?.trimmedCount ?? 0;
    const exitedCount = unified?.exitedCount ?? 0;
    let eliteBuyersCount =
      unified?.eliteBuyersCount ?? idxEntry?.eliteBuyersCount ?? 0;
    const eliteSellersCount =
      unified?.eliteSellersCount ?? idxEntry?.eliteSellersCount ?? 0;
    const eliteHoldersCount =
      unified?.eliteHoldersCount ?? idxEntry?.eliteHoldersCount ?? 0;

    if (!unified && idxEntry) {
      const sv = idxEntry.signalVerdict || '';
      if (sv.includes('Cluster')) newCount = 2;
      else if (sv.includes('Elite Consensus') && idxEntry.eliteHoldersCount >= 2) addedCount = 2;
      if (idxEntry.eliteBuyersCount == null && (newCount > 0 || addedCount > 0)) {
        eliteBuyersCount = Math.min(2, idxEntry.eliteHoldersCount);
      }
    }

    const name =
      unified?.name ||
      idxEntry?.name ||
      alerts[0]?.issuerName ||
      tk;

    const sig = computeDualConvictionSignal({
      ticker: tk,
      name,
      sector: unified?.sector || idxEntry?.sector || 'Other',
      subSector: unified?.subSector || idxEntry?.subSector || 'Other',
      form4Summary: f4,
      alerts,
      priceUsd: unified?.priceUsd ?? idxEntry?.priceUsd ?? null,
      estQuarterVwap: unified?.estQuarterVwap ?? null,
      pctVsQuarterVwap: unified?.pctVsQuarterVwap ?? idxEntry?.pctVsQuarterVwap ?? null,
      alphaDecayStatus: unified?.alphaDecayStatus ?? idxEntry?.alphaDecayStatus ?? null,
      actionabilityScore: unified?.actionabilityScore ?? idxEntry?.actionabilityScore ?? null,
      actionVerdict: unified?.actionVerdict ?? idxEntry?.actionVerdict ?? null,
      newCount,
      addedCount,
      trimmedCount,
      exitedCount,
      eliteBuyersCount,
      eliteSellersCount,
      eliteHoldersCount,
      netEstFlowUsd: unified?.netFlowUsd ?? null,
    });

    if (sig) {
      byTicker.set(tk, sig);
    }
  }

  return Array.from(byTicker.values()).sort((a, b) => {
    const tDiff =
      (CONVERGENCE_TIER_ORDER[b.tier] ?? 0) - (CONVERGENCE_TIER_ORDER[a.tier] ?? 0);
    if (tDiff !== 0) return tDiff;
    if (b.convergenceScore !== a.convergenceScore) {
      return b.convergenceScore - a.convergenceScore;
    }
    if (b.insiderBuyValueUsd !== a.insiderBuyValueUsd) {
      return b.insiderBuyValueUsd - a.insiderBuyValueUsd;
    }
    return (b.latestSignalDate ?? '').localeCompare(a.latestSignalDate ?? '');
  });
}

export function buildDualConvictionLookup(
  feed: DualConvictionSignal[]
): Record<string, DualConvictionSignal> {
  const out: Record<string, DualConvictionSignal> = {};
  for (const sig of feed) {
    out[sig.ticker.toUpperCase()] = sig;
  }
  return out;
}
