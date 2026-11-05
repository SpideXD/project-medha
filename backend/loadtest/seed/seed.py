"""Fills a load-test database with fake schools, and writes a manifest of who exists
for the Locust scenarios to log in as.

All people, schools and phone numbers are invented. Run the reference seeds first
(grades, subjects and chapters come from them, unchanged):

    python scripts/seed_phase0.py
    python scripts/seed_subjects_extra.py
    python scripts/seed_curriculum.py
    python scripts/seed_library.py
    python loadtest/seed/seed.py --schools 50 --out /tmp/manifest.json

Refuses any database whose name lacks "loadtest", and any Neon or Render host, so
it cannot be pointed at production by a stray DATABASE_URL.

Row counts per school with the defaults: 400 students, 13 staff and about 41,000
attendance rows (students x school days of history).
"""

import argparse
import hashlib
import io
import json
import os
import random
import sys
import time
import uuid
from collections.abc import Iterable
from datetime import date, datetime, timedelta, timezone

import bcrypt
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

PASSWORD = "LoadTest#2026"
_NAMESPACE = uuid.UUID("6f1c2f5e-7a55-4c55-9a11-0c6d3f1f2b10")
_SUBJECTS = ("Science", "Social Science", "Mathematics", "English", "Hindi")
_GRADE_LEVELS = (6, 7, 8, 9, 10)
_PERIODS_PER_DAY = 8
_SCHOOL_DAYS = (1, 2, 3, 4, 5, 6)  # Monday .. Saturday
_ABSENT_RATE = 0.08
_FIRST_NAMES = ("Aarav", "Priya", "Rohan", "Sneha", "Vikram", "Anjali", "Rahul", "Pooja",
                "Amit", "Neha", "Suresh", "Kavita", "Manoj", "Ritu", "Deepak", "Sunita")
_LAST_NAMES = ("Kumar", "Sharma", "Singh", "Yadav", "Prasad", "Mishra", "Jha", "Gupta")
_FILLER = "प्रकाश संश्लेषण वह प्रक्रिया है जिसमें हरे पौधे अपना भोजन बनाते हैं। "


def det_uuid(*parts: object) -> uuid.UUID:
    return uuid.uuid5(_NAMESPACE, ":".join(str(p) for p in parts))


def refresh_token_for(actor_id: uuid.UUID) -> str:
    """A known token per user, so a run can use the real refresh endpoint without
    every virtual user first paying for a bcrypt login."""
    return hashlib.sha256(f"loadtest-refresh:{actor_id}".encode()).hexdigest()


def assert_safe_target(database_url: str) -> None:
    url = make_url(database_url)
    host = (url.host or "").lower()
    if "loadtest" not in (url.database or ""):
        raise SystemExit(f"refusing to seed {url.database!r}: its name must contain 'loadtest'.")
    if any(marker in host for marker in ("neon.tech", "render.com", "projectmedha")):
        raise SystemExit(f"refusing to seed host {host!r}: this looks like a real environment.")


def _copy_value(value: object) -> str:
    if value is None:
        return r"\N"
    if isinstance(value, bool):
        return "t" if value else "f"
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False)
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace("\t", "\\t")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
    )


class Writer:
    """Buffers rows per table and sends them with COPY, the fastest bulk path.
    Only `flush_all` writes, so a child table can never reach the database before
    the parents it references."""

    def __init__(self, connection) -> None:
        self.connection = connection
        self.buffers: dict[tuple[str, tuple[str, ...]], list[str]] = {}
        self.totals: dict[str, int] = {}

    def add(self, table: str, columns: tuple[str, ...], rows: Iterable[tuple]) -> None:
        buffer = self.buffers.setdefault((table, columns), [])
        for row in rows:
            buffer.append("\t".join(_copy_value(v) for v in row))
            self.totals[table] = self.totals.get(table, 0) + 1

    def _flush_one(self, table: str, columns: tuple[str, ...]) -> None:
        buffer = self.buffers.get((table, columns))
        if not buffer:
            return
        payload = io.StringIO("\n".join(buffer) + "\n")
        with self.connection.cursor() as cursor:
            cursor.copy_expert(f"COPY {table} ({', '.join(columns)}) FROM STDIN", payload)
        buffer.clear()

    def flush_all(self) -> None:
        rank = {name: position for position, name in enumerate(_TABLE_ORDER)}
        for table, columns in sorted(self.buffers, key=lambda key: rank[key[0]]):
            self._flush_one(table, columns)


