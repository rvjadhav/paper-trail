import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { HoldingsTab } from '../components/HoldingsTab';
import { StockDetailDrawer } from '../components/StockDetailDrawer';
import { UpdatesTab } from '../components/UpdatesTab';
import type {
  ConsensusHolding,
  DualConvictionSignal,
  Form4InsiderSummary,
  MidQuarterAlert,
  SignalScreenerData,
  StockDetail,
  StockIndexEntry,
  UpdatesOverview,
} from '../types/data';
import { buildConvergenceFeed, computeDualConvictionSignal } from '../utils/convergence';
import * as dataLoader from '../utils/dataLoader';

function makeStockIndexEntry(overrides: Partial<StockIndexEntry> = {}): StockIndexEntry {
  return {
    ticker: 'CLD',
    name: 'Cloud Dip Corp',
    sector: 'Technology',
    subSector: 'Enterprise Software',
    shard: 0,
    priceUsd: 95,
    marketCapTier: 'Large $30B-$200B',
    holderCount: 12,
    eliteHoldersCount: 6,
    signalVerdict: 'Cluster Initiation',
    entryZoneStatus: 'Discount to Q-Avg',
    pctVsQuarterVwap: -5,
    ...overrides,
  };
}

function makeForm4Cluster(overrides: Partial<Form4InsiderSummary> = {}): Form4InsiderSummary {
  return {
    ticker: 'CLD',
    issuerCik: '0001045810',
    lookbackDays: 90,
    openMarketBuyCount: 3,
    distinctBuyersCount: 2,
    officerBuyersCount: 2,
    cSuiteBuyersCount: 2,
    directorBuyersCount: 0,
    tenPctOwnerBuyersCount: 0,
    totalBuyValueUsd: 1_250_000,
    totalBuyShares: 12_500,
    avgBuyPriceUsd: 94.0,
    buyPriceVsQuarterVwapPct: -6.0,
    latestBuyDate: '2026-11-10',
    latestFilingDate: '2026-11-10',
    non10b5Count: 3,
    non10b5BuyValueUsd: 1_250_000,
    discretionarySellValueUsd: 0,
    totalSellValueUsd: 0,
    insiderSignalLabel: 'CLUSTER_BUY',
    clusterDetected: true,
    recentTransactions: [
      {
        accessionNumber: '0001-26-0001',
        filingDate: '2026-11-10',
        transactionDate: '2026-11-09',
        insiderName: 'Jane Doe',
        insiderRole: 'Chief Executive Officer',
        isOfficer: true,
        isDirector: true,
        isCsuite: true,
        isTenPercentOwner: false,
        shares: 8_000,
        pricePerShare: 94.5,
        valueUsd: 756_000,
        is10b5Plan: false,
        edgarUrl: 'https://www.sec.gov/Archives/edgar/data/1045810/0001260001/doc.xml',
      },
      {
        accessionNumber: '0001-26-0002',
        filingDate: '2026-11-08',
        transactionDate: '2026-11-07',
        insiderName: 'John Smith',
        insiderRole: 'Chief Financial Officer',
        isOfficer: true,
        isDirector: false,
        isCsuite: true,
        isTenPercentOwner: false,
        shares: 4_500,
        pricePerShare: 93.1,
        valueUsd: 494_000,
        is10b5Plan: false,
        edgarUrl: 'https://www.sec.gov/Archives/edgar/data/1045810/0001260002/doc.xml',
      },
    ],
    ...overrides,
  };
}

