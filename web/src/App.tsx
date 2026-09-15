import { useEffect, useRef, useState } from "react";
import { api } from "./api";
import type { ChatItem, SchemaResponse, TurnResponse } from "./types";
import { CoverageBanner } from "./components/CoverageBanner";
import { StateInspector } from "./components/StateInspector";
import { TurnCard } from "./components/TurnCard";

const SUGGESTIONS = [
  "average yield per hectare in Belgaum for kharif 2025",
  "how many advisories have never been acknowledged?",
  "total rainfall recorded per district",
  "which farmers have the highest credit score?",
];

export default function App() {
  const [items, setItems] = useState<ChatItem[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [schema, setSchema] = useState<SchemaResponse | null>(null);
  const [health, setHealth] = useState<"checking" | "ok" | "down">("checking");
  const [conversationId, setConversationId] = useState<string | null>(null);
  const endRef = useRef<HTMLDivElement>(null);

  const lastTurn = [...items].reverse().find((i) => i.kind === "turn") as
    | { kind: "turn"; data: TurnResponse }
    | undefined;

  useEffect(() => {
    api.schema().then(setSchema).catch(() => setSchema(null));
    api.health()
      .then((h) => setHealth(h.ok ? "ok" : "down"))
      .catch(() => setHealth("down"));
  }, []);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [items]);

  async function send(message: string) {
    const text = message.trim();
    if (!text || busy) return;
    setInput("");
    setBusy(true);
    setItems((prev) => [
      ...prev,
      { kind: "user", text },
      { kind: "pending", text: "Working on it" },
    ]);
    try {
      const data = await api.ask(text, conversationId);
      setConversationId(data.conversation_id);
      setItems((prev) => [...prev.slice(0, -1), { kind: "turn", data }]);
    } catch (error) {
      setItems((prev) => [
        ...prev.slice(0, -1),
        {
          kind: "failed",
          text:
            error instanceof Error
              ? `Could not reach the agent: ${error.message}`
              : "Could not reach the agent.",
        },
      ]);
    } finally {
      setBusy(false);
    }
  }

  async function reset() {
    if (conversationId) await api.reset(conversationId).catch(() => undefined);
    setConversationId(null);
    setItems([]);
  }

  return (
    <div className="app">
      <header className="topbar">
        <div>
          <h1>Agri Insights</h1>
          <p className="sub">Ask about farmers, plots, crops, sensors, advisories and visits.</p>
        </div>
        <div className="topbar-right">
          <span className={`health health-${health}`}>
            {health === "ok" ? "agent ready" : health === "down" ? "agent unreachable" : "checking"}
          </span>
          <button className="ghost" onClick={reset} disabled={busy || items.length === 0}>
            New conversation
          </button>
        </div>
      </header>

      <div className="body">
        <main className="chat">
          {schema && <CoverageBanner schema={schema} />}

          {items.length === 0 && (
            <div className="empty">
              <p>No questions yet. Try one of these:</p>
              <ul>
                {SUGGESTIONS.map((s) => (
                  <li key={s}>
                    <button className="suggestion" onClick={() => send(s)} disabled={busy}>
                      {s}
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          )}

          {items.map((item, index) => {
            if (item.kind === "user") {
              return (
                <div className="bubble user" key={index}>
                  {item.text}
                </div>
              );
            }
            if (item.kind === "pending") {
              return (
                <div className="bubble pending" key={index}>
                  <span className="dots"><i /><i /><i /></span>
                  {item.text} — a local 7B model takes a few seconds per turn.
                </div>
              );
            }
            if (item.kind === "failed") {
              return (
                <div className="bubble failed" key={index}>
                  {item.text}
                </div>
              );
            }
            return <TurnCard key={index} turn={item.data} />;
          })}
          <div ref={endRef} />
        </main>

        <aside className="side">
          <StateInspector state={lastTurn?.data.state ?? null} />
        </aside>
      </div>

      <form
        className="composer"
        onSubmit={(event) => {
          event.preventDefault();
          void send(input);
        }}
      >
        <input
          value={input}
          onChange={(event) => setInput(event.target.value)}
          placeholder={
            lastTurn?.data.action === "clarify"
              ? "Answer the question above…"
              : "Ask a question about the data…"
          }
          disabled={busy}
          autoFocus
        />
        <button type="submit" disabled={busy || !input.trim()}>
          {busy ? "Thinking…" : "Ask"}
        </button>
      </form>
    </div>
  );
}
