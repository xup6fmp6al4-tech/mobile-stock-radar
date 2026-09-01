# Mobile Stock Radar v1.1 — Automatic Black Box

This version is designed for ChatGPT/Radar walk-forward testing, not for human-facing charts.

## Architecture

GitHub Actions (every 5 minutes) -> Yahoo 1-minute data -> external PostgreSQL -> Render read-only API -> ChatGPT

Why: Render Free web services sleep after inactivity and have ephemeral local disks. Therefore the recorder must not depend on the Render process or local SQLite for production persistence.

## What is recorded now
- 1-minute OHLCV for configured Taiwan/US stocks, ETFs, indices, Gold/Oil/DXY when Yahoo provides it.
- Provider capture success/failure and timestamp.
- Structural missing-feed registry.
- Duplicate minute bars are upserted by `(symbol, ts_utc)`, so a later capture can fill minutes missed by an earlier scheduled run.

## What is still explicitly missing
Check `/api/blackbox/gaps`. Initial structural gaps:
- TWSE foreign/investment-trust/dealer daily flows
- margin/short balance
- broker-branch flows
- TX/MTX day+night intraday feed
- TAIFEX institutional positions
- timestamped news/catalysts

These are NOT silently treated as collected.

## API for ChatGPT
- `GET /health`
- `GET /api/radar`
- `GET /api/blackbox/status`
- `GET /api/blackbox/gaps`
- `GET /api/blackbox/bars?symbol=6770.TW&start=...&end=...`
- `GET /api/blackbox/raw?symbol=6770.TW`
- `GET /api/blackbox/export?date=2026-09-01`

## Production setup
1. Create a persistent PostgreSQL database (Neon Free is suitable for testing; other Postgres works too).
2. Copy its connection string as `DATABASE_URL`.
3. In GitHub repo Settings -> Secrets and variables -> Actions, create secret `DATABASE_URL`.
4. Push this project to GitHub. The included workflow runs every 5 minutes and writes the full watchlist.
5. Deploy the repo as a Render Free Web Service using `render.yaml`; add the same `DATABASE_URL` to Render.
6. Test `/health`, then `/api/blackbox/status`, then `/api/blackbox/gaps`.

## Local test
Without DATABASE_URL the project falls back to SQLite only for local development:

```bash
pip install -r requirements.txt
python collector.py
uvicorn app:app --reload
```

## Important
GitHub Actions schedule is not guaranteed to run at an exact second/minute. This design compensates by requesting the provider's current 1-minute day series on every run and UPSERTing all bars, so later runs can backfill minute bars that appeared while a run was delayed.
