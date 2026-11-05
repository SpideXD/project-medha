"""Load-test metrics (core/metrics.py). Needs the `loadtest` dependency group but
no database: the routes exercised here never open a connection.

The real app runs in a subprocess because metrics are switched on at import time
and other tests import `backend.app` with them off.
"""

import os
import subprocess
import sys
import tempfile
import threading
import unittest

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "../src"))
sys.path.insert(0, SRC)

os.environ.setdefault("JWT_SECRET_KEY", "x" * 40)

from unittest import mock  # noqa: E402

from backend.core.metrics import _Sampler, feature_for  # noqa: E402


class StallNoteTests(unittest.TestCase):
    def _sampler(self) -> _Sampler:
        sampler = _Sampler(mock.Mock(), mock.Mock())
        sampler.loop_thread = threading.get_ident()
        return sampler

    def test_records_where_the_loop_is_stuck(self):
        with tempfile.NamedTemporaryFile("r", suffix=".log") as log:
            with mock.patch.dict(os.environ, {"MEDHA_STALL_LOG": log.name}):
                self._sampler().note_stall(2.5)
            written = log.read()
        self.assertIn("loop stuck for 2.5s", written)
        self.assertIn("test_records_where_the_loop_is_stuck", written)

    def test_writes_nothing_for_a_short_stall_or_without_a_file(self):
        with tempfile.NamedTemporaryFile("r", suffix=".log") as log:
            with mock.patch.dict(os.environ, {"MEDHA_STALL_LOG": log.name}):
                self._sampler().note_stall(0.5)
            self.assertEqual(log.read(), "")
        with mock.patch.dict(os.environ):
            os.environ.pop("MEDHA_STALL_LOG", None)
            self._sampler().note_stall(5.0)  # must not raise

    def test_a_worker_with_no_requests_yet_is_not_stalled(self):
        self.assertIsNone(_Sampler(mock.Mock(), mock.Mock()).last_beat)

    def test_notes_are_spaced_out(self):
        with tempfile.NamedTemporaryFile("r", suffix=".log") as log:
            with mock.patch.dict(os.environ, {"MEDHA_STALL_LOG": log.name}):
                sampler = self._sampler()
                sampler.note_stall(2.0)
                sampler.note_stall(3.0)
            self.assertEqual(log.read().count("--- pid"), 1)

_REAL_APP_SCRIPT = """
import time
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sse_starlette.sse import EventSourceResponse
from backend.app import app

@app.get("/homework/{item_id}/boom")
def boom(item_id: str):
    raise HTTPException(503, "nope")

@app.get("/tutor/{session_id}/stream")
async def stream(session_id: str):
    async def events():
        yield {"event": "token", "data": "hi"}
        yield {"event": "done", "data": "{}"}
    return EventSourceResponse(events())

# The context manager keeps one event loop alive across requests, as a real server does.
with TestClient(app) as client:
    assert client.get("/health").status_code == 200
    assert client.get("/api/health").status_code == 200
    assert client.get("/homework/11111111-1111-1111-1111-111111111111/boom").status_code == 503
    assert client.get("/tutor/22222222-2222-2222-2222-222222222222/stream").text.count("event:") == 2
    assert client.get("/no/such/route/33333333-3333-3333-3333-333333333333").status_code == 404
    time.sleep(1.5)  # let the sampler thread and the heartbeat publish

from prometheus_client import CollectorRegistry, generate_latest, multiprocess
registry = CollectorRegistry()
multiprocess.MultiProcessCollector(registry)
print(generate_latest(registry).decode())
"""


