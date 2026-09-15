# Conversational Text-to-SQL Agent — Architecture

**Revision 2** — 2026-09-12. Revised against *SDE-2 AI Engineering Assignment*.
Revision 1 was written before the brief was available; §13 records what changed and why.

A non-technical user asks questions about an agricultural PostgreSQL database in
plain English, across a back-and-forth conversation, and gets correct answers —
with the SQL shown. Runs entirely locally on an 8 GB laptop.

---

## 1. What the brief actually demands

Two hard constraints define the exercise:

1. **It must be conversational.** "Handling a single question correctly is table
   stakes; handling turn four of a conversation correctly is the exercise."
2. **Small local model only.** ≤8B parameters, open weights, no hosted frontier
   model in the runtime path. "A 7B model prompted naively will perform poorly.
   Closing that gap is what we are evaluating."

Everything below follows from those two sentences. The conversation subsystem
(§5) and the evaluation harness (§8) carry the weight; the rest is scaffolding
that makes an undersized model behave.

**Disclosure required by the brief:** a frontier model (Claude, via Claude Code)
was used *offline* to author the evaluation set, the gold SQL and this codebase.
No hosted model is called at inference time.

---

## 2. Constraints from the hardware

| Constraint | Consequence |
|---|---|
| Apple M1, **8 GB unified RAM** | One model resident at a time. The prompt budget is a hard budget. |
| Apple Silicon + Docker | Containers get no Metal access and the VM reserves memory the model needs, so no container setup is shipped. §11 |
| ≤8B model | SQL correctness cannot be trusted. Validation (§4) is load-bearing, not defensive polish. |
| Sample data ends 2026-04-23 | Coverage is disclosed to the user before they ask, and injected into the prompt. §7 |

---

## 3. Component map

```
┌───────────────────────────────────────────────────────────────┐
│ Web — React + Vite + TypeScript                      :5173    │
│ chat · SQL per turn · result table · state inspector ·         │
│ retried / refused / clarified badges · loading+empty+error     │
└──────────────────────────┬────────────────────────────────────┘
                           │ POST /api/ask
┌──────────────────────────▼────────────────────────────────────┐
│ Agent server — FastAPI (Python 3.13)                 :8000    │
│                                                               │
│  ┌── context/ ──────┐ ┌── conversation/ ─┐ ┌── sql/ ───────┐ │
│  │ catalog.yaml     │ │ state machine    │ │ validator     │ │
│  │ prompts/*.md     │ │ intent classifier│ │ executor      │ │
│  │ (versioned       │ │ slot carry-over  │ │ repair loop   │ │
│  │  artifacts)      │ │ bounded window   │ │               │ │
│  └──────────────────┘ └──────────────────┘ └───────────────┘ │
│  ┌── model/ ────────┐ ┌── eval hooks ────┐                    │
│  │ ModelClient      │ │ audit log        │                    │
│  │ (swappable)      │ │ token+latency    │                    │
│  └──────────────────┘ └──────────────────┘                    │
└────────┬──────────────────────────────────┬───────────────────┘
         │                                  │
┌────────▼──────────────────┐   ┌───────────▼───────────────────┐
│ llama.cpp llama-server    │   │ PostgreSQL 17        :5432    │
│ one of N candidate models │   │ agri_insights / schema agri   │
│ Q4_K_M · Metal    :8080   │   │ role agent_ro = SELECT only   │
└───────────────────────────┘   └───────────────────────────────┘
```

`ModelClient` is an interface, not a concrete model. The bake-off (§9) swaps the
implementation behind it and re-runs the same harness.

---

## 4. Turn lifecycle

`POST /api/ask {conversation_id, message}`

