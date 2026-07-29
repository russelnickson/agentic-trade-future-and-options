/**
 * PM2 process cluster for the F&O trading data engine.
 *
 * Usage:
 *   pm2 start ecosystem.config.js
 *   pm2 status
 *   pm2 logs
 *   pm2 restart all
 *   pm2 stop all
 *
 * Requires: Node.js PM2 (`npm i -g pm2`), project venv, .env, Docker Redis/Timescale.
 * Set TRADE_TOKENS before start (comma-separated F&O instrument tokens).
 */

const path = require("path");

const ROOT = __dirname;
const PYTHON = path.join(ROOT, "venv", "bin", "python");

module.exports = {
  apps: [
    {
      name: "tick_worker",
      script: path.join(ROOT, "workers", "tick_worker.py"),
      interpreter: PYTHON,
      cwd: ROOT,
      autorestart: true,
      watch: false,
      max_restarts: 50,
      min_uptime: "10s",
      restart_delay: 3000,
      exp_backoff_restart_delay: 1000,
      kill_timeout: 10000,
      env: {
        PYTHONUNBUFFERED: "1",
        PYTHONPATH: ROOT,
      },
    },
    {
      name: "greeks_engine",
      script: path.join(ROOT, "workers", "greeks_engine.py"),
      interpreter: PYTHON,
      cwd: ROOT,
      autorestart: true,
      watch: false,
      max_restarts: 50,
      min_uptime: "5s",
      restart_delay: 2000,
      exp_backoff_restart_delay: 1000,
      env: {
        PYTHONUNBUFFERED: "1",
        PYTHONPATH: ROOT,
      },
    },
    {
      name: "auto_squareoff",
      script: path.join(ROOT, "workers", "auto_squareoff.py"),
      interpreter: PYTHON,
      cwd: ROOT,
      autorestart: true,
      watch: false,
      max_restarts: 50,
      min_uptime: "5s",
      restart_delay: 2000,
      exp_backoff_restart_delay: 1000,
      env: {
        PYTHONUNBUFFERED: "1",
        PYTHONPATH: ROOT,
      },
    },
    {
      name: "strategic_controller",
      script: path.join(ROOT, "workers", "strategic_controller.py"),
      interpreter: PYTHON,
      cwd: ROOT,
      autorestart: true,
      watch: false,
      max_restarts: 50,
      min_uptime: "5s",
      restart_delay: 3000,
      exp_backoff_restart_delay: 1000,
      env: {
        PYTHONUNBUFFERED: "1",
        PYTHONPATH: ROOT,
        STRATEGIC_INTERVAL_SEC: "120",
        STRATEGIC_TTL_SEC: "180",
      },
    },
    {
      name: "tactical_executor",
      script: path.join(ROOT, "workers", "tactical_executor.py"),
      interpreter: PYTHON,
      cwd: ROOT,
      autorestart: true,
      watch: false,
      max_restarts: 50,
      min_uptime: "5s",
      restart_delay: 2000,
      exp_backoff_restart_delay: 1000,
      env: {
        PYTHONUNBUFFERED: "1",
        PYTHONPATH: ROOT,
        TACTICAL_POLL_SEC: "1",
      },
    },
    {
      name: "streamlit_dashboard",
      script: path.join(ROOT, "workers", "streamlit_dashboard.py"),
      interpreter: PYTHON,
      cwd: ROOT,
      autorestart: true,
      watch: false,
      max_restarts: 50,
      min_uptime: "5s",
      restart_delay: 3000,
      exp_backoff_restart_delay: 1000,
      env: {
        PYTHONUNBUFFERED: "1",
        PYTHONPATH: ROOT,
        STREAMLIT_SERVER_PORT: "8501",
        STREAMLIT_SERVER_ADDRESS: "0.0.0.0",
      },
    },
  ],
};
