# SmartShelf Raspberry Pi Backend

Backend service for collecting BLE packets from SmartShelf ESP nodes, exposing live API data, and persisting minute-level aggregates.

## What This Service Does

- Scans BLE manufacturer advertisements from ESP modules.
- Decodes sensor payloads from SHT/VCNL/SGP sensors.
- Deduplicates repeated advertisements by `(node_id, sequence)`.
- Keeps a rolling 10-minute per-second cache in memory and on disk.
- Stores per-minute averages in SQLite:
  - with outliers
  - with outliers filtered (IQR-based)
- Exposes HTTP endpoints for dashboard integration.

## Architecture

```mermaid
flowchart LR
  espNodes[ESPNodes x9] -->|BLE manufacturer packets| collector[BlePacketCollector]
  collector --> protocol[protocol.decode_payload]
  protocol --> dedupe[Dedupe by node_id+sequence]
  dedupe --> cache[RollingSecondCache 10min]
  cache --> cacheFile[second_cache.json]
  dedupe --> minuteAgg[Minute Aggregator]
  minuteAgg --> outlierFilter[IQR Filter]
  minuteAgg --> dbWith[minute_avg_with_outliers]
  outlierFilter --> dbWithout[minute_avg_no_outliers]
  dedupe --> nodeHealth[node_health table]
  dbWith --> api[FastAPI]
  dbWithout --> api
  cache --> api
```

## Module Layout

- `main.py` - service entrypoint and FastAPI startup/shutdown lifecycle.
- `api.py` - HTTP routes.
- `service.py` - orchestration: ingest, dedupe, cache, minute aggregation, DB writes.
- `collector.py` - BLE scanner callback integration (`BleakScanner`).
- `protocol.py` - payload decoding and packet/node dataclasses.
- `cache.py` - rolling second cache + atomic file persistence.
- `storage.py` - SQLite schema initialization and queries/upserts.
- `models.py` - Pydantic response models.
- `monitor_esp_ble.py` - optional terminal monitor for debugging.

## Prerequisites

- Python 3.10+ recommended
- Bluetooth available on Raspberry Pi host
- Dependencies from project root `requirements.txt`

Install dependencies from repository root:

```bash
pip install -r requirements.txt
```

## Run Instructions

### Start Backend API + BLE Ingestion

From repository root:

```bash
python -m RaspberryPi.backend.main
```

Optional arguments:

```bash
python -m RaspberryPi.backend.main \
  --host 0.0.0.0 \
  --port 8000 \
  --company-id 0xFFFF \
  --expected-nodes 9 \
  --stale-seconds 4.0 \
  --db-path RaspberryPi/data/smartshelf.db \
  --cache-path RaspberryPi/data/second_cache.json \
  --live-reload
```

`--live-reload` is enabled by default. Disable it with `--no-live-reload`.

### Optional Debug Monitor (Terminal UI)

This is not required for backend operation:

```bash
python -m RaspberryPi.backend.monitor_esp_ble
```

## API Endpoints

- `GET /health` - backend/scanner/database status.
- `GET /nodes/latest` - latest reading per node from rolling cache.
- `GET /nodes/realtime?window_seconds=600` - cached per-second readings.
- `GET /nodes/realtime/stream?window_seconds=600` - SSE stream of rolling-window cache snapshots (1s cadence).
- `GET /nodes/status` - online/stale and packet/drop counters per node.
- `GET /nodes/minute-history?outliers=include|exclude&from_ts=&to_ts=&node_id=` - minute aggregates from SQLite.
- `GET /api/categories` - predefined food-category trigger presets and metric limits.
- `GET /api/cells/config` - current trigger bounds/category assignment for every cell.
- `PUT /api/cells/<node_id>/config` - assign a category (`{"mode": "category", "category_id": "1001"}`) or set custom bounds (`{"mode": "custom", "custom_bounds": {...}}`) for a cell.

## Trigger System & Email Alerts

Each cell's temperature/humidity/VOC readings are classified into green/yellow/red
zones using either a predefined food category (`config/categories.json`, read-only)
or manually-set custom bounds. Bounds and category assignments are persisted per
cell in `config/config.json`, which the backend rewrites whenever a cell's
configuration changes via the API (or the Live Tiles popup in the dashboard).

When a metric stays in a worse zone (green->yellow or yellow->red) continuously for
more than `TRIGGER_SUSTAIN_SECONDS` (default 120s, see `config/constants.py`), an
email alert is sent once for that transition. Evaluation is skipped for empty
cells (no product placed) and resets if a sensor's readings go stale.

### Configuring email alerts (SMTP)

Easiest path with Gmail:

1. Enable 2-Step Verification on the Google account.
2. Create an [App Password](https://myaccount.google.com/apppasswords) (16 characters).
3. Copy `config/email_config.example.json` to `config/email_config.json` (gitignored)
   and fill in:
   - `username` / `password` — Gmail address + app password
   - `from_address` — usually the same Gmail address
   - `recipients` — one or more email addresses to notify
4. Defaults (`smtp.gmail.com`, port `587`, `use_tls: true`) work for Gmail.
   Other providers: set `smtp_host` / `smtp_port` accordingly.

Or set environment variables instead (they take precedence over the file):
`EMAIL_SMTP_HOST`, `EMAIL_SMTP_PORT`, `EMAIL_USE_TLS`, `EMAIL_USERNAME`,
`EMAIL_PASSWORD`, `EMAIL_FROM`, `EMAIL_FROM_NAME`, `EMAIL_RECIPIENTS`
(comma-separated list).

Until SMTP credentials and at least one recipient are configured, the backend
logs a one-time warning and skips sending alerts — everything else keeps working
normally.

To send a test email after filling the config:

```bash
python RaspberryPi/backend/scripts/test_email.py
```

## Data Storage

Default paths:

- SQLite DB: `RaspberryPi/data/smartshelf.db`
- Cache snapshot: `RaspberryPi/data/second_cache.json`

SQLite tables:

- `minute_avg_with_outliers`
- `minute_avg_no_outliers`
- `node_health`

## Quick Testing

After backend starts, check:

- [http://localhost:8000/health](http://localhost:8000/health)
- [http://localhost:8000/nodes/latest](http://localhost:8000/nodes/latest)

If minute tables are empty, wait for the next minute boundary (minute aggregates are persisted after each minute closes).