```
  0  input guard          length, rate, injection heuristics
  1  intent classify      NEW_TOPIC | REFINE | CORRECT | REFERENCE | META
  2  state resolve        build the effective question from carried slots
  3  answerability check  can this schema answer it at all?
        ├── no  ──────────► REFUSE   (no SQL, explain what is missing)
        └── ambiguous ────► CLARIFY  (ask once, store the answer, never re-ask)
  4  context build        catalog + coverage + few-shots + resolved question
  5  generate SQL         llama-server, temp 0, GBNF-constrained JSON
  6  VALIDATE             §5 — model output is untrusted input
  7  execute              agent_ro, read-only txn, 5s timeout, row cap
        └── error ───────► repair loop, bounded, max 2 retries
  8  narrate              rows → plain English, grounded, units stated
  9  update state         commit resolved slots, evict stale context
 10  respond + audit      one envelope; full trace to the audit log
```

Stages 1–3 and 9 are the conversation subsystem and are where most of the
engineering value sits. Stage 6 is where safety sits.

---

## 5. Conversation state — the centre of the assignment

The brief asks what we **carry forward**, what we **discard**, and **how we
decide**. State is an explicit, inspectable object, not a chat transcript
handed wholesale to the model.

### What is carried

A typed `ConversationState` of resolved **slots**:

| Slot | Example | Carried across |
|---|---|---|
| `metric` | avg yield per hectare | REFINE, REFERENCE |
| `entity_filters` | district = Belgaum | REFINE, REFERENCE, CORRECT |
| `time_filters` | season = kharif, year = 2025 | REFINE, REFERENCE, CORRECT |
| `grouping` | by crop | REFINE |
| `answered_clarifications` | "district means plot district" | whole session |
| `last_sql`, `last_row_count` | — | REFINE, CORRECT |

Plus a bounded window of the last **3 turns** as `(question, sql, row_count)` —
never result rows. Rows are expensive in tokens and near-useless for resolving
the next question.

### How the decision is made

Turn intent is classified first, and the classification decides what survives:

| Intent | Example from the brief | Effect on state |
|---|---|---|
| `NEW_TOPIC` | "How many field agents do we have?" | **Clear all slots.** Fresh start |
| `REFINE` | "Only irrigated plots" | Keep slots, add a filter |
| `REFERENCE` | "What about Dharwad?" / "Now break that down by crop" | Keep metric + time, substitute the referenced slot |
| `CORRECT` | "No, I meant rabi, not kharif" | Keep slots, **replace** the named one, re-run |
| `META` | "show me the SQL again" | No DB call |

Classification is a small constrained generation (one label + the slots it
touches) rather than a heuristic, because "Only irrigated plots" and "How many
field agents do we have?" are not separable by keywords. It is cheap: a short
prompt, a tiny grammar, one label out.

### What is discarded, and when

- `NEW_TOPIC` clears every slot — this is what stops turn-six context leaking
  into an unrelated question.
- The turn window is hard-capped at 3. Older turns are dropped, not summarised.
- Slots carry a `set_at_turn`; any slot older than 5 turns is evicted even
  without a topic change. Staleness is bounded by both count and content.
- `answered_clarifications` is the deliberate exception — it persists for the
  whole session, because the brief requires that a clarification, once
  answered, is never asked again.

### Clarification

Triggered by genuine ambiguity that the data proves is material — not by
uncertainty in general. The canonical case is district: "yield in Belgaum"
resolves to 67 rows averaging 8,134 kg/ha via `plot.district`, or 75 rows
averaging 11,767 via `farmer.district`. A 45% swing is worth one question.
The answer is written to `answered_clarifications` and applied silently
thereafter.

---

## 6. Guardrails — enforced outside the model

The brief is explicit: *"A prompt asking the model to only write SELECT
statements scores zero."* Three independent layers, each sufficient alone.

### Layer 1 — AST validation (`sql/validator.py`)

Checks in order; first failure short-circuits and feeds the repair loop.

1. **Parse** with `sqlglot`, dialect `postgres`. Unparseable → reject.
2. **Single statement.** No `;` chaining.
3. **Root is `SELECT` or `WITH…SELECT`.** Reject INSERT/UPDATE/DELETE/DDL/
   COPY/SET/GRANT/CALL/EXPLAIN-ANALYZE.
