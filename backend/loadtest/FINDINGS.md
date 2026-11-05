# Backend freeze under load

**Status:** fixed and optimised on branch `feat/load-testing`, not yet merged or deployed. Capacity results are below. The per-feature limits and the morning and evening tests were not run.

## Summary

Under load, the backend stops serving requests almost completely, while its CPU and the database sit nearly idle. It is a deadlock between worker threads and database connections.

**It comes from the original codebase, not from the load test kit.** The unmodified code at commit `4ab0bf4` froze the same way, with no metrics or other changes.

## Symptoms

- Requests hang for up to 90 seconds, then fail with 500 errors.
- `/health` hangs too, though it never touches the database.
- The log shows `QueuePool limit of size 5 overflow 10 reached, connection timed out, timeout 30.00`.
- Adding CPU does not help, because the CPU is not what is limiting it.

## Cause

The backend has 40 threads (FastAPI's default) and 15 database connections per process (SQLAlchemy's defaults; `db/session.py` set no pool size).

1. A session keeps its connection from its first query until `get_db` closes it. That first query is normally the auth lookup, so nearly every request holds a connection early.
2. The later steps of the request (the route and the session close) each need a free thread.
3. Under load, all 40 threads block waiting for a connection, while the requests holding the 15 connections wait for a thread.
4. Nothing moves until the pool's 30-second timeout fails some requests, and then the cycle repeats.

Separately, LLM and voice calls kept their connection for the whole outside call, which filled the pool much faster.

The thread states and database states below are measured. That the holders are specifically waiting for a thread is the explanation that fits them, and the fix built on it works, but it was not observed directly.

## Evidence

All on a 2 vCPU backend with 1 uvicorn worker and 20 simulated schools (320 users), with the database on its own box. The usage mix is an assumption, so treat school counts as approximate.

| Measure | Original code | With our metrics, no fix | With the fix |
|---|---|---|---|
| Requests completed | 2,454 | 1,557 | **12,139** |
| Failed | 882 | 786 | **1** |
| p50 / p95 across the run | 130ms / 90s | 31s / 90s | **25ms / 670ms** |
| Threads blocked on the pool at capture | 40 of 40 | 40 of 40 | **0** |
| Connections `idle in transaction`, longest | 22, 189s | 16, 196s | **none** (6 idle) |
| Pool timeouts | not measured | 335 | **0** |
| Backend server CPU, run average | 8% | 5% | 14% |
| Finished the 5-minute run? | no, stopped early | no, stopped early | **yes** |

The fixed run also used a database five times larger (100 schools rather than 20), so it had the harder job.

**Do the metrics cause it or slow things down?** No. Below the freeze level, taking turns (20 users, requests per second): original 276 and 419, with metrics 341 and 373. Differences between versions are smaller than the variation between runs.

**A bigger pool does not fix it.** With a pool of 45, larger than the thread count, 200 connections to `/auth/me` still froze, with 428 pool timeouts.

## Fix

In `backend/src/backend/db/session.py`:

1. **Slots.** A request takes a slot before opening a session, and there are fewer slots (13) than connections (15). Whoever holds a slot is guaranteed a connection without waiting, so threads never block on the pool. Other requests queue on the event loop, which is cheap.
2. **`outside_call()`.** During LLM, speech and embedding calls, a request gives back its connection and slot, then retakes them. The clients do this themselves (`llm/gemini.py`, `llm/claude.py`, `speech/client.py`, `retrieval/embedder.py`), so features need no changes of their own. It keeps the slot if the session has unsaved changes, because saving them early could split one save into two.
3. **503 instead of hanging.** After 30 seconds in the queue a request gets `503` with `Retry-After: 5`. The frontend does not yet treat this specially.

New settings, with defaults that keep today's pool size: `DB_POOL_SIZE`, `DB_MAX_OVERFLOW`, `DB_POOL_TIMEOUT`, `DB_SLOTS`, `DB_SLOT_WAIT_SECONDS`. Total Postgres connections can reach workers x (pool size + overflow), so check that against Neon's limit before raising them.

`absence_calls/service.py` now gives back the request's slot and takes its own, since it opens a second session on the event loop.

## Testing

