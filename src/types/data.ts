export type ReturnWindow = 'YTD' | 'TTM' | '18M' | '2Y' | '3Y' | '5Y';
export type ReturnMode = 'quarter_end' | 'filing_lag';
export type WeightMode = 'equal' | 'aum';
export type FundTier = 'Elite 50' | 'Top 100' | 'Qualified' | 'Excluded' | 'Watchlist';
export type MarketCapTier = 'Mega >$200B' | 'Large $30B-$200B' | 'Emerging <$30B';
export type EntryZoneStatus =
  | 'Discount to Q-Avg'
  | 'Near Q-Avg'
  | 'Extended'
  | 'Chasing (+30%+)';

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
  weightPct: number;
  reportedWeightPct: number;
  qoqSharesChangePct: number | null;
  qoqWeightDeltaPct: number | null;
  estFlowUsd?: number | null;
  status: 'NEW' | 'ADDED' | 'UNCHANGED' | 'TRIMMED';
  pricePerShare: number | null;
}

export interface EquityPoint {
  date: string;
  nav: number;
}

export interface QuarterlyReturnRow {
  quarter: string;
  periodEnd: string;
  filingDate: string;
  equityAumUsd: number;
  positionCount: number;
  top10WeightPct: number;
  techTmtPct: number;
  quarterReturnPct: number | null;
  qqqQuarterReturnPct: number | null;
}

export interface FundMetrics {
  cagrPct: number | null;
  maxDrawdownPct: number | null;
  sharpeProxy: number | null;
  sortinoRatio: number | null;
  infoRatioVsQqq: number | null;
  betaVsQqq: number | null;
  alphaVsQqqPct: number | null;
  annualizedVolPct: number | null;
  battingAvgPct: number | null;
  avgTurnoverPct: number | null;
  skillScore: number | null;
  skillRank: number | null;
}

export interface MidQuarterAlert {
  id: string;
  filerCik: string;
  fundName: string;
  form: string;
  isActivist: boolean;
  filingDate: string;
  accessionNumber: string;
  subjectCik: string;
  ticker: string;
  issuerName: string;
  edgarUrl: string;
  percentOfClass?: number | null;
  sharesOwned?: number | null;
  stakeStatus?: string | null;
  eventDate?: string | null;
}

