#!/usr/bin/env python3
"""Small Docker traffic monitor.

This application polls the Docker Engine API for per-container network counters,
calculates the deltas between samples, and stores those deltas in SQLite. The
result is a simple, persistent traffic total that survives container restarts.

The app exposes a tiny HTML dashboard and a simple JSON API so it can be used
as a lightweight host dashboard or homepage widget without a full Prometheus +
Grafana stack.
"""

import json
import os
import sqlite3
import socket
import threading
import time
from datetime import datetime, timedelta, timezone
from html import escape
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

# Environment configuration. These can be overridden when running the container
# in Docker Compose or in a custom deployment.
DATA_DIR = os.environ.get("DATA_DIR", "/data")
DB_PATH = os.path.join(DATA_DIR, "traffic.db")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "30"))
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8000"))
DOCKER_SOCKET = os.environ.get("DOCKER_SOCKET", "/var/run/docker.sock")


def get_sqlite_connection():
    """Create a SQLite connection for the persistent traffic database."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_database():
    """Initialize the SQLite schema used for the last snapshot and traffic logs."""
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = get_sqlite_connection()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS last_snapshot (
            name TEXT PRIMARY KEY,
            rx_bytes INTEGER NOT NULL,
            tx_bytes INTEGER NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            download_delta INTEGER NOT NULL,
            upload_delta INTEGER NOT NULL,
            captured_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_usage_name_time ON usage (name, captured_at)"
    )
    conn.commit()
    conn.close()


def calculate_network_delta(previous, current):
    """Return the positive deltas between two snapshots.

    The Docker API provides cumulative counters. This helper compares the previous
    and current snapshot for each container name and only adds positive changes.
    This prevents negative values when a container disappears or when the counter
    is replaced by a fresh instance.
    """
    download_total = 0
    upload_total = 0
    for name in set(previous) | set(current):
        previous_stats = previous.get(name, {"rx_bytes": 0, "tx_bytes": 0})
        current_stats = current.get(name, {"rx_bytes": 0, "tx_bytes": 0})
        previous_rx = int(previous_stats.get("rx_bytes", 0) or 0)
        current_rx = int(current_stats.get("rx_bytes", 0) or 0)
        previous_tx = int(previous_stats.get("tx_bytes", 0) or 0)
        current_tx = int(current_stats.get("tx_bytes", 0) or 0)
        download_total += max(current_rx - previous_rx, 0)
        upload_total += max(current_tx - previous_tx, 0)
    return {"download": download_total, "upload": upload_total}


def http_get_json(path):
    """Send an HTTP GET request to the Docker Unix socket and return JSON."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(5)
        sock.connect(DOCKER_SOCKET)
        request = (
            f"GET {path} HTTP/1.1\r\n"
            "Host: docker\r\n"
            "Connection: close\r\n\r\n"
        )
        sock.sendall(request.encode("utf-8"))

        chunks = []
        while True:
            data = sock.recv(65536)
            if not data:
                break
            chunks.append(data)

    payload = b"".join(chunks)
    if not payload:
        return None

    header, body = payload.split(b"\r\n\r\n", 1)
    status_line = header.decode("utf-8", errors="replace").splitlines()[0]
    status_code = int(status_line.split()[1])
    if status_code != 200:
        raise RuntimeError(f"Docker API returned status {status_code}: {status_line}")

    return json.loads(body.decode("utf-8", errors="replace"))


def normalize_container_name(raw_name):
    """Convert Docker's name format into a readable container name."""
    if not raw_name:
        return "unknown"
    if isinstance(raw_name, list):
        raw_name = raw_name[0] if raw_name else "unknown"
    return raw_name.lstrip("/").split("/")[-1]


def read_current_snapshot():
    """Fetch the current network counters for every container.

    Docker exposes cumulative RX/TX counters for each network interface. We sum
    the values across interfaces so each container has a single download and
    upload total for the current observation.
    """
    containers_response = http_get_json("/v1.41/containers/json?all=1")
    if not containers_response:
        return {}

    snapshot = {}
    for container in containers_response:
        container_id = container.get("Id")
        if not container_id:
            continue

        container_name = normalize_container_name(container.get("Names", ["unknown"]))
        stats = http_get_json(f"/v1.41/containers/{container_id}/stats?stream=0")
        if not stats:
            continue

        networks = stats.get("networks") or {}
        rx_bytes = 0
        tx_bytes = 0
        for iface in networks.values():
            rx_bytes += int(iface.get("rx_bytes", 0) or 0)
            tx_bytes += int(iface.get("tx_bytes", 0) or 0)

        snapshot[container_name] = {"rx_bytes": rx_bytes, "tx_bytes": tx_bytes}

    return snapshot


def load_last_snapshot(conn):
    """Read the previous counters saved in SQLite to compare against the next read."""
    rows = conn.execute("SELECT name, rx_bytes, tx_bytes FROM last_snapshot").fetchall()
    return {
        row["name"]: {"rx_bytes": int(row["rx_bytes"]), "tx_bytes": int(row["tx_bytes"])}
        for row in rows
    }


def save_snapshot(conn, snapshot):
    """Persist the most recent raw counters so deltas can be calculated later."""
    timestamp = datetime.now(timezone.utc).isoformat()
    for name, values in snapshot.items():
        conn.execute(
            """
            INSERT INTO last_snapshot (name, rx_bytes, tx_bytes, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                rx_bytes = excluded.rx_bytes,
                tx_bytes = excluded.tx_bytes,
                updated_at = excluded.updated_at
            """,
            (name, int(values.get("rx_bytes", 0) or 0), int(values.get("tx_bytes", 0) or 0), timestamp),
        )
    conn.commit()


