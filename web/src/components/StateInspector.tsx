import type { AgentState } from "../types";

/**
 * Shows what the agent is actually carrying between turns. The slots are
 * derived from the last validated SQL rather than self-reported by the model,
 * so this panel cannot drift from the query that really ran.
 */
export function StateInspector({ state }: { state: AgentState | null }) {
  if (!state) {
    return (
      <div className="inspector">
        <h2>Conversation state</h2>
        <p className="muted">Nothing carried yet — ask a question.</p>
      </div>
    );
  }

  const { slots } = state;
  const empty =
    !slots.metric &&
    Object.keys(slots.entity_filters).length === 0 &&
    slots.time_filters.length === 0 &&
    slots.grouping.length === 0;

  return (
    <div className="inspector">
      <h2>Conversation state</h2>
      <p className="muted small">
        Turn {state.turn_index} · derived from the last validated SQL
      </p>

      {empty ? (
        <p className="muted">
          Nothing carried forward. The last turn was a new topic, or the carried
          state expired.
        </p>
      ) : (
        <dl>
          {slots.metric && (
            <>
              <dt>Measuring</dt>
              <dd><code>{slots.metric}</code></dd>
            </>
          )}
          {Object.keys(slots.entity_filters).length > 0 && (
            <>
              <dt>Filters</dt>
              <dd>
                {Object.entries(slots.entity_filters).map(([key, value]) => (
                  <code key={key} className="chip">{key} = {value}</code>
                ))}
              </dd>
            </>
          )}
          {slots.time_filters.length > 0 && (
            <>
              <dt>Time</dt>
              <dd>{slots.time_filters.map((f) => <code key={f} className="chip">{f}</code>)}</dd>
            </>
          )}
          {slots.grouping.length > 0 && (
            <>
              <dt>Grouped by</dt>
              <dd>{slots.grouping.map((g) => <code key={g} className="chip">{g}</code>)}</dd>
            </>
          )}
          {slots.tables.length > 0 && (
            <>
              <dt>Tables</dt>
              <dd>{slots.tables.join(", ")}</dd>
            </>
          )}
          {state.slots_age_turns !== null && (
            <>
              <dt>Age</dt>
              <dd className="muted">
                set on turn {state.slots_set_at_turn} ({state.slots_age_turns}{" "}
                {state.slots_age_turns === 1 ? "turn" : "turns"} ago)
              </dd>
            </>
          )}
        </dl>
      )}

      {Object.keys(state.answered_clarifications).length > 0 && (
        <>
          <h3>Settled this session</h3>
          <ul className="settled">
            {Object.entries(state.answered_clarifications).map(([question, answer]) => (
              <li key={question}>
                <span className="muted small">{question}</span>
                <strong>{answer}</strong>
              </li>
            ))}
          </ul>
        </>
      )}

      <h3>Context window</h3>
      <p className="muted small">
        Showing {state.window.turns_visible.length} of {state.window.turns_total} turns
        (cap {state.window.turn_window}).
        {state.window.turns_total > state.window.turns_visible.length &&
          " Older turns are dropped, not summarised."}
      </p>
    </div>
  );
}
