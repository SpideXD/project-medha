"""Writes the Grafana dashboard. Generated rather than hand-edited JSON so each panel
stays a readable one-liner.

    python loadtest/monitoring/build_dashboard.py
"""

import json
from pathlib import Path

OUT = Path(__file__).parent / "grafana" / "dashboards" / "medha.json"
WIDTH = 24


class Layout:
    def __init__(self) -> None:
        self.panels: list[dict] = []
        self.y = 0
        self.x = 0
        self.row_height = 0
        self.next_id = 1

    def _id(self) -> int:
        self.next_id += 1
        return self.next_id

    def row(self, title: str) -> None:
        self._newline()
        self.panels.append({
            "id": self._id(), "type": "row", "title": title, "collapsed": False,
            "gridPos": {"h": 1, "w": WIDTH, "x": 0, "y": self.y},
        })
        self.y += 1

    def _newline(self) -> None:
        if self.x:
            self.y += self.row_height
            self.x = 0
            self.row_height = 0

    def _place(self, width: int, height: int) -> dict:
        if self.x + width > WIDTH:
            self._newline()
        position = {"h": height, "w": width, "x": self.x, "y": self.y}
        self.x += width
        self.row_height = max(self.row_height, height)
        return position

    def chart(self, title: str, targets: list[tuple[str, str]], *, unit: str = "short", width: int = 8,
              height: int = 8, description: str = "", stacked: bool = False) -> None:
        self.panels.append({
            "id": self._id(), "type": "timeseries", "title": title, "description": description,
            "gridPos": self._place(width, height),
            "datasource": {"type": "prometheus", "uid": "prometheus"},
            "fieldConfig": {"defaults": {
                "unit": unit,
                "custom": {"fillOpacity": 25 if stacked else 8, "showPoints": "never", "lineWidth": 1.5,
                           "spanNulls": True, "stacking": {"mode": "normal" if stacked else "none"}},
            }, "overrides": []},
            "options": {"legend": {"displayMode": "table", "placement": "right", "calcs": ["lastNotNull", "max"]},
                        "tooltip": {"mode": "multi", "sort": "desc"}},
            "targets": [{"refId": chr(65 + i), "expr": expr, "legendFormat": legend, "interval": "5s"}
                        for i, (expr, legend) in enumerate(targets)],
        })

    def stat(self, title: str, expr: str, *, unit: str = "short", width: int = 4, description: str = "",
             thresholds: list[tuple[float, str]] | None = None) -> None:
        steps = [{"color": "green", "value": None}]
        steps += [{"color": color, "value": value} for value, color in (thresholds or [])]
        self.panels.append({
            "id": self._id(), "type": "stat", "title": title, "description": description,
            "gridPos": self._place(width, 4),
            "datasource": {"type": "prometheus", "uid": "prometheus"},
            "fieldConfig": {"defaults": {"unit": unit, "thresholds": {"mode": "absolute", "steps": steps}},
                            "overrides": []},
            "options": {"colorMode": "background", "graphMode": "area",
                        "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}},
            "targets": [{"refId": "A", "expr": expr, "instant": False, "interval": "5s"}],
        })

    def table(self, title: str, targets: list[tuple[str, str]], *, width: int = 24, height: int = 11,
              description: str = "", join_on: str = "feature") -> None:
        """One row per feature: each query is instant and one column of the table."""
        units = {"Req/s": "reqps", "p50": "s", "p95": "s", "p99": "s", "Errors": "percentunit",
                 "DB time per req": "s", "In progress": "short", "SQL per req": "short"}
        self.panels.append({
            "id": self._id(), "type": "table", "title": title, "description": description,
            "gridPos": self._place(width, height),
            "datasource": {"type": "prometheus", "uid": "prometheus"},
            "fieldConfig": {"defaults": {"decimals": 2}, "overrides": [
                {"matcher": {"id": "byName", "options": name},
                 "properties": [{"id": "unit", "value": unit}]} for name, unit in units.items()
            ]},
            "options": {"showHeader": True, "sortBy": [{"displayName": "Req/s", "desc": True}]},
            "transformations": [
                {"id": "joinByField", "options": {"byField": join_on, "mode": "outer"}},
                {"id": "organize", "options": {
                    "excludeByName": {"Time": True, **{f"Time {i}": True for i in range(1, 12)}},
                    "renameByName": {f"Value #{chr(65 + i)}": label for i, (_, label) in enumerate(targets)},
                }},
            ],
            "targets": [{"refId": chr(65 + i), "expr": expr, "instant": True, "format": "table"}
                        for i, (expr, _) in enumerate(targets)],
        })


def build() -> dict:
    d = Layout()
    rate_window = "1m"
    by_feature = f'sum by (feature) (rate(medha_http_requests_total{{feature!="unmatched"}}[{rate_window}]))'
    q = "histogram_quantile({q}, sum by (le, feature) (rate(medha_http_request_duration_seconds_bucket[1m])))"
    errors = (
        'sum by (feature) (rate(medha_http_requests_total{{status=~"5..|429"}}[1m])) '
        "/ sum by (feature) (rate(medha_http_requests_total[1m]))"
    )

    d.row("Right now")
    d.stat("Schools online", "locust_schools", description="At this run's assumed users per school.")
    d.stat("Simulated users", "locust_users")
    d.stat("Requests/s", "sum(rate(medha_http_requests_total[1m]))", unit="reqps")
    d.stat("Failing (5xx)",
           'sum(rate(medha_http_requests_total{status=~"5.."}[1m])) / sum(rate(medha_http_requests_total[1m]))',
           unit="percentunit", thresholds=[(0.005, "orange"), (0.02, "red")])
    d.stat("DB pool in use",
           "sum(medha_db_pool_checked_out) / sum(medha_db_pool_capacity)", unit="percentunit",
           thresholds=[(0.7, "orange"), (0.95, "red")],
           description="100% is where the freeze starts. Every other request waits for a connection.")
    d.stat("Event loop blocked", "max(medha_event_loop_stalled_seconds)", unit="s",
           thresholds=[(0.5, "orange"), (2, "red")],
           description="Seconds since the event loop last ran. While high, every request in that worker waits.")

    d.row("Each feature")
    d.table("Throughput and speed by feature", [
        (by_feature, "Req/s"),
        (q.format(q=0.5), "p50"),
        (q.format(q=0.95), "p95"),
        (q.format(q=0.99), "p99"),
        (errors, "Errors"),
        ("sum by (feature) (medha_http_in_progress)", "In progress"),
        ("sum by (feature) (rate(medha_db_query_seconds_total[1m])) / sum by (feature) (rate(medha_http_requests_total[1m]))",
         "DB time per req"),
        ("sum by (feature) (rate(medha_db_queries_total[1m])) / sum by (feature) (rate(medha_http_requests_total[1m]))",
         "SQL per req"),
    ], description="Refreshes live. Errors counts 5xx and 429.")
    d.chart("Requests/s by feature", [(by_feature, "{{feature}}")], unit="reqps", width=12, stacked=True)
    d.chart("p95 by feature", [(q.format(q=0.95), "{{feature}}")], unit="s", width=12)
    d.chart("Failures/s by feature and status",
            [('sum by (feature, status) (rate(medha_http_requests_total{status=~"5..|429"}[1m]))',
              "{{feature}} {{status}}")], unit="reqps", width=12)
    d.chart("Slowest routes (p95)", [(
        "topk(10, histogram_quantile(0.95, sum by (le, route) (rate(medha_http_request_duration_seconds_bucket[1m]))))",
        "{{route}}")], unit="s", width=12)

    d.row("LLM streaming")
    d.chart("Open streams", [("sum by (feature) (medha_sse_active)", "{{feature}}"),
                             ("mock_llm_active_streams", "at the fake LLM")], width=8)
    d.chart("Time to first event, p95",
            [('histogram_quantile(0.95, sum by (le, feature) (rate(medha_http_first_byte_seconds_bucket'
              '{feature=~"ask_medha|generation|tutor|english"}[1m])))', "{{feature}}")], unit="s", width=8,
            description="Includes the fake LLM's own delay, so watch how far it climbs above that baseline.")
    d.chart("Fake LLM calls/s", [("sum by (shape) (rate(mock_llm_calls_total[1m]))", "{{shape}}")],
            unit="reqps", width=8,
            description="Far fewer calls than generation requests means the cache is doing its job.")

    d.row("Why it is slow: the backend")
    d.chart("Database connections", [
        ("sum(medha_db_pool_checked_out)", "in use"),
        ("sum(medha_db_pool_capacity)", "limit"),
    ], width=6)
    d.chart("Database slots (the deadlock guard)", [
        ("sum(medha_db_slots_in_use)", "in use"),
        ("sum(medha_db_slots_waiting)", "queued"),
        ("sum(medha_db_slots_limit)", "limit"),
    ], width=6, description="Requests queue here, cheaply, instead of blocking threads. A long queue means more capacity would help.")
    d.chart("503s from waiting too long", [("sum(rate(medha_db_slots_rejected_total[1m]))", "rejected/s")],
            unit="reqps", width=6)
    d.chart("Waiting for a connection, p95",
            [("histogram_quantile(0.95, sum by (le) (rate(medha_db_pool_wait_seconds_bucket[1m])))", "p95")],
            unit="s", width=6)
    d.chart("Connection timeouts/s", [("sum(rate(medha_db_pool_timeouts_total[1m]))", "gave up after 30s")],
            unit="reqps", width=6)
    d.chart("Threads", [
        ("sum(medha_threadpool_busy)", "busy"),
        ("sum(medha_threadpool_waiting)", "queued"),
        ("sum(medha_threadpool_capacity)", "limit"),
    ], width=6, description="Sync routes run here. Queued and limit both high means requests are stuck behind threads.")
    d.chart("Event loop blocked, by worker", [("medha_event_loop_stalled_seconds", "pid {{pid}}")], unit="s", width=6)
    d.chart("Event loop lag, p99",
            [("histogram_quantile(0.99, sum by (le) (rate(medha_event_loop_lag_seconds_bucket[1m])))", "p99")],
            unit="s", width=6)
    d.chart("CPU per worker", [("rate(medha_worker_cpu_seconds[1m])", "pid {{pid}}")],
            width=6, description="1.0 is one full core. A worker at 1.0 cannot go faster, however many cores are free.")
    d.chart("Memory per worker (peak)", [("medha_worker_rss_bytes", "pid {{pid}}")], unit="bytes", width=6)

    d.row("Why it is slow: the database")
    d.chart("Connections by state", [
        ('sum by (state) (pg_stat_activity_count{datname=~".*loadtest.*"})', "{{state}}")],
        width=6, description="'idle in transaction' holds a connection while doing nothing, e.g. during an LLM call.")
    d.chart("Transactions/s", [('sum(rate(pg_stat_database_xact_commit{datname=~".*loadtest.*"}[1m]))', "commits"),
                               ('sum(rate(pg_stat_database_xact_rollback{datname=~".*loadtest.*"}[1m]))', "rollbacks")],
            width=6)
    d.chart("Slowest statements by total time", [(
        "topk(8, sum by (queryid) (rate(pg_stat_statements_seconds_total[1m])))", "{{queryid}}")],
        unit="s", width=6, description="Seconds of database time per second. Full SQL is in the run's saved report.")
    d.chart("Locks and deadlocks", [
        ("sum by (mode) (pg_locks_count{mode=~\"exclusivelock|rowexclusivelock|accessexclusivelock\"})", "{{mode}}"),
        ('sum(rate(pg_stat_database_deadlocks{datname=~".*loadtest.*"}[1m]))', "deadlocks/s")], width=6)

    d.row("The machines")
    cpu = '1 - avg by (instance) (rate(node_cpu_seconds_total{mode="idle"}[1m]))'
    d.chart("CPU busy", [(cpu, "{{instance}}")], unit="percentunit", width=6,
            description="If the load generator stays high, the test itself is the limit and the result is not valid.")
    d.chart("Memory used", [(
        "1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes", "{{instance}}")],
        unit="percentunit", width=6)
    d.chart("Disk write latency", [(
        'rate(node_disk_write_time_seconds_total{device="vda"}[1m]) / rate(node_disk_writes_completed_total{device="vda"}[1m])',
        "{{instance}}")], unit="s", width=6)
    d.chart("Network received", [(
        'rate(node_network_receive_bytes_total{device="ens3"}[1m])', "{{instance}}")], unit="Bps", width=6)

    d.row("Load generator")
    d.chart("Failures/s as users see them", [('topk(10, rate(locust_failures_total{name!="Aggregated"}[1m]))', "{{name}}")],
            unit="reqps", width=12)
    d.chart("p95 as users see it, slowest", [('topk(10, locust_response_p95_ms{name!="Aggregated"})', "{{name}}")],
            unit="ms", width=12)

    return {
        "uid": "medha-load",
        "title": "Medha load test",
        "tags": ["medha"],
        "timezone": "browser",
        "schemaVersion": 39,
        "refresh": "5s",
        "time": {"from": "now-15m", "to": "now"},
        "timepicker": {"refresh_intervals": ["5s", "10s", "30s", "1m"]},
        "panels": d.panels,
    }


if __name__ == "__main__":
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(build(), indent=1), encoding="utf-8")
    print(f"wrote {OUT}")
