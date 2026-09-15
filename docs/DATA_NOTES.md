# Data notes — what profiling found, and why it matters

The brief says the schema "is not simple", and that the places where the
obvious query gives a subtly wrong answer are to be discovered by reading the
schema and the data. This document records what that profiling found, with the
evidence, because every one of these facts ends up in `db/schema.sql` as a
column comment and in `server/catalog.yaml` as a rule the model is given.

The database is loaded **raw**. None of these problems are fixed in the ETL --
fixing them there would delete the exercise.

---

## Shape

```
farmer (300) ──1:N──► plot (450) ──1:N──► crop_cycle (900)
                       │
                       ├──1:N──► sensor_reading (19,778)
                       ├──1:N──► advisory (500)
                       └──1:N──► field_visit (600) ◄──N:1── field_agent (25)
```

`plot` is the hub: every path between two fact tables runs through it. There is
no direct join between `crop_cycle`, `sensor_reading`, `advisory` and
`field_visit`.

Referential integrity is clean — zero orphans and zero nulls across all six
foreign keys, and no duplicate primary keys.

---

## Trap 1 — `expected_yield` and `actual_yield` are in different units

**The single most damaging trap in the schema.**

`expected_yield` is kilograms **per hectare**. `actual_yield` is **total**
kilograms for the plot. Nothing in the column names says so.

Evidence — if the units matched, the ratio of actual to expected would cluster
near 1.0 with low spread:

| Ratio | Median | Std. dev. |
|---|---|---|
| `actual_yield / expected_yield` | 3.394 | 5.645 |
| `(actual_yield / area_hectares) / expected_yield` | **0.989** | **0.262** |

The second row is decisive: dividing by area first makes the relationship
tight and centred on 1. Median `expected_yield` by crop also reads as per-
hectare figures (sunflower 1,185; wheat 3,036; sugarcane 77,631).

**Consequence** — "how many cycles came in below expectation":

```sql
-- naive: 123 rows
WHERE c.actual_yield < c.expected_yield

-- correct: 387 rows
WHERE c.actual_yield / p.area_hectares < c.expected_yield
```

A three-fold error, and the naive version looks entirely reasonable.

---

## Trap 2 — sentinel error codes in `sensor_reading.value`

Four sentinel values are written by faulty sensors: **-273, -99, 500, 999.9**.
79 rows in total (0.40%).

| reading_type | raw mean | clean mean | plausible range once excluded |
|---|---|---|---|
| temperature | 29.12 | **27.70** | 16.53 – 41.04 °C |
| humidity | 56.79 | 56.29 | 27.21 – 85.90 % |
| soil_moisture | 36.04 | 35.84 | 13.97 – 54.79 % |
| rainfall | 13.14 | 12.26 | 0 – 76.38 mm |

`rainfall = 0` occurs 3,545 times and is a genuine dry-hour reading, not an
error — it must not be filtered out with the sentinels.

---

## Trap 3 — duplicate sensor readings

386 `(plot_id, reading_type, recorded_at)` keys appear twice. The structure is
not random:

- **All 79 sentinel rows are the duplicate half of a pair** whose other half is
  a good reading.
- The remaining 307 duplicate pairs are **exact copies** — median absolute
  difference 0.00.

So excluding sentinels resolves the harmful duplication, but plain copies still
inflate any `sum` or `count`.

**Consequence** — total rainfall across all plots:

```
naive sum:                     64,934.7 mm
sentinel-excluded, de-duped:   56,020.0 mm     (a 14% overcount)
```

Averages move slightly; sums move a lot.

---

## Trap 4 — two different `district` columns

`farmer.district` is where the farmer registered. `plot.district` is where the
land is. **They disagree for 91 of 450 plots** (20%).

"Average yield in Belgaum":

| Basis | Cycles | Avg kg/ha |
|---|---|---|
| `plot.district = 'Belgaum'` | 67 | 8,134 |
| `farmer.district = 'Belgaum'` | 75 | **11,767** |

A 45% difference from one join choice. This is the schema's one genuine
ambiguity, and it is why the agent asks a clarifying question rather than
picking silently.

