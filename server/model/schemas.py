"""JSON schemas that constrain model output.

llama-server compiles each of these into a GBNF grammar, so the model is
physically unable to emit a malformed object. That removes an entire class of
failure (unparseable output) from the bake-off, which matters because the
weaker candidates are exactly the ones that would otherwise fail on formatting
rather than on reasoning -- and formatting is not what is being measured.
"""

GENERATE_SQL_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["sql", "refuse", "clarify"]},
        "sql": {"type": "string"},
        "assumptions": {"type": "array", "items": {"type": "string"}, "maxItems": 5},
        "reason": {"type": "string"},
        "question": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": ["action"],
    "additionalProperties": False,
}

CLASSIFY_INTENT_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {
            "type": "string",
            "enum": ["NEW_TOPIC", "REFINE", "REFERENCE", "CORRECT", "META"],
        },
        "replaces": {
            "type": "array",
            "items": {
                "type": "string",
                "enum": ["metric", "entity_filters", "time_filters", "grouping"],
            },
        },
        "rationale": {"type": "string"},
    },
    "required": ["intent", "replaces"],
    "additionalProperties": False,
}
