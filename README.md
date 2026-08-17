# Docker Traffic Monitor

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Docker](https://img.shields.io/badge/Docker-Container-blue.svg)](https://www.docker.com/)

A lightweight Docker monitoring app that reads per-container network traffic from the Docker Engine API and stores traffic deltas in SQLite. Historical totals remain persistent when monitored containers, or the monitor itself, are restarted or recreated.

## Why this exists

This is intentionally not a full observability stack. It is built for the simple use case of:

- tracking RX/TX traffic per Docker container
- keeping persistent traffic totals over time
- viewing Today, Last 30 days, and All time statistics
- exposing compact data for a Homepage service widget
- avoiding the weight and complexity of Prometheus + Grafana for this specific job

## Features

- Reads per-container RX/TX counters from Docker stats
- Stores measured deltas in SQLite instead of relying on Docker's current counters
- Handles Docker counter resets without subtracting traffic from historical totals
- Browser dashboard with Today, Last 30 days, and All time tables
- JSON API containing all three periods
- Homepage-specific JSON endpoint with a configurable period
- Homepage period choices: `today`, `30d`, or `alltime`
- `alltime` is the default Homepage period
- Homepage API returns a human-readable period label so the widget can always show which period it represents
- Small standalone container with no agent required inside monitored containers

## How it works

```text
Docker socket
     |
     v
Docker Traffic Monitor
     |
     +--> reads current RX/TX counters for each container
     |
     +--> compares them with the previous snapshot
     |
     +--> stores only positive traffic deltas in SQLite
     |
     +--> keeps the latest raw counters for the next sample
     |
     +--> serves HTML dashboard + JSON APIs
                         |
                         +--> Homepage custom API widget
```

Docker's network counters are cumulative for the lifetime of a container and can reset when a container is recreated. Docker Traffic Monitor therefore does not use those raw counters as its historical total. Each polling cycle calculates the change since the previous snapshot and stores that change in the `usage` table. This lets the database accumulate traffic across container restarts.

The first snapshot for a container establishes a baseline. Subsequent snapshots contribute traffic deltas to the historical database.

## Quick start

```bash
git clone https://github.com/Merpzz/docker-traffic-monitor.git
cd docker-traffic-monitor
mkdir -p data

docker compose up --build -d
```

Open the web UI at:

```text
http://<host>:8000
```

## Docker configuration

The project includes an example environment file at [.env.example](.env.example).

Example:

```env
HOST=0.0.0.0
PORT=8000
POLL_INTERVAL=30
DATA_DIR=/data
DOCKER_SOCKET=/var/run/docker.sock
```

The important volume mappings are:

```yaml
volumes:
  - /var/run/docker.sock:/var/run/docker.sock:ro
  - ./data:/data
```

The Docker socket is mounted read-only and is used to retrieve container information and network statistics. `/data` contains the persistent SQLite database.

## Web dashboard

The main page displays three independent views:

- **Today** - traffic recorded since 00:00 UTC today
- **Last 30 days** - rolling 30-day traffic totals
- **All time** - everything stored in the database

Each table contains:

| Container | Download | Upload | Total |
|-----------|----------|--------|-------|
| qbittorrentvpn | 4.83 TB | 9.21 TB | 14.04 TB |
| Jellyfin | 312 GB | 1.48 TB | 1.79 TB |
| Nextcloud | 84 GB | 61 GB | 145 GB |

Rows are ordered by total traffic, highest first.

## API

### Full traffic API

```text
GET /api/traffic
```

Returns all three datasets in one JSON response:

```json
{
  "today": [],
  "last_30_days": [],
  "all_time": []
}
```

Each container entry contains its name, total download bytes, and total upload bytes for that period.

### Homepage API

```text
GET /api/homepage
GET /api/homepage?period=today
GET /api/homepage?period=30d
GET /api/homepage?period=alltime
```

The Homepage endpoint returns the selected period plus the three containers with the highest total traffic for that period, formatted for a compact Homepage card.

Supported period values are:

| Value | API label | Period |
|-------|-----------|--------|
| `today` | `Today` | Since 00:00 UTC today |
| `30d` | `Last 30 days` | Rolling last 30 days |
| `alltime` | `All time` | All traffic stored in the database |

If `period` is omitted, or an unsupported value is supplied, the endpoint uses `alltime`.

Example response:

```json
{
  "period": "All time",
  "top1": "qbittorrentvpn · 14.04 TB",
  "top2": "Jellyfin · 1.79 TB",
  "top3": "Nextcloud · 145.00 GB"
}
```

Because the `period` value is generated from the same query parameter that controls the data selection, the displayed period cannot silently drift out of sync with the traffic totals.

## Homepage integration

Docker Traffic Monitor can be used with Homepage's `customapi` widget. Select the desired period in the widget URL and map the returned `period` field as the first row.

A neutral service description such as `Docker-trafik per container` is recommended, while the widget itself shows the currently selected period dynamically.

### All time (recommended/default)

```yaml
- Docker Traffic Monitor:
    description: Docker-trafik per container
    href: http://<traffic-monitor>:8000
    widget:
      type: customapi
      url: http://<traffic-monitor>:8000/api/homepage?period=alltime
      mappings:
        - field: period
          label: Period
        - field: top1
          label: Top 1
        - field: top2
          label: Top 2
        - field: top3
          label: Top 3
```

To change what the Homepage card displays, only change the `period` value:

```yaml
# Today
url: http://<traffic-monitor>:8000/api/homepage?period=today

# Last 30 days
url: http://<traffic-monitor>:8000/api/homepage?period=30d

# All time
url: http://<traffic-monitor>:8000/api/homepage?period=alltime
```

The `Period` row updates automatically to `Today`, `Last 30 days`, or `All time` to match the selected dataset.

The Homepage card is intended as the compact overview. Clicking the service card can open the full Docker Traffic Monitor dashboard, where Today, Last 30 days, and All time remain visible together.

Because `/api/homepage` defaults to All time, this also works:

```yaml
url: http://<traffic-monitor>:8000/api/homepage
```

Explicitly setting `period=alltime` is useful because it makes the intended widget behavior clear in the Homepage configuration.

## Configuration reference

Docker Traffic Monitor itself is configured through environment variables:

- `HOST` - bind address, default `0.0.0.0`
- `PORT` - web port, default `8000`
- `POLL_INTERVAL` - polling interval in seconds, default `30`
- `DATA_DIR` - location for the SQLite database, default `/data`
- `DOCKER_SOCKET` - Docker socket path, default `/var/run/docker.sock`

The Homepage display period is not an environment variable. It is selected per request using the `period` query parameter on `/api/homepage`.

## Data model

Traffic is stored in two SQLite tables:

### `usage`

Stores each measured traffic increment:

- container name
- download delta
- upload delta
- capture timestamp

Historical period totals are calculated by summing these rows.

### `last_snapshot`

Stores the latest raw Docker RX/TX counters for each container. The next polling cycle compares the new Docker counters with this snapshot to determine how much traffic occurred since the previous sample.

## Persistence and restarts

The SQLite database must be stored on persistent storage, normally by mounting a host directory to `/data`.

Restarting or recreating Docker Traffic Monitor does not erase historical totals as long as the same `/data` directory is mounted again. Container counter resets are handled when collecting new samples so historical traffic remains intact.

## Notes

Docker Traffic Monitor is deliberately focused on persistent Docker network totals and a small dashboard/API surface. It is not intended to replace a complete metrics platform when CPU, memory, latency, alerting, long-term graphing, or distributed observability are required.

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE) for details.
