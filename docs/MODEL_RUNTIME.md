# Model runtime

How the local model is selected, launched, talked to, and kept from taking the
machine down — plus the latency numbers that follow from all of it.

Everything here is measured on the development machine: **Apple M1, 8 GB
unified memory, macOS**, `llama-server` version 0.4.0 (build 10809).

---

## 1. The model

| | |
|---|---|
| Model | Qwen2.5-Coder-7B-Instruct |
| Parameters | 7.6B |
| Quantisation | Q4_K_M (GGUF) |
| File size | 4.36 GB |
| Context | 6144 tokens |
| Runtime | llama.cpp `llama-server`, Metal backend |
| Endpoint | `http://127.0.0.1:8080`, OpenAI-compatible |

Chosen by the bake-off in [`MODEL_REPORT.md`](MODEL_REPORT.md). The roster and
per-model rationale live in [`model/models.yaml`](../model/models.yaml).

Weights are fetched by `model/download.py` into `model/gguf/<id>.gguf` as
symlinks into the Hugging Face cache, so the same blob is shared rather than
copied.

---

## 2. Initialisation

### Preflight — refusing to launch what will not fit

`model/preflight.py` runs **before** any weights are loaded and exits non-zero
if the model cannot fit.

This exists because of a real incident. Repeatedly launching 8B models on this
machine drove Metal into
`kIOGPUCommandBufferCallbackErrorOutOfMemory`. Unified memory is shared with
the window server, so the display began glitching and the machine had to be
shut down — a failed inference experiment took out the whole desktop. "Does
not fit" is therefore decided from file size and free memory, never discovered
by watching a GPU die.

```
estimate = weights + (ctx / 1000 × 0.035 GB) + 0.45 GB compute buffers
budget   = physical memory − 2.9 GB reserved for the system
```

The 2.9 GB reserve is calibrated against observed behaviour, not theory:

| Observation | Result |
|---|---|
| 4.36 GB model (7B) at ctx 6144 | runs — 55-turn passes with no incident |
| 4.58 GB model (Llama-3.1-8B) at ctx 6144 and 4096 | Metal OOM |
| 4.68 GB model (Qwen3-8B) at ctx 6144 and 4096 | Metal OOM |

That puts the real boundary between 5.03 and 5.17 GB, so the budget is 5.10 GB.
Two earlier values were wrong in both directions and are recorded in the source:
2.6 GB predicted the 8B models would fit (they do not), and 3.0 GB blocked the
7B at ctx 6144 (which demonstrably works).

Models observed to fail are listed as `known_incompatible` in `models.yaml`
with their exact error, and that list **overrides the arithmetic** — a recorded
observation cannot be wrong in the way an estimate can.

```
$ uv run python model/preflight.py --all
physical 8.0 GB, reserved 2.9 GB, budget 5.10 GB, ctx 6144

  FITS     qwen2.5-coder-7b: needs ~5.03 GB, margin +0.07 GB
  TOO BIG  qwen3-8b: OOMs on 8 GB unified memory...
  TOO BIG  llama-3.1-8b: OOMs on 8 GB unified memory...
  FITS     mistral-7b-v0.3: needs ~4.74 GB, margin +0.36 GB
  FITS     qwen2.5-coder-3b: needs ~2.63 GB, margin +2.47 GB
  TOO BIG  sqlcoder-7b-2: cannot compile the JSON schema to a GBNF grammar...
```

`model/serve.sh` calls this and refuses to launch on failure. `ALLOW_UNSAFE_MODEL=1`
overrides it — only appropriate on a machine with more memory.

### Launch

```bash
./model/serve.sh qwen2.5-coder-7b
```

```
llama-server \
  --model model/gguf/qwen2.5-coder-7b.gguf \
  --alias qwen2.5-coder-7b \
  --host 127.0.0.1 --port 8080 \
  --ctx-size 6144 \
  --parallel 1 \
  --cache-type-k q8_0 \
  --cache-type-v q8_0 \
  --jinja \
  --no-warmup
```

Every flag, and why:

| Flag | Reason |
|---|---|
| `--ctx-size 6144` | System prompt is ~3,250 tokens; plus conversation context and a 700-token generation budget, 4096 is too tight and 8192 wastes KV memory this machine does not have |
| `--parallel 1` | One slot. On 8 GB there is no memory for concurrent slots, and the agent issues calls serially anyway |
| `--cache-type-k/v q8_0` | Quantised KV cache, roughly halving its footprint versus f16. At ctx 6144 that is ~215 MB instead of ~430 MB |
| `--jinja` | Use the model's own chat template from the GGUF metadata. Required for correct role formatting, and its absence is what makes `sqlcoder-7b-2` unusable here |
| `--no-warmup` | Skips a dummy decode at startup. Saves a few seconds per launch, which matters when the bake-off restarts the server once per model |
| **no `--n-gpu-layers`** | See below |

