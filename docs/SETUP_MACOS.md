# Setup on macOS — without Docker

This is the **recommended** path on any Mac, and the only practical one on 8 GB.
Every number in this repository's evaluation was measured on this path.

---

## Why not Docker here

Two independent reasons, both decisive on Apple Silicon.

### 1. The VM takes memory the model needs

Docker on macOS is not native: it runs a Linux VM, and that VM reserves RAM up
front whether or not a container is using it. colima defaults to 2 GiB, Docker
Desktop to 8 GB (typically reduced to 2-4 GB by hand).

On this machine:

```
physical memory                                        8.00 GB
reserved for macOS, Postgres, Node, browser          - 2.90 GB
                                                     ---------
budget for the model                                   5.10 GB

qwen2.5-coder-7b Q4_K_M at ctx 6144 needs              5.03 GB
                                                       margin  +0.07 GB
```

The margin is 70 MB. Add a 2 GiB VM and the budget falls to ~3.1 GB — the 7B
does not fit at all, and `model/preflight.py` will refuse to launch it. You
would be left with the 3B, which scores 35.3% against the 7B's 69.1%.

### 2. Containers get no GPU on Apple Silicon

The Linux VM cannot reach Metal. A containerised `llama-server` runs **CPU-only**,
several times slower than the ~5.9 tok/s the native path achieves — and that is
already the slow part of every turn.

So on a Mac, Docker costs you the memory *and* the accelerator. That is why
this project ships **no container setup at all** — see README, "Why there is no
Docker setup", for the full reasoning and what it trades away.

---

## Prerequisites

```bash
# Homebrew, if not already installed
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

brew install llama.cpp postgresql@17 node
brew install --cask font-sf-mono   # optional

# uv — Python toolchain
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Versions this was built and measured against:

| Tool | Version |
|---|---|
| llama.cpp (`llama-server`) | 0.4.0 (build 10809) |
| PostgreSQL | 17.9 |
| Node | 24.14.1 |
| Python | 3.13 (via uv) |
| macOS | 15, Apple M1 |

Start Postgres and leave it running:

```bash
brew services start postgresql@17
pg_isready          # expect: accepting connections
```

---

## Install

```bash
git clone <repo> && cd satsure-assg
make setup          # uv sync + npm install
```

## Database

```bash
make db
```

Drops and recreates `agri_insights`, applies `db/schema.sql` (DDL plus the
column comments that document every data trap), loads the seven CSV extracts,
and creates the read-only `agent_ro` role.

Verify the guardrail actually holds — this is worth doing once rather than
trusting it:

```bash
PGPASSWORD=agent_ro psql "postgresql://agent_ro@localhost/agri_insights" \
  -c "delete from farmer;"
# expect: ERROR: cannot execute DELETE in a read-only transaction
```

## Model weights

```bash
# Just the one you need — 4.36 GB, ~20 minutes at 4 MB/s
uv run python model/download.py --only qwen2.5-coder-7b

# Or the whole bake-off roster — 25 GB, ~1h45
make model
```

Weights land in `model/gguf/` as symlinks into the Hugging Face cache, so
nothing is duplicated.

Check what your machine can actually run:

```bash
uv run python model/preflight.py --all
```

On 8 GB this admits the 7B and 3B and refuses both 8B models — they OOM Metal,
and because Metal shares unified memory with the window server, that can take
the display down. The refusal is deliberate.

---

## Run — three terminals

```bash
# 1. model runtime
make serve                    # llama-server on :8080, ~9s to healthy

# 2. agent API
make api                      # FastAPI on :8000

# 3. web UI
make web                      # Vite on :5173
```

Open <http://localhost:5173>.

Check everything is up before using it:

```bash
curl -s localhost:8000/api/health | python3 -m json.tool
```

```json
{
  "ok": true,
  "model": {"ok": true, "model": "qwen2.5-coder-7b.gguf", "ctx": 6144},
  "database": {"ok": true, "error": null},
  "catalog_version": 5
}
```

If you script the startup yourself, wait on the **status code**, not on curl's
exit code — `llama-server` returns HTTP 503 while loading weights and
`curl -s -o /dev/null <url>` exits 0 on a 503. Use the helper:

```bash
./model/wait_healthy.sh http://127.0.0.1:8080/health 300
```

---

## Expect it to be slow

On an 8 GB M1 a single turn takes **30-90 seconds**. That is the hardware, not
a fault:

| Stage | Typical |
|---|---|
| classify intent | 0s on the first turn, ~8s after |
| generate SQL | 25-68s |
| execute SQL | **2-35 ms** |
| narrate result | 15-25s |

Generation runs at ~5.9 tok/s and each turn makes two or three model calls.
Database execution is three orders of magnitude below model time — nothing
about the SQL side is the bottleneck.

For a faster demo at lower accuracy:

```bash
uv run python model/download.py --only qwen2.5-coder-3b
make serve MODEL=qwen2.5-coder-3b
make api   MODEL=qwen2.5-coder-3b
```

~4× faster (12.7s p50), 35.3% accuracy against the 7B's 69.1%.

---

## Keeping memory healthy

The margin is 70 MB, so a few habits matter:

- **One model at a time.** Stop `llama-server` before starting another.
- **Restart the server between long runs.** Memory creeps across a 55-turn
  evaluation; a restart took swap from 5,026 MB back to 2,932 MB.
- **Do not download weights while a model is loaded.** Doing both drove free
  memory to 6% with 1.6M swapouts, and a bare `SELECT count(*)` took 1.9s.
- **Use the watchdog for unattended runs.** It stops everything on a Metal OOM
  or swap above 5 GB:

```bash
./model/watchdog.sh eval/reports/qwen2.5-coder-7b.server.log &
```

Healthy figures while running: swap ~2.8-3.3 GB, no `OutOfMemory` lines in the
server log.

---

## Tests and evaluation

```bash
make test           # 168 unit tests
make cov            # with coverage
make eval-verify    # every gold SQL still runs
make eval           # full 55-turn harness
make eval-baseline  # zero-shot comparison
```

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `preflight: refusing to launch` | Model too large for this machine | Use a smaller model; do not override unless you have more RAM |
| Every call returns 503 | Server still loading | Wait with `model/wait_healthy.sh`, not a bare curl |
| `Insufficient Memory` in the server log | Metal OOM | Stop everything; use a model preflight admits |
| Answers take minutes, machine sluggish | Something else is holding memory | Check for a second `llama-server`, a download, or a Docker VM |
| `/api/health` shows `database.ok: false` | Postgres not running | `brew services start postgresql@17` |
| UI loads but every question errors | API or model down | `curl localhost:8000/api/health` |

---

## If you want to containerise this

Nothing here is Mac-specific except the memory arithmetic, but **no compose
file is shipped** — see README, "Why there is no Docker setup". On Linux or a
16 GB+ machine it would be four services:

| Service | Notes |
|---|---|
| `db` | `postgres:17-alpine`, health-checked |
| `loader` | one-shot: applies `db/schema.sql`, loads the CSVs, creates `agent_ro`, then exits. Everything else waits on it completing successfully |
| `llama` | `ghcr.io/ggml-org/llama.cpp:server` with `model/gguf` mounted read-only |
| `api` / `web` | this repository's FastAPI app and the Vite build behind nginx |

The loader is the only non-obvious piece: the others must wait on
`service_completed_successfully`, not merely on the database being healthy.
