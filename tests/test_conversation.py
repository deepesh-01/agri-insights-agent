"""Conversation state tests.

These cover the four behaviours the brief names: reference resolution,
refinement versus reset, correction, and bounded context.
"""

from __future__ import annotations

import pytest

from server.conversation.slots import derive
from server.conversation.state import ConversationState, ConversationStore

YIELD_SQL = """
    SELECT p.district, avg(c.actual_yield / p.area_hectares) AS yield_per_ha
    FROM crop_cycle c JOIN plot p ON p.id = c.plot_id
    WHERE c.season = 'kharif' AND p.district = 'Belgaum'
      AND c.actual_yield IS NOT NULL
    GROUP BY p.district
"""

AGENT_SQL = "SELECT count(*) AS agent_count FROM field_agent"


@pytest.fixture
def state() -> ConversationState:
    return ConversationState()


# ------------------------------------------------------------ slot derivation --
def test_slots_come_from_the_sql_that_actually_ran(state):
    state.begin_turn("avg yield in Belgaum last kharif")
    state.record_sql(YIELD_SQL)
    assert state.slots.entity_filters == {
        "c.season": "'kharif'",
        "p.district": "'Belgaum'",
    }
    assert state.slots.grouping == ["p.district"]
    assert "AVG" in state.slots.metric
    assert state.slots.tables == ["crop_cycle", "plot"]


def test_sentinel_exclusion_is_not_reported_as_a_user_filter():
    sql = """
        SELECT avg(value) AS m FROM sensor_reading
        WHERE reading_type = 'soil_moisture'
          AND value NOT IN (-273, -99, 500, 999.9)
    """
    slots = derive(sql)
    assert slots.entity_filters == {"reading_type": "'soil_moisture'"}


def test_derive_never_raises_on_junk():
    assert derive("not sql at all").is_empty()


# ----------------------------------------------------- refinement versus reset --
def test_new_topic_clears_every_slot(state):
    state.begin_turn("avg yield in Belgaum last kharif")
    state.record_sql(YIELD_SQL)
    assert not state.slots.is_empty()

    state.begin_turn("how many field agents do we have?")
    state.apply_intent("NEW_TOPIC")
    assert state.slots.is_empty()
    assert state.last_sql is None


def test_refine_keeps_slots(state):
    state.begin_turn("avg yield in Belgaum last kharif")
    state.record_sql(YIELD_SQL)
    state.begin_turn("only irrigated plots")
    state.apply_intent("REFINE")
    assert state.slots.entity_filters["p.district"] == "'Belgaum'"


@pytest.mark.parametrize("intent", ["REFERENCE", "CORRECT", "REFINE"])
def test_continuation_intents_show_history(state, intent):
    policy = state.apply_intent(intent)
    assert policy["show_history"] is True
    assert policy["keep_slots"] is True


def test_new_topic_hides_history_from_the_generator(state):
    policy = state.apply_intent("NEW_TOPIC")
    assert policy["show_history"] is False


# -------------------------------------------------------------- bounded context --
def test_history_block_is_capped_at_the_window(state):
    state.turn_window = 3
    for i in range(8):
        turn = state.begin_turn(f"question {i}")
        turn.sql = f"SELECT {i} FROM farmer"
        turn.row_count = i
    visible = [t.index for t in state.recent_turns()]
    assert len(visible) == 3
    assert visible == [5, 6, 7]          # the current turn is excluded
    assert "question 0" not in state.history_block()


def test_context_from_six_turns_ago_cannot_leak(state):
    """The brief's explicit requirement, tested end to end on state."""
    state.begin_turn("avg yield in Belgaum")
    state.record_sql(YIELD_SQL)
    for i in range(6):
        state.begin_turn(f"filler {i}")
        state.apply_intent("REFINE")
    # Age eviction fires even though every turn was a continuation.
    assert state.slots.is_empty()
    assert "Belgaum" not in state.history_block()


def test_slots_expire_on_age_even_without_a_topic_change(state):
    state.slot_max_age_turns = 2
    state.begin_turn("avg yield in Belgaum")
    state.record_sql(YIELD_SQL)
    for _ in range(3):
        state.begin_turn("another refinement")
        state.apply_intent("REFINE")
    assert state.slots.is_empty()


# ------------------------------------------------------------------ correction --
def test_correction_replaces_the_slot_and_keeps_the_rest(state):
    state.begin_turn("avg yield in Belgaum last kharif")
    state.record_sql(YIELD_SQL)
    state.begin_turn("no, I meant rabi, not kharif")
    state.apply_intent("CORRECT")
    corrected = YIELD_SQL.replace("'kharif'", "'rabi'")
    state.record_sql(corrected)
    assert state.slots.entity_filters["c.season"] == "'rabi'"
    assert state.slots.entity_filters["p.district"] == "'Belgaum'"


# --------------------------------------------------------------- clarification --
def test_clarification_is_stored_and_survives_a_topic_change(state):
    state.begin_turn("what is the average yield in Belgaum?")
    state.ask_clarification("plots located there, or farmers registered there?",
                            "what is the average yield in Belgaum?")
    merged = state.resolve_clarification("plots located there")
    assert "plots located there" in merged
    assert state.pending_clarification is None

    state.begin_turn("how many field agents?")
    state.apply_intent("NEW_TOPIC")
    assert state.answered_clarifications                  # survived the reset
    assert "plots located there" in state.clarifications_block()


def test_resolving_without_a_pending_question_is_a_no_op(state):
    assert state.resolve_clarification("anything") is None


# ----------------------------------------------------------------- inspector --
def test_state_json_reports_what_is_carried(state):
    state.begin_turn("avg yield in Belgaum last kharif")
    state.record_sql(YIELD_SQL)
    state.begin_turn("what about Dharwad?")
    payload = state.to_json()
    assert payload["slots"]["grouping"] == ["p.district"]
    assert payload["slots_age_turns"] == 1
    assert payload["window"]["turn_window"] == 3


def test_store_isolates_conversations():
    store = ConversationStore()
    a = store.get(None)
    b = store.get(None)
    assert a.conversation_id != b.conversation_id
    assert store.get(a.conversation_id) is a
