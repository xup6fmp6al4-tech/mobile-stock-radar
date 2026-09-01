# Mobile Stock Radar v1.2 — Automatic Black Box / Storage Guard

Machine-readable recorder for ChatGPT/Radar walk-forward testing. The UI is intentionally minimal.

## Architecture

cron-job.org every 5 minutes -> GitHub `workflow_dispatch` -> Yahoo 1-minute feed -> Neon PostgreSQL -> Render read-only API -> ChatGPT

GitHub's native `schedule` is intentionally NOT used after it proved unreliable in testing.

## v1.2 storage fixes

1. **One capture record per run** — not one record per symbol. With 35 symbols this cuts `captures` growth by about 97%.
2. **Incremental DB writes** — Yahoo may return the whole current day every call, but v1.2 writes only new bars plus a 3-minute correction window while a feed is live. Closed/stale markets normally write zero repeated bars.
3. **First-seen boundary** — `bars.captured_at` is no longer overwritten on UPSERT. It becomes a useful first-observed timestamp for stricter walk-forward checks.
4. **Automatic 1m -> 5m archive** — 1-minute bars default to 30 days. Older rows are aggregated into `bars_5m` and then removed from `bars`.
5. **5-minute retention** — default 270 days.
6. **Capture-log retention** — successful run summaries 14 days; failed run summaries 90 days.
7. **Soft storage guard** — default 180 MB warning/guard. If table usage exceeds it, the raw 1-minute window is shortened to 21 days on maintenance. PostgreSQL may reuse freed pages instead of immediately shrinking physical bytes.
8. **Maintenance every 12 hours** — automatic, inside a normal collector run.

All retention values are configurable with environment variables:

- `RAW_1M_RETENTION_DAYS=30`
- `ARCHIVE_5M_RETENTION_DAYS=270`
- `CAPTURE_SUCCESS_RETENTION_DAYS=14`
- `CAPTURE_ERROR_RETENTION_DAYS=90`
- `MAINTENANCE_INTERVAL_HOURS=12`
- `STORAGE_SOFT_LIMIT_MB=180`

## Recorded feeds now

- 1-minute OHLCV for configured Taiwan/US stocks, ETFs, indices, Gold/Oil/DXY when Yahoo provides it.
- Batch capture result, timestamps, per-symbol success/error metadata.
- Structural missing-feed registry.
- Duplicate bars are keyed by `(symbol, ts_utc)`.

## Still missing / explicitly marked as gaps

Check `/api/blackbox/gaps`:

- TWSE foreign/investment-trust/dealer daily flows
- margin/short balance
- broker-branch flows
- TX/MTX day+night intraday feed
- TAIFEX institutional positions
- timestamped news/catalysts

## API for ChatGPT

- `GET /health`
- `GET /api/radar`
- `GET /api/blackbox/status`
- `GET /api/blackbox/gaps`
- `GET /api/blackbox/storage`
- `GET /api/blackbox/bars?symbol=6770.TW&start=...&end=...`
- `GET /api/blackbox/bars5m?symbol=6770.TW&start=...&end=...`
- `GET /api/blackbox/raw`
- `GET /api/blackbox/export?date=2026-09-01&resolution=1m`

## Upgrade from v1.1

No manual database migration is required. On the first v1.2 collector/API start, `init_db()` creates `bars_5m` and `system_state` while keeping the existing `bars`, `captures`, and `source_status` data.

Existing v1.1 `captures` rows remain until retention cleanup removes them. New v1.2 runs add only one summary row per run.

## External trigger

The workflow only declares `workflow_dispatch`. cron-job.org should POST to:

`https://api.github.com/repos/<owner>/<repo>/actions/workflows/capture.yml/dispatches`

with JSON body `{"ref":"main"}` and an Authorization bearer token scoped only to this repository with Actions read/write.
