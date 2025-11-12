import React, { useState, useEffect } from 'react';
import Header from './components/Header';
import RecommendationTable from './components/RecommendationTable';
import { getRecommendations, getHealth } from './api';
import './styles.css';

function App() {
  const [recommendations, setRecommendations] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [assetType, setAssetType] = useState('stocks');
  const [lastUpdate, setLastUpdate] = useState(null);
  const [healthStatus, setHealthStatus] = useState(null);

  useEffect(() => {
    checkHealth();
    loadRecommendations();
  }, [assetType]);

  const checkHealth = async () => {
    try {
      const health = await getHealth();
      setHealthStatus(health);
      if (health.last_update) {
        setLastUpdate(health.last_update);
      }
    } catch (err) {
      console.error('Health check failed:', err);
    }
  };

  const loadRecommendations = async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await getRecommendations(assetType, 50);
      setRecommendations(data.recommendations || []);
      if (data.last_update) {
        setLastUpdate(data.last_update);
      }
    } catch (err) {
      setError(err.message || 'Failed to load recommendations. Make sure the backend is running.');
      console.error('Error loading recommendations:', err);
    } finally {
      setLoading(false);
    }
  };

  const handleRefresh = () => {
    loadRecommendations();
    checkHealth();
  };

  const handleAssetClick = (ticker) => {
    // Handled by RecommendationTable component
    console.log('Asset clicked:', ticker);
  };

  return (
    <div className="app">
      <Header lastUpdate={lastUpdate} onRefresh={handleRefresh} />
      
      <main className="main-content">
        <div className="container">
          {error && (
            <div className="error-banner">
              <strong>Error:</strong> {error}
              <br />
              <small>Make sure the backend API is running at {process.env.REACT_APP_API_URL || 'http://localhost:8000'}</small>
            </div>
          )}

          {healthStatus && (
            <div className="status-banner">
              <span className="status-indicator">●</span>
              <span>Backend Status: {healthStatus.status}</span>
              {healthStatus.uptime_seconds && (
                <span className="uptime">
                  Uptime: {Math.floor(healthStatus.uptime_seconds / 3600)}h {Math.floor((healthStatus.uptime_seconds % 3600) / 60)}m
                </span>
              )}
            </div>
          )}

          <div className="controls">
            <div className="asset-type-selector">
              <label>Asset Type:</label>
              <select value={assetType} onChange={(e) => setAssetType(e.target.value)}>
                <option value="stocks">Stocks</option>
                <option value="crypto">Crypto</option>
                <option value="forex">Forex</option>
              </select>
            </div>
          </div>

          {loading ? (
            <div className="loading-container">
              <div className="spinner"></div>
              <p>Loading recommendations...</p>
            </div>
          ) : (
            <RecommendationTable
              recommendations={recommendations}
              assetType={assetType}
              onAssetClick={handleAssetClick}
            />
          )}

          {recommendations.length > 0 && (
            <div className="summary-stats">
              <div className="stat-card">
                <div className="stat-label">Total Recommendations</div>
                <div className="stat-value">{recommendations.length}</div>
              </div>
              <div className="stat-card">
                <div className="stat-label">Buy Signals</div>
                <div className="stat-value buy">{recommendations.filter(r => r.recommendation === 'BUY').length}</div>
              </div>
              <div className="stat-card">
                <div className="stat-label">Sell Signals</div>
                <div className="stat-value sell">{recommendations.filter(r => r.recommendation === 'SELL').length}</div>
              </div>
              <div className="stat-card">
                <div className="stat-label">Hold Signals</div>
                <div className="stat-value hold">{recommendations.filter(r => r.recommendation === 'HOLD').length}</div>
              </div>
            </div>
          )}
        </div>
      </main>

      <footer className="footer">
        <p>Quantitative Trading Recommendation System v1.0.0</p>
        <p>For informational purposes only. Not financial advice.</p>
      </footer>
    </div>
  );
}

export default App;

