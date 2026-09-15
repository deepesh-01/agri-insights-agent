# Model bake-off

Every candidate is ≤8B parameters, open weights, Q4_K_M GGUF, served by
`llama-server` with identical flags, scored on the identical evaluation set
with identical prompts.

**Hardware:** Apple M1, 8 GB unified memory, macOS. `llama-server` 0.4.0.
**Evaluation set:** 16 conversations, 55 turns, 48 gold queries.
**Determinism:** temperature 0 on first pass, seed 1337, pinned catalog and
prompt versions.

---

## 1. Which models could run at all

Half the roster never produced a single successful call. That is a result, not
a gap, and the brief asks for exactly this kind of hardware reporting.

| Model | Size | Outcome |
|---|---|---|
| qwen2.5-coder-7b | 4.36 GB | **ran** |
| mistral-7b-v0.3 | 4.07 GB | ran, very slow |
| qwen2.5-coder-3b | 1.96 GB | ran |
| qwen3-8b | 4.68 GB | **Metal OOM** |
| llama-3.1-8b | 4.58 GB | **Metal OOM** |
| sqlcoder-7b-2 | 3.80 GB | **grammar sampler failed** |

**The two 8B models do not fit in 8 GB.** Both died with
`kIOGPUCommandBufferCallbackErrorOutOfMemory` during decode, at ctx 6144 and
again at ctx 4096. Because Metal shares unified memory with the window server,
this destabilised the display. They were not retried further: `model/preflight.py`
now refuses to launch them, and "does not fit" is recorded as the finding.

**sqlcoder-7b-2 could not compile the JSON schema to a GBNF grammar**
(`common_sampler_init: error initializing grammar sampler`). It is a
completion-style SQL model with no usable chat template, so it cannot take part
in a structured conversational pipeline at all.

That last result is worth dwelling on, because it was the reason for including
the model. The premise being tested was: *on a conversational benchmark, does
SQL specialism beat conversational ability?* The answer here is that the
specialist could not enter the arena. A model tuned purely for single-shot SQL
lacks the chat template and instruction-following that a multi-turn agent is
built on. For this assignment, conversational ability is not a nice-to-have
layered on top of SQL skill — it is the prerequisite.

---

## 2. Screening — 5 conversations, 18 turns

| Model | Per-turn | Full-conv | Intent | p50 | p95 | Tokens/turn |
|---|---|---|---|---|---|---|
| **qwen2.5-coder-7b** | **82.3%** | **80.0%** | 82.3% | 49.4s | 92.9s | 5494 + 223 |
| qwen2.5-coder-3b | 35.3% | 0% | 58.8% | **12.7s** | **24.6s** | 4410 + 114 |
| mistral-7b-v0.3 | 23.5% | 0% | 76.5% | 178.9s | 420.9s | 4987 + 323 |
| qwen3-8b | — | — | — | — | — | did not run |
| llama-3.1-8b | — | — | — | — | — | did not run |
| sqlcoder-7b-2 | — | — | — | — | — | did not run |

The coder-tuned 7B wins decisively — more than double the nearest model that
ran. Two observations worth recording:

**The 3B is four times faster and less than half as accurate.** 12.7s p50
against 49.4s is the difference between an interactive tool and a slow one, but
35.3% per-turn and **0% full-conversation** means it never completed a single
conversation correctly. On this task, size buys correctness that speed cannot
compensate for.

**mistral-7b-v0.3 is unusable on this hardware** — 420s p95 means a single turn
can take seven minutes. Its full 55-turn run was not extended, on hardware
grounds; the screening result is clear enough.

---

## 3. Winner — qwen2.5-coder-7b, full 55-turn set

Four configurations were measured on the identical set.

| Configuration | Prompt | ctx | Per-turn | Full-conv | p50 | p95 |
|---|---|---|---|---|---|---|
| Trimmed prompt (v6) | 2,404 tok | 4096 | 61.8% | 31.2% | **22.4s** | 50.3s |
| Restored prompt (v7) | 3,253 tok | 6144 | 60.0% | 31.2% | 30.1s | 77.5s |
| **After failure fixes (v8)** | 3,528 tok | 6144 | **69.1%** | **37.5%** | 31.2s | 82.2s |

### Category breakdown — final configuration

| Category | Score |
|---|---|
| unanswerable | **6/6 (100%)** |
| correction | 1/1 (100%) |
| ambiguous | 1/1 (100%) |
| first_turn | 11/13 (85%) |
| **follow_up** | **19/34 (56%)** |

Every category except follow-ups is at 85-100%. Follow-ups are the weakness,
and they are 62% of all turns — which is why full-conversation accuracy sits at
37.5% while per-turn is 69.1%.

### Prompt size made no material difference