describe('Dual-Conviction & Form 4 Convergence Engine', () => {
  it('classifies PRIME vs EXTENDED vs ACTIVIST_13F vs WATCH and filters sell-heavy false positives', () => {
    const prime = computeDualConvictionSignal({
      ticker: 'CLD',
      name: 'Cloud Dip Corp',
      form4Summary: makeForm4Cluster(),
      pctVsQuarterVwap: -5,
      actionabilityScore: 86,
      actionVerdict: 'STRONG ACCUMULATE',
      newCount: 2,
      addedCount: 2,
      eliteBuyersCount: 3,
      eliteHoldersCount: 6,
      netEstFlowUsd: 160_000_000,
    });
    expect(prime).not.toBeNull();
    expect(prime!.tier).toBe('DUAL_CONVICTION_PRIME');
    expect(prime!.convergenceScore).toBeGreaterThan(65);
    expect(prime!.nearQuarterVwap).toBe(true);

    const extended = computeDualConvictionSignal({
      ticker: 'HYPE',
      name: 'Hype Rally Co',
      form4Summary: makeForm4Cluster({ ticker: 'HYPE' }),
      pctVsQuarterVwap: 22,
      actionabilityScore: 62,
      actionVerdict: 'ACCUMULATE ON DIP',
      newCount: 1,
      addedCount: 2,
      eliteBuyersCount: 2,
      eliteHoldersCount: 4,
    });
    expect(extended!.tier).toBe('DUAL_CONVICTION_EXTENDED');

    const sellHeavy = computeDualConvictionSignal({
      ticker: 'SELL',
      name: 'Sell Heavy Co',
      form4Summary: makeForm4Cluster({
        ticker: 'SELL',
        insiderSignalLabel: 'MIXED_SELL_HEAVY',
        clusterDetected: false,
      }),
      newCount: 1,
      addedCount: 1,
      eliteBuyersCount: 2,
    });
    expect(sellHeavy).toBeNull();
  });

  it('builds client-side fallback convergence feed when backend precomputed array is omitted', () => {
    const feed = buildConvergenceFeed({
      form4Signals: {
        CLD: makeForm4Cluster(),
      },
      midQuarterAlerts: [
        {
          id: 'al-13d',
          filerCik: '0001000099',
          fundName: 'Starboard Value',
          form: 'SC 13D',
          isActivist: true,
          filingDate: '2026-11-12',
          accessionNumber: '0001-99',
          subjectCik: '0001045810',
          ticker: 'ACTV',
          issuerName: 'Activist Target Co',
          edgarUrl: 'https://www.sec.gov',
          percentOfClass: 8.1,
          stakeStatus: 'Active (≥5%)',
          item4Category: 'Strategic Review / M&A',
          item4Intent: 'Evaluating strategic alternatives and board refresh.',
        },
      ],
      stocksIndex: {
        CLD: makeStockIndexEntry(),
        ACTV: makeStockIndexEntry({
          ticker: 'ACTV',
          name: 'Activist Target Co',
          eliteHoldersCount: 3,
          signalVerdict: 'Tracked Equity',
          pctVsQuarterVwap: 2.0,
        }),
      },
    });

    expect(feed.length).toBe(2);
    expect(feed[0].ticker).toBe('CLD');
    expect(feed[0].tier).toBe('DUAL_CONVICTION_PRIME');
    expect(feed[1].ticker).toBe('ACTV');
    expect(feed[1].tier).toBe('ACTIVIST_13F_CONVERGENCE');
    expect(feed[1].activistIntent).toMatch(/strategic alternatives/i);
  });

  it('prevents deep drawdowns (<-20% vs Q-VWAP), negative flows, and CLUSTER_SELL from triggering PRIME', () => {
    const deepDrawdown = computeDualConvictionSignal({
      ticker: 'FALL',
      name: 'Falling Knife Corp',
      form4Summary: makeForm4Cluster({ ticker: 'FALL' }),
      pctVsQuarterVwap: -32,
      actionabilityScore: 76,
      actionVerdict: 'STRONG ACCUMULATE',
      newCount: 2,
      addedCount: 2,
      eliteBuyersCount: 3,
      eliteHoldersCount: 5,
      netEstFlowUsd: 90_000_000,
    });
    expect(deepDrawdown).not.toBeNull();
    expect(deepDrawdown!.nearQuarterVwap).toBe(false);
    expect(deepDrawdown!.tier).toBe('DUAL_CONVICTION_EXTENDED');
    expect(deepDrawdown!.headline).toMatch(/deep drawdown/i);

    const negFlow = computeDualConvictionSignal({
      ticker: 'OUTFL',
      name: 'Negative Flow Corp',
      form4Summary: makeForm4Cluster({ ticker: 'OUTFL' }),
      pctVsQuarterVwap: -4,
      actionabilityScore: 68,
      actionVerdict: 'ACCUMULATE ON DIP',
      newCount: 3,
      addedCount: 2,
      trimmedCount: 1,
      eliteBuyersCount: 2,
      eliteHoldersCount: 5,
      netEstFlowUsd: -120_000_000,
    });
    expect(negFlow).not.toBeNull();
    expect(negFlow!.has13FAccumulation).toBe(false);
    expect(negFlow!.tier).toBe('INSIDER_CLUSTER_WATCH');

    const sellCluster = computeDualConvictionSignal({
      ticker: 'DUMP',
      name: 'Insider Dump Corp',
      form4Summary: makeForm4Cluster({
        ticker: 'DUMP',
        openMarketBuyCount: 0,
        distinctBuyersCount: 0,
        clusterDetected: false,
        discretionarySellValueUsd: 18_000_000,
        sellClusterDetected: true,
        insiderSignalLabel: 'CLUSTER_SELL',
      }),
      pctVsQuarterVwap: -2,
      actionabilityScore: 74,
      actionVerdict: 'STRONG ACCUMULATE',
      newCount: 2,
      addedCount: 2,
      eliteBuyersCount: 2,
      netEstFlowUsd: 80_000_000,
    });
    expect(sellCluster).toBeNull();
  });
});

