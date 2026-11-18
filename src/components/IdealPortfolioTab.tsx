import { useMemo, useState, type FC } from 'react';
import { Calculator, Check, Download, RotateCcw, Save } from 'lucide-react';
import type {
  BenchmarkSeries,
  IdealPortfolioData,
  IdealPortfolioPosition,
  ReturnMode,
  StockIndexEntry,
} from '../types/data';
import {
  downloadCsv,
  entryZoneChipClass,
  formatPct,
  formatSignedPct,
  formatUsdCompact,
  returnClass,
} from '../utils/format';
import { EquityCurveChart, SectorBreakdownBar } from './SvgCharts';

interface IdealPortfolioTabProps {
  idealPortfolio: IdealPortfolioData;
  benchmarks: Record<'QQQ' | 'SPY' | 'XLK', BenchmarkSeries>;
  stocksIndex: Record<string, StockIndexEntry>;
  mode: ReturnMode;
  onModeChange: (m: ReturnMode) => void;
  onSelectFund: (cik: string) => void;
  onSelectStock: (ticker: string) => void;
}

type UniverseFilter = 'ALL' | 'EX_MEGA' | 'EMERGING';
type SignalMode = 'rawScore' | 'inflectionScore';
type SectorCapOption = null | 35 | 25;

interface SavedBuilderPreset {
  universeFilter: UniverseFilter;
  signalMode: SignalMode;
  maxNames: number;
  capPct: number;
  sectorCapPct: SectorCapOption;
  capitalUsd: number;
}

const PRESET_STORAGE_KEY = 'paper-trail-builder-preset-v1';

function loadSavedPreset(): Partial<SavedBuilderPreset> {
  try {
    const raw = window.localStorage.getItem(PRESET_STORAGE_KEY);
    if (!raw) return {};
    return JSON.parse(raw) as Partial<SavedBuilderPreset>;
  } catch {
    return {};
  }
}

interface DynamicPosition extends IdealPortfolioPosition {
  dynamicWeightPct: number;
  weightVsDefaultPct: number | null;
}

