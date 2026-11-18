export type ReturnWindow = 'YTD' | 'TTM' | '18M' | '2Y' | '3Y' | '5Y';
export type ReturnMode = 'quarter_end' | 'filing_lag';
export type WeightMode = 'equal' | 'aum';

export interface WindowReturns {
  YTD: number | null;
  TTM: number | null;
  '18M': number | null;
  '2Y': number | null;
  '3Y': number | null;
  '5Y': number | null;
}

export interface FundHolding {
  cusip: string;
  ticker: string;
  name: string;
  sector: string;
  industry: string;
  isTechTmt: boolean;
  securityType: 'EQUITY' | 'CALL' | 'PUT' | 'NOTE';
  shares: number;
  valueUsd: number;
  weightPct: number; // % of equity portfolio (0 for options/notes if excluded, or % of reported)
  reportedWeightPct: number; // % of total reported 13F value
  qoqSharesChangePct: number | null; // null if new position
  qoqWeightDeltaPct: number | null;
  status: 'NEW' | 'ADDED' | 'UNCHANGED' | 'TRIMMED';
  pricePerShare: number | null;
}

export interface EquityPoint {
  date: string; // YYYY-MM-DD
  nav: number; // Base 100 at start of series
}

export interface QuarterlyReturnRow {
  quarter: string; // e.g. "2026 Q3"
  periodEnd: string;
  filingDate: string;
  equityAumUsd: number;
  positionCount: number;
  top10WeightPct: number;
  techTmtPct: number;
  quarterReturnPct: number | null;
  qqqQuarterReturnPct: number | null;
}

export interface FundSummary {
  cik: string;
  name: string;
  legalName: string;
  manager: string;
  hq: string;
  styleTag: string;
  strategySummary: string;
  source: 'Curated Core' | 'EDGAR Discovered';
  latestPeriod: string; // YYYY-MM-DD
  latestQuarterLabel: string; // e.g. "2026 Q3"
  latestFilingDate: string; // YYYY-MM-DD
  equityAumUsd: number;
  totalReportedUsd: number;
  equityPositionCount: number;
  totalPositionCount: number;
  top10WeightPct: number;
  effectivePositions: number;
  techTmtPct: number;
  quartersTracked: number;
  returns: {
    quarter_end: WindowReturns;
    filing_lag: WindowReturns;
  };
  metrics: {
    quarter_end: {
      cagrPct: number | null;
      maxDrawdownPct: number | null;
      sharpeProxy: number | null;
      betaVsQqq: number | null;
      alphaVsQqqPct: number | null;
      annualizedVolPct: number | null;
    };
    filing_lag: {
      cagrPct: number | null;
      maxDrawdownPct: number | null;
      sharpeProxy: number | null;
      betaVsQqq: number | null;
      alphaVsQqqPct: number | null;
      annualizedVolPct: number | null;
    };
  };
  topHoldingsPreview: Array<{
    ticker: string;
    name: string;
    weightPct: number;
  }>;
  sectorBreakdown: Array<{
    sector: string;
    weightPct: number;
  }>;
  equityCurve: {
    quarter_end: EquityPoint[];
    filing_lag: EquityPoint[];
  };
  quarterlyHistory: QuarterlyReturnRow[];
  holdings: FundHolding[];
  edgarUrl: string;
}

export interface RankedWindowEntry {
  rank: number;
  cik: string;
  returnPct: number;
}

export interface StockHolderDetail {
  cik: string;
  fundName: string;
  weightPct: number;
  valueUsd: number;
  status: 'NEW' | 'ADDED' | 'UNCHANGED' | 'TRIMMED';
  qoqSharesChangePct: number | null;
  rankedWindows: ReturnWindow[];
}

export interface ConsensusHolding {
  rank: number;
  ticker: string;
  cusip: string;
  name: string;
  sector: string;
  industry: string;
  isTechTmt: boolean;
  priceUsd: number | null;
  stockYtdPct: number | null;
  stockTtmPct: number | null;
  holderCount: number;
  totalRankedFundsPool: number;
  equalAggregateWeightPct: number; // Average weight across the ranked funds pool
  avgHolderWeightPct: number; // Average weight among funds that hold it
  aumAggregateWeightPct: number; // Dollar held / combined pool AUM
  totalValueUsd: number;
  netBuyers: number; // NEW + ADDED
  netTrimmers: number; // TRIMMED
  holders: StockHolderDetail[];
  summaryText: string;
}

