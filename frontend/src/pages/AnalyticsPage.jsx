import React, { useEffect, useState, useCallback } from 'react';
import Plot from 'react-plotly.js';
import clsx from 'clsx';
import { getPortfolioAnalytics } from '../api';
import styles from './AnalyticsPage.module.css';

const assets = [
  { label: 'Stocks', value: 'stocks' },
  { label: 'Crypto', value: 'crypto' },
  { label: 'Forex', value: 'forex' },
];

function AnalyticsPage() {
  const [assetType, setAssetType] = useState('stocks');
  const [analytics, setAnalytics] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  const loadAnalytics = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const response = await getPortfolioAnalytics(assetType, 12);
      setAnalytics(response);
    } catch (err) {
      console.error('Analytics error', err);
      setError(err.message || 'Unable to load analytics.');
    } finally {
      setLoading(false);
    }
  }, [assetType]);

  useEffect(() => {
    loadAnalytics();
  }, [loadAnalytics]);

  const metrics = analytics?.summary?.performance_metrics;
  const equityCurve = analytics?.summary?.equity_curve || [];
  const allocation = analytics?.summary?.allocation || {};
  const winLoss = analytics?.summary?.win_loss || {};

  return (
    <div className={styles.container}>
      <div className={styles.headerRow}>
        <h2 className={styles.title}>Portfolio Analytics</h2>
        <div className={styles.assetSwitch}>
          {assets.map((item) => (
            <button
              key={item.value}
              type="button"
              onClick={() => setAssetType(item.value)}
              className={clsx(styles.assetButton, assetType === item.value && styles.assetButtonActive)}
            >
              {item.label}
            </button>
          ))}
        </div>
      </div>

      {loading && <div className={styles.status}>Generating backtest…</div>}
      {error && <div className={styles.error}>{error}</div>}

      {metrics && !loading && !error && (
        <div className={styles.grid}>
          <div className={styles.metricCard}>
            <span className={styles.metricLabel}>Sharpe Ratio</span>
            <span className={styles.metricValue}>{metrics.sharpe_ratio.toFixed(2)}</span>
          </div>
          <div className={styles.metricCard}>
            <span className={styles.metricLabel}>Total Return</span>
            <span className={styles.metricValue}>{(metrics.total_return * 100).toFixed(1)}%</span>
          </div>
          <div className={styles.metricCard}>
            <span className={styles.metricLabel}>Volatility</span>
            <span className={styles.metricValue}>{(metrics.volatility * 100).toFixed(1)}%</span>
          </div>
          <div className={styles.metricCard}>
            <span className={styles.metricLabel}>Max Drawdown</span>
            <span className={styles.metricValue}>{(metrics.max_drawdown * 100).toFixed(1)}%</span>
          </div>
          <div className={styles.metricCard}>
            <span className={styles.metricLabel}>Win Rate</span>
            <span className={styles.metricValue}>{(metrics.win_rate * 100).toFixed(1)}%</span>
          </div>
        </div>
      )}

      {equityCurve.length > 0 && (
        <div className={styles.chartCard}>
          <h3 className={styles.chartTitle}>Simulated Equity Curve (90 days)</h3>
          <Plot
            data={[
              {
                x: equityCurve.map((point) => point.date),
                y: equityCurve.map((point) => point.equity),
                type: 'scatter',
                mode: 'lines',
                line: { color: '#00ff95', width: 3 },
                name: 'Equity',
              },
            ]}
            layout={{
              margin: { l: 40, r: 20, t: 20, b: 40 },
              paper_bgcolor: 'transparent',
              plot_bgcolor: 'transparent',
              xaxis: { color: '#e6edf3' },
              yaxis: { color: '#e6edf3', title: 'Equity (normalized)' },
            }}
            config={{ responsive: true, displaylogo: false, modeBarButtonsToRemove: ['lasso2d'] }}
            className={styles.plot}
          />
        </div>
      )}

      {Object.keys(allocation).length > 0 && (
        <div className={styles.chartRow}>
          <div className={styles.chartCard}>
            <h3 className={styles.chartTitle}>Allocation</h3>
            <Plot
              data={[
                {
                  values: Object.values(allocation).map((weight) => Number(weight.toFixed(3))),
                  labels: Object.keys(allocation),
                  type: 'pie',
                  hole: 0.45,
                },
              ]}
              layout={{
                margin: { l: 20, r: 20, t: 20, b: 20 },
                paper_bgcolor: 'transparent',
                plot_bgcolor: 'transparent',
                showlegend: true,
                legend: { font: { color: '#e6edf3' } },
              }}
              config={{ responsive: true, displaylogo: false }}
              className={styles.plot}
            />
          </div>
          <div className={styles.chartCard}>
            <h3 className={styles.chartTitle}>Trade Distribution</h3>
            <Plot
              data={[
                {
                  x: ['Wins', 'Losses'],
                  y: [winLoss.wins || 0, winLoss.losses || 0],
                  type: 'bar',
                  marker: { color: ['#00ff95', '#ff5e5b'] },
                },
              ]}
              layout={{
                margin: { l: 40, r: 20, t: 20, b: 40 },
                paper_bgcolor: 'transparent',
                plot_bgcolor: 'transparent',
                xaxis: { color: '#e6edf3' },
                yaxis: { color: '#e6edf3', title: 'Count' },
              }}
              config={{ responsive: true, displaylogo: false }}
              className={styles.plot}
            />
          </div>
        </div>
      )}
    </div>
  );
}

export default AnalyticsPage;