function buildConstrainedPortfolio(
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
      // Only take another name from a sub-sector in Pass 1 if it adds capacity toward the sub-sector cap
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
    // 1. Single-stock cap projection
    for (let i = 0; i < weights.length; i++) {
      if (weights[i] > effStockCap) weights[i] = effStockCap;
    }

    // 2. Sub-sector cap projection
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

    // 3. Reallocate remaining budget only to positions with both stock & sector headroom
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

  // Sort descending & round to 2 decimals without breaching stock or sector caps
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
    // Prefer items farthest from ceiling (when adding) or largest weight (when trimming)
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

const DUAL_ALIAS_MAP: Record<string, string> = {
  GOOG: 'GOOGL',
  FOXA: 'FOX',
  NWSA: 'NWS',
  BRKB: 'BRK.B',
  'BRK-B': 'BRK.B',
};

function parseHoldingsInput(rawText: string, capitalUsd: number): Map<string, number> {
  const out = new Map<string, number>();
  if (!rawText.trim()) return out;

  const chunks = rawText
    .split(/\n|;|,(?=\s*[A-Za-z])/g)
    .map((s) => s.trim())
    .filter(Boolean);

  for (const chunk of chunks) {
    const m = chunk.match(/^([A-Za-z][A-Za-z0-9.-]{0,9})[\s:=]+(\$?[\d,]+(?:\.\d+)?%?)$/);
    if (!m) continue;
    let sym = m[1].toUpperCase();
    sym = DUAL_ALIAS_MAP[sym] ?? sym;
    const rawVal = m[2];
    const isPct = rawVal.endsWith('%');
    const num = Number(rawVal.replace(/[$,%]/g, ''));
    if (!Number.isFinite(num) || num < 0) continue;
    const usd = isPct ? (num / 100) * capitalUsd : num;
    out.set(sym, (out.get(sym) ?? 0) + usd);
  }
  return out;
}

export const IdealPortfolioTab: FC<IdealPortfolioTabProps> = ({
  idealPortfolio,
  benchmarks,
  stocksIndex,
  mode,
  onModeChange,
  onSelectFund,
  onSelectStock,
}) => {
  const initialPreset = useMemo(() => loadSavedPreset(), []);
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

  const [showRebalance, setShowRebalance] = useState(false);
  const [holdingsText, setHoldingsText] = useState('');

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

  const rebalancePlan = useMemo(() => {
    const currentMap = parseHoldingsInput(holdingsText, capitalUsd);
    if (currentMap.size === 0) return null;

    const targetMap = new Map<string, DynamicPosition>(
      activePositions.map((p) => [p.ticker, p])
    );
    const allTickers = new Set<string>([
      ...Array.from(currentMap.keys()),
      ...Array.from(targetMap.keys()),
    ]);

    const rows = Array.from(allTickers).map((ticker) => {
      const currUsd = currentMap.get(ticker) ?? 0;
      const targetPos = targetMap.get(ticker);
      const targetWeight = targetPos?.dynamicWeightPct ?? 0;
      const targetUsd = (capitalUsd * targetWeight) / 100;
      const deltaUsd = targetUsd - currUsd;
      const priceUsd = targetPos?.priceUsd ?? stocksIndex[ticker]?.priceUsd ?? null;
      const estSharesDelta =
        priceUsd && priceUsd > 0 ? Math.round(deltaUsd / priceUsd) : null;
      const threshold = Math.max(50, capitalUsd * 0.0025);

      let action: 'BUY' | 'ADD' | 'TRIM' | 'EXIT' | 'HOLD';
      if (currUsd <= 0 && targetUsd > 0) action = 'BUY';
      else if (targetUsd <= 0 && currUsd > 0) action = 'EXIT';
      else if (Math.abs(deltaUsd) < threshold) action = 'HOLD';
      else if (deltaUsd > 0) action = 'ADD';
      else action = 'TRIM';

      return {
        ticker,
        name: targetPos?.name ?? stocksIndex[ticker]?.name ?? ticker,
        action,
        currUsd,
        currPct: capitalUsd > 0 ? (currUsd / capitalUsd) * 100 : 0,
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

    const totalCurrentUsd = Array.from(currentMap.values()).reduce((a, b) => a + b, 0);
    const totalBuyUsd = rows.filter((r) => r.deltaUsd > 0).reduce((s, r) => s + r.deltaUsd, 0);
    const totalSellUsd = rows.filter((r) => r.deltaUsd < 0).reduce((s, r) => s + Math.abs(r.deltaUsd), 0);
    return {
      rows,
      totalCurrentUsd,
      totalBuyUsd,
      totalSellUsd,
      cashDeltaUsd: capitalUsd - totalCurrentUsd,
    };
  }, [holdingsText, capitalUsd, activePositions, stocksIndex]);

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
      `paper-trail-ideal-portfolio-${universeFilter.toLowerCase()}-${signalMode}-${activePositions.length}names.csv`,
      [
        'Rank',
        'Ticker',
        'Company',
        'Sub-Sector',
        'Market Cap Tier',
        'Ideal Weight (%)',
        'Delta vs Default (pts)',
        'Allocation (USD)',
        'Est. Q-VWAP (USD)',
        'vs Q-VWAP (%)',
        'Entry Zone',
        'XBRL Rev YoY (%)',
        'XBRL Op Margin (%)',
        'Conviction Score',
        'Ranked Holders',
        'Avg Fund Weight (%)',
        'Signal Tag',
        'Top Backers',
      ],
      activePositions.map((p, idx) => [
        idx + 1,
        p.ticker,
        p.name,
        p.subSector || p.sector,
        p.marketCapTier ?? '',
        p.dynamicWeightPct,
        p.weightVsDefaultPct ?? '',
        Math.round((capitalUsd * p.dynamicWeightPct) / 100),
        p.estQuarterVwap ?? '',
        p.pctVsQuarterVwap ?? '',
        p.entryZoneStatus ?? '',
        p.fundamentals?.revenueGrowthYoYPct ?? '',
        p.fundamentals?.operatingMarginPct ?? '',
        p.convictionScore,
        p.holderCount,
        p.avgFundWeightPct,
        p.rationaleTag,
        p.topBackers.map((b) => `${b.fundName} (${b.weightPct.toFixed(1)}%)`).join('; '),
      ])
    );
  };

  const handleExportRebalance = () => {
    if (!rebalancePlan) return;
    downloadCsv(
      `paper-trail-rebalance-trades-${activePositions.length}names.csv`,
      ['Action', 'Ticker', 'Company', 'Current (USD)', 'Current (%)', 'Target (USD)', 'Target (%)', 'Trade Delta (USD)', 'Est. Shares'],
      rebalancePlan.rows.map((r) => [
        r.action,
        r.ticker,
        r.name,
        Math.round(r.currUsd),
        r.currPct.toFixed(2),
        Math.round(r.targetUsd),
        r.targetPct.toFixed(2),
        Math.round(r.deltaUsd),
        r.estSharesDelta ?? '',
      ])
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
            {savedToast ? <Check size={14} color="#00823b" /> : <Save size={14} />}
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
                <Calculator size={16} color="#0071e3" /> Portfolio Rebalance Trade Calculator
              </h3>
              <p className="panel-subtitle">
                Paste current positions in USD or % (e.g. <code>NVDA 20000</code> or <code>MSFT 15%</code>) to generate exact BUY / ADD / TRIM / EXIT orders against your {activePositions.length}-name target model (${capitalUsd.toLocaleString()} capital).
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

          <div style={{ padding: '1rem 1.25rem', display: 'grid', gridTemplateColumns: 'minmax(240px, 320px) 1fr', gap: '1.15rem', alignItems: 'start' }}>
            <div>
              <label htmlFor="rebalance-input" style={{ display: 'block', fontSize: '0.76rem', fontWeight: 600, color: 'var(--text-secondary)', marginBottom: '0.35rem' }}>
                Your Current Holdings (one per line or comma-separated)
              </label>
              <textarea
                id="rebalance-input"
                rows={7}
                className="rebalance-textarea mono"
                placeholder={`NVDA 22000\nMSFT 16000\nMETA 12%\nINTC 8000`}
                value={holdingsText}
                onChange={(e) => setHoldingsText(e.target.value)}
              />
              {rebalancePlan && (
                <div className="mono" style={{ fontSize: '0.75rem', color: 'var(--text-secondary)', marginTop: '0.6rem', display: 'flex', flexDirection: 'column', gap: '0.2rem' }}>
                  <span>Current Invested: <strong>{formatUsdCompact(rebalancePlan.totalCurrentUsd)}</strong></span>
                  <span className="ret-pos">Total Buys / Adds: <strong>+{formatUsdCompact(rebalancePlan.totalBuyUsd)}</strong></span>
                  <span className="ret-neg">Total Sells / Trims: <strong>-{formatUsdCompact(rebalancePlan.totalSellUsd)}</strong></span>
                  <span>
                    Net Cash Needed:{' '}
                    <strong>
                      {rebalancePlan.cashDeltaUsd >= 0 ? '+' : '-'}
                      {formatUsdCompact(Math.abs(rebalancePlan.cashDeltaUsd))}
                    </strong>
                  </span>
                </div>
              )}
            </div>

            <div>
              {rebalancePlan ? (
                <div className="table-scroll" style={{ maxHeight: '300px', overflowY: 'auto' }}>
                  <table className="data-table compact">
                    <thead>
                      <tr>
                        <th>Action</th>
                        <th>Stock</th>
                        <th className="num">Current ($)</th>
                        <th className="num">Target ($)</th>
                        <th className="num">Trade Delta ($)</th>
                        <th className="num">Est. Shares</th>
                      </tr>
                    </thead>
                    <tbody>
                      {rebalancePlan.rows.map((r) => (
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
                          <td className="num mono">
                            {formatUsdCompact(r.currUsd)} ({r.currPct.toFixed(1)}%)
                          </td>
                          <td className="num mono">
                            {formatUsdCompact(r.targetUsd)} ({r.targetPct.toFixed(1)}%)
                          </td>
                          <td className={`num mono ${returnClass(r.deltaUsd)}`}>
                            {r.deltaUsd >= 0 ? '+' : '-'}
                            {formatUsdCompact(Math.abs(r.deltaUsd))}
                          </td>
                          <td className={`num mono ${returnClass(r.estSharesDelta)}`}>
                            {r.estSharesDelta != null
                              ? `${r.estSharesDelta > 0 ? '+' : ''}${r.estSharesDelta.toLocaleString()} sh`
                              : '—'}
                          </td>
                        </tr>
                      ))}
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
                  color: '#00a388',
                  points: idealPortfolio.backtest.filing_lag.curve,
                },
                {
                  id: 'ideal-qe',
                  label: 'Ideal Portfolio (QE)',
                  color: '#0071e3',
                  dashed: true,
                  points: idealPortfolio.backtest.quarter_end.curve,
                },
                {
                  id: 'qqq',
                  label: 'QQQ',
                  color: '#5856d6',
                  dashed: true,
                  points: benchmarks.QQQ.curve,
                },
                {
                  id: 'spy',
                  label: 'SPY',
                  color: '#8e8e93',
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
              Ideal Portfolio Allocation ({activePositions.length} Positions · 100.00%)
            </h3>
            <p className="panel-subtitle">
              Click any stock ticker for full Q-VWAP &amp; XBRL Stock Dossier, or click any backing fund to inspect its 13F book
            </p>
          </div>
          <button type="button" className="action-btn" onClick={handleExport}>
            <Download size={14} /> Export Ideal Portfolio CSV
          </button>
        </div>

        <div className="table-scroll">
          <table className="data-table">
            <thead>
              <tr>
                <th>#</th>
                <th>Stock</th>
                <th>Sub-Sector</th>
                <th className="num">Ideal Weight</th>
                <th className="num">Target ($)</th>
                <th>Entry Zone (Q-VWAP)</th>
                <th className="num">XBRL Rev / OpM</th>
                <th className="num">Conviction</th>
                <th>Signal Driver</th>
                <th className="num">Holders</th>
                <th className="num">TTM</th>
                <th>Top Backing Funds</th>
              </tr>
            </thead>
            <tbody>
              {activePositions.map((p, idx) => {
                const targetUsd = (capitalUsd * p.dynamicWeightPct) / 100;
                const estShares =
                  p.priceUsd && p.priceUsd > 0 ? Math.floor(targetUsd / p.priceUsd) : null;
                return (
                  <tr key={p.ticker}>
                    <td>
                      <span className={`rank-badge ${idx < 3 ? 'top3' : ''}`}>#{idx + 1}</span>
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
                    <td style={{ minWidth: '145px' }}>
                      {p.entryZoneStatus ? (
                        <div>
                          <span className={`chip ${entryZoneChipClass(p.entryZoneStatus)}`}>
                            {p.entryZoneStatus}
                          </span>
                          <div className="mono" style={{ fontSize: '0.68rem', color: 'var(--text-muted)', marginTop: '0.12rem' }}>
                            {p.pctVsQuarterVwap != null ? `${formatSignedPct(p.pctVsQuarterVwap)} vs ` : ''}
                            {p.estQuarterVwap != null ? `$${p.estQuarterVwap.toFixed(2)}` : ''}
                          </div>
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
              {activePositions.length === 0 && (
                <tr>
                  <td colSpan={12} style={{ textAlign: 'center', padding: '2rem', color: 'var(--text-muted)' }}>
                    No stocks match this market-cap filter. Switch Universe back to All-Cap Tech or Ex-Mega.
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