# Parents must be written before the rows that reference them.
_TABLE_ORDER = (
    "districts", "blocks", "schools", "academic_years", "teachers", "class_sections",
    "teacher_subjects", "teaching_assignments", "students", "student_enrollments", "auth_sessions",
    "period_slots", "timetables", "timetable_cells", "attendance_records", "homework",
    "homework_status", "work_updates", "chat_sessions", "chat_messages", "modules",
    "module_artifacts", "generations", "notifications", "report_card_marks", "fee_payments",
    "chapter_notes", "practice_questions", "library_items",
)



def _name(rng: random.Random) -> str:
    return f"{rng.choice(_FIRST_NAMES)} {rng.choice(_LAST_NAMES)}"


def _phone(prefix_digit: int, number: int) -> str:
    return f"+91{prefix_digit}{number:09d}"


def _school_dates(today: date, history_days: int) -> list[date]:
    days = (today - timedelta(days=n) for n in range(1, history_days + 1))
    return [d for d in days if d.isoweekday() in _SCHOOL_DAYS]


def _current_year_bounds(today: date) -> tuple[str, date, date]:
    start_year = today.year if today.month >= 4 else today.year - 1
    label = f"{start_year}-{str(start_year + 1)[-2:]}"
    return label, date(start_year, 4, 1), date(start_year + 1, 3, 31)


def _quiz_content() -> dict:
    return {
        "questions": [
            {"q": _FILLER, "type": "mcq", "options": [_FILLER] * 4, "answer": _FILLER,
             "difficulty": "medium", "explanation": _FILLER}
            for _ in range(10)
        ]
    }


def _deck_content() -> dict:
    return {
        "title": "प्रकाश संश्लेषण",
        "subtitle": "कक्षा 8",
        "slides": [
            {"layout": "bullets", "heading": f"Slide {n}", "bullets": [_FILLER] * 4, "notes": _FILLER}
            for n in range(1, 9)
        ],
    }


def _notes_content() -> dict:
    return {
        "sections": [{"heading": "परिचय", "body_md": _FILLER * 6, "key_points": [_FILLER] * 3}] * 4,
        "summary": _FILLER * 3,
        "important_terms": [{"term": "क्लोरोफिल", "meaning": _FILLER}] * 5,
    }


_CONTENT = {"quiz": _quiz_content(), "presentation": _deck_content(), "notes": _notes_content()}

_ACTOR_COLUMNS = (
    "id", "school_id", "full_name", "email", "password_hash", "phone_number", "role",
    "onboarded_at", "email_verified_at", "approval_status", "approved_at",
    "years_of_experience", "qualification", "employee_code",
)


def _load_reference(connection) -> dict:
    with connection.cursor() as cursor:
        cursor.execute("SELECT numeric_level, id, label FROM grades")
        grades = {level: {"id": str(grade_id), "label": label}
                  for level, grade_id, label in cursor.fetchall()}
        cursor.execute("SELECT name, id FROM subjects WHERE board = 'BSEB'")
        subjects = {name: str(subject_id) for name, subject_id in cursor.fetchall()}
        cursor.execute(
            "SELECT id, subject_id, grade_id, title FROM curriculum_chapters ORDER BY chapter_number"
        )
        chapter_rows = cursor.fetchall()
        cursor.execute("SELECT id, chapter_id FROM curriculum_topics ORDER BY sequence_order")
        topic_rows = cursor.fetchall()

    missing_grades = [g for g in _GRADE_LEVELS if g not in grades]
    missing_subjects = [s for s in _SUBJECTS if s not in subjects]
    if missing_grades or missing_subjects:
        raise SystemExit(
            f"reference data missing (grades {missing_grades}, subjects {missing_subjects}). "
            "Run the reference seeds first; see this file's docstring."
        )

    topics_by_chapter: dict[str, list[str]] = {}
    for topic_id, chapter_id in topic_rows:
        topics_by_chapter.setdefault(str(chapter_id), []).append(str(topic_id))

    chapters: dict[str, list[dict]] = {}
    for chapter_id, subject_id, grade_id, title in chapter_rows:
        chapters.setdefault(f"{grade_id}|{subject_id}", []).append(
            {"id": str(chapter_id), "title": title, "topics": topics_by_chapter.get(str(chapter_id), [])}
        )
    for level in _GRADE_LEVELS:
        for subject in _SUBJECTS:
            if f"{grades[level]['id']}|{subjects[subject]}" not in chapters:
                raise SystemExit(f"no chapters for {grades[level]['label']} {subject}; run seed_curriculum.py")
    return {"grades": grades, "subjects": subjects, "chapters": chapters}


