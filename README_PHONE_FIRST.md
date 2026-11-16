# SENTINEL V10 — iPhone + GitHub + Streamlit Cloud

No Windows commands needed. PAPER TRADING ONLY — no real Kalshi orders.

## Install from iPhone
1. Download the ZIP in ChatGPT. Open it in iPhone Files and tap once to unzip.
2. Sign in at https://github.com in Safari and create a **new private or public repository** named `sentinel-v10-phone` (public is easier for free Streamlit Cloud).
3. In the repository, use **Add file → Upload files** to upload **app.py, engine.py, report.py, requirements.txt**, plus the `.streamlit/config.toml` optional theme file. Upload files to the repo root; don't accidentally add an outer folder. GitHub mobile UI may require Safari's **Request Desktop Website**. If hidden folder uploading is difficult, skip `.streamlit`: the app still works.
4. Visit https://share.streamlit.io on your iPhone and deploy that repository with **Main file path = app.py**. Open the resulting Streamlit link in Safari.
5. Leave the hosted app awake for scanning. Add its URL to your Home Screen via Safari Share → Add to Home Screen.

## Crucial limitations
- In this phone-first version the paper scanner runs as a daemon thread INSIDE the Streamlit server, **not on the iPhone itself**. The phone is just your dashboard.
- **Not 24/7 guaranteed**. Streamlit Community Cloud may sleep, restart, redeploy, or kill the app, which stops scanning. Closing Safari can result in inactivity / eventual sleep. Missing intervals are not backfilled.
- **History is not durably persistent on Community Cloud** because local SQLite is ephemeral. Entries may reset on restart. Use the separate V10 worker package with a persistent disk and secured remote dashboard for genuinely continuous operation.
- Maximum one paper entry per detected market ticker, not a guaranteed trade every market. Missing book, strike, liquidity or connectivity can cause skips.
- Scores are heuristic evidence, not 75% calibrated probabilities. 75% is a target requiring 500+ actual completed paper trades and honest fee-aware reporting.
- The engine uses public Kalshi KXBTC15M quotes and Coinbase candles, and official Kalshi contract results for settlement. Paper fills simulate quotes with slippage and fees; never actual executions.

## Contents
- `app.py`: phone-optimized Streamlit dashboard with in-process scanner
- `engine.py`: Kalshi public-data paper engine and SQLite trade record
- `requirements.txt`: Streamlit dependencies
- `report.py`: offline CSV/performance analysis when run in a Python environment
- `.streamlit/config.toml`: optional visual theme

Do not enter API keys. The code uses public feeds only.
