# FinTrade
This repository contains the source code for Personal Project FinTrade. 

FinTrade is a paper-trading platform that simulates stock trading and portfolio management. I built it to explore how financial concepts such as order types, bid/ask spreads and transaction costs translate into a functional trading product. 

Key features
- Market / Limit / Stop-Loss / Take-Profit orders
- Portfolio and trade tracking
- Technical indicators
- BUY/HOLD/SELL decision assistant
- Backtesting on real historical data

Tech
- Python, Streamlit, SQLite, yfinance, etc.

Backtesting Results
- Refer to /results/backtest_runs.csv
- The strategy was profitable on 8 of 9 tickers tested but trailed buy-and-hold on every one, with the gap widest for the strongest uptrends (MU, TSLA, GOOGL)
- Limitations: All 9 tickers share the same 2024-2025 window, mostly a bull market, so it's one market regime rather than 9 independent tests

Run instruction for command prompt: streamlit run app.py
