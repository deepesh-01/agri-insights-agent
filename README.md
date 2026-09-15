# Conversational text-to-SQL agent — agricultural advisory database

A non-technical user asks questions in plain English, across a back-and-forth
conversation, and gets correct answers with the SQL shown. Everything runs
locally on a laptop: no hosted model is called at inference time.

```
"average yield per hectare in Belgaum for kharif 2025"
  → SQL, result table, and a plain-English answer

"what about Dharwad?"        ← resolved against the previous turn
"now break that down by crop"← keeps district and season, adds a grouping
"how many field agents?"     ← topic change; every carried filter is discarded
```

---

## Contents

- [Setup](#setup) · [Hardware](#hardware-requirements) · [Model card](#model-card)
- [Architecture](#architecture) · [Life of a turn](#life-of-a-turn)
- [Conversation state](#conversation-state-what-is-carried-what-is-discarded-how-we-decide)
- [Guardrails](#guardrails) · [Closing the gap](#closing-the-gap-for-a-small-model)
- [Evaluation](#evaluation) · [Results](#results) · [Failure analysis](#failure-analysis)
- [Trade-offs](#design-decisions-and-trade-offs) · [Disclosure](#disclosure)

---

## Setup

### Native — the only supported path

**Full step-by-step guide: [`docs/SETUP_MACOS.md`](docs/SETUP_MACOS.md)** —
prerequisites, verification, memory hygiene and troubleshooting.

```bash
brew install llama.cpp postgresql@17
brew services start postgresql@17

make setup      # uv sync + npm install
make db         # create the database, load the CSVs, create the read-only role
make model      # download the GGUF weights (~25 GB, see below)

make serve      # terminal 1 — llama-server on :8080
make api        # terminal 2 — agent API on :8000
make web        # terminal 3 — UI on :5173
```

Open <http://localhost:5173>.

### Why there is no Docker setup

**This project ships no `docker-compose.yml`, and that is a deliberate
deviation from the brief.** The brief asks for `docker compose up` to bring up
the database, model runtime, agent and interface with no manual steps. A
compose file was written and then removed. The reasoning, so it can be judged
rather than taken on trust:

**1. The VM reserves memory the model cannot spare.**

Docker on macOS is not native — it runs a Linux VM that reserves RAM up front
whether or not a container uses it. colima defaults to 2 GiB; Docker Desktop
to more.

```
physical memory                                      8.00 GB
reserved for macOS, Postgres, Node, browser        - 2.90 GB
                                                   ---------
budget for the model                                 5.10 GB
qwen2.5-coder-7b Q4_K_M at ctx 6144 needs            5.03 GB
                                                     margin  +0.07 GB
```

The margin is 70 MB. A 2 GiB VM takes the budget to ~3.1 GB, at which point
`model/preflight.py` refuses to launch the 7B and the only model that fits is
the 3B — which scores **35.3% against the 7B's 69.1%**. Containerising the
stack would cost half the accuracy of the system being evaluated.

**2. Containers on Apple Silicon cannot reach the GPU.**

The Linux VM has no Metal access, so a containerised `llama-server` runs
CPU-only. Generation is already the slow part of every turn at ~5.9 tok/s
natively; on CPU it is several times worse.

**3. Shipping it untested would be worse than not shipping it.**

The compose file was authored and `docker compose config` validated, but it was
never built or brought up — because doing so is exactly the load that, earlier
in this project, drove Metal into repeated OOM and destabilised the display.
A compose file claiming "no manual steps" that has never been run is a claim I
cannot stand behind, and the brief values honest reporting over a box ticked.

**What this costs, and what replaces it.** The deliverable's purpose is
one-command reproducibility. That is served instead by
[`docs/SETUP_MACOS.md`](docs/SETUP_MACOS.md) — exact versions, `make setup`,
`make db`, `make model`, three `make` targets to run, a health check to verify,
and a troubleshooting table. On a Linux host or a 16 GB+ machine, a compose
file would be straightforward to restore: four services, and the only
non-obvious piece is the one-shot loader the others wait on.

### First-run model download

| Model | Size |
|---|---|
| qwen2.5-coder-7b | 4.68 GB |
| qwen3-8b | 5.03 GB |
| llama-3.1-8b | 4.92 GB |
| mistral-7b-v0.3 | 4.37 GB |
| sqlcoder-7b-2 | 4.08 GB |
| qwen2.5-coder-3b | 2.10 GB |
| **total** | **25.2 GB** |

At ~4 MB/s that is roughly 1h45 for the full roster. Only the chosen model is
needed to run the agent — `uv run python model/download.py --only qwen2.5-coder-7b`
fetches 4.68 GB and takes about 20 minutes.

---

## Hardware requirements

Developed and measured on **Apple M1, 8 GB unified memory, macOS 15**.

8 GB is the binding constraint and it shapes the design:

- **One model resident at a time.** A 7B Q4_K_M is ~4.4 GB of weights plus
  ~0.25 GB of quantised KV cache at 8192 context — about 5 GB, leaving ~3 GB
  for macOS, Postgres, Node and a browser.
- **The prompt budget is a hard budget**, not a nicety. The rendered schema
  context is ~1,220 tokens and is measured, not estimated.
- Running the model *and* downloading weights at the same time pushed the
  machine to 6% free memory and 1.6M swapouts, at which point a bare
  `SELECT count(*)` took 1.9 seconds. Benchmarks are run on a quiet machine.

**Minimum viable:** 8 GB unified memory runs the 7B models, tightly. 16 GB is
comfortable. No discrete GPU is required — `-ngl 99` offloads to Metal on Apple
Silicon. On a CUDA machine the same flags work unchanged and a 7B Q4_K_M needs
~6 GB of VRAM.

---

## Model card

| | |
|---|---|
| **Model** | **Qwen2.5-Coder-7B-Instruct** |
| **Parameters** | 7.6B |
| **Quantisation** | Q4_K_M (GGUF) |
| **Runtime** | llama.cpp `llama-server`, OpenAI-compatible endpoint |
| **Flags** | `-c 8192 -np 1 -ngl 99 --cache-type-k q8_0 --cache-type-v q8_0 --jinja` |
| **Sampling** | temperature 0, fixed seed 1337 |
| **Hardware** | Apple M1, 8 GB unified memory |
| **Structured output** | JSON schema compiled to a GBNF grammar by llama-server |

Six candidates were evaluated on identical flags, prompts and evaluation set —
see [`docs/MODEL_REPORT.md`](docs/MODEL_REPORT.md).

**[`docs/MODEL_RUNTIME.md`](docs/MODEL_RUNTIME.md)** covers the runtime in
depth: how the model is selected and launched, every `llama-server` flag and
why, the memory preflight and watchdog that keep an oversized model from taking
the display down, the request protocol and grammar-constrained output, prompt
caching, and where the latency actually goes.

---

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│ Web — React + Vite + TypeScript                     :5173    │
│ chat · SQL per turn · result table · state inspector ·        │
│ retried / refused / clarified badges · coverage banner        │
└───────────────────────────┬──────────────────────────────────┘
                            │ POST /api/ask
┌───────────────────────────▼──────────────────────────────────┐
│ Agent — FastAPI (Python 3.13)                       :8000    │
│                                                              │
│  context/      catalog.yaml + versioned prompt artifacts     │
│  conversation/ intent classifier · slots · bounded window    │
│  sql/          AST validator · executor · repair loop        │
│  model/        ModelClient interface (swappable)             │
└────────┬─────────────────────────────────┬───────────────────┘
         │                                 │
┌────────▼────────────────┐   ┌────────────▼───────────────────┐
│ llama.cpp llama-server  │   │ PostgreSQL 17          :5432   │
│ one ≤8B model, Q4_K_M   │   │ schema agri · role agent_ro    │
│ Metal             :8080 │   │ SELECT only, 5s timeout        │
└─────────────────────────┘   └────────────────────────────────┘
```

The four concerns the brief asks to see separated are separate modules:
**prompt/context construction** (`server/context/`), **model invocation**
(`server/model/`), **SQL execution** (`server/sql/`) and **conversation state**
(`server/conversation/`).

Full detail in [`ARCHITECTURE.md`](ARCHITECTURE.md).

---

## Life of a turn

```
 0  input guard        length, rate, injection heuristics — before any model call
 1  intent classify    NEW_TOPIC | REFINE | REFERENCE | CORRECT | META
 2  state resolve      apply the retention policy for that intent
 3  generate           one call decides answerable / ambiguous / writable, and
                       returns {action, sql, assumptions} under a JSON grammar
       ├── refuse ───► explain what is missing. No SQL.
       └── clarify ──► ask once, remember the answer for the session
 4  validate           sqlglot AST — parse, single statement, SELECT-only,
                       identifier allowlist, denylist, row cap, cost gate
 5  execute            agent_ro, read-only transaction, 5s timeout, row cap
       └── error ────► exact error back to the model, retry (bounded, max 2)
 6  narrate            rows → plain English, grounded, units stated
 7  update state       record slots from the validated SQL, evict stale context
 8  respond + audit    one envelope; full trace to audit/audit.jsonl
```

Two decisions worth calling out:

**Answerability, ambiguity and generation are one model call, not three.** On an
8 GB laptop each call costs seconds of wall clock, and a model that can write
the SQL is already deciding whether the SQL is writable.

**The first turn skips the classifier entirely.** A first turn cannot be a
continuation, so the call is pure cost. It is the cheapest latency win in the
pipeline.

---

## Conversation state: what is carried, what is discarded, how we decide

The brief asks for this reasoning explicitly. It also lives in the module
docstring of [`server/conversation/state.py`](server/conversation/state.py).

### Carried

| What | Why |
|---|---|
| **Slots** — metric, entity filters, time filters, grouping, tables | The substance of the previous question |
| A bounded window of the last **3 turns** as `(question, sql, row_count)` | Enough to resolve a reference; rows are never carried — expensive and rarely needed |
| **Answered clarifications**, for the whole session | The brief requires that a settled clarification is never asked twice |

### Slots are derived from the previous SQL's AST, not self-reported

This is the design decision I would defend hardest.

The obvious approach is to ask the model to report what it is carrying in a
structured field. That was rejected: a 7B model self-reporting its own state is
exactly the kind of thing it does unreliably, and a state object that silently
drifts from the query that actually ran is worse than none — the UI would then
show the user something untrue.

The last validated SQL is already a precise, machine-readable statement of what
was asked. Parsing it with `sqlglot` gives the same information
deterministically, costs no tokens and no extra round trip, and guarantees the
state inspector shows what the agent genuinely carries. Sentinel-exclusion
predicates are filtered out of the display, since they are data hygiene
re-applied every turn rather than anything the user asked for.

### Discarded

- **Every slot, the moment a turn is classified `NEW_TOPIC`.** This is the
  mechanism that stops turn one leaking into turn six.
- **Turns older than the window.** Dropped, not summarised — a summary of a
  stale turn is a confident-looking source of contamination.
- **Any slot older than 5 turns**, even without a topic change, so staleness is
  bounded by age as well as by topic.

### How the decision is made

| Intent | Example | Slots | History shown |
|---|---|---|---|
| `NEW_TOPIC` | "how many field agents do we have?" | **cleared** | **hidden** |
| `REFINE` | "only irrigated plots" | kept | shown |
| `REFERENCE` | "what about Dharwad?" | kept | shown |
| `CORRECT` | "no, I meant rabi, not kharif" | kept, named slot replaced | shown |
| `META` | "show me that SQL again" | kept | shown, no DB call |

Classification is a small grammar-constrained model call rather than keyword
matching, because "Only irrigated plots" and "How many field agents do we
have?" are not separable by keywords — the first is a fragment that only means
something as an addition to the previous question, and no word in it signals
that.

**If the classifier fails, the turn falls back to `NEW_TOPIC`.** Losing the
classifier must lose context, never leak it.

---

## Guardrails

Enforced outside the model, in three independent layers. The brief is explicit
that *"a prompt asking the model to only write SELECT statements scores zero."*

### Layer 1 — AST validation

`server/sql/validator.py`, using `sqlglot`. Checks in order, first failure
short-circuits into the repair loop:

1. **Parse** (dialect `postgres`). Unparseable → reject.
2. **Single statement.** No `;` chaining.
3. **Root is `SELECT` / `WITH…SELECT`.** Everything else rejected, including
   destructive statements hidden behind a CTE.
4. **Identifier allowlist from the catalog.** Every table and column must
   exist — this turns a hallucinated column into a clean, actionable error
   without a database round trip.
5. **Denylist:** `pg_*`, `information_schema`, `pg_read_file`, `pg_sleep`,
   `dblink`, `lo_import`.
6. **Row cap.** Missing `LIMIT` → `LIMIT 1000`; larger → clamped to 5000.
7. **Cost gate.** `EXPLAIN` first; reject plans above threshold. A three-way
   self-join of `sensor_reading` is rejected at an estimated cost of 96 billion
   rather than waiting for the timeout.

### Layer 2 — database role

`agent_ro` holds `SELECT` and nothing else, plus
`default_transaction_read_only`, `statement_timeout = 5s`, and no `CREATE` or
`TEMP` anywhere. Verified, not assumed:

```
DELETE → ERROR: cannot execute DELETE in a read-only transaction
DROP   → ERROR: cannot execute DROP TABLE in a read-only transaction
CREATE → ERROR: cannot execute CREATE TABLE in a read-only transaction
```

### Layer 3 — refusal

A question the schema cannot answer produces an explicit refusal naming what is
missing. Refusal is a scored outcome in the evaluation, not an error path:
*"confident wrong answers are the worst possible failure mode for this
product."*

### Bounded repair

Validation or execution failure sends the **exact** error text back to the
model — `column 'yield_kg' does not exist; crop_cycle has: …` — and
regenerates, at most twice, then refuses. A 7B model corrects a named error far
more reliably than an instruction to try again. Attempt counts are always
surfaced, and a retried answer is badged as retried in the UI.

---

## Closing the gap for a small model

The brief notes that a naively prompted 7B will do poorly, and that the gap
between that and the final system is the exercise. What closes it here:

**1. A catalog that teaches the traps.** The database has four defects that
make the obvious query subtly wrong. Full evidence in
[`docs/DATA_NOTES.md`](docs/DATA_NOTES.md); each is a column comment in
`db/schema.sql` and a rule in `server/catalog.yaml`:

| Trap | Naive | Correct |
|---|---|---|
| `expected_yield` is kg/**ha**, `actual_yield` is **total** kg | 123 underperformers | **387** |
| Sentinel sensor values `-273, -99, 500, 999.9` (79 rows) | avg temp 29.12 | **27.70** |
| 386 duplicated `(plot, type, timestamp)` keys | rainfall 64,934.7 | **56,020.0** |
| `plot.district` vs `farmer.district` (91/450 disagree) | 8,134 kg/ha (n=67) | vs **11,767** (n=75) |

**2. The catalog is generated from the live database**, so it cannot drift from
the schema, and it feeds three consumers from one source: the prompt, the
validator's allowlist, and the UI schema panel.

**3. Enum domains in the prompt.** Every low-cardinality column ships its actual
values, which sharply reduces invented `WHERE` values. Free-text columns are
explicitly excluded — `field_visit.notes` has only 19 distinct strings in this
sample, so a cardinality cap alone would have taught the model a closed
vocabulary that does not exist.

**4. Grammar-constrained output.** The JSON schema is compiled to GBNF by
llama-server, so malformed output is impossible. This is a *syntax* guarantee
only — layer 1 still does the semantic work — but it matters for the bake-off,
because the weaker candidates would otherwise fail on formatting rather than on
reasoning, and formatting is not what is being measured.

**5. Data coverage disclosed up front.** The data stops before today. The UI
shows the coverage window *before the first question*, the prompt carries both
the real date and the coverage bound, and an empty date-filtered result is
narrated as "the data ends on 2026-04-23" rather than "there were none".

---

## Evaluation

```bash
make eval-verify   # check every gold query still runs
make eval          # full harness against the default model
make eval-baseline # zero-shot, no engineering, same model
make bakeoff       # every model in the roster → docs/MODEL_REPORT.md
```

**16 conversations, 55 turns, 48 gold queries.** Format and authoring rules in
[`eval/FORMAT.md`](eval/FORMAT.md).

Deterministic by construction *and verified*: temperature 0, fixed seed, pinned
catalog and prompt versions, all recorded in the report header. Both the final
and baseline configurations were run twice with the server restarted between,
and every metric reproduced exactly — with **0 correctness flips and 0 turns
with different SQL text** across all 55 turns of the final configuration.

Every model call is recorded verbatim in
`eval/reports/<model>-<mode>.transcript.jsonl`: rendered system and user
prompts, raw response text, sampling parameters, tokens and latency, for all
three stages and every retry attempt. The audit trail is alongside it in
`.audit.jsonl`. This is what makes a result diagnosable rather than merely
reported — the first real use of it caught a baseline run scoring 10.9% because
every call had returned HTTP 503 against a still-loading server.

**Execution accuracy, never string comparison.** Gold and generated SQL both
run and the result sets are compared: column names ignored (positions
compared), row order ignored unless the gold query has `ORDER BY`, numbers
compared within a relative tolerance of 1e-6.

Refusals and clarifications are scored outcomes — a refusal on an unanswerable
question is correct, a refusal on an answerable one is a miss.

**Intent accuracy is reported separately** from SQL accuracy, because a turn can
produce the right SQL with the wrong intent label, and that distinction is what
says whether a failure is in conversation handling or in SQL generation.

The gold queries are themselves verified (`make eval-verify`) — a gold query
that fell into one of the data traps would score the agent's correct answer as
wrong. The harness is an instrument and this is its calibration check.

---

## Results

Full detail in [`docs/MODEL_REPORT.md`](docs/MODEL_REPORT.md).

**Final system — qwen2.5-coder-7b, 16 conversations, 55 turns:**

| Metric | Value |
|---|---|
| Per-turn accuracy | **69.1%** |
| Full-conversation accuracy | **37.5%** |
| Intent accuracy | 70.9% |
| Retry rate | 7.3% |
| Latency p50 / p95 | 31.2s / 82.2s |
| Tokens per turn | 5,450 prompt + 194 completion |

| Category | Score |
|---|---|
| unanswerable | **6/6 (100%)** |
| correction | 1/1 (100%) |
| ambiguous | 1/1 (100%) |
| first_turn | 11/13 (85%) |
| **follow_up** | **19/34 (56%)** |

Every category except follow-ups is 85-100%. Follow-ups are 62% of all turns,
which is why full-conversation accuracy (37.5%) is so much lower than per-turn
(69.1%). **Multi-turn refinement is the system's real weakness**, and that is a
more useful conclusion than the headline number.

### Half the roster could not run

| Model | Outcome |
|---|---|
| qwen2.5-coder-7b | ran — the winner |
| qwen2.5-coder-3b | ran — 35.3% per-turn, 0% full-conversation, 4× faster |
| mistral-7b-v0.3 | ran — 23.5%, p95 of 420s, unusable here |
| qwen3-8b / llama-3.1-8b | **Metal OOM — 8B does not fit in 8 GB** |
| sqlcoder-7b-2 | **cannot compile a JSON grammar — no chat template** |

The sqlcoder result answers the question it was included for: on a
conversational benchmark, a SQL specialist without conversational ability
cannot even enter the pipeline.

### Baseline comparison — what the engineering is worth

Same model, same database, same 55 turns, with the catalog rules, few-shots,
conversation state, intent classifier and repair loop all removed:

| Metric | Zero-shot baseline | Final system | Delta |
|---|---|---|---|
| Per-turn accuracy | 23.6% | **69.1%** | **+45.4pp** |
| Full-conversation accuracy | **0.0%** | **37.5%** | **+37.5pp** |
| Intent accuracy | 45.5% | 70.9% | +25.5pp |
| Latency p50 | 27.7s | 33.4s | |

**The baseline completed zero of 16 conversations correctly.** It never once
excluded a sentinel sensor value (0 turns vs 11) or de-duplicated a reading
(0 vs 5), so every aggregate it produced over `sensor_reading` was plausibly
wrong. Refusals went from 1/6 to 6/6.

Cost: ~8× prompt tokens, 2.5× latency.

### Effect of the engineering

| Configuration | Per-turn | Full-conv | p50 |
|---|---|---|---|
| Trimmed prompt | 61.8% | 31.2% | 22.4s |
| Restored prompt | 60.0% | 31.2% | 30.1s |
| **After failure fixes** | **69.1%** | **37.5%** | 31.2s |

Prompt length between 2,400 and 3,250 tokens made no material difference
(61.8% vs 60.0%, 5 turns flipping in both directions). The +9.1pp came from the
four fixes in [`docs/FAILURE_ANALYSIS.md`](docs/FAILURE_ANALYSIS.md).

---

## Failure analysis

Three failures root-caused in full in
[`docs/FAILURE_ANALYSIS.md`](docs/FAILURE_ANALYSIS.md). The summary is
uncomfortable and worth stating plainly: **two of the three were my defects,
not the model's.**

1. **My prompt contradicted my own gold SQL.** The few-shot example for
   "average soil moisture in Dharwad" answers with two columns; the gold
   expected one. The model followed instruction precisely and was scored wrong,
   losing all three turns of that conversation. An evaluation defect measuring
   my own inconsistency.

2. **Few-shot contamination.** `season = 'rabi'` appeared 4× and
   `district = 'Mysore'` 4× across the examples, and the model copied both into
   a question that asked for neither — turning a count of 387 into 93. The
   examples taught values, not just patterns.

3. **Top-N-per-group was never taught.** Asked "which crop is best per
   district", the model sorted instead of ranking and returned 119 rows instead
   of 12. `grep -c "rank()\|OVER (" examples` returned **0** — the brief names
   window functions as required and no example demonstrated one.

Plus a product bug: the input guard reported a deliberately blocked
prompt-injection attempt as `action="error"` rather than `"refuse"`. A working
guardrail should never present as a malfunction.

All four fixed: **5 turns flipped, all 5 gained, none regressed.** One of the
five (`advisory-backlog`) was never analysed and improved because the
anti-contamination instruction generalised.

---

## Design decisions and trade-offs

**Slots from the AST, not from the model.** Deterministic, free, and the UI
cannot show the user a lie. Cost: slots are the *shape of the previous query*
rather than a semantic intent, so a question that changes meaning without
changing query shape is not represented.

**One generation call instead of separate answerability, ambiguity and SQL
calls.** Roughly a third of the wall clock on this hardware. Cost: the three
decisions are entangled in one output, and a model that is bad at one can drag
the others down.

**Bounded window of 3 turns, dropped rather than summarised.** Summarising
would preserve more, but a summary of a stale turn is a confident-looking
contamination source, and the brief explicitly requires turn-six context not to
leak.

**In-memory conversation store.** Nothing in the brief asks for conversations to
outlive a run; a database table here would be scaffolding without a user.

**No framework (no LangChain, LlamaIndex or DSPy).** The pipeline is roughly
200 lines of explicit control flow. A framework would supply prompt templating
and a retry loop — both small here — in exchange for indirection in exactly the
code that most needs to be readable: the guardrails and the state machine. The
brief asks what a framework does for you and what you would lose without it; at
this size the answer went the other way.

**No Docker.** A named deliverable, deliberately dropped: the VM reserves
memory that takes the 7B out of reach on 8 GB, and containers get no Metal on
Apple Silicon. Reasoning and the cost of the trade are in
[Why there is no Docker setup](#why-there-is-no-docker-setup).

**Deliberately not built:** streaming, charting, RAG, fine-tuning, auth,
multi-database support — all out of scope per the brief.

### What I would do with more time

- **Intent classification is one model call with no fallback beyond
  `NEW_TOPIC`.** A cheap deterministic pre-filter for the unambiguous cases
  (an interrogative opening a new subject) would cut latency and reduce the
  blast radius of a misclassification.
- **Slot-level correction.** `CORRECT` currently re-generates the whole query
  with the previous SQL in context. Rewriting the AST node directly would be
  exact rather than probabilistic.
- **The eval set was authored by the same mind as the system**, which is a real
  bias. Categories and seed cases come from the brief, and traps from the data
  rather than from what the system happens to handle — but adversarial
  conversations written by someone else would be worth more than another ten
  written by me.
- **Scale.** Production `sensor_reading` is ~2M rows against 19,778 here.
  Indexes, the cost gate and time bucketing are built for that size, but the
  numbers reported are measured on the data provided.

---

## Repository layout

```
db/          schema.sql (DDL + trap comments) · roles.sql · load.py
model/       models.yaml (roster) · download.py · serve.sh
server/      app.py · pipeline.py · catalog.yaml · prompts/ (versioned)
             context/ · conversation/ · sql/ · model/
web/         React + Vite + TypeScript
eval/        FORMAT.md · conversations/ (16) · compare.py · run_eval.py · bakeoff.py
tests/       168 tests — validator, state, comparator, pipeline, API, transcript
docs/        DATA_NOTES.md (profiling evidence) · MODEL_REPORT.md (bake-off)
             MODEL_RUNTIME.md (init, protocol, latency, failure modes)
             SETUP_MACOS.md (native setup, no Docker) · FAILURE_ANALYSIS.md
```

---

## Disclosure

The brief permits a frontier model to be used **offline** to help author the
evaluation set and gold SQL, and requires that use to be disclosed.

**A frontier model (Claude, via Claude Code) was used offline** to author this
codebase, the evaluation conversations and their gold SQL, and this
documentation. **No hosted model is called at inference time** — the runtime
path is llama.cpp serving local weights, and the agent works with networking
disabled.
