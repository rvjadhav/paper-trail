import type {
  ActionVerdict,
  AlphaDecayStatus,
  ConvergenceTier,
  EntryZoneStatus,
  FundTier,
  InsiderSignalLabel,
  ReturnMode,
  ReturnWindow,
} from '../types/data';

export const RETURN_WINDOWS: Array<{ id: ReturnWindow; label: string; fullLabel: string }> = [
  { id: 'YTD', label: 'YTD', fullLabel: 'Year to Date' },
  { id: 'TTM', label: 'TTM', fullLabel: 'Trailing 12 Months' },
  { id: '18M', label: '18M', fullLabel: '18 Months' },
  { id: '2Y', label: '2Y', fullLabel: '2 Years' },
  { id: '3Y', label: '3Y', fullLabel: '3 Years' },
  { id: '5Y', label: '5Y', fullLabel: '5 Years' },
];

export const SECTOR_COLORS: Record<string, string> = {
  Technology: 'var(--accent-primary)',
  'Communication Services': '#32ade6',
  'Consumer Cyclical': '#00c7be',
  'Financial Services': 'var(--chart-indigo)',
  Industrials: '#ff9500',
  Healthcare: '#34c759',
  Utilities: '#30b0c7',
  Energy: '#ff6b22',
  'Consumer Defensive': 'var(--chart-gray)',
  Semiconductors: 'var(--accent-primary)',
  'Enterprise Software': 'var(--chart-indigo)',
  'Platforms & Interactive': '#32ade6',
  'Platforms & Tech': '#32ade6',
  'Hardware & IT Services': 'var(--chart-teal)',
  'Fintech & Payments': '#00c7be',
  Other: '#aeaeb2',
};

export function sectorColor(sector: string): string {
  return SECTOR_COLORS[sector] ?? 'var(--chart-gray)';
}

export function formatUsdCompact(val: number | null | undefined): string {
  if (val == null || !Number.isFinite(val)) return '—';
  const abs = Math.abs(val);
  if (abs >= 1e12) return `$${(val / 1e12).toFixed(2)}T`;
  if (abs >= 1e9) return `$${(val / 1e9).toFixed(2)}B`;
  if (abs >= 1e6) return `$${(val / 1e6).toFixed(1)}M`;
  if (abs >= 1e3) return `$${(val / 1e3).toFixed(0)}K`;
  return `$${val.toFixed(0)}`;
}

export function formatSignedUsd(val: number | null | undefined): string {
  if (val == null || !Number.isFinite(val)) return '—';
  if (val === 0) return '$0';
  return `${val > 0 ? '+' : '-'}${formatUsdCompact(Math.abs(val))}`;
}

export function formatSignedPct(val: number | null | undefined, decimals = 1): string {
  if (val == null || !Number.isFinite(val)) return 'N/A';
  const sign = val > 0 ? '+' : '';
  return `${sign}${val.toFixed(decimals)}%`;
}

export function formatPct(val: number | null | undefined, decimals = 2): string {
  if (val == null || !Number.isFinite(val)) return '—';
  return `${val.toFixed(decimals)}%`;
}

export function formatShares(val: number | null | undefined): string {
  if (val == null || !Number.isFinite(val)) return '—';
  return val.toLocaleString('en-US', { maximumFractionDigits: 0 });
}

export function compactShares(sh: number): string {
  const abs = Math.abs(sh);
  if (abs >= 1e6) return `${(sh / 1e6).toFixed(2)}M`;
  if (abs >= 1e3) return `${(sh / 1e3).toFixed(0)}k`;
  return sh.toLocaleString('en-US', { maximumFractionDigits: 0 });
}

export function returnClass(val: number | null | undefined): string {
  if (val == null || !Number.isFinite(val)) return 'ret-muted';
  if (val > 0) return 'ret-pos';
  if (val < 0) return 'ret-neg';
  return 'ret-muted';
}

export function entryZoneChipClass(zone: EntryZoneStatus | null | undefined): string {
  if (!zone) return '';
  if (zone === 'Discount to Q-Avg') return 'pos';
  if (zone === 'Near Q-Avg') return 'tech';
  if (zone === 'Extended') return 'warn';
  return 'neg';
}

export function alphaDecayChipClass(status: AlphaDecayStatus | null | undefined): string {
  if (status === 'LAG_ADVANTAGE') return 'pos';
  if (status === 'FOMO_TRAP') return 'neg';
  if (status === 'POST_FILING_EXTENDED') return 'warn';
  return '';
}

export function alphaDecayLabel(status: AlphaDecayStatus | null | undefined): string {
  if (status === 'LAG_ADVANTAGE') return 'Lag Advantage';
  if (status === 'FOMO_TRAP') return 'FOMO / Late Crowded';
  if (status === 'POST_FILING_EXTENDED') return 'Post-Filing Extended';
  return 'Q-VWAP Neutral';
}

