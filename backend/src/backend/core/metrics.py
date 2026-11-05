"""Prometheus metrics for load testing. Off unless METRICS_ENABLED=true.

`prometheus-client` lives in the `loadtest` dependency group and is imported
only by `install()`, so a normal deployment never loads it. Requires
PROMETHEUS_MULTIPROC_DIR: every worker writes to that directory and the separate
exporter (`backend/loadtest/exporter.py`) reads it.

The exporter is a separate process on purpose. An endpoint inside this app could
not answer during the failures we most want to watch: a blocked event loop stops
it directly, and a sync route would also stop when the threadpool is exhausted.
For the same reason the pool and thread gauges are sampled from a plain thread
rather than the event loop.

Per-feature numbers come from `feature_for()`, which groups route templates into
the product's features. Labels use the route template, never the raw path, so
UUIDs in URLs cannot create unbounded series.
"""

import asyncio
import contextvars
import logging
import os
import resource
import threading
import time
from collections.abc import Callable

logger = logging.getLogger("backend.metrics")

# Longest prefix wins, so "/report-card/omr" beats "/report-card".
_FEATURE_PREFIXES: dict[str, str] = {
    "/absence-calls": "attendance",
    "/admin": "admin",
    "/ask": "ask_medha",
    "/attendance": "attendance",
    "/auth": "auth",
    "/chat": "ask_medha",
    "/curriculum": "reference",
    "/day-timetable": "timetable",
    "/english": "english",
    "/fees": "fees",
    "/generate": "generation",
    "/generations": "generation",
    "/homework": "homework",
    "/library": "library",
    "/modules": "library",
    "/notes": "notes_practice",
    "/notifications": "notifications",
    "/onboarding": "profile",
    "/practice": "notes_practice",
    "/principal": "principal",
    "/principal/absences": "cover",
    "/principal/day": "cover",
    "/principal/period-slots": "timetable",
    "/principal/school": "profile",
    "/principal/substitutions": "cover",
    "/principal/timetables": "timetable",
    "/profile": "profile",
    "/reference": "reference",
    "/report-card": "report_card",
    "/report-card/omr": "omr",
    "/school": "profile",
    "/schools": "reference",
    # FastAPI reports the Ask router's routes without the "/ask" or "/chat" it is mounted
    # under (app.py), and no other router has this prefix.
    "/sessions": "ask_medha",
    "/speech": "voice",
    "/student": "auth",
    "/students": "report_card",
    "/teacher": "teacher",
    "/timetable": "timetable",
    "/tools": "tools",
    "/tutor": "tutor",
    "/work-updates": "work_updates",
}
_SORTED_PREFIXES = sorted(_FEATURE_PREFIXES, key=len, reverse=True)


def feature_for(route: str) -> str:
    """Map a route template such as `/tutor/sessions/{session_id}` to a feature."""
    for prefix in _SORTED_PREFIXES:
        if route == prefix or route.startswith(prefix + "/"):
            return _FEATURE_PREFIXES[prefix]
    return "other"


class _RequestStats:
    """Per-request state. SQLAlchemy's events run in worker threads that inherit
    this via the copied context, so they add to the same object."""

    __slots__ = ("db_queries", "db_seconds", "scope")

    def __init__(self, scope: dict) -> None:
        self.scope = scope
        self.db_seconds = 0.0
        self.db_queries = 0


_current: contextvars.ContextVar[_RequestStats | None] = contextvars.ContextVar(
    "medha_metrics_request", default=None
)


def _route_of(scope: dict) -> str:
    route = scope.get("route")
    return getattr(route, "path", None) or "unmatched"


_LATENCY_BUCKETS = (
    0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 3, 5, 8, 12, 20, 30, 45, 60, 120,
)


