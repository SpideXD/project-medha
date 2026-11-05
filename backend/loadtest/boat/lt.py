#!/usr/bin/env python3
"""Runs the load test environment on boat.dev. Standard library only; run it from
`backend/` on your own machine.

    python loadtest/boat/lt.py up --schools 50      # create and fill the boxes
    python loadtest/boat/lt.py link                 # the live dashboard
    python loadtest/boat/lt.py backend small 1      # size and uvicorn workers to test
    python loadtest/boat/lt.py run NAME [ENV=VALUE ...]
    python loadtest/boat/lt.py down                 # delete everything

Boxes: one Postgres, one load generator, one for monitoring and the fake LLM, and one
backend per size under test. The backend box is the only thing that changes between
configurations, so differences in results come from it.

Safety: boxes hold invented data only and start without your stored boat secrets.
Telephony, push, email and the real LLM keys are never configured on them, so no test
can phone a guardian or spend money.
"""

import argparse
import json
import os
import secrets
import shlex
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[2]  # backend/
STATE_FILE = Path(__file__).with_name(".state.json")
RESULTS = ROOT / "loadtest" / "results"
BOAT = os.environ.get("BOAT_BIN") or str(Path.home() / ".ascii/bin/boat")
os.environ["BOAT_NO_UPDATE"] = "1"

BACKEND_PORT = 8010
EXPORTER_PORT = 9200
MOCK_PORT = 9300
NODE_PORT = 9100
PG_EXPORTER_PORT = 9187
LOCUST_METRICS_PORT = 9646
LOCUST_MASTER_PORT = 5557
GENERATOR_WORKERS = 7
BREAK_FAILURE_SHARE = 0.5
BREAK_CONFIRM_SECONDS = 120  # longer than a brief latency spike, which recovers on its own
BREAK_P95_MULTIPLE = 4
CAPTURE_P95_MULTIPLE = 2
CAPTURE_MAX_PER_RUN = 4
CAPTURE_MIN_GAP_SECONDS = 90
SEED_JOBS = 4
STALL_LOG = "/tmp/medha-stalls.log"
EXEC_ATTEMPTS = 4

# An SSD commit is thousands of IOPS. Boxes on some hosts managed under 30, which
# would make Postgres the bottleneck in every result, so those are rejected.
MIN_FSYNC_IOPS = 1000
MAX_BOX_ATTEMPTS = 6
TTL_SECONDS = 36 * 3600  # a safety net: `down` deletes the boxes when the work is done

# The code as forked, before this kit existed.
ORIGINAL_REPO = "https://github.com/SpideXD/project-medha.git"
ORIGINAL_COMMIT = "4ab0bf4"

BACKEND_BOXES = (("backend_small", "small"), ("backend_default", "default"),
                 ("backend_small2", "small"), ("backend_default2", "default"))

DB_NAME = "medha_loadtest"
DB_TEMPLATE = "medha_loadtest_seed"
HOST_PATH = "/home/user/medha"


def load_state() -> dict:
    return json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {"boxes": {}}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=1))
    STATE_FILE.chmod(0o600)


def boat(*args: str, timeout: int = 300, check: bool = True) -> str:
    result = subprocess.run([BOAT, *args], capture_output=True, text=True, timeout=timeout)
    if check and result.returncode != 0:
        raise SystemExit(f"boat {' '.join(args[:3])} failed: {(result.stderr or result.stdout)[-600:]}")
    return result.stdout


def create_box(kind: str, name: str) -> dict:
    # --no-snapshots: boat otherwise captures every box each minute, which is useless
    # here, adds disk activity to a measured system, and leaves data to clean up.
    output = boat("new", "--type", kind, "--name", name, "--ttl", str(TTL_SECONDS), "--no-env", "--no-snapshots",
                  "--json", timeout=600)
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("event") == "ready":
            return {"id": event["id"], "ip": event["ip"], "type": kind, "name": name}
    raise SystemExit(f"could not create {name}: {output[-400:]}")


def run(box: dict, script: str, *, timeout: int = 300, check: bool = True, quiet: bool = False) -> str:
    """Run a bash script on a box, returning its stdout. Boat's exec API occasionally
    answers without an exit code, or not at all, which says nothing about the script,
    so those replies are retried rather than failing a run that takes half an hour."""
    for attempt in range(1, EXEC_ATTEMPTS + 1):
        output = boat("exec", box["id"], "--timeout", str(timeout), "--json", "--", "bash", "-c",
                      "set -e\n" + script, timeout=timeout + 60, check=False)
        try:
            result = json.loads(output.strip().splitlines()[-1])
        except (json.JSONDecodeError, IndexError):
            result = None
        if result is not None and result.get("exitCode") is not None:
            break
        if attempt == EXEC_ATTEMPTS:
            if result is None:
                raise SystemExit(f"[{box['name']}] unreadable reply: {output[-500:]}")
            break
        print(f"  [{box['name']}] no exit code from boat, retrying", flush=True)
        time.sleep(15)
    if check and result.get("exitCode") != 0:
        detail = (result.get("stderr") or "")[-800:] + (result.get("stdout") or "")[-800:]
        raise SystemExit(f"[{box['name']}] script failed (exit {result.get('exitCode')}, "
                         f"timed out: {result.get('timedOut')}):\n{detail}")
    if not quiet and result.get("stdout"):
        print(result["stdout"].rstrip())
    return result.get("stdout", "")


def url_host(ip: str) -> str:
    return f"[{ip}]" if ":" in ip else ip


def box_fsync_iops(box: dict) -> int:
    out = run(box, """
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq fio >/dev/null 2>&1
cd /tmp
fio --name=f --rw=write --bs=8k --size=32m --fdatasync=1 --runtime=6 --time_based --filename=/tmp/fsyncprobe --minimal 2>/dev/null \
  | awk -F';' '{printf "%.0f",$49}'
""", timeout=120, quiet=True)
    return int(float(out.strip() or 0))


def create_db_box() -> dict:
    """Keep creating Postgres boxes until one lands on a host with a usable disk."""
    for attempt in range(1, MAX_BOX_ATTEMPTS + 1):
        kind = "large" if attempt <= MAX_BOX_ATTEMPTS // 2 else "default"
        box = create_box(kind, f"medha-lt-db-{attempt}")
        iops = box_fsync_iops(box)
        print(f"  db attempt {attempt}: {kind}, {iops} fsync IOPS")
        if iops >= MIN_FSYNC_IOPS:
            box["fsync_iops"] = iops
            return box
        boat("delete", box["id"], "--yes", "--json", check=False)
    raise SystemExit(f"no host with {MIN_FSYNC_IOPS}+ fsync IOPS after {MAX_BOX_ATTEMPTS} tries.")


