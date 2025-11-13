import React from 'react';
import { BrowserRouter as Router, Routes, Route, NavLink } from 'react-router-dom';
import clsx from 'clsx';

import DashboardPage from './pages/DashboardPage';
import AnalyticsPage from './pages/AnalyticsPage';
import { API_BASE_URL } from './api';
import styles from './App.module.css';

function App() {
  const effectiveApiUrl = process.env.REACT_APP_API_URL || API_BASE_URL;

  return (
    <Router>
      <div className={styles.appShell}>
        <header className={styles.header}>
          <div className={styles.branding}>
            <div className={styles.brandMark}>Q</div>
            <div>
              <h1 className={styles.title}>Quant Intelligence</h1>
              <p className={styles.subtitle}>Live trading signals &amp; analytics dashboard</p>
            </div>
          </div>
          <div className={styles.environment}>
            <span className={styles.envLabel}>API Endpoint</span>
            <span className={styles.envValue}>{effectiveApiUrl}</span>
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
