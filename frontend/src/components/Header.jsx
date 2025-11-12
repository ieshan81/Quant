import React from 'react';
import './Header.css';

function Header({ lastUpdate, onRefresh }) {
  return (
    <header className="header">
      <div className="header-content">
        <h1 className="header-title">Quantitative Trading Recommendations</h1>
        <div className="header-info">
          {lastUpdate && (
            <span className="last-update">
              Last updated: {new Date(lastUpdate).toLocaleString()}
            </span>
          )}
          <button className="refresh-button" onClick={onRefresh}>
            🔄 Refresh
          </button>
        </div>
      </div>
    </header>
  );
}

export default Header;

