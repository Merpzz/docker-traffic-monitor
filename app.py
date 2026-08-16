#!/usr/bin/env python3
"""Small Docker traffic monitor."""

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

DATA_DIR = os.environ.get("DATA_DIR", "/data")
DB_PATH = os.path.join(DATA_DIR, "traffic.db")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "30"))
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8000"))
DOCKER_SOCKET = os.environ.get("DOCKER_SOCKET", "/var/run/docker.sock")


def get_sqlite_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_database():
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


def decode_chunked_body(body):
    """Decode an HTTP/1.1 chunked response body."""
    decoded = bytearray()
    pos = 0

    while True:
        line_end = body.find(b"\r\n", pos)
        if line_end == -1:
            raise RuntimeError("Malformed chunked Docker API response")

        size_line = body[pos:line_end].split(b";", 1)[0].strip()
        try:
            chunk_size = int(size_line, 16)
        except ValueError as exc:
            raise RuntimeError("Invalid chunk size from Docker API") from exc

        pos = line_end + 2
        if chunk_size == 0:
            break

        chunk_end = pos + chunk_size
        if chunk_end > len(body):
            raise RuntimeError("Incomplete chunked Docker API response")

        decoded.extend(body[pos:chunk_end])
        pos = chunk_end

        if body[pos:pos + 2] != b"\r\n":
            raise RuntimeError("Malformed chunk terminator from Docker API")
        pos += 2

    return bytes(decoded)


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

    if b"\r\n\r\n" not in payload:
        raise RuntimeError("Malformed HTTP response from Docker API")

    header, body = payload.split(b"\r\n\r\n", 1)
    header_text = header.decode("iso-8859-1", errors="replace")
    header_lines = header_text.splitlines()
    status_line = header_lines[0]
    status_code = int(status_line.split()[1])

    if status_code != 200:
        raise RuntimeError(f"Docker API returned status {status_code}: {status_line}")

    headers = {}
    for line in header_lines[1:]:
        if ":" in line:
            key, value = line.split(":", 1)
            headers[key.strip().lower()] = value.strip().lower()

    if "chunked" in headers.get("transfer-encoding", ""):
        body = decode_chunked_body(body)

    if not body:
        return None

    return json.loads(body.decode("utf-8"))


def normalize_container_name(raw_name):
    if not raw_name:
        return "unknown"
    if isinstance(raw_name, list):
        raw_name = raw_name[0] if raw_name else "unknown"
    return raw_name.lstrip("/").split("/")[-1]


def read_current_snapshot():
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
    rows = conn.execute("SELECT name, rx_bytes, tx_bytes FROM last_snapshot").fetchall()
    return {
        row["name"]: {"rx_bytes": int(row["rx_bytes"]), "tx_bytes": int(row["tx_bytes"])}
        for row in rows
    }


def save_snapshot(conn, snapshot):
    timestamp = datetime.now(timezone.utc).isoformat()
    current_names = set(snapshot)

    if current_names:
        placeholders = ",".join("?" for _ in current_names)
        conn.execute(
            f"DELETE FROM last_snapshot WHERE name NOT IN ({placeholders})",
            tuple(current_names),
        )
    else:
        conn.execute("DELETE FROM last_snapshot")

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
            (
                name,
                int(values.get("rx_bytes", 0) or 0),
                int(values.get("tx_bytes", 0) or 0),
                timestamp,
            ),
        )
    conn.commit()


def record_usage(conn, name, download_delta, upload_delta):
    if download_delta <= 0 and upload_delta <= 0:
        return
    timestamp = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO usage (name, download_delta, upload_delta, captured_at) VALUES (?, ?, ?, ?)",
        (name, int(download_delta), int(upload_delta), timestamp),
    )


def collect_once():
    conn = get_sqlite_connection()
    try:
        previous_snapshot = load_last_snapshot(conn)
        current_snapshot = read_current_snapshot()

        for name, current_values in current_snapshot.items():
            previous_values = previous_snapshot.get(name)
            if previous_values is None:
                continue

            current_rx = int(current_values.get("rx_bytes", 0) or 0)
            current_tx = int(current_values.get("tx_bytes", 0) or 0)
            previous_rx = int(previous_values.get("rx_bytes", 0) or 0)
            previous_tx = int(previous_values.get("tx_bytes", 0) or 0)

            download_delta = current_rx - previous_rx if current_rx >= previous_rx else current_rx
            upload_delta = current_tx - previous_tx if current_tx >= previous_tx else current_tx

            record_usage(conn, name, download_delta, upload_delta)

        save_snapshot(conn, current_snapshot)
        conn.commit()
    finally:
        conn.close()


def collector_loop():
    while True:
        try:
            collect_once()
        except Exception as exc:
            print(f"collect_once failed: {exc}", flush=True)
        time.sleep(POLL_INTERVAL)


def format_bytes(value):
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
    conn = get_sqlite_connection()
    try:
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
        return [dict(row) for row in rows]
    finally:
        conn.close()


def build_page():
    now = datetime.now(timezone.utc)
    period_rows = {
        "Today": fetch_period_rows(
            now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        ),
        "Last 30 days": fetch_period_rows((now - timedelta(days=30)).isoformat()),
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
            download = int(row["total_download"] or 0)
            upload = int(row["total_upload"] or 0)
            total = download + upload
            html_rows.append(
                "<tr>"
                f"<td>{escape(str(row['name']))}</td>"
                f"<td>{format_bytes(download)}</td>"
                f"<td>{format_bytes(upload)}</td>"
                f"<td>{format_bytes(total)}</td>"
                "</tr>"
            )
        html_rows.append("</tbody></table>")

    rows_html = "\n".join(html_rows)

    return f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>Docker Traffic Monitor</title>
        <style>
            body {{ font-family: Arial, sans-serif; background: #111827; color: #e5e7eb; margin: 0; padding: 1.5rem; font-size: 13px; }}
            h1 {{ color: #f9fafb; font-size: 26px; margin: 0 0 0.75rem; }}
            h2 {{ color: #f9fafb; font-size: 20px; margin: 1.15rem 0 0.7rem; }}
            table {{ border-collapse: collapse; width: min(980px, 100%); margin-bottom: 1.4rem; }}
            th, td {{ border: 1px solid #374151; padding: 0.5rem 0.7rem; text-align: left; }}
            th {{ background: #1f2937; }}
            tr:nth-child(even) {{ background: rgba(255,255,255,0.02); }}
            p {{ color: #d1d5db; margin: 0.5rem 0 1rem; }}
        </style>
    </head>
    <body>
        <h1>Docker Traffic Monitor</h1>
        <p>Tracks Docker container network traffic and stores the deltas in SQLite so totals survive restarts.</p>
        {rows_html}
    </body>
    </html>
    """


class TrafficHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/":
            page = build_page().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)
            return

        if parsed.path == "/api/traffic":
            now = datetime.now(timezone.utc)
            payload = {
                "today": fetch_period_rows(
                    now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
                ),
                "last_30_days": fetch_period_rows(
                    (now - timedelta(days=30)).isoformat()
                ),
                "all_time": fetch_period_rows(None),
            }
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        self.send_response(404)
        self.end_headers()

    def log_message(self, format, *args):
        return


def main():
    ensure_database()
    thread = threading.Thread(target=collector_loop, daemon=True)
    thread.start()
    server = HTTPServer((HOST, PORT), TrafficHandler)
    print(f"Docker traffic monitor running on http://{HOST}:{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
