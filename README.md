# Quantitative Trading Recommendation System

A complete, production-ready quantitative trading recommendation system that generates ranked buy/sell/hold recommendations with confidence scores. **This system does NOT execute trades** - it only provides recommendations for analysis.

## Features

- **Multiple Trading Strategies**: Moving Average Crossover, RSI Mean Reversion, Multi-Factor, and ML-based strategies
- **Intelligent Aggregation**: Weighted combination of strategy signals with volatility adjustment
- **Confidence Scoring**: 0-100 confidence scores based on signal strength and strategy agreement
- **Backtesting Engine**: Historical performance evaluation with Sharpe ratio, max drawdown, and win rate
- **Modern UI**: React-based frontend with real-time recommendations and detailed asset views
- **Caching**: SQLite-based caching to minimize API calls and improve performance
- **RESTful API**: FastAPI backend with comprehensive endpoints

## Project Structure

```
quant-recommender/
├── backend/
│   ├── app/
│   │   ├── main.py              # FastAPI app entry
│   │   ├── api/
│   │   │   └── endpoints.py     # API endpoints
│   │   ├── services/
│   │   │   ├── data_manager.py  # Data fetching & caching
│   │   │   ├── strategies.py    # Trading strategies
│   │   │   ├── recommender.py   # Recommendation engine
│   │   │   └── backtester.py    # Backtesting engine
│   │   ├── models/
│   │   │   └── schemas.py       # Pydantic schemas
│   │   ├── db/
│   │   │   └── storage.py       # SQLite storage
│   │   └── utils/
│   │       ├── indicators.py    # Technical indicators
│   │       └── logging_config.py
│   ├── requirements.txt
│   ├── Dockerfile
│   └── Procfile
├── frontend/
│   ├── src/
│   │   ├── App.jsx
│   │   ├── api.js
│   │   ├── components/
│   │   │   ├── RecommendationTable.jsx
│   │   │   ├── AssetDetailModal.jsx
│   │   │   └── Header.jsx
│   │   └── styles.css
│   ├── package.json
│   └── netlify.toml
├── tests/
│   ├── test_strategies.py
│   └── test_recommender.py
├── example_data/
│   ├── sample_prices.csv
│   └── sample_recommendations.json
└── README.md
```

## Quick Start

### Prerequisites

- Python 3.11+
- Node.js 18+
- npm or yarn

### Backend Setup

1. **Navigate to backend directory:**
   ```bash
   cd backend
   ```

2. **Create virtual environment:**
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

3. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

4. **Set up environment variables:**
   ```bash
   cp ../env.example .env
   # Edit .env with your API keys (optional for basic usage)
   ```

5. **Run the backend:**
   ```bash
   uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
   ```

   The API will be available at `http://localhost:8000`
   - API docs: `http://localhost:8000/docs`
   - Health check: `http://localhost:8000/api/v1/health`

### Frontend Setup

1. **Navigate to frontend directory:**
   ```bash
   cd frontend
   ```

2. **Install dependencies:**
   ```bash
   npm install
   ```

3. **Set environment variable (optional):**
   Create `.env` file:
   ```
   REACT_APP_API_URL=http://localhost:8000/api/v1
   ```

4. **Run the frontend:**
   ```bash
   npm start
   ```

   The app will open at `http://localhost:3000`

## Running Tests

```bash
# From project root
cd backend
python -m pytest ../tests/ -v
```

Or using unittest:
```bash
python -m unittest discover -s ../tests -p "test_*.py"
```

## API Endpoints

### Health Check
```http
GET /api/v1/health
```

### Get Recommendations
```http
GET /api/v1/recommendations?asset_type=stocks&limit=50&tickers=AAPL,MSFT
```

**Response:**
```json
{
  "recommendations": [
    {
      "ticker": "AAPL",
      "asset_type": "stocks",
      "score": 1.245,
      "confidence": 78.5,
      "recommendation": "BUY",
      "volatility": 0.0234,
      "contributing_signals": {
        "ma_crossover": 1.12,
        "rsi_mean_reversion": 0.85,
        "multi_factor": 1.45,
        "ml_strategy": 0.32
      },
      "current_price": 205.80,
      "price_change_pct": 2.34
    }
  ],
  "last_update": "2024-03-15T10:30:00",
  "total_count": 1
}
```

