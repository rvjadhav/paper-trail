import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { HoldingsTab } from '../components/HoldingsTab';
import { StockDetailDrawer } from '../components/StockDetailDrawer';
import type {
  ConsensusHolding,
  SignalScreenerData,
  SignalScreenerEntry,
  StockDetail,
  StockIndexEntry,
} from '../types/data';
import {
  formatHoldingPeriodLabel,
  inferStatutoryDeadlineFromQuarterLabel,
  resolveAlphaDecayMetrics,
} from '../utils/alphaDecay';
import * as dataLoader from '../utils/dataLoader';

function makeStockIndexEntry(overrides: Partial<StockIndexEntry> = {}): StockIndexEntry {
  return {
    ticker: 'TEST',
    name: 'Test Corp',
    sector: 'Technology',
    subSector: 'Enterprise Software',
    shard: 0,
    priceUsd: 100,
    marketCapTier: 'Large $30B-$200B',
    holderCount: 10,
    eliteHoldersCount: 6,
    signalVerdict: 'Tracked Equity',
    entryZoneStatus: 'Near Q-Avg',
    pctVsQuarterVwap: 0,
    ...overrides,
  };
}

function makeScreenerEntry(overrides: Partial<SignalScreenerEntry> = {}): SignalScreenerEntry {
  return {
    ticker: 'TEST',
    name: 'Test Corp',
    sector: 'Technology',
    subSector: 'Enterprise Software',
    isTechTmt: true,
    priceUsd: 100,
    stockTtmPct: 18,
    holderCount: 10,
    eliteHoldersCount: 6,
    totalValueUsd: 500_000_000,
    avgWeightPct: 2.5,
    maxWeightPct: 5.0,
    newCount: 0,
    eliteNewCount: 0,
    addedCount: 0,
    trimmedCount: 0,
    exitedCount: 0,
    netDeltaUsd: 0,
    netEstFlowUsd: 0,
    movers: [],
    topBackers: [],
    estQuarterVwap: 100,
    pctVsQuarterVwap: 0,
    entryZoneStatus: 'Near Q-Avg',
    ...overrides,
  };
}

describe('alphaDecay engine utilities', () => {
  it('computes 3-timestamp returns and keeps missing historical fields null instead of fabricating values', () => {
    const preRefreshEntry = makeScreenerEntry({
      ticker: 'CLD',
      priceUsd: 88,
      estQuarterVwap: 100,
      pctVsQuarterVwap: undefined,
      entryZoneStatus: 'Discount to Q-Avg',
      newCount: 3,
      eliteNewCount: 2,
      addedCount: 1,
      trimmedCount: 0,
      netEstFlowUsd: 120_000_000,
    });

    const preResolved = resolveAlphaDecayMetrics(preRefreshEntry, { latestQuarterLabel: '2026 Q3' });
    expect(preResolved.status).toBe('LAG_ADVANTAGE');
    expect(preResolved.pctVsQuarterVwap).toBe(-12);
    expect(preResolved.consensusFilingDate).toBe('2026-11-16');
    expect(preResolved.filingDatePriceUsd).toBeNull();
    expect(preResolved.qVwapToFilingPct).toBeNull();
    expect(preResolved.filingToTodayPct).toBeNull();
    expect(preResolved.avgEliteHoldingQuarters).toBeNull();

    const withFilingPrice = resolveAlphaDecayMetrics({
      ...preRefreshEntry,
      priceUsd: 128,
      estQuarterVwap: 100,
      filingDatePriceUsd: 112,
      pctVsQuarterVwap: 28,
      newCount: 0,
      addedCount: 1,
      trimmedCount: 3,
      exitedCount: 1,
      eliteBuyersCount: 1,
      eliteSellersCount: 4,
      netEstFlowUsd: -95_000_000,
      avgEliteHoldingQuarters: 6.4,
    });

    expect(withFilingPrice.qVwapToFilingPct).toBe(12);
    expect(withFilingPrice.filingToTodayPct).toBe(14.3);
    expect(withFilingPrice.status).toBe('FOMO_TRAP');
    expect(withFilingPrice.holdingHorizonLabel).toBe('6.4Q · Multi-Quarter');
  });

  it('infers weekend-adjusted SEC 45-day deadline and holding horizon labels accurately', () => {
    expect(inferStatutoryDeadlineFromQuarterLabel('2026 Q3')).toBe('2026-11-16');
    expect(inferStatutoryDeadlineFromQuarterLabel('bad')).toBeNull();
    expect(formatHoldingPeriodLabel(null)).toBe('—');
    expect(formatHoldingPeriodLabel(9.2)).toBe('9.2Q · Core Compounder');
    expect(formatHoldingPeriodLabel(1.5)).toBe('1.5Q · Fresh Entry');
  });
});

