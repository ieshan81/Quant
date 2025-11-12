/** API client for backend communication */
const API_BASE_URL = process.env.REACT_APP_API_URL || 'http://localhost:8000/api/v1';

/**
 * Fetch wrapper with error handling
 */
async function fetchAPI(endpoint, options = {}) {
  const url = `${API_BASE_URL}${endpoint}`;
  
  try {
    const response = await fetch(url, {
      ...options,
      headers: {
        'Content-Type': 'application/json',
        ...options.headers,
      },
    });

    if (!response.ok) {
      const error = await response.json().catch(() => ({ detail: response.statusText }));
      throw new Error(error.detail || `HTTP error! status: ${response.status}`);
    }

    return await response.json();
  } catch (error) {
    console.error(`API Error (${endpoint}):`, error);
    throw error;
  }
}

/**
 * Get health status
 */
export async function getHealth() {
  return fetchAPI('/health');
}

/**
 * Get recommendations
 * @param {string} assetType - 'stocks', 'crypto', or 'forex'
 * @param {number} limit - Maximum number of recommendations
 * @param {string} tickers - Comma-separated list of tickers (optional)
 */
export async function getRecommendations(assetType = 'stocks', limit = 50, tickers = null) {
  const params = new URLSearchParams({
    asset_type: assetType,
    limit: limit.toString(),
  });
  
  if (tickers) {
    params.append('tickers', tickers);
  }
  
  return fetchAPI(`/recommendations?${params.toString()}`);
}

/**
 * Get detailed asset information
 * @param {string} ticker - Ticker symbol
 */
export async function getAssetDetail(ticker) {
  return fetchAPI(`/asset/${ticker}`);
}

/**
 * Run backtest
 * @param {Object} backtestParams - Backtest parameters
 */
export async function runBacktest(backtestParams) {
  return fetchAPI('/backtest', {
    method: 'POST',
    body: JSON.stringify(backtestParams),
  });
}

/**
 * Get available strategies
 */
export async function getStrategies() {
  return fetchAPI('/strategies');
}