class _Metrics:
    def __init__(self) -> None:
        from prometheus_client import Counter, Gauge, Histogram

        labels = ("feature", "route", "method")
        self.requests = Counter(
            "medha_http_requests", "Finished requests.", (*labels, "status")
        )
        self.duration = Histogram(
            "medha_http_request_duration_seconds",
            "Time to the last byte of the response.",
            labels,
            buckets=_LATENCY_BUCKETS,
        )
        self.first_byte = Histogram(
            "medha_http_first_byte_seconds",
            "Time to the first body byte (for SSE, the first event).",
            labels,
            buckets=_LATENCY_BUCKETS,
        )
        self.in_progress = Gauge(
            "medha_http_in_progress", "Requests being handled.", ("feature",),
            multiprocess_mode="livesum",
        )
        self.sse_active = Gauge(
            "medha_sse_active", "Open SSE responses.", ("feature",),
            multiprocess_mode="livesum",
        )
        self.db_seconds = Counter(
            "medha_db_query_seconds", "Time spent executing SQL.", ("feature", "route")
        )
        self.db_queries = Counter(
            "medha_db_queries", "SQL statements executed.", ("feature", "route")
        )
        self.pool_wait = Histogram(
            "medha_db_pool_wait_seconds",
            "Time spent waiting for a pooled connection.",
            buckets=(0.001, 0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 30, 45),
        )
        self.pool_timeouts = Counter(
            "medha_db_pool_timeouts", "Pool checkouts that gave up waiting."
        )
        self.pool_checked_out = Gauge(
            "medha_db_pool_checked_out", "Connections in use.", multiprocess_mode="livesum"
        )
        self.pool_capacity = Gauge(
            "medha_db_pool_capacity", "Most connections the pool can open.",
            multiprocess_mode="livesum",
        )
        self.loop_lag = Histogram(
            "medha_event_loop_lag_seconds",
            "How late a scheduled event-loop wakeup ran.",
            buckets=(0.001, 0.005, 0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30),
        )
        self.loop_stalled = Gauge(
            "medha_event_loop_stalled_seconds",
            "Seconds since the event loop last ran the heartbeat. Grows while blocked.",
            multiprocess_mode="liveall",
        )
        self.slots_in_use = Gauge(
            "medha_db_slots_in_use", "Requests holding a database slot.", multiprocess_mode="livesum"
        )
        self.slots_waiting = Gauge(
            "medha_db_slots_waiting", "Requests queued for a database slot.", multiprocess_mode="livesum"
        )
        self.slots_limit = Gauge(
            "medha_db_slots_limit", "Database slots available.", multiprocess_mode="livesum"
        )
        self.slots_rejected = Counter(
            "medha_db_slots_rejected", "Requests answered 503 after waiting too long for a slot."
        )
        self.threads_busy = Gauge(
            "medha_threadpool_busy", "Busy threadpool tokens.", multiprocess_mode="livesum"
        )
        self.threads_waiting = Gauge(
            "medha_threadpool_waiting", "Calls queued for a thread.", multiprocess_mode="livesum"
        )
        self.threads_capacity = Gauge(
            "medha_threadpool_capacity", "Threadpool size.", multiprocess_mode="livesum"
        )
        self.worker_cpu = Gauge(
            "medha_worker_cpu_seconds", "CPU used by this worker.", multiprocess_mode="liveall"
        )
        self.worker_rss = Gauge(
            "medha_worker_rss_bytes", "Peak memory of this worker.", multiprocess_mode="liveall"
        )


class MetricsMiddleware:
    """Pure ASGI, so SSE responses pass through unbuffered."""

    def __init__(self, app, metrics: _Metrics, on_first_request: Callable[[], None]) -> None:
        self.app = app
        self.metrics = metrics
        self._on_first_request = on_first_request
        self._started = False

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if not self._started:
            self._started = True
            self._on_first_request()

        metrics = self.metrics
        stats = _RequestStats(scope)
        token = _current.set(stats)
        started = time.perf_counter()
        status = 500
        first_byte: float | None = None
        is_sse = False
        # The route is unknown until routing runs, so requests in progress are
        # tracked per feature from the path and re-resolved at the end.
        in_progress_feature = feature_for(scope["path"])
        metrics.in_progress.labels(in_progress_feature).inc()

        async def tracking_send(message) -> None:
            nonlocal status, first_byte, is_sse
            if message["type"] == "http.response.start":
                status = message["status"]
                headers = dict(message.get("headers") or [])
                is_sse = headers.get(b"content-type", b"").startswith(b"text/event-stream")
                if is_sse:
                    metrics.sse_active.labels(in_progress_feature).inc()
            elif (
                message["type"] == "http.response.body"
                and first_byte is None
                and message.get("body")
            ):
                first_byte = time.perf_counter() - started
            await send(message)

        try:
            await self.app(scope, receive, tracking_send)
        finally:
            elapsed = time.perf_counter() - started
            route = _route_of(scope)
            feature = feature_for(route) if route != "unmatched" else "unmatched"
            method = scope["method"]
            metrics.in_progress.labels(in_progress_feature).dec()
            if is_sse:
                metrics.sse_active.labels(in_progress_feature).dec()
            metrics.requests.labels(feature, route, method, str(status)).inc()
            metrics.duration.labels(feature, route, method).observe(elapsed)
            if first_byte is not None:
                metrics.first_byte.labels(feature, route, method).observe(first_byte)
            if stats.db_queries:
                metrics.db_seconds.labels(feature, route).inc(stats.db_seconds)
                metrics.db_queries.labels(feature, route).inc(stats.db_queries)
            _current.reset(token)