describe('StockDetailDrawer & HoldingsTab Alpha-Decay UI', () => {
  it('renders Is the Move Already Over 3-timestamp bridge, holding period, and FOMO banner in StockDetailDrawer', async () => {
    const detail: StockDetail = {
      ticker: 'HYPE',
      cusip: '44919P100',
      name: 'Hype Cloud Inc',
      sector: 'Technology',
      industry: 'Application Software',
      subSector: 'Enterprise Software',
      isTechTmt: true,
      priceUsd: 128,
      stockYtdPct: 35,
      stockTtmPct: 48,
      marketCapUsd: 40_000_000_000,
      marketCapTier: 'Large $30B-$200B',
      estQuarterVwap: 100,
      quarterLow: 92,
      quarterHigh: 110,
      pctVsQuarterVwap: 28,
      entryZoneStatus: 'Extended',
      fundamentals: null,
      signalVerdict: 'Active Distribution',
      idealPortfolioRank: null,
      idealWeightPct: null,
      holderCount: 14,
      eliteHoldersCount: 5,
      totalValueUsd: 900_000_000,
      avgWeightPct: 1.9,
      maxWeightPct: 4.1,
      newCount: 1,
      addedCount: 0,
      trimmedCount: 3,
      exitedCount: 1,
      netEstFlowUsd: -110_000_000,
      holders: [],
      recentMoves: [],
      midQuarterAlerts: [],
      consensusFilingDate: '2026-11-16',
      filingDatePriceUsd: 112,
      qVwapToFilingPct: 12.0,
      filingToTodayPct: 14.3,
      avgEliteHoldingQuarters: 3.5,
      eliteBuyersCount: 1,
      eliteSellersCount: 4,
      alphaDecayStatus: 'FOMO_TRAP',
      alphaDecayReason: 'Trading +28.0% above Est. Q-VWAP while 4 Elite-50 funds trimmed into strength.',
    };

    const spy = vi.spyOn(dataLoader, 'fetchStockDetail').mockResolvedValueOnce(detail);

    render(
      <StockDetailDrawer
        ticker="HYPE"
        stocksIndex={{ HYPE: makeStockIndexEntry({ ticker: 'HYPE', name: 'Hype Cloud Inc', priceUsd: 128, pctVsQuarterVwap: 28 }) }}
        latestQuarterLabel="2026 Q3"
        onClose={() => {}}
        onSelectFund={() => {}}
      />
    );

    await waitFor(() => {
      expect(screen.getByText(/Is the Move Already Over\?/i)).toBeInTheDocument();
    });

    expect(screen.getByText('1. Est. Q-VWAP')).toBeInTheDocument();
    expect(screen.getByText('2. SEC Filing Date')).toBeInTheDocument();
    expect(screen.getByText('3. Today')).toBeInTheDocument();
    expect(screen.getByText('$112.00')).toBeInTheDocument();
    expect(screen.getByText('+12.0%')).toBeInTheDocument();
    expect(screen.getByText('+14.3%')).toBeInTheDocument();
    expect(screen.getByText(/3\.5Q · Emerging/)).toBeInTheDocument();
    expect(screen.getByText(/FOMO \/ Late Crowded Trap:/)).toBeInTheDocument();

    spy.mockRestore();
  });

  it('filters Consensus rows cleanly between Lag Advantage and FOMO / Late Crowded in HoldingsTab', () => {
    const lagStock: ConsensusHolding = {
      rank: 1,
      ticker: 'CLD',
      cusip: '000000001',
      name: 'Cloud Dip Co',
      sector: 'Technology',
      industry: 'Semiconductors',
      subSector: 'Semiconductors',
      isTechTmt: true,
      priceUsd: 88,
      stockYtdPct: 5,
      stockTtmPct: 12,
      holderCount: 15,
      totalRankedFundsPool: 50,
      equalAggregateWeightPct: 4.5,
      avgHolderWeightPct: 3.1,
      aumAggregateWeightPct: 4.2,
      totalValueUsd: 1_200_000_000,
      netBuyers: 5,
      netTrimmers: 1,
      holders: [
        {
          cik: '0001',
          fundName: 'Alpha Tech',
          fundTier: 'Elite 50',
          weightPct: 4.0,
          valueUsd: 400_000_000,
          status: 'NEW',
          qoqSharesChangePct: null,
          rankedWindows: ['TTM'],
        },
      ],
      summaryText: 'Strong cluster dip',
      marketCapTier: 'Mega >$200B',
      estQuarterVwap: 100,
      pctVsQuarterVwap: -12,
      entryZoneStatus: 'Discount to Q-Avg',
      avgEliteHoldingQuarters: 4.2,
    };

    const fomoStock: ConsensusHolding = {
      ...lagStock,
      rank: 2,
      ticker: 'HYPE',
      name: 'Hype Rally Co',
      priceUsd: 132,
      estQuarterVwap: 100,
      pctVsQuarterVwap: 32,
      entryZoneStatus: 'Chasing (+30%+)',
      netBuyers: 1,
      netTrimmers: 6,
      holders: [
        {
          cik: '0002',
          fundName: 'Momentum Fund',
          fundTier: 'Elite 50',
          weightPct: 2.0,
          valueUsd: 200_000_000,
          status: 'TRIMMED',
          qoqSharesChangePct: -35,
          rankedWindows: ['TTM'],
        },
      ],
      avgEliteHoldingQuarters: 2.1,
    };

    const signalScreener: SignalScreenerData = {
      quarterLabel: '2026 Q3',
      clusterInitiations: [],
      heavyAdds: [],
      smartMoneyExits: [],
      contrarianBets: [],
    };

    render(
      <HoldingsTab
        topHoldings={{ filing_lag: [lagStock, fomoStock], quarter_end: [lagStock, fomoStock] }}
        signalScreener={signalScreener}
        stocksIndex={{
          CLD: makeStockIndexEntry({ ticker: 'CLD', name: 'Cloud Dip Co', pctVsQuarterVwap: -12 }),
          HYPE: makeStockIndexEntry({ ticker: 'HYPE', name: 'Hype Rally Co', pctVsQuarterVwap: 32 }),
        }}
        mode="filing_lag"
        onModeChange={() => {}}
        onSelectFund={() => {}}
        onSelectStock={() => {}}
      />
    );

    expect(screen.getByText('Cloud Dip Co')).toBeInTheDocument();
    expect(screen.getByText('Hype Rally Co')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /Lag Advantage \(1\)/i }));
    expect(screen.getByText('Cloud Dip Co')).toBeInTheDocument();
    expect(screen.queryByText('Hype Rally Co')).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /FOMO \/ Late Crowded \(1\)/i }));
    expect(screen.queryByText('Cloud Dip Co')).not.toBeInTheDocument();
    expect(screen.getByText('Hype Rally Co')).toBeInTheDocument();
  });
});