def install_node_exporter(box: dict, allow_from: str) -> None:
    run(box, f"""
sudo ufw allow from {allow_from} to any port {NODE_PORT} proto tcp >/dev/null
sudo docker rm -f node-exporter >/dev/null 2>&1 || true
sudo docker run -d --name node-exporter --restart unless-stopped --network host --pid host \
  -v /:/host:ro,rslave prom/node-exporter:v1.8.2 --path.rootfs=/host >/dev/null
""", timeout=300, quiet=True)


def push_code(box: dict) -> None:
    boat("push", box["id"], str(ROOT), "--to", f"{HOST_PATH}/backend", timeout=600)


def setup_database(state: dict) -> None:
    db = state["boxes"]["db"]
    password = state["db_password"]
    run(db, f"""
sudo docker rm -f pg >/dev/null 2>&1 || true
sudo docker run -d --name pg --restart unless-stopped --network host --shm-size=1g \
  -e POSTGRES_USER=shiksha -e POSTGRES_PASSWORD={password} -e POSTGRES_DB={DB_NAME} \
  pgvector/pgvector:pg16 postgres -c max_connections=400 -c shared_buffers=4GB -c effective_cache_size=12GB \
  -c max_wal_size=8GB -c min_wal_size=2GB -c checkpoint_timeout=15min -c checkpoint_completion_target=0.9 \
  -c log_checkpoints=on \
  -c shared_preload_libraries=pg_stat_statements -c pg_stat_statements.track=all -c track_io_timing=on >/dev/null
for i in $(seq 1 60); do
  sudo docker exec pg pg_isready -q -U shiksha -d {DB_NAME} -h 127.0.0.1 && break
  sleep 2
done
sleep 3
sudo docker exec pg psql -q -U shiksha -d {DB_NAME} -c 'CREATE EXTENSION IF NOT EXISTS pg_stat_statements'
sudo docker rm -f pg-exporter >/dev/null 2>&1 || true
sudo docker run -d --name pg-exporter --restart unless-stopped --network host \
  -e DATA_SOURCE_NAME='postgresql://shiksha:{password}@127.0.0.1:5432/{DB_NAME}?sslmode=disable' \
  quay.io/prometheuscommunity/postgres-exporter:v0.15.0 --collector.stat_statements >/dev/null
""", timeout=600, quiet=True)


def allow_postgres_from(state: dict, box: dict) -> None:
    run(state["boxes"]["db"], f"sudo ufw allow from {box['ip']} to any port 5432 proto tcp >/dev/null", quiet=True)


def backend_env(state: dict) -> str:
    db, mock = state["boxes"]["db"], state["boxes"]["monitor"]
    mock_url = f"http://{url_host(mock['ip'])}:{MOCK_PORT}"
    db_host = url_host(db["ip"])
    return "\n".join([
        f"DATABASE_URL=postgresql+psycopg2://shiksha:{state['db_password']}@{db_host}:5432/{DB_NAME}",
        f"JWT_SECRET_KEY={state['jwt_secret']}",
        "FRONTEND_ORIGIN=http://localhost:3000",
        "ENVIRONMENT=development",
        "COOKIE_SECURE=true",
        "COOKIE_SAMESITE=lax",
        "LOG_LEVEL=WARNING",
        "METRICS_ENABLED=true",
        "LLM_PROVIDER=gemini",
        "GEMINI_API_KEY=fake-key-for-load-test",
        f"GOOGLE_GEMINI_BASE_URL={mock_url}",
        "SARVAM_API_KEY=fake-key-for-load-test",
        f"SARVAM_BASE_URL={mock_url}",
        # Never dial anyone, whatever a scenario marks absent.
        "ABSENCE_CALLING_ENABLED=false",
    ]) + "\n"


def setup_backend_box(state: dict, role: str, kind: str) -> dict:
    box = state["boxes"].get(role)
    if box is None:
        box = create_box(kind, f"medha-lt-{role}")
        state["boxes"][role] = box
        save_state(state)
    allow_postgres_from(state, box)
    push_code(box)
    generator, monitor = state["boxes"]["generator"], state["boxes"]["monitor"]
    run(monitor, f"sudo ufw allow from {box['ip']} to any port {MOCK_PORT} proto tcp >/dev/null", quiet=True)
    run(box, f"""
export PATH=$HOME/.local/bin:$PATH
cd {HOST_PATH}/backend
cat > .env <<'ENV'
{backend_env(state)}ENV
uv sync --frozen -q --group loadtest
uv pip install -q --python .venv/bin/python py-spy
sudo ufw allow from {generator['ip']} to any port {BACKEND_PORT} proto tcp >/dev/null
sudo ufw allow from {monitor['ip']} to any port {EXPORTER_PORT} proto tcp >/dev/null
""", timeout=600, quiet=True)
    install_node_exporter(box, monitor["ip"])
    return box


def stop_backend_script() -> str:
    # Found by port rather than by pattern: a pkill pattern also matches this very script.
    return f"""
for port in {BACKEND_PORT} {EXPORTER_PORT}; do
  for pid in $(sudo ss -ltnpH "sport = :$port" | grep -oP 'pid=\\K[0-9]+' | sort -u); do sudo kill "$pid" || true; done
done
sleep 2
"""


def prepare_original_code(state: dict, role: str) -> None:
    """A clean checkout of the code before this kit, with no metrics: the control for
    "is this problem ours?". Only the environment points at the fake services."""
    box = state["boxes"][role]
    env = "".join(line + "\n" for line in backend_env(state).splitlines() if not line.startswith("METRICS_"))
    run(box, f"""
export PATH=$HOME/.local/bin:$PATH
if [ ! -d {HOST_PATH}/original ]; then
  git clone -q --filter=blob:none {ORIGINAL_REPO} {HOST_PATH}/original
fi
cd {HOST_PATH}/original
git checkout -q {ORIGINAL_COMMIT}
cd backend
cat > .env <<'ENV'
{env}ENV
uv sync --frozen -q
git rev-parse --short HEAD
""", timeout=600)


def snapshot_code(state: dict, role: str, name: str) -> None:
    """Keep what is deployed now under another name, so a later version can be compared
    with it on the same machine. Boat's host speed varies too much to compare across
    boxes. It gets its own venv, because the package installs by absolute path."""
    run(state["boxes"][role], f"""
export PATH=$HOME/.local/bin:$PATH
cd {HOST_PATH}
if [ ! -d {name} ]; then
  mkdir {name}
  tar --exclude=.venv --exclude=__pycache__ -C backend -cf - . | tar -C {name} -xf -
  cd {name}
  uv sync --frozen -q --group loadtest
fi
cd {HOST_PATH}/{name}
.venv/bin/python -c "import backend, inspect; print(inspect.getfile(backend))"
""", timeout=600)


