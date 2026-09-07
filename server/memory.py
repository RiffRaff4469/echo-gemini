"""Jarvis long-term memory (MEMORY-BRIEF-8).

A small JSON store next to the server. The device stays thin; everything here
runs on the PC. Nothing in this module sends data anywhere -- the only path
that leaves the machine is the composed memory block already going into the
Gemini session prompt.

Layout (``server/memory/jarvis.json``, gitignored runtime file):
    {
      "facts":     [{"text": "...", "added": "2026-09-07", "source": "session"}],
      "summaries": [{"text": "...", "added": "2026-09-07"}]
    }

``seed.json`` (committed) supplies the initial facts on first run; the seed is
merged once and never re-applied, so the owner can edit ``jarvis.json`` freely.

V1 keeps extraction explicit and model-driven: the Live model calls the
``remember`` tool when the user states something durable ("my name is...", "I
prefer..."). That avoids a second model call after every session and stores
exactly what the user cared about, not a transcript distillation. Session
summaries are a later pass.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import date
from pathlib import Path

log = logging.getLogger("echo.memory")

# Runtime store (gitignored). Seed file sits next to it and is committed.
_MEMORY_DIR = Path(__file__).resolve().parent / "memory"
_RUNTIME_PATH = _MEMORY_DIR / "jarvis.json"
_SEED_PATH = _MEMORY_DIR / "seed.json"

# Caps from the brief: ~50 facts, ~20 summaries, oldest evicted.
FACT_CAP = 50
SUMMARY_CAP = 20
# Injection budget for the composed memory block inside the system prompt.
BLOCK_LIMIT = 1500

_lock = threading.Lock()
_store: "MemoryStore | None" = None


def get_store() -> "MemoryStore":
    """Process-wide singleton. Single-process server, so this is safe."""
    global _store
    with _lock:
        if _store is None:
            _store = MemoryStore()
        return _store


class MemoryStore:
    def __init__(self, path: Path | None = None, seed: Path | None = None) -> None:
        self.path = Path(path) if path else _RUNTIME_PATH
        self.seed = Path(seed) if seed else _SEED_PATH
        self.facts: list[dict] = []
        self.summaries: list[dict] = []
        self._load()

    # --- persistence ------------------------------------------------------

    def _load(self) -> None:
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self.facts = list(data.get("facts", []))
                self.summaries = list(data.get("summaries", []))
                log.info("memory loaded: %d fact(s), %d summarie(s)",
                         len(self.facts), len(self.summaries))
                return
            except (json.JSONDecodeError, OSError) as exc:
                log.warning("memory store unreadable (%s); starting from seed", exc)
        # First run: apply the committed seed once.
        self._apply_seed()

    def _apply_seed(self) -> None:
        if not self.seed.exists():
            self._save()
            return
        try:
            data = json.loads(self.seed.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("memory seed unreadable (%s)", exc)
            data = {}
        for fact in data.get("facts", []):
            self.facts.append({
                "text": fact["text"],
                "added": fact.get("added", date.today().isoformat()),
                "source": "seed",
            })
        for summary in data.get("summaries", []):
            self.summaries.append({
                "text": summary["text"],
                "added": summary.get("added", date.today().isoformat()),
            })
        self._save()
        log.info("memory seeded with %d fact(s)", len(self.facts))

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"facts": self.facts, "summaries": self.summaries}
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    # --- mutations --------------------------------------------------------

    def add_fact(self, text: str, source: str = "session") -> str:
        """Store a durable fact, deduped against existing ones.

        Returns a short confirmation the model can read out.
        """
        clean = " ".join(text.split())
        if not clean:
            return "Nothing to remember."
        lowered = clean.lower()
        for fact in self.facts:
            if lowered in fact["text"].lower() or fact["text"].lower() in lowered:
                # Refresh the stored wording with the newest phrasing.
                fact["text"] = clean
                fact["added"] = date.today().isoformat()
                self._save()
                return "Already remembered that."
        self.facts.append({
            "text": clean,
            "added": date.today().isoformat(),
            "source": source,
        })
        if len(self.facts) > FACT_CAP:
            # Oldest evicted (insertion order == age order).
            self.facts = self.facts[-FACT_CAP:]
        self._save()
        return f"Remembered: {clean}"

    def add_summary(self, text: str) -> None:
        clean = " ".join(text.split())
        if not clean:
            return
        self.summaries.append({"text": clean, "added": date.today().isoformat()})
        if len(self.summaries) > SUMMARY_CAP:
            self.summaries = self.summaries[-SUMMARY_CAP:]
        self._save()

    def forget(self, needle: str) -> str:
        """Delete facts mentioning ``needle`` (owner/admin cleanup path)."""
        lowered = needle.lower()
        before = len(self.facts)
        self.facts = [f for f in self.facts if lowered not in f["text"].lower()]
        removed = before - len(self.facts)
        if removed:
            self._save()
        return f"Forgot {removed} fact(s) about {needle}." if removed else f"No stored facts mention {needle}."

    # --- injection --------------------------------------------------------

    def compose_block(self, limit: int = BLOCK_LIMIT) -> str:
        """The MEMORY section spliced into the system prompt.

        Recent facts first (they are the ones a current conversation most
        likely needs), then the most recent summaries.
        """
        parts: list[str] = []
        for fact in reversed(self.facts):  # newest first
            parts.append(f"- {fact['text']}")
        for summary in reversed(self.summaries):
            parts.append(f"* (recap) {summary['text']}")
        if not parts:
            return ""
        block = "You have a long-term memory of these things the owner told you:\n" + "\n".join(parts)
        if len(block) <= limit:
            return block
        # Trim oldest first: build from the newest fact backwards.
        trimmed: list[str] = []
        used = 0
        for part in reversed(parts):
            if used + len(part) + 1 > limit - 40:  # keep room for the header
                break
            trimmed.append(part)
            used += len(part) + 1
        if not trimmed:
            trimmed = [parts[0][: limit - 60]]
        header = "You have a long-term memory of these things the owner told you (truncated):\n"
        return header + "\n".join(reversed(trimmed))
