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
  top10Windows: ReturnWindow[];
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

export interface EtfFundMatch {
  cik: string;
  fundName: string;
  cosineSimilarityPct: number; // 0-100 holdings cosine match
  weightOverlapPct: number; // sum(min(w_etf, w_fund)) * 100
  returnCorrelationPct: number | null; // trailing 1Y daily return Pearson r * 100
  blendedScorePct: number;
  sharedTopStocks: Array<{
    ticker: string;
    etfWeightPct: number;
    fundWeightPct: number;
  }>;
}

export interface EtfEntry {
  ticker: string;
  name: string;
  category: string;
  reportDate: string;
  holdingsCount: number;
  top10WeightPct: number;
  returns: WindowReturns;
  topHoldings: Array<{
    ticker: string;
    name: string;
    weightPct: number;
  }>;
  bestMatch: EtfFundMatch;
  fundMatches: EtfFundMatch[]; // sorted descending by similarity
  idealPortfolioMatch: {
    cosineSimilarityPct: number;
    weightOverlapPct: number;
    returnCorrelationPct: number | null;
  };
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

export interface TechWhaleDataset {
  metadata: {
    generatedAt: string;
    asOfDate: string;
    latestQuarterCovered: string;
    fundsEvaluatedCount: number;
    qualifiedFundsCount: number;
    rankedFundsUnionCount: number;
    total13fFilingsProcessed: number;
    etfsProcessedCount: number;
    next13fSeasonLabel: string;
    dataSources: string[];
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
  etfs: EtfEntry[];
}