export interface IdealPortfolioPosition {
  rank: number;
  ticker: string;
  cusip: string;
  name: string;
  sector: string;
  industry: string;
  idealWeightPct: number; // Sums to 100.0
  convictionScore: number; // 0-100
  holderCount: number;
  avgFundWeightPct: number;
  maxFundWeightPct: number;
  topHolderName: string;
  rationaleTag:
    | 'Core Consensus'
    | 'Top-Decile Alpha'
    | 'High-Conviction Specialist'
    | 'Active Accumulation';
  topBackers: Array<{
    cik: string;
    fundName: string;
    weightPct: number;
    status: string;
  }>;
  stockTtmPct: number | null;
  priceUsd: number | null;
}

export interface IdealPortfolioData {
  positionCount: number;
  maxPositionCapPct: number;
  rankedFundsCount: number;
  effectivePositions: number;
  top10WeightPct: number;
  methodologySummary: string;
  sectorBreakdown: Array<{
    sector: string;
    weightPct: number;
    stockCount: number;
  }>;
  positions: IdealPortfolioPosition[];
  backtest: {
    quarter_end: {
      returns: WindowReturns;
      cagrPct: number | null;
      maxDrawdownPct: number | null;
      sharpeProxy: number | null;
      curve: EquityPoint[];
    };
    filing_lag: {
      returns: WindowReturns;
      cagrPct: number | null;
      maxDrawdownPct: number | null;
      sharpeProxy: number | null;
      curve: EquityPoint[];
    };
  };
}

export type SecurityType = 'EQUITY' | 'CALL' | 'PUT' | 'NOTE';
export type MoveAction = 'NEW' | 'ADDED' | 'TRIMMED' | 'UNCHANGED' | 'EXITED';

export interface PositionMoveItem {
  cusip: string;
  ticker: string;
  name: string;
  sector: string;
  industry: string;
  securityType: SecurityType;
  action: MoveAction;
  prevShares: number;
  currShares: number;
  sharesDelta: number;
  sharesDeltaPct: number | null; // null if NEW, -100 if EXITED
  prevValueUsd: number;
  currValueUsd: number;
  valueDeltaUsd: number;
  prevBookPct: number; // % of total reported 13F book in prior quarter
  currBookPct: number; // % of total reported 13F book in current quarter
  bookDeltaPct: number;
  valuationShiftType: 'MARKDOWN' | 'MARKUP' | null;
  tagNote: string | null;
}

export interface HedgeLegSummary {
  ticker: string;
  name: string;
  securityType: 'PUT' | 'CALL';
  action: MoveAction;
  prevShares: number;
  currShares: number;
  prevValueUsd: number;
  currValueUsd: number;
  prevBookPct: number;
  currBookPct: number;
  sharesDeltaPct: number | null;
}

export interface InstrumentShiftSignal {
  ticker: string;
  name: string;
  shiftType:
    | 'COMMON_TO_CALL'
    | 'CALL_TO_COMMON'
    | 'MULTI_LEG_SURGE'
    | 'MULTI_LEG_CUT'
    | 'NEW_HEDGE'
    | 'HEDGE_SURGE'
    | 'CALL_EXIT';
  label: string;
  detail: string;
  prevTotalUsd: number;
  currTotalUsd: number;
  currBookPct: number;
}

export interface FundUpdateEntry {
  cik: string;
  fundName: string;
  legalName: string;
  manager: string;
  styleTag: string;
  source: 'Curated Core' | 'EDGAR Discovered';
  inRankings: boolean;
  isRankedTop100AnyWindow: boolean;
  rankedWindows: ReturnWindow[];

  quarterLabel: string;
  periodEnd: string;
  prevQuarterLabel: string;
  prevPeriodEnd: string;
  filingDate: string;
  prevFilingDate: string;
  statutoryDeadline: string;
  daysBeforeDeadline: number;
  deadlineBadge: 'Deadline Day' | 'Early Filer' | 'Amended / Post-Deadline';
  accessionNumber: string;
  filingUrl: string;
  edgarUrl: string;

  currTotalReportedUsd: number;
  prevTotalReportedUsd: number;
  totalValueDeltaUsd: number;
  totalValueDeltaPct: number | null;
  currEquityAumUsd: number;
  prevEquityAumUsd: number;
  currPositionCount: number;
  prevPositionCount: number;

