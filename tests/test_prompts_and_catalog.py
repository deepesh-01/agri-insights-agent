"""Prompt artifacts and catalog rendering.

Prompts are versioned files whose (id, version) pairs are stamped into every
audit row, so the loader is part of the traceability contract rather than a
convenience. The catalog renderers are tested for the things that would quietly
corrupt the prompt: a free-text column enumerated as an enum, a trap note
dropped, or the context budget blown.
"""

from __future__ import annotations

import pytest

from server.catalog import load as load_catalog
from server.context.prompts import load as load_prompt, versions

PROMPT_IDS = {"generate_sql", "generate_sql_examples", "classify_intent", "narrate"}

# The rendered schema block shares a 6144-token context with the few-shot
# examples, the conversation and the model's own output. This is a budget, not
# a guideline, so it is asserted. It was briefly squeezed to ~940 tokens to fit
# a 4096-token context for 8B models; that cost 5.9 accuracy points and the 8B
# models did not fit on this hardware anyway, so the budget is set where the
# content actually needs to be.
MAX_SCHEMA_CONTEXT_TOKENS = 1600


@pytest.fixture(scope="module")
def catalog():
    return load_catalog()


# ------------------------------------------------------------------ prompts --
def test_every_prompt_loads_with_an_id_and_version():
    found = versions()
    assert set(found) == PROMPT_IDS
    assert all(isinstance(v, int) and v >= 1 for v in found.values())


def test_prompt_stamp_is_traceable():
    prompt = load_prompt("generate_sql")
    assert prompt.stamp == f"generate_sql@v{prompt.version}"


def test_render_substitutes_placeholders():
    rendered = load_prompt("generate_sql").render(
        schema="SCHEMA", keys="KEYS", rules="RULES", coverage="COVERAGE",
        today="2026-09-12", latest_observation="2026-08-21",
    )
    assert "SCHEMA" in rendered and "RULES" in rendered
    assert "2026-09-12" in rendered
    assert "{schema}" not in rendered


def test_render_leaves_json_braces_in_examples_untouched():
    """The examples are full of literal JSON braces; str.format would break."""
    body = load_prompt("generate_sql_examples").body
    assert '{"action":"sql"' in body
    assert '"action":"refuse"' in body
    assert '"action":"clarify"' in body


def test_examples_cover_every_action():
    body = load_prompt("generate_sql_examples").body
    for action in ("sql", "refuse", "clarify"):
        assert f'"action":"{action}"' in body


def test_examples_teach_the_traps():
    body = load_prompt("generate_sql_examples").body
    assert "area_hectares" in body                      # yield units
    assert "-273" in body                               # sentinels
    assert "DISTINCT ON" in body                        # de-duplication
    assert "'completed', 'partial'" in body             # visit definition


def test_narrate_prompt_forbids_inventing_status_meanings():
    body = load_prompt("narrate").body
    assert "Do not invent meanings" in body


# ------------------------------------------------------------------ catalog --
def test_schema_render_stays_within_the_context_budget(catalog):
    blocks = [catalog.render_schema(), catalog.render_keys(),
              catalog.render_rules(), catalog.render_coverage()]
    approx_tokens = sum(len(b) for b in blocks) // 4
    assert approx_tokens < MAX_SCHEMA_CONTEXT_TOKENS, (
        f"schema context grew to ~{approx_tokens} tokens"
    )


def test_free_text_columns_are_not_enumerated(catalog):
    rendered = catalog.render_schema()
    assert "Soil sample collected" not in rendered      # field_visit.notes
    assert "None" not in rendered                       # no leaked NULL repr


def test_enum_domains_are_present_for_real_enums(catalog):
    rendered = catalog.render_schema()
    assert "soil_moisture|temperature|rainfall|humidity" in rendered.replace(
        "temperature|soil_moisture", "soil_moisture|temperature")
    assert "kharif" in rendered and "rabi" in rendered


def test_booleans_render_as_sql_literals(catalog):
    assert "bool{true|false}" in catalog.render_schema()


def test_traps_are_taught_twice_on_purpose(catalog):
    """The worst traps appear in both the DDL comments and the rules block.

    This duplication costs ~250 tokens and is deliberate. Removing it to fit a
    smaller context was measured at 82.4% -> 76.5% on the same conversations,
    so the redundancy is worth more than the tokens it costs. If a future
    change drops one of these renders, this test should fail loudly rather than
    the accuracy quietly sagging.
    """
    schema = catalog.render_schema()
    rules = catalog.render_rules()
    assert "KILOGRAMS PER HECTARE" in schema
    assert "SENTINEL ERROR CODES" in schema
    assert "area_hectares" in rules and "-273" in rules


def test_every_business_rule_reaches_the_prompt(catalog):
    rules = catalog.render_rules()
    for fragment in ("area_hectares", "-273", "DISTINCT ON", "plot.district",
                     "202 of 450", "completed", "rainfall is mm", "A, F, H, P",
                     "kharif 2025"):
        assert fragment in rules, f"missing rule fragment: {fragment}"


def test_foreign_keys_render_readably(catalog):
    assert "crop_cycle.plot_id -> plot.id" in catalog.render_keys()
    assert "field_visit.agent_id -> field_agent.id" in catalog.render_keys()


def test_allowlists_match_the_schema(catalog):
    assert catalog.allowed_tables == {
        "farmer", "plot", "crop_cycle", "sensor_reading",
        "advisory", "field_visit", "field_agent",
    }
    assert ("crop_cycle", "actual_yield") in catalog.qualified_columns
    assert ("farmer", "actual_yield") not in catalog.qualified_columns


def test_coverage_separates_observed_from_forward_looking(catalog):
    assert catalog.raw["forward_looking_columns"] == ["crop_cycle.sown_date"]
    # The latest observation must not be dragged forward by planned sowings.
    assert catalog.latest_observation_date == "2026-08-21"
    assert "future-dated" in catalog.render_coverage()


def test_minimal_render_strips_everything_but_names(catalog):
    minimal = catalog.render_schema_minimal()
    assert "farmer(id, name, district, state, registered_on, is_active)" in minimal
    assert "SENTINEL" not in minimal and "{" not in minimal


# ------------------------------------------------- prompt / eval consistency --
def test_few_shot_examples_do_not_contradict_the_gold_sql():
    """A question answered in the examples must match its gold SQL shape.

    The soil-moisture example answers with two columns while its gold expected
    one, so the model was scored wrong for following instructions precisely -
    and it cost all three turns of that conversation. The harness was measuring
    an internal contradiction, not model quality.
    """
    import pathlib
    import re

    import yaml

    examples = load_prompt("generate_sql_examples").body
    example_sql = {
        q.strip().lower().rstrip("?"): sql
        for q, sql in re.findall(r"Q: (.+?)\nA: .*?\"sql\":\"(.*?)\"", examples)
    }

    for path in sorted(pathlib.Path("eval/conversations").glob("*.yaml")):
        for turn in yaml.safe_load(path.read_text())["turns"]:
            gold = turn.get("gold_sql")
            question = turn["user"].strip().lower().rstrip("?")
            if not gold or question not in example_sql:
                continue
            taught = _projection_count(example_sql[question])
            expected = _projection_count(gold)
            assert taught == expected, (
                f"{path.name}: the few-shot example for {turn['user']!r} returns "
                f"{taught} columns but gold expects {expected}"
            )


def _projection_count(sql: str) -> int:
    """Columns in the outermost SELECT list, ignoring nesting."""
    import sqlglot

    tree = sqlglot.parse_one(sql, dialect="postgres")
    select = tree.find(sqlglot.exp.Select)
    return len(select.expressions) if select else 0