def start_backend(state: dict, role: str, workers: int, *, original: bool = False, code: str = "backend") -> None:
    box = state["boxes"][role]
    if original:
        launch = f"""
cd {HOST_PATH}/original/backend
"""
    else:
        launch = f"""
cd {HOST_PATH}/{code}
# A new directory each start, so counters from the previous run cannot leak in.
export PROMETHEUS_MULTIPROC_DIR=/tmp/medha-metrics-$(date +%s)
mkdir -p "$PROMETHEUS_MULTIPROC_DIR"
export MEDHA_STALL_LOG={STALL_LOG}
: > {STALL_LOG}
setsid nohup .venv/bin/python loadtest/exporter.py --port {EXPORTER_PORT} > /tmp/exporter.log 2>&1 < /dev/null &
"""
    run(box, stop_backend_script() + launch + f"""
setsid nohup .venv/bin/python -m uvicorn backend.app:app --host :: --port {BACKEND_PORT} --workers {workers} \
  --proxy-headers --forwarded-allow-ips '*' > /tmp/uvicorn.log 2>&1 < /dev/null &
for i in $(seq 1 30); do curl -sf -g -m 3 'http://[::1]:{BACKEND_PORT}/health' >/dev/null && break; sleep 1; done
curl -sS -g -m 5 'http://[::1]:{BACKEND_PORT}/health' >/dev/null
""", timeout=120, quiet=True)
    state["active_backend"] = role
    state["active_workers"] = workers
    state["active_original"] = original
    save_state(state)
    write_prometheus_config(state)


def write_prometheus_config(state: dict) -> None:
    boxes = state["boxes"]
    role = state.get("active_backend")
    jobs = [
        ("load-generator", "locust", boxes["generator"]["ip"], LOCUST_METRICS_PORT),
        ("fake-llm", "mock", boxes["monitor"]["ip"], MOCK_PORT),
        ("postgres", "postgres", boxes["db"]["ip"], PG_EXPORTER_PORT),
    ]
    if role:
        jobs.append((role, "medha", boxes[role]["ip"], EXPORTER_PORT))
    machines = [(name, box["ip"]) for name, box in boxes.items()]
    scrape = []
    for instance, job, ip, port in jobs:
        scrape.append(_scrape_block(f"{job}", f"{url_host(ip)}:{port}", instance))
    for name, ip in machines:
        scrape.append(_scrape_block(f"node-{name}", f"{url_host(ip)}:{NODE_PORT}", name))
    config = "global:\n  scrape_interval: 5s\n  scrape_timeout: 4s\nscrape_configs:\n" + "".join(scrape)
    run(boxes["monitor"], f"""
cat > {HOST_PATH}/prometheus.yml <<'PROM'
{config}PROM
curl -s -m 5 -X POST http://127.0.0.1:9090/-/reload >/dev/null || true
""", quiet=True)


def _scrape_block(job: str, target: str, instance: str) -> str:
    return (f"  - job_name: {job}\n    static_configs:\n      - targets: ['{target}']\n"
            f"        labels: {{instance: {instance}}}\n")


def setup_monitor(state: dict) -> None:
    monitor = state["boxes"]["monitor"]
    push_code(monitor)
    subprocess.run([sys.executable, str(ROOT / "loadtest/monitoring/build_dashboard.py")], check=True)
    boat("push", monitor["id"], str(ROOT / "loadtest/monitoring"), "--to", f"{HOST_PATH}/monitoring", timeout=300)
    run(monitor, f"""
mkdir -p {HOST_PATH}
export PATH=$HOME/.local/bin:$PATH
cd {HOST_PATH}/backend/loadtest
uv venv -q --allow-existing /tmp/gen-venv
uv pip install -q --python /tmp/gen-venv/bin/python -r requirements-generator.txt
for pid in $(sudo ss -ltnpH "sport = :{MOCK_PORT}" | grep -oP 'pid=\\K[0-9]+' | sort -u); do sudo kill "$pid" || true; done
setsid nohup /tmp/gen-venv/bin/python -m uvicorn app:app --app-dir mock_services --host :: --port {MOCK_PORT} \
  --no-access-log > /tmp/mock.log 2>&1 < /dev/null &
sleep 2
curl -sS -g -m 5 'http://[::1]:{MOCK_PORT}/health' >/dev/null
touch {HOST_PATH}/prometheus.yml
sudo chmod -R a+rX {HOST_PATH}
sudo docker rm -f prometheus grafana >/dev/null 2>&1 || true
sudo docker run -d --name prometheus --restart unless-stopped --network host \
  -v {HOST_PATH}/prometheus.yml:/etc/prometheus/prometheus.yml:ro prom/prometheus:v2.53.0 \
  --config.file=/etc/prometheus/prometheus.yml --web.enable-lifecycle --storage.tsdb.retention.time=7d >/dev/null
sudo docker run -d --name grafana --restart unless-stopped --network host \
  -e GF_SECURITY_ADMIN_PASSWORD={state['grafana_password']} \
  -e GF_AUTH_ANONYMOUS_ENABLED=true -e GF_AUTH_ANONYMOUS_ORG_ROLE=Viewer \
  -e GF_PATHS_PROVISIONING=/etc/grafana/provisioning \
  -e GF_DASHBOARDS_DEFAULT_HOME_DASHBOARD_PATH=/var/lib/grafana/dashboards/medha.json \
  -e GF_INSTALL_PLUGINS= -e GF_ANALYTICS_REPORTING_ENABLED=false -e GF_ANALYTICS_CHECK_FOR_UPDATES=false \
  -v {HOST_PATH}/monitoring/grafana/provisioning:/etc/grafana/provisioning:ro \
  -v {HOST_PATH}/monitoring/grafana/dashboards:/var/lib/grafana/dashboards:ro \
  grafana/grafana:11.3.0 >/dev/null
for i in $(seq 1 45); do curl -sf -m 2 http://127.0.0.1:3000/api/health >/dev/null && break; sleep 2; done
curl -sS -m 3 http://127.0.0.1:3000/api/health >/dev/null
""", timeout=900, quiet=True)
    install_node_exporter(monitor, monitor["ip"])
    output = boat("host", monitor["id"], "3000", "--json", "--title", "Medha load test")
    for token in output.replace('"', " ").split():
        if token.startswith("https://"):
            state["dashboard_url"] = token
            break
    save_state(state)


def setup_generator(state: dict) -> None:
    generator, monitor, db = state["boxes"]["generator"], state["boxes"]["monitor"], state["boxes"]["db"]
    push_code(generator)
    run(generator, f"""
export PATH=$HOME/.local/bin:$PATH
sudo ufw allow from {monitor['ip']} to any port {LOCUST_METRICS_PORT} proto tcp >/dev/null
cd {HOST_PATH}/backend/loadtest
uv venv -q --allow-existing /tmp/gen-venv
uv pip install -q --python /tmp/gen-venv/bin/python -r requirements-generator.txt
""", timeout=600, quiet=True)
    install_node_exporter(generator, monitor["ip"])
    install_node_exporter(db, monitor["ip"])
    run(db, f"sudo ufw allow from {monitor['ip']} to any port {PG_EXPORTER_PORT} proto tcp >/dev/null", quiet=True)
    run(monitor, f"sudo ufw allow from {monitor['ip']} to any port {MOCK_PORT} proto tcp >/dev/null", quiet=True)


