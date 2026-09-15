# Evaluation conversation format

The brief refers to five provided examples establishing a format. No example
set was included with the materials supplied, so this format is defined here
and every conversation in `eval/conversations/` follows it.

## File

One YAML file per conversation, 3–5 turns each.

```yaml
id: yield-belgaum-drilldown          # unique, kebab-case, matches the filename
title: Yield in Belgaum, then Dharwad, then by crop
tags: [yield, district, reference-resolution]
turns:
  - user: Average yield per hectare in Belgaum for kharif 2025
    category: first_turn
    expect: answer
    intent: NEW_TOPIC
    gold_sql: |
      SELECT avg(c.actual_yield / p.area_hectares) AS yield_per_ha
      FROM agri.crop_cycle c
      JOIN agri.plot p ON p.id = c.plot_id
      WHERE p.district = 'Belgaum' AND c.season = 'kharif'
        AND c.sown_date >= DATE '2025-01-01' AND c.sown_date < DATE '2026-01-01'
        AND c.actual_yield IS NOT NULL
```

## Turn fields

| Field | Required | Meaning |
|---|---|---|
| `user` | yes | Exactly what the user types |
| `category` | yes | One of `first_turn`, `follow_up`, `correction`, `ambiguous`, `unanswerable`. These are the categories the brief asks to be reported separately |
| `expect` | yes | The correct outcome: `answer`, `refuse` or `clarify` |
| `intent` | yes | The correct classification: `NEW_TOPIC`, `REFINE`, `REFERENCE`, `CORRECT`, `META`. Scored separately from SQL accuracy so a conversation failure can be attributed |
| `gold_sql` | when `expect: answer` | The reference query. Correctness is judged by comparing its **result set** against the agent's, never its text |
| `note` | no | Why this turn is here — what it is testing |

## Scoring

- **Execution accuracy.** Gold SQL and generated SQL both run; the result sets
  are compared by `eval/compare.py`. Column names are ignored, row order is
  ignored unless the gold query has `ORDER BY`, numbers compare within a
  relative tolerance of 1e-6. String comparison of SQL is never used.
- **Refusals and clarifications are scored outcomes, not errors.** A refusal on
  an `unanswerable` turn is correct; a refusal on an answerable turn is a miss.
  A clarification is correct only where `expect: clarify`.
- **Per-turn accuracy** is the fraction of all turns correct.
  **Full-conversation accuracy** is the fraction of conversations in which
  *every* turn is correct — the number that reflects whether the agent can hold
  a session rather than answer a question.
- **Intent accuracy** is reported separately. A turn can produce the right SQL
  with the wrong intent label, and that distinction is what tells us whether a
  failure is in conversation handling or in SQL generation.

## Authoring rules

1. Every `gold_sql` must run against the loaded database. `make eval-verify`
   executes all of them and fails on any error.
2. Gold SQL applies the catalog's business rules — per-hectare division,
   sentinel exclusion, de-duplication, the plot-versus-farmer district choice.
   A gold query that falls into a trap would score the agent's correct answer
   as wrong.
3. A conversation must exercise a conversational behaviour, not just carry four
   unrelated questions. At minimum the set covers reference resolution,
   refinement versus reset, correction, clarification-then-reuse, and bounded
   context.
4. Questions are written the way an operations person would type them —
   fragments, lower case and mid-conversation shorthand included.

## Disclosure

These conversations and their gold SQL were authored with the help of a
frontier model (Claude) used **offline**, which the brief permits and requires
to be disclosed. No hosted model is used at inference time.
