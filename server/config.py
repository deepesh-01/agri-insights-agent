"""Runtime configuration. Environment first, sensible local defaults after."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    # Read-only role. Every model-generated query runs as this user.
    agent_dsn: str = os.getenv(
        "AGENT_DSN", "postgresql://agent_ro:agent_ro@localhost/agri_insights"
    )
    # Owner role, used only for catalog introspection at startup.
    admin_dsn: str = os.getenv("ADMIN_DSN", "postgresql:///agri_insights")

    llama_url: str = os.getenv("LLAMA_URL", "http://127.0.0.1:8080")
    model_id: str = os.getenv("MODEL_ID", "qwen2.5-coder-7b")

    statement_timeout_ms: int = int(os.getenv("STATEMENT_TIMEOUT_MS", "5000"))
    max_repair_attempts: int = int(os.getenv("MAX_REPAIR_ATTEMPTS", "2"))
    max_question_chars: int = int(os.getenv("MAX_QUESTION_CHARS", "500"))

    # Conversation state bounds -- see ARCHITECTURE.md section 5.
    turn_window: int = int(os.getenv("TURN_WINDOW", "3"))
    slot_max_age_turns: int = int(os.getenv("SLOT_MAX_AGE_TURNS", "5"))

    # Rows handed to the narration prompt. The full result still goes to the UI.
    narrate_row_cap: int = int(os.getenv("NARRATE_ROW_CAP", "50"))

    audit_path: str = os.getenv("AUDIT_PATH", "audit/audit.jsonl")
    transcript_path: str = os.getenv("TRANSCRIPT_PATH", "audit/transcript.jsonl")


settings = Settings()