def seed(state: dict, schools: int, history_days: int) -> None:
    db = state["boxes"]["db"]
    box = state["boxes"]["backend_default"]
    # Detached and polled, because a large seed outlasts any single remote call.
    run(box, f"""
export PATH=$HOME/.local/bin:$PATH
cd {HOST_PATH}/backend
set -a; . ./.env; set +a
.venv/bin/python -W ignore -m alembic upgrade head 2>&1 | tail -1
.venv/bin/python -W ignore scripts/seed_phase0.py >/dev/null
.venv/bin/python -W ignore scripts/seed_subjects_extra.py >/dev/null
.venv/bin/python -W ignore scripts/seed_curriculum.py >/dev/null
.venv/bin/python -W ignore scripts/seed_library.py >/dev/null
rm -f /tmp/seed.done /tmp/manifest.json
setsid nohup bash -c '.venv/bin/python -W ignore loadtest/seed/seed.py --schools {schools} --history-days {history_days} \
  --jobs {SEED_JOBS} --out /tmp/manifest.json > /tmp/seed.log 2>&1; echo $? > /tmp/seed.done' > /dev/null 2>&1 < /dev/null &
""", timeout=1800)
    deadline = time.time() + 8 * 3600
    while True:
        done = run(box, "cat /tmp/seed.done 2>/dev/null || true; tail -1 /tmp/seed.log 2>/dev/null || true",
                   quiet=True, check=False).splitlines()
        if done and done[0].strip().isdigit():
            if done[0].strip() != "0":
                raise SystemExit("seeding failed:\n" + run(box, "tail -30 /tmp/seed.log", quiet=True, check=False))
            break
        print(f"  {done[-1] if done else 'starting'}", flush=True)
        if time.time() > deadline:
            raise SystemExit("seeding did not finish in 8 hours")
        time.sleep(120)
    print(run(box, "tail -30 /tmp/seed.log", quiet=True))
    # Move the manifest through this machine: boxes hold no credentials for each other.
    local = STATE_FILE.with_name(".manifest.json")
    boat("scp", f"{box['id']}:/tmp/manifest.json", str(local), timeout=600)
    boat("scp", str(local), f"{state['boxes']['generator']['id']}:/tmp/manifest.json", timeout=600)
    # An exact copy to restore from, so every run starts on identical data.
    run(db, f"""
sudo docker stop pg-exporter >/dev/null
sudo docker exec pg psql -q -U shiksha -d postgres -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='{DB_NAME}'" >/dev/null
sudo docker exec pg psql -q -U shiksha -d postgres -c 'CHECKPOINT'
sudo docker exec pg psql -q -U shiksha -d postgres -c 'CREATE DATABASE {DB_TEMPLATE} TEMPLATE {DB_NAME} STRATEGY FILE_COPY'
sudo docker start pg-exporter >/dev/null
sudo docker exec pg psql -At -U shiksha -d postgres -c "SELECT pg_size_pretty(pg_database_size('{DB_NAME}'))"
df -h / | tail -1
""", timeout=7200)
    state["schools"] = schools
    save_state(state)


def reset_database(state: dict) -> None:
    """Restore the seed data. FILE_COPY rather than the default, which pushes the whole
    copy through the WAL and leaves the disk busy well into the next run. The checkpoint
    is so the run does not start with that work still pending."""
    script = f"""
sudo docker stop pg-exporter >/dev/null || true
sudo docker exec pg psql -q -U shiksha -d postgres -c 'DROP DATABASE IF EXISTS {DB_NAME} WITH (FORCE)'
sudo docker exec pg psql -q -U shiksha -d postgres -c 'CREATE DATABASE {DB_NAME} TEMPLATE {DB_TEMPLATE} STRATEGY FILE_COPY'
sudo docker exec pg psql -q -U shiksha -d {DB_NAME} -c 'CREATE EXTENSION IF NOT EXISTS pg_stat_statements'
sudo docker exec pg psql -q -U shiksha -d {DB_NAME} -c 'SELECT pg_stat_statements_reset()' >/dev/null
sudo docker exec pg psql -q -U shiksha -d postgres -c 'CHECKPOINT'
sudo docker start pg-exporter >/dev/null
"""
    for attempt in (1, 2, 3):
        try:
            run(state["boxes"]["db"], script, timeout=1800, quiet=True)
            return
        except SystemExit as exc:
            print(f"  restoring the database failed (attempt {attempt}): {exc}", flush=True)
            if attempt == 3:
                raise
            time.sleep(30)


def check_paths(state: dict, role: str) -> None:
    """Boat sometimes puts two boxes on hosts with no route between them. A backend that
    cannot reach the fake LLM still answers everything else, so the run looks plausible
    while measuring nothing real: refuse to run it."""
    boxes = state["boxes"]
    box = boxes[role]
    mock_host, db_ip = url_host(boxes["monitor"]["ip"]), boxes["db"]["ip"]
    run(box, f"""
curl -sSf -g -m 8 'http://{mock_host}:{MOCK_PORT}/health' >/dev/null
python3 -c "import socket; socket.create_connection(('{db_ip}', 5432), 5)"
""", timeout=60, quiet=True)
    host = url_host(box["ip"])
    run(boxes["monitor"], f"curl -sSf -g -m 8 'http://{host}:{EXPORTER_PORT}/metrics' >/dev/null", timeout=60, quiet=True)
    run(boxes["generator"], f"curl -sSf -g -m 8 'http://{host}:{BACKEND_PORT}/health' >/dev/null", timeout=60, quiet=True)


def stop_traffic(state: dict) -> None:
    """Locust workers outlive their master by a minute, and would hit the backend and
    the database all through the restore that comes next."""
    run(state["boxes"]["generator"], "pkill -x locust || true", check=False, quiet=True)
    role = state.get("active_backend")
    if role:
        run(state["boxes"][role], stop_backend_script(), check=False, quiet=True)


def _prom_value(state: dict, query: str) -> float | None:
    rows = prometheus_query(state, query, time.time())
    if not rows or rows[0]["value"][1] in ("NaN", "+Inf", "-Inf"):
        return None
    return float(rows[0]["value"][1])


