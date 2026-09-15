export type Action = "answer" | "refuse" | "clarify" | "error";

export interface Column {
  name: string;
  type: string;
}

export interface Slots {
  metric: string | null;
  entity_filters: Record<string, string>;
  time_filters: string[];
  grouping: string[];
  tables: string[];
}

export interface AgentState {
  conversation_id: string;
  turn_index: number;
  slots: Slots;
  slots_set_at_turn: number;
  slots_age_turns: number | null;
  last_sql: string | null;
  answered_clarifications: Record<string, string>;
  pending_clarification: string | null;
  window: { turn_window: number; turns_visible: number[]; turns_total: number };
}

export interface AttemptDetail {
  sql: string;
  verdict: string;
  error_code: string | null;
  error: string | null;
}

export interface TurnResponse {
  conversation_id: string;
  turn_index: number;
  question: string;
  resolved_question: string;
  intent: string;
  action: Action;
  answer: string;
  sql: string | null;
  assumptions: string[];
  attempts: number;
  attempt_detail: AttemptDetail[];
  retried: boolean;
  coverage_note: string | null;
  warnings: string[];
  timings_ms: Record<string, number>;
  tokens: Record<string, number>;
  state: AgentState;
  columns: Column[];
  rows: (string | number | null)[][];
  row_count: number;
  truncated: boolean;
}

export interface CoverageSpan {
  min: string;
  max: string;
  forward_looking?: boolean;
  rows_in_future?: number;
}

export interface SchemaResponse {
  catalog_version: number;
  schema: string;
  latest_observation_date: string;
  data_coverage: Record<string, CoverageSpan>;
  forward_looking_columns: string[];
  tables: Record<string, {
    rows: number;
    description: string | null;
    columns: Record<string, {
      type: string;
      nullable: boolean;
      nulls: number | null;
      values: string[] | null;
      description: string | null;
    }>;
  }>;
  business_rules: { id: string; text: string }[];
}

export type ChatItem =
  | { kind: "user"; text: string }
  | { kind: "turn"; data: TurnResponse }
  | { kind: "pending"; text: string }
  | { kind: "failed"; text: string };
