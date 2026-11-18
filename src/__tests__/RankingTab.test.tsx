import { fireEvent, render, screen, within } from '@testing-library/react';
import { useState } from 'react';
import { describe, expect, it, vi } from 'vitest';
import { RankingTab } from '../components/RankingTab';
import type {
  BenchmarkSeries,
  FundSummary,
  RankedWindowEntry,
  ReturnMode,
  ReturnWindow,
} from '../types/data';

function makeBench(ticker: string, name: string): BenchmarkSeries {
  const win = { YTD: 10, TTM: 15, '18M': 20, '2Y': 25, '3Y': 35, '5Y': 50 };
  return {
    ticker,
    name,
    returns: win,
    cagrPct: 15,
    maxDrawdownPct: -12,
    sharpeProxy: 1.2,
    sortinoRatio: 1.7,
    curve: [
      { date: '2025-01-15', nav: 100 },
      { date: '2026-01-15', nav: 115 },
    ],
  };
}

function makeFund(overrides: Partial<FundSummary> & { cik: string; name: string }): FundSummary {
  const { cik, name, ...rest } = overrides;
  return {
    cik,
    shard: '0',
    name,
    legalName: `${name} LLC`,
    manager: 'Test Manager',
    hq: 'New York, NY',
    styleTag: 'Concentrated Tech',
    strategySummary: 'Tech growth equity',
    source: 'Curated Core',
    fundTier: 'Elite 50',
    isLeaderboardEligible: true,
    exclusionReason: null,
    latestPeriod: '2026-09-30',
    latestQuarterLabel: '2026 Q3',
    isStaleQuarter: false,
    latestFilingDate: '2026-11-16',
    equityAumUsd: 1_000_000_000,
    totalReportedUsd: 1_000_000_000,
    equityPositionCount: 20,
    totalPositionCount: 20,
    top10WeightPct: 70,
    effectivePositions: 10,
    techTmtPct: 85,
    etfSharePct: 0,
    quartersTracked: 12,
    returns: {
      quarter_end: { YTD: 10, TTM: 15, '18M': 20, '2Y': 25, '3Y': 30, '5Y': 45 },
      filing_lag: { YTD: 12, TTM: 18, '18M': 22, '2Y': 28, '3Y': 35, '5Y': 50 },
    },
    metrics: {
      quarter_end: {
        cagrPct: 16,
        maxDrawdownPct: -10,
        sharpeProxy: 1.3,
        sortinoRatio: 1.9,
        infoRatioVsQqq: 0.7,
        betaVsQqq: 1.0,
        alphaVsQqqPct: 3,
        annualizedVolPct: 17,
        battingAvgPct: 60,
        avgTurnoverPct: 15,
        skillScore: 75,
        skillRank: 1,
      },
      filing_lag: {
        cagrPct: 18,
        maxDrawdownPct: -10,
        sharpeProxy: 1.4,
        sortinoRatio: 2.0,
        infoRatioVsQqq: 0.8,
        betaVsQqq: 1.0,
        alphaVsQqqPct: 4,
        annualizedVolPct: 17,
        battingAvgPct: 65,
        avgTurnoverPct: 15,
        skillScore: 78,
        skillRank: 1,
      },
    },
    topHoldingsPreview: [{ ticker: 'NVDA', name: 'NVIDIA Corp', weightPct: 10 }],
    sectorBreakdown: [{ sector: 'Technology', weightPct: 85 }],
    searchableTickers: ['NVDA'],
    edgarUrl: 'https://www.sec.gov',
    ...rest,
  };
}

function makeEmptyRankings(): Record<ReturnMode, Record<ReturnWindow, RankedWindowEntry[]>> {
  const rec = {
    YTD: [],
    TTM: [],
    '18M': [],
    '2Y': [],
    '3Y': [],
    '5Y': [],
  };
  return { quarter_end: rec, filing_lag: rec };
}

function RankingHarness({ funds }: { funds: FundSummary[] }) {
  const [windowSize, setWindowSize] = useState<ReturnWindow>('TTM');
  const [mode, setMode] = useState<ReturnMode>('filing_lag');

  return (
    <RankingTab
      funds={funds}
      rankings={makeEmptyRankings()}
      benchmarks={{
        QQQ: makeBench('QQQ', 'Invesco QQQ'),
        SPY: makeBench('SPY', 'SPDR S&P 500'),
        XLK: makeBench('XLK', 'Tech Select SPDR'),
      }}
      stocksIndex={{}}
      latestQuarterCovered="2026 Q3"
      windowSize={windowSize}
      onWindowChange={setWindowSize}
      mode={mode}
      onModeChange={setMode}
      onSelectFund={vi.fn()}
      onSelectStock={vi.fn()}
    />
  );
}

