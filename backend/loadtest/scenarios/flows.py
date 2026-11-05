"""What each feature does on the wire. Paths, bodies and call order mirror the frontend
(`shiksha_sathi/lib/api.ts` and its pages), because realism comes from replaying what
the browser really sends, including the extra calls each page makes.

Every request is named after its route template, so Locust's per-route numbers line up
with the backend's `medha_http_*` metrics on the dashboard.

`locked` flows match the rollout in `shiksha_sathi/app/(protected)/(app)/layout.tsx`:
locked screens still fetch their data behind the overlay, but nobody can click submit.
So under the `launch` profile those flows only read.
"""

import json
import random
import time
from datetime import date, timedelta

from locust import events

ASK_QUESTIONS = (
    "कक्षा में इसे रोचक तरीके से कैसे समझाऊं?",
    "इसे और सरल भाषा में बताइए।",
    "बिना किसी सामग्री के एक गतिविधि दीजिए।",
)
TUTOR_QUESTIONS = ("यह अध्याय समझ नहीं आया, समझाइए।", "इसका एक उदाहरण दीजिए।")
ENGLISH_MESSAGES = ("I go to school yesterday.", "How do I say this correctly?")
TERMS = ("Term 1", "Term 2")
STREAM_TIMEOUT = (10, 180)
REQUEST_TIMEOUT = (10, 90)


def _today() -> str:
    return date.today().isoformat()


class Browser:
    """The HTTP behaviour shared by every simulated person."""

    def __init__(self, user) -> None:
        self.user = user
        self.client = user.client
        self.token: str | None = None
        self.refresh_cookie: str | None = None

    def _headers(self, extra: dict | None = None) -> dict:
        headers = {"X-Forwarded-For": self.user.school_ip}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if extra:
            headers.update(extra)
        return headers

    def request(self, method: str, path: str, name: str, *, json_body=None, params=None,
                files=None, data=None, ok=(200, 201, 204), extra_headers=None) -> dict | list | None:
        with self.client.request(
            method, path, name=name, json=json_body, params=params, files=files, data=data,
            headers=self._headers(extra_headers), catch_response=True, timeout=REQUEST_TIMEOUT,
        ) as response:
            if response.status_code not in ok:
                response.failure(f"HTTP {response.status_code}: {response.text[:160]}")
                return None
            response.success()
            if response.status_code == 204 or not response.content:
                return None
            if "json" not in response.headers.get("content-type", ""):
                return None
            return response.json()

    def get(self, path: str, name: str | None = None, **params) -> dict | list | None:
        return self.request("GET", path, name or f"GET {path}", params=params or None)

    def stream(self, path: str, name: str, json_body: dict | None = None) -> dict | None:
        """An SSE POST. Reads the whole stream, as a browser does, and reports time to
        the first event separately. Returns the `done` payload."""
        started = time.perf_counter()
        first_event: float | None = None
        event_name = None
        done = None
        error = None
        with self.client.post(
            path, name=name, json=json_body or {}, stream=True, catch_response=True,
            headers=self._headers({"Accept": "text/event-stream"}), timeout=STREAM_TIMEOUT,
        ) as response:
            if response.status_code != 200:
                response.failure(f"HTTP {response.status_code}: {response.text[:160]}")
                return None
            try:
                for raw in response.iter_lines():
                    if not raw:
                        continue
                    line = raw.decode("utf-8", "replace")
                    if line.startswith("event:"):
                        event_name = line[6:].strip()
                        if first_event is None:
                            first_event = time.perf_counter() - started
                    elif line.startswith("data:") and event_name in ("done", "error"):
                        payload = line[5:].strip()
                        if event_name == "error":
                            error = payload
                        else:
                            done = json.loads(payload) if payload else {}
            except Exception as exc:  # a dropped stream is a real failure, not a crash
                response.failure(f"stream broke: {type(exc).__name__}: {exc}")
                return None
            if error is not None:
                response.failure(f"SSE error event: {error[:160]}")
                return None
            if done is None:
                response.failure("stream ended without a done event")
                return None
            response.success()
        if first_event is not None:
            events.request.fire(
                request_type="SSE", name=f"first event: {name}", response_time=first_event * 1000,
                response_length=0, exception=None, context={},
            )
        return done

    def refresh(self) -> bool:
        """What a page load does: trade the cookie for an access token."""
        with self.client.post(
            "/auth/refresh", name="POST /auth/refresh", catch_response=True, timeout=REQUEST_TIMEOUT,
            headers={"X-Forwarded-For": self.user.school_ip, "X-Medha-Client": "web",
                     "Cookie": f"refresh_token={self.refresh_cookie}"},
        ) as response:
            if response.status_code != 200:
                response.failure(f"HTTP {response.status_code}: {response.text[:160]}")
                return False
            self.token = response.json()["access_token"]
            rotated = response.cookies.get("refresh_token")
            if rotated:
                self.refresh_cookie = rotated
            response.success()
            return True

    def login_phone(self, phone: str, password: str, role: str, student_id: str | None = None) -> bool:
        return self._login("/auth/login/phone", "POST /auth/login/phone", {
            "phone": phone, "password": password, "role": role,
            **({"student_id": student_id} if student_id else {}),
        })

    def login_email(self, email: str, password: str, role: str) -> bool:
        return self._login("/auth/login", "POST /auth/login", {"email": email, "password": password, "role": role})

    def _login(self, path: str, name: str, body: dict) -> bool:
        with self.client.post(
            path, name=name, json=body, catch_response=True, timeout=REQUEST_TIMEOUT,
            headers={"X-Forwarded-For": self.user.school_ip, "X-Medha-Client": "web"},
        ) as response:
            if response.status_code != 200:
                response.failure(f"HTTP {response.status_code}: {response.text[:160]}")
                return False
            self.token = response.json()["access_token"]
            self.refresh_cookie = response.cookies.get("refresh_token") or self.refresh_cookie
            response.success()
            return True