def broken_now(state: dict, p95_limit_ms: float) -> str | None:
    """Why this run has clearly failed, or None. Once it has, more minutes at higher
    load teach nothing and only cost money."""
    users = _prom_value(state, "locust_users")
    if not users:
        return None
    total = 'name="Aggregated"'
    rate = _prom_value(state, f"rate(locust_requests_total{{{total}}}[1m])") or 0
    failures = _prom_value(state, f"rate(locust_failures_total{{{total}}}[1m])") or 0
    p95 = _prom_value(state, f"locust_response_p95_ms{{{total}}}") or 0
    if rate < 0.5:
        return f"almost nothing completing: {rate:.2f} req/s with {users:.0f} users"
    if failures / rate > BREAK_FAILURE_SHARE:
        return f"{failures / rate:.0%} of requests failing with {users:.0f} users"
    if p95 > p95_limit_ms * BREAK_P95_MULTIPLE:
        return f"p95 {p95 / 1000:.1f}s with {users:.0f} users"
    return None


def capture_if_slow(state: dict, name: str, p95_limit_ms: float, taken: list[float]) -> None:
    """Record who is waiting for whom while latency is high, as a spike can be gone by
    the time anyone looks. Only the first few, because each one costs a few seconds."""
    if len(taken) >= CAPTURE_MAX_PER_RUN or (taken and time.time() - taken[-1] < CAPTURE_MIN_GAP_SECONDS):
        return
    p95 = _prom_value(state, 'locust_response_p95_ms{name="Aggregated"}') or 0
    if p95 < p95_limit_ms * CAPTURE_P95_MULTIPLE:
        return
    taken.append(time.time())
    out_dir = RESULTS / name
    out_dir.mkdir(parents=True, exist_ok=True)
    users = _prom_value(state, "locust_users") or 0
    (out_dir / f"capture-{len(taken)}.txt").write_text(
        f"at {taken[-1]:.0f}, {users:.0f} users, p95 {p95:.0f}ms\n{capture_freeze(state)}")


def wait_for_locust(state: dict, deadline_seconds: int, *, stop_when_broken: bool,
                    p95_limit_ms: float = 1500, capture_as: str | None = None) -> str | None:
    """Wait for the run to end. Returns why it was stopped early, if it was."""
    generator = state["boxes"]["generator"]
    deadline = time.time() + deadline_seconds
    broken_since = None
    reason = None
    captures: list[float] = []
    while time.time() < deadline:
        running = run(generator, f"sudo ss -ltnH 'sport = :{LOCUST_MASTER_PORT}' | wc -l", quiet=True).strip()
        if running == "0":
            run(generator, "pkill -x locust || true", check=False, quiet=True)
            return reason
        time.sleep(15)
        if capture_as:
            capture_if_slow(state, capture_as, p95_limit_ms, captures)
        if not stop_when_broken:
            continue
        why = broken_now(state, p95_limit_ms)
        if why is None:
            broken_since = None
        elif broken_since is None:
            broken_since = time.time()
        elif time.time() - broken_since >= BREAK_CONFIRM_SECONDS:
            reason = why
            print(f"  stopping early: {why}")
            break
    else:
        reason = "ran past its deadline"
        print("  run went past its deadline; stopping")
    # SIGTERM lets the master write its CSV files; give it time before forcing.
    run(generator, "sudo ss -ltnpH 'sport = :%d' | grep -oP 'pid=\\K[0-9]+' | sort -u | xargs -r kill || true" % LOCUST_MASTER_PORT,
        check=False, quiet=True)
    time.sleep(20)
    run(generator, "pkill -x locust || true", check=False, quiet=True)
    return reason


def start_locust(state: dict, name: str, env: dict[str, str]) -> None:
    generator = state["boxes"]["generator"]
    backend = state["boxes"][state["active_backend"]]
    host = f"http://{url_host(backend['ip'])}:{BACKEND_PORT}"
    exports = "\n".join(f"export {key}={shlex.quote(value)}" for key, value in env.items())
    # Matched by exact process name: `pkill -f` would also match this script's own text.
    run(generator, f"""
pkill -x locust || true
sleep 1
mkdir -p /tmp/results/{name}
cd {HOST_PATH}/backend/loadtest/scenarios
export LT_MANIFEST=/tmp/manifest.json LT_WORKERS={GENERATOR_WORKERS} LT_METRICS_PORT={LOCUST_METRICS_PORT}
{exports}
for i in $(seq 1 {GENERATOR_WORKERS}); do
  setsid nohup /tmp/gen-venv/bin/locust -f locustfile.py --worker --master-host 127.0.0.1 > /tmp/results/{name}/worker$i.log 2>&1 < /dev/null &
done
setsid nohup /tmp/gen-venv/bin/locust -f locustfile.py --master --expect-workers {GENERATOR_WORKERS} --headless \
  --host {shlex.quote(host)} --csv /tmp/results/{name}/run --html /tmp/results/{name}/report.html \
  --stop-timeout 30 > /tmp/results/{name}/master.log 2>&1 < /dev/null &
# Every process loads the manifest first, which takes a while at thousands of schools.
for i in $(seq 1 120); do
  sudo ss -ltnH 'sport = :{LOCUST_MASTER_PORT}' | grep -q . && break
  sleep 1
done
sudo ss -ltnH 'sport = :{LOCUST_MASTER_PORT}' | grep -q . || (tail -25 /tmp/results/{name}/master.log; exit 1)
""", timeout=240, quiet=True)


def prometheus_query(state: dict, query: str, at: float) -> list:
    encoded = quote(query, safe="")
    out = run(state["boxes"]["monitor"],
              f"curl -sS -m 20 'http://127.0.0.1:9090/api/v1/query?query={encoded}&time={at}'", quiet=True)
    return json.loads(out).get("data", {}).get("result", [])


def prometheus_range(state: dict, query: str, start: float, end: float, step: int = 5) -> list:
    encoded = quote(query, safe="")
    out = run(state["boxes"]["monitor"],
              f"curl -sS -m 30 'http://127.0.0.1:9090/api/v1/query_range?query={encoded}"
              f"&start={start}&end={end}&step={step}'", quiet=True)
    rows = json.loads(out).get("data", {}).get("result", [])
    return rows[0]["values"] if rows else []


def feature_timeline(state: dict, started: float, finished: float) -> dict[str, list]:
    """p95 per feature over time, to tell whether a spike hits everything or one feature."""
    query = quote("histogram_quantile(0.95, sum by (le, feature) (rate(medha_http_request_duration_seconds_bucket[30s])))",
                  safe="")
    out = run(state["boxes"]["monitor"],
              f"curl -sS -m 60 'http://127.0.0.1:9090/api/v1/query_range?query={query}"
              f"&start={started}&end={finished}&step=30'", quiet=True)
    return {
        row["metric"].get("feature", "?"): [[round(float(at) - started), value] for at, value in row["values"]
                                            if value not in ("NaN", "+Inf", "-Inf")]
        for row in json.loads(out).get("data", {}).get("result", [])
    }


