import { useRef, useState } from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import type {
  FundShardPayload,
  PaperTrailSummaryDataset,
  StockDetail,
} from '../types/data';
import * as dataLoader from '../utils/dataLoader';
import { useModalFocusTrap } from '../utils/useModalFocusTrap';
import { App } from '../App';

function FocusHarness() {
  const [open1, setOpen1] = useState(false);
  const [open2, setOpen2] = useState(false);
  const ref1 = useRef<HTMLDivElement>(null);
  const ref2 = useRef<HTMLDivElement>(null);

  useModalFocusTrap(ref1, open1);
  useModalFocusTrap(ref2, open2);

  return (
    <div>
      <button type="button" onClick={() => setOpen1(true)}>
        Open First
      </button>
      {open1 && (
        <div ref={ref1} tabIndex={-1} role="dialog" aria-label="First Modal">
          <button type="button" onClick={() => setOpen2(true)}>
            Open Second
          </button>
          <button type="button" onClick={() => setOpen1(false)}>
            Close First
          </button>
        </div>
      )}
      {open2 && (
        <div ref={ref2} tabIndex={-1} role="dialog" aria-label="Second Modal">
          <button type="button" onClick={() => setOpen2(false)}>
            Close Second
          </button>
        </div>
      )}
    </div>
  );
}

function makeEmptyWindows(val = 12) {
  return { YTD: val, TTM: val, '18M': val, '2Y': val, '3Y': val, '5Y': val };
}

