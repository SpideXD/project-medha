# Medha load test

Finds how much each feature, and the backend as a whole, can handle on a given server size. It runs on [boat.dev](https://boat.dev) with invented data and fake AI services, so it never touches production, costs nothing in AI usage, and cannot phone anyone.

```text
Locust (large) ──> Backend under test (small or default) ──> Postgres (large or default)
                              │
                              └──> Fake Gemini and Sarvam ┐
                                                          ├─ monitoring box (default)
Prometheus and Grafana collect from every box ────────────┘
```

## Run it

From `backend/`, with the `boat` CLI signed in:

```bash
python loadtest/boat/lt.py up --schools 50      # create boxes, install, seed
python loadtest/boat/lt.py link                 # live dashboard

python loadtest/boat/lt.py backend small 1      # 2 vCPU, 1 uvicorn worker
python loadtest/boat/lt.py run small-1w-ask LT_SCENARIO=ask_medha LT_SHAPE=steps \
    LT_USERS=5 LT_USERS_STEP=5 LT_MAX_USERS=60 LT_STEP_SECONDS=120

python loadtest/boat/lt.py down                 # delete everything; do this when finished
```

Each run restores the database, restarts the backend, then saves its results to `loadtest/results/<name>/`. Boxes stop themselves after 8 hours if forgotten.

## Test types

| Question | Settings |
|---|---|
| What is one feature's limit? | `LT_SCENARIO=<feature> LT_SHAPE=steps LT_USERS=… LT_USERS_STEP=… LT_MAX_USERS=…` |
| How many schools can the backend carry? | `LT_SCENARIO=day LT_PHASE=school LT_SHAPE=steps LT_SCHOOLS=… LT_SCHOOLS_STEP=… LT_MAX_SCHOOLS=…` |
| Does it survive the morning rush? | `LT_SCENARIO=day LT_PHASE=morning LT_SHAPE=spike LT_SCHOOLS=… LT_SPIKE_SECONDS=120` |
| Does it degrade over hours? | `LT_SCENARIO=day LT_SHAPE=steady LT_SCHOOLS=… LT_RUN_SECONDS=14400` |

`LT_SCENARIO=list` prints every feature. `LT_PROFILE=launch` matches today's locked screens and is the default; `LT_PROFILE=full` allows everything, as if all features were unlocked.

## Replace these assumptions

The tooling measures accurately, but "X schools" is only as true as what it assumes about usage. All of these are placeholders until production data replaces them:

| What | Where | Get the real value from |
|---|---|---|
| People online per school | `ONLINE_PER_SCHOOL` in `scenarios/locustfile.py` | Distinct active users per school, per hour |
| Which features people use | `_teacher_mix`, `_student_mix` in `scenarios/locustfile.py` | Row counts per hour in `chat_messages`, `generations`, `work_updates` |
| Share of sessions that log in | `LT_LOGIN_SHARE` | New rows per hour in `auth_sessions` |
| AI speed | `MOCK_LLM_*` in `mock_services/app.py` | `llm_call` log lines (`latency_ms`, `output_tokens`) |

## Reading results

The dashboard shows each feature's throughput, speed and errors, and below that the reasons: database connections in use, threads queued, whether the event loop is blocked, slow SQL and machine load.

A run only counts if the **load generator's CPU stays well below 100%**. Otherwise the test itself was the limit. The same goes for Postgres when you are measuring the backend.

Results apply to boat's hardware. Disk and CPU speed vary between boat hosts, so `lt.py` rejects slow-disk hosts for Postgres. Repeat important runs, because CPU speed still varies between them.

## What it deliberately leaves out

- **Real AI and voice providers:** their quotas and speed are not measured here. Compare your peak calls per minute with your provider's limit separately.
- **Render, Vercel and Neon:** these are different machines and network paths. Treat results as relative until confirmed on the real stack.
- **Phone calls:** `ABSENCE_CALLING_ENABLED` stays off, so the telephony voicebot WebSocket is not exercised.
- **Slow phones:** generators sit near the server, so these numbers exclude mobile network time.

## Backend changes this needs

All are inactive in a normal deployment:

- `core/metrics.py`, switched on only by `METRICS_ENABLED=true`, using the separate `loadtest` dependency group.
- `SARVAM_BASE_URL`, which defaults to the real Sarvam address.