def build_timeline(state: dict, started: float, finished: float) -> list[dict]:
    """A sample every 5 seconds. Locust's own history file reports running-total
    percentiles, which blur what each load level did, so these come from short windows."""
    total = 'name="Aggregated"'
    backend = state.get("active_backend")
    series = {
        # Cores the host gave to someone else. A run with much of this measured boat, not the backend.
        "backend_steal": f'sum(rate(node_cpu_seconds_total{{instance="{backend}",mode="steal"}}[30s]))',
        "backend_idle": f'sum(rate(node_cpu_seconds_total{{instance="{backend}",mode="idle"}}[30s]))',
        "users": "locust_users",
        "rps": f"rate(locust_requests_total{{{total}}}[30s])",
        "failures": f"rate(locust_failures_total{{{total}}}[30s])",
        "p50_ms": f"locust_response_p50_ms{{{total}}}",
        "p95_ms": f"locust_response_p95_ms{{{total}}}",
        "backend_p95_s": "histogram_quantile(0.95, sum by (le) (rate(medha_http_request_duration_seconds_bucket[30s])))",
        "backend_cores": "sum(rate(medha_worker_cpu_seconds[30s]))",
        "slots_queued": "sum(medha_db_slots_waiting)",
        "loop_stalled_s": "max(medha_event_loop_stalled_seconds)",
        # If either is near 1, the run measured that machine rather than the backend.
        "generator_cpu": '1 - avg(rate(node_cpu_seconds_total{instance="generator",mode="idle"}[30s]))',
        "db_cpu": '1 - avg(rate(node_cpu_seconds_total{instance="db",mode="idle"}[30s]))',
        "db_iowait": 'avg(rate(node_cpu_seconds_total{instance="db",mode="iowait"}[30s]))',
        "db_disk_write_mb": 'sum(rate(node_disk_written_bytes_total{instance="db"}[30s])) / 1e6',
        "monitor_cpu": '1 - avg(rate(node_cpu_seconds_total{instance="monitor",mode="idle"}[30s]))',
        "threads_queued": "sum(medha_threadpool_waiting)",
        "pool_in_use": "sum(medha_db_pool_checked_out)",
        # Per worker: one saturated worker among idle ones does not show in the totals.
        "busiest_worker_cores": "max(rate(medha_worker_cpu_seconds[30s]))",
        "idlest_worker_cores": "min(rate(medha_worker_cpu_seconds[30s]))",
        "most_queued_worker": "max(medha_db_slots_waiting)",
        "least_queued_worker": "min(medha_db_slots_waiting)",
    }
    samples: dict[float, dict] = {}
    for label, query in series.items():
        for at, value in prometheus_range(state, query, started, finished):
            if value not in ("NaN", "+Inf", "-Inf"):
                samples.setdefault(float(at), {"t": float(at) - started})[label] = float(value)
    return [samples[at] for at in sorted(samples)]


def collect_results(state: dict, name: str, env: dict, started: float, finished: float,
                    stopped_early: str | None = None) -> None:
    """Save what the run measured, from the backend's own numbers."""
    out_dir = RESULTS / name
    out_dir.mkdir(parents=True, exist_ok=True)
    window = f"{max(int(finished - started), 60)}s"
    queries = {
        "requests_per_second_by_feature": f"sum by (feature) (rate(medha_http_requests_total[{window}]))",
        "p50_by_feature": f"histogram_quantile(0.5, sum by (le, feature) (rate(medha_http_request_duration_seconds_bucket[{window}])))",
        "p95_by_feature": f"histogram_quantile(0.95, sum by (le, feature) (rate(medha_http_request_duration_seconds_bucket[{window}])))",
        "p99_by_feature": f"histogram_quantile(0.99, sum by (le, feature) (rate(medha_http_request_duration_seconds_bucket[{window}])))",
        "errors_by_feature_status": f'sum by (feature, status) (increase(medha_http_requests_total{{status=~"5..|429"}}[{window}]))',
        "requests_by_route": f"sum by (route, status) (increase(medha_http_requests_total[{window}]))",
        "p95_by_route": f"histogram_quantile(0.95, sum by (le, route) (rate(medha_http_request_duration_seconds_bucket[{window}])))",
        "pool_timeouts": f"sum(increase(medha_db_pool_timeouts_total[{window}]))",
        "pool_in_use_max": f"max_over_time(sum(medha_db_pool_checked_out)[{window}:5s])",
        "threads_queued_max": f"max_over_time(sum(medha_threadpool_waiting)[{window}:5s])",
        "slots_queued_max": f"max_over_time(sum(medha_db_slots_waiting)[{window}:5s])",
        "slots_rejected": f"sum(increase(medha_db_slots_rejected_total[{window}]))",
        "loop_stall_max": f"max_over_time(max(medha_event_loop_stalled_seconds)[{window}:5s])",
        "backend_cores_used": f"sum(rate(medha_worker_cpu_seconds[{window}]))",
        "backend_rss_max": f"max(max_over_time(medha_worker_rss_bytes[{window}]))",
        "machine_cpu_busy": f'1 - avg by (instance) (rate(node_cpu_seconds_total{{mode="idle"}}[{window}]))',
        "peak_users": f"max_over_time(locust_users[{window}])",
    }
    summary = {"name": name, "env": env, "started": started, "finished": finished,
               "backend": state.get("active_backend"), "workers": state.get("active_workers"),
               "backend_type": state["boxes"][state["active_backend"]]["type"],
               "original_code": bool(state.get("active_original")), "stopped_early": stopped_early, "metrics": {}}
    for label, query in queries.items():
        summary["metrics"][label] = [
            {"labels": row["metric"], "value": float(row["value"][1])}
            for row in prometheus_query(state, query, finished)
            if row["value"][1] not in ("NaN", "+Inf", "-Inf")
        ]
    summary["timeline"] = build_timeline(state, started, finished)
    summary["feature_timeline"] = feature_timeline(state, started, finished)
    backend_box = state["boxes"][state["active_backend"]]
    summary["stalls"] = int(run(backend_box, f"grep -c '^--- pid' {STALL_LOG} || true", check=False, quiet=True).strip() or 0)
    boat("scp", f"{backend_box['id']}:{STALL_LOG}", str(out_dir / "stalls.txt"), timeout=120, check=False)
    summary["checkpoints"] = run(state["boxes"]["db"],
                                 f"sudo docker logs pg --since {int(started)} 2>&1 | grep -E 'checkpoint (starting|complete)' | tail -60 || true",
                                 check=False, quiet=True)
    summary["slow_sql"] = run(state["boxes"]["db"], f"""
sudo docker exec pg psql -U shiksha -d {DB_NAME} -At -F '|' -c "
SELECT round(total_exec_time::numeric/1000,2), calls, round(mean_exec_time::numeric,2), left(regexp_replace(query, '\\s+', ' ', 'g'), 200)
FROM pg_stat_statements ORDER BY total_exec_time DESC LIMIT 15"
""", quiet=True, check=False)
    generator = state["boxes"]["generator"]
    # A run where the load generator itself stalled says nothing about the backend.
    summary["generator"] = {
        "heartbeat_misses": int(run(generator, f"grep -c 'failed to send heartbeat' /tmp/results/{name}/master.log || true",
                                    check=False, quiet=True).strip() or 0),
        "lost_all_workers": "went missing" in run(generator, f"grep -m1 'last worker went missing' /tmp/results/{name}/master.log || true",
                                                  check=False, quiet=True),
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=1))
    for filename in ("run_stats.csv", "run_stats_history.csv", "run_failures.csv", "report.html", "master.log"):
        boat("scp", f"{generator['id']}:/tmp/results/{name}/{filename}", str(out_dir / filename),
             timeout=120, check=False)
    print(f"saved {out_dir}")