### Get Asset Details
```http
GET /api/v1/asset/{ticker}
```

### Run Backtest
```http
POST /api/v1/backtest
Content-Type: application/json

{
  "tickers": ["AAPL", "MSFT", "GOOGL"],
  "strategy_set": ["ma_crossover", "rsi_mean_reversion"],
  "start_date": "2023-01-01",
  "end_date": "2024-01-01",
  "rebalance_period": 5,
  "top_n": 10,
  "initial_capital": 100000,
  "commission": 0.001,
  "slippage": 0.0005
}
```

### Get Strategies
```http
GET /api/v1/strategies
```

## Example Recommendations Output

See `example_data/sample_recommendations.json` for a sample output:

```json
[
  {
    "ticker": "AAPL",
    "asset_type": "stocks",
    "score": 1.245,
    "confidence": 78.5,
    "recommendation": "BUY",
    "volatility": 0.0234,
    "contributing_signals": {
      "ma_crossover": 1.12,
      "rsi_mean_reversion": 0.85,
      "multi_factor": 1.45,
      "ml_strategy": 0.32
    },
    "current_price": 205.80,
    "price_change_pct": 2.34
  }
]
```

## Trading Strategies

### 1. Moving Average Crossover
- **Description**: Generates buy signal when short MA (50-day) crosses above long MA (200-day)
- **Signal**: Normalized difference between short and long MA
- **Default Weight**: 1.0

### 2. RSI Mean Reversion
- **Description**: Buys when RSI is oversold (<30), sells when overbought (>70)
- **Signal**: Centered RSI mapped to z-score
- **Default Weight**: 1.0

### 3. Multi-Factor Strategy
- **Description**: Combines momentum (6-month return) and value (P/E ratio)
- **Signal**: Weighted combination of momentum (70%) and value (30%)
- **Default Weight**: 1.0

### 4. ML Strategy
- **Description**: Random Forest classifier on technical features
- **Signal**: Probability-based prediction
- **Default Weight**: 0.5 (placeholder - requires training)

## Recommendation Logic

1. **Signal Calculation**: Each strategy calculates a raw signal score
2. **Normalization**: Signals are normalized to z-scores using historical distribution
3. **Weighted Aggregation**: Signals are combined using configurable weights
4. **Volatility Adjustment**: Final score is adjusted by recent volatility
5. **Threshold Mapping**:
   - Score ≥ 0.5 → **BUY**
   - Score ≤ -0.5 → **SELL**
   - Otherwise → **HOLD**
6. **Confidence Calculation**: Based on signal strength, strategy agreement, and volatility

## Deployment

### Backend Deployment (Render/Railway)

#### Render

1. **Create a new Web Service** on Render
2. **Connect your repository**
3. **Build settings:**
   - Build Command: `cd backend && pip install -r requirements.txt`
   - Start Command: `cd backend && uvicorn app.main:app --host 0.0.0.0 --port $PORT`
4. **Environment Variables:**
   - Add variables from `env.example`
   - Set `PORT` (Render provides this automatically)
   - Set `CORS_ORIGINS` to your frontend URL

#### Railway

1. **Create a new project** on Railway
2. **Deploy from GitHub**
3. **Set root directory** to `backend`
4. **Add environment variables** from `env.example`
5. Railway will auto-detect Python and install dependencies

#### Docker

```bash
cd backend
docker build -t quant-recommender-backend .
docker run -p 8000:8000 --env-file .env quant-recommender-backend
```

### Frontend Deployment (Netlify)

1. **Connect your repository** to Netlify
2. **Build settings:**
   - Base directory: `frontend`
   - Build command: `npm run build`
   - Publish directory: `frontend/build`
3. **Environment variables:**
   - `REACT_APP_API_URL`: Your backend API URL (e.g., `https://your-backend.onrender.com/api/v1`)
