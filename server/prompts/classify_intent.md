---
id: classify_intent
version: 2
purpose: >
  Decide how a new user turn relates to the conversation so far. This decides
  what conversation state survives, which is the difference between handling
  turn four correctly and leaking turn one into an unrelated question.
consumes: [state_summary, history, message]
produces: json{intent, replaces, rationale}
---
You classify the user's new message against the conversation so far.

# Conversation so far

{history}

# What the agent is currently carrying

{state_summary}

# New message

{message}

# Labels

- `NEW_TOPIC` — a different subject. Nothing from the previous turns applies.
  Example: after several turns about yields, "How many field agents do we
  have?" is NEW_TOPIC.
- `REFINE` — narrows or extends the previous question, keeping its subject.
  Example: "Only irrigated plots".
- `REFERENCE` — the same question asked about something else, or the same
  result cut a different way. It cannot be understood without the previous
  turn. Examples: "What about Dharwad?", "Now break that down by crop".
- `CORRECT` — the user is fixing something they or the agent got wrong, and the
  previous query should be amended rather than restarted.
  Example: "No, I meant rabi, not kharif".
- `META` — about the conversation or the agent itself, not the data.
  Example: "show me that SQL again".

The hard cases are REFINE versus NEW_TOPIC. A message that only makes sense as
an addition to the previous question is REFINE. A message that stands on its
own and shares no subject with the previous question is NEW_TOPIC - even if it
mentions the same table.

In `replaces`, list the slot names the message overrides: any of
`metric`, `entity_filters`, `time_filters`, `grouping`. Empty for NEW_TOPIC.
