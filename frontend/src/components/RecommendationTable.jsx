import React, { useState } from 'react';
import AssetDetailModal from './AssetDetailModal';
import './RecommendationTable.css';

function RecommendationTable({ recommendations, assetType, onAssetClick }) {
  const [selectedAsset, setSelectedAsset] = useState(null);
  const [filter, setFilter] = useState('all'); // all, buy, sell, hold

  const filteredRecommendations = recommendations.filter(rec => {
    if (filter === 'all') return true;
    return rec.recommendation === filter.toUpperCase();
  });

  const getRecommendationClass = (rec) => {
    if (rec.recommendation === 'BUY') return 'recommendation-buy';
    if (rec.recommendation === 'SELL') return 'recommendation-sell';
    return 'recommendation-hold';
  };

  const formatPrice = (price) => {
    if (!price) return 'N/A';
    return `$${price.toFixed(2)}`;
  };

  const formatPercent = (value) => {
    if (value === null || value === undefined) return 'N/A';
    const sign = value >= 0 ? '+' : '';
    return `${sign}${value.toFixed(2)}%`;
  };

  const handleRowClick = (ticker) => {
    setSelectedAsset(ticker);
    if (onAssetClick) {
      onAssetClick(ticker);
    }
  };

  return (
    <div className="recommendation-table-container">
      <div className="table-controls">
        <div className="filter-buttons">
          <button
            className={filter === 'all' ? 'active' : ''}
            onClick={() => setFilter('all')}
          >
            All ({recommendations.length})
          </button>
          <button
            className={filter === 'buy' ? 'active' : ''}
            onClick={() => setFilter('buy')}
          >
            Buy ({recommendations.filter(r => r.recommendation === 'BUY').length})
          </button>
          <button
            className={filter === 'sell' ? 'active' : ''}
            onClick={() => setFilter('sell')}
          >
            Sell ({recommendations.filter(r => r.recommendation === 'SELL').length})
          </button>
          <button
            className={filter === 'hold' ? 'active' : ''}
            onClick={() => setFilter('hold')}
          >
            Hold ({recommendations.filter(r => r.recommendation === 'HOLD').length})
          </button>
        </div>
      </div>

      <div className="table-wrapper">
        <table className="recommendation-table">
          <thead>
            <tr>
              <th>Rank</th>
              <th>Ticker</th>
              <th>Type</th>
              <th>Recommendation</th>
              <th>Score</th>
              <th>Confidence</th>
              <th>Price</th>
              <th>Change</th>
            </tr>
          </thead>
          <tbody>
            {filteredRecommendations.length === 0 ? (
              <tr>
                <td colSpan="8" className="no-data">No recommendations found</td>
              </tr>
            ) : (
              filteredRecommendations.map((rec, index) => (
                <tr
                  key={rec.ticker}
                  className={`table-row ${getRecommendationClass(rec)}`}
                  onClick={() => handleRowClick(rec.ticker)}
                >
                  <td className="rank-cell">{index + 1}</td>
                  <td className="ticker-cell">
                    <strong>{rec.ticker}</strong>
                  </td>
                  <td className="type-cell">{rec.asset_type}</td>
                  <td className="recommendation-cell">
                    <span className={`badge badge-${rec.recommendation.toLowerCase()}`}>
                      {rec.recommendation}
                    </span>
                  </td>
                  <td className="score-cell">
                    <span className={rec.score >= 0 ? 'positive' : 'negative'}>
                      {rec.score.toFixed(3)}
                    </span>
                  </td>
                  <td className="confidence-cell">
                    <div className="confidence-bar-container">
                      <div
                        className="confidence-bar"
                        style={{ width: `${rec.confidence}%` }}
                      />
                      <span className="confidence-text">{rec.confidence.toFixed(0)}%</span>
                    </div>
                  </td>
                  <td className="price-cell">{formatPrice(rec.current_price)}</td>
                  <td className="change-cell">
                    <span className={rec.price_change_pct >= 0 ? 'positive' : 'negative'}>
                      {formatPercent(rec.price_change_pct)}
                    </span>
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>

      {selectedAsset && (
        <AssetDetailModal
          ticker={selectedAsset}
          onClose={() => setSelectedAsset(null)}
        />
      )}
    </div>
  );
}

export default RecommendationTable;