4. **Identifier allowlist from the catalog.** Every table and column must
   exist. This is what turns a hallucinated column into a clean refusal
   instead of a database error.
5. **Denylist:** `pg_*`, `information_schema`, `pg_read_file`, `pg_sleep`,
   `dblink`, `lo_import`, `COPY … FROM PROGRAM`.
6. **Row cap.** Missing `LIMIT` → append `LIMIT 1000`; larger → clamp.
7. **Cost gate.** `EXPLAIN` and reject plans above threshold before executing.

### Layer 2 — database role

`agent_ro` holds `SELECT` and nothing else, with
`default_transaction_read_only = on`, `statement_timeout = 5s`,
`idle_in_transaction_session_timeout = 10s`, and no `CREATE`/`TEMP` anywhere.
Verified, not assumed:

```
DELETE → ERROR: cannot execute DELETE in a read-only transaction
DROP   → ERROR: cannot execute DROP TABLE in a read-only transaction
CREATE → ERROR: cannot execute CREATE TABLE in a read-only transaction
```

### Layer 3 — refusal

A question the schema cannot answer produces an explicit refusal naming what
is missing. Per the brief, *"confident wrong answers are the worst possible
failure mode for this product"* — so refusal is a first-class outcome that the
eval scores, not an error path.

### Bounded repair

Validation failure or DB error → the exact error text goes back to the model,
regenerate, **max 2 retries**, then refuse. Attempt count is always surfaced;
a retried answer is badged as retried in the UI.

---

## 7. Context construction

Prompts are **versioned files** in `server/prompts/`, never inline f-strings.
Each carries a header with its id, version and purpose; the id and version are
recorded in every audit row, so any eval number can be traced to the exact
prompt text that produced it.

`server/catalog.yaml` is the single source of truth, feeding three consumers —
the prompt, the validator allowlist and the UI schema panel — so they cannot
drift. It is generated from, and checked against, the live `COMMENT ON COLUMN`
metadata in `db/schema.sql`.

### The traps the catalog must teach

Discovered by profiling the data, each verified with naive-vs-correct SQL.
These are the substance of "closing the gap" for a small model — a 7B model
will not infer any of them.

