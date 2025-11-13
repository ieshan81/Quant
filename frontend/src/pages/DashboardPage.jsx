import React, { useState, useEffect, useCallback, useMemo } from 'react';
import clsx from 'clsx';
import { formatDistanceToNow } from 'date-fns';

import {
  getRecommendations,
  getHealth,
  getLivePrice,
} from '../api';
import SearchBar from '../components/SearchBar';
import RecommendationTable from '../components/RecommendationTable';
import AssetDetailModal from '../components/AssetDetailModal';
import styles from './DashboardPage.module.css';

const LIVE_REFRESH_MS = 8000;
const RECOMMENDATION_REFRESH_MS = 60000;

const assetFilters = [
  { label: 'Stocks', value: 'stocks' },
  { label: 'Crypto', value: 'crypto' },
  { label: 'Forex', value: 'forex' },
];

function DashboardPage() {
  const [assetType, setAssetType] = useState('stocks');
  const [recommendations, setRecommendations] = useState([]);
  const [health, setHealth] = useState(null);
  const [lastUpdate, setLastUpdate] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [selectedTicker, setSelectedTicker] = useState(null);
  const [liveQuotes, setLiveQuotes] = useState({});

  const loadHealth = useCallback(async () => {
    try {
      const data = await getHealth();
      setHealth(data);
      if (data.last_update) {
        setLastUpdate(data.last_update);
      }
    } catch (err) {
      console.error('Health check failed', err);
    }
  }, []);

  const loadRecommendations = useCallback(async (showLoader = true) => {
    if (showLoader) setLoading(true);
    setError('');
    try {
      const response = await getRecommendations(assetType, 50);
      setRecommendations(response.recommendations || []);
      if (response.last_update) {
        setLastUpdate(response.last_update);
      }
    } catch (err) {
      console.error('Failed to load recommendations', err);
      setError(err.message || 'Unable to load recommendations.');
    } finally {
      if (showLoader) setLoading(false);
    }
  }, [assetType]);

  useEffect(() => {
    loadHealth();
    loadRecommendations();
  }, [loadHealth, loadRecommendations]);

  useEffect(() => {
    const recInterval = setInterval(() => {
      loadRecommendations(false);
    }, RECOMMENDATION_REFRESH_MS);
    return () => clearInterval(recInterval);
  }, [loadRecommendations]);

  const fetchLiveQuotes = useCallback(async () => {
    const universe = recommendations.slice(0, 12).map((rec) => rec.ticker);
    if (universe.length === 0) return;

    const updates = {};
    await Promise.all(
      universe.map(async (ticker) => {
        try {
          const quote = await getLivePrice(ticker);
          updates[ticker] = quote;
        } catch (err) {
          console.warn('Live quote failed for', ticker, err);
        }
      }),
    );
    if (Object.keys(updates).length > 0) {
      setLiveQuotes((prev) => ({ ...prev, ...updates }));
    }
  }, [recommendations]);

  useEffect(() => {
    fetchLiveQuotes();
    const liveInterval = setInterval(fetchLiveQuotes, LIVE_REFRESH_MS);
    return () => clearInterval(liveInterval);
  }, [fetchLiveQuotes]);

  const summary = useMemo(() => {
    const total = recommendations.length;
    const buy = recommendations.filter((r) => r.recommendation === 'BUY').length;
    const sell = recommendations.filter((r) => r.recommendation === 'SELL').length;
    const hold = total - buy - sell;
    const avgConfidence =
      total > 0 ? recommendations.reduce((acc, rec) => acc + rec.confidence, 0) / total : 0;

    return { total, buy, sell, hold, avgConfidence };
  }, [recommendations]);

  const uptime = useMemo(() => {
    if (!health?.uptime_seconds) return null;
    const minutes = Math.floor(health.uptime_seconds / 60);
    const hours = Math.floor(minutes / 60);
    return `${hours}h ${minutes % 60}m`;
  }, [health]);

  const handleAssetSelect = useCallback((ticker) => {
    setSelectedTicker(ticker);
  }, []);

  const handleSearchSelect = useCallback((symbol) => {
    if (symbol) {
      setSelectedTicker(symbol);
    }
  }, []);

  const lastSyncLabel = useMemo(() => {
    if (!lastUpdate) return 'Unknown';
    try {
      return formatDistanceToNow(new Date(lastUpdate), { addSuffix: true });
    } catch (e) {
      return lastUpdate;
    }
  }, [lastUpdate]);

  return (
    <div className={styles.container}>
      <div className={styles.topRow}>
        <SearchBar onSelect={handleSearchSelect} />
        <div className={styles.healthCard}>
          <div className={styles.healthHeader}>
            <span className={clsx(styles.statusDot, health?.status === 'healthy' ? styles.statusOk : styles.statusWarn)} />
            <span className={styles.healthTitle}>Backend Status</span>
          </div>
          <div className={styles.healthBody}>
            <div className={styles.healthMetric}>{health?.status || 'unknown'}</div>
            <div className={styles.healthMeta}>Uptime {uptime || '—'}</div>
            <div className={styles.healthMeta}>Last sync {lastSyncLabel}</div>
          </div>
        </div>
      </div>

      {error && (
        <div className={styles.errorBanner}>
          <div>
            <strong>API Error:</strong> {error}
          </div>
          <button type="button" className={styles.reloadButton} onClick={() => loadRecommendations()}>
            Retry
          </button>
        </div>
      )}

      <div className={styles.controlsRow}>
        <div className={styles.assetToggle}>
          {assetFilters.map((option) => (
            <button
              key={option.value}
              type="button"
              onClick={() => setAssetType(option.value)}
              className={clsx(styles.assetButton, assetType === option.value && styles.assetButtonActive)}
            >
              {option.label}
            </button>
          ))}
        </div>
        <div className={styles.refreshMeta}>
          <button type="button" className={styles.refreshButton} onClick={() => { loadRecommendations(); loadHealth(); }}>
            Refresh
          </button>
          <span className={styles.refreshHint}>Auto-refreshing live quotes every {LIVE_REFRESH_MS / 1000}s</span>
        </div>
      </div>

      <div className={styles.metricsRow}>
        <div className={styles.metricCard}>
          <span className={styles.metricLabel}>Signals</span>
          <span className={styles.metricValue}>{summary.total}</span>
        </div>
        <div className={styles.metricCard}>
          <span className={styles.metricLabel}>Buy</span>
          <span className={clsx(styles.metricValue, styles.buy)}>{summary.buy}</span>
        </div>
        <div className={styles.metricCard}>
          <span className={styles.metricLabel}>Sell</span>
          <span className={clsx(styles.metricValue, styles.sell)}>{summary.sell}</span>
        </div>
        <div className={styles.metricCard}>
          <span className={styles.metricLabel}>Hold</span>
          <span className={styles.metricValue}>{summary.hold}</span>
        </div>
        <div className={styles.metricCard}>
          <span className={styles.metricLabel}>Avg Confidence</span>
          <span className={styles.metricValue}>{summary.avgConfidence.toFixed(1)}%</span>
        </div>
      </div>

      <div className={styles.tableCard}>
        <RecommendationTable
          assetType={assetType}
          loading={loading}
          recommendations={recommendations}
          liveQuotes={liveQuotes}
          onSelectTicker={handleAssetSelect}
        />
      </div>

      <AssetDetailModal ticker={selectedTicker} onClose={() => setSelectedTicker(null)} />
    </div>
  );
}

export default DashboardPage;