export interface FundSummary {
  cik: string;
  shard: string;
  name: string;
  legalName: string;
  manager: string;
  hq: string;
  styleTag: string;
  strategySummary: string;
  source: 'Curated Core' | 'EDGAR Discovered';
  fundTier: FundTier;
  isLeaderboardEligible: boolean;
  exclusionReason: string | null;
  latestPeriod: string;
  latestQuarterLabel: string;
  isStaleQuarter?: boolean;
  latestFilingDate: string;
  equityAumUsd: number;
  totalReportedUsd: number;
  equityPositionCount: number;
  totalPositionCount: number;
  top10WeightPct: number;
  effectivePositions: number;
  techTmtPct: number;
  etfSharePct: number;
  quartersTracked: number;
  returns: {
    quarter_end: WindowReturns;
    filing_lag: WindowReturns;
  };
  metrics: {
    quarter_end: FundMetrics;
    filing_lag: FundMetrics;
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
  searchableTickers: string[];
  edgarUrl: string;
}

export interface FundShardDetail {
  cik: string;
  holdings: FundHolding[];
  equityCurve: {
    quarter_end: EquityPoint[];
    filing_lag: EquityPoint[];
  };
  quarterlyHistory: QuarterlyReturnRow[];
  midQuarterAlerts: MidQuarterAlert[];
}

export interface RankedWindowEntry {
  rank: number;
  skillRank: number;
  cik: string;
  returnPct: number;
  skillScore: number | null;
}

export interface StockFundamentals {
  revenueTtmUsd: number | null;
  revenueGrowthYoYPct: number | null;
  operatingMarginPct: number | null;
  fcfMarginPct: number | null;
  rdIntensityPct: number | null;
  shareDilutionYoYPct: number | null;
}

export interface StockHolderDetail {
  cik: string;
  fundName: string;
  fundTier: FundTier;
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
  subSector: string;
  isTechTmt: boolean;
  priceUsd: number | null;
  stockYtdPct: number | null;
  stockTtmPct: number | null;
  holderCount: number;
  totalRankedFundsPool: number;
  equalAggregateWeightPct: number;
  avgHolderWeightPct: number;
  aumAggregateWeightPct: number;
  totalValueUsd: number;
  netBuyers: number;
  netTrimmers: number;
  holders: StockHolderDetail[];
  summaryText: string;
  marketCapUsd?: number | null;
  marketCapTier?: MarketCapTier | null;
  estQuarterVwap?: number | null;
  pctVsQuarterVwap?: number | null;
  entryZoneStatus?: EntryZoneStatus | null;
  fundamentals?: StockFundamentals | null;
}

export interface SignalScreenerEntry {
  ticker: string;
  name: string;
  sector: string;
  subSector: string;
  isTechTmt: boolean;
  priceUsd: number | null;
  stockTtmPct: number | null;
  holderCount: number;
  eliteHoldersCount: number;
  totalValueUsd: number;
  avgWeightPct: number;
  maxWeightPct: number;
  newCount: number;
  eliteNewCount: number;
  addedCount: number;
  trimmedCount: number;
  exitedCount: number;
  netDeltaUsd: number;
  netEstFlowUsd?: number;
  movers: Array<{
    cik: string;
    fundName: string;
    fundTier: FundTier;
    action: 'NEW' | 'ADDED' | 'TRIMMED' | 'EXITED';
    currBookPct: number;
    prevBookPct: number;
    valueDeltaUsd: number;
    estFlowUsd?: number;
    sharesDeltaPct: number | null;
  }>;
  topBackers: Array<{
    cik: string;
    fundName: string;
    fundTier: FundTier;
    weightPct: number;
    status: 'NEW' | 'ADDED' | 'UNCHANGED' | 'TRIMMED';
  }>;
  marketCapUsd?: number | null;
  marketCapTier?: MarketCapTier | null;
  estQuarterVwap?: number | null;
  pctVsQuarterVwap?: number | null;
  entryZoneStatus?: EntryZoneStatus | null;
  fundamentals?: StockFundamentals | null;
}

export interface SignalScreenerData {
  quarterLabel: string;
  clusterInitiations: SignalScreenerEntry[];
  heavyAdds: SignalScreenerEntry[];
  smartMoneyExits: SignalScreenerEntry[];
  contrarianBets: SignalScreenerEntry[];
}

export interface IdealPortfolioPosition {
  rank: number;
  ticker: string;
  cusip: string;
  name: string;
  sector: string;
  subSector: string;
  industry: string;
  isTechTmt: boolean;
  rawScore: number;
  inflectionScore: number;
  idealWeightPct: number;
  convictionScore: number;
  holderCount: number;
  netNew: number;
  netAdd: number;
  netTrim: number;
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
  marketCapUsd?: number | null;
  marketCapTier?: MarketCapTier | null;
  estQuarterVwap?: number | null;
  pctVsQuarterVwap?: number | null;
  entryZoneStatus?: EntryZoneStatus | null;
  fundamentals?: StockFundamentals | null;
}

export interface IdealPortfolioBacktestMode {
  returns: WindowReturns;
  cagrPct: number | null;
  maxDrawdownPct: number | null;
  sharpeProxy: number | null;
  sortinoRatio: number | null;
  infoRatioVsQqq: number | null;
  curve: EquityPoint[];
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
  candidatePool: IdealPortfolioPosition[];
  backtest: {
    quarter_end: IdealPortfolioBacktestMode;
    filing_lag: IdealPortfolioBacktestMode;
  };
}

export type SecurityType = 'EQUITY' | 'CALL' | 'PUT' | 'NOTE';
export type MoveAction = 'NEW' | 'ADDED' | 'TRIMMED' | 'UNCHANGED' | 'EXITED';

export interface PositionMoveItem {
  cusip: string;
  ticker: string;
  name: string;
  sector: string;
  industry?: string;
  securityType: SecurityType;
  action: MoveAction;
  prevShares: number;
  currShares: number;
  sharesDelta: number;
  sharesDeltaPct: number | null;
  prevValueUsd: number;
  currValueUsd: number;
  valueDeltaUsd: number;
  estFlowUsd?: number;
  prevBookPct: number;
  currBookPct: number;
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

export interface FundUpdateSummary {
  cik: string;
  shard: string;
  fundName: string;
  legalName: string;
  manager: string;
  styleTag: string;
  source: 'Curated Core' | 'EDGAR Discovered';
  fundTier: FundTier;
  form: string;
  amendmentType: string;
  inRankings: boolean;
  isRankedTop100AnyWindow: boolean;
  rankedWindows: ReturnWindow[];