export function actionVerdictChipClass(verdict: ActionVerdict | null | undefined): string {
  if (verdict === 'STRONG ACCUMULATE') return 'pos';
  if (verdict === 'ACCUMULATE ON DIP') return 'tech';
  if (verdict === 'DISTRIBUTION WARNING') return 'warn';
  if (verdict === 'EXIT SIGNAL') return 'neg';
  return '';
}

export function actionVerdictShortLabel(verdict: ActionVerdict | null | undefined): string {
  if (verdict === 'STRONG ACCUMULATE') return 'Strong Accumulate';
  if (verdict === 'ACCUMULATE ON DIP') return 'Accumulate on Dip';
  if (verdict === 'HOLD') return 'Hold';
  if (verdict === 'DISTRIBUTION WARNING') return 'Distribution Warning';
  if (verdict === 'EXIT SIGNAL') return 'Exit Signal';
  return 'Hold';
}

export function tierChipClass(tier: FundTier | string | undefined): string {
  if (tier === 'Elite 50') return 'elite';
  if (tier === 'Top 100') return 'tech';
  if (tier === 'Excluded') return 'warn';
  return '';
}

export function stakeStatusChipClass(status: string | null | undefined): string {
  if (!status) return '';
  if (status.startsWith('Exited')) return 'neg';
  if (status.startsWith('Reduced')) return 'warn';
  if (status.startsWith('Active')) return 'pos';
  return '';
}

export function convergenceTierLabel(tier: ConvergenceTier | string | null | undefined): string {
  if (tier === 'DUAL_CONVICTION_PRIME') return 'Prime Dual-Conviction';
  if (tier === 'ACTIVIST_13F_CONVERGENCE') return '13D Activist + 13F';
  if (tier === 'DUAL_CONVICTION_EXTENDED') return 'Dual-Conviction (Extended)';
  if (tier === 'INSIDER_CLUSTER_WATCH') return 'Insider Cluster Watch';
  if (tier === 'ACTIVIST_13D_CATALYST') return 'Activist 13D Catalyst';
  return 'Convergence Signal';
}

export function convergenceTierChipClass(tier: ConvergenceTier | string | null | undefined): string {
  if (tier === 'DUAL_CONVICTION_PRIME') return 'pos';
  if (tier === 'ACTIVIST_13F_CONVERGENCE') return 'tech';
  if (tier === 'DUAL_CONVICTION_EXTENDED') return 'warn';
  if (tier === 'INSIDER_CLUSTER_WATCH') return 'elite';
  if (tier === 'ACTIVIST_13D_CATALYST') return 'warn';
  return '';
}

export function insiderSignalLabelText(label: InsiderSignalLabel | string | null | undefined): string {
  if (label === 'CLUSTER_BUY') return 'Form 4 Cluster Buy';
  if (label === 'C_SUITE_BUY') return 'C-Suite Buy';
  if (label === 'DIRECTOR_BUY') return 'Director Buy';
  if (label === 'SINGLE_BUY') return 'Open-Market Buy';
  if (label === 'MIXED_SELL_HEAVY') return 'Mixed (Sell-Heavy)';
  return 'No Open-Market Buys';
}

export function insiderSignalChipClass(label: InsiderSignalLabel | string | null | undefined): string {
  if (label === 'CLUSTER_BUY' || label === 'C_SUITE_BUY') return 'pos';
  if (label === 'DIRECTOR_BUY' || label === 'SINGLE_BUY') return 'tech';
  if (label === 'MIXED_SELL_HEAVY') return 'warn';
  return '';
}

export function item4CategoryChipClass(category: string | null | undefined): string {
  if (!category) return '';
  if (category === 'Strategic Review / M&A' || category === 'Board / Governance') return 'neg';
  if (
    category === 'Capital Return / Value' ||
    category === 'Management Engagement' ||
    category === 'Active 13D'
  ) {
    return 'warn';
  }
  return '';
}

export function modeLabel(mode: ReturnMode): string {
  return mode === 'quarter_end'
    ? 'Quarter-End Attribution'
    : '45-Day Filing-Lag Actionable';
}

export function downloadCsv(
  filename: string,
  headers: string[],
  rows: Array<Array<string | number | null | undefined>>
): void {
  const esc = (v: string | number | null | undefined) => {
    const s = v == null ? '' : String(v);
    if (s.includes(',') || s.includes('"') || s.includes('\n')) {
      return `"${s.replace(/"/g, '""')}"`;
    }
    return s;
  };
  const csv = [headers.map(esc).join(','), ...rows.map((r) => r.map(esc).join(','))].join('\n');
  const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}