function getVisibleFundNames(): string[] {
  const rows = Array.from(document.querySelectorAll('table.data-table tbody tr'));
  return rows.map((row) => row.querySelector('.fund-link-btn')?.textContent?.trim() ?? '');
}

describe('RankingTab Total Fund Size sorting', () => {
  const sampleFunds: FundSummary[] = [
    makeFund({
      cik: '0000000001',
      name: 'Small Skill Leader',
      equityAumUsd: 500_000_000,
      returns: {
        quarter_end: { YTD: 5, TTM: 10, '18M': 12, '2Y': 14, '3Y': 20, '5Y': 30 },
        filing_lag: { YTD: 5, TTM: 10, '18M': 12, '2Y': 14, '3Y': 20, '5Y': 30 },
      },
      metrics: {
        quarter_end: {
          cagrPct: 20,
          maxDrawdownPct: -8,
          sharpeProxy: 1.6,
          sortinoRatio: 2.5,
          infoRatioVsQqq: 1.1,
          betaVsQqq: 1.0,
          alphaVsQqqPct: 6,
          annualizedVolPct: 15,
          battingAvgPct: 75,
          avgTurnoverPct: 10,
          skillScore: 95,
          skillRank: 1,
        },
        filing_lag: {
          cagrPct: 20,
          maxDrawdownPct: -8,
          sharpeProxy: 1.6,
          sortinoRatio: 2.5,
          infoRatioVsQqq: 1.1,
          betaVsQqq: 1.0,
          alphaVsQqqPct: 6,
          annualizedVolPct: 15,
          battingAvgPct: 75,
          avgTurnoverPct: 10,
          skillScore: 95,
          skillRank: 1,
        },
      },
    }),
    makeFund({
      cik: '0000000002',
      name: 'Mega Capital',
      equityAumUsd: 25_000_000_000,
      returns: {
        quarter_end: { YTD: 18, TTM: 32, '18M': 35, '2Y': 40, '3Y': 50, '5Y': 80 },
        filing_lag: { YTD: 18, TTM: 32, '18M': 35, '2Y': 40, '3Y': 50, '5Y': 80 },
      },
      metrics: {
        quarter_end: {
          cagrPct: 15,
          maxDrawdownPct: -12,
          sharpeProxy: 1.2,
          sortinoRatio: 1.8,
          infoRatioVsQqq: 0.5,
          betaVsQqq: 1.0,
          alphaVsQqqPct: 2,
          annualizedVolPct: 18,
          battingAvgPct: 55,
          avgTurnoverPct: 14,
          skillScore: 70,
          skillRank: 3,
        },
        filing_lag: {
          cagrPct: 15,
          maxDrawdownPct: -12,
          sharpeProxy: 1.2,
          sortinoRatio: 1.8,
          infoRatioVsQqq: 0.5,
          betaVsQqq: 1.0,
          alphaVsQqqPct: 2,
          annualizedVolPct: 18,
          battingAvgPct: 55,
          avgTurnoverPct: 14,
          skillScore: 70,
          skillRank: 3,
        },
      },
    }),
    makeFund({
      cik: '0000000003',
      name: 'Mid Growth Fund',
      equityAumUsd: 4_200_000_000,
      returns: {
        quarter_end: { YTD: 40, TTM: 22, '18M': 24, '2Y': 28, '3Y': 36, '5Y': 60 },
        filing_lag: { YTD: 40, TTM: 22, '18M': 24, '2Y': 28, '3Y': 36, '5Y': 60 },
      },
      metrics: {
        quarter_end: {
          cagrPct: 18,
          maxDrawdownPct: -10,
          sharpeProxy: 1.4,
          sortinoRatio: 2.1,
          infoRatioVsQqq: 0.8,
          betaVsQqq: 1.0,
          alphaVsQqqPct: 4,
          annualizedVolPct: 16,
          battingAvgPct: 65,
          avgTurnoverPct: 12,
          skillScore: 84,
          skillRank: 2,
        },
        filing_lag: {
          cagrPct: 18,
          maxDrawdownPct: -10,
          sharpeProxy: 1.4,
          sortinoRatio: 2.1,
          infoRatioVsQqq: 0.8,
          betaVsQqq: 1.0,
          alphaVsQqqPct: 4,
          annualizedVolPct: 16,
          battingAvgPct: 65,
          avgTurnoverPct: 12,
          skillScore: 84,
          skillRank: 2,
        },
      },
    }),
  ];

  it('sorts descending on first click of Total Fund Size header, toggles ascending on second click, and resets to descending when switching from other columns', () => {
    render(<RankingHarness funds={sampleFunds} />);

    // Initial default sort is skillScore descending
    expect(getVisibleFundNames()).toEqual([
      'Small Skill Leader',
      'Mid Growth Fund',
      'Mega Capital',
    ]);

    const table = screen.getByRole('table');
    const fundSizeTh = within(table).getByRole('columnheader', { name: /Total Fund Size/i });
    expect(fundSizeTh).toHaveClass('sortable');
    expect(fundSizeTh).not.toHaveClass('active-col');

    // Click Total Fund Size -> sorts descending (largest AUM first)
    fireEvent.click(fundSizeTh);
    expect(fundSizeTh).toHaveTextContent('Total Fund Size ▼');
    expect(fundSizeTh).toHaveClass('active-col');
    expect(getVisibleFundNames()).toEqual([
      'Mega Capital',
      'Mid Growth Fund',
      'Small Skill Leader',
    ]);

    // Active cell highlighting on Total Fund Size td
    const firstRowCells = within(table).getAllByRole('row')[1].querySelectorAll('td');
    expect(firstRowCells[2]).toHaveClass('active-col');

    // Segmented group stays synced with Fund Size active
    const sortGroup = screen.getByRole('group', { name: 'Sort ranking metric' });
    expect(within(sortGroup).getByRole('button', { name: 'Fund Size' })).toHaveClass('active');

    // Click Total Fund Size again -> toggles ascending (smallest AUM first)
    fireEvent.click(fundSizeTh);
    expect(fundSizeTh).toHaveTextContent('Total Fund Size ▲');
    expect(getVisibleFundNames()).toEqual([
      'Small Skill Leader',
      'Mid Growth Fund',
      'Mega Capital',
    ]);

    // Click Total Fund Size third time -> toggles back to descending
    fireEvent.click(fundSizeTh);
    expect(fundSizeTh).toHaveTextContent('Total Fund Size ▼');
    expect(getVisibleFundNames()).toEqual([
      'Mega Capital',
      'Mid Growth Fund',
      'Small Skill Leader',
    ]);

    // Switch to YTD (sorts descending), toggle YTD ascending, then switch back to Total Fund Size
    const ytdTh = within(table).getByRole('columnheader', { name: /^YTD/i });
    fireEvent.click(ytdTh);
    expect(ytdTh).toHaveTextContent('YTD ▼');
    expect(getVisibleFundNames()).toEqual([
      'Mid Growth Fund',
      'Mega Capital',
      'Small Skill Leader',
    ]);

    fireEvent.click(ytdTh);
    expect(ytdTh).toHaveTextContent('YTD ▲');
    expect(getVisibleFundNames()).toEqual([
      'Small Skill Leader',
      'Mega Capital',
      'Mid Growth Fund',
    ]);

    // Clicking Total Fund Size from ascending YTD starts descending by fund size
    fireEvent.click(fundSizeTh);
    expect(fundSizeTh).toHaveTextContent('Total Fund Size ▼');
    expect(ytdTh).not.toHaveTextContent('▲');
    expect(ytdTh).not.toHaveTextContent('▼');
    expect(getVisibleFundNames()).toEqual([
      'Mega Capital',
      'Mid Growth Fund',
      'Small Skill Leader',
    ]);
  });

  it('supports Fund Size segmented button, direction toggle button, tie-breaking, and Top 100 cohort label', () => {
    const tiedFunds: FundSummary[] = [
      ...sampleFunds,
      makeFund({
        cik: '0000000004',
        name: 'Tied Top 100 Qualified',
        fundTier: 'Top 100',
        equityAumUsd: 4_200_000_000,
        metrics: {
          ...sampleFunds[2].metrics,
          filing_lag: { ...sampleFunds[2].metrics.filing_lag, skillScore: 90, skillRank: 4 },
        },
      }),
    ];

    render(<RankingHarness funds={tiedFunds} />);

    // Switch to Top 100 cohort so Tied Top 100 Qualified is included
    fireEvent.click(screen.getByRole('button', { name: 'Top 100' }));

    const sortGroup = screen.getByRole('group', { name: 'Sort ranking metric' });
    fireEvent.click(within(sortGroup).getByRole('button', { name: 'Fund Size' }));

    expect(screen.getByRole('heading', { level: 2 })).toHaveTextContent(
      'Top 4 Cohort (Total Fund Size Sort)'
    );

    // 25B first; between the two 4.2B funds, higher skillScore (90 vs 84) breaks the tie first
    expect(getVisibleFundNames()).toEqual([
      'Mega Capital',
      'Tied Top 100 Qualified',
      'Mid Growth Fund',
      'Small Skill Leader',
    ]);

    // Direction toggle button switches to ascending
    const dirBtn = document.getElementById('ranking-sort-dir-btn')!;
    fireEvent.click(dirBtn);
    expect(dirBtn).toHaveTextContent('▲');
    expect(
      within(screen.getByRole('table')).getByRole('columnheader', { name: /Total Fund Size/i })
    ).toHaveTextContent('Total Fund Size ▲');
    expect(getVisibleFundNames()).toEqual([
      'Small Skill Leader',
      'Tied Top 100 Qualified',
      'Mid Growth Fund',
      'Mega Capital',
    ]);
  });
});