# ------------------------------------------------------------------- page shells


def teacher_shell(b: Browser, person: dict, chapters: list[dict]) -> None:
    """The calls every teacher screen makes on a full page load."""
    b.get("/auth/me")
    b.get("/school")
    b.get("/profile")
    b.get("/notifications/unread-count")
    b.get("/work-updates")
    assignment = person["sections"][0] if person["sections"] else None
    if assignment:
        b.get("/curriculum/chapters", grade_id=assignment["grade_id"], subject_id=assignment["subject_id"])
    if chapters:
        b.get("/curriculum/topics", chapter_id=chapters[0]["id"])


def student_shell(b: Browser, person: dict) -> None:
    b.get("/auth/me")
    b.get("/school")
    b.get("/profile")
    b.get("/notifications/unread-count")
    b.get("/reference/subjects")
    b.get("/reference/grades")


def principal_shell(b: Browser) -> None:
    b.get("/auth/me")
    b.get("/school")
    b.get("/principal/stats")
    # This shell mounts two notification bells (principal-shell.tsx), so the call
    # really does happen twice.
    b.get("/notifications/unread-count")
    b.get("/notifications/unread-count")


# ---------------------------------------------------------------------- teachers


def _teacher_scope(person: dict, manifest: dict) -> tuple[str, str, dict, str | None] | None:
    if not person["sections"]:
        return None
    assignment = random.choice(person["sections"])
    grade_id, subject_id = assignment["grade_id"], assignment["subject_id"]
    # A long tail, not uniform: most teachers are on the early chapters, which is
    # what gives the generation cache a realistic hit rate.
    chapters = manifest["chapters"][f"{grade_id}|{subject_id}"]
    chapter = chapters[min(int(random.expovariate(0.5)), len(chapters) - 1)]
    topic_id = chapter["topics"][0] if chapter["topics"] else None
    return grade_id, subject_id, chapter, topic_id


def ask_medha(b: Browser, person: dict, manifest: dict, turns: int | None = None) -> None:
    """Open Ask, start a chat, ask a few things, sometimes make an artifact."""
    scope = _teacher_scope(person, manifest)
    if scope is None:
        return
    grade_id, subject_id, chapter, topic_id = scope
    b.get("/modules", grade_id=grade_id, subject_id=subject_id, chapter_id=chapter["id"])
    b.get("/library/presentations", chapter_id=chapter["id"], language="hi", limit=3)
    session = b.request("POST", "/chat/sessions", "POST /chat/sessions", json_body={
        "grade_id": grade_id, "subject_id": subject_id, "chapter_id": chapter["id"], "topic_id": topic_id,
    })
    if not session:
        return
    path = f"/chat/sessions/{session['id']}"
    for _ in range(turns or random.randint(1, 3)):
        b.stream(f"{path}/messages", "POST /chat/sessions/{id}/messages", {"content": random.choice(ASK_QUESTIONS)})
        # The chat rate limit is 6 a minute per teacher; people read before replying.
        b.user.sleep_like_a_person(15, 40)
    if random.random() < 0.4:
        b.stream(f"{path}/generate", "POST /chat/sessions/{id}/generate",
                 {"artifact_type": random.choice(("quiz", "activity", "ppt"))})