class FeatureMappingTests(unittest.TestCase):
    def test_longest_prefix_wins(self):
        self.assertEqual(feature_for("/report-card/omr/evaluate"), "omr")
        self.assertEqual(feature_for("/report-card/marks"), "report_card")
        self.assertEqual(feature_for("/principal/substitutions/bulk"), "cover")
        self.assertEqual(feature_for("/principal/timetables/{timetable_id}/grid"), "timetable")
        self.assertEqual(feature_for("/principal/stats"), "principal")

    def test_old_and_new_ask_paths_are_one_feature(self):
        self.assertEqual(feature_for("/chat/sessions"), "ask_medha")
        self.assertEqual(feature_for("/ask/sessions"), "ask_medha")
        # As the router really reports them at runtime, without the mount prefix.
        self.assertEqual(feature_for("/sessions/{session_id}/messages"), "ask_medha")

    def test_prefix_must_end_at_a_path_boundary(self):
        self.assertEqual(feature_for("/schools/search"), "reference")
        self.assertEqual(feature_for("/school"), "profile")
        self.assertEqual(feature_for("/studentsx"), "other")

    def test_every_real_route_is_assigned_a_feature(self):
        from backend.app import app

        unassigned = sorted(
            route.path
            for route in app.routes
            if getattr(route, "methods", None)
            and route.path not in ("/health", "/docs", "/redoc", "/openapi.json",
                                   "/docs/oauth2-redirect")
            and not route.path.startswith("/__test")  # added by test_middleware_headers
            and feature_for(route.path) == "other"
        )
        self.assertEqual(unassigned, [])


class RealAppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory() as multiproc_dir:
            env = {
                **os.environ,
                "METRICS_ENABLED": "true",
                "PROMETHEUS_MULTIPROC_DIR": multiproc_dir,
                "PYTHONPATH": SRC,
            }
            result = subprocess.run(
                [sys.executable, "-c", _REAL_APP_SCRIPT],
                env=env, capture_output=True, text=True, timeout=120,
            )
        if result.returncode != 0:
            raise AssertionError(result.stderr[-3000:])
        cls.output = result.stdout

    def test_counts_use_route_template_not_raw_path(self):
        self.assertIn(
            'medha_http_requests_total{feature="homework",method="GET",'
            'route="/homework/{item_id}/boom",status="503"} 1.0',
            self.output,
        )
        self.assertNotIn("11111111-1111", self.output)

    def test_api_prefix_resolves_to_same_route(self):
        self.assertIn(
            'medha_http_requests_total{feature="other",method="GET",'
            'route="/health",status="200"} 2.0',
            self.output,
        )

    def test_unknown_paths_cannot_create_new_series(self):
        self.assertIn(
            'medha_http_requests_total{feature="unmatched",method="GET",'
            'route="unmatched",status="404"} 1.0',
            self.output,
        )
        self.assertNotIn("33333333-3333", self.output)

    def test_sse_first_byte_recorded_and_gauge_returns_to_zero(self):
        self.assertIn(
            'medha_http_first_byte_seconds_count{feature="tutor",method="GET",'
            'route="/tutor/{session_id}/stream"} 1.0',
            self.output,
        )
        self.assertIn('medha_sse_active{feature="tutor"} 0.0', self.output)

    def test_sampler_publishes_pool_and_loop_gauges(self):
        self.assertIn("medha_db_pool_capacity 15.0", self.output)
        self.assertIn("medha_db_pool_checked_out 0.0", self.output)
        self.assertIn("medha_threadpool_capacity 40.0", self.output)
        self.assertIn("medha_event_loop_stalled_seconds", self.output)


class PoolInstrumentationTests(unittest.TestCase):
    def test_waits_and_timeouts_are_counted(self):
        code = """
import sqlalchemy as sa
from sqlalchemy.pool import QueuePool
from backend.core.metrics import _Metrics, _instrument_engine
from prometheus_client import CollectorRegistry, generate_latest, multiprocess

engine = sa.create_engine("sqlite://", poolclass=QueuePool, pool_size=1, max_overflow=0, pool_timeout=0.05)
_instrument_engine(engine, _Metrics())
held = engine.connect()
try:
    engine.connect()
except sa.exc.TimeoutError:
    pass
else:
    raise SystemExit("expected a pool timeout")
held.close()
registry = CollectorRegistry()
multiprocess.MultiProcessCollector(registry)
print(generate_latest(registry).decode())
"""
        with tempfile.TemporaryDirectory() as multiproc_dir:
            env = {**os.environ, "PROMETHEUS_MULTIPROC_DIR": multiproc_dir, "PYTHONPATH": SRC}
            result = subprocess.run(
                [sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=60
            )
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        self.assertIn("medha_db_pool_timeouts_total 1.0", result.stdout)
        self.assertIn("medha_db_pool_wait_seconds_count 2.0", result.stdout)


if __name__ == "__main__":
    unittest.main()