def seed_school(index: int, args, writer: Writer, ref: dict, ctx: dict) -> dict:
    """One school with its staff, classes, students and history. Returns its manifest."""
    rng = random.Random(index)
    now: datetime = ctx["now"]
    today: date = ctx["today"]
    password_hash: str = ctx["password_hash"]
    grades, subjects, chapters = ref["grades"], ref["subjects"], ref["chapters"]

    school_id = det_uuid("school", index)
    writer.add(
        "schools",
        ("id", "udise_code", "name", "district_id", "block_id", "medium_of_instruction", "school_type"),
        [(school_id, f"LT{index:09d}", f"LoadTest Govt School {index}", ctx["district_id"],
          ctx["block_id"], "Hindi", "government")],
    )
    year_id = det_uuid("year", index)
    label, starts, ends = ctx["year"]
    writer.add(
        "academic_years",
        ("id", "school_id", "label", "starts_on", "ends_on", "is_current"),
        [(year_id, school_id, label, starts, ends, True)],
    )

    # --- staff ---
    section_count = len(_GRADE_LEVELS) * args.sections_per_grade
    teacher_count = section_count + args.extra_teachers
    principal_id = det_uuid("principal", index)
    principal_email = f"principal-{index}@loadtest.example.com"
    staff_rows = [
        (principal_id, school_id, _name(rng), principal_email, password_hash, None, "principal",
         now, now, "approved", now, 20, "M.Ed", f"LTP{index}")
    ]
    teachers: list[dict] = []
    for t in range(teacher_count):
        teacher_id = det_uuid("teacher", index, t)
        phone = _phone(6, ctx["next_teacher_phone"]())
        staff_rows.append(
            (teacher_id, school_id, _name(rng), None, password_hash, phone, "teacher",
             now, None, "approved", now, rng.randint(1, 30), "B.Ed", f"LTT{index}-{t}")
        )
        teachers.append({
            "id": str(teacher_id), "phone": phone, "subject": _SUBJECTS[t % len(_SUBJECTS)],
            "subject_id": subjects[_SUBJECTS[t % len(_SUBJECTS)]], "refresh": refresh_token_for(teacher_id),
            "class_section_id": None, "sections": [],
        })
    writer.add("teachers", _ACTOR_COLUMNS, staff_rows)

    # --- classes, each with its own class teacher ---
    sections: list[dict] = []
    section_rows = []
    letters = "ABCDEFGHIJ"
    for grade_index, level in enumerate(_GRADE_LEVELS):
        for s in range(args.sections_per_grade):
            position = grade_index * args.sections_per_grade + s
            section_id = det_uuid("section", index, level, s)
            class_teacher = teachers[position]
            class_teacher["class_section_id"] = str(section_id)
            section_rows.append(
                (section_id, school_id, year_id, grades[level]["id"], letters[s], class_teacher["id"])
            )
            sections.append({
                "id": str(section_id), "grade_id": grades[level]["id"], "level": level,
                "class_teacher_id": class_teacher["id"], "position": position, "students": [],
            })
    writer.add(
        "class_sections",
        ("id", "school_id", "academic_year_id", "grade_id", "section", "class_teacher_id"),
        section_rows,
    )

    # --- who teaches what: a rotation that never double-books a teacher in one slot ---
    slot_ids = {
        (day, period): det_uuid("slot", index, day, period)
        for day in _SCHOOL_DAYS for period in range(1, _PERIODS_PER_DAY + 1)
    }
    writer.add(
        "period_slots",
        ("id", "school_id", "day_of_week", "period_number", "label"),
        [(slot_id, school_id, day, period, f"P{period}") for (day, period), slot_id in slot_ids.items()],
    )
    timetable_id = det_uuid("timetable", index)
    writer.add(
        "timetables",
        ("id", "school_id", "academic_year_id", "name", "status", "effective_from", "created_by"),
        [(timetable_id, school_id, year_id, "Main timetable", "published", starts, principal_id)],
    )
    assignments: set[tuple[int, int]] = set()
    cell_rows = []
    for section in sections:
        for period in range(1, _PERIODS_PER_DAY + 1):
            t = (section["position"] + period) % teacher_count
            assignments.add((t, section["position"]))
            for day in _SCHOOL_DAYS:
                cell_rows.append(
                    (timetable_id, section["id"], slot_ids[(day, period)], teachers[t]["subject_id"], teachers[t]["id"])
                )
    writer.add(
        "timetable_cells",
        ("timetable_id", "class_section_id", "period_slot_id", "subject_id", "teacher_id"),
        cell_rows,
    )
    assignment_rows, subject_rows = [], set()
    for t, position in sorted(assignments):
        teacher, section = teachers[t], sections[position]
        assignment_rows.append((teacher["id"], section["id"], teacher["subject_id"], "primary"))
        subject_rows.add((teacher["id"], teacher["subject_id"], section["grade_id"]))
        teacher["sections"].append(
            {"id": section["id"], "grade_id": section["grade_id"], "subject_id": teacher["subject_id"]}
        )
    writer.add(
        "teaching_assignments", ("teacher_id", "class_section_id", "subject_id", "role"), assignment_rows
    )
    writer.add("teacher_subjects", ("teacher_id", "subject_id", "grade_id"), sorted(subject_rows))

    # --- students ---
    student_rows, enrollment_rows, students = [], [], []
    for section in sections:
        for roll in range(1, args.students_per_section + 1):
            student_id = det_uuid("student", index, section["position"], roll)
            number = ctx["next_student_phone"]()
            phone = _phone(7, number)
            student_rows.append(
                (student_id, school_id, _name(rng), _name(rng), "father", _phone(8, number),
                 password_hash, phone, "approved", now)
            )
            enrollment_rows.append((student_id, section["id"], year_id, roll, starts))
            section["students"].append(str(student_id))
            students.append({
                "id": str(student_id), "phone": phone, "grade_id": section["grade_id"],
                "section_id": section["id"], "refresh": refresh_token_for(student_id),
            })
    writer.add(
        "students",
        ("id", "school_id", "full_name", "guardian_name", "guardian_relation", "guardian_phone",
         "password_hash", "phone_number", "approval_status", "approved_at"),
        student_rows,
    )
    writer.add(
        "student_enrollments",
        ("student_id", "class_section_id", "academic_year_id", "roll_number", "enrolled_on"),
        enrollment_rows,
    )

    # --- existing sessions, so refresh works without a login first ---
    expires = now + timedelta(days=25)
    session_rows = []
    for actor_id in [principal_id, *[uuid.UUID(t["id"]) for t in teachers]]:
        session_rows.append((actor_id, None, hashlib.sha256(refresh_token_for(actor_id).encode()).hexdigest(),
                             det_uuid("family", actor_id), now, expires))
    for student in students:
        actor_id = uuid.UUID(student["id"])
        session_rows.append((None, actor_id, hashlib.sha256(student["refresh"].encode()).hexdigest(),
                             det_uuid("family", actor_id), now, expires))
    writer.add(
        "auth_sessions",
        ("teacher_id", "student_id", "refresh_token_hash", "family_id", "issued_at", "expires_at"),
        session_rows,
    )

    write_history(index, args, writer, ref, ctx, rng, school_id, principal_id, teachers, sections, students)

    return {
        "index": index,
        "id": str(school_id),
        "ip": f"10.{(index >> 16) & 255}.{(index >> 8) & 255}.{index & 255}",
        "principal": {"id": str(principal_id), "email": principal_email,
                      "refresh": refresh_token_for(principal_id)},
        "teachers": teachers,
        "sections": [{k: s[k] for k in ("id", "grade_id", "class_teacher_id", "students")} for s in sections],
        "students": students,
    }