The same split exists for agents: `field_agent.district` differs from the
visited plot's district for **159 of 600 visits**, so "coverage in district X"
is ambiguous in exactly the same way.

---

## Trap 5 — nulls that carry meaning

| Column | Nulls | Meaning |
|---|---|---|
| `crop_cycle.actual_yield` | 153 | not yet harvested |
| `crop_cycle.harvest_date` | 153 | same rows |
| `advisory.acknowledged_at` | 208 | never acknowledged — this *is* the definition |
| `field_visit.notes` | 155 | no note written |

`AVG` and `SUM` skip nulls silently, so a yield figure computed over
`crop_cycle` covers harvested cycles only whether or not anyone says so. The
agent is required to say so.

---

## Trap 6 — partial sensor coverage

Only **202 of 450 plots** have any sensor readings at all (median ~98 readings
per instrumented plot, four reading types each). An average "per plot" over
instrumented plots is a different population from an average over all plots.

---

## Trap 7 — `field_visit.outcome` includes visits that did not happen

| outcome | rows |
|---|---|
| completed | 434 |
| rescheduled | 51 |
| farmer_absent | 47 |
| partial | 45 |
| cancelled | 23 |

Counting all 600 rows as "visits" overstates every agent's workload by roughly
a quarter. A visit that actually happened is
`outcome IN ('completed', 'partial')`.

---

## Trap 8 — the data does not reach today, and part of it runs past today

Coverage differs per column, and `crop_cycle.sown_date` contains **59 rows
dated after today** (planned sowings, all with status `P`).

| Column | From | To |
|---|---|---|
| `sensor_reading.recorded_at` | 2025-06-26 | **2026-04-23** |
| `crop_cycle.harvest_date` | 2023-05-12 | 2026-07-13 |
| `advisory.issued_at` | 2024-06-13 | 2026-08-18 |
| `advisory.acknowledged_at` | 2024-06-17 | **2026-08-21** |
| `field_visit.visited_at` | 2024-03-04 | 2026-08-19 |
| `crop_cycle.sown_date` | 2023-02-04 | 2026-11-28 *(future-dated)* |
| `farmer.registered_on` | 2019-01-06 | 2025-12-16 |
| `field_agent.joined_on` | 2020-02-07 | 2025-12-25 |

A single "latest data date" would be misleading in both directions, so the
catalog computes `latest_observation_date` (2026-08-21) while flagging
forward-looking columns separately. The classification is derived from the data
— a column with rows after `CURRENT_DATE` — not from a hand-maintained list.

Also worth knowing: **kharif 2026 has 61 cycles and zero harvested**. "Average
yield last kharif" therefore returns nothing, and the agent must explain that
rather than report a null as an answer.

---

## `crop_cycle.status` — left deliberately opaque

Values and counts: **A** 33, **F** 48, **H** 699, **P** 120.

No expansion for these letters is documented anywhere, so the catalog records
only what is observable and forbids the model from inventing meanings:

- A and P rows **always** have NULL `harvest_date` and NULL `actual_yield`.
- F and H rows **always** have both.
- All 59 future-dated sowings are status P.

For the record, and deliberately **not** written into the catalog: F rows have
a median `actual_per_ha / expected` of 0.155 (max 0.249) against H's 1.013. The
letters are strongly inferable. Inferable is not documented, and a confident
wrong expansion in a user-facing answer is exactly the failure mode the brief
warns about.

---

## Season sanity check

Sowing months line up with the seasons as expected, so no trap here:

| season | sowing months |
|---|---|
| kharif | June (168), July (174) |
| rabi | October (235), November (225) |
| summer | February (54), March (44) |

---

## Note on the CSV extracts

The provided workbooks were converted to CSV before loading. The first
conversion had three defects, all fixed: CRLF line endings, rows with trailing
empty fields dropped rather than padded (which made `severity` look like it had
eight distinct values instead of four), and integer ids written as floats
(`1.0`). `CSV/csv/` holds the corrected extracts.