  currEquityBookPct: number;
  prevEquityBookPct: number;
  currCallUsd: number;
  prevCallUsd: number;
  currCallBookPct: number;
  prevCallBookPct: number;
  currPutUsd: number;
  prevPutUsd: number;
  currPutBookPct: number;
  prevPutBookPct: number;
  putBookDeltaPct: number;
  hasSignificantHedge: boolean;

  newCount: number;
  addedCount: number;
  trimmedCount: number;
  exitedCount: number;
  unchangedCount: number;
  grossActivityUsd: number;
  activityRatioPct: number;

  narrativeHeadline: string;
  narrativeLead: string;
  narrativeFocusNotes: string[];
  narrativeHedgeAndStructure: string | null;
  narrativeOtherMoves: string | null;

  putLegs: HedgeLegSummary[];
  callLegs: HedgeLegSummary[];
  instrumentShifts: InstrumentShiftSignal[];
  valuationShifts: PositionMoveItem[];

  topNew: PositionMoveItem[];
  topAdded: PositionMoveItem[];
  topTrimmed: PositionMoveItem[];
  topExited: PositionMoveItem[];
  allMoves: PositionMoveItem[];
}

export interface ConsensusMoveSummary {
  ticker: string;
  name: string;
  sector: string;
  fundCount: number;
  totalCurrValueUsd: number;
  totalDeltaUsd: number;
  funds: Array<{
    cik: string;
    fundName: string;
    action: 'NEW' | 'ADDED' | 'TRIMMED' | 'EXITED';
    securityType: SecurityType;
    currBookPct: number;
    prevBookPct: number;
    valueDeltaUsd: number;
    sharesDeltaPct: number | null;
  }>;
}

export interface UpdatesOverview {
  latestQuarterLabel: string;
  latestQuarterPeriodEnd: string;
  totalUpdateCards: number;
  latestQuarterFilersCount: number;
  totalEligibleFundsCount: number;
  deadlineDayFilersCount: number;
  fundsWithPutHedgesCount: number;
  totalPutNotionalUsd: number;
  totalCallNotionalUsd: number;
  totalLatestBookUsd: number;
  consensusNewInitiations: ConsensusMoveSummary[];
  consensusHeavyAdds: ConsensusMoveSummary[];
  consensusExitsAndTrims: ConsensusMoveSummary[];
  consensusOptionMoves: ConsensusMoveSummary[];
}

export interface BenchmarkSeries {
  ticker: string;
  name: string;
  returns: WindowReturns;
  cagrPct: number | null;
  maxDrawdownPct: number | null;
  sharpeProxy: number | null;
  curve: EquityPoint[];
}

export interface UniverseFunnel {
  method: string;
  totalCiksScanned: number;
  activeRecentCount: number;
  excludedInstitutionsCount: number;
  nonExcludedManagerCount: number;
  fourPlusQuartersCount: number;
  sizeAndEquityPassCount: number;
  concentrationPassCount: number;
  techFloorPassCount: number;
  strictQualificationCount: number;
  pricedHistoryPassCount: number;
  qualifiedFundsCount: number;
  curatedQualifiedCount: number;
  discoveredQualifiedCount: number;
  rankedUnionQeCount: number;
  rankedUnionLagCount: number;
  updatesCardsCount: number;
  idealPortfolioCount: number;
  bulkDatasetsCount: number;
  bulkMaxFilingDate: string;
  postBulkFilingsCount: number;
}

export interface PaperTrailDataset {
  metadata: {
    generatedAt: string;
    asOfDate: string;
    latestQuarterCovered: string;
    fundsEvaluatedCount: number;
    qualifiedFundsCount: number;
    rankedFundsUnionCount: number;
    total13fFilingsProcessed: number;
    next13fSeasonLabel: string;
    dataSources: string[];
    universeFunnel?: UniverseFunnel;
  };
  benchmarks: Record<'QQQ' | 'SPY' | 'XLK', BenchmarkSeries>;
  funds: FundSummary[];
  rankings: {
    quarter_end: Record<ReturnWindow, RankedWindowEntry[]>;
    filing_lag: Record<ReturnWindow, RankedWindowEntry[]>;
  };
  rankedUnionCiks: {
    quarter_end: string[];
    filing_lag: string[];
  };
  topHoldings: {
    quarter_end: ConsensusHolding[];
    filing_lag: ConsensusHolding[];
  };
  idealPortfolio: IdealPortfolioData;
  fundUpdates: FundUpdateEntry[];
  updatesOverview: UpdatesOverview;
}
