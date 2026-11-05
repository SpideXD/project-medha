#!/usr/bin/env python3
"""Turns saved runs into a capacity table. Standard library only.

    python loadtest/report.py                     # all of loadtest/results/
    python loadtest/report.py --json report.json

A load level "passes" when, across its stable stretch, under LIMIT_FAILED of requests
fail and p95 stays under the limit for that test. The first 60 seconds after users are
added are ignored because they are the ramp, not the level. Capacity is the highest
level that passed with no failed level below it: a test that recovers after failing
did not really hold.
"""

import argparse
import json
from pathlib import Path

RESULTS = Path(__file__).parent / "results"
LIMIT_FAILED = 0.01
LIMIT_P95_MS = 1500
SAMPLE_SECONDS = 5
RAMP_SAMPLES_IGNORED = 12
MIN_STABLE_SAMPLES = 4


def _number(value: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[len(ordered) // 2] if ordered else 0.0


def levels_of(summary: dict) -> list[dict]:
    """What each user count did, from the run's 5-second samples."""
    grouped: dict[int, list[dict]] = {}
    for sample in summary.get("timeline", []):
        users = int(sample.get("users", 0))
        if users:
            grouped.setdefault(users, []).append(sample)
    levels = []
    for users in sorted(grouped):
        stable = grouped[users][RAMP_SAMPLES_IGNORED:]
        if len(stable) < MIN_STABLE_SAMPLES:
            continue
        rps = _median([s.get("rps", 0.0) for s in stable])
        failures = _median([s.get("failures", 0.0) for s in stable])
        levels.append({
            "users": users,
            "seconds": len(stable) * SAMPLE_SECONDS,
            "rps": rps,
            "failed_share": failures / rps if rps else 1.0,
            "p50_ms": _median([s.get("p50_ms", 0.0) for s in stable]),
            "p95_ms": _median([s.get("p95_ms", 0.0) for s in stable]),
            "backend_cores": _median([s.get("backend_cores", 0.0) for s in stable]),
            "slots_queued": _median([s.get("slots_queued", 0.0) for s in stable]),
            "generator_cpu": max((s.get("generator_cpu", 0.0) for s in stable), default=0.0),
            "db_cpu": max((s.get("db_cpu", 0.0) for s in stable), default=0.0),
        })
    return levels


def capacity_of(levels: list[dict], p95_limit_ms: float) -> dict | None:
    best = None
    for level in levels:
        if level["rps"] > 0 and level["failed_share"] < LIMIT_FAILED and level["p95_ms"] < p95_limit_ms:
            best = level
        else:
            break
    return best


def _metric(summary: dict, name: str) -> float | None:
    rows = summary.get("metrics", {}).get(name) or []
    return rows[0]["value"] if rows else None


def _instance(summary: dict, name: str) -> float | None:
    for row in summary.get("metrics", {}).get("machine_cpu_busy", []):
        if row["labels"].get("instance") == name:
            return row["value"]
    return None


def read_run(run_dir: Path) -> dict | None:
    path = run_dir / "summary.json"
    if not path.exists():
        return None
    summary = json.loads(path.read_text())
    env = summary.get("env", {})
    levels = levels_of(summary)
    limit = float(env.get("LT_P95_LIMIT_MS", LIMIT_P95_MS))
    best = capacity_of(levels, limit)
    first_failed = next((lv for lv in levels if best is None or lv["users"] > best["users"]), None)
    return {
        "name": summary["name"],
        "scenario": env.get("LT_SCENARIO"),
        "phase": env.get("LT_PHASE"),
        "shape": env.get("LT_SHAPE"),
        "config": f"{summary.get('backend_type')}:{summary.get('workers')}",
        "original_code": summary.get("original_code", False),
        "stopped_early": summary.get("stopped_early"),
        "capacity": best,
        "first_failed": first_failed,
        "levels": levels,
        "pool_timeouts": _metric(summary, "pool_timeouts"),
        "pool_max": _metric(summary, "pool_in_use_max"),
        "threads_queued_max": _metric(summary, "threads_queued_max"),
        "loop_stall_max": _metric(summary, "loop_stall_max"),
        "backend_cores": _metric(summary, "backend_cores_used"),
        "generator_cpu": _instance(summary, "generator"),
        "db_cpu": _instance(summary, "db"),
        "slow_sql": summary.get("slow_sql", ""),
    }


def markdown(runs: list[dict]) -> str:
    lines = [
        "| Test | Server:workers | Holds up to (users) | req/s there | p95 there | First failing level | Timeouts | Stopped early? |",
        "|---|---|---:|---:|---:|---|---:|---|",
    ]
    for run in sorted(runs, key=lambda r: (str(r["scenario"]), str(r["phase"]), r["config"])):
        best, bad = run["capacity"], run["first_failed"]
        lines.append(" | ".join([
            f"| {run['name']}",
            run["config"],
            f"{best['users']}" if best else "below lowest level",
            f"{best['rps']:.0f}" if best else "-",
            f"{best['p95_ms']:.0f}ms" if best else "-",
            (f"{bad['users']} users ({bad['failed_share']:.0%} failed, p95 {bad['p95_ms'] / 1000:.1f}s)" if bad else "none"),
            f"{run['pool_timeouts']:.0f}" if run["pool_timeouts"] is not None else "-",
            (run["stopped_early"] or "no")[:60] + " |",
        ]))
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--results", default=str(RESULTS))
    parser.add_argument("--json")
    args = parser.parse_args()
    runs = [run for run in (read_run(p) for p in sorted(Path(args.results).iterdir()) if p.is_dir()) if run]
    print(markdown(runs))
    if args.json:
        Path(args.json).write_text(json.dumps(runs, indent=1))


if __name__ == "__main__":
    main()
