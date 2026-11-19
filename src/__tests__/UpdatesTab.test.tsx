import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { UpdatesTab } from '../components/UpdatesTab';
import type {
  FundUpdateSummary,
  MidQuarterAlert,
  StockIndexEntry,
  UpdatesOverview,
} from '../types/data';

function makeUpdateCard(cik: string, name: string, manager: string): FundUpdateSummary {
  return {
    cik,
    shard: '0',
    fundName: name,
    legalName: `${name} LLC`,
    manager,
    styleTag: 'Concentrated Tech',
    source: manager === 'SEC EDGAR Discovered' ? 'EDGAR Discovered' : 'Curated Core',
    fundTier: 'Elite 50',
    form: '13F-HR',
    amendmentType: '',
    inRankings: true,
    isRankedTop100AnyWindow: true,
    rankedWindows: ['TTM'],
    quarterLabel: '2026 Q3',
    periodEnd: '2026-09-30',
    prevQuarterLabel: '2026 Q2',
    prevPeriodEnd: '2026-06-30',
    filingDate: '2026-11-16',
    originalFilingDate: '2026-11-16',
    latestAmendmentDate: '2026-11-16',
    prevFilingDate: '2026-08-14',
    statutoryDeadline: '2026-11-16',
    daysBeforeDeadline: 0,
    deadlineBadge: 'Deadline Day',
    accessionNumber: `0001-${cik}-26`,
    filingUrl: 'https://www.sec.gov',
    edgarUrl: 'https://www.sec.gov',
    currTotalReportedUsd: 2_000_000_000,
    prevTotalReportedUsd: 1_800_000_000,
    totalValueDeltaUsd: 200_000_000,
    totalValueDeltaPct: 11.1,
    currEquityAumUsd: 1_900_000_000,
    prevEquityAumUsd: 1_700_000_000,
    currPositionCount: 25,
    prevPositionCount: 24,
    currEquityBookPct: 95,
    prevEquityBookPct: 94.4,
    currCallUsd: 100_000_000,
    prevCallUsd: 0,
    currCallBookPct: 5,
    prevCallBookPct: 0,
    currPutUsd: 0,
    prevPutUsd: 0,
    currPutBookPct: 0,
    prevPutBookPct: 0,
    putBookDeltaPct: 0,
    hasSignificantHedge: false,
    newCount: 1,
    addedCount: 2,
    trimmedCount: 1,
    exitedCount: 1,
    unchangedCount: 20,
    grossActivityUsd: 150_000_000,
    activityRatioPct: 7.5,
    narrativeHeadline: 'Active quarter',
    narrativeLead: 'Added NVDA and initiated MSFT.',
    narrativeFocusNotes: [],
    narrativeHedgeAndStructure: null,
    narrativeOtherMoves: null,
    putLegs: [],
    callLegs: [],
    instrumentShifts: [],
    valuationShifts: [],
    topNew: [],
    topAdded: [],
    topTrimmed: [],
    topExited: [],
  };
}

