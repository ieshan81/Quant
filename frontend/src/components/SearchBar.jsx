import React, { useState, useEffect, useCallback } from 'react';
import { searchTicker } from '../api';
import styles from './SearchBar.module.css';

const defaultUniverse = ['AAPL', 'MSFT', 'NVDA', 'TSLA', 'AMZN', 'META', 'BTC-USD', 'ETH-USD', 'EURUSD=X', 'GBPUSD=X'];

function SearchBar({ onSelect }) {
  const [query, setQuery] = useState('');
  const [suggestions, setSuggestions] = useState(defaultUniverse);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  const performSearch = useCallback(async () => {
    if (!query || query.length < 2) {
      setSuggestions(defaultUniverse);
      setError('');
      return;
    }

    setLoading(true);
    setError('');
    try {
      const result = await searchTicker(query);
      if (result?.symbol) {
        const label = result.name ? `${result.symbol} · ${result.name}` : result.symbol;
        setSuggestions([label, ...defaultUniverse.filter((item) => item !== result.symbol)]);
      }
    } catch (err) {
      console.warn('Search failed', err);
      setError('Symbol not found');
    } finally {
      setLoading(false);
    }
  }, [query]);

  useEffect(() => {
    const timeout = setTimeout(() => {
      performSearch();
    }, 250);

    return () => clearTimeout(timeout);
  }, [performSearch]);

  const handleSelect = (value) => {
    if (!value) return;
    const symbol = value.split(' · ')[0];
    if (onSelect) {
      onSelect(symbol);
    }
    setQuery(symbol);
    setSuggestions((prev) => [value, ...prev.filter((item) => item !== value)]);
  };

  return (
    <div className={styles.wrapper}>
      <div className={styles.inputShell}>
        <span className={styles.icon}>🔍</span>
        <input
          className={styles.input}
          placeholder="Search any ticker…"
          value={query}
          onChange={(event) => setQuery(event.target.value.toUpperCase())}
        />
        {loading && <span className={styles.spinner} />}
      </div>
      {error && <div className={styles.error}>{error}</div>}
      <div className={styles.suggestionGrid}>
        {suggestions.map((item) => (
          <button key={item} type="button" className={styles.suggestion} onClick={() => handleSelect(item)}>
            {item}
          </button>
        ))}
      </div>
    </div>
  );
}

export default SearchBar;
