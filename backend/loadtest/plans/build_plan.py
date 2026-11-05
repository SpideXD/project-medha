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


if __name__ == "__main__":
    comparison = Path(__file__).with_name("comparison.json")
    comparison.write_text(json.dumps(build_comparison(), indent=1))
    out = Path(__file__).with_name("full.json")
    tests = build()
    out.write_text(json.dumps(tests, indent=1))
    worst = sum(t["max_seconds"] for t in tests) / 3600
    print(f"{len(tests)} tests, at most {worst:.1f} hours if nothing stops early; wrote {out}")
