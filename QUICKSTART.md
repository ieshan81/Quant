# Quick Start Guide

## 1. Backend Setup (5 minutes)

```bash
# Navigate to backend
cd backend

# Create virtual environment
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Copy environment file (optional - works without API keys)
cp ../env.example .env

# Run the server
uvicorn app.main:app --reload
```

Backend will be available at: `http://localhost:8000`
- API Docs: `http://localhost:8000/docs`
- Health: `http://localhost:8000/api/v1/health`

## 2. Frontend Setup (3 minutes)

```bash
# Navigate to frontend
cd frontend

# Install dependencies
npm install

# Create .env file (optional)
echo "REACT_APP_API_URL=http://localhost:8000/api/v1" > .env

# Start development server
npm start
```

Frontend will open at: `http://localhost:3000`

## 3. Test the System

1. Open `http://localhost:3000` in your browser
2. Select an asset type (Stocks, Crypto, or Forex)
3. Click "Refresh" to load recommendations
4. Click on any ticker to see detailed analysis

## 4. Run Tests

```bash
# From project root
cd backend
python -m unittest discover -s ../tests -p "test_*.py"
```

## 5. Example API Calls

### Get Recommendations
```bash
curl http://localhost:8000/api/v1/recommendations?asset_type=stocks&limit=10
```

### Get Asset Details
```bash
curl http://localhost:8000/api/v1/asset/AAPL
```

### Health Check
```bash
curl http://localhost:8000/api/v1/health
```

## Troubleshooting

**Backend won't start:**
- Check Python version: `python --version` (needs 3.11+)
- Activate virtual environment
- Install dependencies: `pip install -r requirements.txt`

**Frontend can't connect:**
- Ensure backend is running on port 8000
- Check `REACT_APP_API_URL` in frontend `.env`
- Verify CORS settings in backend

**No data showing:**
- Check internet connection (yfinance needs internet)
- Verify ticker symbols are valid
- Check backend logs for errors

## Next Steps

- Read the full [README.md](README.md) for detailed documentation
- Check `example_data/` for sample data formats
- Review API documentation at `/docs` endpoint
- Customize strategies in `backend/app/services/strategies.py`

