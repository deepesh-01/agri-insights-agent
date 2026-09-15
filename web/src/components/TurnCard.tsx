import { useState } from "react";
import type { TurnResponse } from "../types";
import { ResultTable } from "./ResultTable";

const INTENT_LABEL: Record<string, string> = {
  NEW_TOPIC: "new topic",
  REFINE: "refinement",
  REFERENCE: "follow-up",
  CORRECT: "correction",
  META: "meta",
};

export function TurnCard({ turn }: { turn: TurnResponse }) {
  const [showSql, setShowSql] = useState(turn.action === "answer");
  const [showAttempts, setShowAttempts] = useState(false);

  return (
    <div className={`turn turn-${turn.action}`}>
      <div className="badges">
        <span className={`tag tag-${turn.action}`}>
          {turn.action === "answer" ? "answered"
            : turn.action === "refuse" ? "refused"
            : turn.action === "clarify" ? "needs clarification"
            : "failed"}
        </span>
        <span className="tag tag-intent">{INTENT_LABEL[turn.intent] ?? turn.intent}</span>
        {turn.retried && (
          <button className="tag tag-retry" onClick={() => setShowAttempts((v) => !v)}>
            retried {turn.attempts - 1}×
          </button>
        )}
        {turn.truncated && <span className="tag tag-note">truncated</span>}
        <span className="tag tag-timing">
          {(turn.timings_ms.total / 1000).toFixed(1)}s
          {turn.tokens.prompt > 0 && ` · ${turn.tokens.prompt + turn.tokens.completion} tok`}
        </span>
      </div>

      <p className="answer">{turn.answer}</p>

      {turn.resolved_question !== turn.question && (
        <p className="resolved muted small">
          Understood as: <em>{turn.resolved_question}</em>
        </p>
      )}

      {turn.assumptions.length > 0 && (
        <div className="assumptions">
          <strong>Assumptions</strong>
          <ul>
            {turn.assumptions.map((a) => <li key={a}>{a}</li>)}
          </ul>
        </div>
      )}

      {showAttempts && turn.attempt_detail.length > 1 && (
        <div className="attempts">
          <strong>Attempts</strong>
          {turn.attempt_detail.map((attempt, index) => (
            <div key={index} className={`attempt attempt-${attempt.verdict}`}>
              <span className="muted small">
                #{index + 1} {attempt.verdict}
                {attempt.error_code && ` (${attempt.error_code})`}
              </span>
              <pre>{attempt.sql}</pre>
              {attempt.error && <p className="error small">{attempt.error}</p>}
            </div>
          ))}
        </div>
      )}

      {turn.sql && (
        <div className="sql-block">
          <button className="link" onClick={() => setShowSql((v) => !v)}>
            {showSql ? "hide SQL" : "show SQL"}
          </button>
          {showSql && <pre className="sql">{turn.sql}</pre>}
        </div>
      )}

      {turn.action === "answer" && (
        turn.row_count > 0 ? (
          <ResultTable columns={turn.columns} rows={turn.rows} />
        ) : (
          <p className="muted empty-result">
            The query ran and returned no rows.
            {turn.coverage_note && " This period is outside the data's coverage."}
          </p>
        )
      )}
    </div>
  );
}