function makeMockSummary(): PaperTrailSummaryDataset {
  const curve = [
    { date: '2025-01-15', nav: 100 },
    { date: '2026-01-15', nav: 120 },
  ];
  const emptyMetrics = {
    cagrPct: 20,
    maxDrawdownPct: -10,
    sharpeProxy: 1.4,
    sortinoRatio: 2.1,
    infoRatioVsQqq: 0.9,
    betaVsQqq: 1.1,
    alphaVsQqqPct: 4.5,
    annualizedVolPct: 18,
    battingAvgPct: 68,
    avgTurnoverPct: 14,
    skillScore: 88,
    skillRank: 1,
  };
  const bench = (ticker: string, name: string) => ({
    ticker,
    name,
    returns: makeEmptyWindows(15),
    cagrPct: 15,
    maxDrawdownPct: -12,
    sharpeProxy: 1.1,
    sortinoRatio: 1.6,
    curve,
  });
  const emptyWinRec = {
    YTD: [{ rank: 1, skillRank: 1, cik: '0001000002', returnPct: 25, skillScore: 88 }],
    TTM: [{ rank: 1, skillRank: 1, cik: '0001000002', returnPct: 25, skillScore: 88 }],
    '18M': [{ rank: 1, skillRank: 1, cik: '0001000002', returnPct: 25, skillScore: 88 }],
    '2Y': [{ rank: 1, skillRank: 1, cik: '0001000002', returnPct: 25, skillScore: 88 }],
    '3Y': [{ rank: 1, skillRank: 1, cik: '0001000002', returnPct: 25, skillScore: 88 }],
    '5Y': [{ rank: 1, skillRank: 1, cik: '0001000002', returnPct: 25, skillScore: 88 }],
  };

  return {
    metadata: {
      generatedAt: '2026-11-17T05:15:00Z',
      asOfDate: '2026-11-16',
      latestQuarterCovered: '2026 Q3',
      fundsEvaluatedCount: 100,
      qualifiedFundsCount: 60,
      leaderboardEligibleCount: 50,
      elite50Count: 50,
      rankedFundsUnionCount: 50,
      total13fFilingsProcessed: 1200,
      next13fSeasonLabel: 'Feb 2027',
      riskFreeLatestPct: 4.5,
      dataSources: ['SEC EDGAR Form 13F'],
    },
    benchmarks: {
      QQQ: bench('QQQ', 'Invesco QQQ'),
      SPY: bench('SPY', 'SPDR S&P 500'),
      XLK: bench('XLK', 'Tech Select SPDR'),
    },
    funds: [
      {
        cik: '0001000002',
        shard: '2',
        name: 'Tiger Global',
        legalName: 'Tiger Global Management LLC',
        manager: 'Chase Coleman',
        hq: 'New York, NY',
        styleTag: 'Concentrated Tech',
        strategySummary: 'Long-term technology growth investor.',
        source: 'Curated Core',
        fundTier: 'Elite 50',
        isLeaderboardEligible: true,
        exclusionReason: null,
        latestPeriod: '2026-09-30',
        latestQuarterLabel: '2026 Q3',
        isStaleQuarter: false,
        latestFilingDate: '2026-11-16',
        equityAumUsd: 5_000_000_000,
        totalReportedUsd: 5_100_000_000,
        equityPositionCount: 25,
        totalPositionCount: 26,
        top10WeightPct: 72,
        effectivePositions: 11.4,
        techTmtPct: 86,
        etfSharePct: 0,
        quartersTracked: 16,
        returns: {
          quarter_end: makeEmptyWindows(24),
          filing_lag: makeEmptyWindows(25),
        },
        metrics: {
          quarter_end: emptyMetrics,
          filing_lag: emptyMetrics,
        },
        topHoldingsPreview: [{ ticker: 'NVDA', name: 'NVIDIA Corp', weightPct: 12.5 }],
        sectorBreakdown: [{ sector: 'Technology', weightPct: 86 }],
        searchableTickers: ['NVDA'],
        edgarUrl: 'https://www.sec.gov',
      },
    ],
    rankings: {
      quarter_end: emptyWinRec,
      filing_lag: emptyWinRec,
    },
    rankedUnionCiks: {
      quarter_end: ['0001000002'],
      filing_lag: ['0001000002'],
    },
    topHoldings: {
      quarter_end: [],
      filing_lag: [],
    },
    signalScreener: {
      quarterLabel: '2026 Q3',
      clusterInitiations: [],
      heavyAdds: [],
      smartMoneyExits: [],
      contrarianBets: [],
    },
    idealPortfolio: {
      positionCount: 1,
      maxPositionCapPct: 8,
      rankedFundsCount: 50,
      effectivePositions: 1,
      top10WeightPct: 100,
      methodologySummary: 'Mock ideal portfolio',
      sectorBreakdown: [{ sector: 'Semiconductors', weightPct: 100, stockCount: 1 }],
      positions: [],
      candidatePool: [],
      backtest: {
        quarter_end: {
          returns: makeEmptyWindows(20),
          cagrPct: 20,
          maxDrawdownPct: -10,
          sharpeProxy: 1.5,
          sortinoRatio: 2.0,
          infoRatioVsQqq: 0.9,
          curve,
        },
        filing_lag: {
          returns: makeEmptyWindows(21),
          cagrPct: 21,
          maxDrawdownPct: -10,
          sharpeProxy: 1.6,
          sortinoRatio: 2.1,
          infoRatioVsQqq: 1.0,
          curve,
        },
      },
    },
    fundUpdates: [],
    updatesOverview: {
      latestQuarterLabel: '2026 Q3',
      latestQuarterPeriodEnd: '2026-09-30',
      totalUpdateCards: 0,
      latestQuarterFilersCount: 1,
      totalEligibleFundsCount: 1,
      deadlineDayFilersCount: 1,
      fundsWithPutHedgesCount: 0,
      totalPutNotionalUsd: 0,
      totalCallNotionalUsd: 0,
      totalLatestBookUsd: 5_000_000_000,
      consensusNewInitiations: [],
      consensusHeavyAdds: [],
      consensusExitsAndTrims: [],
      consensusOptionMoves: [],
    },
    midQuarterAlerts: [],
    stocksIndex: {
      NVDA: {
        ticker: 'NVDA',
        name: 'NVIDIA Corp',
        sector: 'Technology',
        subSector: 'Semiconductors',
        shard: 0,
        priceUsd: 140,
        marketCapTier: 'Mega >$200B',
        holderCount: 1,
        eliteHoldersCount: 1,
        signalVerdict: 'Core Consensus',
        entryZoneStatus: 'Near Q-Avg',
        pctVsQuarterVwap: 3.1,
      },
    },
  };
}

