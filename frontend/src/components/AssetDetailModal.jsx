import React, { useState, useEffect, useCallback } from 'react';
import { getAssetDetail } from '../api';
import styles from './AssetDetailModal.module.css';

const timeframes = [
  { label: '1D', value: '1D' },
  { label: '1W', value: '1W' },
  { label: '1M', value: '1M' },
  { label: '3M', value: '3M' },
  { label: '1Y', value: '1Y' },
];

function AssetDetailModal({ ticker, onClose }) {
  const [assetData, setAssetData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const loadAssetData = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const response = await getAssetDetail(ticker);
      setAssetData(response);
      if (response.timeframes && response.timeframes['3M']) {
        setActiveTimeframe('3M');
      }
    } catch (err) {
      console.error('Asset detail error', err);
      setError(err.message || 'Unable to load asset data.');
    } finally {
      setLoading(false);
    }
  }, [ticker]);

  useEffect(() => {
    if (ticker) {
      loadAssetData();
    }
  }, [ticker, loadAssetData]);

    const dateSet = new Set(dates);
    const ma50 = (assetData?.indicators?.ma50 || []).filter((entry) => dateSet.has(entry.date));
    const ma200 = (assetData?.indicators?.ma200 || []).filter((entry) => dateSet.has(entry.date));

    return {
      dates,
      open,
      high,
      low,
      close,
      volume,
      ma50,
      ma200,
    };
  }, [timeframeSeries, assetData]);

  const rsiSeries = useMemo(() => {
    if (!assetData?.indicators?.rsi) return [];
    return assetData.indicators.rsi.filter((entry) => timeframeSeries.find((i) => i.date === entry.date));
  }, [assetData, timeframeSeries]);

  const macdSeries = useMemo(() => {
    if (!assetData?.indicators?.macd) return [];
    return assetData.indicators.macd.filter((entry) => timeframeSeries.find((i) => i.date === entry.date));
  }, [assetData, timeframeSeries]);

  if (!ticker) return null;

  return (
    <div className={styles.backdrop} onClick={onClose}>
      <div className={styles.modal} onClick={(event) => event.stopPropagation()}>
        <div className={styles.header}>
          <div>
            <h2 className={styles.title}>{ticker}</h2>
            <p className={styles.subtitle}>{assetData?.asset_type?.toUpperCase() || ''}</p>
          </div>
          <button type="button" className={styles.closeButton} onClick={onClose}>
            ×
          </button>
        </div>

        {loading && <div className={styles.loading}>Loading market data…</div>}
        {error && <div className={styles.error}>{error}</div>}

        {assetData && !loading && !error && (
          <div className={styles.body}>
            <div className={styles.summaryRow}>
              <div className={styles.priceCard}>
                <span className={styles.priceLabel}>Current Price</span>
                <span className={styles.priceValue}>${assetData.current_price.toFixed(2)}</span>
              </div>
              <div className={styles.recommendationBadge}>
                {assetData.recommendation}
              </div>
              {assetData.position_size && (
                <div className={styles.positionCard}>
                  <div className={styles.positionHeader}>Position sizing</div>
                  <div className={styles.positionRow}>
                    Suggested {assetData.recommendation}
                    <span className={styles.positionValue}>
                      {assetData.position_size.recommended_size.toFixed(2)} units @
                      {` ${assetData.position_size.risk_pct.toFixed(2)}% risk`}
                    </span>
                  </div>
                  <div className={styles.positionRow}>Stop loss {assetData.position_size.stop_loss.toFixed(2)}</div>
                  <div className={styles.positionRow}>Take profit {assetData.position_size.take_profit.toFixed(2)}</div>
                </div>
              )}
            </div>

            <div className={styles.timeframeToggle}>
              {timeframes.map((item) => (
                <button
                  key={item.value}
                  type="button"
                  onClick={() => setActiveTimeframe(item.value)}
                  className={clsx(styles.tfButton, activeTimeframe === item.value && styles.tfButtonActive)}
                >
                  {item.label}
                </button>
              ))}
            </div>

            {chartData ? (
              <div className={styles.chartStack}>
                <Plot
                  data={[
                    {
                      x: chartData.dates,
                      open: chartData.open,
                      high: chartData.high,
                      low: chartData.low,
                      close: chartData.close,
                      type: 'candlestick',
                      name: 'Price',
                      increasing: { line: { color: '#00ff95' } },
                      decreasing: { line: { color: '#ff5e5b' } },
                    },
                    {
                      x: chartData.ma50.map((item) => item.date),
                      y: chartData.ma50.map((item) => item.value),
                      type: 'scatter',
                      mode: 'lines',
                      name: 'MA50',
                      line: { color: '#00c8b3', width: 2 },
                    },
                    {
                      x: chartData.ma200.map((item) => item.date),
                      y: chartData.ma200.map((item) => item.value),
                      type: 'scatter',
                      mode: 'lines',
                      name: 'MA200',
                      line: { color: '#ffaa00', width: 2 },
                    },
                    {
                      x: chartData.dates,
                      y: chartData.volume,
                      type: 'bar',
                      name: 'Volume',
                      marker: { color: 'rgba(0, 255, 149, 0.3)' },
                      yaxis: 'y2',
                    },
                  ]}
                  layout={{
                    dragmode: 'pan',
                    margin: { l: 40, r: 30, t: 20, b: 20 },
                    paper_bgcolor: 'transparent',
                    plot_bgcolor: 'transparent',
                    xaxis: {
                      title: 'Date',
                      color: '#e6edf3',
                      rangeslider: { visible: false },
                    },
                    yaxis: { title: 'Price', color: '#e6edf3' },
                    yaxis2: {
                      overlaying: 'y',
                      side: 'right',
                      showgrid: false,
                      color: '#8b949e',
                      rangemode: 'tozero',
                    },
                    legend: {
                      orientation: 'h',
                      y: -0.25,
                      x: 0,
                      font: { color: '#e6edf3' },
                    },
                    shapes:
                      assetData.position_size
                        ? [
                            {
                              type: 'line',
                              xref: 'paper',
                              x0: 0,
                              x1: 1,
                              y0: assetData.position_size.stop_loss,
                              y1: assetData.position_size.stop_loss,
                              line: { color: '#ff5e5b', width: 1, dash: 'dot' },
                            },
                            {
                              type: 'line',
                              xref: 'paper',
                              x0: 0,
                              x1: 1,
                              y0: assetData.position_size.take_profit,
                              y1: assetData.position_size.take_profit,
                              line: { color: '#00ff95', width: 1, dash: 'dot' },
                            },
                          ]
                        : [],
                  }}
                  config={{ responsive: true, displaylogo: false, modeBarButtonsToRemove: ['lasso2d'] }}
                  className={styles.plot}
                />

                <Plot
                  data={[
                    {
                      x: rsiSeries.map((item) => item.date),
                      y: rsiSeries.map((item) => item.value),
                      type: 'scatter',
                      mode: 'lines',
                      name: 'RSI',
                      line: { color: '#00c8b3', width: 2 },
                    },
                    {
                      x: rsiSeries.map((item) => item.date),
                      y: Array(rsiSeries.length).fill(70),
                      type: 'scatter',
                      mode: 'lines',
                      name: 'Overbought',
                      line: { color: '#ff5e5b', width: 1, dash: 'dot' },
                      hoverinfo: 'skip',
                    },
                    {
                      x: rsiSeries.map((item) => item.date),
                      y: Array(rsiSeries.length).fill(30),
                      type: 'scatter',
                      mode: 'lines',
                      name: 'Oversold',
                      line: { color: '#00ff95', width: 1, dash: 'dot' },
                      hoverinfo: 'skip',
                    },
                  ]}
                  layout={{
                    margin: { l: 40, r: 30, t: 10, b: 20 },
                    paper_bgcolor: 'transparent',
                    plot_bgcolor: 'transparent',
                    xaxis: { color: '#e6edf3' },
                    yaxis: { title: 'RSI', range: [0, 100], color: '#e6edf3' },
                    showlegend: false,
                  }}
                  config={{ responsive: true, displaylogo: false, modeBarButtonsToRemove: ['lasso2d'] }}
                  className={styles.plot}
                />

                <Plot
                  data={[
                    {
                      x: macdSeries.map((item) => item.date),
                      y: macdSeries.map((item) => item.hist),
                      type: 'bar',
                      name: 'MACD Histogram',
                      marker: { color: 'rgba(0, 255, 149, 0.3)' },
                    },
                    {
                      x: macdSeries.map((item) => item.date),
                      y: macdSeries.map((item) => item.macd),
                      type: 'scatter',
                      mode: 'lines',
                      name: 'MACD',
                      line: { color: '#00ff95', width: 2 },
                    },
                    {
                      x: macdSeries.map((item) => item.date),
                      y: macdSeries.map((item) => item.signal),
                      type: 'scatter',
                      mode: 'lines',
                      name: 'Signal',
                      line: { color: '#ffaa00', width: 2 },
                    },
                  ]}
                  layout={{
                    margin: { l: 40, r: 30, t: 10, b: 30 },
                    paper_bgcolor: 'transparent',
                    plot_bgcolor: 'transparent',
                    xaxis: { color: '#e6edf3' },
                    yaxis: { title: 'MACD', color: '#e6edf3' },
                    legend: { orientation: 'h', y: -0.25, font: { color: '#e6edf3' } },
                  }}
                  config={{ responsive: true, displaylogo: false, modeBarButtonsToRemove: ['lasso2d'] }}
                  className={styles.plot}
                />
              </div>
            ) : (
              <div className={styles.noData}>No chart data available for {activeTimeframe}.</div>
            )}

            <div className={styles.signalsSection}>
              <h3 className={styles.sectionTitle}>Strategy Contributions</h3>
              <div className={styles.signalsGrid}>
                {Object.entries(assetData.strategy_signals || {}).map(([name, value]) => (
                  <div key={name} className={styles.signalCard}>
                    <span className={styles.signalName}>{name.replace(/_/g, ' ')}</span>
                    <span className={clsx(value >= 0 ? styles.signalPositive : styles.signalNegative)}>
                      {value.toFixed(3)}
                    </span>
                  </div>
                ))}
              </div>
            </div>

            {assetData.recent_recommendations?.length > 0 && (
              <div className={styles.recommendationHistory}>
                <h3 className={styles.sectionTitle}>Recent Signals</h3>
                <table className={styles.recTable}>
                  <thead>
                    <tr>
                      <th>Date</th>
                      <th>Recommendation</th>
                      <th>Score</th>
                      <th>Confidence</th>
                    </tr>
                  </thead>
                  <tbody>
                    {assetData.recent_recommendations.map((item, idx) => (
                      <tr key={idx}>
                        <td>{new Date(item.date).toLocaleString()}</td>
                        <td>
                          <span className={clsx(styles.badge, styles[`badge${item.recommendation}`])}>{item.recommendation}</span>
                        </td>
                        <td>{item.score.toFixed(3)}</td>
                        <td>{item.confidence.toFixed(0)}%</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

export default AssetDetailModal;