def capture_freeze(state: dict) -> str:
    """Everything that shows who is waiting for whom, at this instant."""
    backend = state["boxes"][state["active_backend"]]
    code = "original/backend" if state.get("active_original") else "backend"
    stacks = run(backend, f"""
export PATH=$HOME/.local/bin:$PATH
cd {HOST_PATH}/{code}
uv pip install -q --python .venv/bin/python py-spy
for pid in $(sudo ss -ltnpH 'sport = :{BACKEND_PORT}' | grep -oP 'pid=\\K[0-9]+' | sort -u); do
  echo "=== process $pid"
  sudo .venv/bin/py-spy dump --nonblocking --pid "$pid" || true
done
""", timeout=120, quiet=True, check=False)
    database = run(state["boxes"]["db"], f"""
sudo docker exec pg psql -U shiksha -d {DB_NAME} -At -F ' | ' -c "
SELECT state, wait_event_type, count(*), round(max(extract(epoch FROM now()-state_change))::numeric,1) AS longest_s,
       left(regexp_replace(min(query), '\\s+', ' ', 'g'), 110)
FROM pg_stat_activity WHERE datname='{DB_NAME}' AND pid<>pg_backend_pid()
GROUP BY 1,2 ORDER BY 3 DESC"
""", quiet=True, check=False)
    sockets = run(backend, f"""
echo "client sockets by state:"; ss -tan '( sport = :{BACKEND_PORT} )' | awk 'NR>1{{print $1}}' | sort | uniq -c
echo "postgres sockets by state:"; ss -tan '( dport = :5432 )' | awk 'NR>1{{print $1}}' | sort | uniq -c
""", quiet=True, check=False)
    return f"--- threads\n{stacks}\n--- database connections\n{database}\n--- sockets\n{sockets}"


def cmd_capture(args) -> None:
    """Start a run, wait, capture the state of the backend, then let it finish."""
    state = load_state()
    env = dict(pair.split("=", 1) for pair in args.env)
    reset_database(state)
    start_backend(state, state["active_backend"], state["active_workers"],
                  original=bool(state.get("active_original")))
    started = time.time()
    start_locust(state, args.name, env)
    time.sleep(args.after)
    text = capture_freeze(state)
    out_dir = RESULTS / args.name
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "capture.txt").write_text(text)
    print(f"captured {out_dir / 'capture.txt'}")
    reason = wait_for_locust(state, args.max_seconds, stop_when_broken=True)
    finished = time.time()
    time.sleep(10)
    collect_results(state, args.name, env, started, finished, reason)


def reseed(state: dict, schools: int, history_days: int) -> None:
    """Empty the database and fill it again at a new size."""
    run(state["boxes"]["db"], f"""
sudo docker stop pg-exporter >/dev/null
sudo docker exec pg psql -q -U shiksha -d postgres -c 'DROP DATABASE IF EXISTS {DB_NAME} WITH (FORCE)'
sudo docker exec pg psql -q -U shiksha -d postgres -c 'DROP DATABASE IF EXISTS {DB_TEMPLATE} WITH (FORCE)'
sudo docker exec pg psql -q -U shiksha -d postgres -c 'CREATE DATABASE {DB_NAME}'
sudo docker exec pg psql -q -U shiksha -d {DB_NAME} -c 'CREATE EXTENSION IF NOT EXISTS pg_stat_statements'
sudo docker start pg-exporter >/dev/null
""", timeout=600, quiet=True)
    seed(state, schools, history_days)


def cmd_reseed(args) -> None:
    state = load_state()
    reseed(state, args.schools, args.history_days)


def cmd_sweep(args) -> None:
    """Run a list of tests, from a JSON file, unattended. A failure in one test is
    recorded and the rest continue; a test with saved results is skipped, so an
    interrupted sweep resumes where it stopped."""
    state = load_state()
    plan = json.loads(Path(args.plan).read_text())
    prepared: set[str] = set()
    for number, test in enumerate(plan, start=1):
        name = test["name"]
        if (RESULTS / name / "summary.json").exists():
            print(f"[{number}/{len(plan)}] {name}: already done")
            continue
        print(f"[{number}/{len(plan)}] {name}", flush=True)
        try:
            size, workers = test["config"].split(":")
            role = f"backend_{size}"
            if test.get("original") and role not in prepared:
                prepare_original_code(state, role)
                prepared.add(role)
            stop_traffic(state)
            if test.get("restore", True):
                reset_database(state)
            else:
                # Rough runs: a restore takes minutes, and one run adds little to this much data.
                run(state["boxes"]["db"], f"sudo docker exec pg psql -q -U shiksha -d {DB_NAME} "
                                          "-c 'SELECT pg_stat_statements_reset()' >/dev/null", quiet=True)
            start_backend(state, role, int(workers), original=test.get("original", False),
                          code=test.get("code", "backend"))
            check_paths(state, role)
            started = time.time()
            start_locust(state, name, test["env"])
            reason = wait_for_locust(state, test.get("max_seconds", 1800), stop_when_broken=True,
                                     p95_limit_ms=float(test["env"].get("LT_P95_LIMIT_MS", 1500)), capture_as=name)
            finished = time.time()
            time.sleep(10)
            collect_results(state, name, test["env"], started, finished, reason)
        except (SystemExit, Exception) as exc:  # one bad test must not lose a long sweep
            print(f"  FAILED: {exc}", flush=True)
            run(state["boxes"]["generator"], "pkill -x locust || true", check=False, quiet=True)
    print("sweep finished")