### Why `-ngl 99` is deliberately absent

Forcing full GPU offload made llama.cpp **abort its own memory fitting**:

```
W common_fit_params: failed to fit params to free device memory:
  n_gpu_layers already set by user to 99, abort
```

| Configuration | Generation throughput |
|---|---|
| `-ngl 99` | 3.55 tok/s |
| auto-fit (no flag) | **5.86 tok/s** |

A 65% speedup from removing a flag that looked like an optimisation. On a
machine with GPU headroom the opposite is true and `-ngl 99` is correct; on
8 GB it is actively harmful. The reasoning is recorded in `serve.sh` so it is
not "tidied up" later.

**Startup time:** ~8 seconds to healthy from a warm page cache.

### Watchdog

`model/watchdog.sh` polls every 20 s during an evaluation run and kills
everything on either signal:

| Signal | Threshold | Why |
|---|---|---|
| Metal OOM in the server log | any occurrence | The real danger — this is what destabilises the display |
| Swap used | > 5,000 MB | Secondary, catches a runaway |

The swap threshold is also calibrated rather than guessed. Loading a 7B takes
swap to ~3.0 GB as a matter of course, and the model ran a full 18-turn pass at
3.7 GB with no trouble. A first attempt at 2,500 MB killed a perfectly healthy
run before its first turn.

---

## 3. Communication

### Interface

`server/model/client.py` defines `LlamaCppClient` against llama-server's
OpenAI-compatible endpoint. It is the only module that knows the model is
local, which is what lets the bake-off swap models without touching anything
else.

```
POST /v1/chat/completions
{
  "messages": [{"role": "system", ...}, {"role": "user", ...}],
  "temperature": 0.0,
  "seed": 1337,
  "max_tokens": 700,
  "cache_prompt": true,
  "response_format": {"type": "json_schema", "json_schema": {...}}
}
```

`GET /health` and `GET /props` back the agent's own `/api/health`.

### Structured output

`response_format: json_schema` is compiled by llama-server into a **GBNF
grammar**, so the model is physically unable to emit a malformed object. Two
schemas, in `server/model/schemas.py`:

- `GENERATE_SQL_SCHEMA` → `{action: sql|refuse|clarify, sql, assumptions, reason, question, confidence}`
- `CLASSIFY_INTENT_SCHEMA` → `{intent, replaces, rationale}`

This is a **syntax guarantee only**. A grammatically perfect JSON object can
still contain nonsense SQL, so `server/sql/validator.py` does the semantic work
regardless. The grammar matters most for fairness in the bake-off: without it,
weaker models fail on formatting rather than on reasoning, and formatting is
not what is being measured.

`Completion.json()` still tolerates fenced or prose-wrapped output, because not
every candidate honours a schema and a model that writes correct SQL inside a
code fence should be scored on the SQL.

### Prompt caching

`cache_prompt: true` means the long, unchanging system prefix is encoded once
and reused. Measured effect: prompt evaluation processes **~600 tokens per
call, not ~3,900** — only the changed suffix.

```
prompt eval: ~600 tokens @ ~53 tok/s   ≈ 11 s
generation:  ~270 tokens @ ~5.9 tok/s  ≈ 46 s
```

Generation dominates. Prompt size therefore matters less for latency directly
than it does for KV-cache pressure.

### Calls per turn

| Stage | Schema | max_tokens | Temperature |
|---|---|---|---|
| classify intent | `CLASSIFY_INTENT_SCHEMA` | 120 | 0.0 |
| generate SQL | `GENERATE_SQL_SCHEMA` | 700 | 0.0 → 0.35 → 0.7 |
| narrate | none (free text) | 280 | 0.2 |

**The first turn of a conversation skips the classifier** — a first turn cannot
be a continuation, so the call is pure cost. It is the cheapest latency win in
the pipeline.

Answerability, ambiguity and generation are **one call, not three**. Each call
costs tens of seconds here, and a model that can write the SQL is already
deciding whether the SQL is writable.

### Temperature escalation on retry

The repair loop escalates sampling: `0.0 → 0.35 → 0.7`.

At temperature 0 with a near-identical prompt, a retry reproduces the same
tokens. Observed directly: a malformed `CAST('2026-01-01')` was regenerated
**byte-identically three times**, consuming the entire retry budget without
exploring anything. The first pass stays greedy for determinism; retries must
vary or they are not retries. `tests/test_pipeline.py` asserts the escalation
so it cannot regress silently.

### Determinism

