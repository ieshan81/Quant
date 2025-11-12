import React, { useState, useEffect, useCallback } from 'react';
import { getAssetDetail } from '../api';
import './AssetDetailModal.css';

function AssetDetailModal({ ticker, onClose }) {
  const [assetData, setAssetData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const loadAssetData = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await getAssetDetail(ticker);
      setAssetData(data);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }, [ticker]);

  useEffect(() => {
    if (ticker) {
      loadAssetData();
    }
  }, [ticker, loadAssetData]);

  const formatPrice = (price) => {
    if (!price) return 'N/A';
    return `$${price.toFixed(2)}`;
  };

  const formatDate = (dateStr) => {
    return new Date(dateStr).toLocaleDateString();
  };

  if (!ticker) return null;

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal-content" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <h2>{ticker} - Details</h2>
          <button className="close-button" onClick={onClose}>×</button>
        </div>

        <div className="modal-body">
          {loading && <div className="loading">Loading asset data...</div>}
          {error && <div className="error">Error: {error}</div>}
          
          {assetData && (
            <>
              <div className="asset-summary">
                <div className="summary-item">
                  <label>Current Price</label>
                  <span className="price-large">{formatPrice(assetData.current_price)}</span>
                </div>
                <div className="summary-item">
                  <label>Asset Type</label>
                  <span>{assetData.asset_type}</span>
                </div>
                <div className="summary-item">
                  <label>Volatility</label>
                  <span>{(assetData.metadata?.volatility || 0).toFixed(4)}</span>
                </div>
              </div>

              <div className="strategy-signals">
                <h3>Strategy Signals</h3>
                <div className="signals-grid">
                  {Object.entries(assetData.strategy_signals || {}).map(([strategy, score]) => (
                    <div key={strategy} className="signal-item">
                      <span className="signal-name">{strategy.replace('_', ' ').toUpperCase()}</span>
                      <span className={`signal-score ${score >= 0 ? 'positive' : 'negative'}`}>
                        {score.toFixed(3)}
                      </span>
                    </div>
                  ))}
                </div>
              </div>

              <div className="price-history">
                <h3>Price History (Last 60 Days)</h3>
                <div className="history-chart">
                  {assetData.price_history && assetData.price_history.length > 0 ? (
                    <SimpleChart data={assetData.price_history} />
                  ) : (
                    <div className="no-chart-data">No chart data available</div>
                  )}
                </div>
              </div>

              {assetData.recent_recommendations && assetData.recent_recommendations.length > 0 && (
                <div className="recent-recommendations">
                  <h3>Recent Recommendations</h3>
                  <table className="recommendations-table">
                    <thead>
                      <tr>
                        <th>Date</th>
                        <th>Recommendation</th>
                        <th>Score</th>
                        <th>Confidence</th>
                      </tr>
                    </thead>
                    <tbody>
                      {assetData.recent_recommendations.map((rec, idx) => (
                        <tr key={idx}>
                          <td>{formatDate(rec.date)}</td>
                          <td>
                            <span className={`badge badge-${rec.recommendation.toLowerCase()}`}>
                              {rec.recommendation}
                            </span>
                          </td>
                          <td>{rec.score.toFixed(3)}</td>
                          <td>{rec.confidence.toFixed(0)}%</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}

// Simple line chart component
function SimpleChart({ data }) {
  if (!data || data.length === 0) return null;

  const prices = data.map(d => d.close);
  const minPrice = Math.min(...prices);
  const maxPrice = Math.max(...prices);
  const range = maxPrice - minPrice || 1;

  const points = data.map((d, idx) => {
    const x = (idx / (data.length - 1)) * 100;
    const y = 100 - ((d.close - minPrice) / range) * 100;
    return `${x},${y}`;
  }).join(' ');

  const currentPrice = prices[prices.length - 1];
  const firstPrice = prices[0];
  const change = ((currentPrice - firstPrice) / firstPrice) * 100;
  const isPositive = change >= 0;

  return (
    <div className="simple-chart">
      <div className="chart-header">
        <span className="chart-price">{formatPrice(currentPrice)}</span>
        <span className={`chart-change ${isPositive ? 'positive' : 'negative'}`}>
          {isPositive ? '+' : ''}{change.toFixed(2)}%
        </span>
      </div>
      <svg viewBox="0 0 100 100" preserveAspectRatio="none" className="chart-svg">
        <polyline
          points={points}
          fill="none"
          stroke={isPositive ? '#28a745' : '#dc3545'}
          strokeWidth="0.5"
        />
      </svg>
    </div>
  );
}

function formatPrice(price) {
  return `$${price.toFixed(2)}`;
}

export default AssetDetailModal;