def cmd_up(args) -> None:
    state = load_state()
    state.setdefault("db_password", secrets.token_hex(16))
    state.setdefault("jwt_secret", secrets.token_hex(32))
    state.setdefault("grafana_password", secrets.token_urlsafe(12))
    boxes = state["boxes"]
    print("creating boxes (rejecting slow disks for Postgres)")
    boxes.setdefault("db", create_db_box())
    save_state(state)
    boxes.setdefault("generator", create_box("large", "medha-lt-generator"))
    boxes.setdefault("monitor", create_box("default", "medha-lt-monitor"))
    save_state(state)
    print("installing Postgres"); setup_database(state)
    print("installing monitoring and the fake LLM"); setup_monitor(state)
    print("installing the load generator"); setup_generator(state)
    # Two of each size, so repeats also cover a second machine: boat's hosts vary in speed.
    for role, kind in BACKEND_BOXES:
        print(f"installing {role}"); setup_backend_box(state, role, kind)
    write_prometheus_config(state)
    print(f"seeding {args.schools} schools"); seed(state, args.schools, args.history_days)
    print("ready:", state.get("dashboard_url"))


def cmd_backend(args) -> None:
    state = load_state()
    role = f"backend_{args.size}"
    if args.original:
        prepare_original_code(state, role)
    start_backend(state, role, args.workers, original=args.original)
    print(f"{role} running with {args.workers} worker(s){' on the original code' if args.original else ''}")


def cmd_redeploy(_args) -> None:
    """Push code changes to the backend boxes and the generator."""
    state = load_state()
    roles = [role for role, _ in BACKEND_BOXES if role in state["boxes"]]
    for role in ("generator", "monitor", *roles):
        if role in state["boxes"]:
            push_code(state["boxes"][role])
    for role in roles:
        run(state["boxes"][role], f"""
export PATH=$HOME/.local/bin:$PATH
cd {HOST_PATH}/backend
uv sync --frozen -q --group loadtest
""", timeout=300, quiet=True)
    print("pushed")


def cmd_run(args) -> None:
    state = load_state()
    if not state.get("active_backend"):
        raise SystemExit("start a backend first: lt.py backend small 1")
    env = dict(pair.split("=", 1) for pair in args.env)
    if not args.keep_data:
        print("restoring the database"); reset_database(state)
    print("restarting the backend")
    start_backend(state, state["active_backend"], state["active_workers"], original=bool(state.get("active_original")))
    started = time.time()
    print(f"running {args.name}: {env}")
    print(f"watch: {state.get('dashboard_url')}")
    start_locust(state, args.name, env)
    reason = wait_for_locust(state, args.max_seconds, stop_when_broken=args.stop_when_broken)
    finished = time.time()
    time.sleep(10)  # one last scrape
    collect_results(state, args.name, env, started, finished, reason)


def cmd_link(_args) -> None:
    state = load_state()
    print(state.get("dashboard_url") or "no dashboard yet")
    print(f"Grafana admin password (viewing needs none): {state.get('grafana_password')}")


def cmd_status(_args) -> None:
    state = load_state()
    print(f"active: {state.get('active_backend')} x{state.get('active_workers')}, schools: {state.get('schools')}")
    for name, box in state["boxes"].items():
        print(f"  {name:18s} {box['id']:14s} {box['type']:8s} {box['ip']}")


def _snapshot_count(box_ids: set[str]) -> dict[str, int]:
    output = boat("snapshots", "--all", "--json", timeout=300)
    data = json.loads(output)
    rows = data.get("snapshots", []) if isinstance(data, dict) else data
    counts: dict[str, int] = {}
    for row in rows:
        if row.get("sandboxId") in box_ids:
            counts[row["sandboxId"]] = counts.get(row["sandboxId"], 0) + 1
    return counts


def cmd_down(_args) -> None:
    """Delete this environment's boxes and their snapshots, then check nothing remains.
    Only touches the boxes recorded in the state file. State is kept if anything is
    left, so this can simply be run again."""
    state = load_state()
    boxes = state["boxes"]
    ids = {box["id"] for box in boxes.values()}
    for name, box in boxes.items():
        print(f"deleting {name} ({box['id']}): snapshots, then the box")
        boat("snapshot", "delete", "--all", box["id"], "--yes", "--json", timeout=600, check=False)
        boat("delete", box["id"], "--yes", "--json", check=False)

    # Deletion finishes in the background.
    deadline = time.time() + 900
    live, snapshots = ids, {}
    while time.time() < deadline:
        listing = json.loads(boat("list", "--json"))
        live = ids & {box["id"] for box in listing.get("sandboxes", [])}
        snapshots = _snapshot_count(ids)
        if not live and not snapshots:
            break
        time.sleep(30)

    if live or snapshots:
        print(f"NOT CLEAN: boxes still listed {sorted(live)}, snapshots remaining {snapshots}")
        raise SystemExit(1)
    print(f"clean: {len(ids)} boxes deleted, 0 snapshots remaining for them")
    STATE_FILE.unlink(missing_ok=True)
    STATE_FILE.with_name(".manifest.json").unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    up = sub.add_parser("up", help="create, install and seed")
    up.add_argument("--schools", type=int, default=50)
    up.add_argument("--history-days", type=int, default=120)
    up.set_defaults(func=cmd_up)

    backend = sub.add_parser("backend", help="choose the configuration under test")
    backend.add_argument("size", choices=("small", "default"))
    backend.add_argument("workers", type=int)
    backend.add_argument("--original", action="store_true", help="the code before this kit, with no metrics")
    backend.set_defaults(func=cmd_backend)

    run_parser = sub.add_parser("run", help="run a test and save its results")
    run_parser.add_argument("name")
    run_parser.add_argument("env", nargs="*", help="settings such as LT_SCENARIO=day LT_SCHOOLS=10")
    run_parser.add_argument("--keep-data", action="store_true", help="skip restoring the database")
    run_parser.add_argument("--max-seconds", type=int, default=7200)
    run_parser.add_argument("--stop-when-broken", action="store_true", help="end once most requests are failing")
    run_parser.set_defaults(func=cmd_run)

    capture = sub.add_parser("capture", help="run a test and record thread and database state partway through")
    capture.add_argument("name")
    capture.add_argument("env", nargs="*")
    capture.add_argument("--after", type=int, default=180, help="seconds into the run to capture")
    capture.add_argument("--max-seconds", type=int, default=900)
    capture.set_defaults(func=cmd_capture)

    reseed_parser = sub.add_parser("reseed", help="empty the database and fill it at a new size")
    reseed_parser.add_argument("--schools", type=int, required=True)
    reseed_parser.add_argument("--history-days", type=int, default=120)
    reseed_parser.set_defaults(func=cmd_reseed)

    sweep = sub.add_parser("sweep", help="run many tests from a JSON plan")
    sweep.add_argument("plan")
    sweep.set_defaults(func=cmd_sweep)

    sub.add_parser("redeploy", help="push code changes").set_defaults(func=cmd_redeploy)
    sub.add_parser("link", help="print the dashboard link").set_defaults(func=cmd_link)
    sub.add_parser("status", help="show the boxes").set_defaults(func=cmd_status)
    sub.add_parser("down", help="delete all boxes").set_defaults(func=cmd_down)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