def write_history(index, args, writer, ref, ctx, rng, school_id, principal_id, teachers, sections, students) -> None:
    """Past activity, because many queries only slow down once tables have real size."""
    now: datetime = ctx["now"]
    today: date = ctx["today"]
    dates: list[date] = ctx["dates"]
    grades, subjects, chapters = ref["grades"], ref["subjects"], ref["chapters"]

    # attendance: one row per student per school day
    for section in sections:
        rows = [
            (student_id, section["class_teacher_id"], day,
             "absent" if rng.random() < _ABSENT_RATE else "present")
            for day in dates for student_id in section["students"]
        ]
        writer.add(
            "attendance_records", ("student_id", "marked_by_teacher_id", "attendance_date", "status"), rows
        )

    # homework: about three a week per class, with recent ones part-completed
    homework_rows, status_rows = [], []
    for section in sections:
        section_teachers = [t for t in teachers if any(s["id"] == section["id"] for s in t["sections"])]
        for n, day in enumerate(dates[::2]):
            teacher = section_teachers[n % len(section_teachers)]
            homework_id = det_uuid("homework", section["id"], n)
            created = datetime(day.year, day.month, day.day, 9, tzinfo=timezone.utc)
            homework_rows.append((homework_id, teacher["id"], school_id, section["id"], teacher["subject_id"],
                                  f"Homework {n}", _FILLER, day + timedelta(days=2), created))
            if n < 10:
                status_rows += [(homework_id, student_id, True, created)
                                for student_id in section["students"] if rng.random() < 0.6]
    writer.add(
        "homework",
        ("id", "teacher_id", "school_id", "class_section_id", "subject_id", "title", "description",
         "due_date", "created_at"),
        homework_rows,
    )
    writer.add("homework_status", ("homework_id", "student_id", "done", "done_at"), status_rows)

    level_of = {g["id"]: level for level, g in grades.items()}
    work_rows, session_rows, message_rows, module_rows, artifact_rows, generation_rows = [], [], [], [], [], []
    for t_index, teacher in enumerate(teachers):
        if not teacher["sections"]:
            continue
        grade_id = teacher["sections"][0]["grade_id"]
        chapter_list = chapters[f"{grade_id}|{teacher['subject_id']}"]

        # work updates on about 60% of school days
        for n, day in enumerate(dates):
            if rng.random() < 0.6:
                chapter = chapter_list[n % len(chapter_list)]
                work_rows.append((
                    teacher["id"], school_id, grade_id, teacher["subject_id"], chapter["id"], day,
                    grades[level_of[grade_id]]["label"], teacher["subject"], chapter["title"],
                    ["taught_chapter"], ["Topic"], [], "somewhat_helpful", "Finished as planned.",
                    datetime(day.year, day.month, day.day, 16, tzinfo=timezone.utc),
                ))

        # Ask Medha history, plus what it leaves behind
        for n in range(args.chats_per_teacher):
            chapter = chapter_list[n % len(chapter_list)]
            session_id = det_uuid("chat", teacher["id"], n)
            created = now - timedelta(days=n + 1)
            session_rows.append((session_id, teacher["id"], None, grade_id, teacher["subject_id"],
                                 chapter["id"], f"Chat {n}", created, created))
            for m in range(3):
                message_rows.append((session_id, "teacher", "इसे कैसे समझाएं?", None, created))
                message_rows.append((session_id, "assistant", _FILLER * 12, 300, created))
            module_id = det_uuid("module", session_id)
            module_rows.append((module_id, teacher["id"], session_id, grade_id, teacher["subject_id"],
                                chapter["id"], f"Module {n}", created))
            artifact_rows.append((module_id, "explanation", {"text": _FILLER * 12}))

        # saved generations, across the types the history page lists
        gen_types = list(_CONTENT)
        for n in range(args.generations_per_teacher):
            chapter = chapter_list[n % len(chapter_list)]
            gen_type = gen_types[n % len(gen_types)]
            created = now - timedelta(days=n + 1)
            generation_rows.append((
                det_uuid("generation", teacher["id"], n), teacher["id"], gen_type, f"{gen_type} {n}", "hi-BiharBoli",
                grade_id, teacher["subject_id"], chapter["id"], {}, _CONTENT[gen_type], "completed", created,
                "gemini-flash-lite-latest", f"{gen_type}-seed", created,
            ))

    writer.add(
        "work_updates",
        ("teacher_id", "school_id", "grade_id", "subject_id", "chapter_id", "work_date", "grade_label",
         "subject_name", "chapter_title", "activities", "topics", "homework", "ai_usefulness", "note", "created_at"),
        work_rows,
    )

    # student tutor history on a tenth of students
    for student in students[::10]:
        subject_id = subjects[_SUBJECTS[0]]
        chapter = chapters[f"{student['grade_id']}|{subject_id}"][0]
        for n in range(2):
            session_id = det_uuid("tutor", student["id"], n)
            created = now - timedelta(days=n + 1)
            session_rows.append((session_id, None, student["id"], student["grade_id"], subject_id,
                                 chapter["id"], "Doubt", created, created))
            message_rows.append((session_id, "teacher", "समझ नहीं आया", None, created))
            message_rows.append((session_id, "assistant", _FILLER * 8, 200, created))

    writer.add(
        "chat_sessions",
        ("id", "teacher_id", "student_id", "grade_id", "subject_id", "chapter_id", "title", "created_at", "updated_at"),
        session_rows,
    )
    writer.add("chat_messages", ("session_id", "role", "content", "token_count", "created_at"), message_rows)
    writer.add(
        "modules",
        ("id", "teacher_id", "session_id", "grade_id", "subject_id", "chapter_id", "title", "created_at"),
        module_rows,
    )
    writer.add("module_artifacts", ("module_id", "artifact_type", "content_json"), artifact_rows)
    writer.add(
        "generations",
        ("id", "teacher_id", "type", "title", "language", "grade_id", "subject_id", "chapter_id", "input_params",
         "content_json", "status", "completed_at", "model", "prompt_version", "created_at"),
        generation_rows,
    )

    # notifications, mostly read
    notification_rows = []
    for teacher in teachers:
        notification_rows += [
            (teacher["id"], None, "announcement", "Notice", _FILLER,
             now if rng.random() < 0.8 else None, now - timedelta(days=n))
            for n in range(args.notifications_per_person)
        ]
    for student in students:
        notification_rows += [
            (None, student["id"], "homework", "New homework", _FILLER,
             now if rng.random() < 0.8 else None, now - timedelta(days=n))
            for n in range(args.notifications_per_person)
        ]
    writer.add(
        "notifications",
        ("recipient_id", "recipient_student_id", "type", "title", "body", "read_at", "created_at"),
        notification_rows,
    )

    # report cards and fees
    mark_rows, fee_rows = [], []
    for section in sections:
        marker = section["class_teacher_id"]
        for student_id in section["students"]:
            for subject in _SUBJECTS:
                for term in ("Term 1", "Term 2"):
                    mark_rows.append((student_id, subjects[subject], term, rng.randint(30, 98), 100, marker))
            for n in range(2):
                fee_rows.append((student_id, school_id, 500, "tuition", ctx["today"] - timedelta(days=30 * (n + 1)), marker))
    writer.add(
        "report_card_marks",
        ("student_id", "subject_id", "term", "marks_obtained", "max_marks", "entered_by"),
        mark_rows,
    )
    writer.add("fee_payments", ("student_id", "school_id", "amount", "fee_type", "payment_date", "logged_by"), fee_rows)

    # school content
    author = teachers[0]["id"]
    sample_chapters = [c for key in sorted(chapters) for c in chapters[key][:1]]
    rng.shuffle(sample_chapters)
    picked = sample_chapters[:20]
    writer.add(
        "chapter_notes",
        ("chapter_id", "school_id", "summary", "key_points", "important_terms", "created_by"),
        [(c["id"], school_id, _FILLER * 6, [_FILLER] * 4, [], author) for c in picked],
    )
    writer.add(
        "practice_questions",
        ("chapter_id", "school_id", "question", "type", "options", "answer", "created_by"),
        [(c["id"], school_id, _FILLER, "mcq", ["A", "B", "C", "D"], "A", author) for c in picked for _ in range(2)],
    )
    writer.add(
        "library_items",
        ("school_id", "title", "description", "url", "added_by"),
        [(school_id, f"Resource {n}", _FILLER, f"https://example.com/resource/{n}", author) for n in range(10)],
    )


