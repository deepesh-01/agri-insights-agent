"""Conversation state: what is carried forward, what is discarded, how we decide.

The brief asks for that reasoning explicitly, so it is stated here rather than
left implicit in the code.

CARRIED
  - Slots derived from the last validated SQL (see slots.py): the metric, the
    entity filters, the time filters and the grouping. Derived from the AST, so
    they are always what actually ran.
  - A bounded window of recent turns as (question, sql, row_count). Never the
    result rows: rows are expensive in tokens and almost never needed to
    resolve the next question.
  - Answered clarifications, for the whole session. This is the one thing that
    deliberately outlives a topic change, because the brief requires that a
    clarification once answered is never asked again.

DISCARDED
  - Everything slot-shaped, the moment the turn is classified NEW_TOPIC. This
    is the mechanism that stops turn one leaking into turn six.
  - Turns older than the window (default 3). Dropped, not summarised: a summary
    of a stale turn is a confident-looking source of contamination.
  - Any slot older than `slot_max_age_turns` (default 5), even without a topic
    change, so staleness is bounded by age as well as by topic.

HOW WE DECIDE
  - A small constrained classification call labels each turn NEW_TOPIC, REFINE,
    REFERENCE, CORRECT or META, and the label selects the policy below. It is a
    model call rather than keyword matching because "Only irrigated plots" and
    "How many field agents do we have?" are not separable by keywords -- the
    first is a fragment that only means something as an addition to the
    previous question, and no word in it signals that.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

from server.conversation.slots import Slots, derive

Intent = Literal["NEW_TOPIC", "REFINE", "REFERENCE", "CORRECT", "META"]

# Per intent: does the generator see prior context, and do slots survive?
POLICY: dict[str, dict[str, bool]] = {
    "NEW_TOPIC": {"keep_slots": False, "show_history": False, "amend": False},
    "REFINE":    {"keep_slots": True,  "show_history": True,  "amend": True},
    "REFERENCE": {"keep_slots": True,  "show_history": True,  "amend": True},
    "CORRECT":   {"keep_slots": True,  "show_history": True,  "amend": True},
    "META":      {"keep_slots": True,  "show_history": True,  "amend": False},
}


@dataclass
class Turn:
    index: int
    question: str
    intent: Intent | None = None
    action: str = ""          # sql | refuse | clarify | error
    sql: str | None = None
    row_count: int | None = None
    answer: str = ""
    attempts: int = 1
    created_at: float = field(default_factory=time.time)

    def to_json(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "question": self.question,
            "intent": self.intent,
            "action": self.action,
            "sql": self.sql,
            "row_count": self.row_count,
            "answer": self.answer,
            "attempts": self.attempts,
        }


@dataclass
class PendingClarification:
    question: str
    asked_at_turn: int
    original_message: str


@dataclass
class ConversationState:
    conversation_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    turn_index: int = 0
    turns: list[Turn] = field(default_factory=list)

    slots: Slots = field(default_factory=Slots)
    slots_set_at_turn: int = 0
    last_sql: str | None = None

    # Survives topic changes by design; see module docstring.
    answered_clarifications: dict[str, str] = field(default_factory=dict)
    pending_clarification: PendingClarification | None = None

    turn_window: int = 3
    slot_max_age_turns: int = 5

    # ------------------------------------------------------------- lifecycle --
    def begin_turn(self, question: str) -> Turn:
        self.turn_index += 1
        turn = Turn(index=self.turn_index, question=question)
        self.turns.append(turn)
        self._evict_stale_slots()
        return turn

    def apply_intent(self, intent: Intent) -> dict[str, bool]:
        """Apply the retention policy for `intent`. Returns the policy used."""
        policy = POLICY.get(intent, POLICY["NEW_TOPIC"])
        if not policy["keep_slots"]:
            self.clear_slots()
        return policy

    def clear_slots(self) -> None:
        """Full reset of query state. Clarifications deliberately survive."""
        self.slots = Slots()
        self.slots_set_at_turn = 0
        self.last_sql = None

    def _evict_stale_slots(self) -> None:
        if self.slots.is_empty():
            return
        age = self.turn_index - self.slots_set_at_turn
        if age > self.slot_max_age_turns:
            self.clear_slots()

    def record_sql(self, sql: str) -> None:
        """A query ran and validated: it becomes the state."""
        self.slots = derive(sql)
        self.slots_set_at_turn = self.turn_index
        self.last_sql = sql

    # --------------------------------------------------------- clarification --
    def ask_clarification(self, question: str, original_message: str) -> None:
        self.pending_clarification = PendingClarification(
            question=question,
            asked_at_turn=self.turn_index,
            original_message=original_message,
        )

    def resolve_clarification(self, answer: str) -> str | None:
        """Fold the user's answer into session memory. Returns the merged text."""
        if self.pending_clarification is None:
            return None
        pending = self.pending_clarification
        self.answered_clarifications[pending.question] = answer
        self.pending_clarification = None
        return f"{pending.original_message} ({answer})"

    def clarifications_block(self) -> str:
        if not self.answered_clarifications:
            return ""
        lines = [
            f"- asked: {q}\n  answered: {a}"
            for q, a in self.answered_clarifications.items()
        ]
        return (
            "Already settled earlier in this conversation. Apply these without "
            "asking again:\n" + "\n".join(lines)
        )

    # ---------------------------------------------------------------- prompt --
    def recent_turns(self) -> list[Turn]:
        return self.turns[-(self.turn_window + 1) : -1]

    def history_block(self) -> str:
        turns = self.recent_turns()
        if not turns:
            return "(this is the first turn)"
        out = []
        for turn in turns:
            line = f"Turn {turn.index} - user asked: {turn.question}"
            if turn.sql:
                line += f"\n  SQL: {' '.join(turn.sql.split())}"
                line += f"\n  returned {turn.row_count} rows"
            elif turn.action:
                line += f"\n  outcome: {turn.action}"
            out.append(line)
        return "\n".join(out)

    def state_summary(self) -> str:
        return self.slots.describe()

    # ------------------------------------------------------------------ json --
    def to_json(self) -> dict[str, Any]:
        """The state inspector payload. Shows exactly what the agent carries."""
        return {
            "conversation_id": self.conversation_id,
            "turn_index": self.turn_index,
            "slots": self.slots.to_json(),
            "slots_set_at_turn": self.slots_set_at_turn,
            "slots_age_turns": (
                self.turn_index - self.slots_set_at_turn
                if not self.slots.is_empty() else None
            ),
            "last_sql": self.last_sql,
            "answered_clarifications": self.answered_clarifications,
            "pending_clarification": (
                self.pending_clarification.question
                if self.pending_clarification else None
            ),
            "window": {
                "turn_window": self.turn_window,
                "turns_visible": [t.index for t in self.recent_turns()],
                "turns_total": len(self.turns),
            },
        }


class ConversationStore:
    """In-memory conversation store. One process, one session -- no persistence
    layer, because nothing in the brief asks for conversations to outlive a run
    and a database table here would be scaffolding without a user."""

    def __init__(self, turn_window: int = 3, slot_max_age_turns: int = 5) -> None:
        self._store: dict[str, ConversationState] = {}
        self.turn_window = turn_window
        self.slot_max_age_turns = slot_max_age_turns

    def get(self, conversation_id: str | None) -> ConversationState:
        if conversation_id and conversation_id in self._store:
            return self._store[conversation_id]
        state = ConversationState(
            turn_window=self.turn_window,
            slot_max_age_turns=self.slot_max_age_turns,
        )
        if conversation_id:
            state.conversation_id = conversation_id
        self._store[state.conversation_id] = state
        return state

    def reset(self, conversation_id: str) -> None:
        self._store.pop(conversation_id, None)
