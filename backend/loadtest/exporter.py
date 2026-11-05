"""Serves the backend's Prometheus metrics from PROMETHEUS_MULTIPROC_DIR.

Runs beside uvicorn rather than inside it, so metrics keep flowing while the app
is frozen or its event loop is blocked. Those are the moments the dashboard
matters most (see src/backend/core/metrics.py).

    PROMETHEUS_MULTIPROC_DIR=/tmp/medha-metrics python loadtest/exporter.py --port 9200
"""

import argparse
import os
import time

from prometheus_client import CollectorRegistry, multiprocess, start_http_server


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=9200)
    args = parser.parse_args()
    if not os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        raise SystemExit("set PROMETHEUS_MULTIPROC_DIR")

    registry = CollectorRegistry()
    multiprocess.MultiProcessCollector(registry)
    start_http_server(args.port, addr="::", registry=registry)
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