describe('Convergence Feed UI across UpdatesTab, StockDetailDrawer, and HoldingsTab', () => {
  it('renders Dual-Conviction Convergence Feed filters, 13D Item 4 intent, and opens Stock Dossier', () => {
    const overview: UpdatesOverview = {
      latestQuarterLabel: '2026 Q3',
      latestQuarterPeriodEnd: '2026-09-30',
      totalUpdateCards: 0,
      latestQuarterFilersCount: 0,
      totalEligibleFundsCount: 0,
      deadlineDayFilersCount: 0,
      fundsWithPutHedgesCount: 0,
      totalPutNotionalUsd: 0,
      totalCallNotionalUsd: 0,
      totalLatestBookUsd: 0,
      consensusNewInitiations: [],
      consensusHeavyAdds: [],
      consensusExitsAndTrims: [],
      consensusOptionMoves: [],
    };
    const midQuarterAlerts: MidQuarterAlert[] = [
      {
        id: 'al-13d',
        filerCik: '0001000099',
        fundName: 'Starboard Value',
        form: 'SC 13D',
        isActivist: true,
        filingDate: '2026-11-12',
        eventDate: '2026-11-08',
        accessionNumber: '0001-99',
        subjectCik: '0001045810',
        ticker: 'ACTV',
        issuerName: 'Activist Target Co',
        edgarUrl: 'https://www.sec.gov/idx',
        filingDocUrl: 'https://www.sec.gov/doc.htm',
        percentOfClass: 8.1,
        sharesOwned: 3_200_000,
        stakeStatus: 'Active (≥5%)',
        item4Category: 'Strategic Review / M&A',
        item4Intent: 'Engaging board on strategic alternatives and capital allocation.',
      },
    ];
    const onSelectStock = vi.fn();

    render(
      <UpdatesTab
        updates={[]}
        overview={overview}
        midQuarterAlerts={midQuarterAlerts}
        stocksIndex={{
          CLD: makeStockIndexEntry(),
          ACTV: makeStockIndexEntry({
            ticker: 'ACTV',
            name: 'Activist Target Co',
            eliteHoldersCount: 3,
          }),
        }}
        searchableTickersByCik={new Map()}
        form4Signals={{ CLD: makeForm4Cluster() }}
        onSelectFund={vi.fn()}
        onDiveDeeper={vi.fn()}
        onSelectStock={onSelectStock}
      />
    );

    expect(
      screen.getByText(/Dual-Conviction Convergence Feed \(2\)/i)
    ).toBeInTheDocument();
    expect(screen.getByText('Cloud Dip Corp')).toBeInTheDocument();
    expect(screen.getAllByText('Activist Target Co').length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText('Strategic Review / M&A').length).toBeGreaterThanOrEqual(1);
    expect(
      screen.getAllByText(/Engaging board on strategic alternatives/i).length
    ).toBeGreaterThanOrEqual(1);

    fireEvent.click(screen.getByRole('button', { name: /Prime Dual-Conviction \(1\)/i }));
    expect(screen.getByText('Cloud Dip Corp')).toBeInTheDocument();
    expect(screen.getByText('$1.3M')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'CLD' }));
    expect(onSelectStock).toHaveBeenCalledWith('CLD');
  });

  it('renders SEC Form 4 Open-Market Insider Purchases, Convergence Banner, and 13D Item 4 inside StockDetailDrawer', async () => {
    const f4 = makeForm4Cluster();
    const convSignal = computeDualConvictionSignal({
      ticker: 'CLD',
      name: 'Cloud Dip Corp',
      form4Summary: f4,
      pctVsQuarterVwap: -5,
      actionabilityScore: 86,
      actionVerdict: 'STRONG ACCUMULATE',
      newCount: 2,
      addedCount: 2,
      eliteBuyersCount: 3,
      eliteHoldersCount: 6,
      netEstFlowUsd: 160_000_000,
    }) as DualConvictionSignal;

    const detail: StockDetail = {
      ticker: 'CLD',
      cusip: '000000001',
      name: 'Cloud Dip Corp',
      sector: 'Technology',
      industry: 'Enterprise Software',
      subSector: 'Enterprise Software',
      isTechTmt: true,
      priceUsd: 95,
      stockYtdPct: 12,
      stockTtmPct: 24,
      marketCapUsd: 60_000_000_000,
      marketCapTier: 'Large $30B-$200B',
      estQuarterVwap: 100,
      quarterLow: 88,
      quarterHigh: 106,
      pctVsQuarterVwap: -5,
      entryZoneStatus: 'Discount to Q-Avg',
      fundamentals: null,
      signalVerdict: 'Cluster Initiation',
      idealPortfolioRank: 1,
      idealWeightPct: 6.0,
      holderCount: 12,
      eliteHoldersCount: 6,
      totalValueUsd: 800_000_000,
      avgWeightPct: 3.1,
      maxWeightPct: 6.2,
      newCount: 2,
      addedCount: 2,
      trimmedCount: 0,
      exitedCount: 0,
      netEstFlowUsd: 160_000_000,
      holders: [],
      recentMoves: [],
      midQuarterAlerts: [
        {
          id: 'al-13d-cld',
          filerCik: '0001000099',
          fundName: 'Starboard Value',
          form: 'SC 13D',
          isActivist: true,
          filingDate: '2026-11-12',
          accessionNumber: '0001-99',
          subjectCik: '0001045810',
          ticker: 'CLD',
          issuerName: 'Cloud Dip Corp',
          edgarUrl: 'https://www.sec.gov/idx',
          filingDocUrl: 'https://www.sec.gov/doc.xml',
          percentOfClass: 6.5,
          sharesOwned: 2_500_000,
          stakeStatus: 'Active (≥5%)',
          item4Category: 'Board / Governance',
          item4Intent: 'Nominating two independent directors ahead of annual meeting.',
        },
      ],
      insiderActivity: f4,
      convergenceSignal: convSignal,
    };

    const spy = vi.spyOn(dataLoader, 'fetchStockDetail').mockResolvedValueOnce(detail);

    render(
      <StockDetailDrawer
        ticker="CLD"
        stocksIndex={{ CLD: makeStockIndexEntry() }}
        latestQuarterLabel="2026 Q3"
        form4Signals={{ CLD: f4 }}
        dualConvictionByTicker={{ CLD: convSignal }}
        onClose={() => {}}
        onSelectFund={() => {}}
      />
    );

    await waitFor(() => {
      expect(screen.getByText(/SEC Form 4 Insider Purchases & Dual-Conviction/i)).toBeInTheDocument();
    });

    expect(screen.getByText('3 buys · $1.3M')).toBeInTheDocument();
    expect(screen.getByText('2 C-Suite · 0 Director')).toBeInTheDocument();
    expect(screen.getByText('-6.0% vs Q-VWAP')).toBeInTheDocument();
    expect(screen.getByText('Jane Doe')).toBeInTheDocument();
    expect(screen.getByText('John Smith')).toBeInTheDocument();
    expect(screen.getAllByText('C-Suite').length).toBeGreaterThanOrEqual(2);
    expect(screen.getAllByText('Discretionary').length).toBeGreaterThanOrEqual(2);
    expect(screen.getByText('Board / Governance')).toBeInTheDocument();
    expect(screen.getByText(/Nominating two independent directors/i)).toBeInTheDocument();

    spy.mockRestore();
  });

  it('filters HoldingsTab Matrix rows via Dual-Conviction & Form 4 toggle and renders badges', () => {
    const f4 = makeForm4Cluster();
    const convSignal = computeDualConvictionSignal({
      ticker: 'CLD',
      name: 'Cloud Dip Corp',
      form4Summary: f4,
      pctVsQuarterVwap: -5,
      actionabilityScore: 86,
      actionVerdict: 'STRONG ACCUMULATE',
      newCount: 2,
      addedCount: 2,
      eliteBuyersCount: 3,
      eliteHoldersCount: 6,
      netEstFlowUsd: 160_000_000,
    }) as DualConvictionSignal;

    const c1: ConsensusHolding = {
      rank: 1,
      ticker: 'CLD',
      cusip: '000000001',
      name: 'Cloud Dip Corp',
      sector: 'Technology',
      industry: 'Enterprise Software',
      subSector: 'Enterprise Software',
      isTechTmt: true,
      priceUsd: 95,
      stockYtdPct: 12,
      stockTtmPct: 24,
      holderCount: 12,
      totalRankedFundsPool: 50,
      equalAggregateWeightPct: 3.8,
      avgHolderWeightPct: 3.1,
      aumAggregateWeightPct: 4.0,
      totalValueUsd: 800_000_000,
      netBuyers: 4,
      netTrimmers: 0,
      holders: [
        {
          cik: '0001',
          fundName: 'Alpha Tech',
          fundTier: 'Elite 50',
          weightPct: 4.0,
          valueUsd: 300_000_000,
          status: 'NEW',
          qoqSharesChangePct: null,
          rankedWindows: ['TTM'],
        },
      ],
      summaryText: 'Strong accumulation',
      marketCapTier: 'Large $30B-$200B',
      estQuarterVwap: 100,
      pctVsQuarterVwap: -5,
      entryZoneStatus: 'Discount to Q-Avg',
    };

    const c2: ConsensusHolding = {
      ...c1,
      rank: 2,
      ticker: 'NONE',
      name: 'No Convergence Inc',
    };

    const screener: SignalScreenerData = {
      quarterLabel: '2026 Q3',
      clusterInitiations: [],
      heavyAdds: [],
      smartMoneyExits: [],
      contrarianBets: [],
    };

    render(
      <HoldingsTab
        topHoldings={{ filing_lag: [c1, c2], quarter_end: [c1, c2] }}
        signalScreener={screener}
        stocksIndex={{
          CLD: makeStockIndexEntry(),
          NONE: makeStockIndexEntry({ ticker: 'NONE', name: 'No Convergence Inc' }),
        }}
        dualConvictionByTicker={{ CLD: convSignal }}
        form4Signals={{ CLD: f4 }}
        mode="filing_lag"
        onModeChange={() => {}}
        onSelectFund={() => {}}
        onSelectStock={() => {}}
      />
    );

    expect(screen.getByText('Cloud Dip Corp')).toBeInTheDocument();
    expect(screen.getByText('No Convergence Inc')).toBeInTheDocument();
    expect(screen.getByText('Prime Dual-Conviction')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /Dual-Conviction & Form 4 \(1\)/i }));
    expect(screen.getByText('Cloud Dip Corp')).toBeInTheDocument();
    expect(screen.queryByText('No Convergence Inc')).not.toBeInTheDocument();
  });
});