| Trap | Naive result | Correct result |
|---|---|---|
| `expected_yield` is kg/**ha**, `actual_yield` is **total** kg | 123 underperformers | **387** |
| Sentinel sensor values `-273, -99, 500, 999.9` (79 rows) | avg temp 29.12 | **27.70** |
| 386 duplicated `(plot, type, recorded_at)` keys | rainfall 64,934.7 | **56,020.0** |
| `plot.district` vs `farmer.district` (91/450 disagree) | 8,134 kg/ha (n=67) | vs 11,767 (n=75) |

Also encoded: only 202 of 450 plots have sensor readings; `field_visit.outcome`
includes `cancelled` and `farmer_absent`, so "visits" needs a stated definition;
agent home district ≠ visited plot district for 159/600 visits; never aggregate
across `reading_type` because the units differ.

`crop_cycle.status` (A/F/H/P) is documented as **opaque** — the catalog states
only the observable fact that A and P rows always have NULL `harvest_date` and
`actual_yield` while F and H always have both. The model must not invent
expansions.

### Data coverage disclosure

Data stops before today. Handled by telling the user up front rather than
silently rewriting their dates:

1. Server computes `MIN`/`MAX` of every date column at startup.
2. `GET /api/schema` serves it; the UI shows a coverage banner **before** the
   first question.
3. The prompt carries the real clock date *and* the coverage bound.
4. A date-filtered query returning zero rows is narrated as "no data in that
   window — coverage ends 2026-04-23", never as "there were none".

Per-table coverage differs and is stated per table: sensor readings
2025-06-26 → 2026-04-23, advisories → 2026-08-18, visits → 2026-08-19,
harvests → 2026-07-13.

---

## 8. Evaluation harness

Carries the most weight of any section, and is built **before** the model is
chosen — it is the instrument that decides §9.

**One command, deterministic, same numbers on repeated runs.** Temperature 0,
fixed seed, frozen catalog version, prompt versions pinned per run.

### Dataset

`eval/conversations/*.yaml` — **15+ labelled conversations, 3–5 turns each**,
gold SQL for every turn. The brief's §4 supplies the seed cases; the rest are
authored to cover every category below. Format is defined in
`eval/FORMAT.md` (no example set was supplied with the brief).

### Scoring

**Execution accuracy** — run gold and generated SQL, compare result *sets*.
String comparison of SQL is explicitly not acceptable. The comparator is
order-insensitive unless the gold query has an `ORDER BY`, alias-insensitive
by column position, and numeric-tolerant to 1e-6.

Reported separately:

- **per-turn accuracy** and **full-conversation accuracy** (every turn correct)
- **category breakdown**: first-turn · follow-up · correction · ambiguous ·
  unanswerable
- **baseline comparison**: chosen model zero-shot with no engineering, against
  the final system — the delta is the point of the exercise
- **latency p50 and p95**, and **token counts per turn**
- **failure analysis**: three failures, root-caused, with the intended fix

Refusals and clarifications are scored as outcomes: a refusal on an
unanswerable question is *correct*; a refusal on an answerable one is a miss.

No tuning toward a target number. Numbers are reported as measured.

---

## 9. Model bake-off

All candidates ≤8B, open weights, GGUF, Q4_K_M, served by `llama-server`,
evaluated on the identical harness and prompts. The report lands in
`docs/MODEL_REPORT.md` and the winner becomes the default.

| Model | Params | Why it is in the set |
|---|---|---|
| Qwen2.5-Coder-7B-Instruct | 7B | Strongest open coder model in class for text-to-SQL |
| Qwen3-8B | 8B | Newer generation, at the size ceiling |
| Llama-3.1-8B-Instruct | 8B | General-purpose reference point |
| Mistral-7B-Instruct-v0.3 | 7B | Older, leaner general model |
| Qwen2.5-Coder-3B-Instruct | 3B | Speed/size floor — how much does 7B actually buy? |
| sqlcoder-7b-2 | 7B | Purpose-built text-to-SQL, but weak at multi-turn chat. Included precisely to test whether SQL specialism beats conversational ability on a conversational benchmark |

Scored on execution accuracy (per-turn and full-conversation), category
breakdown, p50/p95 latency, tokens per turn, and RAM headroom on 8 GB.

Runtime flags are pinned per model in `model/models.yaml`; each is launched with
`-c 8192 -np 1 --jinja -ngl 99` and quantised KV cache. Resident footprint for a
7B Q4_K_M is ≈ 5 GB, leaving ~3 GB for macOS, Postgres, node and the browser —
which is why one model runs at a time and the prompt budget is enforced.

GBNF grammars force well-formed JSON out of every model regardless of how well
it follows instructions. That is a syntax guarantee only; §6 still does the
semantic work.

---

## 10. Interface

React + Vite + TypeScript. Minimal, working, legible — explicitly not a design
exercise. Required elements:

- conversation history with the generated SQL visible **for every turn**
- result table, with explicit **retried / refused / clarified** badges
- a **state inspector** showing the slots the agent is actually carrying, which
  turn each was set on, and what the last topic change cleared
- explicit loading, empty and error states
- the data-coverage banner from §7

No streaming and no charting — both are out of scope per the brief.

---

## 11. Packaging

**No container setup is shipped.** The brief names `docker compose up` as a
deliverable; a compose file was written and then removed. Three reasons, in
order of weight:

1. **Memory.** Docker on macOS runs a Linux VM that reserves RAM up front
   (colima defaults to 2 GiB). The budget here is 5.10 GB and the 7B needs
   5.03 GB — a margin of 70 MB. A VM puts the chosen model out of reach and
   leaves only the 3B, which scores 35.3% against the 7B's 69.1%.
2. **No GPU.** Containers on Apple Silicon cannot reach Metal, so a
   containerised model runtime is CPU-only and several times slower than the
   ~5.9 tok/s measured natively.
3. **Untested is worse than absent.** Building and running the stack is exactly
   the load that drove Metal into repeated OOM earlier in this project and
   destabilised the display. Shipping a "no manual steps" claim that was never
   executed is not something to stand behind.

One-command reproducibility is served instead by `docs/SETUP_MACOS.md` and the
`make` targets. On Linux or a 16 GB+ host a compose file would be easy to
restore — four services, with the one-shot database loader the others wait on
being the only non-obvious piece.

## 12. Repository layout

```
satsure-assg/
├── README.md                  setup, architecture, turn lifecycle, model card,
│                              design decisions, eval report, disclosure
├── ARCHITECTURE.md            this document
├── Makefile                   make db | model | serve | api | web | eval | test
├── db/
│   ├── schema.sql             DDL + COMMENT ON COLUMN (units, enums, traps)
│   ├── roles.sql              agent_ro, guardrail layer 2
│   └── load.py                CSV → Postgres, raw, traps preserved
├── model/
│   ├── models.yaml            bake-off roster + per-model runtime flags
│   ├── download.py            fetch GGUFs
│   └── serve.sh               launch llama-server for a named model
├── server/
│   ├── app.py                 FastAPI routes
│   ├── catalog.yaml           single source of truth
│   ├── prompts/               versioned prompt artifacts
│   ├── grammars/              GBNF
│   ├── context/               catalog loading, prompt rendering
│   ├── conversation/          state machine, intent classifier, slots
│   ├── sql/                   validator, executor, repair loop
│   ├── model/                 ModelClient interface + llama.cpp impl
│   └── audit.py
├── web/                       React + Vite + TS
├── eval/
│   ├── FORMAT.md              conversation file format
│   ├── conversations/         15+ labelled, gold SQL per turn
│   ├── compare.py             result-set comparison
│   ├── run_eval.py            one command, deterministic
│   └── reports/
├── tests/                     state handling, validation, comparison, retry
└── docs/
    ├── MODEL_REPORT.md        bake-off results
    └── DATA_NOTES.md          profiling evidence for every trap
```

---

## 13. Changes from revision 1

| Change | Reason |
|---|---|
| Conversation state promoted from a footnote to the core subsystem (§5) | The brief calls it "the centre of the assignment"; r1 carried only the last N questions |
| SSE / token streaming removed | Explicitly out of scope |
| Refusal and clarification added as first-class turn outcomes (§4, §6) | Required behaviours; absent from r1 |
| Eval harness expanded to the largest work item (§8) | "Carries the most weight of any section" |
| Prompts moved to versioned file artifacts (§7) | "Prompts versioned and stored as artifacts, not inline f-strings" |
| Single fixed model → bake-off behind a `ModelClient` interface (§9) | Requested, and it supplies the model card and baseline delta |
| Docker dropped entirely (§11) | Added in r2 as a named deliverable, then removed: the VM's memory reservation puts the chosen model out of reach on 8 GB, and containers get no Metal |
| Unit tests and coverage added (§12) | Named deliverable |
| Traps promoted into schema comments and the catalog (§7) | Profiling found four that change answers materially |

---

## 14. Known risks

| Risk | Mitigation |
|---|---|
| 8 GB is tight; browser + Postgres + model could swap | Q4_K_M, ctx 8192, quantised KV, one model at a time, enforced prompt budget |
| Intent misclassification breaks the conversation | Its own eval category with gold labels, measured separately from SQL accuracy |
| A small model writes plausible-but-wrong joins | Explicit join paths and trap notes in the catalog, few-shots, execution-accuracy eval |
| Eval set authored by the same mind as the system | Categories and seed cases taken from the brief; traps derived from the data, not from what the system happens to handle |
| Containerised inference is slow on Apple Silicon | Documented; native path used for measurement |
| Free-text `field_visit.notes` could carry injection text | Reaches the model only as delimited result data; the narrate prompt treats result rows as data, never instructions |