def _instrument_engine(engine, metrics: _Metrics) -> None:
    from sqlalchemy import event

    @event.listens_for(engine, "before_cursor_execute")
    def _before(conn, cursor, statement, parameters, context, executemany) -> None:
        context._medha_started = time.perf_counter()

    @event.listens_for(engine, "after_cursor_execute")
    def _after(conn, cursor, statement, parameters, context, executemany) -> None:
        stats = _current.get()
        if stats is not None:
            stats.db_seconds += time.perf_counter() - context._medha_started
            stats.db_queries += 1

    # SQLAlchemy has no event for "waiting on the pool", which is the number that
    # matters most here, so time the pool's internal checkout directly.
    pool = engine.pool
    original_do_get = pool._do_get

    def timed_do_get():
        started = time.perf_counter()
        try:
            return original_do_get()
        except Exception as exc:
            if type(exc).__name__ == "TimeoutError":
                metrics.pool_timeouts.inc()
            raise
        finally:
            metrics.pool_wait.observe(time.perf_counter() - started)

    pool._do_get = timed_do_get


class _Sampler:
    """Publishes process-wide gauges once a second."""

    def __init__(self, metrics: _Metrics, engine) -> None:
        self.metrics = metrics
        self.engine = engine
        self.last_beat = time.monotonic()

    def start_thread(self) -> None:
        threading.Thread(target=self._sample_forever, name="medha-metrics", daemon=True).start()

    def _sample_forever(self) -> None:
        from backend.db.session import SlotStats, slot_limit

        pool = self.engine.pool
        capacity = pool.size() + max(getattr(pool, "_max_overflow", 0), 0)
        self.metrics.pool_capacity.set(capacity)
        self.metrics.slots_limit.set(slot_limit())
        rejected_seen = 0
        while True:
            try:
                self.metrics.pool_checked_out.set(pool.checkedout())
                self.metrics.slots_in_use.set(SlotStats.in_use)
                self.metrics.slots_waiting.set(SlotStats.waiting)
                self.metrics.slots_rejected.inc(SlotStats.rejected - rejected_seen)
                rejected_seen = SlotStats.rejected
                self.metrics.loop_stalled.set(time.monotonic() - self.last_beat)
                self.metrics.worker_cpu.set(time.process_time())
                # ru_maxrss is KB on Linux and bytes on macOS; only Linux is deployed to.
                self.metrics.worker_rss.set(
                    resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
                )
            except Exception:
                logger.exception("metrics sampler failed")
            time.sleep(1)

    async def heartbeat(self) -> None:
        import anyio.to_thread

        interval = 0.25
        while True:
            scheduled = time.monotonic() + interval
            await asyncio.sleep(interval)
            now = time.monotonic()
            self.last_beat = now
            self.metrics.loop_lag.observe(max(now - scheduled, 0))
            limiter = anyio.to_thread.current_default_thread_limiter()
            self.metrics.threads_capacity.set(limiter.total_tokens)
            self.metrics.threads_busy.set(limiter.borrowed_tokens)
            self.metrics.threads_waiting.set(limiter.statistics().tasks_waiting)


def install(app) -> None:
    """Add the middleware and start sampling. Called once at import by app.py."""
    if not os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        raise RuntimeError(
            "METRICS_ENABLED=true needs PROMETHEUS_MULTIPROC_DIR set to a writable directory."
        )
    from backend.db.session import engine

    metrics = _Metrics()
    _instrument_engine(engine, metrics)
    sampler = _Sampler(metrics, engine)
    sampler.start_thread()

    background: list[asyncio.Task] = []

    def start_heartbeat() -> None:
        # Hold a reference, or the task can be garbage-collected mid-run.
        background.append(asyncio.get_running_loop().create_task(sampler.heartbeat()))

    app.add_middleware(MetricsMiddleware, metrics=metrics, on_first_request=start_heartbeat)
    logger.info("load-test metrics enabled")
