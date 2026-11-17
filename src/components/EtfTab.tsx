import { useMemo, useState, type FC } from 'react';
import { ArrowUpRight, Search } from 'lucide-react';
import type { EtfEntry, FundSummary } from '../types/data';
import { formatPct, formatSignedPct, returnClass } from '../utils/format';

interface EtfTabProps {
  etfs: EtfEntry[];
  funds: FundSummary[];
  onSelectFund: (cik: string) => void;
}

type CorrMetric = 'overlap' | 'cosine' | 'returnCorr';

export const EtfTab: FC<EtfTabProps> = ({ etfs, funds, onSelectFund }) => {
  const [selectedFundCik, setSelectedFundCik] = useState<string>('ALL');
  const [metric, setMetric] = useState<CorrMetric>('overlap');
  const [search, setSearch] = useState('');

  const rows = useMemo(() => {
    const q = search.trim().toLowerCase();
    const metricValue = (m: EtfEntry['bestMatch']) =>
      metric === 'overlap'
        ? m.weightOverlapPct
        : metric === 'returnCorr'
          ? m.returnCorrelationPct ?? -999
          : m.cosineSimilarityPct;

    return etfs
      .filter((e) => {
        if (!q) return true;
        return (
          e.ticker.toLowerCase().includes(q) ||
          e.name.toLowerCase().includes(q) ||
          e.category.toLowerCase().includes(q) ||
          e.bestMatch.fundName.toLowerCase().includes(q) ||
          e.topHoldings.some((h) => h.ticker.toLowerCase().includes(q))
        );
      })
      .map((etf) => {
        const focusMatch =
          selectedFundCik === 'ALL'
            ? [...etf.fundMatches].sort((a, b) => metricValue(b) - metricValue(a))[0] ?? etf.bestMatch
            : etf.fundMatches.find((m) => m.cik === selectedFundCik) ?? etf.bestMatch;

        const score = metricValue(focusMatch);
        return { etf, focusMatch, score };
      })
      .sort((a, b) => b.score - a.score);
  }, [etfs, selectedFundCik, metric, search]);

  // Top 8 funds by AUM/Rank for the ETF x Fund Similarity Heatmap
  const heatmapFunds = useMemo(() => funds.slice(0, 8), [funds]);
  const heatmapEtfs = useMemo(() => rows.slice(0, 12).map((r) => r.etf), [rows]);

  const cellBg = (val: number | null) => {
    if (val == null) return 'transparent';
    const clamped = Math.max(0, Math.min(100, val));
    const alpha = Math.min(0.38, (clamped / 100) * 0.44);
    return `rgba(0, 113, 227, ${alpha.toFixed(3)})`;
  };

  const bestOverall = rows[0];
  const metricLabel =
    metric === 'overlap'
      ? 'Weight Overlap'
      : metric === 'returnCorr'
        ? '1Y Return Correlation'
        : 'Holdings Match';

  const bestScorePct = bestOverall
    ? metric === 'overlap'
      ? bestOverall.focusMatch.weightOverlapPct
      : metric === 'returnCorr'
        ? bestOverall.focusMatch.returnCorrelationPct
        : bestOverall.focusMatch.cosineSimilarityPct
    : null;

  return (
    <section aria-label="Similar ETFs and Fund Correlations">
      <div className="section-toolbar">
        <div className="section-title-group">
          <h2>ETF Lookalikes &amp; Fund Correlation Matrix</h2>
          <p>
            Official SEC Form NPORT-P ETF holdings compared against each tracked fund&apos;s latest 13F equity weights and trailing 1-year daily return correlation.
          </p>
        </div>

        <div className="controls-row">
          <select
            aria-label="Filter ETF similarity for specific fund"
            value={selectedFundCik}
            onChange={(e) => setSelectedFundCik(e.target.value)}
            className="search-input"
            style={{ paddingLeft: '0.75rem', minWidth: '230px' }}
          >
            <option value="ALL">Best Match Across All Funds</option>
            {funds.map((f) => (
              <option key={f.cik} value={f.cik}>
                Compare vs: {f.name}
              </option>
            ))}
          </select>

          <div className="segmented-group" role="group" aria-label="Correlation metric">
            <button
              type="button"
              id="etf-metric-overlap"
              className={`seg-btn ${metric === 'overlap' ? 'active' : ''}`}
              onClick={() => setMetric('overlap')}
              title="Sum of minimum overlapping portfolio weights"
            >
              Weight Overlap %
            </button>
            <button
              type="button"
              id="etf-metric-cosine"
              className={`seg-btn ${metric === 'cosine' ? 'active' : ''}`}
              onClick={() => setMetric('cosine')}
              title="Portfolio weight vector cosine similarity (0–100%)"
            >
              Holdings Match %
            </button>
            <button
              type="button"
              id="etf-metric-corr"
              className={`seg-btn ${metric === 'returnCorr' ? 'active' : ''}`}
              onClick={() => setMetric('returnCorr')}
              title="1-Year daily return Pearson correlation %"
            >
              Return Corr %
            </button>
          </div>
        </div>
      </div>

      {/* KPI Summary */}
      <div className="metrics-strip">
        <div className="metric-card">
          <div className="metric-label">Highest {metricLabel} ETF</div>
          <div className="metric-value">
            {bestOverall ? bestOverall.etf.ticker : '—'}{' '}
            <span style={{ fontSize: '0.95rem', color: 'var(--accent-primary)' }}>
              {bestScorePct !== null ? formatPct(bestScorePct, 1) : ''}
            </span>
          </div>
          <div className="metric-sub">
            Matches {bestOverall ? bestOverall.focusMatch.fundName : '—'}
          </div>
        </div>

        <div className="metric-card">
          <div className="metric-label">1Y Return Correlation</div>
          <div className="metric-value">
            {bestOverall && bestOverall.focusMatch.returnCorrelationPct !== null
              ? `${bestOverall.focusMatch.returnCorrelationPct.toFixed(1)}%`
              : '—'}
          </div>
          <div className="metric-sub">Daily co-movement vs. matched 13F book</div>
        </div>

        <div className="metric-card">
          <div className="metric-label">ETFs Indexed (NPORT-P)</div>
          <div className="metric-value">{etfs.length}</div>
          <div className="metric-sub">Tech, AI, Semi, Cloud, Growth &amp; Market ETFs</div>
        </div>

        <div className="metric-card">
          <div className="metric-label">Top Ideal Portfolio ETF Proxy</div>
          {(() => {
            const idealVal = (e: EtfEntry) =>
              metric === 'overlap'
                ? e.idealPortfolioMatch.weightOverlapPct
                : metric === 'returnCorr'
                  ? e.idealPortfolioMatch.returnCorrelationPct ?? -999
                  : e.idealPortfolioMatch.cosineSimilarityPct;
            const bestIdeal = [...etfs].sort((a, b) => idealVal(b) - idealVal(a))[0];
            const bestIdealPct = bestIdeal ? idealVal(bestIdeal) : null;
            return (
              <>
                <div className="metric-value">
                  {bestIdeal ? bestIdeal.ticker : '—'}{' '}
                  <span style={{ fontSize: '0.95rem', color: 'var(--pos-text)' }}>
                    {bestIdealPct !== null && bestIdealPct > -900 ? `${bestIdealPct.toFixed(1)}%` : ''}
                  </span>
                </div>
                <div className="metric-sub">
                  {bestIdeal
                    ? `Overlap: ${bestIdeal.idealPortfolioMatch.weightOverlapPct.toFixed(1)}% · Corr: ${bestIdeal.idealPortfolioMatch.returnCorrelationPct ?? '—'}%`
                    : '—'}
                </div>
              </>
            );
          })()}
        </div>
      </div>

      {/* ETF List Table */}
      <div className="panel">
        <div className="panel-header">
          <div>
            <h3 className="panel-title">ETF Holdings Similarity &amp; Correlation Leaderboard ({rows.length})</h3>
            <p className="panel-subtitle">
              Shows each ETF&apos;s closest matching concentrated tech fund, exact correlation %, and overlapping top stocks
            </p>
          </div>
          <div className="search-input-wrap">
            <Search size={14} />
            <input
              type="text"
              className="search-input"
              placeholder="Search ETF, category, fund, or stock..."
              value={search}
              onChange={(e) => setSearch(e.target.value)}
            />
          </div>
        </div>

        <div className="table-scroll">
          <table className="data-table compact">
            <thead>
              <tr>
                <th>ETF</th>
                <th>Category</th>
                <th className="num">ETF TTM</th>
                <th>Similar Fund</th>
                <th className={`num ${metric === 'overlap' ? 'active-col' : ''}`}>Overlap %</th>
                <th className={`num ${metric === 'cosine' ? 'active-col' : ''}`}>Match %</th>
                <th className={`num ${metric === 'returnCorr' ? 'active-col' : ''}`}>Corr %</th>
                <th>Shared Holdings (ETF / Fund)</th>
                <th>Also Similar To</th>
              </tr>
            </thead>
            <tbody>
              {rows.map(({ etf, focusMatch }) => (
                <tr key={etf.ticker}>
                  <td style={{ minWidth: '130px', maxWidth: '195px' }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: '0.4rem' }}>
                      <span className="ticker-badge">{etf.ticker}</span>
                      <span style={{ fontWeight: 600, fontSize: '0.82rem' }}>{etf.name}</span>
                    </div>
                    <div className="mono" style={{ fontSize: '0.68rem', color: 'var(--text-muted)', marginTop: '0.12rem' }}>
                      {etf.holdingsCount} holdings · {etf.reportDate}
                    </div>
                  </td>

                  <td>
                    <span className="chip">{etf.category}</span>
                  </td>

                  <td className={`num mono ${returnClass(etf.returns.TTM)}`}>
                    {formatSignedPct(etf.returns.TTM, 1)}
                  </td>

                  <td style={{ minWidth: '130px' }}>
                    <button
                      type="button"
                      className="fund-link-btn"
                      style={{ fontSize: '0.82rem' }}
                      onClick={() => onSelectFund(focusMatch.cik)}
                    >
                      {focusMatch.fundName}
                      <ArrowUpRight size={13} style={{ color: 'var(--accent-primary)', flexShrink: 0 }} />
                    </button>
                  </td>

                  <td className={`num ${metric === 'overlap' ? 'active-col' : ''}`}>
                    <div className="weight-cell">
                      <span
                        className="mono"
                        style={{
                          fontWeight: metric === 'overlap' ? 700 : 600,
                          color: metric === 'overlap' ? 'var(--accent-primary)' : 'var(--text-primary)',
                          fontSize: metric === 'overlap' ? '0.9rem' : '0.84rem',
                        }}
                      >
                        {focusMatch.weightOverlapPct.toFixed(1)}%
                      </span>
                      <div className="bar-track" style={{ width: '54px' }}>
                        <div className="bar-fill" style={{ width: `${Math.min(100, focusMatch.weightOverlapPct)}%` }} />
                      </div>
                    </div>
                  </td>

                  <td className={`num ${metric === 'cosine' ? 'active-col' : ''}`}>
                    <div className="weight-cell">
                      <span
                        className="mono"
                        style={{
                          fontWeight: metric === 'cosine' ? 700 : 600,
                          color: metric === 'cosine' ? 'var(--accent-primary)' : 'var(--text-primary)',
                          fontSize: metric === 'cosine' ? '0.9rem' : '0.84rem',
                        }}
                      >
                        {focusMatch.cosineSimilarityPct.toFixed(1)}%
                      </span>
                      <div className="bar-track" style={{ width: '54px' }}>
                        <div className="bar-fill" style={{ width: `${Math.min(100, focusMatch.cosineSimilarityPct)}%` }} />
                      </div>
                    </div>
                  </td>

                  <td className={`num mono ${metric === 'returnCorr' ? 'active-col' : ''}`}>
                    <strong style={{ color: metric === 'returnCorr' ? 'var(--accent-primary)' : 'inherit' }}>
                      {focusMatch.returnCorrelationPct !== null
                        ? `${focusMatch.returnCorrelationPct.toFixed(1)}%`
                        : '—'}
                    </strong>
                  </td>

                  <td style={{ maxWidth: '235px' }}>
                    <div style={{ display: 'flex', flexWrap: 'wrap', gap: '0.22rem' }}>
                      {focusMatch.sharedTopStocks.slice(0, 4).map((s) => (
                        <span
                          key={s.ticker}
                          className="chip"
                          title={`${s.ticker}: ETF ${s.etfWeightPct}% vs Fund ${s.fundWeightPct}%`}
                        >
                          <strong className="mono">{s.ticker}</strong>{' '}
                          <span className="mono" style={{ fontSize: '0.67rem', color: 'var(--text-muted)' }}>
                            {s.etfWeightPct.toFixed(0)}%/{s.fundWeightPct.toFixed(0)}%
                          </span>
                        </span>
                      ))}
                    </div>
                  </td>

                  <td style={{ maxWidth: '175px' }}>
                    <div style={{ display: 'flex', flexWrap: 'wrap', gap: '0.22rem' }}>
                      {[...etf.fundMatches]
                        .filter((m) => m.cik !== focusMatch.cik)
                        .sort((a, b) => {
                          const va =
                            metric === 'overlap'
                              ? a.weightOverlapPct
                              : metric === 'returnCorr'
                                ? a.returnCorrelationPct ?? -999
                                : a.cosineSimilarityPct;
                          const vb =
                            metric === 'overlap'
                              ? b.weightOverlapPct
                              : metric === 'returnCorr'
                                ? b.returnCorrelationPct ?? -999
                                : b.cosineSimilarityPct;
                          return vb - va;
                        })
                        .slice(0, 2)
                        .map((m) => {
                          const val =
                            metric === 'overlap'
                              ? m.weightOverlapPct
                              : metric === 'returnCorr'
                                ? m.returnCorrelationPct
                                : m.cosineSimilarityPct;
                          return (
                            <button
                              key={m.cik}
                              type="button"
                              className="chip clickable"
                              onClick={() => onSelectFund(m.cik)}
                            >
                              <span>{m.fundName.split(' ')[0]}</span>
                              <strong className="mono">{val !== null ? `${val.toFixed(0)}%` : '—'}</strong>
                            </button>
                          );
                        })}
                    </div>
                  </td>
                </tr>
              ))}
              {rows.length === 0 && (
                <tr>
                  <td colSpan={9} style={{ textAlign: 'center', padding: '2rem', color: 'var(--text-muted)' }}>
                    No ETFs match &ldquo;{search}&rdquo;.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>

      {/* ETF x Top Funds Similarity Matrix */}
      <div className="panel">
        <div className="panel-header">
          <div>
            <h3 className="panel-title">
              ETF × Leading Tech Funds Correlation Heatmap (
              {metric === 'returnCorr' ? '1Y Daily Return Correlation %' : metric === 'overlap' ? 'Weight Overlap %' : 'Holdings Cosine Similarity %'}
              )
            </h3>
            <p className="panel-subtitle">
              Cross-compare the top ETFs directly against our highest-performing tech funds
            </p>
          </div>
        </div>

        <div className="table-scroll">
          <table className="data-table">
            <thead>
              <tr>
                <th>ETF</th>
                {heatmapFunds.map((f) => (
                  <th key={f.cik} style={{ textAlign: 'center', minWidth: '105px' }}>
                    <button
                      type="button"
                      className="fund-link-btn"
                      style={{ fontSize: '0.73rem', justifyContent: 'center', width: '100%' }}
                      onClick={() => onSelectFund(f.cik)}
                    >
                      {f.name.replace(/ Management| Capital| Partners| Asset| Investment| Group/g, '')}
                    </button>
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {heatmapEtfs.map((etf) => (
                <tr key={etf.ticker}>
                  <td>
                    <span className="ticker-badge">{etf.ticker}</span>{' '}
                    <span style={{ fontSize: '0.76rem', color: 'var(--text-secondary)' }}>{etf.category}</span>
                  </td>
                  {heatmapFunds.map((f) => {
                    const m = etf.fundMatches.find((x) => x.cik === f.cik);
                    const val = !m
                      ? null
                      : metric === 'returnCorr'
                        ? m.returnCorrelationPct
                        : metric === 'overlap'
                          ? m.weightOverlapPct
                          : m.cosineSimilarityPct;
                    return (
                      <td
                        key={f.cik}
                        className="matrix-cell"
                        style={{ backgroundColor: cellBg(val) }}
                        title={`${etf.ticker} vs ${f.name}: Weight Overlap ${m?.weightOverlapPct ?? 0}%, Holdings Match ${m?.cosineSimilarityPct ?? 0}%, Return Corr ${m?.returnCorrelationPct ?? 'N/A'}%`}
                      >
                        {val !== null ? `${val.toFixed(1)}%` : '—'}
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </section>
  );
};