def _generation_params(gen_type: str) -> dict:
    if gen_type == "quiz":
        return {"question_count": random.choice((5, 10, 15)), "difficulty": random.choice(("easy", "medium"))}
    if gen_type == "lesson_plan":
        return {"periods": random.choice((1, 2, 3))}
    if gen_type == "notes":
        return {"depth": random.choice(("summary", "standard"))}
    if gen_type == "question_paper":
        return {"mcq_count": 10, "short_count": 5, "long_count": random.choice((2, 3))}
    return {}


def generate_content(b: Browser, person: dict, manifest: dict) -> None:
    """Create a quiz, lesson plan, paper, notes or slides, then open the result."""
    scope = _teacher_scope(person, manifest)
    if scope is None:
        return
    grade_id, subject_id, chapter, topic_id = scope
    gen_type = random.choice(("quiz", "lesson_plan", "question_paper", "notes", "presentation"))
    done = b.stream(f"/generate/{gen_type}", "POST /generate/{type}", {
        "scope": {"grade_id": grade_id, "subject_id": subject_id, "chapter_id": chapter["id"], "topic_id": topic_id},
        "params": _generation_params(gen_type),
        "language": "hi-BiharBoli",
    })
    if done and done.get("generation_id"):
        b.get(f"/generations/{done['generation_id']}", "GET /generations/{id}")


def history_page(b: Browser, person: dict, manifest: dict) -> None:
    rows = b.get("/generations", limit=20)
    if rows:
        b.get(f"/generations/{random.choice(rows)['id']}", "GET /generations/{id}")


def dashboard_page(b: Browser, person: dict, manifest: dict) -> None:
    b.get("/generations", limit=6)


def work_update(b: Browser, person: dict, manifest: dict, *, submit: bool = True) -> None:
    """The end-of-day report."""
    b.get("/teacher/sections")
    b.get("/work-updates/my-syllabus-coverage")
    scope = _teacher_scope(person, manifest)
    if scope is None:
        return
    grade_id, subject_id, chapter, _ = scope
    b.get("/curriculum/chapters", grade_id=grade_id, subject_id=subject_id)
    b.get("/curriculum/topics", chapter_id=chapter["id"])
    b.get("/work-updates/homework-today", grade_id=grade_id, subject_id=subject_id)
    if submit:
        b.user.sleep_like_a_person(10, 30)
        b.request("POST", "/work-updates", "POST /work-updates", json_body={
            "grade_id": grade_id, "subject_id": subject_id, "chapter_id": chapter["id"],
            "activities": ["taught_chapter"], "topics": [], "ai_usefulness": "somewhat_helpful",
            "note": "Finished as planned.", "chapter_completed": False,
        })


def attendance(b: Browser, person: dict, manifest: dict, *, submit: bool) -> None:
    """The class teacher's page, including its 4-second poll of guardian calls."""
    section_id = person.get("class_section_id")
    b.get("/teacher/sections")
    if not section_id:
        return
    day = _today()
    roster = b.get("/attendance", class_section_id=section_id, date=day)
    b.get("/absence-calls", class_section_id=section_id)

    # Stay on the page for 5-15 polls (20-60 seconds at the real interval).
    for _ in range(random.randint(5, 15)):
        b.user.sleep_like_a_person(4, 4)
        b.get("/absence-calls", class_section_id=section_id)

    if submit and roster:
        records = [
            {"student_id": s["student_id"], "status": "absent" if random.random() < 0.08 else "present"}
            for s in roster["students"]
        ]
        if records:
            b.request("POST", "/attendance", "POST /attendance", json_body={
                "class_section_id": section_id, "date": day, "records": records,
            })


def homework_teacher(b: Browser, person: dict, manifest: dict, *, submit: bool) -> None:
    b.get("/homework")
    b.get("/teacher/sections")
    if submit and person["sections"]:
        assignment = random.choice(person["sections"])
        b.request("POST", "/homework", "POST /homework", json_body={
            "class_section_id": assignment["id"], "subject_id": assignment["subject_id"],
            "title": "Read the chapter", "description": "Answer the questions at the end.",
            "due_date": (date.today() + timedelta(days=2)).isoformat(),
        })


def timetable_teacher(b: Browser, person: dict, manifest: dict) -> None:
    b.get("/day-timetable", date=_today())