4. **Deploy**

The `netlify.toml` file is already configured for this setup.

## Environment Variables

### Backend

| Variable | Description | Default |
|----------|-------------|---------|
| `LOG_LEVEL` | Logging level | `INFO` |
| `PORT` | Server port | `8000` |
| `CORS_ORIGINS` | Allowed CORS origins | `http://localhost:3000` |
| `CACHE_TTL_HOURS` | Cache time-to-live | `24` |
| `THRESHOLD_BUY` | Buy threshold | `0.5` |
| `THRESHOLD_SELL` | Sell threshold | `-0.5` |
| `VOLATILITY_FACTOR` | Volatility penalty factor | `0.5` |
| `ALPHA_VANTAGE_KEY` | Alpha Vantage API key (optional) | - |
| `BINANCE_API_KEY` | Binance API key (optional) | - |
| `BINANCE_SECRET` | Binance API secret (optional) | - |

### Frontend

| Variable | Description | Default |
|----------|-------------|---------|
| `REACT_APP_API_URL` | Backend API URL | `http://localhost:8000/api/v1` |

## Development

### Adding a New Strategy

1. Create a new strategy class in `backend/app/services/strategies.py`:
   ```python
   class MyStrategy(BaseStrategy):
       def calculate_signal(self, data: pd.DataFrame, **kwargs) -> float:
           # Your logic here
           return score
   ```

2. Register it in `Recommender._init_strategies()` in `recommender.py`

3. Update default weights in `endpoints.py`

### Modifying Recommendation Logic

Edit `backend/app/services/recommender.py`:
- `_aggregate_signals()`: Change aggregation method
- `_calculate_confidence()`: Adjust confidence calculation
- `_map_to_recommendation()`: Modify thresholds

## Data Sources

- **Stocks**: yfinance (Yahoo Finance)
- **Crypto**: yfinance (supports crypto tickers like BTC-USD)
- **Forex**: yfinance (supports forex pairs like EURUSD=X)

**Note**: yfinance works without API keys for basic data. For production use, consider:
- Alpha Vantage (stocks)
- Binance API (crypto)
- OANDA or FXCM (forex)

## Limitations & Disclaimers

⚠️ **IMPORTANT**: This system is for **educational and research purposes only**. 

- **Not Financial Advice**: Recommendations are generated by algorithms and should not be considered financial advice
- **No Trade Execution**: This system does NOT execute trades
- **Data Accuracy**: Relies on third-party data sources; verify data accuracy
- **Past Performance**: Historical performance does not guarantee future results
- **Risk**: Trading involves substantial risk of loss
- **ML Strategy**: The ML strategy is a placeholder and requires proper training data

## Troubleshooting

### Backend Issues

**Import errors:**
```bash
# Make sure you're in the backend directory
cd backend
# Ensure virtual environment is activated
source venv/bin/activate
# Reinstall dependencies
pip install -r requirements.txt
```

**Database errors:**
```bash
# Delete existing database
rm data/recommendations.db
# Restart the server
```

**Port already in use:**
```bash
# Use a different port
uvicorn app.main:app --port 8001
```

### Frontend Issues

**Cannot connect to backend:**
- Check that backend is running
- Verify `REACT_APP_API_URL` in `.env`
- Check CORS settings in backend

**Build errors:**
```bash
# Clear node_modules and reinstall
rm -rf node_modules package-lock.json
npm install
```

## License

This project is provided as-is for educational purposes.

## Contributing

This is a complete, production-ready system. To extend it:

1. Add new strategies in `backend/app/services/strategies.py`
2. Enhance the UI in `frontend/src/components/`
3. Add new endpoints in `backend/app/api/endpoints.py`
4. Improve backtesting in `backend/app/services/backtester.py`

## Support

For issues or questions:
1. Check the API documentation at `/docs` when backend is running
2. Review the code comments and docstrings
3. Check the example data files for expected formats

---

**Built with**: FastAPI, React, pandas, yfinance, scikit-learn

