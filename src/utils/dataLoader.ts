import type {
  BootstrapDataset,
  DataManifest,
  FundShardPayload,
  PaperTrailSummaryDataset,
  ScreenerPayload,
  StockDetail,
  StockIndexEntry,
  StocksSearchPayload,
  UpdatesFeedPayload,
} from '../types/data';

let manifestPromise: Promise<DataManifest | null> | null = null;
let bootstrapPromise: Promise<BootstrapDataset | null> | null = null;
let screenerPromise: Promise<ScreenerPayload> | null = null;
let updatesFeedPromise: Promise<UpdatesFeedPayload> | null = null;
let stocksSearchPromise: Promise<StocksSearchPayload> | null = null;
let summaryPromise: Promise<PaperTrailSummaryDataset> | null = null;
const fundShardCache = new Map<string, Promise<FundShardPayload>>();
const stockShardCache = new Map<number, Promise<Record<string, StockDetail>>>();

async function fetchJsonStrict<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, init);
  if (!res.ok) {
    throw new Error(`Failed to fetch ${url} (HTTP ${res.status})`);
  }
  return res.json() as Promise<T>;
}

export function resetDataLoaderCacheForTests(): void {
  manifestPromise = null;
  bootstrapPromise = null;
  screenerPromise = null;
  updatesFeedPromise = null;
  stocksSearchPromise = null;
  summaryPromise = null;
  fundShardCache.clear();
  stockShardCache.clear();
}

export function fetchManifest(): Promise<DataManifest | null> {
  if (!manifestPromise) {
    manifestPromise = fetchJsonStrict<DataManifest>('./data/manifest.json', { cache: 'no-cache' })
      .then((m) => (m && typeof m === 'object' && m.files ? m : null))
      .catch(() => null);
  }
  return manifestPromise;
}

export async function resolveDataUrl(filename: string): Promise<string> {
  const manifest = await fetchManifest();
  const hash = manifest?.files?.[filename];
  return hash ? `./data/${filename}?v=${encodeURIComponent(hash)}` : `./data/${filename}`;
}

export function fetchBootstrap(): Promise<BootstrapDataset | null> {
  if (!bootstrapPromise) {
    bootstrapPromise = (async () => {
      const manifest = await fetchManifest();
      if (!manifest?.files?.['bootstrap.json']) {
        return null;
      }
      try {
        const url = `./data/bootstrap.json?v=${encodeURIComponent(manifest.files['bootstrap.json'])}`;
        return await fetchJsonStrict<BootstrapDataset>(url);
      } catch {
        return null;
      }
    })();
  }
  return bootstrapPromise;
}

export function fetchScreener(): Promise<ScreenerPayload> {
  if (!screenerPromise) {
    screenerPromise = resolveDataUrl('screener.json')
      .then((url) => fetchJsonStrict<ScreenerPayload>(url))
      .catch((err) => {
        screenerPromise = null;
        throw err;
      });
  }
  return screenerPromise;
}

export function fetchUpdatesFeed(): Promise<UpdatesFeedPayload> {
  if (!updatesFeedPromise) {
    updatesFeedPromise = resolveDataUrl('updates_feed.json')
      .then((url) => fetchJsonStrict<UpdatesFeedPayload>(url))
      .catch((err) => {
        updatesFeedPromise = null;
        throw err;
      });
  }
  return updatesFeedPromise;
}

export function fetchStocksSearch(): Promise<StocksSearchPayload> {
  if (!stocksSearchPromise) {
    stocksSearchPromise = resolveDataUrl('stocks_search.json')
      .then((url) => fetchJsonStrict<StocksSearchPayload>(url))
      .catch((err) => {
        stocksSearchPromise = null;
        throw err;
      });
  }
  return stocksSearchPromise;
}

export function fetchSummary(): Promise<PaperTrailSummaryDataset> {
  if (!summaryPromise) {
    summaryPromise = resolveDataUrl('summary.json')
      .then((url) => fetchJsonStrict<PaperTrailSummaryDataset>(url))
      .catch((err) => {
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

  const p = resolveDataUrl(`funds_${cleanId}.json`)
    .then((url) => fetchJsonStrict<FundShardPayload>(url))
    .catch((err) => {
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
    shardPromise = resolveDataUrl(`stocks_${shardId}.json`)
      .then((url) => fetchJsonStrict<Record<string, StockDetail>>(url))
      .catch((err) => {
        stockShardCache.delete(shardId);
        throw err;
      });
    stockShardCache.set(shardId, shardPromise);
  }
  const shardMap = await shardPromise;
  return shardMap[idxEntry.ticker] ?? shardMap[upper] ?? null;
}
