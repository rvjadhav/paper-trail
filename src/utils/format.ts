import type { EntryZoneStatus, FundTier, ReturnMode, ReturnWindow } from '../types/data';

export const RETURN_WINDOWS: Array<{ id: ReturnWindow; label: string; fullLabel: string }> = [
  { id: 'YTD', label: 'YTD', fullLabel: 'Year to Date' },
  { id: 'TTM', label: 'TTM', fullLabel: 'Trailing 12 Months' },
  { id: '18M', label: '18M', fullLabel: '18 Months' },
  { id: '2Y', label: '2Y', fullLabel: '2 Years' },
  { id: '3Y', label: '3Y', fullLabel: '3 Years' },
  { id: '5Y', label: '5Y', fullLabel: '5 Years' },
];

export const SECTOR_COLORS: Record<string, string> = {
  Technology: '#0071e3',
  'Communication Services': '#32ade6',
  'Consumer Cyclical': '#00c7be',
  'Financial Services': '#5856d6',
  Industrials: '#ff9500',
  Healthcare: '#34c759',
  Utilities: '#30b0c7',
  Energy: '#ff6b22',
  'Consumer Defensive': '#8e8e93',
  Semiconductors: '#0071e3',
  'Enterprise Software': '#5856d6',
  'Platforms & Interactive': '#32ade6',
  'Platforms & Tech': '#32ade6',
  'Hardware & IT Services': '#00a388',
  'Fintech & Payments': '#00c7be',
  Other: '#aeaeb2',
};

export function sectorColor(sector: string): string {
  return SECTOR_COLORS[sector] ?? '#8e8e93';
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

export function tierChipClass(tier: FundTier | string | undefined): string {
  if (tier === 'Elite 50') return 'elite';
  if (tier === 'Top 100') return 'tech';
  if (tier === 'Excluded') return 'warn';
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
