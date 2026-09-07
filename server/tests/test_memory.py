"""Tests for server/memory.py (MEMORY-BRIEF-8)."""

from __future__ import annotations

import json

from memory import FACT_CAP, MemoryStore


def _store(tmp_path, seed_facts: list[str] | None = None):
    seed = tmp_path / "seed.json"
    seed.write_text(
        json.dumps({"facts": [{"text": f, "added": "2026-01-01"} for f in (seed_facts or [])]}),
        encoding="utf-8",
    )
    runtime = tmp_path / "jarvis.json"
    return MemoryStore(path=runtime, seed=seed)


def test_first_run_seeds_from_seed_file(tmp_path):
    store = _store(tmp_path, seed_facts=["Jarvis serves Jaiden and Patrick."])
    assert any("Jaiden and Patrick" in f["text"] for f in store.facts)
    assert runtime_path(tmp_path).exists()


def runtime_path(tmp_path):
    return tmp_path / "jarvis.json"


def test_add_fact_persists(tmp_path):
    store = _store(tmp_path)
    result = store.add_fact("Jaiden trains five times a week.")
    assert result.startswith("Remembered")
    assert any("trains five times" in f["text"] for f in store.facts)
    # Reload from disk.
    again = MemoryStore(path=runtime_path(tmp_path), seed=tmp_path / "seed.json")
    assert any("trains five times" in f["text"] for f in again.facts)


def test_dedupe_substring(tmp_path):
    store = _store(tmp_path)
    store.add_fact("Jaiden prefers light blue lights")
    result = store.add_fact("Jaiden prefers light blue lights for the room")
    assert result == "Already remembered that."
    assert len(store.facts) == 1


def test_blank_fact_rejected(tmp_path):
    store = _store(tmp_path)
    assert store.add_fact("   ") == "Nothing to remember."
    assert len(store.facts) == 0


def test_cap_evicts_oldest(tmp_path):
    store = _store(tmp_path)
    for i in range(FACT_CAP + 5):
        store.add_fact(f"unique household fact {i:03d} about nothing in particular")
    assert len(store.facts) == FACT_CAP
    assert not any("fact 000 " in f["text"] for f in store.facts)
    assert any(f"fact {FACT_CAP + 4:03d} " in f["text"] for f in store.facts)


def test_forget_removes_matching(tmp_path):
    store = _store(tmp_path)
    store.add_fact("Patrick has a quiz on Thursday.")
    result = store.forget("quiz")
    assert "1 fact(s)" in result
    assert len(store.facts) == 0
    result = store.forget("quiz")
    assert "No stored facts" in result


def test_compose_block_newest_first(tmp_path):
    store = _store(tmp_path)
    store.add_fact("old fact alpha")
    store.add_fact("new fact beta")
    block = store.compose_block()
    assert block.index("new fact beta") < block.index("old fact alpha")
    assert "long-term memory" in block


def test_compose_block_empty_when_no_facts(tmp_path):
    store = _store(tmp_path)
    assert store.compose_block() == ""


def test_compose_block_truncates_to_limit(tmp_path):
    store = _store(tmp_path)
    for i in range(30):
        store.add_fact(f"a fairly long durable fact about the household number {i} " * 3)
    block = store.compose_block(limit=600)
    assert len(block) <= 700  # header slack
    assert "truncated" in block or len(block) < 600


def test_summaries_capped(tmp_path):
    store = _store(tmp_path)
    for i in range(25):
        store.add_summary(f"session recap {i}")
    assert len(store.summaries) == 20
