#!/usr/bin/env python3
"""Writes the full test matrix to plans/full.json, most valuable tests first, so a sweep
that is cut short still answers the main questions.

    python loadtest/plans/build_plan.py
    python loadtest/boat/lt.py sweep loadtest/plans/full.json
"""

import json
from pathlib import Path

# Size and uvicorn workers. 1 worker is the current start command; the others use the
# whole server. All four matter, because a server's size only helps if it is used.
CONFIGS = ("small:1", "small:2", "default:1", "default:4")
# Feature limits are measured on fewer configurations, to keep the run to a sensible length.
FEATURE_CONFIGS = ("small:1", "small:2", "default:4")


def _day(phase: str, config: str, *, start: int, step: int, maximum: int, seconds: int = 120) -> dict:
    return {
        "name": f"day-{phase}-{config.replace(':', '-')}w",
        "config": config,
        "max_seconds": (((maximum - start) // step + 1) * seconds) + 300,
        "env": {
            "LT_SCENARIO": "day", "LT_PHASE": phase, "LT_PROFILE": "launch", "LT_SHAPE": "steps",
            "LT_SCHOOLS": str(start), "LT_SCHOOLS_STEP": str(step), "LT_MAX_SCHOOLS": str(maximum),
            "LT_STEP_SECONDS": str(seconds),
        },
    }


def _before(config: str) -> dict:
    """The original code, for a true before-and-after on the same data."""
    test = _day("school", config, start=5, step=5, maximum=40)
    test["name"] = f"before-{test['name']}"
    test["original"] = True
    return test


# name: (first level, step, last level, p95 limit in ms). Users repeat the flow with short
# pauses, except LLM flows, which keep the human pauses their rate limits require, so they
# need far more users to create the same pressure.
FEATURES = {
    "ask_medha": (25, 50, 275, 5000),
    "generation": (25, 50, 275, 5000),
    "attendance": (25, 50, 275, 2000),
    "auth_login": (2, 4, 22, 3000),
    "auth_refresh": (10, 20, 130, 1500),
    "work_update": (10, 20, 130, 1500),
    "tutor": (25, 50, 275, 5000),
    "principal_dashboard": (5, 10, 65, 3000),
    "omr": (1, 2, 13, 5000),
    "polling": (10, 20, 130, 1000),
    "history": (10, 20, 130, 1500),
    "report_card": (5, 10, 65, 3000),
    "student_records": (10, 20, 130, 1500),
    "homework": (10, 20, 130, 1500),
    "exports": (5, 10, 65, 3000),
    "english": (25, 50, 275, 5000),
    "voice": (10, 20, 130, 6000),
    "announce": (5, 10, 65, 3000),
    "principal_timetable": (5, 10, 65, 3000),
    "student_content": (10, 20, 130, 1500),
}


def _feature(name: str, config: str) -> dict:
    start, step, maximum, limit = FEATURES[name]
    seconds = 60
    return {
        "name": f"feature-{name}-{config.replace(':', '-')}w",
        "config": config,
        "max_seconds": (((maximum - start) // step + 1) * seconds) + 240,
        "env": {
            "LT_SCENARIO": name, "LT_PROFILE": "full", "LT_SHAPE": "steps",
            "LT_USERS": str(start), "LT_USERS_STEP": str(step), "LT_MAX_USERS": str(maximum),
            "LT_STEP_SECONDS": str(seconds), "LT_P95_LIMIT_MS": str(limit),
        },
    }


def build() -> list[dict]:
    plan = []
    # 1. How many schools can each server size carry, once the code is fixed?
    plan += [_day("school", c, start=10, step=10, maximum=100) for c in CONFIGS]
    # 2. The true before: the same test on the original code.
    plan += [_before(c) for c in CONFIGS]
    # 3. The other parts of the day, on the smallest and largest setups first.
    plan += [_day("morning", c, start=10, step=10, maximum=100) for c in ("small:2", "default:4")]
    plan += [_day("evening", c, start=10, step=10, maximum=60) for c in ("small:2", "default:4")]
    # 4. Each feature's own limit, one configuration at a time.
    for config in reversed(FEATURE_CONFIGS):
        plan += [_feature(name, config) for name in FEATURES]
    return plan


def _ab(config: str, version: str, repeat: int, *, start: int, step: int, maximum: int) -> dict:
    # Longer levels than the other tests: each step adds hundreds of users, and the rush
    # of logins that follows takes about a minute to settle.
    test = _day("school", config, start=start, step=step, maximum=maximum, seconds=180)
    test["name"] = f"ab{repeat}-{config.replace(':', '-')}w-{version}"
    if version == "base":
        test["code"] = "baseline"  # lt.py snapshot_code: the deadlock fix without the optimisations
    return test


def build_comparison() -> list[dict]:
    """The same test on two code versions on the same machine, in A B B A order so that
    a host slowing down over time does not favour either version."""
    plan = []
    for config, levels in (
        ("small:1", {"start": 60, "step": 20, "maximum": 200}),
        ("small:2", {"start": 80, "step": 20, "maximum": 240}),
        ("default:4", {"start": 100, "step": 25, "maximum": 300}),
    ):
        plan += [_ab(config, "base", 1, **levels), _ab(config, "opt", 1, **levels)]
        plan += [_ab(config, "opt", 2, **levels), _ab(config, "base", 2, **levels)]
    return plan


# Where each part of the day starts and how fast it climbs, in schools. The ceilings are
# far above the expected limits, because each run is meant to end by breaking the server.
CAPACITY_LEVELS = {
    "school": {"small": (60, 20, 500), "default": (100, 25, 800)},
    "morning": {"small": (60, 20, 500), "default": (100, 25, 800)},
    "evening": {"small": (30, 10, 250), "default": (50, 15, 400)},
}
CAPACITY_WORKERS = {"small": 2, "default": 4}
# Repeat, phase, and which machine of that size. Most valuable first, and alternating
# machines, so a sweep cut short still has each answer on both.
CAPACITY_ORDER = (
    (1, "school", ""), (1, "evening", ""), (1, "morning", ""),
    (2, "school", "2"), (2, "evening", "2"), (2, "morning", "2"),
    (3, "school", ""),
)


# Rough limits: wider steps and shorter levels, one run each, no restore between runs.
ROUGH_LEVELS = {
    "school": {"small": (60, 40, 500), "default": (100, 50, 800)},
    "evening": {"small": (30, 20, 250), "default": (50, 25, 400)},
    "morning": {"small": (60, 40, 500), "default": (100, 50, 800)},
}


def build_rough_capacity() -> list[dict]:
    plan = []
    for phase, sizes in ROUGH_LEVELS.items():
        for size, (start, step, maximum) in sizes.items():
            workers = CAPACITY_WORKERS[size]
            # The first small box had no network route to the monitoring box, so use the second.
            machine = "small2" if size == "small" else size
            test = _day(phase, f"{machine}:{workers}", start=start, step=step, maximum=maximum, seconds=150)
            test["name"] = f"indexed-{phase}-{size}-{workers}w"  # after migration 0041
            test["env"]["LT_PROFILE"] = "full"
            test["restore"] = False
            plan.append(test)
    return plan


# More workers than vCPUs, on the same machines as the rough runs. Servers fail with spare
# CPU while their event loops lag, so if blocking on I/O is what limits them, more loops
# should use that CPU. School hours has fewer extra workers: its workers peak over 1 GB.
WORKER_TESTS = (("evening", "small", 4), ("morning", "default", 8), ("school", "small", 3), ("school", "default", 6))


def build_worker_test() -> list[dict]:
    plan = []
    for phase, size, workers in WORKER_TESTS:
        start, step, maximum = ROUGH_LEVELS[phase][size]
        machine = "small2" if size == "small" else size
        test = _day(phase, f"{machine}:{workers}", start=start, step=step, maximum=maximum, seconds=150)
        test["name"] = f"workers-{phase}-{size}-{workers}w"
        test["env"]["LT_PROFILE"] = "full"
        test["restore"] = False
        plan.append(test)
    return plan


def build_worker_retest() -> list[dict]:
    """The first 8 worker run lost nearly half its CPU to the host, so repeat it on the other
    4 vCPU machine, with 4 workers beside it for a fair comparison on that machine."""
    plan = []
    for workers in (4, 8):
        start, step, maximum = ROUGH_LEVELS["morning"]["default"]
        test = _day("morning", f"default2:{workers}", start=start, step=step, maximum=maximum, seconds=150)
        test["name"] = f"retest-morning-default2-{workers}w"
        test["env"]["LT_PROFILE"] = "full"
        test["restore"] = False
        plan.append(test)
    return plan


def build_throughput() -> list[dict]:
    """The most a server can do, with no simulated schools: users repeat one action with no
    pause at all, in rising numbers, until throughput stops growing. AI features are left out,
    because their per-person limits would cap them, and the fake AI's speed would be measured."""
    plan = []
    for feature in ("polling", "student_records", "history", "attendance"):
        for machine, workers in (("small2", 2), ("default2", 4)):
            test = {
                "name": f"throughput-{feature}-{machine}-{workers}w",
                "config": f"{machine}:{workers}",
                "restore": False,
                "max_seconds": 12 * 60 + 240,
                "env": {
                    "LT_SCENARIO": feature, "LT_PROFILE": "full", "LT_SHAPE": "steps", "LT_THINK_MIN": "0",
                    "LT_THINK_MAX": "0", "LT_USERS": "10", "LT_USERS_STEP": "40", "LT_MAX_USERS": "450",
                    "LT_STEP_SECONDS": "60", "LT_P95_LIMIT_MS": "1500",
                },
            }
            plan.append(test)
    return plan


def _on_far_boxes(prefix: str, sizes: tuple[str, ...], phases: tuple[str, ...]) -> list[dict]:
    """On the two boxes about 1.8 ms from the database. The first 4 vCPU box is 0.3 ms away,
    which let it handle 2.5 times the users of an identical box, so it flatters the code."""
    plan = []
    for phase in phases:
        for size in sizes:
            start, step, maximum = ROUGH_LEVELS[phase][size]
            workers = CAPACITY_WORKERS[size]
            machine = {"small": "small2", "default": "default2"}[size]
            test = _day(phase, f"{machine}:{workers}", start=start, step=step, maximum=maximum, seconds=150)
            test["name"] = f"{prefix}-{phase}-{size}-{workers}w"
            test["env"]["LT_PROFILE"] = "full"
            test["restore"] = False
            plan.append(test)
    return plan


def build_before_fix() -> list[dict]:
    """The tests the 4 vCPU far box has not done yet, to compare the event loop fix with."""
    return _on_far_boxes("before", ("default",), ("school", "evening"))


def build_after_fix() -> list[dict]:
    return _on_far_boxes("fixed", ("small", "default"), ("school", "evening", "morning"))


def build_after_review() -> list[dict]:
    """The same tests with the code review's fixes in. Each 2 vCPU morning run so far has
    told a different story, so this repeats that one twice."""
    plan = _on_far_boxes("reviewed", ("default",), ("morning", "school"))
    plan += _on_far_boxes("reviewed", ("small",), ("evening", "morning"))
    plan += _on_far_boxes("reviewed2", ("small",), ("morning",))
    return plan


def build_final_check() -> list[dict]:
    """The exact code going in the PR. The 2 vCPU evening run before it is repeated here
    because its load generator stalled, which made that run unreliable."""
    return _on_far_boxes("final", ("small",), ("evening",)) + _on_far_boxes("final", ("default",), ("morning",))


def build_capacity() -> list[dict]:
    """Push each server size until it breaks, on a database with far more data."""
    plan = []
    for repeat, phase, machine in CAPACITY_ORDER:
        for size in ("small", "default"):
            start, step, maximum = CAPACITY_LEVELS[phase][size]
            workers = CAPACITY_WORKERS[size]
            test = _day(phase, f"{size}{machine}:{workers}", start=start, step=step, maximum=maximum, seconds=180)
            test["name"] = f"cap{repeat}-{phase}-{size}-{workers}w"
            # All features unlocked, so attendance saves, OMR and exports are part of the load.
            test["env"]["LT_PROFILE"] = "full"
            plan.append(test)
    return plan


if __name__ == "__main__":
    comparison = Path(__file__).with_name("comparison.json")
    comparison.write_text(json.dumps(build_comparison(), indent=1))
    Path(__file__).with_name("capacity.json").write_text(json.dumps(build_rough_capacity(), indent=1))
    Path(__file__).with_name("capacity-full.json").write_text(json.dumps(build_capacity(), indent=1))
    Path(__file__).with_name("workers.json").write_text(json.dumps(build_worker_test(), indent=1))
    Path(__file__).with_name("worker-retest.json").write_text(json.dumps(build_worker_retest(), indent=1))
    Path(__file__).with_name("throughput.json").write_text(json.dumps(build_throughput(), indent=1))
    Path(__file__).with_name("before-fix.json").write_text(json.dumps(build_before_fix(), indent=1))
    Path(__file__).with_name("after-fix.json").write_text(json.dumps(build_after_fix(), indent=1))
    Path(__file__).with_name("after-review.json").write_text(json.dumps(build_after_review(), indent=1))
    Path(__file__).with_name("final-check.json").write_text(json.dumps(build_final_check(), indent=1))
    out = Path(__file__).with_name("full.json")
    tests = build()
    out.write_text(json.dumps(tests, indent=1))
    worst = sum(t["max_seconds"] for t in tests) / 3600
    print(f"{len(tests)} tests, at most {worst:.1f} hours if nothing stops early; wrote {out}")
