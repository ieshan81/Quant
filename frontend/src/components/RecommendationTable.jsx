import React, { useMemo, useState } from 'react';
import clsx from 'clsx';
import {
  ResponsiveContainer,
  AreaChart,
  Area,
  Tooltip,
  YAxis,
  XAxis,
} from 'recharts';
import styles from './RecommendationTable.module.css';

const recommendationFilters = [
  { label: 'All', value: 'all' },
  { label: 'Buy', value: 'BUY' },
  { label: 'Sell', value: 'SELL' },
  { label: 'Hold', value: 'HOLD' },
];

function RecommendationTable({ recommendations, liveQuotes, assetType, loading, onSelectTicker }) {
  const [filter, setFilter] = useState('all');

  const filtered = useMemo(() => {
    if (filter === 'all') return recommendations;
    return recommendations.filter((rec) => rec.recommendation === filter);
  }, [recommendations, filter]);

  const rows = useMemo(() => {
    return filtered.map((rec, index) => {
      const live = liveQuotes?.[rec.ticker];
      const price = live?.price ?? rec.current_price;
      const change = live?.change_pct ?? rec.price_change_pct;
      const bid = live?.bid;
      const ask = live?.ask;

      return {
        ...rec,
        rank: index + 1,
        price,
        change,
        bid,
        ask,
      };
    });
  }, [filtered, liveQuotes]);

  const formatPrice = (value) => {
    if (value === null || value === undefined) return '—';
    if (value > 1000) {
      return `$${value.toLocaleString(undefined, { maximumFractionDigits: 2 })}`;
    }
    return `$${value.toFixed(2)}`;
  };

  const formatPercent = (value) => {
    if (value === null || value === undefined) return '—';
    const sign = value >= 0 ? '+' : '';
    return `${sign}${value.toFixed(2)}%`;
  };

  const renderSparkline = (series = [], id) => {
    if (!series || series.length < 2) {
      return <div className={styles.sparklineEmpty}>—</div>;
    }
    const data = series.map((value, index) => ({ index, value }));
    const positive = data[data.length - 1].value >= data[0].value;
    return (
      <ResponsiveContainer width="100%" height={40}>
        <AreaChart data={data}>
          <defs>
            <linearGradient id={`spark-${id}`} x1="0" y1="0" x2="0" y2="1">
              <stop offset="5%" stopColor={positive ? '#00ff95' : '#ff5e5b'} stopOpacity={0.6} />
              <stop offset="95%" stopColor={positive ? '#00ff95' : '#ff5e5b'} stopOpacity={0} />
            </linearGradient>
          </defs>
          <XAxis dataKey="index" hide />
          <YAxis domain={['dataMin', 'dataMax']} hide />
          <Tooltip
            formatter={(value) => `$${Number(value).toFixed(2)}`}
            labelFormatter={() => ''}
            cursor={{ stroke: 'rgba(255,255,255,0.1)' }}
            contentStyle={{ background: '#161b22', borderRadius: 8, border: '1px solid #30363d' }}
          />
          <Area
            type="monotone"
            dataKey="value"
            stroke={positive ? '#00ff95' : '#ff5e5b'}
            strokeWidth={2}
            fill={`url(#spark-${id})`}
          />
        </AreaChart>
      </ResponsiveContainer>
    );
  };

  return (
    <div className={styles.wrapper}>
      <div className={styles.toolbar}>
        <div className={styles.filterGroup}>
          {recommendationFilters.map((item) => (
            <button
              key={item.value}
              type="button"
              className={clsx(styles.filterButton, filter === item.value && styles.filterActive)}
              onClick={() => setFilter(item.value)}
            >
              {item.label}
              <span className={styles.filterBadge}>
                {item.value === 'all'
                  ? recommendations.length
                  : recommendations.filter((rec) => rec.recommendation === item.value).length}
              </span>
            </button>
          ))}
        </div>
        <div className={styles.assetLabel}>Asset universe: {assetType.toUpperCase()}</div>
      </div>

      <div className={styles.tableScroll}>
        <table className={styles.table}>
          <thead>
            <tr>
              <th>#</th>
              <th>Ticker</th>
              <th>Signal</th>
              <th>Score</th>
              <th>Confidence</th>
              <th>Price</th>
              <th>Change</th>
              <th>Position Size</th>
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <tr>
                <td colSpan={8} className={styles.loadingCell}>
                  Loading market intelligence…
                </td>
              </tr>
            ) : rows.length === 0 ? (
              <tr>
                <td colSpan={8} className={styles.emptyCell}>
                  No recommendations available. Adjust filters or refresh.
                </td>
              </tr>
            ) : (
              rows.map((row) => (
                <tr key={row.ticker} className={styles.row} onClick={() => onSelectTicker?.(row.ticker)}>
                  <td>{row.rank}</td>
                  <td>
                    <div className={styles.tickerCell}>
                      <div className={styles.tickerSymbol}>{row.ticker}</div>
                      <div className={styles.sparkline}>{renderSparkline(row.sparkline, row.ticker)}</div>
                    </div>
                  </td>
                  <td>
                    <span className={clsx(styles.badge, styles[`badge${row.recommendation}`])}>{row.recommendation}</span>
                  </td>
                  <td>
                    <span className={clsx(row.score >= 0 ? styles.positive : styles.negative)}>
                      {row.score.toFixed(3)}
                    </span>
                  </td>
                  <td>
                    <div className={styles.confidenceBar}>
                      <div className={styles.confidenceFill} style={{ width: `${row.confidence}%` }} />
                      <span className={styles.confidenceLabel}>{row.confidence.toFixed(0)}%</span>
                    </div>
                  </td>
                  <td>
                    <div className={styles.priceCell}>
                      <span>{formatPrice(row.price)}</span>
                      {row.bid && row.ask && (
                        <small className={styles.quoteMeta}>
                          B {formatPrice(row.bid)} · A {formatPrice(row.ask)}
                        </small>
                      )}
                    </div>
                  </td>
                  <td>
                    <span className={clsx(row.change >= 0 ? styles.positive : styles.negative)}>
                      {formatPercent(row.change)}
                    </span>
                  </td>
                  <td>
                    {row.position_size ? (
                      <div className={styles.positionCell}>
                        <span className={styles.positionHeadline}>
                          {row.recommendation === 'BUY' ? 'Buy' : row.recommendation === 'SELL' ? 'Sell' : 'Hold'}
                          : {row.position_size.recommended_size.toFixed(2)}
                        </span>
                        <small className={styles.positionMeta}>
                          SL {formatPrice(row.position_size.stop_loss)} · TP {formatPrice(row.position_size.take_profit)}
                        </small>
                      </div>
                    ) : (
                      <span className={styles.muted}>—</span>
                    )}
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export default RecommendationTable;