def _counter(start: int = 1):
    value = start - 1

    def next_value() -> int:
        nonlocal value
        value += 1
        return value

    return next_value


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--schools", type=int, required=True)
    parser.add_argument("--sections-per-grade", type=int, default=2)
    parser.add_argument("--students-per-section", type=int, default=40)
    parser.add_argument("--extra-teachers", type=int, default=2)
    parser.add_argument("--history-days", type=int, default=120)
    parser.add_argument("--chats-per-teacher", type=int, default=10)
    parser.add_argument("--generations-per-teacher", type=int, default=15)
    parser.add_argument("--notifications-per-person", type=int, default=20)
    parser.add_argument("--out", required=True, help="where to write the manifest JSON")
    parser.add_argument("--analyze", action=argparse.BooleanOptionalAction, default=True)
    args = parser.parse_args()

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise SystemExit("set DATABASE_URL")
    assert_safe_target(database_url)

    engine = create_engine(database_url)
    with engine.connect() as probe:
        existing = probe.execute(text("SELECT count(*) FROM schools WHERE udise_code LIKE 'LT%'")).scalar()
    if existing:
        raise SystemExit(f"{existing} load-test schools already exist; start from an empty migrated database.")

    connection = engine.raw_connection()
    started = time.time()
    ref = _load_reference(connection)
    now = datetime.now(timezone.utc)
    today = now.date()

    district_id, block_id = det_uuid("district"), det_uuid("block")
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO districts (id, name, state) VALUES (%s, 'LoadTest District', 'Bihar')", (str(district_id),)
        )
        cursor.execute(
            "INSERT INTO blocks (id, district_id, name) VALUES (%s, %s, 'LoadTest Block')",
            (str(block_id), str(district_id)),
        )

    ctx = {
        "now": now,
        "today": today,
        "dates": _school_dates(today, args.history_days),
        "year": _current_year_bounds(today),
        "district_id": district_id,
        "block_id": block_id,
        # One hash for everyone: same bcrypt cost as a real account, without a long seed.
        "password_hash": bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt()).decode(),
        "next_teacher_phone": _counter(),
        "next_student_phone": _counter(),
    }

    writer = Writer(connection)
    schools = []
    admin_id = det_uuid("admin")
    writer.add("teachers", _ACTOR_COLUMNS, [
        (admin_id, None, "LoadTest Admin", "admin@loadtest.example.com", ctx["password_hash"], None, "admin",
         now, now, "approved", now, None, None, None)
    ])
    writer.add(
        "auth_sessions",
        ("teacher_id", "student_id", "refresh_token_hash", "family_id", "issued_at", "expires_at"),
        [(admin_id, None, hashlib.sha256(refresh_token_for(admin_id).encode()).hexdigest(),
          det_uuid("family", admin_id), now, now + timedelta(days=25))],
    )
    for index in range(1, args.schools + 1):
        schools.append(seed_school(index, args, writer, ref, ctx))
        # COPY runs per table, so parents and children must be flushed together.
        writer.flush_all()
        connection.commit()
        if index % 10 == 0 or index == args.schools:
            print(f"  {index}/{args.schools} schools, {time.time() - started:.0f}s", flush=True)

    if args.analyze:
        with connection.cursor() as cursor:
            cursor.execute("ANALYZE")
    connection.commit()
    connection.close()

    manifest = {
        "password": PASSWORD,
        "admin": {"id": str(admin_id), "email": "admin@loadtest.example.com", "refresh": refresh_token_for(admin_id)},
        "grades": {str(level): g for level, g in ref["grades"].items()},
        "subjects": {name: ref["subjects"][name] for name in _SUBJECTS},
        "chapters": ref["chapters"],
        "schools": schools,
    }
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle)

    print(f"done in {time.time() - started:.0f}s; rows written:")
    for table in _TABLE_ORDER:
        if table in writer.totals:
            print(f"  {table:24s} {writer.totals[table]:>12,}")
    print(f"manifest: {args.out}")


if __name__ == "__main__":
    sys.exit(main())