def report_card_teacher(b: Browser, person: dict, manifest: dict, *, submit: bool = True) -> None:
    b.get("/teacher/sections")
    b.get("/teacher/students")
    if not person["sections"]:
        return
    assignment = random.choice(person["sections"])
    term = random.choice(TERMS)
    b.get("/report-card/class-marks", class_section_id=assignment["id"],
          subject_id=assignment["subject_id"], term=term)
    if submit:
        section = next((s for s in manifest["_school_sections"] if s["id"] == assignment["id"]), None)
        if section:
            b.request("POST", "/report-card/bulk-marks", "POST /report-card/bulk-marks", json_body={
                "class_section_id": assignment["id"], "subject_id": assignment["subject_id"],
                "term": term, "max_marks": 100,
                "marks": [{"student_id": s, "marks_obtained": random.randint(30, 98)} for s in section["students"]],
            })


def omr_evaluate(b: Browser, person: dict, manifest: dict) -> None:
    """Upload a sheet for marking. CPU-heavy, and run on the event loop today."""
    section_id = person.get("class_section_id")
    sheet = manifest.get("_omr_sheet")
    if not section_id or sheet is None:
        return
    # 400 means the sheet could not be read, which still ran the image processing.
    b.request(
        "POST", "/report-card/omr/evaluate", "POST /report-card/omr/evaluate",
        params={"class_section_id": section_id, "max_marks": 100},
        files={"file": ("sheet.pdf", sheet, "application/pdf")}, ok=(200, 400),
    )


def locked_previews(b: Browser, person: dict, manifest: dict) -> None:
    """Screens behind the launch overlay: they still load their real data."""
    b.get("/notifications")
    b.get("/library/items")
    b.get("/teacher/students/pending")
    if person["sections"]:
        assignment = person["sections"][0]
        chapters = manifest["chapters"][f"{assignment['grade_id']}|{assignment['subject_id']}"]
        b.get("/notes", chapter_id=chapters[0]["id"])
        b.get("/practice", chapter_id=chapters[0]["id"])
    b.get("/modules")


def export_download(b: Browser, person: dict, manifest: dict) -> None:
    """Build a PPTX from a saved deck: real server-side CPU and memory."""
    rows = b.get("/generations", type="presentation", limit=5)
    if rows:
        b.request("GET", f"/generations/{random.choice(rows)['id']}/export/pptx",
                  "GET /generations/{id}/export/{fmt}", ok=(200, 400, 404))


def announce_class(b: Browser, person: dict, manifest: dict) -> None:
    """Fans out to one notification row per student."""
    if person["sections"]:
        b.request("POST", "/notifications/announce", "POST /notifications/announce", json_body={
            "title": "Class notice", "body": "Please bring your notebooks tomorrow.",
            "class_section_id": person["sections"][0]["id"],
        })


# ---------------------------------------------------------------------- students


def _student_chapter(person: dict, manifest: dict) -> tuple[str, dict]:
    subject_id = random.choice(list(manifest["subjects"].values()))
    return subject_id, manifest["chapters"][f"{person['grade_id']}|{subject_id}"][0]


def tutor_chat(b: Browser, person: dict, manifest: dict) -> None:
    subject_id, chapter = _student_chapter(person, manifest)
    b.get("/curriculum/chapters", grade_id=person["grade_id"], subject_id=subject_id)
    session = b.request("POST", "/tutor/sessions", "POST /tutor/sessions",
                        json_body={"subject_id": subject_id, "chapter_id": chapter["id"]})
    if not session:
        return
    for _ in range(random.randint(1, 3)):
        b.stream(f"/tutor/sessions/{session['id']}/messages", "POST /tutor/sessions/{id}/messages",
                 {"content": random.choice(TUTOR_QUESTIONS)})
        b.user.sleep_like_a_person(15, 40)


def english_practice(b: Browser, person: dict, manifest: dict) -> None:
    session = b.request("POST", "/english/sessions", "POST /english/sessions", json_body={})
    if not session:
        return
    for _ in range(random.randint(1, 3)):
        b.stream(f"/english/sessions/{session['id']}/messages", "POST /english/sessions/{id}/messages",
                 {"content": random.choice(ENGLISH_MESSAGES)})
        b.user.sleep_like_a_person(15, 40)


