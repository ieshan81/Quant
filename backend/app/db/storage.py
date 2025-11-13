"""Simple storage layer for caching data and recommendations."""
import json
import sqlite3
from pathlib import Path
from typing import Optional, Dict, Any, List
from datetime import datetime, timedelta
import pandas as pd
import logging

logger = logging.getLogger(__name__)


class Storage:
    """Simple SQLite-based storage for caching."""
    
    def __init__(self, db_path: str = "data/recommendations.db"):
        """Initialize storage.
        
        Args:
            db_path: Path to SQLite database file
        """
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
    
    def _init_db(self):
        """Initialize database tables."""
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()
        
        # Price data cache
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS price_cache (
                ticker TEXT,
                date TEXT,
                open REAL,
                high REAL,
                low REAL,
                close REAL,
                volume REAL,
                cached_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (ticker, date)
            )
        """)
        
        # Recommendations cache
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS recommendations_cache (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT,
                asset_type TEXT,
                score REAL,
                confidence REAL,
                recommendation TEXT,
                volatility REAL,
                contributing_signals TEXT,
                current_price REAL,
                price_change_pct REAL,
                position_size TEXT,
                sparkline TEXT,
                cached_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Ensure new columns exist when upgrading from previous schema versions
        cursor.execute("PRAGMA table_info(recommendations_cache)")
        existing_columns = {row[1] for row in cursor.fetchall()}
        migrations = [
            ("current_price", "ALTER TABLE recommendations_cache ADD COLUMN current_price REAL"),
            ("price_change_pct", "ALTER TABLE recommendations_cache ADD COLUMN price_change_pct REAL"),
            ("position_size", "ALTER TABLE recommendations_cache ADD COLUMN position_size TEXT"),
            ("sparkline", "ALTER TABLE recommendations_cache ADD COLUMN sparkline TEXT"),
        ]
        for column, statement in migrations:
            if column not in existing_columns:
                try:
                    cursor.execute(statement)
                except Exception as exc:
                    logger.warning("Failed to migrate column %s: %s", column, exc)

        # Cache metadata
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS cache_metadata (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        conn.commit()
        conn.close()
    
    def cache_price_data(self, ticker: str, df: pd.DataFrame):
        """Cache price data for a ticker.
        
        Args:
            ticker: Ticker symbol
            df: DataFrame with columns: date, open, high, low, close, volume
        """
        try:
            conn = sqlite3.connect(str(self.db_path))
            df_cache = df.copy()
            df_cache['ticker'] = ticker
            df_cache['date'] = df_cache.index.astype(str)
            
            # Convert to records
            records = df_cache[['ticker', 'date', 'open', 'high', 'low', 'close', 'volume']].to_dict('records')
            
            cursor = conn.cursor()
            cursor.executemany("""
                INSERT OR REPLACE INTO price_cache 
                (ticker, date, open, high, low, close, volume, cached_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, [
                (r['ticker'], r['date'], r['open'], r['high'], r['low'], r['close'], r['volume'], datetime.now())
                for r in records
            ])
            
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Error caching price data for {ticker}: {e}")
    
    def get_cached_price_data(self, ticker: str, start_date: Optional[str] = None, 
                              end_date: Optional[str] = None, 
                              max_age_hours: int = 24) -> Optional[pd.DataFrame]:
        """Retrieve cached price data.
        
        Args:
            ticker: Ticker symbol
            start_date: Start date filter (YYYY-MM-DD)
            end_date: End date filter (YYYY-MM-DD)
            max_age_hours: Maximum age of cache in hours
            
        Returns:
            DataFrame with price data or None if not found/expired
        """
        try:
            conn = sqlite3.connect(str(self.db_path))
            
            cutoff_time = datetime.now() - timedelta(hours=max_age_hours)
            
            query = """
                SELECT date, open, high, low, close, volume
                FROM price_cache
                WHERE ticker = ? AND cached_at > ?
            """
            params = [ticker, cutoff_time]
            
            if start_date:
                query += " AND date >= ?"
                params.append(start_date)
            if end_date:
                query += " AND date <= ?"
                params.append(end_date)
            
            query += " ORDER BY date"
            
            df = pd.read_sql_query(query, conn, params=params)
            conn.close()
            
            if df.empty:
                return None
            
            df['date'] = pd.to_datetime(df['date'])
            df.set_index('date', inplace=True)
            return df
            
        except Exception as e:
            logger.error(f"Error retrieving cached price data for {ticker}: {e}")
            return None
    
    def cache_recommendations(self, recommendations: List[Dict[str, Any]]):
        """Cache recommendations.
        
        Args:
            recommendations: List of recommendation dictionaries
        """
        try:
            conn = sqlite3.connect(str(self.db_path))
            cursor = conn.cursor()
            
            # Clear old recommendations
            cursor.execute("DELETE FROM recommendations_cache")
            
            # Insert new recommendations
            for rec in recommendations:
                position_payload = rec.get('position_size') if isinstance(rec, dict) else None
                if position_payload:
                    position_json = json.dumps(position_payload)
                else:
                    position_json = None

                cursor.execute("""
                    INSERT INTO recommendations_cache
                    (ticker, asset_type, score, confidence, recommendation, volatility, contributing_signals, current_price, price_change_pct, position_size, sparkline)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    rec['ticker'],
                    rec['asset_type'],
                    rec['score'],
                    rec['confidence'],
                    rec['recommendation'],
                    rec['volatility'],
                    json.dumps(rec['contributing_signals']),
                    rec.get('current_price'),
                    rec.get('price_change_pct'),
                    position_json,
                    json.dumps(rec.get('sparkline')) if rec.get('sparkline') else None,
                ))
            
            # Update last_update timestamp
            cursor.execute("""
                INSERT OR REPLACE INTO cache_metadata (key, value, updated_at)
                VALUES ('last_update', ?, ?)
            """, (datetime.now().isoformat(), datetime.now()))
            
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Error caching recommendations: {e}")
    
    def get_cached_recommendations(self) -> Optional[List[Dict[str, Any]]]:
        """Retrieve cached recommendations.
        
        Returns:
            List of recommendation dictionaries or None
        """
        try:
            conn = sqlite3.connect(str(self.db_path))
            cursor = conn.cursor()
            
            cursor.execute("""
                SELECT ticker, asset_type, score, confidence, recommendation, volatility, contributing_signals, current_price, price_change_pct, position_size, sparkline
                FROM recommendations_cache
                ORDER BY score DESC
            """)
            
            rows = cursor.fetchall()
            conn.close()
            
            if not rows:
                return None
            
            recommendations = []
            for row in rows:
                recommendations.append({
                    'ticker': row[0],
                    'asset_type': row[1],
                    'score': row[2],
                    'confidence': row[3],
                    'recommendation': row[4],
                    'volatility': row[5],
                    'contributing_signals': json.loads(row[6]),
                    'current_price': row[7],
                    'price_change_pct': row[8],
                    'position_size': json.loads(row[9]) if row[9] else None,
                    'sparkline': json.loads(row[10]) if row[10] else None,
                })
            
            return recommendations
            
        except Exception as e:
            logger.error(f"Error retrieving cached recommendations: {e}")
            return None
    
    def get_last_update(self) -> Optional[datetime]:
        """Get timestamp of last recommendations update.
        
        Returns:
            Datetime of last update or None
        """
        try:
            conn = sqlite3.connect(str(self.db_path))
            cursor = conn.cursor()
            
            cursor.execute("""
                SELECT value FROM cache_metadata WHERE key = 'last_update'
            """)
            
            row = cursor.fetchone()
            conn.close()
            
            if row:
                return datetime.fromisoformat(row[0])
            return None
            
        except Exception as e:
            logger.error(f"Error retrieving last update: {e}")
            return None

