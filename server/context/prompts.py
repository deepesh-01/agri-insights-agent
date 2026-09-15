"""Load versioned prompt artifacts from server/prompts/.

Prompts are files, not f-strings in code. Each carries YAML front matter with
an id and a version, and every audit row records the (id, version) pairs that
produced it -- so any evaluation number can be traced back to the exact prompt
text that generated it, and a prompt change that moves the numbers is visible
rather than invisible.
"""

from __future__ import annotations

import functools
import pathlib
import string
from dataclasses import dataclass

import yaml

PROMPT_DIR = pathlib.Path(__file__).resolve().parent.parent / "prompts"


@dataclass(frozen=True)
class Prompt:
    id: str
    version: int
    purpose: str
    body: str

    @property
    def stamp(self) -> str:
        return f"{self.id}@v{self.version}"

    def render(self, **values: object) -> str:
        out = self.body
        for key, value in values.items():
            out = out.replace("{" + key + "}", str(value))
        return out.strip()


def _split_front_matter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        raise ValueError("prompt file is missing YAML front matter")
    _, meta, body = text.split("---", 2)
    return yaml.safe_load(meta), body


@functools.lru_cache(maxsize=None)
def load(name: str) -> Prompt:
    path = PROMPT_DIR / f"{name}.md"
    meta, body = _split_front_matter(path.read_text(encoding="utf-8"))
    return Prompt(
        id=meta["id"],
        version=int(meta["version"]),
        purpose=str(meta.get("purpose", "")).strip(),
        body=body,
    )


def versions() -> dict[str, int]:
    """Every prompt id -> version. Recorded in eval reports and audit rows."""
    out = {}
    for path in sorted(PROMPT_DIR.glob("*.md")):
        meta, _ = _split_front_matter(path.read_text(encoding="utf-8"))
        out[meta["id"]] = int(meta["version"])
    return out