def voice_turn(b: Browser, person: dict, manifest: dict) -> None:
    """Speech to text, a spoken reply with its audio, then the voice history."""
    wav = manifest.get("_wav_clip")
    subject_id, chapter = _student_chapter(person, manifest)
    session = b.request("POST", "/tutor/sessions", "POST /tutor/sessions",
                        json_body={"subject_id": subject_id, "chapter_id": chapter["id"]})
    if not session:
        return
    if wav:
        b.request("POST", "/speech/transcribe", "POST /speech/transcribe",
                  files={"file": ("clip.wav", wav, "audio/wav")}, data={"language": "hi-IN"})
    b.stream(f"/tutor/sessions/{session['id']}/converse", "POST /tutor/sessions/{id}/converse",
             {"session_id": session["id"], "transcript": "प्रकाश संश्लेषण क्या होता है?"})
    b.get(f"/tutor/sessions/{session['id']}/voice-turns", "GET /tutor/sessions/{id}/voice-turns", limit=20)


def homework_student(b: Browser, person: dict, manifest: dict) -> None:
    rows = b.get("/homework/mine")
    if rows:
        item = random.choice(rows)
        b.request("POST", f"/homework/{item['id']}/done", "POST /homework/{id}/done")


def student_records(b: Browser, person: dict, manifest: dict) -> None:
    """The "my ..." pages a student browses."""
    b.get("/attendance/mine")
    b.get(f"/report-card/{person['id']}", "GET /report-card/{student_id}")
    b.get("/timetable", grade_id=person["grade_id"])
    b.get(f"/fees/{person['id']}", "GET /fees/{student_id}")


def student_updates(b: Browser, person: dict, manifest: dict) -> None:
    rows = b.get("/work-updates/mine")
    b.get("/notifications")
    if rows and random.random() < 0.3:
        b.request("POST", f"/work-updates/{random.choice(rows)['id']}/react",
                  "POST /work-updates/{id}/react", json_body={"value": "approve"})


def student_content(b: Browser, person: dict, manifest: dict) -> None:
    _, chapter = _student_chapter(person, manifest)
    b.get("/notes", chapter_id=chapter["id"])
    b.get("/practice", chapter_id=chapter["id"])
    b.get("/library/items")
    b.get("/library/presentations", grade_id=person["grade_id"], language="hi")


# --------------------------------------------------------------------- principals


def principal_dashboard(b: Browser, person: dict, manifest: dict) -> None:
    """The heaviest read in the app: whole-school summaries, all at once."""
    b.get("/principal/stats")
    b.get("/principal/sections")
    b.get("/principal/students")
    b.get("/principal/attendance/summary")
    b.get("/work-updates")
    b.get("/work-updates/syllabus-coverage")
    b.get("/work-updates/daily-status")


def principal_staff_and_classes(b: Browser, person: dict, manifest: dict) -> None:
    b.get("/principal/teachers/pending")
    b.get("/principal/teachers")
    b.get("/principal/academic-years")
    b.get("/reference/grades")
    sections = b.get("/principal/sections")
    if sections:
        section_id = random.choice(sections)["id"]
        b.get(f"/principal/sections/{section_id}", "GET /principal/sections/{id}")
        b.get("/reference/subjects")
        b.get(f"/principal/sections/{section_id}/students", "GET /principal/sections/{id}/students")
        b.get(f"/principal/sections/{section_id}/reserve-teachers", "GET /principal/sections/{id}/reserve-teachers")


def principal_timetable(b: Browser, person: dict, manifest: dict) -> None:
    rows = b.get("/principal/timetables")
    if rows:
        b.get(f"/principal/timetables/{rows[0]['id']}/grid", "GET /principal/timetables/{id}/grid", day=1)


def principal_cover(b: Browser, person: dict, manifest: dict) -> None:
    day = _today()
    b.get("/principal/teachers")
    b.get("/principal/day", date=day)
    b.get("/principal/day/final", date=day)


def principal_search(b: Browser, person: dict, manifest: dict) -> None:
    b.get("/principal/students/search", q=random.choice(("Kumar", "Priya", "Singh", "Sharma")))


def principal_work_review(b: Browser, person: dict, manifest: dict) -> None:
    b.get("/work-updates/daily-status", date=_today())


def admin_pages(b: Browser, person: dict, manifest: dict) -> None:
    b.get("/admin/stats")
    b.get("/admin/principals/pending")
    b.get("/admin/activity", limit=20)
    b.get("/admin/districts")
    b.get("/admin/schools")


def public_reference(b: Browser, person: dict, manifest: dict) -> None:
    """No login: the sign-up form's lookups."""
    b.get("/reference/grades")
    b.get("/reference/subjects")
    b.get("/schools/search", q="LoadTest")