An earlier reading of a 17-turn subset suggested trimming the prompt cost ~6
accuracy points. On the full 55 turns that did not hold: 61.8% trimmed against
60.0% restored, with only 5 turns flipping and in both directions. The subset
was too small to support the conclusion drawn from it.

The conclusion that survives is: **between 2,400 and 3,250 prompt tokens,
accuracy is flat and latency is not.** The trimmed configuration is 26% faster
for statistically indistinguishable accuracy.

The +9.1pp in v8 came from the fixes in
[`FAILURE_ANALYSIS.md`](FAILURE_ANALYSIS.md), not from prompt length.

---

## 4. Baseline comparison — what the engineering is worth

The brief asks for the chosen model prompted naively, against the final system,
with the delta stated. `--baseline` strips the catalog rules, the few-shot
examples, the conversation state, the intent classifier and the repair loop.
Same model, same database, same 55 turns, bare table names only.

| Metric | Zero-shot baseline | Final system | Delta |
|---|---|---|---|
| Per-turn accuracy | 23.6% | **69.1%** | **+45.4pp** |
| Full-conversation accuracy | **0.0%** | **37.5%** | **+37.5pp** |
| Intent accuracy | 45.5% | 70.9% | +25.5pp |
| Latency p50 | 27.7s | 33.4s | +5.7s |
| Tokens per turn | 661 + 91 | 5,450 + 194 | ~8× prompt |

**The baseline completed zero of 16 conversations correctly.** It answers about
one turn in four and never holds a session.

### Where the gap actually comes from

Trap handling, counted by how many generated queries contain the necessary
clause:

| Trap | Baseline | Final |
|---|---|---|
| Sentinel exclusion (`-273, -99, 500, 999.9`) | **0 turns** | 11 turns |
| Per-hectare division (`area_hectares`) | 2 turns | 16 turns |
| De-duplication (`DISTINCT ON`) | **0 turns** | 5 turns |

The baseline never once excluded a sentinel reading or de-duplicated a sensor
row. Every aggregate it produced over `sensor_reading` was wrong — and wrong in
a way that looks entirely reasonable, which is the failure mode the brief calls
the worst possible for this product.

| Category | Baseline | Final |
|---|---|---|
| unanswerable | 1/6 (17%) | **6/6 (100%)** |
| correction | 0/1 | 1/1 (100%) |
| ambiguous | 0/1 | 1/1 (100%) |
| first_turn | 4/13 (31%) | 11/13 (85%) |
| follow_up | 8/34 (24%) | 19/34 (56%) |

**Refusal is where the engineering matters most**, 17% → 100%. Without the
catalog and the validator the baseline invents columns rather than declining.
It also produced 9 turns of outright error, having no repair loop to recover
from its own invalid SQL.

The cost is roughly 8× the prompt tokens and 2.5× the latency. On this task
that trade is not close.

### Reproducibility

Both sides of this comparison were run twice on separate occasions with the
server restarted between them. Every headline metric reproduced exactly:

| Configuration | Run 1 | Run 2 |
|---|---|---|
| Final — per-turn / full-conv / intent | 69.1% / 37.5% / 70.9% | 69.1% / 37.5% / 70.9% |
| Baseline — per-turn / full-conv / intent | 23.6% / 0.0% / 45.5% | 23.6% / 0.0% / 45.5% |

For the final configuration a turn-by-turn diff was run: **0 correctness flips
and 0 turns with different SQL text across all 55 turns.** Not merely the same
score by coincidence — byte-identical queries.

This holds despite the repair loop escalating temperature (0 → 0.35 → 0.7) on
retries, because the escalated samples still run under a fixed seed. The
7.3% of turns that retry are as reproducible as the greedy ones.

---

## 5. Why each model was in the roster

- **qwen2.5-coder-7b** — strongest open coder model in class for text-to-SQL.
  The expected winner, and it won.
- **qwen3-8b** — newer generation at the parameter ceiling. Untested: does not
  fit on 8 GB.
- **llama-3.1-8b** — general-purpose reference point. Untested: does not fit.
- **mistral-7b-v0.3** — older, leaner general model, to separate recent
  training from scaffolding. Ran, but too slow to be usable here.
- **qwen2.5-coder-3b** — speed and size floor. Answers the question the brief
  implies: going 3B → 7B buys 47 percentage points on this task.
- **sqlcoder-7b-2** — SQL specialist, included to test whether specialism beats
  conversational ability. It could not run the pipeline at all.

---

## 6. Reproducing

```bash
make bakeoff                      # every model, full set
uv run python eval/bakeoff.py --limit 5          # screening
uv run python eval/bakeoff.py --only qwen2.5-coder-7b
uv run python model/preflight.py --all           # what fits on this machine
```

Raw per-run JSON, including every generated query and every rejected attempt,
is in `eval/reports/`. Runtime detail is in
[`MODEL_RUNTIME.md`](MODEL_RUNTIME.md).
