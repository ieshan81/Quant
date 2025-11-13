import React, { useState, useEffect, useCallback } from 'react';
import Header from './components/Header';
import RecommendationTable from './components/RecommendationTable';
import { getRecommendations, getHealth, API_BASE_URL } from './api';
import './styles.css';

function App() {
  const [recommendations, setRecommendations] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [assetType, setAssetType] = useState('stocks');
  const [lastUpdate, setLastUpdate] = useState(null);
  const [healthStatus, setHealthStatus] = useState(null);

  const checkHealth = useCallback(async () => {
    try {
      const health = await getHealth();
      setHealthStatus(health);
      if (health.last_update) {
        setLastUpdate(health.last_update);
      }
    } catch (err) {
      console.error('Health check failed:', err);
    }
  }, []);

  const loadRecommendations = useCallback(async () => {
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
  }, [assetType]);

  useEffect(() => {
    checkHealth();
    loadRecommendations();
  }, [checkHealth, loadRecommendations]);

  const handleRefresh = () => {
    loadRecommendations();
    checkHealth();
  };

  const handleAssetClick = (ticker) => {
    // Handled by RecommendationTable component
    console.log('Asset clicked:', ticker);
  };

function App() {
  return (
    <div className="app">
      <Header lastUpdate={lastUpdate} onRefresh={handleRefresh} />
      
      <main className="main-content">
        <div className="container">
          {error && (
            <div className="error-banner">
              <strong>Error:</strong> {error}
              <br />
              <small>Make sure the backend API is reachable at {process.env.REACT_APP_API_URL || API_BASE_URL}</small>
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
          <div className={styles.environment}>
            <span className={styles.envLabel}>API</span>
            <span className={styles.envValue}>{API_BASE_URL}</span>
          </div>
        </header>

        <nav className={styles.navbar}>
          <NavLink
            to="/"
            end
            className={({ isActive }) => clsx(styles.navLink, isActive && styles.navLinkActive)}
          >
            Dashboard
          </NavLink>
          <NavLink
            to="/analytics"
            className={({ isActive }) => clsx(styles.navLink, isActive && styles.navLinkActive)}
          >
            Analytics
          </NavLink>
        </nav>

        <main className={styles.mainContent}>
          <Routes>
            <Route path="/" element={<DashboardPage />} />
            <Route path="/analytics" element={<AnalyticsPage />} />
          </Routes>
        </main>

        <footer className={styles.footer}>
          <div>© {new Date().getFullYear()} Quant Platform. All rights reserved.</div>
          <div className={styles.disclaimer}>Trade responsibly. Educational purposes only.</div>
        </footer>
      </div>
    </Router>
  );
}

export default App;