def record_usage(conn, name, download_delta, upload_delta):
    """Store a positive traffic increment for a container in the usage table."""
    if download_delta <= 0 and upload_delta <= 0:
        return
    timestamp = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO usage (name, download_delta, upload_delta, captured_at) VALUES (?, ?, ?, ?)",
        (name, int(download_delta), int(upload_delta), timestamp),
    )
    conn.commit()


def collect_once():
    """Collect one polling cycle and save the deltas for the current snapshot."""
    conn = get_sqlite_connection()
    previous_snapshot = load_last_snapshot(conn)
    current_snapshot = read_current_snapshot()

    for name, current_values in current_snapshot.items():
        previous_values = previous_snapshot.get(name, {"rx_bytes": 0, "tx_bytes": 0})
        download_delta = max(int(current_values.get("rx_bytes", 0) or 0) - int(previous_values.get("rx_bytes", 0) or 0), 0)
        upload_delta = max(int(current_values.get("tx_bytes", 0) or 0) - int(previous_values.get("tx_bytes", 0) or 0), 0)
        record_usage(conn, name, download_delta, upload_delta)

    save_snapshot(conn, current_snapshot)
    conn.close()


def collector_loop():
    """Run the collection loop forever with a configurable polling interval."""
    while True:
        try:
            collect_once()
        except Exception as exc:  # pragma: no cover - runtime guard
            print(f"collect_once failed: {exc}", flush=True)
        time.sleep(POLL_INTERVAL)


def format_bytes(value):
    """Convert a byte count into an easy-to-read unit such as GB or TB."""
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    size = float(value or 0)
    unit_index = 0
    while size >= 1024 and unit_index < len(units) - 1:
        size /= 1024
        unit_index += 1
    if unit_index == 0:
        return f"{int(size)} {units[unit_index]}"
    return f"{size:.2f} {units[unit_index]}"


def fetch_period_rows(since):
    """Return aggregated traffic totals for a selected time window.

    If `since` is None, all historical data is included. Otherwise only rows from
    the given timestamp forward are counted.
    """
    conn = get_sqlite_connection()
    if since is None:
        rows = conn.execute(
            """
            SELECT name,
                   SUM(download_delta) AS total_download,
                   SUM(upload_delta) AS total_upload
            FROM usage
            GROUP BY name
            ORDER BY (SUM(download_delta) + SUM(upload_delta)) DESC
            """
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT name,
                   SUM(download_delta) AS total_download,
                   SUM(upload_delta) AS total_upload
            FROM usage
            WHERE captured_at >= ?
            GROUP BY name
            ORDER BY (SUM(download_delta) + SUM(upload_delta)) DESC
            """,
            (since,),
        ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def build_page():
    """Render the simple HTML dashboard with three traffic views."""
    now = datetime.now(timezone.utc)
    period_rows = {
        "Today": fetch_period_rows(now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()),
        "Month": fetch_period_rows((now - timedelta(days=30)).isoformat()),
        "All time": fetch_period_rows(None),
    }

    html_rows = []
    for label, rows in period_rows.items():
        html_rows.append(f"<h2>{escape(label)}</h2>")
        if not rows:
            html_rows.append("<p>No traffic recorded yet.</p>")
            continue

        html_rows.append(
            """
            <table>
              <thead>
                <tr>
                  <th>Container</th>
                  <th>Download</th>
                  <th>Upload</th>
                  <th>Total</th>
                </tr>
              </thead>
              <tbody>
            """
        )
        for row in rows:
            total = int((row["total_download"] or 0) + (row["total_upload"] or 0))
            html_rows.append(
                "<tr>"
                f"<td>{escape(str(row['name']))}</td>"
                f"<td>{format_bytes(row['total_download'])}</td>"
                f"<td>{format_bytes(row['total_upload'])}</td>"
                f"<td>{format_bytes(total)}</td>"
                "</tr>"
            )
        html_rows.append("</tbody></table>")

    return """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>Docker Traffic Monitor</title>
        <style>
            body { font-family: Arial, sans-serif; background: #111827; color: #e5e7eb; margin: 0; padding: 2rem; }
            h1, h2 { color: #f9fafb; }
            table { border-collapse: collapse; width: min(980px, 100%); margin-bottom: 2rem; }
            th, td { border: 1px solid #374151; padding: 0.7rem 0.9rem; text-align: left; }
            th { background: #1f2937; }
            tr:nth-child(even) { background: rgba(255,255,255,0.02); }
            p { color: #d1d5db; }
        </style>
    </head>
    <body>
        <h1>Docker Traffic Monitor</h1>
        <p>Tracks Docker container network traffic and stores the deltas in SQLite so totals survive restarts.</p>
        {rows}
    </body>
    </html>
    """.format(rows="\n".join(html_rows))


class TrafficHandler(BaseHTTPRequestHandler):
    """Very small HTTP API for exposing the dashboard and JSON data."""

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(build_page().encode("utf-8"))
            return

        if parsed.path == "/api/traffic":
            now = datetime.now(timezone.utc)
            payload = {
                "today": fetch_period_rows(now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()),
                "month": fetch_period_rows((now - timedelta(days=30)).isoformat()),
                "all_time": fetch_period_rows(None),
            }
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode("utf-8"))
            return

        self.send_response(404)
        self.end_headers()

    def log_message(self, format, *args):
        return


def main():
    """Start the collector thread and serve the dashboard on the configured port."""
    ensure_database()
    thread = threading.Thread(target=collector_loop, daemon=True)
    thread.start()
    server = HTTPServer((HOST, PORT), TrafficHandler)
    print(f"Docker traffic monitor running on http://{HOST}:{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
