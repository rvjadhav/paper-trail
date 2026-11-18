import type {
  FundShardPayload,
  PaperTrailSummaryDataset,
  StockDetail,
  StockIndexEntry,
} from '../types/data';

let summaryPromise: Promise<PaperTrailSummaryDataset> | null = null;
const fundShardCache = new Map<string, Promise<FundShardPayload>>();
const stockShardCache = new Map<number, Promise<Record<string, StockDetail>>>();

async function fetchJsonStrict<T>(url: string): Promise<T> {
  const res = await fetch(url);
  if (!res.ok) {
    throw new Error(`Failed to fetch ${url} (HTTP ${res.status})`);
  }
  return res.json() as Promise<T>;
}

export function fetchSummary(): Promise<PaperTrailSummaryDataset> {
  if (!summaryPromise) {
    summaryPromise = fetchJsonStrict<PaperTrailSummaryDataset>('./data/summary.json').catch((err) => {
      summaryPromise = null;
      throw err;
    });
  }
  return summaryPromise;
}

export function fetchFundShard(shardId: string): Promise<FundShardPayload> {
  const cleanId = shardId.toLowerCase();
  const existing = fundShardCache.get(cleanId);
  if (existing) return existing;

  const p = fetchJsonStrict<FundShardPayload>(`./data/funds_${cleanId}.json`).catch((err) => {
    fundShardCache.delete(cleanId);
    throw err;
  });
  fundShardCache.set(cleanId, p);
  return p;
}

export async function fetchStockDetail(
  ticker: string,
  stocksIndex: Record<string, StockIndexEntry>
): Promise<StockDetail | null> {
  const upper = ticker.toUpperCase();
  const idxEntry = stocksIndex[upper] ?? stocksIndex[ticker];
  if (!idxEntry) return null;

  const shardId = idxEntry.shard;
  let shardPromise = stockShardCache.get(shardId);
  if (!shardPromise) {
    shardPromise = fetchJsonStrict<Record<string, StockDetail>>(`./data/stocks_${shardId}.json`).catch((err) => {
      stockShardCache.delete(shardId);
      throw err;
    });
    stockShardCache.set(shardId, shardPromise);
  }
  const shardMap = await shardPromise;
  return shardMap[idxEntry.ticker] ?? shardMap[upper] ?? null;
}