Temperature 0 on the first pass, fixed seed 1337, pinned catalog version and
pinned prompt versions — all recorded in every evaluation report header. Repeated
runs produce the same numbers.

---

## 4. Latency

### Measured, full 55-turn evaluation

| Configuration | Prompt | ctx | p50 | p95 | Accuracy |
|---|---|---|---|---|---|
| Trimmed prompt (v6) | 2,404 tok | 4096 | **22.4 s** | 50.3 s | 61.8% |
| Restored prompt (v7) | 3,253 tok | 6144 | 30.1 s | 77.5 s | 60.0% |
| After failure fixes (v8) | 3,528 tok | 6144 | 31.2 s | 82.2 s | **69.1%** |

Screening (5 conversations, ctx 6144): p50 49.4 s, p95 92.9 s.

Latency tracks context size, not accuracy: the +9.1pp between v7 and v8 came
from prompt *content*, at essentially unchanged cost (30.1 → 31.2 s p50).
Accuracy and latency are not on the same axis here.

### Where the time goes

A typical answered turn issues 2–3 model calls totalling ~270 generated tokens
at ~5.9 tok/s. SQL execution is negligible by comparison:

| Stage | Typical |
|---|---|
| classify | 0 s on first turn, ~8 s after |
| generate | ~25–45 s |
| **execute** | **2–35 ms** |
| narrate | ~15–25 s |

Database execution is roughly **three orders of magnitude** below model time.
Everything that matters for latency is token generation.

One caveat on that number: the executor originally opened two connections per
query — one for `EXPLAIN`, one for the query. Under memory pressure a trivial
`COUNT` took 10,866 ms. Pooling and sharing a connection brought it to 2.3 ms
warm. Connection handshakes, not queries, were the cost.

### What makes it slow, and what does not

- **Memory pressure is the binding constraint.** With the model resident and
  large downloads running concurrently, free memory hit 6% with 1.6M swapouts
  and a bare `SELECT count(*)` took 1.9 s. Benchmarks are only run on a quiet
  machine.
- **Prompt size is not the main lever** — caching means only the changed suffix
  is processed. Trimming 850 tokens roughly halved p50 (49.4 → 22.4 s), but
  that came mostly from the smaller context reducing memory pressure, not from
  the tokens themselves.
- **Weights are mmap'd**, so they do not appear in RSS but fill RAM regardless.
  `ps` shows `llama-server` at 0.56 GB while it is really holding 4.36 GB.

---

## 5. Observed failure modes

| Failure | Cause | Handling |
|---|---|---|
| Metal OOM at startup or decode | Model too large for unified memory | Preflight refuses to launch; watchdog kills any run where it appears |
| `common_sampler_init: error initializing grammar sampler` | Model has no usable chat template, so the GBNF grammar cannot compile (`sqlcoder-7b-2`) | Marked `known_incompatible`; reported as a capability result |
| Identical SQL regenerated on every retry | Greedy sampling at temperature 0 | Temperature escalation, with a regression test |
| `500 Internal Server Error` from llama-server | Backend in an error state after an earlier OOM | Surfaces as a refusal rather than a crash; the turn degrades, the session survives |
| Model returns unparseable text | Weak instruction-following | `Completion.json()` strips fences and prose; failing that, the turn degrades to a refusal |

Every one of these degrades the turn rather than the session. A failed model
call becomes a refusal the user can see, not a stack trace.

---

## 6. Swapping models

```bash
uv run python model/download.py --only qwen2.5-coder-3b
./model/serve.sh qwen2.5-coder-3b          # preflight runs first
MODEL_ID=qwen2.5-coder-3b make api
```

Only one model can be resident on 8 GB, so `eval/bakeoff.py` is strictly
serial: start server, wait for health, run the harness, stop it, let the OS
reclaim the memory, repeat. Identical flags for every candidate are what make
the comparison mean anything — with one variable, differences are attributable
to the model.

---

## 7. On different hardware

Nothing here is Mac-specific except the memory arithmetic.

- **16 GB unified memory** — the 8B models become viable, and `-ngl 99` becomes
  the right flag rather than a harmful one. Raise `--ctx-size` to 8192.
- **CUDA** — the same flags work unchanged. A 7B Q4_K_M needs ~6 GB VRAM and
  should generate at 40–80 tok/s, which would take p50 from ~50 s to a few
  seconds and make the whole pipeline feel interactive.
- **Containers on Apple Silicon get no Metal access.** That, plus the memory a
  Docker VM reserves on macOS, is why this project ships no container setup at
  all — see README, "Why there is no Docker setup". Every number in this
  document comes from running natively.

To re-measure after any change:

```bash
make eval MODEL=<id>        # full harness, p50/p95 and tokens per turn
uv run python model/preflight.py --all
```