  quarterLabel: string;
  periodEnd: string;
  prevQuarterLabel: string;
  prevPeriodEnd: string;
  filingDate: string;
  originalFilingDate: string;
  latestAmendmentDate: string;
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
}

export interface UpdateShardDetail {
  cik: string;
  allMoves: PositionMoveItem[];
}

export interface FundShardPayload {
  funds: Record<string, FundShardDetail>;
  updates: Record<string, UpdateShardDetail>;
}

export interface ConsensusMoveSummary {
  ticker: string;
  name: string;
  sector: string;
  fundCount: number;
  totalCurrValueUsd: number;
  totalDeltaUsd: number;
  totalEstFlowUsd?: number;
  funds: Array<{
    cik: string;
    fundName: string;
    action: 'NEW' | 'ADDED' | 'TRIMMED' | 'EXITED';
    securityType: SecurityType;
    currBookPct: number;
    prevBookPct: number;
    valueDeltaUsd: number;
    estFlowUsd?: number;
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
  sortinoRatio: number | null;
  curve: EquityPoint[];
}

export interface StockIndexEntry {
  ticker: string;
  name: string;
  sector: string;
  subSector: string;
  shard: number;
  priceUsd: number | null;
  marketCapTier: MarketCapTier | null;
  holderCount: number;
  eliteHoldersCount: number;
  signalVerdict: string;
  entryZoneStatus: EntryZoneStatus | null;
  pctVsQuarterVwap: number | null;
}

export interface StockDetailHolder {
  cik: string;
  fundName: string;
  fundTier: FundTier;
  skillScore: number | null;
  weightPct: number;
  valueUsd: number;
  status: 'NEW' | 'ADDED' | 'UNCHANGED' | 'TRIMMED';
  qoqSharesChangePct: number | null;
  qoqWeightDeltaPct: number | null;
}

export interface StockDetailMove {
  cik: string;
  fundName: string;
  fundTier: FundTier;
  quarterLabel: string;
  action: MoveAction;
  sharesDeltaPct: number | null;
  valueDeltaUsd: number;
  estFlowUsd?: number;
  prevBookPct: number;
  currBookPct: number;
}

export interface StockDetail {
  ticker: string;
  cusip: string;
  name: string;
  sector: string;
  industry: string;
  subSector: string;
  isTechTmt: boolean;
  priceUsd: number | null;
  stockYtdPct: number | null;
  stockTtmPct: number | null;
  marketCapUsd: number | null;
  marketCapTier: MarketCapTier | null;
  estQuarterVwap: number | null;
  quarterLow: number | null;
  quarterHigh: number | null;
  pctVsQuarterVwap: number | null;
  entryZoneStatus: EntryZoneStatus | null;
  fundamentals: StockFundamentals | null;
  signalVerdict: string;
  idealPortfolioRank: number | null;
  idealWeightPct: number | null;
  holderCount: number;
  eliteHoldersCount: number;
  totalValueUsd: number;
  avgWeightPct: number;
  maxWeightPct: number;
  newCount: number;
  addedCount: number;
  trimmedCount: number;
  exitedCount: number;
  netEstFlowUsd?: number;
  holders: StockDetailHolder[];
  recentMoves: StockDetailMove[];
  midQuarterAlerts: MidQuarterAlert[];
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
  leaderboardEligibleCount: number;
  elite50Count: number;
  curatedQualifiedCount: number;
  discoveredQualifiedCount: number;
  pitHistoricalBacktestCount?: number;
  rankedUnionQeCount: number;
  rankedUnionLagCount: number;
  updatesCardsCount: number;
  idealPortfolioCount: number;
  stocksIndexedCount: number;
  midQuarterAlertsCount: number;
  bulkDatasetsCount: number;
  bulkMaxFilingDate: string;
  postBulkFilingsCount: number;
}

export interface PaperTrailSummaryDataset {
  metadata: {
    generatedAt: string;
    asOfDate: string;
    latestQuarterCovered: string;
    fundsEvaluatedCount: number;
    qualifiedFundsCount: number;
    leaderboardEligibleCount: number;
    elite50Count: number;
    rankedFundsUnionCount: number;
    total13fFilingsProcessed: number;
    next13fSeasonLabel: string;
    riskFreeLatestPct: number;
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
  signalScreener: SignalScreenerData;
  idealPortfolio: IdealPortfolioData;
  fundUpdates: FundUpdateSummary[];
  updatesOverview: UpdatesOverview;
  midQuarterAlerts: MidQuarterAlert[];
  stocksIndex: Record<string, StockIndexEntry>;
}
