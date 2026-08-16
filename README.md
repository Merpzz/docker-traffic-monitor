# Docker Traffic Monitor

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Docker](https://img.shields.io/badge/Docker-Container-blue.svg)](https://www.docker.com/)

A lightweight Docker monitoring app that reads container network traffic from the Docker Engine API and stores the deltas in SQLite. The total remains persistent even when the monitor container is restarted or recreated.

## Why this exists

This is intentionally not a full observability stack. It is built for the simple use case of:

- tracking traffic per Docker container
- keeping totals over time
- showing them in a compact web dashboard
- avoiding the weight and complexity of Prometheus + Grafana

## Features

- Reads per-container RX/TX traffic from Docker stats
- Stores deltas in SQLite instead of relying only on Docker's current counters
- Shows totals for Today, Last 30 days, and All time
- Runs as a very small standalone container
- Exposes a simple HTML dashboard and JSON API
- Works with any Docker host as long as the Docker socket is mounted

## Architecture

```text
Docker socket
     |
     v
Python app
     |
     +--> reads current container network counters
     +--> compares against previous snapshot
     +--> stores deltas in SQLite
     +--> serves HTML + JSON dashboard
```

## Quick start

```bash
git clone <your-repo-url>
cd docker-traffic-monitor
mkdir -p data

docker compose up --build -d
```

Open the web UI at:

```text
http://<host>:8000
```

## Docker template / example configuration

The project includes an example environment file at [.env.example](.env.example). You can use it as a template for Docker or Compose configuration.

Example:

```env
HOST=0.0.0.0
PORT=8000
POLL_INTERVAL=30
DATA_DIR=/data
DOCKER_SOCKET=/var/run/docker.sock
```

In Docker Compose, the important mapping is:

```yaml
volumes:
  - /var/run/docker.sock:/var/run/docker.sock:ro
  - ./data:/data
```

This gives the container access to Docker's socket so it can read container network counters without requiring an agent inside every service.

## Dashboard presentation

The app serves a browser-based HTML page with tables like this:

| Container | Download | Upload | Total |
|-----------|----------|--------|-------|
| qbittorrentvpn | 4.83 TB | 9.21 TB | 14.04 TB |
| Jellyfin | 312 GB | 1.48 TB | 1.79 TB |
| Nextcloud | 84 GB | 61 GB | 145 GB |
| Sonarr | 12 GB | 3.1 GB | 15.1 GB |

It also exposes JSON data at:

```text
http://<host>:8000/api/traffic
```

That makes it easy to embed in a homepage widget or reuse in another application later.

## Configuration

The app can be configured through environment variables:

- `HOST` - bind address, default `0.0.0.0`
- `PORT` - web port, default `8000`
- `POLL_INTERVAL` - polling interval in seconds, default `30`
- `DATA_DIR` - location for the SQLite database, default `/data`
- `DOCKER_SOCKET` - Docker socket path, default `/var/run/docker.sock`

## Data model

Traffic is stored as deltas in SQLite tables called `usage` and `last_snapshot`:

- `usage` stores each measured download/upload delta together with a timestamp.
- `last_snapshot` stores the most recent raw network counters so the next sample can be compared against it.

This pattern avoids the problem where Docker counters are cumulative and may reset or be recreated after container restarts.

## Notes

This is intentionally minimal. It is designed for simple dashboards, homepage cards, and host-level monitoring without the overhead of Prometheus, Grafana, or large monitoring stacks.

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE) for details.
