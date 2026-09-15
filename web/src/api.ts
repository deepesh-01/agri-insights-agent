import type { SchemaResponse, TurnResponse } from "./types";

const BASE = import.meta.env.VITE_API_BASE ?? "http://127.0.0.1:8000";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!response.ok) {
    throw new Error(`${response.status} ${response.statusText}`);
  }
  return (await response.json()) as T;
}

export const api = {
  ask: (message: string, conversationId: string | null) =>
    request<TurnResponse>("/api/ask", {
      method: "POST",
      body: JSON.stringify({ message, conversation_id: conversationId }),
    }),
  schema: () => request<SchemaResponse>("/api/schema"),
  health: () => request<{ ok: boolean; model: Record<string, unknown> }>("/api/health"),
  reset: (conversationId: string) =>
    request<{ status: string }>(`/api/conversation/${conversationId}/reset`, {
      method: "POST",
    }),
};