describe('useModalFocusTrap and App Navigation', () => {
  it('locks body scroll across stacked modals and restores focus only to connected trigger on final close', () => {
    render(<FocusHarness />);

    const triggerBtn = screen.getByRole('button', { name: 'Open First' });
    triggerBtn.focus();
    fireEvent.click(triggerBtn);

    expect(document.body.style.overflow).toBe('hidden');
    expect(screen.getByRole('dialog', { name: 'First Modal' })).toBeInTheDocument();

    const openSecondBtn = screen.getByRole('button', { name: 'Open Second' });
    expect(document.activeElement).toBe(openSecondBtn);

    // Tabbing at last element wraps to first in First Modal
    const closeFirstBtn = screen.getByRole('button', { name: 'Close First' });
    closeFirstBtn.focus();
    fireEvent.keyDown(document, { key: 'Tab' });
    expect(document.activeElement).toBe(openSecondBtn);

    // Open stacked second modal
    fireEvent.click(openSecondBtn);
    expect(screen.getByRole('dialog', { name: 'Second Modal' })).toBeInTheDocument();
    expect(document.body.style.overflow).toBe('hidden');

    // Close second modal -> body scroll remains locked because First Modal is still open
    fireEvent.click(screen.getByRole('button', { name: 'Close Second' }));
    expect(document.body.style.overflow).toBe('hidden');

    // Close first modal -> body scroll unlocks and focus returns to original trigger button
    fireEvent.click(screen.getByRole('button', { name: 'Close First' }));
    expect(document.body.style.overflow).toBe('');
    expect(document.activeElement).toBe(triggerBtn);
  });

  it('preserves tab filter state across switches and supports Stock Dossier -> Fund -> Back to Stock round-trip', async () => {
    const mockSummary = makeMockSummary();
    const mockStockDetail: StockDetail = {
      ticker: 'NVDA',
      cusip: '67066G104',
      name: 'NVIDIA Corp',
      sector: 'Technology',
      industry: 'Semiconductors',
      subSector: 'Semiconductors',
      isTechTmt: true,
      priceUsd: 140,
      stockYtdPct: 22,
      stockTtmPct: 45,
      marketCapUsd: 3_400_000_000_000,
      marketCapTier: 'Mega >$200B',
      estQuarterVwap: 135,
      quarterLow: 120,
      quarterHigh: 150,
      pctVsQuarterVwap: 3.7,
      entryZoneStatus: 'Near Q-Avg',
      fundamentals: null,
      signalVerdict: 'Core Consensus',
      idealPortfolioRank: 1,
      idealWeightPct: 8,
      holderCount: 1,
      eliteHoldersCount: 1,
      totalValueUsd: 600_000_000,
      avgWeightPct: 12.5,
      maxWeightPct: 12.5,
      newCount: 0,
      addedCount: 1,
      trimmedCount: 0,
      exitedCount: 0,
      netEstFlowUsd: 25_000_000,
      holders: [
        {
          cik: '0001000002',
          fundName: 'Tiger Global',
          fundTier: 'Elite 50',
          skillScore: 88,
          weightPct: 12.5,
          valueUsd: 600_000_000,
          status: 'ADDED',
          qoqSharesChangePct: 10,
          qoqWeightDeltaPct: 1.2,
        },
      ],
      recentMoves: [],
      midQuarterAlerts: [],
    };
    const mockFundShard: FundShardPayload = {
      funds: {
        '0001000002': {
          cik: '0001000002',
          holdings: [],
          equityCurve: {
            quarter_end: [{ date: '2025-01-15', nav: 100 }, { date: '2026-01-15', nav: 120 }],
            filing_lag: [{ date: '2025-01-15', nav: 100 }, { date: '2026-01-15', nav: 122 }],
          },
          quarterlyHistory: [],
          midQuarterAlerts: [],
        },
      },
      updates: {},
    };

    vi.spyOn(dataLoader, 'fetchSummary').mockResolvedValue(mockSummary);
    vi.spyOn(dataLoader, 'fetchStockDetail').mockResolvedValue(mockStockDetail);
    vi.spyOn(dataLoader, 'fetchFundShard').mockResolvedValue(mockFundShard);

    render(<App />);

    // Wait for initial dataset to resolve and Ranking tab to mount
    const rankingPanel = await waitFor(() => {
      const el = document.getElementById('panel-ranking');
      expect(el).not.toBeNull();
      return el!;
    });

    const rankingQueries = within(rankingPanel);
    const searchBox = rankingQueries.getByPlaceholderText('Search fund, manager, or stock...') as HTMLInputElement;
    fireEvent.change(searchBox, { target: { value: 'Tiger' } });
    expect(searchBox.value).toBe('Tiger');

    // Switch to Updates tab and back -> Ranking tab remains mounted with search preserved
    fireEvent.click(screen.getByRole('tab', { name: /Updates/i }));
    expect(rankingPanel).toHaveAttribute('hidden');
    fireEvent.click(screen.getByRole('tab', { name: /Ranking/i }));
    expect(rankingPanel).not.toHaveAttribute('hidden');
    expect((rankingQueries.getByPlaceholderText('Search fund, manager, or stock...') as HTMLInputElement).value).toBe('Tiger');

    // Open NVDA Stock Dossier from Ranking top-holdings pill
    fireEvent.click(rankingQueries.getByTitle(/Open NVDA .*Stock Dossier/));
    await waitFor(() => {
      expect(screen.getByText('Core Consensus')).toBeInTheDocument();
    });

    // From NVDA Dossier, click Tiger Global -> opens FundDetailDrawer with "Back to NVDA Dossier" button
    const holderBtn = screen
      .getByRole('dialog')
      .querySelector('.data-table button.fund-link-btn') as HTMLButtonElement;
    expect(holderBtn).not.toBeNull();
    fireEvent.click(holderBtn);

    const backBtn = await screen.findByRole('button', { name: /Back to NVDA Dossier/i });
    expect(backBtn).toBeInTheDocument();

    // Click Back to NVDA Dossier -> returns to NVDA Stock Dossier
    fireEvent.click(backBtn);
    await waitFor(() => {
      expect(screen.getByRole('dialog')).toHaveTextContent('NVIDIA Corp');
    });

    vi.restoreAllMocks();
  });

  it('renders Methodology modal within container-relative width and closes via backdrop or Escape', async () => {
    const mockSummary = makeMockSummary();
    vi.spyOn(dataLoader, 'fetchSummary').mockResolvedValue(mockSummary);

    render(<App />);
    const methBtn = await screen.findByRole('button', { name: /Methodology & Universe Funnel/i });
    fireEvent.click(methBtn);

    const dialog = screen.getByRole('dialog', { name: /Data Methodology/i });
    expect(dialog).toHaveClass('drawer-backdrop');
    const panel = dialog.querySelector('.drawer-panel') as HTMLElement | null;
    expect(panel).not.toBeNull();
    expect(panel!.style.width).toBe('min(760px, 100%)');

    // Clicking inside the centered panel must not close it
    fireEvent.click(panel!);
    expect(screen.getByRole('dialog', { name: /Data Methodology/i })).toBeInTheDocument();

    // Clicking the surrounding backdrop closes it
    fireEvent.click(dialog);
    expect(screen.queryByRole('dialog', { name: /Data Methodology/i })).not.toBeInTheDocument();

    // Re-open and close with Escape
    fireEvent.click(methBtn);
    expect(screen.getByRole('dialog', { name: /Data Methodology/i })).toBeInTheDocument();
    fireEvent.keyDown(window, { key: 'Escape' });
    expect(screen.queryByRole('dialog', { name: /Data Methodology/i })).not.toBeInTheDocument();

    vi.restoreAllMocks();
  });
});