- `tests/test_db_slots.py` reproduces the deadlock on a small pool with the old `get_db`, as a control, then shows the fix serving 120 simultaneous requests through 3 connections with no failures. It also covers slots being returned when a client disconnects.
- `tests/test_query_batching.py` and `tests/test_middleware_headers.py` check that the optimisations below return the same results with fewer queries.
- Full suite on Postgres, with the test database seeded: **the same 17 tests fail on the original and optimised code, so no new failures.** Original: 94 tests, 62 pass, 15 skipped. Optimised: 137 tests, 105 pass, 15 skipped, so all 43 new tests pass. Those 17 need separate attention. They include a path hard-coded to one laptop in `test_omr_marked.py` and missing test data.

## Optimisations

None of these changes a flow or a response.

- **Homework lists** (`/homework`, `/homework/mine`): grouped counts and one lookup, instead of queries per item.
- **Attendance saves and bulk marks saves:** one lookup for all students instead of one each. Saving marks for 10 students went from 28 queries to 18. One INSERT per new row remains, because the database generates the id, so SQLAlchemy cannot batch them.
- **Middleware:** the request-id and security-header middleware are now pure ASGI, with the same order and behaviour.

## Capacity

Fake LLM and voice services, Postgres on its own box, 300 seeded schools. A school is 16 simulated users, which is an assumed usage level. A level passes with under 1% failed and p95 under 1,500ms. "Schools held" is the highest passing level before the first failed one. Each cell gives two runs, on the same machine in A B B A order. `plans/comparison.json` holds the plan.

| Server | Code | Schools held | Highest passing level overall | Peak req/s | Backend CPU per request |
|---|---|---|---|---|---|
| 2 vCPU, 1 worker | Before optimising | 80, 80 | 80, 80 | 163, 150 | 5.8ms, 5.5ms |
| 2 vCPU, 1 worker | Optimised | 60, 80 | 60, 80 | 188, 203 | 5.2ms, 5.0ms |
| 2 vCPU, 2 workers | Before optimising | 120, 100 | 120, 100 | 248, 229 | 6.3ms, 6.7ms |
| 2 vCPU, 2 workers | Optimised | **160, 160** | 160, 160 | 297, 273 | 4.5ms, 4.9ms |
| 4 vCPU, 4 workers | Before optimising | under 100, 175 | 275, 275 | 488, 487 | 5.6ms, 5.9ms |
| 4 vCPU, 4 workers | Optimised | 250, 125 | 300, 225 | 520, 373 | 4.3ms, 5.7ms |

"Under 100" means the first level tested, 100 schools, failed. Peak req/s is the highest throughput at any level, including levels over the latency limit.

What this supports:

- **The optimisations clearly helped on 2 vCPU with 2 workers:** 160 schools in both runs, against 100 to 120.
- **With 1 worker they raised peak throughput but not the schools held.** Latency passes the limit at about the same load.
- **On 4 vCPU the result is inconclusive.** The two optimised runs differ by more than the two versions do.
- **CPU per request fell in every pair**, by roughly 5% to 30%. Run-to-run variation is large, so this is a range rather than a single figure.
- **Workers matter more than optimising.** On 2 vCPU, going from 1 to 2 workers took optimised code from 80 schools to 160.

The original code, tested earlier on a 100-school database, froze just above 10 schools with 1 worker, 20 on 2 vCPU with 2 workers, and 30 on 4 vCPU with 4 workers.

**Unexplained: 4 worker latency spikes.** On 4 vCPU, individual levels jump to a p95 of 2 to 5 seconds and the next level recovers, with both versions, while backend CPU stays near 2.5 of 4 cores and the database near 20%. Neither machine is short of CPU, and the cause was not found. Until it is, treat about 125 schools as the safe figure for this setup and 275 as what it can reach.

**Recommendation.** Set `WEB_CONCURRENCY` on the server to the number of vCPUs, which uvicorn honours with no code change. Peak memory was 250 to 365MB per worker. Total Postgres connections can reach workers x 15, so check that against Neon's limit first.

## Still open

These are not fixed, and the load tests will show how much each matters:

- **OMR marking** runs image processing on the event loop (`omr/service.py`), so one upload stalls everything else in that process.
- **Streaming routes** make short database calls on the event loop. This no longer deadlocks, but each call blocks the loop briefly.
- **One uvicorn worker:** `render-start.sh` sets no `--workers`, so Python request handling uses about one core on any size of server.
- **The telephony voicebot** holds its own session for a whole call and is not load tested. With many calls at once it could still use up the connections outside the slot limit.

## Not yet verified

- The fix has only run on boat with a fake LLM, not on Render and Neon, or with real traffic.
- The school counts rest on assumed usage (see `README.md`).
- Boat's CPU and disk speed vary between hosts, so compare results as ranges.