describe('UpdatesTab', () => {
  it('renders signed estimated flows, 13F put fair-value disclaimer, enriched 13D/13G badges, and omits synthetic manager placeholders', () => {
    const stocksIndex: Record<string, StockIndexEntry> = {
      NVDA: {
        ticker: 'NVDA',
        name: 'NVIDIA Corp',
        sector: 'Technology',
        subSector: 'Semiconductors',
        shard: 0,
        priceUsd: 140,
        marketCapTier: 'Mega >$200B',
        holderCount: 30,
        eliteHoldersCount: 20,
        signalVerdict: 'Strong Buy',
        entryZoneStatus: 'Near Q-Avg',
        pctVsQuarterVwap: 3.2,
      },
      INTC: {
        ticker: 'INTC',
        name: 'Intel Corp',
        sector: 'Technology',
        subSector: 'Semiconductors',
        shard: 0,
        priceUsd: 24,
        marketCapTier: 'Large $30B-$200B',
        holderCount: 4,
        eliteHoldersCount: 1,
        signalVerdict: 'Distribution',
        entryZoneStatus: 'Discount to Q-Avg',
        pctVsQuarterVwap: -8.0,
      },
    };

    const overview: UpdatesOverview = {
      latestQuarterLabel: '2026 Q3',
      latestQuarterPeriodEnd: '2026-09-30',
      totalUpdateCards: 2,
      latestQuarterFilersCount: 2,
      totalEligibleFundsCount: 2,
      deadlineDayFilersCount: 2,
      fundsWithPutHedgesCount: 1,
      totalPutNotionalUsd: 2_500_000_000,
      totalCallNotionalUsd: 1_200_000_000,
      totalLatestBookUsd: 4_000_000_000,
      consensusNewInitiations: [],
      consensusHeavyAdds: [
        {
          ticker: 'NVDA',
          name: 'NVIDIA Corp',
          sector: 'Technology',
          fundCount: 6,
          totalCurrValueUsd: 500_000_000,
          totalDeltaUsd: 10_000_000,
          totalEstFlowUsd: 2_500_000,
          funds: [],
        },
      ],
      consensusExitsAndTrims: [
        {
          ticker: 'INTC',
          name: 'Intel Corp',
          sector: 'Technology',
          fundCount: 4,
          totalCurrValueUsd: 12_000_000,
          totalDeltaUsd: -6_000_000,
          totalEstFlowUsd: -4_100_000,
          funds: [],
        },
      ],
      consensusOptionMoves: [],
    };

    const midQuarterAlerts: MidQuarterAlert[] = [
      {
        id: 'al-exit',
        filerCik: '0001000001',
        fundName: 'Discovered Fund',
        form: 'SC 13G/A',
        isActivist: false,
        filingDate: '2026-11-02',
        eventDate: '2026-10-15',
        accessionNumber: '0001-01',
        subjectCik: '0001045810',
        ticker: 'INTC',
        issuerName: 'Intel Corp',
        edgarUrl: 'https://www.sec.gov',
        percentOfClass: 0,
        sharesOwned: 0,
        stakeStatus: 'Exited (0%)',
      },
      {
        id: 'al-active',
        filerCik: '0001000002',
        fundName: 'Tiger Global',
        form: 'SC 13D',
        isActivist: true,
        filingDate: '2026-11-10',
        eventDate: '2026-11-05',
        accessionNumber: '0001-02',
        subjectCik: '0001045810',
        ticker: 'NVDA',
        issuerName: 'NVIDIA Corp',
        edgarUrl: 'https://www.sec.gov',
        percentOfClass: 6.4,
        sharesOwned: 1_500_000,
        stakeStatus: 'Active ≥5%',
      },
    ];

    const updates = [
      makeUpdateCard('0001000001', 'Discovered Fund', 'SEC EDGAR Discovered'),
      makeUpdateCard('0001000002', 'Tiger Global', 'Chase Coleman'),
    ];

    render(
      <UpdatesTab
        updates={updates}
        overview={overview}
        midQuarterAlerts={midQuarterAlerts}
        stocksIndex={stocksIndex}
        searchableTickersByCik={new Map()}
        onSelectFund={vi.fn()}
        onDiveDeeper={vi.fn()}
        onSelectStock={vi.fn()}
      />
    );

    expect(screen.getByText('13F Disclosed Put Notional')).toBeInTheDocument();
    expect(screen.getByText(/call notional in 2026 Q3/i)).toBeInTheDocument();
    expect(screen.getByText(/6f · \+\$2\.5M/)).toBeInTheDocument();
    expect(screen.getByText(/4f · -\$4\.1M/)).toBeInTheDocument();

    expect(screen.getByText('Exited (0%)')).toBeInTheDocument();
    expect(screen.getByText('0.0%')).toBeInTheDocument();
    expect(screen.getByText(/Event 2026-10-15/)).toBeInTheDocument();
    expect(screen.getByText('Active ≥5%')).toBeInTheDocument();
    expect(screen.getByText('6.4%')).toBeInTheDocument();
    expect(screen.getByText('1.50M sh')).toBeInTheDocument();

    expect(screen.queryByText('SEC EDGAR Discovered')).not.toBeInTheDocument();
    expect(screen.getByText('Chase Coleman')).toBeInTheDocument();
  });
});
