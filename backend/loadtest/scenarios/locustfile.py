"""Simulated Medha users. Two ways to run it:

Whole backend, realistic mix (LT_SCENARIO=day). Size is in schools, the number that
decides what you can actually support:
    LT_PHASE=morning|school|evening   which part of the day
    LT_PROFILE=launch|full            today's locked screens, or everything unlocked

One feature alone (LT_SCENARIO=<feature>), to find that feature's own limit:
    see FEATURES below, or run with LT_SCENARIO=list

Load shape, in schools online at once (LT_SHAPE=steps|spike|steady):
    LT_SCHOOLS       steady / spike target, and the steps' first level
    LT_SCHOOLS_STEP, LT_STEP_SECONDS, LT_MAX_SCHOOLS   for steps
    LT_SPIKE_SECONDS how fast a spike arrives
    LT_RUN_SECONDS   total length for steady / spike

For a feature run the unit is users, not schools: LT_USERS, LT_USERS_STEP, LT_MAX_USERS.
"""

import io
import json
import os
import random
import time
import wave
from urllib.parse import urlparse

import gevent
from locust import HttpUser, LoadTestShape, events, task
from locust.exception import StopUser
from locust.runners import WorkerRunner
from prometheus_client import start_http_server
from prometheus_client.core import REGISTRY, CounterMetricFamily, GaugeMetricFamily

import flows

SCENARIO = os.environ.get("LT_SCENARIO", "day")
PHASE = os.environ.get("LT_PHASE", "school")
PROFILE = os.environ.get("LT_PROFILE", "launch")
SHAPE = os.environ.get("LT_SHAPE", "steady")
MANIFEST_PATH = os.environ.get("LT_MANIFEST", "/tmp/manifest.json")
OMR_FILE = os.environ.get(
    "LT_OMR_FILE", os.path.join(os.path.dirname(__file__), "..", "fixtures", "omr_sheet.pdf")
)
METRICS_PORT = int(os.environ.get("LT_METRICS_PORT", "9646"))
# Shortens human pauses. Only for the smoke check; real runs must keep them at 1.
SLEEP_SCALE = float(os.environ.get("LT_SLEEP_SCALE", "1"))

# Share of real sessions that start with a password login rather than an existing
# cookie. Placeholder until production `auth_sessions` shows the real figure.
LOGIN_SHARE = float(os.environ.get("LT_LOGIN_SHARE", "0.05"))

# How many people are online per school at the peak of each phase. These are
# ASSUMPTIONS, and the "X schools" result is only as true as they are. Replace them
# with counts from production.
ONLINE_PER_SCHOOL = {
    "morning": {"teacher": 8, "student": 5, "principal": 1},
    "school": {"teacher": 5, "student": 10, "principal": 1},
    "evening": {"teacher": 2, "student": 30, "principal": 0},
}


def _assert_safe_host(host: str) -> None:
    hostname = (urlparse(host).hostname or "").lower()
    for marker in ("onrender.com", "projectmedha", "vercel.app", "neon.tech"):
        if marker in hostname:
            raise SystemExit(f"refusing to load test {hostname}: this looks like a real environment.")


with open(MANIFEST_PATH, encoding="utf-8") as handle:
    MANIFEST = json.load(handle)
with open(OMR_FILE, "rb") as handle:
    MANIFEST["_omr_sheet"] = handle.read()


def _spoken_clip(seconds: float = 4.0) -> bytes:
    """A voice clip of realistic size for the transcribe upload. The fake STT ignores
    its content; the backend only checks the format and length."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as clip:
        clip.setnchannels(1)
        clip.setsampwidth(2)
        clip.setframerate(16000)
        clip.writeframes(b"\x00\x01" * int(16000 * seconds))
    return buffer.getvalue()


MANIFEST["_wav_clip"] = _spoken_clip()


def _build_pool(role: str) -> list[tuple[dict, dict]]:
    """(person, school) for everyone in a role. Each person is used by one virtual
    user at a time, because the per-person rate limits depend on it."""
    if role == "teacher":
        people = [(t, s) for s in MANIFEST["schools"] for t in s["teachers"]]
    elif role == "student":
        people = [(st, s) for s in MANIFEST["schools"] for st in s["students"]]
    elif role == "principal":
        people = [(s["principal"], s) for s in MANIFEST["schools"]]
    else:
        people = [(MANIFEST["admin"], {"ip": "10.255.255.1", "sections": []})]
    # Same order in every worker process, so workers can take non-overlapping slices.
    random.Random(42).shuffle(people)
    return people


class _Allocator:
    """Hands out people, wrapping if a run asks for more users than were seeded."""

    def __init__(self) -> None:
        self.pools: dict[str, list] = {}
        self.next_index: dict[str, int] = {}
        self.runner = None
        self.worker_count = max(int(os.environ.get("LT_WORKERS", "1")), 1)

    def take(self, role: str) -> tuple[dict, dict]:
        # Not setdefault: that would rebuild the whole pool for every user it hands out.
        if role not in self.pools:
            self.pools[role] = _build_pool(role)
        pool = self.pools[role]
        position = self.next_index.get(role, 0)
        self.next_index[role] = position + 1
        # The master assigns worker_index after start-up, so read it here, not at init.
        worker_index = max(getattr(self.runner, "worker_index", 0), 0)
        return pool[(worker_index + position * self.worker_count) % len(pool)]


ALLOCATOR = _Allocator()


class MedhaUser(HttpUser):
    abstract = True
    role = "teacher"

    def on_start(self) -> None:
        self.person, self.school = ALLOCATOR.take(self.role)
        self.school_ip = self.school["ip"]
        self.manifest = {**MANIFEST, "_school_sections": self.school.get("sections", [])}
        self.browser = flows.Browser(self)
        self.pollers: list[gevent.Greenlet] = []
        self.browser.refresh_cookie = self.person.get("refresh")
        self.sign_in()
        self.load_shell()
        if SCENARIO == "day":
            self.pollers.append(gevent.spawn(self._poll_bell))

    def on_stop(self) -> None:
        gevent.killall(self.pollers, block=False)

    def sign_in(self) -> None:
        if random.random() < LOGIN_SHARE or not self.browser.refresh():
            if not self._password_login():
                # Without a token, every later call would only fail and hide the real signal.
                raise StopUser()

    def _password_login(self) -> bool:
        raise NotImplementedError

    def load_shell(self) -> None:
        raise NotImplementedError

    def sleep_like_a_person(self, low: float, high: float) -> None:
        gevent.sleep(random.uniform(low, high) * SLEEP_SCALE)

    def _poll_bell(self) -> None:
        """notification-bell.tsx: every 30 seconds for as long as anyone is signed in."""
        gevent.sleep(random.uniform(0, 30))
        while True:
            self.browser.get("/notifications/unread-count")
            if self.role == "principal":  # two bells on this shell
                self.browser.get("/notifications/unread-count")
            gevent.sleep(30)

    def wait_time(self) -> float:
        # Time spent reading a screen before moving on.
        return random.uniform(*_READ_SECONDS)


_READ_SECONDS = (20, 60) if SCENARIO == "day" else (float(os.environ.get("LT_THINK_MIN", "1")),
                                                     float(os.environ.get("LT_THINK_MAX", "3")))


class TeacherUser(MedhaUser):
    abstract = True
    role = "teacher"

    def _password_login(self) -> bool:
        return self.browser.login_phone(self.person["phone"], MANIFEST["password"], "teacher")

    def load_shell(self) -> None:
        assignment = self.person["sections"][0] if self.person["sections"] else None
        chapters = (
            MANIFEST["chapters"][f"{assignment['grade_id']}|{assignment['subject_id']}"] if assignment else []
        )
        flows.teacher_shell(self.browser, self.person, chapters)


class StudentUser(MedhaUser):
    abstract = True
    role = "student"

    def _password_login(self) -> bool:
        return self.browser.login_phone(self.person["phone"], MANIFEST["password"], "student", self.person["id"])

    def load_shell(self) -> None:
        flows.student_shell(self.browser, self.person)


class PrincipalUser(MedhaUser):
    abstract = True
    role = "principal"

    def _password_login(self) -> bool:
        return self.browser.login_email(self.person["email"], MANIFEST["password"], "principal")

    def load_shell(self) -> None:
        flows.principal_shell(self.browser)


class AdminUser(MedhaUser):
    abstract = True
    role = "admin"

    def _password_login(self) -> bool:
        return self.browser.login_email(self.person["email"], MANIFEST["password"], "admin")

    def load_shell(self) -> None:
        self.browser.get("/auth/me")
        self.browser.get("/admin/stats")
        self.browser.get("/notifications/unread-count")


def _weighted(user, table: list[tuple[int, object]]) -> None:
    """Run one flow, chosen by weight."""
    weights = [weight for weight, _ in table]
    flow = random.choices([f for _, f in table], weights=weights)[0]
    flow(user.browser, user.person, user.manifest)


def _write_allowed() -> bool:
    return PROFILE == "full"


def _teacher_mix() -> list[tuple[int, object]]:
    submit = _write_allowed()
    attendance = lambda b, p, m: flows.attendance(b, p, m, submit=submit)  # noqa: E731
    homework = lambda b, p, m: flows.homework_teacher(b, p, m, submit=submit)  # noqa: E731
    if PHASE == "morning":
        return [
            (50, attendance), (10, flows.dashboard_page), (10, flows.timetable_teacher),
            (10, flows.ask_medha), (10, flows.generate_content), (10, flows.locked_previews),
        ]
    if PHASE == "evening":
        return [
            (40, flows.work_update), (20, flows.ask_medha), (20, flows.generate_content),
            (10, flows.history_page), (10, flows.dashboard_page),
        ]
    mix = [
        (25, flows.ask_medha), (20, flows.generate_content), (10, flows.history_page),
        (10, flows.dashboard_page), (10, flows.work_update), (10, flows.timetable_teacher),
        (5, flows.report_card_teacher), (5, homework), (5, flows.locked_previews),
    ]
    if submit:
        mix += [(3, flows.omr_evaluate), (2, flows.announce_class), (2, flows.export_download)]
    return mix


def _student_mix() -> list[tuple[int, object]]:
    if PHASE == "evening":
        return [
            (35, flows.tutor_chat), (20, flows.homework_student), (15, flows.english_practice),
            (10, flows.student_records), (10, flows.student_content), (10, flows.student_updates),
        ]
    return [
        (20, flows.homework_student), (20, flows.student_records), (20, flows.student_updates),
        (20, flows.student_content), (15, flows.tutor_chat), (5, flows.english_practice),
    ]


_PRINCIPAL_MIX = [
    (40, flows.principal_dashboard), (20, flows.principal_staff_and_classes),
    (15, flows.principal_timetable), (10, flows.principal_cover), (10, flows.principal_work_review),
    (5, flows.principal_search),
]


# LT_SCENARIO=<name> runs just this feature, always with writes allowed.
def _submit(flow):
    return lambda b, p, m: flow(b, p, m, submit=True)


def _ask_one_turn(b, p, m):
    return flows.ask_medha(b, p, m, turns=1)


FEATURES: dict[str, tuple[type, object]] = {
    "auth_refresh": (TeacherUser, lambda b, p, m: (b.refresh(), b.get("/auth/me"))),
    "auth_login": (TeacherUser, lambda b, p, m: b.login_phone(p["phone"], MANIFEST["password"], "teacher")),
    "ask_medha": (TeacherUser, _ask_one_turn),
    "generation": (TeacherUser, flows.generate_content),
    "attendance": (TeacherUser, _submit(flows.attendance)),
    "work_update": (TeacherUser, flows.work_update),
    "homework": (TeacherUser, _submit(flows.homework_teacher)),
    "report_card": (TeacherUser, _submit(flows.report_card_teacher)),
    "omr": (TeacherUser, flows.omr_evaluate),
    "exports": (TeacherUser, flows.export_download),
    "announce": (TeacherUser, flows.announce_class),
    "history": (TeacherUser, flows.history_page),
    "timetable": (TeacherUser, flows.timetable_teacher),
    "tutor": (StudentUser, flows.tutor_chat),
    "english": (StudentUser, flows.english_practice),
    "voice": (StudentUser, flows.voice_turn),
    "student_homework": (StudentUser, flows.homework_student),
    "student_records": (StudentUser, flows.student_records),
    "student_content": (StudentUser, flows.student_content),
    "principal_dashboard": (PrincipalUser, flows.principal_dashboard),
    "principal_admin_pages": (PrincipalUser, flows.principal_staff_and_classes),
    "principal_timetable": (PrincipalUser, flows.principal_timetable),
    "principal_cover": (PrincipalUser, flows.principal_cover),
    "principal_search": (PrincipalUser, flows.principal_search),
    "admin": (AdminUser, flows.admin_pages),
    "public_reference": (TeacherUser, flows.public_reference),
    "polling": (TeacherUser, lambda b, p, m: b.get("/notifications/unread-count")),
}

if SCENARIO == "list":
    raise SystemExit("features: " + ", ".join(sorted(FEATURES)))


def _define_smoke_users() -> list[type]:
    """One person per role running every flow in turn, to catch wrong requests. A
    failure here means the scenario, or the backend, is wrong; it says nothing about load."""
    classes: list[type] = []
    for base in (TeacherUser, StudentUser, PrincipalUser, AdminUser):
        flows_for_role = [(name, flow) for name, (role, flow) in sorted(FEATURES.items())
                          if role is base and name != "auth_login"]

        class SmokeUser(base):
            weight = 1
            role_flows = flows_for_role

            @task
            def act(self):
                for _, flow in self.role_flows:
                    flow(self.browser, self.person, self.manifest)

        SmokeUser.__name__ = f"Smoke{base.__name__}"
        classes.append(SmokeUser)
    return classes


def _define_users() -> list[type]:
    if SCENARIO == "smoke":
        return _define_smoke_users()
    if SCENARIO == "day":
        online = ONLINE_PER_SCHOOL[PHASE]

        class DayTeacher(TeacherUser):
            weight = online["teacher"]

            @task
            def act(self):
                _weighted(self, _teacher_mix())

        class DayStudent(StudentUser):
            weight = online["student"]

            @task
            def act(self):
                _weighted(self, _student_mix())

        classes: list[type] = [DayTeacher, DayStudent]
        if online["principal"]:
            class DayPrincipal(PrincipalUser):
                weight = online["principal"]

                @task
                def act(self):
                    _weighted(self, _PRINCIPAL_MIX)

            classes.append(DayPrincipal)
        return classes

    if SCENARIO not in FEATURES:
        raise SystemExit(f"unknown LT_SCENARIO {SCENARIO!r}; features: {', '.join(sorted(FEATURES))}")
    base, flow = FEATURES[SCENARIO]

    class FeatureUser(base):
        @task
        def act(self):
            flow(self.browser, self.person, self.manifest)

    return [FeatureUser]


# Locust discovers user classes from this module's globals, so each one must appear
# exactly once: a leftover loop variable would be found as a duplicate.
globals().update({user_class.__name__: user_class for user_class in _define_users()})


def _users_per_school() -> int:
    return sum(ONLINE_PER_SCHOOL[PHASE].values()) if SCENARIO == "day" else 1


class SchoolShape(LoadTestShape):
    """Sized in schools for a day run, and in users for a feature run."""

    def __init__(self) -> None:
        super().__init__()
        unit = _users_per_school()
        day = SCENARIO == "day"
        self.start = int(os.environ.get("LT_SCHOOLS" if day else "LT_USERS", "1")) * unit
        self.step = int(os.environ.get("LT_SCHOOLS_STEP" if day else "LT_USERS_STEP", "0")) * unit
        self.maximum = int(os.environ.get("LT_MAX_SCHOOLS" if day else "LT_MAX_USERS", "0")) * unit
        self.step_seconds = int(os.environ.get("LT_STEP_SECONDS", "300"))
        self.run_seconds = int(os.environ.get("LT_RUN_SECONDS", "900"))
        self.spike_seconds = int(os.environ.get("LT_SPIKE_SECONDS", "120"))

    def tick(self):
        elapsed = self.get_run_time()
        if SHAPE == "steps":
            level = self.start + self.step * int(elapsed // self.step_seconds)
            if level > self.maximum:
                return None
            # Arrive within the first tenth of the step, so most of it is measured at full size.
            return level, max(1, self.step / max(self.step_seconds * 0.1, 1))
        if elapsed >= self.run_seconds:
            return None
        if SHAPE == "spike":
            return self.start, max(1, self.start / self.spike_seconds)
        return self.start, max(1, self.start / 60)


class _LocustCollector:
    """Exposes Locust's live per-request numbers to Prometheus, from the master."""

    def __init__(self, environment) -> None:
        self.environment = environment

    def collect(self):
        stats = self.environment.stats
        rps = GaugeMetricFamily("locust_requests_per_second", "Current requests per second.", labels=["name"])
        p50 = GaugeMetricFamily("locust_response_p50_ms", "Recent median.", labels=["name"])
        p95 = GaugeMetricFamily("locust_response_p95_ms", "Recent p95.", labels=["name"])
        requests = CounterMetricFamily("locust_requests", "Requests seen.", labels=["name"])
        failures = CounterMetricFamily("locust_failures", "Failures seen.", labels=["name"])
        for entry in [*stats.entries.values(), stats.total]:
            rps.add_metric([entry.name], entry.current_rps)
            p50.add_metric([entry.name], entry.get_current_response_time_percentile(0.5) or 0)
            p95.add_metric([entry.name], entry.get_current_response_time_percentile(0.95) or 0)
            requests.add_metric([entry.name], entry.num_requests)
            failures.add_metric([entry.name], entry.num_failures)
        yield from (rps, p50, p95, requests, failures)
        users = GaugeMetricFamily("locust_users", "Simulated users.")
        users.add_metric([], self.environment.runner.user_count if self.environment.runner else 0)
        yield users
        schools = GaugeMetricFamily("locust_schools", "Schools online, at this run's assumptions.")
        count = self.environment.runner.user_count if self.environment.runner else 0
        schools.add_metric([], count / _users_per_school())
        yield schools


@events.init.add_listener
def _on_init(environment, **_) -> None:
    if environment.host:
        _assert_safe_host(environment.host)
    runner = environment.runner
    ALLOCATOR.runner = runner
    if isinstance(runner, WorkerRunner):
        return
    # On the master, or a single-process run. Workers have no aggregate stats to show.
    REGISTRY.register(_LocustCollector(environment))
    start_http_server(METRICS_PORT, addr="::")


@events.test_start.add_listener
def _on_start(environment, **_) -> None:
    if not isinstance(environment.runner, WorkerRunner):
        print(f"[medha] started {SCENARIO} phase={PHASE} profile={PROFILE} shape={SHAPE} at {time.time():.0f}")
